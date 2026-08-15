#!/usr/bin/env python3
"""Orbbec 深度相机 dora 节点。

支持 Gemini 2 系列，每个 tick 同时输出：
  - image/color  Color 帧（Image rgb8 或 CompressedImage jpeg）
  - image/depth  Depth 帧（Image 32FC1，单位 m）
  - image/ir     IR 帧（Image mono8 或 16UC1）

CLI 子命令（不启动 dora）：
  init-device    检查并初始化 udev 规则和 video 用户组，必要时请求 PolicyKit 权限
  list-devices   列出已连接的 Orbbec 设备（可加 --json 给前端读取）
  snapshot       截单帧保存图像（默认 Color JPEG；可选用 --all-streams 写 Color/Depth/IR）
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from dataclasses import dataclass


def _configure_runtime_env() -> None:
    """Disable OTEL exporters before importing Dora in node mode."""
    os.environ.setdefault("OTEL_SDK_DISABLED", "true")
    os.environ.setdefault("OTEL_TRACES_EXPORTER", "none")
    os.environ.setdefault("OTEL_METRICS_EXPORTER", "none")
    os.environ.setdefault("OTEL_LOGS_EXPORTER", "none")


_configure_runtime_env()

import pyarrow as pa
from forge_common import get_logger
from forge_msgs import CompressedImage, Image

from . import __version__
from .backend import CaptureBackend, OrbbeFrame, create_backend
from .config import OrbbecConfig
from .list_devices import run_list_devices
from .snapshot import resolve_snapshot_config, run_snapshot
from .system_setup import run_init_device, runtime_preflight

logger = get_logger(__name__)


class _HelpFormatter(
    argparse.ArgumentDefaultsHelpFormatter,
    argparse.RawDescriptionHelpFormatter,
):
    """显示参数默认值，并保留 epilog 中的换行。"""


_ROOT_DESCRIPTION = """\
Orbbec Gemini 2 深度相机 dora 节点：在 tick 驱动下输出 Color / Depth / IR（forge_msgs.Image / CompressedImage）。

【节点模式】不提供子命令时启动 dora，需配置文件。
【设备初始化】init-device 检查环境；可信路径中的 frozen 二进制可请求固定系统配置。
【工具子命令】list-devices / snapshot 仅操作本机相机并退出；licenses 显示内嵌许可证。"""

_ROOT_EPILOG = """\
运行模式说明:
  节点      在本仓库根目录:
            uv run orbbec-camera --config <YAML>
            或: ORBBEC_CAMERA_NODE_CONFIG=<YAML 绝对或相对路径> 同上
            从 YAML 读设备、分辨率、对齐方式等；向 dataflow 声明的 topic 发图。

  源码初始化 sudo bash scripts/install_permissions.sh
  部署初始化 orbbec-camera init-device

  枚举设备  uv run orbbec-camera list-devices
            uv run orbbec-camera list-devices --json

  查看许可  uv run orbbec-camera licenses

  截帧落盘  uv run orbbec-camera snapshot ...
            uv run orbbec-camera snapshot --config <YAML> ...
            默认只存 Color JPEG；--all-streams 另存 Depth/IR。

环境:
  仅 Linux；普通启动会检查运行环境，但不会自动提权。

常用示例（均在本仓库根目录执行）:
  sudo bash scripts/install_permissions.sh
  uv run orbbec-camera --config config/sensor.example.yaml
  uv run orbbec-camera list-devices
  uv run orbbec-camera snapshot -o snapshot.jpg
"""

_LIST_DEVICES_DESCRIPTION = """\
查询当前 USB 上已枚举的 Orbbec 设备（依赖 pyorbbecsdk）。

输出列为 Index、Serial、Name、Firmware、USB 等；无设备时给出排查提示。
加 --json 时输出 {"devices": [{"name": "...", "address": "..."}]}，适合前端或自动化工具读取。
与 snapshot 联合使用: 先用本命令确认 Index 或 Serial，再在 snapshot 里指定。"""

_SNAPSHOT_DESCRIPTION = """\
打开相机、预热丢帧后采集一帧并写入磁盘；不经过 dora，不启动节点循环。

默认仅保存 Color（RGB→JPEG），与 forge_runtime 中 Rust camera 节点的 snapshot 行为对齐。
加 --all-streams 时额外写出 Depth（uint16 mm，PNG）与 IR（PNG），文件名由 -o 的主干派生。

设备选择: 未指定 --config 时使用内置默认配置；--serial 优先于 --device-index（默认 0）。
使用 --config 可完整控制分辨率、depth/ir 开关等；CLI 仍可通过 --serial / --device-index 覆盖设备字段。"""

_SNAPSHOT_EPILOG = """\
输出文件约定:
  默认       仅 -o 指向的一个 JPEG（Color）。
  --all-streams  在 -o 所在目录生成:
              <主干>_color.jpg   Color
              <主干>_depth.png   Depth，16 位 PNG，单位 mm
              <主干>_ir.png      IR（配置关闭 depth/ir 流时跳过并提示）

示例（本仓库根目录，uv run）:
  uv run orbbec-camera snapshot -o snapshot.jpg
  uv run orbbec-camera snapshot --all-streams -o ./out/prefix.jpg
  uv run orbbec-camera snapshot --device-index 1 -o second.jpg
  uv run orbbec-camera snapshot --serial DEVICE_SERIAL --all-streams -o cap.jpg
  uv run orbbec-camera snapshot \\
      --config config/sensor.example.yaml --jpeg-quality 90 -o one.jpg
"""


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="orbbec-camera",
        description=_ROOT_DESCRIPTION,
        epilog=_ROOT_EPILOG,
        formatter_class=_HelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        metavar="PATH",
        help="YAML 配置文件路径。节点模式和 snapshot 子命令共用；也可通过环境变量 ORBBEC_CAMERA_NODE_CONFIG 指定。",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    init_parser = subparsers.add_parser(
        "init-device",
        prog="orbbec-camera init-device",
        help="检查设备环境；可信安装路径中的 frozen 二进制可通过 PolicyKit 初始化系统配置",
        formatter_class=_HelpFormatter,
    )
    init_parser.add_argument(
        "--privileged",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    subparsers.add_parser(
        "licenses",
        prog="orbbec-camera licenses",
        help="显示项目和第三方依赖许可证并退出",
        formatter_class=_HelpFormatter,
    )

    list_parser = subparsers.add_parser(
        "list-devices",
        prog="orbbec-camera list-devices",
        description=_LIST_DEVICES_DESCRIPTION,
        epilog=(
            "示例（本仓库根目录）:\n"
            "  uv run orbbec-camera list-devices\n"
            "  uv run orbbec-camera list-devices --json"
        ),
        help="列出已连接的 Orbbec 设备（Index/Serial 等）并退出",
        formatter_class=_HelpFormatter,
    )
    list_parser.add_argument(
        "--json",
        action="store_true",
        help="输出 JSON：{\"devices\": [{\"name\": \"...\", \"address\": \"...\"}]}，供前端读取。",
    )

    snap = subparsers.add_parser(
        "snapshot",
        prog="orbbec-camera snapshot",
        description=_SNAPSHOT_DESCRIPTION,
        epilog=_SNAPSHOT_EPILOG,
        help="截一帧保存为图像文件并退出（默认仅 Color JPEG；可加 --all-streams）",
        formatter_class=_HelpFormatter,
    )
    snap.add_argument(
        "-o",
        "--output",
        default="snapshot.jpg",
        metavar="PATH",
        help="输出路径。默认: 单个 Color JPEG。若使用 --all-streams，则用该路径的文件名主干生成三路文件名（见本帮助末尾）。",
    )
    snap.add_argument(
        "--all-streams",
        action="store_true",
        help="除 Color 外，同时写出 Depth（uint16 PNG）与 IR（PNG）；需配置中启用对应流，否则跳过并警告。",
    )
    snap.add_argument(
        "--device-index",
        type=int,
        default=None,
        metavar="N",
        help="设备索引，非负整数；与 list-devices 第一列 Index 一致。省略时为 0（未使用 --config 时）。",
    )
    snap.add_argument(
        "--serial",
        type=str,
        default=None,
        metavar="SN",
        help="设备序列号；若设置则优先于 --device-index 选设备。",
    )
    snap.add_argument(
        "--config",
        dest="snapshot_config",
        type=str,
        default=None,
        metavar="PATH",
        help="可选。加载完整 Orbbec YAML（分辨率、depth/ir 开关、对齐方式等）；不传则使用内置默认快照配置。",
    )
    snap.add_argument(
        "--jpeg-quality",
        type=int,
        default=None,
        metavar="Q",
        help="仅影响 Color JPEG 质量，范围 1–100；省略时使用 YAML 中的 color.jpeg_quality（内置默认约 85）。",
    )

    return parser.parse_args()


@dataclass
class EncodedOutputs:
    """encode 线程产出的最新 Arrow 载荷（drop-old 单槽）。"""

    seq: int
    timestamp_ms: int
    capture_timestamp_ns: int | None
    color: pa.RecordBatch | None
    depth: pa.RecordBatch | None
    ir: pa.RecordBatch | None


def _frame_to_color_image(frame: OrbbeFrame, config: OrbbecConfig) -> Image | CompressedImage | None:
    """将 OrbbeFrame.color / color_jpeg 转为 forge_msgs.Image / CompressedImage。"""
    if frame.color_jpeg is not None:
        return CompressedImage(format="jpeg", data=frame.color_jpeg)
    if frame.color is None:
        return None
    if config.color.format == "jpeg":
        return CompressedImage.from_numpy(
            frame.color,
            format="jpeg",
            quality=config.color.jpeg_quality,
        )
    return Image.from_numpy(frame.color, encoding="rgb8")


def _frame_to_depth_image(frame: OrbbeFrame) -> Image | None:
    """将 OrbbeFrame.depth（米 float32）转为 forge_msgs.Image 32FC1。"""
    if frame.depth is None:
        return None
    return Image.from_numpy(frame.depth, encoding="32FC1")


def _frame_to_ir_image(frame: OrbbeFrame) -> Image | None:
    """将 OrbbeFrame.ir 转为 forge_msgs.Image。

    Y8（uint8）→ mono8；Y16（uint16）→ 16UC1。
    """
    if frame.ir is None:
        return None
    if frame.ir.dtype.itemsize == 1:
        return Image.from_numpy(frame.ir, encoding="mono8")
    return Image.from_numpy(frame.ir, encoding="16UC1")


def _encode_frame(frame: OrbbeFrame, config: OrbbecConfig, seq: int) -> EncodedOutputs:
    color_img = _frame_to_color_image(frame, config)
    depth_img = _frame_to_depth_image(frame) if config.depth.enabled else None
    ir_img = _frame_to_ir_image(frame) if config.ir.enabled else None
    return EncodedOutputs(
        seq=seq,
        timestamp_ms=frame.timestamp_ms,
        capture_timestamp_ns=frame.capture_timestamp_ns,
        color=None if color_img is None else color_img.to_arrow(),
        depth=None if depth_img is None else depth_img.to_arrow(),
        ir=None if ir_img is None else ir_img.to_arrow(),
    )


def _capture_metadata(capture_timestamp_ns: int | None) -> dict[str, int]:
    """Build optional user metadata without touching Dora-managed metadata."""
    if capture_timestamp_ns is None:
        return {}
    return {"capture_timestamp_ns": capture_timestamp_ns}


def _send_output(
    node: object,
    output_id: str,
    payload: pa.RecordBatch,
    capture_timestamp_ns: int | None,
) -> None:
    node.send_output(  # type: ignore[attr-defined]
        output_id,
        payload,
        metadata=_capture_metadata(capture_timestamp_ns),
    )


def run_node(config: OrbbecConfig) -> int:
    """启动 dora 节点主循环。"""
    logger.info(
        "[orbbec_camera] 初始化设备 serial=%s index=%d align=%s",
        config.device_serial or "auto",
        config.device_index,
        config.align_mode,
    )

    # Start isolated USB capture before creating Dora/Zenoh file descriptors.
    backend: CaptureBackend = create_backend(config)

    from dora import Node  # noqa: PLC0415

    node = Node()

    stop_encode = threading.Event()
    outputs_lock = threading.Lock()
    latest_outputs: EncodedOutputs | None = None
    encode_error: str | None = None
    last_sent_seq = -1

    def encode_loop() -> None:
        nonlocal latest_outputs, encode_error
        after_seq = -1
        while not stop_encode.is_set():
            try:
                frame, seq = backend.wait_new_frame(after_seq, timeout=0.5)
            except RuntimeError as exc:
                encode_error = str(exc)
                stop_encode.set()
                return
            if seq <= after_seq:
                continue
            after_seq = seq
            encoded = _encode_frame(frame, config, seq)
            with outputs_lock:
                latest_outputs = encoded

    encode_thread = threading.Thread(
        target=encode_loop,
        name="orbbec-encode",
        daemon=True,
    )
    encode_thread.start()

    logger.info(
        "[orbbec_camera] 节点启动，输出: color=%s depth=%s ir=%s",
        config.output_color,
        config.output_depth,
        config.output_ir,
    )

    try:
        for event in node:
            match event["type"]:
                case "INPUT":
                    if event["id"] != "tick":
                        continue

                    with outputs_lock:
                        err = encode_error
                        out = latest_outputs
                    if err is not None:
                        logger.error("[orbbec_camera] 采集失败，节点退出: %s", err)
                        return 1
                    if out is None or out.seq == last_sent_seq:
                        continue
                    last_sent_seq = out.seq

                    if out.color is not None:
                        _send_output(
                            node,
                            config.output_color,
                            out.color,
                            out.capture_timestamp_ns,
                        )
                    else:
                        logger.warning(
                            "[orbbec_camera] tick %d: Color 帧为空，跳过",
                            out.timestamp_ms,
                        )

                    if config.depth.enabled:
                        if out.depth is not None:
                            _send_output(
                                node,
                                config.output_depth,
                                out.depth,
                                out.capture_timestamp_ns,
                            )
                        else:
                            logger.warning(
                                "[orbbec_camera] tick %d: Depth 帧为空，跳过",
                                out.timestamp_ms,
                            )

                    if config.ir.enabled:
                        if out.ir is not None:
                            _send_output(
                                node,
                                config.output_ir,
                                out.ir,
                                out.capture_timestamp_ns,
                            )
                        else:
                            logger.warning(
                                "[orbbec_camera] tick %d: IR 帧为空，跳过",
                                out.timestamp_ms,
                            )

                case "STOP":
                    logger.info("[orbbec_camera] 收到 Stop 事件，退出")
                    break

                case "ERROR":
                    logger.error("[orbbec_camera] dora 错误: %s", event.get("error", "unknown"))
                    return 1

    finally:
        stop_encode.set()
        backend.close()
        encode_thread.join(timeout=5.0)
        logger.info("[orbbec_camera] 节点已关闭")

    return 0


def _print_licenses() -> int:
    """Print the license bundle embedded by the release build."""
    from pathlib import Path

    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root is not None:
        candidates = [Path(frozen_root) / "THIRD_PARTY_LICENSES.txt"]
    else:
        project_root = Path(__file__).resolve().parents[2]
        candidates = [
            project_root / "build" / "pyinstaller" / "THIRD_PARTY_LICENSES.txt",
            project_root / "LICENSE",
        ]
        try:
            from importlib.metadata import PackageNotFoundError, distribution

            package_distribution = distribution("forge-devices-orbbec-camera")
            for entry in package_distribution.files or ():
                entry_text = str(entry).replace("\\", "/")
                if entry_text.endswith(".dist-info/licenses/LICENSE"):
                    candidates.append(Path(package_distribution.locate_file(entry)))
        except PackageNotFoundError:
            pass

    for candidate in candidates:
        if candidate.is_file():
            print(candidate.read_text(encoding="utf-8"), end="")
            return 0
    print("License information is unavailable in this installation.", file=sys.stderr)
    return 1


def main() -> int:
    args = _parse_args()

    if args.command == "init-device":
        return run_init_device(privileged=args.privileged)

    if args.command == "licenses":
        return _print_licenses()

    if args.command == "list-devices":
        return run_list_devices(json_output=args.json)

    if args.command == "snapshot":
        config_path = args.snapshot_config or args.config
        cfg = resolve_snapshot_config(
            config_path=config_path,
            device_index=args.device_index,
            device_serial=args.serial,
        )
        if not runtime_preflight():
            return 1
        return run_snapshot(
            cfg,
            args.output,
            jpeg_quality=args.jpeg_quality,
            all_streams=args.all_streams,
        )

    config = OrbbecConfig.load(args.config)
    if not runtime_preflight():
        return 1
    return run_node(config)


if __name__ == "__main__":
    sys.exit(main())
