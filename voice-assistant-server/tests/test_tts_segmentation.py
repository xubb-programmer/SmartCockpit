import sys
import unittest
from pathlib import Path


SERVER_ROOT = Path(__file__).parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from core.providers.tts.segmenter import normalize_tts_punctuation, take_segment


class TtsSegmentationTests(unittest.TestCase):
    def setUp(self):
        self.max_segment_chars = 12

    def test_prefers_early_punctuation(self):
        segment = take_segment(
            "第一句，第二句还在继续", ("，", "。"), self.max_segment_chars
        )
        self.assertEqual(segment, "第一句，")

    def test_long_text_is_hard_split(self):
        segment = take_segment(
            "一二三四五六七八九十甲乙丙丁", ("。",), self.max_segment_chars
        )
        self.assertEqual(segment, "一二三四五六七八九十甲乙")

    def test_short_remainder_waits_until_forced(self):
        self.assertIsNone(take_segment("最后一句", ("。",), self.max_segment_chars))
        self.assertEqual(
            take_segment("最后一句", ("。",), self.max_segment_chars, force=True),
            "最后一句",
        )

    def test_unsupported_semicolon_is_normalized_for_tts(self):
        self.assertEqual(
            normalize_tts_punctuation("白光电源开；热像电源关;"),
            "白光电源开，热像电源关，",
        )


if __name__ == "__main__":
    unittest.main()
