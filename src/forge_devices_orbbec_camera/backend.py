"""Orbbec 采集后端抽象层。

此文件定义：
- OrbbeFrame：我们自己的帧数据类，不暴露任何 pyorbbecsdk 类型
- CaptureBackend：采集后端 Protocol，所有具体实现必须遵守
- create_backend：工厂函数，main.py 通过此处获取 backend，无需直接 import backend_orbbec

设计原则：本文件不 import pyorbbecsdk，与 SDK 完全解耦。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import numpy as np

if TYPE_CHECKING:
    from .config import OrbbecConfig


@dataclass(frozen=True)
class OrbbecPointCloud:
    """SDK-independent immutable columns for an organized point cloud."""

    width: int
    height: int
    is_dense: bool
    x: np.ndarray
    y: np.ndarray
    z: np.ndarray
    rgb: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None


@dataclass
class OrbbeFrame:
    """一次采集周期的三路帧数据。

    所有数组均为 numpy / bytes，不含任何 pyorbbecsdk 类型。

    - color:  HWC uint8，shape (H, W, 3)，RGB 顺序。None 表示未采到或仅有 JPEG 透传。
    - depth:  HW float32，shape (H, W)，单位米。None 表示未启用或未采到。
    - ir:     HW uint8 或 uint16，shape (H, W)。None 表示未启用或未采到。
    - timestamp_ms: 帧时间戳（毫秒），来自 SDK，用于调试和日志。
    - color_jpeg: 设备 MJPG 透传字节；与 color 互斥优先用于 jpeg 输出。
    - capture_timestamp_ns: 后端取得该 FrameSet 时的 Unix epoch 纳秒时间；None 表示不可用。
    - point_cloud: 可选 organized XYZ / XYZRGB 点云；不含任何 SDK 对象。
    """

    color: np.ndarray | None
    depth: np.ndarray | None
    ir: np.ndarray | None
    timestamp_ms: int = 0
    color_jpeg: bytes | None = None
    capture_timestamp_ns: int | None = None
    point_cloud: OrbbecPointCloud | None = None

    @property
    def color_shape(self) -> tuple[int, int] | None:
        """返回 Color 帧的 (height, width)，不可用时返回 None。"""
        if self.color is None:
            return None
        h, w = self.color.shape[:2]
        return h, w

    @property
    def depth_shape(self) -> tuple[int, int] | None:
        """返回 Depth 帧的 (height, width)，不可用时返回 None。"""
        if self.depth is None:
            return None
        h, w = self.depth.shape[:2]
        return h, w

    @property
    def ir_shape(self) -> tuple[int, int] | None:
        """返回 IR 帧的 (height, width)，不可用时返回 None。"""
        if self.ir is None:
            return None
        h, w = self.ir.shape[:2]
        return h, w


@dataclass
class DeviceInfo:
    """已连接设备的信息，用于 list-devices 命令。"""

    index: int
    serial: str
    name: str
    firmware_version: str = ""
    usb_type: str = ""


@runtime_checkable
class CaptureBackend(Protocol):
    """采集后端统一接口。

    具体实现：OrbbecBackend（backend_orbbec.py）
    测试用实现：MockBackend（直接在测试文件中定义）
    """

    def capture_frame(self) -> OrbbeFrame:
        """获取最新一帧（三路数据）。

        - 若后台线程已有新帧，立即返回最新帧。
        - 若尚无新帧（如首次调用），阻塞直到收到第一帧。
        - 若设备断开，抛出 RuntimeError。
        """
        ...

    def wait_new_frame(self, after_seq: int, timeout: float = 2.0) -> tuple[OrbbeFrame, int]:
        """等待比 after_seq 更新的一帧，返回 (frame, seq)。

        超时仍返回当前最新帧与其 seq（可能仍等于 after_seq）。
        设备终止时抛出 RuntimeError。
        """
        ...

    def close(self) -> None:
        """停止采集，释放设备资源。"""
        ...


def create_backend(config: OrbbecConfig) -> CaptureBackend:
    """工厂函数：创建 Orbbec 采集后端。

    main.py 通过此函数获取 backend，从而与具体 SDK 解耦。
    替换 SDK 时只需修改 backend_orbbec.py，本函数签名不变。
    """
    if config.capture_process == "isolated":
        from .isolated_backend import IsolatedOrbbecBackend

        return IsolatedOrbbecBackend(config)

    from .backend_orbbec import OrbbecBackend

    return OrbbecBackend(config)
