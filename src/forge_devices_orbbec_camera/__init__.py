"""Orbbec 深度相机采集与 Dora 集成。"""

from .backend import DeviceInfo, OrbbeFrame
from .config import OrbbecConfig

__all__ = ["DeviceInfo", "OrbbeFrame", "OrbbecConfig"]
