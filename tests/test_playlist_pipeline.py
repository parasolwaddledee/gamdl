import asyncio
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import gamdl_cn.cli.playlist_pipeline as playlist_pipeline
from gamdl_cn.cli.playlist_pipeline import (
    PipelineConfig,
    PipelineError,
    _rclone_command,
    run_pipeline,
)


class PlaylistPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.cookies_path = self.root / "cookies.txt"
        self.rclone_config_path = self.root / "rclone.conf"
        self.output_root = self.root / "downloads"
        self.state_dir = self.root / "state"
        self.cookies_path.write_text("test", encoding="utf-8")
        self.rclone_config_path.write_text("[mock]\ntype = local\n", encoding="utf-8")
        self.output_root.mkdir()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def config(self, **overrides: object) -> PipelineConfig:
        values: dict[str, object] = {
            "cookies_path": self.cookies_path,
            "rclone_config_path": self.rclone_config_path,
            "rclone_destination": "mock:music",
            "output_root": self.output_root,
            "state_dir": self.state_dir,
        }
        values.update(overrides)
        return PipelineConfig(**values)

    def run_with_mocks(
        self,
        config: PipelineConfig,
        rclone_results: list[SimpleNamespace],
        *,
        queue_result: int = 0,
    ) -> tuple[int, list[list[str]]]:
        commands: list[list[str]] = []

        def run(command: list[str], **kwargs: object) -> SimpleNamespace:
            del kwargs
            commands.append(command)
            return rclone_results.pop(0)

        with (
            patch.object(playlist_pipeline.shutil, "which", return_value="/usr/bin/rclone"),
            patch.object(
                playlist_pipeline.playlist_queue,
                "_async_main",
                AsyncMock(return_value=queue_result),
            ),
            patch.object(playlist_pipeline.subprocess, "run", side_effect=run),
        ):
            exit_code = asyncio.run(run_pipeline(config))
        return exit_code, commands

    def test_successful_copy_and_check_delete_only_media_files(self) -> None:
        media_path = self.output_root / "US" / "song.m4a"
        lyrics_path = self.output_root / "CN" / "song.lrc"
        ignored_path = self.output_root / "notes.txt"
        media_path.parent.mkdir(parents=True)
        lyrics_path.parent.mkdir(parents=True)
        media_path.write_bytes(b"media")
        lyrics_path.write_text("lyrics", encoding="utf-8")
        ignored_path.write_text("keep", encoding="utf-8")

        exit_code, commands = self.run_with_mocks(
            self.config(),
            [SimpleNamespace(returncode=0), SimpleNamespace(returncode=0)],
        )

        self.assertEqual(exit_code, 0)
        self.assertFalse(media_path.exists())
        self.assertFalse(lyrics_path.exists())
        self.assertTrue(ignored_path.exists())
        self.assertEqual([command[1] for command in commands], ["copy", "check"])

    def test_copy_failure_retains_local_file(self) -> None:
        media_path = self.output_root / "song.m4a"
        media_path.write_bytes(b"media")

        exit_code, commands = self.run_with_mocks(
            self.config(), [SimpleNamespace(returncode=3)]
        )

        self.assertEqual(exit_code, 3)
        self.assertTrue(media_path.exists())
        self.assertEqual([command[1] for command in commands], ["copy"])

    def test_dry_run_retains_local_file_and_skips_check(self) -> None:
        media_path = self.output_root / "song.m4a"
        media_path.write_bytes(b"media")

        exit_code, commands = self.run_with_mocks(
            self.config(dry_run=True), [SimpleNamespace(returncode=0)]
        )

        self.assertEqual(exit_code, 0)
        self.assertTrue(media_path.exists())
        self.assertEqual([command[1] for command in commands], ["copy"])
        self.assertIn("--dry-run", commands[0])

    def test_check_failure_retains_local_file(self) -> None:
        media_path = self.output_root / "song.m4a"
        media_path.write_bytes(b"media")

        exit_code, commands = self.run_with_mocks(
            self.config(),
            [SimpleNamespace(returncode=0), SimpleNamespace(returncode=4)],
        )

        self.assertEqual(exit_code, 4)
        self.assertTrue(media_path.exists())
        self.assertEqual([command[1] for command in commands], ["copy", "check"])

    def test_queue_failure_does_not_block_retrying_staged_uploads(self) -> None:
        media_path = self.output_root / "song.m4a"
        media_path.write_bytes(b"media")
        with (
            patch.object(playlist_pipeline.shutil, "which", return_value="/usr/bin/rclone"),
            patch.object(
                playlist_pipeline.playlist_queue,
                "_async_main",
                AsyncMock(side_effect=playlist_pipeline.playlist_queue.QueueError("offline")),
            ),
            patch.object(
                playlist_pipeline.subprocess,
                "run",
                side_effect=[SimpleNamespace(returncode=0), SimpleNamespace(returncode=0)],
            ) as run,
        ):
            exit_code = asyncio.run(run_pipeline(self.config()))

        self.assertEqual(exit_code, 1)
        self.assertFalse(media_path.exists())
        self.assertEqual(run.call_count, 2)

    def test_changed_file_after_check_is_retained(self) -> None:
        media_path = self.output_root / "song.m4a"
        media_path.write_bytes(b"before")
        call_count = 0

        def run(command: list[str], **kwargs: object) -> SimpleNamespace:
            nonlocal call_count
            del command, kwargs
            call_count += 1
            if call_count == 2:
                media_path.write_bytes(b"after")
            return SimpleNamespace(returncode=0)

        with (
            patch.object(playlist_pipeline.shutil, "which", return_value="/usr/bin/rclone"),
            patch.object(
                playlist_pipeline.playlist_queue,
                "_async_main",
                AsyncMock(return_value=0),
            ),
            patch.object(playlist_pipeline.subprocess, "run", side_effect=run),
        ):
            with self.assertRaises(PipelineError):
                asyncio.run(run_pipeline(self.config()))

        self.assertTrue(media_path.exists())

    def test_rclone_filters_only_media_and_lyrics(self) -> None:
        command = _rclone_command(
            "check",
            output_root=Path("/downloads"),
            destination="music:music",
            config_path=Path("/config/rclone.conf"),
        )
        self.assertIn("**/*.m4a", command)
        self.assertIn("**/*.lrc", command)
        self.assertIn("--one-way", command)


if __name__ == "__main__":
    unittest.main()
