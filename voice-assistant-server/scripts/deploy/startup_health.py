#!/usr/bin/env python3
"""Read-only startup checks. Never requests inference or sends device commands."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.request


def http_get(url):
    # Local probes must not be sent through the desktop's external HTTP proxy.
    handlers = [urllib.request.ProxyHandler({})]
    if url.startswith("https:"):
        import ssl
        handlers.append(urllib.request.HTTPSHandler(
            context=ssl._create_unverified_context(),
        ))
    opener = urllib.request.build_opener(*handlers)
    with opener.open(url, timeout=2) as response:
        if response.status != 200:
            raise ValueError(f"HTTP {response.status}")
        return response.read(65536)


def process_matches(pid, marker):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        command = (Path("/proc") / str(pid) / "cmdline").read_bytes()
        return marker.encode() in command
    except OSError:
        return False


def headless_error(path, now=None):
    now = time.monotonic() if now is None else now
    try:
        status = json.loads(Path(path).read_text(encoding="utf-8"))
        if not process_matches(status.get("pid"), "headless_voice_client.py"):
            return "唤醒客户端进程不存在或状态已过期"
        age = now - float(status.get("updated_monotonic", 0))
        if not 0 <= age <= 5:
            return "唤醒客户端状态更新超时"
        if not status.get("capture_alive"):
            return "音频采集进程未运行"
        audio_age = now - float(status.get("last_audio_monotonic", 0))
        if not 0 <= audio_age <= 5:
            return "音频采集没有持续返回数据"
        if not status.get("connected"):
            return "唤醒客户端尚未连接语音服务"
        if not status.get("server_ready"):
            return "语音识别或播报组件尚未初始化完成"
    except (OSError, ValueError, TypeError, AttributeError):
        return "尚未收到有效的唤醒客户端状态"
    return None


def check_services(mode, vehicle_enabled=False, status_path=None):
    failures = []
    try:
        model = json.loads(http_get("http://127.0.0.1:18080/health"))
        if model.get("status") != "ok":
            raise ValueError("模型未就绪")
    except (OSError, ValueError, AttributeError) as exc:
        failures.append(("rkllm", f"大模型健康检查失败: {exc}"))
    try:
        if b"Server is running" not in http_get("http://127.0.0.1:18000/"):
            raise ValueError("语音服务响应不符合预期")
    except (OSError, ValueError) as exc:
        failures.append(("voice", f"语音服务响应异常: {exc}"))
    if mode in ("web", "hybrid"):
        errors = []
        for label, url in [
            ("网页", "https://127.0.0.1:18007/"),
            ("接口代理", "https://127.0.0.1:18007/voice_assistant/health"),
        ]:
            try:
                body = http_get(url)
                if not body.strip():
                    raise ValueError("响应为空")
                if label == "接口代理" and json.loads(body).get("status") != "ok":
                    raise ValueError("接口未就绪")
            except (OSError, ValueError, AttributeError) as exc:
                errors.append(f"{label}: {exc}")
        if errors:
            failures.append(("web", "; ".join(errors)))
    if mode in ("wakeup", "hybrid"):
        path = status_path or os.environ.get(
            "VOICE_ASSISTANT_HEADLESS_STATUS_FILE",
            str(Path(tempfile.gettempdir()) / "headless_voice_status.json"),
        )
        error = headless_error(path)
        if error:
            failures.append(("headless", error))
    if vehicle_enabled:
        # Read listening sockets only; never connect to or command the controller.
        try:
            result = subprocess.run(
                ["ss", "-H", "-ltn"], capture_output=True, text=True,
                timeout=2, check=True,
            )
            if not re.search(r":9000\s", result.stdout):
                raise ValueError("9000 端口未监听")
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            failures.append(("vehicle_control", f"车辆控制服务入口不可达: {exc}"))
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("web", "wakeup", "hybrid"), default="web")
    parser.add_argument("--vehicle-enabled", action="store_true")
    args = parser.parse_args()
    for key, reason in check_services(args.mode, args.vehicle_enabled):
        print(key + "\t" + reason.replace("\n", " ").replace("\t", " "))


if __name__ == "__main__":
    main()
