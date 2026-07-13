# Sample Output

本目录只记录样本约定，不提交实际图像。

- `capture_color.jpg`：Color JPEG
- `capture_depth.png`：用于人工检查的 16 位毫米深度 PNG
- `capture_ir.png`：IR PNG
- Dora `image/depth`：`forge_msgs.Image`，`encoding=32FC1`，float32 米

注意：PNG 是采集示例的落盘格式，Dora Depth 传输格式是 `32FC1` 米，两者不同。
