#!/usr/bin/env python3
"""
Shared storage helpers for the Web client's latest video frame.
"""

import os
import tempfile


VISION_FRAME_PATH = os.path.join(
    tempfile.gettempdir(), "voice_assistant_latest_vision.jpg"
)
MAX_VISION_FRAME_BYTES = 4 * 1024 * 1024


def validate_vision_frame(content_type, frame_data):
    media_type = str(content_type or "").split(";", 1)[0].strip().lower()
    if media_type not in ("image/jpeg", "image/jpg"):
        raise ValueError("仅支持 JPEG 图片")
    if not frame_data:
        raise ValueError("图片内容为空")
    if len(frame_data) > MAX_VISION_FRAME_BYTES:
        raise ValueError("图片超过 4 MB 限制")
    if not frame_data.startswith(b"\xff\xd8\xff") or not frame_data.endswith(b"\xff\xd9"):
        raise ValueError("JPEG 图片格式无效")


def write_vision_frame_atomic(frame_data, destination=VISION_FRAME_PATH):
    directory = os.path.dirname(os.path.realpath(destination))
    os.makedirs(directory, exist_ok=True)
    file_descriptor, temporary_path = tempfile.mkstemp(
        prefix=".voice_assistant_vision_",
        suffix=".jpg.tmp",
        dir=directory,
    )
    try:
        with os.fdopen(file_descriptor, "wb") as temporary_file:
            temporary_file.write(frame_data)
        os.replace(temporary_path, destination)
        return os.stat(destination)
    except Exception:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise
