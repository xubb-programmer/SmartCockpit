import asyncio
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from core.interaction_access import (
    InteractionCoordinator,
    get_access_mode,
    normalize_access_mode,
    source_allowed,
)
class InteractionAccessConfigTests(unittest.TestCase):
    def test_default_mode_preserves_web_behavior(self):
        self.assertEqual(get_access_mode({}), "web")
        self.assertTrue(source_allowed("web", "web"))
        self.assertFalse(source_allowed("web", "headless"))

    def test_all_supported_modes(self):
        self.assertEqual(normalize_access_mode("WAKEUP"), "wakeup")
        self.assertEqual(normalize_access_mode("hybrid"), "hybrid")
        self.assertEqual(normalize_access_mode("invalid"), "web")
        self.assertTrue(source_allowed("wakeup", "headless"))
        self.assertFalse(source_allowed("wakeup", "web"))
        self.assertTrue(source_allowed("hybrid", "web"))
        self.assertTrue(source_allowed("hybrid", "headless"))

    def test_environment_override_is_shared_by_all_components(self):
        with patch.dict(os.environ, {"VOICE_ACCESS_MODE": "hybrid"}):
            self.assertEqual(
                "hybrid", get_access_mode({"voice_access": {"mode": "web"}})
            )


class InteractionCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.coordinator = InteractionCoordinator(
            {
                "voice_access": {
                    "mode": "hybrid",
                    "arbitration": {"max_turn_seconds": 30},
                }
            }
        )

    async def test_first_session_wins_and_same_session_renews(self):
        acquired, lease = await self.coordinator.acquire(
            "session-web", "web", "speech"
        )
        self.assertTrue(acquired)
        self.assertEqual(lease.source, "web")

        renewed, renewed_lease = await self.coordinator.acquire(
            "session-web", "web", "tts"
        )
        self.assertTrue(renewed)
        self.assertEqual(renewed_lease.session_id, "session-web")

        acquired, owner = await self.coordinator.acquire(
            "session-headless", "headless", "wake_word"
        )
        self.assertFalse(acquired)
        self.assertEqual(owner.session_id, "session-web")

    async def test_release_allows_other_source(self):
        await self.coordinator.acquire("session-web", "web", "speech")
        self.assertTrue(await self.coordinator.release("session-web", "done"))

        acquired, lease = await self.coordinator.acquire(
            "session-headless", "headless", "wake_word"
        )
        self.assertTrue(acquired)
        self.assertEqual(lease.source, "headless")

    async def test_stale_lease_is_recovered(self):
        await self.coordinator.acquire("session-web", "web", "speech")
        self.coordinator._lease.expires_at = 0
        acquired, lease = await self.coordinator.acquire(
            "session-headless", "headless", "wake_word"
        )
        self.assertTrue(acquired)
        self.assertEqual(lease.session_id, "session-headless")

    async def test_single_source_mode_has_no_global_lease(self):
        coordinator = InteractionCoordinator({"voice_access": {"mode": "web"}})
        acquired, lease = await coordinator.acquire("one", "web", "speech")
        self.assertTrue(acquired)
        self.assertIsNone(lease)
        denied, _ = await coordinator.acquire("two", "headless", "speech")
        self.assertFalse(denied)


class HeadlessListenProtocolTests(unittest.TestCase):
    def test_headless_uses_single_manual_audio_submission(self):
        project_dir = Path(__file__).resolve().parents[1]
        client_source = (project_dir / "clients/headless_voice_client.py").read_text(
            encoding="utf-8"
        )
        handler_source = (
            project_dir / "core/handle/textHandler/listenMessageHandler.py"
        ).read_text(encoding="utf-8")

        self.assertIn(
            '{"type": "listen", "state": "start", "mode": "manual"}',
            client_source,
        )
        self.assertIn(
            '{"type": "listen", "state": "stop", "mode": "manual"}',
            client_source,
        )
        self.assertIn('msg_json["state"] == "cancel"', handler_source)
        self.assertIn('release_interaction_turn("listen_cancel")', handler_source)


if __name__ == "__main__":
    unittest.main()
