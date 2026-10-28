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
            self.client = HeadlessVoiceClient({"voice_access": {"mode": "hybrid", "headless": {"speech_vad_enabled": False}}})
        self.client.echo_tail = 0
        self.client.tone_echo_tail = 0
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

    async def test_music_start_enables_kws_without_uploading_music(self):
        self.client.state = STATE_PLAYING
        await self.client._handle_tts({"state": "music_start"})
        self.client.kws.accept_pcm.return_value = ""
        await self.client._stream_music_audio(b"\x00\x10" * 640)
        self.assertTrue(self.client._music_active)
        self.client.kws.accept_pcm.assert_called_once()
        self.client.websocket.send.assert_not_awaited()

    async def test_music_wake_stops_playback_then_starts_one_new_recording(self):
        self.client.state = STATE_PLAYING
        await self.client._handle_tts({"state": "music_start"})
        self.client.kws.accept_pcm.return_value = "你好小江"
        async def acknowledge(raw):
            if isinstance(raw, str) and json.loads(raw).get("type") == "abort":
                self.assertEqual(self.client.state, "reserving")
                await self.client._handle_message(b"late old music")
                await self.client._handle_tts({"state": "stop"})
        self.client.websocket.send.side_effect = acknowledge
        await self.client._stream_music_audio(b"\x00\x10" * 640)
        self.assertEqual(self.client.state, STATE_LISTENING)
        self.assertFalse(self.client._music_active)
        self.client.playback.stop_stream.assert_called_with(False)
        self.client.playback.write.assert_not_called()
        self.client.playback.play_cue.assert_called_once_with("ready")
        self.assertEqual([m.get("type") for m in self.json_messages()], ["abort", "listen"])
        self.assertIsNone(self.client._completion_task)

    async def test_music_flag_does_not_open_idle_or_normal_speech(self):
        await self.client._handle_tts({"state": "music_start"})
        self.assertFalse(self.client._music_active)
        self.client.state = STATE_PROCESSING
        await self.client._handle_tts({"state": "start"})
        self.assertFalse(self.client._music_active)

    async def test_music_end_uses_normal_followup(self):
        self.client.state = STATE_PLAYING
        await self.client._handle_tts({"state": "music_start"})
        await self.client._handle_tts({"state": "stop"})
        await self.client._completion_task
        self.assertFalse(self.client._music_active)
        self.assertEqual(self.client.state, STATE_FOLLOWUP)

    async def test_music_interrupt_send_failure_closes_connection(self):
        self.client.state = STATE_PLAYING
        self.client._music_active = True
        self.client.kws.accept_pcm.return_value = "你好小江"
        self.client.websocket.send.side_effect = ConnectionError("offline")
        await self.client._stream_music_audio(b"\0" * 1280)
        self.assertEqual(self.client.state, STATE_WAIT_WAKE)
        self.client.websocket.close.assert_awaited_once()
        self.client.playback.play_cue.assert_called_once_with("error")

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

    async def test_followup_onset_before_deadline_can_finish_confirmation(self):
        await self.followup()
        self.client._followup_started_at = 100
        speech = b"\xff\x0f" * 640
        with patch("clients.headless_voice_client.time", types.SimpleNamespace(monotonic=lambda: 107.97)):
            await self.client._stream_followup_audio(speech)
        with patch("clients.headless_voice_client.time", types.SimpleNamespace(monotonic=lambda: 108.02)):
            await self.client._check_state_timeout()
            self.assertEqual(self.client.state, STATE_FOLLOWUP)
            await self.client._stream_followup_audio(speech)
        self.assertEqual(self.client.state, STATE_LISTENING)
        self.client._acquire_turn.assert_awaited_once()

    async def test_followup_new_onset_after_deadline_is_rejected(self):
        await self.followup()
        self.client._followup_started_at = 100
        with patch("clients.headless_voice_client.time", types.SimpleNamespace(monotonic=lambda: 108.02)):
            await self.client._stream_followup_audio(b"\xff\x0f" * 640)
        self.assertEqual(self.client.state, STATE_WAIT_WAKE)
        self.client._acquire_turn.assert_not_awaited()

    async def test_stale_onset_cannot_extend_window_indefinitely(self):
        await self.followup()
        self.client._followup_started_at = 100
        self.client.speech_endpoint.voice_candidate_at = 107.97
        with patch("clients.headless_voice_client.time", types.SimpleNamespace(monotonic=lambda: 108.2)):
            await self.client._check_state_timeout()
        self.assertEqual(self.client.state, STATE_WAIT_WAKE)

    async def test_initial_window_allows_pending_onset_confirmation(self):
        await self.wake()
        self.client._listen_started_at = 100
        self.client.speech_endpoint.voice_candidate_at = 107.97
        with patch("clients.headless_voice_client.time", types.SimpleNamespace(monotonic=lambda: 108.02)):
            await self.client._check_state_timeout()
        self.assertEqual(self.client.state, STATE_LISTENING)

    async def test_failed_audio_upload_signals_error(self):
        await self.wake()
        self.client.playback.play_cue.reset_mock()
        self.client.websocket.send.side_effect = ConnectionError("offline")
        self.assertFalse(await self.client._encode_and_send(b"\0" * self.client.frame_bytes))
        self.assertEqual(self.client.state, STATE_WAIT_WAKE)
        self.client.playback.play_cue.assert_called_once_with("error")

    async def test_non_speech_background_cannot_reacquire_followup(self):
        await self.followup()
        self.client.speech_vad = types.SimpleNamespace(probability=lambda _: .05)
        for _ in range(10):
            await self.client._stream_followup_audio(b"\x00\x10" * 640)
        self.assertEqual(self.client.state, STATE_FOLLOWUP)
        self.client._acquire_turn.assert_not_awaited()
        self.assertFalse(self.client.speech_endpoint.heard_voice)

    async def test_lingering_reply_voice_does_not_open_followup(self):
        self.client.speech_vad = types.SimpleNamespace(probability=lambda _: .95)
        await self.client._speech_activity(b"\x00\x10" * 640)
        await self.followup()
        self.assertEqual(self.client.state, STATE_WAIT_WAKE)
        self.client.playback.play_cue.assert_called_once_with("sleep")

    async def test_fresh_quiet_gap_allows_followup(self):
        self.client.speech_vad = types.SimpleNamespace(probability=lambda _: .05)
        await self.client._speech_activity(b"\0" * 1280)
        self.client._vad_quiet_since -= .3
        await self.followup()
        self.assertEqual(self.client.state, STATE_FOLLOWUP)

    async def test_playback_drain_counts_toward_server_echo_wait(self):
        self.client._tts_stopped_at = 100
        self.client.reply_echo_tail = .8
        self.client.echo_tail = .2
        with (patch("clients.headless_voice_client.time", types.SimpleNamespace(monotonic=lambda: 100.6)),
              patch("clients.headless_voice_client.asyncio.sleep", new_callable=AsyncMock) as sleep):
            await self.client._after_reply(self.client._epoch)
        self.assertAlmostEqual(sleep.await_args_list[0].args[0], .2)
        self.assertEqual(self.client.state, STATE_FOLLOWUP)

    async def test_goodbye_socket_close_finishes_sleep_cue_once(self):
        client = self.client
        client.state = STATE_PROCESSING
        client.reply_echo_tail = 0.05
        websocket = client.websocket
        async def incoming():
            yield '{"type":"stt","text":"再见"}'
            yield '{"type":"tts","state":"stop"}'
        socket = MagicMock()
        socket.send = websocket.send
        socket.__aiter__ = lambda _: incoming()
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=socket)
        context.__aexit__ = AsyncMock(return_value=False)
        with patch.dict("sys.modules", {"websockets": types.SimpleNamespace(connect=lambda *a, **k: context)}):
            running = asyncio.create_task(client._connection_loop())
            try:
                async def finished():
                    while client.state != STATE_WAIT_WAKE:
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(finished(), 2)
                client.playback.play_cue.assert_called_once_with("sleep")
            finally:
                client.stopping.set()
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
