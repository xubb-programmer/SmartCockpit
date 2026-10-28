import ast
import asyncio
from pathlib import Path
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from core.handle.abortHandle import handleAbortMessage
from core.handle.textHandler.abortMessageHandler import AbortTextMessageHandler


def listen_handler():
    path = Path(__file__).resolve().parents[1] / "core/handle/textHandler/listenMessageHandler.py"
    tree = ast.parse(path.read_text(encoding="utf8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    method = next(node for node in cls.body if getattr(node, "name", None) == "handle")
    namespace = {"Dict": dict, "Any": object, "TAG": "test",
                 "_wait_audio_queue_drained": AsyncMock()}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["handle"]


class MusicInterruptTests(unittest.IsolatedAsyncioTestCase):
    def connection(self, source="headless", music=True):
        conn = types.SimpleNamespace(
            client_source=source, music_barge_in_active=music, close_after_chat=False,
            is_exiting=False, _abort_lock=asyncio.Lock(), logger=MagicMock(),
            sentence_id="old-song", session_id="test-session", client_abort=False, clear_queues=MagicMock(),
            websocket=types.SimpleNamespace(send=AsyncMock()),
            client_listen_mode="manual", ignore_audio_until=123,
            wait_until_components_ready=AsyncMock(return_value=True),
            reset_audio_states=MagicMock(),
        )
        def clear():
            conn.music_barge_in_active = False
            conn.ignore_audio_until = 800
        conn.clearSpeakStatus = MagicMock(side_effect=clear)
        return conn

    async def test_music_abort_invalidates_old_song_without_global_model_abort(self):
        conn = self.connection()
        with patch("core.handle.abortHandle._abort_inference_providers", new_callable=AsyncMock) as abort:
            await handleAbortMessage(conn)
            abort.assert_not_awaited()
        self.assertTrue(conn.client_abort)
        self.assertNotEqual(conn.sentence_id, "old-song")
        self.assertTrue(conn.headless_music_interrupted)
        self.assertFalse(conn.music_barge_in_active)
        conn.clear_queues.assert_called_once()
        conn.websocket.send.assert_awaited_once()

    async def test_explicit_new_manual_start_accepts_new_onset(self):
        conn = self.connection()
        with patch("core.handle.abortHandle._abort_inference_providers", new_callable=AsyncMock):
            await handleAbortMessage(conn)
        await listen_handler()(None, conn, {"state": "start", "mode": "manual"})
        self.assertEqual(conn.ignore_audio_until, 0)
        self.assertFalse(conn.headless_music_interrupted)

    async def test_web_client_does_not_skip_echo_protection(self):
        conn = self.connection(source="web")
        conn.headless_music_interrupted = True
        await listen_handler()(None, conn, {"state": "start", "mode": "manual"})
        self.assertEqual(conn.ignore_audio_until, 123)

    async def test_regular_speech_abort_keeps_existing_cancellation(self):
        conn = self.connection(music=False)
        with patch("core.handle.abortHandle._abort_inference_providers", new_callable=AsyncMock) as abort:
            await handleAbortMessage(conn)
            abort.assert_awaited_once()
        self.assertEqual(conn.sentence_id, "old-song")

    async def test_song_ending_at_wake_does_not_abort_another_model_owner(self):
        conn = self.connection(music=False)
        with patch("core.handle.abortHandle._abort_inference_providers", new_callable=AsyncMock) as abort:
            await AbortTextMessageHandler().handle(conn, {"reason": "wake_word"})
            abort.assert_not_awaited()
        self.assertTrue(conn.headless_music_interrupted)
        self.assertNotEqual(conn.sentence_id, "old-song")

    async def test_stop_and_pause_do_not_cancel_their_own_confirmation(self):
        path = Path(__file__).resolve().parents[1] / "plugins_func/functions/play_music.py"
        tree = ast.parse(path.read_text(encoding="utf8"))
        function = next(node for node in tree.body if getattr(node, "name", None) == "play_music")
        function.decorator_list = []
        namespace = {"TAG": "test", "Action": types.SimpleNamespace(RESPONSE="response"),
                     "ActionResponse": lambda **kwargs: types.SimpleNamespace(**kwargs)}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
        for action in ("stop", "pause"):
            conn = self.connection(music=False)
            result = namespace["play_music"](conn, action)
            self.assertFalse(conn.client_abort)
            self.assertTrue(result.response)
            conn.clear_queues.assert_called_once()
