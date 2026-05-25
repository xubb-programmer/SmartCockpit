#!/bin/bash
# 启动文本 LLM 推理服务
# 使用方法: ./start_llm.sh
# 支持环境变量覆盖默认配置:
#   RKLLM_MODEL      - 模型文件路径
#   RKLLM_PLATFORM   - 目标平台 (rk3588/rk3576/rk3562/rv1126b)
#   RKLLM_PORT       - 服务端口
#   RKLLM_CACHE      - Prompt Cache 文件路径
#   RKLLM_PROMPT     - 静态 System Prompt 文件路径
#   RKLLM_MAX_CTX    - 最大上下文长度

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 自动推导模型目录：脚本在 rknn-server/ 下，models 在上一级
MODEL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)/models"

# 可通过环境变量覆盖的默认配置
RKLLM_MODEL="${RKLLM_MODEL:-${MODEL_DIR}/qwen3-4b-instruct_w8a8.rkllm}"
RKLLM_PLATFORM="${RKLLM_PLATFORM:-rk3588}"
RKLLM_PORT="${RKLLM_PORT:-18080}"
RKLLM_CACHE="${RKLLM_CACHE:-${MODEL_DIR}/voice_assistant_system_prompt.cache}"
RKLLM_PROMPT="${RKLLM_PROMPT:-${MODEL_DIR}/voice_assistant_static_prompt.txt}"
RKLLM_MAX_CTX="${RKLLM_MAX_CTX:-4096}"

echo "=========================================="
echo "  启动文本 LLM 服务 (端口 ${RKLLM_PORT})"
echo "  模型: $(basename ${RKLLM_MODEL})"
echo "  平台: ${RKLLM_PLATFORM}"
echo "=========================================="

export LD_LIBRARY_PATH="${SCRIPT_DIR}/lib:${LD_LIBRARY_PATH}"

python3 llm_server.py \
  --rkllm_model_path "${RKLLM_MODEL}" \
  --target_platform "${RKLLM_PLATFORM}" \
  --port "${RKLLM_PORT}" \
  --prompt_cache_path "${RKLLM_CACHE}" \
  --system_prompt_file "${RKLLM_PROMPT}" \
  --max_context_len "${RKLLM_MAX_CTX}" \
  2>&1 | tee llm_server.log
