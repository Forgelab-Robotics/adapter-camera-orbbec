# Orbbec Camera Viewer（兼容示例）

此目录保留通用 `image_viewer` 联调方式；标准交付链路和可解码测试 sink 位于
`../dora_sensor_stream`。

先从 `forge-tools-image-viewer` 项目构建 `image_viewer`，并把该可执行文件加入
`PATH`。然后从本目录运行源码节点：

```bash
dora run dataflow.yaml
```

本示例只依赖命令名 `image_viewer`，不假设本仓库与 viewer 仓库的相对位置。

先在项目根运行 `bash scripts/build_pyinstaller.sh` 后，可执行：

```bash
dora run dataflow_binary.yaml
```

`orbbec_camera.yaml` 为该 viewer 的详细调参配置。30 fps 对应 33 ms tick；修改 fps
时也需调整 dataflow 定时器。Depth 输入现在是 `32FC1` 米，viewer 必须支持该编码。
