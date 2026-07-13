#!/usr/bin/env python3
"""执行不访问相机的运行环境自检。"""

from __future__ import annotations

import importlib.metadata
import platform
import shutil
import sys


def main() -> int:
    checks: list[tuple[str, bool, str]] = []
    checks.append(("platform", sys.platform.startswith("linux"), platform.platform()))
    checks.append(("python", sys.version_info >= (3, 11), platform.python_version()))
    checks.append(("uv", shutil.which("uv") is not None, shutil.which("uv") or "未找到"))

    for distribution in ("pyorbbecsdk2", "dora-rs", "forge-msgs", "forge-common"):
        try:
            version = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            checks.append((distribution, False, "未安装"))
        else:
            checks.append((distribution, True, version))

    for name, ok, detail in checks:
        print(f"[{'OK' if ok else 'FAIL'}] {name}: {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
