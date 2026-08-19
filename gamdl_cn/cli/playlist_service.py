from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gamdl_cn.cli.playlist_pipeline import PipelineConfig, PipelineError, run_pipeline
from gamdl_cn.cli.playlist_queue import QUEUES


TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})
DURATION_PATTERN = re.compile(r"^(?P<value>[1-9][0-9]*)(?P<unit>[smhd]?)$")
DURATION_MULTIPLIERS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


class ServiceConfigError(ValueError):
    pass


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise ServiceConfigError(f"{name} must be true or false")


def _duration_seconds(value: str) -> int:
    match = DURATION_PATTERN.fullmatch(value.strip().lower())
    if not match:
        raise ServiceConfigError(
            "Run interval must be a positive integer with optional s, m, h, or d suffix"
        )
    return int(match.group("value")) * DURATION_MULTIPLIERS[match.group("unit")]


def _environment_queues() -> tuple[str, ...]:
    values = tuple(
        value.strip().lower()
        for value in os.environ.get("GAMDL_QUEUES", "us,cn").split(",")
        if value.strip()
    )
    if not values or any(value not in QUEUES for value in values):
        raise ServiceConfigError("GAMDL_QUEUES must contain only us and/or cn")
    return values


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the verified Apple Music to R2 pipeline on a fixed interval."
    )
    parser.add_argument(
        "--cookies-path",
        type=Path,
        default=Path(os.environ.get("GAMDL_COOKIES_PATH", "/config/cookies.txt")),
    )
    parser.add_argument(
        "--rclone-config",
        type=Path,
        default=Path(os.environ.get("RCLONE_CONFIG", "/config/rclone.conf")),
    )
    parser.add_argument(
        "--rclone-destination",
        default=os.environ.get("RCLONE_DESTINATION", "music:music"),
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path(os.environ.get("GAMDL_OUTPUT_ROOT", "/downloads")),
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.environ.get("GAMDL_STATE_DIR", "/state")),
    )
    parser.add_argument(
        "--interval",
        default=os.environ.get("GAMDL_RUN_INTERVAL", "1h"),
        help="Delay after each run, for example 30m, 1h, or 1d.",
    )
    parser.add_argument(
        "--queue",
        action="append",
        choices=sorted(QUEUES),
        help="Queue to process; repeat to select multiple queues.",
    )
    parser.add_argument(
        "--download-timeout",
        type=int,
        default=int(os.environ.get("GAMDL_DOWNLOAD_TIMEOUT", "3600")),
    )
    parser.add_argument(
        "--verify-attempts",
        type=int,
        default=int(os.environ.get("GAMDL_VERIFY_ATTEMPTS", "6")),
    )
    parser.add_argument(
        "--verify-delay",
        type=float,
        default=float(os.environ.get("GAMDL_VERIFY_DELAY", "3")),
    )
    parser.add_argument(
        "--run-once",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("GAMDL_RUN_ONCE", False),
    )
    parser.add_argument(
        "--run-immediately",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("GAMDL_RUN_IMMEDIATELY", True),
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("GAMDL_DRY_RUN", False),
    )
    parser.add_argument(
        "--keep-local",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("GAMDL_KEEP_LOCAL", False),
    )
    return parser


def _write_status(state_dir: Path, payload: dict[str, object]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    status_path = state_dir / "last-run.json"
    temporary_path = state_dir / ".last-run.json.tmp"
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary_path.replace(status_path)


def _wait(stop_event: threading.Event, seconds: int, next_run: datetime) -> bool:
    print(f"Next run at {next_run.isoformat()} (in {seconds} seconds).", flush=True)
    return stop_event.wait(seconds)


def main() -> None:
    try:
        args = _parser().parse_args()
        interval_seconds = _duration_seconds(args.interval)
        queues = tuple(args.queue) if args.queue else _environment_queues()
    except (ServiceConfigError, ValueError) as error:
        print(f"Service configuration failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error

    pipeline_config = PipelineConfig(
        cookies_path=args.cookies_path,
        rclone_config_path=args.rclone_config,
        rclone_destination=args.rclone_destination,
        output_root=args.output_root,
        state_dir=args.state_dir,
        queues=queues,
        download_timeout=args.download_timeout,
        verify_attempts=args.verify_attempts,
        verify_delay=args.verify_delay,
        dry_run=args.dry_run,
        keep_local=args.keep_local,
    )
    stop_event = threading.Event()

    def stop_service(signum: int, frame: object) -> None:
        del signum, frame
        print("Shutdown requested; stopping after the active run.", flush=True)
        stop_event.set()

    signal.signal(signal.SIGTERM, stop_service)
    signal.signal(signal.SIGINT, stop_service)

    if not args.run_immediately and not args.run_once:
        next_run = datetime.now(timezone.utc) + timedelta(seconds=interval_seconds)
        if _wait(stop_event, interval_seconds, next_run):
            raise SystemExit(0)

    run_number = 0
    while not stop_event.is_set():
        run_number += 1
        started_at = datetime.now(timezone.utc)
        print(f"Pipeline run {run_number} started at {started_at.isoformat()}.", flush=True)
        try:
            exit_code = asyncio.run(run_pipeline(pipeline_config))
        except PipelineError as error:
            print(f"Pipeline failed: {error}", file=sys.stderr, flush=True)
            exit_code = 1
        except Exception as error:
            print(
                f"Pipeline failed with {type(error).__name__}",
                file=sys.stderr,
                flush=True,
            )
            exit_code = 1
        completed_at = datetime.now(timezone.utc)
        _write_status(
            pipeline_config.state_dir,
            {
                "run": run_number,
                "started_at": started_at.isoformat(),
                "completed_at": completed_at.isoformat(),
                "exit_code": exit_code,
                "success": exit_code == 0,
            },
        )
        print(
            f"Pipeline run {run_number} completed with exit code {exit_code}.",
            flush=True,
        )
        if args.run_once:
            raise SystemExit(exit_code)
        next_run = completed_at + timedelta(seconds=interval_seconds)
        if _wait(stop_event, interval_seconds, next_run):
            break
    raise SystemExit(0)


if __name__ == "__main__":
    main()
