from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import gamdl_cn.cli.playlist_queue as playlist_queue
from gamdl_cn.cli.playlist_queue import (
    QUEUES,
    QueueError,
    TrackRef,
    _api_url,
    _catalog_id,
    _media_is_decodable,
    _registered_download,
    _resolve_playlist,
    _track_ref,
    _process_queue,
)


class PlaylistQueueTests(unittest.TestCase):
    def test_api_url_rejects_untrusted_pagination_host(self) -> None:
        with self.assertRaises(QueueError):
            _api_url("https://example.com/v1/me/library/playlists")

    def test_resolve_playlist_requires_exact_unique_editable_match(self) -> None:
        playlists = [
            {
                "id": "p.pending",
                "attributes": {"name": "US_Pending", "canEdit": True},
            },
            {
                "id": "p.other",
                "attributes": {"name": "US_Pending copy", "canEdit": True},
            },
        ]
        self.assertEqual(_resolve_playlist(playlists, "US_Pending")["id"], "p.pending")

        playlists.append(
            {
                "id": "p.duplicate",
                "attributes": {"name": "US_Pending", "canEdit": True},
            }
        )
        with self.assertRaises(QueueError):
            _resolve_playlist(playlists, "US_Pending")

        with self.assertRaises(QueueError):
            _resolve_playlist(
                [
                    {
                        "id": "p.pending",
                        "attributes": {"name": "US_Pending", "canEdit": False},
                    }
                ],
                "US_Pending",
            )

    def test_track_ref_uses_library_and_catalog_ids(self) -> None:
        track = {
            "id": "i.library",
            "type": "library-songs",
            "attributes": {"playParams": {"catalogId": "123456"}},
        }
        self.assertEqual(_catalog_id(track), "123456")
        self.assertEqual(_track_ref(track), TrackRef("i.library", "123456"))

    def test_track_ref_rejects_non_catalog_and_non_song_items(self) -> None:
        self.assertIsNone(
            _track_ref({"id": "i.upload", "type": "uploaded-audios"})
        )
        self.assertIsNone(
            _track_ref(
                {
                    "id": "i.library",
                    "type": "library-songs",
                    "attributes": {"playParams": {"id": "i.library"}},
                }
            )
        )

    def test_registered_download_requires_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            tmp_path = Path(temporary_directory)
            media_path = tmp_path / "song.m4a"
            media_path.write_bytes(b"media")
            database_path = tmp_path / "queue.sqlite3"
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    "CREATE TABLE media (id TEXT PRIMARY KEY, path TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO media (id, path) VALUES (?, ?)",
                    ("123456", str(media_path)),
                )
            track = TrackRef("i.library", "123456")
            self.assertEqual(_registered_download(database_path, track), media_path)

            media_path.unlink()
            self.assertIsNone(_registered_download(database_path, track))

    def test_media_verification_requires_positive_duration(self) -> None:
        successful_probe = SimpleNamespace(returncode=0, stdout="12.5\n")
        empty_probe = SimpleNamespace(returncode=0, stdout="0\n")
        with (
            patch.object(playlist_queue.shutil, "which", return_value="ffprobe"),
            patch.object(
                playlist_queue.subprocess,
                "run",
                side_effect=[successful_probe, empty_probe],
            ),
        ):
            self.assertTrue(_media_is_decodable(Path("song.m4a")))
            self.assertFalse(_media_is_decodable(Path("empty.m4a")))


class PlaylistQueueAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_process_queue_downloads_then_removes(self) -> None:
        operations: list[str] = []
        api = SimpleNamespace(client=SimpleNamespace(aclose=AsyncMock()))
        playlists = [
            {
                "id": "p.pending",
                "attributes": {"name": "US_Pending", "canEdit": True},
            },
        ]
        track = {
            "id": "i.library",
            "type": "library-songs",
            "attributes": {"playParams": {"catalogId": "123456"}},
        }

        async def download(*args, **kwargs) -> Path:
            operations.append("download")
            return Path("/downloads/song.m4a")

        async def remove(*args, **kwargs) -> None:
            operations.append("remove")

        with (
            patch.object(playlist_queue, "_create_api", AsyncMock(return_value=api)),
            patch.object(
                playlist_queue,
                "_list_playlists",
                AsyncMock(return_value=playlists),
            ),
            patch.object(
                playlist_queue,
                "_list_tracks",
                AsyncMock(return_value=[track]),
            ),
            patch.object(playlist_queue, "_download", side_effect=download),
            patch.object(playlist_queue, "_remove_track", side_effect=remove),
        ):
            failures = await _process_queue(
                QUEUES["us"],
                cookies_path=Path("cookies.txt"),
                output_root=Path("downloads"),
                state_dir=Path("state"),
                dry_run=False,
                download_timeout=10,
                verify_attempts=1,
                verify_delay=0,
            )

        self.assertEqual(failures, 0)
        self.assertEqual(operations, ["download", "remove"])
        api.client.aclose.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
