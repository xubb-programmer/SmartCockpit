#!/usr/bin/env python3
import argparse
import asyncio
import hashlib
import json
import os
import shutil
import signal
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from clients.audio_endpoint import SpeechEndpoint
from clients.headless_audio import (
    PcmCapture,
    PcmPlayback,
    resolve_audio_backend,
    resolve_audio_device,
)
from clients.headless_kws import SherpaKeywordDetector
from config.config_loader import load_config
from core.auth import AuthManager
from core.interaction_access import get_access_config, get_access_mode


STATE_WAIT_WAKE = "wait_wake"
STATE_RESERVING = "reserving"
STATE_ACKNOWLEDGING = "acknowledging"
STATE_LISTENING = "listening"
STATE_PROCESSING = "processing"
STATE_PLAYING = "playing"


def log(level: str, message: str) -> None:
    print(f"[{level:<5}] {message}", flush=True)


def resolve_project_path(value: str) -> Path:
    path = Path(str(value or ""))
    return path if path.is_absolute() else PROJECT_DIR / path


def stable_device_id() -> str:
    source = ""
    for path in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
        try:
            source = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if source:
            break
    if not source:
        source = f"{uuid.getnode():012x}"
    digest = hashlib.sha256((source + ":smartcockpit-headless").encode()).hexdigest()[:12]
    return ":".join(digest[index:index + 2] for index in range(0, 12, 2)).upper()


def build_websocket_url(config: dict, headless_config: dict) -> str:
    raw_url = str(
        headless_config.get(
            "websocket_url", "ws://127.0.0.1:18000/voice_assistant/v1/"
        )
    )
    device_id = stable_device_id()
    client_id = "headless_client"
    query = {
        "device-id": device_id,
        "client-id": client_id,
    }

    server_config = config.get("server", {})
    auth_config = server_config.get("auth", {})
    if auth_config.get("enabled", False):
        token = AuthManager(
            secret_key=str(server_config.get("auth_key", "")),
            expire_seconds=auth_config.get("expire_seconds"),
        ).generate_token(client_id, device_id)
        query["authorization"] = f"Bearer {token}"

    parts = urlsplit(raw_url)
    existing = parts.query
    encoded = urlencode(query)
    combined = f"{existing}&{encoded}" if existing else encoded
    return urlunsplit((parts.scheme, parts.netloc, parts.path, combined, parts.fragment))


class HeadlessVoiceClient:
    def __init__(self, config: dict):
        access_config = get_access_config(config)
        headless_config = access_config.get("headless", {})
        if not isinstance(headless_config, dict):
            headless_config = {}

        self.config = config
        self.mode = get_access_mode(config)
        self.headless_config = headless_config
        self.sample_rate = int(headless_config.get("sample_rate", 16000))
        self.output_sample_rate = int(
            headless_config.get("output_sample_rate", 24000)
        )
        self.frame_duration_ms = int(headless_config.get("frame_duration_ms", 60))
        self.frame_samples = self.sample_rate * self.frame_duration_ms // 1000
        self.frame_bytes = self.frame_samples * 2
        self.capture_chunk_ms = min(
            200, max(20, int(headless_config.get("capture_chunk_ms", 100)))
        )
        self.command_timeout = float(
            headless_config.get("command_timeout_seconds", 12)
        )
        self.response_timeout = float(
            headless_config.get("response_timeout_seconds", 120)
        )
        self.ping_interval = float(
            headless_config.get("ping_interval_seconds", 30)
        )
        self.echo_tail = float(
            headless_config.get("echo_suppression_tail_ms", 120)
        ) / 1000.0
        self.speech_endpoint = SpeechEndpoint(
            speech_rms_threshold=float(
                headless_config.get("speech_rms_threshold", 0.006)
            ),
            speech_rms_threshold_low=float(
                headless_config.get("speech_rms_threshold_low", 0.003)
            ),
            speech_noise_multiplier=float(
                headless_config.get("speech_noise_multiplier", 2.0)
            ),
            speech_noise_low_multiplier=float(
                headless_config.get("speech_noise_low_multiplier", 1.35)
            ),
            speech_start_hold_seconds=float(
                headless_config.get("speech_start_hold_ms", 40)
            )
            / 1000.0,
            speech_min_seconds=float(
                headless_config.get("speech_min_seconds", 0.8)
            ),
            speech_end_seconds=float(
                headless_config.get("speech_end_seconds", 1.2)
            ),
        )
        self.capture_queue_seconds = int(
            headless_config.get("capture_queue_seconds", 3)
        )
        self.audio_start_timeout = max(
            0.0, float(headless_config.get("audio_start_timeout_seconds", 0))
        )

        configured_backend = str(
            headless_config.get("audio_backend", "auto") or "auto"
        ).strip().lower()
        pulse_commands_available = all(
            shutil.which(command) is not None
            for command in ("pactl", "parec", "paplay")
        )
        if (
            self.mode == "hybrid"
            and configured_backend == "auto"
            and pulse_commands_available
        ):
            self.audio_backend = "pulse"
        else:
            self.audio_backend = resolve_audio_backend(configured_backend)
        required_commands = (
            ("parec", "paplay")
            if self.audio_backend == "pulse"
            else ("arecord", "aplay")
        )

        missing_commands = [
            command for command in required_commands if shutil.which(command) is None
        ]
        if missing_commands:
            raise RuntimeError(
                f"缺少{self.audio_backend}音频命令: " + ", ".join(missing_commands)
            )

        input_device = resolve_audio_device(
            "input",
            headless_config.get("input_device", "auto"),
            self.audio_backend,
        )
        output_device = resolve_audio_device(
            "output",
            headless_config.get("output_device", "auto"),
            self.audio_backend,
        )
        self.capture = PcmCapture(
            input_device,
            self.sample_rate,
            chunk_ms=self.capture_chunk_ms,
            queue_seconds=max(2, self.capture_queue_seconds),
            backend=self.audio_backend,
        )
        self.playback = PcmPlayback(
            output_device,
            self.output_sample_rate,
            backend=self.audio_backend,
        )
        self.kws = SherpaKeywordDetector(headless_config, PROJECT_DIR)
        self.acknowledgement_audio = resolve_project_path(
            headless_config.get(
                "acknowledgement_audio", "config/assets/wakeup_words_short.wav"
            )
        )
        self.websocket_url = build_websocket_url(config, headless_config)

        from opuslib_next import Decoder, Encoder, constants

        self._encoder = Encoder(
            self.sample_rate, 1, constants.APPLICATION_AUDIO
        )
        self._decoder_type = Decoder
        self._decoder = None
        self._encoder_buffer = bytearray()

        self.state = STATE_WAIT_WAKE
        self.websocket = None
        self.connected = asyncio.Event()
        self.stopping = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._access_future = None
        self._listen_started_at = 0.0
        self._processing_started_at = 0.0
        self._last_audio_diagnostic_at = 0.0
        self._current_wake_word = ""

        log("INFO", f"语音入口模式: {self.mode}")
        log("INFO", f"音频后端: {self.audio_backend}")
        log("INFO", f"采集分块: {self.capture_chunk_ms}毫秒")
        log("INFO", f"麦克风设备: {input_device}")
        log("INFO", f"扬声器设备: {output_device}")
        log("INFO", f"提示音防回声等待: {self.echo_tail * 1000:.0f}毫秒")

    async def run(self) -> None:
        if not await self._start_capture_with_retry(self.audio_start_timeout):
            raise RuntimeError(self.capture.error or "麦克风采集启动失败")

        connection_task = asyncio.create_task(self._connection_loop())
        audio_task = asyncio.create_task(self._audio_loop())
        try:
            await self.stopping.wait()
        finally:
            for task in (connection_task, audio_task):
                task.cancel()
            await asyncio.gather(connection_task, audio_task, return_exceptions=True)
            self.capture.stop()
            self.playback.close()

    async def _start_capture_with_retry(self, timeout: float) -> bool:
        wait_forever = self.audio_backend == "pulse" and timeout <= 0
        deadline = None if wait_forever else time.monotonic() + max(0.0, timeout)
        attempts = 0
        while not self.stopping.is_set():
            attempts += 1
            await asyncio.to_thread(self.capture.start)
            await asyncio.sleep(0.4)
            if self.capture.is_alive():
                if attempts > 1:
                    log("INFO", f"音频服务已就绪，麦克风采集恢复成功，尝试={attempts}")
                return True

            reason = self.capture.error or "音频服务尚未就绪"
            await asyncio.to_thread(self.capture.stop)
            if self.audio_backend != "pulse" or (
                deadline is not None and time.monotonic() >= deadline
            ):
                log("ERROR", f"麦克风采集启动失败: {reason}")
                return False
            if attempts == 1 or attempts % 10 == 0:
                wait_status = (
                    "将持续重试"
                    if deadline is None
                    else f"剩余={max(0, int(deadline - time.monotonic()))}秒"
                )
                log("WARN", f"等待PulseAudio就绪: {reason}，{wait_status}")
            await asyncio.sleep(1.0)
        return False

    async def stop(self) -> None:
        self.stopping.set()
        websocket = self.websocket
        if websocket:
            try:
                await websocket.close()
            except Exception:
                pass

    async def _connection_loop(self) -> None:
        import websockets

        retry_delay = 1.0
        while not self.stopping.is_set():
            try:
                async with websockets.connect(
                    self.websocket_url,
                    ping_interval=None,
                    close_timeout=3,
                    max_size=None,
                ) as websocket:
                    self.websocket = websocket
                    await self._send_hello()
                    heartbeat = asyncio.create_task(self._heartbeat_loop())
                    retry_delay = 1.0
                    try:
                        async for message in websocket:
                            await self._handle_message(message)
                    finally:
                        heartbeat.cancel()
                        await asyncio.gather(heartbeat, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log("WARN", f"语音服务连接中断: {exc}")
            finally:
                self.connected.clear()
                self.websocket = None
                if self._access_future and not self._access_future.done():
                    self._access_future.set_result("disconnected")
                self._access_future = None
                await asyncio.to_thread(self.playback.stop_stream)
                self.state = STATE_WAIT_WAKE
                self._encoder_buffer.clear()

            if not self.stopping.is_set():
                await asyncio.sleep(retry_delay)
                retry_delay = min(10.0, retry_delay * 2)

    async def _send_hello(self) -> None:
        await self._send_json(
            {
                "type": "hello",
                "device_id": stable_device_id(),
                "device_name": "RK3588无界面语音客户端",
                "device_mac": stable_device_id(),
                "features": {"mcp": False, "client_mode": "headless"},
            }
        )

    async def _heartbeat_loop(self) -> None:
        while not self.stopping.is_set() and self.websocket:
            await asyncio.sleep(max(5.0, self.ping_interval))
            await self._send_json({"type": "ping"})

    async def _send_json(self, payload: dict) -> bool:
        websocket = self.websocket
        if websocket is None:
            return False
        try:
            async with self._send_lock:
                await websocket.send(json.dumps(payload, ensure_ascii=False))
            return True
        except Exception as exc:
            log("WARN", f"发送消息失败: {exc}")
            return False

    async def _send_audio(self, payload: bytes) -> bool:
        websocket = self.websocket
        if websocket is None:
            return False
        try:
            async with self._send_lock:
                await websocket.send(payload)
            return True
        except Exception as exc:
            log("WARN", f"发送音频失败: {exc}")
            return False

    async def _handle_message(self, message) -> None:
        if isinstance(message, bytes):
            if self._decoder is None:
                self._decoder = self._decoder_type(self.output_sample_rate, 1)
            try:
                frame_size = self.output_sample_rate * self.frame_duration_ms // 1000
                pcm = self._decoder.decode(message, frame_size)
                await asyncio.to_thread(self.playback.write, pcm)
            except Exception as exc:
                log("WARN", f"TTS音频解码失败: {exc}")
            return

        try:
            payload = json.loads(message)
        except (TypeError, json.JSONDecodeError):
            log("WARN", f"收到无法解析的消息: {message}")
            return

        message_type = payload.get("type")
        if message_type == "hello":
            audio_params = payload.get("audio_params", {})
            server_rate = int(audio_params.get("sample_rate", self.output_sample_rate))
            if server_rate != self.output_sample_rate:
                self.output_sample_rate = server_rate
                self.playback.sample_rate = server_rate
                self._decoder = None
            self.connected.set()
            log("INFO", f"语音服务连接成功，输出采样率={self.output_sample_rate}Hz")
            return

        if message_type == "access":
            status = str(payload.get("status", "unknown"))
            if self._access_future and not self._access_future.done():
                self._access_future.set_result(status)
            if status == "denied":
                log("ERROR", payload.get("message", "当前模式不接受唤醒客户端"))
            elif status == "busy":
                log("WARN", payload.get("message", "语音通道正在使用"))
            return

        if message_type == "stt":
            text = payload.get("text", "")
            if text:
                log("INFO", f"识别结果: {text}")
            if self.state == STATE_LISTENING:
                self.state = STATE_PROCESSING
                self._processing_started_at = time.monotonic()
            return

        if message_type == "tts":
            await self._handle_tts(payload)
            return

        if message_type == "server":
            log("WARN", payload.get("message", "语音服务状态异常"))

    async def _handle_tts(self, payload: dict) -> None:
        tts_state = payload.get("state")
        if tts_state == "start":
            self.state = STATE_PLAYING
            self.capture.flush()
            self._decoder = self._decoder_type(self.output_sample_rate, 1)
            await asyncio.to_thread(
                self.playback.start_stream, self.output_sample_rate
            )
            return
        if tts_state == "sentence_start" and payload.get("text"):
            log("INFO", f"助手回复: {payload['text']}")
            return
        if tts_state == "stop":
            await asyncio.to_thread(self.playback.stop_stream)
            await self._return_to_wake("tts_complete", release=False)

    async def _audio_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                chunk = await asyncio.to_thread(self.capture.read, 0.5)
                if chunk is None:
                    if not self.capture.is_alive():
                        reason = self.capture.error or "麦克风采集进程异常退出"
                        log("ERROR", f"{reason}，尝试自动恢复")
                        await self._restart_capture()
                    await self._check_state_timeout()
                    continue

                if self.state == STATE_WAIT_WAKE:
                    self.speech_endpoint.observe_pcm(chunk)
                    wake_word = self.kws.accept_pcm(chunk)
                    if wake_word:
                        await self._handle_wake_word(wake_word)
                elif self.state == STATE_LISTENING:
                    await self._stream_command_audio(chunk)
                    await self._check_state_timeout()
                elif self.state == STATE_PROCESSING:
                    await self._check_state_timeout()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log("ERROR", f"音频循环异常: {exc}，尝试自动恢复")
                self.state = STATE_WAIT_WAKE
                self._reset_command_state()
                await self._restart_capture()

    async def _restart_capture(self) -> None:
        await asyncio.to_thread(self.capture.stop)
        if self.stopping.is_set():
            return
        await asyncio.sleep(0.5)
        try:
            if await self._start_capture_with_retry(self.audio_start_timeout):
                log("INFO", "麦克风采集已自动恢复")
            else:
                log("ERROR", self.capture.error or "麦克风采集恢复失败")
        except Exception as exc:
            log("ERROR", f"麦克风采集恢复失败: {exc}")

    async def _handle_wake_word(self, wake_word: str) -> None:
        if self.state != STATE_WAIT_WAKE:
            return
        self.state = STATE_RESERVING
        self._current_wake_word = wake_word
        log("INFO", f"检测到唤醒词: {wake_word}")

        if not self.connected.is_set():
            log("WARN", "语音服务尚未连接")
            await asyncio.to_thread(self.playback.play_tone, 260, 260)
            await self._return_to_wake("server_unavailable", release=False)
            return

        access_status = await self._acquire_turn()
        if access_status != "granted":
            await asyncio.to_thread(self.playback.play_tone, 330, 160)
            await asyncio.to_thread(self.playback.play_tone, 260, 220)
            await self._return_to_wake("access_busy", release=False)
            return

        self.state = STATE_ACKNOWLEDGING
        # 保持采集进程运行，避免 ES8388/PulseAudio 重开输入时产生强瞬态。
        # 提示音及其声学尾音会在进入正式收音前统一丢弃。
        self.capture.flush()
        played = await asyncio.to_thread(
            self.playback.play_wave_file, self.acknowledgement_audio
        )
        if not played:
            await asyncio.to_thread(self.playback.play_tone, 660, 120)
        await asyncio.sleep(self.echo_tail)
        self.capture.flush()
        if not self.capture.is_alive():
            log("WARN", self.capture.error or "播放提示音期间麦克风采集已退出")
            await self._restart_capture()
            if not self.capture.is_alive():
                await self._return_to_wake("capture_restart_failed", release=True)
                return
        self.kws.reset()
        self._reset_command_state()
        if not await self._send_json(
            {"type": "listen", "state": "start", "mode": "manual"}
        ):
            await self._return_to_wake("listen_start_failed", release=True)
            return
        self.state = STATE_LISTENING
        self._listen_started_at = time.monotonic()
        log("INFO", "唤醒成功，请说出指令")

    async def _acquire_turn(self) -> str:
        loop = asyncio.get_running_loop()
        self._access_future = loop.create_future()
        if not await self._send_json(
            {"type": "access", "action": "acquire", "reason": "wake_word"}
        ):
            return "disconnected"
        try:
            return await asyncio.wait_for(self._access_future, timeout=4)
        except asyncio.TimeoutError:
            return "timeout"
        finally:
            self._access_future = None

    async def _stream_command_audio(self, chunk: bytes) -> None:
        now = time.monotonic()
        rms, high_threshold, low_threshold, should_finish = (
            self.speech_endpoint.process_pcm(
                chunk,
                now,
                self._listen_started_at,
            )
        )

        if now - self._last_audio_diagnostic_at >= 1.0:
            log(
                "DEBUG",
                f"[音频诊断][本地端点] RMS={rms:.4f}，"
                f"噪声底={self.speech_endpoint.noise_floor:.4f}，"
                f"阈值={high_threshold:.4f}/{low_threshold:.4f}，"
                f"已检测语音={self.speech_endpoint.heard_voice}",
            )
            self._last_audio_diagnostic_at = now

        self._encoder_buffer.extend(chunk)
        while len(self._encoder_buffer) >= self.frame_bytes:
            frame = bytes(self._encoder_buffer[: self.frame_bytes])
            del self._encoder_buffer[: self.frame_bytes]
            encoded = self._encoder.encode(frame, self.frame_samples)
            if not await self._send_audio(encoded):
                await self._return_to_wake("audio_send_failed", release=True)
                return

        if should_finish:
            await self._finish_listening("local_silence")

    async def _finish_listening(self, reason: str) -> None:
        if self.state != STATE_LISTENING:
            return
        self.state = STATE_PROCESSING
        self._processing_started_at = time.monotonic()
        await self._send_json(
            {"type": "listen", "state": "stop", "mode": "manual"}
        )
        log("INFO", f"指令录音结束，等待处理: {reason}")

    async def _check_state_timeout(self) -> None:
        now = time.monotonic()
        if (
            self.state == STATE_LISTENING
            and self._listen_started_at
            and now - self._listen_started_at >= self.command_timeout
        ):
            if not self.speech_endpoint.heard_voice:
                await self._send_json(
                    {"type": "listen", "state": "cancel", "mode": "manual"}
                )
                log("WARN", "唤醒后未检测到有效指令，取消本轮处理")
                await self._return_to_wake("no_command", release=True)
            else:
                await self._finish_listening("command_timeout")
        elif (
            self.state == STATE_PROCESSING
            and self._processing_started_at
            and now - self._processing_started_at >= self.response_timeout
        ):
            log("WARN", "语音请求处理超时，重新进入唤醒等待")
            await self._return_to_wake("response_timeout", release=True)

    def _reset_command_state(self) -> None:
        self._encoder.reset_state()
        self._encoder_buffer.clear()
        self._listen_started_at = 0.0
        self._processing_started_at = 0.0
        self.speech_endpoint.reset_command()
        self._last_audio_diagnostic_at = 0.0

    async def _return_to_wake(self, reason: str, release: bool) -> None:
        if release and self.websocket:
            await self._send_json({"type": "access", "action": "release"})
        self.playback.stop_stream()
        self.capture.flush()
        self.kws.reset()
        self._reset_command_state()
        self.state = STATE_WAIT_WAKE
        log("INFO", f"等待唤醒词，原因={reason}")


def parse_args():
    parser = argparse.ArgumentParser(description="RK3588无界面唤醒词语音客户端")
    parser.add_argument("--print-mode", action="store_true", help="仅输出当前入口模式")
    parser.add_argument("--check", action="store_true", help="检查配置、模型和音频设备")
    return parser.parse_args()


async def async_main(config: dict) -> int:
    client = HeadlessVoiceClient(config)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(client.stop()))
        except NotImplementedError:
            pass
    await client.run()
    return 0


def main() -> int:
    args = parse_args()
    config = load_config()
    mode_override = os.environ.get("VOICE_ACCESS_MODE", "").strip().lower()
    if mode_override:
        config.setdefault("voice_access", {})["mode"] = mode_override
    mode = get_access_mode(config)
    if args.print_mode:
        print(mode)
        return 0
    if mode == "web":
        log("INFO", "当前为 web 模式，无需启动板端唤醒客户端")
        return 0
    try:
        if args.check:
            client = HeadlessVoiceClient(config)
            log("INFO", "唤醒词配置、模型和音频设备检查通过")
            client.capture.stop()
            client.playback.close()
            return 0
        return asyncio.run(async_main(config))
    except Exception as exc:
        log("ERROR", f"无界面语音客户端启动失败: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
