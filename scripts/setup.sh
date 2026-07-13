#!/usr/bin/env bash
# 目标机「仅系统环境」配置：udev 规则 + libusb 运行时（.so）。
# 供「PyInstaller 打包后的 orbbec_camera 二进制」等场景在目标 Linux 上免源码部署时使用。
#
# 本脚本不执行：uv、pip、npm、不安装 Python/项目依赖；与 forge_runtime 内 venv 无关。
# 若因执行本机上的 `uv run ...` 而创建 .venv，那是 uv 自身行为，不是本脚本触发的。
#
# 从源码编译 pyorbbecsdk 需头文件时，请开发机自行：apt install libusb-1.0-0-dev
#
# 为何必须 sudo：向 /etc/udev/rules.d/ 写入文件只有 root 能做。
#
# 用法：
#   sudo bash scripts/setup.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RULES_SRC="$SCRIPT_DIR/udev/99-obsensor-libusb.rules"
RULES_DEST="/etc/udev/rules.d/99-obsensor-libusb.rules"

if [ "$(id -u)" -ne 0 ]; then
    echo "请以 sudo 运行本脚本：sudo bash $0"
    exit 1
fi

# libusb 运行时：打包二进制 / wheel 在本机运行需要 libusb-1.0.so（不含 -dev）。
if command -v apt-get >/dev/null 2>&1; then
    echo "[setup] apt-get install -y libusb-1.0-0"
    if ! DEBIAN_FRONTEND=noninteractive apt-get install -y libusb-1.0-0; then
        echo "[setup] 错误：apt 安装 libusb 失败（可先执行 sudo apt --fix-broken install）。" >&2
        exit 1
    fi
else
    echo "[setup] 未检测到 apt-get，跳过自动安装 libusb；请按发行版自行安装 libusb 运行时（见 README）。"
fi

if [ -f "$RULES_SRC" ]; then
    cp "$RULES_SRC" "$RULES_DEST"
    echo "[setup] udev 规则已安装：$RULES_DEST（来源：$RULES_SRC）"
else
    echo "[setup] 错误：未找到 $RULES_SRC" >&2
    echo "[setup] 请将 99-obsensor-libusb.rules 置于与 setup.sh 同级的 udev/ 目录。" >&2
    exit 1
fi

udevadm control --reload-rules && udevadm trigger
echo "[setup] 完成。请重新插拔 Orbbec 设备。"
