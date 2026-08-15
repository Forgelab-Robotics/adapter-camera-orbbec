# Releasing Orbbec Camera

## Versioning

The project follows Semantic Versioning. Update the package version in `pyproject.toml`, regenerate `uv.lock`, update version-sensitive tests, and move relevant entries from `Unreleased` into a dated section in `CHANGELOG.md`.

## Release checks

Release from a clean, reviewed commit using a supported Python version:

```bash
uv lock --check
uv sync --frozen
uv run --frozen pytest tests -q
uv build
uvx pip-audit --path .venv/lib/python3.12/site-packages
```

Confirm the wheel and sdist contain `LICENSE`, use only public dependency sources, and contain no generated captures, device identifiers, private paths, internal URLs, SDK files, or build artifacts.

Complete the applicable hardware checks for device discovery, Color/Depth/IR capture, alignment, shutdown/reopen behavior, isolated process cleanup, and the failure modes affected by the release.

## Source release

Before creating a tag:

1. Confirm `pyproject.toml`, `uv.lock`, `README.md`, and `CHANGELOG.md` agree on the version and requirements.
2. Confirm the repository working tree is clean and CI passes.
3. Create an immutable annotated tag named `v<version>` at the validated commit.

Do not store PyPI or repository tokens in this repository. Published package versions and public release tags must never be replaced.

## Binary release

Build the user-facing Linux x86_64 executable in a clean environment using the locked dependencies:

```bash
bash scripts/build_pyinstaller.sh
```

The public archive is named `orbbec_camera-v<version>-linux-x86_64.tar.gz` and contains only the executable named `orbbec_camera`. Project and third-party license texts are embedded in that executable and must be available through `orbbec_camera licenses`. Do not include development utilities, test sinks, configuration files, captures, or previously generated files from `dist/`.

The PyInstaller executable bundles Python and native Orbbec SDK components but remains tied to Linux x86_64 and a compatible glibc baseline. The target system still requires the appropriate kernel USB support, libusb runtime, udev permissions, and camera hardware.

Before upload, verify the CLI and a real snapshot, inspect ELF dependencies and minimum glibc symbols, scan for private paths and internal URLs, record SHA-256, and test on the oldest supported deployment image. Verify both `orbbec_camera --version` and `orbbec_camera licenses`; license information must also remain available in the repository and source release.
