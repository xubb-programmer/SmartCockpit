import json
import os
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path
import sys

SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from core.utils import npu_priority


class NpuPriorityTests(unittest.TestCase):
    def test_signal_text_priority_writes_expiring_atomic_lease(self):
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(
            os.environ,
            {"VOICE_ASSISTANT_NPU_PRIORITY_DIR": temp_dir},
        ):
            path = npu_priority.signal_text_priority("session:one", ttl_seconds=2.0)
            with open(path, encoding="utf-8") as file_obj:
                payload = json.load(file_obj)

            self.assertTrue(path.endswith("speech-session-one.priority"))
            self.assertEqual(payload["owner"], "session-one")
            self.assertGreater(payload["expires_at"], time.time())
            self.assertFalse(
                [name for name in os.listdir(temp_dir) if name.startswith(".priority-")]
            )


if __name__ == "__main__":
    unittest.main()
