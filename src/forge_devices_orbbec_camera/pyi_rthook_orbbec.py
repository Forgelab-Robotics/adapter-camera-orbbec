"""PyInstaller runtime hook：在导入任何 Orbbec 原生模块之前修正动态库搜索路径。

pyorbbecsdk2 将 libOrbbecSDK.so 与 extensions/ 置于 site-packages 根目录；
打包为 onefile 后依赖解压目录 sys._MEIPASS。部分环境下需显式把 _MEIPASS
置于 LD_LIBRARY_PATH 前端，以便 libOrbbecSDK.so 解析其插件 .so。
"""

from __future__ import annotations

import os
import sys

_meipass = getattr(sys, "_MEIPASS", None)
if _meipass:
    # 与 PyInstaller bootloader 行为对齐：bundle 根优先
    prev = os.environ.get("LD_LIBRARY_PATH", "")
    os.environ["LD_LIBRARY_PATH"] = (
        _meipass + (":" + prev if prev else "")
    )
