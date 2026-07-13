# Dora Orbbec 数据流

链路为 `sensor_node -> test_sink`。测试 sink 会解码
`forge_msgs.Image`/`CompressedImage`，并打印 topic、累计计数、尺寸、编码和
UTC 接收时间。

连接相机并完成权限配置后，在本目录运行：

```bash
dora run dataflow.yaml
```

`tick` 为 33 ms，与 `sensor_node.yaml` 的 30 fps 近似匹配。Depth 消息为
`Image(32FC1)`，像素单位是米；Color 为 `rgb8`（改成 `jpeg` 时为
`CompressedImage`）；IR 为 `mono8`。
