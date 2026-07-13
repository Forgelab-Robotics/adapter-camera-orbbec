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
