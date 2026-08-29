import os
import json
import base64
import time
import requests
from config.logger import setup_logging
from core.providers.vllm.base import VLLMProviderBase

TAG = __name__
logger = setup_logging()

class VLLMProvider(VLLMProviderBase):
    def __init__(self, config):
        # 官方多模态结构由同一个 RKLLM 实例提供文本和视觉接口。
        self.base_url = config.get("base_url", "http://127.0.0.1:18080/vision/analyze")
        self.timeout = config.get("timeout", 60)
        try:
            self.max_tokens = max(16, int(config.get("max_tokens", 48)))
        except (TypeError, ValueError):
            self.max_tokens = 48
        self.session = requests.Session()
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

            # 通知当前多模态 RKLLM 实例读取固定路径图片并进行推理
            payload = {
                "image_path": actual_path,
                "question": question,
                "max_tokens": max(16, int(kwargs.get("max_tokens", self.max_tokens))),
            }
            
            logger.bind(tag=TAG).debug(f"Calling RKLLM vision endpoint at {self.base_url}")
            started_at = time.monotonic()
            resp = self.session.post(self.base_url, json=payload, timeout=self.timeout)
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            
            if resp.status_code == 200:
                result = resp.json().get("result", "")
                logger.bind(tag=TAG).info(
                    f"[性能][VLLM-RKNN] 请求耗时={elapsed_ms}毫秒，"
                    f"max_tokens={payload['max_tokens']}，输出字符={len(str(result))}"
                )
                return result
            else:
                error_msg = resp.json().get("error", "Unknown error")
                raise Exception(f"RKLLM Vision Error: {error_msg}")

        except Exception as e:
            logger.bind(tag=TAG).error(f"Error in RKNN vision response generation: {e}")
            raise

    def abort(self):
        """请求共享 RKLLM 服务取消当前视觉推理。"""
        base_url = self.base_url.rstrip("/")
        if base_url.endswith("/vision/analyze"):
            abort_url = base_url[:-len("/vision/analyze")] + "/rkllm_abort"
        else:
            abort_url = base_url + "/rkllm_abort"
        try:
            response = requests.post(
                abort_url,
                json={},
                timeout=2,
                headers={"Content-Type": "application/json"},
            )
            if response.status_code == 200:
                payload = response.json()
                logger.bind(tag=TAG).info(
                    f"视觉推理取消请求已发送: {payload.get('task', 'idle')}"
                )
                return True
            logger.bind(tag=TAG).warning(
                f"视觉推理取消失败: HTTP {response.status_code}"
            )
        except requests.RequestException as exc:
            logger.bind(tag=TAG).warning(f"视觉推理取消请求异常: {exc}")
        return False
