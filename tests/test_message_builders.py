from __future__ import annotations

import unittest
import threading
from pathlib import Path
from unittest import mock

import numpy as np
from forge_msgs import CompressedImage, Image

PACKAGE_ROOT = Path(__file__).resolve().parents[1]

from forge_devices_orbbec_camera.backend import OrbbeFrame
from forge_devices_orbbec_camera.config import OrbbecConfig
from forge_devices_orbbec_camera import main as orbbec_main
from forge_devices_orbbec_camera import backend_orbbec


class MessageBuilderTests(unittest.TestCase):
    def test_rgb_color_uses_raw_image(self) -> None:
        config = OrbbecConfig()
        frame = OrbbeFrame(
            color=np.zeros((2, 3, 3), dtype=np.uint8),
            depth=None,
            ir=None,
            timestamp_ms=123,
        )

        msg = orbbec_main._frame_to_color_image(frame, config)

        self.assertIsNotNone(msg)
        decoded = Image.from_arrow(msg.to_arrow())
        self.assertEqual(decoded.width, 3)
        self.assertEqual(decoded.height, 2)
        self.assertEqual(decoded.encoding, "rgb8")
        self.assertEqual(decoded.data, frame.color.tobytes())

    def test_jpeg_color_uses_compressed_image(self) -> None:
        config = OrbbecConfig()
        config.color.format = "jpeg"
        config.color.jpeg_quality = 80
        frame = OrbbeFrame(
            color=np.zeros((2, 3, 3), dtype=np.uint8),
            depth=None,
            ir=None,
        )

        msg = orbbec_main._frame_to_color_image(frame, config)

        self.assertIsNotNone(msg)
        decoded = CompressedImage.from_arrow(msg.to_arrow())
        self.assertEqual(decoded.format, "jpeg")
        self.assertGreater(len(decoded.data), 0)

    def test_depth_converts_millimetres_to_32fc1_metres(self) -> None:
        depth = np.array(
            [[0.0, 1.5, 1000.0], [1500.25, 2500.0, 10000.0]],
            dtype=np.float32,
        )
        frame = OrbbeFrame(color=None, depth=depth, ir=None)

        msg = orbbec_main._frame_to_depth_image(frame)

        self.assertIsNotNone(msg)
        decoded = Image.from_arrow(msg.to_arrow())
        self.assertEqual(decoded.width, 3)
        self.assertEqual(decoded.height, 2)
        self.assertEqual(decoded.encoding, "32FC1")
        self.assertEqual(decoded.step, 3 * np.dtype(np.float32).itemsize)
        np.testing.assert_allclose(
            decoded.to_numpy(),
            depth * 0.001,
            rtol=0,
            atol=1e-7,
        )

    def test_uint8_ir_uses_mono8_image(self) -> None:
        ir = np.arange(6, dtype=np.uint8).reshape(2, 3)
        frame = OrbbeFrame(color=None, depth=None, ir=ir)

        msg = orbbec_main._frame_to_ir_image(frame)

        self.assertIsNotNone(msg)
        decoded = Image.from_arrow(msg.to_arrow())
        self.assertEqual(decoded.width, 3)
        self.assertEqual(decoded.height, 2)
        self.assertEqual(decoded.encoding, "mono8")
        self.assertEqual(decoded.data, ir.tobytes())

    def test_uint16_ir_uses_raw_16uc1_image(self) -> None:
        ir = np.arange(6, dtype=np.uint16).reshape(2, 3)
        frame = OrbbeFrame(color=None, depth=None, ir=ir)

        msg = orbbec_main._frame_to_ir_image(frame)

        self.assertIsNotNone(msg)
        decoded = Image.from_arrow(msg.to_arrow())
        self.assertEqual(decoded.width, 3)
        self.assertEqual(decoded.height, 2)
        self.assertEqual(decoded.encoding, "16UC1")
        self.assertEqual(decoded.data, ir.tobytes())

    def test_missing_frames_return_none(self) -> None:
        config = OrbbecConfig()
        frame = OrbbeFrame(color=None, depth=None, ir=None)

        self.assertIsNone(orbbec_main._frame_to_color_image(frame, config))
        self.assertIsNone(orbbec_main._frame_to_depth_image(frame))
        self.assertIsNone(orbbec_main._frame_to_ir_image(frame))


class ConfigTests(unittest.TestCase):
    def test_standard_example_loads(self) -> None:
        config = OrbbecConfig.from_yaml_path(PACKAGE_ROOT / "config" / "sensor.example.yaml")
        self.assertGreater(config.color.width, 0)
        self.assertGreater(config.depth.fps, 0)

    def test_rejects_invalid_device_index_and_stream_size(self) -> None:
        with self.assertRaisesRegex(ValueError, "device_index"):
            OrbbecConfig.from_dict({"device_index": -1})
        with self.assertRaisesRegex(ValueError, "color.width"):
            OrbbecConfig.from_dict({"color": {"data_flow": {"width": 0}}})

    def test_rejects_invalid_prewarm_and_nonfinite_init_timeout(self) -> None:
        for prewarm in (-1, 61):
            with self.subTest(prewarm=prewarm), self.assertRaisesRegex(ValueError, "prewarm_frames"):
                OrbbecConfig.from_dict({"prewarm_frames": prewarm})
        for timeout in (float("inf"), float("-inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaisesRegex(ValueError, "init_timeout_sec"):
                OrbbecConfig.from_dict({"init_timeout_sec": timeout})


class BackendLifecycleTests(unittest.TestCase):
    @staticmethod
    def _loop_backend(config: OrbbecConfig, pipeline: mock.Mock) -> backend_orbbec.OrbbecBackend:
        backend = backend_orbbec.OrbbecBackend.__new__(backend_orbbec.OrbbecBackend)
        backend._config = config
        backend._latest_frame = None
        backend._lock = threading.Lock()
        backend._first_frame_event = threading.Event()
        backend._stop_event = threading.Event()
        backend._stop_event.wait = mock.Mock(return_value=False)
        backend._init_done = threading.Event()
        backend._init_error = None
        backend._terminal_error = None
        backend._depth_sdk_filters = []
        backend._depth_hw_range_ok = False
        backend._create_pipeline = mock.Mock(return_value=pipeline)
        backend._build_ob_config = mock.Mock(return_value=object())
        backend._configure_alignment = mock.Mock(return_value=None)
        backend._apply_and_verify_properties = mock.Mock()
        backend._build_depth_sdk_filters = mock.Mock()
        return backend

    def test_terminal_error_prevents_returning_stale_frame(self) -> None:
        backend = backend_orbbec.OrbbecBackend.__new__(backend_orbbec.OrbbecBackend)
        backend._first_frame_event = threading.Event()
        backend._first_frame_event.set()
        backend._lock = threading.Lock()
        backend._latest_frame = OrbbeFrame(
            color=np.zeros((1, 1, 3), dtype=np.uint8),
            depth=None,
            ir=None,
        )
        backend._terminal_error = "device disconnected"

        with self.assertRaisesRegex(RuntimeError, "device disconnected"):
            backend.capture_frame()

    def test_initialization_timeout_stops_and_joins_thread(self) -> None:
        fake_thread = mock.Mock()
        fake_thread.is_alive.return_value = True
        config = OrbbecConfig(init_timeout_sec=0.001)

        with mock.patch.object(backend_orbbec.threading, "Thread", return_value=fake_thread):
            with self.assertRaisesRegex(RuntimeError, "初始化超时"):
                backend_orbbec.OrbbecBackend(config)

        fake_thread.start.assert_called_once()
        fake_thread.join.assert_called_once_with(timeout=5.0)

    def test_alignment_modes_are_distinct(self) -> None:
        backend = backend_orbbec.OrbbecBackend.__new__(backend_orbbec.OrbbecBackend)

        class FakeConfig:
            def __init__(self) -> None:
                self.modes: list[object] = []

            def set_align_mode(self, mode: object) -> None:
                self.modes.append(mode)

        sdk_config = FakeConfig()
        backend._config = OrbbecConfig(align_mode="disable")
        self.assertIsNone(backend._configure_alignment(sdk_config))
        self.assertEqual(sdk_config.modes, [backend_orbbec.ob.OBAlignMode.DISABLE])

        sdk_config = FakeConfig()
        backend._config = OrbbecConfig(align_mode="hw")
        self.assertIsNone(backend._configure_alignment(sdk_config))
        self.assertEqual(sdk_config.modes, [backend_orbbec.ob.OBAlignMode.HW_MODE])

        sdk_config = FakeConfig()
        backend._config = OrbbecConfig(align_mode="sw")
        sentinel = object()
        with mock.patch.object(backend_orbbec.ob, "AlignFilter", return_value=sentinel):
            self.assertIs(backend._configure_alignment(sdk_config), sentinel)
        self.assertEqual(sdk_config.modes, [backend_orbbec.ob.OBAlignMode.DISABLE])

    def test_ir_manual_controls_and_laser_enabled_are_applied(self) -> None:
        backend = backend_orbbec.OrbbecBackend.__new__(backend_orbbec.OrbbecBackend)
        backend._config = OrbbecConfig()
        backend._config.ir.auto_exposure = False
        backend._config.ir.exposure_us = 1234
        backend._config.ir.gain = 2000
        backend._config.laser.enabled = False
        backend._depth_hw_range_ok = False
        calls: list[tuple[str, object]] = []
        backend._set_and_verify_bool = lambda _device, _prop, value, name: calls.append((name, value))
        backend._set_and_verify_int = lambda _device, _prop, value, name: calls.append((name, value))
        backend._set_prop_bool = lambda *_args: None
        backend._set_prop_int = lambda *_args: None

        pipeline = mock.Mock()
        backend._apply_and_verify_properties(pipeline)

        self.assertIn(("ir.exposure_us", 1234), calls)
        self.assertIn(("ir.gain", 2000), calls)
        self.assertIn(("laser.enabled", False), calls)

    def test_repeated_wait_errors_terminate_capture(self) -> None:
        pipeline = mock.Mock()
        pipeline.wait_for_frames.side_effect = RuntimeError("temporary SDK failure")
        backend = self._loop_backend(OrbbecConfig(), pipeline)

        backend._capture_loop()

        self.assertEqual(
            pipeline.wait_for_frames.call_count,
            backend_orbbec._MAX_CONSECUTIVE_WAIT_ERRORS,
        )
        self.assertIn("连续 3 次", backend._terminal_error)
        self.assertIsNone(backend._latest_frame)

    def test_prewarm_frames_are_discarded(self) -> None:
        pipeline = mock.Mock()
        pipeline.wait_for_frames.return_value = object()
        config = OrbbecConfig(prewarm_frames=2)
        backend = self._loop_backend(config, pipeline)
        expected = OrbbeFrame(color=None, depth=None, ir=None)

        def convert(_frameset: object) -> OrbbeFrame:
            backend._stop_event.set()
            return expected

        backend._convert_frameset = mock.Mock(side_effect=convert)
        backend._capture_loop()

        self.assertEqual(pipeline.wait_for_frames.call_count, 3)
        backend._convert_frameset.assert_called_once()
        self.assertIs(backend._latest_frame, expected)


class ExampleDataflowTests(unittest.TestCase):
    def test_examples_use_canonical_image_viewer(self) -> None:
        example_dir = PACKAGE_ROOT / "examples" / "orbbec_camera_viewer"
        files = [
            example_dir / "dataflow.yaml",
            example_dir / "dataflow_binary.yaml",
            example_dir / "README.md",
        ]

        for path in files:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("test_viewer.py", text)
                self.assertIn("image_viewer", text)
                self.assertIn("forge_runtime", text)

    def test_packaging_and_permissions_follow_project_policy(self) -> None:
        build_script = (PACKAGE_ROOT / "scripts" / "build_pyinstaller.sh").read_text()
        self.assertIn("uv sync", build_script)
        self.assertIn("--frozen", build_script)
        self.assertIn("--group build", build_script)
        self.assertNotIn("uv pip install", build_script)

        rules = (PACKAGE_ROOT / "scripts" / "udev" / "99-obsensor-libusb.rules").read_text()
        self.assertNotIn('MODE:="0666"', rules)
        self.assertIn('MODE:="0660"', rules)
        self.assertIn('GROUP:="video"', rules)
        self.assertIn('TAG+="uaccess"', rules)

        setup = (PACKAGE_ROOT / "scripts" / "setup.sh").read_text()
        self.assertRegex(setup, r"apt-get install.*\\n.*if !|if ! DEBIAN_FRONTEND")
        self.assertIn("exit 1", setup)

        backend_source = (
            PACKAGE_ROOT / "src" / "forge_devices_orbbec_camera" / "backend_orbbec.py"
        ).read_text()
        self.assertIn("prewarm_remaining -= 1", backend_source)
        self.assertIn("_MAX_CONSECUTIVE_WAIT_ERRORS", backend_source)
        self.assertIn("self._latest_frame = None", backend_source)

    def test_vendor_sdk_import_is_confined_to_backend(self) -> None:
        source_root = PACKAGE_ROOT / "src" / "forge_devices_orbbec_camera"
        importing_files = [
            path.name
            for path in source_root.glob("*.py")
            if any(
                line.strip().startswith(("import pyorbbecsdk", "from pyorbbecsdk"))
                for line in path.read_text(encoding="utf-8").splitlines()
            )
        ]
        self.assertEqual(importing_files, ["backend_orbbec.py"])

    def test_standard_dora_sink_decodes_both_message_types(self) -> None:
        sink_path = PACKAGE_ROOT / "examples" / "dora_sensor_stream" / "test_sink.py"
        namespace: dict[str, object] = {"__name__": "test_sink_module"}
        exec(sink_path.read_text(encoding="utf-8"), namespace)
        decode_message = namespace["decode_message"]

        raw = Image.from_numpy(np.zeros((2, 3), dtype=np.float32), encoding="32FC1")
        self.assertEqual(decode_message(raw.to_arrow()), (3, 2, "32FC1"))

        compressed = CompressedImage.from_numpy(
            np.zeros((2, 3, 3), dtype=np.uint8), format="jpeg"
        )
        self.assertEqual(decode_message(compressed.to_arrow()), (3, 2, "jpeg"))


if __name__ == "__main__":
    unittest.main()
