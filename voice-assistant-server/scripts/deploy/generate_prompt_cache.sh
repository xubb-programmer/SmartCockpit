#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
# 离线生成 RKLLM Prompt Cache
# 用途：一次性 prefill 静态系统提示词并保存 KV 缓存到磁盘。
#       flask_server.py 启动时自动加载，Turn 1 延迟从 ~4s 降至 ~0.5s。
# 用法：./generate_prompt_cache.sh [模型参数] [--vision] [--force]
#       --force : 强制重新生成（即使 cache 已存在）
#       --vision: 生成视觉模式提示词（仅 Qwen3.5 支持）
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVER_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ── 配置（与 voice-assistant.sh 保持一致）─────────────────────────
# 自动推导部署根目录：脚本在 voice-assistant-server/scripts/deploy/ 下
HOME_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
MODEL_DIR="${HOME_DIR}/models"
RKLLM_SERVER_DIR="${HOME_DIR}/rknn-server"
PLATFORM="rk3588"

# ── 颜色 ──────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO ]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN ]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ── 参数解析 ──────────────────────────────────────────────────────
FORCE=false
MODEL_SIZE="${VOICE_ASSISTANT_MODEL:-qwen3-4b}"
ENABLE_VISION=false
for arg in "$@"; do
    case "$arg" in
        --force)     FORCE=true ;;
        --vision)    ENABLE_VISION=true ;;
        qwen3.5|qwen35|qwen3.5-0.8b|qwen35-0.8b) MODEL_SIZE="qwen3.5-0.8b" ;;
        qwen3.5-2b|qwen35-2b) MODEL_SIZE="qwen3.5-2b" ;;
        3b)          MODEL_SIZE="3b" ;;
        7b)          MODEL_SIZE="7b" ;;
        qwen3-1.7b)  MODEL_SIZE="qwen3-1.7b" ;;
        qwen3-4b)    MODEL_SIZE="qwen3-4b" ;;
    esac
done

if [[ "${ENABLE_VISION}" == "true" && "${MODEL_SIZE}" != qwen3.5-* ]]; then
    error "模型 ${MODEL_SIZE} 是纯文本模型，不支持 --vision"
    exit 1
fi

case "${MODEL_SIZE}" in
  qwen3.5-0.8b) MODEL_PATH="${MODEL_DIR}/Qwen3.5-0.8B_w8a8_rk3588.rkllm"; MODEL_CACHE_KEY="qwen3.5-0.8b"; MAX_CTX=4096 ;;
  qwen3.5-2b)   MODEL_PATH="${MODEL_DIR}/qwen3.5-2b_w8a8_rk3588.rkllm";  MODEL_CACHE_KEY="qwen3.5-2b";   MAX_CTX=4096 ;;
  3b)           MODEL_PATH="${MODEL_DIR}/qwen2.5-3b_w8a8.rkllm";        MODEL_CACHE_KEY="qwen2.5-3b"; MAX_CTX=4096 ;;
  7b)           MODEL_PATH="${MODEL_DIR}/qwen2.5-7b_w8a8.rkllm";        MODEL_CACHE_KEY="qwen2.5-7b"; MAX_CTX=4096 ;;
  qwen3-1.7b)   MODEL_PATH="${MODEL_DIR}/qwen3-1.7b_w8a8.rkllm";       MODEL_CACHE_KEY="qwen3-1.7b"; MAX_CTX=4096 ;;
  qwen3-4b|*)   MODEL_PATH="${MODEL_DIR}/qwen3-4b-instruct_w8a8.rkllm"; MODEL_CACHE_KEY="qwen3-4b-instruct"; MAX_CTX=4096 ;;
esac

PROMPT_CACHE_SUPPORTED=true
MULTIMODAL_ARGS=()
if [[ "${MODEL_SIZE}" == qwen3.5-* ]]; then
    PROMPT_CACHE_SUPPORTED=false
fi
PROMPT_MODE="novision"
PROMPT_MODE_ARGS=()
if [[ "${ENABLE_VISION}" == "true" ]]; then
    PROMPT_MODE="vision"
    PROMPT_MODE_ARGS=(--vision)
fi
CACHE_OUTPUT="${MODEL_DIR}/voice_assistant_system_prompt_${MODEL_CACHE_KEY}_${PROMPT_MODE}.cache"

if [[ "${PROMPT_CACHE_SUPPORTED}" == "true" && -f "$CACHE_OUTPUT" && "$FORCE" != true ]]; then
    info "Prompt cache 已存在: ${CACHE_OUTPUT}"
    info "大小: $(du -h "$CACHE_OUTPUT" | cut -f1)"
    info "如需重新生成，请运行: $0 --force"
    exit 0
fi

# ── 检查模型文件 ──────────────────────────────────────────────────
if [[ ! -f "$MODEL_PATH" ]]; then
    error "模型文件不存在: ${MODEL_PATH}"
    exit 1
fi

# ── 检查 generate_prompt_cache.py ─────────────────────────────────
GEN_SCRIPT="${RKLLM_SERVER_DIR}/generate_prompt_cache.py"
if [[ ! -f "$GEN_SCRIPT" ]]; then
    error "缓存生成脚本不存在: ${GEN_SCRIPT}"
    exit 1
fi

# ── 从配置文件读取提示词 ──────────────────────────────────────────
# 读取 base_prompt（config.yaml 中的 prompt 字段）
# 读取 prompt_template 文件名
# 渲染模板，剥离动态 <context> 块
info "从配置文件读取系统提示词..."

STATIC_PROMPT=$(python3 - "$SERVER_DIR" <<'PYEOF'
import sys, os, re, yaml

server_dir = sys.argv[1]
os.chdir(server_dir)

# 加载配置（优先 data/.config.yaml 覆盖）
def load_config():
    with open("config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    override_path = os.path.join("data", ".config.yaml")
    if os.path.exists(override_path):
        with open(override_path, "r", encoding="utf-8") as f:
            override = yaml.safe_load(f) or {}
        # 浅合并顶层 key
        for k, v in override.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg

cfg = load_config()

# ── 车型 Profile 覆盖（与 config_loader.py 的 _apply_vehicle_profile 保持一致）
profile_name = cfg.get("vehicle_profile")
if profile_name:
    profile_path = os.path.join("data", "vehicle_profiles", f"{profile_name}.yaml")
    if os.path.exists(profile_path):
        with open(profile_path, "r", encoding="utf-8") as f:
            profile = yaml.safe_load(f) or {}
        print(f"[Profile] 加载车型配置: {profile.get('name', profile_name)}", file=sys.stderr)
        for key in ("prompt_template", "prompt", "tool_calling_rules"):
            if key in profile:
                cfg[key] = profile[key]
    else:
        print(f"[Profile] 警告：车型配置文件不存在: {profile_path}", file=sys.stderr)

# 获取 base_prompt
base_prompt = cfg.get("prompt", "")

# 获取模板文件
template_file = cfg.get("prompt_template", "agent-base-prompt.txt")
if not os.path.exists(template_file):
    print(f"ERROR: 模板文件不存在: {template_file}", file=sys.stderr)
    sys.exit(1)

with open(template_file, "r", encoding="utf-8") as f:
    template_content = f.read()

# 简单 Jinja2 变量替换（仅替换我们需要的静态变量）
rendered = template_content
rendered = rendered.replace("{{base_prompt}}", base_prompt.strip())
rendered = rendered.replace("{{ base_prompt }}", base_prompt.strip())
# 动态变量用空字符串替换
for var in ["current_time", "today_date", "today_weekday", "lunar_date",
            "local_address", "weather_info", "dynamic_context",
            "language", "emojiList", "device_id", "client_ip"]:
    rendered = rendered.replace("{{" + var + "}}", "")
    rendered = rendered.replace("{{ " + var + " }}", "")

# 剥离 <context>...</context> 动态块（保留纯静态部分作为缓存）
rendered = re.sub(r'<context>.*?</context>\s*', '', rendered, flags=re.DOTALL)

# 剥离 <memory>...</memory> 空块
rendered = re.sub(r'<memory>\s*</memory>\s*', '', rendered, flags=re.DOTALL)

# 清理多余空行
rendered = re.sub(r'\n{3,}', '\n\n', rendered).strip()

print(rendered)
PYEOF
)

# 使用与语音服务相同的配置加载和运行时能力裁剪逻辑重新渲染，
# 确保预热提示词与实际首轮请求完全一致。
PROMPT_TXT_OUTPUT="${MODEL_DIR}/voice_assistant_static_prompt_${PROMPT_MODE}.txt"
PROMPT_RENDERER="${SCRIPT_DIR}/render_static_prompt.py"
if [[ ! -f "${PROMPT_RENDERER}" ]]; then
    error "静态提示词渲染器不存在: ${PROMPT_RENDERER}"
    exit 1
fi
python3 "${PROMPT_RENDERER}" \
    --output "${PROMPT_TXT_OUTPUT}" \
    "${PROMPT_MODE_ARGS[@]}"
STATIC_PROMPT="$(<"${PROMPT_TXT_OUTPUT}")"

if [[ -z "$STATIC_PROMPT" ]]; then
    error "无法从配置文件解析静态提示词"
    exit 1
fi

PROMPT_LEN=${#STATIC_PROMPT}
info "静态提示词长度: ${PROMPT_LEN} 字符"
echo "──────────────────────────────────────────"
sed -n '1,5p' <<< "$STATIC_PROMPT"
echo "..."
echo "──────────────────────────────────────────"

echo "$STATIC_PROMPT" > "$PROMPT_TXT_OUTPUT"
info "静态 prompt 文件: ${PROMPT_TXT_OUTPUT}"

if [[ "${PROMPT_CACHE_SUPPORTED}" != "true" ]]; then
    warn "Qwen3.5 RKLLM 不支持保存 prompt cache，已跳过 cache 生成"
    warn "启动脚本会使用静态 prompt 预热 set_function_tools"
    exit 0
fi

# ── 确保 RKLLM Server 未运行（释放 NPU）──────────────────────────
if pgrep -f "llm_server.py" > /dev/null 2>&1; then
    warn "检测到 RKLLM Server 正在运行，需要先停止以释放 NPU"
    if [[ -x "${SCRIPT_DIR}/voice-assistant.sh" ]]; then
        bash "${SCRIPT_DIR}/voice-assistant.sh" stop
        sleep 2
    else
        error "请先手动停止 RKLLM Server"
        exit 1
    fi
fi

# ── 生成缓存 ──────────────────────────────────────────────────────
info "正在生成 Prompt Cache（预计 30-60 秒）..."

cd "$RKLLM_SERVER_DIR"
python3 generate_prompt_cache.py \
    --rkllm_model_path "$MODEL_PATH" \
    --target_platform "$PLATFORM" \
    --max_context_len "$MAX_CTX" \
    --output "$CACHE_OUTPUT" \
    "${MULTIMODAL_ARGS[@]}" \
    --prompt "$STATIC_PROMPT"

# ── 验证 ──────────────────────────────────────────────────────────
if [[ -f "$CACHE_OUTPUT" ]]; then
    CACHE_SIZE=$(du -h "$CACHE_OUTPUT" | cut -f1)
    info "✓ Prompt Cache 生成成功！"
    info "  路径: ${CACHE_OUTPUT}"
    info "  大小: ${CACHE_SIZE}"

    # 同时保存静态 prompt 文本（供 flask_server 启动预热使用）
    echo "$STATIC_PROMPT" > "$PROMPT_TXT_OUTPUT"
    info "  静态 prompt 文件: ${PROMPT_TXT_OUTPUT}"
    echo ""
    info "现在可以启动服务: cd ${SCRIPT_DIR} && ./voice-assistant.sh start --no-follow"
else
    error "Prompt Cache 生成失败，文件不存在: ${CACHE_OUTPUT}"
    exit 1
fi
