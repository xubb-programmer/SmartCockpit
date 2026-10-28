import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from inference_cancellation import InferenceCancellation


class CancellationTests(unittest.TestCase):
    def test_close_cancels_own_active_request(self):
        guard = InferenceCancellation()
        self.assertTrue(guard.start("chat", "old"))
        self.assertEqual(guard.cancel("old", close_session=True), "chat")
        self.assertTrue(guard.cancelled.is_set())

    def test_late_close_does_not_cancel_new_owner(self):
        guard = InferenceCancellation()
        guard.start("chat", "new")
        self.assertIsNone(guard.cancel("old", close_session=True))
        self.assertFalse(guard.cancelled.is_set())

    def test_close_rejects_queued_request_but_allows_new_connection(self):
        guard = InferenceCancellation()
        guard.cancel("closed", close_session=True)
        self.assertFalse(guard.start("chat", "closed"))
        self.assertTrue(guard.start("chat", "new"))
        self.assertFalse(guard.cancelled.is_set())

    def test_session_close_does_not_cancel_unrelated_vision(self):
        guard = InferenceCancellation()
        guard.start("vision")
        self.assertIsNone(guard.cancel("old", close_session=True))
        self.assertFalse(guard.cancelled.is_set())

    def test_legacy_explicit_abort_and_next_request(self):
        guard = InferenceCancellation()
        guard.start("chat", "one")
        self.assertEqual(guard.cancel(), "chat")
        guard.finish()
        self.assertTrue(guard.start("chat", "one"))
        self.assertFalse(guard.cancelled.is_set())

    def test_closed_session_records_expire(self):
        guard = InferenceCancellation(retention_seconds=300)
        with patch("inference_cancellation.time.monotonic", return_value=100):
            guard.cancel("old", close_session=True)
        with patch("inference_cancellation.time.monotonic", return_value=401):
            self.assertTrue(guard.start("chat", "old"))


if __name__ == "__main__":
    unittest.main()
