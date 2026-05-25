import re
import json
import threading
import requests
from config.logger import setup_logging
from core.providers.llm.base import LLMProviderBase

TAG = __name__
logger = setup_logging()

_RE_CHINESE = re.compile(r'[\u4e00-\u9fff]')


def _has_chinese(text: str) -> bool:
    """文本中是否包含至少一个中文字符"""
    return bool(_RE_CHINESE.search(text))


# ── 模式判定阈值（response_with_functions 使用）──────────────────────────────
# 缓冲超过此长度且含中文字符 → 判定为普通文本，切换流式输出
_TEXT_MODE_MIN_LEN = 12
# 缓冲超过此长度仍未判定 → 强制视为文本，防止无限缓冲
_TEXT_MODE_FALLBACK_LEN = 200


class LLMProvider(LLMProviderBase):
    """
    RKLLM 离线大模型 Provider。

    function calling 策略：
      - 流式检测模型输出中的工具调用（<tool_call>标签 / 裸JSON / RKLLM原生格式）
      - 检测到工具调用时丢弃前置文本，只返回规范化的 tool_call
      - 未检测到时流式返回中文文本，过滤垃圾输出
    """

    def __init__(self, config):
        self.base_url = config.get("base_url", "http://localhost:18080/rkllm_chat")
        self.model_name = config.get("model_name", "rkllm_model")
        self.timeout = int(config.get("timeout", 120))

        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Authorization": "not_required",
        })
        logger.bind(tag=TAG).info(
            f"RKLLM LLM Provider 初始化完成，Server 地址: {self.base_url}"
        )

    @staticmethod
    def _normalize_dialogue(dialogue):
        for msg in dialogue:
            if "role" in msg and "content" not in msg:
                msg["content"] = ""
        return dialogue

    @staticmethod
    def _strip_think_blocks(buffer, is_thinking):
        """
        过滤 <think>...</think> 块，返回 (cleaned_buffer, is_thinking)。
        """
        while "<think>" in buffer and "</think>" in buffer:
            buffer = buffer.split("<think>", 1)[0] + buffer.split("</think>", 1)[1]
        if "<think>" in buffer:
            is_thinking = True
            buffer = buffer.split("<think>", 1)[0]
        if "</think>" in buffer:
            is_thinking = False
            buffer = buffer.split("</think>", 1)[1]
        return buffer, is_thinking

    def _post_with_retry(self, payload, stream=True):
        import time as _time
        max_retries = 4
        retry_delay = 1.5
        resp = None
        for attempt in range(max_retries):
            resp = self.session.post(
                self.base_url, json=payload, stream=stream,
                timeout=self.timeout, verify=False,
            )
            if resp.status_code != 503:
                break
            if attempt < max_retries - 1:
                logger.bind(tag=TAG).warning(
                    f"RKLLM busy，{retry_delay}s 后重试 ({attempt+1}/{max_retries-1})..."
                )
                _time.sleep(retry_delay)
        return resp

    # ══════════════════════════════════════════════════════
    #  tool_call 提取（支持 3 种格式）
    # ══════════════════════════════════════════════════════

    @staticmethod
    def _extract_tool_call(text):
        """
        从模型输出中提取工具调用 JSON，支持三种格式：
          1. <tool_call>{"name":..., "arguments":...}</tool_call>
          2. {"name":..., "arguments":...}  （裸 JSON）
          3. function_name{"arg1":"val1",...}  （RKLLM 原生格式）
        返回标准 JSON 字符串，或 None。
        """
        # 格式1: <tool_call> 标签包裹
        m = re.search(r'<tool_call>\s*(.*?)\s*</tool_call>', text, re.DOTALL)
        if m:
            content = m.group(1).strip()
            try:
                json.loads(content)
                return content
            except json.JSONDecodeError:
                pass

        # 格式2: 裸 JSON {"name":...}
        start = text.find('{"name"')
        if start == -1:
            start = text.find('{ "name"')
        if start != -1:
            depth = 0
            for i, ch in enumerate(text[start:]):
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        candidate = text[start:start + i + 1]
                        try:
                            data = json.loads(candidate)
                            if "name" in data and "arguments" in data:
                                return candidate
                        except json.JSONDecodeError:
                            break

        # 格式3: RKLLM 原生格式 function_name{"arg":"val",...}
        m3 = re.match(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*(\{.*\})\s*$', text.strip(), re.DOTALL)
        if m3:
            func_name, args_str = m3.group(1), m3.group(2)
            try:
                args = json.loads(args_str)
                if isinstance(args, dict):
                    return json.dumps(
                        {"name": func_name, "arguments": args}, ensure_ascii=False
                    )
            except json.JSONDecodeError:
                pass

        return None

    # ══════════════════════════════════════════════════════
    #  buffer 最终处理（stop / 流结束 统一入口）
    # ══════════════════════════════════════════════════════

    def _finalize_buffer(self, buffer, source="stop"):
        """
        处理累积的 buffer，返回 (yield_content, yield_tool) 或 None。
        source: "stop" 或 "流结束"，用于日志区分。
        """
        if not buffer:
            return None

        tc_json = self._extract_tool_call(buffer)
        if tc_json:
            logger.bind(tag=TAG).info(f"[TC] {source}提取工具调用: {tc_json[:80]}")
            return f"<tool_call>{tc_json}</tool_call>", None

        if _has_chinese(buffer):
            return buffer, None

        # 区分已知结构标签和未知内容，日志更准确
        stripped = buffer.strip()
        if re.search(r'</?tool_call>|</?think>', stripped):
            logger.bind(tag=TAG).debug(f"[TC] {source}过滤残留标签: {stripped[:50]!r}")
        else:
            logger.bind(tag=TAG).warning(f"[TC] {source}过滤无效内容: {stripped[:50]!r}")
        return None

    # ══════════════════════════════════════════════════════
    #  普通对话（无 function calling）
    # ══════════════════════════════════════════════════════

    def response(self, session_id, dialogue, **kwargs):
        """流式调用 RKLLM Server，逐 token yield 文本，自动过滤 <think> 块。"""
        dialogue = self._normalize_dialogue(dialogue)
        reset_kv = kwargs.get('reset_kv', False)
        payload = {"model": self.model_name, "messages": dialogue, "stream": True}
        if reset_kv:
            payload["reset_conversation"] = True

        try:
            resp = self._post_with_retry(payload, stream=True)
            if resp.status_code != 200:
                logger.bind(tag=TAG).error(
                    f"RKLLM Server 返回错误: {resp.status_code} - {resp.text}"
                )
                return

            is_thinking = False
            buffer = ""
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    chunk = json.loads(line.decode("utf-8"))
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    choice = choices[-1]
                    delta = choice.get("delta") or choice.get("message") or {}
                    content = delta.get("content", "")
                    finish_reason = choice.get("finish_reason")

                    if finish_reason == "stop":
                        if not is_thinking and buffer and _has_chinese(buffer):
                            yield buffer
                        break
                    if not content:
                        continue

                    buffer += content
                    # 过滤 <think> 块
                    buffer, is_thinking = self._strip_think_blocks(buffer, is_thinking)
                    if not is_thinking and buffer:
                        if _has_chinese(buffer):
                            yield buffer
                        buffer = ""

                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    logger.bind(tag=TAG).warning(f"解析RKLLM响应行失败: {e}")
                    continue

        except requests.exceptions.ConnectionError:
            logger.bind(tag=TAG).error(f"无法连接到 RKLLM Server ({self.base_url})")
        except requests.exceptions.Timeout:
            logger.bind(tag=TAG).error(f"RKLLM Server 请求超时（{self.timeout}s）")
        except Exception as e:
            logger.bind(tag=TAG).error(f"RKLLM 推理异常: {e}")

    # ══════════════════════════════════════════════════════
    #  带工具调用的对话
    # ══════════════════════════════════════════════════════

    def response_with_functions(self, session_id, dialogue, functions=None, **kwargs):
        """
        RKLLM 推理 + tool_call 流式检测。

        模式判定逻辑（只判定一次）：
          - buffer 以 <tool_call> 或 {"name" 开头 → True（缓冲模式）
          - buffer 含中文且 >12 字符 → False（流式文本模式）
          - 纯英文且 >200 字符 → False（防止无限缓冲）
          - 其他 → None（继续缓冲，可能是 RKLLM 原生工具调用）

        文本模式下：
          - 流式输出中文文本，过滤 <think> 块和非中文垃圾
          - 随时检测 <tool_call> 出现则切换为缓冲模式

        最终处理（stop / 流结束）：
          - 统一调用 _finalize_buffer：先尝试提取 tool_call，否则输出中文
        """
        dialogue = self._normalize_dialogue(dialogue)
        reset_kv = kwargs.get('reset_kv', False)
        payload = {"model": self.model_name, "messages": dialogue, "stream": True}
        if reset_kv:
            payload["reset_conversation"] = True

        try:
            resp = self._post_with_retry(payload, stream=True)
            if resp.status_code != 200:
                logger.bind(tag=TAG).error(
                    f"RKLLM Server 返回错误: {resp.status_code} - {resp.text}"
                )
                return

            buffer = ""
            tool_call_mode = None  # None=未判断, True=缓冲, False=流式文本
            is_thinking = False

            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    chunk = json.loads(line.decode("utf-8"))
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    choice = choices[-1]
                    delta = choice.get("delta") or choice.get("message") or {}
                    content = delta.get("content", "")
                    finish_reason = choice.get("finish_reason")

                    if content:
                        buffer += content

                        # ── 首次判断响应模式（只判断一次）──────────────
                        if tool_call_mode is None:
                            stripped = buffer.lstrip()
                            if stripped.startswith('<tool_call>'):
                                tool_call_mode = True
                            elif stripped.startswith('{"name"') or stripped.startswith('{ "name"'):
                                tool_call_mode = True
                            elif len(stripped) > _TEXT_MODE_MIN_LEN and _has_chinese(stripped):
                                tool_call_mode = False
                                logger.bind(tag=TAG).debug(
                                    f"[TC] 判定为文本模式, buffer: {stripped[:20]!r}"
                                )
                            elif len(stripped) > _TEXT_MODE_FALLBACK_LEN:
                                tool_call_mode = False
                            # else: 继续缓冲（可能是 RKLLM 原生工具调用）

                        # ── 文本模式：流式输出 ────────────────────────
                        if tool_call_mode is False:
                            buffer, is_thinking = self._strip_think_blocks(buffer, is_thinking)

                            # 检测文本中出现的 <tool_call>，切换为缓冲模式
                            if "<tool_call>" in buffer:
                                buffer = "<tool_call>" + buffer.split("<tool_call>", 1)[1]
                                tool_call_mode = True
                                logger.bind(tag=TAG).warning(
                                    "[TC] 文本模式中检测到<tool_call>，切换为工具调用缓冲模式"
                                )
                            elif not is_thinking and buffer:
                                # 流式输出中文文本
                                if _has_chinese(buffer):
                                    yield buffer, None
                                buffer = ""

                        # True/None: 缓冲模式，增量检测完整 JSON
                        if tool_call_mode is not False and len(buffer) > 20:
                            tc_json = self._extract_tool_call(buffer)
                            if tc_json:
                                # 检查第一个工具调用之后是否还有未完成的 <tool_call>
                                first_end = buffer.find('</tool_call>')
                                if first_end >= 0:
                                    remaining = buffer[first_end + len('</tool_call>'):].strip()
                                    if remaining.startswith('<tool_call') and '</tool_call>' not in remaining:
                                        # 还有未完成的工具调用，继续缓冲
                                        continue
                                # 提取所有完整的工具调用
                                import re as _re
                                all_tc = _re.findall(
                                    r'<tool_call>\s*(.*?)\s*</tool_call>',
                                    buffer, _re.DOTALL
                                )
                                if all_tc:
                                    tc_result = ''.join(
                                        f'<tool_call>{tc}</tool_call>' for tc in all_tc
                                    )
                                else:
                                    tc_result = f'<tool_call>{tc_json}</tool_call>'
                                logger.bind(tag=TAG).info(
                                    f"[TC] 增量检测到{len(all_tc) if all_tc else 1}个工具调用: {tc_result[:80]}"
                                )
                                yield tc_result, None
                                # 🔥 提前返回工具调用，跳过剩余 token
                                # 服务端 finally 块会等待模型完成并更新 sent_count
                                logger.bind(tag=TAG).debug("[TC] 提前终止推理流")
                                resp.close()
                                return

                    # ── 结束处理 ──────────────────────────────────
                    if finish_reason == "stop":
                        result = self._finalize_buffer(buffer, "stop")
                        if result:
                            yield result
                        buffer = ""
                        break

                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    logger.bind(tag=TAG).warning(f"解析RKLLM响应行失败: {e}")
                    continue

            # 流结束兜底（未收到 finish_reason="stop"）
            result = self._finalize_buffer(buffer, "流结束")
            if result:
                yield result

        except requests.exceptions.ConnectionError:
            logger.bind(tag=TAG).error(f"无法连接到 RKLLM Server ({self.base_url})")
        except requests.exceptions.Timeout:
            logger.bind(tag=TAG).error(f"RKLLM Server 请求超时（{self.timeout}s）")
        except Exception as e:
            logger.bind(tag=TAG).error(f"RKLLM 推理异常: {e}")
