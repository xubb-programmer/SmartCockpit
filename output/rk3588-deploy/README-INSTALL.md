# SmartCockpit RK3588 离线部署说明

## 1. 最终材料

部署目录只包含三个文件：

```text
rk3588-deploy/
├── smartcockpit-rk3588-install.tar.gz  # 完整离线环境、模型和源码
├── install-smartcockpit-rk3588.sh      # RK3588 板端交互安装脚本
└── README-INSTALL.md                   # 本说明
```

不要手工解压 `smartcockpit-rk3588-install.tar.gz`。安装脚本会先校验 SHA-256，
再解压到临时目录，安装完成后自动清理。

本版本离线包 SHA-256：

```text
ba2fe0797ac5f3193884ad02f21659f3db5d04f204bbbbbe9138c83c1da31353
```

2026-10-27 已逐文件核对：包内 RKLLM 服务 30 个文件、语音服务 368 个文件
均与当前源码目录一致，包含现场配置、唤醒词、热词和启动提示音。本次保留已核验的
压缩包，无需重新压缩。详细范围和验证结果见仓库 `docs/部署包与源码一致性核验.md`。

## 2. 适用环境

- RK3588 / AArch64 开发板。
- Debian 或 Ubuntu 的 Rockchip BSP，使用 systemd。
- 目标用户具有 `sudo` 权限。
- 迁移包和安装目录在同一系统盘时，完整安装前建议至少剩余 25 GiB。
- 需要可用的 RKNPU 驱动、声卡和局域网。
- 已验证基线为 RKNPU 驱动 0.9.8、RKLLM Runtime 1.3.0。

安装器会检查架构、SoC、NPU、操作系统、内核、IP、时间、磁盘、声卡、必要命令、
材料完整性和服务端口。驱动版本不同不会直接拒绝安装，但会明确警告并要求安装后实测。

## 3. 从 Windows 复制到新板

在 Windows PowerShell 中执行，按实际 IP 和用户名修改：

```powershell
scp -r D:\project\SmartCockpit\output\rk3588-deploy `
  jw@192.168.0.201:/home/jw/
```

如果新板复用了旧板 IP，SSH 提示主机指纹变化，先确认确实是刚更换的开发板，然后执行：

```powershell
ssh-keygen -R 192.168.0.201
```

重新执行 `scp`，首次连接输入 `yes` 接受新指纹。

## 4. 一键交互安装

SSH 登录 RK3588：

```powershell
ssh jw@192.168.0.201
```

在板端只运行一个脚本：

```bash
cd ~/rk3588-deploy
chmod +x install-smartcockpit-rk3588.sh
./install-smartcockpit-rk3588.sh
```

如果 `df -hT /tmp` 显示 `tmpfs`，请将解压目录放在磁盘上，避免占用模型所需内存：

```bash
mkdir -p "$HOME/rk3588-install-tmp"
TMPDIR="$HOME/rk3588-install-tmp" ./install-smartcockpit-rk3588.sh
```

安装过程中按提示选择：

1. **完整安装/完整替换**：安装 Conda 环境、离线模型、源码和 systemd 服务。
2. **仅更新源码**：保留已有环境、模型、现场配置和下载文件，只替换最新源码。
3. **只检查**：不修改系统，仅检查文件、依赖、服务和端口。

还可以交互选择：

- 目标运行用户名。
- 是否安装车辆控制服务。
- 是否备份并替换已有安装。
- 是否设置开机自启。
- 是否安装后立即启动。
- 是否安装桌面图标，并在桌面登录后自动打开 Web 页面。

检测到旧安装时不会直接删除。确认替换后，旧目录会备份到：

```text
/opt/jw/migration-backup-YYYYmmdd-HHMMSS/
```

## 5. 安装结果

默认安装目录：

```text
/opt/jw/voice-assistant-server
/opt/jw/rknn-server
/opt/jw/models
/opt/jw/downloads
```

默认模型为 `qwen3-4b`，默认不启动视觉模型。主要端口：

| 端口 | 用途 |
|---|---|
| 18000 | 内部 WebSocket |
| 18003 | OTA HTTP |
| 18006 | Web HTTP 和 XP 文件下载 |
| 18007 | Web HTTPS/WSS 安全入口 |
| 18080 | RKLLM API |

远程浏览器访问：

```text
https://RK3588的IP:18007/
```

第一次访问需要接受目标板自动生成的自签名证书。

## 6. 安装日志和故障提示

安装器会把完整日志保存到运行目录下：

```text
install-logs/rk3588-install-YYYYmmdd-HHMMSS.log
```

任何步骤失败时，屏幕会显示失败步骤、命令、退出码和日志路径，并给出对应排查命令。
常用命令：

```bash
sudo systemctl status voice-assistant.service --no-pager -l
sudo journalctl -u voice-assistant.service -b --no-pager -n 150
tail -100 /tmp/rkllm_server.log
tail -100 /tmp/voice_assistant_server.log
tail -100 /tmp/web_https_server.log
/opt/jw/voice-assistant-server/scripts/deploy/logs.sh
```

## 7. 安装后验收

建议依次确认：

1. 浏览器可以打开 `https://板端IP:18007/`。
2. 麦克风输入、ASR 识别、模型回答和扬声器播放正常。
3. 播放音乐后可以说“停止播放”或“下一首”进行打断。
4. 视频拉流、全屏切换和视觉选项符合部署要求。
5. 需要车辆控制时，桅杆撤收、白光、红外、稳台和组合指令正常。
6. 重启 RK3588 后，`voice-assistant.service` 自动启动。

手工查看状态：

```bash
/opt/jw/voice-assistant-server/scripts/deploy/voice-assistant.sh status
systemctl is-enabled voice-assistant.service
systemctl is-active voice-assistant.service
```

## 8. 注意事项

- 三个发布文件必须放在同一目录，不能只复制安装脚本。
- 不要复用其他板子的 `/opt/jw/.ssl`，每块板应生成自己的 HTTPS 证书。
- `/opt/jw/downloads/` 独立于源码更新，更新时不会被覆盖。
- 车辆控制依赖对应硬件和控制程序；无车辆硬件时不要选择安装车辆控制。
- 如果 NPU 驱动和已验证基线不同，以 RKLLM 实际加载和推理测试为准。
