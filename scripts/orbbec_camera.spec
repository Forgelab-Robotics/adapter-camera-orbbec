# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec：将 Orbbec dora 节点打成单一可执行文件。
#
# pyorbbecsdk2 的原生文件不在常规包目录内，Analysis 无法可靠推断，须在 binaries 中显式列出：
#   - libOrbbecSDK.so*（含 .so.2，与扩展 NEEDED / RUNPATH=$ORIGIN 一致）
#   - pyorbbecsdk*.so
#   - extensions/**（深度引擎 / 帧处理 / 滤波等插件）
#

import glob
import importlib.util
import os


def _collect_pyorbbec_binaries():
    """返回 (绝对路径, bundle 内目标目录) 列表，目录相对于解压根/_MEIPASS。"""
    spec_mod = importlib.util.find_spec("pyorbbecsdk")
    if spec_mod is None or not spec_mod.origin:
        raise SystemExit(
            "PyInstaller: 未找到 pyorbbecsdk。请在构建机执行 uv sync，并安装 pyorbbecsdk2。"
        )
    site_pkgs = os.path.dirname(os.path.abspath(spec_mod.origin))
    out = []
    seen_src = set()

    def add(src: str, dest_dir: str) -> None:
        src = os.path.abspath(src)
        if src in seen_src:
            return
        if not os.path.isfile(src):
            return
        seen_src.add(src)
        out.append((src, dest_dir))

    # 扩展 .so 的 NEEDED 为 libOrbbecSDK.so.2（RUNPATH $ORIGIN）；须与 pyorbbecsdk*.so 同落在解压根目录
    libs = sorted(glob.glob(os.path.join(site_pkgs, "libOrbbecSDK.so*")))
    for path in libs:
        add(path, ".")
    if not libs:
        raise SystemExit(
            "PyInstaller: 未找到 libOrbbecSDK.so*。请确认已 uv sync 且 pyorbbecsdk2 wheel 完整。"
        )

    for path in glob.glob(os.path.join(site_pkgs, "pyorbbecsdk*.so")):
        add(path, ".")

    ext_root = os.path.join(site_pkgs, "extensions")
    if os.path.isdir(ext_root):
        for root, _, files in os.walk(ext_root):
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(root, site_pkgs)
                dest_dir = "." if rel in (".", os.curdir) else rel.replace("\\", "/")
                add(full, dest_dir)
    return out


_spec_dir = os.path.dirname(os.path.abspath(SPEC))
_node_dir = os.path.dirname(_spec_dir)
_src_dir = os.path.join(_node_dir, "src")

_orbbec_bins = _collect_pyorbbec_binaries()
_rule = os.path.join(
    _src_dir,
    "forge_devices_orbbec_camera",
    "resources",
    "99-obsensor-libusb.rules",
)
if not os.path.isfile(_rule):
    raise SystemExit(f"PyInstaller: 未找到内嵌 udev 规则: {_rule}")
_rthook = os.path.join(
    _src_dir, "forge_devices_orbbec_camera", "pyi_rthook_orbbec.py"
)
_license_bundle = os.path.join(
    _node_dir, "build", "pyinstaller", "THIRD_PARTY_LICENSES.txt"
)
if not os.path.isfile(_license_bundle):
    raise SystemExit(
        f"PyInstaller: 未找到许可证清单: {_license_bundle}；请使用 build_pyinstaller.sh 构建。"
    )

a = Analysis(
    [os.path.join(_spec_dir, "pyinstaller_entry.py")],
    pathex=[_src_dir],
    binaries=_orbbec_bins,
    datas=[
        (_rule, "forge_devices_orbbec_camera/resources"),
        (_license_bundle, "."),
    ],
    hiddenimports=[
        "pyorbbecsdk",
        "forge_devices_orbbec_camera.backend_orbbec",
        "cv2",
        "numpy",
        "PIL",
        "PIL.Image",
        "pyarrow",
        "pyarrow.lib",
        "yaml",
        "_yaml",
        "pydantic",
        "pydantic.deprecated.decorator",
        "forge_msgs",
        "forge_msgs.arrow",
        "forge_msgs.image",
        "forge_msgs.point_cloud",
        "forge_common",
        "dora",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[_rthook] if os.path.isfile(_rthook) else [],
    excludes=[
        "torch",
        "torchvision",
        "nvidia",
        "nvidia-cuda-runtime-cu12",
        "nvidia-cublas-cu12",
        "nvidia-cuda-cupti-cu12",
        "nvidia-cuda-nvrtc-cu12",
        "nvidia-cudnn-cu12",
        "nvidia-cufile-cu12",
        "nvidia-curand-cu12",
        "nvidia-cusolver-cu12",
        "nvidia-cusparse-cu12",
        "nvidia-cusparselt-cu12",
        "nvidia-nccl-cu12",
        "nvidia-nvjitlink-cu12",
        "nvidia-nvshmem-cu12",
        "nvidia-nvtx-cu12",
        "cuda-bindings",
        "triton",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="orbbec_camera",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
