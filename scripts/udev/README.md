# udev 规则（仓库托管，不依赖 `sdk/`）

`99-obsensor-libusb.rules` 是指向包内唯一权威资源
`src/forge_devices_orbbec_camera/resources/99-obsensor-libusb.rules` 的兼容 symlink。
设备 ID 源自 Orbbec pyorbbecsdk 发布包，但权限已收紧为 `0660`、`video` 组并附加
`uaccess`，不再使用厂商规则中的全局可写 `0666`。

root-owned 可信路径中的 frozen 二进制可运行 `orbbec-camera init-device` 检查并初始化；
源码部署使用
`sudo bash scripts/install_permissions.sh`。[`setup.sh`](../setup.sh) 会从此兼容路径读取并
安装到 `/etc/udev/rules.d/`，根据 `SUDO_USER` 配置 `video` 组，并在有 `apt-get` 时安装
`libusb-1.0-0`；开发头文件 `libusb-1.0-0-dev` 需自行安装。

升级 SDK 大版本时可同步新增设备 ID，但必须保留上述最小权限设置。
