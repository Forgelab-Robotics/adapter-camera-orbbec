# 参数验证

## 无硬件验证

- `config/sensor.example.yaml` 可加载并通过格式、范围和组合校验。
- Color `format` 决定输出 `Image(rgb8)` 或 `CompressedImage(jpeg)`；设备 MJPG
  且无需软件几何补偿时可透传 bitstream。
- Depth：采集后端一次把 SDK 毫米 `uint16` 转为米 `float32`；Dora 消息直接发
  `32FC1`。snapshot PNG 落盘时再转回毫米 `uint16`。
- 节点内部分采集线程与 encode 线程：采集产出最新 `OrbbeFrame`，encode 预组 Arrow，
  tick 只发送新序号帧（丢旧保新）。
- 分辨率、帧率、对齐模式和设备选择均在创建采集 pipeline 时生效，需重启采集。
- 单测覆盖启动超时停止/join、终止后拒绝旧帧、三种对齐模式分流、手动 IR
  曝光/增益、激光开关、预热帧消费、连续采集异常上限、Depth 米单位透传、JPEG
  透传与 `wait_new_frame` 序号等待。
- `prewarm_frames` 只接受 0..60；`init_timeout_sec` 必须为有限正数。

## 2026-07-13 实机验证

- 设备枚举成功：Gemini 2、USB 3.0，序列号见 `hardware_baseline.md`。
- 默认配置三路快照成功：Color 640×480、Depth/IR 640×400。
- `align_mode=disable/sw/hw` 均启动并产帧；软件/硬件对齐后的 Depth 为
  640×480，关闭对齐时为 640×400。
- `prewarm_frames=2` 已在实机消费后正常产帧。
- IR 手动曝光 3000 μs、增益 1000 均写入并读回一致。
- 激光关闭和功率等级 3 均写入并读回一致；随后恢复默认激光开启配置成功。
- Dora 标准链路短测约 17 秒，三路均稳定达到 510 帧，约 30fps：
  Color `rgb8`、Depth `32FC1` 米、IR `mono8`。
- Dora 收到停止事件后节点和 sink 均正常退出，随后设备可立即重新枚举。
- 深度硬件范围属性在该设备上不可写，节点正确使用软件裁剪兜底。

## 2026-07-13 晚间复核（并行/减拷贝后）

- 枚举：`AY6V16300F8` / Gemini2 / USB3.0。
- 默认 snapshot：Color 640×480、Depth/IR 640×400；Depth PNG 毫米中位数约
  258 mm，有效像素约 78.8%。
- 后端 Depth 已为米：`float32`，非零均值约 0.60 m，最大约 8.5 m。
- `align_mode=disable/sw/hw` 再验通过；disable 深度 640×400，sw/hw 对齐到
  Color 640×480。
- IR 手动曝光/增益与激光关/功率 3/恢复开启再验通过。
- `color.format=jpeg` 时设备 MJPG 透传成功（约 41 KB bitstream）。
- Dora 短测约 12 秒：三路均到 360 帧（约 30fps），编码
  `rgb8`/`32FC1`/`mono8`；STOP 后正常关闭并可立即 `list-devices`。
- 运行中采样：`orbbec-camera` 约 33% 单进程 CPU，`test_sink` 约 12%；未再出现
  整机吃满现象。

动态参数更新当前未实现；修改 YAML 后需重启节点。

## 尚未完成

- 未做数十分钟长稳压测。
- 未覆盖全部可选分辨率、帧率、深度工作模式和滤波组合。
- 未使用已知距离标定物验证绝对深度精度。
- 固件版本需在 Orbbec Viewer 中人工补录。
