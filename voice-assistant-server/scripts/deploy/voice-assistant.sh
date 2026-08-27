#!/bin/bash
# ============================================================
# voice-assistant.sh — 统一启停管理（合并原 start.sh / stop.sh）
# 运行环境：RK3588
#
# 用法
#   ./voice-assistant.sh start [模型参数...] [--no-vision] [--no-follow] [--open-browser]
#   ./voice-assistant.sh stop
#   ./voice-assistant.sh restart [同上]
#   ./voice-assistant.sh status
#   ./voice-assistant.sh desktop [同上]   # 桌面启动：后台启动并打开浏览器
#
# 模型参数（与旧 start.sh 兼容）
#   3b | 7b | qwen3-1.7b | 1.5b-z1 | qwen3-4b
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
MODEL_DIR="${HOME_DIR}/models"
RKLLM_SERVER_DIR="${HOME_DIR}/rknn-server"
VOICE_ASSISTANT_DIR="${HOME_DIR}/voice-assistant-server"
PLATFORM="rk3588"
CONDA_ENV="voice-assistant-server"

LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
LOCAL_IP=${LOCAL_IP:-127.0.0.1}

RKLLM_PORT=18080
VISION_PORT=18090
VOICE_ASSISTANT_WS_PORT=18000
VOICE_ASSISTANT_HTTP_PORT=18003
WEB_HTTP_PORT=18006
WEB_HTTPS_PORT=18007

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
START_LOCK_FILE="/tmp/voice_assistant_start.lock"

WEB_URL="https://127.0.0.1:${WEB_HTTPS_PORT}/"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO ]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN ]${NC} $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
section() { echo -e "\n${CYAN}══ $* ══${NC}"; }

wait_for_port() {
    local port=$1; local timeout=$2
    for _ in $(seq 1 "${timeout}"); do
        sleep 1
        if ss -tlnp 2>/dev/null | grep -q ":${port} "; then return 0; fi
        printf "."
    done
    echo; return 1
}

is_running() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

# 清理失效 pid，避免误判“已运行”或残留干扰
clear_stale_pid() {
    local pid_file=$1
    if [ -f "${pid_file}" ] && ! kill -0 "$(cat "${pid_file}")" 2>/dev/null; then
        rm -f "${pid_file}"
    fi
}

acquire_start_lock() {
    # 防止桌面连点导致多次并行加载 RKLLM（NPU 会分配失败）
    exec 9>"${START_LOCK_FILE}"
    if ! flock -n 9; then
        error "已有启动流程在进行中，请等待当前加载完成（不要连续点击）"
        exit 1
    fi
}

release_npu_soft() {
    # 轻量释放：杀掉残留 llm/vision
    pkill -f 'llm_server.py' 2>/dev/null || true
    pkill -f 'vision_server.py' 2>/dev/null || true
    clear_stale_pid "${RKLLM_PID_FILE}"
    clear_stale_pid "${VISION_PID_FILE}"
    sleep 1
    if sudo -n true 2>/dev/null; then
        sudo sync 2>/dev/null || true
        echo 3 | sudo tee /proc/sys/vm/drop_caches >/dev/null 2>&1 || true
        info "已尝试刷新系统缓存以释放 NPU 相关内存"
    fi
}

start_detached() {
    local log_file=$1
    local pid_file=$2
    shift 2

    rm -f "${pid_file}"
    if command -v setsid >/dev/null 2>&1; then
        nohup setsid bash -c 'exec 9>&-; echo $$ > "$1"; shift; exec "$@"' _ "${pid_file}" "$@" \
            > "${log_file}" 2>&1 < /dev/null &
    else
        warn "未找到 setsid，后台服务可能仍会受到终端 Ctrl+C 影响"
        nohup bash -c 'exec 9>&-; echo $$ > "$1"; shift; exec "$@"' _ "${pid_file}" "$@" \
            > "${log_file}" 2>&1 < /dev/null &
    fi

    for _ in $(seq 1 20); do
        [ -s "${pid_file}" ] && return 0
        sleep 0.1
    done
    echo $! > "${pid_file}"
}

wait_for_exit() {
    local pid=$1 timeout=$2
    for _ in $(seq 1 "${timeout}"); do
        sleep 1
        kill -0 "${pid}" 2>/dev/null || return 0
        printf "."
    done
    echo; return 1
}

stop_process() {
    local name=$1 pid_file=$2 normal_timeout=$3 force=$4 pattern=$5
    local pid=""

    if [ -f "${pid_file}" ]; then
        pid=$(cat "${pid_file}")
    fi

    if [ -z "${pid}" ] || ! kill -0 "${pid}" 2>/dev/null; then
        pid=$(pgrep -f "${pattern}" 2>/dev/null | head -1 || true)
        if [ -z "${pid}" ]; then
            info "${name} 未在运行，跳过"
            rm -f "${pid_file}"
            return 0
        fi
        warn "${name}: 通过进程名找到 PID=${pid}"
    fi

    info "停止 ${name} (PID=${pid})..."
    kill -TERM "${pid}" 2>/dev/null || true

    printf "等待正常退出"
    if wait_for_exit "${pid}" "${normal_timeout}"; then
        info "✓ ${name} 已正常退出"
    else
        warn "${name} ${normal_timeout}s 内未退出，强制终止..."
        kill -KILL "${pid}" 2>/dev/null || true
        printf "等待强制退出"
        if wait_for_exit "${pid}" "${force}"; then
            warn "${name} 已被强制终止"
        else
            error "无法终止 ${name} (PID=${pid})，请手动处理"
        fi
    fi
    rm -f "${pid_file}"
}

open_browser() {
    local url=$1
    info "打开浏览器: ${url}"
    if command -v xdg-open >/dev/null 2>&1; then
        nohup xdg-open "${url}" >/dev/null 2>&1 &
    elif command -v firefox >/dev/null 2>&1; then
        nohup firefox "${url}" >/dev/null 2>&1 &
    elif command -v google-chrome >/dev/null 2>&1; then
        nohup google-chrome "${url}" >/dev/null 2>&1 &
    elif command -v chromium-browser >/dev/null 2>&1; then
        nohup chromium-browser "${url}" >/dev/null 2>&1 &
    else
        warn "未找到可用浏览器，请手动访问: ${url}"
    fi
}

print_summary() {
    section "服务已就绪"
    echo -e "  ${GREEN}RKLLM Server${NC}    PID=$(cat ${RKLLM_PID_FILE} 2>/dev/null)  日志: ${RKLLM_LOG}"
    if [ -f "${VISION_PID_FILE}" ] && is_running "${VISION_PID_FILE}"; then
        echo -e "  ${GREEN}Vision Server${NC}   PID=$(cat ${VISION_PID_FILE} 2>/dev/null)  日志: ${VISION_LOG}"
    fi
    echo -e "  ${GREEN}voice-assistant-server${NC}  PID=$(cat ${VOICE_ASSISTANT_PID_FILE} 2>/dev/null)  日志: ${VOICE_ASSISTANT_LOG}"
    echo -e "  ${GREEN}Web HTTP${NC}       PID=$(cat ${WEB_HTTP_PID_FILE} 2>/dev/null)  日志: ${WEB_HTTP_LOG}"
    echo -e "  ${GREEN}Web HTTPS/WSS${NC}  PID=$(cat ${WEB_HTTPS_PID_FILE} 2>/dev/null)  日志: ${WEB_HTTPS_LOG}"
    echo
    echo -e "  RKLLM API : ${CYAN}http://${LOCAL_IP}:${RKLLM_PORT}/rkllm_chat${NC}"
    if [ -f "${VISION_PID_FILE}" ] && is_running "${VISION_PID_FILE}"; then
        echo -e "  Vision API: ${CYAN}http://${LOCAL_IP}:${VISION_PORT}/vision/analyze${NC}"
    fi
    echo
    echo -e "  ${YELLOW}[本机浏览器]${NC}"
    echo -e "    页面: ${CYAN}${WEB_URL}${NC}"
    echo -e "    OTA : ${CYAN}http://127.0.0.1:${VOICE_ASSISTANT_HTTP_PORT}/voice_assistant/ota/${NC}"
    echo
    echo -e "  ${YELLOW}[远程 HTTPS]${NC}"
    echo -e "    页面: ${CYAN}https://${LOCAL_IP}:${WEB_HTTPS_PORT}/${NC}"
    echo
}

do_status() {
    section "服务状态"
    for item in \
        "RKLLM Server|${RKLLM_PID_FILE}|${RKLLM_PORT}" \
        "Vision Server|${VISION_PID_FILE}|${VISION_PORT}" \
        "voice-assistant|${VOICE_ASSISTANT_PID_FILE}|${VOICE_ASSISTANT_WS_PORT}" \
        "Web HTTP|${WEB_HTTP_PID_FILE}|${WEB_HTTP_PORT}" \
        "Web HTTPS|${WEB_HTTPS_PID_FILE}|${WEB_HTTPS_PORT}"
    do
        IFS='|' read -r name pidf port <<< "${item}"
        if is_running "${pidf}"; then
            echo -e "  ${GREEN}●${NC} ${name}  PID=$(cat "${pidf}")  port=${port}"
        else
            echo -e "  ${RED}○${NC} ${name}  未运行"
        fi
    done
    echo
    echo -e "  Web: ${CYAN}${WEB_URL}${NC}"
}

do_stop() {
    section "停止 voice-assistant-server"
    stop_process "voice-assistant-server" "${VOICE_ASSISTANT_PID_FILE}" 10 5 "app.py"

    section "停止 Web 客户端 HTTP 服务"
    stop_process "Web HTTP服务" "${WEB_HTTP_PID_FILE}" 5 3 "http_server.py"

    section "停止 Web 客户端 HTTPS/WSS 网关"
    stop_process "Web HTTPS/WSS网关" "${WEB_HTTPS_PID_FILE}" 5 3 "https_gateway.py|https_server.py"

    section "停止视觉分析服务"
    stop_process "视觉服务" "${VISION_PID_FILE}" 15 5 "vision_server.py"

    section "停止 RKLLM Server 并释放 NPU 资源"
    stop_process "RKLLM Server" "${RKLLM_PID_FILE}" 20 5 "llm_server.py"

    section "资源检查"
    sleep 1
    if ss -tlnp 2>/dev/null | grep -q ":${RKLLM_PORT} "; then
        warn "端口 ${RKLLM_PORT} 仍被占用："; ss -tlnp | grep ":${RKLLM_PORT} " || true
    else
        info "✓ RKLLM 端口 ${RKLLM_PORT} 已释放"
    fi

    LEFTOVER_RKLLM=$(pgrep -f "llm_server.py" 2>/dev/null || true)
    LEFTOVER_VISION=$(pgrep -f "vision_server.py" 2>/dev/null || true)
    LEFTOVER_VOICE_ASSISTANT=$(pgrep -f "voice_assistant.*app.py" 2>/dev/null || true)
    if [ -n "${LEFTOVER_RKLLM}" ] || [ -n "${LEFTOVER_VISION}" ] || [ -n "${LEFTOVER_VOICE_ASSISTANT}" ]; then
        warn "发现残留进程，强制清理: ${LEFTOVER_RKLLM} ${LEFTOVER_VISION} ${LEFTOVER_VOICE_ASSISTANT}"
        kill -KILL ${LEFTOVER_RKLLM} ${LEFTOVER_VISION} ${LEFTOVER_VOICE_ASSISTANT} 2>/dev/null || true
    fi

    ZOMBIE_PIDS=$(ps -eo pid,stat,comm 2>/dev/null | awk '$2~/Z/ && $3~/python3?/ {print $1}' || true)
    if [ -n "${ZOMBIE_PIDS}" ]; then
        warn "发现僵尸进程: ${ZOMBIE_PIDS}，尝试自动清理..."
        for ZPID in ${ZOMBIE_PIDS}; do
            ZOMBIE_PPID=$(ps -o ppid= -p "${ZPID}" 2>/dev/null | tr -d ' ' || true)
            if [ -n "${ZOMBIE_PPID}" ] && [ "${ZOMBIE_PPID}" != "1" ] && [ "${ZOMBIE_PPID}" != "0" ]; then
                kill -CHLD "${ZOMBIE_PPID}" 2>/dev/null || true
                sleep 1
                if kill -0 "${ZPID}" 2>/dev/null; then
                    warn "SIGCHLD 无效，终止父进程 (PPID=${ZOMBIE_PPID})..."
                    kill -TERM "${ZOMBIE_PPID}" 2>/dev/null || true
                fi
            else
                warn "僵尸 PID=${ZPID} 父进程为 init，无法强制回收"
            fi
        done
        if sudo -n true 2>/dev/null; then
            sudo sync && echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null
            info "✓ 已执行 drop_caches，NPU 内存已刷新"
        else
            warn "建议手动执行：sudo sync && echo 3 | sudo tee /proc/sys/vm/drop_caches"
        fi
    else
        info "✓ 无僵尸进程"
    fi

    info "✓ 所有服务已停止"
}

do_start() {
    local FOLLOW_LOGS=true
    local OPEN_BROWSER=false
    local MODEL_SIZE="qwen3-4b"
    local ENABLE_VISION=true
    local START_ARGS=()

    for arg in "$@"; do
        case "$arg" in
            --no-vision)   ENABLE_VISION=false ;;
            --no-follow)   FOLLOW_LOGS=false ;;
            --open-browser) OPEN_BROWSER=true ;;
            3b|7b|qwen3-1.7b|1.5b-z1|qwen3-4b) MODEL_SIZE="$arg" ;;
            *) START_ARGS+=("$arg") ;;
        esac
    done

    local MODEL_PATH MODEL_LABEL MAX_CTX
    case "${MODEL_SIZE}" in
      3b)           MODEL_PATH="${MODEL_DIR}/qwen2.5-3b_w8a8.rkllm";        MODEL_LABEL="qwen2.5-3b";        MAX_CTX=4096 ;;
      7b)           MODEL_PATH="${MODEL_DIR}/qwen2.5-7b_w8a8.rkllm";        MODEL_LABEL="qwen2.5-7b";        MAX_CTX=4096 ;;
      qwen3-1.7b)   MODEL_PATH="${MODEL_DIR}/qwen3-1.7b_w8a8.rkllm";       MODEL_LABEL="qwen3-1.7b";       MAX_CTX=4096 ;;
      1.5b-z1)      MODEL_PATH="${MODEL_DIR}/Qwen2.5-1.5B-Z1-FC_W8A8_RK3588.rkllm"; MODEL_LABEL="Qwen2.5-1.5B-Z1-FC"; MAX_CTX=4096 ;;
      *)            MODEL_PATH="${MODEL_DIR}/qwen3-4b-instruct_w8a8.rkllm"; MODEL_LABEL="qwen3-4b-instruct"; MAX_CTX=4096 ;;
    esac

    section "环境准备"
    info "本机局域网 IP: ${LOCAL_IP}"

    # 防止桌面连点导致多次并行加载 RKLLM
    acquire_start_lock

    clear_stale_pid "${RKLLM_PID_FILE}"
    clear_stale_pid "${VISION_PID_FILE}"
    clear_stale_pid "${VOICE_ASSISTANT_PID_FILE}"
    clear_stale_pid "${WEB_HTTP_PID_FILE}"
    clear_stale_pid "${WEB_HTTPS_PID_FILE}"

    section "初始 conda 环境: ${CONDA_ENV}"
    CONDA_BASE=$(conda info --base 2>/dev/null || echo "")
    if [ -z "${CONDA_BASE}" ]; then
        for p in /opt/conda ~/miniconda3 ~/anaconda3 ~/miniforge3; do
            [ -f "${p}/etc/profile.d/conda.sh" ] && CONDA_BASE="${p}" && break
        done
    fi
    [ -z "${CONDA_BASE}" ] && { error "未找到 conda 安装"; exit 1; }

    # shellcheck source=/dev/null
    source "${CONDA_BASE}/etc/profile.d/conda.sh"
    conda activate "${CONDA_ENV}"
    info "conda 环境已激活: ${CONDA_ENV}"

    if [ -f /etc/ssl/openssl.cnf ]; then
        export OPENSSL_CONF=/etc/ssl/openssl.cnf
    fi

    section "启动 RKLLM Server (模型: ${MODEL_LABEL})"
    if is_running "${RKLLM_PID_FILE}"; then
        warn "RKLLM Server 已在运行 (PID=$(cat ${RKLLM_PID_FILE}))"
    else
        [ ! -f "${MODEL_PATH}" ] && { error "模型文件不存在: ${MODEL_PATH}"; exit 1; }
        if ss -tlnp 2>/dev/null | grep -q ":${RKLLM_PORT} "; then
            error "端口 ${RKLLM_PORT} 已被占用"; ss -tlnp | grep ":${RKLLM_PORT} "; exit 1
        fi

        FIX_FREQ="${RKLLM_SERVER_DIR}/scripts/fix_freq_${PLATFORM}.sh"
        if [ -f "${FIX_FREQ}" ]; then
            if sudo -n bash "${FIX_FREQ}" 2>/dev/null; then
                info "✓ CPU 频率已固定"
            else
                warn "跳过固定 CPU 频率（sudo 需要密码或脚本失败），推理性能可能受影响"
            fi
        fi

        cd "${RKLLM_SERVER_DIR}"
        PROMPT_CACHE_PATH="${MODEL_DIR}/voice_assistant_system_prompt.cache"
        SYSTEM_PROMPT_FILE="${MODEL_DIR}/voice_assistant_static_prompt.txt"
        WARMUP_ARGS=""
        if [ -f "${SYSTEM_PROMPT_FILE}" ]; then
            WARMUP_ARGS="--system_prompt_file ${SYSTEM_PROMPT_FILE}"
            info "找到静态 prompt 文件，启动时将预热 set_function_tools"
        else
            warn "未找到 ${SYSTEM_PROMPT_FILE}，首次请求将有延迟"
            warn "请先运行: ./generate_prompt_cache.sh --force"
        fi

        export LD_LIBRARY_PATH="${RKLLM_SERVER_DIR}/lib:${LD_LIBRARY_PATH}"

        local rkllm_ok=0
        local attempt
        for attempt in 1 2; do
            # shellcheck disable=SC2086
            start_detached "${RKLLM_LOG}" "${RKLLM_PID_FILE}" \
                python3 llm_server.py \
                --rkllm_model_path "${MODEL_PATH}" \
                --target_platform  "${PLATFORM}" \
                --port "${RKLLM_PORT}" \
                --prompt_cache_path "${PROMPT_CACHE_PATH}" \
                --max_context_len  "${MAX_CTX}" \
                ${WARMUP_ARGS}
            info "RKLLM Server 已启动 (PID=$(cat ${RKLLM_PID_FILE})) attempt=${attempt}"

            printf "加载中..."
            if wait_for_port "${RKLLM_PORT}" 90; then
                info "RKLLM Server 就绪: http://${LOCAL_IP}:${RKLLM_PORT}/rkllm_chat"
                rkllm_ok=1
                break
            fi

            if is_running "${RKLLM_PID_FILE}"; then
                warn "端口未开但进程仍在运行，模型可能仍在加载，继续..."
                rkllm_ok=1
                break
            fi

            error "RKLLM Server 启动失败（attempt=${attempt}），查看日志："
            tail -20 "${RKLLM_LOG}" || true
            rm -f "${RKLLM_PID_FILE}"
            if [ "${attempt}" = "1" ]; then
                warn "尝试清理 NPU/残留进程后重试一次..."
                release_npu_soft
            fi
        done

        if [ "${rkllm_ok}" != "1" ]; then
            error "RKLLM Server 两次启动均失败，中止后续服务启动"
            exit 1
        fi
    fi

    if [[ "${ENABLE_VISION}" == "true" && -f "${VISION_RKNN_MODEL}" && -f "${VISION_RKLLM_MODEL}" ]]; then
        section "启动视觉分析服务"
        if is_running "${VISION_PID_FILE}"; then
            warn "视觉服务已在运行 (PID=$(cat ${VISION_PID_FILE}))"
        else
            if ss -tlnp 2>/dev/null | grep -q ":${VISION_PORT} "; then
                error "端口 ${VISION_PORT} 已被占用"; ss -tlnp | grep ":${VISION_PORT} "; exit 1
            fi
            cd "${RKLLM_SERVER_DIR}"
            export LD_LIBRARY_PATH="${RKLLM_SERVER_DIR}/lib:${LD_LIBRARY_PATH}"
            start_detached "${VISION_LOG}" "${VISION_PID_FILE}" \
                python3 vision_server.py \
                --rknn_model "${VISION_RKNN_MODEL}" \
                --rkllm_model "${VISION_RKLLM_MODEL}" \
                --npu_core 1 \
                --port "${VISION_PORT}"
            info "视觉服务已启动 (PID=$(cat ${VISION_PID_FILE}))"
            printf "加载中..."
            if wait_for_port "${VISION_PORT}" 120; then
                info "视觉服务就绪: http://0.0.0.0:${VISION_PORT}/health"
            else
                if is_running "${VISION_PID_FILE}"; then
                    warn "端口未开但进程仍在运行，模型可能仍在加载，继续..."
                else
                    error "视觉服务启动失败，查看日志："; tail -20 "${VISION_LOG}"
                    warn "视觉服务启动失败，其他服务继续启动..."
                    rm -f "${VISION_PID_FILE}"
                fi
            fi
        fi
    else
        > "${VISION_LOG}" 2>/dev/null || true
        if [[ "${ENABLE_VISION}" == "false" ]]; then
            warn "已使用 --no-vision 参数，跳过视觉服务启动"
        else
            warn "视觉模型文件不存在，跳过视觉服务启动"
        fi
    fi

    section "启动 voice-assistant-server"
    if is_running "${VOICE_ASSISTANT_PID_FILE}"; then
        warn "voice-assistant-server 已在运行 (PID=$(cat ${VOICE_ASSISTANT_PID_FILE}))"
    else
        [ ! -f "${VOICE_ASSISTANT_DIR}/app.py" ] && { error "未找到 app.py: ${VOICE_ASSISTANT_DIR}/app.py"; exit 1; }
        cd "${VOICE_ASSISTANT_DIR}"
        start_detached "${VOICE_ASSISTANT_LOG}" "${VOICE_ASSISTANT_PID_FILE}" python3 app.py
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

    section "启动 Web 客户端服务（HTTP + HTTPS/WSS）"
    TEST_DIR="${VOICE_ASSISTANT_DIR}/web"

    if is_running "${WEB_HTTP_PID_FILE}"; then
        warn "Web HTTP 服务已在运行 (PID=$(cat ${WEB_HTTP_PID_FILE}))"
    else
        HTTP_SCRIPT="${TEST_DIR}/http_server.py"
        if [ ! -f "${HTTP_SCRIPT}" ]; then
            warn "未找到 HTTP 服务脚本: ${HTTP_SCRIPT}，跳过启动"
        else
            start_detached "${WEB_HTTP_LOG}" "${WEB_HTTP_PID_FILE}" \
                python3 "${HTTP_SCRIPT}" \
                --port "${WEB_HTTP_PORT}" \
                --web-dir "${TEST_DIR}"
            printf "  HTTP 启动中..."
            if wait_for_port "${WEB_HTTP_PORT}" 10; then
                info "Web HTTP 服务就绪 (端口 ${WEB_HTTP_PORT})"
            else
                warn "HTTP 服务可能仍在启动，请查看日志: ${WEB_HTTP_LOG}"
            fi
        fi
    fi

    if is_running "${WEB_HTTPS_PID_FILE}"; then
        warn "Web HTTPS/WSS 网关已在运行 (PID=$(cat ${WEB_HTTPS_PID_FILE}))"
    else
        HTTPS_SCRIPT="${TEST_DIR}/https_gateway.py"
        if [ ! -f "${HTTPS_SCRIPT}" ]; then
            warn "未找到 HTTPS/WSS 网关脚本: ${HTTPS_SCRIPT}，跳过启动"
        else
            start_detached "${WEB_HTTPS_LOG}" "${WEB_HTTPS_PID_FILE}" \
                python3 "${HTTPS_SCRIPT}" \
                --port "${WEB_HTTPS_PORT}" \
                --host "${LOCAL_IP}" \
                --cert-dir "${HOME_DIR}/.ssl" \
                --web-dir "${TEST_DIR}" \
                --http-target "http://127.0.0.1:${VOICE_ASSISTANT_HTTP_PORT}" \
                --ws-target "ws://127.0.0.1:${VOICE_ASSISTANT_WS_PORT}"
            printf "  HTTPS/WSS 网关启动中..."
            if wait_for_port "${WEB_HTTPS_PORT}" 10; then
                info "Web HTTPS/WSS 网关就绪 (端口 ${WEB_HTTPS_PORT})"
            else
                warn "HTTPS/WSS 网关可能仍在启动，请查看日志: ${WEB_HTTPS_LOG}"
            fi
        fi
    fi

    print_summary

    if [[ "${OPEN_BROWSER}" == "true" ]]; then
        if wait_for_port "${WEB_HTTP_PORT}" 5; then
            open_browser "${WEB_URL}"
        else
            open_browser "${WEB_URL}"
        fi
    fi

    if [[ "${FOLLOW_LOGS}" == "true" ]]; then
        info "停止服务请运行: $0 stop"
        info "正在显示实时日志（Ctrl+C 仅退出日志，服务不会停止）"
        echo -e "${CYAN}────────────────────── 实时日志 ──────────────────────${NC}"
        tail -f "${RKLLM_LOG}" "${VISION_LOG}" "${VOICE_ASSISTANT_LOG}" "${WEB_HTTPS_LOG}" 2>/dev/null || true
        echo
        info "已退出实时日志，服务仍在后台运行"
        info "再次查看日志请运行 ./logs.sh"
    else
        info "服务已在后台运行（未跟随日志）"
        info "停止: $0 stop   |  日志: ./logs.sh"
    fi
}

usage() {
    cat <<EOF
用法: $0 <命令> [选项]

命令:
  start     启动全部服务（默认跟随日志）
  stop      停止全部服务
  restart   先停后启
  status    查看运行状态
  desktop   桌面启动：后台启动并自动打开浏览器

启动选项:
  3b | 7b | qwen3-1.7b | 1.5b-z1 | qwen3-4b
  --no-vision
  --no-follow
  --open-browser

示例:
  $0 start
  $0 start 3b --no-follow
  $0 desktop
  $0 stop
EOF
}

CMD="${1:-}"
shift || true

case "${CMD}" in
    start)
        do_start "$@"
        ;;
    stop)
        do_stop
        ;;
    restart)
        do_stop
        do_start "$@"
        ;;
    status)
        do_status
        ;;
    desktop)
        # 桌面图标入口：不跟随日志，启动后打开本机 Web 页
        do_start --no-follow --open-browser "$@"
        ;;
    -h|--help|help|"")
        usage
        [[ -n "${CMD}" ]] || exit 1
        ;;
    *)
        error "未知命令: ${CMD}"
        usage
        exit 1
        ;;
esac
