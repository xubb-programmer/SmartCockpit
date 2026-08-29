import importlib.util
import sys
import threading
import types
import unittest
from collections import OrderedDict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_provider_class():
    logger_module = types.ModuleType("config.logger")
    dummy_logger = types.SimpleNamespace(
        bind=lambda **kwargs: types.SimpleNamespace(
            info=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
        )
    )
    logger_module.setup_logging = lambda: dummy_logger
    sys.modules["config.logger"] = logger_module

    base_module = types.ModuleType("core.providers.tts.base")
    base_module.TTSProviderBase = object
    sys.modules["core.providers.tts.base"] = base_module

    path = ROOT / "core" / "providers" / "tts" / "sherpa_onnx.py"
    spec = importlib.util.spec_from_file_location("sherpa_tts_cache_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.TTSProvider


def make_provider(provider_class, max_entries=2):
    provider = object.__new__(provider_class)
    provider.cache_enabled = True
    provider.cache_max_entries = max_entries
    provider.cache_max_text_chars = 48
    provider._cache_namespace = ("model", "vocoder", "", 0, 1.0)
    provider_class._audio_cache = OrderedDict()
    provider_class._audio_cache_lock = threading.RLock()
    return provider


class SherpaTtsCacheTests(unittest.TestCase):
    def test_short_audio_is_cached_and_lru_bounded(self):
        provider_class = load_provider_class()
        provider = make_provider(provider_class)

        provider._put_cached_audio("白光已开机。", b"one")
        provider._put_cached_audio("热像已开机。", b"two")
        self.assertEqual(provider._get_cached_audio("白光已开机。"), b"one")

        provider._put_cached_audio("激光已开机。", b"three")
        self.assertIsNone(provider._get_cached_audio("热像已开机。"))
        self.assertEqual(provider._get_cached_audio("激光已开机。"), b"three")

    def test_long_text_is_not_cached(self):
        provider_class = load_provider_class()
        provider = make_provider(provider_class)
        text = "长" * 49

        provider._put_cached_audio(text, b"audio")

        self.assertIsNone(provider._get_cached_audio(text))


if __name__ == "__main__":
    unittest.main()
