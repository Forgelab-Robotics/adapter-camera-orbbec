from __future__ import annotations

import multiprocessing as mp
import pickle
import queue
from unittest import mock

import numpy as np
import pytest

from forge_devices_orbbec_camera import backend
from forge_devices_orbbec_camera.backend import OrbbeFrame, OrbbecPointCloud
from forge_devices_orbbec_camera.config import OrbbecConfig
from forge_devices_orbbec_camera.isolated_backend import (
    IsolatedOrbbecBackend,
    _capture_worker,
    _put_latest,
)


def _readonly_array(values: object, dtype: np.dtype) -> np.ndarray:
    array = np.array(values, dtype=dtype, order="C", copy=True)
    array.setflags(write=False)
    return array


def _point_cloud_frame() -> OrbbeFrame:
    cloud = OrbbecPointCloud(
        width=2,
        height=2,
        is_dense=True,
        x=_readonly_array([[1.0, 2.0], [3.0, 4.0]], np.dtype(np.float32)),
        y=_readonly_array([[5.0, 6.0], [7.0, 8.0]], np.dtype(np.float32)),
        z=_readonly_array([[9.0, 10.0], [11.0, 12.0]], np.dtype(np.float32)),
        rgb=(
            _readonly_array([[10, 20], [30, 40]], np.dtype(np.uint8)),
            _readonly_array([[50, 60], [70, 80]], np.dtype(np.uint8)),
            _readonly_array([[90, 100], [110, 120]], np.dtype(np.uint8)),
        ),
    )
    return OrbbeFrame(
        color=None,
        depth=np.ones((2, 2), dtype=np.float32),
        ir=None,
        timestamp_ms=123,
        point_cloud=cloud,
    )


def _put_point_cloud_packet(frame_queue: object) -> None:
    frame_queue.put((7, _point_cloud_frame()))  # type: ignore[attr-defined]


def _point_cloud_proxy() -> IsolatedOrbbecBackend:
    isolated = IsolatedOrbbecBackend.__new__(IsolatedOrbbecBackend)
    isolated._latest_frame = None
    isolated._latest_seq = -1
    return isolated


def _assert_frozen_point_cloud(frame: OrbbeFrame) -> None:
    cloud = frame.point_cloud
    assert cloud is not None
    assert cloud.rgb is not None
    for array in (cloud.x, cloud.y, cloud.z):
        assert array.dtype == np.dtype(np.float32)
        assert array.flags.c_contiguous
        assert not array.flags.writeable
    for channel in cloud.rgb:
        assert channel.dtype == np.dtype(np.uint8)
        assert channel.flags.c_contiguous
        assert not channel.flags.writeable


def test_capture_process_config_validation() -> None:
    assert OrbbecConfig.from_dict({}).capture_process == "isolated"
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


def test_capture_worker_uses_init_timeout_for_first_frame() -> None:
    config = OrbbecConfig(init_timeout_sec=3.0)
    expected = OrbbeFrame(color=None, depth=None, ir=None, timestamp_ms=1)
    capture_backend = mock.Mock()
    capture_backend.wait_new_frame.return_value = (expected, 1)
    frame_queue: queue.Queue = queue.Queue(maxsize=2)
    status_queue: queue.Queue = queue.Queue(maxsize=8)
    stop_event = mock.Mock()
    stop_event.is_set.side_effect = [False, True]

    with mock.patch(
        "forge_devices_orbbec_camera.backend_orbbec.OrbbecBackend",
        return_value=capture_backend,
    ):
        _capture_worker(config, frame_queue, status_queue, stop_event)

    capture_backend.wait_new_frame.assert_called_once_with(-1, timeout=3.0)
    capture_backend.close.assert_called_once_with()
    assert status_queue.get_nowait() == ("ready", "")
    seq, frame = frame_queue.get_nowait()
    assert seq == 1
    assert frame is expected


def test_proxy_expands_short_timeout_while_waiting_for_first_frame() -> None:
    isolated = IsolatedOrbbecBackend.__new__(IsolatedOrbbecBackend)
    isolated._config = OrbbecConfig(init_timeout_sec=3.0)
    isolated._latest_frame = None
    isolated._latest_seq = -1
    isolated._closed = False
    isolated._frame_queue = mock.Mock()
    isolated._frame_queue.get_nowait.side_effect = queue.Empty
    isolated._frame_queue.get.side_effect = queue.Empty
    isolated._status_queue = mock.Mock()
    isolated._status_queue.get_nowait.side_effect = queue.Empty
    isolated._process = mock.Mock()
    isolated._process.is_alive.return_value = True

    with mock.patch(
        "forge_devices_orbbec_camera.isolated_backend.time.monotonic",
        side_effect=[0.0, 0.6, 3.1],
    ):
        with pytest.raises(RuntimeError, match="首帧超时"):
            isolated.wait_new_frame(-1, timeout=0.5)

    isolated._frame_queue.get.assert_called_once_with(timeout=0.2)


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


def test_pickled_point_cloud_is_refrozen_when_parent_accepts_packet() -> None:
    packet = pickle.loads(pickle.dumps((7, _point_cloud_frame())))
    received_cloud = packet[1].point_cloud
    assert received_cloud is not None
    assert received_cloud.x.flags.writeable
    assert received_cloud.rgb is not None
    assert all(channel.flags.writeable for channel in received_cloud.rgb)
    isolated = _point_cloud_proxy()

    isolated._accept_packet(packet)

    assert isolated._latest_seq == 7
    assert isolated._latest_frame is packet[1]
    _assert_frozen_point_cloud(isolated._latest_frame)


def test_spawn_queue_point_cloud_is_refrozen_in_parent() -> None:
    context = mp.get_context("spawn")
    frame_queue = context.Queue(maxsize=1)
    process = context.Process(
        target=_put_point_cloud_packet,
        args=(frame_queue,),
    )
    started = False
    try:
        process.start()
        started = True
        packet = frame_queue.get(timeout=15.0)
        process.join(timeout=15.0)
        assert not process.is_alive()
        assert process.exitcode == 0
        received_cloud = packet[1].point_cloud
        assert received_cloud is not None
        assert received_cloud.x.flags.writeable

        isolated = _point_cloud_proxy()
        isolated._accept_packet(packet)

        assert isolated._latest_seq == 7
        assert isolated._latest_frame is packet[1]
        _assert_frozen_point_cloud(isolated._latest_frame)
    finally:
        if started and process.is_alive():
            process.terminate()
            process.join(timeout=5.0)
        frame_queue.close()
        frame_queue.join_thread()


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
