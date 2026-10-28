"""Real local WebSocket + Opus round trip, with simulated microphone/speaker.

Requires the service's websockets/opuslib_next dependencies. Never connects to
the deployed service, model or device-control endpoints.
"""
import asyncio
import importlib.util
import json
import queue
import unittest
from unittest.mock import MagicMock, patch

AVAILABLE = all(importlib.util.find_spec(name) for name in ("websockets", "opuslib_next"))


class QueueCapture:
    error = None

    def __init__(self):
        self.queue = queue.Queue()
        self.alive = False

    def start(self):
        self.alive = True

    def stop(self):
        self.alive = False

    def is_alive(self):
        return self.alive

    def flush(self):
        while not self.queue.empty():
            self.queue.get_nowait()

    def read(self, timeout):
        try:
            return self.queue.get(timeout=min(timeout, 0.05))
        except queue.Empty:
            return None


@unittest.skipUnless(AVAILABLE, "requires service WebSocket/Opus environment")
class HeadlessTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_manual_turns_followup_and_idle_expiry(self):
        import websockets
        from opuslib_next import Decoder, Encoder, constants
        from clients.headless_voice_client import (
            HeadlessVoiceClient, STATE_FOLLOWUP, STATE_WAIT_WAKE,
        )
        capture = QueueCapture()
        playback = MagicMock()
        kws = MagicMock()
        kws.accept_pcm.return_value = ""
        messages, received_audio, turns = [], [], []
        encoder = Encoder(24000, 1, constants.APPLICATION_AUDIO)

        async def server(ws):
            decoder = Decoder(16000, 1)
            async for raw in ws:
                if isinstance(raw, bytes):
                    received_audio.append(decoder.decode(raw, 960))
                    continue
                message = json.loads(raw)
                messages.append(message)
                if message["type"] == "hello":
                    await ws.send(json.dumps({"type": "hello", "audio_params": {"sample_rate": 24000}}))
                elif message["type"] == "access":
                    status = "granted" if message["action"] == "acquire" else "released"
                    await ws.send(json.dumps({"type": "access", "status": status}))
                elif message["type"] == "listen" and message["state"] == "stop":
                    turns.append(b"".join(received_audio))
                    received_audio.clear()
                    await ws.send('{"type":"stt","text":"测试语音"}')
                    await ws.send('{"type":"tts","state":"start"}')
                    await ws.send(encoder.encode(b"\0" * 2880, 1440))
                    await ws.send('{"type":"tts","state":"stop"}')

        async def wait_state(client, state):
            async def wait():
                while client.state != state:
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(wait(), 5)

        async def speak():
            for _ in range(15):
                capture.queue.put(b"\x00\x10" * 640)
                await asyncio.sleep(0.04)
            for _ in range(15):
                capture.queue.put(b"\0" * 1280)
                await asyncio.sleep(0.04)

        async with websockets.serve(server, "127.0.0.1", 0) as listener:
            port = listener.sockets[0].getsockname()[1]
            with (
                patch("clients.headless_voice_client.shutil.which", return_value="audio"),
                patch("clients.headless_voice_client.resolve_audio_device", return_value="fake"),
                patch("clients.headless_voice_client.PcmCapture", return_value=capture),
                patch("clients.headless_voice_client.PcmPlayback", return_value=playback),
                patch("clients.headless_voice_client.SherpaKeywordDetector", return_value=kws),
                patch("clients.headless_voice_client.log"),
            ):
                client = HeadlessVoiceClient({"voice_access": {"mode": "hybrid", "headless": {
                    "websocket_url": f"ws://127.0.0.1:{port}/",
                    "followup_timeout_seconds": 1,
                }}})
                client.echo_tail = client.reply_echo_tail = 0
                running = asyncio.create_task(client.run())
                try:
                    await asyncio.wait_for(client.connected.wait(), 5)
                    await client._handle_wake_word("你好小江")
                    await speak()
                    await wait_state(client, STATE_FOLLOWUP)
                    self.assertEqual(len(turns), 1)
                    self.assertEqual(sum(m.get("action") == "acquire" for m in messages), 1)
                    await speak()
                    await wait_state(client, STATE_FOLLOWUP)
                    self.assertEqual(len(turns), 2)
                    self.assertEqual(sum(m.get("action") == "acquire" for m in messages), 2)
                    self.assertTrue(all(len(pcm) > 16000 for pcm in turns))
                    await wait_state(client, STATE_WAIT_WAKE)
                    playback.play_cue.assert_called_with("sleep")
                    self.assertEqual(len(turns), 2)
                    self.assertEqual(playback.write.call_count, 2)
                finally:
                    await client.stop()
                    await asyncio.wait_for(running, 5)


if __name__ == "__main__":
    unittest.main()
