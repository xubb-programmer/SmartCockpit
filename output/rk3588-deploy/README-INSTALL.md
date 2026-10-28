# SmartCockpit RK3588 离线部署说明

## 1. 最终材料

v1.5.2 部署目录保留最新 4 个分卷及安装器、说明：

```text
rk3588-deploy/
├── smartcockpit-rk3588-install.tar.gz.part01
├── smartcockpit-rk3588-install.tar.gz.part02
├── smartcockpit-rk3588-install.tar.gz.part03
├── smartcockpit-rk3588-install.tar.gz.part04
├── install-smartcockpit-rk3588.sh
└── README-INSTALL.md
```

每卷不超过 1,800,000,000 字节，符合 GitHub LFS 单文件限制。复制全部 6 个文件到同一目录后运行安装器，
安装器按顺序合并数据、校验完整归档的 SHA-256 并直接解包，无需手动合并。
Git 检出后先运行 `git lfs pull`，确保分卷是实际文件。

```bash
bash install-smartcockpit-rk3588.sh --verify-package  # 只校验，不安装
bash install-smartcockpit-rk3588.sh                   # 交互安装
```

完整归档 SHA-256：

```text
07eafa4bb1b22131fd9fcc85b90dd2e42785365c629a6b339981fed7f12d575a
```

2026-10-28 重建，归档大小为 6,878,168,357 字节，对应源码提交
`9720ac873f4c940f3eaeb8c07a247fb24e614052`。包内 RKLLM 服务 33 个文件、
语音服务 376 个文件；408 个受控文件按提交内容校验，另保留 1 份默认现场配置快照。
包含 80 毫秒短就绪音、本地 Silero 防回灌、8 秒免唤醒续说、音乐中唤醒停歌和按会话取消。
完整语音回归 163 项通过，唤醒后就绪耗时实测中位数约 196 毫秒。
完整的范围与验证结果见仓库 `docs/部署包与源码一致性核验.md`。

包内 `manifests/source-version.json` 记录源码提交，`source-files.sha256` 记录逐文件校验值。
安装器会保存到 `/opt/jw/release/`，安装后验收也会检查源码文件。已有板的现场配置仍予保留。

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
   板端说“你好小江”，听到短就绪音后开始说话；回复后的就绪音表示可在 8 秒内直接续说，
   下降双音表示窗口结束、需要再次唤醒。就绪提示后仍保留 200 毫秒防回声等待。
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


## 分卷校验

| 分卷 | 字节数 | SHA-256 |
|---|---:|---|
| `smartcockpit-rk3588-install.tar.gz.part01` | 1,800,000,000 | `d0d30f25303f873410025028fd616f69315e18396ca9508870981023d9a8fa76` |
| `smartcockpit-rk3588-install.tar.gz.part02` | 1,800,000,000 | `804a40998ee2448faef7abc27bfb30a72f2b9aeef5ce9f1dfd4e98bd9272d32c` |
| `smartcockpit-rk3588-install.tar.gz.part03` | 1,800,000,000 | `5d0be9c59f1a1a9d2c780883916854ee8c213e39ebb11cefcd9c271e14c6c059` |
| `smartcockpit-rk3588-install.tar.gz.part04` | 1,478,168,357 | `e120303e66a130876920cd0358f1c6886ea2f78a4268d406d01063bfad0d9a19` |
