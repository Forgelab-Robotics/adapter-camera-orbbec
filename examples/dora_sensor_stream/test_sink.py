#!/usr/bin/env python3
"""Dora 图像消息测试消费节点。"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dora import Node
from forge_msgs import CompressedImage, Image


def decode_message(value: object) -> tuple[int, int, str]:
    """解码 Image/CompressedImage，返回宽、高和编码。"""
    try:
        message = Image.from_arrow(value)
        return message.width, message.height, message.encoding
    except (KeyError, TypeError, ValueError):
        pass
    try:
        message = CompressedImage.from_arrow(value)
        frame = message.to_numpy()
        height, width = frame.shape[:2]
        return width, height, message.format
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"无法解码 Image/CompressedImage: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="解码并统计 Dora 图像流")
    parser.add_argument("--config", default="test_sink.yaml")
    args = parser.parse_args()
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    log_every = max(1, int(config.get("log_every", 30)))
    counts: dict[str, int] = defaultdict(int)

    for event in Node():
        if event["type"] == "STOP":
            break
        if event["type"] != "INPUT":
            continue
        topic = str(event["id"])
        counts[topic] += 1
        width, height, encoding = decode_message(event["value"])
        if counts[topic] == 1 or counts[topic] % log_every == 0:
            received_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            print(
                f"topic={topic} count={counts[topic]} size={width}x{height} "
                f"encoding={encoding} received_at={received_at}",
                flush=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
