"""Orbbec 传感器节点配置加载与校验。

参数范围来源：设备查询（Orbbec Gemini 2，固件 1.4.60+）
仅保留 Gemini 2 实际支持的 property，不支持的项目已移除。
"""

from __future__ import annotations

import os
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml


@dataclass
class ColorStreamConfig:
    """Color 流配置（对应 UI「Color」数据流 + 控制两块）。"""

    # ── 数据流（需重启）──────────────────────────────────────────────────────
    width: int = 640                     # 实测可用: 640 / 1280 / 1920
    height: int = 480                     # 与 width 配对: 360/480 / 720 / 1080
    fps: int = 30                         # 实测可用: 5 / 10 / 15 / 30
    # 传输格式（实测三种均可采集，output shape 相同）
    # rgb8  → RGB888，uint8 HWC，无压缩
    # jpeg  → MJPG，cv2.imdecode 解码为 RGB，压缩传输
    # yuyv  → YUYV，cv2.COLOR_YUV2BGR_YUYV 转换，标准 UVC 格式
    format: Literal["rgb8", "jpeg", "yuyv"] = "rgb8"
    # JPEG 重编码质量（仅 format=jpeg 时在节点内对 MJPG 解码后重编码输出）
    jpeg_quality: int = 85                # [1, 100]

    # ── 控制（实时生效）──────────────────────────────────────────────────────
    # 图像方向
    mirror: bool = False
    flip: bool = False
    rotate: int = 0                       # 0 / 90 / 180 / 270

    # 白平衡（对应 Viewer Auto White Balance）
    auto_white_balance: bool = True
    white_balance: int | None = None      # [2800, 6800] step=10（K），需 auto_white_balance: false

    # 曝光（对应 Viewer Auto Exposure / Adjust Exposure / Adjust Gain）
    auto_exposure: bool = True
    exposure_us: int | None = None        # [0, 33000] μs step=1，需 auto_exposure: false
    gain: int | None = None               # [1, 255] step=2，需 auto_exposure: false

    # 图像质量（使用设备实测默认值；设为 None 时节点跳过写入，保留设备当前值）
    brightness: int | None = 52          # [1, 255]，设备默认 52
    contrast: int | None = 40            # [0, 255]，设备默认 40
    saturation: int | None = 32          # [0, 255]，设备默认 32
    sharpness: int | None = 99           # [0, 255]，设备默认 99
    power_line_frequency: int | None = 0  # 0=关（默认）/ 1=50Hz / 2=60Hz


def _flatten_stream_section(c: dict[str, Any], nested_keys: tuple[str, ...]) -> dict[str, Any]:
    """合并任意流的多块嵌套配置（与扁平键兼容）。

    先合并所有嵌套块，再以同级扁平键覆盖（便于局部覆写）。
    """
    if not any(k in c for k in nested_keys):
        return dict(c)
    merged: dict[str, Any] = {}
    for nk in nested_keys:
        block = c.get(nk)
        if isinstance(block, dict):
            merged.update(block)
    for k, v in c.items():
        if k not in nested_keys:
            merged[k] = v
    return merged


def _flatten_depth_section(c: dict[str, Any]) -> dict[str, Any]:
    """合并 Depth 四块（data_flow / control / advanced / rendering）。"""
    return _flatten_stream_section(c, ("data_flow", "control", "advanced", "rendering"))


@dataclass
class DepthStreamConfig:
    """Depth 流配置。"""

    # 流参数（需重启）
    enabled: bool = True
    width: int = 640
    height: int = 400
    fps: int = 30
    # 深度传输格式（实测三种 SDK 均输出 Y16；差异在 USB 传输编码/带宽）
    # y16=标准 16-bit，y14=14-bit 线上打包（SDK 输出 Y16），rle=行程编码压缩
    format: Literal["y16", "y14", "rle"] = "y16"
    # 深度算法工作模式（设备），须与 get_depth_work_mode_list() 中 name 一致；None=不切换
    depth_work_mode: str | None = None

    # 软件深度裁剪（实时，节点内 numpy 处理）
    # 注意：Gemini 2 不支持 OB_PROP_MIN/MAX_DEPTH_INT，只能软件裁剪
    min_mm: int = 0
    max_mm: int = 10000

    # 曝光（实时，Gemini 2 实测范围）
    auto_exposure: bool = True
    exposure: int | None = None           # [200, 10000] step=1
    gain: int | None = None               # [1000, 15000] step=100

    # 噪声去除滤波（实时）
    # OB_PROP_DEPTH_NOISE_REMOVAL_FILTER_BOOL，默认已开启（cur=True）
    noise_removal_filter: bool = True
    noise_removal_max_diff: int | None = None       # [1, 10000] step=1，默认 256
    noise_removal_max_speckle: int | None = None    # [1, 1000] step=1，默认 200

    # 空洞填充（实时）
    hole_filter: bool = False

    # 深度精度等级（实时，Gemini 2 专属 OBDepthPrecisionLevel 枚举）
    # 0=1mm, 1=0.8mm, 2=0.4mm, 3=0.1mm, 4=0.2mm
    precision_level: int | None = None

    # 视差转深度（实时）
    disparity_to_depth: bool = True       # OB_PROP_DISPARITY_TO_DEPTH_BOOL，默认开启

    # 深度单位（实时，Gemini 2 支持，影响 DepthFrame 像素值的毫米解析）
    # OB_PROP_DEPTH_UNIT_FLEXIBLE_ADJUSTMENT_FLOAT；None=不写，保持设备默认（1.0）
    depth_unit: float | None = None

    # 高级滤波（设备支持情况因型号而异；None=不写 property，保留设备默认）
    post_filter: bool | None = None
    soft_filter: bool | None = None
    soft_filter_max_diff: int | None = None
    soft_filter_max_speckle: int | None = None
    rm_filter: bool | None = None

    # ── 后处理 SDK 管道滤波（客户端每帧应用；对应 UI「后处理」四个子开关）──────────────
    # 边缘滤波（SDK EdgeNoiseRemovalFilter；实测参数来源 get_config_schema_vec()）
    edge_filter: bool = False
    edge_margin_x_th: int | None = None          # [0, 640]，默认 6
    edge_margin_y_th: int | None = None          # [0, 400]，默认 6
    edge_limit_x_th: int | None = None           # [1, 640]，默认 70
    edge_limit_y_th: int | None = None           # [1, 400]，默认 30
    edge_vertical_direction: bool | None = None  # 是否启用垂直方向，默认 False

    # 空域滤波（SDK SpatialAdvancedFilter）
    spatial_filter: bool = False
    spatial_alpha: float | None = None           # [0.1, 1.0]，默认 0.5
    spatial_disp_diff: int | None = None         # [1, 10000]，默认 160
    spatial_magnitude: int | None = None         # [1, 5]，默认 1
    spatial_radius: int | None = None            # [0, 8]，默认 1

    # 时域滤波（SDK TemporalFilter）
    temporal_filter: bool = False
    temporal_diff_scale: float | None = None     # [0.1, 1.0]，默认 0.1
    temporal_weight: float | None = None         # [0.1, 1.0]，默认 0.4

    # 填洞滤波（SDK HoleFillingFilter）
    hole_fill_filter: bool = False
    hole_fill_mode: str = "TOP"                  # TOP / NEAREST / FURTHEST

    # 图像方向（实时）
    mirror: bool = False
    flip: bool = False
    rotate: int = 0                        # 0 / 90 / 180 / 270


@dataclass
class IrStreamConfig:
    """IR 流配置（对应 UI「IR」数据流 + 控制两块）。"""

    # ── 数据流（需重启）──────────────────────────────────────────────────────
    enabled: bool = True
    width: int = 640                      # 实测可用: 320 / 640 / 1280
    height: int = 400                     # 与 width 配对: 200 / 400 / 800
    fps: int = 30                         # 实测可用: 5 / 10 / 15 / 30
    # 传输格式（实测 Gemini 2 IR 可用: Y8 / MJPG；不支持 Y16）
    # ir8  → Y8（uint8 灰度，直接 reshape）
    # mjpg → MJPG 压缩（cv2.imdecode 解码为 uint8 灰度）
    format: Literal["ir8", "mjpg"] = "ir8"

    # ── 控制（实时生效）──────────────────────────────────────────────────────
    # 图像方向
    mirror: bool = False
    flip: bool = False
    rotate: int = 0                       # 0 / 90 / 180 / 270

    # 曝光（实时，Gemini 2 实测范围）
    auto_exposure: bool = True
    exposure_us: int | None = None        # [200, 10000] step=1，需 auto_exposure: false
    gain: int | None = None               # [1000, 15000] step=100，需 auto_exposure: false

    # IR 数据通道（实时）
    channel_data_source: int | None = None  # 0=Left IR（默认）/ 1=Right IR


@dataclass
class LaserConfig:
    """激光投射器配置（Gemini 2 实际支持项）。"""

    # OB_PROP_LASER_BOOL：总开关（pipeline 启动后由 SDK 自动管理，通常无需手动设置）
    enabled: bool = True
    # OB_PROP_LASER_POWER_LEVEL_CONTROL_INT：[0, 5] step=1，5=最强（默认）
    power_level: int | None = None
    # OB_PROP_LDP_BOOL：LDP 安全保护，建议保持开启
    ldp_enabled: bool = True


@dataclass
class OrbbecConfig:
    """Orbbec 传感器节点完整配置（针对 Gemini 2）。"""

    # 设备识别（启动时生效）
    device_serial: str | None = None
    device_index: int = 0

    # 各流配置
    color: ColorStreamConfig = field(default_factory=ColorStreamConfig)
    depth: DepthStreamConfig = field(default_factory=DepthStreamConfig)
    ir: IrStreamConfig = field(default_factory=IrStreamConfig)
    laser: LaserConfig = field(default_factory=LaserConfig)

    # 全局（重启生效）
    align_mode: Literal["disable", "sw", "hw"] = "disable"
    frame_sync: bool = False

    # 全局（启动时生效）
    prewarm_frames: int = 0
    connect_delay_ms: int = 0
    init_timeout_sec: float = 15.0
    capture_process: Literal["isolated", "direct"] = "isolated"

    # dora 输出 ID（启动时生效）
    output_color: str = "image/color"
    output_depth: str = "image/depth"
    output_ir: str = "image/ir"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OrbbecConfig":
        def _opt_int(v: Any) -> int | None:
            return int(v) if v is not None else None

        def _positive_int(name: str, value: Any) -> int:
            parsed = int(value)
            if parsed <= 0:
                raise ValueError(f"{name} 须大于 0")
            return parsed

        def _color(d: dict[str, Any]) -> ColorStreamConfig:
            raw = d.get("color", {}) or {}
            df = raw.get("data_flow", {}) or {}
            ctrl = raw.get("control", {}) or {}

            fmt = df.get("format", "rgb8")
            if fmt not in ("rgb8", "jpeg", "yuyv"):
                raise ValueError(f"color.format 须为 rgb8/jpeg/yuyv，当前: {fmt}")
            jq = int(df.get("jpeg_quality", 85))
            if not (1 <= jq <= 100):
                raise ValueError("color.jpeg_quality 须在 [1, 100]")
            rotate = int(ctrl.get("rotate", 0))
            if rotate not in (0, 90, 180, 270):
                raise ValueError("color.rotate 须为 0/90/180/270")
            plf = _opt_int(ctrl.get("power_line_frequency"))
            if plf is not None and plf not in (0, 1, 2):
                raise ValueError("color.power_line_frequency 须为 0/1/2")
            return ColorStreamConfig(
                width=_positive_int("color.width", df.get("width", 640)),
                height=_positive_int("color.height", df.get("height", 480)),
                fps=_positive_int("color.fps", df.get("fps", 30)),
                format=fmt,
                jpeg_quality=jq,
                mirror=bool(ctrl.get("mirror", False)),
                flip=bool(ctrl.get("flip", False)),
                rotate=rotate,
                auto_white_balance=bool(ctrl.get("auto_white_balance", True)),
                white_balance=_opt_int(ctrl.get("white_balance")),
                auto_exposure=bool(ctrl.get("auto_exposure", True)),
                exposure_us=_opt_int(ctrl.get("exposure_us")),
                gain=_opt_int(ctrl.get("gain")),
                brightness=_opt_int(ctrl.get("brightness", 52)),
                contrast=_opt_int(ctrl.get("contrast", 40)),
                saturation=_opt_int(ctrl.get("saturation", 32)),
                sharpness=_opt_int(ctrl.get("sharpness", 99)),
                power_line_frequency=plf if plf is not None else 0,
            )

        def _depth(d: dict[str, Any]) -> DepthStreamConfig:
            raw = d.get("depth", {}) or {}
            c = _flatten_depth_section(raw)
            min_mm = int(c.get("min_mm", 0))
            max_mm = int(c.get("max_mm", 10000))
            if min_mm < 0:
                raise ValueError("depth.min_mm 须 >= 0")
            if max_mm <= min_mm:
                raise ValueError("depth.max_mm 须 > min_mm")
            rotate = int(c.get("rotate", 0))
            if rotate not in (0, 90, 180, 270):
                raise ValueError("depth.rotate 须为 0/90/180/270")
            pl = _opt_int(c.get("precision_level"))
            if pl is not None and pl not in range(5):
                raise ValueError("depth.precision_level 须为 0~4")
            dfmt = c.get("format", "y16")
            if dfmt not in ("y16", "y14", "rle"):
                raise ValueError(f"depth.format 须为 y16/y14/rle，当前: {dfmt}")

            dwm = c.get("depth_work_mode")
            if dwm is not None:
                s = str(dwm).strip()
                dwm = s if s else None
            def _opt_bool(key: str) -> bool | None:
                if key not in c:
                    return None
                v = c[key]
                if v is None:
                    return None
                return bool(v)

            def _opt_float(v: Any) -> float | None:
                return float(v) if v is not None else None

            depth_unit = _opt_float(c.get("depth_unit"))
            if depth_unit is not None and depth_unit <= 0:
                raise ValueError("depth.depth_unit 须大于 0")

            hfm = c.get("hole_fill_mode", "TOP")
            valid_hfm = ("TOP", "NEAREST", "FURTHEST")
            if hfm not in valid_hfm:
                raise ValueError(f"hole_fill_mode 须为 {valid_hfm}，当前: {hfm}")

            return DepthStreamConfig(
                enabled=bool(c.get("enabled", True)),
                width=_positive_int("depth.width", c.get("width", 640)),
                height=_positive_int("depth.height", c.get("height", 400)),
                fps=_positive_int("depth.fps", c.get("fps", 30)),
                format=dfmt,
                depth_work_mode=dwm,
                depth_unit=depth_unit,
                min_mm=min_mm,
                max_mm=max_mm,
                auto_exposure=bool(c.get("auto_exposure", True)),
                exposure=_opt_int(c.get("exposure")),
                gain=_opt_int(c.get("gain")),
                noise_removal_filter=bool(c.get("noise_removal_filter", True)),
                noise_removal_max_diff=_opt_int(c.get("noise_removal_max_diff")),
                noise_removal_max_speckle=_opt_int(c.get("noise_removal_max_speckle")),
                hole_filter=bool(c.get("hole_filter", False)),
                precision_level=pl,
                disparity_to_depth=bool(c.get("disparity_to_depth", True)),
                post_filter=_opt_bool("post_filter"),
                soft_filter=_opt_bool("soft_filter"),
                soft_filter_max_diff=_opt_int(c.get("soft_filter_max_diff")),
                soft_filter_max_speckle=_opt_int(c.get("soft_filter_max_speckle")),
                rm_filter=_opt_bool("rm_filter"),
                edge_filter=bool(c.get("edge_filter", False)),
                edge_margin_x_th=_opt_int(c.get("edge_margin_x_th")),
                edge_margin_y_th=_opt_int(c.get("edge_margin_y_th")),
                edge_limit_x_th=_opt_int(c.get("edge_limit_x_th")),
                edge_limit_y_th=_opt_int(c.get("edge_limit_y_th")),
                edge_vertical_direction=_opt_bool("edge_vertical_direction"),
                spatial_filter=bool(c.get("spatial_filter", False)),
                spatial_alpha=_opt_float(c.get("spatial_alpha")),
                spatial_disp_diff=_opt_int(c.get("spatial_disp_diff")),
                spatial_magnitude=_opt_int(c.get("spatial_magnitude")),
                spatial_radius=_opt_int(c.get("spatial_radius")),
                temporal_filter=bool(c.get("temporal_filter", False)),
                temporal_diff_scale=_opt_float(c.get("temporal_diff_scale")),
                temporal_weight=_opt_float(c.get("temporal_weight")),
                hole_fill_filter=bool(c.get("hole_fill_filter", False)),
                hole_fill_mode=hfm,
                mirror=bool(c.get("mirror", False)),
                flip=bool(c.get("flip", False)),
                rotate=rotate,
            )

        def _ir(d: dict[str, Any]) -> IrStreamConfig:
            raw = d.get("ir", {}) or {}
            # 仅接受嵌套块写法；data_flow 缺失时用空 dict 触发全部默认值
            df = raw.get("data_flow", {}) or {}
            ctrl = raw.get("control", {}) or {}

            fmt = df.get("format", "ir8")
            if fmt not in ("ir8", "mjpg"):
                raise ValueError(f"ir.format 须为 ir8 或 mjpg，当前: {fmt}")
            rotate = int(ctrl.get("rotate", 0))
            if rotate not in (0, 90, 180, 270):
                raise ValueError("ir.rotate 须为 0/90/180/270")
            cds = _opt_int(ctrl.get("channel_data_source"))
            if cds is not None and cds not in (0, 1):
                raise ValueError("ir.channel_data_source 须为 0 或 1")
            return IrStreamConfig(
                enabled=bool(df.get("enabled", True)),
                width=_positive_int("ir.width", df.get("width", 640)),
                height=_positive_int("ir.height", df.get("height", 400)),
                fps=_positive_int("ir.fps", df.get("fps", 30)),
                format=fmt,
                mirror=bool(ctrl.get("mirror", False)),
                flip=bool(ctrl.get("flip", False)),
                rotate=rotate,
                auto_exposure=bool(ctrl.get("auto_exposure", True)),
                exposure_us=_opt_int(ctrl.get("exposure_us")),
                gain=_opt_int(ctrl.get("gain")),
                channel_data_source=cds,
            )

        def _laser(d: dict[str, Any]) -> LaserConfig:
            c = d.get("laser", {}) or {}
            pl = _opt_int(c.get("power_level"))
            if pl is not None and not (0 <= pl <= 5):
                raise ValueError("laser.power_level 须在 [0, 5]")
            return LaserConfig(
                enabled=bool(c.get("enabled", True)),
                power_level=pl,
                ldp_enabled=bool(c.get("ldp_enabled", True)),
            )

        align_mode = data.get("align_mode", "disable")
        if align_mode not in ("disable", "sw", "hw"):
            raise ValueError(f"align_mode 须为 disable/sw/hw，当前: {align_mode}")

        prewarm = int(data.get("prewarm_frames", 0))
        if not (0 <= prewarm <= 60):
            raise ValueError("prewarm_frames 须在 [0, 60]")

        connect_delay = int(data.get("connect_delay_ms", 0))
        if connect_delay < 0:
            raise ValueError("connect_delay_ms 不能为负数")
        init_timeout = float(data.get("init_timeout_sec", 15.0))
        if not math.isfinite(init_timeout) or init_timeout <= 0:
            raise ValueError("init_timeout_sec 须为有限且大于 0 的数")
        capture_process = data.get("capture_process", "isolated")
        if capture_process not in ("isolated", "direct"):
            raise ValueError(
                f"capture_process 须为 isolated/direct，当前: {capture_process}"
            )

        device_index = int(data.get("device_index", 0))
        if device_index < 0:
            raise ValueError("device_index 不能为负数")

        return cls(
            device_serial=data.get("device_serial") or None,
            device_index=device_index,
            color=_color(data),
            depth=_depth(data),
            ir=_ir(data),
            laser=_laser(data),
            align_mode=align_mode,
            frame_sync=bool(data.get("frame_sync", False)),
            prewarm_frames=prewarm,
            connect_delay_ms=connect_delay,
            init_timeout_sec=init_timeout,
            capture_process=capture_process,
            output_color=str(data.get("output_color", "image/color")),
            output_depth=str(data.get("output_depth", "image/depth")),
            output_ir=str(data.get("output_ir", "image/ir")),
        )

    @classmethod
    def from_yaml_path(cls, path: str | Path) -> "OrbbecConfig":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"配置文件不存在: {p}")
        with open(p, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not data:
            raise ValueError(f"配置文件为空: {p}")
        return cls.from_dict(data)

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> "OrbbecConfig":
        if config_path is not None:
            return cls.from_yaml_path(config_path)
        env_path = os.environ.get("ORBBEC_CAMERA_NODE_CONFIG", "")
        if env_path:
            return cls.from_yaml_path(env_path)
        raise ValueError(
            "未找到配置。请设置 ORBBEC_CAMERA_NODE_CONFIG 环境变量，或通过 --config 指定配置文件。"
        )

    @classmethod
    def for_snapshot(
        cls,
        *,
        device_index: int = 0,
        device_serial: str | None = None,
    ) -> "OrbbecConfig":
        """用于 snapshot 子命令：仅选择设备，其余与默认 YAML 等价（对齐 camera::for_snapshot）。"""
        data: dict[str, Any] = {
            "device_index": device_index,
            "align_mode": "disable",
        }
        if device_serial:
            data["device_serial"] = device_serial
        return cls.from_dict(data)
