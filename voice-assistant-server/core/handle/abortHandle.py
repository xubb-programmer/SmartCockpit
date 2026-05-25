import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
TAG = __name__


async def handleAbortMessage(conn: "ConnectionHandler"):
    if conn.close_after_chat or conn.is_exiting:
        conn.logger.bind(tag=TAG).info("退出流程中被打断，直接关闭连接")
        return
        
    conn.logger.bind(tag=TAG).info("收到打断指令")
    # 设置成打断状态，会自动打断llm、tts任务
    conn.client_abort = True
    conn.clear_queues()
    # 打断客户端说话状态
    await conn.websocket.send(
        json.dumps({"type": "tts", "state": "stop", "session_id": conn.session_id})
    )
    conn.clearSpeakStatus()
    conn.logger.bind(tag=TAG).debug("打断指令处理完成")
