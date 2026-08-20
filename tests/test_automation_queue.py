from pathlib import Path
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import gamdl_cn.automation.queue as playlist_queue
from gamdl_cn.automation.queue import (
    QUEUES,
    QueueError,
    TrackRef,
    _account_storefront,
    _api_url,
    _backfill_source_urls,
    _catalog_id,
    _create_api,
    _download,
    _download_url,
    _media_is_decodable,
    _migrate_download_databases,
    _pending_playlist_name,
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
            with closing(sqlite3.connect(database_path)) as connection, connection:
                connection.execute(
                    "CREATE TABLE media ("
                    "id TEXT NOT NULL, path TEXT NOT NULL, source_url TEXT, "
                    "source TEXT NOT NULL, PRIMARY KEY (source, id))"
                )
                connection.execute(
                    "INSERT INTO media (id, path, source) VALUES (?, ?, ?)",
                    ("123456", str(media_path), "us"),
                )
            track = TrackRef("i.library", "123456")
            self.assertEqual(
                _registered_download(database_path, QUEUES["us"], track),
                media_path,
            )

            media_path.unlink()
            self.assertIsNone(
                _registered_download(database_path, QUEUES["us"], track)
            )

    def test_registered_download_surfaces_corrupt_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "downloads.sqlite3"
            database_path.write_bytes(b"not a sqlite database")
            with self.assertRaisesRegex(QueueError, "Could not read download database"):
                _registered_download(
                    database_path,
                    QUEUES["us"],
                    TrackRef("i.library", "123456"),
                )

    def test_backfill_source_urls_migrates_existing_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_dir = Path(temporary_directory)
            legacy_path = state_dir / "cn.sqlite3"
            with closing(sqlite3.connect(legacy_path)) as connection, connection:
                connection.execute(
                    "CREATE TABLE media (id TEXT PRIMARY KEY, path TEXT NOT NULL)"
                )
                connection.executemany(
                    "INSERT INTO media (id, path) VALUES (?, ?)",
                    [
                        ("1721450032", "/downloads/song.m4a"),
                        ("1794222374", "/downloads/other.m4a"),
                    ],
                )

            database_path = _migrate_download_databases(state_dir)
            _backfill_source_urls(database_path, QUEUES["cn"], "us")

            with closing(sqlite3.connect(database_path)) as connection, connection:
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(media)")
                }
                rows = dict(
                    connection.execute(
                        "SELECT id, source_url FROM media WHERE source = 'cn'"
                    )
                )

            self.assertIn("source_url", columns)
            self.assertIn("source", columns)
            self.assertIn("downloaded_at", columns)
            self.assertFalse(legacy_path.exists())
            self.assertTrue((state_dir / "cn.sqlite3.pre-merge.bak").is_file())
            self.assertEqual(
                rows["1721450032"],
                "https://music.apple.com/us/song/queue/1721450032?l=zh-Hans-CN",
            )
            self.assertEqual(
                rows["1794222374"],
                "https://music.apple.com/us/song/queue/1794222374?l=zh-Hans-CN",
            )

            with closing(sqlite3.connect(database_path)) as connection:
                timestamps = connection.execute(
                    "SELECT downloaded_at FROM media"
                ).fetchall()
            self.assertEqual(timestamps, [(None,), (None,)])

    def test_existing_merged_database_adds_downloaded_at_column(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_dir = Path(temporary_directory)
            database_path = state_dir / "downloads.sqlite3"
            with closing(sqlite3.connect(database_path)) as connection, connection:
                connection.execute(
                    "CREATE TABLE media ("
                    "id TEXT NOT NULL, path TEXT NOT NULL, source_url TEXT, "
                    "source TEXT NOT NULL, PRIMARY KEY (source, id))"
                )
                connection.execute(
                    "INSERT INTO media (id, path, source) VALUES (?, ?, ?)",
                    ("123456", "/downloads/song.m4a", "us"),
                )

            _migrate_download_databases(state_dir)
            with closing(sqlite3.connect(database_path)) as connection:
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(media)")
                }
                downloaded_at = connection.execute(
                    "SELECT downloaded_at FROM media"
                ).fetchone()[0]
            self.assertIn("downloaded_at", columns)
            self.assertIsNone(downloaded_at)

    def test_database_merge_keeps_same_catalog_id_for_both_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_dir = Path(temporary_directory)
            for source in ("us", "cn"):
                legacy_path = state_dir / f"{source}.sqlite3"
                with closing(sqlite3.connect(legacy_path)) as connection, connection:
                    connection.execute(
                        "CREATE TABLE media ("
                        "id TEXT PRIMARY KEY, path TEXT NOT NULL, source_url TEXT)"
                    )
                    connection.execute(
                        "INSERT INTO media (id, path, source_url) VALUES (?, ?, ?)",
                        (
                            "1721450032",
                            f"/downloads/{source}.m4a",
                            f"https://music.apple.com/us/song/queue/1721450032?l={source}",
                        ),
                    )

            database_path = _migrate_download_databases(state_dir)
            with closing(sqlite3.connect(database_path)) as connection:
                rows = connection.execute(
                    "SELECT id, path, source FROM media ORDER BY source"
                ).fetchall()

            self.assertEqual(
                rows,
                [
                    ("1721450032", "/downloads/cn.m4a", "cn"),
                    ("1721450032", "/downloads/us.m4a", "us"),
                ],
            )
            self.assertEqual(_migrate_download_databases(state_dir), database_path)

    def test_database_merge_tolerates_legacy_archive_permission_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_dir = Path(temporary_directory)
            legacy_path = state_dir / "us.sqlite3"
            with closing(sqlite3.connect(legacy_path)) as connection, connection:
                connection.execute(
                    "CREATE TABLE media (id TEXT PRIMARY KEY, path TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO media (id, path) VALUES (?, ?)",
                    ("123456", "/downloads/song.m4a"),
                )

            with patch.object(Path, "replace", side_effect=PermissionError):
                database_path = _migrate_download_databases(state_dir)

            with closing(sqlite3.connect(database_path)) as connection:
                row = connection.execute(
                    "SELECT id, source FROM media"
                ).fetchone()
            self.assertEqual(row, ("123456", "us"))
            self.assertTrue(legacy_path.is_file())

    def test_download_url_includes_storefront_id_and_language(self) -> None:
        self.assertEqual(
            _download_url(QUEUES["cn"], "us", "1721450032"),
            "https://music.apple.com/us/song/queue/1721450032?l=zh-Hans-CN",
        )

    def test_pending_playlist_name_uses_environment_override(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_pending_playlist_name(QUEUES["us"]), "US_Pending")
            self.assertEqual(_pending_playlist_name(QUEUES["cn"]), "CN_Pending")

        with patch.dict(
            os.environ,
            {
                "GAMDL_US_PLAYLIST": "My US Queue",
                "GAMDL_CN_PLAYLIST": "我的下载队列",
            },
            clear=True,
        ):
            self.assertEqual(_pending_playlist_name(QUEUES["us"]), "My US Queue")
            self.assertEqual(_pending_playlist_name(QUEUES["cn"]), "我的下载队列")

        with patch.dict(os.environ, {"GAMDL_CN_PLAYLIST": "  "}, clear=True):
            with self.assertRaisesRegex(QueueError, "GAMDL_CN_PLAYLIST"):
                _pending_playlist_name(QUEUES["cn"])

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

    async def test_download_uses_storefront_original_id_and_shared_output(
        self,
    ) -> None:
        completed = SimpleNamespace(returncode=0, stdout="")
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            media_path = root / "song.m4a"
            media_path.write_bytes(b"media")
            state_dir = root / "state"
            state_dir.mkdir()
            with (
                patch.object(
                    playlist_queue,
                    "_registered_download",
                    return_value=None,
                ),
                patch.object(
                    playlist_queue,
                    "_downloader_registered_download",
                    return_value=media_path,
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
                await _download(
                    QUEUES["cn"],
                    TrackRef("i.library", "1721450032"),
                    storefront="us",
                    cookies_path=root / "cookies.txt",
                    output_root=root / "downloads",
                    state_dir=state_dir,
                    timeout=10,
                )
            database_path = state_dir / "downloads.sqlite3"
            with closing(sqlite3.connect(database_path)) as connection, connection:
                source_url, source, downloaded_at = connection.execute(
                    "SELECT source_url, source, downloaded_at "
                    "FROM media WHERE id = ?",
                    ("1721450032",),
                ).fetchone()

        command = run.call_args.args[0]
        self.assertIn("--language", command)
        self.assertIn("zh-Hans-CN", command)
        output_path_index = command.index("--output-path") + 1
        self.assertEqual(command[output_path_index], str(root / "downloads"))
        self.assertNotIn(str(root / "downloads" / "CN"), command)
        database_path_index = command.index("--database-path") + 1
        self.assertEqual(
            command[database_path_index],
            str(root / "state" / "tmp" / "cn" / "downloader.sqlite3"),
        )
        self.assertIn(
            "https://music.apple.com/us/song/queue/1721450032?l=zh-Hans-CN",
            command,
        )
        self.assertEqual(
            source_url,
            "https://music.apple.com/us/song/queue/1721450032?l=zh-Hans-CN",
        )
        self.assertEqual(source, "cn")
        self.assertRegex(
            downloaded_at,
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$",
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
