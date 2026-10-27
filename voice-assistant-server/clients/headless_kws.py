from pathlib import Path

import numpy as np


class SherpaKeywordDetector:
    def __init__(self, config: dict, project_dir: Path):
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise RuntimeError("未安装 sherpa_onnx，无法启动唤醒词检测") from exc

        model_dir = self._resolve(project_dir, config.get("model_dir", ""))
        keywords_file = self._resolve(project_dir, config.get("keywords_file", ""))
        files = {
            "tokens": model_dir / "tokens.txt",
            "encoder": model_dir / "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
            "decoder": model_dir / "decoder-epoch-12-avg-2-chunk-16-left-64.onnx",
            "joiner": model_dir / "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
            "keywords_file": keywords_file,
        }
        missing = [str(path) for path in files.values() if not path.is_file()]
        if missing:
            raise RuntimeError("唤醒词模型文件缺失: " + ", ".join(missing))

        self.sample_rate = int(config.get("sample_rate", 16000))
        self._spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(files["tokens"]),
            encoder=str(files["encoder"]),
            decoder=str(files["decoder"]),
            joiner=str(files["joiner"]),
            num_threads=max(1, int(config.get("num_threads", 1))),
            max_active_paths=max(1, int(config.get("max_active_paths", 4))),
            keywords_file=str(files["keywords_file"]),
            keywords_score=float(config.get("keywords_score", 3.0)),
            keywords_threshold=float(config.get("keywords_threshold", 0.12)),
            num_trailing_blanks=max(1, int(config.get("num_trailing_blanks", 1))),
            provider="cpu",
        )
        self._stream = self._spotter.create_stream()

    @staticmethod
    def _resolve(project_dir: Path, value: str) -> Path:
        path = Path(str(value or ""))
        return path if path.is_absolute() else project_dir / path

    def accept_pcm(self, pcm: bytes) -> str:
        if not pcm:
            return ""
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        self._stream.accept_waveform(self.sample_rate, samples)
        result = ""
        while self._spotter.is_ready(self._stream):
            self._spotter.decode_stream(self._stream)
            current = self._spotter.get_result(self._stream)
            if current:
                result = current
                self._spotter.reset_stream(self._stream)
                break
        return result

    def reset(self) -> None:
        self._spotter.reset_stream(self._stream)
