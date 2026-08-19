import os
import unittest
from unittest.mock import patch

from gamdl_cn.cli.playlist_service import (
    ServiceConfigError,
    _duration_seconds,
    _env_bool,
    _environment_queues,
)


class PlaylistServiceTests(unittest.TestCase):
    def test_duration_supports_seconds_minutes_hours_and_days(self) -> None:
        self.assertEqual(_duration_seconds("30"), 30)
        self.assertEqual(_duration_seconds("15m"), 900)
        self.assertEqual(_duration_seconds("2h"), 7200)
        self.assertEqual(_duration_seconds("1d"), 86400)

    def test_duration_rejects_zero_and_unknown_units(self) -> None:
        with self.assertRaises(ServiceConfigError):
            _duration_seconds("0")
        with self.assertRaises(ServiceConfigError):
            _duration_seconds("1w")

    def test_boolean_environment_is_strict(self) -> None:
        with patch.dict(os.environ, {"PIPELINE_TEST_BOOL": "yes"}):
            self.assertTrue(_env_bool("PIPELINE_TEST_BOOL", False))
        with patch.dict(os.environ, {"PIPELINE_TEST_BOOL": "maybe"}):
            with self.assertRaises(ServiceConfigError):
                _env_bool("PIPELINE_TEST_BOOL", False)

    def test_environment_queue_selection(self) -> None:
        with patch.dict(os.environ, {"GAMDL_QUEUES": "cn,us"}):
            self.assertEqual(_environment_queues(), ("cn", "us"))
        with patch.dict(os.environ, {"GAMDL_QUEUES": "other"}):
            with self.assertRaises(ServiceConfigError):
                _environment_queues()


if __name__ == "__main__":
    unittest.main()
