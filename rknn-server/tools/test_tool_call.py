#!/usr/bin/env python3
"""
测试 RKLLM + flask_server.py + Qwen 模型是否支持 <tool_call> 输出格式。

用法（在板子上运行，确保 flask_server.py 已启动）:
  python3 test_tool_call.py

测试策略:
  1. 用 Qwen 标准 ChatML 工具定义格式构建 system prompt
  2. 发送明确触发工具调用的用户消息
  3. 观察模型是否输出 <tool_call>...</tool_call>

结论判断:
  - 输出中含 <tool_call> → RKLLM 支持原生 function calling ✅
  - 输出中没有 <tool_call> → 需要继续依赖关键词匹配 ❌
"""

import json
import re
import requests
import sys

FLASK_SERVER_URL = "http://127.0.0.1:18080/rkllm_chat"
MODEL_NAME = "rkllm_model"

# ── Qwen ChatML 工具定义（标准格式）──────────────────────────────
TOOLS_QWEN_FORMAT = """# 工具

## 你拥有如下工具：

### play_music

播放音乐工具，可以播放、暂停、停止音乐，也可以切换到下一首。

参数:
- song_name (string, 必填): 歌曲名称，或 "random"（随机）、"next"（下一首）、"stop"（停止）、"pause"（暂停）

调用格式:
<tool_call>
{"name": "play_music", "arguments": {"song_name": "歌曲名或指令"}}
</tool_call>

### get_weather

查询天气信息。

参数:
- city (string, 必填): 城市名称

调用格式:
<tool_call>
{"name": "get_weather", "arguments": {"city": "城市名"}}
</tool_call>

### handle_exit_intent

当用户说再见、拜拜、退出时调用。

调用格式:
<tool_call>
{"name": "handle_exit_intent", "arguments": {}}
</tool_call>
"""

# ── 测试用例 ────────────────────────────────────────────────────
TEST_CASES = [
    {
        "name": "播放指定歌曲",
        "user": "帮我播放周杰伦的青花瓷",
        "expect_tool": "play_music",
        "expect_arg_key": "song_name",
    },
    {
        "name": "随机播放音乐",
        "user": "随便放首歌",
        "expect_tool": "play_music",
        "expect_arg_key": "song_name",
    },
    {
        "name": "查询天气",
        "user": "深圳今天天气怎么样",
        "expect_tool": "get_weather",
        "expect_arg_key": "city",
    },
    {
        "name": "退出意图",
        "user": "好的，再见",
        "expect_tool": "handle_exit_intent",
        "expect_arg_key": None,
    },
    {
        "name": "普通对话（不应触发工具）",
        "user": "你好，你叫什么名字",
        "expect_tool": None,
        "expect_arg_key": None,
    },
]

# ── 系统提示词（包含工具定义）────────────────────────────────────
SYSTEM_PROMPT = f"""你是Z1，一个车载智能语音助手。
声音自然亲切，善于回答路线、时间、天气、知识等问题。
驾驶场景下回答简洁准确，不啰嗦。

{TOOLS_QWEN_FORMAT}

当用户的请求需要使用工具时，你必须输出 <tool_call> 标签调用工具，不要输出其他内容。
当用户进行普通对话时，直接用中文回答，不要使用工具。"""


def call_rkllm(messages, stream=False):
    """调用 flask_server.py 接口"""
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "stream": stream,
    }
    try:
        resp = requests.post(
            FLASK_SERVER_URL,
            json=payload,
            stream=stream,
            timeout=60,
        )
        if resp.status_code != 200:
            print(f"  ❌ HTTP {resp.status_code}: {resp.text[:200]}")
            return None
        return resp
    except requests.exceptions.ConnectionError:
        print(f"  ❌ 无法连接到 RKLLM Server: {FLASK_SERVER_URL}")
        print("     请确认 flask_server.py 已启动")
        sys.exit(1)
    except requests.exceptions.Timeout:
        print("  ❌ 请求超时")
        return None


def collect_full_response(resp, stream=True):
    """收集完整响应文本"""
    full_text = ""
    if stream:
        for line in resp.iter_lines():
            if not line:
                continue
            try:
                chunk = json.loads(line.decode("utf-8"))
                choices = chunk.get("choices", [])
                if not choices:
                    continue
                choice = choices[-1]
                delta = choice.get("delta") or choice.get("message") or {}
                content = delta.get("content", "")
                finish_reason = choice.get("finish_reason")
                if content:
                    full_text += content
                    print(content, end="", flush=True)
                if finish_reason == "stop":
                    break
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
    else:
        data = resp.json()
        full_text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        print(full_text, end="")
    return full_text


def analyze_response(text, expect_tool, expect_arg_key):
    """分析响应中是否包含正确的 tool_call（支持有/无 wrapper 两种格式）"""
    # 优先匹配带 wrapper 的格式
    tool_call_pattern = re.compile(r'<tool_call>(.*?)</tool_call>', re.DOTALL)
    matches = tool_call_pattern.findall(text)
    has_wrapper = bool(matches)

    # 若无 wrapper，尝试匹配裸 JSON 格式：{"name": "...", "arguments": {...}}
    if not matches:
        bare_pattern = re.compile(r'\{[^{}]*"name"\s*:\s*"[^"]+"\s*,[^{}]*"arguments"\s*:\s*\{[^{}]*\}[^{}]*\}', re.DOTALL)
        bare_matches = bare_pattern.findall(text)
        if bare_matches:
            matches = bare_matches

    if expect_tool is None:
        # 期望普通对话，不应有 tool_call
        if matches:
            print(f"\n  ⚠️  意外触发了工具调用: {matches[0].strip()[:80]}")
            return "unexpected_tool"
        else:
            print(f"\n  ✅ 正常对话，无工具调用")
            return "ok_normal"

    if not matches:
        print(f"\n  ❌ 未输出任何工具调用格式，期望调用: {expect_tool}")
        return "no_tool_call"

    # 解析第一个 tool_call
    try:
        call_data = json.loads(matches[0].strip())
        actual_tool = call_data.get("name")
        arguments = call_data.get("arguments", {})
        wrapper_status = "有wrapper ✅" if has_wrapper else "无wrapper ⚠️"

        if actual_tool != expect_tool:
            print(f"\n  ❌ 工具名称不匹配: 期望 {expect_tool}，实际 {actual_tool}")
            return "wrong_tool"

        if expect_arg_key and expect_arg_key not in arguments:
            print(f"\n  ⚠️  工具参数缺失 '{expect_arg_key}'，实际 {arguments} [{wrapper_status}]")
            return "missing_arg"

        if has_wrapper:
            print(f"\n  ✅ 正确调用 {actual_tool}({arguments}) [格式: 有<tool_call>标签]")
            return "ok_tool_wrapped"
        else:
            print(f"\n  ⚠️  正确调用 {actual_tool}({arguments}) [格式: 裸JSON，无<tool_call>标签]")
            return "ok_tool_bare"

    except json.JSONDecodeError as e:
        print(f"\n  ❌ JSON 解析失败: {e}")
        print(f"     原始内容: {matches[0][:200]}")
        return "parse_error"


def run_tests():
    print("=" * 60)
    print("RKLLM <tool_call> 格式支持测试")
    print(f"Server: {FLASK_SERVER_URL}")
    print("=" * 60)
    print()
    print("注意: 每个测试用例会在 system prompt 加唯一 ID，")
    print("      强制 flask_server.py 重置 KV cache，避免状态污染。")

    results = {}

    for i, tc in enumerate(TEST_CASES):
        print(f"\n[{i+1}/{len(TEST_CASES)}] {tc['name']}")
        print(f"  用户: {tc['user']}")
        print(f"  响应: ", end="", flush=True)

        # 关键：每个测试加唯一前缀，触发 flask_server.py 的
        # "system prompt changed → 全量 KV reset" 逻辑
        unique_system = f"[TEST-{i+1}]\n{SYSTEM_PROMPT}"

        messages = [
            {"role": "system", "content": unique_system},
            {"role": "user", "content": tc["user"]},
        ]

        resp = call_rkllm(messages, stream=True)
        if resp is None:
            results[tc["name"]] = "error"
            continue

        full_text = collect_full_response(resp, stream=True)
        result = analyze_response(full_text, tc["expect_tool"], tc["expect_arg_key"])
        results[tc["name"]] = result

    # ── 汇总 ──
    print("\n" + "=" * 60)
    print("测试结论")
    print("=" * 60)

    ok_count = sum(1 for r in results.values() if r.startswith("ok"))
    total = len(results)

    for name, result in results.items():
        icon = "✅" if result.startswith("ok") else "❌"
        print(f"  {icon} {name}: {result}")

    print(f"\n通过率: {ok_count}/{total}")

    if ok_count == total:
        print("\n🎉 RKLLM 完全支持 <tool_call> 格式！")
        print("   可以考虑将 rkllm.py 改为让模型直接输出工具调用。")
    elif ok_count >= 3:
        print("\n⚠️  RKLLM 基本支持 <tool_call>，可进一步优化 prompt 格式。")
    else:
        print("\n❌ RKLLM 不稳定支持 <tool_call>，建议继续使用关键词匹配方案。")


if __name__ == "__main__":
    run_tests()
