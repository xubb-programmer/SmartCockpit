"""
RKLLM ctypes 共享类型定义
========================
从 llm_server.py / generate_prompt_cache.py 提取的公共类型，
保证三处代码使用统一的结构体定义和枚举值。

所有 ctypes Structure 必须与 librkllmrt.so 的 C 头文件 rkllm.h 保持一致。
"""

import ctypes
import queue


# ── 枚举常量 ─────────────────────────────────────────────────────
# 注意：generate_prompt_cache.py 曾使用 FINISH=1, ERROR=2（错误），
# 现统一为与 rkllm.h 及 llm_server.py 一致的值。

class LLMCallState:
    RKLLM_RUN_NORMAL  = 0
    RKLLM_RUN_WAITING = 1
    RKLLM_RUN_FINISH  = 2
    RKLLM_RUN_ERROR   = 3


class RKLLMInputType:
    RKLLM_INPUT_PROMPT     = 0
    RKLLM_INPUT_TOKEN      = 1
    RKLLM_INPUT_EMBED      = 2
    RKLLM_INPUT_MULTIMODAL = 3


class RKLLMInferMode:
    RKLLM_INFER_GENERATE             = 0
    RKLLM_INFER_GET_LAST_HIDDEN_LAYER = 1
    RKLLM_INFER_GET_LOGITS           = 2


# ── ctypes 结构体 ────────────────────────────────────────────────

class RKLLMExtendParam(ctypes.Structure):
    _fields_ = [
        ("base_domain_id",   ctypes.c_int32),
        ("embed_flash",      ctypes.c_int8),
        ("enabled_cpus_num", ctypes.c_int8),
        ("enabled_cpus_mask", ctypes.c_uint32),
        ("n_batch",          ctypes.c_uint8),
        ("use_cross_attn",   ctypes.c_int8),
        ("reserved",         ctypes.c_uint8 * 104),
    ]


class RKLLMParam(ctypes.Structure):
    _fields_ = [
        ("model_path",         ctypes.c_char_p),
        ("max_context_len",    ctypes.c_int32),
        ("max_new_tokens",     ctypes.c_int32),
        ("top_k",              ctypes.c_int32),
        ("n_keep",             ctypes.c_int32),
        ("top_p",              ctypes.c_float),
        ("temperature",        ctypes.c_float),
        ("repeat_penalty",     ctypes.c_float),
        ("frequency_penalty",  ctypes.c_float),
        ("presence_penalty",   ctypes.c_float),
        ("mirostat",           ctypes.c_int32),
        ("mirostat_tau",       ctypes.c_float),
        ("mirostat_eta",       ctypes.c_float),
        ("skip_special_token", ctypes.c_bool),
        ("ignore_eos_token",   ctypes.c_bool),
        ("is_async",           ctypes.c_bool),
        ("extend_param",       RKLLMExtendParam),
    ]


class RKLLMLoraAdapter(ctypes.Structure):
    _fields_ = [
        ("lora_adapter_path", ctypes.c_char_p),
        ("lora_adapter_name", ctypes.c_char_p),
        ("scale",             ctypes.c_float),
    ]


class RKLLMEmbedInput(ctypes.Structure):
    _fields_ = [
        ("embed",    ctypes.POINTER(ctypes.c_float)),
        ("n_tokens", ctypes.c_size_t),
    ]


class RKLLMTokenInput(ctypes.Structure):
    _fields_ = [
        ("input_ids", ctypes.POINTER(ctypes.c_int32)),
        ("n_tokens",  ctypes.c_size_t),
    ]


class RKLLMImageInput(ctypes.Structure):
    _fields_ = [
        ("image_embed",    ctypes.POINTER(ctypes.c_float)),
        ("n_image_tokens", ctypes.c_size_t),
        ("n_image",        ctypes.c_size_t),
        ("image_start",    ctypes.c_char_p),
        ("image_end",      ctypes.c_char_p),
        ("image_content",  ctypes.c_char_p),
        ("image_width",    ctypes.c_size_t),
        ("image_height",   ctypes.c_size_t),
    ]


class RKLLMVideoInput(ctypes.Structure):
    _fields_ = [
        ("video_embed",        ctypes.POINTER(ctypes.c_float)),
        ("n_frame_tokens",     ctypes.c_size_t),
        ("n_frame_per_video",  ctypes.c_size_t),
        ("n_video",            ctypes.c_size_t),
        ("video_start",        ctypes.c_char_p),
        ("video_end",          ctypes.c_char_p),
        ("video_content",      ctypes.c_char_p),
        ("frame_width",        ctypes.c_size_t),
        ("frame_height",       ctypes.c_size_t),
    ]


class RKLLMMultiModalInput(ctypes.Structure):
    _fields_ = [
        ("prompt", ctypes.c_char_p),
        ("image",  RKLLMImageInput),
        ("video",  RKLLMVideoInput),
    ]


class RKLLMInputUnion(ctypes.Union):
    """完整的输入 Union —— 必须包含所有字段以保证 sizeof 与 C 库一致。
    generate_prompt_cache.py 曾只定义 prompt_input 导致 Union 过小。"""
    _fields_ = [
        ("prompt_input",     ctypes.c_char_p),
        ("embed_input",      RKLLMEmbedInput),
        ("token_input",      RKLLMTokenInput),
        ("multimodal_input", RKLLMMultiModalInput),
    ]


class RKLLMInput(ctypes.Structure):
    _fields_ = [
        ("role",            ctypes.c_char_p),
        ("enable_thinking", ctypes.c_bool),
        ("input_type",      ctypes.c_int),
        ("input_data",      RKLLMInputUnion),
    ]


class RKLLMLoraParam(ctypes.Structure):
    _fields_ = [
        ("lora_adapter_name", ctypes.c_char_p),
    ]


class RKLLMPromptCacheParam(ctypes.Structure):
    _fields_ = [
        ("save_prompt_cache",  ctypes.c_int),
        ("prompt_cache_path",  ctypes.c_char_p),
    ]


class RKLLMSamplingParam(ctypes.Structure):
    _fields_ = [
        ("top_k",             ctypes.c_int32),
        ("top_p",             ctypes.c_float),
        ("temperature",       ctypes.c_float),
        ("repeat_penalty",    ctypes.c_float),
        ("frequency_penalty", ctypes.c_float),
        ("presence_penalty",  ctypes.c_float),
        ("mirostat",          ctypes.c_int32),
        ("mirostat_tau",      ctypes.c_float),
        ("mirostat_eta",      ctypes.c_float),
    ]


class RKLLMInferParam(ctypes.Structure):
    _fields_ = [
        ("mode",                ctypes.c_int),
        ("lora_params",         ctypes.POINTER(RKLLMLoraParam)),
        ("prompt_cache_params", ctypes.POINTER(RKLLMPromptCacheParam)),
        ("sampling_params",     ctypes.POINTER(RKLLMSamplingParam)),
        ("keep_history",        ctypes.c_int),
        ("max_new_tokens",      ctypes.c_int32),
    ]


class RKLLMResultLastHiddenLayer(ctypes.Structure):
    _fields_ = [
        ("hidden_states", ctypes.POINTER(ctypes.c_float)),
        ("embd_size",     ctypes.c_int),
        ("num_tokens",    ctypes.c_int),
    ]


class RKLLMResultLogits(ctypes.Structure):
    _fields_ = [
        ("logits",     ctypes.POINTER(ctypes.c_float)),
        ("vocab_size", ctypes.c_int),
        ("num_tokens", ctypes.c_int),
    ]


class RKLLMPerfStat(ctypes.Structure):
    _fields_ = [
        ("prefill_time_ms",  ctypes.c_float),
        ("prefill_tokens",   ctypes.c_int),
        ("generate_time_ms", ctypes.c_float),
        ("generate_tokens",  ctypes.c_int),
        ("memory_usage_mb",  ctypes.c_float),
    ]


class RKLLMResult(ctypes.Structure):
    _fields_ = [
        ("text",              ctypes.c_char_p),
        ("token_id",          ctypes.c_int),
        ("last_hidden_layer", RKLLMResultLastHiddenLayer),
        ("logits",            RKLLMResultLogits),
        ("perf",              RKLLMPerfStat),
    ]


# ── 公共句柄类型 ─────────────────────────────────────────────────

RKLLM_Handle_t = ctypes.c_void_p


# ── 回调函数类型 ─────────────────────────────────────────────────

LLMResultCallback = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.POINTER(RKLLMResult),
    ctypes.c_void_p,
    ctypes.c_int,
)

LLMTokenizerCallback = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.c_char_p,
    ctypes.c_int32,
    ctypes.POINTER(ctypes.c_int32),
    ctypes.c_int32,
)

LLMGetEmbedCallback = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_int32),
    ctypes.c_uint64,
    ctypes.c_void_p,
    ctypes.c_uint64,
)


class RKLLMCallback(ctypes.Structure):
    _fields_ = [
        ("result_callback",    LLMResultCallback),
        ("result_userdata",    ctypes.c_void_p),
        ("tokenizer_callback", LLMTokenizerCallback),
        ("tokenizer_userdata", ctypes.c_void_p),
        ("embed_callback",     LLMGetEmbedCallback),
        ("embed_userdata",     ctypes.c_void_p),
    ]


# Backward-compatible alias used by existing server files when defining the
# Python callback. rkllm_init now receives RKLLMCallback, not this function
# pointer directly.
RKLLM_Callback_t = LLMResultCallback


def make_rkllm_callback(result_callback, result_userdata=None):
    callback = RKLLMCallback()
    callback.result_callback = result_callback
    callback.result_userdata = result_userdata
    callback.tokenizer_callback = LLMTokenizerCallback()
    callback.tokenizer_userdata = None
    callback.embed_callback = LLMGetEmbedCallback()
    callback.embed_userdata = None
    return callback


# ── 工具函数 ─────────────────────────────────────────────────────

def drain_queue(q):
    """清空 SimpleQueue 中的所有待读取元素"""
    while not q.empty():
        try:
            q.get_nowait()
        except Exception:
            break
