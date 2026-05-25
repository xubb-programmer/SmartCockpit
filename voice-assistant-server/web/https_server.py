#!/usr/bin/env python3
"""
HTTPS 静态文件服务器
用途：为 Web 客户端提供 HTTPS 服务，使远程浏览器能够使用麦克风/摄像头
      （浏览器要求 getUserMedia 必须在安全上下文中调用）

用法：python3 https_server.py [--port 18006] [--cert-dir ~/.ssl] [--host 192.168.0.201]
"""

import argparse
import http.server
import os
import ssl
import subprocess
import sys


def generate_self_signed_cert(cert_dir, host_ip):
    """自动生成自签名 SSL 证书（10 年有效期）"""
    cert_file = os.path.join(cert_dir, "cert.pem")
    key_file = os.path.join(cert_dir, "key.pem")

    if os.path.exists(cert_file) and os.path.exists(key_file):
        print(f"[INFO] SSL 证书已存在: {cert_dir}", flush=True)
        return cert_file, key_file

    os.makedirs(cert_dir, exist_ok=True)
    print(f"[INFO] 正在生成自签名 SSL 证书...", flush=True)

    cmd = [
        "openssl", "req", "-x509",
        "-newkey", "rsa:2048",
        "-keyout", key_file,
        "-out", cert_file,
        "-days", "3650",
        "-nodes",
        "-subj", f"/CN={host_ip}",
        "-addext", f"subjectAltName=IP:{host_ip},IP:127.0.0.1,DNS:localhost",
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True)
        print(f"[INFO] ✓ SSL 证书已生成: {cert_dir}", flush=True)
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] 生成证书失败: {e.stderr.decode()}", file=sys.stderr, flush=True)
        sys.exit(1)
    except FileNotFoundError:
        print("[ERROR] 未找到 openssl 命令，请先安装: sudo apt install openssl", file=sys.stderr, flush=True)
        sys.exit(1)

    return cert_file, key_file


def main():
    parser = argparse.ArgumentParser(description="HTTPS 静态文件服务器")
    parser.add_argument("--port", type=int, default=int(os.environ.get('WEB_PORT', 18006)), help="监听端口 (默认 18006，可通过 WEB_PORT 环境变量覆盖)")
    parser.add_argument("--host", default="0.0.0.0", help="主机 IP (用于证书 SAN)")
    parser.add_argument("--cert-dir", default=None, help="证书目录 (默认 ~/.ssl)")
    parser.add_argument("--web-dir", default=None, help="静态文件目录 (默认为脚本所在目录)")
    args = parser.parse_args()

    # 确定证书目录
    cert_dir = args.cert_dir or os.path.join(os.path.expanduser("~"), ".ssl")

    # 生成或加载证书
    cert_file, key_file = generate_self_signed_cert(cert_dir, args.host)

    # 使用 directory 参数指定静态文件目录（比 os.chdir 更可靠）
    web_dir = args.web_dir or os.path.dirname(os.path.abspath(__file__))

    from functools import partial
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=web_dir)
    httpd = http.server.HTTPServer(("0.0.0.0", args.port), handler)

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_file, key_file)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)

    print(f"[INFO] ✓ HTTPS 服务已启动: https://{args.host}:{args.port}/", flush=True)
    print(f"[INFO]   首次访问时浏览器会提示证书不受信任，点击「高级」→「继续访问」即可", flush=True)
    print(f"[INFO]   接受一次后浏览器会记住，后续访问不再提示", flush=True)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[INFO] HTTPS 服务已停止", flush=True)
        httpd.shutdown()


if __name__ == "__main__":
    main()
