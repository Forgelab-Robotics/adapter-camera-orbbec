# Python 单帧采集

在项目根目录连接相机并完成权限配置后运行：

```bash
uv run python examples/python_capture_sample/run_capture_sample.py \
  --config config/sensor.example.yaml \
  --all-streams
```

默认输出到 `sample_output/`（该目录仅保留说明文件，不提交采集数据）。
