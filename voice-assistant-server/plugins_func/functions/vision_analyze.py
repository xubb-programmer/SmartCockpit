import base64
import tempfile
import os
import time
from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, Action, ActionResponse
from core.utils.vllm import create_instance
from core.utils import textUtils
from core.utils.tts import MarkdownCleaner

TAG = __name__
logger = setup_logging()

# 阿拉伯数字 → 中文映射（Sherpa TTS 不支持数字朗读）
_DIGIT_MAP = str.maketrans("0123456789", "零一二三四五六七八九")

def _convert_digits_for_tts(text):
    """将文本中的阿拉伯数字转为中文数字，方便 TTS 朗读"""
    import re
    # 处理常见数字+单位：50米→五十米，200米→二百米
    def _num_to_cn(match):
        n = int(match.group(0))
        if n == 0: return "零"
        
        def _convert_segment(num):
            units = ["", "十", "百", "千"]
            result = ""
            s = str(num)
            for i, c in enumerate(s):
                d = int(c)
                pos = len(s) - 1 - i
                if d == 0:
                    if result and not result.endswith("零"):
                        result += "零"
                else:
                    if d == 1 and pos == 1 and i == 0:
                        result += "十"  # 10→十, 15→十五
                    else:
                        result += "零一二三四五六七八九"[d] + (units[pos] if pos < len(units) else "")
            return result.rstrip("零")
        
        if n < 10000:
            return _convert_segment(n)
        else:
            w = n // 10000
            rem = n % 10000
            res = _convert_segment(w) + "万"
            if rem > 0:
                if rem < 1000:
                    res += "零"
                res += _convert_segment(rem)
            return res
    
    # 先处理连续数字（如 50、200）
    text = re.sub(r'\d+', _num_to_cn, text)
    # 清理列表标号格式（如 "一." → ""）
    text = re.sub(r'^[一二三四五六七八九十]\.\s*', '', text, flags=re.MULTILINE)
    return text


def _sanitize_result_for_tts(text):
    """清理视觉模型返回文本，避免 Markdown、首尾符号进入 TTS/网页显示。"""
    text = MarkdownCleaner.clean_markdown(str(text or ""))
    text = _convert_digits_for_tts(text)
    return textUtils.get_string_no_punctuation_or_emoji(text)

vision_analyze_desc = {
    "type": "function",
    "function": {
        "name": "vision_analyze",
        "description": "分析车辆前方画面。当用户说'看看前面有什么'、'描述画面'、'分析图片'等指令时调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "用户对画面提出的具体问题。如果不明确，默认为战场态势分析。"
                }
            },
            "required": ["question"]
        }
    }
}

# 战场态势分析的默认提问词
DEFAULT_TACTICAL_QUESTION = (
    "用中文一句话描述当前画面中的主要目标和态势。限三十字。"
)

@register_function("vision_analyze", vision_analyze_desc, ToolType.SYSTEM_CTL)
def vision_analyze(conn, question=None):
    """
    视觉分析插件：
    从固定路径读取最新截图，调用配置的 VLLM 模块进行分析。
    """
    total_started_at = time.monotonic()
    image_age_seconds = None

    # 如果用户没有提供具体问题，则使用默认的战术分析
    if not question or question.strip() == "":
        question = DEFAULT_TACTICAL_QUESTION
    else:
        question = f"{question}。用中文一句话回答，限三十字。"
    logger.bind(tag=TAG).info(f"Triggered vision_analyze with question: {question}")
    
    # 图片来源优先级：
    # 1. 配置的 fixed_image_path（摄像头截图的保存路径）
    # 2. 配置的 test_image_path（无摄像头时的测试图片）
    # 3. /tmp 下最新的图片文件（兜底）
    vllm_cfg = conn.config.get("VLLM", {}).get("rknn_vision", {})
    image_path = vllm_cfg.get("fixed_image_path", os.path.join(tempfile.gettempdir(), "voice_assistant_latest_vision.jpg"))

    if os.path.exists(image_path):
        try:
            max_image_age_seconds = max(
                0.0,
                float(vllm_cfg.get("max_image_age_seconds", 5.0)),
            )
        except (TypeError, ValueError):
            max_image_age_seconds = 5.0
        image_age_seconds = max(0.0, time.time() - os.path.getmtime(image_path))
        if max_image_age_seconds and image_age_seconds > max_image_age_seconds:
            logger.bind(tag=TAG).warning(
                f"视觉画面已过期: path={image_path}, age={image_age_seconds:.1f}s"
            )
            return ActionResponse(
                action=Action.RESPONSE,
                result="当前视频画面未更新。",
                response="当前视频画面未更新，请确认视频正在播放。",
            )
        logger.bind(tag=TAG).info(
            f"使用视频最新帧: {image_path}, age={image_age_seconds:.1f}s"
        )
    else:
        # 尝试测试图片路径
        test_path = vllm_cfg.get("test_image_path", os.path.join(tempfile.gettempdir(), "test_vision.png"))
        if os.path.exists(test_path):
            image_path = test_path
            logger.bind(tag=TAG).info(f"使用测试图片: {image_path}")
        else:
            # 兜底：扫描 /tmp 下最新的图片
            import glob
            _tmp = tempfile.gettempdir()
            candidates = glob.glob(os.path.join(_tmp, "*.jpg")) + glob.glob(os.path.join(_tmp, "*.png")) + glob.glob(os.path.join(_tmp, "*.jpeg"))
            if candidates:
                image_path = max(candidates, key=os.path.getmtime)
                logger.bind(tag=TAG).info(f"使用最新图片: {image_path}")
            else:
                return ActionResponse(
                    action=Action.RESPONSE, 
                    result="未找到可分析的图片。", 
                    response="未找到可分析的图片，请先拍照或放置图片到指定路径。"
                )
        
    try:
        # [优化] 缓存 VLLM 实例，避免每次调用都重新创建（含 HTTP session 初始化）
        instance_started_at = time.monotonic()
        select_vllm_module = conn.config["selected_module"].get("VLLM")
        if not select_vllm_module:
            raise ValueError("未配置视觉分析模块")

        if hasattr(conn, '_vllm_instance') and conn._vllm_instance is not None:
            vllm = conn._vllm_instance
            vllm_config = conn.config["VLLM"][select_vllm_module]
            vllm_type = vllm_config.get("type", select_vllm_module)
        else:
            vllm_config = conn.config["VLLM"][select_vllm_module]
            vllm_type = vllm_config.get("type", select_vllm_module)
            vllm = create_instance(vllm_type, vllm_config)
            conn._vllm_instance = vllm
            logger.bind(tag=TAG).info(f"VLLM 实例已创建并缓存: type={vllm_type}")
        instance_elapsed_ms = int((time.monotonic() - instance_started_at) * 1000)
        
        # [优化] 本地 RKNN 模式：直接传路径，跳过 base64 编码→解码→写盘的冗余链路
        # OpenAI 等远程模式仍走 base64 兼容路径
        model_started_at = time.monotonic()
        if vllm_type in ("rknn", "rknn_vision"):
            result = vllm.response(question, image_path=image_path)
        else:
            # 远程 API 需要 base64
            with open(image_path, "rb") as f:
                image_data = f.read()
            image_base64 = base64.b64encode(image_data).decode("utf-8")
            result = vllm.response(question, image_base64)
        model_elapsed_ms = int((time.monotonic() - model_started_at) * 1000)
        
        # TTS 兼容处理：清理 Markdown/首尾符号，并将阿拉伯数字转中文。
        sanitize_started_at = time.monotonic()
        result = _sanitize_result_for_tts(result)
        sanitize_elapsed_ms = int((time.monotonic() - sanitize_started_at) * 1000)
        total_elapsed_ms = int((time.monotonic() - total_started_at) * 1000)
        age_text = "未知" if image_age_seconds is None else f"{image_age_seconds:.1f}s"
        logger.bind(tag=TAG).info(
            f"[性能][Vision插件] 总耗时={total_elapsed_ms}毫秒，"
            f"模型请求={model_elapsed_ms}毫秒，实例准备={instance_elapsed_ms}毫秒，"
            f"结果清理={sanitize_elapsed_ms}毫秒，图片age={age_text}"
        )
        
        # 直接返回给 TTS 播报
        return ActionResponse(action=Action.RESPONSE, result=result, response=result)
        
    except Exception as e:
        logger.bind(tag=TAG).error(f"Vision analyze error: {e}")
        return ActionResponse(action=Action.RESPONSE, result="视觉分析失败", response="视觉分析失败，请稍后再试。")
