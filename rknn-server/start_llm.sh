#!/bin/bash
# 启动一个 RKLLM 推理实例
# 使用方法: ./start_llm.sh
# 支持环境变量覆盖默认配置:
#   RKLLM_MODEL      - 模型文件路径
#   RKLLM_PLATFORM   - 目标平台 (rk3588/rk3576/rk3562/rv1126b)
#   RKLLM_PORT       - 服务端口
#   RKLLM_CACHE      - Prompt Cache 文件路径（Qwen3.5 默认不启用）
#   RKLLM_PROMPT     - 静态 System Prompt 文件路径
#   RKLLM_WARMUP     - 是否加载静态提示词预热 (true/false)
#   RKLLM_MAX_CTX    - 最大上下文长度
#   RKLLM_TEXT_MAX_NEW_TOKENS - 文本回复最大生成 token 数
#   RKLLM_ENABLE_VISION - 是否启用视觉分析 (true/false)
#   VISION_RKNN_MODEL - 视觉编码器模型路径
#   VISION_MAX_NEW_TOKENS - 视觉回答最大生成 token 数
#   VISION_PREENCODE_INTERVAL - 空闲预编码间隔，0 表示关闭

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 自动推导模型目录：脚本在 rknn-server/ 下，models 在上一级
MODEL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)/models"

# 可通过环境变量覆盖的默认配置
RKLLM_MODEL="${RKLLM_MODEL:-${MODEL_DIR}/Qwen3.5-0.8B_w8a8_rk3588.rkllm}"
RKLLM_PLATFORM="${RKLLM_PLATFORM:-rk3588}"
RKLLM_PORT="${RKLLM_PORT:-18080}"
RKLLM_CACHE="${RKLLM_CACHE:-}"
RKLLM_WARMUP="${RKLLM_WARMUP:-true}"
RKLLM_MAX_CTX="${RKLLM_MAX_CTX:-4096}"
RKLLM_TEXT_MAX_NEW_TOKENS="${RKLLM_TEXT_MAX_NEW_TOKENS:-256}"
# 默认仅启动文本能力；启用视觉时由同一个 Qwen3.5 实例加载配套编码器。
RKLLM_ENABLE_VISION="${RKLLM_ENABLE_VISION:-false}"
PROMPT_MODE="novision"
[[ "${RKLLM_ENABLE_VISION}" == "true" ]] && PROMPT_MODE="vision"
DEFAULT_PROMPT="${MODEL_DIR}/voice_assistant_static_prompt_${PROMPT_MODE}.txt"
if [[ ! -f "${DEFAULT_PROMPT}" && -f "${MODEL_DIR}/voice_assistant_static_prompt.txt" ]]; then
  DEFAULT_PROMPT="${MODEL_DIR}/voice_assistant_static_prompt.txt"
fi
RKLLM_PROMPT="${RKLLM_PROMPT:-${DEFAULT_PROMPT}}"
VISION_RKNN_MODEL="${VISION_RKNN_MODEL:-}"
VISION_NPU_CORE="${VISION_NPU_CORE:-3}"
# 视觉播报限定为短句，减少 0.8B 多模态生成尾延时；可用环境变量覆盖。
VISION_MAX_NEW_TOKENS="${VISION_MAX_NEW_TOKENS:-48}"
VISION_IMAGE_WIDTH="${VISION_IMAGE_WIDTH:-448}"
VISION_IMAGE_HEIGHT="${VISION_IMAGE_HEIGHT:-448}"
VISION_PREENCODE_PATH="${VISION_PREENCODE_PATH:-/tmp/voice_assistant_latest_vision.jpg}"
VISION_PREENCODE_INTERVAL="${VISION_PREENCODE_INTERVAL:-1.0}"
VISION_IMG_START="${VISION_IMG_START:-<|vision_start|>}"
VISION_IMG_END="${VISION_IMG_END:-<|vision_end|>}"
VISION_IMG_CONTENT="${VISION_IMG_CONTENT:-<|image_pad|>}"
RKLLM_MODEL_BASENAME="$(basename "${RKLLM_MODEL}")"
VISION_ARGS=()
if [[ "${RKLLM_ENABLE_VISION}" == "true" ]]; then
  case "${RKLLM_MODEL_BASENAME}" in
    Qwen3.5-0.8B_w8a8_rk3588.rkllm)
      VISION_RKNN_MODEL="${VISION_RKNN_MODEL:-${MODEL_DIR}/Qwen3.5-0.8B_vision_rk3588.rknn}"
      ;;
    qwen3.5-2b_w8a8_rk3588.rkllm)
      VISION_RKNN_MODEL="${VISION_RKNN_MODEL:-${MODEL_DIR}/qwen3.5-2b_vision_rk3588.rknn}"
      ;;
    *)
      echo "[ERROR] ${RKLLM_MODEL_BASENAME} 是纯文本或未知模型，不支持 RKLLM_ENABLE_VISION=true" >&2
      exit 1
      ;;
  esac
  if [[ ! -f "${VISION_RKNN_MODEL}" ]]; then
    echo "[ERROR] 视觉编码器模型不存在: ${VISION_RKNN_MODEL}" >&2
    exit 1
  fi
  VISION_ARGS=(
    --vision_rknn_model_path "${VISION_RKNN_MODEL}"
    --vision_npu_core "${VISION_NPU_CORE}"
    --vision_max_new_tokens "${VISION_MAX_NEW_TOKENS}"
    --vision_image_width "${VISION_IMAGE_WIDTH}"
    --vision_image_height "${VISION_IMAGE_HEIGHT}"
    --vision_preencode_path "${VISION_PREENCODE_PATH}"
    --vision_preencode_interval "${VISION_PREENCODE_INTERVAL}"
    --vision_img_start "${VISION_IMG_START}"
    --vision_img_end "${VISION_IMG_END}"
    --vision_img_content "${VISION_IMG_CONTENT}"
  )
fi
PROMPT_CACHE_ARGS=()
if [ -n "${RKLLM_CACHE}" ]; then
  PROMPT_CACHE_ARGS=(--prompt_cache_path "${RKLLM_CACHE}")
elif [[ "${RKLLM_MODEL_BASENAME}" != Qwen3.5-* && "${RKLLM_MODEL_BASENAME}" != qwen3.5-* ]]; then
  RKLLM_CACHE="${MODEL_DIR}/voice_assistant_system_prompt.cache"
  PROMPT_CACHE_ARGS=(--prompt_cache_path "${RKLLM_CACHE}")
fi
WARMUP_ARGS=()
if [[ "${RKLLM_WARMUP}" == "true" && -f "${RKLLM_PROMPT}" ]]; then
  WARMUP_ARGS=(--system_prompt_file "${RKLLM_PROMPT}")
fi

echo "=========================================="
echo "  启动 RKLLM 单实例服务 (端口 ${RKLLM_PORT})"
echo "  模型: $(basename ${RKLLM_MODEL})"
echo "  平台: ${RKLLM_PLATFORM}"
if [ "${#VISION_ARGS[@]}" -gt 0 ]; then
  echo "  视觉: $(basename ${VISION_RKNN_MODEL})"
  echo "  视觉输出: max_tokens=${VISION_MAX_NEW_TOKENS}"
  echo "  视觉预编码: ${VISION_PREENCODE_INTERVAL}s"
else
  echo "  视觉: 未启用"
fi
if [ "${#PROMPT_CACHE_ARGS[@]}" -gt 0 ]; then
  echo "  Prompt Cache: ${RKLLM_CACHE}"
else
  echo "  Prompt Cache: 未启用（Qwen3.5 RKLLM 不支持保存 prompt cache）"
fi
echo "=========================================="

export LD_LIBRARY_PATH="${SCRIPT_DIR}/lib:${LD_LIBRARY_PATH}"

python3 llm_server.py \
  --rkllm_model_path "${RKLLM_MODEL}" \
  --target_platform "${RKLLM_PLATFORM}" \
  --port "${RKLLM_PORT}" \
  "${PROMPT_CACHE_ARGS[@]}" \
  "${WARMUP_ARGS[@]}" \
  --max_context_len "${RKLLM_MAX_CTX}" \
  --text_max_new_tokens "${RKLLM_TEXT_MAX_NEW_TOKENS}" \
  "${VISION_ARGS[@]}" \
  2>&1 | tee llm_server.log
