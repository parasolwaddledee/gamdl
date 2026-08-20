from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode


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
    pending_name_env: str
    language: str


@dataclass(frozen=True)
class TrackRef:
    library_id: str
    catalog_id: str


@dataclass(frozen=True)
class QueueRunConfig:
    cookies_path: Path
    output_root: Path = Path("/downloads")
    state_dir: Path = Path("/state")
    queues: tuple[str, ...] = ("us", "cn")
    dry_run: bool = False
    download_timeout: int = 3600
    verify_attempts: int = 6
    verify_delay: float = 3.0


QUEUES = {
    "us": QueueConfig(
        key="us",
        package="gamdl",
        command="gamdl",
        pending_name="US_Pending",
        pending_name_env="GAMDL_US_PLAYLIST",
        language="en-US",
    ),
    "cn": QueueConfig(
        key="cn",
        package="gamdl_cn",
        command="gamdl_cn",
        pending_name="CN_Pending",
        pending_name_env="GAMDL_CN_PLAYLIST",
        language="zh-Hans-CN",
    ),
}


def download_url(queue: QueueConfig, storefront: str, catalog_id: str) -> str:
    return (
        f"https://music.apple.com/{storefront}/song/queue/{catalog_id}?"
        f"{urlencode({'l': queue.language})}"
    )
