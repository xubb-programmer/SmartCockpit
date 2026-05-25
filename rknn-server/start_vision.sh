#!/bin/bash
# 启动视觉分析服务
# 使用方法: ./start_vision.sh
# 支持环境变量覆盖默认配置:
#   VISION_RKNN_MODEL   - 视觉编码器模型路径
#   VISION_RKLLM_MODEL  - 语言模型路径
#   VISION_NPU_CORE     - NPU 核心 (0/1/2/3/12)
#   VISION_PORT         - 服务端口

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 自动推导模型目录：脚本在 rknn-server/ 下，models 在上一级
MODEL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)/models"

# 可通过环境变量覆盖的默认配置
VISION_RKNN_MODEL="${VISION_RKNN_MODEL:-${MODEL_DIR}/qwen3-vl-2b_vision_rk3588.rknn}"
VISION_RKLLM_MODEL="${VISION_RKLLM_MODEL:-${MODEL_DIR}/qwen3-vl-2b-instruct_w8a8_rk3588.rkllm}"
VISION_NPU_CORE="${VISION_NPU_CORE:-1}"
VISION_PORT="${VISION_PORT:-18090}"

echo "=========================================="
echo "  启动视觉分析服务 (端口 ${VISION_PORT})"
echo "  视觉模型: $(basename ${VISION_RKNN_MODEL})"
echo "  语言模型: $(basename ${VISION_RKLLM_MODEL})"
echo "=========================================="

export LD_LIBRARY_PATH="${SCRIPT_DIR}/lib:${LD_LIBRARY_PATH}"

python3 vision_server.py \
  --rknn_model "${VISION_RKNN_MODEL}" \
  --rkllm_model "${VISION_RKLLM_MODEL}" \
  --npu_core "${VISION_NPU_CORE}" \
  --port "${VISION_PORT}" \
  2>&1 | tee vision_server.log
