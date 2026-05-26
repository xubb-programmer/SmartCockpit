#!/bin/bash
# ============================================================
# start.sh 启动 RKLLM Server + voice-assistant-server + Web 客户HTTP 服务
# 运行环境：RK3588 (${LOCAL_IP})
#
# 用法
#   ./start.sh            # 默认使用 qwen3-4b-instruct 模型
#   ./start.sh 3b         # 使用 qwen2.5-3b 模型
#   ./start.sh 7b         # 使用 qwen2.5-7b 模型
#   ./start.sh qwen3-1.7b # 使用 qwen3-1.7b 模型
#   ./start.sh 1.5b-z1    # 使用 Qwen2.5-1.5B-Z1-FC 模型
# ============================================================

set -e

# ================================================================
# 配置区（如需修改路径/端口请在此处调整
# ================================================================
# 自动获取本机第一块网卡的 IP 地址，默fallback 127.0.0.1
LOCAL_IP=$(hostname -I | awk '{print $1}')
LOCAL_IP=${LOCAL_IP:-127.0.0.1}

# 自动推导部署根目录：脚本在 voice-assistant-server/scripts/deploy/ 下
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
MODEL_DIR="${HOME_DIR}/models"
RKLLM_SERVER_DIR="${HOME_DIR}/rknn-server"
VOICE_ASSISTANT_DIR="${HOME_DIR}/voice-assistant-server"
PLATFORM="rk3588"
CONDA_ENV="voice-assistant-server"
RKLLM_PORT=18080
VISION_PORT=18090
VOICE_ASSISTANT_WS_PORT=18000
WEB_HTTP_PORT=18006           # Web 客户HTTP 服务端口（RK3588 自身通过 localhost 访问
WEB_HTTPS_PORT=18007          # Web 客户HTTPS 服务端口（远程客户端访问

# 视觉模型文件
VISION_RKNN_MODEL="${MODEL_DIR}/qwen3-vl-2b_vision_rk3588.rknn"
VISION_RKLLM_MODEL="${MODEL_DIR}/qwen3-vl-2b-instruct_w8a8_rk3588.rkllm"

RKLLM_PID_FILE="/tmp/rkllm_server.pid"
VISION_PID_FILE="/tmp/vision_server.pid"
VOICE_ASSISTANT_PID_FILE="/tmp/voice_assistant_server.pid"
WEB_HTTP_PID_FILE="/tmp/web_http_server.pid"
WEB_HTTPS_PID_FILE="/tmp/web_https_server.pid"
RKLLM_LOG="/tmp/rkllm_server.log"
VISION_LOG="/tmp/vision_server.log"
VOICE_ASSISTANT_LOG="/tmp/voice_assistant_server.log"
WEB_HTTP_LOG="/tmp/web_http_server.log"
WEB_HTTPS_LOG="/tmp/web_https_server.log"

# ----------------------------------------------------------------
MODEL_SIZE="qwen3-4b"
ENABLE_VISION=true

for arg in "$@"; do
    case "$arg" in
        --no-vision) ENABLE_VISION=false ;;
        3b)          MODEL_SIZE="3b" ;;
        7b)          MODEL_SIZE="7b" ;;
        qwen3-1.7b)  MODEL_SIZE="qwen3-1.7b" ;;
        1.5b-z1)     MODEL_SIZE="1.5b-z1" ;;
        qwen3-4b)    MODEL_SIZE="qwen3-4b" ;;
    esac
done

case "${MODEL_SIZE}" in
  3b)           MODEL_PATH="${MODEL_DIR}/qwen2.5-3b_w8a8.rkllm";        MODEL_LABEL="qwen2.5-3b";        MAX_CTX=4096 ;;
  7b)           MODEL_PATH="${MODEL_DIR}/qwen2.5-7b_w8a8.rkllm";        MODEL_LABEL="qwen2.5-7b";        MAX_CTX=4096 ;;
  qwen3-1.7b)   MODEL_PATH="${MODEL_DIR}/qwen3-1.7b_w8a8.rkllm";       MODEL_LABEL="qwen3-1.7b";       MAX_CTX=4096 ;;
  1.5b-z1)      MODEL_PATH="${MODEL_DIR}/Qwen2.5-1.5B-Z1-FC_W8A8_RK3588.rkllm"; MODEL_LABEL="Qwen2.5-1.5B-Z1-FC"; MAX_CTX=4096 ;;
  *)            MODEL_PATH="${MODEL_DIR}/qwen3-4b-instruct_w8a8.rkllm"; MODEL_LABEL="qwen3-4b-instruct"; MAX_CTX=4096 ;;
esac
# ================================================================

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO ]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN ]${NC} $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
section() { echo -e "\n${CYAN}══ $* ══${NC}"; }

wait_for_port() {
    local port=$1; local timeout=$2
    for i in $(seq 1 "${timeout}"); do
        sleep 1
        if ss -tlnp | grep -q ":${port} "; then return 0; fi
        printf "."
    done
    echo; return 1
}

is_running() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

# ================================================================
# 1. 初始conda
# ================================================================
section "环境准备"
info "本机局域网 IP: ${LOCAL_IP}"

section "初始conda 环境: ${CONDA_ENV}"

CONDA_BASE=$(conda info --base 2>/dev/null || echo "")
if [ -z "${CONDA_BASE}" ]; then
    for p in /opt/conda ~/miniconda3 ~/anaconda3 ~/miniforge3; do
        [ -f "${p}/etc/profile.d/conda.sh" ] && CONDA_BASE="${p}" && break
    done
fi
[ -z "${CONDA_BASE}" ] && { error "未找conda 安装"; exit 1; }

source "${CONDA_BASE}/etc/profile.d/conda.sh"
conda activate "${CONDA_ENV}"
info "conda 环境已激 ${CONDA_ENV}"

# Relocated conda OpenSSL may point at a build-time openssl.cnf path.
# Use the system config so HTTPS certificate generation is stable.
if [ -f /etc/ssl/openssl.cnf ]; then
    export OPENSSL_CONF=/etc/ssl/openssl.cnf
fi

# ================================================================
# 2. 启动 RKLLM Flask Server
# ================================================================
section "启动 RKLLM Server (模型: ${MODEL_LABEL})"

if is_running "${RKLLM_PID_FILE}"; then
    warn "RKLLM Server 已在运行 (PID=$(cat ${RKLLM_PID_FILE}))"
else
    # 检查模型文
    [ ! -f "${MODEL_PATH}" ] && { error "模型文件不存 ${MODEL_PATH}"; exit 1; }

    # 检查端
    if ss -tlnp | grep -q ":${RKLLM_PORT} "; then
        error "端口 ${RKLLM_PORT} 已被占用"; ss -tlnp | grep ":${RKLLM_PORT} "; exit 1
    fi

    # 固定 CPU 频率（提NPU 推理稳定性）
    FIX_FREQ="${RKLLM_SERVER_DIR}/scripts/fix_freq_${PLATFORM}.sh"
    if [ -f "${FIX_FREQ}" ]; then
        if sudo -n bash "${FIX_FREQ}" 2>/dev/null; then
            info "✓ CPU 频率已固定"
        else
            warn "跳过固定 CPU 频率（sudo 需要密码或脚本失败），推理性能可能受影响"
        fi
    fi

    # 必须 cd rknn-server/ 目录：llm_server.py 使用相对路径 lib/librkllmrt.so
    cd "${RKLLM_SERVER_DIR}"
    PROMPT_CACHE_PATH="${MODEL_DIR}/voice_assistant_system_prompt.cache"

    # 静prompt 文件（由 generate_prompt_cache.sh 生成，用于启动预set_function_tools
    SYSTEM_PROMPT_FILE="${MODEL_DIR}/voice_assistant_static_prompt.txt"
    WARMUP_ARGS=""
    if [ -f "${SYSTEM_PROMPT_FILE}" ]; then
        WARMUP_ARGS="--system_prompt_file ${SYSTEM_PROMPT_FILE}"
        info "找到静prompt 文件，启动时将预set_function_tools"
    else
        warn "未找${SYSTEM_PROMPT_FILE}，首次请求将~8s 延迟"
        warn "请先运行: ./generate_prompt_cache.sh --force"
    fi

    export LD_LIBRARY_PATH="${RKLLM_SERVER_DIR}/lib:${LD_LIBRARY_PATH}"
    nohup python3 llm_server.py \
        --rkllm_model_path "${MODEL_PATH}" \
        --target_platform  "${PLATFORM}" \
        --port "${RKLLM_PORT}" \
        --prompt_cache_path "${PROMPT_CACHE_PATH}" \
        --max_context_len  "${MAX_CTX}" \
        ${WARMUP_ARGS} \
        > "${RKLLM_LOG}" 2>&1 &
    echo $! > "${RKLLM_PID_FILE}"
    info "RKLLM Server 已启动 (PID=$(cat ${RKLLM_PID_FILE}))"

    # 等待端口就绪（模型加载最90 秒）
    printf "加载中..."
    if wait_for_port "${RKLLM_PORT}" 90; then
        info "RKLLM Server 就绪: http://${LOCAL_IP}:${RKLLM_PORT}/rkllm_chat"
    else
        if is_running "${RKLLM_PID_FILE}"; then
            warn "端口未开但进程仍在运行，模型可能仍在加载，继.."
        else
            error "RKLLM Server 启动失败，查看日志："; tail -20 "${RKLLM_LOG}"
            rm -f "${RKLLM_PID_FILE}"; exit 1
        fi
    fi
fi

# ================================================================
# 3. 启动视觉分析服务（可选，模型存在时自动启动）
# ================================================================
if [[ "${ENABLE_VISION}" == "true" && -f "${VISION_RKNN_MODEL}" && -f "${VISION_RKLLM_MODEL}" ]]; then
    section "启动视觉分析服务"

    if is_running "${VISION_PID_FILE}"; then
        warn "视觉服务已在运行 (PID=$(cat ${VISION_PID_FILE}))"
    else
        if ss -tlnp | grep -q ":${VISION_PORT} "; then
            error "端口 ${VISION_PORT} 已被占用"; ss -tlnp | grep ":${VISION_PORT} "; exit 1
        fi

        cd "${RKLLM_SERVER_DIR}"
        export LD_LIBRARY_PATH="${RKLLM_SERVER_DIR}/lib:${LD_LIBRARY_PATH}"
        nohup python3 vision_server.py \
            --rknn_model "${VISION_RKNN_MODEL}" \
            --rkllm_model "${VISION_RKLLM_MODEL}" \
            --npu_core 1 \
            --port "${VISION_PORT}" \
            > "${VISION_LOG}" 2>&1 &
        echo $! > "${VISION_PID_FILE}"
        info "视觉服务已启动 (PID=$(cat ${VISION_PID_FILE}))"

    printf "加载中..."
        if wait_for_port "${VISION_PORT}" 120; then
            info "视觉服务就绪: http://0.0.0.0:${VISION_PORT}/health"
        else
            if is_running "${VISION_PID_FILE}"; then
                warn "端口未开但进程仍在运行，模型可能仍在加载，继.."
            else
                error "视觉服务启动失败，查看日志："; tail -20 "${VISION_LOG}"
                warn "视觉服务启动失败，其他服务继续启.."
                rm -f "${VISION_PID_FILE}"
            fi
        fi
    fi
else
    # 清空可能残留的旧日志，防止日志追踪时产生误导
    > "${VISION_LOG}" 2>/dev/null || true
    if [[ "${ENABLE_VISION}" == "false" ]]; then
        warn "已使用 --no-vision 参数，跳过视觉服务启动"
    else
        warn "视觉模型文件不存在，跳过视觉服务启动"
        warn "  需 ${VISION_RKNN_MODEL}"
        warn "  需 ${VISION_RKLLM_MODEL}"
    fi
fi

# ================================================================
# 4. 启动 voice-assistant-server
# ================================================================
section "启动 voice-assistant-server"

if is_running "${VOICE_ASSISTANT_PID_FILE}"; then
    warn "voice-assistant-server 已在运行 (PID=$(cat ${VOICE_ASSISTANT_PID_FILE}))"
else
    [ ! -f "${VOICE_ASSISTANT_DIR}/app.py" ] && { error "未找app.py: ${VOICE_ASSISTANT_DIR}/app.py"; exit 1; }

    cd "${VOICE_ASSISTANT_DIR}"
    nohup python3 app.py > "${VOICE_ASSISTANT_LOG}" 2>&1 &
    echo $! > "${VOICE_ASSISTANT_PID_FILE}"
    info "voice-assistant-server 已启动 (PID=$(cat ${VOICE_ASSISTANT_PID_FILE}))"

    printf "启动中..."
    if wait_for_port "${VOICE_ASSISTANT_WS_PORT}" 30; then
        info "voice-assistant-server 就绪"
    else
        if is_running "${VOICE_ASSISTANT_PID_FILE}"; then
            warn "WebSocket 端口未开，服务可能仍在初始化"
        else
            error "voice-assistant-server 启动失败，查看日志："; tail -20 "${VOICE_ASSISTANT_LOG}"
            rm -f "${VOICE_ASSISTANT_PID_FILE}"; exit 1
        fi
    fi
fi

section "启动 Web 客户端服务（HTTP + HTTPS）"

TEST_DIR="${VOICE_ASSISTANT_DIR}/web"

# ── 4a. HTTP 服务（RK3588 自身通过 127.0.0.1 访问，localhost 是安全上下文，支持麦克风
if is_running "${WEB_HTTP_PID_FILE}"; then
    warn "Web HTTP服务已在运行 (PID=$(cat ${WEB_HTTP_PID_FILE}))"
else
    HTTP_SCRIPT="${TEST_DIR}/http_server.py"
    if [ ! -f "${HTTP_SCRIPT}" ]; then
        warn "未找到 HTTP 服务脚本: ${HTTP_SCRIPT}，跳过启动"
    else
        nohup python3 "${HTTP_SCRIPT}" \
            --port "${WEB_HTTP_PORT}" \
            --web-dir "${TEST_DIR}" \
            > "${WEB_HTTP_LOG}" 2>&1 &
        echo $! > "${WEB_HTTP_PID_FILE}"

        printf "  HTTP 启动中..."
        if wait_for_port "${WEB_HTTP_PORT}" 10; then
            info "Web HTTP 服务就绪 (端口 ${WEB_HTTP_PORT})"
        else
            warn "HTTP 服务可能仍在启动，请查看日志: ${WEB_HTTP_LOG}"
        fi
    fi
fi

# ── 4b. HTTPS 服务（远程客户端访问，HTTPS 是安全上下文，支持麦克风
if is_running "${WEB_HTTPS_PID_FILE}"; then
    warn "Web HTTPS服务已在运行 (PID=$(cat ${WEB_HTTPS_PID_FILE}))"
else
    HTTPS_SCRIPT="${TEST_DIR}/https_server.py"
    if [ ! -f "${HTTPS_SCRIPT}" ]; then
        warn "未找到 HTTP 服务脚本: ${HTTP_SCRIPT}，跳过启动"
    else
        nohup python3 "${HTTPS_SCRIPT}" \
            --port "${WEB_HTTPS_PORT}" \
            --host "${LOCAL_IP}" \
            --cert-dir "${HOME_DIR}/.ssl" \
            --web-dir "${TEST_DIR}" \
            > "${WEB_HTTPS_LOG}" 2>&1 &
        echo $! > "${WEB_HTTPS_PID_FILE}"

        printf "  HTTPS 启动中..."
        if wait_for_port "${WEB_HTTPS_PORT}" 10; then
            info "Web HTTPS 服务就绪 (端口 ${WEB_HTTPS_PORT})"
        else
            warn "HTTPS 服务可能仍在启动，请查看日志: ${WEB_HTTPS_LOG}"
        fi
    fi
fi

# ================================================================
# 5. 启动摘要
# ================================================================
section "服务已就绪"
echo -e "  ${GREEN}RKLLM Server${NC}    PID=$(cat ${RKLLM_PID_FILE} 2>/dev/null)  日志: ${RKLLM_LOG}"
if [ -f "${VISION_PID_FILE}" ]; then
echo -e "  ${GREEN}Vision Server${NC}   PID=$(cat ${VISION_PID_FILE} 2>/dev/null)  日志: ${VISION_LOG}"
fi
echo -e "  ${GREEN}voice-assistant-server${NC}  PID=$(cat ${VOICE_ASSISTANT_PID_FILE} 2>/dev/null)  日志: ${VOICE_ASSISTANT_LOG}"
echo -e "  ${GREEN}Web HTTP${NC}       PID=$(cat ${WEB_HTTP_PID_FILE} 2>/dev/null)  日志: ${WEB_HTTP_LOG}"
echo -e "  ${GREEN}Web HTTPS${NC}      PID=$(cat ${WEB_HTTPS_PID_FILE} 2>/dev/null)  日志: ${WEB_HTTPS_LOG}"
echo
echo -e "  WebSocket : ${CYAN}ws://${LOCAL_IP}:${VOICE_ASSISTANT_WS_PORT}/voice_assistant/v1/${NC}"
echo -e "  OTA HTTP  : ${CYAN}http://${LOCAL_IP}:18003/voice_assistant/ota/${NC}"
echo -e "  RKLLM API : ${CYAN}http://${LOCAL_IP}:${RKLLM_PORT}/rkllm_chat${NC}"
if [ -f "${VISION_PID_FILE}" ]; then
echo -e "  Vision API: ${CYAN}http://${LOCAL_IP}:${VISION_PORT}/vision/analyze${NC}"
fi
echo
echo -e "  ${YELLOW}[RK3588 本机访问]${NC} (支持麦克+ WebSocket)"
echo -e "    地址: ${CYAN}http://127.0.0.1:${WEB_HTTP_PORT}/${NC}"
echo -e "    OTA:  ${CYAN}http://127.0.0.1:18003/voice_assistant/ota/${NC}"
echo
echo -e "  ${YELLOW}[远程客户端访问]${NC} (支持麦克风，首次需接受证书)"
echo -e "    地址: ${CYAN}https://${LOCAL_IP}:${WEB_HTTPS_PORT}/${NC}"
echo -e "    OTA:  ${CYAN}http://${LOCAL_IP}:18003/voice_assistant/ota/${NC}"
echo
info "停止服务请运 ./stop.sh"
echo
info "正在显示实时日志（Ctrl+C 退出日志，服务不会停止.."
echo -e "${CYAN}────────────────────── 实时日志 ──────────────────────${NC}"

# tail -f 同时追踪所有服务日
tail -f "${RKLLM_LOG}" "${VISION_LOG}" "${VOICE_ASSISTANT_LOG}" 2>/dev/null || true
