#!/usr/bin/env python3
"""使用项目现有 Sherpa-ONNX TTS 预生成桌面启动提示音。"""

import argparse
import asyncio
from pathlib import Path
import sys
import wave

import numpy as np
import yaml


PROMPTS = {
    "startup-ok.wav": "语音服务运行正常。",
    "startup-error.wav": "语音服务运行异常。",
    "startup-recovered.wav": "语音服务已恢复。",
    "vehicle-control-unavailable.wav": "车辆控制服务未连接。",
    "vehicle-control-recovered.wav": "车辆控制服务已恢复。",
}


def resolve_model_paths(config: dict, project_dir: Path) -> dict:
    resolved = dict(config)
    for key in (
        "vits_model",
        "matcha_model",
        "vocoder",
        "vits_tokens",
        "vits_lexicon",
        "data_dir",
        "dict_dir",
    ):
        value = resolved.get(key)
        if not value:
            continue
        path = Path(value)
        if not path.is_absolute():
            resolved[key] = str((project_dir / path).resolve())
    return resolved


async def generate_all(project_dir: Path, output_dir: Path, force: bool) -> None:
    sys.path.insert(0, str(project_dir))
    from core.providers.tts.sherpa_onnx import TTSProvider

    config_path = project_dir / "data" / ".config.yaml"
    with config_path.open("r", encoding="utf-8") as config_file:
        project_config = yaml.safe_load(config_file)

    tts_config = project_config.get("TTS", {}).get("SherpaOnnxTTS")
    if not tts_config:
        raise RuntimeError(f"未找到 SherpaOnnxTTS 配置: {config_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    provider = TTSProvider(resolve_model_paths(tts_config, project_dir), False)

    for filename, text in PROMPTS.items():
        output_file = output_dir / filename
        if output_file.exists() and not force:
            print(f"[SKIP ] 已存在: {output_file}")
            continue
        result = await provider.text_to_speak(text, str(output_file))
        if not result or not output_file.exists() or output_file.stat().st_size == 0:
            raise RuntimeError(f"生成提示音失败: {filename}")
        convert_to_stereo(output_file)
        print(f"[INFO ] 已生成: {filename} ({text})")


def convert_to_stereo(output_file: Path) -> None:
    """板载 ES8388 只接受双声道，预生成时完成单声道复制。"""
    with wave.open(str(output_file), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        frames = wav_file.readframes(wav_file.getnframes())

    if channels == 2:
        return
    if channels != 1 or sample_width != 2:
        raise RuntimeError(
            f"不支持的提示音格式: channels={channels}, sample_width={sample_width}"
        )

    mono = np.frombuffer(frames, dtype="<i2")
    stereo = np.repeat(mono[:, np.newaxis], 2, axis=1).reshape(-1)
    with wave.open(str(output_file), "wb") as wav_file:
        wav_file.setnchannels(2)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(stereo.astype("<i2", copy=False).tobytes())


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    project_dir = script_dir.parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=project_dir / "assets" / "startup-audio",
        help="提示音输出目录",
    )
    parser.add_argument("--force", action="store_true", help="覆盖已有提示音")
    args = parser.parse_args()
    asyncio.run(generate_all(project_dir, args.output_dir.resolve(), args.force))


if __name__ == "__main__":
    main()
