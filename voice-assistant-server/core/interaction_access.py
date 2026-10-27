import asyncio
import os
import time
from dataclasses import dataclass
from typing import Optional, Tuple


VALID_ACCESS_MODES = {"web", "wakeup", "hybrid"}
VALID_CLIENT_SOURCES = {"web", "headless"}


def normalize_access_mode(value) -> str:
    mode = str(value or "web").strip().lower()
    return mode if mode in VALID_ACCESS_MODES else "web"


def get_access_config(config: dict) -> dict:
    value = config.get("voice_access", {})
    return value if isinstance(value, dict) else {}


def get_access_mode(config: dict) -> str:
    env_mode = os.environ.get("VOICE_ACCESS_MODE", "").strip().lower()
    if env_mode in VALID_ACCESS_MODES:
        return env_mode
    return normalize_access_mode(get_access_config(config).get("mode", "web"))


def normalize_client_source(value) -> str:
    source = str(value or "web").strip().lower()
    return source if source in VALID_CLIENT_SOURCES else "web"


def source_allowed(mode: str, source: str) -> bool:
    mode = normalize_access_mode(mode)
    source = normalize_client_source(source)
    if mode == "hybrid":
        return True
    if mode == "wakeup":
        return source == "headless"
    return source == "web"


@dataclass
class TurnLease:
    session_id: str
    source: str
    reason: str
    acquired_at: float
    expires_at: float


class InteractionCoordinator:
    """Serializes complete voice turns only when hybrid mode is enabled."""

    def __init__(self, config: dict, logger=None):
        access_config = get_access_config(config)
        arbitration = access_config.get("arbitration", {})
        if not isinstance(arbitration, dict):
            arbitration = {}

        self.mode = get_access_mode(config)
        self.max_turn_seconds = max(
            15.0, float(arbitration.get("max_turn_seconds", 120))
        )
        self.logger = logger
        self._lock = asyncio.Lock()
        self._lease: Optional[TurnLease] = None

    def is_source_allowed(self, source: str) -> bool:
        return source_allowed(self.mode, source)

    def _log(self, level: str, message: str) -> None:
        if self.logger is None:
            return
        target = self.logger.bind(tag=__name__)
        getattr(target, level)(message)

    def _expire_stale_lease(self, now: float) -> None:
        lease = self._lease
        if lease is None or lease.expires_at > now:
            return
        self._log(
            "warning",
            f"[语音入口] 轮次租约超时释放: 来源={lease.source}, "
            f"会话={lease.session_id[:8]}",
        )
        self._lease = None

    async def acquire(
        self, session_id: str, source: str, reason: str
    ) -> Tuple[bool, Optional[TurnLease]]:
        source = normalize_client_source(source)
        if not self.is_source_allowed(source):
            return False, self._lease

        # Single-source modes preserve the existing behavior and need no contention.
        if self.mode != "hybrid":
            return True, None

        now = time.monotonic()
        async with self._lock:
            self._expire_stale_lease(now)
            if self._lease is None:
                self._lease = TurnLease(
                    session_id=session_id,
                    source=source,
                    reason=reason,
                    acquired_at=now,
                    expires_at=now + self.max_turn_seconds,
                )
                self._log(
                    "info",
                    f"[语音入口] 已取得轮次: 来源={source}, "
                    f"会话={session_id[:8]}, 原因={reason}",
                )
                return True, self._lease

            if self._lease.session_id == session_id:
                self._lease.expires_at = now + self.max_turn_seconds
                self._lease.reason = reason
                return True, self._lease

            return False, self._lease

    async def touch(self, session_id: str) -> None:
        if self.mode != "hybrid":
            return
        now = time.monotonic()
        async with self._lock:
            self._expire_stale_lease(now)
            if self._lease and self._lease.session_id == session_id:
                self._lease.expires_at = now + self.max_turn_seconds

    async def release(self, session_id: str, reason: str) -> bool:
        if self.mode != "hybrid":
            return True
        async with self._lock:
            if self._lease is None or self._lease.session_id != session_id:
                return False
            lease = self._lease
            self._lease = None
            self._log(
                "info",
                f"[语音入口] 已释放轮次: 来源={lease.source}, "
                f"会话={session_id[:8]}, 原因={reason}",
            )
            return True

    async def status(self) -> Optional[TurnLease]:
        now = time.monotonic()
        async with self._lock:
            self._expire_stale_lease(now)
            return self._lease
