
"""Render Forge PointCloud v1 messages into an RGB preview image for Dora."""

from __future__ import annotations

import argparse
import math
from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
from dora import Node
from forge_msgs import Image, PointCloudView


@dataclass(frozen=True)
class RendererConfig:
    """Virtual-camera and rasterization settings for the preview."""

    width: int = 640
    height: int = 480
    yaw_deg: float = 25.0
    pitch_deg: float = 15.0
    target_z_m: float = 1.5
    camera_distance_m: float = 3.0
    fov_y_deg: float = 60.0
    near_m: float = 0.05
    far_m: float = 12.0
    max_points: int = 160_000
    point_size: int = 1
    every: int = 2
    output_id: str = "image/point_cloud"

    def __post_init__(self) -> None:
        for name, value in (
            ("yaw_deg", self.yaw_deg),
            ("pitch_deg", self.pitch_deg),
            ("target_z_m", self.target_z_m),
            ("camera_distance_m", self.camera_distance_m),
            ("fov_y_deg", self.fov_y_deg),
            ("near_m", self.near_m),
            ("far_m", self.far_m),
        ):
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        if not 64 <= self.width <= 4096 or not 64 <= self.height <= 4096:
            raise ValueError("preview width and height must be in [64, 4096]")
        if not -89.0 <= self.pitch_deg <= 89.0:
            raise ValueError("pitch_deg must be in [-89, 89]")
        if not 10.0 <= self.fov_y_deg <= 120.0:
            raise ValueError("fov_y_deg must be in [10, 120]")
        if self.camera_distance_m <= 0.0:
            raise ValueError("camera_distance_m must be positive")
        if self.near_m <= 0.0 or self.far_m <= self.near_m:
            raise ValueError("far_m must be greater than positive near_m")
        if self.max_points <= 0:
            raise ValueError("max_points must be positive")
        if not 1 <= self.point_size <= 4:
            raise ValueError("point_size must be in [1, 4]")
        if self.every <= 0:
            raise ValueError("every must be positive")
        if not self.output_id.strip():
            raise ValueError("output_id must not be empty")


def _depth_colors(depth_m: np.ndarray) -> np.ndarray:
    """Create a visible RGB gradient when a cloud has no RGB fields."""
    if depth_m.size == 0:
        return np.empty((0, 3), dtype=np.uint8)
    low = float(np.min(depth_m))
    high = float(np.max(depth_m))
    scale = high - low
    normalized = (
        np.zeros_like(depth_m, dtype=np.float32)
        if scale <= np.finfo(np.float32).eps
        else (depth_m - low) / scale
    )
    red = np.clip(1.5 - np.abs(4.0 * normalized - 3.0), 0.0, 1.0)
    green = np.clip(1.5 - np.abs(4.0 * normalized - 2.0), 0.0, 1.0)
    blue = np.clip(1.5 - np.abs(4.0 * normalized - 1.0), 0.0, 1.0)
    return np.rint(np.column_stack((red, green, blue)) * 255.0).astype(np.uint8)


def _camera_basis(config: RendererConfig) -> tuple[np.ndarray, np.ndarray]:
    """Return virtual-camera position and right/up/forward basis rows."""
    yaw = math.radians(config.yaw_deg)
    pitch = math.radians(config.pitch_deg)
    target = np.array([0.0, 0.0, config.target_z_m], dtype=np.float32)
    distance = config.camera_distance_m
    camera = target + np.array(
        [
            distance * math.sin(yaw) * math.cos(pitch),
            distance * math.sin(pitch),
            -distance * math.cos(yaw) * math.cos(pitch),
        ],
        dtype=np.float32,
    )
    forward = target - camera
    forward /= np.linalg.norm(forward)
    world_up = np.array([0.0, 1.0, 0.0], dtype=np.float32)
    right = np.cross(world_up, forward)
    right /= np.linalg.norm(right)
    up = np.cross(forward, right)
    return camera, np.stack((right, up, forward))


def _rasterize(
    image: np.ndarray,
    u: np.ndarray,
    v: np.ndarray,
    camera_depth: np.ndarray,
    colors: np.ndarray,
    point_size: int,
) -> None:
    """Draw nearest points with a small CPU z-buffer."""
    height, width = image.shape[:2]
    z_buffer = np.full(width * height, np.inf, dtype=np.float32)
    pixels = image.reshape(-1, 3)
    offset_start = -(point_size // 2)

    for dy in range(offset_start, offset_start + point_size):
        shifted_v = v + dy
        valid_v = (shifted_v >= 0) & (shifted_v < height)
        if not np.any(valid_v):
            continue
        for dx in range(offset_start, offset_start + point_size):
            shifted_u = u + dx
            inside = valid_v & (shifted_u >= 0) & (shifted_u < width)
            if not np.any(inside):
                continue
            pixel_indices = shifted_v[inside] * width + shifted_u[inside]
            depths = camera_depth[inside]
            np.minimum.at(z_buffer, pixel_indices, depths)
            nearest = depths <= z_buffer[pixel_indices] + 1e-6
            pixels[pixel_indices[nearest]] = colors[inside][nearest]


def render_point_cloud(
    cloud: PointCloudView, config: RendererConfig
) -> tuple[np.ndarray, int]:
    """Project an optical-frame cloud into a fixed oblique RGB preview."""
    image = np.empty((config.height, config.width, 3), dtype=np.uint8)
    image[:] = (8, 10, 14)
    if cloud.point_count == 0:
        return image, 0

    stride = max(1, math.ceil(cloud.point_count / config.max_points))
    x = cloud.x[::stride]
    y = cloud.y[::stride]
    z = cloud.z[::stride]
    finite = np.isfinite(x) & np.isfinite(y) & np.isfinite(z)
    if not np.any(finite):
        return image, 0

    x = x[finite]
    y = y[finite]
    z = z[finite]
    points = np.column_stack((x, -y, z)).astype(np.float32, copy=False)
    if cloud.has_rgb:
        colors = np.column_stack(
            (
                cloud.red[::stride][finite],
                cloud.green[::stride][finite],
                cloud.blue[::stride][finite],
            )
        )
    else:
        colors = _depth_colors(z)

    camera, basis = _camera_basis(config)
    camera_points = (points - camera) @ basis.T
    camera_depth = camera_points[:, 2]
    visible = (camera_depth >= config.near_m) & (camera_depth <= config.far_m)
    if not np.any(visible):
        return image, 0

    camera_points = camera_points[visible]
    camera_depth = camera_depth[visible]
    colors = colors[visible]
    focal = 0.5 * config.height / math.tan(math.radians(config.fov_y_deg) / 2.0)
    u = np.rint(
        focal * camera_points[:, 0] / camera_depth + (config.width - 1) / 2.0
    ).astype(np.int32)
    v = np.rint(
        (config.height - 1) / 2.0 - focal * camera_points[:, 1] / camera_depth
    ).astype(np.int32)
    on_screen = (u >= 0) & (u < config.width) & (v >= 0) & (v < config.height)
    if not np.any(on_screen):
        return image, 0

    _rasterize(
        image,
        u[on_screen],
        v[on_screen],
        camera_depth[on_screen],
        colors[on_screen],
        config.point_size,
    )
    return image, int(np.count_nonzero(on_screen))


def _parse_args() -> RendererConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--yaw-deg", type=float, default=25.0)
    parser.add_argument("--pitch-deg", type=float, default=15.0)
    parser.add_argument("--target-z-m", type=float, default=1.5)
    parser.add_argument("--camera-distance-m", type=float, default=3.0)
    parser.add_argument("--fov-y-deg", type=float, default=60.0)
    parser.add_argument("--near-m", type=float, default=0.05)
    parser.add_argument("--far-m", type=float, default=12.0)
    parser.add_argument("--max-points", type=int, default=160_000)
    parser.add_argument("--point-size", type=int, default=1)
    parser.add_argument("--every", type=int, default=2)
    parser.add_argument("--output-id", default="image/point_cloud")
    args = parser.parse_args()
    try:
        return RendererConfig(
            width=args.width,
            height=args.height,
            yaw_deg=args.yaw_deg,
            pitch_deg=args.pitch_deg,
            target_z_m=args.target_z_m,
            camera_distance_m=args.camera_distance_m,
            fov_y_deg=args.fov_y_deg,
            near_m=args.near_m,
            far_m=args.far_m,
            max_points=args.max_points,
            point_size=args.point_size,
            every=args.every,
            output_id=args.output_id,
        )
    except ValueError as exc:
        parser.error(str(exc))


def _forge_metadata(value: object) -> dict[str, int]:
    """Forward capture time, but not source frame_id for the synthetic view."""
    if not isinstance(value, Mapping):
        return {}
    metadata: dict[str, int] = {}
    capture_timestamp_ns = value.get("capture_timestamp_ns")
    if isinstance(capture_timestamp_ns, int) and not isinstance(
        capture_timestamp_ns, bool
    ):
        metadata["capture_timestamp_ns"] = capture_timestamp_ns
    return metadata


def main() -> int:
    config = _parse_args()
    received = 0
    rendered = 0
    node = Node()
    for event in node:
        if event["type"] == "STOP":
            break
        if event["type"] != "INPUT" or event["id"] != "point_cloud":
            continue
        received += 1
        if (received - 1) % config.every:
            continue
        cloud = None
        preview = None
        payload = None
        try:
            cloud = PointCloudView.from_arrow(event["value"])
            source_points = cloud.point_count
            preview, projected_points = render_point_cloud(cloud, config)
            payload = Image.from_numpy(preview, encoding="rgb8").to_arrow()
            metadata = _forge_metadata(event.get("metadata"))
            node.send_output(config.output_id, payload, metadata=metadata)
        except Exception as exc:
            print(f"point_cloud_renderer error: {exc}", flush=True)
            continue
        finally:
            cloud = None
            preview = None
            payload = None
        rendered += 1
        if rendered == 1 or rendered % 30 == 0:
            print(
                (
                    "point_cloud_renderer frames={} source_points={} "
                    + "projected_points={} preview={}x{}"
                ).format(
                    rendered,
                    source_points,
                    projected_points,
                    config.width,
                    config.height,
                ),
                flush=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
