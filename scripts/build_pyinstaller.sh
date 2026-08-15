#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
DIST_DIR="${PACKAGE_DIR}/dist"
WORK_DIR="${PACKAGE_DIR}/build/pyinstaller"
VENV_DIR="${PACKAGE_DIR}/.venv_build"

cd "${PACKAGE_DIR}"
mkdir -p "${DIST_DIR}" "${WORK_DIR}"
rm -f "${DIST_DIR}/orbbec_camera" "${DIST_DIR}/orbbec_camera.exe"
rm -rf "${VENV_DIR}"
trap 'rm -rf "${VENV_DIR}"' EXIT

echo "==> [orbbec_camera] 正在初始化隔离的局部构建虚拟环境..."
UV_PROJECT_ENVIRONMENT="${VENV_DIR}" uv sync \
  --project "${PACKAGE_DIR}" \
  --frozen \
  --no-default-groups \
  --group build \
  --python 3.12

LICENSE_BUNDLE="${WORK_DIR}/THIRD_PARTY_LICENSES.txt"
LICENSE_JSON="${WORK_DIR}/third-party-licenses.json"
echo "==> [orbbec_camera] 正在生成内嵌许可证清单..."
"${VENV_DIR}/bin/pip-licenses" \
  --format=json \
  --with-license-file \
  --no-license-path \
  --ignore-packages dora-rs forge-devices-orbbec-camera pygame \
  > "${LICENSE_JSON}"
if grep -q '"LicenseText": "UNKNOWN"' "${LICENSE_JSON}"; then
  echo "ERROR: 依赖许可证正文仍包含 UNKNOWN 条目。" >&2
  exit 1
fi
{
  printf '%s\n\n' 'forge-devices-orbbec-camera — Apache License 2.0'
  cat "${PACKAGE_DIR}/LICENSE"
  printf '\n\n%s\n\n' 'Third-party Python distributions and license texts'
  "${VENV_DIR}/bin/pip-licenses" \
    --format=plain-vertical \
    --with-license-file \
    --no-license-path \
    --ignore-packages dora-rs forge-devices-orbbec-camera pygame
  printf '\n\n%s\n\n' 'Dependencies with incomplete wheel license files'
  cat "${SCRIPT_DIR}/licenses/dora-rs-0.4.1.txt"
  printf '\n\n'
  cat "${SCRIPT_DIR}/licenses/pygame-2.6.1-LGPL-2.1.txt"
} > "${LICENSE_BUNDLE}"
if grep -Eq '/home/|file:///|git\+ssh://' "${LICENSE_BUNDLE}"; then
  echo "ERROR: 许可证清单包含私有 URL 或构建路径。" >&2
  exit 1
fi
chmod 0644 "${LICENSE_BUNDLE}"


echo "==> [orbbec_camera] 开始使用 PyInstaller 进行打包..."
"${VENV_DIR}/bin/pyinstaller" \
  --noconfirm \
  --clean \
  --distpath "${DIST_DIR}" \
  --workpath "${WORK_DIR}" \
  "${SCRIPT_DIR}/orbbec_camera.spec"

if [[ -f "${DIST_DIR}/orbbec_camera" ]]; then
  echo "Built: ${DIST_DIR}/orbbec_camera"
elif [[ -f "${DIST_DIR}/orbbec_camera.exe" ]]; then
  echo "Built: ${DIST_DIR}/orbbec_camera.exe"
else
  echo "WARNING: ${DIST_DIR}/orbbec_camera 未找到，请检查 PyInstaller 输出。" >&2
  exit 1
fi
