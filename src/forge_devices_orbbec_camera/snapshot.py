"""截一帧保存为图像文件（与 Rust camera 的 snapshot 对齐；可选三路 Color/Depth/IR）。

不启动 dora，仅作硬件连通性 / 快速检查用。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np

from .backend import OrbbeFrame, create_backend
from .config import OrbbecConfig
from forge_common import get_logger

logger = get_logger(__name__)

# 与 crates/nodes/sensors/usb_camera/src/main.rs 一致：先丢帧减轻 AE 未稳 / 首帧异常
SNAPSHOT_DISCARD_FRAMES = 2
# 全黑 / 过暗帧时继续尝试
SNAPSHOT_MAX_DARK_SKIPS = 24
# RGB 小图均值低于此视为「无可见内容」
_MIN_CONTENT_MEAN = 10.0


def _rgb_has_visible_content(rgb: np.ndarray) -> bool:
    if rgb is None or rgb.size == 0 or rgb.ndim != 3:
        return False
    h, w = rgb.shape[:2]
    if h < 2 or w < 2:
        return float(np.mean(rgb)) > _MIN_CONTENT_MEAN
    small = cv2.resize(rgb, (48, 27), interpolation=cv2.INTER_AREA)
    return float(small.mean()) > _MIN_CONTENT_MEAN


def _write_color_jpeg(rgb: np.ndarray, out_path: Path, quality: int) -> None:
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    q = max(1, min(100, int(quality)))
    ok = cv2.imwrite(str(out_path), bgr, [int(cv2.IMWRITE_JPEG_QUALITY), q])
    if not ok:
        raise RuntimeError(f"cv2.imwrite 失败: {out_path}")


def _write_color_jpeg_bytes(jpeg: bytes, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(jpeg)


def _write_depth_png(depth_m: np.ndarray, out_path: Path) -> None:
    """将后端 float32 米深度转为 uint16 毫米 PNG。"""
    depth_mm = np.clip(depth_m * 1000.0, 0, np.iinfo(np.uint16).max).astype(np.uint16)
    ok, buf = cv2.imencode(".png", depth_mm)
    if not ok:
        raise RuntimeError("depth PNG 编码失败")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(buf.tobytes())


def _frame_has_visible_color(frame: OrbbeFrame) -> bool:
    if frame.color_jpeg:
        return True
    return frame.color is not None and _rgb_has_visible_content(frame.color)


def _write_frame_color(frame: OrbbeFrame, out_path: Path, quality: int) -> None:
    if frame.color_jpeg is not None:
        _write_color_jpeg_bytes(frame.color_jpeg, out_path)
        return
    if frame.color is None:
        raise RuntimeError("本帧无 Color 数据")
    _write_color_jpeg(frame.color, out_path, quality)


def _write_ir(ir: np.ndarray, out_path: Path, *, ir_format: str) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if ir_format == "ir8" or ir.dtype == np.uint8:
        ok = cv2.imwrite(str(out_path), ir)
        if not ok:
            raise RuntimeError(f"cv2.imwrite IR 失败: {out_path}")
        return
    ok, buf = cv2.imencode(".png", ir)
    if not ok:
        raise RuntimeError("IR PNG 编码失败")
    out_path.write_bytes(buf.tobytes())


def run_snapshot(
    config: OrbbecConfig,
    output: str | Path,
    *,
    jpeg_quality: int | None = None,
    all_streams: bool = False,
) -> int:
    """打开设备、采一帧并写出文件，成功返回 0。"""
    out_arg = Path(output).expanduser().resolve()
    # Snapshot only writes image files. Avoid generating and pickling a large
    # derived cloud, while leaving the caller's reusable config unchanged.
    cfg = replace(
        config,
        point_cloud=replace(config.point_cloud, enabled=False),
    )
    if jpeg_quality is not None:
        jq = max(1, min(100, int(jpeg_quality)))
        cfg = replace(cfg, color=replace(cfg.color, jpeg_quality=jq))

    backend = create_backend(cfg)
    try:
        for _ in range(SNAPSHOT_DISCARD_FRAMES):
            _ = backend.capture_frame()

        frame: OrbbeFrame | None = None
        for attempt in range(SNAPSHOT_MAX_DARK_SKIPS + 1):
            f = backend.capture_frame()
            if _frame_has_visible_color(f):
                frame = f
                break
            if attempt > 0 and attempt % 8 == 0:
                logger.warning("snapshot: 仍偏暗/空帧，继续抓取…")

        if frame is None or (frame.color is None and not frame.color_jpeg):
            logger.error("错误：连续 %s 次未得到有效 Color 帧。", SNAPSHOT_MAX_DARK_SKIPS + 1)
            return 1

        if not all_streams:
            _write_frame_color(frame, out_arg, cfg.color.jpeg_quality)
            logger.info("snapshot: 已写入 %s", out_arg)
            return 0

        # 三路：与节点 topic 一致 — Color JPEG、Depth uint16 PNG、IR PNG
        p = Path(output).expanduser().resolve()
        parent = p.parent
        stem = p.stem if p.suffix else p.name
        color_path = (parent / f"{stem}_color.jpg").resolve()
        depth_path = (parent / f"{stem}_depth.png").resolve()
        ir_path = (parent / f"{stem}_ir.png").resolve()

        _write_frame_color(frame, color_path, cfg.color.jpeg_quality)
        written = [str(color_path)]

        if cfg.depth.enabled:
            if frame.depth is not None:
                _write_depth_png(frame.depth, depth_path)
                written.append(str(depth_path))
            else:
                logger.warning("snapshot: 警告：本帧无 Depth，跳过 depth PNG（检查 YAML depth.enabled 与设备）")
        else:
            logger.warning("snapshot: depth 已在配置中关闭，跳过 depth PNG")

        if cfg.ir.enabled:
            if frame.ir is not None:
                _write_ir(frame.ir, ir_path, ir_format=cfg.ir.format)
                written.append(str(ir_path))
            else:
                logger.warning("snapshot: 警告：本帧无 IR，跳过 IR 文件")
        else:
            logger.warning("snapshot: IR 已在配置中关闭，跳过 IR 文件")

        logger.info("snapshot: 已写入\n  %s", "\n  ".join(written))
        return 0
    except Exception as e:
        logger.error("错误：%s", e)
        return 1
    finally:
        backend.close()


def resolve_snapshot_config(
    *,
    config_path: str | None,
    device_index: int | None,
    device_serial: str | None,
) -> OrbbecConfig:
    """--config 则加载；否则 for_snapshot。传入配置时可用 CLI 覆盖设备。"""
    if config_path:
        cfg = OrbbecConfig.from_yaml_path(config_path)
        if device_serial is not None:
            cfg.device_serial = device_serial
        if device_index is not None:
            cfg.device_index = int(device_index)
        return cfg

    idx = 0 if device_index is None else int(device_index)
    return OrbbecConfig.for_snapshot(device_index=idx, device_serial=device_serial)
