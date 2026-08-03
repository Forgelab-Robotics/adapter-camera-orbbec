"""PyInstaller entry point."""

from __future__ import annotations

import multiprocessing
import sys


def _run() -> int:
    # Keep the root helper import surface limited to stdlib + system_setup.
    if sys.argv[1:] == ["init-device", "--privileged"]:
        from forge_devices_orbbec_camera.system_setup import run_init_device

        return run_init_device(privileged=True)

    from forge_devices_orbbec_camera.main import main

    return main()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(_run())
