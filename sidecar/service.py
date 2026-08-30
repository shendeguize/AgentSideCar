"""Platform facade for the explicit sidecar daemon service lifecycle."""

from __future__ import annotations

import sys
from typing import Any, Optional

from sidecar.launchd import ServiceResult


def _backend(platform: Optional[str]) -> Any:
    selected = sys.platform if platform is None else platform
    if selected == "darwin":
        from sidecar import launchd

        return launchd
    if selected.startswith("linux"):
        from sidecar import systemd

        return systemd
    return None


def _unsupported() -> ServiceResult:
    return ServiceResult(
        2,
        "service control is supported only on macOS LaunchAgent or Linux systemd --user",
    )


def install_service(**kwargs: Any) -> ServiceResult:
    backend = _backend(kwargs.get("platform"))
    return _unsupported() if backend is None else backend.install_service(**kwargs)


def uninstall_service(**kwargs: Any) -> ServiceResult:
    backend = _backend(kwargs.get("platform"))
    return _unsupported() if backend is None else backend.uninstall_service(**kwargs)


def service_status(**kwargs: Any) -> ServiceResult:
    backend = _backend(kwargs.get("platform"))
    return _unsupported() if backend is None else backend.service_status(**kwargs)


__all__ = [
    "ServiceResult",
    "install_service",
    "service_status",
    "uninstall_service",
]
