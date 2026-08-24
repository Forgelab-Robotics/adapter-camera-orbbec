# Dora Orbbec 图像与点云联调

本目录提供两条 Dora dataflow：

- `dataflow.yaml`：`sensor_node -> test_sink`，用于无桌面环境下的消息验证；
- `dataflow_viewer.yaml`：相机图像直接进入 `image_viewer`，PointCloud v1 经过
  `point_cloud_renderer.py` 投影成 RGB 预览图后进入同一个 viewer；所有原始消息仍同时
  路由到 `test_sink` 验证。

`test_sink` 使用 `forge_msgs.PointCloudView` 解码点云，并同时解码图像，周期打印各
topic 的计数、尺寸、编码、点数和 RGB 状态。Python renderer 使用固定斜视虚拟相机和
CPU z-buffer；预览画布默认 `640×480`，最多采样 160,000 点、每两帧渲染一次，不修改
原始点云消息。预览仅
转发 `capture_timestamp_ns`；合成视角不会沿用源 optical `frame_id`。

此节点用于调试预览，`--every` 只降低计算频率，不保证严格的 latest-frame 队列语义。
若预览延迟开始增长，应增大 `--every` 或减小 `--max-points`/`--point-size`。

## Viewer 联调

确保 `dora`、`uv` 和 `image_viewer` 均位于 `PATH`。独立安装 viewer：

```bash
git clone https://github.com/Forgelab-Robotics/adapter-viewer-image.git
cd adapter-viewer-image
cargo build --release --locked
install -Dm755 target/release/image_viewer ~/.local/bin/image_viewer
export PATH="$HOME/.local/bin:$PATH"
```

然后进入本目录并运行：

```bash
dora run --uv dataflow_viewer.yaml
```

在完整 framework checkout 中，也可以不安装，直接从本目录运行：

```bash
cargo build --release --locked \
  --manifest-path ../../../../../tools/viewers/image_viewer/Cargo.toml
PATH="../../../../../tools/viewers/image_viewer/target/release:$PATH" \
  dora run --uv dataflow_viewer.yaml
```

连接 Gemini 2 并完成权限配置后，命令会打开 Color、Depth、IR 和 PointCloud Preview
四个窗口，并在终端打印点云统计。按 `Ctrl-C` 停止整个 dataflow。无图形桌面时改用
`dora run --uv dataflow.yaml`。

点云消息采用 optical frame（`+X` 右、`+Y` 下、`+Z` 前）；renderer 仅在显示时将
`Y` 轴翻转为向上，并从斜上方观察。可在 `dataflow_viewer.yaml` 的 renderer `args` 中
增加 `--yaw-deg`、`--pitch-deg`、`--target-z-m`、`--camera-distance-m`、
`--point-size`、`--max-points` 或 `--every` 调整预览。

两个 dataflow 都将 `point_cloud` 路由到测试 sink。此硬件验证示例在
`sensor_node.yaml` 中设置 `align_mode: sw`、`frame_sync: true`，并启用
`colorize: true` 的点云，因此输出 Forge PointCloud v1 XYZRGB，坐标位于 Color optical
frame；帧同步用于改善动态场景的 Color/Depth 时间对应。

`tick` 为 33 ms，与 `sensor_node.yaml` 的 30 fps 近似匹配。Depth 消息为
`Image(32FC1)`，像素单位是米；Color 为 `rgb8`（改成 `jpeg` 时为
`CompressedImage`）；IR 为 `mono8`。点云 XYZ 为 organized float32 米制坐标，RGB 为
uint8；无效点的 XYZ 全部为 NaN，RGB 为黑色。点云与同一 FrameSet 的图像共享可选
`capture_timestamp_ns` metadata；将 `point_cloud.frame_id` 设为非 null 可附加可选
`frame_id` metadata。

示例使用 `capture_process: isolated`，帧会经 multiprocessing IPC/pickle 从采集子进程
拷贝到父进程。`PointCloudView` 的本地视图不代表 Dora 链路端到端 zero-copy。
