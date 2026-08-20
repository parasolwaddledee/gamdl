from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import structlog

from gamdl_cn.automation import queue

from .config import AutomationConfigError, validate_runtime_limits


MEDIA_SUFFIXES = frozenset({".m4a", ".lrc"})


class PipelineError(RuntimeError):
    pass


@dataclass(frozen=True)
class PipelineConfig:
    cookies_path: Path
    rclone_config_path: Path
    rclone_destination: str
    output_root: Path = Path("/downloads")
    state_dir: Path = Path("/state")
    queues: tuple[str, ...] = ("us", "cn")
    download_timeout: int = 3600
    verify_attempts: int = 6
    verify_delay: float = 3.0
    dry_run: bool = False
    keep_local: bool = False


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    size: int
    md5: str


def _required_file(path: Path, label: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except FileNotFoundError as error:
        raise PipelineError(f"{label} file not found: {path}") from error
    if not resolved.is_file():
        raise PipelineError(f"{label} path is not a file: {path}")
    return resolved


def _md5(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_files(output_root: Path) -> list[FileSnapshot]:
    root = output_root.resolve()
    snapshots: list[FileSnapshot] = []
    for candidate in sorted(root.rglob("*")):
        if candidate.suffix.lower() not in MEDIA_SUFFIXES:
            continue
        if candidate.is_symlink() or not candidate.is_file():
            continue
        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise PipelineError("Refusing to archive a path outside the output root") from error
        snapshots.append(
            FileSnapshot(path=resolved, size=resolved.stat().st_size, md5=_md5(resolved))
        )
    return snapshots


def _rclone_command(
    operation: str,
    *,
    output_root: Path,
    destination: str,
    config_path: Path,
    dry_run: bool = False,
) -> list[str]:
    command = [
        "rclone",
        operation,
        str(output_root),
        destination,
        "--config",
        str(config_path),
        "--include",
        "**/*.m4a",
        "--include",
        "**/*.lrc",
    ]
    if operation == "copy":
        command.extend(["--checksum", "--stats-one-line", "--log-level", "INFO"])
        if dry_run:
            command.append("--dry-run")
    elif operation == "check":
        command.extend(["--one-way", "--log-level", "INFO"])
    else:
        raise ValueError(f"Unsupported rclone operation: {operation}")
    return command


def _run_rclone(command: list[str]) -> int:
    result = subprocess.run(command, check=False)
    return result.returncode


def _validated_deletion_targets(
    snapshots: list[FileSnapshot], output_root: Path
) -> list[Path]:
    root = output_root.resolve()
    targets: list[Path] = []
    for snapshot in snapshots:
        path = snapshot.path
        if path.is_symlink() or not path.is_file():
            raise PipelineError(
                "A local file disappeared or became a symlink after R2 verification"
            )
        resolved = path.resolve(strict=True)
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise PipelineError("Refusing to delete a path outside the output root") from error
        if resolved.stat().st_size != snapshot.size or _md5(resolved) != snapshot.md5:
            raise PipelineError(
                "A local file changed after R2 verification; local files were retained"
            )
        targets.append(resolved)
    return targets


async def run_pipeline(config: PipelineConfig) -> int:
    # gamdl logs complete API response objects at DEBUG. The service only emits
    # privacy-safe queue counters and pipeline state transitions.
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL)
    )
    validate_runtime_limits(
        download_timeout=config.download_timeout,
        verify_attempts=config.verify_attempts,
        verify_delay=config.verify_delay,
    )
    cookies_path = _required_file(config.cookies_path, "Apple Music Cookies")
    rclone_config_path = _required_file(config.rclone_config_path, "rclone config")
    if not config.rclone_destination.strip():
        raise PipelineError("Rclone destination cannot be empty")
    if not shutil.which("rclone"):
        raise PipelineError("Required command is not installed: rclone")

    output_root = config.output_root.expanduser().resolve()
    state_dir = config.state_dir.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    queue_config = queue.QueueRunConfig(
        cookies_path=cookies_path,
        output_root=output_root,
        state_dir=state_dir,
        queues=config.queues,
        dry_run=config.dry_run,
        download_timeout=config.download_timeout,
        verify_attempts=config.verify_attempts,
        verify_delay=config.verify_delay,
    )
    try:
        queue_exit_code = await queue.run_queues(queue_config)
    except (AutomationConfigError, queue.QueueError) as error:
        print(f"Queue processing failed: {error}", file=sys.stderr, flush=True)
        queue_exit_code = 1
    except Exception as error:
        print(
            f"Queue processing failed with {type(error).__name__}",
            file=sys.stderr,
            flush=True,
        )
        queue_exit_code = 1

    snapshots = _snapshot_files(output_root)
    if not snapshots:
        print("R2 upload: no local .m4a or .lrc files found.", flush=True)
        return queue_exit_code

    copy_command = _rclone_command(
        "copy",
        output_root=output_root,
        destination=config.rclone_destination,
        config_path=rclone_config_path,
        dry_run=config.dry_run,
    )
    copy_exit_code = await asyncio.to_thread(_run_rclone, copy_command)
    if copy_exit_code:
        print("R2 copy failed; local files were retained.", file=sys.stderr, flush=True)
        return copy_exit_code
    if config.dry_run:
        print("R2 upload dry-run complete; local files were retained.", flush=True)
        return queue_exit_code

    check_command = _rclone_command(
        "check",
        output_root=output_root,
        destination=config.rclone_destination,
        config_path=rclone_config_path,
    )
    check_exit_code = await asyncio.to_thread(_run_rclone, check_command)
    if check_exit_code:
        print(
            "R2 verification failed; local files were retained.",
            file=sys.stderr,
            flush=True,
        )
        return check_exit_code
    if config.keep_local:
        print("R2 verification passed; local files were retained by policy.", flush=True)
        return queue_exit_code

    targets = _validated_deletion_targets(snapshots, output_root)
    for target in targets:
        target.unlink()
    print(
        f"R2 verification passed; deleted {len(targets)} local file(s).",
        flush=True,
    )
    return queue_exit_code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Process Apple Music queues, copy completed media to rclone storage, "
            "verify it, and remove unchanged local files."
        )
    )
    parser.add_argument("--cookies-path", type=Path, required=True)
    parser.add_argument("--rclone-config", type=Path, required=True)
    parser.add_argument("--rclone-destination", required=True)
    parser.add_argument("--output-root", type=Path, default=Path("/downloads"))
    parser.add_argument("--state-dir", type=Path, default=Path("/state"))
    parser.add_argument(
        "--queue",
        action="append",
        choices=sorted(queue.QUEUES),
        help="Queue to process; repeat for both. Defaults to us and cn.",
    )
    parser.add_argument("--download-timeout", type=int, default=3600)
    parser.add_argument("--verify-attempts", type=int, default=6)
    parser.add_argument("--verify-delay", type=float, default=3.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-local", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    config = PipelineConfig(
        cookies_path=args.cookies_path,
        rclone_config_path=args.rclone_config,
        rclone_destination=args.rclone_destination,
        output_root=args.output_root,
        state_dir=args.state_dir,
        queues=tuple(args.queue or ("us", "cn")),
        download_timeout=args.download_timeout,
        verify_attempts=args.verify_attempts,
        verify_delay=args.verify_delay,
        dry_run=args.dry_run,
        keep_local=args.keep_local,
    )
    try:
        exit_code = asyncio.run(run_pipeline(config))
    except (AutomationConfigError, PipelineError) as error:
        print(f"Pipeline failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    except KeyboardInterrupt as error:
        print("Interrupted", file=sys.stderr)
        raise SystemExit(130) from error
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
