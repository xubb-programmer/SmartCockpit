#!/usr/bin/env bash
# 为当前桌面用户安装语音助手自动启动入口和桌面图标。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTOSTART_SOURCE="${SCRIPT_DIR}/VoiceAssistantAutoStart.desktop"
DESKTOP_SOURCE="${SCRIPT_DIR}/VoiceAssistant.desktop"
AUTOSTART_DIR="${HOME}/.config/autostart"
AUTOSTART_TARGET="${AUTOSTART_DIR}/voice-assistant-autostart.desktop"

if [[ ! -f "${AUTOSTART_SOURCE}" || ! -f "${DESKTOP_SOURCE}" ]]; then
    printf '[ERROR] 桌面启动文件不完整，请检查 %s\n' "${SCRIPT_DIR}" >&2
    exit 1
fi

DESKTOP_DIR="${HOME}/Desktop"
if command -v xdg-user-dir >/dev/null 2>&1; then
    detected_dir="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
    if [[ -n "${detected_dir}" ]]; then
        DESKTOP_DIR="${detected_dir}"
    fi
fi

mkdir -p "${AUTOSTART_DIR}" "${DESKTOP_DIR}"
ln -sfn "${AUTOSTART_SOURCE}" "${AUTOSTART_TARGET}"
install -m 0755 "${DESKTOP_SOURCE}" "${DESKTOP_DIR}/VoiceAssistant.desktop"
chmod 0755 "${SCRIPT_DIR}/startup-notifier.sh" "$0"

if command -v gio >/dev/null 2>&1; then
    gio set "${DESKTOP_DIR}/VoiceAssistant.desktop" metadata::trusted true >/dev/null 2>&1 || true
fi

printf '[INFO ] 自动启动入口: %s -> %s\n' "${AUTOSTART_TARGET}" "${AUTOSTART_SOURCE}"
printf '[INFO ] 桌面图标: %s\n' "${DESKTOP_DIR}/VoiceAssistant.desktop"
printf '[INFO ] 下次登录桌面后将自动等待服务并打开网页。\n'
