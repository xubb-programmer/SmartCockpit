import os
import wave
import traceback
import asyncio
import time
import threading
from collections import OrderedDict
from config.logger import setup_logging
from core.providers.tts.base import TTSProviderBase

TAG = __name__
logger = setup_logging()

class TTSProvider(TTSProviderBase):
    _audio_cache = OrderedDict()
    _audio_cache_lock = threading.RLock()

    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        
        self.vits_model = config.get("vits_model", "")
        self.matcha_model = config.get("matcha_model", "")
        self.vocoder = config.get("vocoder", "")
        self.vits_tokens = config.get("vits_tokens", "")
        self.vits_lexicon = config.get("vits_lexicon", "")
        self.data_dir = config.get("data_dir", "")
        self.dict_dir = config.get("dict_dir", "")
        self.num_threads = config.get("num_threads", 2)
        self.speed = float(config.get("speed", 1.0))
        self.sid = int(config.get("sid", 0))
        self.cache_enabled = str(config.get("cache_enabled", True)).lower() in (
            "true", "1", "yes", "on"
        )
        self.cache_max_entries = max(8, int(config.get("cache_max_entries", 96)))
        self.cache_max_text_chars = max(8, int(config.get("cache_max_text_chars", 48)))
        self._cache_namespace = (
            self.matcha_model,
            self.vocoder,
            self.vits_model,
            self.sid,
            self.speed,
        )
        
        try:
            import sherpa_onnx
            
            # 初始化 Sherpa-ONNX TTS 配置 (支持 VITS 和 Matcha 自适应)
            vits_config = None
            if self.vits_model:
                vits_config = sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=self.vits_model,
                    lexicon=self.vits_lexicon,
                    tokens=self.vits_tokens,
                )
            
            matcha_config = None
            if self.matcha_model and self.vocoder:
                matcha_params = dict(
                    acoustic_model=self.matcha_model,
                    vocoder=self.vocoder,
                    lexicon=self.vits_lexicon,
                    tokens=self.vits_tokens,
                    data_dir=self.data_dir,
                )
                if config.get("use_dict_dir", False) and self.dict_dir:
                    matcha_params["dict_dir"] = self.dict_dir
                matcha_config = sherpa_onnx.OfflineTtsMatchaModelConfig(
                    **matcha_params
                )

            model_params = {
                "provider": "cpu",
                "num_threads": self.num_threads,
                "debug": False,
            }
            if vits_config:
                model_params["vits"] = vits_config
            if matcha_config:
                model_params["matcha"] = matcha_config

            tts_config = sherpa_onnx.OfflineTtsConfig(
                model=sherpa_onnx.OfflineTtsModelConfig(**model_params),
                max_num_sentences=1,
            )
            
            if not tts_config.validate():
                logger.bind(tag=TAG).error("Sherpa-ONNX TTS 配置验证失败，请检查模型文件路径。")
                raise ValueError("Invalid Sherpa-ONNX TTS configuration")
                
            self.tts = sherpa_onnx.OfflineTts(tts_config)
            self.sample_rate = self.tts.sample_rate
            mode_str = "Matcha模式" if matcha_config else "VITS模式"
            logger.bind(tag=TAG).info(f"Sherpa-ONNX TTS 初始化成功, 采样率: {self.sample_rate} ({mode_str})")
            
        except ImportError:
            logger.bind(tag=TAG).error("未安装 sherpa_onnx 库，请运行: pip install sherpa-onnx")
            raise
        except Exception as e:
            logger.bind(tag=TAG).error(f"初始化 Sherpa-ONNX TTS 失败: {e}\n{traceback.format_exc()}")
            raise

    def _cache_key(self, text):
        return self._cache_namespace + (text,)

    def _get_cached_audio(self, text):
        if not self.cache_enabled or len(text) > self.cache_max_text_chars:
            return None
        key = self._cache_key(text)
        with self._audio_cache_lock:
            audio_bytes = self._audio_cache.get(key)
            if audio_bytes is not None:
                self._audio_cache.move_to_end(key)
            return audio_bytes

    def _put_cached_audio(self, text, audio_bytes):
        if (
            not self.cache_enabled
            or not audio_bytes
            or len(text) > self.cache_max_text_chars
        ):
            return
        key = self._cache_key(text)
        with self._audio_cache_lock:
            self._audio_cache[key] = audio_bytes
            self._audio_cache.move_to_end(key)
            while len(self._audio_cache) > self.cache_max_entries:
                self._audio_cache.popitem(last=False)

    async def text_to_speak(self, text, output_file):
        try:
            import numpy as np

            synthesis_started_at = time.monotonic()
            spoken_text = text.replace("Z1", "Z一").replace("z1", "Z一")

            cached_audio = self._get_cached_audio(spoken_text)
            if cached_audio is not None:
                elapsed_ms = (time.monotonic() - synthesis_started_at) * 1000
                logger.bind(tag=TAG).info(
                    f"[性能][TTS] 缓存命中，耗时={elapsed_ms:.1f}毫秒，"
                    f"字节={len(cached_audio)}"
                )
                if output_file:
                    with open(output_file, "wb") as file_obj:
                        file_obj.write(cached_audio)
                    return output_file
                return cached_audio

            logger.bind(tag=TAG).info(
                f"[音频诊断][TTS合成] 输入文本={text!r}"
            )
            # 使用 Sherpa-ONNX 生成音频
            audio = self.tts.generate(spoken_text, sid=self.sid, speed=self.speed)
            
            if not audio or len(audio.samples) == 0:
                logger.bind(tag=TAG).error(f"Sherpa-ONNX 生成音频为空: {text}")
                return None

            # 将 float32 类型的音频样本(-1.0 到 1.0) 转换为 int16 格式 PCM
            samples_int16 = (np.array(audio.samples) * 32767).astype(np.int16)
            pcm_bytes = samples_int16.tobytes()
            audio_seconds = len(samples_int16) / self.sample_rate
            synthesis_seconds = time.monotonic() - synthesis_started_at
            logger.bind(tag=TAG).info(
                f"[音频诊断][TTS合成] 生成样本={len(samples_int16)}，"
                f"采样率={self.sample_rate}Hz，时长={audio_seconds:.3f}秒"
            )
            logger.bind(tag=TAG).info(
                f"[性能][TTS] 合成耗时={synthesis_seconds * 1000:.0f}毫秒，"
                f"音频时长={audio_seconds:.3f}秒，实时率="
                f"{synthesis_seconds / max(audio_seconds, 0.001):.2f}"
            )

            if output_file:
                # 写入到 WAV 文件
                with wave.open(output_file, "w") as f:
                    f.setnchannels(1)
                    f.setsampwidth(2) # 16-bit
                    f.setframerate(self.sample_rate)
                    f.writeframes(pcm_bytes)
                return output_file
            else:
                import io
                wav_buffer = io.BytesIO()
                with wave.open(wav_buffer, "wb") as f:
                    f.setnchannels(1)
                    f.setsampwidth(2) # 16-bit
                    f.setframerate(self.sample_rate)
                    f.writeframes(pcm_bytes)
                wav_bytes = wav_buffer.getvalue()
                self._put_cached_audio(spoken_text, wav_bytes)
                return wav_bytes

        except Exception as e:
            logger.bind(tag=TAG).error(f"Sherpa-ONNX TTS 生成失败: {e}\n{traceback.format_exc()}")
            return None
