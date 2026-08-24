"""Orbbec SDK 包装层。

此文件是项目中唯一 import pyorbbecsdk 的地方。

职责：
- 管理 pyorbbecsdk Pipeline 的生命周期（开启、采集、关闭）
- 在后台线程持续采集，主线程取最新帧（与 Rust backend_v4l.rs 相同模式）
- 将 pyorbbecsdk 的帧类型转换为 OrbbeFrame（不向上层暴露 SDK 类型）
- 处理设备断开、超时等异常

不知道：dora、forge_msgs、tick 机制。

参考：hihihi/pyorbbecsdk/examples/sync_align.py, multi_streams.py
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

import cv2
import numpy as np

import pyorbbecsdk as ob

from .backend import DeviceInfo, OrbbeFrame, OrbbecPointCloud
from .config import OrbbecConfig
from forge_common import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    pass

_MAX_CONSECUTIVE_WAIT_ERRORS = 3


def _freeze_contiguous(value: np.ndarray, dtype: np.dtype) -> np.ndarray:
    """Return a C-contiguous array and make its complete base chain read-only."""
    array = np.asarray(value, dtype=dtype)
    if not array.flags.c_contiguous or not array.flags.aligned:
        array = np.array(array, dtype=dtype, order="C", copy=True)
    current: object | None = array
    while isinstance(current, np.ndarray):
        current.setflags(write=False)
        current = current.base
    return array


def _point_cloud_from_sdk_buffer(
    data: object,
    depth_m: np.ndarray,
    *,
    width: int,
    height: int,
    position_value_scale: float,
    colorized: bool,
) -> OrbbecPointCloud:
    """Detach an Orbbec POINT/RGB_POINT AoS buffer into PointCloud v1 columns."""
    if width <= 0 or height <= 0:
        raise ValueError(f"Orbbec point-cloud dimensions must be positive, got {width}x{height}")

    depth = np.asarray(depth_m)
    if depth.dtype != np.dtype(np.float32) or depth.ndim != 2:
        raise ValueError("Orbbec point-cloud depth must be a float32 image")
    if depth.shape != (height, width):
        raise ValueError(
            "Orbbec point-cloud dimensions do not match the published depth image"
        )

    try:
        scale = float(position_value_scale)
    except (TypeError, ValueError) as e:
        raise ValueError("Orbbec point-cloud position scale is invalid") from e
    if not np.isfinite(scale) or scale <= 0:
        raise ValueError(
            f"Orbbec point-cloud position scale must be finite and positive, got {scale}"
        )

    component_count = 6 if colorized else 3
    point_count = width * height
    expected_values = point_count * component_count
    expected_bytes = expected_values * np.dtype(np.float32).itemsize
    try:
        buffer = memoryview(data)
        if not buffer.contiguous:
            raise ValueError("Orbbec point-cloud SDK buffer is not contiguous")
        if buffer.nbytes != expected_bytes:
            raise ValueError(
                "Orbbec point-cloud SDK buffer has an unexpected size: "
                f"expected {expected_bytes} bytes, got {buffer.nbytes}"
            )
        values = np.frombuffer(buffer, dtype=np.float32)
    except (BufferError, TypeError, ValueError) as e:
        if isinstance(e, ValueError) and str(e).startswith("Orbbec point-cloud"):
            raise
        raise ValueError("Orbbec point-cloud SDK buffer is invalid") from e
    if values.size != expected_values:
        raise ValueError(
            "Orbbec point-cloud SDK buffer has an unexpected float count"
        )

    matrix = values.reshape(point_count, component_count)
    scale_m = np.float32(scale * 0.001)
    shape = (height, width)
    x = np.array(matrix[:, 0] * scale_m, dtype=np.float32, order="C", copy=True).reshape(shape)
    y = np.array(matrix[:, 1] * scale_m, dtype=np.float32, order="C", copy=True).reshape(shape)
    z = np.array(matrix[:, 2] * scale_m, dtype=np.float32, order="C", copy=True).reshape(shape)

    valid = np.isfinite(depth) & (depth > 0)
    valid &= np.isfinite(x) & np.isfinite(y) & np.isfinite(z)
    invalid = ~valid
    if np.any(invalid):
        x[invalid] = np.nan
        y[invalid] = np.nan
        z[invalid] = np.nan

    rgb: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None
    if colorized:
        source_rgb = matrix[:, 3:6]
        finite_rgb = np.isfinite(source_rgb)
        safe_rgb = np.where(finite_rgb, source_rgb, np.float32(0.0))
        packed_rgb = np.clip(np.rint(safe_rgb), 0, 255).astype(np.uint8)
        red = np.array(packed_rgb[:, 0], dtype=np.uint8, order="C", copy=True).reshape(shape)
        green = np.array(packed_rgb[:, 1], dtype=np.uint8, order="C", copy=True).reshape(shape)
        blue = np.array(packed_rgb[:, 2], dtype=np.uint8, order="C", copy=True).reshape(shape)
        if np.any(invalid):
            red[invalid] = 0
            green[invalid] = 0
            blue[invalid] = 0
        rgb = (red, green, blue)

    float32 = np.dtype(np.float32)
    x = _freeze_contiguous(x, float32)
    y = _freeze_contiguous(y, float32)
    z = _freeze_contiguous(z, float32)
    if rgb is not None:
        uint8 = np.dtype(np.uint8)
        rgb = tuple(_freeze_contiguous(channel, uint8) for channel in rgb)

    return OrbbecPointCloud(
        width=width,
        height=height,
        is_dense=bool(valid.all()),
        x=x,
        y=y,
        z=z,
        rgb=rgb,
    )


def _create_point_cloud_processor(
    enabled: bool,
    colorize: bool,
) -> object | None:
    """Create the optional SDK processor without making camera startup fatal."""
    if not enabled:
        return None
    try:
        processor = ob.PointCloudFilter()
        processor.set_color_data_normalization(False)
        processor.set_create_point_format(
            ob.OBFormat.RGB_POINT if colorize else ob.OBFormat.POINT
        )
        return processor
    except Exception as e:
        logger.warning("[orbbec_camera] 点云处理器初始化失败，继续输出图像: %s", e)
        return None


def _reshape_raw_frame(
    data: object,
    *,
    dtype: np.dtype,
    shape: tuple[int, ...],
    stream: str,
    format_name: str,
) -> np.ndarray | None:
    """Validate a raw SDK buffer before interpreting its pixels."""
    if any(dimension <= 0 for dimension in shape):
        logger.warning(
            "[orbbec_camera] %s %s 帧尺寸非法: %s",
            stream,
            format_name,
            shape,
        )
        return None

    expected_elements = 1
    for dimension in shape:
        expected_elements *= dimension
    target_dtype = np.dtype(dtype)
    expected_bytes = expected_elements * target_dtype.itemsize

    try:
        buffer = np.asanyarray(data)
        actual_bytes = buffer.nbytes
        if actual_bytes != expected_bytes:
            logger.warning(
                "[orbbec_camera] %s %s 帧缓冲区长度异常: 期望 %s bytes "
                "(%s elements)，实际 %s bytes",
                stream,
                format_name,
                expected_bytes,
                expected_elements,
                actual_bytes,
            )
            return None
        raw = np.frombuffer(buffer, dtype=target_dtype)
        if raw.size != expected_elements:
            logger.warning(
                "[orbbec_camera] %s %s 帧元素数异常: 期望 %s，实际 %s",
                stream,
                format_name,
                expected_elements,
                raw.size,
            )
            return None
        return raw.reshape(shape)
    except (BufferError, TypeError, ValueError) as e:
        logger.warning(
            "[orbbec_camera] %s %s 帧缓冲区解析失败: %s",
            stream,
            format_name,
            e,
        )
        return None


_COLOR_FORMAT_MAP = {
    "rgb8": ob.OBFormat.RGB,
    "jpeg": ob.OBFormat.MJPG,
    "yuyv": ob.OBFormat.YUYV,
}

_DEPTH_FORMAT_MAP = {
    "y16": ob.OBFormat.Y16,
    "y14": ob.OBFormat.Y14,
    "rle": ob.OBFormat.RLE,
}

_IR_FORMAT_MAP = {
    "ir8":  ob.OBFormat.Y8,
    "mjpg": ob.OBFormat.MJPG,
    # ir16/Y16 在 Gemini 2 IR 流中不可用（实测），保留映射仅作兼容兜底
    "ir16": ob.OBFormat.Y16,
}


class OrbbecBackend:
    """pyorbbecsdk Pipeline 的封装实现。

    采用后台线程持续采集：
    - 后台线程：不断调用 pipeline.wait_for_frames()，将最新 OrbbeFrame 存入缓存。
    - 消费端：capture_frame() 取最新帧；wait_new_frame() 等待更新的帧序号。
    """

    def __init__(self, config: OrbbecConfig) -> None:
        self._config = config
        self._latest_frame: OrbbeFrame | None = None
        self._frame_seq: int = 0
        self._lock = threading.Lock()
        self._frame_cond = threading.Condition(self._lock)
        self._first_frame_event = threading.Event()
        self._stop_event = threading.Event()
        self._init_done = threading.Event()
        self._init_error: str | None = None
        self._terminal_error: str | None = None
        # SDK 后处理滤波链（在 _capture_loop 中初始化，每帧按序应用）
        self._depth_sdk_filters: list = []
        # 是否已通过硬件写入深度范围（True 则深度转换跳过软件裁剪）
        self._depth_hw_range_ok: bool = False
        # Created in the capture thread after pipeline.start(), matching the SDK
        # examples and keeping all filter lifecycle work on the owning thread.
        self._point_cloud: object | None = None
        self._point_cloud_failure_logged = False

        self._thread = threading.Thread(
            target=self._capture_loop,
            name="orbbec-capture",
            daemon=True,
        )
        self._thread.start()

        if not self._init_done.wait(timeout=config.init_timeout_sec):
            self._stop_event.set()
            with self._frame_cond:
                self._frame_cond.notify_all()
            self._thread.join(timeout=5.0)
            raise RuntimeError(
                f"Orbbec 设备初始化超时 ({config.init_timeout_sec}s)，"
                "请检查设备是否连接、udev 规则是否已配置。"
            )
        if self._init_error:
            self._thread.join(timeout=5.0)
            raise RuntimeError(f"Orbbec 设备初始化失败: {self._init_error}")

    # ------------------------------------------------------------------
    # CaptureBackend Protocol 实现
    # ------------------------------------------------------------------

    def capture_frame(self) -> OrbbeFrame:
        """取最新帧。若尚无帧则阻塞，若设备断开则抛出 RuntimeError。"""
        if not self._first_frame_event.wait(timeout=5.0):
            if not self._thread.is_alive():
                raise RuntimeError("Orbbec 采集线程已退出，请检查设备是否断开。")
            raise RuntimeError("等待 Orbbec 首帧超时（5s），设备可能无数据输出。")

        with self._lock:
            terminal_error = self._terminal_error
            frame = self._latest_frame
        if terminal_error is not None:
            raise RuntimeError(f"Orbbec 采集已终止: {terminal_error}")
        if frame is None:
            raise RuntimeError("Orbbec 采集线程已退出，请检查设备是否断开。")
        return frame

    def wait_new_frame(self, after_seq: int, timeout: float = 2.0) -> tuple[OrbbeFrame, int]:
        """等待比 after_seq 更新的帧；超时则返回当前最新帧及其序号。"""
        if not self._first_frame_event.wait(timeout=max(timeout, 0.0)):
            if not self._thread.is_alive():
                raise RuntimeError("Orbbec 采集线程已退出，请检查设备是否断开。")
            raise RuntimeError("等待 Orbbec 首帧超时，设备可能无数据输出。")

        deadline = time.monotonic() + max(timeout, 0.0)
        with self._frame_cond:
            while True:
                if self._terminal_error is not None:
                    raise RuntimeError(f"Orbbec 采集已终止: {self._terminal_error}")
                if self._latest_frame is not None and self._frame_seq > after_seq:
                    return self._latest_frame, self._frame_seq
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if self._latest_frame is None:
                        raise RuntimeError("Orbbec 采集线程已退出，请检查设备是否断开。")
                    return self._latest_frame, self._frame_seq
                self._frame_cond.wait(timeout=remaining)

    def close(self) -> None:
        """停止采集线程，释放 Pipeline。"""
        self._stop_event.set()
        with self._frame_cond:
            self._frame_cond.notify_all()
        self._thread.join(timeout=5.0)

    # ------------------------------------------------------------------
    # 内部：采集线程
    # ------------------------------------------------------------------

    def _capture_loop(self) -> None:
        pipeline: ob.Pipeline | None = None
        try:
            if self._config.connect_delay_ms > 0:
                if self._stop_event.wait(self._config.connect_delay_ms / 1000.0):
                    return

            pipeline = self._create_pipeline()
            cfg = self._config
            # 与 Sample Viewer 一致：在选深度 profile 之前切换 depth work mode
            if cfg.depth.enabled and cfg.depth.depth_work_mode:
                try:
                    st = pipeline.get_device().set_depth_work_mode(cfg.depth.depth_work_mode)
                    logger.info("[orbbec_camera] depth_work_mode → %r (%s)", cfg.depth.depth_work_mode, st)
                except Exception as e:
                    logger.warning("[orbbec_camera] depth_work_mode %r 失败: %s", cfg.depth.depth_work_mode, e)

            ob_config = self._build_ob_config(pipeline)
            align_filter = self._configure_alignment(ob_config)

            # 硬件帧同步
            if self._config.frame_sync:
                try:
                    pipeline.enable_frame_sync()
                except Exception as e:
                    logger.warning("[orbbec_camera] 硬件帧同步启用失败: %s", e)

            pipeline.start(ob_config)

            # 等待流稳定后再设置所有属性（设备 start 时会重置属性，必须在此之后设置）
            if self._stop_event.wait(1.0):
                return
            self._apply_and_verify_properties(pipeline)
            # 构建 SDK 后处理滤波链（必须在 pipeline.start() 之后）
            self._build_depth_sdk_filters(pipeline)
            self._point_cloud = _create_point_cloud_processor(
                cfg.point_cloud.enabled,
                cfg.point_cloud.colorize,
            )
            if (
                cfg.point_cloud.enabled
                and cfg.point_cloud.colorize
                and not cfg.frame_sync
            ):
                logger.warning(
                    "[orbbec_camera] 彩色点云未启用 frame_sync；空间对齐有效，"
                    "但动态场景的 Color/Depth 时间对应仅为 best-effort"
                )

            self._init_done.set()

            consecutive_wait_errors = 0
            prewarm_remaining = self._config.prewarm_frames
            while not self._stop_event.is_set():
                try:
                    frameset = pipeline.wait_for_frames(2000)
                except Exception as e:
                    if self._is_disconnect_error(e):
                        raise
                    consecutive_wait_errors += 1
                    if consecutive_wait_errors >= _MAX_CONSECUTIVE_WAIT_ERRORS:
                        raise RuntimeError(
                            "连续 "
                            f"{consecutive_wait_errors} 次 wait_for_frames 失败，采集终止: {e}"
                        ) from e
                    logger.warning(
                        "[orbbec_camera] wait_for_frames 失败 (%s/%s): %s",
                        consecutive_wait_errors,
                        _MAX_CONSECUTIVE_WAIT_ERRORS,
                        e,
                    )
                    continue

                if not frameset:
                    continue
                consecutive_wait_errors = 0
                capture_timestamp_ns = time.time_ns()

                # 软件模式才使用 AlignFilter；硬件模式已由 Config.set_align_mode 配置。
                if align_filter is not None:
                    frameset = align_filter.process(frameset)
                    if not frameset:
                        continue
                    frameset = frameset.as_frame_set()

                if prewarm_remaining > 0:
                    prewarm_remaining -= 1
                    continue

                frame = self._convert_frameset(frameset)
                frame.capture_timestamp_ns = capture_timestamp_ns
                with self._frame_cond:
                    self._latest_frame = frame
                    self._frame_seq += 1
                    self._frame_cond.notify_all()
                self._first_frame_event.set()

        except Exception as e:
            self._init_error = str(e)
            with self._frame_cond:
                self._terminal_error = str(e)
                self._latest_frame = None
                self._frame_cond.notify_all()
            self._init_done.set()
            self._first_frame_event.set()
        finally:
            if pipeline is not None:
                try:
                    pipeline.stop()
                except Exception:
                    pass
            with self._frame_cond:
                self._frame_cond.notify_all()

    def _configure_alignment(self, ob_config: ob.Config) -> ob.AlignFilter | None:
        """配置对齐模式；硬件模式不允许静默降级为软件实现。"""
        mode = self._config.align_mode
        if mode == "disable":
            if hasattr(ob_config, "set_align_mode") and hasattr(ob, "OBAlignMode"):
                ob_config.set_align_mode(ob.OBAlignMode.DISABLE)
            return None
        if mode == "sw":
            if hasattr(ob_config, "set_align_mode") and hasattr(ob, "OBAlignMode"):
                ob_config.set_align_mode(ob.OBAlignMode.DISABLE)
            return ob.AlignFilter(align_to_stream=ob.OBStreamType.COLOR_STREAM)

        set_align_mode = getattr(ob_config, "set_align_mode", None)
        align_modes = getattr(ob, "OBAlignMode", None)
        hw_mode = getattr(align_modes, "HW_MODE", None) if align_modes is not None else None
        if set_align_mode is None or hw_mode is None:
            raise RuntimeError(
                "align_mode=hw 需要 pyorbbecsdk Config.set_align_mode(OBAlignMode.HW_MODE)，"
                "当前 SDK 不支持硬件对齐"
            )
        try:
            set_align_mode(hw_mode)
        except Exception as e:
            raise RuntimeError(f"align_mode=hw 硬件对齐配置失败: {e}") from e
        return None

    def _create_pipeline(self) -> ob.Pipeline:
        """按配置创建 Pipeline，支持按序列号或索引选择设备。"""
        serial = self._config.device_serial
        ctx = ob.Context()
        device_list = ctx.query_devices()
        count = device_list.get_count()

        if count == 0:
            raise RuntimeError(
                "未发现 Orbbec 设备，请检查 USB 连接，并运行 init-device 或 install_permissions.sh 检查权限。"
            )

        if serial:
            device = device_list.get_device_by_serial_number(serial)
            if device is None:
                raise RuntimeError(
                    f"未找到序列号为 {serial!r} 的 Orbbec 设备（共发现 {count} 个设备）。"
                )
            return ob.Pipeline(device)

        device = device_list.get_device_by_index(self._config.device_index)
        return ob.Pipeline(device)

    def _build_ob_config(self, pipeline: ob.Pipeline) -> ob.Config:
        """根据配置构建 pyorbbecsdk Config（参考 sync_align.py）。"""
        cfg = self._config
        ob_cfg = ob.Config()

        # Color 流：用 0,0,format,0 让 SDK 选择最优分辨率，也可指定具体值
        color_profiles = pipeline.get_stream_profile_list(ob.OBSensorType.COLOR_SENSOR)
        color_profile = color_profiles.get_video_stream_profile(
            cfg.color.width, cfg.color.height,
            _COLOR_FORMAT_MAP[cfg.color.format], cfg.color.fps,
        )
        ob_cfg.enable_stream(color_profile)

        # Depth 流：按配置格式 + 分辨率选 profile（SDK 均输出 Y16，格式影响 USB 传输编码）
        if cfg.depth.enabled:
            depth_profiles = pipeline.get_stream_profile_list(ob.OBSensorType.DEPTH_SENSOR)
            depth_fmt = _DEPTH_FORMAT_MAP.get(cfg.depth.format, ob.OBFormat.Y16)
            try:
                depth_profile = depth_profiles.get_video_stream_profile(
                    cfg.depth.width, cfg.depth.height, depth_fmt, cfg.depth.fps,
                )
            except Exception as e:
                logger.warning(
                    "[orbbec_camera] depth %sx%s@%s %s 未找到，回退 Y16: %s",
                    cfg.depth.width,
                    cfg.depth.height,
                    cfg.depth.fps,
                    cfg.depth.format,
                    e,
                )
                try:
                    depth_profile = depth_profiles.get_video_stream_profile(
                        cfg.depth.width, cfg.depth.height, ob.OBFormat.Y16, cfg.depth.fps,
                    )
                except Exception:
                    depth_profile = depth_profiles.get_default_video_stream_profile()
            ob_cfg.enable_stream(depth_profile)

        # IR 流
        if cfg.ir.enabled:
            ir_profiles = pipeline.get_stream_profile_list(ob.OBSensorType.IR_SENSOR)
            ir_profile = ir_profiles.get_video_stream_profile(
                cfg.ir.width, cfg.ir.height,
                _IR_FORMAT_MAP[cfg.ir.format], cfg.ir.fps,
            )
            ob_cfg.enable_stream(ir_profile)

        # 要求 frameset 中所有已启用的流都到齐才输出（参考 sync_align.py）
        ob_cfg.set_frame_aggregate_output_mode(
            ob.OBFrameAggregateOutputMode.FULL_FRAME_REQUIRE
        )

        return ob_cfg

    # ------------------------------------------------------------------
    # Device property 设置（pipeline 启动后调用，全部实时生效）
    # ------------------------------------------------------------------

    def _set_prop_bool(self, device: ob.Device, prop_id: ob.OBPropertyID, value: bool, name: str) -> None:
        try:
            device.set_bool_property(prop_id, value)
        except Exception as e:
            logger.warning("[orbbec_camera] %s=%s 设置失败（设备可能不支持）: %s", name, value, e)

    def _set_prop_int(self, device: ob.Device, prop_id: ob.OBPropertyID, value: int | None, name: str) -> None:
        if value is None:
            return
        try:
            device.set_int_property(prop_id, value)
        except Exception as e:
            logger.warning("[orbbec_camera] %s=%s 设置失败（设备可能不支持）: %s", name, value, e)

    def _set_prop_float(self, device: ob.Device, prop_id: ob.OBPropertyID, value: float | None, name: str) -> None:
        if value is None:
            return
        try:
            device.set_float_property(prop_id, value)
        except Exception as e:
            logger.warning("[orbbec_camera] %s=%s 设置失败（设备可能不支持）: %s", name, value, e)

    def _apply_and_verify_properties(self, pipeline: ob.Pipeline) -> None:
        """在 pipeline 稳定后设置所有属性，并读回验证是否生效。"""
        device = pipeline.get_device()
        P = ob.OBPropertyID
        c = self._config.color
        d = self._config.depth
        ir = self._config.ir

        logger.info("[orbbec_camera] === 开始应用属性 ===")

        # ── Color 方向（硬件，与 Viewer toggleMirror/toggleFlip 逻辑一致）──
        self._set_and_verify_bool(device, P.OB_PROP_COLOR_MIRROR_BOOL, c.mirror, "color.mirror")
        self._set_and_verify_bool(device, P.OB_PROP_COLOR_FLIP_BOOL, c.flip, "color.flip")
        if c.rotate != 0:
            self._set_and_verify_int(device, P.OB_PROP_COLOR_ROTATE_INT, c.rotate, "color.rotate")

        # ── Depth 方向（硬件，与 Viewer toggleDepthMirror/toggleDepthFlip 逻辑一致）──
        self._set_and_verify_bool(device, P.OB_PROP_DEPTH_MIRROR_BOOL, d.mirror, "depth.mirror")
        self._set_and_verify_bool(device, P.OB_PROP_DEPTH_FLIP_BOOL, d.flip, "depth.flip")
        if d.rotate != 0:
            self._set_and_verify_int(device, P.OB_PROP_DEPTH_ROTATE_INT, d.rotate, "depth.rotate")

        # ── IR 方向（硬件，与 Viewer toggleMirror/toggleFlip for IR 逻辑一致）──
        self._set_and_verify_bool(device, P.OB_PROP_IR_MIRROR_BOOL, ir.mirror, "ir.mirror")
        self._set_and_verify_bool(device, P.OB_PROP_IR_FLIP_BOOL, ir.flip, "ir.flip")
        if ir.rotate != 0:
            self._set_and_verify_int(device, P.OB_PROP_IR_ROTATE_INT, ir.rotate, "ir.rotate")

        # ── Color 图像质量 ──
        self._set_and_verify_bool(device, P.OB_PROP_COLOR_AUTO_EXPOSURE_BOOL, c.auto_exposure, "color.auto_exposure")
        if not c.auto_exposure:
            self._set_and_verify_int(device, P.OB_PROP_COLOR_EXPOSURE_INT, c.exposure_us, "color.exposure_us")
            self._set_and_verify_int(device, P.OB_PROP_COLOR_GAIN_INT, c.gain, "color.gain")
        self._set_and_verify_bool(device, P.OB_PROP_COLOR_AUTO_WHITE_BALANCE_BOOL, c.auto_white_balance, "color.auto_white_balance")
        if not c.auto_white_balance:
            self._set_and_verify_int(device, P.OB_PROP_COLOR_WHITE_BALANCE_INT, c.white_balance, "color.white_balance")
        self._set_and_verify_int(device, P.OB_PROP_COLOR_BRIGHTNESS_INT, c.brightness, "color.brightness")
        self._set_and_verify_int(device, P.OB_PROP_COLOR_SHARPNESS_INT, c.sharpness, "color.sharpness")
        self._set_and_verify_int(device, P.OB_PROP_COLOR_SATURATION_INT, c.saturation, "color.saturation")
        self._set_and_verify_int(device, P.OB_PROP_COLOR_CONTRAST_INT, c.contrast, "color.contrast")
        self._set_and_verify_int(device, P.OB_PROP_COLOR_POWER_LINE_FREQUENCY_INT, c.power_line_frequency, "color.power_line_frequency")

        # ── Depth ──
        self._set_and_verify_bool(device, P.OB_PROP_DEPTH_AUTO_EXPOSURE_BOOL, d.auto_exposure, "depth.auto_exposure")
        if not d.auto_exposure:
            self._set_and_verify_int(device, P.OB_PROP_DEPTH_EXPOSURE_INT, d.exposure, "depth.exposure")
            self._set_and_verify_int(device, P.OB_PROP_DEPTH_GAIN_INT, d.gain, "depth.gain")
        # 深度单位（OB_PROP_DEPTH_UNIT_FLEXIBLE_ADJUSTMENT_FLOAT）
        if d.depth_unit is not None:
            try:
                device.set_float_property(P.OB_PROP_DEPTH_UNIT_FLEXIBLE_ADJUSTMENT_FLOAT, d.depth_unit)
                actual = device.get_float_property(P.OB_PROP_DEPTH_UNIT_FLEXIBLE_ADJUSTMENT_FLOAT)
                mark = "✅" if abs(actual - d.depth_unit) < 0.001 else "⚠️"
                logger.info("  %s depth.depth_unit: %s → %s", mark, d.depth_unit, actual)
            except Exception as e:
                logger.warning("  ❌ depth.depth_unit=%s: %s", d.depth_unit, e)
        # 硬件深度范围（Gemini 2 若支持则直接写设备；不支持则 _extract_depth 软件裁剪兜底）
        self._depth_hw_range_ok = False
        if d.min_mm > 0 or d.max_mm < 10000:
            try:
                device.set_int_property(P.OB_PROP_MIN_DEPTH_INT, d.min_mm)
                device.set_int_property(P.OB_PROP_MAX_DEPTH_INT, d.max_mm)
                r_min = device.get_int_property(P.OB_PROP_MIN_DEPTH_INT)
                r_max = device.get_int_property(P.OB_PROP_MAX_DEPTH_INT)
                logger.info("  ✅ depth hw range: [%s, %s] mm", r_min, r_max)
                self._depth_hw_range_ok = True
            except Exception as e:
                logger.warning("  ⚠️ depth hw range 不支持，将软件裁剪: %s", e)
        self._set_and_verify_bool(device, P.OB_PROP_DEPTH_NOISE_REMOVAL_FILTER_BOOL, d.noise_removal_filter, "depth.noise_removal_filter")
        self._set_and_verify_int(device, P.OB_PROP_DEPTH_NOISE_REMOVAL_FILTER_MAX_DIFF_INT, d.noise_removal_max_diff, "depth.noise_removal_max_diff")
        self._set_and_verify_int(device, P.OB_PROP_DEPTH_NOISE_REMOVAL_FILTER_MAX_SPECKLE_SIZE_INT, d.noise_removal_max_speckle, "depth.noise_removal_max_speckle")
        self._set_and_verify_bool(device, P.OB_PROP_DEPTH_HOLEFILTER_BOOL, d.hole_filter, "depth.hole_filter")
        self._set_and_verify_int(device, P.OB_PROP_DEPTH_PRECISION_LEVEL_INT, d.precision_level, "depth.precision_level")
        self._set_and_verify_bool(device, P.OB_PROP_DISPARITY_TO_DEPTH_BOOL, d.disparity_to_depth, "depth.disparity_to_depth")
        if d.post_filter is not None:
            self._set_prop_bool(device, P.OB_PROP_DEPTH_POSTFILTER_BOOL, d.post_filter, "depth.post_filter")
        if d.soft_filter is not None:
            self._set_prop_bool(device, P.OB_PROP_DEPTH_SOFT_FILTER_BOOL, d.soft_filter, "depth.soft_filter")
        self._set_prop_int(device, P.OB_PROP_DEPTH_MAX_DIFF_INT, d.soft_filter_max_diff, "depth.soft_filter_max_diff")
        self._set_prop_int(device, P.OB_PROP_DEPTH_MAX_SPECKLE_SIZE_INT, d.soft_filter_max_speckle, "depth.soft_filter_max_speckle")
        if d.rm_filter is not None:
            self._set_prop_bool(device, P.OB_PROP_DEPTH_RM_FILTER_BOOL, d.rm_filter, "depth.rm_filter")

        # ── IR ──
        self._set_and_verify_bool(device, P.OB_PROP_IR_AUTO_EXPOSURE_BOOL, ir.auto_exposure, "ir.auto_exposure")
        if not ir.auto_exposure:
            self._set_and_verify_int(device, P.OB_PROP_IR_EXPOSURE_INT, ir.exposure_us, "ir.exposure_us")
            self._set_and_verify_int(device, P.OB_PROP_IR_GAIN_INT, ir.gain, "ir.gain")
        self._set_and_verify_int(device, P.OB_PROP_IR_CHANNEL_DATA_SOURCE_INT, ir.channel_data_source, "ir.channel_data_source")

        # ── Laser ──
        self._set_and_verify_bool(device, P.OB_PROP_LDP_BOOL, self._config.laser.ldp_enabled, "laser.ldp_enabled")
        self._set_and_verify_bool(device, P.OB_PROP_LASER_BOOL, self._config.laser.enabled, "laser.enabled")
        self._set_and_verify_int(device, P.OB_PROP_LASER_POWER_LEVEL_CONTROL_INT, self._config.laser.power_level, "laser.power_level")

        logger.info("[orbbec_camera] === 属性应用完成 ===")

    def _set_and_verify_bool(self, device: ob.Device, prop_id: ob.OBPropertyID, value: bool, name: str) -> None:
        try:
            device.set_bool_property(prop_id, value)
            actual = device.get_bool_property(prop_id)
            mark = "✅" if actual == value else "⚠️"
            logger.info("  %s %s: → %s", mark, name, actual)
        except Exception as e:
            logger.warning("  ❌ %s: %s", name, e)

    def _set_and_verify_int(self, device: ob.Device, prop_id: ob.OBPropertyID, value: int | None, name: str) -> None:
        if value is None:
            return
        try:
            device.set_int_property(prop_id, int(value))
            actual = device.get_int_property(prop_id)
            mark = "✅" if actual == value else "⚠️"
            logger.info("  %s %s: %s → %s", mark, name, value, actual)
        except Exception as e:
            logger.warning("  ❌ %s=%s: %s", name, value, e)

    def _apply_all_properties(self, pipeline: ob.Pipeline) -> None:
        """兼容旧调用入口。"""
        self._apply_and_verify_properties(pipeline)

    def _apply_orientation_properties(self, device: ob.Device) -> None:
        """方向类属性（mirror/flip/rotate），硬件写入，与 Viewer 逻辑一致。"""
        P = ob.OBPropertyID
        c, d, ir = self._config.color, self._config.depth, self._config.ir
        self._set_prop_bool(device, P.OB_PROP_COLOR_MIRROR_BOOL, c.mirror, "color.mirror")
        self._set_prop_bool(device, P.OB_PROP_COLOR_FLIP_BOOL, c.flip, "color.flip")
        if c.rotate != 0:
            self._set_prop_int(device, P.OB_PROP_COLOR_ROTATE_INT, c.rotate, "color.rotate")
        self._set_prop_bool(device, P.OB_PROP_DEPTH_MIRROR_BOOL, d.mirror, "depth.mirror")
        self._set_prop_bool(device, P.OB_PROP_DEPTH_FLIP_BOOL, d.flip, "depth.flip")
        if d.rotate != 0:
            self._set_prop_int(device, P.OB_PROP_DEPTH_ROTATE_INT, d.rotate, "depth.rotate")
        self._set_prop_bool(device, P.OB_PROP_IR_MIRROR_BOOL, ir.mirror, "ir.mirror")
        self._set_prop_bool(device, P.OB_PROP_IR_FLIP_BOOL, ir.flip, "ir.flip")
        if ir.rotate != 0:
            self._set_prop_int(device, P.OB_PROP_IR_ROTATE_INT, ir.rotate, "ir.rotate")

    def _apply_image_properties(self, pipeline: ob.Pipeline) -> None:
        """图像质量、曝光、激光等属性，在 pipeline.start() 之后设置。"""
        device = pipeline.get_device()
        self._apply_color_image_properties(device)
        self._apply_depth_image_properties(device)
        self._apply_ir_image_properties(device)
        self._apply_laser_properties(device)

    def _apply_color_image_properties(self, device: ob.Device) -> None:
        """Color 图像质量、曝光、白平衡（pipeline start 后设置）。"""
        c = self._config.color
        P = ob.OBPropertyID
        logger.info(
            "[orbbec_camera] Color: auto_exp=%s brightness=%s saturation=%s contrast=%s sharpness=%s",
            c.auto_exposure,
            c.brightness,
            c.saturation,
            c.contrast,
            c.sharpness,
        )
        self._set_prop_bool(device, P.OB_PROP_COLOR_AUTO_EXPOSURE_BOOL, c.auto_exposure, "color.auto_exposure")
        if not c.auto_exposure:
            self._set_prop_int(device, P.OB_PROP_COLOR_EXPOSURE_INT, c.exposure_us, "color.exposure_us")
            self._set_prop_int(device, P.OB_PROP_COLOR_GAIN_INT, c.gain, "color.gain")
        self._set_prop_bool(device, P.OB_PROP_COLOR_AUTO_WHITE_BALANCE_BOOL, c.auto_white_balance, "color.auto_white_balance")
        if not c.auto_white_balance:
            self._set_prop_int(device, P.OB_PROP_COLOR_WHITE_BALANCE_INT, c.white_balance, "color.white_balance")
        self._set_prop_int(device, P.OB_PROP_COLOR_BRIGHTNESS_INT, c.brightness, "color.brightness")
        self._set_prop_int(device, P.OB_PROP_COLOR_SHARPNESS_INT, c.sharpness, "color.sharpness")
        self._set_prop_int(device, P.OB_PROP_COLOR_SATURATION_INT, c.saturation, "color.saturation")
        self._set_prop_int(device, P.OB_PROP_COLOR_CONTRAST_INT, c.contrast, "color.contrast")
        self._set_prop_int(device, P.OB_PROP_COLOR_POWER_LINE_FREQUENCY_INT, c.power_line_frequency, "color.power_line_frequency")

    def _apply_color_properties(self, device: ob.Device) -> None:
        """兼容旧调用。"""
        self._apply_color_image_properties(device)

    def _apply_depth_image_properties(self, device: ob.Device) -> None:
        """Depth 曝光、滤波、精度（pipeline start 后设置）。"""
        d = self._config.depth
        P = ob.OBPropertyID
        logger.info(
            "[orbbec_camera] Depth: auto_exp=%s noise_removal=%s hole_filter=%s precision=%s",
            d.auto_exposure,
            d.noise_removal_filter,
            d.hole_filter,
            d.precision_level,
        )
        self._set_prop_bool(device, P.OB_PROP_DEPTH_AUTO_EXPOSURE_BOOL, d.auto_exposure, "depth.auto_exposure")
        if not d.auto_exposure:
            self._set_prop_int(device, P.OB_PROP_DEPTH_EXPOSURE_INT, d.exposure, "depth.exposure")
            self._set_prop_int(device, P.OB_PROP_DEPTH_GAIN_INT, d.gain, "depth.gain")
        self._set_prop_bool(device, P.OB_PROP_DEPTH_NOISE_REMOVAL_FILTER_BOOL, d.noise_removal_filter, "depth.noise_removal_filter")
        self._set_prop_int(device, P.OB_PROP_DEPTH_NOISE_REMOVAL_FILTER_MAX_DIFF_INT, d.noise_removal_max_diff, "depth.noise_removal_max_diff")
        self._set_prop_int(device, P.OB_PROP_DEPTH_NOISE_REMOVAL_FILTER_MAX_SPECKLE_SIZE_INT, d.noise_removal_max_speckle, "depth.noise_removal_max_speckle")
        self._set_prop_bool(device, P.OB_PROP_DEPTH_HOLEFILTER_BOOL, d.hole_filter, "depth.hole_filter")
        self._set_prop_int(device, P.OB_PROP_DEPTH_PRECISION_LEVEL_INT, d.precision_level, "depth.precision_level")
        self._set_prop_bool(device, P.OB_PROP_DISPARITY_TO_DEPTH_BOOL, d.disparity_to_depth, "depth.disparity_to_depth")
        if d.post_filter is not None:
            self._set_prop_bool(device, P.OB_PROP_DEPTH_POSTFILTER_BOOL, d.post_filter, "depth.post_filter")
        if d.soft_filter is not None:
            self._set_prop_bool(device, P.OB_PROP_DEPTH_SOFT_FILTER_BOOL, d.soft_filter, "depth.soft_filter")
        self._set_prop_int(device, P.OB_PROP_DEPTH_MAX_DIFF_INT, d.soft_filter_max_diff, "depth.soft_filter_max_diff")
        self._set_prop_int(device, P.OB_PROP_DEPTH_MAX_SPECKLE_SIZE_INT, d.soft_filter_max_speckle, "depth.soft_filter_max_speckle")
        if d.rm_filter is not None:
            self._set_prop_bool(device, P.OB_PROP_DEPTH_RM_FILTER_BOOL, d.rm_filter, "depth.rm_filter")

    def _apply_depth_properties(self, device: ob.Device) -> None:
        """兼容旧调用。"""
        self._apply_depth_image_properties(device)

    def _apply_ir_image_properties(self, device: ob.Device) -> None:
        """IR 曝光、通道（pipeline start 后设置）。"""
        ir = self._config.ir
        P = ob.OBPropertyID
        self._set_prop_bool(device, P.OB_PROP_IR_AUTO_EXPOSURE_BOOL, ir.auto_exposure, "ir.auto_exposure")
        if not ir.auto_exposure:
            self._set_prop_int(device, P.OB_PROP_IR_EXPOSURE_INT, ir.exposure_us, "ir.exposure_us")
            self._set_prop_int(device, P.OB_PROP_IR_GAIN_INT, ir.gain, "ir.gain")
        self._set_prop_int(device, P.OB_PROP_IR_CHANNEL_DATA_SOURCE_INT, ir.channel_data_source, "ir.channel_data_source")

    def _apply_ir_properties(self, device: ob.Device) -> None:
        """兼容旧调用。"""
        self._apply_ir_image_properties(device)

    def _build_depth_sdk_filters(self, pipeline: ob.Pipeline) -> None:
        """构建 SDK 后处理滤波链。

        从 sensor.get_recommended_filters() 获取设备推荐的全部滤波器实例，
        按配置决定每个滤波器是否启用，并通过通用 set_config_value() API 设置参数。

        Gemini 2 推荐滤波器（实测）：
          EdgeNoiseRemovalFilter  (default OFF) — 边缘噪声去除
          SpatialAdvancedFilter   (default OFF) — 空域滤波
          TemporalFilter          (default OFF) — 时域滤波
          HoleFillingFilter       (default OFF) — 填洞滤波
          DisparityTransform      (default ON)  — 视差→深度转换，含 min/max_depth, depth_uint
          ThresholdFilter         (default ON)  — 深度阈值裁剪，含 min, max
        """
        d = self._config.depth

        sensor = pipeline.get_device().get_sensor(ob.OBSensorType.DEPTH_SENSOR)
        try:
            rec_filters = sensor.get_recommended_filters()
        except Exception as e:
            logger.warning("[orbbec_camera] get_recommended_filters 失败: %s", e)
            self._depth_sdk_filters = []
            return

        # name → filter 映射
        rec_map: dict[str, object] = {f.get_name(): f for f in rec_filters}

        def _cfg(f, name: str, val) -> None:
            """通用参数设置（set_config_value），带异常保护。"""
            if val is None:
                return
            try:
                f.set_config_value(name, float(val))
            except Exception as e:
                logger.warning("[orbbec_camera]   %s.%s=%s 失败: %s", f.get_name(), name, val, e)

        # ── DisparityTransform（始终保持启用；当前 SDK 版本只读，参数通过构造时传入）──
        dt = rec_map.get("DisparityTransform")
        if dt is not None:
            dt.enable(True)
            # set_config_value 在当前 pyorbbecsdk 版本对 DisparityTransform 不可写
            # min/max/depth_unit 通过 _extract_depth 软件裁剪兜底
            logger.info(
                "[orbbec_camera] DisparityTransform ON (params 只读，软件裁剪兜底: min=%s max=%s)",
                d.min_mm,
                d.max_mm,
            )
        else:
            logger.warning("[orbbec_camera] 警告: DisparityTransform 未在推荐列表中")

        # ── ThresholdFilter（始终保持启用；用 set_value_range() 设置深度范围）──
        tf_thresh = rec_map.get("ThresholdFilter")
        if tf_thresh is not None:
            tf_thresh.enable(True)
            try:
                if hasattr(tf_thresh, "set_value_range"):
                    tf_thresh.set_value_range(d.min_mm, d.max_mm)
                    self._depth_hw_range_ok = True
                    logger.info("[orbbec_camera] ThresholdFilter ON: set_value_range(%s, %s)", d.min_mm, d.max_mm)
                else:
                    logger.info("[orbbec_camera] ThresholdFilter ON (set_value_range 不可用，软件裁剪兜底)")
            except Exception as e:
                logger.warning("[orbbec_camera] ThresholdFilter set_value_range 失败: %s", e)

        # ── 边缘滤波 EdgeNoiseRemovalFilter ─────────────────────────────────
        ef = rec_map.get("EdgeNoiseRemovalFilter")
        if ef is not None:
            ef.enable(d.edge_filter)
            if d.edge_filter:
                _cfg(ef, "margin_x_th",             d.edge_margin_x_th)
                _cfg(ef, "margin_y_th",             d.edge_margin_y_th)
                _cfg(ef, "limit_x_th",              d.edge_limit_x_th)
                _cfg(ef, "limit_y_th",              d.edge_limit_y_th)
                _cfg(ef, "enable_vertical_direction", int(d.edge_vertical_direction) if d.edge_vertical_direction is not None else None)
                logger.info("[orbbec_camera] EdgeNoiseRemovalFilter ON")

        # ── 空域滤波 SpatialAdvancedFilter ──────────────────────────────────
        sf = rec_map.get("SpatialAdvancedFilter")
        if sf is not None:
            sf.enable(d.spatial_filter)
            if d.spatial_filter:
                _cfg(sf, "magnitude",  d.spatial_magnitude)
                _cfg(sf, "alpha",      d.spatial_alpha)
                _cfg(sf, "disp_diff",  d.spatial_disp_diff)
                _cfg(sf, "radius",     d.spatial_radius)
                logger.info("[orbbec_camera] SpatialAdvancedFilter ON")

        # ── 时域滤波 TemporalFilter ──────────────────────────────────────────
        tf = rec_map.get("TemporalFilter")
        if tf is not None:
            tf.enable(d.temporal_filter)
            if d.temporal_filter:
                _cfg(tf, "diff_scale", d.temporal_diff_scale)
                _cfg(tf, "weight",     d.temporal_weight)
                logger.info("[orbbec_camera] TemporalFilter ON")

        # ── 填洞滤波 HoleFillingFilter ───────────────────────────────────────
        hf = rec_map.get("HoleFillingFilter")
        if hf is not None:
            hf.enable(d.hole_fill_filter)
            if d.hole_fill_filter:
                mode_map = {"TOP": 0, "NEAREST": 1, "FURTHEST": 2}
                _cfg(hf, "hole_filling_mode", mode_map.get(d.hole_fill_mode, 0))
                logger.info("[orbbec_camera] HoleFillingFilter ON (mode=%s)", d.hole_fill_mode)

        # 按推荐顺序保留所有 filter（包括始终启用的 DisparityTransform/ThresholdFilter）
        self._depth_sdk_filters = list(rec_filters)
        enabled = [f.get_name() for f in rec_filters if f.is_enabled()]
        logger.info("[orbbec_camera] SDK 滤波链就绪: %s", enabled)

    def _apply_laser_properties(self, device: ob.Device) -> None:
        """应用激光 device property（Gemini 2 实际支持项）。"""
        laser = self._config.laser
        P = ob.OBPropertyID
        self._set_prop_bool(device, P.OB_PROP_LDP_BOOL, laser.ldp_enabled, "laser.ldp_enabled")
        self._set_prop_bool(device, P.OB_PROP_LASER_BOOL, laser.enabled, "laser.enabled")
        self._set_prop_int(device, P.OB_PROP_LASER_POWER_LEVEL_CONTROL_INT, laser.power_level, "laser.power_level")  # [0,5]

    def _convert_frameset(self, frameset: ob.FrameSet) -> OrbbeFrame:
        """将 pyorbbecsdk FrameSet 转换为 SDK-free 图像与可选点云。"""
        color_frame = frameset.get_color_frame()
        timestamp_ms = int(color_frame.get_timestamp()) if color_frame is not None else 0

        color, color_jpeg = self._extract_color(frameset)
        processed_depth = (
            self._process_depth_frame(frameset) if self._config.depth.enabled else None
        )
        depth = self._depth_frame_to_metres(processed_depth)
        point_cloud: OrbbecPointCloud | None = None
        if self._config.point_cloud.enabled and self._point_cloud is not None:
            try:
                point_cloud = self._extract_point_cloud(
                    frameset,
                    processed_depth,
                    depth,
                )
            except Exception as e:
                if not getattr(self, "_point_cloud_failure_logged", False):
                    logger.warning(
                        "[orbbec_camera] 点云转换失败，继续输出图像: %s",
                        e,
                    )
                self._point_cloud_failure_logged = True
            else:
                if getattr(self, "_point_cloud_failure_logged", False):
                    logger.info("[orbbec_camera] 点云转换已恢复")
                self._point_cloud_failure_logged = False
        ir = self._extract_ir(frameset) if self._config.ir.enabled else None

        return OrbbeFrame(
            color=color,
            depth=depth,
            ir=ir,
            timestamp_ms=timestamp_ms,
            color_jpeg=color_jpeg,
            point_cloud=point_cloud,
        )

    def _extract_color(self, frameset: ob.FrameSet) -> tuple[np.ndarray | None, bytes | None]:
        """提取 Color：返回 (RGB uint8, JPEG bytes)。二者至多其一非空。"""
        frame = frameset.get_color_frame()
        if frame is None:
            return None, None
        w, h = frame.get_width(), frame.get_height()
        data = np.asanyarray(frame.get_data())
        fmt = frame.get_format()
        c = self._config.color
        need_geom = bool(c.flip or c.mirror or c.rotate)

        if fmt == ob.OBFormat.MJPG:
            jpeg_bytes = data.tobytes() if isinstance(data, np.ndarray) else bytes(data)
            # jpeg 输出且无需软件几何补偿时，直接透传设备 MJPG，避免 decode/re-encode。
            if c.format == "jpeg" and not need_geom:
                return None, jpeg_bytes

            bgr = cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
            if bgr is None:
                return None, None
            arr = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            if c.flip:
                arr = cv2.flip(arr, 0)
            if c.mirror:
                arr = cv2.flip(arr, 1)
            if c.rotate == 90:
                arr = cv2.rotate(arr, cv2.ROTATE_90_CLOCKWISE)
            elif c.rotate == 180:
                arr = cv2.rotate(arr, cv2.ROTATE_180)
            elif c.rotate == 270:
                arr = cv2.rotate(arr, cv2.ROTATE_90_COUNTERCLOCKWISE)
            return np.ascontiguousarray(arr), None
        if fmt == ob.OBFormat.RGB:
            rgb = _reshape_raw_frame(
                data,
                dtype=np.uint8,
                shape=(h, w, 3),
                stream="Color",
                format_name="RGB",
            )
            return (None, None) if rgb is None else (np.ascontiguousarray(rgb), None)
        if fmt == ob.OBFormat.BGR:
            bgr = _reshape_raw_frame(
                data,
                dtype=np.uint8,
                shape=(h, w, 3),
                stream="Color",
                format_name="BGR",
            )
            if bgr is None:
                return None, None
            return np.ascontiguousarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)), None
        if fmt == ob.OBFormat.YUYV:
            yuyv = _reshape_raw_frame(
                data,
                dtype=np.uint8,
                shape=(h, w, 2),
                stream="Color",
                format_name="YUYV",
            )
            if yuyv is None:
                return None, None
            try:
                bgr = cv2.cvtColor(yuyv, cv2.COLOR_YUV2BGR_YUYV)
                return np.ascontiguousarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)), None
            except cv2.error as e:
                logger.warning("[orbbec_camera] Color YUYV 帧转换失败: %s", e)
                return None, None
        logger.warning("[orbbec_camera] 不支持的 Color 帧格式: %s", fmt)
        return None, None

    def _process_depth_frame(self, frameset: ob.FrameSet) -> object | None:
        """Apply the SDK filter chain once and return the shared Depth frame."""
        source = frameset.get_depth_frame()
        if source is None:
            return None
        if not self._depth_sdk_filters:
            return source

        try:
            processed: object = source
            for sdk_filter in self._depth_sdk_filters:
                result = sdk_filter.process(processed)
                if result is not None:
                    processed = result
            depth_frame = processed.as_depth_frame()  # type: ignore[attr-defined]
            if depth_frame is not None:
                return depth_frame
            logger.warning(
                "[orbbec_camera] SDK 滤波链未返回 DepthFrame，使用原始 Depth"
            )
        except Exception as e:
            logger.warning("[orbbec_camera] SDK 滤波链应用失败（跳过）: %s", e)
        return source

    def _depth_frame_to_metres(self, frame: object | None) -> np.ndarray | None:
        """Convert one processed SDK Depth frame to a float32 image in metres."""
        if frame is None:
            return None
        width = int(frame.get_width())  # type: ignore[attr-defined]
        height = int(frame.get_height())  # type: ignore[attr-defined]
        raw = _reshape_raw_frame(
            frame.get_data(),  # type: ignore[attr-defined]
            dtype=np.uint16,
            shape=(height, width),
            stream="Depth",
            format_name="Y16",
        )
        if raw is None:
            return None

        scale = float(frame.get_depth_scale())  # type: ignore[attr-defined]
        if not np.isfinite(scale) or scale <= 0:
            logger.warning(
                "[orbbec_camera] 非法 depth scale=%s，按 1.0 mm/pixel 处理",
                scale,
            )
            scale = 1.0
        # SDK scale 为毫米/单位；一次乘到米，避免后续再扫一遍。
        depth_m = raw.astype(np.float32, copy=False) * np.float32(scale * 0.001)

        if not self._depth_hw_range_ok:
            lo_m = self._config.depth.min_mm * 0.001
            hi_m = self._config.depth.max_mm * 0.001
            if lo_m > 0.0 or hi_m < 10.0:
                invalid = (depth_m > 0.0) & (
                    (depth_m < lo_m) | (depth_m > hi_m)
                )
                if np.any(invalid):
                    depth_m[invalid] = 0.0

        return np.ascontiguousarray(depth_m)

    def _extract_depth(self, frameset: ob.FrameSet) -> np.ndarray | None:
        """Compatibility wrapper returning processed Depth in metres."""
        return self._depth_frame_to_metres(self._process_depth_frame(frameset))

    def _extract_point_cloud(
        self,
        frameset: ob.FrameSet,
        depth_frame: object | None,
        depth_m: np.ndarray | None,
    ) -> OrbbecPointCloud | None:
        """Generate a point cloud from the same processed Depth frame we publish."""
        if depth_frame is None or depth_m is None:
            return None
        processor = self._point_cloud
        if processor is None:
            raise RuntimeError("Orbbec point-cloud processor is unavailable")

        # FrameSet replaces an existing frame of the same type. This keeps XYZ/Z
        # derived from the SDK-filtered Depth frame rather than the original frame.
        frameset.push_frame(depth_frame)
        result = processor.process(frameset)  # type: ignore[attr-defined]
        if result is None:
            return None
        points_frame = result.as_points_frame()
        if points_frame is None:
            raise ValueError("Orbbec PointCloudFilter did not return a PointsFrame")

        return _point_cloud_from_sdk_buffer(
            points_frame.get_data(),
            depth_m,
            width=int(points_frame.get_width()),
            height=int(points_frame.get_height()),
            position_value_scale=float(points_frame.get_position_value_scale()),
            colorized=self._config.point_cloud.colorize,
        )

    def _extract_ir(self, frameset: ob.FrameSet) -> np.ndarray | None:
        """提取 IR 帧，返回 HW uint8（ir8/mjpg）或 uint16（ir16）numpy 数组。

        格式处理：
          Y8   → 直接 reshape 为 (H,W) uint8
          MJPG → cv2.imdecode 解码为 (H,W) uint8 灰度
          Y16  → frombuffer uint16 reshape（Gemini 2 IR 不支持，保留兜底）
        """
        frame = frameset.get_ir_frame()
        if frame is None:
            return None
        frame = frame.as_video_frame()
        w, h = frame.get_width(), frame.get_height()
        data = np.asanyarray(frame.get_data())
        ir_fmt = frame.get_format()

        if ir_fmt == ob.OBFormat.Y8:
            arr = _reshape_raw_frame(
                data,
                dtype=np.uint8,
                shape=(h, w),
                stream="IR",
                format_name="Y8",
            )
            return None if arr is None else np.ascontiguousarray(arr)
        if ir_fmt == ob.OBFormat.MJPG:
            try:
                gray = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
            except cv2.error as e:
                logger.warning("[orbbec_camera] IR MJPG 帧解码失败: %s", e)
                return None
            if gray is None:
                logger.warning("[orbbec_camera] IR MJPG 帧解码失败")
                return None
            return np.ascontiguousarray(gray)
        if ir_fmt == ob.OBFormat.Y16:
            arr = _reshape_raw_frame(
                data,
                dtype=np.uint16,
                shape=(h, w),
                stream="IR",
                format_name="Y16",
            )
            return None if arr is None else np.ascontiguousarray(arr)
        logger.warning("[orbbec_camera] 不支持的 IR 帧格式: %s", ir_fmt)
        return None

    @staticmethod
    def _is_disconnect_error(e: Exception) -> bool:
        msg = str(e).lower()
        return any(
            kw in msg for kw in ("no such device", "disconnected", "device not found", "i/o error")
        )


def list_orbbec_devices() -> list[DeviceInfo]:
    """列出当前系统中所有已连接的 Orbbec 设备。"""
    ctx = ob.Context()
    device_list = ctx.query_devices()
    count = device_list.get_count()
    result: list[DeviceInfo] = []
    for i in range(count):
        try:
            result.append(
                DeviceInfo(
                    index=i,
                    serial=device_list.get_device_serial_number_by_index(i) or "",
                    name=device_list.get_device_name_by_index(i) or "",
                    firmware_version="",
                    usb_type=device_list.get_device_connection_type_by_index(i) or "",
                )
            )
        except Exception:
            continue
    return result


def probe_orbbec_device(
    device_index: int = 0,
    device_serial: str | None = None,
) -> dict[str, object]:
    """读取设备基本信息，供诊断工具使用。

    该函数保留所有厂商 SDK 访问在本模块边界内。
    """
    devices = list_orbbec_devices()
    if device_serial:
        matches = [device for device in devices if device.serial == device_serial]
    else:
        matches = [device for device in devices if device.index == device_index]
    if not matches:
        selector = f"serial={device_serial}" if device_serial else f"index={device_index}"
        raise RuntimeError(f"未找到 Orbbec 设备（{selector}）")
    device = matches[0]
    return {
        "index": device.index,
        "serial": device.serial,
        "name": device.name,
        "firmware_version": device.firmware_version,
        "usb_type": device.usb_type,
    }
