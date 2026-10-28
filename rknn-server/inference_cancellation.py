"""Thread-safe cancellation ownership for the shared synchronous model."""
import threading
import time


class InferenceCancellation:
    def __init__(self, retention_seconds=300):
        self._lock = threading.Lock()
        self._kind = None
        self._conversation = None
        self._closed = {}
        self._retention = retention_seconds
        self.cancelled = threading.Event()

    def _prune(self):
        now = time.monotonic()
        self._closed = {key: until for key, until in self._closed.items() if until > now}

    def start(self, kind, conversation_id=None):
        with self._lock:
            self._prune()
            if conversation_id and conversation_id in self._closed:
                return False
            self._kind, self._conversation = kind, conversation_id
            self.cancelled.clear()
            return True

    def finish(self):
        with self._lock:
            self._kind = self._conversation = None

    def cancel(self, conversation_id=None, close_session=False):
        with self._lock:
            self._prune()
            if close_session and conversation_id:
                # Also reject requests already waiting for the inference lock.
                self._closed[conversation_id] = time.monotonic() + self._retention
            if self._kind is None or (
                conversation_id and conversation_id != self._conversation
            ):
                return None
            self.cancelled.set()
            return self._kind
