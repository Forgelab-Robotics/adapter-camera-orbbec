# forge-devices-orbbec-camera

Orbbec Gemini 2 深度相机的 Python 采集包与 Dora 节点。Python 包使用标准
src layout：`src/forge_devices_orbbec_camera`。

## 支持范围

- Linux + USB；运行时需要 libusb 和 Orbbec udev 规则
- Color：`forge_msgs.Image(rgb8)` 或 `CompressedImage(jpeg)`
- Depth：`forge_msgs.Image(32FC1)`，float32，单位米，零值表示无效深度
- IR：`forge_msgs.Image(mono8)`（兼容后端返回 uint16 时为 `16UC1`）
- 已验证硬件基线、风险和尚未完成项见 [hardware_baseline.md](hardware_baseline.md)

## 安装与环境检查

```bash
uv sync
uv run python scripts/check_environment.py
sudo bash scripts/install_permissions.sh
```

权限脚本安装 Debian/Ubuntu 的 `libusb-1.0-0` 运行时和
`scripts/udev/99-obsensor-libusb.rules`。规则使用 `0660`、`video` 组和
`uaccess`；依赖安装失败时脚本会非零退出。其他发行版需自行安装等价运行时。
执行后重新插拔设备。源码构建 SDK 时才需要 `libusb-1.0-0-dev`。

官方工具验证应先使用 Orbbec Viewer 或 SDK 官方示例确认设备、固件、流 profile
和深度工作模式，再运行本项目。

## 基础命令

```bash
# CLI 帮助（不访问硬件）
uv run orbbec-camera --help

# 列举设备
uv run orbbec-list-devices
uv run orbbec-camera list-devices --json

# 单帧采集
uv run python examples/python_capture_sample/run_capture_sample.py \
  --config config/sensor.example.yaml --all-streams

# Dora 节点
uv run orbbec-camera --config config/sensor.example.yaml
```

Python 独立示例见 `examples/python_list_devices` 和
`examples/python_capture_sample`。Dora 的可消费链路见
`examples/dora_sensor_stream`，其中 sink 会实际解码消息并打印计数、尺寸、编码和
UTC 接收时间。旧的 `examples/orbbec_camera_viewer` 仍保留用于可视化联调。

## 配置

完整示例位于 `config/sensor.example.yaml`。主要字段：

- `device_serial`/`device_index`：设备选择，优先使用 serial
- `color/depth/ir`：流开关、分辨率、帧率、传输格式和控制参数
- `align_mode`：`disable` 保持原始坐标系；`sw` 使用 SDK `AlignFilter`；
  `hw` 使用 SDK Config 硬件对齐，不支持时启动会明确失败
- `frame_sync`：多设备硬件同步
- `prewarm_frames`、`connect_delay_ms`、`init_timeout_sec`：启动行为
- `capture_process`：`isolated` 将 pyorbbecsdk 放入独立子进程，避免与
  Dora/Zenoh 共享文件描述符；Dora 节点推荐使用。`direct` 仅用于独立工具调试
- `output_color`、`output_depth`、`output_ir`：稳定的 Dora topic

当前不支持运行时配置更新。流 profile、设备、对齐和绝大多数控制参数需修改 YAML
后重启节点。参数验证范围与实机清单见
[parameter_validation.md](parameter_validation.md)。

## 输出语义

| topic | 消息 | 编码/单位 |
| --- | --- | --- |
| `image/color` | `Image` 或 `CompressedImage` | `rgb8` 或 JPEG |
| `image/depth` | `Image` | `32FC1`，米 |
| `image/ir` | `Image` | `mono8`，兼容 `16UC1` |

Dora 图像输出会尽力附加用户 metadata `capture_timestamp_ns`：它是采集后端取得
该 FrameSet 时记录的 Unix epoch 纳秒时间，用于关联同一次采集的 Color / Depth /
IR。该字段是可选的最佳估计，不替代 Dora 管理的消息时间戳；缓存、编码和发送过程
不会重新生成它。后端日志中的 `timestamp_ms` 是 SDK 帧时间戳，仅用于诊断。
`align_mode=sw/hw` 时 Depth 会投影到 Color 坐标系；`disable` 时各流保持原始坐标系。

SDK 提供的 Depth 原始像素会结合 `get_depth_scale()` 归一为毫米，公共消息层再除以
1000 转为米。快照 PNG 仍保存 uint16 毫米值，不能与 Dora `32FC1` 直接混用。

## 打包与部署

```bash
bash scripts/build_pyinstaller.sh
```

产物为 `dist/orbbec_camera`。spec 会从已安装的 wheel 收集
`libOrbbecSDK.so*`、Python 扩展和 `extensions/`，运行时 hook 设置 bundle 内动态库
搜索路径。构建脚本使用 `uv.lock`、`[tool.uv.sources]` 和隔离的 `.venv_build`
同步依赖；锁文件过期会直接失败。构建机和目标机必须使用兼容架构与 libc。

## 常见问题

- 未发现设备：检查 USB、运行权限脚本并重新插拔。
- `libusb`/动态库加载失败：运行 `scripts/check_environment.py`，确认系统运行时和
  wheel 架构匹配。
- `uvc_open` 失败或设备忙：确认没有残留采集进程；必要时正常结束旧进程并重新插拔。
- profile 不支持：用官方 Viewer 确认当前型号/固件提供的分辨率、帧率和格式。
- 深度全零：检查工作模式、曝光、有效范围和 `depth_unit`，并用已知距离实测。

不要把个人绝对路径、设备录包、私有 SDK 或密钥写入配置和示例。
