"""Orbbec depth-camera capture and Dora integration."""

from __future__ import annotations

from typing import Any

__version__ = "1.0.1"

__all__ = ["DeviceInfo", "OrbbeFrame", "OrbbecConfig", "__version__"]


def __getattr__(name: str) -> Any:
    """Keep package import lightweight for the frozen privileged helper."""
    if name in {"DeviceInfo", "OrbbeFrame"}:
        from .backend import DeviceInfo, OrbbeFrame

        return {"DeviceInfo": DeviceInfo, "OrbbeFrame": OrbbeFrame}[name]
    if name == "OrbbecConfig":
        from .config import OrbbecConfig

        return OrbbecConfig
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
