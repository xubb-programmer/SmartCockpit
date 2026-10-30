import io
import json
from pathlib import Path
import tempfile
import types
import unittest

from clients.headless_audio import PcmCapture
from clients.headless_status import HeadlessStatus


class HeadlessStatusTests(unittest.TestCase):
    def test_capture_timestamp_advances_for_silent_audio(self):
        capture = PcmCapture("test", 16000, chunk_ms=40)
        capture._process = types.SimpleNamespace(
            stdout=io.BytesIO(bytes(1280)), stderr=io.BytesIO(), poll=lambda: 0,
        )
        capture._reader()
        self.assertGreater(capture.last_chunk_at, 0)
        self.assertEqual(capture.read(.01), bytes(1280))

    def test_publish_and_clear_only_own_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            status = HeadlessStatus(path)
            capture = types.SimpleNamespace(is_alive=lambda: True, last_chunk_at=12.0)
            status.publish(capture, True, "wait_wake")
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["pid"], status.pid)
            self.assertTrue(payload["capture_alive"])
            self.assertEqual(payload["last_audio_monotonic"], 12.0)
            self.assertEqual(len(list(Path(directory).iterdir())), 1)
            payload["pid"] += 1
            path.write_text(json.dumps(payload), encoding="utf-8")
            status.clear()
            self.assertTrue(path.exists())
            status.publish(capture, False, "stopping")
            status.clear()
            self.assertFalse(path.exists())
