"""RTSP/RTMP to fragmented MP4 WebSocket bridge for browser playback."""

import asyncio
import json
import shutil
from contextlib import suppress
from fractions import Fraction
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

SUPPORTED_SCHEMES = {"rtsp", "rtsps", "rtmp", "rtmps"}
MAX_STREAM_SESSIONS = 4
SMOOTH_MAX_WIDTH = 1280
SMOOTH_MAX_HEIGHT = 720
SMOOTH_MAX_FPS = 30.0
FRAGMENT_DURATION_US = 150_000


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


def parse_frame_rate(value):
    try:
        rate = float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError):
        return 0.0
    return rate if 0 < rate < 240 else 0.0


def smooth_dimensions(width, height):
    try:
        width = int(width)
        height = int(height)
    except (TypeError, ValueError):
        return None, None
    if width <= 0 or height <= 0:
        return None, None
    scale = min(1.0, SMOOTH_MAX_WIDTH / width, SMOOTH_MAX_HEIGHT / height)
    target_width = max(2, int(width * scale) // 2 * 2)
    target_height = max(2, int(height * scale) // 2 * 2)
    return target_width, target_height


def build_stream_plan(probe):
    source_codec = str(probe.get("codec_name", "")).lower()
    source_mime_codec = h264_codec_string(probe.get("profile"), probe.get("level"))
    source_width = probe.get("width")
    source_height = probe.get("height")
    target_width, target_height = smooth_dimensions(source_width, source_height)
    source_fps = parse_frame_rate(probe.get("avg_frame_rate")) or parse_frame_rate(
        probe.get("r_frame_rate")
    )
    output_fps = min(source_fps or 25.0, SMOOTH_MAX_FPS)
    oversized = (
        target_width is not None
        and target_height is not None
        and (target_width != int(source_width) or target_height != int(source_height))
    )
    high_frame_rate = source_fps > SMOOTH_MAX_FPS + 0.5
    transcode = (
        source_codec != "h264"
        or not source_mime_codec
        or oversized
        or high_frame_rate
    )
    return {
        "source_codec": source_codec or "unknown",
        "source_mime_codec": source_mime_codec,
        "source_width": source_width,
        "source_height": source_height,
        "source_fps": source_fps,
        "output_width": target_width if transcode else source_width,
        "output_height": target_height if transcode else source_height,
        "output_fps": output_fps if transcode else source_fps,
        "transcode": transcode,
        "codec": "avc1.42E01F" if transcode else source_mime_codec,
    }


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
        plan = build_stream_plan(probe)
        transcode = plan["transcode"]
        print(
            f"[INFO] 视频流开始: {masked_stream_url(url)} "
            f"({plan['source_codec']} {plan['source_width']}x{plan['source_height']} "
            f"-> {'H.264 低延时转码' if transcode else 'H.264 低延时直通'} "
            f"{plan['output_width']}x{plan['output_height']})",
            flush=True,
        )
        self.process = await asyncio.create_subprocess_exec(
            *self._command(url, plan),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await self.socket.send_json({
            "type": "stream-started",
            "codec": plan["codec"],
            "sourceCodec": plan["source_codec"],
            "transcoding": transcode,
            "width": plan["output_width"],
            "height": plan["output_height"],
            "sourceWidth": plan["source_width"],
            "sourceHeight": plan["source_height"],
            "fps": plan["output_fps"],
            "latencyMode": "smooth",
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
            "stream=codec_name,profile,level,width,height,r_frame_rate,avg_frame_rate",
            "-of", "json", url,
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

    def _command(self, url, plan):
        scheme = urlsplit(url).scheme.lower()
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-fflags", "nobuffer+genpts+discardcorrupt",
            "-flags", "low_delay",
            "-probesize", "512000",
            "-analyzeduration", "500000",
        ]
        if scheme.startswith("rtsp"):
            command.extend([
                "-rtsp_transport", "tcp",
                "-reorder_queue_size", "16",
                "-max_delay", "300000",
                "-buffer_size", "1048576",
            ])
        elif scheme.startswith("rtmp"):
            command.extend(["-rtmp_live", "live", "-rtmp_buffer", "100"])
        command.extend(["-i", url, "-map", "0:v:0", "-an", "-sn", "-dn"])
        if plan["transcode"]:
            filters = []
            if plan["output_width"] and plan["output_height"]:
                filters.append(
                    f"scale={plan['output_width']}:{plan['output_height']}:flags=bilinear"
                )
            if plan["output_fps"]:
                filters.append(f"fps={plan['output_fps']:g}")
            if filters:
                command.extend(["-vf", ",".join(filters)])
            keyframe_interval = max(10, round((plan["output_fps"] or 25.0) / 2))
            command.extend([
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-profile:v", "baseline", "-level:v", "3.1", "-pix_fmt", "yuv420p",
                "-g", str(keyframe_interval), "-keyint_min", str(keyframe_interval),
                "-sc_threshold", "0", "-bf", "0", "-refs", "1",
                "-crf", "23", "-maxrate", "2400k", "-bufsize", "480k", "-threads", "4",
            ])
        else:
            command.extend(["-c:v", "copy"])
        command.extend([
            "-movflags",
            "frag_keyframe+empty_moov+default_base_moof+omit_tfhd_offset+negative_cts_offsets+delay_moov",
            "-frag_duration", str(FRAGMENT_DURATION_US),
            "-min_frag_duration", "75000",
            "-video_track_timescale", "90000",
            "-flush_packets", "1",
            "-f", "mp4", "pipe:1",
        ])
        return command

    async def _relay(self):
        try:
            while self.process and self.process.stdout:
                chunk = await self.process.stdout.read(256 * 1024)
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
