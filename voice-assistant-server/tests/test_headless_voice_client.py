"""Exercise real headless transitions with fake hardware/transport, without RK3588."""
import asyncio
import json
import time
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from clients.headless_voice_client import (
    HeadlessVoiceClient, STATE_WAIT_WAKE, STATE_LISTENING, STATE_PROCESSING,
    STATE_PLAYING, STATE_DRAINING, STATE_FOLLOWUP,
)


class FakeEncoder:
    def __init__(self, *args):
        self.frames = []

    def reset_state(self):
        self.frames.clear()

    def encode(self, frame, samples):
        self.frames.append(frame)
        return b"opus:" + frame


class HeadlessClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        logger_patch = patch("clients.headless_voice_client.log")
        logger_patch.start()
        self.addCleanup(logger_patch.stop)
        opus = types.SimpleNamespace(
            Encoder=FakeEncoder, Decoder=MagicMock(),
            constants=types.SimpleNamespace(APPLICATION_AUDIO=1),
        )
        with (
            patch.dict("sys.modules", {"opuslib_next": opus}),
            patch("clients.headless_voice_client.shutil.which", return_value="audio"),
            patch("clients.headless_voice_client.resolve_audio_device", return_value="default"),
            patch("clients.headless_voice_client.PcmCapture"),
            patch("clients.headless_voice_client.PcmPlayback"),
            patch("clients.headless_voice_client.SherpaKeywordDetector"),
            patch("clients.headless_voice_client.log"),
        ):
            self.client = HeadlessVoiceClient({"voice_access": {"mode": "hybrid"}})
        self.client.echo_tail = 0
        self.client.reply_echo_tail = 0
        self.client.connected.set()
        self.client.websocket = types.SimpleNamespace(send=AsyncMock(), close=AsyncMock())
        self.client._acquire_turn = AsyncMock(return_value="granted")
        self.client.playback.play_cue.return_value = True
        self.client.capture.is_alive.return_value = True
        self.client.speech_endpoint.noise_samples.extend([0.001] * 40)

    async def asyncTearDown(self):
        task = self.client._completion_task
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def json_messages(self):
        return [json.loads(call.args[0]) for call in self.client.websocket.send.call_args_list
                if isinstance(call.args[0], str)]

    async def wake(self):
        await self.client._handle_wake_word("你好小江")

    async def followup(self):
        self.client.state = STATE_PLAYING
        await self.client._handle_tts({"state": "stop"})
        task = self.client._completion_task
        await task

    async def test_wake_uses_short_cue_and_single_manual_start(self):
        await self.wake()
        self.assertEqual(self.client.state, STATE_LISTENING)
        self.client.playback.play_wave_file.assert_not_called()
        self.client.playback.play_cue.assert_called_once_with("ready")
        self.assertEqual(self.json_messages(), [
            {"type": "listen", "state": "start", "mode": "manual"},
        ])

    async def test_voice_ack_remains_configurable(self):
        self.client.acknowledgement_mode = "voice"
        await self.wake()
        self.client.playback.play_wave_file.assert_called_once()
        self.client.playback.play_cue.assert_not_called()

    async def test_initial_silence_cancels_without_asr_and_signals_sleep(self):
        await self.wake()
        self.client._listen_started_at = time.monotonic() - 9
        await self.client._check_state_timeout()
        self.assertEqual(self.client.state, STATE_WAIT_WAKE)
        self.assertIn({"type": "listen", "state": "cancel", "mode": "manual"}, self.json_messages())
        self.assertFalse(any(m.get("state") == "stop" for m in self.json_messages()))
        self.client.playback.play_cue.assert_called_with("sleep")

    async def test_speaking_at_end_of_idle_window_is_not_cut_off(self):
        await self.wake()
        now = time.monotonic()
        self.client._listen_started_at = now - 13
        endpoint = self.client.speech_endpoint
        endpoint.heard_voice = True
        endpoint.first_voice_at = now - 2
        await self.client._check_state_timeout()
        self.assertEqual(self.client.state, STATE_LISTENING)

    async def test_max_utterance_duration_still_submits(self):
        await self.wake()
        endpoint = self.client.speech_endpoint
        endpoint.heard_voice = True
        endpoint.first_voice_at = time.monotonic() - 13
        await self.client._check_state_timeout()
        self.assertEqual(self.client.state, STATE_PROCESSING)
        self.assertEqual(self.json_messages()[-1]["state"], "stop")

    async def test_finish_pads_last_partial_opus_frame_and_submits_once(self):
        await self.wake()
        tail = b"\x11\x22" * 100
        self.client._encoder_buffer.extend(tail)
        await self.client._finish_listening("test")
        await self.client._finish_listening("duplicate")
        self.assertEqual(len(self.client._encoder.frames), 1)
        frame = self.client._encoder.frames[0]
        self.assertEqual(frame[:len(tail)], tail)
        self.assertEqual(len(frame), self.client.frame_bytes)
        self.assertEqual(sum(m.get("state") == "stop" for m in self.json_messages()), 1)

    async def test_followup_waits_for_playback_drain_before_opening_window(self):
        import threading
        gate = threading.Event()
        self.client.playback.stop_stream.side_effect = lambda: gate.wait(2)
        self.client.state = STATE_PLAYING
        await self.client._handle_tts({"state": "stop"})
        self.assertEqual(self.client.state, STATE_DRAINING)
        gate.set()
        await self.client._completion_task
        self.assertEqual(self.client.state, STATE_FOLLOWUP)

    async def test_followup_silence_does_not_reserve_or_transmit(self):
        await self.followup()
        await self.client._stream_followup_audio(b"\0" * 1280)
        self.client._acquire_turn.assert_not_awaited()
        self.client.websocket.send.assert_not_awaited()

    async def test_followup_expires_with_end_cue(self):
        await self.followup()
        self.client._followup_started_at = time.monotonic() - 9
        await self.client._check_state_timeout()
        self.assertEqual(self.client.state, STATE_WAIT_WAKE)
        self.client.playback.play_cue.assert_called_with("sleep")
        self.client._acquire_turn.assert_not_awaited()

    async def test_followup_reacquires_and_keeps_onset_preroll(self):
        await self.followup()
        endpoint = self.client.speech_endpoint
        endpoint.speech_start_hold_seconds = 0
        silence = b"\0" * 1280
        speech = (1000).to_bytes(2, "little", signed=True) * 640
        await self.client._stream_followup_audio(silence)
        await self.client._stream_followup_audio(speech)
        self.assertEqual(self.client.state, STATE_LISTENING)
        self.client._acquire_turn.assert_awaited_once()
        sent = b"".join(self.client._encoder.frames) + bytes(self.client._encoder_buffer)
        self.assertEqual(sent, silence + speech)
        self.assertTrue(endpoint.heard_voice)

    async def test_busy_followup_discards_audio_and_does_not_queue_command(self):
        await self.followup()
        self.client._acquire_turn.return_value = "busy"
        self.client.speech_endpoint.speech_start_hold_seconds = 0
        await self.client._stream_followup_audio(b"\xff\x0f" * 640)
        self.assertEqual(self.client.state, STATE_WAIT_WAKE)
        self.assertFalse(any(isinstance(c.args[0], bytes) for c in self.client.websocket.send.call_args_list))
        self.assertFalse(any(m.get("state") == "start" for m in self.json_messages()))
        self.client.playback.play_cue.assert_called_with("error")

    async def test_no_followup_mode_requires_wake_and_cues_end(self):
        self.client.followup_timeout = 0
        await self.followup()
        self.assertEqual(self.client.state, STATE_WAIT_WAKE)
        self.client.playback.play_cue.assert_called_with("sleep")

    async def test_explicit_goodbye_does_not_reopen_followup(self):
        self.client.state = STATE_PROCESSING
        await self.client._handle_message('{"type":"stt","text":"再见"}')
        await self.followup()
        self.assertEqual(self.client.state, STATE_WAIT_WAKE)
        self.client.playback.play_cue.assert_called_with("sleep")

    async def test_tts_start_without_audio_is_covered_by_timeout(self):
        self.client.state = STATE_PROCESSING
        await self.client._handle_tts({"state": "start"})
        self.client._response_activity_at = time.monotonic() - 121
        await self.client._check_state_timeout()
        self.assertEqual(self.client.state, STATE_WAIT_WAKE)
        self.client.websocket.close.assert_awaited_once()
        self.client.playback.play_cue.assert_called_with("error")

    async def test_active_audio_refreshes_stall_timeout(self):
        self.client.state = STATE_PROCESSING
        await self.client._handle_tts({"state": "start"})
        self.client._response_activity_at = time.monotonic() - 121
        self.client._decoder.decode.return_value = b"\0" * 100
        await self.client._handle_message(b"encoded audio")
        await self.client._check_state_timeout()
        self.assertEqual(self.client.state, STATE_PLAYING)
        self.client.websocket.close.assert_not_awaited()

    async def test_late_tts_and_audio_cannot_reopen_expired_turn(self):
        await self.client._handle_tts({"state": "start"})
        await self.client._handle_message(b"old audio")
        await self.client._handle_tts({"state": "stop"})
        self.assertEqual(self.client.state, STATE_WAIT_WAKE)
        self.client.playback.start_stream.assert_not_called()
        self.client.playback.write.assert_not_called()

    async def test_empty_asr_exits_without_waiting_response_timeout(self):
        self.client.state = STATE_PROCESSING
        await self.client._handle_message(json.dumps({"type": "server", "status": "empty_asr"}))
        task = self.client._completion_task
        await task
        self.assertEqual(self.client.state, STATE_WAIT_WAKE)
        self.client.playback.play_cue.assert_called_with("error")

    async def test_access_release_ack_does_not_resolve_acquire(self):
        future = asyncio.get_running_loop().create_future()
        self.client._access_future = future
        await self.client._handle_message('{"type":"access","status":"released"}')
        self.assertFalse(future.done())
        await self.client._handle_message('{"type":"access","status":"granted"}')
        self.assertEqual(future.result(), "granted")

    async def test_disconnect_during_ack_never_starts_recording(self):
        def disconnect(_cue):
            self.client.connected.clear()
            self.client._epoch += 1
        self.client.playback.play_cue.side_effect = disconnect
        await self.wake()
        self.assertEqual(self.json_messages(), [])

    async def test_invalidated_completion_does_not_open_followup(self):
        old_epoch = self.client._epoch
        self.client._invalidate_turn()
        await self.client._after_reply(old_epoch)
        self.assertEqual(self.client.state, STATE_WAIT_WAKE)
        self.client.playback.play_cue.assert_not_called()

    async def test_failed_stop_send_returns_to_wake(self):
        await self.wake()
        self.client.websocket.send.side_effect = ConnectionError("offline")
        await self.client._finish_listening("test")
        self.assertEqual(self.client.state, STATE_WAIT_WAKE)


if __name__ == "__main__":
    unittest.main()
