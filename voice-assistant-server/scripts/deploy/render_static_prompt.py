#!/usr/bin/env python3
"""渲染与当前启动能力完全一致的 RKLLM 静态预热提示词。"""

import argparse
import os
import re
import sys
from pathlib import Path

from jinja2 import Template


SCRIPT_DIR = Path(__file__).resolve().parent
SERVER_DIR = SCRIPT_DIR.parent.parent


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="静态提示词输出路径")
    parser.add_argument("--vision", action="store_true", help="保留视觉分析能力")
    return parser.parse_args()


def main():
    args = parse_args()
    os.environ["VOICE_ASSISTANT_VISION_ENABLED"] = (
        "true" if args.vision else "false"
    )
    os.chdir(SERVER_DIR)
    sys.path.insert(0, str(SERVER_DIR))

    from config.config_loader import load_config

    config = load_config()
    template_path = Path(config.get("prompt_template") or "agent-base-prompt.txt")
    if not template_path.is_absolute():
        template_path = SERVER_DIR / template_path
    if not template_path.is_file():
        raise FileNotFoundError(f"提示词模板不存在: {template_path}")

    template = Template(template_path.read_text(encoding="utf-8"))
    rendered = template.render(
        base_prompt=str(config.get("prompt") or "").strip(),
        current_time="",
        today_date="",
        today_weekday="",
        lunar_date="",
        local_address="",
        weather_info="",
        emojiList="",
        device_id="",
        client_ip="",
        dynamic_context="",
        language="中文",
    )
    rendered = re.sub(r"<context>.*?</context>\s*", "", rendered, flags=re.DOTALL)
    rendered = re.sub(r"<memory>\s*</memory>\s*", "", rendered, flags=re.DOTALL)
    rendered = re.sub(r"\n{3,}", "\n\n", rendered).strip() + "\n"

    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(output_path.name + ".tmp")
    temp_path.write_text(rendered, encoding="utf-8")
    os.replace(temp_path, output_path)
    mode = "视觉" if args.vision else "纯文本"
    print(f"[Prompt] 已生成{mode}预热提示词: {output_path} ({len(rendered)} 字符)")


if __name__ == "__main__":
    main()
