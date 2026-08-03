"""Orbbec 设备列举工具。

对应 Rust camera 节点的 list_devices.rs，可独立运行，不启动 dora。

用法：
    orbbec-camera list-devices
    orbbec-camera snapshot -o snapshot.jpg
    # 或直接运行：
    python scripts/list_devices.py
"""

from __future__ import annotations

import json
import sys

from forge_common import get_logger

logger = get_logger(__name__)


def _device_to_json(dev) -> dict[str, object]:
    """Convert DeviceInfo to the JSON shape consumed by UI tooling."""
    return {
        "name": dev.name,
        "address": str(dev.index),
    }


def run_list_devices(*, json_output: bool = False) -> int:
    """列出所有已连接的 Orbbec 设备，每行输出一个设备的信息。

    Returns:
        0 表示成功（包括没有设备的情况），1 表示 SDK 加载失败。
    """
    try:
        from .backend_orbbec import list_orbbec_devices
    except ImportError as e:
        logger.error("错误：无法加载 pyorbbecsdk，请确认已安装：pip install pyorbbecsdk2\n%s", e)
        return 1

    try:
        devices = list_orbbec_devices()
    except Exception as e:
        logger.error("错误：查询设备失败: %s", e)
        return 1

    if json_output:
        payload = {"devices": [_device_to_json(dev) for dev in devices]}
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0

    if not devices:
        logger.info("未发现已连接的 Orbbec 设备。")
        logger.info("请检查：")
        logger.info("  1. 设备是否已通过 USB 连接")
        logger.info("  2. 是否已运行 init-device 或 install_permissions.sh 配置设备权限")
        logger.info("  3. 执行 udev 配置后是否重新插拔了设备")
        return 0

    logger.info("发现 %s 个 Orbbec 设备：", len(devices))
    logger.info("%-6s %-20s %-30s %-16s %s", "Index", "Serial", "Name", "Firmware", "USB")
    logger.info("%s", "-" * 85)
    for dev in devices:
        logger.info(
            "%-6s %-20s %-30s %-16s %s",
            dev.index,
            dev.serial,
            dev.name,
            dev.firmware_version,
            dev.usb_type,
        )
    return 0


def main() -> int:
    """Console-script entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="列出已连接的 Orbbec 设备")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出，供前端或自动化工具读取")
    args = parser.parse_args()
    return run_list_devices(json_output=args.json)


if __name__ == "__main__":
    sys.exit(main())
