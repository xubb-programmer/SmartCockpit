"""Small runtime heartbeat for startup checks; silence is valid captured audio."""
import json
import os
from pathlib import Path
import tempfile
import time


def default_status_path():
    return Path(os.environ.get(
        "VOICE_ASSISTANT_HEADLESS_STATUS_FILE",
        str(Path(tempfile.gettempdir()) / "headless_voice_status.json"),
    ))


class HeadlessStatus:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else default_status_path()
        self.pid = os.getpid()

    def publish(self, capture, connected, state, server_ready=False):
        payload = {
            "pid": self.pid,
            "updated_monotonic": time.monotonic(),
            "capture_alive": capture.is_alive(),
            "last_audio_monotonic": getattr(capture, "last_chunk_at", 0.0),
            "connected": bool(connected),
            "server_ready": bool(server_ready),
            "state": state,
        }
        temporary = self.path.with_name(f".{self.path.name}.{self.pid}.tmp")
        try:
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def clear(self):
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("pid") == self.pid:
                self.path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
