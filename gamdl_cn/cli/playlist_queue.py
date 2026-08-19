from __future__ import annotations

import argparse
import asyncio
import importlib
import logging
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import structlog


AMP_API_URL = "https://amp-api.music.apple.com"
SUCCESS_STATUS_CODES = {200, 201, 202, 204}


class QueueError(RuntimeError):
    pass


class QueueMutationError(QueueError):
    pass


@dataclass(frozen=True)
class QueueConfig:
    key: str
    package: str
    command: str
    pending_name: str
    storefront: str
    language: str


@dataclass(frozen=True)
class TrackRef:
    library_id: str
    catalog_id: str


QUEUES = {
    "us": QueueConfig(
        key="us",
        package="gamdl",
        command="gamdl",
        pending_name="US_Pending",
        storefront="us",
        language="en-US",
    ),
    "cn": QueueConfig(
        key="cn",
        package="gamdl_cn",
        command="gamdl_cn",
        pending_name="CN_Pending",
        storefront="cn",
        language="zh-Hans-CN",
    ),
}


def _api_url(path_or_url: str) -> str:
    parsed = urlparse(path_or_url)
    if parsed.scheme:
        if parsed.scheme != "https" or parsed.netloc != "amp-api.music.apple.com":
            raise QueueError("Apple Music pagination returned an unexpected host")
        return path_or_url
    if not path_or_url.startswith("/v1/"):
        raise QueueError("Apple Music pagination returned an unexpected path")
    return AMP_API_URL + path_or_url


async def _request(
    api: Any,
    method: str,
    path_or_url: str,
    *,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
) -> Any:
    response = await api.client.request(
        method,
        _api_url(path_or_url),
        params=params,
        json=json,
    )
    if response.status_code not in SUCCESS_STATUS_CODES:
        safe_path = re.sub(
            r"(/library/playlists/)[^/]+",
            r"\1{playlist_id}",
            urlparse(str(response.request.url)).path,
        )
        raise QueueMutationError(
            f"Apple Music {method} {safe_path} failed with HTTP {response.status_code}"
        )
    if response.status_code == 204 or not response.content:
        return {}
    return response.json()


async def _pages(
    api: Any,
    path: str,
    *,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    next_url: str | None = path
    next_params = params
    page_count = 0
    while next_url:
        page_count += 1
        if page_count > 1000:
            raise QueueError("Apple Music pagination exceeded the safety limit")
        envelope = await _request(api, "GET", next_url, params=next_params)
        items.extend(envelope.get("data") or [])
        next_url = envelope.get("next")
        next_params = None
    return items


async def _list_playlists(api: Any) -> list[dict[str, Any]]:
    return await _pages(
        api,
        "/v1/me/library/playlists",
        params={"limit": 100},
    )


def _resolve_playlist(
    playlists: list[dict[str, Any]],
    name: str,
) -> dict[str, Any]:
    matches = [
        playlist
        for playlist in playlists
        if (playlist.get("attributes") or {}).get("name") == name
    ]
    if len(matches) != 1:
        raise QueueError(
            f'Expected exactly one editable playlist named "{name}", found {len(matches)}'
        )
    playlist = matches[0]
    if (playlist.get("attributes") or {}).get("canEdit") is False:
        raise QueueError(f'Playlist "{name}" is not editable through Apple Music API')
    if not playlist.get("id"):
        raise QueueError(f'Playlist "{name}" has no library identifier')
    return playlist


async def _list_tracks(api: Any, playlist_id: str) -> list[dict[str, Any]]:
    # amp-api returns 404 for the direct /tracks relationship on some empty
    # library playlists. Fetching the playlist with include=tracks works for
    # both empty and populated playlists and still supplies a `next` cursor.
    envelope = await _request(
        api,
        "GET",
        f"/v1/me/library/playlists/{playlist_id}",
        params={"include": "tracks", "limit[tracks]": 100},
    )
    playlist_data = envelope.get("data") or []
    if len(playlist_data) != 1:
        raise QueueError("Apple Music did not return the requested library playlist")
    relationship = (
        ((playlist_data[0].get("relationships") or {}).get("tracks")) or {}
    )
    tracks = list(relationship.get("data") or [])
    next_url = relationship.get("next")
    if next_url:
        tracks.extend(await _pages(api, next_url))
    return tracks


def _catalog_id(track: dict[str, Any]) -> str | None:
    attributes = track.get("attributes") or {}
    play_params = attributes.get("playParams") or {}
    candidates = [play_params.get("catalogId")]
    catalog_data = (
        ((track.get("relationships") or {}).get("catalog") or {}).get("data") or []
    )
    if catalog_data:
        candidates.append(catalog_data[0].get("id"))
    candidates.append(play_params.get("id"))
    for candidate in candidates:
        value = str(candidate or "")
        if re.fullmatch(r"[0-9]+", value):
            return value
    return None


def _track_ref(track: dict[str, Any]) -> TrackRef | None:
    if track.get("type") != "library-songs":
        return None
    library_id = str(track.get("id") or "")
    catalog_id = _catalog_id(track)
    if not library_id or not catalog_id:
        return None
    return TrackRef(library_id=library_id, catalog_id=catalog_id)


def _registered_download(database_path: Path, track: TrackRef) -> Path | None:
    if not database_path.is_file():
        return None
    try:
        with sqlite3.connect(database_path) as connection:
            row = connection.execute(
                "SELECT path FROM media WHERE id IN (?, ?) ORDER BY id = ? DESC LIMIT 1",
                (track.catalog_id, track.library_id, track.catalog_id),
            ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    path = Path(row[0])
    return path if path.is_file() else None


def _media_is_decodable(path: Path) -> bool:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise QueueError("ffprobe is required to verify completed downloads")
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        return False
    try:
        return float(result.stdout.strip()) > 0
    except ValueError:
        return False


async def _download(
    queue: QueueConfig,
    track: TrackRef,
    *,
    cookies_path: Path,
    output_root: Path,
    state_dir: Path,
    timeout: int,
) -> Path:
    database_path = state_dir / f"{queue.key}.sqlite3"
    registered = _registered_download(database_path, track)
    if registered and await asyncio.to_thread(_media_is_decodable, registered):
        return registered

    output_path = output_root / queue.key.upper()
    temp_path = state_dir / "tmp" / queue.key
    output_path.mkdir(parents=True, exist_ok=True)
    temp_path.mkdir(parents=True, exist_ok=True)
    url = f"https://music.apple.com/{queue.storefront}/song/queue/{track.catalog_id}"
    command = [
        queue.command,
        "--no-config-file",
        "--cookies-path",
        str(cookies_path),
        "--language",
        queue.language,
        "--output-path",
        str(output_path),
        "--temp-path",
        str(temp_path),
        "--database-path",
        str(database_path),
        "--song-codec-priority",
        "aac-web",
        "--overwrite",
        "--no-exceptions",
        url,
    ]

    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )

    try:
        result = await asyncio.to_thread(run)
    except subprocess.TimeoutExpired as error:
        raise QueueError(f"{queue.command} download timed out") from error

    (state_dir / f"{queue.key}-last-download.log").write_text(
        result.stdout or "",
        encoding="utf-8",
    )
    registered = _registered_download(database_path, track)
    decodable = bool(
        registered and await asyncio.to_thread(_media_is_decodable, registered)
    )
    if result.returncode != 0 or not registered or not decodable:
        raise QueueError(
            f"{queue.command} did not register a decodable local media file"
        )
    return registered


async def _wait_for_catalog_id(
    api: Any,
    playlist_id: str,
    catalog_id: str,
    *,
    present: bool,
    attempts: int,
    delay: float,
) -> bool:
    for attempt in range(attempts):
        tracks = await _list_tracks(api, playlist_id)
        found = any(_catalog_id(track) == catalog_id for track in tracks)
        if found is present:
            return True
        if attempt + 1 < attempts:
            await asyncio.sleep(delay)
    return False


async def _remove_track(
    api: Any,
    playlist_id: str,
    track: TrackRef,
    *,
    attempts: int,
    delay: float,
) -> None:
    # This is the music.apple.com web player's endpoint. Apple does not document
    # individual playlist-track removal in the public Apple Music API.
    await _request(
        api,
        "DELETE",
        f"/v1/me/library/playlists/{playlist_id}/tracks",
        params={"ids[library-songs]": track.library_id, "mode": "all"},
    )
    if not await _wait_for_catalog_id(
        api,
        playlist_id,
        track.catalog_id,
        present=False,
        attempts=attempts,
        delay=delay,
    ):
        raise QueueMutationError("Removed track still appears in the pending playlist")


async def _create_api(queue: QueueConfig, cookies_path: Path) -> Any:
    api_module = importlib.import_module(f"{queue.package}.api")
    api_class = api_module.AppleMusicApi
    return await api_class.create_from_netscape_cookies(
        cookies_path=str(cookies_path),
        storefront=queue.storefront,
        language=queue.language,
    )


async def _process_queue(
    queue: QueueConfig,
    *,
    cookies_path: Path,
    output_root: Path,
    state_dir: Path,
    dry_run: bool,
    download_timeout: int,
    verify_attempts: int,
    verify_delay: float,
) -> int:
    api = await _create_api(queue, cookies_path)
    label = queue.key.upper()
    try:
        print(f"[{label}] reading playlist queue", flush=True)
        playlists = await _list_playlists(api)
        pending = _resolve_playlist(playlists, queue.pending_name)

        pending_tracks = await _list_tracks(api, pending["id"])
        actionable = [track for item in pending_tracks if (track := _track_ref(item))]
        unsupported = len(pending_tracks) - len(actionable)
        print(
            f"[{label}] pending={len(pending_tracks)} "
            f"actionable={len(actionable)} unsupported={unsupported}"
        )
        if dry_run:
            return unsupported

        failures = unsupported
        for index, track in enumerate(actionable, 1):
            print(f"[{label}] item {index}/{len(actionable)}: checking local download")
            try:
                await _download(
                    queue,
                    track,
                    cookies_path=cookies_path,
                    output_root=output_root,
                    state_dir=state_dir,
                    timeout=download_timeout,
                )
            except QueueError as error:
                failures += 1
                print(f"[{label}] item {index}/{len(actionable)}: download failed: {error}")
                continue

            print(f"[{label}] item {index}/{len(actionable)}: removing from Pending")
            await _remove_track(
                api,
                pending["id"],
                track,
                attempts=verify_attempts,
                delay=verify_delay,
            )
            print(f"[{label}] item {index}/{len(actionable)}: complete")
        return failures
    finally:
        await api.client.aclose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Download songs from US_Pending/CN_Pending and remove each item "
            "after its local download is verified."
        )
    )
    parser.add_argument("--cookies-path", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=Path("/downloads"))
    parser.add_argument("--state-dir", type=Path, default=Path("/state"))
    parser.add_argument(
        "--queue",
        action="append",
        choices=sorted(QUEUES),
        help="Queue to process; repeat for both. Defaults to us and cn.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--download-timeout", type=int, default=3600)
    parser.add_argument("--verify-attempts", type=int, default=6)
    parser.add_argument("--verify-delay", type=float, default=3.0)
    return parser


async def _async_main(args: argparse.Namespace) -> int:
    cookies_path = args.cookies_path.resolve()
    if not cookies_path.is_file():
        raise QueueError(f"Cookies file not found: {cookies_path}")
    output_root = args.output_root.resolve()
    state_dir = args.state_dir.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    queue_keys = args.queue or ["us", "cn"]
    for queue_key in queue_keys:
        command = QUEUES[queue_key].command
        if not shutil.which(command):
            raise QueueError(f"Required command is not installed: {command}")

    failures = 0
    for queue_key in queue_keys:
        failures += await _process_queue(
            QUEUES[queue_key],
            cookies_path=cookies_path,
            output_root=output_root,
            state_dir=state_dir,
            dry_run=args.dry_run,
            download_timeout=args.download_timeout,
            verify_attempts=args.verify_attempts,
            verify_delay=args.verify_delay,
        )
    return 1 if failures else 0


def main() -> None:
    # gamdl logs complete API response objects at DEBUG. The queue processor only
    # emits privacy-safe counters and state transitions.
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL)
    )
    args = _parser().parse_args()
    try:
        exit_code = asyncio.run(_async_main(args))
    except (QueueError, QueueMutationError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    except KeyboardInterrupt as error:
        print("Interrupted", file=sys.stderr)
        raise SystemExit(130) from error
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
