#!/usr/bin/env python3
"""
HTTP 静态文件服务器
用途：为 Web 客户端提供 HTTP 服务（无 SSL），适用于 RK3588 本机 localhost 访问。
      如需远程浏览器使用麦克风（getUserMedia），请通过 deploy/start.sh 启动 HTTPS/WSS 网关。

用法：python3 http_server.py [--port 18006] [--host 0.0.0.0] [--web-dir /path/to/web]
"""

import argparse
import http.server
import os
from functools import partial


def main():
    parser = argparse.ArgumentParser(description="HTTP 静态文件服务器")
    parser.add_argument("--port", type=int, default=int(os.environ.get('WEB_PORT', 18006)), help="监听端口 (默认 18006，可通过 WEB_PORT 环境变量覆盖)")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址 (默认 0.0.0.0)")
    parser.add_argument("--web-dir", default=None, help="静态文件目录 (默认为脚本所在目录)")
    args = parser.parse_args()

    web_dir = args.web_dir or os.path.dirname(os.path.abspath(__file__))
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=web_dir)
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
