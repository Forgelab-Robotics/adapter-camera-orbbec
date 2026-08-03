"""Process-isolated Orbbec capture backend.

The Dora parent process owns Dora/Zenoh/OTEL file descriptors. A clean child
process owns pyorbbecsdk and the USB pipeline, preventing native ``select()``
calls from sharing Dora's descriptor table.
"""

from __future__ import annotations

import multiprocessing as mp
import queue
import time
from multiprocessing.queues import Queue
from typing import Any

from .backend import CaptureBackend, OrbbeFrame
from .config import OrbbecConfig


FramePacket = tuple[int, OrbbeFrame]


def _put_latest(frame_queue: Queue, packet: FramePacket) -> None:
    """Keep only the newest frames so the parent never builds backlog."""
    while True:
        try:
            frame_queue.put_nowait(packet)
            return
        except queue.Full:
            try:
                frame_queue.get_nowait()
            except queue.Empty:
                return


def _put_status(status_queue: Queue, status: str, message: str = "") -> None:
    try:
        status_queue.put_nowait((status, message))
    except queue.Full:
        pass


def _capture_worker(
    config: OrbbecConfig,
    frame_queue: Queue,
    status_queue: Queue,
    stop_event: Any,
) -> None:
    """Run pyorbbecsdk in a clean child process with no Dora imports."""
    backend: CaptureBackend | None = None
    try:
        from .backend_orbbec import OrbbecBackend

        backend = OrbbecBackend(config)
        _put_status(status_queue, "ready")
        after_seq = -1
        while not stop_event.is_set():
            timeout = max(float(config.init_timeout_sec), 0.5) if after_seq < 0 else 0.5
            frame, seq = backend.wait_new_frame(after_seq, timeout=timeout)
            if seq <= after_seq:
                continue
            after_seq = seq
            _put_latest(frame_queue, (seq, frame))
    except KeyboardInterrupt:
        _put_status(status_queue, "stopped")
    except BaseException as exc:  # native SDK wrappers can raise broad types
        _put_status(status_queue, "error", str(exc))
        raise
    finally:
        if backend is not None:
            backend.close()


class IsolatedOrbbecBackend:
    """CaptureBackend proxying frames from an SDK-only child process."""

    def __init__(self, config: OrbbecConfig) -> None:
        self._config = config
        self._latest_frame: OrbbeFrame | None = None
        self._latest_seq = -1
        self._closed = False
        self._ctx = mp.get_context("spawn")
        self._frame_queue: Queue = self._ctx.Queue(maxsize=2)
        self._status_queue: Queue = self._ctx.Queue(maxsize=8)
        self._stop_event = self._ctx.Event()
        self._process = self._ctx.Process(
            target=_capture_worker,
            args=(
                config,
                self._frame_queue,
                self._status_queue,
                self._stop_event,
            ),
            name="orbbec-sdk-capture",
            daemon=True,
        )
        self._process.start()
        try:
            self._wait_until_ready()
        except BaseException:
            self.close()
            raise

    def capture_frame(self) -> OrbbeFrame:
        self._ensure_open()
        self._raise_if_worker_failed()
        self._drain_status()
        self._drain_frames()
        if self._latest_frame is not None:
            return self._latest_frame
        frame, _ = self.wait_new_frame(-1, timeout=float(self._config.init_timeout_sec))
        return frame

    def wait_new_frame(
        self,
        after_seq: int,
        timeout: float = 2.0,
    ) -> tuple[OrbbeFrame, int]:
        self._ensure_open()
        self._raise_if_worker_failed()
        self._drain_status()
        self._drain_frames()
        if self._latest_frame is not None and self._latest_seq > after_seq:
            return self._latest_frame, self._latest_seq

        effective_timeout = max(float(timeout), 0.0)
        if after_seq < 0 and self._latest_frame is None:
            effective_timeout = max(effective_timeout, float(self._config.init_timeout_sec))
        deadline = time.monotonic() + effective_timeout
        while True:
            self._raise_if_worker_failed()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if self._latest_frame is None:
                    raise RuntimeError("等待 Orbbec 子进程首帧超时")
                return self._latest_frame, self._latest_seq
            try:
                packet = self._frame_queue.get(timeout=min(0.2, remaining))
            except queue.Empty:
                self._drain_status()
                continue
            self._accept_packet(packet)
            self._drain_frames()
            if self._latest_seq > after_seq:
                assert self._latest_frame is not None
                return self._latest_frame, self._latest_seq

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop_event.set()
        if self._process.is_alive():
            self._process.join(timeout=3.0)
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=2.0)
        if self._process.is_alive():
            self._process.kill()
            self._process.join(timeout=1.0)
        self._close_queue(self._frame_queue)
        self._close_queue(self._status_queue)

    def _wait_until_ready(self) -> None:
        timeout = max(float(self._config.init_timeout_sec) + 5.0, 5.0)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._raise_if_worker_failed(allow_starting=True)
            try:
                status, message = self._status_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if status == "ready":
                return
            if status == "error":
                raise RuntimeError(f"Orbbec 子进程初始化失败: {message}")
        raise RuntimeError(f"等待 Orbbec 子进程初始化超时（{timeout:.0f}s）")

    def _drain_frames(self) -> None:
        while True:
            try:
                packet = self._frame_queue.get_nowait()
            except queue.Empty:
                return
            self._accept_packet(packet)

    def _accept_packet(self, packet: FramePacket) -> None:
        seq, frame = packet
        if int(seq) >= self._latest_seq:
            self._latest_seq = int(seq)
            self._latest_frame = frame

    def _drain_status(self) -> None:
        while True:
            try:
                status, message = self._status_queue.get_nowait()
            except queue.Empty:
                return
            if status == "error":
                raise RuntimeError(f"Orbbec 子进程采集失败: {message}")

    def _raise_if_worker_failed(self, *, allow_starting: bool = False) -> None:
        if self._process.is_alive():
            return
        code = self._process.exitcode
        if allow_starting and code is None:
            return
        if code is None:
            return
        self._drain_status()
        raise RuntimeError(f"Orbbec 子进程退出，exitcode={code}")

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Orbbec 隔离采集后端已关闭")

    @staticmethod
    def _close_queue(q: Any) -> None:
        try:
            q.close()
            q.join_thread()
        except Exception:
            pass
