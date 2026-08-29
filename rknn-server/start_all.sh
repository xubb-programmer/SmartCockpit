#!/bin/bash
# 使用官方单实例结构启动 Qwen3.5 文本与视觉服务。

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${SCRIPT_DIR}"

echo "=========================================="
echo "  启动 RKNN/RKLLM 单实例多模态服务"
echo "  文本与视觉接口共用端口 18080"
echo "=========================================="

export RKLLM_PORT="${RKLLM_PORT:-18080}"
export RKLLM_ENABLE_VISION=true
exec bash start_llm.sh
