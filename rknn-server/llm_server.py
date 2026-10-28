import ctypes
import sys
import os
import subprocess
import threading
import time
import queue
import argparse
import json
import re
import atexit
import traceback
import glob
from flask import Flask, request, jsonify, Response

# 平台兼容：resource 模块仅 Linux 可用
try:
    import resource
except ImportError:
    resource = None  # Windows 开发环境

# 使用共享类型定义（消除 3 份重复代码）
from rkllm_types import (
    RKLLMParam, RKLLMInput, RKLLMResult, RKLLMInferParam,
    RKLLMLoraAdapter, RKLLMLoraParam, RKLLMPromptCacheParam,
    RKLLMExtendParam, RKLLMInputUnion,
    RKLLMInferMode, RKLLMInputType, LLMCallState,
    RKLLM_Handle_t, RKLLMCallback, RKLLM_Callback_t, drain_queue,
    make_rkllm_callback,
)

app = Flask(__name__)
rkllm_model = None
ALLOWED_IMAGE_DIRS = ["/tmp", "/data/images", "/var/tmp"]

# Set the dynamic library path
rkllm_lib = ctypes.CDLL('lib/librkllmrt.so')
userdata = ctypes.c_void_p(None)

# ── 线程安全的并发控制 ──────────────────────────────────────────
# 1. lock：串行化推理请求（同一时刻只允许一个推理）
lock = threading.Lock()

# 2. _model_ready：模型初始化完成后 set()
#    替代原来的 global_state==0 就绪检查，消除竞态
_model_ready = threading.Event()

# 3. _prefill_event：prefill-only 模式控制
#    set() = 只做 prefill 写 KV、不生成输出
#    clear() = 正常推理
#    替代原来的裸 bool _prefill_only，消除回调线程竞态
_prefill_event = threading.Event()

# ── 推理输出 ────────────────────────────────────────────────────
global_text = queue.SimpleQueue()
global_state = -1
token_event = threading.Event()  # 新 token 到达时通知消费侧

# 当前 RKLLM 调用状态。Flask 使用 threaded=True，取消请求必须能在推理
# 请求持锁期间并行进入，因此这里不能复用推理锁。
from inference_cancellation import InferenceCancellation

_call_cancellation = InferenceCancellation()
_active_call_done = threading.Event()
_active_call_done.set()
_active_call_cancelled = _call_cancellation.cancelled

# 视觉推理期间允许后续请求等待同一 RKLLM 句柄，避免短暂 503 触发上层重试风暴。
# 取消请求只负责设置取消标记；真正释放句柄必须等待当前 C 调用返回。
INFERENCE_LOCK_WAIT_SECONDS = 30.0
NPU_PRIORITY_DIR = os.environ.get(
    "VOICE_ASSISTANT_NPU_PRIORITY_DIR",
    "/tmp/voice_assistant_npu_priority",
)

# 预编码是后台优化任务，不能与已经到达的文本/视觉请求竞争锁。
_waiting_requests_lock = threading.Lock()
_waiting_requests = 0


def _read_priority_expiry(path):
    try:
        with open(path, "r", encoding="utf-8") as file_obj:
            return float(json.load(file_obj).get("expires_at", 0))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return 0.0


def _text_priority_active():
    now = time.time()
    active = False
    for path in glob.glob(os.path.join(NPU_PRIORITY_DIR, "*.priority")):
        if _read_priority_expiry(path) > now:
            active = True
            continue
        try:
            os.unlink(path)
        except OSError:
            pass
    return active


def _acquire_text_priority(owner="rkllm", ttl_seconds=120.0):
    os.makedirs(NPU_PRIORITY_DIR, exist_ok=True)
    path = os.path.join(NPU_PRIORITY_DIR, f"text-{os.getpid()}-{owner}.priority")
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as file_obj:
        json.dump(
            {"owner": owner, "expires_at": time.time() + ttl_seconds},
            file_obj,
        )
    os.replace(tmp_path, path)
    return path


def _release_text_priority(path):
    if not path:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _mark_request_waiting():
    global _waiting_requests
    with _waiting_requests_lock:
        _waiting_requests += 1


def _unmark_request_waiting():
    global _waiting_requests
    with _waiting_requests_lock:
        _waiting_requests = max(0, _waiting_requests - 1)


def _has_waiting_request():
    with _waiting_requests_lock:
        return _waiting_requests > 0


def _begin_call(kind, conversation_id=None):
    if not _call_cancellation.start(kind, conversation_id):
        return False
    _active_call_done.clear()
    return True


def _finish_call():
    _call_cancellation.finish()
    _active_call_done.set()


def _is_call_cancelled():
    return _active_call_cancelled.is_set()


# ── 回调函数 ────────────────────────────────────────────────────
def callback_impl(result, userdata, state):
    global global_state
    if state == LLMCallState.RKLLM_RUN_FINISH:
        global_state = state
        if not _prefill_event.is_set():
            print("\n")
        sys.stdout.flush()
        token_event.set()
    elif state == LLMCallState.RKLLM_RUN_ERROR:
        global_state = state
        print("[RKLLM] 推理出错 (RUN_ERROR)")
        sys.stdout.flush()
        token_event.set()
    elif state == LLMCallState.RKLLM_RUN_NORMAL:
        global_state = state
        if _active_call_cancelled.is_set():
            # 回调返回 1 的官方语义是“暂停并等待下一次 rkllm_run 恢复”，
            # 不能用于取消。否则句柄会停在 suspended 状态，下一次文本
            # 推理先等待十余秒再 RUN_ERROR。取消由并行调用的 rkllm_abort
            # 完成；这里仅丢弃已经在途的 token，并让 runtime 正常收尾。
            token_event.set()
            return 0
        if _prefill_event.is_set():
            # prefill 阶段已将 user 消息写入 KV，第一个 decode token 到达即可中断
            # 返回 1 挂起本次 run，避免把慢速 prefill 输出送入队列。
            return 1
        if result.contents.text is not None:
            global_text.put(result.contents.text.decode('utf-8'))
            token_event.set()
    return 0

callback = RKLLM_Callback_t(callback_impl)


# ── 辅助函数 ────────────────────────────────────────────────────

def _is_path_allowed(path):
    real_path = os.path.realpath(path)
    for allowed_dir in ALLOWED_IMAGE_DIRS:
        if not allowed_dir:
            continue
        allowed_real = os.path.realpath(allowed_dir)
        try:
            if os.path.commonpath([allowed_real, real_path]) == allowed_real:
                return True
        except ValueError:
            continue
    return False

def _static_prompt(prompt):
    """Strip dynamic blocks for system-prompt change detection.
    - <context>...</context>  : time/date injected every minute by dialogue.py
    - <tool_calling>...end    : tool reminder appended by connection.py after 2+ turns
    """
    if not prompt:
        return ""
    result = re.sub(r'<context>.*?</context>\s*', '', prompt, flags=re.DOTALL)
    result = re.sub(r'\n*<tool_calling>.*$', '', result, flags=re.DOTALL)
    result = re.sub(r'\s+', '', result)
    return result


class _PrepareError(Exception):
    """推理预处理阶段的可恢复错误，用于提前返回 HTTP 响应"""
    def __init__(self, response, status_code=400):
        self.response = response
        self.status_code = status_code


def _recover_after_run_error(reason, keep_system_prompt=1):
    """RKLLM 运行异常后清理易污染的对话 KV，避免后续请求被拖慢。"""
    global global_state

    print(f"[RKLLM] {reason}，执行异常恢复：清理对话KV", flush=True)
    try:
        rkllm_model.clear_kv_cache(keep_system_prompt=keep_system_prompt)
    except Exception as exc:
        print(f"[RKLLM] 清理KV失败: {exc}", flush=True)
    drain_queue(global_text)
    global_state = -1


def _prepare_inference(data):
    """流式 / 非流式分支的公共预处理逻辑。

    职责：
    1. 提取 system_prompt、合并 tool_calling 指令
    2. 检测 system prompt 结构变化 → KV 重置
    3. 设置 function tools
    4. KV cache 溢出保护
    5. 计算增量消息 delta
    6. 中间消息 prefill-only 喂入

    Returns:
        (new_msgs, enable_thinking, last_msg) — 最后一条消息供调用方生成输出

    Raises:
        _PrepareError — 需提前终止并返回 HTTP 响应
    """
    messages = data['messages']
    enable_thinking = data.get('enable_thinking')
    TOOLS = data.get('tools')
    msg_roles = [m.get('role', '?') for m in messages]
    print(f"Received messages: {len(messages)}条 [{', '.join(msg_roles)}]")

    # ── 提取 system_prompt 与 tool_calling 注入 ──────────────
    tool_instructions = ""
    for msg in messages:
        if msg.get('role') == 'user' and '<tool_calling>' in msg.get('content', ''):
            tool_instructions = msg['content']
            break

    system_prompt = ""
    for msg in messages:
        if msg.get('role') == 'system':
            system_prompt = msg['content']
            if tool_instructions:
                system_prompt = system_prompt + "\n" + tool_instructions
            break

    conversation_id = str(data.get("conversation_id") or "").strip()
    if not conversation_id:
        raise _PrepareError(
            jsonify({
                "status": "error",
                "message": "conversation_id is required to isolate RKLLM KV state",
            }),
            400,
        )
    if not system_prompt.strip():
        raise _PrepareError(
            jsonify({
                "status": "error",
                "message": "A non-empty system message is required",
            }),
            400,
        )

    # ── 静态结构变化才重置 KV（忽略 <context> 时间动态变化）────────
    static_current = _static_prompt(rkllm_model._current_system_prompt)
    static_new = _static_prompt(system_prompt)
    if static_new != static_current:
        if rkllm_model._current_system_prompt is not None:
            print(f"[KV] system prompt 结构变化，执行全量 KV 重置")
            print(f"[DEBUG] static_current length: {len(static_current)}, static_new length: {len(static_new)}")
            for i, (c1, c2) in enumerate(zip(static_current, static_new)):
                if c1 != c2:
                    print(f"[DEBUG] Difference at index {i}: current='{c1}', new='{c2}'")
                    print(f"[DEBUG] Context: current='{static_current[max(0, i-10):i+10]}', new='{static_new[max(0, i-10):i+10]}'")
                    break
            rkllm_model.clear_kv_cache(keep_system_prompt=0)
            rkllm_model.tools = None
        else:
            print(f"[KV] 首次请求，设置 system prompt（保留启动缓存）")
        rkllm_model._current_system_prompt = system_prompt
    elif (
        rkllm_model._active_conversation_id is not None
        and rkllm_model._active_conversation_id != conversation_id
    ):
        print(
            f"[KV] 会话切换 {rkllm_model._active_conversation_id[:8]} -> "
            f"{conversation_id[:8]}，清理旧对话KV"
        )
        rkllm_model.clear_kv_cache(keep_system_prompt=1)
        rkllm_model._current_system_prompt = system_prompt

    rkllm_model._active_conversation_id = conversation_id

    rkllm_model.set_function_tools(
        system_prompt=system_prompt,
        tools=json.dumps(TOOLS) if TOOLS is not None else "[]",
        tool_response_str="tool_response" if TOOLS is not None else "none"
    )

    # ── KV cache 溢出保护（保留 system prompt）──────────────
    kv_size = rkllm_model.get_kv_cache_size()
    kv_limit = rkllm_model.max_context_len
    kv_threshold = int(kv_limit * 0.85)
    print(f"[KV] cache size: {kv_size}/{kv_limit} tokens")
    if kv_size > kv_threshold:
        print(f"[KV] 缓存接近上限，清理对话历史")
        rkllm_model.clear_kv_cache(keep_system_prompt=1)
        rkllm_model._sent_count = 0

    # ── 增量推理：只发 user/tool 新增消息（delta）──────────
    sendable = [
        m for m in messages
        if m.get('role') in ('user', 'tool')
        and '<tool_calling>' not in m.get('content', '')
    ]

    # ── 客户端 trim_history 触发 KV 重置 ──────────────────
    if data.get('reset_conversation'):
        print(f"[KV] 客户端 trim 触发，重置对话 KV")
        rkllm_model.clear_kv_cache(keep_system_prompt=1)
        rkllm_model._current_system_prompt = system_prompt
        rkllm_model._sent_count = max(0, len(sendable) - 1)

    new_msgs = sendable[rkllm_model._sent_count:]
    print(f"[KV] sent_count={rkllm_model._sent_count}, total_sendable={len(sendable)}, delta={len(new_msgs)}")

    # 新会话检测：sent_count > sendable 说明对话历史被重置（新 WebSocket）
    if not new_msgs and rkllm_model._sent_count > 0:
        print(f"[KV] 检测到新会话，清理对话历史（保留 system prompt）")
        rkllm_model.clear_kv_cache(keep_system_prompt=1)
        rkllm_model._current_system_prompt = system_prompt
        rkllm_model._sent_count = max(0, len(sendable) - 1)
        new_msgs = sendable[rkllm_model._sent_count:]
    if not new_msgs:
        raise _PrepareError(jsonify({'status': 'error', 'message': 'No new messages to process'}))

    # 历史 delta 落后时不要用 prefill-only 补写 KV。
    # RKLLM 的 prefill-only 需要跑到首 token 再中断，实测会触发 rkllm_run=-1，
    # 且可能把下一条控制指令拖到十几秒。这里直接清理对话 KV，只处理最新输入。
    if len(new_msgs) > 1:
        skipped = len(new_msgs) - 1
        print(
            f"[KV] 检测到 {skipped} 条历史消息未同步，跳过慢速prefill，"
            f"清理对话KV后仅处理最新消息"
        )
        rkllm_model.clear_kv_cache(keep_system_prompt=1)
        rkllm_model._current_system_prompt = system_prompt
        rkllm_model._sent_count = max(0, len(sendable) - 1)
        new_msgs = sendable[rkllm_model._sent_count:]

    return new_msgs, enable_thinking


# ── RKLLM 模型封装 ──────────────────────────────────────────────

class RKLLM(object):
    def __init__(
        self,
        model_path,
        lora_model_path=None,
        prompt_cache_path=None,
        platform="rk3588",
        max_context_len=4096,
        multimodal_enabled=False,
        text_max_new_tokens=256,
    ):
        rkllm_param = RKLLMParam()
        rkllm_param.model_path = bytes(model_path, 'utf-8')
        self.platform = platform.lower()
        self.multimodal_enabled = multimodal_enabled

        rkllm_param.max_context_len = max_context_len
        self.max_context_len = max_context_len
        self.text_max_new_tokens = max(16, int(text_max_new_tokens))
        rkllm_param.max_new_tokens = self.text_max_new_tokens
        rkllm_param.skip_special_token = True
        rkllm_param.n_keep = -1
        rkllm_param.top_k = 1
        rkllm_param.top_p = 0.9
        rkllm_param.temperature = 0.8
        rkllm_param.repeat_penalty = 1.1
        rkllm_param.frequency_penalty = 0.0
        rkllm_param.presence_penalty = 0.0

        rkllm_param.mirostat = 0
        rkllm_param.mirostat_tau = 5.0
        rkllm_param.mirostat_eta = 0.1

        rkllm_param.ignore_eos_token = False
        rkllm_param.is_async = False

        if multimodal_enabled and self.platform in ("rk3588", "rk3576"):
            # 与 RKNN-LLM 官方多模态 demo 保持一致；同一个 handle 仍可处理纯文本 prompt。
            rkllm_param.extend_param.base_domain_id = 1
        else:
            rkllm_param.extend_param.base_domain_id = 0
        rkllm_param.extend_param.embed_flash = 1
        rkllm_param.extend_param.n_batch = 1
        rkllm_param.extend_param.use_cross_attn = 0
        rkllm_param.extend_param.enabled_cpus_num = 4
        if self.platform in ["rk3576", "rk3588"]:
            rkllm_param.extend_param.enabled_cpus_mask = (1 << 4)|(1 << 5)|(1 << 6)|(1 << 7)
        else:
            rkllm_param.extend_param.enabled_cpus_mask = (1 << 0)|(1 << 1)|(1 << 2)|(1 << 3)

        self.handle = RKLLM_Handle_t()
        self._rkllm_callback = make_rkllm_callback(callback)

        self.rkllm_init = rkllm_lib.rkllm_init
        self.rkllm_init.argtypes = [ctypes.POINTER(RKLLM_Handle_t), ctypes.POINTER(RKLLMParam), ctypes.POINTER(RKLLMCallback)]
        self.rkllm_init.restype = ctypes.c_int
        ret = self.rkllm_init(ctypes.byref(self.handle), ctypes.byref(rkllm_param), ctypes.byref(self._rkllm_callback))
        if ret != 0:
            print("\nrkllm init failed\n")
            sys.exit(1)  # 修复：失败应返回错误码
        else:
            print("\nrkllm init success!\n")

        self.rkllm_run = rkllm_lib.rkllm_run
        self.rkllm_run.argtypes = [RKLLM_Handle_t, ctypes.POINTER(RKLLMInput), ctypes.POINTER(RKLLMInferParam), ctypes.c_void_p]
        self.rkllm_run.restype = ctypes.c_int
        
        self.set_chat_template = rkllm_lib.rkllm_set_chat_template
        self.set_chat_template.argtypes = [RKLLM_Handle_t, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
        self.set_chat_template.restype = ctypes.c_int
        
        self.set_function_tools_ = rkllm_lib.rkllm_set_function_tools
        self.set_function_tools_.argtypes = [RKLLM_Handle_t, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
        self.set_function_tools_.restype = ctypes.c_int

        self.rkllm_destroy = rkllm_lib.rkllm_destroy
        self.rkllm_destroy.argtypes = [RKLLM_Handle_t]
        self.rkllm_destroy.restype = ctypes.c_int
        
        self.rkllm_abort = rkllm_lib.rkllm_abort
        self.rkllm_abort.argtypes = [RKLLM_Handle_t]
        self.rkllm_abort.restype = ctypes.c_int
        self.rkllm_is_running = rkllm_lib.rkllm_is_running
        self.rkllm_is_running.argtypes = [RKLLM_Handle_t]
        self.rkllm_is_running.restype = ctypes.c_int

        rkllm_lora_params = None
        if lora_model_path:
            lora_adapter_name = "test"
            lora_adapter = RKLLMLoraAdapter()
            ctypes.memset(ctypes.byref(lora_adapter), 0, ctypes.sizeof(RKLLMLoraAdapter))
            lora_adapter.lora_adapter_path = ctypes.c_char_p((lora_model_path).encode('utf-8'))
            lora_adapter.lora_adapter_name = ctypes.c_char_p((lora_adapter_name).encode('utf-8'))
            lora_adapter.scale = 1.0

            rkllm_load_lora = rkllm_lib.rkllm_load_lora
            rkllm_load_lora.argtypes = [RKLLM_Handle_t, ctypes.POINTER(RKLLMLoraAdapter)]
            rkllm_load_lora.restype = ctypes.c_int
            rkllm_load_lora(self.handle, ctypes.byref(lora_adapter))
            rkllm_lora_params = RKLLMLoraParam()
            rkllm_lora_params.lora_adapter_name = ctypes.c_char_p((lora_adapter_name).encode('utf-8'))
        
        self.rkllm_infer_params = RKLLMInferParam()
        ctypes.memset(ctypes.byref(self.rkllm_infer_params), 0, ctypes.sizeof(RKLLMInferParam))
        self.rkllm_infer_params.mode = RKLLMInferMode.RKLLM_INFER_GENERATE
        self.rkllm_infer_params.lora_params = ctypes.pointer(rkllm_lora_params) if rkllm_lora_params else None
        self.rkllm_infer_params.keep_history = 1  # 跨请求保留 KV cache，实现增量推理
        self.rkllm_infer_params.max_new_tokens = self.text_max_new_tokens

        # ── KV cache 管理 API ──────────────────────────────────────
        self.rkllm_get_kv_cache_size_fn = rkllm_lib.rkllm_get_kv_cache_size
        self.rkllm_get_kv_cache_size_fn.argtypes = [RKLLM_Handle_t, ctypes.POINTER(ctypes.c_int)]
        self.rkllm_get_kv_cache_size_fn.restype = ctypes.c_int

        self.rkllm_clear_kv_cache_fn = rkllm_lib.rkllm_clear_kv_cache
        self.rkllm_clear_kv_cache_fn.argtypes = [
            RKLLM_Handle_t, ctypes.c_int,
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)
        ]
        self.rkllm_clear_kv_cache_fn.restype = ctypes.c_int

        # ── 会话状态（增量推理所需）────────────────────────────────
        self._sent_count = 0
        self._current_system_prompt = None
        self._active_conversation_id = None

        self.prompt_cache_path = prompt_cache_path

        self.rkllm_load_prompt_cache_fn = rkllm_lib.rkllm_load_prompt_cache
        self.rkllm_load_prompt_cache_fn.argtypes = [RKLLM_Handle_t, ctypes.c_char_p]
        self.rkllm_load_prompt_cache_fn.restype = ctypes.c_int

        if prompt_cache_path and os.path.exists(prompt_cache_path):
            ret = self.rkllm_load_prompt_cache_fn(
                self.handle, ctypes.c_char_p(prompt_cache_path.encode('utf-8'))
            )
            if ret == 0:
                print(f"[Cache] Prompt cache loaded from {prompt_cache_path}")
            else:
                print(f"[Cache] Failed to load prompt cache (ret={ret})")
        elif prompt_cache_path:
            print(f"[Cache] Cache file not found, will generate on first session: {prompt_cache_path}")

        self.tools = None
        self.vision_enabled = False
        self.rknn = None
        self._embed_cache = None
        self._embed_cache_key = None
        self._vision_preencode_stop = threading.Event()
        self._vision_preencode_thread = None
        self._vision_preencode_path = None
        self._vision_preencode_interval = 0.0

    def warmup_function_tools(self, system_prompt_file):
        """启动时预热 set_function_tools + 触发 KV prefill（将 ~8s 移到启动阶段）"""
        if not system_prompt_file or not os.path.exists(system_prompt_file):
            print(f"[Warmup] No system prompt file, will init tools on first request")
            return
        with open(system_prompt_file, 'r', encoding='utf-8') as f:
            static_prompt = f.read().strip()

        print(f"[Warmup] Calling set_function_tools at startup ({len(static_prompt)} chars)...")
        tools_json = "[]"
        self.set_function_tools_(self.handle,
            ctypes.c_char_p(static_prompt.encode('utf-8')),
            ctypes.c_char_p(tools_json.encode('utf-8')),
            ctypes.c_char_p(b"none"))
        self.tools = tools_json
        print(f"[Warmup] set_function_tools done.")

        print(f"[Warmup] Running dummy inference to prefill KV...")
        global global_state
        drain_queue(global_text)
        global_state = -1
        self.run("user", False, ".")
        drain_queue(global_text)

        self.clear_kv_cache(keep_system_prompt=1)
        self._current_system_prompt = static_prompt
        print(f"[Warmup] KV prefill done, system prompt cached ({self.get_kv_cache_size()} tokens)")


    def set_function_tools(self, system_prompt, tools, tool_response_str):
        """仅当 tools 变化时才调用 C API（避免重复 prefill 8s）"""
        if self.tools is None or self.tools != tools:
            self.tools = tools
            self.set_function_tools_(self.handle, ctypes.c_char_p(system_prompt.encode('utf-8')), ctypes.c_char_p(tools.encode('utf-8')),  ctypes.c_char_p(tool_response_str.encode('utf-8')))
            print(f"[Tools] 工具列表变化，已调用 set_function_tools")

    def get_kv_cache_size(self):
        """返回当前 KV cache 已占用的 token 数"""
        size = ctypes.c_int(0)
        self.rkllm_get_kv_cache_size_fn(self.handle, ctypes.byref(size))
        return size.value

    def clear_kv_cache(self, keep_system_prompt=1):
        """清除 KV cache。keep_system_prompt=1 仅清除对话历史，保留 system prompt token。"""
        self.rkllm_clear_kv_cache_fn(self.handle, ctypes.c_int(keep_system_prompt), None, None)
        self._sent_count = 0
        if not keep_system_prompt:
            self._current_system_prompt = None
            if self.prompt_cache_path and os.path.exists(self.prompt_cache_path):
                ret = self.rkllm_load_prompt_cache_fn(
                    self.handle, ctypes.c_char_p(self.prompt_cache_path.encode('utf-8'))
                )
                if ret == 0:
                    print(f"[Cache] Prompt cache reloaded after KV reset")
                else:
                    print(f"[Cache] Failed to reload prompt cache (ret={ret})")
        print(f"[Cache] KV cleared (keep_system_prompt={keep_system_prompt})")

    def run(self, *param):
        role, enable_thinking, prompt = param
        # RKLLM C 库对 role='tool' 要求 content 为 JSON 数组格式。
        # xiaozhi 的 tool response 是自然语言文本，直接用 role='user' 送入即可。
        if role == 'tool':
            role = 'user'
        rkllm_input = RKLLMInput()
        rkllm_input.role = role.encode('utf-8') if role is not None else "user".encode('utf-8')
        rkllm_input.enable_thinking = ctypes.c_bool(enable_thinking if enable_thinking is not None else False)
        rkllm_input.input_type = RKLLMInputType.RKLLM_INPUT_PROMPT
        rkllm_input.input_data.prompt_input = ctypes.c_char_p(prompt.encode('utf-8'))
        ret = self.rkllm_run(
            self.handle,
            ctypes.byref(rkllm_input),
            ctypes.byref(self.rkllm_infer_params),
            None,
        )
        if ret != 0:
            raise RuntimeError(f"rkllm_run failed: {ret}")
        return

    def set_request_max_tokens(self, requested):
        """按请求收紧输出上限，不允许超过服务启动时配置的上限。"""
        try:
            requested = int(requested)
        except (TypeError, ValueError):
            requested = self.text_max_new_tokens
        effective = max(16, min(requested, self.text_max_new_tokens))
        self.rkllm_infer_params.max_new_tokens = effective
        return effective

    def load_vision_encoder(
        self,
        rknn_model_path,
        npu_core=3,
        max_new_tokens=96,
        image_width=448,
        image_height=448,
        img_start="<|vision_start|>",
        img_end="<|vision_end|>",
        img_content="<|image_pad|>",
    ):
        try:
            import cv2
            import numpy as np
            from rknnlite.api import RKNNLite
        except ImportError as exc:
            raise RuntimeError("缺少视觉依赖，请检查 opencv-python、numpy、rknn-toolkit-lite2") from exc

        self._cv2 = cv2
        self._np = np
        self.vision_max_new_tokens = max(16, int(max_new_tokens))
        self.vision_img_start = img_start.encode("utf-8")
        self.vision_img_end = img_end.encode("utf-8")
        self.vision_img_content = img_content.encode("utf-8")

        print(f"[Vision] Loading vision encoder: {rknn_model_path}")
        self.rknn = RKNNLite()
        ret = self.rknn.load_rknn(rknn_model_path)
        if ret != 0:
            raise RuntimeError(f"Load RKNN vision model failed: {ret}")

        if npu_core == 0:
            core_mask = RKNNLite.NPU_CORE_0
        elif npu_core == 1:
            core_mask = RKNNLite.NPU_CORE_1
        elif npu_core == 2:
            core_mask = RKNNLite.NPU_CORE_2
        elif npu_core == 12:
            core_mask = RKNNLite.NPU_CORE_0_1
        elif npu_core == 3:
            core_mask = RKNNLite.NPU_CORE_0_1_2
        else:
            core_mask = RKNNLite.NPU_CORE_AUTO

        ret = self.rknn.init_runtime(core_mask=core_mask)
        if ret != 0:
            raise RuntimeError(f"Init RKNN vision runtime failed: {ret}")

        self.vision_model_width = image_width
        self.vision_model_height = image_height
        dummy_input = np.zeros((1, image_height, image_width, 3), dtype=np.uint8)
        dummy_outputs = self.rknn.inference(inputs=[dummy_input])

        self.vision_n_output = len(dummy_outputs)
        flat_shape = list(dummy_outputs[0].shape)
        self.vision_n_image_tokens = 0
        self.vision_embed_size = 0
        for i, dim in enumerate(flat_shape):
            if dim > 1:
                self.vision_n_image_tokens = dim
                if i + 1 < len(flat_shape):
                    self.vision_embed_size = flat_shape[i + 1]
                break

        if self.vision_n_image_tokens == 0:
            self.vision_n_image_tokens = 3136
            self.vision_embed_size = flat_shape[-1] if flat_shape else 1536

        self.vision_enabled = True
        print(f"[Vision] Encoder ready: {image_width}x{image_height}, "
              f"tokens={self.vision_n_image_tokens}, embed={self.vision_embed_size}, "
              f"outputs={self.vision_n_output}")

    def process_image(self, image_path):
        process_started_at = time.monotonic()
        if not self.vision_enabled or self.rknn is None:
            raise RuntimeError("视觉编码器未启用")
        if not os.path.exists(image_path):
            raise FileNotFoundError(f"Image not found: {image_path}")

        cv2 = self._cv2
        np = self._np
        img_stat = os.stat(image_path)
        cache_key = (image_path, img_stat.st_mtime_ns, img_stat.st_size)
        if self._embed_cache is not None and self._embed_cache_key == cache_key:
            elapsed_ms = int((time.monotonic() - process_started_at) * 1000)
            print(
                f"[性能][Vision] 复用图像编码缓存，总耗时={elapsed_ms}毫秒 "
                f"path={image_path}",
                flush=True,
            )
            return self._embed_cache

        read_started_at = time.monotonic()
        img = cv2.imread(image_path)
        if img is None:
            raise ValueError(f"Failed to read image: {image_path}")
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        read_elapsed_ms = int((time.monotonic() - read_started_at) * 1000)

        preprocess_started_at = time.monotonic()
        h, w = img.shape[:2]
        if h == w:
            square = img.copy()
        else:
            size = max(h, w)
            square = np.full((size, size, 3), 128, dtype=np.uint8)
            y_off = (size - h) // 2
            x_off = (size - w) // 2
            square[y_off:y_off + h, x_off:x_off + w] = img

        resized = cv2.resize(
            square,
            (self.vision_model_width, self.vision_model_height),
            interpolation=cv2.INTER_LINEAR,
        )
        nhwc = resized[np.newaxis, :, :, :].copy()
        preprocess_elapsed_ms = int((time.monotonic() - preprocess_started_at) * 1000)

        encode_started_at = time.monotonic()
        outputs = self.rknn.inference(inputs=[nhwc])
        encode_elapsed_ms = int((time.monotonic() - encode_started_at) * 1000)

        embed_started_at = time.monotonic()
        if len(outputs) == 1:
            embed = outputs[0].flatten().astype(np.float32)
        else:
            n_tokens = self.vision_n_image_tokens
            embed_size = self.vision_embed_size
            n_out = len(outputs)
            embed = np.zeros(n_tokens * embed_size * n_out, dtype=np.float32)
            for i in range(n_tokens):
                for j in range(n_out):
                    out_flat = outputs[j].flatten().astype(np.float32)
                    src_offset = i * embed_size
                    dst_offset = i * n_out * embed_size + j * embed_size
                    embed[dst_offset:dst_offset + embed_size] = out_flat[src_offset:src_offset + embed_size]
        embed_elapsed_ms = int((time.monotonic() - embed_started_at) * 1000)

        self._embed_cache = embed.copy()
        self._embed_cache_key = cache_key
        total_elapsed_ms = int((time.monotonic() - process_started_at) * 1000)
        print(
            f"[性能][Vision] 图像编码完成，总耗时={total_elapsed_ms}毫秒，"
            f"读取={read_elapsed_ms}毫秒，预处理={preprocess_elapsed_ms}毫秒，"
            f"RKNN编码={encode_elapsed_ms}毫秒，整理={embed_elapsed_ms}毫秒，"
            f"size={img_stat.st_size}, mtime_ns={img_stat.st_mtime_ns}",
            flush=True,
        )
        return embed

    def start_vision_preencoder(self, image_path, interval=0.0):
        try:
            interval = float(interval)
        except (TypeError, ValueError):
            interval = 0.0
        if not self.vision_enabled or not image_path or interval <= 0:
            return
        if self._vision_preencode_thread and self._vision_preencode_thread.is_alive():
            return

        self._vision_preencode_path = image_path
        self._vision_preencode_interval = interval
        self._vision_preencode_stop.clear()

        def worker():
            last_error = None
            while not self._vision_preencode_stop.is_set():
                try:
                    if os.path.exists(image_path):
                        img_stat = os.stat(image_path)
                        cache_key = (image_path, img_stat.st_mtime_ns, img_stat.st_size)
                        if (
                            self._embed_cache_key != cache_key
                            and not _has_waiting_request()
                            and not _text_priority_active()
                            and lock.acquire(blocking=False)
                        ):
                            try:
                                if (
                                    not _has_waiting_request()
                                    and not _text_priority_active()
                                    and self._embed_cache_key != cache_key
                                ):
                                    started_at = time.monotonic()
                                    self.process_image(image_path)
                                    elapsed_ms = int((time.monotonic() - started_at) * 1000)
                                    print(
                                        f"[性能][Vision] 空闲预编码完成，耗时={elapsed_ms}毫秒 "
                                        f"path={image_path}",
                                        flush=True,
                                    )
                            finally:
                                lock.release()
                    last_error = None
                except Exception as exc:
                    current_error = str(exc)
                    if current_error != last_error:
                        print(f"[Vision] 空闲预编码跳过: {current_error}", flush=True)
                    last_error = current_error
                wait_seconds = (
                    min(interval, 0.2)
                    if _text_priority_active()
                    else interval
                )
                self._vision_preencode_stop.wait(wait_seconds)

        self._vision_preencode_thread = threading.Thread(
            target=worker,
            name="vision-preencoder",
            daemon=True,
        )
        self._vision_preencode_thread.start()
        print(
            f"[Vision] 空闲预编码已启用: path={image_path}, interval={interval}s",
            flush=True,
        )

    def run_vision(self, question, image_path, max_new_tokens=None):
        request_started_at = time.monotonic()
        embed = self.process_image(image_path)
        embed_elapsed_ms = int((time.monotonic() - request_started_at) * 1000)
        embed_ptr = embed.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        prompt_bytes = ("<image>" + question).encode("utf-8")

        rkllm_input = RKLLMInput()
        rkllm_input.role = b"user"
        rkllm_input.enable_thinking = ctypes.c_bool(False)
        rkllm_input.input_type = RKLLMInputType.RKLLM_INPUT_MULTIMODAL
        rkllm_input.input_data.multimodal_input.prompt = prompt_bytes
        rkllm_input.input_data.multimodal_input.image.image_embed = embed_ptr
        rkllm_input.input_data.multimodal_input.image.n_image_tokens = self.vision_n_image_tokens
        rkllm_input.input_data.multimodal_input.image.n_image = 1
        rkllm_input.input_data.multimodal_input.image.image_start = self.vision_img_start
        rkllm_input.input_data.multimodal_input.image.image_end = self.vision_img_end
        rkllm_input.input_data.multimodal_input.image.image_content = self.vision_img_content
        rkllm_input.input_data.multimodal_input.image.image_width = self.vision_model_width
        rkllm_input.input_data.multimodal_input.image.image_height = self.vision_model_height

        infer_params = RKLLMInferParam()
        ctypes.memset(ctypes.byref(infer_params), 0, ctypes.sizeof(RKLLMInferParam))
        infer_params.mode = RKLLMInferMode.RKLLM_INFER_GENERATE
        infer_params.keep_history = 0
        try:
            requested_tokens = (
                self.vision_max_new_tokens
                if max_new_tokens is None
                else int(max_new_tokens)
            )
        except (TypeError, ValueError):
            requested_tokens = self.vision_max_new_tokens
        infer_params.max_new_tokens = max(
            16,
            min(requested_tokens, self.vision_max_new_tokens),
        )

        generate_started_at = time.monotonic()
        ret = self.rkllm_run(
            self.handle,
            ctypes.byref(rkllm_input),
            ctypes.byref(infer_params),
            None,
        )
        if ret != 0:
            raise RuntimeError(f"rkllm_run vision failed: {ret}")
        generate_elapsed_ms = int((time.monotonic() - generate_started_at) * 1000)
        total_elapsed_ms = int((time.monotonic() - request_started_at) * 1000)
        print(
            f"[性能][Vision] 多模态推理返回，总耗时={total_elapsed_ms}毫秒，"
            f"图像编码={embed_elapsed_ms}毫秒，RKLLM生成={generate_elapsed_ms}毫秒，"
            f"max_tokens={infer_params.max_new_tokens}",
            flush=True,
        )

    def abort(self):
        return self.rkllm_abort(self.handle)

    def release(self):
        if self._vision_preencode_stop:
            self._vision_preencode_stop.set()
        if self.rknn is not None:
            try:
                self.rknn.release()
            except Exception:
                pass
        self.rkllm_destroy(self.handle)


# ── 主程序入口 ──────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--rkllm_model_path', type=str, required=True, help='Absolute path of the converted RKLLM model on the Linux board;')
    parser.add_argument('--target_platform', type=str, required=True, help='Target platform: e.g., rk3588/rk3576;')
    parser.add_argument('--lora_model_path', type=str, help='Absolute path of the lora_model on the Linux board;')
    parser.add_argument('--prompt_cache_path', type=str, help='Absolute path of the prompt_cache file on the Linux board;')
    parser.add_argument('--system_prompt_file', type=str, help='Path to static system prompt file for startup warmup (avoids 8s delay on first request);')
    parser.add_argument('--max_context_len', type=int, default=4096, help='Max context length (must <= model max_context_limit);')
    parser.add_argument('--text_max_new_tokens', type=int, default=256, help='Maximum generated tokens for text chat requests.')
    parser.add_argument('--port', type=int, default=int(os.environ.get('RKLLM_PORT', 18080)), help='Server port (also via RKLLM_PORT env var)')
    parser.add_argument('--vision_rknn_model_path', type=str, default=None, help='Optional RKNN vision encoder model path; if set, /vision/analyze is enabled in this process.')
    parser.add_argument('--vision_npu_core', type=int, default=3, help='Vision encoder NPU core: 0/1/2, 12 dual, 3 triple, other auto.')
    parser.add_argument('--vision_max_new_tokens', type=int, default=48)
    parser.add_argument('--vision_image_width', type=int, default=448)
    parser.add_argument('--vision_image_height', type=int, default=448)
    parser.add_argument('--vision_img_start', default='<|vision_start|>')
    parser.add_argument('--vision_img_end', default='<|vision_end|>')
    parser.add_argument('--vision_img_content', default='<|image_pad|>')
    parser.add_argument('--vision_allowed_dirs', type=str, default=None, help='Comma-separated list of allowed image directories for /vision/analyze.')
    parser.add_argument('--vision_preencode_path', type=str, default="/tmp/voice_assistant_latest_vision.jpg")
    parser.add_argument('--vision_preencode_interval', type=float, default=0.0)
    args = parser.parse_args()

    if not os.path.exists(args.rkllm_model_path):
        print("Error: Please provide the correct rkllm model path, and ensure it is the absolute path on the board.")
        sys.stdout.flush()
        sys.exit(1)

    if not (args.target_platform in ["rk3588", "rk3576", "rv1126b", "rk3562"]):
        print("Error: Please specify the correct target platform: rk3588/rk3576/rv1126b/rk3562.")
        sys.stdout.flush()
        sys.exit(1)

    if args.lora_model_path:
        if not os.path.exists(args.lora_model_path):
            print("Error: Please provide the correct lora_model path, and advise it is the absolute path on the board.")
            sys.stdout.flush()
            sys.exit(1)

    if args.vision_rknn_model_path and not os.path.exists(args.vision_rknn_model_path):
        print("Error: Please provide the correct vision RKNN model path.")
        sys.stdout.flush()
        sys.exit(1)

    if args.vision_allowed_dirs:
        ALLOWED_IMAGE_DIRS.clear()
        ALLOWED_IMAGE_DIRS.extend(d.strip() for d in args.vision_allowed_dirs.split(",") if d.strip())

    # Fix frequency
    command = ["sudo", "bash", f"scripts/fix_freq_{args.target_platform}.sh"]
    subprocess.run(command)

    # Set resource limit (Linux only)
    if resource is not None:
        resource.setrlimit(resource.RLIMIT_NOFILE, (102400, 102400))

    # Initialize RKLLM model
    print("=========init....===========")
    sys.stdout.flush()
    model_path = args.rkllm_model_path
    rkllm_model = RKLLM(
        model_path,
        args.lora_model_path,
        args.prompt_cache_path,
        args.target_platform,
        args.max_context_len,
        multimodal_enabled=bool(args.vision_rknn_model_path),
        text_max_new_tokens=args.text_max_new_tokens,
    )

    if args.vision_rknn_model_path:
        rkllm_model.load_vision_encoder(
            args.vision_rknn_model_path,
            npu_core=args.vision_npu_core,
            max_new_tokens=args.vision_max_new_tokens,
            image_width=args.vision_image_width,
            image_height=args.vision_image_height,
            img_start=args.vision_img_start,
            img_end=args.vision_img_end,
            img_content=args.vision_img_content,
        )

    # 注册 atexit 确保资源释放（app.run() 之后的代码不可达）
    atexit.register(rkllm_model.release)

    # ── 启动预热：将 set_function_tools 的 8s 移到此处 ──────────
    rkllm_model.warmup_function_tools(getattr(args, 'system_prompt_file', None))

    # 标记模型就绪
    _model_ready.set()
    if args.vision_rknn_model_path:
        rkllm_model.start_vision_preencoder(
            args.vision_preencode_path,
            args.vision_preencode_interval,
        )

    print("==============================")
    sys.stdout.flush()

    # ── 健康检查端点 ────────────────────────────────────────────
    @app.route('/health', methods=['GET'])
    def health():
        if not _model_ready.is_set():
            return jsonify({"status": "not_ready"}), 503
        return jsonify({
            "status": "ok",
            "kv_cache_size": rkllm_model.get_kv_cache_size(),
            "max_context_len": rkllm_model.max_context_len,
            "text_max_new_tokens": rkllm_model.text_max_new_tokens,
            "model_path": args.rkllm_model_path,
            "vision": {
                "enabled": rkllm_model.vision_enabled,
                "model_width": getattr(rkllm_model, "vision_model_width", None),
                "model_height": getattr(rkllm_model, "vision_model_height", None),
                "max_new_tokens": getattr(rkllm_model, "vision_max_new_tokens", None),
                "img_start": getattr(rkllm_model, "vision_img_start", b"").decode("utf-8"),
                "img_end": getattr(rkllm_model, "vision_img_end", b"").decode("utf-8"),
                "img_content": getattr(rkllm_model, "vision_img_content", b"").decode("utf-8"),
                "n_image_tokens": getattr(rkllm_model, "vision_n_image_tokens", None),
                "embed_size": getattr(rkllm_model, "vision_embed_size", None),
                "n_output": getattr(rkllm_model, "vision_n_output", None),
                "preencode_path": getattr(rkllm_model, "_vision_preencode_path", None),
                "preencode_interval": getattr(rkllm_model, "_vision_preencode_interval", 0.0),
            },
        })

    @app.route('/rkllm_abort', methods=['POST'])
    def abort_inference():
        """停止输出并让同步 RKLLM 调用安全收尾。"""
        if not _model_ready.is_set():
            return jsonify({"status": "not_ready", "aborted": False}), 503

        data = request.get_json(silent=True) or {}
        conversation_id = str(data.get("conversation_id") or "").strip()
        close_session = data.get("close_session") is True
        if close_session and not conversation_id:
            return jsonify({"status": "invalid_conversation", "aborted": False}), 400
        task_kind = _call_cancellation.cancel(conversation_id, close_session)
        if task_kind is None:
            return jsonify({"status": "idle", "aborted": False}), 200

        # 当前板端 runtime 在同步 rkllm_run 期间跨线程调用 rkllm_abort 后，
        # 下一次调用会先耗时十余秒再返回 -1，随后还需一次慢推理才能恢复。
        # 因此这里只设置协作取消标记：回调丢弃后续 token，持锁线程等待
        # 同步 C 调用自然返回并清理对话 KV。前端和 TTS 已由上层立即停止。
        print(
            f"[RKLLM] 收到取消请求，当前任务={task_kind}，等待同步调用安全收尾",
            flush=True,
        )
        return jsonify({
            "status": "cancelling",
            "aborted": True,
            "task": task_kind,
        }), 200

    @app.route('/vision/analyze', methods=['POST'])
    def vision_analyze():
        global global_state

        if not _model_ready.is_set():
            return jsonify({"error": "Model not ready"}), 503
        if not rkllm_model.vision_enabled:
            return jsonify({"error": "Vision encoder not enabled"}), 503

        data = request.json
        if not data or 'image_path' not in data or 'question' not in data:
            return jsonify({"error": "Missing image_path or question"}), 400

        image_path = data['image_path']
        question = data['question']
        if not _is_path_allowed(image_path):
            return jsonify({"error": f"Path not allowed. Allowed dirs: {ALLOWED_IMAGE_DIRS}"}), 403

        lock_started_at = time.monotonic()
        _mark_request_waiting()
        try:
            acquired = lock.acquire(timeout=INFERENCE_LOCK_WAIT_SECONDS)
        finally:
            _unmark_request_waiting()
        if not acquired:
            return jsonify({"error": "RKLLM_Server is busy! Maybe you can try again later."}), 503
        lock_wait_ms = int((time.monotonic() - lock_started_at) * 1000)
        _begin_call("vision")

        model_thread = None
        error_holder = []
        try:
            request_started_at = time.monotonic()
            drain_queue(global_text)
            token_event.clear()
            global_state = -1
            max_tokens = data.get("max_tokens")
            print(
                f"[性能][Vision] 请求 image={image_path} max_tokens={max_tokens} "
                f"lock_wait={lock_wait_ms}毫秒",
                flush=True,
            )

            def run_target():
                global global_state
                try:
                    rkllm_model.run_vision(question, image_path, max_tokens)
                except Exception as exc:
                    error_holder.append(exc)
                    global_state = LLMCallState.RKLLM_RUN_ERROR
                    token_event.set()

            output = ""
            first_token_ms = None
            model_thread = threading.Thread(target=run_target)
            model_thread.start()
            while model_thread.is_alive():
                token_event.wait(timeout=0.1)
                token_event.clear()
                while not global_text.empty():
                    if first_token_ms is None:
                        first_token_ms = int(
                            (time.monotonic() - request_started_at) * 1000
                        )
                    output += global_text.get_nowait()
                if _is_call_cancelled():
                    # 取消端点已经调用过 abort；这里尽快结束 HTTP 请求，
                    # finally 会再做一次清理并等待线程退出。
                    break
            if model_thread.is_alive():
                # 即使 runtime 没有响应 abort，也必须等 C 调用真正返回后
                # 才能释放共享句柄，禁止下一条请求与旧任务并发进入 RKLLM。
                model_thread.join()
            while not global_text.empty():
                if first_token_ms is None:
                    first_token_ms = int(
                        (time.monotonic() - request_started_at) * 1000
                    )
                output += global_text.get_nowait()

            if _is_call_cancelled():
                print("[性能][Vision] 请求已取消，不再返回旧画面分析结果", flush=True)
                return jsonify({"status": "aborted", "result": ""}), 200
            if error_holder:
                raise error_holder[0]
            total_elapsed_ms = int((time.monotonic() - request_started_at) * 1000)
            first_token_text = "无" if first_token_ms is None else f"{first_token_ms}毫秒"
            print(
                f"[性能][Vision] 完成 首Token={first_token_text} "
                f"总耗时={total_elapsed_ms}毫秒 输出字符={len(output.strip())}",
                flush=True,
            )
            return jsonify({"result": output.strip()})
        except Exception as exc:
            traceback.print_exc()
            return jsonify({"error": str(exc)}), 500
        finally:
            cancelled = _is_call_cancelled()
            if cancelled:
                if model_thread and model_thread.is_alive():
                    model_thread.join()
                rkllm_model.clear_kv_cache(keep_system_prompt=1)
                drain_queue(global_text)
                print("[RKLLM] 视觉推理已取消，已清理对话KV", flush=True)
            elif error_holder or global_state == LLMCallState.RKLLM_RUN_ERROR:
                message = str(error_holder[0]) if error_holder else "RKLLM vision run error"
                _recover_after_run_error(f"视觉推理失败: {message}")
            elif model_thread and model_thread.is_alive():
                _active_call_cancelled.set()
                model_thread.join()
                rkllm_model.clear_kv_cache(keep_system_prompt=1)
            global_state = -1
            _finish_call()
            lock.release()

    # ── 关键词匹配后同步 KV（不生成输出，只推进 sent_count）──────
    @app.route('/rkllm_sync_kv', methods=['POST'])
    def sync_kv():
        """
        关键词匹配成功时，rkllm.py 调用此接口将已处理的对话历史
        写入 KV cache，使 sent_count 保持最新。
        """
        global global_state

        if not _model_ready.is_set():
            return jsonify({'status': 'error', 'message': 'Model not ready'}), 503

        data = request.json
        if not data or 'messages' not in data:
            return jsonify({'status': 'error', 'message': 'No messages'}), 400

        conversation_id = str(data.get("conversation_id") or "").strip()
        if not conversation_id:
            return jsonify({
                'status': 'error',
                'message': 'conversation_id is required to isolate RKLLM KV state',
            }), 400

        messages = data['messages']
        system_prompt_local = next(
            (
                msg.get('content', '')
                for msg in messages
                if msg.get('role') == 'system'
            ),
            '',
        )
        if not str(system_prompt_local).strip():
            return jsonify({
                'status': 'error',
                'message': 'A non-empty system message is required',
            }), 400

        # 非阻塞获取 lock：如果正在推理，直接跳过
        _mark_request_waiting()
        try:
            acquired = lock.acquire(blocking=False)
        finally:
            _unmark_request_waiting()
        if not acquired:
            print(f"[SyncKV] RKLLM 忙碌，跳过同步（下次请求补齐）")
            return jsonify({'status': 'busy', 'synced': 0}), 200
        _begin_call("sync_kv")

        try:
            # 更新 system prompt 状态
            if _static_prompt(system_prompt_local) != _static_prompt(rkllm_model._current_system_prompt):
                if rkllm_model._current_system_prompt is not None:
                    rkllm_model.clear_kv_cache(keep_system_prompt=0)
                    rkllm_model.tools = None
                rkllm_model._current_system_prompt = system_prompt_local
                rkllm_model.set_function_tools(
                    system_prompt=system_prompt_local,
                    tools=rkllm_model.tools or "[]",
                    tool_response_str="tool_response" if rkllm_model.tools and rkllm_model.tools != "[]" else "none"
                )
            elif (
                rkllm_model._active_conversation_id is not None
                and rkllm_model._active_conversation_id != conversation_id
            ):
                print(
                    f"[KV] 同步接口切换会话 "
                    f"{rkllm_model._active_conversation_id[:8]} -> "
                    f"{conversation_id[:8]}，清理旧对话KV",
                    flush=True,
                )
                rkllm_model.clear_kv_cache(keep_system_prompt=1)
                rkllm_model._current_system_prompt = system_prompt_local

            rkllm_model._active_conversation_id = conversation_id

            # 计算需要补充的 delta 消息
            sendable = [
                m for m in messages
                if m.get('role') in ('user', 'tool')
                and '<tool_calling>' not in m.get('content', '')
            ]
            new_msgs = sendable[rkllm_model._sent_count:]

            if not new_msgs:
                return jsonify({'status': 'ok', 'synced': 0}), 200

            drain_queue(global_text)
            global_state = -1

            # prefill_only 模式
            _prefill_event.set()
            synced = 0
            for msg in new_msgs:
                drain_queue(global_text)
                t = threading.Thread(
                    target=rkllm_model.run,
                    args=(msg['role'], False, msg['content'])
                )
                t.start()
                t.join()
                drain_queue(global_text)
                rkllm_model._sent_count += 1
                synced += 1

            _prefill_event.clear()
            global_state = 1
            print(f"[SyncKV] 已同步 {synced} 条消息到 KV（sent_count={rkllm_model._sent_count}）")
            return jsonify({'status': 'ok', 'synced': synced}), 200
        finally:
            _prefill_event.clear()
            if global_state == LLMCallState.RKLLM_RUN_ERROR:
                print("[SyncKV] WARNING: 推理过程中发生 RUN_ERROR，KV 状态可能不一致")
                drain_queue(global_text)
            global_state = 1
            _finish_call()
            lock.release()

    # ── 主推理端点 ──────────────────────────────────────────────
    @app.route('/rkllm_chat', methods=['POST'])
    def receive_message():
        global global_state

        request_received_at = time.monotonic()
        priority_lease = None

        # 模型就绪检查
        if not _model_ready.is_set():
            return jsonify({'status': 'error', 'message': 'Model not ready'}), 503

        priority_lease = _acquire_text_priority("chat")

        # 串行进入共享 RKLLM 句柄；视觉任务取消后可能仍需等待底层同步
        # C 调用返回，不能用短暂的非阻塞获取制造 503。
        _mark_request_waiting()
        try:
            acquired = lock.acquire(timeout=INFERENCE_LOCK_WAIT_SECONDS)
        finally:
            _unmark_request_waiting()
        if not acquired:
            _release_text_priority(priority_lease)
            return jsonify({'status': 'error', 'message': 'RKLLM_Server is busy! Maybe you can try again later.'}), 503
        lock_wait_ms = (time.monotonic() - request_received_at) * 1000
        streaming_started = False
        try:
            data = request.json
            if not data or 'messages' not in data:
                return jsonify({'status': 'error', 'message': 'Invalid JSON data!'}), 400
            if not _begin_call("chat", str(data.get("conversation_id") or "").strip()):
                return jsonify({'status': 'closed', 'message': 'Conversation is closed'}), 409

            # Reset global variables.
            drain_queue(global_text)
            global_state = -1

            # 公共响应结构
            rkllm_responses = {
                "id": "rkllm_chat",
                "object": "rkllm_chat",
                "created": None,
                "choices": [],
                "usage": {
                    "prompt_tokens": None,
                    "completion_tokens": None,
                    "total_tokens": None
                }
            }

            # ── 公共预处理（提取 prompt、KV 管理、增量计算）──────
            try:
                new_msgs, enable_thinking = _prepare_inference(data)
            except _PrepareError as e:
                return e.response, e.status_code

            effective_max_tokens = rkllm_model.set_request_max_tokens(
                data.get("max_tokens")
            )
            inference_started_at = time.monotonic()
            conversation_id = str(data.get("conversation_id"))
            print(
                f"[性能][RKLLM] 请求 会话={conversation_id[:8]} "
                f"最大输出={effective_max_tokens} 新消息={len(new_msgs)} "
                f"lock_wait={lock_wait_ms:.0f}毫秒",
                flush=True,
            )

            last_msg = new_msgs[-1]
            drain_queue(global_text)

            if not data.get("stream", False):
                # ── 非流式分支 ──────────────────────────────────
                rkllm_output = ""
                error_holder = []

                def run_target():
                    global global_state
                    try:
                        rkllm_model.run(last_msg['role'], enable_thinking, last_msg['content'])
                    except Exception as exc:
                        error_holder.append(exc)
                        global_state = LLMCallState.RKLLM_RUN_ERROR
                        token_event.set()

                model_thread = threading.Thread(
                    target=run_target
                )
                token_event.clear()
                model_thread.start()
                model_thread_finished = False
                while not model_thread_finished:
                    token_event.wait(timeout=0.1)
                    token_event.clear()
                    while not global_text.empty():
                        rkllm_output += global_text.get_nowait()
                    if _is_call_cancelled():
                        model_thread.join()
                        rkllm_model.clear_kv_cache(keep_system_prompt=1)
                        drain_queue(global_text)
                        print(
                            f"[RKLLM] 会话={conversation_id[:8]} 非流式推理已取消",
                            flush=True,
                        )
                        return jsonify({'status': 'aborted', 'message': '推理已取消'}), 200
                    model_thread.join(timeout=0)
                    model_thread_finished = not model_thread.is_alive()
                if error_holder or global_state == LLMCallState.RKLLM_RUN_ERROR:
                    message = str(error_holder[0]) if error_holder else "RKLLM run error"
                    _recover_after_run_error(
                        f"会话={conversation_id[:8]} 非流式推理失败: {message}"
                    )
                    return jsonify({'status': 'error', 'message': message}), 503
                rkllm_model._sent_count += 1
                total_ms = (time.monotonic() - request_received_at) * 1000
                print(
                    f"[性能][RKLLM] 完成 会话={conversation_id[:8]} "
                    f"总耗时={total_ms:.0f}毫秒 "
                    f"推理耗时={(time.monotonic() - inference_started_at) * 1000:.0f}毫秒 "
                    f"输出字符={len(rkllm_output)}",
                    flush=True,
                )

                rkllm_responses["choices"].append(
                    {"index": 0,
                    "message": {
                        "role": "assistant",
                        "content": rkllm_output,
                    },
                    "logprobs": None,
                    "finish_reason": "stop"
                    }
                )
                return jsonify(rkllm_responses), 200
            else:
                # ── 流式分支 ────────────────────────────────────
                def generate():
                    global global_state
                    model_thread = None
                    first_token_at = None
                    output_chars = 0
                    error_holder = []
                    try:
                        def run_target():
                            global global_state
                            try:
                                rkllm_model.run(
                                    last_msg['role'],
                                    enable_thinking,
                                    last_msg['content'],
                                )
                            except Exception as exc:
                                error_holder.append(exc)
                                global_state = LLMCallState.RKLLM_RUN_ERROR
                                token_event.set()

                        model_thread = threading.Thread(
                            target=run_target
                        )
                        token_event.clear()
                        model_thread.start()
                        model_thread_finished = False
                        while not model_thread_finished:
                            token_event.wait(timeout=0.1)
                            token_event.clear()
                            while not global_text.empty():
                                if _is_call_cancelled():
                                    break
                                rkllm_output = global_text.get_nowait()
                                output_chars += len(rkllm_output)
                                if first_token_at is None and rkllm_output:
                                    first_token_at = time.monotonic()
                                    print(
                                        f"[性能][RKLLM] 首Token 会话={conversation_id[:8]} "
                                        f"总耗时={(first_token_at - request_received_at) * 1000:.0f}毫秒 "
                                        f"推理耗时={(first_token_at - inference_started_at) * 1000:.0f}毫秒",
                                        flush=True,
                                    )
                                rkllm_responses["choices"] = [
                                    {"index": 0,
                                    "delta": {
                                        "role": "assistant",
                                        "content": rkllm_output,
                                    },
                                    "logprobs": None,
                                    "finish_reason": "stop" if global_state == LLMCallState.RKLLM_RUN_FINISH else None,
                                    }
                                ]
                                yield f"{json.dumps(rkllm_responses)}\n\n"
                            if _is_call_cancelled():
                                break
                            model_thread.join(timeout=0)
                            model_thread_finished = not model_thread.is_alive()
                        if error_holder or global_state == LLMCallState.RKLLM_RUN_ERROR:
                            message = str(error_holder[0]) if error_holder else "RKLLM run error"
                            print(
                                f"[RKLLM] 会话={conversation_id[:8]} 流式推理失败: {message}",
                                flush=True,
                            )
                            rkllm_responses["choices"] = [
                                {"index": 0,
                                "delta": {
                                    "role": "assistant",
                                    "content": "",
                                },
                                "logprobs": None,
                                "finish_reason": "error",
                                }
                            ]
                            yield f"{json.dumps(rkllm_responses)}\n\n"
                            return
                    finally:
                        total_ms = (time.monotonic() - request_received_at) * 1000
                        first_ms = (
                            (first_token_at - request_received_at) * 1000
                            if first_token_at is not None else -1
                        )
                        print(
                            f"[性能][RKLLM] 完成 会话={conversation_id[:8]} "
                            f"首Token={first_ms:.0f}毫秒 总耗时={total_ms:.0f}毫秒 "
                            f"推理耗时={(time.monotonic() - inference_started_at) * 1000:.0f}毫秒 "
                            f"输出字符={output_chars}",
                            flush=True,
                        )
                        # 同步 runtime 不能跨线程原生 abort；取消时丢弃输出并
                        # 等当前 C 调用自然返回，避免污染下一次推理。
                        cancelled = _is_call_cancelled()
                        if cancelled:
                            if model_thread and model_thread.is_alive():
                                model_thread.join()
                            rkllm_model.clear_kv_cache(keep_system_prompt=1)
                            drain_queue(global_text)
                            print(
                                f"[RKLLM] 会话={conversation_id[:8]} 推理已取消，已清理对话KV",
                                flush=True,
                            )
                        elif error_holder or global_state == LLMCallState.RKLLM_RUN_ERROR:
                            message = str(error_holder[0]) if error_holder else "RKLLM run error"
                            _recover_after_run_error(
                                f"会话={conversation_id[:8]} 流式推理失败: {message}"
                            )
                        elif model_thread and model_thread.is_alive():
                            _active_call_cancelled.set()
                            model_thread.join()
                            rkllm_model.clear_kv_cache(keep_system_prompt=1)
                            print(
                                f"[KV] 会话={conversation_id[:8]} 推理被中断，已清理对话KV",
                                flush=True,
                            )
                        else:
                            rkllm_model._sent_count += 1
                        global_state = -1
                        _finish_call()
                        lock.release()
                        _release_text_priority(priority_lease)

                streaming_started = True
                return Response(generate(), content_type='text/plain')

        finally:
            # 非流式分支（包括异常）由这里释放 lock。
            # 流式分支的 lock 已在 generate() finally 内释放。
            if not streaming_started:
                _finish_call()
                lock.release()
                _release_text_priority(priority_lease)

    # Start the Flask application.
    app.run(host='0.0.0.0', port=args.port, threaded=True, debug=False)
