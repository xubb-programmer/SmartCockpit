#!/usr/bin/env python3
"""
一次性离线生成 Prompt Cache 文件。
在 RK3588 板子上运行一次即可，后续 flask_server.py 启动时自动加载。

用法:
  python3 generate_prompt_cache.py \
      --rkllm_model_path ../models/qwen3-4b-instruct_w8a8.rkllm \
      --target_platform rk3588 \
      --output ../models/voice_assistant_system_prompt.cache \
      --prompt_file ../models/voice_assistant_static_prompt.txt
"""

import argparse
import ctypes
import os
import sys

# 使用共享类型定义（消除 3 份重复代码）
from rkllm_types import (
    RKLLMParam, RKLLMInput, RKLLMResult, RKLLMInferParam,
    RKLLMPromptCacheParam, RKLLMInputUnion,
    RKLLMInferMode, RKLLMInputType, LLMCallState,
    RKLLM_Handle_t, RKLLM_Callback_t,
)

# ---------- 回调 ----------

def callback_impl(result, userdata, state):
    return 0  # 不输出任何 token

callback = RKLLM_Callback_t(callback_impl)


def main():
    parser = argparse.ArgumentParser(description="Generate RKLLM prompt cache offline")
    parser.add_argument("--rkllm_model_path", type=str, required=True)
    parser.add_argument("--target_platform", type=str, default="rk3588")
    parser.add_argument("--output", type=str, required=True, help="Output .cache file path")
    parser.add_argument("--prompt", type=str, default=None, help="Static system prompt text")
    parser.add_argument("--prompt_file", type=str, default=None, help="File containing static system prompt")
    parser.add_argument("--max_context_len", type=int, default=4096, help="Max context length (must <= model's max_context_limit)")
    args = parser.parse_args()

    # 获取静态提示词
    if args.prompt_file and os.path.exists(args.prompt_file):
        with open(args.prompt_file, "r", encoding="utf-8") as f:
            static_prompt = f.read()
    elif args.prompt:
        static_prompt = args.prompt
    else:
        print("Error: provide --prompt or --prompt_file")
        sys.exit(1)

    print(f"Static prompt ({len(static_prompt)} chars):")
    print(static_prompt[:200] + "...")
    print()

    # 加载 C 库
    lib_path = os.path.join(os.path.dirname(__file__), "lib", "librkllmrt.so")
    rkllm_lib = ctypes.CDLL(lib_path)

    # 初始化模型参数
    rkllm_param = RKLLMParam()
    rkllm_param.model_path = args.rkllm_model_path.encode("utf-8")
    rkllm_param.max_context_len = args.max_context_len
    rkllm_param.max_new_tokens = 1     # prefill 完即停，不需要生成内容
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
    if args.target_platform.lower() in ["rk3576", "rk3588"]:
        rkllm_param.extend_param.enabled_cpus_mask = (1 << 4)|(1 << 5)|(1 << 6)|(1 << 7)
    else:
        rkllm_param.extend_param.enabled_cpus_mask = (1 << 0)|(1 << 1)|(1 << 2)|(1 << 3)


    # 初始化模型
    handle = RKLLM_Handle_t()
    rkllm_init = rkllm_lib.rkllm_init
    rkllm_init.argtypes = [ctypes.POINTER(RKLLM_Handle_t), ctypes.POINTER(RKLLMParam), RKLLM_Callback_t]
    rkllm_init.restype = ctypes.c_int

    print("Loading model...")
    ret = rkllm_init(ctypes.byref(handle), ctypes.byref(rkllm_param), callback)
    if ret != 0:
        print(f"rkllm_init failed: {ret}")
        sys.exit(1)
    print("Model loaded.")

    # 设置推理参数
    infer_params = RKLLMInferParam()
    ctypes.memset(ctypes.byref(infer_params), 0, ctypes.sizeof(RKLLMInferParam))
    infer_params.mode = RKLLMInferMode.RKLLM_INFER_GENERATE
    infer_params.keep_history = 1   # 保留 system prompt 的 KV，让 cache 包含完整 system 上下文

    # 设置 prompt cache save 参数
    cache_param = RKLLMPromptCacheParam()
    cache_param.save_prompt_cache = 1
    cache_path_bytes = args.output.encode("utf-8")
    cache_param.prompt_cache_path = cache_path_bytes
    infer_params.prompt_cache_params = ctypes.pointer(cache_param)

    # 构建输入（纯 system prompt，不含任何 user 消息，生成干净的 cache）
    rkllm_input = RKLLMInput()
    rkllm_input.role = b"system"
    rkllm_input.enable_thinking = ctypes.c_bool(False)
    rkllm_input.input_type = RKLLMInputType.RKLLM_INPUT_PROMPT
    prompt_bytes = static_prompt.encode("utf-8")
    rkllm_input.input_data.prompt_input = ctypes.c_char_p(prompt_bytes)

    rkllm_run = rkllm_lib.rkllm_run
    rkllm_run.argtypes = [
        RKLLM_Handle_t,
        ctypes.POINTER(RKLLMInput),
        ctypes.POINTER(RKLLMInferParam),
        ctypes.c_void_p,
    ]
    rkllm_run.restype = ctypes.c_int

    print("Running prefill to generate prompt cache...")
    ret = rkllm_run(handle, ctypes.byref(rkllm_input), ctypes.byref(infer_params), None)
    if ret != 0:
        print(f"rkllm_run failed: {ret}")
    else:
        print(f"Prompt cache saved to: {args.output}")

    # 清理
    rkllm_destroy = rkllm_lib.rkllm_destroy
    rkllm_destroy.argtypes = [RKLLM_Handle_t]
    rkllm_destroy.restype = ctypes.c_int
    rkllm_destroy(handle)
    print("Done.")


if __name__ == "__main__":
    main()
