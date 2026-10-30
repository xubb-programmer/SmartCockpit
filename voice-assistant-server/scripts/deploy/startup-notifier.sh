#!/usr/bin/env bash
# 桌面登录后等待语音服务就绪，播放本地提示音并打开 Web 页面。

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
AUDIO_DIR="${PROJECT_DIR}/assets/startup-audio"
WEB_URL="${VOICE_ASSISTANT_WEB_URL:-https://127.0.0.1:18007/?v=0903a}"
WAIT_SECONDS="${VOICE_ASSISTANT_STARTUP_TIMEOUT:-240}"
POLL_SECONDS="${VOICE_ASSISTANT_STARTUP_POLL_INTERVAL:-2}"
RECOVERY_SECONDS="${VOICE_ASSISTANT_RECOVERY_POLL_INTERVAL:-5}"
PREFER_INTERNAL_AUDIO="${VOICE_ASSISTANT_PREFER_INTERNAL_AUDIO:-1}"
PULSE_SINK_OVERRIDE="${VOICE_ASSISTANT_PULSE_SINK:-}"
ALSA_DEVICE_OVERRIDE="${VOICE_ASSISTANT_AUDIO_DEVICE:-}"
UNATTENDED_BROWSER="${VOICE_ASSISTANT_UNATTENDED_BROWSER:-1}"
BROWSER_OVERRIDE="${VOICE_ASSISTANT_BROWSER:-}"
CERT_FILE="${VOICE_ASSISTANT_CERT_FILE:-/opt/jw/.ssl/cert.pem}"
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp}"
LOCK_FILE="${RUNTIME_DIR}/voice-assistant-startup-notifier.lock"
VEHICLE_LOCK_FILE="${RUNTIME_DIR}/voice-assistant-vehicle-recovery.lock"
AUDIO_PLAYBACK_LOCK_FILE="${RUNTIME_DIR}/voice-assistant-announcement.lock"
AUDIO_LOCK_FILE="${RUNTIME_DIR}/voice-assistant-audio-route.lock"
STATUS_FILE="${RUNTIME_DIR}/voice-assistant-startup-status"
ACCESS_MODE_FILE="/tmp/voice_assistant_access_mode"

MODE="${1:---autostart}"

declare -A NAMES=(
    [rkllm]="大模型服务"
    [voice]="语音服务"
    [web]="网页访问"
    [headless]="唤醒服务"
    [vehicle_control]="车辆控制服务"
    [startup_check]="启动自检"
)
declare -A FAILURE_REASONS=()
LAST_FAILURE_DETAILS=""
VEHICLE_DIAGNOSTIC=""
LAST_VEHICLE_DIAGNOSTIC=""

log_info() {
    printf '[INFO ] %s\n' "$*"
    logger -t voice-assistant-startup -- "$*" 2>/dev/null || true
}

log_warn() {
    printf '[WARN ] %s\n' "$*" >&2
    logger -p user.warning -t voice-assistant-startup -- "$*" 2>/dev/null || true
}

show_help() {
    cat <<'EOF'
用法：
  startup-notifier.sh --autostart  等待服务就绪，提示状态并自动打开网页
  startup-notifier.sh --manual     桌面图标入口，立即检查并打开网页
  startup-notifier.sh --check      只输出当前状态
  startup-notifier.sh --help       显示帮助

音频兼容环境变量：
  VOICE_ASSISTANT_PREFER_INTERNAL_AUDIO=0  不自动优先内置扬声器
  VOICE_ASSISTANT_PULSE_SINK=<sink>        指定 PulseAudio 输出
  VOICE_ASSISTANT_AUDIO_DEVICE=<device>    指定 ALSA 输出

无人值守浏览器环境变量：
  VOICE_ASSISTANT_UNATTENDED_BROWSER=0     使用系统默认浏览器打开网页
  VOICE_ASSISTANT_BROWSER=<command>        指定 Chromium 可执行程序
  VOICE_ASSISTANT_CERT_FILE=<path>         指定 HTTPS 证书文件
EOF
}

vehicle_check_enabled() {
    systemctl is-enabled --quiet scout-monitor-1501.service 2>/dev/null
}

collect_core_missing() {
    local key reason result
    local -a args=()
    CORE_MISSING=()
    FAILURE_REASONS=()
    VEHICLE_DIAGNOSTIC=""
    vehicle_check_enabled && args+=(--vehicle-enabled)
    if ! result="$(python3 "${SCRIPT_DIR}/startup_health.py" --mode "$(access_mode)" "${args[@]}")"; then
        CORE_MISSING=(startup_check)
        FAILURE_REASONS[startup_check]="健康检查程序执行失败"
        return
    fi
    while IFS=$'\t' read -r key reason; do
        [[ -n "${key}" ]] || continue
        case "${key}" in
            vehicle_control) VEHICLE_DIAGNOSTIC="${reason}"; continue ;;
            rkllm|voice|web|headless) ;;
            *) key=startup_check; reason="健康检查返回未知状态" ;;
        esac
        CORE_MISSING+=("${key}")
        FAILURE_REASONS[${key}]="${reason}"
    done <<<"${result}"
}

access_mode() {
    local mode="${VOICE_ACCESS_MODE:-}"
    [[ -n "${mode}" ]] || mode="$(cat "${ACCESS_MODE_FILE}" 2>/dev/null || echo web)"
    case "${mode}" in web|wakeup|hybrid) printf '%s\n' "${mode}" ;; *) echo web ;; esac
}

failure_details() {
    local key output=""
    for key in "${CORE_MISSING[@]}"; do
        [[ -z "${output}" ]] || output+="；"
        output+="${NAMES[${key}]}: ${FAILURE_REASONS[${key}]}"
    done
    printf '%s' "${output}"
}

log_health_changes() {
    local details
    details="$(failure_details)"
    if [[ "${details}" != "${LAST_FAILURE_DETAILS}" ]]; then
        if [[ -n "${details}" ]]; then log_warn "启动自检未通过: ${details}"; fi
        LAST_FAILURE_DETAILS="${details}"
    fi
    if [[ "${VEHICLE_DIAGNOSTIC}" != "${LAST_VEHICLE_DIAGNOSTIC}" ]]; then
        if [[ -n "${VEHICLE_DIAGNOSTIC}" ]]; then
            log_warn "车辆控制独立状态: ${VEHICLE_DIAGNOSTIC}"
        fi
        LAST_VEHICLE_DIAGNOSTIC="${VEHICLE_DIAGNOSTIC}"
    fi
}

write_status() {
    local state="$1"
    local detail="${2:-}"
    printf 'state=%s\ndetail=%s\nupdated_at=%s\n' \
        "${state}" "${detail}" "$(date '+%Y-%m-%d %H:%M:%S')" > "${STATUS_FILE}"
}

notify_desktop() {
    local title="$1"
    local message="$2"
    if command -v notify-send >/dev/null 2>&1; then
        notify-send -u normal "${title}" "${message}" >/dev/null 2>&1 || true
    fi
}

enabled_value() {
    case "${1,,}" in
        1|true|yes|on)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

find_internal_pulse_sink() {
    local sinks sink

    if [[ -n "${PULSE_SINK_OVERRIDE}" ]]; then
        printf '%s\n' "${PULSE_SINK_OVERRIDE}"
        return 0
    fi
    command -v pactl >/dev/null 2>&1 || return 1

    sinks="$(pactl list short sinks 2>/dev/null)" || return 1
    sink="$(printf '%s\n' "${sinks}" | awk '
        BEGIN { IGNORECASE = 1 }
        $2 ~ /(es8388|es8323|rt5651|rt5640|rk809|acodec)/ {
            print $2
            exit
        }
    ')"
    if [[ -z "${sink}" ]]; then
        sink="$(printf '%s\n' "${sinks}" | awk '
            BEGIN { IGNORECASE = 1 }
            $2 ~ /^alsa_output[.]platform-/ &&
            $2 !~ /(hdmi|displayport|spdif|[.]dp[0-9_]*|dp[0-9]+-sound)/ {
                print $2
                exit
            }
        ')"
    fi

    [[ -n "${sink}" ]] || return 1
    printf '%s\n' "${sink}"
}

find_internal_alsa_device() {
    local device

    if [[ -n "${ALSA_DEVICE_OVERRIDE}" ]]; then
        printf '%s\n' "${ALSA_DEVICE_OVERRIDE}"
        return 0
    fi
    command -v aplay >/dev/null 2>&1 || return 1

    device="$(aplay -l 2>/dev/null | awk '
        BEGIN { IGNORECASE = 1 }
        /^card [0-9]+:/ &&
        $0 ~ /(es8388|es8323|rt5651|rt5640|rk809|acodec)/ &&
        $0 !~ /(hdmi|displayport|spdif)/ {
            gsub(":", "", $3)
            print "plughw:CARD=" $3 ",DEV=0"
            exit
        }
    ')"

    [[ -n "${device}" ]] || return 1
    printf '%s\n' "${device}"
}

current_pulse_sink() {
    command -v pactl >/dev/null 2>&1 || return 1
    LANG=C pactl info 2>/dev/null | sed -n 's/^Default Sink:[[:space:]]*//p' | head -n 1
}

is_display_audio_sink() {
    [[ "${1,,}" =~ (hdmi|displayport|spdif|(^|[._-])dp[0-9_-]) ]]
}

configure_preferred_output() {
    local sink current input_id should_switch=0

    if ! enabled_value "${PREFER_INTERNAL_AUDIO}" \
        && [[ -z "${PULSE_SINK_OVERRIDE}" ]]; then
        return 0
    fi
    sink="$(find_internal_pulse_sink)" || return 0
    current="$(current_pulse_sink || true)"

    if [[ -n "${PULSE_SINK_OVERRIDE}" || -z "${current}" ]] \
        || is_display_audio_sink "${current}"; then
        should_switch=1
    fi
    if [[ "${current}" == "${sink}" ]]; then
        should_switch=0
    fi
    (( should_switch == 1 )) || return 0

    if ! pactl set-default-sink "${sink}" >/dev/null 2>&1; then
        log_warn "无法将默认声音输出切换到内置扬声器: ${sink}"
        return 1
    fi

    while read -r input_id _; do
        [[ -n "${input_id}" ]] || continue
        pactl move-sink-input "${input_id}" "${sink}" >/dev/null 2>&1 || true
    done < <(pactl list short sink-inputs 2>/dev/null)
    log_info "声音输出已切换到内置扬声器: ${sink}"
}

start_audio_route_monitor() {
    local sink

    if ! enabled_value "${PREFER_INTERNAL_AUDIO}" \
        && [[ -z "${PULSE_SINK_OVERRIDE}" ]]; then
        return 0
    fi
    command -v pactl >/dev/null 2>&1 || return 0
    sink="$(find_internal_pulse_sink)" || return 0

    (
        exec 9>&-
        exec 7>"${AUDIO_LOCK_FILE}"
        flock -n 7 || exit 0
        while true; do
            while IFS= read -r event; do
                case "${event}" in
                    *" on sink "*|*" on card "*|*" on server "*)
                        sleep 1
                        configure_preferred_output || true
                        ;;
                esac
            done < <(LANG=C pactl subscribe 2>/dev/null)
            sleep 2
        done
    ) >/dev/null 2>&1 &
}

play_audio_file() (
    exec 6>"${AUDIO_PLAYBACK_LOCK_FILE}"
    flock -w 40 6 || return 1
    local file="$1"
    local path="${AUDIO_DIR}/${file}"
    local pulse_sink=""
    local audio_device=""
    if [[ ! -r "${path}" ]]; then
        log_warn "提示音不存在: ${path}"
        return 1
    fi

    if [[ -n "${PULSE_SINK_OVERRIDE}" ]] || enabled_value "${PREFER_INTERNAL_AUDIO}"; then
        pulse_sink="$(find_internal_pulse_sink || true)"
    fi
    if [[ -n "${pulse_sink}" ]] \
        && command -v paplay >/dev/null 2>&1 \
        && timeout 8s paplay --device="${pulse_sink}" "${path}" >/dev/null 2>&1; then
        return 0
    fi

    if [[ -n "${ALSA_DEVICE_OVERRIDE}" ]] || enabled_value "${PREFER_INTERNAL_AUDIO}"; then
        audio_device="$(find_internal_alsa_device || true)"
    fi
    if [[ -n "${audio_device}" ]] \
        && command -v aplay >/dev/null 2>&1 \
        && timeout 8s aplay -q -D "${audio_device}" "${path}" >/dev/null 2>&1; then
        return 0
    fi

    if command -v paplay >/dev/null 2>&1 \
        && timeout 8s paplay "${path}" >/dev/null 2>&1; then
        return 0
    fi
    if command -v aplay >/dev/null 2>&1 \
        && timeout 8s aplay -q "${path}" >/dev/null 2>&1; then
        return 0
    fi

    log_warn "无法播放提示音: ${path}"
    return 1
)

find_chromium_browser() {
    local candidate

    if [[ -n "${BROWSER_OVERRIDE}" ]]; then
        if [[ -x "${BROWSER_OVERRIDE}" ]]; then
            printf '%s\n' "${BROWSER_OVERRIDE}"
            return 0
        fi
        candidate="$(command -v "${BROWSER_OVERRIDE}" 2>/dev/null || true)"
        if [[ -n "${candidate}" ]]; then
            printf '%s\n' "${candidate}"
            return 0
        fi
        log_warn "指定的浏览器不可用: ${BROWSER_OVERRIDE}"
    fi

    for candidate in chromium chromium-browser google-chrome-stable google-chrome; do
        if command -v "${candidate}" >/dev/null 2>&1; then
            command -v "${candidate}"
            return 0
        fi
    done
    return 1
}

certificate_spki_hash() {
    local hash

    [[ -r "${CERT_FILE}" ]] || return 1
    command -v openssl >/dev/null 2>&1 || return 1
    command -v base64 >/dev/null 2>&1 || return 1

    hash="$(
        openssl x509 -in "${CERT_FILE}" -pubkey -noout 2>/dev/null \
            | openssl pkey -pubin -outform DER 2>/dev/null \
            | openssl dgst -sha256 -binary 2>/dev/null \
            | base64 \
            | tr -d '\r\n'
    )"
    [[ -n "${hash}" ]] || return 1
    printf '%s\n' "${hash}"
}

open_unattended_browser() {
    local browser spki
    local browser_log="/tmp/voice-assistant-browser.log"
    local -a flags=(
        --autoplay-policy=no-user-gesture-required
        --use-fake-ui-for-media-stream
        --no-first-run
        --no-default-browser-check
        --disable-session-crashed-bubble
        --password-store=basic
        --start-maximized
    )

    browser="$(find_chromium_browser)" || return 1
    spki="$(certificate_spki_hash || true)"
    if [[ -n "${spki}" ]]; then
        flags+=("--ignore-certificate-errors-spki-list=${spki}")
    elif [[ "${WEB_URL}" =~ ^https://(127[.]0[.]0[.]1|localhost)(:|/) ]]; then
        flags+=(--ignore-certificate-errors)
        log_warn "无法读取 HTTPS 证书公钥，已仅为本机语音助手浏览器启用证书兼容模式"
    fi

    log_info "使用 Chromium 无人值守模式打开网页: ${browser}"
    (
        exec 9>&-
        nohup "${browser}" "${flags[@]}" "${WEB_URL}" >>"${browser_log}" 2>&1 &
    )
}

open_browser() {
    log_info "打开浏览器: ${WEB_URL}"
    if enabled_value "${UNATTENDED_BROWSER}"; then
        if open_unattended_browser; then
            return 0
        fi
        log_warn "未找到 Chromium，无人值守麦克风和声音播放无法保证，回退到系统默认浏览器"
    fi

    if command -v xdg-open >/dev/null 2>&1; then
        (exec 9>&-; nohup xdg-open "${WEB_URL}" >/dev/null 2>&1 &)
    else
        notify_desktop "语音助手" "未找到浏览器，请手动访问 ${WEB_URL}"
        log_warn "未找到可用浏览器"
        return 1
    fi
}

print_current_status() {
    if [[ -n "${VEHICLE_DIAGNOSTIC}" ]]; then
        printf '车辆控制独立状态：%s。\n' "${VEHICLE_DIAGNOSTIC}" >&2
    fi
    if (( ${#CORE_MISSING[@]} == 0 )); then
        printf '语音服务运行正常。\n'
        return 0
    fi
    printf '语音服务运行异常：%s。\n' "$(failure_details)"
    return 1
}

core_ready() {
    local key
    for key in "${CORE_MISSING[@]}"; do
        [[ "${key}" == vehicle_control ]] || return 1
    done
    return 0
}

BROWSER_OPENED=0
open_browser_when_ready() {
    [[ "$(access_mode)" != wakeup ]] || return 0
    core_ready || return 0
    if (( BROWSER_OPENED == 0 )); then
        if open_browser; then BROWSER_OPENED=1; fi
    fi
}

do_manual() {
    collect_core_missing
    if core_ready; then
        configure_preferred_output || true
        start_audio_route_monitor
        open_browser_when_ready
        return 0
    fi
    notify_desktop "语音服务运行异常" "服务暂不可用，请稍后重试。"
    log_warn "桌面打开请求未执行: $(failure_details)"
    return 1
}

announce_ready() {
    local state="${1:-ready}"
    configure_preferred_output || true
    start_audio_route_monitor
    if [[ "${state}" == recovered ]]; then
        write_status recovered "语音服务已恢复"
        notify_desktop "语音服务已恢复" "语音服务已恢复。"
        play_audio_file "startup-recovered.wav" || true
    else
        write_status ready "语音服务运行正常"
        notify_desktop "语音服务运行正常" "语音服务运行正常。"
        play_audio_file "startup-ok.wav" || true
    fi
    open_browser_when_ready
}

vehicle_port_ready() {
    ss -H -ltn 2>/dev/null | grep -Eq ':9000[[:space:]]'
}

start_vehicle_recovery_monitor() {
    vehicle_check_enabled || return 0
    (
        exec 9>&-
        exec 8>"${VEHICLE_LOCK_FILE}"
        flock -n 8 || exit 0
        local status_file="${RUNTIME_DIR}/voice-assistant-vehicle-status"
        local healthy_samples=0
        if vehicle_port_ready; then
            printf 'state=ready\n' > "${status_file}"
            return 0
        fi
        printf 'state=failed\ndetail=车辆控制服务入口未监听\n' > "${status_file}"
        log_warn "车辆控制服务未连接；仅观察恢复，不重启或修改服务"
        notify_desktop "车辆控制服务未连接" "语音服务状态独立检查。"
        play_audio_file "vehicle-control-unavailable.wav" || true
        while (( healthy_samples < 2 )); do
            sleep "${RECOVERY_SECONDS}"
            if vehicle_port_ready; then
                healthy_samples=$((healthy_samples + 1))
            else
                healthy_samples=0
            fi
        done
        printf 'state=recovered\n' > "${status_file}"
        log_info "车辆控制服务已恢复"
        notify_desktop "车辆控制服务已恢复" "车辆控制服务已恢复。"
        play_audio_file "vehicle-control-recovered.wav" || true
    ) &
}

do_autostart() {
    local deadline=$((SECONDS + WAIT_SECONDS))
    local healthy_samples=0

    write_status "starting" "正在等待语音服务"
    log_info "开始等待语音服务，最长 ${WAIT_SECONDS} 秒"

    while true; do
        collect_core_missing
        log_health_changes
        if (( ${#CORE_MISSING[@]} == 0 )); then
            log_info "语音服务已就绪"
            announce_ready
            start_vehicle_recovery_monitor
            return 0
        fi

        if systemctl is-failed --quiet voice-assistant.service 2>/dev/null; then
            log_warn "voice-assistant.service 已进入失败状态"
            break
        fi
        if (( SECONDS >= deadline )); then
            log_warn "等待语音服务超过 ${WAIT_SECONDS} 秒"
            break
        fi
        sleep "${POLL_SECONDS}"
    done

    write_status failed "$(failure_details)"
    notify_desktop "语音服务运行异常" "系统正在等待服务恢复。"
    configure_preferred_output || true
    start_audio_route_monitor
    play_audio_file "startup-error.wav" || true
    start_vehicle_recovery_monitor
    open_browser_when_ready

    while true; do
        sleep "${RECOVERY_SECONDS}"
        collect_core_missing
        log_health_changes
        if (( ${#CORE_MISSING[@]} == 0 )); then
            healthy_samples=$((healthy_samples + 1))
            # Two successful observations avoid a short port/connection flap.
            if (( healthy_samples >= 2 )); then
                log_info "故障服务已恢复"
                announce_ready recovered
                return 0
            fi
        else
            healthy_samples=0
            write_status failed "$(failure_details)"
            open_browser_when_ready
        fi
    done
}

# Allow the functions to be exercised without starting desktop/audio processes.
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then return 0; fi

case "${MODE}" in
    --autostart|--manual|--check)
        ;;
    --help|-h)
        show_help
        exit 0
        ;;
    *)
        printf '未知参数: %s\n' "${MODE}" >&2
        show_help >&2
        exit 2
        ;;
esac

mkdir -p "${RUNTIME_DIR}" 2>/dev/null || true
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    if [[ "${MODE}" == "--manual" ]]; then
        notify_desktop "语音助手正在启动" "自动启动检查正在运行，服务就绪后页面会自动打开。"
    fi
    log_warn "已有桌面启动检查正在运行"
    exit 0
fi

collect_core_missing
case "${MODE}" in
    --autostart)
        do_autostart
        ;;
    --manual)
        do_manual
        ;;
    --check)
        print_current_status
        ;;
esac
