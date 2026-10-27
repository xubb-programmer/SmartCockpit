#!/usr/bin/env python3
"""
HTTPS/WSS gateway for the Web client.

It keeps the browser on one secure origin while forwarding internally to the
existing HTTP OTA/API server and WebSocket server.
"""

import argparse
import asyncio
import json
import os
import ssl
from urllib.parse import unquote, urlsplit

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

from https_server import generate_self_signed_cert
from video_stream import VideoStreamBridge
from vision_frame_store import (
    MAX_VISION_FRAME_BYTES,
    VISION_FRAME_PATH,
    validate_vision_frame,
    write_vision_frame_atomic,
)


HOP_BY_HOP_HEADERS = {
    "connection",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

WEBSOCKET_HEADERS = {
    "sec-websocket-key",
    "sec-websocket-version",
    "sec-websocket-extensions",
    "sec-websocket-protocol",
}

def copy_proxy_headers(headers, *, websocket=False):
    skipped = HOP_BY_HOP_HEADERS | (WEBSOCKET_HEADERS if websocket else set())
    return {key: value for key, value in headers.items() if key.lower() not in skipped}


def external_https_origin(request):
    return f"https://{request.host}"


def external_wss_url(request):
    return f"wss://{request.host}/voice_assistant/v1/"


def rewrite_ota_json(request, response_headers, body):
    content_type = response_headers.get("Content-Type", "")
    if "application/json" not in content_type.lower():
        return body, response_headers

    try:
        text = body.decode("utf-8")
        payload = json.loads(text)
    except Exception:
        return body, response_headers

    changed = False
    websocket_config = payload.get("websocket")
    if isinstance(websocket_config, dict) and websocket_config.get("url"):
        websocket_config["url"] = external_wss_url(request)
        changed = True

    firmware = payload.get("firmware")
    if isinstance(firmware, dict):
        firmware_url = firmware.get("url", "")
        if firmware_url:
            parsed = urlsplit(firmware_url)
            if parsed.scheme in ("http", "https") and parsed.path.startswith(
                "/voice_assistant/ota/download/"
            ):
                firmware["url"] = external_https_origin(request) + parsed.path
                changed = True

    if not changed:
        return body, response_headers

    new_body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    new_headers = response_headers.copy()
    new_headers.pop("Content-Length", None)
    return new_body, new_headers


class HttpsGateway:
    def __init__(
        self,
        web_dir,
        http_target,
        ws_target,
        vision_frame_path=VISION_FRAME_PATH,
    ):
        self.web_dir = os.path.realpath(web_dir)
        self.http_target = http_target.rstrip("/")
        self.ws_target = ws_target.rstrip("/")
        self.vision_frame_path = os.path.realpath(vision_frame_path)
        self.vision_frame_upload_count = 0
        self.session = None

    async def start_session(self, app):
        timeout = ClientTimeout(total=None, sock_connect=30, sock_read=None)
        self.session = ClientSession(timeout=timeout)

    async def close_session(self, app):
        if self.session:
            await self.session.close()

    async def proxy_http(self, request):
        target_url = self.http_target + str(request.rel_url)
        headers = copy_proxy_headers(request.headers)
        headers["Host"] = urlsplit(self.http_target).netloc
        headers["X-Forwarded-Proto"] = "https"
        headers["X-Forwarded-Host"] = request.host

        body = await request.read()
        async with self.session.request(
            request.method,
            target_url,
            headers=headers,
            data=body if body else None,
            allow_redirects=False,
        ) as upstream:
            response_body = await upstream.read()
            response_headers = copy_proxy_headers(upstream.headers)
            response_headers.pop("Content-Length", None)
            response_headers.pop("content-length", None)
            response_body, response_headers = rewrite_ota_json(
                request, response_headers, response_body
            )
            return web.Response(
                status=upstream.status,
                headers=response_headers,
                body=response_body,
            )

    async def proxy_websocket(self, request):
        client_ws = web.WebSocketResponse(max_msg_size=0)
        if not client_ws.can_prepare(request).ok:
            return web.Response(status=400, text="Expected WebSocket upgrade\n")

        await client_ws.prepare(request)

        target_url = self.ws_target + str(request.rel_url)
        headers = copy_proxy_headers(request.headers, websocket=True)
        headers["Host"] = urlsplit(self.ws_target).netloc
        headers["X-Forwarded-Proto"] = "https"
        headers["X-Forwarded-Host"] = request.host

        try:
            async with self.session.ws_connect(
                target_url,
                headers=headers,
                max_msg_size=0,
            ) as upstream_ws:
                client_to_upstream = asyncio.create_task(
                    self._relay_websocket(client_ws, upstream_ws)
                )
                upstream_to_client = asyncio.create_task(
                    self._relay_websocket(upstream_ws, client_ws)
                )
                done, pending = await asyncio.wait(
                    {client_to_upstream, upstream_to_client},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    exc = task.exception()
                    if exc:
                        print(f"[WARN] WebSocket relay ended with error: {exc}", flush=True)
                if not upstream_ws.closed:
                    await upstream_ws.close()
        except Exception as exc:
            print(f"[ERROR] WebSocket upstream connection failed: {exc}", flush=True)
        finally:
            if not client_ws.closed:
                await client_ws.close()

        return client_ws

    async def upload_video_frame(self, request):
        content_length = request.content_length
        if content_length is not None and content_length > MAX_VISION_FRAME_BYTES:
            return web.json_response(
                {"error": "图片超过 4 MB 限制"},
                status=413,
            )

        frame_data = await request.read()
        try:
            validate_vision_frame(request.headers.get("Content-Type"), frame_data)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        try:
            frame_stat = await asyncio.to_thread(
                write_vision_frame_atomic,
                frame_data,
                self.vision_frame_path,
            )
        except Exception as exc:
            print(f"[ERROR] 保存视觉画面失败: {exc}", flush=True)
            return web.json_response({"error": "保存视觉画面失败"}, status=500)

        self.vision_frame_upload_count += 1
        if self.vision_frame_upload_count == 1:
            print(
                f"[INFO] 已接收首张视觉画面: {self.vision_frame_path} "
                f"({frame_stat.st_size} bytes)",
                flush=True,
            )
        elif self.vision_frame_upload_count % 30 == 0:
            print(
                f"[INFO] 视觉画面持续更新: count={self.vision_frame_upload_count}, "
                f"size={frame_stat.st_size} bytes",
                flush=True,
            )
        return web.json_response(
            {
                "status": "ok",
                "size": frame_stat.st_size,
                "updated_at": frame_stat.st_mtime,
            },
            headers={"Cache-Control": "no-store"},
        )

    async def _relay_websocket(self, source, target):
        async for msg in source:
            if msg.type == WSMsgType.TEXT:
                await target.send_str(msg.data)
            elif msg.type == WSMsgType.BINARY:
                await target.send_bytes(msg.data)
            elif msg.type == WSMsgType.PING:
                await target.ping()
            elif msg.type == WSMsgType.PONG:
                await target.pong()
            elif msg.type == WSMsgType.CLOSE:
                await target.close()
            elif msg.type == WSMsgType.ERROR:
                exc = source.exception()
                if exc:
                    raise exc
                break

    async def serve_static(self, request):
        raw_path = request.match_info.get("path", "")
        rel_path = unquote(raw_path).replace("\\", "/")
        if not rel_path or rel_path.endswith("/"):
            rel_path = os.path.join(rel_path, "index.html")

        normalized = os.path.normpath(rel_path).lstrip(os.sep)
        file_path = os.path.realpath(os.path.join(self.web_dir, normalized))

        if file_path != self.web_dir and not file_path.startswith(self.web_dir + os.sep):
            raise web.HTTPForbidden(text="forbidden\n")
        if os.path.isdir(file_path):
            file_path = os.path.join(file_path, "index.html")
        if not os.path.isfile(file_path):
            raise web.HTTPNotFound(text="not found\n")

        response = web.FileResponse(file_path)
        response.headers.update(
            {
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            }
        )
        return response


def create_app(web_dir, http_target, ws_target):
    gateway = HttpsGateway(web_dir, http_target, ws_target)
    video_bridge = VideoStreamBridge()
    app = web.Application(client_max_size=16 * 1024 * 1024)
    app.on_startup.append(gateway.start_session)
    app.on_cleanup.append(gateway.close_session)
    app.on_cleanup.append(video_bridge.close)

    app.router.add_get("/video/ws", video_bridge.websocket)
    app.router.add_post("/video/frame", gateway.upload_video_frame)
    app.router.add_route("*", "/voice_assistant/v1", gateway.proxy_websocket)
    app.router.add_route("*", "/voice_assistant/v1/{tail:.*}", gateway.proxy_websocket)
    app.router.add_route("*", "/voice_assistant/{tail:.*}", gateway.proxy_http)
    app.router.add_route("*", "/mcp/{tail:.*}", gateway.proxy_http)
    app.router.add_get("/{path:.*}", gateway.serve_static)
    return app


def main():
    parser = argparse.ArgumentParser(description="HTTPS/WSS gateway for Web client")
    parser.add_argument("--port", type=int, default=18007, help="listen port")
    parser.add_argument("--host", default="0.0.0.0", help="host IP for certificate SAN")
    parser.add_argument("--cert-dir", default=None, help="certificate directory")
    parser.add_argument("--web-dir", default=None, help="static web directory")
    parser.add_argument(
        "--http-target",
        default="http://127.0.0.1:18003",
        help="upstream HTTP OTA/API origin",
    )
    parser.add_argument(
        "--ws-target",
        default="ws://127.0.0.1:18000",
        help="upstream WebSocket origin",
    )
    args = parser.parse_args()

    cert_dir = args.cert_dir or os.path.join(os.path.expanduser("~"), ".ssl")
    cert_file, key_file = generate_self_signed_cert(cert_dir, args.host)
    web_dir = args.web_dir or os.path.dirname(os.path.abspath(__file__))

    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(cert_file, key_file)

    app = create_app(web_dir, args.http_target, args.ws_target)
    print(
        f"[INFO] HTTPS/WSS gateway started: https://{args.host}:{args.port}/",
        flush=True,
    )
    print(f"[INFO]   HTTP upstream: {args.http_target}", flush=True)
    print(f"[INFO]   WS upstream:   {args.ws_target}", flush=True)

    web.run_app(app, host="0.0.0.0", port=args.port, ssl_context=ssl_context)


if __name__ == "__main__":
    main()
