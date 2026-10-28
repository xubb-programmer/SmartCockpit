import json
import asyncio
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
TAG = __name__


async def _abort_inference_providers(conn: "ConnectionHandler"):
    """在工作线程中调用 provider 的取消接口，不阻塞语音事件循环。"""
    providers = []
    backend_keys = set()
    for provider in (
        getattr(conn, "llm", None),
        getattr(getattr(conn, "intent", None), "llm", None),
        getattr(conn, "_vllm_instance", None),
    ):
        if provider is not None and callable(getattr(provider, "abort", None)):
            base_url = str(getattr(provider, "base_url", "")).rstrip("/")
            backend_key = base_url
            for endpoint in ("/rkllm_chat", "/vision/analyze"):
                if backend_key.endswith(endpoint):
                    backend_key = backend_key[:-len(endpoint)]
                    break
            key = backend_key or f"instance:{id(provider)}"
            if key in backend_keys:
                continue
            backend_keys.add(key)
            providers.append(provider)
    if not providers:
        return

    results = await asyncio.gather(
        *(asyncio.to_thread(provider.abort) for provider in providers),
        return_exceptions=True,
    )
    for provider, result in zip(providers, results):
        if isinstance(result, Exception):
            conn.logger.bind(tag=TAG).warning(
                f"取消{provider.__class__.__name__}推理失败: {result}"
            )


async def handleAbortMessage(conn: "ConnectionHandler", music_only=False):
    # ASR/VAD 可能在相邻事件中重复触发打断。串行化整个取消流程，
    # 避免第二个延迟到达的 /rkllm_abort 误取消已经开始的新指令。
    abort_lock = getattr(conn, "_abort_lock", None)
    if abort_lock is None:
        abort_lock = asyncio.Lock()
        conn._abort_lock = abort_lock

    async with abort_lock:
        if conn.close_after_chat or conn.is_exiting:
            conn.logger.bind(tag=TAG).info("退出流程中被打断，直接关闭连接")
            return

        conn.logger.bind(tag=TAG).info("收到打断指令")
        headless_music = (
            (getattr(conn, "music_barge_in_active", False) or music_only)
            and getattr(conn, "client_source", "web") == "headless"
        )
        if headless_music:
            # Invalidate the old song's pending LAST/control messages before the
            # client starts a new manual recording on this same connection.
            conn.sentence_id = uuid.uuid4().hex
            conn.headless_music_interrupted = True
        # 先设置本地标记并停止 TTS，再通知推理后端停止输出并安全收尾。
        conn.client_abort = True
        conn.clear_queues()
        conn.clearSpeakStatus()
        # 打断客户端说话状态
        await conn.websocket.send(
            json.dumps({"type": "tts", "state": "stop", "session_id": conn.session_id})
        )
        if not headless_music:
            await _abort_inference_providers(conn)
        conn.logger.bind(tag=TAG).debug("打断指令处理完成")
