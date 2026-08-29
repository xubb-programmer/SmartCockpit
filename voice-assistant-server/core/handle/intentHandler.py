import json
import uuid
import asyncio
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from core.utils.dialogue import Message
from core.providers.tts.dto.dto import ContentType
from core.handle.helloHandle import checkWakeupWords
from plugins_func.register import Action, ActionResponse
from core.handle.sendAudioHandle import send_stt_message
from core.handle.reportHandle import enqueue_tool_report
from core.utils.util import remove_punctuation_and_length
from core.providers.tts.dto.dto import TTSMessageDTO, SentenceType

TAG = __name__

_GENERAL_CAPABILITY_QUERIES = {
    "你能做什么",
    "你能帮我做什么",
    "你能帮我干什么",
    "你会做什么",
    "你有什么功能",
    "你有哪些功能",
    "有什么功能",
    "有哪些功能",
    "支持什么功能",
    "支持哪些功能",
    "你支持什么功能",
    "你支持哪些功能",
    "能干什么",
}

_CAPABILITY_QUERY_TERMS = (
    "有哪些功能",
    "有什么功能",
    "支持什么功能",
    "支持哪些功能",
    "支持什么操作",
    "支持哪些操作",
    "有哪些操作",
    "有什么操作",
    "能做什么",
    "能干什么",
    "能力",
)


_DIRECT_VISION_EXACT_QUERIES = {
    "分析画面",
    "画面分析",
    "分析一下画面",
    "看看画面",
    "看一下画面",
    "描述画面",
    "识别画面",
    "分析视频",
    "看看视频",
    "看一下视频",
    "看看前方",
    "看一下前方",
    "观察前方",
    "侦察前方",
    "前方有什么",
    "前面有什么",
    "画面里有什么",
    "画面有什么",
    "看看前面有什么",
    "看一下前面有什么",
}

_DIRECT_VISION_TERMS = (
    "分析前方画面",
    "分析前方",
    "分析前面",
    "分析画面",
    "画面分析",
    "描述画面",
    "识别画面",
    "分析视频",
    "看看画面",
    "看一下画面",
    "看看视频",
    "看一下视频",
    "看看前方",
    "看一下前方",
    "观察前方",
    "侦察前方",
    "前方有什么",
    "前面有什么",
    "画面里有什么",
    "画面有什么",
    "看到什么",
    "有没有目标",
    "有没有人员",
    "有没有车辆",
    "有没有无人机",
)

_VISION_QUESTION_MARKERS = (
    "什么",
    "有没有",
    "是否",
    "几",
    "多少",
    "哪里",
    "人员",
    "车辆",
    "目标",
    "无人机",
    "障碍",
    "威胁",
)


def _normalize_intent_text(text: object) -> str:
    return "".join(str(text or "").split())


def _get_direct_vision_question(text: object) -> str:
    text = _normalize_intent_text(text)
    if not text:
        return ""

    if text in _DIRECT_VISION_EXACT_QUERIES:
        return "用中文一句话描述当前画面中的主要目标"

    if not any(term in text for term in _DIRECT_VISION_TERMS):
        return ""

    if any(marker in text for marker in _VISION_QUESTION_MARKERS) and len(text) > 4:
        return text
    return "用中文一句话描述当前画面中的主要目标"


def _get_direct_capability_response(conn: "ConnectionHandler", text: str) -> str:
    text = _normalize_intent_text(text)
    summary = str(conn.config.get("capability_summary") or "").strip()
    if text in _GENERAL_CAPABILITY_QUERIES:
        return summary

    if not any(term in text for term in _CAPABILITY_QUERY_TERMS):
        return ""

    vc = conn.config.get("vehicle_control") or {}
    devices = vc.get("devices") or {}
    for spec in devices.values():
        if not isinstance(spec, dict):
            continue
        cn_names = spec.get("cn_names") or []
        if any(str(name) and str(name) in text for name in cn_names):
            return str(spec.get("capability") or "").strip()

    if any(marker in text for marker in ("你", "助手", "系统")):
        return summary
    return ""


async def handle_user_intent(conn: "ConnectionHandler", text):
    # 预处理输入文本，处理可能的JSON格式
    try:
        if text.strip().startswith("{") and text.strip().endswith("}"):
            parsed_data = json.loads(text)
            if isinstance(parsed_data, dict) and "content" in parsed_data:
                text = parsed_data["content"]  # 提取content用于意图分析
                conn.current_speaker = parsed_data.get("speaker")  # 保留说话人信息
    except (json.JSONDecodeError, TypeError):
        pass

    # 检查是否有明确的退出命令
    _, filtered_text = remove_punctuation_and_length(text)
    if await check_direct_exit(conn, filtered_text):
        return True

    if await check_direct_capability_query(conn, filtered_text):
        return True

    # 明确再见不被打断
    if conn.is_exiting:
        return True

    if await check_direct_vision_analyze(conn, filtered_text, text):
        return True

    # 检查是否是唤醒词
    if await checkWakeupWords(conn, filtered_text):
        return True

    if conn.intent_type == "function_call":
        # 使用支持function calling的聊天方法,不再进行意图分析
        return False
    # 使用LLM进行意图分析
    intent_result = await analyze_intent_with_llm(conn, text)
    if not intent_result:
        return False
    # 会话开始时生成sentence_id
    conn.sentence_id = str(uuid.uuid4().hex)
    # 处理各种意图
    return await process_intent_result(conn, intent_result, text)


async def check_direct_capability_query(conn: "ConnectionHandler", text):
    """对笼统能力询问给出确定性短答，避免小模型生成冗长或夹杂英文。"""
    response = _get_direct_capability_response(conn, text)
    if not response:
        return False

    conn.logger.bind(tag=TAG).info(f"识别到通用功能咨询，直接回复: {text}")
    await send_stt_message(conn, text)
    conn.client_abort = False
    conn.current_turn_started_at = time.monotonic()
    conn.sentence_id = str(uuid.uuid4().hex)
    speak_txt(conn, response)
    return True


async def check_direct_vision_analyze(
    conn: "ConnectionHandler",
    text,
    original_text,
):
    """明确看图指令直达视觉工具，跳过一次文本模型工具选择。"""
    question = _get_direct_vision_question(text)
    if not question:
        return False

    vision_enabled = bool(
        (conn.config.get("runtime_capabilities") or {}).get("vision_enabled", False)
    )
    if not vision_enabled:
        conn.logger.bind(tag=TAG).info(
            f"识别到视觉分析指令，但当前未启用视觉分析: {text}"
        )
        await send_stt_message(conn, original_text)
        conn.client_abort = False
        conn.current_turn_started_at = time.monotonic()
        conn.sentence_id = str(uuid.uuid4().hex)
        speak_txt(conn, "当前未启用视觉分析功能。")
        return True

    conn.logger.bind(tag=TAG).info(
        f"识别到明确视觉分析指令，跳过文本模型: {text}"
    )
    conn.sentence_id = str(uuid.uuid4().hex)
    intent_result = json.dumps(
        {
            "function_call": {
                "name": "vision_analyze",
                "arguments": {"question": question},
            }
        },
        ensure_ascii=False,
    )
    return await process_intent_result(conn, intent_result, original_text)


async def check_direct_exit(conn: "ConnectionHandler", text):
    """检查是否有明确的退出命令"""
    _, text = remove_punctuation_and_length(text)
    cmd_exit = conn.cmd_exit
    for cmd in cmd_exit:
        if text == cmd:
            conn.logger.bind(tag=TAG).info(f"识别到明确的退出命令: {text}")
            await send_stt_message(conn, text)
            goodbye_text = (
                (conn.config.get("end_prompt") or {}).get("text")
                or "再见，祝您生活愉快！"
            )
            conn.client_abort = False
            conn.close_after_chat = True
            conn.is_exiting = True
            conn.current_turn_started_at = time.monotonic()
            conn.sentence_id = str(uuid.uuid4().hex)
            speak_txt(conn, goodbye_text)
            return True
    return False


async def analyze_intent_with_llm(conn: "ConnectionHandler", text):
    """使用LLM分析用户意图"""
    if not hasattr(conn, "intent") or not conn.intent:
        conn.logger.bind(tag=TAG).warning("意图识别服务未初始化")
        return None

    # 对话历史记录
    dialogue = conn.dialogue
    try:
        intent_result = await conn.intent.detect_intent(conn, dialogue.dialogue, text)
        return intent_result
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"意图识别失败: {str(e)}")

    return None


async def process_intent_result(
    conn: "ConnectionHandler", intent_result, original_text
):
    """处理意图识别结果"""
    try:
        # 尝试将结果解析为JSON
        intent_data = json.loads(intent_result)

        # 检查是否有function_call
        if "function_call" in intent_data:
            # 直接从意图识别获取了function_call
            conn.logger.bind(tag=TAG).debug(
                f"检测到function_call格式的意图结果: {intent_data['function_call']['name']}"
            )
            function_name = intent_data["function_call"]["name"]
            if function_name == "continue_chat":
                return False

            # 直达工具任务也必须和普通 chat 共用同一把锁。
            # 之前视觉任务走 executor、状态任务走另一个 executor，
            # 会同时改写 dialogue/sentence_id，并让 RKLLM 收到并发请求。
            turn_sentence_id = conn.sentence_id

            if function_name == "result_for_context":
                await send_stt_message(conn, original_text)
                conn.client_abort = False

                def process_context_result():
                    if not conn._chat_lock.acquire(timeout=10):
                        conn.logger.bind(tag=TAG).warning("上下文回复等待会话锁超时，已丢弃")
                        return
                    try:
                        if not _is_current_turn(conn, turn_sentence_id):
                            return
                        conn.dialogue.put(Message(role="user", content=original_text))

                        from core.utils.current_time import get_current_time_info

                        current_time, today_date, today_weekday, lunar_date = (
                            get_current_time_info()
                        )

                        # 构建带上下文的基础提示
                        context_prompt = f"""当前时间：{current_time}
                                        今天日期：{today_date} ({today_weekday})
                                        今天农历：{lunar_date}

                                        请根据以上信息回答用户的问题：{original_text}"""

                        response = conn.intent.replyResult(context_prompt, original_text)
                        if _is_current_turn(conn, turn_sentence_id):
                            speak_txt(conn, response, turn_sentence_id)
                    finally:
                        conn._chat_lock.release()

                conn.executor.submit(process_context_result)
                return True

            function_args = {}
            if "arguments" in intent_data["function_call"]:
                function_args = intent_data["function_call"]["arguments"]
                if function_args is None:
                    function_args = {}
            # 确保参数是字符串格式的JSON
            if isinstance(function_args, dict):
                function_args = json.dumps(function_args)

            function_call_data = {
                "name": function_name,
                "id": str(uuid.uuid4().hex),
                "arguments": function_args,
            }

            await send_stt_message(conn, original_text)
            conn.client_abort = False

            # 准备工具调用参数
            tool_input = {}
            if function_args:
                if isinstance(function_args, str):
                    tool_input = json.loads(function_args) if function_args else {}
                elif isinstance(function_args, dict):
                    tool_input = function_args

            # 上报工具调用
            enqueue_tool_report(conn, function_name, tool_input)

            # 使用executor执行函数调用和结果处理
            def process_function_call():
                if not conn._chat_lock.acquire(timeout=10):
                    conn.logger.bind(tag=TAG).warning(
                        f"工具调用等待会话锁超时，已丢弃: {function_name}"
                    )
                    return
                try:
                    if not _is_current_turn(conn, turn_sentence_id):
                        conn.logger.bind(tag=TAG).info(
                            f"工具调用在执行前被新指令取消: {function_name}"
                        )
                        return

                    conn.dialogue.put(Message(role="user", content=original_text))

                    # 工具调用超时时间
                    tool_call_timeout = int(conn.config.get("tool_call_timeout", 30))
                    # 使用统一工具处理器处理所有工具调用
                    try:
                        result = asyncio.run_coroutine_threadsafe(
                            conn.func_handler.handle_llm_function_call(
                                conn, function_call_data
                            ),
                            conn.loop,
                        ).result(timeout=tool_call_timeout)
                    except Exception as e:
                        conn.logger.bind(tag=TAG).error(f"工具调用失败: {e}")
                        result = ActionResponse(
                            action=Action.ERROR, result="工具调用超时，请一会再试下哈", response="工具调用超时，请一会再试下哈"
                        )

                    # 打断或新指令到来后，旧工具的结果不能再写入对话或播放。
                    if not _is_current_turn(conn, turn_sentence_id):
                        conn.logger.bind(tag=TAG).info(
                            f"工具调用结果已丢弃（当前轮次已变化）: {function_name}"
                        )
                        return

                    # 上报工具调用结果
                    if result:
                        enqueue_tool_report(conn, function_name, tool_input, str(result.result) if result.result else None, report_tool_call=False)

                        if result.action == Action.RESPONSE:  # 直接回复前端
                            text = result.response
                            if text is not None:
                                speak_txt(conn, text, turn_sentence_id)
                        elif result.action == Action.REQLLM:  # 调用函数后再请求llm生成回复
                            text = result.result
                            conn.dialogue.put(Message(role="tool", content=text))
                            llm_result = conn.intent.replyResult(text, original_text)
                            if llm_result is None:
                                llm_result = text
                            if _is_current_turn(conn, turn_sentence_id):
                                speak_txt(conn, llm_result, turn_sentence_id)
                        elif (
                            result.action == Action.NOTFOUND
                            or result.action == Action.ERROR
                        ):
                            text = result.response if result.response else result.result
                            if text is not None:
                                speak_txt(conn, text, turn_sentence_id)
                        elif function_name != "play_music":
                            # For backward compatibility with original code
                            text = result.response
                            if text is None:
                                text = result.result
                            if text is not None:
                                speak_txt(conn, text, turn_sentence_id)
                finally:
                    conn._chat_lock.release()

            # 将函数执行放在线程池中
            conn.executor.submit(process_function_call)
            return True
        return False
    except json.JSONDecodeError as e:
        conn.logger.bind(tag=TAG).error(f"处理意图结果时出错: {e}")
        return False


def _is_current_turn(conn: "ConnectionHandler", sentence_id: str) -> bool:
    return bool(
        sentence_id
        and not conn.client_abort
        and not conn.stop_event.is_set()
        and conn.sentence_id == sentence_id
    )


def speak_txt(conn: "ConnectionHandler", text, sentence_id=None):
    sentence_id = sentence_id or conn.sentence_id
    if sentence_id != conn.sentence_id or conn.client_abort or conn.stop_event.is_set():
        conn.logger.bind(tag=TAG).info("忽略已取消或过期的语音回复")
        return
    # 记录文本到 sentence_id 映射
    conn.tts.store_tts_text(sentence_id, text)

    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=sentence_id,
            sentence_type=SentenceType.FIRST,
            content_type=ContentType.ACTION,
        )
    )
    conn.tts.tts_one_sentence(conn, ContentType.TEXT, content_detail=text)
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=sentence_id,
            sentence_type=SentenceType.LAST,
            content_type=ContentType.ACTION,
        )
    )
    conn.dialogue.put(Message(role="assistant", content=text))
