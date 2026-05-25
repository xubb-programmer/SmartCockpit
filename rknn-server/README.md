# RKNN 板端推理服务

统一管理 RK3588 板端的 LLM 文本推理和视觉分析两个服务。

## 目录结构

```
rknn-server/
├── llm_server.py              # 文本 LLM 推理服务（端口 18080）
├── vision_server.py           # 视觉分析服务（端口 18090）
├── generate_prompt_cache.py   # 离线生成 prompt cache 工具
├── lib/                       # 运行时动态库（板端需要）
│   └── librkllmrt.so
├── scripts/                   # 辅助脚本
│   ├── fix_freq_rk3588.sh
│   ├── fix_freq_rk3576.sh
│   ├── fix_freq_rk3562.sh
│   └── fix_freq_rv1126b.sh
├── tools/                     # 测试工具
│   └── test_tool_call.py      # tool_call 格式测试
├── start_llm.sh               # 启动文本服务
├── start_vision.sh            # 启动视觉服务
├── start_all.sh               # 一键启动所有服务
└── README.md                  # 本文件
```

## 模型文件

部署到板端后，模型统一放在 `<部署目录>/models/` 目录下：

| 模型文件 | 用途 | 服务 |
|----------|------|------|
| `qwen3-4b-instruct_w8a8.rkllm` | 文本对话 LLM | llm_server |
| `qwen3-vl-2b_vision_rk3588.rknn` | 视觉编码器 | vision_server |
| `qwen3-vl-2b-instruct_w8a8_rk3588.rkllm` | 视觉语言 LLM | vision_server |
| `voice_assistant_system_prompt.cache` | system prompt 缓存 | llm_server |

## 部署步骤

### 1. 推送到板端

```bash
# 将整个 rknn-server 目录推送到板端（替换 <user> 和 <ip> 为实际值）
scp -r ./rknn-server <user>@<ip>:~/ 

# 设置脚本可执行
ssh <user>@<ip> "chmod +x ~/rknn-server/*.sh ~/rknn-server/scripts/*.sh"
```

### 2. 启动服务

```bash
# 方式一：一键启动所有服务
cd ~/rknn-server
./start_all.sh

# 方式二：单独启动
./start_llm.sh      # 仅文本服务
./start_vision.sh   # 仅视觉服务
```

### 3. 验证

```bash
# 检查文本服务
curl http://<板端IP>:18080/rkllm_chat -X POST \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"你好"}],"stream":false}'

# 检查视觉服务
curl http://<板端IP>:18090/health
```

## 接口规范

| 服务 | 接口 | 方法 | 请求体 | 说明 |
|------|------|------|--------|------|
| LLM :18080 | `/rkllm_chat` | POST | `messages`(必填), `stream`, `tools`, `enable_thinking`, `reset_conversation` | 对话推理，503=忙 |
| LLM :18080 | `/rkllm_sync_kv` | POST | `messages`(必填) | 关键词匹配后同步 KV cache |
| Vision :18090 | `/vision/analyze` | POST | `image_path`(必填), `question`(必填) | 图像分析，路径为板端本地路径 |
| Vision :18090 | `/health` | GET | — | 健康检查 |

## 工具说明

### generate_prompt_cache.py

离线生成 system prompt 的 KV cache，加速首次推理：

```bash
python3 generate_prompt_cache.py \
  --rkllm_model_path ../models/qwen3-4b-instruct_w8a8.rkllm \
  --target_platform rk3588 \
  --output ../models/voice_assistant_system_prompt.cache \
  --prompt_file ../models/voice_assistant_static_prompt.txt
```

### tools/test_tool_call.py

测试 RKLLM 模型是否支持 `<tool_call>` 输出格式：

```bash
python3 tools/test_tool_call.py
```

## 架构关系

```
voice-assistant-server (端口 18000/18006)
  ├── 文本对话 → llm_server  (端口 18080) /rkllm_chat, /rkllm_sync_kv
  └── 视觉分析 → vision_server (端口 18090) /vision/analyze, /health
```
