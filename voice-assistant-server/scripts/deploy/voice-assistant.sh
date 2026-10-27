#!/bin/bash
# ============================================================
# voice-assistant.sh — 统一启停管理（合并原 start.sh / stop.sh）
# 运行环境：RK3588
#
# 用法
#   ./voice-assistant.sh start [模型参数...] [--vision] [--no-follow] [--open-browser]
#   ./voice-assistant.sh stop
#   ./voice-assistant.sh restart [同上]
#   ./voice-assistant.sh status
#   ./voice-assistant.sh desktop         # 检查后台就绪后打开浏览器
#
# 模型参数（与旧 start.sh 兼容）
#   qwen3.5 | qwen3.5-2b | 3b | 7b | qwen3-1.7b | qwen3-4b
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
# 18081 仅用于清理旧版双实例部署，新版本统一使用 18080 单实例。
LEGACY_VISION_RKLLM_PORT=18081
VOICE_ASSISTANT_WS_PORT=18000
VOICE_ASSISTANT_HTTP_PORT=18003
WEB_HTTP_PORT=18006
WEB_HTTPS_PORT=18007
DOWNLOAD_DIR="${DOWNLOAD_DIR:-${HOME_DIR}/downloads}"

VISION_RKNN_MODEL="${VISION_RKNN_MODEL:-}"
# 视觉播报限定为短句，减少 0.8B 多模态生成尾延时；可用环境变量覆盖。
VISION_MAX_NEW_TOKENS="${VISION_MAX_NEW_TOKENS:-48}"
RKLLM_TEXT_MAX_NEW_TOKENS="${RKLLM_TEXT_MAX_NEW_TOKENS:-256}"
VISION_NPU_CORE="${VISION_NPU_CORE:-3}"
VISION_IMAGE_WIDTH="${VISION_IMAGE_WIDTH:-448}"
VISION_IMAGE_HEIGHT="${VISION_IMAGE_HEIGHT:-448}"
VISION_PREENCODE_PATH="${VISION_PREENCODE_PATH:-/tmp/voice_assistant_latest_vision.jpg}"
VISION_PREENCODE_INTERVAL="${VISION_PREENCODE_INTERVAL:-1.0}"
VISION_IMG_START="${VISION_IMG_START:-<|vision_start|>}"
VISION_IMG_END="${VISION_IMG_END:-<|vision_end|>}"
VISION_IMG_CONTENT="${VISION_IMG_CONTENT:-<|image_pad|>}"

RKLLM_PID_FILE="/tmp/rkllm_server.pid"
LEGACY_VISION_RKLLM_PID_FILE="/tmp/rkllm_vision_server.pid"
VOICE_ASSISTANT_PID_FILE="/tmp/voice_assistant_server.pid"
WEB_HTTP_PID_FILE="/tmp/web_http_server.pid"
WEB_HTTPS_PID_FILE="/tmp/web_https_server.pid"
HEADLESS_VOICE_PID_FILE="/tmp/headless_voice_client.pid"
RKLLM_LOG="/tmp/rkllm_server.log"
VOICE_ASSISTANT_LOG="/tmp/voice_assistant_server.log"
WEB_HTTP_LOG="/tmp/web_http_server.log"
WEB_HTTPS_LOG="/tmp/web_https_server.log"
HEADLESS_VOICE_LOG="/tmp/headless_voice_client.log"
START_LOCK_FILE="/tmp/voice_assistant_start.lock"
ACCESS_MODE_FILE="/tmp/voice_assistant_access_mode"
HEADLESS_VOICE_CLIENT="${VOICE_ASSISTANT_DIR}/clients/headless_voice_client.py"
ACTIVE_ACCESS_MODE="web"

WEB_URL="https://127.0.0.1:${WEB_HTTPS_PORT}/?v=0903a"

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

prepare_download_dir() {
    local requested="${DOWNLOAD_DIR}"
    local fallback="${VOICE_ASSISTANT_DIR}/downloads"

    if mkdir -p "${requested}" 2>/dev/null; then
        return 0
    fi

    if [[ "${requested}" == "${HOME_DIR}/downloads" ]] \
        && command -v sudo >/dev/null 2>&1 \
        && sudo -n install -d -m 0755 -o "$(id -un)" -g "$(id -gn)" "${requested}" 2>/dev/null; then
        info "已创建文件下载目录: ${requested}"
        return 0
    fi

    warn "无法创建文件下载目录 ${requested}，改用 ${fallback}"
    if ! mkdir -p "${fallback}"; then
        error "无法创建备用文件下载目录: ${fallback}"
        return 1
    fi
    DOWNLOAD_DIR="${fallback}"
}

is_running() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

rkllm_vision_enabled() {
    local pid
    is_running "${RKLLM_PID_FILE}" || return 1
    pid=$(cat "${RKLLM_PID_FILE}")
    [ -r "/proc/${pid}/cmdline" ] \
        && tr '\0' ' ' < "/proc/${pid}/cmdline" | grep -q -- '--vision_rknn_model_path'
}

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
    # 轻量释放：杀掉残留 llm 以及历史版本独立视觉进程
    pkill -f 'llm_server.py' 2>/dev/null || true
    pkill -f '[v]ision_server.py' 2>/dev/null || true
    clear_stale_pid "${RKLLM_PID_FILE}"
    clear_stale_pid "${LEGACY_VISION_RKLLM_PID_FILE}"
    rm -f /tmp/vision_server.pid
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

show_desktop_message() {
    local title="$1"
    local message="$2"
    if command -v zenity >/dev/null 2>&1; then
        zenity --warning --title="${title}" --text="${message}" --width=430 >/dev/null 2>&1 || true
    elif command -v xmessage >/dev/null 2>&1; then
        xmessage -center -title "${title}" "${message}" >/dev/null 2>&1 || true
    elif command -v notify-send >/dev/null 2>&1; then
        notify-send -u normal "${title}" "${message}" >/dev/null 2>&1 || true
    else
        warn "${title}: ${message}"
    fi
}

desktop_backend_ready() {
    if ! systemctl is-enabled --quiet voice-assistant.service 2>/dev/null; then
        show_desktop_message "语音助手未配置自启" "未检测到 voice-assistant.service，请先配置开机自启动。"
        return 1
    fi

    if ! systemctl is-active --quiet voice-assistant.service 2>/dev/null; then
        if systemctl is-failed --quiet voice-assistant.service 2>/dev/null; then
            show_desktop_message "语音助手启动失败" "后台语音服务启动失败，请查看：journalctl -u voice-assistant.service -b"
        else
            show_desktop_message "语音助手正在启动" "后台服务尚未完成启动，请稍候几秒后再次双击桌面图标。"
        fi
        return 1
    fi

    for port in "${VOICE_ASSISTANT_WS_PORT}" "${VOICE_ASSISTANT_HTTP_PORT}" "${WEB_HTTP_PORT}" "${WEB_HTTPS_PORT}" "${RKLLM_PORT}"; do
        if ! ss -H -ltn 2>/dev/null | grep -q ":${port}[[:space:]]"; then
            show_desktop_message "语音助手正在启动" "后台服务已启动但端口 ${port} 尚未就绪，请稍候几秒后再次双击桌面图标。"
            return 1
        fi
    done
    return 0
}

do_desktop() {
    if desktop_backend_ready; then
        open_browser "${WEB_URL}"
    fi
}

print_summary() {
    section "服务已就绪"
    if rkllm_vision_enabled; then
        echo -e "  ${GREEN}RKLLM 多模态服务${NC} PID=$(cat ${RKLLM_PID_FILE} 2>/dev/null)  日志: ${RKLLM_LOG}"
    else
        echo -e "  ${GREEN}RKLLM 文本服务${NC}  PID=$(cat ${RKLLM_PID_FILE} 2>/dev/null)  日志: ${RKLLM_LOG}"
    fi
    echo -e "  ${GREEN}voice-assistant-server${NC}  PID=$(cat ${VOICE_ASSISTANT_PID_FILE} 2>/dev/null)  日志: ${VOICE_ASSISTANT_LOG}"
    echo -e "  ${GREEN}Web HTTP${NC}       PID=$(cat ${WEB_HTTP_PID_FILE} 2>/dev/null)  日志: ${WEB_HTTP_LOG}"
    echo -e "  ${GREEN}Web HTTPS/WSS${NC}  PID=$(cat ${WEB_HTTPS_PID_FILE} 2>/dev/null)  日志: ${WEB_HTTPS_LOG}"
    if [[ "${ACTIVE_ACCESS_MODE}" == "wakeup" || "${ACTIVE_ACCESS_MODE}" == "hybrid" ]]; then
        echo -e "  ${GREEN}板端唤醒客户端${NC} PID=$(cat ${HEADLESS_VOICE_PID_FILE} 2>/dev/null)  日志: ${HEADLESS_VOICE_LOG}"
    fi
    echo
    echo -e "  语音入口 : ${CYAN}${ACTIVE_ACCESS_MODE}${NC}"
    echo -e "  RKLLM API : ${CYAN}http://${LOCAL_IP}:${RKLLM_PORT}/rkllm_chat${NC}"
    if rkllm_vision_enabled; then
        echo -e "  Vision API: ${CYAN}http://${LOCAL_IP}:${RKLLM_PORT}/vision/analyze${NC}  ${YELLOW}(同一多模态实例)${NC}"
    fi
    echo
    echo -e "  ${YELLOW}[本机浏览器]${NC}"
    echo -e "    页面: ${CYAN}${WEB_URL}${NC}"
    echo -e "    OTA : ${CYAN}http://127.0.0.1:${VOICE_ASSISTANT_HTTP_PORT}/voice_assistant/ota/${NC}"
    echo
    echo -e "  ${YELLOW}[远程 HTTPS]${NC}"
    echo -e "    页面: ${CYAN}https://${LOCAL_IP}:${WEB_HTTPS_PORT}/${NC}"
    echo
    echo -e "  ${YELLOW}[文件下载（兼容 Windows XP）]${NC}"
    echo -e "    地址: ${CYAN}http://${LOCAL_IP}:${WEB_HTTP_PORT}/download/${NC}"
    echo -e "    目录: ${DOWNLOAD_DIR}"
    echo
}

do_status() {
    section "服务状态"
    for item in \
        "RKLLM|${RKLLM_PID_FILE}|${RKLLM_PORT}" \
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
    ACTIVE_ACCESS_MODE="$(cat "${ACCESS_MODE_FILE}" 2>/dev/null || echo web)"
    if [[ "${ACTIVE_ACCESS_MODE}" == "wakeup" || "${ACTIVE_ACCESS_MODE}" == "hybrid" ]]; then
        if is_running "${HEADLESS_VOICE_PID_FILE}"; then
            echo -e "  ${GREEN}●${NC} 板端唤醒客户端  PID=$(cat "${HEADLESS_VOICE_PID_FILE}")"
        else
            echo -e "  ${RED}○${NC} 板端唤醒客户端  未运行"
        fi
    fi
    if rkllm_vision_enabled; then
        echo -e "  ${GREEN}●${NC} 视觉分析  与 RKLLM 共用 port=${RKLLM_PORT}"
    else
        echo -e "  ${YELLOW}○${NC} 视觉分析  未启用"
    fi
    echo
    echo -e "  Web: ${CYAN}${WEB_URL}${NC}"
}

do_stop() {
    section "停止板端唤醒客户端"
    stop_process "板端唤醒客户端" "${HEADLESS_VOICE_PID_FILE}" 5 3 "clients/headless_voice_client.py"

    section "停止 voice-assistant-server"
    stop_process "voice-assistant-server" "${VOICE_ASSISTANT_PID_FILE}" 10 5 "app.py"

    section "停止 Web 客户端 HTTP 服务"
    stop_process "Web HTTP服务" "${WEB_HTTP_PID_FILE}" 5 3 "http_server.py"

    section "停止 Web 客户端 HTTPS/WSS 网关"
    stop_process "Web HTTPS/WSS网关" "${WEB_HTTPS_PID_FILE}" 5 3 "https_gateway.py|https_server.py"

    section "清理旧版 RKLLM 多模态服务"
    stop_process "旧版 RKLLM 多模态服务" "${LEGACY_VISION_RKLLM_PID_FILE}" 20 5 "llm_server.py.*--port ${LEGACY_VISION_RKLLM_PORT}"

    section "清理历史独立视觉进程"
    LEGACY_VISION_PIDS=$(pgrep -f "[v]ision_server.py" 2>/dev/null || true)
    if [ -n "${LEGACY_VISION_PIDS}" ]; then
        warn "发现历史独立视觉进程，准备清理: ${LEGACY_VISION_PIDS}"
        kill -TERM ${LEGACY_VISION_PIDS} 2>/dev/null || true
        sleep 1
    else
        info "无历史独立视觉进程"
    fi
    rm -f /tmp/vision_server.pid

    section "停止 RKLLM 文本服务并释放 NPU 资源"
    stop_process "RKLLM 文本服务" "${RKLLM_PID_FILE}" 20 5 "llm_server.py.*--port ${RKLLM_PORT}"

    section "资源检查"
    sleep 1
    if ss -tlnp 2>/dev/null | grep -q ":${RKLLM_PORT} "; then
        warn "端口 ${RKLLM_PORT} 仍被占用："; ss -tlnp | grep ":${RKLLM_PORT} " || true
    else
        info "✓ RKLLM 端口 ${RKLLM_PORT} 已释放"
    fi
    if ss -tlnp 2>/dev/null | grep -q ":${LEGACY_VISION_RKLLM_PORT} "; then
        warn "旧版端口 ${LEGACY_VISION_RKLLM_PORT} 仍被占用："; ss -tlnp | grep ":${LEGACY_VISION_RKLLM_PORT} " || true
    else
        info "✓ 旧版多模态端口 ${LEGACY_VISION_RKLLM_PORT} 已释放"
    fi

    LEFTOVER_RKLLM=$(pgrep -f "llm_server.py" 2>/dev/null || true)
    LEFTOVER_LEGACY_VISION=$(pgrep -f "[v]ision_server.py" 2>/dev/null || true)
    LEFTOVER_VOICE_ASSISTANT=$(pgrep -f "voice_assistant.*app.py" 2>/dev/null || true)
    if [ -n "${LEFTOVER_RKLLM}" ] || [ -n "${LEFTOVER_LEGACY_VISION}" ] || [ -n "${LEFTOVER_VOICE_ASSISTANT}" ]; then
        warn "发现残留进程，强制清理: ${LEFTOVER_RKLLM} ${LEFTOVER_LEGACY_VISION} ${LEFTOVER_VOICE_ASSISTANT}"
        kill -KILL ${LEFTOVER_RKLLM} ${LEFTOVER_LEGACY_VISION} ${LEFTOVER_VOICE_ASSISTANT} 2>/dev/null || true
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
    rm -f "${ACCESS_MODE_FILE}"
}

read_access_mode() {
    local mode
    [ -f "${HEADLESS_VOICE_CLIENT}" ] || { echo "web"; return 0; }
    mode="$(python3 "${HEADLESS_VOICE_CLIENT}" --print-mode 2>/dev/null | tail -n 1)"
    case "${mode}" in
        web|wakeup|hybrid) echo "${mode}" ;;
        *) echo "web" ;;
    esac
}

selected_model_from_args() {
    local selected="${VOICE_ASSISTANT_MODEL:-qwen3-4b}"
    local arg
    for arg in "$@"; do
        case "${arg}" in
            qwen3.5|qwen35|qwen3.5-0.8b|qwen35-0.8b) selected="qwen3.5-0.8b" ;;
            qwen3.5-2b|qwen35-2b) selected="qwen3.5-2b" ;;
            3b|7b|qwen3-1.7b|qwen3-4b) selected="${arg}" ;;
        esac
    done
    echo "${selected}"
}

validate_start_request() {
    local selected
    local vision_requested=false
    local arg
    selected=$(selected_model_from_args "$@")
    for arg in "$@"; do
        [ "${arg}" = "--vision" ] && vision_requested=true
    done

    if [[ "${vision_requested}" == "true" && "${selected}" != qwen3.5-* ]]; then
        error "模型 ${selected} 是纯文本模型，不支持 --vision"
        error "请改用: $0 start qwen3.5 --vision --no-follow"
        return 1
    fi
}

do_start() {
    local FOLLOW_LOGS=true
    local OPEN_BROWSER=false
    local MODEL_SIZE="${VOICE_ASSISTANT_MODEL:-qwen3-4b}"
    local ENABLE_VISION=false

    for arg in "$@"; do
        case "$arg" in
            --vision)      ENABLE_VISION=true ;;
            --no-follow)   FOLLOW_LOGS=false ;;
            --open-browser) OPEN_BROWSER=true ;;
            qwen3.5|qwen35|qwen3.5-0.8b|qwen35-0.8b) MODEL_SIZE="qwen3.5-0.8b" ;;
            qwen3.5-2b|qwen35-2b) MODEL_SIZE="qwen3.5-2b" ;;
            3b|7b|qwen3-1.7b|qwen3-4b) MODEL_SIZE="$arg" ;;
            *) error "未知启动参数: ${arg}"; usage; exit 1 ;;
        esac
    done

    local MODEL_PATH MODEL_LABEL MODEL_CACHE_KEY MAX_CTX
    case "${MODEL_SIZE}" in
      qwen3.5-0.8b) MODEL_PATH="${MODEL_DIR}/Qwen3.5-0.8B_w8a8_rk3588.rkllm"; MODEL_LABEL="Qwen3.5-0.8B"; MODEL_CACHE_KEY="qwen3.5-0.8b"; MAX_CTX=4096 ;;
      qwen3.5-2b)   MODEL_PATH="${MODEL_DIR}/qwen3.5-2b_w8a8_rk3588.rkllm";  MODEL_LABEL="Qwen3.5-2B";   MODEL_CACHE_KEY="qwen3.5-2b";   MAX_CTX=4096 ;;
      3b)           MODEL_PATH="${MODEL_DIR}/qwen2.5-3b_w8a8.rkllm";        MODEL_LABEL="qwen2.5-3b";        MODEL_CACHE_KEY="qwen2.5-3b"; MAX_CTX=4096 ;;
      7b)           MODEL_PATH="${MODEL_DIR}/qwen2.5-7b_w8a8.rkllm";        MODEL_LABEL="qwen2.5-7b";        MODEL_CACHE_KEY="qwen2.5-7b"; MAX_CTX=4096 ;;
      qwen3-1.7b)   MODEL_PATH="${MODEL_DIR}/qwen3-1.7b_w8a8.rkllm";       MODEL_LABEL="qwen3-1.7b";       MODEL_CACHE_KEY="qwen3-1.7b"; MAX_CTX=4096 ;;
      *)            MODEL_PATH="${MODEL_DIR}/qwen3-4b-instruct_w8a8.rkllm"; MODEL_LABEL="qwen3-4b-instruct"; MODEL_CACHE_KEY="qwen3-4b-instruct"; MAX_CTX=4096 ;;
    esac

    section "环境准备"
    info "本机局域网 IP: ${LOCAL_IP}"

    # 防止桌面连点导致多次并行加载 RKLLM
    acquire_start_lock

    clear_stale_pid "${RKLLM_PID_FILE}"
    clear_stale_pid "${LEGACY_VISION_RKLLM_PID_FILE}"
    clear_stale_pid "${VOICE_ASSISTANT_PID_FILE}"
    clear_stale_pid "${WEB_HTTP_PID_FILE}"
    clear_stale_pid "${WEB_HTTPS_PID_FILE}"
    clear_stale_pid "${HEADLESS_VOICE_PID_FILE}"

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

    ACTIVE_ACCESS_MODE="$(read_access_mode)"
    printf '%s\n' "${ACTIVE_ACCESS_MODE}" > "${ACCESS_MODE_FILE}"
    info "语音入口模式: ${ACTIVE_ACCESS_MODE}"
    if [[ "${ACTIVE_ACCESS_MODE}" == "wakeup" || "${ACTIVE_ACCESS_MODE}" == "hybrid" ]]; then
        if ! python3 "${HEADLESS_VOICE_CLIENT}" --check; then
            error "板端唤醒客户端预检失败，请检查KWS模型和音频设备"
            exit 1
        fi
    fi

    if [ -f /etc/ssl/openssl.cnf ]; then
        export OPENSSL_CONF=/etc/ssl/openssl.cnf
    fi

    local EFFECTIVE_VISION_RKNN_MODEL="${VISION_RKNN_MODEL}"
    local VISION_ARGS=()
    if [[ -z "${EFFECTIVE_VISION_RKNN_MODEL}" ]]; then
        case "${MODEL_SIZE}" in
          qwen3.5-0.8b) EFFECTIVE_VISION_RKNN_MODEL="${MODEL_DIR}/Qwen3.5-0.8B_vision_rk3588.rknn" ;;
          qwen3.5-2b)   EFFECTIVE_VISION_RKNN_MODEL="${MODEL_DIR}/qwen3.5-2b_vision_rk3588.rknn" ;;
        esac
    fi
    if [[ "${ENABLE_VISION}" == "true" ]]; then
        [ -n "${EFFECTIVE_VISION_RKNN_MODEL}" ] || { error "模型 ${MODEL_LABEL} 未配置配套视觉编码器"; exit 1; }
        [ -f "${EFFECTIVE_VISION_RKNN_MODEL}" ] || { error "视觉编码器模型不存在: ${EFFECTIVE_VISION_RKNN_MODEL}"; exit 1; }
        VISION_ARGS=(
            --vision_rknn_model_path "${EFFECTIVE_VISION_RKNN_MODEL}"
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
        info "启用官方单实例多模态模式: ${MODEL_LABEL} + $(basename "${EFFECTIVE_VISION_RKNN_MODEL}")"
    else
        info "默认不启动视觉服务；需要时请添加 --vision"
    fi

    export VOICE_ASSISTANT_VISION_ENABLED="${ENABLE_VISION}"
    local PROMPT_MODE="novision"
    local PROMPT_MODE_ARGS=()
    if [[ "${ENABLE_VISION}" == "true" ]]; then
        PROMPT_MODE="vision"
        PROMPT_MODE_ARGS=(--vision)
    fi
    local SYSTEM_PROMPT_FILE="${MODEL_DIR}/voice_assistant_static_prompt_${PROMPT_MODE}.txt"
    local PROMPT_RENDERER="${SCRIPT_DIR}/render_static_prompt.py"
    if [ ! -f "${PROMPT_RENDERER}" ]; then
        error "静态提示词渲染器不存在: ${PROMPT_RENDERER}"
        exit 1
    fi
    if ! python3 "${PROMPT_RENDERER}" --output "${SYSTEM_PROMPT_FILE}" "${PROMPT_MODE_ARGS[@]}"; then
        error "生成 ${PROMPT_MODE} 模式静态提示词失败"
        exit 1
    fi
    info "RKLLM 预热提示词模式: ${PROMPT_MODE}"

    export LD_LIBRARY_PATH="${RKLLM_SERVER_DIR}/lib:${LD_LIBRARY_PATH}"

    if [[ "${ENABLE_VISION}" == "true" ]]; then
        section "启动 RKLLM 多模态服务 (模型: ${MODEL_LABEL})"
    else
        section "启动 RKLLM 文本服务 (模型: ${MODEL_LABEL})"
    fi
    if is_running "${RKLLM_PID_FILE}"; then
        if ss -tlnp 2>/dev/null | grep -q ":${RKLLM_PORT} "; then
            if [[ "${ENABLE_VISION}" == "true" ]] && ! rkllm_vision_enabled; then
                error "当前 RKLLM 是纯文本模式；请使用 restart 重新加载多模态模型"
                exit 1
            fi
            warn "RKLLM Server 已在运行 (PID=$(cat ${RKLLM_PID_FILE}))"
        else
            error "RKLLM PID 存在但端口 ${RKLLM_PORT} 未监听，拒绝启动上层服务"
            tail -20 "${RKLLM_LOG}" || true
            exit 1
        fi
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
        PROMPT_CACHE_ARGS=()
        if [[ "${MODEL_SIZE}" == qwen3.5-* ]]; then
            warn "Qwen3.5 RKLLM 不支持保存 prompt cache 文件，启动时使用静态 prompt 预热"
        else
            PROMPT_CACHE_PATH="${MODEL_DIR}/voice_assistant_system_prompt_${MODEL_CACHE_KEY}_${PROMPT_MODE}.cache"
            PROMPT_CACHE_ARGS=(--prompt_cache_path "${PROMPT_CACHE_PATH}")
        fi

        WARMUP_ARGS=()
        if [ -f "${SYSTEM_PROMPT_FILE}" ]; then
            WARMUP_ARGS=(--system_prompt_file "${SYSTEM_PROMPT_FILE}")
            info "找到静态 prompt 文件，启动时将预热 set_function_tools"
        else
            warn "未找到 ${SYSTEM_PROMPT_FILE}，首次请求将有延迟"
            warn "请先运行: ./generate_prompt_cache.sh --force"
        fi

        local rkllm_ok=0
        local attempt
        for attempt in 1 2; do
            # shellcheck disable=SC2086
            start_detached "${RKLLM_LOG}" "${RKLLM_PID_FILE}" \
                python3 llm_server.py \
                --rkllm_model_path "${MODEL_PATH}" \
                --target_platform  "${PLATFORM}" \
                --port "${RKLLM_PORT}" \
                "${PROMPT_CACHE_ARGS[@]}" \
                --max_context_len  "${MAX_CTX}" \
                --text_max_new_tokens "${RKLLM_TEXT_MAX_NEW_TOKENS}" \
                "${WARMUP_ARGS[@]}" \
                "${VISION_ARGS[@]}"
            info "RKLLM 服务已启动 (PID=$(cat ${RKLLM_PID_FILE})) attempt=${attempt}"

            printf "加载中..."
            if wait_for_port "${RKLLM_PORT}" 90; then
                info "RKLLM 文本服务就绪: http://${LOCAL_IP}:${RKLLM_PORT}/rkllm_chat"
                if [[ "${ENABLE_VISION}" == "true" ]]; then
                    info "视觉分析就绪: http://${LOCAL_IP}:${RKLLM_PORT}/vision/analyze"
                    info "视觉参数: encoder=$(basename "${EFFECTIVE_VISION_RKNN_MODEL}"), max_tokens=${VISION_MAX_NEW_TOKENS}, preencode=${VISION_PREENCODE_INTERVAL}s"
                fi
                rkllm_ok=1
                break
            fi

            if is_running "${RKLLM_PID_FILE}"; then
                error "RKLLM 进程仍在运行但端口未开放，模型启动异常，停止该进程"
                stop_process "RKLLM 文本服务" "${RKLLM_PID_FILE}" 5 3 "llm_server.py.*--port ${RKLLM_PORT}"
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
    prepare_download_dir || exit 1

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
                --web-dir "${TEST_DIR}" \
                --download-dir "${DOWNLOAD_DIR}"
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

    if [[ "${ACTIVE_ACCESS_MODE}" == "wakeup" || "${ACTIVE_ACCESS_MODE}" == "hybrid" ]]; then
        section "启动板端唤醒词客户端"
        if is_running "${HEADLESS_VOICE_PID_FILE}"; then
            warn "板端唤醒客户端已在运行 (PID=$(cat ${HEADLESS_VOICE_PID_FILE}))"
        else
            start_detached "${HEADLESS_VOICE_LOG}" "${HEADLESS_VOICE_PID_FILE}" \
                python3 "${HEADLESS_VOICE_CLIENT}"
            sleep 2
            if is_running "${HEADLESS_VOICE_PID_FILE}"; then
                info "板端唤醒客户端已启动，等待唤醒词：你好小江"
            else
                error "板端唤醒客户端启动失败，查看日志：${HEADLESS_VOICE_LOG}"
                tail -20 "${HEADLESS_VOICE_LOG}" || true
                exit 1
            fi
        fi
    else
        info "web 模式不启动板端唤醒客户端"
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
        tail -f "${RKLLM_LOG}" "${VOICE_ASSISTANT_LOG}" "${WEB_HTTPS_LOG}" "${HEADLESS_VOICE_LOG}" 2>/dev/null || true
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
  start     启动文本、语音和 Web 服务（默认跟随日志）
  stop      停止全部服务
  restart   先停后启
  status    查看运行状态
  desktop   检查后台服务就绪后打开浏览器（不启动后端）

启动选项:
  qwen3.5 | qwen3.5-2b | 3b | 7b | qwen3-1.7b | qwen3-4b（默认）
  --vision      为 Qwen3.5 加载配套视觉编码器（纯文本模型不支持）
  --no-follow
  --open-browser

语音入口模式:
  在 data/.config.yaml 设置 voice_access.mode: web | wakeup | hybrid
  默认 web；wakeup/hybrid 会启动板端“你好小江”唤醒客户端

示例:
  $0 start
  $0 start qwen3-4b --no-follow
  $0 start qwen3-4b --vision       # 报错：纯文本模型不支持视觉
  $0 start qwen3.5 --vision --no-follow
  $0 start qwen3.5-2b --vision --no-follow
  $0 desktop
  $0 stop
EOF
}

CMD="${1:-}"
shift || true

case "${CMD}" in
    start)
        validate_start_request "$@"
        do_start "$@"
        ;;
    stop)
        do_stop
        ;;
    restart)
        validate_start_request "$@"
        do_stop
        do_start "$@"
        ;;
    status)
        do_status
        ;;
    desktop)
        # 桌面图标入口：只检查 systemd 服务，避免重复启动后端
        do_desktop
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
