from __future__ import annotations

import argparse
import asyncio
import importlib
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import structlog

from .config import AutomationConfigError, validate_runtime_limits
from .models import (
    QUEUES,
    QueueConfig,
    QueueError,
    QueueMutationError,
    QueueRunConfig,
    TrackRef,
    download_url as _download_url,
)
from .registry import (
    DATABASE_FILENAME,
    backfill_source_urls as _backfill_source_urls,
    downloader_registered_download as _downloader_registered_download,
    migrate_download_databases as _migrate_download_databases,
    record_download as _record_download,
    record_source_url as _record_source_url,
    registered_download as _registered_download,
)


AMP_API_URL = "https://amp-api.music.apple.com"
SUCCESS_STATUS_CODES = {200, 201, 202, 204}


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
) -> Any:
    response = await api.client.request(
        method,
        _api_url(path_or_url),
        params=params,
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


def _account_storefront(api: Any) -> str | None:
    account_info = getattr(api, "account_info", None) or {}
    metadata = account_info.get("meta") or {}
    subscription = metadata.get("subscription") or {}
    storefront = str(subscription.get("storefront") or "").lower()
    return storefront if re.fullmatch(r"[a-z]{2}", storefront) else None


def _pending_playlist_name(queue: QueueConfig) -> str:
    name = os.environ.get(queue.pending_name_env, queue.pending_name).strip()
    if not name:
        raise QueueError(f"{queue.pending_name_env} must not be empty")
    return name


def _remove_downloader_database(database_path: Path) -> None:
    for suffix in ("", "-journal", "-shm", "-wal"):
        database_path.with_name(database_path.name + suffix).unlink(missing_ok=True)


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
    storefront: str,
    cookies_path: Path,
    output_root: Path,
    state_dir: Path,
    timeout: int,
) -> Path:
    # run_queues performs schema and legacy migration once per service run.
    database_path = state_dir / DATABASE_FILENAME
    url = _download_url(queue, storefront, track.catalog_id)
    registered = _registered_download(database_path, queue, track)
    if registered and await asyncio.to_thread(_media_is_decodable, registered):
        _record_source_url(database_path, queue, track, url)
        return registered

    output_path = output_root
    temp_path = state_dir / "tmp" / queue.key
    downloader_database_path = temp_path / "downloader.sqlite3"
    output_path.mkdir(parents=True, exist_ok=True)
    temp_path.mkdir(parents=True, exist_ok=True)
    _remove_downloader_database(downloader_database_path)
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
        str(downloader_database_path),
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
        _remove_downloader_database(downloader_database_path)
        raise QueueError(f"{queue.command} download timed out") from error

    (state_dir / f"{queue.key}-last-download.log").write_text(
        result.stdout or "",
        encoding="utf-8",
    )
    try:
        registered = _downloader_registered_download(downloader_database_path, track)
        decodable = bool(
            registered and await asyncio.to_thread(_media_is_decodable, registered)
        )
        if result.returncode != 0 or not registered or not decodable:
            raise QueueError(
                f"{queue.command} did not register a decodable local media file"
            )
        _record_download(database_path, queue, track, registered, url)
        return registered
    finally:
        _remove_downloader_database(downloader_database_path)


async def _wait_until_catalog_id_removed(
    api: Any,
    playlist_id: str,
    catalog_id: str,
    *,
    attempts: int,
    delay: float,
) -> bool:
    for attempt in range(attempts):
        tracks = await _list_tracks(api, playlist_id)
        found = any(_catalog_id(track) == catalog_id for track in tracks)
        if not found:
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
    if not await _wait_until_catalog_id_removed(
        api,
        playlist_id,
        track.catalog_id,
        attempts=attempts,
        delay=delay,
    ):
        raise QueueMutationError("Removed track still appears in the pending playlist")


async def _create_api(queue: QueueConfig, cookies_path: Path) -> Any:
    api_module = importlib.import_module(f"{queue.package}.api")
    api_class = api_module.AppleMusicApi
    return await api_class.create_from_netscape_cookies(
        cookies_path=str(cookies_path),
        storefront=None,
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
    pending_name = _pending_playlist_name(queue)
    api = await _create_api(queue, cookies_path)
    label = queue.key.upper()
    try:
        print(f"[{label}] reading playlist queue", flush=True)
        playlists = await _list_playlists(api)
        pending = _resolve_playlist(playlists, pending_name)

        pending_tracks = await _list_tracks(api, pending["id"])
        actionable = [track for item in pending_tracks if (track := _track_ref(item))]
        unsupported = len(pending_tracks) - len(actionable)
        print(
            f"[{label}] pending={len(pending_tracks)} "
            f"actionable={len(actionable)} unsupported={unsupported}"
        )
        if dry_run:
            return unsupported

        storefront = _account_storefront(api)
        if not storefront:
            raise QueueError("Apple Music account storefront is unavailable")
        _backfill_source_urls(
            state_dir / DATABASE_FILENAME,
            queue,
            storefront,
        )

        failures = unsupported
        for index, track in enumerate(actionable, 1):
            print(f"[{label}] item {index}/{len(actionable)}: checking local download")
            try:
                await _download(
                    queue,
                    track,
                    storefront=storefront,
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


async def run_queues(config: QueueRunConfig) -> int:
    validate_runtime_limits(
        download_timeout=config.download_timeout,
        verify_attempts=config.verify_attempts,
        verify_delay=config.verify_delay,
    )
    if not config.queues or any(queue not in QUEUES for queue in config.queues):
        raise AutomationConfigError("Queues must contain only us and/or cn")
    if len(set(config.queues)) != len(config.queues):
        raise AutomationConfigError("Queues must not contain duplicates")

    cookies_path = config.cookies_path.expanduser().resolve()
    if not cookies_path.is_file():
        raise QueueError(f"Cookies file not found: {cookies_path}")
    output_root = config.output_root.expanduser().resolve()
    state_dir = config.state_dir.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    _migrate_download_databases(state_dir)

    if not config.dry_run:
        for queue_key in config.queues:
            command = QUEUES[queue_key].command
            if not shutil.which(command):
                raise QueueError(f"Required command is not installed: {command}")

    failures = 0
    for queue_key in config.queues:
        failures += await _process_queue(
            QUEUES[queue_key],
            cookies_path=cookies_path,
            output_root=output_root,
            state_dir=state_dir,
            dry_run=config.dry_run,
            download_timeout=config.download_timeout,
            verify_attempts=config.verify_attempts,
            verify_delay=config.verify_delay,
        )
    return 1 if failures else 0


def main() -> None:
    # gamdl logs complete API response objects at DEBUG. The queue processor only
    # emits privacy-safe counters and state transitions.
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL)
    )
    args = _parser().parse_args()
    config = QueueRunConfig(
        cookies_path=args.cookies_path,
        output_root=args.output_root,
        state_dir=args.state_dir,
        queues=tuple(args.queue or ("us", "cn")),
        dry_run=args.dry_run,
        download_timeout=args.download_timeout,
        verify_attempts=args.verify_attempts,
        verify_delay=args.verify_delay,
    )
    try:
        exit_code = asyncio.run(run_queues(config))
    except (AutomationConfigError, QueueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    except KeyboardInterrupt as error:
        print("Interrupted", file=sys.stderr)
        raise SystemExit(130) from error
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
