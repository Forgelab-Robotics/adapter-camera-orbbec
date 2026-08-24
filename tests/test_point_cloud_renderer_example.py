from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest
from forge_msgs import PointCloudBatch


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "examples"
    / "dora_sensor_stream"
    / "point_cloud_renderer.py"
)
MODULE_NAME = "orbbec_point_cloud_renderer_example"
SPEC = importlib.util.spec_from_file_location(MODULE_NAME, SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
RENDERER = importlib.util.module_from_spec(SPEC)
sys.modules[MODULE_NAME] = RENDERER
SPEC.loader.exec_module(RENDERER)


def _config(**overrides: object) -> object:
    values: dict[str, object] = {
        "width": 100,
        "height": 100,
        "yaw_deg": 0.0,
        "pitch_deg": 0.0,
        "target_z_m": 1.5,
        "camera_distance_m": 3.0,
        "fov_y_deg": 60.0,
        "near_m": 0.05,
        "far_m": 12.0,
        "max_points": 100,
        "point_size": 1,
        "every": 1,
    }
    values.update(overrides)
    return RENDERER.RendererConfig(**values)


def test_renderer_projects_rgb_and_uses_nearest_point() -> None:
    cloud = PointCloudBatch.from_numpy(
        x=np.array([0.0, 0.0], dtype=np.float32),
        y=np.array([0.0, 0.0], dtype=np.float32),
        z=np.array([2.0, 1.0], dtype=np.float32),
        rgb=np.array([[0, 0, 255], [255, 0, 0]], dtype=np.uint8),
        width=2,
        height=1,
    ).view()

    image, projected = RENDERER.render_point_cloud(cloud, _config())

    assert image.shape == (100, 100, 3)
    assert image.dtype == np.uint8
    assert projected == 2
    np.testing.assert_array_equal(image[50, 50], [255, 0, 0])


def test_renderer_filters_invalid_xyz_and_colorizes_cloud_without_rgb() -> None:
    cloud = PointCloudBatch.from_numpy(
        x=np.array([-0.2, 0.2, np.nan], dtype=np.float32),
        y=np.array([0.0, 0.0, np.nan], dtype=np.float32),
        z=np.array([1.0, 2.0, np.nan], dtype=np.float32),
        width=3,
        height=1,
        is_dense=False,
    ).view()

    image, projected = RENDERER.render_point_cloud(cloud, _config())

    assert projected == 2
    background = np.array([8, 10, 14], dtype=np.uint8)
    assert np.any(np.any(image != background, axis=2))


def test_renderer_config_rejects_unsafe_values() -> None:
    defaults = RENDERER.RendererConfig()
    assert (defaults.width, defaults.height) == (640, 480)

    with pytest.raises(ValueError, match="width and height"):
        _config(width=0)
    with pytest.raises(ValueError, match="far_m"):
        _config(near_m=1.0, far_m=1.0)
    with pytest.raises(ValueError, match="point_size"):
        _config(point_size=5)
    with pytest.raises(ValueError, match="every"):
        _config(every=0)
    for name in ("yaw_deg", "target_z_m", "camera_distance_m", "near_m", "far_m"):
        with pytest.raises(ValueError, match=rf"{name} must be finite"):
            _config(**{name: float("nan")})


def test_renderer_forwards_only_forge_metadata() -> None:
    metadata = RENDERER._forge_metadata(
        {
            "capture_timestamp_ns": 1_234_567_890,
            "frame_id": "camera_color_optical_frame",
            "timestamp": object(),
            "other": 42,
        }
    )

    assert metadata == {"capture_timestamp_ns": 1_234_567_890}
    assert RENDERER._forge_metadata({"capture_timestamp_ns": True}) == {}
