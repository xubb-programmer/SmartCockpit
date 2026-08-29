import re
import json
import threading
import requests
from config.logger import setup_logging
from core.providers.llm.base import LLMProviderBase

TAG = __name__
logger = setup_logging()

_RE_CHINESE = re.compile(r'[\u4e00-\u9fff]')
_RE_SPOKEN_PUNCTUATION = re.compile(
    r'^[\s，。！？；：、,.!?;:~…“”‘’（）()《》\-]+$'
)


def _has_chinese(text: str) -> bool:
    """文本中是否包含至少一个中文字符"""
    return bool(_RE_CHINESE.search(text))


def _is_spoken_punctuation(text: str) -> bool:
    """允许标点单独作为流式片段输出，避免TTS丢失停顿和句末。"""
    return bool(text and _RE_SPOKEN_PUNCTUATION.fullmatch(text))


def _sanitize_spoken_text(text: str) -> str:
    """过滤模型偶发的英文单词，同时保留产品名 Z1、数字和中文标点。"""
    sentinel = "\uFFF0"
    text = re.sub(r"(?i)(?<![A-Za-z0-9])Z1(?![A-Za-z0-9])", sentinel, text)
    text = re.sub(r"[A-Za-z_]+", "", text)
    text = text.replace(sentinel, "Z1")
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"^[\s,，.。:：;；!?！？\-]+(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"\s+([，。！？；：、,.!?;:])", r"\1", text)
    return text


# ── 模式判定阈值（response_with_functions 使用）──────────────────────────────
# 缓冲超过此长度且含中文字符 → 判定为普通文本，切换流式输出
_TEXT_MODE_MIN_LEN = 12
# 缓冲超过此长度仍未判定 → 强制视为文本，防止无限缓冲
_TEXT_MODE_FALLBACK_LEN = 200
_COMPACT_TOOL_PREFIX_RE = re.compile(r"^@(C|S|V|M|D|X)(?:\||\s|$)")


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
        self.max_tokens = max(16, int(config.get("max_tokens", 256)))
        verify_ssl = config.get("verify_ssl", False)
        if isinstance(verify_ssl, str):
            verify_ssl = verify_ssl.strip().lower() in ("1", "true", "yes", "on")
        self.verify_ssl = bool(verify_ssl)

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
        # 取消旧请求后，服务端还需要极短时间释放 RKLLM 句柄；
        # 高频短重试比原先 1.5 秒间隔更快，也不会把一次打断拖成 6 秒。
        max_retries = 12
        retry_delay = 0.25
        resp = None
        for attempt in range(max_retries):
            resp = self.session.post(
                self.base_url, json=payload, stream=stream,
                timeout=self.timeout, verify=self.verify_ssl,
            )
            if resp.status_code != 503:
                break
            if attempt < max_retries - 1:
                logger.bind(tag=TAG).warning(
                    f"RKLLM busy，{retry_delay}s 后重试 ({attempt+1}/{max_retries-1})..."
                )
                _time.sleep(retry_delay)
        return resp

    def abort(self):
        """请求 RKLLM Server 取消当前推理，供语音打断和新指令抢占使用。"""
        base_url = self.base_url.rstrip("/")
        if base_url.endswith("/rkllm_chat"):
            abort_url = base_url[:-len("/rkllm_chat")] + "/rkllm_abort"
        else:
            abort_url = base_url + "/rkllm_abort"
        try:
            response = requests.post(
                abort_url,
                json={},
                timeout=2,
                verify=self.verify_ssl,
                headers={"Content-Type": "application/json"},
            )
            if response.status_code == 200:
                payload = response.json()
                logger.bind(tag=TAG).info(
                    f"RKLLM 推理取消请求已发送: {payload.get('task', 'idle')}"
                )
                return True
            logger.bind(tag=TAG).warning(
                f"RKLLM 推理取消失败: HTTP {response.status_code}"
            )
        except requests.RequestException as exc:
            logger.bind(tag=TAG).warning(f"RKLLM 推理取消请求异常: {exc}")
        return False

    def _build_payload(self, session_id, dialogue, reset_kv=False):
        payload = {
            "model": self.model_name,
            "messages": dialogue,
            "stream": True,
            "conversation_id": session_id,
            "max_tokens": self.max_tokens,
        }
        if reset_kv:
            payload["reset_conversation"] = True
        return payload

    # ══════════════════════════════════════════════════════
    #  tool_call 提取（支持 3 种格式）
    # ══════════════════════════════════════════════════════

    @staticmethod
    def _extract_compact_tool_calls(text):
        """Convert short semantic tool lines into the legacy JSON envelope."""
        normalized = str(text or "").replace("｜", "|").replace("，", ",")
        matches = re.findall(
            r"@(C|S|V|M|D|X)(?:\|([^@\r\n]*))?",
            normalized,
            flags=re.IGNORECASE,
        )
        tool_calls = []
        for code, payload in matches:
            code = code.upper()
            payload = (payload or "").strip().rstrip("。；;")
            name = ""
            arguments = {}

            if code == "C":
                parts = [part.strip() for part in payload.split("|", 2)]
                if len(parts) < 2 or not parts[0] or not parts[1]:
                    continue
                targets = [item.strip() for item in parts[0].split(",") if item.strip()]
                if (
                    not targets
                    or any(not re.fullmatch(r"[A-Za-z0-9_]+", item) for item in targets)
                    or not re.fullmatch(r"[A-Za-z0-9_]+", parts[1])
                ):
                    continue
                arguments = {
                    "target": targets[0] if len(targets) == 1 else targets,
                    "action": parts[1],
                }
                if len(parts) == 3 and parts[2]:
                    params = {}
                    params_valid = True
                    for item in parts[2].split(","):
                        key, separator, value = item.partition("=")
                        if separator and key.strip() and value.strip():
                            params[key.strip()] = value.strip()
                        else:
                            params_valid = False
                            break
                    if not params_valid or not params:
                        continue
                    arguments["params"] = params
                name = "vehicle_control"
            elif code == "S":
                name = "vehicle_status"
                if payload and payload.lower() not in ("all", "全部", "所有"):
                    arguments["target"] = payload
            elif code == "V":
                name = "vision_analyze"
                arguments["question"] = payload or "描述当前画面中的主要目标"
            elif code == "M":
                name = "play_music"
                arguments["song_name"] = payload or "random"
            elif code == "D":
                name = "get_datetime"
                arguments["query"] = payload or "all"
            elif code == "X":
                name = "handle_exit_intent"

            if name:
                tool_calls.append(
                    json.dumps(
                        {"name": name, "arguments": arguments},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
        return tool_calls

    @staticmethod
    def _extract_tool_calls(text):
        """
        从模型输出中提取工具调用 JSON，支持三种格式：
          1. <tool_call>{"name":..., "arguments":...}</tool_call>
          2. {"name":..., "arguments":...}  （裸 JSON）
          3. function_name{"arg1":"val1",...}  （RKLLM 原生格式）
        返回标准 JSON 字符串列表；标签格式可返回多个调用。
        """
        compact_calls = LLMProvider._extract_compact_tool_calls(text)
        if compact_calls:
            return compact_calls

        # 格式1: <tool_call> 标签包裹
        tagged_calls = []
        for match in re.finditer(
            r'<tool_call>\s*(.*?)\s*</tool_call>', text, re.DOTALL
        ):
            content = match.group(1).strip()
            try:
                json.loads(content)
                tagged_calls.append(content)
            except json.JSONDecodeError:
                continue
        if tagged_calls:
            return tagged_calls

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
                                return [candidate]
                        except json.JSONDecodeError:
                            break

        # 格式3: RKLLM 原生格式 function_name{"arg":"val",...}
        m3 = re.match(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*(\{.*\})\s*$', text.strip(), re.DOTALL)
        if m3:
            func_name, args_str = m3.group(1), m3.group(2)
            try:
                args = json.loads(args_str)
                if isinstance(args, dict):
                    return [
                        json.dumps(
                            {"name": func_name, "arguments": args},
                            ensure_ascii=False,
                        )
                    ]
            except json.JSONDecodeError:
                pass

        return []

    @staticmethod
    def _extract_tool_call(text):
        """兼容旧调用方：返回模型输出中的第一个工具调用。"""
        tool_calls = LLMProvider._extract_tool_calls(text)
        return tool_calls[0] if tool_calls else None

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

        tool_calls = self._extract_tool_calls(buffer)
        if tool_calls:
            normalized = "".join(
                f"<tool_call>{tool_call}</tool_call>" for tool_call in tool_calls
            )
            logger.bind(tag=TAG).info(
                f"[TC] {source}提取 {len(tool_calls)} 个工具调用: {normalized[:80]}"
            )
            return normalized, None

        spoken_text = _sanitize_spoken_text(buffer)
        if _has_chinese(spoken_text) or _is_spoken_punctuation(spoken_text):
            return spoken_text, None

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
        payload = self._build_payload(session_id, dialogue, reset_kv)

        resp = None
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

                    if content:
                        buffer += content
                        # 过滤 <think> 块
                        buffer, is_thinking = self._strip_think_blocks(buffer, is_thinking)
                        if not is_thinking and buffer:
                            spoken_text = _sanitize_spoken_text(buffer)
                            if _has_chinese(spoken_text) or _is_spoken_punctuation(spoken_text):
                                yield spoken_text
                            buffer = ""

                    if finish_reason == "stop":
                        if not is_thinking and buffer and (
                            _has_chinese(buffer) or _is_spoken_punctuation(buffer)
                        ):
                            spoken_text = _sanitize_spoken_text(buffer)
                            if spoken_text:
                                yield spoken_text
                        break

                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    logger.bind(tag=TAG).warning(f"解析RKLLM响应行失败: {e}")
                    continue

        except requests.exceptions.ConnectionError:
            logger.bind(tag=TAG).error(f"无法连接到 RKLLM Server ({self.base_url})")
        except requests.exceptions.Timeout:
            logger.bind(tag=TAG).error(f"RKLLM Server 请求超时（{self.timeout}s）")
        except Exception as e:
            logger.bind(tag=TAG).error(f"RKLLM 推理异常: {e}")
        finally:
            if resp is not None:
                resp.close()

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
        payload = self._build_payload(session_id, dialogue, reset_kv)

        resp = None
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
                            if _COMPACT_TOOL_PREFIX_RE.match(stripped):
                                tool_call_mode = True
                            elif stripped.startswith('<tool_call>'):
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

                            compact_match = re.search(
                                r"@(C|S|V|M|D|X)(?:\||\s|$)",
                                buffer,
                            )
                            if compact_match:
                                buffer = buffer[compact_match.start():]
                                tool_call_mode = True
                                logger.bind(tag=TAG).warning(
                                    "[TC] 文本模式中检测到紧凑工具协议，切换缓冲模式"
                                )
                            # 检测文本中出现的 <tool_call>，切换为缓冲模式
                            elif "<tool_call>" in buffer:
                                buffer = "<tool_call>" + buffer.split("<tool_call>", 1)[1]
                                tool_call_mode = True
                                logger.bind(tag=TAG).warning(
                                    "[TC] 文本模式中检测到<tool_call>，切换为工具调用缓冲模式"
                                )
                            elif not is_thinking and buffer:
                                # 流式输出中文文本
                                if _has_chinese(buffer) or _is_spoken_punctuation(buffer):
                                    spoken_text = _sanitize_spoken_text(buffer)
                                    if spoken_text:
                                        yield spoken_text, None
                                    buffer = ""
                                elif len(buffer) > _TEXT_MODE_FALLBACK_LEN:
                                    logger.bind(tag=TAG).warning(
                                        f"[TC] 丢弃过长的非中文输出: {buffer[:50]!r}"
                                    )
                                    buffer = ""

                        # 工具调用保持缓冲到 stop/流结束，再统一提取全部调用。
                        # 不能在第一个完整调用后提前关闭，否则会截断同设备多动作。

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
        finally:
            if resp is not None:
                resp.close()
