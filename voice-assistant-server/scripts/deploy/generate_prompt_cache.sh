#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
# 离线生成 RKLLM Prompt Cache
# 用途：一次性 prefill 静态系统提示词并保存 KV 缓存到磁盘。
#       flask_server.py 启动时自动加载，Turn 1 延迟从 ~4s 降至 ~0.5s。
# 用法：./generate_prompt_cache.sh [--force]
#       --force : 强制重新生成（即使 cache 已存在）
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVER_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ── 配置（与 start.sh 保持一致）─────────────────────────────────
# 自动推导部署根目录：脚本在 voice-assistant-server/scripts/deploy/ 下
HOME_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
MODEL_DIR="${HOME_DIR}/models"
RKLLM_SERVER_DIR="${HOME_DIR}/rknn-server"
PLATFORM="rk3588"
CACHE_OUTPUT="${MODEL_DIR}/voice_assistant_system_prompt.cache"

# ── 颜色 ──────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO ]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN ]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ── 参数解析 ──────────────────────────────────────────────────────
FORCE=false
MODEL_SIZE="qwen3-4b"
for arg in "$@"; do
    case "$arg" in
        --force)     FORCE=true ;;
        3b)          MODEL_SIZE="3b" ;;
        7b)          MODEL_SIZE="7b" ;;
        qwen3-1.7b)  MODEL_SIZE="qwen3-1.7b" ;;
        qwen3-4b)    MODEL_SIZE="qwen3-4b" ;;
        1.5b-z1)     MODEL_SIZE="1.5b-z1" ;;
    esac
done

case "${MODEL_SIZE}" in
  3b)           MODEL_PATH="${MODEL_DIR}/qwen2.5-3b_w8a8.rkllm";        MAX_CTX=4096 ;;
  7b)           MODEL_PATH="${MODEL_DIR}/qwen2.5-7b_w8a8.rkllm";        MAX_CTX=4096 ;;
  qwen3-1.7b)   MODEL_PATH="${MODEL_DIR}/qwen3-1.7b_w8a8.rkllm";       MAX_CTX=4096 ;;
  1.5b-z1)      MODEL_PATH="${MODEL_DIR}/Qwen2.5-1.5B-Z1-FC_W8A8_RK3588.rkllm"; MAX_CTX=4096 ;;
  qwen3-4b|*)   MODEL_PATH="${MODEL_DIR}/qwen3-4b-instruct_w8a8.rkllm"; MAX_CTX=4096 ;;
esac


if [[ -f "$CACHE_OUTPUT" && "$FORCE" != true ]]; then
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

if [[ -z "$STATIC_PROMPT" ]]; then
    error "无法从配置文件解析静态提示词"
    exit 1
fi

PROMPT_LEN=${#STATIC_PROMPT}
info "静态提示词长度: ${PROMPT_LEN} 字符"
echo "──────────────────────────────────────────"
echo "$STATIC_PROMPT" | head -5
echo "..."
echo "──────────────────────────────────────────"

# ── 确保 RKLLM Server 未运行（释放 NPU）──────────────────────────
if pgrep -f "llm_server.py" > /dev/null 2>&1; then
    warn "检测到 RKLLM Server 正在运行，需要先停止以释放 NPU"
    if [[ -f "${SCRIPT_DIR}/stop.sh" ]]; then
        bash "${SCRIPT_DIR}/stop.sh"
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
    --prompt "$STATIC_PROMPT"

# ── 验证 ──────────────────────────────────────────────────────────
PROMPT_TXT_OUTPUT="${MODEL_DIR}/voice_assistant_static_prompt.txt"
if [[ -f "$CACHE_OUTPUT" ]]; then
    CACHE_SIZE=$(du -h "$CACHE_OUTPUT" | cut -f1)
    info "✓ Prompt Cache 生成成功！"
    info "  路径: ${CACHE_OUTPUT}"
    info "  大小: ${CACHE_SIZE}"

    # 同时保存静态 prompt 文本（供 flask_server 启动预热使用）
    echo "$STATIC_PROMPT" > "$PROMPT_TXT_OUTPUT"
    info "  静态 prompt 文件: ${PROMPT_TXT_OUTPUT}"
    echo ""
    info "现在可以启动服务: cd ${SCRIPT_DIR} && ./start.sh"
else
    error "Prompt Cache 生成失败，文件不存在: ${CACHE_OUTPUT}"
    exit 1
fi
