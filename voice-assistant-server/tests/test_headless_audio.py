import array
import os
import unittest
import time
from unittest.mock import patch

from clients.audio_endpoint import SpeechEndpoint
from clients.headless_audio import (
    convert_pcm_s16le,
    parse_alsa_cards,
    parse_pulse_sinks,
    pulse_environment,
    resolve_alsa_device,
    resolve_audio_backend,
    resolve_audio_device,
    PcmPlayback,
    PcmCapture,
)


ALSA_OUTPUT = """
card 0: rockchipes8316c [rockchip-es8316-codec], device 0: fe470000.i2s-ES8316 HiFi-0 []
card 1: rockchiphdmi0 [rockchip-hdmi0], device 0: rockchip-hdmi0 i2s-hifi-0 []
card 2: USB [USB Audio], device 0: USB Audio [USB Audio]
"""


class HeadlessAudioDeviceTests(unittest.TestCase):
    @patch("clients.headless_audio.subprocess.Popen")
    @patch("clients.headless_audio.threading.Thread")
    def test_pulse_capture_requests_small_hardware_buffers(self, _thread, popen):
        capture = PcmCapture("source", backend="pulse")
        capture.start()
        command = popen.call_args.args[0]
        self.assertIn("--latency-msec=40", command)
        self.assertIn("--process-time-msec=20", command)

    @patch("clients.headless_audio.resolve_pulse_playback_format", return_value=(48000, 2))
    def test_repeated_cues_reuse_sink_format_but_refresh_after_expiry(self, resolve):
        playback = PcmPlayback("sink", backend="pulse")
        with patch("clients.headless_audio.time.monotonic", side_effect=[10, 11, 41]):
            playback._prepare_pulse_format()
            playback._prepare_pulse_format()
            self.assertEqual(resolve.call_count, 1)
            playback._prepare_pulse_format()
        self.assertEqual(resolve.call_count, 2)

    def test_ready_cue_is_100ms_and_end_cue_is_distinct(self):
        playback = PcmPlayback("sink")
        with patch.object(playback, "_play_pcm", return_value=True) as play:
            playback.play_cue("ready")
            ready = play.call_args.args[0]
            playback.play_cue("sleep")
            sleep = play.call_args.args[0]
        self.assertEqual(len(ready), 24000 * 2 // 10)
        self.assertNotEqual(ready, sleep)
        self.assertEqual(ready[:2], b"\0\0")
        self.assertEqual(ready[-2:], b"\0\0")

    @patch("clients.headless_audio.Path.is_dir", return_value=True)
    @patch("clients.headless_audio.Path.is_socket", return_value=True)
    @patch("clients.headless_audio.os.getuid", return_value=1000, create=True)
    def test_pulse_environment_prefers_shared_system_socket(
        self, _getuid, _is_socket, _is_dir
    ):
        with patch.dict(os.environ, {}, clear=True):
            env = pulse_environment()
        self.assertEqual(env["XDG_RUNTIME_DIR"], "/run/user/1000")
        self.assertEqual(env["PULSE_SERVER"], "unix:/tmp/pulse-socket")

    def test_output_prefers_internal_codec_and_excludes_hdmi(self):
        cards = parse_alsa_cards(ALSA_OUTPUT, "output")
        self.assertEqual(cards[0][2], "plughw:0,0")
        self.assertNotIn("hdmi", " ".join(item[3] for item in cards).lower())

    def test_input_can_prefer_usb_microphone(self):
        cards = parse_alsa_cards(ALSA_OUTPUT, "input")
        self.assertIn(cards[0][2], {"plughw:0,0", "plughw:2,0"})

    def test_explicit_device_is_preserved(self):
        self.assertEqual(resolve_alsa_device("input", "plughw:7,0"), "plughw:7,0")

    @patch("clients.headless_audio.subprocess.run")
    def test_auto_device_uses_discovered_card(self, run):
        run.return_value.stdout = ALSA_OUTPUT
        run.return_value.stderr = ""
        self.assertEqual(resolve_alsa_device("output", "auto"), "plughw:0,0")

    @patch("clients.headless_audio.pulse_available", return_value=True)
    def test_auto_backend_prefers_shared_pulse_audio(self, _available):
        self.assertEqual(resolve_audio_backend("auto"), "pulse")

    @patch("clients.headless_audio.pulse_available", return_value=False)
    def test_auto_backend_falls_back_to_alsa(self, _available):
        self.assertEqual(resolve_audio_backend("auto"), "alsa")

    def test_pulse_defaults_use_shared_source_and_sink(self):
        self.assertEqual(
            resolve_audio_device("input", "auto", "pulse"), "@DEFAULT_SOURCE@"
        )
        self.assertEqual(
            resolve_audio_device("output", "auto", "pulse"), "@DEFAULT_SINK@"
        )

    def test_pulse_sink_format_is_parsed(self):
        sinks = parse_pulse_sinks(
            "0\talsa_output.platform-es8388\tmodule-alsa-card.c\t"
            "s16le 2ch 44100Hz\tRUNNING\n"
        )
        self.assertEqual(sinks, [("alsa_output.platform-es8388", 44100, 2)])

    def test_mono_pcm_is_resampled_and_duplicated_to_stereo(self):
        source = array.array("h", [1000, -1000] * 120).tobytes()
        converted, _state = convert_pcm_s16le(
            source, 24000, 1, 48000, 2
        )
        samples = array.array("h")
        samples.frombytes(converted)
        self.assertGreater(len(samples), 900)
        self.assertEqual(samples[0], samples[1])
        self.assertEqual(samples[2], samples[3])


class HeadlessSpeechEndpointTests(unittest.TestCase):
    def test_zero_hold_accepts_first_chunk_including_clock_origin(self):
        endpoint = SpeechEndpoint(speech_start_hold_seconds=0)
        endpoint.process_pcm(b"\x00\x10" * 640, 0.0, 0.0)
        self.assertTrue(endpoint.heard_voice)
        self.assertEqual(endpoint.first_voice_at, 0.0)

    def test_onset_does_not_pollute_noise_floor(self):
        endpoint = SpeechEndpoint(speech_start_hold_seconds=0.04)
        endpoint.noise_samples.extend([0.001] * 20)
        endpoint.process_pcm(b"\x00\x10" * 640, 1.0, 1.0)
        endpoint.process_pcm(b"\x00\x10" * 640, 1.05, 1.0)
        self.assertEqual(len(endpoint.noise_samples), 20)
        self.assertTrue(endpoint.heard_voice)

    def test_single_impulse_is_not_speech(self):
        endpoint = SpeechEndpoint(speech_start_hold_seconds=0.04)
        endpoint.process_pcm(b"\x00\x10" * 640, 1.0, 1.0)
        endpoint.process_pcm(b"\0" * 1280, 1.04, 1.0)
        self.assertFalse(endpoint.heard_voice)

    def test_new_endpoint_keeps_short_pauses_but_finishes_at_half_second(self):
        endpoint = SpeechEndpoint(speech_start_hold_seconds=0, speech_min_seconds=0.35, speech_end_seconds=0.5)
        endpoint.process_pcm(b"\x00\x10" * 640, 1.0, 1.0)
        self.assertFalse(endpoint.process_pcm(b"\0" * 1280, 1.4, 1.0)[3])
        self.assertTrue(endpoint.process_pcm(b"\0" * 1280, 1.51, 1.0)[3])

    @staticmethod
    def _endpoint():
        endpoint = SpeechEndpoint(
            speech_start_hold_seconds=0.0,
            speech_min_seconds=0.8,
            speech_end_seconds=1.2,
        )
        endpoint.noise_samples.extend([0.001] * 20)
        return endpoint

    def test_quiet_background_does_not_raise_voice_threshold_unbounded(self):
        endpoint = self._endpoint()
        high, low = endpoint.thresholds()
        self.assertAlmostEqual(high, 0.006, places=4)
        self.assertAlmostEqual(low, 0.003, places=4)

    def test_short_pause_after_voice_is_not_immediate_end(self):
        endpoint = self._endpoint()
        now = time.monotonic()
        endpoint.heard_voice = True
        endpoint.last_voice_at = now - 0.5
        pcm = b"\x00\x00" * 320
        _rms, _high, _low, should_finish = endpoint.process_pcm(
            pcm, now, now - 1.0
        )
        self.assertFalse(should_finish)

    def test_voice_onset_then_silence_finishes_after_minimum_duration(self):
        endpoint = self._endpoint()
        speech = (328).to_bytes(2, "little", signed=True) * 320
        silence = b"\x00\x00" * 320
        now = time.monotonic()
        endpoint.process_pcm(speech, now, now - 1.0)
        endpoint.process_pcm(speech, now + 0.01, now - 1.0)
        self.assertTrue(endpoint.heard_voice)
        endpoint.last_voice_at = now - 1.3
        _rms, _high, _low, should_finish = endpoint.process_pcm(
            silence, now, now - 1.0
        )
        self.assertTrue(should_finish)


if __name__ == "__main__":
    unittest.main()
