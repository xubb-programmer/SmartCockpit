#!/bin/bash
# ============================================================
# stop.sh — 停止 voice-assistant-server + Web HTTP 服务 + RKLLM Server
# 停止顺序：先停应用层 voice_assistant → HTTP 服务 → 再释放 NPU 资源 RKLLM
# 运行环境：RK3588 (10.100.25.56)
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
RKLLM_SERVER_DIR="${HOME_DIR}/rknn-server"
VOICE_ASSISTANT_DIR="${HOME_DIR}/voice-assistant-server"
WEB_DIR="${VOICE_ASSISTANT_DIR}/web"

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
RKLLM_PORT=18080
WEB_HTTP_PORT=18006
WEB_HTTPS_PORT=18007

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO ]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN ]${NC} $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
section() { echo -e "\n${CYAN}══ $* ══${NC}"; }

# 等待进程退出，超时返回非零
wait_for_exit() {
    local pid=$1 timeout=$2
    for i in $(seq 1 "${timeout}"); do
        sleep 1
        kill -0 "${pid}" 2>/dev/null || return 0
        printf "."
    done
    echo; return 1
}

find_pids_by_command() {
    local script_pattern=$1 expected_path=$2 expected_cwd=$3
    local candidate cmdline cwd

    pgrep -f "${script_pattern}" 2>/dev/null | while read -r candidate; do
        [ "${candidate}" = "$$" ] && continue
        cmdline=$(tr '\0' ' ' < "/proc/${candidate}/cmdline" 2>/dev/null || true)
        cwd=$(readlink -f "/proc/${candidate}/cwd" 2>/dev/null || true)

        if [ -n "${expected_path}" ] && [[ "${cmdline}" == *"${expected_path}"* ]]; then
            echo "${candidate}"
            continue
        fi

        if [ -n "${expected_cwd}" ] && [ "${cwd}" = "${expected_cwd}" ] && [[ "${cmdline}" =~ ${script_pattern} ]]; then
            echo "${candidate}"
        fi
    done
}

# 通用停止函数：SIGTERM → 等待 → SIGKILL
stop_process() {
    local name=$1 pid_file=$2 normal_timeout=$3 force=$4

    local pid=""
    if [ -f "${pid_file}" ]; then
        pid=$(cat "${pid_file}")
    fi

    # PID 文件不存在或进程已死，按脚本名和项目路径查找兜底
    if [ -z "${pid}" ] || ! kill -0 "${pid}" 2>/dev/null; then
        local script_pattern=$5 expected_path=$6 expected_cwd=$7
        pid=$(find_pids_by_command "${script_pattern}" "${expected_path}" "${expected_cwd}" | head -1 || true)
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

# ================================================================
# 1. 先停 voice-assistant-server（应用层）
# ================================================================
section "停止 voice-assistant-server"
stop_process "voice-assistant-server" "${VOICE_ASSISTANT_PID_FILE}" 10 5 "app.py" "${VOICE_ASSISTANT_DIR}/app.py" "${VOICE_ASSISTANT_DIR}"

# ================================================================
# 2. 停止 Web 客户端 HTTP 服务
# ================================================================
section "停止 Web 客户端 HTTP 服务"
stop_process "Web HTTP服务" "${WEB_HTTP_PID_FILE}" 5 3 "http_server.py" "${WEB_DIR}/http_server.py" "${VOICE_ASSISTANT_DIR}"

# ================================================================
# 2b. 停止 Web 客户端 HTTPS/WSS 网关
# ================================================================
section "停止 Web 客户端 HTTPS/WSS 网关"
stop_process "Web HTTPS/WSS网关" "${WEB_HTTPS_PID_FILE}" 5 3 "https_gateway.py|https_server.py" "${WEB_DIR}/https_gateway.py" "${VOICE_ASSISTANT_DIR}"

# ================================================================
# 3. 停止视觉分析服务
# ================================================================
section "停止视觉分析服务"
stop_process "视觉服务" "${VISION_PID_FILE}" 15 5 "vision_server.py" "${RKLLM_SERVER_DIR}/vision_server.py" "${RKLLM_SERVER_DIR}"

# ================================================================
# 4. 再停 RKLLM Server（让 rkllm_model.release() 释放 NPU 资源）
# ================================================================
section "停止 RKLLM Server 并释放 NPU 资源"
# 给 llm_server 更长的正常退出时间（rkllm_model.release() 需要时间）
stop_process "RKLLM Server" "${RKLLM_PID_FILE}" 20 5 "llm_server.py" "${RKLLM_SERVER_DIR}/llm_server.py" "${RKLLM_SERVER_DIR}"

# ================================================================
# 4. 验证资源已释放
# ================================================================
section "资源检查"

sleep 1
if ss -tlnp | grep -q ":${RKLLM_PORT} "; then
    warn "端口 ${RKLLM_PORT} 仍被占用："; ss -tlnp | grep ":${RKLLM_PORT} "
else
    info "✓ RKLLM 端口 ${RKLLM_PORT} 已释放"
fi

# 检查残留 python 进程（兜底清理）
LEFTOVER_RKLLM=$(find_pids_by_command "llm_server.py" "${RKLLM_SERVER_DIR}/llm_server.py" "${RKLLM_SERVER_DIR}" || true)
LEFTOVER_VISION=$(find_pids_by_command "vision_server.py" "${RKLLM_SERVER_DIR}/vision_server.py" "${RKLLM_SERVER_DIR}" || true)
LEFTOVER_VOICE_ASSISTANT=$(find_pids_by_command "app.py" "${VOICE_ASSISTANT_DIR}/app.py" "${VOICE_ASSISTANT_DIR}" || true)
if [ -n "${LEFTOVER_RKLLM}" ] || [ -n "${LEFTOVER_VISION}" ] || [ -n "${LEFTOVER_VOICE_ASSISTANT}" ]; then
    warn "发现残留进程，强制清理: ${LEFTOVER_RKLLM} ${LEFTOVER_VISION} ${LEFTOVER_VOICE_ASSISTANT}"
    kill -KILL ${LEFTOVER_RKLLM} ${LEFTOVER_VISION} ${LEFTOVER_VOICE_ASSISTANT} 2>/dev/null || true
fi

# 检查并清理僵尸进程（自动处理，不仅仅是提示）
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
            # 父进程是 init，僵尸无法强制清理，只能清内存
            warn "僵尸 PID=${ZPID} 父进程为 init，无法强制回收（无害，重启系统可彻底清理）"
        fi
    done

    # 无论如何都清理内核缓存，释放 NPU 内存占用
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
info "RKLLM 日志：tail -50 ${RKLLM_LOG}"
info "视觉服务日志：tail -50 ${VISION_LOG}"
info "voice_assistant 日志：tail -50 ${VOICE_ASSISTANT_LOG}"
info "HTTP 日志：tail -50 ${WEB_HTTP_LOG}"
info "HTTPS/WSS 网关日志：tail -50 ${WEB_HTTPS_LOG}"
