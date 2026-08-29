# RKNN/RKLLM 板端推理服务

本目录用于在 RK3588 板端提供离线文本对话和视觉分析。部署遵循 RKNN-LLM 官方多模态结构：

- 纯文本模型只加载一个 RKLLM 句柄并监听 `18080`
- Qwen3.5 启用视觉时，在同一进程加载配套的 Vision + Projector RKNN
- 文本接口和视觉接口共用同一个 RKLLM 句柄及 `18080` 端口
- 纯文本模型不允许加载 Qwen3.5 视觉编码器，避免模型与图像嵌入不匹配

## 目录结构

```text
rknn-server/
├── llm_server.py              # 通用推理服务，支持纯文本或单实例多模态
├── generate_prompt_cache.py   # 离线生成 prompt cache 工具
├── lib/                       # 运行时动态库（板端需要）
│   ├── librkllmrt.so          # RKLLM v1.3.0 aarch64 runtime
│   └── librknnrt.so           # RKNN vision encoder runtime
├── scripts/                   # 辅助脚本
├── tools/                     # 测试工具
│   ├── test_tool_call.py
│   └── test_vision_qwen35.py
├── start_llm.sh               # 启动一个实例，默认纯文本
├── start_all.sh               # 启动 Qwen3.5 单实例多模态服务
└── README.md
```

## 模型文件

模型统一放在 `<部署目录>/models/` 目录下：

| 模型文件 | 用途 | 默认状态 |
|----------|------|----------|
| `Qwen3.5-0.8B_w8a8_rk3588.rkllm` | 文本对话 + 视觉语言推理 | 默认 |
| `Qwen3.5-0.8B_vision_rk3588.rknn` | 视觉编码器 | 启用视觉时加载 |
| `voice_assistant_static_prompt.txt` | 启动预热用静态 system prompt | 默认 |

视觉参数按 RKNN-LLM 官方 Qwen3.5 示例对齐：

```bash
./demo demo.jpg \
  ./Qwen3.5-0.8B_vision_rk3588.rknn \
  ./Qwen3.5-0.8B_w8a8_rk3588.rkllm \
  2048 4096 3 rk3588 \
  "<|vision_start|>" "<|vision_end|>" "<|image_pad|>"
```

## 启动

```bash
cd /opt/jw/rknn-server
./start_llm.sh
```

`start_llm.sh` 默认只启动 `18080` 文本实例：

- 文本接口：`http://<板端IP>:18080/rkllm_chat`
- 健康检查：`http://<板端IP>:18080/health`

完整部署请使用统一脚本。纯文本模型不能搭配 `--vision`：

```bash
cd /opt/jw/voice-assistant-server/scripts/deploy
./voice-assistant.sh start qwen3-4b --no-follow
./voice-assistant.sh start qwen3.5 --vision --no-follow
```

## Prompt 预热

RKLLM 1.3 的 Qwen3.5 模型不支持保存 prompt cache。部署脚本会保存静态 system prompt 文本，并在服务启动时预热 `set_function_tools`，避免首轮请求重复初始化。

```bash
cd /opt/jw/voice-assistant-server/scripts/deploy
./generate_prompt_cache.sh --force
```

这会生成：

```text
/opt/jw/models/voice_assistant_static_prompt.txt
```

旧文本模型仍可使用 prompt cache。底层工具可单独运行：

```bash
python3 generate_prompt_cache.py \
  --rkllm_model_path ../models/qwen3-4b-instruct_w8a8.rkllm \
  --target_platform rk3588 \
  --output ../models/voice_assistant_system_prompt_qwen3-4b-instruct.cache \
  --prompt_file ../models/voice_assistant_static_prompt.txt
```

## 验证

```bash
# 健康检查，确认 18080 单实例已加载视觉参数
python3 tools/test_vision_qwen35.py --health-only

# 使用板端本地图片做真实视觉分析，图片路径需在 /tmp、/data/images 或 /var/tmp 下
python3 tools/test_vision_qwen35.py --image /tmp/demo.jpg

# 文本对话测试
python3 tools/test_chat.py --url http://127.0.0.1:18080/rkllm_chat
```

## 接口

| 接口 | 方法 | 请求体 | 说明 |
|------|------|--------|------|
| `/rkllm_chat` | POST | `conversation_id`, `messages`, `stream`, `tools`, `max_tokens`, `reset_conversation` | 文本对话推理；必须提供会话ID和非空system消息 |
| `/rkllm_sync_kv` | POST | `messages` | 关键词匹配后同步 KV cache |
| `/vision/analyze` | POST | `image_path`, `question` | 板端本地图片分析 |
| `/health` | GET | 无 | 健康检查，包含文本和视觉参数 |

## 架构关系

```text
voice-assistant-server (端口 18000/18003)
  └── llm_server (端口 18080，单个 RKLLM 句柄)
      ├── 文本对话 → /rkllm_chat, /rkllm_sync_kv
      └── 视觉分析 → /vision/analyze（仅 Qwen3.5 + --vision）
```
