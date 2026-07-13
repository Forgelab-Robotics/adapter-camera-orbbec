# 硬件基线

- 验证日期：2026-07-13
- 设备：Orbbec Gemini 2（USB ID `2bc5:0670`）
- 序列号：`AY6V16300F8`；示例配置仍不固化序列号
- 连接：USB 3.0
- 固件：项目已知基线为 1.4.92；当前 Python SDK 未可靠返回实机固件，需在
  Orbbec Viewer 中补录
- SDK：`pyorbbecsdk2 2.1.1`（Python 3.12）
- 系统：Linux 6.17.0-35-generic x86_64，系统需 libusb 运行时和 Orbbec udev 规则
- 数据通道：Color、Depth、IR
- 实测参数：Color 640×480@30 RGB8；Depth/IR 640×400@30
- 输出：Color `rgb8`/JPEG；Depth `32FC1` 米；IR `mono8`
- 实测样本：Color `(480, 640, 3) uint8`；Depth `(400, 640) uint16 mm`；
  IR `(400, 640) uint8`
- 实测深度非零范围 121–9748 mm、中位数 274 mm；该场景只用于链路检查，
  不是标定精度结论
- 官方工具：本机已有 Orbbec Viewer 2.8.6；固件和官方 profile 页面仍需人工补录
- 恢复记录：一次 SDK 枚举出现 `Net pal is not exist`，执行 USB reset 后恢复；
  后续快照、Dora 停止和立即重开均成功

已知风险：USB 带宽、固件支持的 profile、深度工作模式和属性范围会随型号/
固件变化；异常退出可能暂时占用 UVC 句柄。三路 30fps 仍有一定主机开销；节点已
采用采集/编码进程内流水线与 Depth 单次米单位转换。2026-07-13 晚间短测中
`orbbec-camera` 约 33% 单进程 CPU，未再吃满整机；长稳压测仍可按需抽查。
