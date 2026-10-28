import unittest
import numpy as np

from clients.headless_vad import PcmSpeechDetector


class FakeSession:
    def __init__(self):
        self.inputs = []

    def run(self, _, inputs):
        self.inputs.append({key: value.copy() for key, value in inputs.items()})
        return np.array([[0.8]], dtype=np.float32), inputs["state"] + 1


class PcmSpeechDetectorTests(unittest.TestCase):
    def test_pcm_fragmentation_keeps_every_sample_and_model_context(self):
        detector = object.__new__(PcmSpeechDetector)
        detector.session = FakeSession()
        detector.reset()
        pcm = np.arange(1280, dtype=np.int16).tobytes()
        detector.probability(pcm[:800])
        self.assertEqual(len(detector.session.inputs), 0)
        self.assertAlmostEqual(detector.probability(pcm[800:]), .8)
        self.assertEqual(len(detector.session.inputs), 2)
        frames = detector.session.inputs
        reconstructed = np.concatenate([frame["input"][0, 64:] for frame in frames])
        np.testing.assert_array_equal(reconstructed, np.arange(1024, dtype=np.float32) / 32768)
        np.testing.assert_array_equal(frames[1]["input"][0, :64], frames[0]["input"][0, -64:])
        self.assertEqual(bytes(detector.pending), pcm[2048:])
        self.assertEqual(float(frames[1]["state"].mean()), 1)
