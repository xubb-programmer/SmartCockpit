def normalize_tts_punctuation(text):
    """将离线中文 TTS 常见词表不支持的分号转换为自然停顿。"""
    return str(text or "").replace("；", "，").replace(";", "，")


def take_segment(text, punctuations, max_segment_chars, force=False):
    """按最早标点或硬字符上限切出一段文本。"""
    if not text:
        return None

    limit = min(len(text), max_segment_chars)
    for index, char in enumerate(text[:limit]):
        if char in punctuations or char in ("\n", "\r"):
            return text[:index + 1]

    if len(text) >= max_segment_chars:
        return text[:max_segment_chars]
    if force:
        return text
    return None
