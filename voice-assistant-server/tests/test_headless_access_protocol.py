import types
import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from core.handle.textHandler.accessMessageHandler import AccessTextMessageHandler


class HeadlessAccessProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_reports_initialization_without_reserving_a_turn(self):
        conn = self.connection()
        conn.components_ready_event = asyncio.Event()
        conn.components_init_error = None
        conn.tts = conn.asr = object()
        conn.websocket = types.SimpleNamespace(send=AsyncMock())
        handler = AccessTextMessageHandler()
        await handler.handle(conn, {"action": "health"})
        self.assertFalse(json.loads(conn.websocket.send.call_args.args[0])["ready"])
        conn.components_ready_event.set()
        await handler.handle(conn, {"action": "health"})
        self.assertTrue(json.loads(conn.websocket.send.call_args.args[0])["ready"])
        conn.components_init_error = "tts failed"
        await handler.handle(conn, {"action": "health"})
        self.assertFalse(json.loads(conn.websocket.send.call_args.args[0])["ready"])
        conn.acquire_interaction_turn.assert_not_awaited()

    def connection(self, ready=True, acquired=True):
        return types.SimpleNamespace(
            wait_until_components_ready=AsyncMock(return_value=ready),
            acquire_interaction_turn=AsyncMock(return_value=acquired),
            send_access_status=AsyncMock(), last_turn_busy_owner="web",
        )

    async def test_initializing_does_not_reserve_or_claim_ready(self):
        conn = self.connection(ready=False)
        await AccessTextMessageHandler().handle(conn, {"action": "acquire"})
        conn.acquire_interaction_turn.assert_not_awaited()
        self.assertEqual(conn.send_access_status.call_args.args[0], "initializing")

    async def test_ready_connection_reserves_before_granting(self):
        conn = self.connection()
        await AccessTextMessageHandler().handle(conn, {"action": "acquire"})
        conn.acquire_interaction_turn.assert_awaited_once()
        self.assertEqual(conn.send_access_status.call_args.args[0], "granted")

    async def test_web_owner_causes_busy(self):
        conn = self.connection(acquired=False)
        await AccessTextMessageHandler().handle(conn, {"action": "acquire"})
        self.assertEqual(conn.send_access_status.call_args.args[0], "busy")
        self.assertEqual(conn.send_access_status.call_args.kwargs["owner"], "web")
