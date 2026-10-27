#!/usr/bin/env bash

# SmartCockpit RK3588 interactive installer.
# Run this script as a normal login user after copying the complete migration kit.

set -Eeuo pipefail

# Debian 普通用户的 PATH 可能不包含系统管理命令目录。
export PATH="${PATH}:/usr/sbin:/sbin"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_NAME="smartcockpit-rk3588-install.tar.gz"
PACKAGE_FILE="${SCRIPT_DIR}/${PACKAGE_NAME}"
PACKAGE_SHA256="ba2fe0797ac5f3193884ad02f21659f3db5d04f204bbbbbe9138c83c1da31353"
KIT_ROOT=''
WORK_ROOT=''
INSTALL_ROOT="/opt/jw"
LOG_DIR="${SCRIPT_DIR}/install-logs"
STAMP="$(date +%Y%m%d-%H%M%S)"
CURRENT_STEP="初始化"
LOG_FILE=''

cleanup() {
    if [[ -n "${WORK_ROOT}" && -d "${WORK_ROOT}" ]]; then
        rm -rf -- "${WORK_ROOT}"
    fi
}
trap cleanup EXIT

if [[ -t 1 ]]; then
    C_RESET='\033[0m'
    C_GREEN='\033[32m'
    C_YELLOW='\033[33m'
    C_RED='\033[31m'
    C_BLUE='\033[36m'
else
    C_RESET=''
    C_GREEN=''
    C_YELLOW=''
    C_RED=''
    C_BLUE=''
fi

info() { printf "${C_BLUE}[信息]${C_RESET} %s\n" "$*"; }
pass() { printf "${C_GREEN}[通过]${C_RESET} %s\n" "$*"; }
warn() { printf "${C_YELLOW}[警告]${C_RESET} %s\n" "$*"; }
fail() { printf "${C_RED}[失败]${C_RESET} %s\n" "$*" >&2; }

print_help() {
    cat <<'EOF'
SmartCockpit RK3588 一键交互安装脚本

用法:
  chmod +x install-smartcockpit-rk3588.sh
  ./install-smartcockpit-rk3588.sh

脚本会交互选择：
  1. 全新安装或完整替换
  2. 仅更新语音服务与 RKLLM 源码
  3. 只检查当前安装

脚本同目录必须存在 smartcockpit-rk3588-install.tar.gz。
EOF
}

print_diagnostics() {
    set +e
    echo
    fail "步骤“${CURRENT_STEP}”执行失败。"
    info "完整安装日志: ${LOG_FILE}"
    echo
    echo '建议先执行以下命令定位：'
    echo '  sudo systemctl status voice-assistant.service --no-pager -l'
    echo '  sudo journalctl -u voice-assistant.service -b --no-pager -n 150'
    echo '  tail -100 /tmp/rkllm_server.log'
    echo '  tail -100 /tmp/voice_assistant_server.log'
    echo '  tail -100 /tmp/web_https_server.log'
    echo "  bash ${KIT_ROOT}/scripts/verify.sh --user ${TARGET_USER:-当前用户}"
    echo
    echo '常见原因：材料不完整、SHA-256 校验失败、磁盘空间不足、NPU 驱动不兼容、'
    echo '目标用户不存在、sudo 权限不足、声卡不可用或端口已被其他进程占用。'
}

on_error() {
    local exit_code="$1"
    local line_no="$2"
    local command="$3"
    trap - ERR
    fail "命令失败，退出码=${exit_code}，行号=${line_no}"
    fail "失败命令: ${command}"
    print_diagnostics
    exit "${exit_code}"
}

trap 'on_error "$?" "$LINENO" "$BASH_COMMAND"' ERR

ask_yes_no() {
    local prompt="$1"
    local default_answer="${2:-yes}"
    local suffix='[Y/n]'
    local answer
    [[ "${default_answer}" == 'no' ]] && suffix='[y/N]'

    while true; do
        read -r -p "${prompt} ${suffix} " answer </dev/tty || return 1
        answer="${answer:-${default_answer}}"
        case "${answer,,}" in
            y|yes|是) return 0 ;;
            n|no|否) return 1 ;;
            *) warn '请输入 y 或 n。' ;;
        esac
    done
}

ask_value() {
    local prompt="$1"
    local default_value="$2"
    local value
    read -r -p "${prompt} [${default_value}]: " value </dev/tty
    printf '%s' "${value:-${default_value}}"
}

require_command() {
    local command="$1"
    if command -v "${command}" >/dev/null 2>&1; then
        pass "命令可用: ${command}"
    else
        fail "缺少命令: ${command}"
        MISSING_COMMANDS+=("${command}")
    fi
}

check_required_file() {
    local relative_path="$1"
    if [[ -f "${KIT_ROOT}/${relative_path}" ]]; then
        pass "材料存在: ${relative_path}"
    else
        fail "缺少材料: ${relative_path}"
        MISSING_FILES+=("${relative_path}")
    fi
}

run_as_target_user() {
    if [[ "$(id -u)" -eq 0 ]]; then
        runuser -u "${TARGET_USER}" -- env \
            HOME="${TARGET_HOME}" USER="${TARGET_USER}" \
            bash "$@"
    else
        sudo -u "${TARGET_USER}" env \
            HOME="${TARGET_HOME}" USER="${TARGET_USER}" \
            bash "$@"
    fi
}

if [[ "${1:-}" == '-h' || "${1:-}" == '--help' ]]; then
    print_help
    exit 0
fi
if [[ $# -gt 0 ]]; then
    fail "未知参数: $*"
    print_help
    exit 2
fi
if [[ ! -r /dev/tty ]]; then
    fail '当前没有交互终端，请通过 SSH 或板端终端运行本脚本。'
    exit 1
fi

mkdir -p "${LOG_DIR}" 2>/dev/null || LOG_DIR="/tmp"
LOG_FILE="${LOG_DIR}/rk3588-install-${STAMP}.log"
exec > >(tee -a "${LOG_FILE}") 2>&1

for bootstrap_command in tar gzip sha256sum mktemp awk df; do
    if ! command -v "${bootstrap_command}" >/dev/null 2>&1; then
        fail "解包前缺少必要命令: ${bootstrap_command}"
        echo 'Debian/Ubuntu 可执行：sudo apt-get install -y tar gzip coreutils'
        exit 1
    fi
done
if [[ ! -f "${PACKAGE_FILE}" ]]; then
    fail "缺少离线部署包: ${PACKAGE_FILE}"
    fail "请把 ${PACKAGE_NAME}、install-smartcockpit-rk3588.sh 和 README-INSTALL.md 放在同一目录。"
    exit 1
fi
if [[ "${PACKAGE_SHA256}" == 'TO_BE_FILLED_AFTER_PACKAGE_BUILD' ]]; then
    fail '安装脚本中的离线包校验值尚未生成，请使用正式发布材料。'
    exit 1
fi

CURRENT_STEP='校验离线部署包'
info '正在校验离线部署包 SHA-256，模型较大，可能需要几分钟。'
ACTUAL_PACKAGE_SHA256="$(sha256sum "${PACKAGE_FILE}" | awk '{print $1}')"
if [[ "${ACTUAL_PACKAGE_SHA256}" != "${PACKAGE_SHA256}" ]]; then
    fail '离线部署包 SHA-256 不匹配，文件可能复制不完整或已损坏。'
    echo "期望: ${PACKAGE_SHA256}"
    echo "实际: ${ACTUAL_PACKAGE_SHA256}"
    exit 1
fi
pass '离线部署包 SHA-256 校验通过。'

echo
echo '请选择操作模式：'
echo '  1) 全新安装 / 完整替换（环境、模型、源码、服务）'
echo '  2) 仅更新源码（保留 Conda 环境、模型和现场配置）'
echo '  3) 只检查当前安装，不修改任何内容'
while true; do
    MODE="$(ask_value '请输入序号' '1')"
    case "${MODE}" in
        1) MODE_NAME='完整安装'; break ;;
        2) MODE_NAME='仅更新源码'; break ;;
        3) MODE_NAME='只检查'; break ;;
        *) warn '请输入 1、2 或 3。' ;;
    esac
done

TEMP_ROOT="${TMPDIR:-/tmp}"
if [[ ! -d "${TEMP_ROOT}" || ! -w "${TEMP_ROOT}" ]]; then
    fail "临时目录不存在或不可写: ${TEMP_ROOT}"
    exit 1
fi
TEMP_FREE_KIB="$(df -Pk "${TEMP_ROOT}" | awk 'NR==2 {print $4}')"
TEMP_REQUIRED_GIB=1
[[ "${MODE}" == '1' ]] && TEMP_REQUIRED_GIB=8
if (( TEMP_FREE_KIB < TEMP_REQUIRED_GIB * 1024 * 1024 )); then
    fail "${TEMP_ROOT} 所在分区空间不足，解包至少需要 ${TEMP_REQUIRED_GIB} GiB。"
    exit 1
fi

CURRENT_STEP='解压离线部署包'
WORK_ROOT="$(mktemp -d "${TEMP_ROOT%/}/smartcockpit-rk3588-installer.XXXXXX")"
case "${MODE}" in
    1)
        tar -xzf "${PACKAGE_FILE}" -C "${WORK_ROOT}"
        ;;
    2)
        tar -xzf "${PACKAGE_FILE}" -C "${WORK_ROOT}" \
            payload/source scripts templates \
            manifests/installed-payload.sha256 CHECKSUMS.sha256
        ;;
    3)
        tar -xzf "${PACKAGE_FILE}" -C "${WORK_ROOT}" \
            scripts/verify.sh manifests/installed-payload.sha256
        ;;
esac
KIT_ROOT="${WORK_ROOT}"
REQUIRED_DIRS=(scripts manifests)
[[ "${MODE}" != '3' ]] && REQUIRED_DIRS+=(payload templates)
for required_dir in "${REQUIRED_DIRS[@]}"; do
    if [[ ! -d "${KIT_ROOT}/${required_dir}" ]]; then
        fail "离线部署包内部缺少目录: ${required_dir}"
        exit 1
    fi
done
pass "离线部署包已解压到临时目录: ${WORK_ROOT}"

echo '============================================================'
echo '       SmartCockpit RK3588 一键交互安装程序'
echo '============================================================'
info "安装日志将保存到: ${LOG_FILE}"
info '脚本不会直接删除旧安装；选择替换时会先创建带时间戳的备份。'
echo

CURRENT_STEP='获取 sudo 权限'
if [[ "$(id -u)" -eq 0 ]]; then
    SUDO=()
    warn '当前以 root 身份运行，请仔细确认目标运行用户。'
else
    command -v sudo >/dev/null 2>&1 || {
        fail '系统未安装 sudo，请切换到 root 后重新运行。'
        exit 1
    }
    info '正在验证 sudo 权限，系统可能要求输入当前登录用户密码。'
    sudo -v
    SUDO=(sudo)
fi

DEFAULT_USER="${SUDO_USER:-${USER:-}}"
if [[ -z "${DEFAULT_USER}" || "${DEFAULT_USER}" == 'root' ]]; then
    DEFAULT_USER="$(getent passwd 1000 2>/dev/null | cut -d: -f1 || true)"
fi
DEFAULT_USER="${DEFAULT_USER:-jw}"
TARGET_USER="$(ask_value '请输入语音服务运行用户名' "${DEFAULT_USER}")"
if [[ ! "${TARGET_USER}" =~ ^[a-z_][a-z0-9_-]*$ ]]; then
    fail "用户名格式不合法: ${TARGET_USER}"
    exit 1
fi

if ! id "${TARGET_USER}" >/dev/null 2>&1; then
    warn "目标用户不存在: ${TARGET_USER}"
    if ask_yes_no '是否现在创建该用户并设置密码？' yes; then
        "${SUDO[@]}" useradd -m -s /bin/bash "${TARGET_USER}"
        info "请为 ${TARGET_USER} 设置登录密码。"
        "${SUDO[@]}" passwd "${TARGET_USER}"
    else
        fail '没有可用的目标用户，安装已取消。'
        exit 1
    fi
fi
TARGET_HOME="$(getent passwd "${TARGET_USER}" | cut -d: -f6)"

CURRENT_STEP='环境预检'
echo
echo '===== 硬件与系统环境检查 ====='
ARCH="$(uname -m)"
if [[ "${ARCH}" == 'aarch64' || "${ARCH}" == 'arm64' ]]; then
    pass "CPU 架构: ${ARCH}"
else
    fail "CPU 架构为 ${ARCH}，本安装包只支持 RK3588/AArch64。"
    exit 1
fi

if [[ -r /proc/device-tree/compatible ]]; then
    SOC_COMPATIBLE="$(tr '\0' ' ' </proc/device-tree/compatible)"
else
    SOC_COMPATIBLE='未知'
fi
if [[ "${SOC_COMPATIBLE,,}" == *rk3588* ]]; then
    pass "SoC: 检测到 RK3588 (${SOC_COMPATIBLE})"
elif [[ -e /dev/rknpu || -e /dev/rknpu0 ]]; then
    warn "未从设备树确认 RK3588，但检测到 NPU 设备节点。"
    ask_yes_no '确认这是 RK3588，继续安装吗？' no || exit 1
else
    fail '未检测到 RK3588 或 RKNPU 设备节点。'
    exit 1
fi

if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    source /etc/os-release
    pass "操作系统: ${PRETTY_NAME:-未知}"
else
    warn '无法读取操作系统版本。'
fi
pass "内核版本: $(uname -r)"
if [[ -r /sys/module/rknpu/version ]]; then
    RKNPU_VERSION="$(tr -d '[:space:]' </sys/module/rknpu/version)"
    [[ "${RKNPU_VERSION}" == '0.9.8' ]] \
        && pass "RKNPU 驱动: ${RKNPU_VERSION}（已验证基线）" \
        || warn "RKNPU 驱动: ${RKNPU_VERSION}；已验证基线为 0.9.8，安装后必须实测模型。"
else
    warn '无法读取 RKNPU 驱动版本，安装后必须检查 RKLLM 日志。'
fi

BOARD_IPS="$(hostname -I 2>/dev/null | xargs || true)"
[[ -n "${BOARD_IPS}" ]] && pass "当前 IP: ${BOARD_IPS}" || warn '当前未获得局域网 IP。'
info "当前板端时间: $(date '+%Y-%m-%d %H:%M:%S %Z')"
CURRENT_YEAR="$(date +%Y)"
if (( CURRENT_YEAR < 2025 )); then
    warn '系统时间明显不正确，可能导致 HTTPS 证书和日志时间异常。'
    if ask_yes_no '是否现在手动设置时间？' yes; then
        NEW_TIME="$(ask_value '请输入时间，格式 YYYY-MM-DD HH:MM:SS' '2026-01-01 00:00:00')"
        "${SUDO[@]}" date -s "${NEW_TIME}"
        "${SUDO[@]}" hwclock --systohc --utc 2>/dev/null || warn '硬件 RTC 不可用，系统时间已设置但断电后可能丢失。'
    fi
fi

MISSING_COMMANDS=()
for command in bash tar gzip bzip2 openssl ip ss ps pgrep sha256sum sed awk grep find systemctl getent install runuser; do
    require_command "${command}"
done
if [[ ${#MISSING_COMMANDS[@]} -gt 0 ]]; then
    fail "缺少必要命令: ${MISSING_COMMANDS[*]}"
    echo 'Debian/Ubuntu 可尝试：'
    echo '  sudo apt-get update'
    echo '  sudo apt-get install -y bash tar gzip bzip2 openssl iproute2 procps coreutils sed gawk grep findutils systemd passwd'
    exit 1
fi

CHECK_PATH='/opt'
[[ -d "${CHECK_PATH}" ]] || CHECK_PATH='/'
FREE_KIB="$(df -Pk "${CHECK_PATH}" | awk 'NR==2 {print $4}')"
FREE_GIB=$((FREE_KIB / 1024 / 1024))
REQUIRED_GIB=1
[[ "${MODE}" == '1' ]] && REQUIRED_GIB=15
if (( FREE_GIB >= REQUIRED_GIB )); then
    pass "可用磁盘空间: ${FREE_GIB} GiB"
else
    warn "可用空间仅 ${FREE_GIB} GiB，建议至少 ${REQUIRED_GIB} GiB。"
    ask_yes_no '仍要继续吗？' no || exit 1
fi

if command -v aplay >/dev/null 2>&1; then
    aplay -l >/dev/null 2>&1 && pass '检测到扬声器/声卡输出设备' || warn '未检测到 ALSA 播放设备。'
else
    warn '未安装 aplay，暂时无法检查扬声器。'
fi
if command -v arecord >/dev/null 2>&1; then
    arecord -l >/dev/null 2>&1 && pass '检测到麦克风/声卡输入设备' || warn '未检测到 ALSA 录音设备。'
else
    warn '未安装 arecord，暂时无法检查麦克风。'
fi

MISSING_FILES=()
check_required_file 'scripts/verify.sh'
if [[ "${MODE}" != '3' ]]; then
    check_required_file 'scripts/install.sh'
    check_required_file 'templates/voice-assistant.service.in'
fi
if [[ "${MODE}" == '1' ]]; then
    check_required_file 'scripts/preflight.sh'
    check_required_file 'CHECKSUMS.sha256'
    check_required_file 'payload/Miniconda3-py310_26.7.1-1-Linux-aarch64.sh'
    check_required_file 'payload/voice-assistant-env.tar.gz'
    check_required_file 'payload/source/voice-assistant-server.tar.gz'
    check_required_file 'payload/source/rknn-server.tar.gz'
    check_required_file 'payload/models/asr/sherpa-onnx-conformer-zh-stateless2-2023-05-23.tar.bz2'
    check_required_file 'payload/models/qwen3-4b-instruct_w8a8.rkllm'
    check_required_file 'payload/models/voice_assistant_static_prompt_novision.txt'
    check_required_file 'payload/models/voice_assistant_system_prompt_qwen3-4b-instruct_novision.cache'
elif [[ "${MODE}" == '2' ]]; then
    check_required_file 'CHECKSUMS.sha256'
    check_required_file 'payload/source/voice-assistant-server.tar.gz'
    check_required_file 'payload/source/rknn-server.tar.gz'
fi
if [[ ${#MISSING_FILES[@]} -gt 0 ]]; then
    fail "迁移包不完整，共缺少 ${#MISSING_FILES[@]} 项。"
    fail '离线部署包内部材料不完整，请重新获取正式发布包。'
    exit 1
fi

WITH_VEHICLE=false
REPLACE=false
ENABLE_SERVICE=true
START_SERVICE=true
DESKTOP_AUTOSTART=false

if [[ "${MODE}" == '1' && -f "${KIT_ROOT}/payload/optional/vehicle-control-server.zip" ]]; then
    ask_yes_no '是否同时安装车辆控制服务？' no && WITH_VEHICLE=true
fi

if [[ "${MODE}" == '1' ]]; then
    EXISTING_PATHS=()
    for path in \
        "${INSTALL_ROOT}/voice-assistant-server" \
        "${INSTALL_ROOT}/rknn-server" \
        "${INSTALL_ROOT}/models" \
        "${TARGET_HOME}/miniconda3/envs/voice-assistant-server"; do
        [[ -e "${path}" ]] && EXISTING_PATHS+=("${path}")
    done
    if [[ ${#EXISTING_PATHS[@]} -gt 0 ]]; then
        warn '检测到已有安装：'
        printf '  %s\n' "${EXISTING_PATHS[@]}"
        info '替换时旧目录会移动到 /opt/jw/migration-backup-时间戳，不会直接删除。'
        ask_yes_no '是否备份并替换已有安装？' no && REPLACE=true || exit 1
    fi
elif [[ "${MODE}" == '2' ]]; then
    for path in \
        "${INSTALL_ROOT}/voice-assistant-server" \
        "${INSTALL_ROOT}/rknn-server" \
        "${TARGET_HOME}/miniconda3/envs/voice-assistant-server"; do
        if [[ ! -e "${path}" ]]; then
            fail "仅更新源码要求已有完整安装，缺少: ${path}"
            exit 1
        fi
    done
    info '源码更新会备份旧源码，并保留 data/.config.yaml、MCP 配置和 OTA 文件。'
    ask_yes_no '确认开始源码更新？' yes || exit 0
    REPLACE=true
fi

if [[ "${MODE}" != '3' ]]; then
    ask_yes_no '是否设置语音服务开机自启？' yes || ENABLE_SERVICE=false
    ask_yes_no '安装完成后是否立即启动语音服务？' yes || START_SERVICE=false
    if [[ -n "${DISPLAY:-}" || -d "${TARGET_HOME}/Desktop" || -d "${TARGET_HOME}/桌面" ]]; then
        ask_yes_no '是否安装桌面图标，并在桌面登录后自动打开 Web 页面？' yes && DESKTOP_AUTOSTART=true
    else
        warn '未确认图形桌面环境；远程 Windows 浏览器访问不依赖板端桌面。'
        ask_yes_no '仍要安装桌面图标和登录自启动入口吗？' no && DESKTOP_AUTOSTART=true
    fi
fi

echo
echo '===== 安装确认 ====='
echo "操作模式       : ${MODE_NAME}"
echo "目标用户       : ${TARGET_USER}"
echo "用户目录       : ${TARGET_HOME}"
echo "安装目录       : ${INSTALL_ROOT}"
echo "车辆控制       : ${WITH_VEHICLE}"
echo "替换已有安装   : ${REPLACE}"
echo "开机自启       : ${ENABLE_SERVICE}"
echo "立即启动       : ${START_SERVICE}"
echo "桌面入口       : ${DESKTOP_AUTOSTART}"
echo "日志文件       : ${LOG_FILE}"
echo
ask_yes_no '确认执行以上操作吗？' yes || {
    info '用户取消安装，未修改系统。'
    exit 0
}

if [[ "${MODE}" == '3' ]]; then
    CURRENT_STEP='检查当前安装'
    bash "${KIT_ROOT}/scripts/verify.sh" --user "${TARGET_USER}"
    pass '当前安装检查完成。'
    exit 0
fi

if [[ "${MODE}" == '1' ]]; then
    CURRENT_STEP='校验完整迁移材料'
    info '正在执行完整预检和 SHA-256 校验；4B 模型较大，请耐心等待。'
    bash "${KIT_ROOT}/scripts/preflight.sh"
else
    CURRENT_STEP='校验源码更新材料'
    info '正在校验两个源码归档。'
    (
        cd "${KIT_ROOT}"
        grep -E '  payload/source/(voice-assistant-server|rknn-server)\.tar\.gz$' \
            CHECKSUMS.sha256 | sha256sum -c -
    )
fi

CURRENT_STEP='安装软件与服务'
INSTALL_ARGS=(--user "${TARGET_USER}")
[[ "${WITH_VEHICLE}" == true ]] && INSTALL_ARGS+=(--with-vehicle-control)
[[ "${ENABLE_SERVICE}" == true ]] && INSTALL_ARGS+=(--enable)
[[ "${START_SERVICE}" == true ]] && INSTALL_ARGS+=(--start)
[[ "${REPLACE}" == true ]] && INSTALL_ARGS+=(--replace)
[[ "${MODE}" == '2' ]] && INSTALL_ARGS+=(--source-only)
"${SUDO[@]}" bash "${KIT_ROOT}/scripts/install.sh" "${INSTALL_ARGS[@]}"

if [[ "${DESKTOP_AUTOSTART}" == true ]]; then
    CURRENT_STEP='安装桌面入口'
    DESKTOP_SETUP="${INSTALL_ROOT}/voice-assistant-server/scripts/deploy/setup-desktop-autostart.sh"
    if [[ -f "${DESKTOP_SETUP}" ]]; then
        run_as_target_user "${DESKTOP_SETUP}"
    else
        warn "当前源码不包含桌面入口脚本: ${DESKTOP_SETUP}"
    fi
fi

CURRENT_STEP='安装后验收'
bash "${KIT_ROOT}/scripts/verify.sh" --user "${TARGET_USER}"

PRIMARY_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
echo
echo '============================================================'
pass 'SmartCockpit 语音服务安装并验收完成。'
echo '============================================================'
[[ -n "${PRIMARY_IP}" ]] && echo "远程 Web 地址 : https://${PRIMARY_IP}:18007/"
echo "服务状态       : sudo systemctl status voice-assistant.service --no-pager"
echo "实时日志       : ${INSTALL_ROOT}/voice-assistant-server/scripts/deploy/logs.sh"
echo "本次安装日志   : ${LOG_FILE}"
if [[ "${WITH_VEHICLE}" == true ]]; then
    echo '车辆服务状态   : sudo systemctl status scout-monitor-1501.service --no-pager'
fi
echo
info '首次通过浏览器访问 HTTPS 时，需要接受目标板新生成的自签名证书。'
info '建议重启目标板一次，再确认服务、麦克风、扬声器、视频和车辆控制。'
