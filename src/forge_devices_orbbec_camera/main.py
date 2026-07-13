#!/usr/bin/env python3
"""Orbbec 深度相机 dora 节点。

支持 Gemini 2 系列，每个 tick 同时输出：
  - image/color  Color 帧（Image rgb8 或 CompressedImage jpeg）
  - image/depth  Depth 帧（Image 32FC1，单位 m）
  - image/ir     IR 帧（Image mono8 或 16UC1）

CLI 子命令（不启动 dora）：
  list-devices   列出已连接的 Orbbec 设备（可加 --json 给前端读取）
  snapshot       截单帧保存图像（默认 Color JPEG；可选用 --all-streams 写 Color/Depth/IR）
"""

from __future__ import annotations

import argparse
import sys

from dora import Node
from forge_common import get_logger
from forge_msgs import CompressedImage, Image

from .backend import CaptureBackend, OrbbeFrame, create_backend
from .config import OrbbecConfig
from .list_devices import run_list_devices
from .snapshot import resolve_snapshot_config, run_snapshot

logger = get_logger(__name__)


class _HelpFormatter(
    argparse.ArgumentDefaultsHelpFormatter,
    argparse.RawDescriptionHelpFormatter,
):
    """显示参数默认值，并保留 epilog 中的换行。"""


_ROOT_DESCRIPTION = """\
Orbbec Gemini 2 深度相机 dora 节点：在 tick 驱动下输出 Color / Depth / IR（forge_msgs.Image / CompressedImage）。

【节点模式】不提供子命令时启动 dora，需配置文件。
【工具子命令】list-devices / snapshot 仅操作本机相机并退出，不启动 dora。"""

_ROOT_EPILOG = """\
运行模式说明:
  节点      在本仓库根目录:
            uv run orbbec-camera --config <YAML>
            或: ORBBEC_CAMERA_NODE_CONFIG=<YAML 绝对或相对路径> 同上
            从 YAML 读设备、分辨率、对齐方式等；向 dataflow 声明的 topic 发图。

  枚举设备  uv run orbbec-camera list-devices
            uv run orbbec-camera list-devices --json

  截帧落盘  uv run orbbec-camera snapshot ...
            uv run orbbec-camera snapshot --config <YAML> ...
            默认只存 Color JPEG；--all-streams 另存 Depth/IR。

环境:
  仅 Linux；首次部署需 udev + libusb（见 README 中 scripts/install_permissions.sh）。

常用示例（均在本仓库根目录执行）:
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
        "--config",
        type=str,
        default=None,
        metavar="PATH",
        help="YAML 配置文件路径。节点模式和 snapshot 子命令共用；也可通过环境变量 ORBBEC_CAMERA_NODE_CONFIG 指定。",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

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


def _frame_to_color_image(frame: OrbbeFrame, config: OrbbecConfig) -> Image | CompressedImage | None:
    """将 OrbbeFrame.color 转为 forge_msgs.Image / CompressedImage。"""
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
    """将 OrbbeFrame.depth 转为 forge_msgs.Image。

    将后端毫米深度转换为 float32 米，编码固定为 32FC1。
    """
    if frame.depth is None:
        return None
    # SDK 边界统一提供毫米 float32；公共消息使用米 float32。
    depth_m = frame.depth.astype("float32") * 0.001
    return Image.from_numpy(depth_m, encoding="32FC1")


def _frame_to_ir_image(frame: OrbbeFrame) -> Image | None:
    """将 OrbbeFrame.ir 转为 forge_msgs.Image。

    Y8（uint8）→ mono8；Y16（uint16）→ 16UC1。
    """
    if frame.ir is None:
        return None
    if frame.ir.dtype.itemsize == 1:
        return Image.from_numpy(frame.ir, encoding="mono8")
    return Image.from_numpy(frame.ir, encoding="16UC1")


def run_node(config: OrbbecConfig) -> int:
    """启动 dora 节点主循环。"""
    logger.info(
        "[orbbec_camera] 初始化设备 serial=%s index=%d align=%s",
        config.device_serial or "auto",
        config.device_index,
        config.align_mode,
    )

    backend: CaptureBackend = create_backend(config)
    node = Node()

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

                    try:
                        frame = backend.capture_frame()
                    except RuntimeError as e:
                        msg = str(e)
                        logger.error("[orbbec_camera] 采集失败，节点退出: %s", msg)
                        return 1

                    # Color
                    color_img = _frame_to_color_image(frame, config)
                    if color_img is not None:
                        node.send_output(config.output_color, color_img.to_arrow())
                    else:
                        logger.warning("[orbbec_camera] tick %d: Color 帧为空，跳过", frame.timestamp_ms)

                    # Depth
                    if config.depth.enabled:
                        depth_img = _frame_to_depth_image(frame)
                        if depth_img is not None:
                            node.send_output(config.output_depth, depth_img.to_arrow())
                        else:
                            logger.warning("[orbbec_camera] tick %d: Depth 帧为空，跳过", frame.timestamp_ms)

                    # IR
                    if config.ir.enabled:
                        ir_img = _frame_to_ir_image(frame)
                        if ir_img is not None:
                            node.send_output(config.output_ir, ir_img.to_arrow())
                        else:
                            logger.warning("[orbbec_camera] tick %d: IR 帧为空，跳过", frame.timestamp_ms)

                case "STOP":
                    logger.info("[orbbec_camera] 收到 Stop 事件，退出")
                    break

                case "ERROR":
                    logger.error("[orbbec_camera] dora 错误: %s", event.get("error", "unknown"))
                    return 1

    finally:
        backend.close()
        logger.info("[orbbec_camera] 节点已关闭")

    return 0


def main() -> int:
    args = _parse_args()

    if args.command == "list-devices":
        return run_list_devices(json_output=args.json)

    if args.command == "snapshot":
        config_path = args.snapshot_config or args.config
        cfg = resolve_snapshot_config(
            config_path=config_path,
            device_index=args.device_index,
            device_serial=args.serial,
        )
        return run_snapshot(
            cfg,
            args.output,
            jpeg_quality=args.jpeg_quality,
            all_streams=args.all_streams,
        )

    config = OrbbecConfig.load(args.config)
    return run_node(config)


if __name__ == "__main__":
    sys.exit(main())
