#!/usr/bin/env bash
# Explicit administrator setup for source deployments.
# Installs the fixed udev rule and configures the actual invoking user for the
# video group. The system libusb runtime must already be installed.
#
# Usage:
#   sudo bash scripts/install_permissions.sh

set -Eeuo pipefail

PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
umask 022
unset BASH_ENV ENV CDPATH GLOBIGNORE LD_PRELOAD LD_AUDIT PYTHONPATH PYTHONHOME || true

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RULES_SRC="$SCRIPT_DIR/../src/forge_devices_orbbec_camera/resources/99-obsensor-libusb.rules"
EXPECTED_RULES_SHA256="3e5b10c65c292ee40f337e11b1c04ec5635e0fdc60bc66e63810e5ca06860fa3"
RULES_DIR="/etc/udev/rules.d"
RULES_DEST="$RULES_DIR/99-obsensor-libusb.rules"
TEMP_RULE=""
COMPLETED_STEPS=()

cleanup() {
    if [ -n "$TEMP_RULE" ] && [ -e "$TEMP_RULE" ]; then
        rm -f -- "$TEMP_RULE"
    fi
}

report_partial_failure() {
    status=$?
    if [ "${#COMPLETED_STEPS[@]}" -gt 0 ]; then
        echo "[setup] 部分修改已经完成：" >&2
        for step in "${COMPLETED_STEPS[@]}"; do
            echo "  - $step" >&2
        done
        echo "[setup] 请根据以上错误处理，再运行 orbbec-camera init-device 或直接启动相机节点确认状态。" >&2
    fi
    exit "$status"
}

trap cleanup EXIT
trap report_partial_failure ERR

if [ "$(id -u)" -ne 0 ]; then
    echo "请以 sudo 运行本脚本：sudo bash scripts/install_permissions.sh" >&2
    exit 1
fi

CALLER_USER=""
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    CALLER_USER="$SUDO_USER"
elif [[ "${PKEXEC_UID:-}" =~ ^[1-9][0-9]*$ ]]; then
    CALLER_USER="$(getent passwd "$PKEXEC_UID" | cut -d: -f1)"
fi

if [ -z "$CALLER_USER" ] || ! id "$CALLER_USER" >/dev/null 2>&1; then
    echo "[setup] 错误：无法确认实际普通调用用户；请通过 sudo 从目标用户会话运行。" >&2
    exit 1
fi
if [ "$(id -u "$CALLER_USER")" -le 0 ]; then
    echo "[setup] 错误：实际调用用户必须是 UID 大于 0 的普通用户。" >&2
    exit 1
fi
if ! getent group video >/dev/null 2>&1; then
    echo "[setup] 错误：系统不存在 video 用户组，请先由管理员确认系统用户组策略。" >&2
    exit 1
fi
if [ ! -f "$RULES_SRC" ] || [ -L "$RULES_SRC" ]; then
    echo "[setup] 错误：固定规则必须是普通文件且不能是符号链接：$RULES_SRC" >&2
    exit 1
fi
if grep -Eiq '(^|,)[[:space:]]*(RUN|PROGRAM|IMPORT)[{+:=]' "$RULES_SRC"; then
    echo "[setup] 错误：udev 规则不得包含 RUN、PROGRAM 或 IMPORT 指令。" >&2
    exit 1
fi
rules_sha256="$(sha256sum -- "$RULES_SRC" | cut -d ' ' -f1)"
if [ "$rules_sha256" != "$EXPECTED_RULES_SHA256" ]; then
    echo "[setup] 错误：udev 规则校验失败；请使用已审核版本。" >&2
    exit 1
fi

validate_root_directory() {
    directory="$1"
    if [ ! -d "$directory" ] || [ -L "$directory" ]; then
        echo "[setup] 错误：安全目录不存在、不是目录或为符号链接：$directory" >&2
        exit 1
    fi
    owner="$(stat -c %u "$directory")"
    mode="$(stat -c %a "$directory")"
    if [ "$owner" -ne 0 ] || (( (8#$mode & 0022) != 0 )); then
        echo "[setup] 错误：安全目录必须属于 root 且不可被 group/world 写入：$directory" >&2
        exit 1
    fi
}

validate_root_directory "/"
validate_root_directory "/etc"
validate_root_directory "/etc/udev"
validate_root_directory "$RULES_DIR"

if [ -e "$RULES_DEST" ] || [ -L "$RULES_DEST" ]; then
    if [ -L "$RULES_DEST" ] || [ ! -f "$RULES_DEST" ]; then
        echo "[setup] 错误：目标规则必须是普通文件且不能是符号链接：$RULES_DEST" >&2
        exit 1
    fi
    target_owner="$(stat -c %u "$RULES_DEST")"
    target_mode="$(stat -c %a "$RULES_DEST")"
    if [ "$target_owner" -ne 0 ] || (( (8#$target_mode & 0022) != 0 )); then
        echo "[setup] 错误：目标规则必须属于 root 且不可被 group/world 写入：$RULES_DEST" >&2
        exit 1
    fi
fi

shopt -s nullglob
CONFLICTS=()
declare -A SEEN_RULES=()
for rule_dir in /etc/udev/rules.d /run/udev/rules.d /usr/lib/udev/rules.d /lib/udev/rules.d; do
    [ -d "$rule_dir" ] || continue
    for candidate in "$rule_dir"/*.rules; do
        [ "$candidate" = "$RULES_DEST" ] && continue
        candidate_key="$(readlink -f "$candidate" 2>/dev/null || printf '%s' "$candidate")"
        if [[ -n "${SEEN_RULES[$candidate_key]+x}" ]]; then
            continue
        fi
        SEEN_RULES["$candidate_key"]=1
        if grep -qi '2bc5' "$candidate" 2>/dev/null; then
            CONFLICTS+=("$candidate")
        else
            grep_status=$?
            if [ "$grep_status" -gt 1 ]; then
                CONFLICTS+=("$candidate (无法读取)")
            fi
        fi
    done
done
if [ "${#CONFLICTS[@]}" -gt 0 ]; then
    echo "[setup] 错误：检测到其他引用 Orbbec vendor 2bc5 的 udev 规则：" >&2
    printf '  - %s\n' "${CONFLICTS[@]}" >&2
    echo "[setup] 请由管理员核对冲突规则；本脚本不会自动删除。" >&2
    exit 1
fi

echo "[setup] 本脚本不会调用包管理器；请事先安装发行版提供的 libusb 运行时。"

TEMP_RULE="$(mktemp "$RULES_DIR/.99-obsensor-libusb.rules.XXXXXX")"
install -o root -g root -m 0644 "$RULES_SRC" "$TEMP_RULE"
temp_sha256="$(sha256sum -- "$TEMP_RULE" | cut -d ' ' -f1)"
if [ "$temp_sha256" != "$EXPECTED_RULES_SHA256" ]; then
    echo "[setup] 错误：临时 udev 规则校验失败。" >&2
    exit 1
fi
mv -fT -- "$TEMP_RULE" "$RULES_DEST"
TEMP_RULE=""
COMPLETED_STEPS+=("已原子安装 $RULES_DEST")
sync -f "$RULES_DIR"

if id -nG "$CALLER_USER" | tr ' ' '\n' | grep -qx video; then
    echo "[setup] 用户 $CALLER_USER 已属于 video 组。"
else
    usermod -aG video "$CALLER_USER"
    COMPLETED_STEPS+=("已将用户 $CALLER_USER 加入 video 组")
fi

udevadm control --reload-rules
COMPLETED_STEPS+=("已重新加载 udev 规则")
udevadm trigger
COMPLETED_STEPS+=("已触发 udev 设备更新")

echo "[setup] 完成："
for step in "${COMPLETED_STEPS[@]}"; do
    echo "  - $step"
done
echo "[setup] 请重新插拔 Orbbec，并注销后重新登录以刷新用户组。"
