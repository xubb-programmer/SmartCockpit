#!/usr/bin/env python3
"""
HTTP 静态文件服务器
用途：为 Web 客户端提供 HTTP 服务（无 SSL），适用于 RK3588 本机 localhost 访问。
      如需远程浏览器使用麦克风（getUserMedia），请通过 deploy/voice-assistant.sh 启动 HTTPS/WSS 网关。

用法：python3 http_server.py [--port 18006] [--host 0.0.0.0] [--web-dir /path/to/web]
"""

import argparse
import html
import http.server
import json
import os
import re
from functools import partial
from urllib.parse import quote, unquote, urlsplit

from vision_frame_store import (
    MAX_VISION_FRAME_BYTES,
    VISION_FRAME_PATH,
    validate_vision_frame,
    write_vision_frame_atomic,
)


class VoiceAssistantHttpHandler(http.server.SimpleHTTPRequestHandler):
    vision_frame_upload_count = 0

    def __init__(self, *args, directory=None, download_dir=None, **kwargs):
        self.download_dir = os.path.realpath(download_dir) if download_dir else None
        super().__init__(*args, directory=directory, **kwargs)

    def do_GET(self):
        if self._is_download_request():
            self._handle_download(send_body=True)
            return
        super().do_GET()

    def do_HEAD(self):
        if self._is_download_request():
            self._handle_download(send_body=False)
            return
        super().do_HEAD()

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

    def _is_download_request(self):
        path = urlsplit(self.path).path
        return path in ("/download", "/download/") or path.startswith("/download/")

    def _download_files(self):
        if not self.download_dir or not os.path.isdir(self.download_dir):
            return []
        files = []
        with os.scandir(self.download_dir) as entries:
            for entry in entries:
                if entry.name.startswith(".") or not entry.is_file(follow_symlinks=False):
                    continue
                files.append(entry.name)
        return sorted(files, key=str.casefold)

    def _handle_download(self, send_body):
        path = urlsplit(self.path).path
        if path in ("/download", "/download/"):
            files = self._download_files()
            if not files:
                self._send_download_page(
                    "文件下载",
                    "暂无可下载文件，请联系管理员。",
                    [],
                    send_body,
                )
            elif len(files) == 1:
                self._send_download_file(files[0], send_body)
            else:
                self._send_download_page(
                    "文件下载",
                    "请选择需要下载的文件：",
                    files,
                    send_body,
                )
            return

        raw_name = path[len("/download/") :]
        name = unquote(raw_name)
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            self.send_error(404, "file not found")
            return
        self._send_download_file(name, send_body)

    def _send_download_page(self, title, message, files, send_body):
        items = "".join(
            '<li><a href="/download/{url}">{name}</a></li>'.format(
                url=quote(name, safe=""),
                name=html.escape(name),
            )
            for name in files
        )
        list_html = f"<ul>{items}</ul>" if items else ""
        body = (
            "<!DOCTYPE html><html><head>"
            '<meta http-equiv="Content-Type" content="text/html; charset=utf-8">'
            f"<title>{html.escape(title)}</title></head>"
            '<body style="font-family:Arial,sans-serif;margin:30px">'
            f"<h2>{html.escape(title)}</h2><p>{html.escape(message)}</p>"
            f"{list_html}</body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header(
            "Cache-Control",
            "no-store, no-cache, must-revalidate, max-age=0",
        )
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def _resolve_download_file(self, name):
        if not self.download_dir:
            return None
        file_path = os.path.realpath(os.path.join(self.download_dir, name))
        if not file_path.startswith(self.download_dir + os.sep):
            return None
        if not os.path.isfile(file_path) or os.path.islink(file_path):
            return None
        return file_path

    @staticmethod
    def _parse_range(value, file_size):
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", (value or "").strip())
        if not match or (not match.group(1) and not match.group(2)) or file_size <= 0:
            return None

        start_text, end_text = match.groups()
        if start_text:
            start = int(start_text)
            end = int(end_text) if end_text else file_size - 1
            if start >= file_size or end < start:
                return None
            return start, min(end, file_size - 1)

        suffix_length = int(end_text)
        if suffix_length <= 0:
            return None
        suffix_length = min(suffix_length, file_size)
        return file_size - suffix_length, file_size - 1

    @staticmethod
    def _download_filename(name):
        try:
            name.encode("ascii")
            safe_name = name
        except UnicodeEncodeError:
            extension = os.path.splitext(name)[1]
            safe_name = "download" + extension if extension.isascii() else "download.bin"
        return safe_name.replace("\\", "_").replace('"', "_")

    def _send_download_file(self, name, send_body):
        file_path = self._resolve_download_file(name)
        if not file_path:
            self.send_error(404, "file not found")
            return

        file_stat = os.stat(file_path)
        file_size = file_stat.st_size
        range_value = self.headers.get("Range")
        byte_range = self._parse_range(range_value, file_size) if range_value else None
        if range_value and byte_range is None:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{file_size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if byte_range:
            start, end = byte_range
            status = 206
        else:
            start, end = 0, max(file_size - 1, -1)
            status = 200
        content_length = max(0, end - start + 1)

        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{self._download_filename(name)}"',
        )
        self.send_header("Content-Length", str(content_length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Last-Modified", self.date_time_string(file_stat.st_mtime))
        if byte_range:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.end_headers()

        if not send_body:
            return

        print(
            f"[INFO] 开始下载: {name}, client={self.client_address[0]}, "
            f"bytes={start}-{end}",
            flush=True,
        )
        remaining = content_length
        try:
            with open(file_path, "rb") as source:
                source.seek(start)
                while remaining > 0:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            print(f"[INFO] 下载完成: {name}", flush=True)
        except (BrokenPipeError, ConnectionResetError):
            print(f"[WARN] 下载连接中断: {name}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="HTTP 静态文件服务器")
    parser.add_argument("--port", type=int, default=int(os.environ.get('WEB_PORT', 18006)), help="监听端口 (默认 18006，可通过 WEB_PORT 环境变量覆盖)")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址 (默认 0.0.0.0)")
    parser.add_argument("--web-dir", default=None, help="静态文件目录 (默认为脚本所在目录)")
    parser.add_argument(
        "--download-dir",
        default=os.environ.get("DOWNLOAD_DIR", "/opt/jw/downloads"),
        help="固定下载目录 (默认 /opt/jw/downloads)",
    )
    args = parser.parse_args()

    web_dir = args.web_dir or os.path.dirname(os.path.abspath(__file__))
    os.makedirs(args.download_dir, exist_ok=True)
    handler = partial(
        VoiceAssistantHttpHandler,
        directory=web_dir,
        download_dir=args.download_dir,
    )
    httpd = http.server.ThreadingHTTPServer((args.host, args.port), handler)

    print(f"[INFO] ✓ HTTP 服务已启动: http://{args.host}:{args.port}/", flush=True)
    print(f"[INFO]   局域网访问: http://<本机IP>:{args.port}/", flush=True)
    print(f"[INFO]   文件下载: http://<本机IP>:{args.port}/download/", flush=True)
    print(f"[INFO]   下载目录: {os.path.realpath(args.download_dir)}", flush=True)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[INFO] HTTP 服务已停止", flush=True)
        httpd.shutdown()


if __name__ == "__main__":
    main()
