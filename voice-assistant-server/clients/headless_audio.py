import array
import audioop
import math
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Optional


_DISPLAY_AUDIO_PATTERN = re.compile(r"hdmi|displayport|spdif|\bdp\b", re.I)
_PULSE_SAMPLE_SPEC_PATTERN = re.compile(r"\bs16(?:le|ne)\s+(\d+)ch\s+(\d+)Hz\b", re.I)


def pulse_environment() -> dict:
    """Build a PulseAudio environment that also works under a system service."""
    env = os.environ.copy()
    runtime_dir = env.get("XDG_RUNTIME_DIR")
    getuid = getattr(os, "getuid", None)
    if not runtime_dir and getuid is not None:
        runtime_dir_candidate = f"/run/user/{getuid()}"
        candidate = Path(runtime_dir_candidate)
        if candidate.is_dir():
            runtime_dir = runtime_dir_candidate
            env["XDG_RUNTIME_DIR"] = runtime_dir

    if not env.get("PULSE_SERVER"):
        candidates = [Path("/tmp/pulse-socket")]
        if runtime_dir:
            candidates.append(Path(runtime_dir) / "pulse" / "native")
        for socket_path in candidates:
            if socket_path.is_socket():
                env["PULSE_SERVER"] = f"unix:{socket_path.as_posix()}"
                break
    return env


def parse_alsa_cards(output: str, kind: str) -> list:
    candidates = []
    preferred = (
        re.compile(r"usb|mic|capture|es8388|es8323|es8316|codec|acodec", re.I)
        if kind == "input"
        else re.compile(r"es8388|es8323|es8316|rt5651|rt5640|rk809|codec|acodec", re.I)
    )
    for line in output.splitlines():
        match = re.match(r"^card\s+(\d+):\s+([^\s\[]+)", line.strip(), re.I)
        if not match or _DISPLAY_AUDIO_PATTERN.search(line):
            continue
        card_number = int(match.group(1))
        score = 10 if preferred.search(line) else 0
        candidates.append((score, card_number, f"plughw:{card_number},0", line.strip()))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates


def resolve_alsa_device(kind: str, configured: str = "auto") -> str:
    configured = str(configured or "auto").strip()
    if configured.lower() not in {"", "auto"}:
        return configured

    command = "arecord" if kind == "input" else "aplay"
    try:
        result = subprocess.run(
            [command, "-l"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        cards = parse_alsa_cards(result.stdout + "\n" + result.stderr, kind)
        if cards:
            return cards[0][2]
    except (OSError, subprocess.SubprocessError):
        pass
    return "default"


def pulse_available(attempts: int = 5, retry_interval: float = 0.25) -> bool:
    if any(shutil.which(command) is None for command in ("pactl", "parec", "paplay")):
        return False
    for attempt in range(max(1, attempts)):
        try:
            result = subprocess.run(
                ["pactl", "info"],
                env=pulse_environment(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            )
            if result.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            pass
        if attempt + 1 < attempts:
            time.sleep(max(0.0, retry_interval))
    return False


def resolve_audio_backend(configured: str = "auto") -> str:
    backend = str(configured or "auto").strip().lower()
    if backend == "auto":
        return "pulse" if pulse_available() else "alsa"
    if backend not in {"alsa", "pulse"}:
        raise ValueError(f"不支持的音频后端: {configured}")
    return backend


def resolve_audio_device(kind: str, configured: str, backend: str) -> str:
    configured = str(configured or "auto").strip()
    if configured.lower() not in {"", "auto"}:
        return configured
    if backend == "pulse":
        return "@DEFAULT_SOURCE@" if kind == "input" else "@DEFAULT_SINK@"
    return resolve_alsa_device(kind, configured)


def parse_pulse_sinks(output: str) -> list:
    sinks = []
    for line in output.splitlines():
        fields = line.split()
        match = _PULSE_SAMPLE_SPEC_PATTERN.search(line)
        if len(fields) < 2 or not match:
            continue
        channels = int(match.group(1))
        sample_rate = int(match.group(2))
        sinks.append((fields[1], sample_rate, channels))
    return sinks


def resolve_pulse_playback_format(device: str) -> tuple:
    try:
        result = subprocess.run(
            ["pactl", "list", "short", "sinks"],
            env=pulse_environment(),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        sinks = parse_pulse_sinks(result.stdout)
        if not sinks:
            return 48000, 2

        configured = str(device or "").strip()
        if configured not in {"", "auto", "@DEFAULT_SINK@"}:
            for name, sample_rate, channels in sinks:
                if name == configured:
                    return sample_rate, min(2, max(1, channels))

        info = subprocess.run(
            ["pactl", "info"],
            env=pulse_environment(),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        for name, sample_rate, channels in sinks:
            if name in info.stdout:
                return sample_rate, min(2, max(1, channels))
        _name, sample_rate, channels = sinks[0]
        return sample_rate, min(2, max(1, channels))
    except (OSError, subprocess.SubprocessError):
        return 48000, 2


def convert_pcm_s16le(
    pcm: bytes,
    source_rate: int,
    source_channels: int,
    target_rate: int,
    target_channels: int,
    rate_state=None,
) -> tuple:
    if not pcm:
        return b"", rate_state
    if source_channels not in {1, 2} or target_channels not in {1, 2}:
        raise ValueError("PCM转换仅支持单声道或双声道")

    converted = pcm
    working_channels = source_channels
    if source_channels == 2 and target_channels == 1:
        converted = audioop.tomono(converted, 2, 0.5, 0.5)
        working_channels = 1
    if source_rate != target_rate:
        converted, rate_state = audioop.ratecv(
            converted,
            2,
            working_channels,
            source_rate,
            target_rate,
            rate_state,
        )
    if working_channels == 1 and target_channels == 2:
        converted = audioop.tostereo(converted, 2, 1.0, 1.0)
    return converted, rate_state


class PcmCapture:
    def __init__(
        self,
        device: str,
        sample_rate: int = 16000,
        chunk_ms: int = 20,
        queue_seconds: int = 4,
        backend: str = "alsa",
    ):
        self.device = device
        self.backend = backend
        self.sample_rate = sample_rate
        self.chunk_ms = chunk_ms
        self.chunk_samples = sample_rate * chunk_ms // 1000
        self.chunk_bytes = self.chunk_samples * 2
        max_chunks = max(10, queue_seconds * 1000 // chunk_ms)
        self._queue = queue.Queue(maxsize=max_chunks)
        self._process: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()
        self.error: Optional[str] = None
        self.dropped_chunks = 0

    def start(self) -> None:
        if self._process and self._process.poll() is None:
            return
        if self._process or self._thread:
            self.stop()
        if self.backend == "pulse":
            command = [
                "parec",
                "--raw",
                f"--device={self.device}",
                "--format=s16le",
                f"--rate={self.sample_rate}",
                "--channels=1",
            ]
        else:
            command = [
                "arecord",
                "-q",
                "-D",
                self.device,
                "-t",
                "raw",
                "-f",
                "S16_LE",
                "-r",
                str(self.sample_rate),
                "-c",
                "1",
            ]
        self._stopping.clear()
        self.error = None
        self._process = subprocess.Popen(
            command,
            env=pulse_environment() if self.backend == "pulse" else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def _reader(self) -> None:
        pending = bytearray()
        process = self._process
        try:
            while not self._stopping.is_set() and process:
                data = process.stdout.read(self.chunk_bytes)
                if not data:
                    if process.poll() is not None:
                        break
                    continue
                pending.extend(data)
                while len(pending) >= self.chunk_bytes:
                    chunk = bytes(pending[: self.chunk_bytes])
                    del pending[: self.chunk_bytes]
                    try:
                        self._queue.put_nowait(chunk)
                    except queue.Full:
                        try:
                            self._queue.get_nowait()
                        except queue.Empty:
                            pass
                        self.dropped_chunks += 1
                        if self.dropped_chunks == 1 or self.dropped_chunks % 100 == 0:
                            print(
                                f"[WARN ] 麦克风队列已满，已丢弃最早音频块: {self.dropped_chunks}",
                                flush=True,
                            )
                        self._queue.put_nowait(chunk)
        except Exception as exc:
            self.error = str(exc)
        finally:
            if not self._stopping.is_set() and self.error is None:
                code = process.poll() if process else None
                details = ""
                if process and process.stderr:
                    try:
                        details = process.stderr.read().decode(
                            "utf-8", errors="replace"
                        ).strip()
                    except OSError:
                        pass
                suffix = f"，错误={details}" if details else ""
                self.error = f"麦克风采集进程已退出，状态码={code}{suffix}"

    def read(self, timeout: float = 1.0) -> Optional[bytes]:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def flush(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def is_alive(self) -> bool:
        return bool(self._process and self._process.poll() is None)

    def stop(self) -> None:
        self._stopping.set()
        process = self._process
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._process = None
        self._thread = None
        self.flush()


class PcmPlayback:
    def __init__(
        self, device: str, sample_rate: int = 24000, backend: str = "alsa"
    ):
        self.device = device
        self.sample_rate = sample_rate
        self.backend = backend
        self.playback_sample_rate = sample_rate
        self.playback_channels = 1
        self._rate_state = None
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    def _pulse_command(self) -> list:
        return [
            "paplay",
            "--raw",
            f"--device={self.device}",
            "--format=s16le",
            f"--rate={self.playback_sample_rate}",
            f"--channels={self.playback_channels}",
        ]

    def _prepare_pulse_format(self) -> None:
        self.playback_sample_rate, self.playback_channels = (
            resolve_pulse_playback_format(self.device)
        )

    def _convert_for_playback(
        self, pcm: bytes, source_rate: int, source_channels: int = 1, state=None
    ) -> tuple:
        if self.backend != "pulse":
            return pcm, state
        return convert_pcm_s16le(
            pcm,
            source_rate,
            source_channels,
            self.playback_sample_rate,
            self.playback_channels,
            state,
        )

    def start_stream(self, sample_rate: Optional[int] = None) -> None:
        with self._lock:
            self._stop_stream_locked()
            rate = int(sample_rate or self.sample_rate)
            self.sample_rate = rate
            self._rate_state = None
            if self.backend == "pulse":
                self._prepare_pulse_format()
                command = self._pulse_command()
            else:
                command = [
                    "aplay",
                    "-q",
                    "-D",
                    self.device,
                    "-t",
                    "raw",
                    "-f",
                    "S16_LE",
                    "-r",
                    str(rate),
                    "-c",
                    "1",
                ]
            self._process = subprocess.Popen(
                command,
                env=pulse_environment() if self.backend == "pulse" else None,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )

    def write(self, pcm: bytes) -> None:
        with self._lock:
            if not self._process or self._process.poll() is not None:
                return
            try:
                converted, self._rate_state = self._convert_for_playback(
                    pcm, self.sample_rate, 1, self._rate_state
                )
                self._process.stdin.write(converted)
            except (BrokenPipeError, OSError):
                self._stop_stream_locked()

    def _stop_stream_locked(self) -> None:
        process = self._process
        self._process = None
        if not process:
            return
        try:
            if process.stdin:
                process.stdin.close()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()

    def stop_stream(self) -> None:
        with self._lock:
            self._stop_stream_locked()

    def play_wave_file(self, path: Path, timeout: float = 10.0) -> bool:
        if not path.is_file():
            return False
        self.stop_stream()
        try:
            if self.backend == "pulse":
                with wave.open(str(path), "rb") as wav_file:
                    if wav_file.getsampwidth() != 2 or wav_file.getcomptype() != "NONE":
                        return False
                    source_rate = wav_file.getframerate()
                    source_channels = wav_file.getnchannels()
                    pcm = wav_file.readframes(wav_file.getnframes())
                self._prepare_pulse_format()
                pcm, _state = self._convert_for_playback(
                    pcm, source_rate, source_channels
                )
                command = self._pulse_command()
            else:
                pcm = None
                command = ["aplay", "-q", "-D", self.device, str(path)]
            result = subprocess.run(
                command,
                env=pulse_environment() if self.backend == "pulse" else None,
                input=pcm,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
            if result.returncode != 0 and result.stderr:
                details = result.stderr.decode("utf-8", errors="replace").strip()
                print(f"[WARN ] 扬声器播放失败: {details}", flush=True)
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def play_tone(self, frequency: int = 440, duration_ms: int = 180) -> bool:
        sample_count = self.sample_rate * duration_ms // 1000
        amplitude = 7000
        samples = array.array(
            "h",
            (
                int(amplitude * math.sin(2 * math.pi * frequency * i / self.sample_rate))
                for i in range(sample_count)
            ),
        )
        try:
            if self.backend == "pulse":
                self._prepare_pulse_format()
                pcm, _state = self._convert_for_playback(
                    samples.tobytes(), self.sample_rate
                )
                command = self._pulse_command()
            else:
                pcm = samples.tobytes()
                command = [
                    "aplay",
                    "-q",
                    "-D",
                    self.device,
                    "-t",
                    "raw",
                    "-f",
                    "S16_LE",
                    "-r",
                    str(self.sample_rate),
                    "-c",
                    "1",
                ]
            result = subprocess.run(
                command,
                env=pulse_environment() if self.backend == "pulse" else None,
                input=pcm,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=3,
                check=False,
            )
            if result.returncode != 0 and result.stderr:
                details = result.stderr.decode("utf-8", errors="replace").strip()
                print(f"[WARN ] 扬声器播放失败: {details}", flush=True)
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def close(self) -> None:
        self.stop_stream()
