from __future__ import annotations

import gc
import threading
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import mock

import numpy as np
from forge_msgs import CompressedImage, Image, PointCloudView

PACKAGE_ROOT = Path(__file__).resolve().parents[1]

from forge_devices_orbbec_camera import __version__, backend_orbbec
from forge_devices_orbbec_camera import main as orbbec_main
from forge_devices_orbbec_camera import snapshot as orbbec_snapshot
from forge_devices_orbbec_camera.backend import OrbbeFrame, OrbbecPointCloud
from forge_devices_orbbec_camera.config import OrbbecConfig


class MessageBuilderTests(unittest.TestCase):
    def test_version_matches_project_metadata(self) -> None:
        import tomllib

        metadata = tomllib.loads((PACKAGE_ROOT / "pyproject.toml").read_text())
        self.assertEqual(__version__, metadata["project"]["version"])

    def test_source_license_command_prints_apache_license(self) -> None:
        with mock.patch("builtins.print") as output:
            self.assertEqual(orbbec_main._print_licenses(), 0)
        self.assertIn("Apache License", output.call_args.args[0])

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

    def test_legacy_positional_constructor_keeps_color_jpeg_position(self) -> None:
        payload = b"\xff\xd8legacy-jpeg\xff\xd9"

        frame = OrbbeFrame(None, None, None, 123, payload)

        self.assertEqual(frame.timestamp_ms, 123)
        self.assertEqual(frame.color_jpeg, payload)
        self.assertIsNone(frame.capture_timestamp_ns)
        self.assertIsNone(frame.point_cloud)

    def test_jpeg_passthrough_prefers_color_jpeg_bytes(self) -> None:
        config = OrbbecConfig()
        config.color.format = "jpeg"
        payload = b"\xff\xd8fake-jpeg\xff\xd9"
        frame = OrbbeFrame(color=None, depth=None, ir=None, color_jpeg=payload)

        msg = orbbec_main._frame_to_color_image(frame, config)

        self.assertIsInstance(msg, CompressedImage)
        self.assertEqual(msg.format, "jpeg")
        self.assertEqual(msg.data, payload)

    def test_depth_metres_pass_through_as_32fc1(self) -> None:
        depth = np.array(
            [[0.0, 0.0015, 1.0], [1.50025, 2.5, 10.0]],
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
        np.testing.assert_allclose(decoded.to_numpy(), depth, rtol=0, atol=1e-7)

    def test_encode_frame_builds_arrow_outputs(self) -> None:
        config = OrbbecConfig()
        frame = OrbbeFrame(
            color=np.zeros((2, 3, 3), dtype=np.uint8),
            depth=np.ones((2, 3), dtype=np.float32),
            ir=np.arange(6, dtype=np.uint8).reshape(2, 3),
            timestamp_ms=42,
            capture_timestamp_ns=1_234_567_890,
        )

        encoded = orbbec_main._encode_frame(frame, config, seq=7)

        self.assertEqual(encoded.seq, 7)
        self.assertEqual(encoded.timestamp_ms, 42)
        self.assertEqual(encoded.capture_timestamp_ns, 1_234_567_890)
        self.assertIsNotNone(encoded.color)
        self.assertIsNotNone(encoded.depth)
        self.assertIsNotNone(encoded.ir)

    def test_send_output_uses_same_optional_capture_metadata(self) -> None:
        node = mock.Mock()
        payloads = [mock.sentinel.color, mock.sentinel.depth, mock.sentinel.ir]

        for output_id, payload in zip(("color", "depth", "ir"), payloads, strict=True):
            orbbec_main._send_output(node, output_id, payload, 1_234_567_890)

        self.assertEqual(node.send_output.call_count, 3)
        for call in node.send_output.call_args_list:
            self.assertEqual(call.kwargs["metadata"], {"capture_timestamp_ns": 1_234_567_890})

    def test_send_output_omits_missing_capture_timestamp(self) -> None:
        node = mock.Mock()

        orbbec_main._send_output(node, "color", mock.sentinel.payload, None)

        self.assertEqual(node.send_output.call_args.kwargs["metadata"], {})

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
        self.assertIsNone(orbbec_main._frame_to_point_cloud(frame))


class PointCloudMessageTests(unittest.TestCase):
    @staticmethod
    def _assert_read_only_contiguous(*arrays: np.ndarray) -> None:
        for array in arrays:
            if array.dtype == np.dtype(np.float32):
                expected_dtype = np.dtype(np.float32)
            else:
                expected_dtype = np.dtype(np.uint8)
            assert array.dtype == expected_dtype
            assert array.flags.c_contiguous
            assert not array.flags.writeable

    def test_point_cloud_is_frozen_and_is_the_last_frame_field(self) -> None:
        cloud = backend_orbbec._point_cloud_from_sdk_buffer(
            np.array([[1000.0, 2000.0, 3000.0]], dtype=np.float32),
            np.ones((1, 1), dtype=np.float32),
            width=1,
            height=1,
            position_value_scale=1.0,
            colorized=False,
        )

        self.assertIsInstance(cloud, OrbbecPointCloud)
        with self.assertRaises(FrozenInstanceError):
            setattr(cloud, "width", 2)

        frame = OrbbeFrame(None, None, None, 123, b"jpeg", 456, cloud)
        self.assertEqual(frame.timestamp_ms, 123)
        self.assertEqual(frame.color_jpeg, b"jpeg")
        self.assertEqual(frame.capture_timestamp_ns, 456)
        self.assertIs(frame.point_cloud, cloud)

    def test_xyz_buffer_scales_to_metres_masks_invalid_and_detaches(self) -> None:
        sdk_buffer = np.array(
            [
                [500.0, 1000.0, 1500.0],
                [2000.0, 2500.0, 3000.0],
                [np.inf, 0.0, 1000.0],
                [-500.0, -1000.0, 250.0],
            ],
            dtype=np.float32,
        )
        depth = np.array([[1.0, 0.0], [1.0, 0.5]], dtype=np.float32)

        cloud = backend_orbbec._point_cloud_from_sdk_buffer(
            sdk_buffer,
            depth,
            width=2,
            height=2,
            position_value_scale=2.0,
            colorized=False,
        )

        self.assertEqual((cloud.width, cloud.height), (2, 2))
        self.assertFalse(cloud.is_dense)
        self.assertIsNone(cloud.rgb)
        np.testing.assert_allclose(
            cloud.x,
            [[1.0, np.nan], [np.nan, -1.0]],
            equal_nan=True,
        )
        np.testing.assert_allclose(
            cloud.y,
            [[2.0, np.nan], [np.nan, -2.0]],
            equal_nan=True,
        )
        np.testing.assert_allclose(
            cloud.z,
            [[3.0, np.nan], [np.nan, 0.5]],
            equal_nan=True,
        )
        self._assert_read_only_contiguous(cloud.x, cloud.y, cloud.z)

        sdk_buffer[:] = -999.0
        self.assertEqual(float(cloud.x[0, 0]), 1.0)
        self.assertEqual(float(cloud.z[1, 1]), 0.5)

    def test_xyzrgb_buffer_rounds_clips_masks_and_detaches(self) -> None:
        sdk_buffer = np.array(
            [
                [1000.0, 2000.0, 3000.0, 1.4, 1.5, 300.1],
                [4000.0, 5000.0, 6000.0, -2.0, np.nan, 254.6],
                [7000.0, 8000.0, 9000.0, 10.0, 20.0, 30.0],
                [np.nan, 11_000.0, 12_000.0, 40.0, 50.0, 60.0],
            ],
            dtype=np.float32,
        )
        depth = np.array([[1.0, 1.0], [0.0, 1.0]], dtype=np.float32)

        cloud = backend_orbbec._point_cloud_from_sdk_buffer(
            sdk_buffer,
            depth,
            width=2,
            height=2,
            position_value_scale=0.5,
            colorized=True,
        )

        self.assertFalse(cloud.is_dense)
        self.assertIsNotNone(cloud.rgb)
        red, green, blue = cloud.rgb or ()
        np.testing.assert_allclose(
            cloud.x,
            [[0.5, 2.0], [np.nan, np.nan]],
            equal_nan=True,
        )
        np.testing.assert_array_equal(red, [[1, 0], [0, 0]])
        np.testing.assert_array_equal(green, [[2, 0], [0, 0]])
        np.testing.assert_array_equal(blue, [[255, 255], [0, 0]])
        self._assert_read_only_contiguous(
            cloud.x,
            cloud.y,
            cloud.z,
            red,
            green,
            blue,
        )

        sdk_buffer[:] = 99.0
        self.assertEqual(float(cloud.x[0, 0]), 0.5)
        self.assertEqual(int(blue[0, 0]), 255)

    def test_point_cloud_buffer_rejects_invalid_dimensions_depth_and_scale(self) -> None:
        valid_data = np.zeros((2, 3), dtype=np.float32)
        valid_depth = np.ones((1, 2), dtype=np.float32)

        for width, height in ((0, 1), (-1, 1), (1, 0), (1, -1)):
            with self.subTest(width=width, height=height), self.assertRaisesRegex(
                ValueError, "dimensions"
            ):
                backend_orbbec._point_cloud_from_sdk_buffer(
                    valid_data,
                    valid_depth,
                    width=width,
                    height=height,
                    position_value_scale=1.0,
                    colorized=False,
                )

        invalid_depths = (
            np.ones((1, 2), dtype=np.float64),
            np.ones(2, dtype=np.float32),
            np.ones((1, 2, 1), dtype=np.float32),
            np.ones((2, 1), dtype=np.float32),
        )
        for depth in invalid_depths:
            with self.subTest(depth_shape=depth.shape, depth_dtype=depth.dtype), self.assertRaises(
                ValueError
            ):
                backend_orbbec._point_cloud_from_sdk_buffer(
                    valid_data,
                    depth,
                    width=2,
                    height=1,
                    position_value_scale=1.0,
                    colorized=False,
                )

        for scale in (0.0, -1.0, np.nan, np.inf, object()):
            with self.subTest(scale=scale), self.assertRaisesRegex(ValueError, "scale"):
                backend_orbbec._point_cloud_from_sdk_buffer(
                    valid_data,
                    valid_depth,
                    width=2,
                    height=1,
                    position_value_scale=scale,
                    colorized=False,
                )

    def test_point_cloud_buffer_rejects_wrong_sizes_and_noncontiguous_data(self) -> None:
        depth = np.ones((1, 2), dtype=np.float32)
        for colorized, component_count in ((False, 3), (True, 6)):
            expected_bytes = 2 * component_count * np.dtype(np.float32).itemsize
            for actual_bytes in (expected_bytes - 1, expected_bytes + 1):
                with self.subTest(
                    colorized=colorized,
                    actual_bytes=actual_bytes,
                ), self.assertRaisesRegex(ValueError, "unexpected size"):
                    backend_orbbec._point_cloud_from_sdk_buffer(
                        bytearray(actual_bytes),
                        depth,
                        width=2,
                        height=1,
                        position_value_scale=1.0,
                        colorized=colorized,
                    )

            noncontiguous = np.zeros(expected_bytes * 2, dtype=np.uint8)[::2]
            self.assertFalse(noncontiguous.flags.c_contiguous)
            with self.subTest(colorized=colorized), self.assertRaisesRegex(
                ValueError, "not contiguous"
            ):
                backend_orbbec._point_cloud_from_sdk_buffer(
                    noncontiguous,
                    depth,
                    width=2,
                    height=1,
                    position_value_scale=1.0,
                    colorized=colorized,
                )

    def test_point_cloud_message_is_zero_copy_for_arrow_and_view(self) -> None:
        cloud = backend_orbbec._point_cloud_from_sdk_buffer(
            np.array(
                [
                    [1000.0, 2000.0, 3000.0, 10.0, 20.0, 30.0],
                    [4000.0, 5000.0, 6000.0, 40.0, 50.0, 60.0],
                ],
                dtype=np.float32,
            ),
            np.ones((1, 2), dtype=np.float32),
            width=2,
            height=1,
            position_value_scale=1.0,
            colorized=True,
        )
        frame = OrbbeFrame(None, None, None, point_cloud=cloud)

        owner = orbbec_main._frame_to_point_cloud(frame)

        self.assertIsNotNone(owner)
        assert owner is not None
        arrow = owner.to_arrow()
        view = PointCloudView.from_arrow(arrow)
        self.assertEqual((view.width, view.height, view.point_count), (2, 1, 2))
        self.assertTrue(view.is_dense)
        self.assertTrue(view.has_rgb)
        cloud_x_address = cloud.x.__array_interface__["data"][0]
        x_column = arrow.column(arrow.schema.get_field_index("x"))
        x_values_buffer = x_column.values.buffers()[1]
        self.assertIsNotNone(x_values_buffer)
        assert x_values_buffer is not None
        self.assertEqual(x_values_buffer.address, cloud_x_address)
        self.assertEqual(view.x.__array_interface__["data"][0], cloud_x_address)

    def test_point_cloud_arrow_keeps_numpy_buffers_alive(self) -> None:
        cloud = backend_orbbec._point_cloud_from_sdk_buffer(
            np.array([[1000.0, 2000.0, 3000.0]], dtype=np.float32),
            np.ones((1, 1), dtype=np.float32),
            width=1,
            height=1,
            position_value_scale=1.0,
            colorized=False,
        )
        frame = OrbbeFrame(None, None, None, point_cloud=cloud)
        owner = orbbec_main._frame_to_point_cloud(frame)
        self.assertIsNotNone(owner)
        assert owner is not None
        arrow = owner.to_arrow()
        x_address = cloud.x.__array_interface__["data"][0]

        del owner, frame, cloud
        gc.collect()

        view = PointCloudView.from_arrow(arrow)
        self.assertEqual(view.x.__array_interface__["data"][0], x_address)
        np.testing.assert_allclose(view.x, [1.0])

    def test_point_cloud_build_failure_does_not_drop_image_outputs(self) -> None:
        config = OrbbecConfig()
        config.point_cloud.enabled = True
        config.ir.enabled = True
        frame = OrbbeFrame(
            color=np.zeros((2, 3, 3), dtype=np.uint8),
            depth=np.ones((2, 3), dtype=np.float32),
            ir=np.zeros((2, 3), dtype=np.uint8),
            point_cloud=mock.sentinel.cloud,
        )

        with mock.patch.object(
            orbbec_main,
            "_frame_to_point_cloud",
            side_effect=ValueError("invalid point cloud"),
        ):
            encoded = orbbec_main._encode_frame(frame, config, seq=9)

        self.assertIsNotNone(encoded.color)
        self.assertIsNotNone(encoded.depth)
        self.assertIsNotNone(encoded.ir)
        self.assertIsNone(encoded.point_cloud)

    def test_point_cloud_send_uses_output_and_metadata_and_isolates_failure(self) -> None:
        config = OrbbecConfig()
        config.output_point_cloud = "cloud/organized"
        config.point_cloud.frame_id = "camera_depth_optical_frame"
        node = mock.Mock()

        self.assertTrue(
            orbbec_main._send_point_cloud_output(
                node,
                mock.sentinel.payload,
                config,
                1_234_567_890,
            )
        )
        node.send_output.assert_called_once_with(
            "cloud/organized",
            mock.sentinel.payload,
            metadata={
                "capture_timestamp_ns": 1_234_567_890,
                "frame_id": "camera_depth_optical_frame",
            },
        )

        node.send_output.side_effect = RuntimeError("output unavailable")
        self.assertFalse(
            orbbec_main._send_point_cloud_output(
                node,
                mock.sentinel.payload,
                config,
                1_234_567_890,
            )
        )

    def test_snapshot_disables_point_cloud_without_mutating_config(self) -> None:
        config = OrbbecConfig()
        config.point_cloud.enabled = True
        backend = mock.Mock()
        backend.capture_frame.side_effect = [
            OrbbeFrame(None, None, None),
            OrbbeFrame(None, None, None),
            OrbbeFrame(
                np.full((2, 2, 3), 255, dtype=np.uint8),
                None,
                None,
            ),
        ]

        with (
            mock.patch.object(
                orbbec_snapshot,
                "create_backend",
                return_value=backend,
            ) as create_backend,
            mock.patch.object(orbbec_snapshot, "_write_frame_color"),
        ):
            result = orbbec_snapshot.run_snapshot(config, "snapshot.jpg")

        self.assertEqual(result, 0)
        self.assertTrue(config.point_cloud.enabled)
        captured_config = create_backend.call_args.args[0]
        self.assertIsNot(captured_config, config)
        self.assertFalse(captured_config.point_cloud.enabled)
        backend.close.assert_called_once_with()


class ConfigTests(unittest.TestCase):
    def test_point_cloud_defaults(self) -> None:
        config = OrbbecConfig()

        self.assertFalse(config.point_cloud.enabled)
        self.assertTrue(config.point_cloud.colorize)
        self.assertIsNone(config.point_cloud.frame_id)
        self.assertEqual(config.output_point_cloud, "point_cloud")

    def test_point_cloud_section_and_output_id_are_strict(self) -> None:
        for value in (False, 0, "", [], "enabled"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "point_cloud 须为映射"
            ):
                OrbbecConfig.from_dict({"point_cloud": value})

        for value in (None, "", "   ", 123, True):
            with self.subTest(output=value), self.assertRaisesRegex(
                ValueError, "output_point_cloud"
            ):
                OrbbecConfig.from_dict({"output_point_cloud": value})

        self.assertEqual(
            OrbbecConfig.from_dict(
                {"output_point_cloud": "  cloud/organized  "}
            ).output_point_cloud,
            "cloud/organized",
        )

    def test_point_cloud_boolean_fields_are_strict(self) -> None:
        for field in ("enabled", "colorize"):
            for value in ("true", "false", 0, 1):
                with self.subTest(field=field, value=value), self.assertRaisesRegex(
                    ValueError, rf"point_cloud\.{field}"
                ):
                    OrbbecConfig.from_dict({"point_cloud": {field: value}})

    def test_point_cloud_frame_id_is_trimmed_and_strict(self) -> None:
        self.assertIsNone(
            OrbbecConfig.from_dict(
                {"point_cloud": {"frame_id": None}}
            ).point_cloud.frame_id
        )
        config = OrbbecConfig.from_dict(
            {"point_cloud": {"frame_id": "  camera_depth_optical_frame  "}}
        )
        self.assertEqual(
            config.point_cloud.frame_id,
            "camera_depth_optical_frame",
        )

        for frame_id in ("", "   ", 123, True):
            with self.subTest(frame_id=frame_id), self.assertRaisesRegex(
                ValueError, r"point_cloud\.frame_id"
            ):
                OrbbecConfig.from_dict(
                    {"point_cloud": {"frame_id": frame_id}}
                )

    def test_point_cloud_requires_depth_and_color_alignment(self) -> None:
        with self.assertRaisesRegex(ValueError, "point_cloud.enabled.*depth.enabled"):
            OrbbecConfig.from_dict(
                {
                    "depth": {"enabled": False},
                    "point_cloud": {"enabled": True, "colorize": False},
                }
            )

        with self.assertRaisesRegex(ValueError, "point_cloud.colorize.*align_mode"):
            OrbbecConfig.from_dict(
                {
                    "align_mode": "disable",
                    "point_cloud": {"enabled": True, "colorize": True},
                }
            )

        for align_mode in ("sw", "hw"):
            with self.subTest(align_mode=align_mode):
                config = OrbbecConfig.from_dict(
                    {
                        "align_mode": align_mode,
                        "point_cloud": {"enabled": True, "colorize": True},
                    }
                )
                self.assertTrue(config.point_cloud.enabled)
                self.assertTrue(config.point_cloud.colorize)

    def test_xyz_point_cloud_allows_disabled_alignment(self) -> None:
        config = OrbbecConfig.from_dict(
            {
                "align_mode": "disable",
                "point_cloud": {
                    "enabled": True,
                    "colorize": False,
                    "frame_id": "depth_frame",
                },
            }
        )

        self.assertTrue(config.point_cloud.enabled)
        self.assertFalse(config.point_cloud.colorize)
        self.assertEqual(config.point_cloud.frame_id, "depth_frame")

    def test_removed_doctor_subcommand_is_rejected(self) -> None:
        with mock.patch.object(orbbec_main.sys, "argv", ["orbbec-camera", "doctor"]):
            with self.assertRaises(SystemExit) as error:
                orbbec_main._parse_args()
        self.assertEqual(error.exception.code, 2)

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

    def test_rejects_string_values_for_boolean_fields(self) -> None:
        invalid_cases = [
            ("frame_sync", {"frame_sync": "false"}),
            (
                "color.mirror",
                {"color": {"control": {"mirror": "true"}}},
            ),
            (
                "color.auto_exposure",
                {"color": {"control": {"auto_exposure": "false"}}},
            ),
            ("depth.enabled", {"depth": {"enabled": "true"}}),
            (
                "depth.noise_removal_filter",
                {"depth": {"noise_removal_filter": "false"}},
            ),
            ("depth.post_filter", {"depth": {"post_filter": "true"}}),
            (
                "depth.edge_vertical_direction",
                {"depth": {"edge_vertical_direction": "false"}},
            ),
            (
                "ir.enabled",
                {"ir": {"data_flow": {"enabled": "false"}}},
            ),
            (
                "ir.auto_exposure",
                {"ir": {"control": {"auto_exposure": "true"}}},
            ),
            ("laser.enabled", {"laser": {"enabled": "false"}}),
            ("laser.ldp_enabled", {"laser": {"ldp_enabled": "true"}}),
        ]

        for field, data in invalid_cases:
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, field.replace(".", r"\.")
            ):
                OrbbecConfig.from_dict(data)

    def test_rejects_unreasonably_large_resource_parameters(self) -> None:
        invalid_cases = [
            ("color.width", {"color": {"data_flow": {"width": 1_000_000}}}),
            ("depth.fps", {"depth": {"fps": 1_000_000}}),
            (
                "ir.width",
                {"ir": {"data_flow": {"width": 8192, "height": 8192}}},
            ),
            ("connect_delay_ms", {"connect_delay_ms": 10**9}),
            ("init_timeout_sec", {"init_timeout_sec": 10**9}),
        ]

        for field, data in invalid_cases:
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, field.replace(".", r"\.")
            ):
                OrbbecConfig.from_dict(data)


class FrameExtractionTests(unittest.TestCase):
    @staticmethod
    def _backend() -> backend_orbbec.OrbbecBackend:
        backend = backend_orbbec.OrbbecBackend.__new__(backend_orbbec.OrbbecBackend)
        backend._config = OrbbecConfig()
        return backend

    @staticmethod
    def _video_frame(fmt: object, data: np.ndarray, *, width: int, height: int) -> mock.Mock:
        frame = mock.Mock()
        frame.get_width.return_value = width
        frame.get_height.return_value = height
        frame.get_format.return_value = fmt
        frame.get_data.return_value = data
        frame.as_video_frame.return_value = frame
        return frame

    def test_color_raw_formats_reject_short_and_long_buffers(self) -> None:
        backend = self._backend()
        width, height = 4, 2
        cases = [
            (backend_orbbec.ob.OBFormat.RGB, width * height * 3),
            (backend_orbbec.ob.OBFormat.BGR, width * height * 3),
            (backend_orbbec.ob.OBFormat.YUYV, width * height * 2),
        ]

        for fmt, expected_bytes in cases:
            for actual_bytes in (expected_bytes - 1, expected_bytes + 1):
                with self.subTest(fmt=fmt, actual_bytes=actual_bytes):
                    frameset = mock.Mock()
                    frameset.get_color_frame.return_value = self._video_frame(
                        fmt,
                        np.zeros(actual_bytes, dtype=np.uint8),
                        width=width,
                        height=height,
                    )
                    with mock.patch.object(backend_orbbec.logger, "warning") as warning:
                        self.assertEqual(backend._extract_color(frameset), (None, None))
                    warning.assert_called()

    def test_ir_raw_formats_reject_short_and_long_buffers(self) -> None:
        backend = self._backend()
        width, height = 3, 2
        cases = [
            (backend_orbbec.ob.OBFormat.Y8, width * height),
            (backend_orbbec.ob.OBFormat.Y16, width * height * 2),
        ]

        for fmt, expected_bytes in cases:
            for actual_bytes in (expected_bytes - 1, expected_bytes + 1):
                with self.subTest(fmt=fmt, actual_bytes=actual_bytes):
                    frameset = mock.Mock()
                    frameset.get_ir_frame.return_value = self._video_frame(
                        fmt,
                        np.zeros(actual_bytes, dtype=np.uint8),
                        width=width,
                        height=height,
                    )
                    with mock.patch.object(backend_orbbec.logger, "warning") as warning:
                        self.assertIsNone(backend._extract_ir(frameset))
                    warning.assert_called()

    def test_installed_sdk_point_cloud_api_smoke(self) -> None:
        processor = backend_orbbec.ob.PointCloudFilter()
        processor.set_color_data_normalization(False)
        processor.set_create_point_format(backend_orbbec.ob.OBFormat.POINT)
        processor.set_create_point_format(backend_orbbec.ob.OBFormat.RGB_POINT)
        self.assertTrue(callable(processor.process))

    def test_point_cloud_processor_is_not_created_when_disabled(self) -> None:
        with mock.patch.object(backend_orbbec.ob, "PointCloudFilter") as factory:
            processor = backend_orbbec._create_point_cloud_processor(False, True)

        self.assertIsNone(processor)
        factory.assert_not_called()

    def test_point_cloud_processor_configures_xyz_and_xyzrgb_formats(self) -> None:
        for colorize, expected_format in (
            (False, backend_orbbec.ob.OBFormat.POINT),
            (True, backend_orbbec.ob.OBFormat.RGB_POINT),
        ):
            processor = mock.Mock()
            with self.subTest(colorize=colorize), mock.patch.object(
                backend_orbbec.ob,
                "PointCloudFilter",
                return_value=processor,
            ) as factory:
                result = backend_orbbec._create_point_cloud_processor(True, colorize)

            self.assertIs(result, processor)
            factory.assert_called_once_with()
            processor.set_color_data_normalization.assert_called_once_with(False)
            processor.set_create_point_format.assert_called_once_with(expected_format)

    def test_point_cloud_processor_setup_failures_are_nonfatal(self) -> None:
        failures = (
            RuntimeError("factory unavailable"),
            mock.Mock(
                set_color_data_normalization=mock.Mock(
                    side_effect=RuntimeError("normalization unsupported")
                )
            ),
        )
        for failure in failures:
            with self.subTest(failure=failure), mock.patch.object(
                backend_orbbec.ob,
                "PointCloudFilter",
                side_effect=failure if isinstance(failure, Exception) else None,
                return_value=None if isinstance(failure, Exception) else failure,
            ):
                self.assertIsNone(
                    backend_orbbec._create_point_cloud_processor(True, True)
                )

    def test_extract_point_cloud_pushes_processed_depth_before_processing(self) -> None:
        backend = self._backend()
        backend._config.point_cloud.colorize = False
        source_depth = mock.sentinel.source_depth
        processed_depth = mock.sentinel.processed_depth
        filtered = mock.Mock()
        filtered.as_depth_frame.return_value = processed_depth
        sdk_filter = mock.Mock()
        sdk_filter.process.return_value = filtered
        backend._depth_sdk_filters = [sdk_filter]

        frameset = mock.Mock()
        frameset.get_depth_frame.return_value = source_depth
        processor = mock.Mock()
        result = mock.Mock()
        points = mock.Mock()
        points.get_data.return_value = np.array(
            [[1000.0, 2000.0, 3000.0]],
            dtype=np.float32,
        )
        points.get_width.return_value = 1
        points.get_height.return_value = 1
        points.get_position_value_scale.return_value = 1.0
        result.as_points_frame.return_value = points
        processor.process.return_value = result
        backend._point_cloud = processor
        manager = mock.Mock()
        manager.attach_mock(frameset.push_frame, "push_frame")
        manager.attach_mock(processor.process, "process")

        actual_processed_depth = backend._process_depth_frame(frameset)
        cloud = backend._extract_point_cloud(
            frameset,
            actual_processed_depth,
            np.ones((1, 1), dtype=np.float32),
        )

        self.assertIs(actual_processed_depth, processed_depth)
        sdk_filter.process.assert_called_once_with(source_depth)
        self.assertEqual(
            manager.mock_calls[:2],
            [
                mock.call.push_frame(processed_depth),
                mock.call.process(frameset),
            ],
        )
        self.assertIsNotNone(cloud)
        assert cloud is not None
        self.assertEqual(float(cloud.x[0, 0]), 1.0)

    def test_point_cloud_conversion_failure_keeps_image_outputs(self) -> None:
        backend = self._backend()
        backend._config.point_cloud.enabled = True
        backend._config.ir.enabled = True
        backend._point_cloud = mock.sentinel.processor
        backend._point_cloud_failure_logged = False
        color = np.zeros((2, 2, 3), dtype=np.uint8)
        depth = np.ones((2, 2), dtype=np.float32)
        ir = np.zeros((2, 2), dtype=np.uint8)
        processed_depth = mock.sentinel.processed_depth
        backend._extract_color = mock.Mock(return_value=(color, None))
        backend._process_depth_frame = mock.Mock(return_value=processed_depth)
        backend._depth_frame_to_metres = mock.Mock(return_value=depth)
        backend._extract_point_cloud = mock.Mock(
            side_effect=RuntimeError("SDK projection failed")
        )
        backend._extract_ir = mock.Mock(return_value=ir)
        color_frame = mock.Mock()
        color_frame.get_timestamp.return_value = 123
        frameset = mock.Mock()
        frameset.get_color_frame.return_value = color_frame

        frame = backend._convert_frameset(frameset)

        self.assertIs(frame.color, color)
        self.assertIs(frame.depth, depth)
        self.assertIs(frame.ir, ir)
        self.assertIsNone(frame.point_cloud)
        backend._process_depth_frame.assert_called_once_with(frameset)
        backend._depth_frame_to_metres.assert_called_once_with(processed_depth)
        backend._extract_point_cloud.assert_called_once_with(
            frameset,
            processed_depth,
            depth,
        )
        backend._extract_ir.assert_called_once_with(frameset)


class BackendLifecycleTests(unittest.TestCase):
    @staticmethod
    def _loop_backend(config: OrbbecConfig, pipeline: mock.Mock) -> backend_orbbec.OrbbecBackend:
        backend = backend_orbbec.OrbbecBackend.__new__(backend_orbbec.OrbbecBackend)
        backend._config = config
        backend._latest_frame = None
        backend._frame_seq = 0
        backend._lock = threading.Lock()
        backend._frame_cond = threading.Condition(backend._lock)
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
        backend._frame_cond = threading.Condition(backend._lock)
        backend._frame_seq = 1
        backend._latest_frame = OrbbeFrame(
            color=np.zeros((1, 1, 3), dtype=np.uint8),
            depth=None,
            ir=None,
        )
        backend._terminal_error = "device disconnected"

        with self.assertRaisesRegex(RuntimeError, "device disconnected"):
            backend.capture_frame()
        with self.assertRaisesRegex(RuntimeError, "device disconnected"):
            backend.wait_new_frame(0, timeout=0.01)

    def test_wait_new_frame_returns_updated_seq(self) -> None:
        backend = backend_orbbec.OrbbecBackend.__new__(backend_orbbec.OrbbecBackend)
        backend._first_frame_event = threading.Event()
        backend._first_frame_event.set()
        backend._lock = threading.Lock()
        backend._frame_cond = threading.Condition(backend._lock)
        backend._frame_seq = 0
        backend._latest_frame = None
        backend._terminal_error = None
        expected = OrbbeFrame(color=None, depth=None, ir=None, timestamp_ms=9)

        def publish() -> None:
            import time

            time.sleep(0.05)
            with backend._frame_cond:
                backend._latest_frame = expected
                backend._frame_seq = 3
                backend._frame_cond.notify_all()

        threading.Thread(target=publish, daemon=True).start()
        frame, seq = backend.wait_new_frame(0, timeout=1.0)
        self.assertIs(frame, expected)
        self.assertEqual(seq, 3)

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
        with mock.patch.object(backend_orbbec.time, "time_ns", return_value=1_234_567_890):
            backend._capture_loop()

        self.assertEqual(pipeline.wait_for_frames.call_count, 3)
        backend._convert_frameset.assert_called_once()
        self.assertIs(backend._latest_frame, expected)
        self.assertEqual(expected.capture_timestamp_ns, 1_234_567_890)


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
                self.assertNotIn("../../../../../forge_runtime", text)

    def test_packaging_and_permissions_follow_project_policy(self) -> None:
        build_script = (PACKAGE_ROOT / "scripts" / "build_pyinstaller.sh").read_text()
        self.assertIn("uv sync", build_script)
        self.assertIn("--frozen", build_script)
        self.assertIn("--group build", build_script)
        self.assertIn("--no-default-groups", build_script)
        self.assertNotIn("uv pip install", build_script)
        self.assertIn("pip-licenses", build_script)
        self.assertIn("THIRD_PARTY_LICENSES.txt", build_script)

        legacy_rules = PACKAGE_ROOT / "scripts" / "udev" / "99-obsensor-libusb.rules"
        canonical_rules = (
            PACKAGE_ROOT
            / "src"
            / "forge_devices_orbbec_camera"
            / "resources"
            / "99-obsensor-libusb.rules"
        )
        self.assertEqual(legacy_rules.resolve(), canonical_rules.resolve())
        rules = canonical_rules.read_text()
        self.assertNotIn('MODE:="0666"', rules)
        self.assertIn('MODE:="0660"', rules)
        self.assertIn('GROUP:="video"', rules)
        self.assertIn('TAG+="uaccess"', rules)

        setup = (PACKAGE_ROOT / "scripts" / "setup.sh").read_text()
        self.assertIn("PATH=/usr/sbin:/usr/bin:/sbin:/bin", setup)
        self.assertNotIn("apt-get", setup)
        self.assertIn("EXPECTED_RULES_SHA256", setup)
        self.assertIn("sha256sum", setup)
        self.assertIn("RUN|PROGRAM|IMPORT", setup)
        self.assertIn("exit 1", setup)

        spec = (PACKAGE_ROOT / "scripts" / "orbbec_camera.spec").read_text()
        self.assertIn("99-obsensor-libusb.rules", spec)
        self.assertIn("forge_devices_orbbec_camera/resources", spec)
        self.assertIn("THIRD_PARTY_LICENSES.txt", spec)
        self.assertIn('"forge_msgs.point_cloud"', spec)
        entry = (PACKAGE_ROOT / "scripts" / "pyinstaller_entry.py").read_text()
        self.assertIn("multiprocessing.freeze_support()", entry)
        project = (PACKAGE_ROOT / "pyproject.toml").read_text()
        self.assertIn('forge_devices_orbbec_camera = ["resources/*.rules"]', project)

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

    def test_standard_dora_example_routes_synchronized_colorized_cloud(self) -> None:
        example_dir = PACKAGE_ROOT / "examples" / "dora_sensor_stream"
        config = OrbbecConfig.from_yaml_path(example_dir / "sensor_node.yaml")
        self.assertEqual(config.align_mode, "sw")
        self.assertTrue(config.frame_sync)
        self.assertTrue(config.point_cloud.enabled)
        self.assertTrue(config.point_cloud.colorize)
        dataflow = (example_dir / "dataflow.yaml").read_text(encoding="utf-8")
        self.assertIn("point_cloud: sensor_node/point_cloud", dataflow)

    def test_standard_dora_sink_decodes_all_message_types(self) -> None:
        sink_path = PACKAGE_ROOT / "examples" / "dora_sensor_stream" / "test_sink.py"
        namespace: dict[str, object] = {"__name__": "test_sink_module"}
        exec(sink_path.read_text(encoding="utf-8"), namespace)
        decode_message = namespace["decode_message"]

        raw = Image.from_numpy(np.zeros((2, 3), dtype=np.float32), encoding="32FC1")
        self.assertEqual(
            decode_message("depth", raw.to_arrow()),
            "type=Image size=3x2 encoding=32FC1",
        )

        compressed = CompressedImage.from_numpy(
            np.zeros((2, 3, 3), dtype=np.uint8), format="jpeg"
        )
        self.assertEqual(
            decode_message("color", compressed.to_arrow()),
            "type=CompressedImage size=3x2 encoding=jpeg",
        )

        cloud = backend_orbbec._point_cloud_from_sdk_buffer(
            np.array([[1000.0, 2000.0, 3000.0]], dtype=np.float32),
            np.ones((1, 1), dtype=np.float32),
            width=1,
            height=1,
            position_value_scale=1.0,
            colorized=False,
        )
        owner = orbbec_main._frame_to_point_cloud(
            OrbbeFrame(None, None, None, point_cloud=cloud)
        )
        self.assertIsNotNone(owner)
        assert owner is not None
        self.assertEqual(
            decode_message("point_cloud", owner.to_arrow()),
            "type=PointCloud size=1x1 point_count=1 is_dense=True has_rgb=False",
        )


if __name__ == "__main__":
    unittest.main()
