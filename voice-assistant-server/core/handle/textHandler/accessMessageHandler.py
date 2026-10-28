from typing import Any, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

from core.handle.textMessageHandler import TextMessageHandler
from core.handle.textMessageType import TextMessageType


class AccessTextMessageHandler(TextMessageHandler):
    """Handles explicit turn reservation from the headless wake-word client."""

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.ACCESS

    async def handle(
        self, conn: "ConnectionHandler", msg_json: Dict[str, Any]
    ) -> None:
        action = str(msg_json.get("action", "status")).strip().lower()

        if action == "acquire":
            if not await conn.wait_until_components_ready(timeout=3):
                await conn.send_access_status(
                    "initializing", "语音服务正在初始化，请稍后再试"
                )
                return
            acquired = await conn.acquire_interaction_turn(
                reason=str(msg_json.get("reason", "client_request")),
                notify_busy=False,
            )
            if acquired:
                await conn.send_access_status("granted", "语音通道已就绪")
            else:
                await conn.send_access_status(
                    "busy",
                    "语音通道正在使用，请稍后再试",
                    owner=conn.last_turn_busy_owner,
                )
            return

        if action == "release":
            await conn.release_interaction_turn("client_release")
            await conn.send_access_status("released", "语音通道已释放")
            return

        coordinator = getattr(conn.server, "interaction_coordinator", None)
        lease = await coordinator.status() if coordinator else None
        if lease is None or lease.session_id == conn.session_id:
            await conn.send_access_status("available", "语音通道可用")
        else:
            await conn.send_access_status(
                "busy",
                "语音通道正在使用，请稍后再试",
                owner=lease.source,
            )
