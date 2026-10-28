"""Run the real lifecycle methods without loading models or control plugins."""
import ast
import asyncio
from pathlib import Path
import threading
import types
import unittest
from unittest.mock import AsyncMock, MagicMock


def connection_method(name):
    path = Path(__file__).resolve().parents[1] / "core/connection.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name == "ConnectionHandler")
    method = next(node for node in cls.body if getattr(node, "name", None) == name)
    namespace = {"asyncio": asyncio, "TAG": "test"}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[name]


class ConnectionCloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_marks_stopped_cancels_owner_then_releases_once(self):
        events = []
        conn = types.SimpleNamespace(
            _close_started=False, _abort_lock=asyncio.Lock(), client_abort=False,
            stop_event=threading.Event(), session_id="old", logger=MagicMock(),
            timeout_task=None, websocket=types.SimpleNamespace(close=AsyncMock()),
            tts=None, asr=None, executor=None, clear_queues=MagicMock(),
        )
        def cancel(session_id):
            self.assertTrue(conn.stop_event.is_set())
            self.assertTrue(conn.client_abort)
            events.append(("cancel", session_id))
        async def release(reason):
            events.append(("release", reason))
        conn.llm = types.SimpleNamespace(close_session=cancel, abort=MagicMock())
        conn.release_interaction_turn = release
        close = connection_method("close")
        await close(conn)
        await close(conn)
        self.assertEqual(events, [("cancel", "old"), ("release", "connection_close")])
        conn.llm.abort.assert_not_called()
        conn.websocket.close.assert_awaited_once()

    async def test_queued_chat_does_not_start_after_connection_closes(self):
        conn = types.SimpleNamespace(stop_event=threading.Event(), _chat_inner=MagicMock(),
                                     _chat_lock=threading.Lock())
        conn.stop_event.set()
        self.assertFalse(connection_method("chat")(conn, "你好"))
        conn._chat_inner.assert_not_called()
