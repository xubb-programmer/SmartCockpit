# RK3588 cat 开发板部署记录

部署日期：2026-10-27。

## 目标环境

| 项目 | 内容 |
|---|---|
| 设备 | LubanCat 5 V2，RK3588，16 GB 内存 |
| 地址与运行用户 | `192.168.0.201`，`cat` |
| 系统 | Debian GNU/Linux 11，AArch64 |
| 内核 | `5.10.160` |
| RKNPU 驱动 | `0.9.8` |
| 安装根目录 | `/opt/jw` |
| Conda 环境 | `/home/cat/miniconda3/envs/voice-assistant-server` |
| 桌面 | 现有 XRDP/XFCE 会话 `:10` |
| 内置音频 | ES8388，PulseAudio 共享输入输出 |

保留该板的现有用户、网络、Qt 安装和远程桌面。系统时间由原来的 2025 年校准到
Windows 当前时间，并写入 RTC。

## 部署材料与配置

使用源码提交 `cfbeabb` 对应的部署归档，SHA-256 为：

```text
ba2fe0797ac5f3193884ad02f21659f3db5d04f204bbbbbe9138c83c1da31353
```

当前语音配置为 `hybrid`，默认模型为 `qwen3-4b`，不启用视觉模型。
保留包内热词、唤醒词和现场配置；浏览器入口与板端唤醒客户端共同使用语音服务。

板端 `/tmp` 是 tmpfs。为避免将约 8 GB 的解压材料放进内存盘，交互安装器新增
对 `TMPDIR` 的支持；本次使用磁盘目录执行：

```bash
cd /home/cat/rk3588-deploy
mkdir -p /home/cat/rk3588-install-tmp
TMPDIR=/home/cat/rk3588-install-tmp ./install-smartcockpit-rk3588.sh
```

安装器默认仍使用 `/tmp`，既有命令保持兼容；自定义目录不存在或不可写时会明确报错。
Debian 普通用户的 `PATH` 可能缺少 `/usr/sbin`；安装器补齐系统命令目录，避免将
已安装的 `runuser`、`usermod` 等误判为缺失。

## 车辆控制无界面配置

该板没有物理 X11 `:0`，只有远程桌面 `:10`。车辆控制服务通过板端 systemd 覆盖配置
使用 Qt `offscreen` 平台，避免等待物理桌面。保留启动脚本、程序、车型库和协议文件检查。

配置位置：

```text
/etc/systemd/system/scout-monitor-1501.service.d/20-headless.conf
```

此覆盖配置属于该板系统配置，不修改归档中的语音或 RKLLM 源码，也不修改现有远程桌面。

启用 `loginctl enable-linger cat`。该用户原有的 `pulseaudio.socket` 和
`pulseaudio.service` 已启用；用户服务可在无人登录桌面时启动，供 `hybrid` 客户端使用。

## 验收结果

- 交互安装器完整执行成功，安装后验收为“失败 0，警告 0”。
- 398 个源码、资源及现场配置文件与部署归档逐字节一致。
- `voice-assistant.service`、`scout-monitor-1501.service` 均已启用且处于 active 状态。
- 9000、18000、18003、18006、18007、18080 端口正常监听。
- Python 为 `3.10.20`，RKLLM Runtime 为 `1.3.0`；模型加载与驱动组合验证通过。
- SherpaHotwordASR 已使用 `modified_beam_search` 和 `config/hotwords_cn.txt` 初始化。
- 提示词缓存已加载，启动预热完成，模型健康检查的 KV 长度为 1334 tokens。
- HTTPS 页面、HTTPS OTA 与 HTTP 下载入口均返回 200；Windows 局域网访问也通过。
- WSS 实际建立连接并返回 `hello`，语音入口模式为 `hybrid`。
- 一次纯文本身份问答完整响应约 3.592 秒，返回“我是Z1车载智能助手，由江苏无线电厂研制”。
  此结果不包含语音识别和语音合成耗时，不作为端到端语音延迟指标。
- 板端唤醒客户端使用 PulseAudio 共享输入输出，桌面图标及登录自动启动入口已安装。
- 本次语音服务从 ExecStart 到就绪约 57 秒，未观察到服务自动重启。

`voice-assistant.service` 使用 `Type=oneshot` 管理后台进程，状态为 `active (exited)`
属于预期行为；实际进程仍在该 systemd 服务的控制组中运行。

启动日志中，推理进程会用相对路径重复调用频率脚本，产生一次 sudo 权限提示。
已核对 systemd 日志：外层启动脚本的绝对路径调用成功，CPU/NPU 已固定频率；
该重复调用提示未阻止模型、预热及推理。本次未修改这段推理源码。

本次未重启整板、未实发车辆动作，也未通过人工听音或现场说话验收麦克风、
扬声器与唤醒词的声学效果。

## 访问与维护

```text
语音 Web：https://192.168.0.201:18007/
XP 下载：http://192.168.0.201:18006/download/
下载文件目录：/opt/jw/downloads/
```

运行用户为 `cat`，服务不依赖安装 SSH 会话持续连接。常用命令：

```bash
sudo systemctl status voice-assistant.service scout-monitor-1501.service --no-pager
sudo systemctl restart voice-assistant.service
/opt/jw/voice-assistant-server/scripts/deploy/logs.sh
```

成功安装日志：

```text
/home/cat/rk3588-deploy/install-logs/rk3588-install-20261027-141411.log
```
