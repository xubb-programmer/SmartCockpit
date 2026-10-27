from collections import deque

import numpy as np


class SpeechEndpoint:
    """Adaptive local endpoint detector for the headless microphone stream."""

    def __init__(
        self,
        speech_rms_threshold: float = 0.006,
        speech_rms_threshold_low: float = 0.003,
        speech_noise_multiplier: float = 2.0,
        speech_noise_low_multiplier: float = 1.35,
        speech_start_hold_seconds: float = 0.04,
        speech_min_seconds: float = 0.8,
        speech_end_seconds: float = 1.2,
    ):
        self.speech_rms_threshold = max(0.0, speech_rms_threshold)
        self.speech_rms_threshold_low = max(0.0, speech_rms_threshold_low)
        self.speech_noise_multiplier = max(1.0, speech_noise_multiplier)
        self.speech_noise_low_multiplier = max(1.0, speech_noise_low_multiplier)
        self.speech_start_hold_seconds = max(0.0, speech_start_hold_seconds)
        self.speech_min_seconds = max(0.0, speech_min_seconds)
        self.speech_end_seconds = max(0.1, speech_end_seconds)
        self.noise_samples = deque(maxlen=80)
        self.heard_voice = False
        self.voice_candidate_at = 0.0
        self.last_voice_at = 0.0

    @staticmethod
    def pcm_rms(pcm: bytes) -> float:
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        return float(np.sqrt(np.mean(np.square(samples)))) if samples.size else 0.0

    @property
    def noise_floor(self) -> float:
        if not self.noise_samples:
            return 0.0
        return float(np.percentile(self.noise_samples, 20))

    def observe_noise(self, rms: float) -> None:
        if rms > 0.0:
            self.noise_samples.append(rms)

    def observe_pcm(self, pcm: bytes) -> float:
        rms = self.pcm_rms(pcm)
        self.observe_noise(rms)
        return rms

    def thresholds(self) -> tuple[float, float]:
        high = max(
            self.speech_rms_threshold,
            self.noise_floor * self.speech_noise_multiplier,
        )
        low = max(
            self.speech_rms_threshold_low,
            self.noise_floor * self.speech_noise_low_multiplier,
        )
        return high, min(low, high)

    def reset_command(self) -> None:
        self.heard_voice = False
        self.voice_candidate_at = 0.0
        self.last_voice_at = 0.0

    def process_pcm(
        self, pcm: bytes, now: float, listen_started_at: float
    ) -> tuple[float, float, float, bool]:
        rms = self.pcm_rms(pcm)
        high, low = self.thresholds()

        if not self.heard_voice:
            self.observe_noise(rms)
            if rms >= high:
                if not self.voice_candidate_at:
                    self.voice_candidate_at = now
                elif now - self.voice_candidate_at >= self.speech_start_hold_seconds:
                    self.heard_voice = True
                    self.last_voice_at = now
            else:
                self.voice_candidate_at = 0.0
        elif rms >= low:
            self.last_voice_at = now

        should_finish = (
            self.heard_voice
            and now - listen_started_at >= self.speech_min_seconds
            and now - self.last_voice_at >= self.speech_end_seconds
        )
        return rms, high, low, should_finish
