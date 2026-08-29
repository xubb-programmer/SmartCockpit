#!/usr/bin/env python3
"""
HTTP 静态文件服务器
用途：为 Web 客户端提供 HTTP 服务（无 SSL），适用于 RK3588 本机 localhost 访问。
      如需远程浏览器使用麦克风（getUserMedia），请通过 deploy/voice-assistant.sh 启动 HTTPS/WSS 网关。

用法：python3 http_server.py [--port 18006] [--host 0.0.0.0] [--web-dir /path/to/web]
"""

import argparse
import http.server
import json
import os
from functools import partial
from urllib.parse import urlsplit

from vision_frame_store import (
    MAX_VISION_FRAME_BYTES,
    VISION_FRAME_PATH,
    validate_vision_frame,
    write_vision_frame_atomic,
)


class VoiceAssistantHttpHandler(http.server.SimpleHTTPRequestHandler):
    vision_frame_upload_count = 0

    def do_POST(self):
        if urlsplit(self.path).path != "/video/frame":
            self.send_error(404, "not found")
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(400, {"error": "Content-Length 无效"})
            return
        if content_length > MAX_VISION_FRAME_BYTES:
            self.send_json(413, {"error": "图片超过 4 MB 限制"})
            return

        frame_data = self.rfile.read(content_length) if content_length else b""
        try:
            validate_vision_frame(self.headers.get("Content-Type"), frame_data)
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
            return

        try:
            frame_stat = write_vision_frame_atomic(frame_data, VISION_FRAME_PATH)
        except Exception as exc:
            print(f"[ERROR] 保存视觉画面失败: {exc}", flush=True)
            self.send_json(500, {"error": "保存视觉画面失败"})
            return

        type(self).vision_frame_upload_count += 1
        upload_count = type(self).vision_frame_upload_count
        if upload_count == 1:
            print(
                f"[INFO] 已接收首张视觉画面: {VISION_FRAME_PATH} "
                f"({frame_stat.st_size} bytes)",
                flush=True,
            )
        elif upload_count % 30 == 0:
            print(
                f"[INFO] 视觉画面持续更新: count={upload_count}, "
                f"size={frame_stat.st_size} bytes",
                flush=True,
            )
        self.send_json(
            200,
            {
                "status": "ok",
                "size": frame_stat.st_size,
                "updated_at": frame_stat.st_mtime,
            },
        )

    def send_json(self, status_code, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description="HTTP 静态文件服务器")
    parser.add_argument("--port", type=int, default=int(os.environ.get('WEB_PORT', 18006)), help="监听端口 (默认 18006，可通过 WEB_PORT 环境变量覆盖)")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址 (默认 0.0.0.0)")
    parser.add_argument("--web-dir", default=None, help="静态文件目录 (默认为脚本所在目录)")
    args = parser.parse_args()

    web_dir = args.web_dir or os.path.dirname(os.path.abspath(__file__))
    handler = partial(VoiceAssistantHttpHandler, directory=web_dir)
    httpd = http.server.HTTPServer((args.host, args.port), handler)

    print(f"[INFO] ✓ HTTP 服务已启动: http://{args.host}:{args.port}/", flush=True)
    print(f"[INFO]   局域网访问: http://<本机IP>:{args.port}/", flush=True)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[INFO] HTTP 服务已停止", flush=True)
        httpd.shutdown()


if __name__ == "__main__":
    main()
