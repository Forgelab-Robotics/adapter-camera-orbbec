#!/usr/bin/env python3
"""采集一组 Color/Depth/IR 样本。"""

from __future__ import annotations

import argparse

from forge_devices_orbbec_camera.config import OrbbecConfig
from forge_devices_orbbec_camera.snapshot import run_snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description="采集 Orbbec 单帧样本")
    parser.add_argument("--config", default="config/sensor.example.yaml")
    parser.add_argument("--output", default="sample_output/capture.jpg")
    parser.add_argument("--all-streams", action="store_true")
    args = parser.parse_args()
    config = OrbbecConfig.from_yaml_path(args.config)
    return run_snapshot(config, args.output, all_streams=args.all_streams)


if __name__ == "__main__":
    raise SystemExit(main())
