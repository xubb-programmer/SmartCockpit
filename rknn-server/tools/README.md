# RKNN-Server 测试工具

本目录包含用于**单独测试 RKLLM 模型推理服务**的工具脚本，无需启动 `voice-assistant-server`。

## 前提条件

- `llm_server.py` 已在板端（或本机）启动，默认端口 **18080**
- Python 3 + `requests` 库

```bash
# 启动模型服务
cd ~/rknn-server
./start_llm.sh
```

---

## 工具一览

| 脚本 | 用途 | 模式 |
|------|------|------|
| `test_chat.py` | 交互式对话测试（自定义提示词、多轮对话） | 交互式 |
| `test_tool_call.py` | 自动化测试模型是否支持 `<tool_call>` 输出格式 | 批量自动 |

---

## test_chat.py — 交互式对话

### 快速开始

```bash
# 默认连接本机 18080，使用内置默认提示词
python3 test_chat.py

# 指定自定义 system prompt
python3 test_chat.py --prompt "你是一个车载智能助手，名字叫小智"

# 从文件加载 prompt（适合长提示词）
python3 test_chat.py --prompt-file ../models/voice_assistant_static_prompt.txt

# 连接远程板端
python3 test_chat.py --url http://192.168.1.100:18080

# 非流式 + 思维链
python3 test_chat.py --no-stream --thinking
```

### 启动参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--url` | llm_server 服务地址 | `http://127.0.0.1:18080` |
| `--prompt` | System prompt 文本 | 内置默认提示词 |
| `--prompt-file` | 从文件加载 system prompt | — |
| `--no-stream` | 使用非流式模式 | 默认流式 |
| `--thinking` | 启用思维链模式 | 默认关闭 |

### 交互命令

启动后，直接输入文字即为对话内容。也可以使用以下命令：

| 命令 | 功能 |
|------|------|
| `/prompt <text>` | 修改 system prompt（同时重置对话历史） |
| `/reset` | 清空对话历史（保留 prompt） |
| `/history` | 查看当前完整对话历史 |
| `/stream` | 运行时切换 流式/非流式 |
| `/thinking` | 运行时切换 思维链模式 |
| `/tools` | 运行时切换 function calling 测试模式 |
| `/health` | 查询服务状态（KV cache 大小等） |
| `/help` | 显示帮助 |
| `/quit` | 退出（或 Ctrl+C） |

### 使用示例

```
═══════════════════════════════════════════════════
 RKLLM 模型服务测试工具
═══════════════════════════════════════════════════
  服务地址:    http://127.0.0.1:18080
  流式模式:    开
  System Prompt: 你是一个智能助手，请用简洁的中文回答用户的问题。

✓ 服务状态: ok
  KV Cache:     128 / 4096 tokens

你: 你好，你是谁？
助手: 😊 你好！我是你的智能助手，有什么可以帮你的吗？
[0.8s | 首token 0.12s | 18字]

你: 给我讲个冷笑话
助手: 😆 为什么程序员总是分不清万圣节和圣诞节？因为 Oct 31 = Dec 25。
[1.2s | 首token 0.11s | 32字]

你: /tools
Function Calling 测试模式: 开启
  可用工具: get_weather、play_music、handle_exit_intent

你: 深圳天气怎么样
助手: <tool_call>{"name":"get_weather","arguments":{"location":"深圳"}}</tool_call>
[0.5s | 首token 0.09s | 52字]

你: /history
═══ 当前对话历史 ═══
[system] 你是一个智能助手，请用简洁的中文回答用户的问题。
[user] 你好，你是谁？
[assistant] 😊 你好！我是你的智能助手，有什么可以帮你的吗？
[user] 给我讲个冷笑话
[assistant] 😆 为什么程序员总是分不清万圣节和圣诞节？...
[user] 深圳天气怎么样
[assistant] <tool_call>{"name":"get_weather","arguments":{"location":"深圳"}}</tool_call>
═══ 共 6 条消息 ═══

你: /quit
再见！
```

---

## test_tool_call.py — 工具调用格式测试

自动化测试模型是否支持 `<tool_call>` 标签格式的 function calling。

### 用法

```bash
python3 test_tool_call.py
```

### 测试用例

| # | 场景 | 期望行为 |
|---|------|----------|
| 1 | "帮我播放周杰伦的青花瓷" | 调用 `play_music` |
| 2 | "随便放首歌" | 调用 `play_music` |
| 3 | "深圳今天天气怎么样" | 调用 `get_weather` |
| 4 | "好的，再见" | 调用 `handle_exit_intent` |
| 5 | "你好，你叫什么名字" | **不应**触发工具调用 |

### 输出示例

```
============================================================
RKLLM <tool_call> 格式支持测试
Server: http://127.0.0.1:18080/rkllm_chat
============================================================

[1/5] 播放指定歌曲
  用户: 帮我播放周杰伦的青花瓷
  响应: <tool_call>{"name":"play_music","arguments":{"song_name":"青花瓷"}}</tool_call>
  ✅ 正确调用 play_music({"song_name": "青花瓷"}) [格式: 有<tool_call>标签]

...

============================================================
测试结论
============================================================
  ✅ 播放指定歌曲: ok_tool_wrapped
  ✅ 随机播放音乐: ok_tool_wrapped
  ✅ 查询天气: ok_tool_wrapped
  ✅ 退出意图: ok_tool_wrapped
  ✅ 普通对话（不应触发工具）: ok_normal

通过率: 5/5

🎉 RKLLM 完全支持 <tool_call> 格式！
```

---

## 常见问题

**Q: 连接不上服务？**
```
✗ 无法连接到 http://127.0.0.1:18080
```
→ 确认 `llm_server.py` 已启动：`curl http://<板端IP>:18080/health`

**Q: 返回 503？**
```
⚠ 服务忙碌 (503)，请稍后重试
```
→ 模型正在处理其他请求（同时只能处理一个），等几秒重试

**Q: 首次响应很慢？**
→ 首次请求需要 prefill system prompt 到 KV cache（~5-8s），后续请求会快很多。可通过 `--prompt_cache_path` 启动 `llm_server.py` 来加速。
