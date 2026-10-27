import time
import wave
import os
import sys
import io
from pathlib import Path
from config.logger import setup_logging
from typing import Optional, Tuple, List
from core.providers.asr.dto.dto import InterfaceType
from core.providers.asr.base import ASRProviderBase

import numpy as np
import sherpa_onnx

from modelscope.hub.file_download import model_file_download

TAG = __name__
logger = setup_logging()
PROJECT_DIR = Path(__file__).resolve().parents[3]


# 捕获标准输出
class CaptureOutput:
    def __enter__(self):
        self._output = io.StringIO()
        self._original_stdout = sys.stdout
        sys.stdout = self._output

    def __exit__(self, exc_type, exc_value, traceback):
        sys.stdout = self._original_stdout
        self.output = self._output.getvalue()
        self._output.close()

        # 将捕获到的内容通过 logger 输出
        if self.output:
            logger.bind(tag=TAG).info(self.output.strip())


class ASRProvider(ASRProviderBase):
    def __init__(self, config: dict, delete_audio_file: bool):
        super().__init__()
        self.interface_type = InterfaceType.LOCAL
        self.model_dir = config.get("model_dir")
        self.output_dir = config.get("output_dir")
        self.model_type = str(config.get("model_type", "sense_voice")).lower()
        self.delete_audio_file = delete_audio_file
        self.text_corrections = self._parse_text_corrections(
            config.get("text_corrections", [])
        )

        if not self.model_dir:
            raise ValueError("Sherpa-ONNX ASR 未配置 model_dir")

        # 确保输出目录存在
        os.makedirs(self.output_dir, exist_ok=True)

        if self.model_type == "transducer":
            self._init_transducer(config)
        elif self.model_type in ("sense_voice", "paraformer"):
            self._init_single_model(config)
        else:
            raise ValueError(
                f"不支持的 Sherpa-ONNX 模型类型: {self.model_type}"
            )

    def _model_path(self, config: dict, key: str, default_name: str) -> str:
        value = str(config.get(key, default_name)).strip()
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = Path(self.model_dir) / path
        return str(path)

    def _auxiliary_path(self, value: str) -> str:
        if not value:
            return ""
        path = Path(value).expanduser()
        if path.is_absolute():
            return str(path)
        if path.parent == Path("."):
            return str(Path(self.model_dir) / path)
        return str(PROJECT_DIR / path)

    @staticmethod
    def _require_files(model_files: dict) -> None:
        missing = [
            f"{name}: {path}"
            for name, path in model_files.items()
            if not os.path.isfile(path)
        ]
        if missing:
            raise FileNotFoundError("Sherpa-ONNX 模型文件缺失: " + "; ".join(missing))

    @staticmethod
    def _parse_text_corrections(corrections: list) -> list:
        parsed = []
        for correction in corrections:
            if not isinstance(correction, dict):
                continue
            source = str(correction.get("source", "")).strip()
            target = str(correction.get("target", "")).strip()
            contexts = [
                str(context).strip()
                for context in correction.get("contexts", [])
                if str(context).strip()
            ]
            if source and target:
                parsed.append(
                    (
                        source,
                        target,
                        contexts,
                        bool(correction.get("match_exact", True)),
                    )
                )
        return parsed

    def _apply_text_corrections(self, text: str) -> str:
        corrected = text
        for source, target, contexts, match_exact in self.text_corrections:
            if source not in corrected:
                continue
            plain_text = corrected.strip(" \t\r\n，。！？,.!?")
            if (match_exact and plain_text == source) or any(
                context in corrected for context in contexts
            ):
                corrected = corrected.replace(source, target)

        if corrected != text:
            logger.bind(tag=TAG).info(f"ASR领域纠错: {text} -> {corrected}")
        return corrected

    def _init_single_model(self, config: dict) -> None:
        model_files = {
            "model.int8.onnx": self._model_path(config, "model", "model.int8.onnx"),
            "tokens.txt": self._model_path(config, "tokens", "tokens.txt"),
        }

        try:
            for file_name, file_path in model_files.items():
                if not os.path.isfile(file_path):
                    logger.bind(tag=TAG).info(f"正在下载模型文件: {file_name}")
                    model_file_download(
                        model_id="pengzhendong/sherpa-onnx-sense-voice-zh-en-ja-ko-yue",
                        file_path=file_name,
                        local_dir=self.model_dir,
                    )

                    if not os.path.isfile(file_path):
                        raise FileNotFoundError(f"模型文件下载失败: {file_path}")

            self.model_path = model_files["model.int8.onnx"]
            self.tokens_path = model_files["tokens.txt"]
        except Exception as e:
            logger.bind(tag=TAG).error(f"模型文件处理失败: {str(e)}")
            raise

        with CaptureOutput():
            if self.model_type == "paraformer":
                self.model = sherpa_onnx.OfflineRecognizer.from_paraformer(
                    paraformer=self.model_path,
                    tokens=self.tokens_path,
                    num_threads=2,
                    sample_rate=16000,
                    feature_dim=80,
                    decoding_method="greedy_search",
                    debug=False,
                )
            else:  # sense_voice
                self.model = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=self.model_path,
                    tokens=self.tokens_path,
                    num_threads=2,
                    sample_rate=16000,
                    feature_dim=80,
                    decoding_method="greedy_search",
                    debug=False,
                    use_itn=True,
                )

        logger.bind(tag=TAG).info(
            f"Sherpa-ONNX {self.model_type} ASR 初始化完成"
        )

    def _init_transducer(self, config: dict) -> None:
        model_files = {
            "encoder": self._model_path(
                config, "encoder", "encoder-epoch-99-avg-1.onnx"
            ),
            "decoder": self._model_path(
                config, "decoder", "decoder-epoch-99-avg-1.onnx"
            ),
            "joiner": self._model_path(
                config, "joiner", "joiner-epoch-99-avg-1.onnx"
            ),
            "tokens": self._model_path(config, "tokens", "tokens.txt"),
        }
        self._require_files(model_files)

        decoding_method = str(
            config.get("decoding_method", "modified_beam_search")
        )
        hotwords_file = self._auxiliary_path(config.get("hotwords_file", ""))
        if hotwords_file:
            self._require_files({"hotwords_file": hotwords_file})
            if decoding_method != "modified_beam_search":
                raise ValueError(
                    "Sherpa-ONNX 热词识别必须使用 modified_beam_search"
                )

        bpe_vocab = self._auxiliary_path(config.get("bpe_vocab", ""))
        if bpe_vocab:
            self._require_files({"bpe_vocab": bpe_vocab})

        with CaptureOutput():
            self.model = sherpa_onnx.OfflineRecognizer.from_transducer(
                encoder=model_files["encoder"],
                decoder=model_files["decoder"],
                joiner=model_files["joiner"],
                tokens=model_files["tokens"],
                num_threads=int(config.get("num_threads", 2)),
                sample_rate=16000,
                feature_dim=80,
                decoding_method=decoding_method,
                max_active_paths=int(config.get("max_active_paths", 4)),
                hotwords_file=hotwords_file,
                hotwords_score=float(config.get("hotwords_score", 2.0)),
                blank_penalty=float(config.get("blank_penalty", 0.0)),
                modeling_unit=str(config.get("modeling_unit", "cjkchar")),
                bpe_vocab=bpe_vocab,
                debug=bool(config.get("debug", False)),
                provider=str(config.get("provider", "cpu")),
            )

        logger.bind(tag=TAG).info(
            "Sherpa-ONNX Transducer ASR 初始化完成: "
            f"decoding={decoding_method}, hotwords={hotwords_file or '未启用'}, "
            f"score={float(config.get('hotwords_score', 2.0))}"
        )

    def read_wave(self, wave_filename: str) -> Tuple[np.ndarray, int]:
        """
        Args:
        wave_filename:
            Path to a wave file. It should be single channel and each sample should
            be 16-bit. Its sample rate does not need to be 16kHz.
        Returns:
        Return a tuple containing:
        - A 1-D array of dtype np.float32 containing the samples, which are
        normalized to the range [-1, 1].
        - sample rate of the wave file
        """

        with wave.open(wave_filename) as f:
            assert f.getnchannels() == 1, f.getnchannels()
            assert f.getsampwidth() == 2, f.getsampwidth()  # it is in bytes
            num_samples = f.getnframes()
            samples = f.readframes(num_samples)
            samples_int16 = np.frombuffer(samples, dtype=np.int16)
            samples_float32 = samples_int16.astype(np.float32)

            samples_float32 = samples_float32 / 32768
            return samples_float32, f.getframerate()

    def requires_file(self) -> bool:
        return True

    async def speech_to_text(
        self, opus_data: List[bytes], session_id: str, audio_format="opus", artifacts=None
    ) -> Tuple[Optional[str], Optional[str]]:
        """语音转文本主处理逻辑"""
        file_path = None
        try:
            if artifacts is None:
                return "", None
            file_path = artifacts.file_path

            start_time = time.monotonic()
            logger.bind(tag=TAG).info(
                f"[音频诊断][Sherpa] 开始读取音频，文件={file_path or '无'}"
            )
            s = self.model.create_stream()
            samples, sample_rate = self.read_wave(file_path)
            logger.bind(tag=TAG).info(
                f"[音频诊断][Sherpa] 音频读取完成，采样点={len(samples)}，"
                f"采样率={sample_rate}"
            )
            s.accept_waveform(sample_rate, samples)
            decode_started_at = time.monotonic()
            logger.bind(tag=TAG).info("[音频诊断][Sherpa] 开始执行decode_stream")
            self.model.decode_stream(s)
            logger.bind(tag=TAG).info(
                f"[音频诊断][Sherpa] decode_stream完成，"
                f"耗时={(time.monotonic() - decode_started_at) * 1000:.0f}毫秒"
            )
            raw_text = s.result.text
            text = self._apply_text_corrections(raw_text)
            logger.bind(tag=TAG).debug(
                f"语音识别耗时: {time.monotonic() - start_time:.3f}s | "
                f"原始结果: {raw_text} | 最终结果: {text}"
            )

            return text, file_path

        except Exception as e:
            logger.bind(tag=TAG).error(f"语音识别失败: {e}", exc_info=True)
            return "", file_path
