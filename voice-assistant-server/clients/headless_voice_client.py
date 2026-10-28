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
from collections import deque
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
from clients.headless_vad import PcmSpeechDetector
from core.auth import AuthManager
from core.interaction_access import get_access_config, get_access_mode


STATE_WAIT_WAKE = "wait_wake"
STATE_RESERVING = "reserving"
STATE_ACKNOWLEDGING = "acknowledging"
STATE_LISTENING = "listening"
STATE_PROCESSING = "processing"
STATE_PLAYING = "playing"
STATE_DRAINING = "draining"
STATE_FOLLOWUP = "followup"


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
            200, max(20, int(headless_config.get("capture_chunk_ms", 40)))
        )
        self.command_timeout = float(
            headless_config.get("command_timeout_seconds", 12)
        )
        self.response_timeout = float(
            headless_config.get("response_timeout_seconds", 120)
        )
        self.initial_listen_timeout = max(1.0, float(
            headless_config.get("initial_listen_timeout_seconds", 8)
        ))
        self.followup_timeout = max(0.0, float(
            headless_config.get("followup_timeout_seconds", 8)
        ))
        self.acknowledgement_mode = str(
            headless_config.get("acknowledgement_mode", "tone")
        )
        if self.acknowledgement_mode not in {"tone", "voice"}:
            raise ValueError("acknowledgement_mode 必须为 tone 或 voice")
        self.ping_interval = float(
            headless_config.get("ping_interval_seconds", 30)
        )
        self.echo_tail = float(
            headless_config.get("echo_suppression_tail_ms", 200)
        ) / 1000.0
        self.tone_echo_tail = min(self.echo_tail, max(0.0, float(
            headless_config.get("tone_echo_suppression_tail_ms", 80)
        ) / 1000.0))
        # 服务端 TTS stop 后也有抑制窗口，续说必须等两侧均可接收。
        self.reply_echo_tail = max(
            self.echo_tail, float(config.get("echo_suppression_tail_ms", 800)) / 1000
        )
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
                headless_config.get("speech_min_seconds", 0.35)
            ),
            speech_end_seconds=float(
                headless_config.get("speech_end_seconds", 0.5)
            ),
        )
        self.speech_vad = None
        self._vad_probability = 0.0
        self._vad_quiet_since = None
        self._vad_checked_at = 0.0
        self._vad_diagnostic_at = 0.0
        self.vad_threshold = float(headless_config.get("speech_vad_threshold", 0.6))
        self.vad_threshold_low = float(headless_config.get("speech_vad_threshold_low", 0.2))
        if headless_config.get("speech_vad_enabled", True):
            if self.sample_rate != 16000:
                raise ValueError("本地 Silero 语音检测要求 sample_rate=16000")
            vad_config = config.get("VAD", {}).get("SileroVAD", {})
            model_dir = resolve_project_path(vad_config.get("model_dir", "models/snakers4_silero-vad"))
            self.speech_vad = PcmSpeechDetector(model_dir / "src/silero_vad/data/silero_vad.onnx")
            self.speech_endpoint.speech_start_hold_seconds = max(
                self.speech_endpoint.speech_start_hold_seconds,
                float(headless_config.get("speech_vad_start_hold_ms", 120)) / 1000,
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
        self._music_active = False
        self._music_abort_future = None
        self._listen_started_at = 0.0
        self._processing_started_at = 0.0
        self._last_audio_diagnostic_at = 0.0
        self._current_wake_word = ""
        self._epoch = 0
        self._completion_task = None
        self._followup_started_at = 0.0
        self._response_activity_at = 0.0
        self._first_audio_at = 0.0
        self._tts_stopped_at = 0.0
        self._wake_detected_at = 0.0
        self._exit_requested = False
        self._preroll = deque(maxlen=max(2, 320 // self.capture_chunk_ms))

        log("INFO", f"语音入口模式: {self.mode}")
        log("INFO", f"音频后端: {self.audio_backend}")
        log("INFO", f"采集分块: {self.capture_chunk_ms}毫秒")
        log("INFO", f"本地人声检测: {'Silero，播报后需安静间隔' if self.speech_vad else '仅能量检测'}")
        log("INFO", f"麦克风设备: {input_device}")
        log("INFO", f"扬声器设备: {output_device}")
        log("INFO", f"短提示音/语音提示防回声等待: {self.tone_echo_tail * 1000:.0f}/{self.echo_tail * 1000:.0f}毫秒")
        log("INFO", f"首次等待说话={self.initial_listen_timeout:g}秒，回复后续说窗口={self.followup_timeout:g}秒")

    async def run(self) -> None:
        if not await self._start_capture_with_retry(self.audio_start_timeout):
            raise RuntimeError(self.capture.error or "麦克风采集启动失败")

        connection_task = asyncio.create_task(self._connection_loop())
        audio_task = asyncio.create_task(self._audio_loop())
        try:
            await self.stopping.wait()
        finally:
            self._invalidate_turn()
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
                # 告别时服务端可在 tts/stop 后立即关连接；先完成本地播放收尾。
                if self._exit_requested and self._completion_task and not self.stopping.is_set():
                    await asyncio.gather(self._completion_task, return_exceptions=True)
                drain_playback = self.state == STATE_DRAINING or self._exit_requested
                farewell = self._exit_requested
                interrupted = self.state in {
                    STATE_LISTENING, STATE_PROCESSING, STATE_PLAYING,
                } and not self._exit_requested
                self.connected.clear()
                self.websocket = None
                self._invalidate_turn()
                if self._access_future and not self._access_future.done():
                    self._access_future.set_result("disconnected")
                self._access_future = None
                if self._music_abort_future and not self._music_abort_future.done():
                    self._music_abort_future.set_result(False)
                self._music_abort_future = None
                self._music_active = False
                await asyncio.to_thread(self.playback.stop_stream, drain_playback)
                if (interrupted or farewell) and not self.stopping.is_set():
                    await asyncio.to_thread(self.playback.play_cue, "sleep" if farewell else "error")
                    await asyncio.sleep(self.echo_tail)
                self.state = STATE_WAIT_WAKE
                self.capture.flush()
                self.kws.reset()
                self._reset_command_state()

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
            if self.state not in {STATE_PROCESSING, STATE_PLAYING}:
                return  # 已结束/超时轮次的迟到音频不能重新打开播放。
            self._response_activity_at = time.monotonic()
            if not self._first_audio_at:
                self._first_audio_at = self._response_activity_at
                if self._processing_started_at:
                    log("INFO", f"[性能][唤醒客户端] 提交录音到首帧={(self._first_audio_at - self._processing_started_at) * 1000:.0f}毫秒")
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
            if status in {"granted", "busy", "denied", "initializing"} and self._access_future and not self._access_future.done():
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
                self._exit_requested = text.strip(" 。！？,.!?\n") in self.config.get(
                    "exit_commands", ["退出", "关闭", "再见", "告别", "结束对话"]
                )
            if self.state == STATE_LISTENING:
                self.state = STATE_PROCESSING
                self._processing_started_at = time.monotonic()
            if self.state in {STATE_PROCESSING, STATE_PLAYING}:
                self._response_activity_at = time.monotonic()
            return

        if message_type == "tts":
            await self._handle_tts(payload)
            return

        if message_type == "server":
            log("WARN", payload.get("message", "语音服务状态异常"))
            if payload.get("status") in {"empty_asr", "asr_error", "initializing"}:
                if self.state in {STATE_LISTENING, STATE_PROCESSING, STATE_PLAYING}:
                    # 接收循环不能等待 acquire 回复；后续转移交给独立任务。
                    self.state = STATE_DRAINING
                    self._completion_task = asyncio.create_task(
                        self._return_to_wake(str(payload["status"]), release=True, cue="error")
                    )

    async def _handle_tts(self, payload: dict) -> None:
        tts_state = payload.get("state")
        if tts_state == "music_start":
            if self.state == STATE_PLAYING:
                self._music_active = True
                self.capture.flush()
                self.kws.reset()
                log("INFO", "音乐播放中，可说唤醒词打断；停歌并响就绪音后继续说话")
            return
        if tts_state == "start":
            if self.state not in {STATE_LISTENING, STATE_PROCESSING, STATE_PLAYING}:
                return
            already_playing = self.state == STATE_PLAYING
            self._music_active = False
            self.state = STATE_PLAYING
            self._response_activity_at = time.monotonic()
            self.capture.flush()
            if already_playing:
                return
            self._decoder = self._decoder_type(self.output_sample_rate, 1)
            await asyncio.to_thread(
                self.playback.start_stream, self.output_sample_rate
            )
            return
        if tts_state == "sentence_start" and payload.get("text"):
            log("INFO", f"助手回复: {payload['text']}")
            return
        if tts_state == "stop":
            self._music_active = False
            if self._music_abort_future and not self._music_abort_future.done():
                self._music_abort_future.set_result(True)
                return
        if tts_state == "stop" and self.state in {STATE_PROCESSING, STATE_PLAYING}:
            self._tts_stopped_at = time.monotonic()
            self.state = STATE_DRAINING
            self._completion_task = asyncio.create_task(self._after_reply(self._epoch))

    async def _audio_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                state_before_read = self.state
                chunk = await asyncio.to_thread(self.capture.read, 0.5)
                if chunk is None:
                    if not self.capture.is_alive():
                        reason = self.capture.error or "麦克风采集进程异常退出"
                        log("ERROR", f"{reason}，尝试自动恢复")
                        active_request = self.state in {STATE_PROCESSING, STATE_PLAYING}
                        websocket = self.websocket
                        await self._return_to_wake("capture_lost", release=not active_request, cue="error")
                        if active_request and websocket:
                            await websocket.close()
                        await self._restart_capture()
                    await self._check_state_timeout()
                    continue

                if state_before_read != self.state:
                    continue  # 状态转换前取出的音频可能仍包含提示音/播报尾音。

                if self.state == STATE_WAIT_WAKE:
                    is_speech = await self._speech_activity(chunk)
                    if is_speech is False:
                        self.speech_endpoint.observe_noise(self.speech_endpoint.pcm_rms(chunk), trusted=True)
                    elif is_speech is None:
                        self.speech_endpoint.observe_pcm(chunk)
                    wake_word = await asyncio.to_thread(self.kws.accept_pcm, chunk)
                    if wake_word:
                        await self._handle_wake_word(wake_word)
                elif self.state == STATE_FOLLOWUP:
                    await self._stream_followup_audio(chunk)
                elif self.state == STATE_LISTENING:
                    await self._stream_command_audio(chunk)
                elif self.state == STATE_PLAYING and self._music_active:
                    await self._stream_music_audio(chunk)
                else:
                    # Keep VAD warm while audio is playing; resetting on every
                    # reply can classify the first background frames as speech.
                    await self._speech_activity(chunk)
                await self._check_state_timeout()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log("ERROR", f"音频循环异常: {exc}，尝试自动恢复")
                await self._return_to_wake("audio_error", release=True, cue="error")
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
        epoch = self._epoch
        self._wake_detected_at = time.monotonic()
        self._current_wake_word = wake_word
        log("INFO", f"检测到唤醒词: {wake_word}")

        if not self.connected.is_set():
            log("WARN", "语音服务尚未连接")
            await self._return_to_wake("server_unavailable", release=False, cue="error")
            return

        access_status = await self._acquire_turn()
        access_ready_at = time.monotonic()
        if epoch != self._epoch:
            return
        if access_status != "granted":
            await self._return_to_wake("access_" + access_status, release=True, cue="error")
            return

        self.state = STATE_ACKNOWLEDGING
        # 保持采集进程运行，避免 ES8388/PulseAudio 重开输入时产生强瞬态。
        # 提示音及其声学尾音会在进入正式收音前统一丢弃。
        self.capture.flush()
        cue_started_at = time.monotonic()
        played = False
        if self.acknowledgement_mode == "voice":
            played = await asyncio.to_thread(
                self.playback.play_wave_file, self.acknowledgement_audio
            )
        if not played:
            await asyncio.to_thread(self.playback.play_cue, "ready")
        cue_finished_at = time.monotonic()
        await asyncio.sleep(self.echo_tail if played else self.tone_echo_tail)
        if epoch != self._epoch or not self.connected.is_set():
            return
        self.capture.flush()
        if not self.capture.is_alive():
            log("WARN", self.capture.error or "播放提示音期间麦克风采集已退出")
            await self._restart_capture()
            if not self.capture.is_alive():
                await self._return_to_wake("capture_restart_failed", release=True, cue="error")
                return
        self.kws.reset()
        if await self._begin_listening():
            log("INFO", f"[性能][唤醒客户端] 检出到可收音={(time.monotonic() - self._wake_detected_at) * 1000:.0f}毫秒")
            log("INFO", f"[性能][唤醒分段] 通道申请={(access_ready_at - self._wake_detected_at) * 1000:.0f}毫秒，"
                f"提示音播放={(cue_finished_at - cue_started_at) * 1000:.0f}毫秒，"
                f"尾音等待={(self.echo_tail if played else self.tone_echo_tail) * 1000:.0f}毫秒")
            log("INFO", f"唤醒成功，请在{self.initial_listen_timeout:g}秒内开始说话")

    async def _stream_music_audio(self, chunk: bytes) -> None:
        # Music/lyrics are never uploaded as a command. Only KWS can interrupt.
        await self._speech_activity(chunk)
        wake_word = await asyncio.to_thread(self.kws.accept_pcm, chunk)
        if not wake_word or self.state != STATE_PLAYING or not self._music_active:
            return
        detected_at = time.monotonic()
        self._invalidate_turn()
        epoch = self._epoch
        self.state = STATE_RESERVING  # Drop buffered music while stopping the server.
        self._music_active = False
        future = asyncio.get_running_loop().create_future()
        self._music_abort_future = future
        try:
            await asyncio.to_thread(self.playback.stop_stream, False)
            self._decoder = None
            self.capture.flush()
            if not await self._send_json({"type": "abort", "reason": "wake_word"}):
                raise ConnectionError("音乐打断消息发送失败")
            stopped = await asyncio.wait_for(future, timeout=3)
            if not stopped or epoch != self._epoch or not self.connected.is_set():
                return
            self.kws.reset()
            self._reset_command_state()
            self.state = STATE_WAIT_WAKE
            await self._handle_wake_word(wake_word)
            if self.state == STATE_LISTENING:
                log("INFO", f"[性能][音乐唤醒] 检出到可收音={(time.monotonic()-detected_at)*1000:.0f}毫秒")
        except (asyncio.TimeoutError, ConnectionError) as exc:
            log("WARN", f"音乐打断失败，重新连接: {exc}")
            websocket = self.websocket
            await self._return_to_wake("music_interrupt_failed", release=False, cue="error")
            if websocket:
                await websocket.close()
        finally:
            self._music_abort_future = None

    async def _begin_listening(self, *, keep_endpoint: bool = False) -> bool:
        epoch = self._epoch
        self._reset_command_state(keep_endpoint=keep_endpoint)
        if not await self._send_json(
            {"type": "listen", "state": "start", "mode": "manual"}
        ):
            await self._return_to_wake("listen_start_failed", release=True, cue="error")
            return False
        if epoch != self._epoch or not self.connected.is_set():
            return False
        self.state = STATE_LISTENING
        self._listen_started_at = time.monotonic()
        return True

    async def _acquire_turn(self) -> str:
        loop = asyncio.get_running_loop()
        self._access_future = loop.create_future()
        try:
            if not await self._send_json(
                {"type": "access", "action": "acquire", "reason": "wake_word"}
            ):
                return "disconnected"
            return await asyncio.wait_for(self._access_future, timeout=4)
        except asyncio.TimeoutError:
            # 关闭本次连接，避免迟到的 granted 被下一次唤醒误用。
            if self.websocket:
                await self.websocket.close()
            return "timeout"
        finally:
            self._access_future = None

    async def _stream_command_audio(self, chunk: bytes) -> None:
        now = time.monotonic()
        if not self.speech_endpoint.heard_voice and self._listen_window_expired(
            now, self._listen_started_at, self.initial_listen_timeout
        ):
            await self._check_state_timeout()
            return
        rms, high_threshold, low_threshold, should_finish = (
            self.speech_endpoint.process_pcm(
                chunk,
                now,
                self._listen_started_at,
                is_speech=await self._speech_activity(chunk),
            )
        )

        if now - self._last_audio_diagnostic_at >= 1.0:
            log(
                "DEBUG",
                f"[音频诊断][本地端点] RMS={rms:.4f}，"
                f"噪声底={self.speech_endpoint.noise_floor:.4f}，"
                f"阈值={high_threshold:.4f}/{low_threshold:.4f}，"
                f"语音概率={self._vad_probability:.3f}，"
                f"已检测语音={self.speech_endpoint.heard_voice}",
            )
            self._last_audio_diagnostic_at = now

        if not await self._encode_and_send(chunk):
            return

        if should_finish:
            await self._finish_listening("local_silence")

    async def _encode_and_send(self, chunk: bytes) -> bool:
        self._encoder_buffer.extend(chunk)
        while len(self._encoder_buffer) >= self.frame_bytes:
            frame = bytes(self._encoder_buffer[: self.frame_bytes])
            del self._encoder_buffer[: self.frame_bytes]
            encoded = self._encoder.encode(frame, self.frame_samples)
            if not await self._send_audio(encoded):
                await self._return_to_wake("audio_send_failed", release=True, cue="error")
                return False
        return True

    async def _finish_listening(self, reason: str) -> None:
        if self.state != STATE_LISTENING:
            return
        # 最后一块不足一帧时补零发送，不能悄悄丢失句尾。
        if self._encoder_buffer:
            padding = b"\0" * (self.frame_bytes - len(self._encoder_buffer))
            if not await self._encode_and_send(padding):
                return
        self.state = STATE_PROCESSING
        self._processing_started_at = time.monotonic()
        self._response_activity_at = self._processing_started_at
        if not await self._send_json(
            {"type": "listen", "state": "stop", "mode": "manual"}
        ):
            await self._return_to_wake("listen_stop_failed", release=True, cue="error")
            return
        log("INFO", f"[性能][唤醒客户端] 句尾等待={(self._processing_started_at - self.speech_endpoint.last_voice_at) * 1000:.0f}毫秒")
        log("INFO", f"指令录音结束，等待处理: {reason}")

    async def _check_state_timeout(self) -> None:
        now = time.monotonic()
        if (
            self.state == STATE_LISTENING
            and self._listen_started_at
        ):
            if not self.speech_endpoint.heard_voice and self._listen_window_expired(now, self._listen_started_at, self.initial_listen_timeout):
                await self._send_json(
                    {"type": "listen", "state": "cancel", "mode": "manual"}
                )
                log("WARN", "唤醒后未检测到有效指令，取消本轮处理")
                await self._return_to_wake("no_command", release=True, cue="sleep")
            elif self.speech_endpoint.first_voice_at is not None and now - self.speech_endpoint.first_voice_at >= self.command_timeout:
                await self._finish_listening("command_timeout")
        elif (
            self.state in {STATE_PROCESSING, STATE_PLAYING}
            and self._response_activity_at
            and now - self._response_activity_at >= self.response_timeout
        ):
            log("WARN", "语音请求处理超时，重新进入唤醒等待")
            websocket = self.websocket
            await self._return_to_wake("response_timeout", release=False, cue="error")
            if websocket:
                await websocket.close()  # 隔离旧回复，重连后才接受下一轮。
        elif self.state == STATE_FOLLOWUP and self._listen_window_expired(now, self._followup_started_at, self.followup_timeout):
            await self._return_to_wake("followup_timeout", release=False, cue="sleep")

    def _listen_window_expired(self, now: float, started_at: float, timeout: float) -> bool:
        deadline = started_at + timeout
        return now >= deadline and not self.speech_endpoint.onset_pending(
            now, deadline, self.capture_chunk_ms / 1000.0
        )

    async def _after_reply(self, epoch: int) -> None:
        try:
            await asyncio.to_thread(self.playback.stop_stream)
            # Server protection runs from tts/stop, concurrently with local drain.
            # Wait for its remaining time rather than adding it again afterward.
            remaining = self._tts_stopped_at + self.reply_echo_tail - time.monotonic()
            await asyncio.sleep(max(self.echo_tail, remaining, 0.0))
            if epoch != self._epoch or not self.connected.is_set():
                return
            if self.followup_timeout <= 0 or self._exit_requested:
                await self._return_to_wake("tts_complete", release=False, cue="sleep")
                return
            if self.speech_vad:
                # Arm followup only after a genuine quiet gap following playback.
                # A lingering reply/echo must never become a fresh speech onset.
                deadline = time.monotonic() + 1.5
                while epoch == self._epoch and self.connected.is_set():
                    now = time.monotonic()
                    if (self._vad_quiet_since is not None
                            and now - self._vad_quiet_since >= 0.2
                            and now - self._vad_checked_at <= 0.2):
                        break
                    if now >= deadline:
                        await self._return_to_wake("reply_not_quiet", release=False, cue="sleep")
                        return
                    await asyncio.sleep(0.04)
                if epoch != self._epoch or not self.connected.is_set():
                    return
            await asyncio.to_thread(self.playback.play_cue, "ready")
            await asyncio.sleep(self.tone_echo_tail)
            if epoch != self._epoch or not self.connected.is_set():
                return
            self.capture.flush()
            self._reset_command_state()
            self._preroll.clear()
            self._followup_started_at = time.monotonic()
            self.state = STATE_FOLLOWUP
            log("INFO", f"可直接继续说话，{self.followup_timeout:g}秒无语音后响结束音，再次使用需唤醒")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log("ERROR", f"恢复续说失败: {exc}")
            await self._return_to_wake("followup_error", release=True, cue="error")

    async def _stream_followup_audio(self, chunk: bytes) -> None:
        now = time.monotonic()
        # 到期前已有起音候选时允许完成短暂确认，超时后新起音仍拒绝。
        if self._listen_window_expired(now, self._followup_started_at, self.followup_timeout):
            await self._return_to_wake("followup_timeout", release=False, cue="sleep")
            return
        self._preroll.append(chunk)
        self.speech_endpoint.process_pcm(
            chunk, now, self._followup_started_at,
            is_speech=await self._speech_activity(chunk),
        )
        if not self.speech_endpoint.heard_voice:
            return
        self.state = STATE_RESERVING
        epoch = self._epoch
        status = await self._acquire_turn()
        if epoch != self._epoch:
            return
        if status != "granted":
            await self._return_to_wake("followup_" + status, release=True, cue="error")
            return
        # 等待续说时不占用通道；确认人声后再申请，并补发起音前缓存。
        buffered = b"".join(self._preroll)
        self._preroll.clear()
        if await self._begin_listening(keep_endpoint=True):
            await self._encode_and_send(buffered)

    async def _speech_activity(self, chunk: bytes):
        if self.speech_vad is None:
            return None
        probability = await asyncio.to_thread(self.speech_vad.probability, chunk)
        self._vad_probability = probability
        now = time.monotonic()
        self._vad_checked_at = now
        if probability <= self.vad_threshold_low:
            if self._vad_quiet_since is None:
                self._vad_quiet_since = now
        else:
            self._vad_quiet_since = None
        threshold = self.vad_threshold_low if self.speech_endpoint.heard_voice else self.vad_threshold
        interval = 2.0 if self.state == STATE_FOLLOWUP else 30.0
        if now - self._vad_diagnostic_at >= interval:
            log("DEBUG", f"[音频诊断][本地人声] 状态={self.state}，"
                f"RMS={self.speech_endpoint.pcm_rms(chunk):.4f}，"
                f"语音概率={probability:.3f}，已检测语音={self.speech_endpoint.heard_voice}")
            self._vad_diagnostic_at = now
        return probability >= threshold

    def _invalidate_turn(self) -> None:
        self._epoch += 1
        task = self._completion_task
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
        self._completion_task = None

    def _reset_command_state(self, *, keep_endpoint: bool = False) -> None:
        self._encoder.reset_state()
        self._encoder_buffer.clear()
        self._listen_started_at = 0.0
        self._processing_started_at = 0.0
        self._response_activity_at = 0.0
        self._first_audio_at = 0.0
        self._tts_stopped_at = 0.0
        self._exit_requested = False
        self._music_active = False
        if not keep_endpoint:
            self.speech_endpoint.reset_command()
        self._last_audio_diagnostic_at = 0.0

    async def _return_to_wake(self, reason: str, release: bool, cue: str = None) -> None:
        self._invalidate_turn()
        epoch = self._epoch
        self.state = STATE_DRAINING
        if release and self.websocket:
            await self._send_json({"type": "access", "action": "release"})
        await asyncio.to_thread(self.playback.stop_stream, False)
        if cue:
            await asyncio.to_thread(self.playback.play_cue, cue)
            await asyncio.sleep(self.echo_tail)
        if epoch != self._epoch:
            return
        self.capture.flush()
        self.kws.reset()
        self._reset_command_state()
        self._preroll.clear()
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
    from config.config_loader import load_config

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
