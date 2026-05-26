#!/bin/bash
# ============================================================
# logs.sh - show live logs for RKLLM, voice-assistant, and Web gateway
# ============================================================

set -e

RKLLM_LOG="/tmp/rkllm_server.log"
VISION_LOG="/tmp/vision_server.log"
VOICE_ASSISTANT_LOG="/tmp/voice_assistant_server.log"
WEB_HTTPS_LOG="/tmp/web_https_server.log"

GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
info() { echo -e "${GREEN}[INFO ]${NC} $*"; }

for log_file in "${RKLLM_LOG}" "${VISION_LOG}" "${VOICE_ASSISTANT_LOG}" "${WEB_HTTPS_LOG}"; do
    [ -f "${log_file}" ] || : > "${log_file}" 2>/dev/null || true
done

info "正在显示实时日志（Ctrl+C 仅退出日志，服务不会停止）"
echo -e "${CYAN}────────────────────── 实时日志 ──────────────────────${NC}"

tail -f "${RKLLM_LOG}" "${VISION_LOG}" "${VOICE_ASSISTANT_LOG}" "${WEB_HTTPS_LOG}"

