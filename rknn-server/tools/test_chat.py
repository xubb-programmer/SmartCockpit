#!/usr/bin/env python3
"""
RKLLM 模型服务交互测试工具

单独测试 llm_server.py 的对话推理能力，无需启动 voice-assistant-server。

用法:
    python3 tools/test_chat.py                              # 默认连接本机 18080
    python3 tools/test_chat.py --url http://192.168.1.100:18080
    python3 tools/test_chat.py --prompt "你是一个车载助手"    # 指定 system prompt
    python3 tools/test_chat.py --prompt-file my_prompt.txt   # 从文件加载 prompt
    python3 tools/test_chat.py --no-stream                   # 关闭流式输出

交互命令:
    /prompt <text>     — 修改 system prompt（重置对话）
    /reset             — 清空对话历史
    /history           — 查看当前对话历史
    /stream            — 切换流式/非流式模式
    /thinking          — 切换思维链模式
    /health            — 查询服务健康状态
    /tools             — 切换 function calling 测试模式
    /help              — 显示帮助
    /quit              — 退出
"""

import argparse
import json
import sys
import os
import time
import requests


# ── 颜色输出 ────────────────────────────────────────────────────
class Colors:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    RED     = "\033[31m"
    GREEN   = "\033[32m"
    YELLOW  = "\033[33m"
    BLUE    = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN    = "\033[36m"

    @staticmethod
    def enabled():
        """检测终端是否支持颜色"""
        if os.environ.get("NO_COLOR"):
            return False
        if sys.platform == "win32":
            # Windows 10+ 支持 ANSI，尝试启用
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
                return True
            except Exception:
                return False
        return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


if not Colors.enabled():
    # 不支持颜色时清空所有颜色代码
    for attr in dir(Colors):
        if attr.isupper() and isinstance(getattr(Colors, attr), str):
            setattr(Colors, attr, "")

C = Colors


# ── 测试用 tools 定义 ──────────────────────────────────────────
SAMPLE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市的天气信息",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "城市名称，如：北京、上海"
                    }
                },
                "required": ["location"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "play_music",
            "description": "播放音乐",
            "parameters": {
                "type": "object",
                "properties": {
                    "song_name": {
                        "type": "string",
                        "description": "歌曲名称或歌手名"
                    }
                },
                "required": ["song_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "handle_exit_intent",
            "description": "当用户想结束对话或需要退出系统时调用",
            "parameters": {
                "type": "object",
                "properties": {
                    "say_goodbye": {
                        "type": "string",
                        "description": "和用户友好结束对话的告别语"
                    }
                },
                "required": ["say_goodbye"]
            }
        }
    }
]


# ── 主类 ────────────────────────────────────────────────────────
class ChatClient:
    def __init__(self, base_url, system_prompt, stream=True, enable_thinking=False):
        self.base_url = base_url.rstrip("/")
        self.chat_url = f"{self.base_url}/rkllm_chat"
        self.health_url = f"{self.base_url}/health"
        self.system_prompt = system_prompt
        self.stream = stream
        self.enable_thinking = enable_thinking
        self.use_tools = False
        self.messages = []  # 对话历史（不含 system）
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def _build_messages(self):
        """构建完整的 messages 数组（system + 对话历史）"""
        msgs = []
        if self.system_prompt:
            msgs.append({"role": "system", "content": self.system_prompt})
        msgs.extend(self.messages)
        return msgs

    def check_health(self):
        """查询服务健康状态"""
        try:
            resp = self.session.get(self.health_url, timeout=5)
            if resp.status_code == 200:
                info = resp.json()
                print(f"\n{C.GREEN}✓ 服务状态: {info.get('status', '?')}{C.RESET}")
                print(f"  KV Cache:     {info.get('kv_cache_size', '?')} / {info.get('max_context_len', '?')} tokens")
                print(f"  模型路径:     {info.get('model_path', '?')}")
            else:
                print(f"\n{C.RED}✗ 服务返回 {resp.status_code}: {resp.text}{C.RESET}")
        except requests.ConnectionError:
            print(f"\n{C.RED}✗ 无法连接到 {self.health_url}{C.RESET}")
        except Exception as e:
            print(f"\n{C.RED}✗ 健康检查失败: {e}{C.RESET}")

    def chat(self, user_input):
        """发送一轮对话"""
        # 添加用户消息
        self.messages.append({"role": "user", "content": user_input})

        # 构建请求
        payload = {
            "messages": self._build_messages(),
            "stream": self.stream,
        }
        if self.enable_thinking:
            payload["enable_thinking"] = True
        if self.use_tools:
            payload["tools"] = SAMPLE_TOOLS

        # 发送请求
        t_start = time.time()
        try:
            if self.stream:
                return self._chat_stream(payload, t_start)
            else:
                return self._chat_sync(payload, t_start)
        except requests.ConnectionError:
            print(f"\n{C.RED}✗ 无法连接到 {self.chat_url}{C.RESET}")
            print(f"  请确认 llm_server.py 已启动")
            self.messages.pop()  # 回滚
            return None
        except requests.Timeout:
            print(f"\n{C.RED}✗ 请求超时{C.RESET}")
            self.messages.pop()
            return None
        except Exception as e:
            print(f"\n{C.RED}✗ 请求失败: {e}{C.RESET}")
            self.messages.pop()
            return None

    def _chat_stream(self, payload, t_start):
        """流式对话"""
        resp = self.session.post(self.chat_url, json=payload, stream=True, timeout=120)

        if resp.status_code == 503:
            print(f"\n{C.YELLOW}⚠ 服务忙碌 (503)，请稍后重试{C.RESET}")
            self.messages.pop()
            return None
        if resp.status_code != 200:
            print(f"\n{C.RED}✗ 服务返回 {resp.status_code}: {resp.text}{C.RESET}")
            self.messages.pop()
            return None

        # 流式读取
        full_response = ""
        first_token = True
        print(f"\n{C.CYAN}助手: {C.RESET}", end="", flush=True)

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
                    if first_token:
                        t_first = time.time() - t_start
                        first_token = False
                    print(content, end="", flush=True)
                    full_response += content

            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

        t_total = time.time() - t_start
        print()  # 换行

        # 统计信息
        if full_response:
            stats = f"{C.DIM}[{t_total:.1f}s"
            if not first_token:
                stats += f" | 首token {t_first:.2f}s"
            stats += f" | {len(full_response)}字]{C.RESET}"
            print(stats)

            # 记录 assistant 回复
            self.messages.append({"role": "assistant", "content": full_response})
        else:
            print(f"{C.YELLOW}(无输出){C.RESET}")
            self.messages.pop()  # 回滚 user 消息

        return full_response

    def _chat_sync(self, payload, t_start):
        """非流式对话"""
        resp = self.session.post(self.chat_url, json=payload, timeout=120)

        if resp.status_code == 503:
            print(f"\n{C.YELLOW}⚠ 服务忙碌 (503)，请稍后重试{C.RESET}")
            self.messages.pop()
            return None
        if resp.status_code != 200:
            print(f"\n{C.RED}✗ 服务返回 {resp.status_code}: {resp.text}{C.RESET}")
            self.messages.pop()
            return None

        t_total = time.time() - t_start
        result = resp.json()
        choices = result.get("choices", [])
        if choices:
            content = choices[0].get("message", {}).get("content", "")
            print(f"\n{C.CYAN}助手: {C.RESET}{content}")
            print(f"{C.DIM}[{t_total:.1f}s | {len(content)}字]{C.RESET}")
            self.messages.append({"role": "assistant", "content": content})
            return content
        else:
            print(f"\n{C.YELLOW}(无输出){C.RESET}")
            self.messages.pop()
            return None

    def reset(self):
        """清空对话历史"""
        self.messages.clear()
        print(f"{C.GREEN}✓ 对话历史已清空{C.RESET}")

    def show_history(self):
        """显示对话历史"""
        print(f"\n{C.BOLD}═══ 当前对话历史 ═══{C.RESET}")
        if self.system_prompt:
            prompt_preview = self.system_prompt[:100]
            if len(self.system_prompt) > 100:
                prompt_preview += "..."
            print(f"{C.MAGENTA}[system]{C.RESET} {prompt_preview}")
        if not self.messages:
            print(f"{C.DIM}(空){C.RESET}")
        for msg in self.messages:
            role = msg["role"]
            content = msg.get("content", "")
            if role == "user":
                print(f"{C.GREEN}[user]{C.RESET} {content}")
            elif role == "assistant":
                preview = content[:200]
                if len(content) > 200:
                    preview += "..."
                print(f"{C.CYAN}[assistant]{C.RESET} {preview}")
            elif role == "tool":
                print(f"{C.YELLOW}[tool]{C.RESET} {content[:100]}")
        print(f"{C.BOLD}═══ 共 {len(self.messages)} 条消息 ═══{C.RESET}\n")

    def set_prompt(self, new_prompt):
        """修改 system prompt 并重置对话"""
        self.system_prompt = new_prompt
        self.messages.clear()
        print(f"{C.GREEN}✓ System prompt 已更新，对话已重置{C.RESET}")
        prompt_preview = new_prompt[:80]
        if len(new_prompt) > 80:
            prompt_preview += "..."
        print(f"  {C.DIM}{prompt_preview}{C.RESET}")


# ── 交互循环 ────────────────────────────────────────────────────
def print_help():
    print(f"""
{C.BOLD}═══ RKLLM 模型服务测试工具 ═══{C.RESET}

{C.YELLOW}交互命令:{C.RESET}
  /prompt <text>     修改 system prompt（重置对话）
  /reset             清空对话历史
  /history           查看当前对话历史
  /stream            切换流式/非流式模式
  /thinking          切换思维链模式
  /health            查询服务健康状态
  /tools             切换 function calling 模式
  /help              显示本帮助
  /quit              退出

{C.DIM}直接输入文字即为对话内容，按 Ctrl+C 也可退出{C.RESET}
""")


def main():
    parser = argparse.ArgumentParser(
        description="RKLLM 模型服务交互测试工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python3 test_chat.py --prompt '你是智能车载助手小智'\n"
               "  python3 test_chat.py --prompt-file system_prompt.txt\n"
               "  python3 test_chat.py --url http://192.168.1.100:18080 --no-stream\n"
    )
    parser.add_argument("--url", type=str, default="http://127.0.0.1:18080",
                        help="llm_server 地址 (默认: http://127.0.0.1:18080)")
    parser.add_argument("--prompt", type=str, default=None,
                        help="System prompt 文本")
    parser.add_argument("--prompt-file", type=str, default=None,
                        help="从文件加载 system prompt")
    parser.add_argument("--no-stream", action="store_true",
                        help="使用非流式模式")
    parser.add_argument("--thinking", action="store_true",
                        help="启用思维链模式")
    args = parser.parse_args()

    # 加载 system prompt
    system_prompt = ""
    if args.prompt_file:
        if os.path.exists(args.prompt_file):
            with open(args.prompt_file, "r", encoding="utf-8") as f:
                system_prompt = f.read().strip()
            print(f"{C.GREEN}✓ 从 {args.prompt_file} 加载 prompt ({len(system_prompt)} 字符){C.RESET}")
        else:
            print(f"{C.RED}✗ 文件不存在: {args.prompt_file}{C.RESET}")
            sys.exit(1)
    elif args.prompt:
        system_prompt = args.prompt
    else:
        # 默认 prompt
        system_prompt = "你是一个智能助手，请用简洁的中文回答用户的问题。"

    # 创建客户端
    client = ChatClient(
        base_url=args.url,
        system_prompt=system_prompt,
        stream=not args.no_stream,
        enable_thinking=args.thinking,
    )

    # 打印欢迎信息
    print(f"\n{C.BOLD}{'═' * 50}{C.RESET}")
    print(f"{C.BOLD} RKLLM 模型服务测试工具{C.RESET}")
    print(f"{C.BOLD}{'═' * 50}{C.RESET}")
    print(f"  服务地址:    {C.CYAN}{args.url}{C.RESET}")
    print(f"  流式模式:    {C.GREEN if client.stream else C.RED}{'开' if client.stream else '关'}{C.RESET}")
    print(f"  思维链:      {C.GREEN if client.enable_thinking else C.RED}{'开' if client.enable_thinking else '关'}{C.RESET}")
    prompt_preview = system_prompt[:60]
    if len(system_prompt) > 60:
        prompt_preview += "..."
    print(f"  System Prompt: {C.DIM}{prompt_preview}{C.RESET}")
    print(f"\n{C.DIM}输入 /help 查看命令，Ctrl+C 退出{C.RESET}\n")

    # 检查服务状态
    client.check_health()
    print()

    # 交互循环
    while True:
        try:
            user_input = input(f"{C.GREEN}你: {C.RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            print(f"\n\n{C.DIM}再见！{C.RESET}")
            break

        if not user_input:
            continue

        # 处理命令
        if user_input.startswith("/"):
            cmd_parts = user_input.split(None, 1)
            cmd = cmd_parts[0].lower()

            if cmd in ("/quit", "/exit", "/q"):
                print(f"\n{C.DIM}再见！{C.RESET}")
                break

            elif cmd == "/help":
                print_help()

            elif cmd == "/reset":
                client.reset()

            elif cmd == "/history":
                client.show_history()

            elif cmd == "/health":
                client.check_health()

            elif cmd == "/stream":
                client.stream = not client.stream
                state = f"{C.GREEN}开启{C.RESET}" if client.stream else f"{C.RED}关闭{C.RESET}"
                print(f"流式模式: {state}")

            elif cmd == "/thinking":
                client.enable_thinking = not client.enable_thinking
                state = f"{C.GREEN}开启{C.RESET}" if client.enable_thinking else f"{C.RED}关闭{C.RESET}"
                print(f"思维链模式: {state}")

            elif cmd == "/tools":
                client.use_tools = not client.use_tools
                state = f"{C.GREEN}开启{C.RESET}" if client.use_tools else f"{C.RED}关闭{C.RESET}"
                print(f"Function Calling 测试模式: {state}")
                if client.use_tools:
                    tool_names = [t["function"]["name"] for t in SAMPLE_TOOLS]
                    print(f"  可用工具: {C.DIM}{', '.join(tool_names)}{C.RESET}")

            elif cmd == "/prompt":
                if len(cmd_parts) > 1:
                    client.set_prompt(cmd_parts[1])
                else:
                    print(f"{C.YELLOW}用法: /prompt <新的提示词>{C.RESET}")
                    print(f"当前: {C.DIM}{client.system_prompt[:100]}{C.RESET}")

            else:
                print(f"{C.YELLOW}未知命令: {cmd}，输入 /help 查看帮助{C.RESET}")

            continue

        # 发送对话
        client.chat(user_input)


if __name__ == "__main__":
    main()
