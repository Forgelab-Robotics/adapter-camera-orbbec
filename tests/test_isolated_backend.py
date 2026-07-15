from __future__ import annotations

import queue
from unittest import mock

import numpy as np
import pytest

from forge_devices_orbbec_camera import backend
from forge_devices_orbbec_camera.backend import OrbbeFrame
from forge_devices_orbbec_camera.config import OrbbecConfig
from forge_devices_orbbec_camera.isolated_backend import (
    IsolatedOrbbecBackend,
    _put_latest,
)


def test_capture_process_config_validation() -> None:
    assert OrbbecConfig.from_dict({}).capture_process == "direct"
    assert (
        OrbbecConfig.from_dict({"capture_process": "isolated"}).capture_process
        == "isolated"
    )
    with pytest.raises(ValueError, match="capture_process"):
        OrbbecConfig.from_dict({"capture_process": "thread"})


def test_backend_factory_selects_isolated_mode() -> None:
    config = OrbbecConfig(capture_process="isolated")
    sentinel = object()
    with mock.patch(
        "forge_devices_orbbec_camera.isolated_backend.IsolatedOrbbecBackend",
        return_value=sentinel,
    ) as constructor:
        assert backend.create_backend(config) is sentinel
    constructor.assert_called_once_with(config)


def test_put_latest_drops_oldest_packet() -> None:
    frame_queue: queue.Queue = queue.Queue(maxsize=2)
    first = OrbbeFrame(color=None, depth=None, ir=None, timestamp_ms=1)
    second = OrbbeFrame(color=None, depth=None, ir=None, timestamp_ms=2)
    newest = OrbbeFrame(color=None, depth=None, ir=None, timestamp_ms=3)
    frame_queue.put_nowait((1, first))
    frame_queue.put_nowait((2, second))

    _put_latest(frame_queue, (3, newest))

    assert frame_queue.get_nowait()[0] == 2
    seq, frame = frame_queue.get_nowait()
    assert seq == 3
    assert frame is newest


def test_wait_new_frame_returns_newest_sequence() -> None:
    isolated = IsolatedOrbbecBackend.__new__(IsolatedOrbbecBackend)
    isolated._config = OrbbecConfig()
    isolated._latest_frame = None
    isolated._latest_seq = -1
    isolated._closed = False
    isolated._frame_queue = queue.Queue()
    isolated._status_queue = queue.Queue()
    isolated._process = mock.Mock()
    isolated._process.is_alive.return_value = True

    older = OrbbeFrame(
        color=np.zeros((1, 1, 3), dtype=np.uint8),
        depth=None,
        ir=None,
        timestamp_ms=10,
    )
    newest = OrbbeFrame(
        color=np.ones((1, 1, 3), dtype=np.uint8),
        depth=None,
        ir=None,
        timestamp_ms=11,
    )
    isolated._frame_queue.put_nowait((4, older))
    isolated._frame_queue.put_nowait((5, newest))

    frame, seq = isolated.wait_new_frame(3, timeout=0.1)

    assert seq == 5
    assert frame is newest


def test_wait_new_frame_surfaces_worker_error() -> None:
    isolated = IsolatedOrbbecBackend.__new__(IsolatedOrbbecBackend)
    isolated._config = OrbbecConfig()
    isolated._latest_frame = None
    isolated._latest_seq = -1
    isolated._closed = False
    isolated._frame_queue = queue.Queue()
    isolated._status_queue = queue.Queue()
    isolated._status_queue.put_nowait(("error", "camera disconnected"))
    isolated._process = mock.Mock()
    isolated._process.is_alive.return_value = True

    with pytest.raises(RuntimeError, match="camera disconnected"):
        isolated.wait_new_frame(-1, timeout=0.1)
