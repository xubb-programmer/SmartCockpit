import time
import asyncio
from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

from core.handle.receiveAudioHandle import startToChat
from core.handle.reportHandle import enqueue_asr_report
from core.handle.sendAudioHandle import send_stt_message, send_tts_message
from core.handle.textMessageHandler import TextMessageHandler
from core.handle.textMessageType import TextMessageType
from core.utils.util import remove_punctuation_and_length
from core.providers.asr.dto.dto import InterfaceType

TAG = __name__


async def _wait_audio_queue_drained(conn: "ConnectionHandler", reason: str) -> None:
    """等待已经进入服务端的音频全部处理完，避免 stop/start 与音频帧乱序。"""
    try:
        await asyncio.wait_for(
            asyncio.to_thread(conn.asr_audio_queue.join),
            timeout=2.0,
        )
    except asyncio.TimeoutError:
        conn.logger.bind(tag=TAG).warning(
            f"[音频诊断][队列] {reason} 等待音频队列超时，继续处理"
        )

class ListenTextMessageHandler(TextMessageHandler):
    """Listen消息处理器"""

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.LISTEN

    async def handle(self, conn: "ConnectionHandler", msg_json: Dict[str, Any]) -> None:
        if not await conn.wait_until_components_ready(timeout=15):
            conn.logger.bind(tag=TAG).warning("连接组件未就绪，本次listen消息未处理")
            await conn.websocket.send(
                '{"type":"server","status":"initializing",'
                '"message":"语音服务正在初始化，请稍后重试"}'
            )
            return

        if "mode" in msg_json:
            conn.client_listen_mode = msg_json["mode"]
            conn.logger.bind(tag=TAG).debug(
                f"客户端拾音模式：{conn.client_listen_mode}"
            )
        if msg_json["state"] == "start":
            # 设备从播放模式切回录音模式,清除所有音频状态和缓冲区
            await _wait_audio_queue_drained(conn, "listen/start")
            conn.reset_audio_states()
            conn.logger.bind(tag=TAG).info(
                f"[音频诊断][协议] 收到listen/start，模式={conn.client_listen_mode}"
            )
        elif msg_json["state"] == "cancel":
            await _wait_audio_queue_drained(conn, "listen/cancel")
            conn.reset_audio_states()
            await conn.release_interaction_turn("listen_cancel")
            conn.logger.bind(tag=TAG).info(
                f"[音频诊断][协议] 收到listen/cancel，模式={conn.client_listen_mode}，"
                "已清空音频且不触发识别"
            )
        elif msg_json["state"] == "stop":
            conn.client_voice_stop = True
            await _wait_audio_queue_drained(conn, "listen/stop")
            conn.logger.bind(tag=TAG).info(
                f"[音频诊断][协议] 收到listen/stop，模式={conn.client_listen_mode}，"
                f"缓存={len(conn.asr_audio)}帧"
            )
            if conn.asr.interface_type == InterfaceType.STREAM:
                # 流式模式下，发送结束请求
                asyncio.create_task(conn.asr._send_stop_request())
            else:
                # 非流式模式：直接触发ASR识别
                if len(conn.asr_audio) > 0:
                    asr_audio_task = conn.asr_audio.copy()
                    conn.reset_audio_states()

                    if len(asr_audio_task) > 0:
                        await conn.asr.handle_voice_stop(conn, asr_audio_task)
                else:
                    await conn.release_interaction_turn("empty_audio")
                    if getattr(conn, "client_source", "web") == "headless":
                        await conn.websocket.send(
                            '{"type":"server","status":"empty_asr",'
                            '"message":"未收到有效音频，请重新唤醒"}'
                        )
        elif msg_json["state"] == "detect":
            conn.client_have_voice = False
            conn.reset_audio_states()
            if "text" in msg_json:
                if not await conn.acquire_interaction_turn("text_detect"):
                    return
                conn.last_activity_time = time.time() * 1000
                original_text = msg_json["text"]  # 保留原始文本
                filtered_len, filtered_text = remove_punctuation_and_length(
                    original_text
                )

                # 识别是否是唤醒词
                is_wakeup_words = filtered_text in conn.config.get("wakeup_words")
                # 是否开启唤醒词回复
                enable_greeting = conn.config.get("enable_greeting", True)

                if is_wakeup_words and not enable_greeting:
                    # 如果是唤醒词，且关闭了唤醒词回复，就不用回答
                    await send_stt_message(conn, original_text)
                    await send_tts_message(conn, "stop", None)
                    conn.client_is_speaking = False
                elif is_wakeup_words:
                    conn.just_woken_up = True
                    # 上报纯文字数据（复用ASR上报功能，但不提供音频数据）
                    enqueue_asr_report(conn, "嘿，你好呀", [])
                    await startToChat(conn, "嘿，你好呀")
                else:
                    conn.just_woken_up = True
                    # 上报纯文字数据（复用ASR上报功能，但不提供音频数据）
                    enqueue_asr_report(conn, original_text, [])
                    # 否则需要LLM对文字内容进行答复
                    await startToChat(conn, original_text)
