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
    RKLLM_Handle_t, RKLLM_Callback_t, drain_queue,
)

app = Flask(__name__)

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
        if _prefill_event.is_set():
            # prefill 阶段已将 user 消息写入 KV，第一个 decode token 到达即可中断
            # 返回非零值触发 RKLLM 内部 abort（比调 rkllm_abort 更快）
            return -1
        if result.contents.text is not None:
            global_text.put(result.contents.text.decode('utf-8'))
            token_event.set()
    return 0

callback = RKLLM_Callback_t(callback_impl)


# ── 辅助函数 ────────────────────────────────────────────────────

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

    # 中间消息（非最后一条）：prefill-only 喂入 KV cache
    _prefill_event.set()
    try:
        for msg in new_msgs[:-1]:
            drain_queue(global_text)
            mid_thread = threading.Thread(
                target=rkllm_model.run,
                args=(msg['role'], enable_thinking, msg['content'])
            )
            mid_thread.start()
            mid_thread.join()
            drain_queue(global_text)
            rkllm_model._sent_count += 1
    finally:
        _prefill_event.clear()

    return new_msgs, enable_thinking


# ── RKLLM 模型封装 ──────────────────────────────────────────────

class RKLLM(object):
    def __init__(self, model_path, lora_model_path=None, prompt_cache_path=None, platform="rk3588", max_context_len=4096):
        rkllm_param = RKLLMParam()
        rkllm_param.model_path = bytes(model_path, 'utf-8')

        rkllm_param.max_context_len = max_context_len
        self.max_context_len = max_context_len
        rkllm_param.max_new_tokens = 4096
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

        rkllm_param.is_async = False

        rkllm_param.img_start = "".encode('utf-8')
        rkllm_param.img_end = "".encode('utf-8')
        rkllm_param.img_content = "".encode('utf-8')

        rkllm_param.extend_param.base_domain_id = 0
        rkllm_param.extend_param.embed_flash = 1
        rkllm_param.extend_param.n_batch = 1
        rkllm_param.extend_param.use_cross_attn = 0
        rkllm_param.extend_param.enabled_cpus_num = 4
        if platform.lower() in ["rk3576", "rk3588"]:
            rkllm_param.extend_param.enabled_cpus_mask = (1 << 4)|(1 << 5)|(1 << 6)|(1 << 7)
        else:
            rkllm_param.extend_param.enabled_cpus_mask = (1 << 0)|(1 << 1)|(1 << 2)|(1 << 3)

        self.handle = RKLLM_Handle_t()

        self.rkllm_init = rkllm_lib.rkllm_init
        self.rkllm_init.argtypes = [ctypes.POINTER(RKLLM_Handle_t), ctypes.POINTER(RKLLMParam), RKLLM_Callback_t]
        self.rkllm_init.restype = ctypes.c_int
        ret = self.rkllm_init(ctypes.byref(self.handle), ctypes.byref(rkllm_param), callback)
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
        self.rkllm_run(self.handle, ctypes.byref(rkllm_input), ctypes.byref(self.rkllm_infer_params), None)
        return

    def abort(self):
        return self.rkllm_abort(self.handle)

    def release(self):
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
    parser.add_argument('--port', type=int, default=int(os.environ.get('RKLLM_PORT', 18080)), help='Server port (also via RKLLM_PORT env var)')
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
    rkllm_model = RKLLM(model_path, args.lora_model_path, args.prompt_cache_path, args.target_platform, args.max_context_len)

    # 注册 atexit 确保资源释放（app.run() 之后的代码不可达）
    atexit.register(rkllm_model.release)

    # ── 启动预热：将 set_function_tools 的 8s 移到此处 ──────────
    rkllm_model.warmup_function_tools(getattr(args, 'system_prompt_file', None))

    # 标记模型就绪
    _model_ready.set()

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
            "model_path": args.rkllm_model_path,
        })

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

        # 非阻塞获取 lock：如果正在推理，直接跳过
        if not lock.acquire(blocking=False):
            print(f"[SyncKV] RKLLM 忙碌，跳过同步（下次请求补齐）")
            return jsonify({'status': 'busy', 'synced': 0}), 200

        try:
            messages = data['messages']

            # 提取 system prompt
            system_prompt_local = ""
            for msg in messages:
                if msg.get('role') == 'system':
                    system_prompt_local = msg['content']
                    break

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
            lock.release()

    # ── 主推理端点 ──────────────────────────────────────────────
    @app.route('/rkllm_chat', methods=['POST'])
    def receive_message():
        global global_state

        # 模型就绪检查
        if not _model_ready.is_set():
            return jsonify({'status': 'error', 'message': 'Model not ready'}), 503

        # 原子化并发控制：用 lock 替代 is_blocking 检查，消除 TOCTOU 竞态
        if not lock.acquire(blocking=False):
            return jsonify({'status': 'error', 'message': 'RKLLM_Server is busy! Maybe you can try again later.'}), 503

        streaming_started = False
        try:
            data = request.json
            if not data or 'messages' not in data:
                return jsonify({'status': 'error', 'message': 'Invalid JSON data!'}), 400

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

            last_msg = new_msgs[-1]
            drain_queue(global_text)

            if not data.get("stream", False):
                # ── 非流式分支 ──────────────────────────────────
                rkllm_output = ""
                model_thread = threading.Thread(
                    target=rkllm_model.run,
                    args=(last_msg['role'], enable_thinking, last_msg['content'])
                )
                token_event.clear()
                model_thread.start()
                model_thread_finished = False
                while not model_thread_finished:
                    token_event.wait(timeout=0.1)
                    token_event.clear()
                    while not global_text.empty():
                        rkllm_output += global_text.get_nowait()
                    model_thread.join(timeout=0)
                    model_thread_finished = not model_thread.is_alive()
                rkllm_model._sent_count += 1

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
                    try:
                        model_thread = threading.Thread(
                            target=rkllm_model.run,
                            args=(last_msg['role'], enable_thinking, last_msg['content'])
                        )
                        token_event.clear()
                        model_thread.start()
                        model_thread_finished = False
                        while not model_thread_finished:
                            token_event.wait(timeout=0.1)
                            token_event.clear()
                            while not global_text.empty():
                                rkllm_output = global_text.get_nowait()
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
                            model_thread.join(timeout=0)
                            model_thread_finished = not model_thread.is_alive()
                    finally:
                        # 客户端提前断开时主动 abort，防止残留推理阻塞下一次请求
                        if model_thread and model_thread.is_alive():
                            rkllm_model.abort()
                            model_thread.join(timeout=2)
                        rkllm_model._sent_count += 1
                        global_state = -1
                        lock.release()

                streaming_started = True
                return Response(generate(), content_type='text/plain')

        finally:
            # 非流式分支（包括异常）由这里释放 lock。
            # 流式分支的 lock 已在 generate() finally 内释放。
            if not streaming_started:
                lock.release()

    # Start the Flask application.
    app.run(host='0.0.0.0', port=args.port, threaded=True, debug=False)
