# Dora Orbbec 数据流

链路为 `sensor_node -> test_sink`。测试 sink 对 `point_cloud` 使用
`forge_msgs.PointCloudView`，对图像使用 `Image`/`CompressedImage`，并打印 topic、
累计计数、点数与 RGB 状态或图像尺寸与编码，以及 UTC 接收时间。

连接相机并完成权限配置后，在本目录运行：

```bash
dora run dataflow.yaml
```

`dataflow.yaml` 将 `point_cloud` 路由到测试 sink。此硬件验证示例在
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
