"""RTSP/RTMP to fragmented MP4 WebSocket bridge for browser playback."""

import asyncio
import json
import shutil
from contextlib import suppress
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

SUPPORTED_SCHEMES = {"rtsp", "rtsps", "rtmp", "rtmps"}
MAX_STREAM_SESSIONS = 4


def validate_stream_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("视频流地址无效")
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in SUPPORTED_SCHEMES or not parsed.hostname:
        raise ValueError("仅支持 RTSP 或 RTMP 视频流地址")
    return value.strip()


def masked_stream_url(value):
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    if parsed.username:
        host = f"{parsed.username}:***@{host}"
    return parsed._replace(netloc=host).geturl()


def h264_codec_string(profile, level):
    profile_bytes = {
        "Constrained Baseline": "42E0",
        "Baseline": "4200",
        "Main": "4D00",
        "High": "6400",
    }
    prefix = profile_bytes.get(profile)
    if not prefix:
        return None
    try:
        level_byte = f"{int(level):02X}"
    except (TypeError, ValueError):
        return None
    return f"avc1.{prefix}{level_byte}"


class VideoStreamBridge:
    def __init__(self):
        self.sessions = set()

    async def close(self, app):
        await asyncio.gather(
            *(session.stop() for session in list(self.sessions)),
            return_exceptions=True,
        )

    async def websocket(self, request):
        socket = web.WebSocketResponse(max_msg_size=16 * 1024)
        await socket.prepare(request)
        if len(self.sessions) >= MAX_STREAM_SESSIONS:
            await socket.send_json({"type": "error", "message": "视频播放会话已达到上限"})
            await socket.close()
            return socket
        session = VideoSession(socket)
        self.sessions.add(session)
        try:
            await socket.send_json({"type": "ready"})
            async for message in socket:
                if message.type != WSMsgType.TEXT:
                    continue
                try:
                    payload = json.loads(message.data)
                    if payload.get("type") == "start":
                        await session.start(validate_stream_url(payload.get("url")))
                    elif payload.get("type") == "stop":
                        await session.stop()
                except (ValueError, json.JSONDecodeError) as exc:
                    await socket.send_json({"type": "error", "message": str(exc)})
        finally:
            await session.stop()
            self.sessions.discard(session)
        return socket


class VideoSession:
    def __init__(self, socket):
        self.socket = socket
        self.process = None
        self.tasks = []
        self.stopping = False

    async def start(self, url):
        await self.stop()
        self.stopping = False
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise ValueError("服务器未安装 ffmpeg/ffprobe，无法播放视频")
        probe = await self._probe(url)
        source_codec = probe.get("codec_name", "").lower()
        source_mime_codec = h264_codec_string(probe.get("profile"), probe.get("level"))
        transcode = source_codec != "h264" or not source_mime_codec
        print(
            f"[INFO] 视频流开始: {masked_stream_url(url)} "
            f"({source_codec or '未知编码'} -> {'H.264 转码' if transcode else 'H.264 直通'})",
            flush=True,
        )
        self.process = await asyncio.create_subprocess_exec(
            *self._command(url, transcode),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await self.socket.send_json({
            "type": "stream-started",
            "codec": "avc1.42E01E" if transcode else source_mime_codec,
            "sourceCodec": source_codec or "unknown",
            "transcoding": transcode,
            "width": probe.get("width"),
            "height": probe.get("height"),
        })
        self.tasks = [
            asyncio.create_task(self._relay()),
            asyncio.create_task(self._log_stderr()),
        ]

    async def stop(self):
        self.stopping = True
        current = asyncio.current_task()
        tasks = [task for task in self.tasks if task is not current and not task.done()]
        for task in tasks:
            task.cancel()
        process = self.process
        self.process = None
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks = []

    async def _probe(self, url):
        command = ["ffprobe", "-v", "error"]
        if urlsplit(url).scheme.lower().startswith("rtsp"):
            command.extend(["-rtsp_transport", "tcp"])
        command.extend([
            "-select_streams", "v:0", "-show_entries",
            "stream=codec_name,profile,level,width,height", "-of", "json", url,
        ])
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=12)
        except asyncio.TimeoutError as exc:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise ValueError("连接视频源超时，请检查地址和网络") from exc
        if process.returncode:
            detail = stderr.decode("utf-8", errors="replace").strip().splitlines()
            raise ValueError(f"无法连接视频源: {(detail[-1] if detail else '未知错误')[:160]}")
        streams = json.loads(stdout).get("streams", [])
        if not streams:
            raise ValueError("视频源中没有可用的视频轨道")
        return streams[0]

    def _command(self, url, transcode):
        command = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-fflags", "nobuffer"]
        if urlsplit(url).scheme.lower().startswith("rtsp"):
            command.extend(["-rtsp_transport", "tcp"])
        command.extend(["-i", url, "-map", "0:v:0", "-an"])
        if transcode:
            command.extend([
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-profile:v", "baseline", "-pix_fmt", "yuv420p", "-g", "25",
            ])
        else:
            command.extend(["-c:v", "copy"])
        command.extend([
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-frag_duration", "500000", "-f", "mp4", "pipe:1",
        ])
        return command

    async def _relay(self):
        try:
            while self.process and self.process.stdout:
                chunk = await self.process.stdout.read(64 * 1024)
                if not chunk:
                    break
                await self.socket.send_bytes(chunk)
            process = self.process
            if process:
                code = await process.wait()
                if not self.stopping and not self.socket.closed:
                    await self.socket.send_json({
                        "type": "error", "message": f"视频流已中断（FFmpeg 退出码 {code}）"
                    })
        except (ConnectionResetError, asyncio.CancelledError):
            pass

    async def _log_stderr(self):
        try:
            while self.process and self.process.stderr:
                line = await self.process.stderr.readline()
                if not line:
                    break
                print(f"[WARN] 视频流 FFmpeg: {line.decode(errors='replace').strip()}", flush=True)
        except asyncio.CancelledError:
            pass
