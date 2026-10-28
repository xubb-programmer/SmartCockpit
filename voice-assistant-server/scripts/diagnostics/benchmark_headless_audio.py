#!/usr/bin/env python3
"""Compare audio plumbing on an isolated PulseAudio sink (no physical playback).

Run using the deployed Python environment, from the candidate source tree:
  python scripts/diagnostics/benchmark_headless_audio.py --baseline /opt/jw/voice-assistant-server
Does not connect to the voice server or send device commands.
"""
import argparse
import concurrent.futures
import importlib.util
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from clients import headless_audio as candidate
from clients.audio_endpoint import SpeechEndpoint


def load_baseline(root):
    spec = importlib.util.spec_from_file_location(
        "baseline_headless_audio", root / "clients/headless_audio.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def measure_capture(module, sink, chunk_ms, repetitions):
    capture = module.PcmCapture(sink + ".monitor", chunk_ms=chunk_ms, backend="pulse")
    stimulus = candidate.PcmPlayback(sink, backend="pulse")
    results = []
    capture.start()
    try:
        # Wait for actual data, not merely a live parec process.
        if capture.read(timeout=5) is None:
            raise RuntimeError(capture.error or "capture produced no data")
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            for _ in range(repetitions):
                capture.flush()
                started = time.monotonic()
                playing = executor.submit(stimulus.play_tone, 1000, 180)
                detected = None
                while time.monotonic() - started < 5:
                    chunk = capture.read(timeout=0.1)
                    if chunk and SpeechEndpoint.pcm_rms(chunk) > 0.01:
                        detected = (time.monotonic() - started) * 1000
                        break
                if not playing.result(timeout=5) or detected is None:
                    raise RuntimeError("virtual audio stimulus was not captured")
                results.append(round(detected, 1))
                time.sleep(0.3)
                capture.flush()
        return {"samples_ms": results, "median_ms": round(statistics.median(results), 1)}
    finally:
        capture.stop()
        stimulus.close()


def measure_ack(module, sink, root, repetitions, new):
    playback = module.PcmPlayback(sink, backend="pulse")
    results = []
    try:
        for _ in range(repetitions):
            started = time.monotonic()
            ok = playback.play_cue("ready") if new else playback.play_wave_file(
                root / "config/assets/wakeup_words_short.wav"
            )
            if not ok:
                raise RuntimeError("acknowledgement playback failed")
            results.append(round((time.monotonic() - started) * 1000, 1))
        return {"samples_ms": results, "median_ms": round(statistics.median(results), 1)}
    finally:
        playback.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    baseline = load_baseline(args.baseline)
    sink = "voice_benchmark_" + str(time.time_ns())
    env = candidate.pulse_environment()
    result = subprocess.run(
        ["pactl", "load-module", "module-null-sink", "sink_name=" + sink,
         "rate=48000", "channels=2"],
        env=env, capture_output=True, text=True, check=True, timeout=5,
    )
    module_id = result.stdout.strip()
    keeper = None
    try:
        report = {
            "scope": "isolated PulseAudio loopback; excludes human/KWS/model latency",
            "baseline_capture": measure_capture(baseline, sink, 100, args.repetitions),
            "candidate_capture": measure_capture(candidate, sink, 40, args.repetitions),
        }
        # 无监听者的 null-sink 会回到约 2 秒的空闲调度周期，污染 drain 计时；
        # 保持 monitor 活跃，使两组在相同的持续运行条件下比较。
        keeper = candidate.PcmCapture(sink + ".monitor", backend="pulse")
        keeper.start()
        if keeper.read(timeout=5) is None:
            raise RuntimeError("monitor did not become ready")
        report["baseline_ack"] = measure_ack(baseline, sink, args.baseline, args.repetitions, False)
        report["candidate_ack"] = measure_ack(candidate, sink, ROOT, args.repetitions, True)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        if keeper:
            keeper.stop()
        subprocess.run(["pactl", "unload-module", module_id], env=env, check=True, timeout=5)


if __name__ == "__main__":
    main()
