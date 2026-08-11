#!/bin/bash
# 一键启动所有 RKNN 推理服务
# 使用方法: ./start_all.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=========================================="
echo "  一键启动所有 RKNN 推理服务"
echo "=========================================="

# 使用进程组管理，确保 kill 时子进程也被终止
set -m

start_service_group() {
    local script=$1
    if command -v setsid >/dev/null 2>&1; then
        setsid bash "${script}" &
    else
        bash "${script}" &
    fi
    SERVICE_PID=$!
}

# 启动文本 LLM 服务（后台运行）
echo "[1/2] 启动文本 LLM 服务..."
start_service_group start_llm.sh
LLM_PID=${SERVICE_PID}
echo "  LLM 服务 PID: ${LLM_PID}"

# 轮询 LLM 健康检查（替代硬编码 15s 等待）
LLM_PORT="${RKLLM_PORT:-18080}"
echo "  等待 LLM 服务就绪 (轮询 /health)..."
for i in $(seq 1 60); do
    if curl -s "http://127.0.0.1:${LLM_PORT}/health" > /dev/null 2>&1; then
        echo "  LLM 服务已就绪 (${i}s)"
        break
    fi
    if [ $i -eq 60 ]; then
        echo "  警告: LLM 服务 60s 内未就绪，继续启动视觉服务"
    fi
    sleep 1
done

# 启动视觉分析服务（后台运行）
echo "[2/2] 启动视觉分析服务..."
start_service_group start_vision.sh
VISION_PID=${SERVICE_PID}
echo "  视觉服务 PID: ${VISION_PID}"

VISION_PORT="${VISION_PORT:-18090}"

echo ""
echo "=========================================="
echo "  所有服务已启动"
echo "  文本 LLM:  http://0.0.0.0:${LLM_PORT}/rkllm_chat"
echo "  视觉分析:  http://0.0.0.0:${VISION_PORT}/vision/analyze"
echo "  健康检查:  http://0.0.0.0:${LLM_PORT}/health"
echo "             http://0.0.0.0:${VISION_PORT}/health"
echo "=========================================="
echo ""
echo "按 Ctrl+C 停止所有服务"

# 捕获退出信号，使用进程组 kill 确保孙进程也被终止
cleanup() {
    echo '正在停止服务...'
    kill -- -${LLM_PID} 2>/dev/null
    kill -- -${VISION_PID} 2>/dev/null
    wait 2>/dev/null
    echo '所有服务已停止'
    exit 0
}
trap cleanup SIGINT SIGTERM

# 等待子进程
wait
