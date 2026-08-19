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
    _account_storefront,
    _api_url,
    _catalog_id,
    _create_api,
    _download,
    _media_is_decodable,
    _process_queue,
    _registered_download,
    _resolve_playlist,
    _track_ref,
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

    def test_account_storefront_requires_two_letter_subscription_storefront(
        self,
    ) -> None:
        api = SimpleNamespace(
            account_info={"meta": {"subscription": {"storefront": "US"}}}
        )
        self.assertEqual(_account_storefront(api), "us")
        self.assertIsNone(_account_storefront(SimpleNamespace(account_info=None)))
        self.assertIsNone(
            _account_storefront(
                SimpleNamespace(
                    account_info={"meta": {"subscription": {"storefront": "usa"}}}
                )
            )
        )

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
    async def test_create_api_uses_authenticated_account_storefront(self) -> None:
        create = AsyncMock(return_value=SimpleNamespace())
        api_module = SimpleNamespace(
            AppleMusicApi=SimpleNamespace(create_from_netscape_cookies=create)
        )
        with patch.object(
            playlist_queue.importlib,
            "import_module",
            return_value=api_module,
        ):
            await _create_api(QUEUES["cn"], Path("cookies.txt"))

        create.assert_awaited_once_with(
            cookies_path="cookies.txt",
            storefront=None,
            language="zh-Hans-CN",
        )

    async def test_download_uses_account_storefront_original_id_and_language(
        self,
    ) -> None:
        completed = SimpleNamespace(returncode=0, stdout="")
        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            patch.object(
                playlist_queue,
                "_registered_download",
                side_effect=[None, Path("song.m4a")],
            ),
            patch.object(
                playlist_queue,
                "_media_is_decodable",
                return_value=True,
            ),
            patch.object(
                playlist_queue.subprocess,
                "run",
                return_value=completed,
            ) as run,
        ):
            root = Path(temporary_directory)
            await _download(
                QUEUES["cn"],
                TrackRef("i.library", "1721450032"),
                storefront="us",
                cookies_path=root / "cookies.txt",
                output_root=root / "downloads",
                state_dir=root / "state",
                timeout=10,
            )

        command = run.call_args.args[0]
        self.assertIn("--language", command)
        self.assertIn("zh-Hans-CN", command)
        self.assertIn(
            "https://music.apple.com/us/song/queue/1721450032?l=zh-Hans-CN",
            command,
        )

    async def test_process_queue_downloads_then_removes(self) -> None:
        operations: list[str] = []
        api = SimpleNamespace(
            client=SimpleNamespace(aclose=AsyncMock()),
            account_info={"meta": {"subscription": {"storefront": "us"}}},
        )
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
            patch.object(
                playlist_queue,
                "_download",
                side_effect=download,
            ) as download_mock,
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
        self.assertEqual(download_mock.await_args.kwargs["storefront"], "us")
        api.client.aclose.assert_awaited_once()

    async def test_cn_queue_uses_account_storefront_and_original_catalog_id(
        self,
    ) -> None:
        api = SimpleNamespace(
            client=SimpleNamespace(aclose=AsyncMock()),
            account_info={"meta": {"subscription": {"storefront": "us"}}},
        )
        playlists = [
            {
                "id": "p.pending",
                "attributes": {"name": "CN_Pending", "canEdit": True},
            },
        ]
        item = {
            "id": "i.library",
            "type": "library-songs",
            "attributes": {"playParams": {"catalogId": "1721450032"}},
        }

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
                AsyncMock(return_value=[item]),
            ),
            patch.object(
                playlist_queue,
                "_download",
                AsyncMock(return_value=Path("/downloads/song.m4a")),
            ) as download,
            patch.object(
                playlist_queue,
                "_remove_track",
                AsyncMock(),
            ) as remove,
        ):
            failures = await _process_queue(
                QUEUES["cn"],
                cookies_path=Path("cookies.txt"),
                output_root=Path("downloads"),
                state_dir=Path("state"),
                dry_run=False,
                download_timeout=10,
                verify_attempts=1,
                verify_delay=0,
            )

        self.assertEqual(failures, 0)
        download_track = download.await_args.args[1]
        self.assertEqual(download_track.catalog_id, "1721450032")
        self.assertEqual(download.await_args.kwargs["storefront"], "us")
        removal_track = remove.await_args.args[2]
        self.assertEqual(removal_track.library_id, "i.library")
        self.assertEqual(removal_track.catalog_id, "1721450032")

    async def test_process_queue_rejects_missing_account_storefront(self) -> None:
        api = SimpleNamespace(
            client=SimpleNamespace(aclose=AsyncMock()),
            account_info=None,
        )
        playlists = [
            {
                "id": "p.pending",
                "attributes": {"name": "CN_Pending", "canEdit": True},
            },
        ]
        item = {
            "id": "i.library",
            "type": "library-songs",
            "attributes": {"playParams": {"catalogId": "1721450032"}},
        }
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
                AsyncMock(return_value=[item]),
            ),
            self.assertRaisesRegex(QueueError, "account storefront is unavailable"),
        ):
            await _process_queue(
                QUEUES["cn"],
                cookies_path=Path("cookies.txt"),
                output_root=Path("downloads"),
                state_dir=Path("state"),
                dry_run=False,
                download_timeout=10,
                verify_attempts=1,
                verify_delay=0,
            )

        api.client.aclose.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
