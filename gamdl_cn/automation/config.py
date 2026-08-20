"""Shared validation for automation runtime settings."""


class AutomationConfigError(ValueError):
    pass


def validate_runtime_limits(
    *,
    download_timeout: int,
    verify_attempts: int,
    verify_delay: float,
) -> None:
    if download_timeout <= 0:
        raise AutomationConfigError("Download timeout must be greater than zero")
    if verify_attempts < 1:
        raise AutomationConfigError("Verify attempts must be at least one")
    if verify_delay < 0:
        raise AutomationConfigError("Verify delay must not be negative")
