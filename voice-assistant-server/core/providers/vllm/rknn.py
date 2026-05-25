import os
import json
import base64
import requests
from config.logger import setup_logging
from core.providers.vllm.base import VLLMProviderBase

TAG = __name__
logger = setup_logging()

class VLLMProvider(VLLMProviderBase):
    def __init__(self, config):
        # 默认指向我们新创建的 rknn-vision-server
        self.base_url = config.get("base_url", "http://127.0.0.1:18090/vision/analyze")
        self.timeout = config.get("timeout", 60)
        # 固定图片存放路径（满足解耦和固定路径需求）
        self.fixed_image_path = config.get("fixed_image_path", "/tmp/voice_assistant_latest_vision.jpg")

    def response(self, question, base64_image=None, **kwargs):
        question = question + "(请使用中文回复)"
        image_path = kwargs.get("image_path")
        try:
            if image_path and os.path.exists(image_path):
                # [优化] 直接传路径，跳过 base64 编码→解码→写盘 的冗余链路
                actual_path = image_path
                logger.bind(tag=TAG).debug(f"Direct path mode: {actual_path}")
            elif base64_image:
                # 兼容模式：外部传入 base64 时仍走原逻辑
                image_data = base64.b64decode(base64_image)
                with open(self.fixed_image_path, "wb") as f:
                    f.write(image_data)
                actual_path = self.fixed_image_path
                logger.bind(tag=TAG).debug(f"Base64 mode: saved to {actual_path}")
            else:
                raise ValueError("未提供图片（image_path 或 base64_image）")

            # 告诉并行的视觉服务，去固定路径读取图片并进行推理
            payload = {
                "image_path": actual_path,
                "question": question
            }
            
            logger.bind(tag=TAG).debug(f"Calling rknn-vision-server at {self.base_url}")
            resp = requests.post(self.base_url, json=payload, timeout=self.timeout)
            
            if resp.status_code == 200:
                result = resp.json().get("result", "")
                return result
            else:
                error_msg = resp.json().get("error", "Unknown error")
                raise Exception(f"Vision Server Error: {error_msg}")

        except Exception as e:
            logger.bind(tag=TAG).error(f"Error in RKNN vision response generation: {e}")
            raise
