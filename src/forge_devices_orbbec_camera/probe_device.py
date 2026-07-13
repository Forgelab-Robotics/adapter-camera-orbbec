"""Orbbec 基本设备诊断入口。

厂商 SDK 访问由 :mod:`backend_orbbec` 封装，本模块不引用 SDK。
"""

from __future__ import annotations

import json


def run_probe_device(device_index: int = 0, device_serial: str | None = None) -> int:
    try:
        from .backend_orbbec import probe_orbbec_device

        result = probe_orbbec_device(device_index=device_index, device_serial=device_serial)
    except Exception as exc:
        print(f"设备诊断失败: {exc}")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="读取 Orbbec 设备基本信息")
    parser.add_argument("--index", type=int, default=0, help="设备索引")
    parser.add_argument("--serial", default=None, help="设备序列号")
    args = parser.parse_args()
    return run_probe_device(args.index, args.serial)


if __name__ == "__main__":
    raise SystemExit(main())
