"""Local speech detection on PCM, independent of the manual server endpoint."""
from pathlib import Path

import numpy as np


class PcmSpeechDetector:
    def __init__(self, model_path: Path):
        import onnxruntime

        if not model_path.is_file():
            raise RuntimeError(f"本地语音检测模型缺失: {model_path}")
        options = onnxruntime.SessionOptions()
        options.inter_op_num_threads = options.intra_op_num_threads = 1
        self.session = onnxruntime.InferenceSession(
            str(model_path), providers=["CPUExecutionProvider"], sess_options=options
        )
        self.reset()

    def reset(self):
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros((1, 64), dtype=np.float32)
        self.pending = bytearray()
        self.last_probability = 0.0

    def probability(self, pcm: bytes) -> float:
        self.pending.extend(pcm)
        probabilities = []
        while len(self.pending) >= 1024:
            frame = bytes(self.pending[:1024])
            del self.pending[:1024]
            samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
            audio = np.concatenate([self.context, samples.reshape(1, -1)], axis=1)
            result, self.state = self.session.run(None, {
                "input": audio, "state": self.state, "sr": np.array(16000, dtype=np.int64),
            })
            self.context = audio[:, -64:]
            probabilities.append(float(result.item()))
        if probabilities:
            self.last_probability = max(probabilities)
        return self.last_probability
