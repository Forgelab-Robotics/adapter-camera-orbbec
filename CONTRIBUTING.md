# Contributing to Orbbec Camera

Thank you for contributing to the Orbbec Camera adapter. Keep changes focused, preserve the documented message contract, and include tests for behavior changes.

## Development setup

The project supports Linux and Python 3.11 or newer. Install `uv` and the system libusb runtime, then create the locked environment:

```bash
uv sync --frozen
```

The unit tests do not require camera hardware.

## Required checks

Before opening a pull request, run:

```bash
uv lock --check
uv run --frozen pytest tests -q
uv build
uvx pip-audit --path .venv/lib/python3.12/site-packages
```

If dependencies change, regenerate `uv.lock`, review source URLs and licenses, and confirm that the public lock file contains no private registry or repository address.

## Hardware-facing changes

Changes to device discovery, stream negotiation, SDK properties, alignment, frame conversion, timestamps, buffering, process isolation, permissions, or reconnect behavior should include:

1. Unit tests for behavior that can be exercised without hardware.
2. A hardware test covering the affected stream, format, or failure mode.
3. A pull request note recording the camera model, SDK version, stream profiles, test duration, and sanitized result.
4. Logs containing no device serial number, private image, personal path, credential, or internal URL.

Do not commit camera images containing people, screens, documents, locations, serial numbers, or other private information. Do not commit large videos, recordings, generated binaries, vendor SDK files, virtual environments, or build directories.

## Privileged and packaging changes

Changes to `system_setup.py`, udev rules, administrator scripts, PyInstaller hooks, or bundled native libraries require explicit security and license review. Never configure passwordless sudo for a script in a user-writable checkout.

## Pull request guidelines

- Explain compatibility, privacy, security, and performance impact where relevant.
- Preserve the `forge_msgs.Image` and `forge_msgs.CompressedImage` semantics documented in `README.md`.
- Do not add private dependencies, mutable Git branches, secrets, credentials, private URLs, or personal filesystem paths.
- Do not add vendor binaries without a documented source, checksum, license, and redistribution review.

## License

By contributing, you agree that your contributions are licensed under the Apache License, Version 2.0.
