import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class DummyLogger:
    def bind(self, **kwargs):
        return self

    def info(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass


class FakeRecognizer:
    calls = []

    @classmethod
    def from_transducer(cls, **kwargs):
        cls.calls.append(("transducer", kwargs))
        return object()

    @classmethod
    def from_sense_voice(cls, **kwargs):
        cls.calls.append(("sense_voice", kwargs))
        return object()

    @classmethod
    def from_paraformer(cls, **kwargs):
        cls.calls.append(("paraformer", kwargs))
        return object()


def load_asr_module():
    fake_base = types.ModuleType("core.providers.asr.base")
    fake_base.ASRProviderBase = type(
        "ASRProviderBase", (), {"__init__": lambda self: None}
    )

    fake_dto = types.ModuleType("core.providers.asr.dto.dto")
    fake_dto.InterfaceType = types.SimpleNamespace(LOCAL="local")

    fake_logger = types.ModuleType("config.logger")
    fake_logger.setup_logging = lambda: DummyLogger()

    fake_sherpa = types.ModuleType("sherpa_onnx")
    fake_sherpa.OfflineRecognizer = FakeRecognizer

    fake_numpy = types.ModuleType("numpy")
    fake_numpy.ndarray = object

    fake_modelscope = types.ModuleType("modelscope")
    fake_modelscope_hub = types.ModuleType("modelscope.hub")
    fake_download = types.ModuleType("modelscope.hub.file_download")
    fake_download.model_file_download = lambda **kwargs: None

    stubs = {
        "core.providers.asr.base": fake_base,
        "core.providers.asr.dto.dto": fake_dto,
        "config.logger": fake_logger,
        "sherpa_onnx": fake_sherpa,
        "numpy": fake_numpy,
        "modelscope": fake_modelscope,
        "modelscope.hub": fake_modelscope_hub,
        "modelscope.hub.file_download": fake_download,
    }

    module_path = (
        Path(__file__).parents[1] / "core/providers/asr/sherpa_onnx_local.py"
    )
    spec = importlib.util.spec_from_file_location("sherpa_asr_under_test", module_path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    return module


sherpa_asr = load_asr_module()


class SherpaAsrConfigTests(unittest.TestCase):
    def setUp(self):
        FakeRecognizer.calls.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.model_dir = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def touch(self, *names):
        for name in names:
            (self.model_dir / name).touch()

    def test_transducer_passes_hotword_configuration(self):
        self.touch(
            "encoder.onnx",
            "decoder.onnx",
            "joiner.onnx",
            "tokens.txt",
            "hotwords.txt",
        )
        config = {
            "model_type": "transducer",
            "model_dir": str(self.model_dir),
            "output_dir": str(self.model_dir / "output"),
            "encoder": "encoder.onnx",
            "decoder": "decoder.onnx",
            "joiner": "joiner.onnx",
            "tokens": "tokens.txt",
            "hotwords_file": "hotwords.txt",
            "hotwords_score": 3.0,
            "max_active_paths": 6,
            "num_threads": 3,
        }

        sherpa_asr.ASRProvider(config, delete_audio_file=True)

        kind, kwargs = FakeRecognizer.calls[-1]
        self.assertEqual(kind, "transducer")
        self.assertEqual(kwargs["decoding_method"], "modified_beam_search")
        self.assertEqual(kwargs["modeling_unit"], "cjkchar")
        self.assertEqual(kwargs["hotwords_score"], 3.0)
        self.assertEqual(kwargs["max_active_paths"], 6)
        self.assertEqual(kwargs["num_threads"], 3)
        self.assertEqual(kwargs["hotwords_file"], str(self.model_dir / "hotwords.txt"))

    def test_hotwords_reject_incompatible_decoding_method(self):
        self.touch(
            "encoder-epoch-99-avg-1.onnx",
            "decoder-epoch-99-avg-1.onnx",
            "joiner-epoch-99-avg-1.onnx",
            "tokens.txt",
            "hotwords.txt",
        )
        config = {
            "model_type": "transducer",
            "model_dir": str(self.model_dir),
            "output_dir": str(self.model_dir / "output"),
            "hotwords_file": "hotwords.txt",
            "decoding_method": "greedy_search",
        }

        with self.assertRaisesRegex(ValueError, "modified_beam_search"):
            sherpa_asr.ASRProvider(config, delete_audio_file=True)

    def test_existing_sense_voice_configuration_still_works(self):
        self.touch("model.int8.onnx", "tokens.txt")
        config = {
            "model_type": "sense_voice",
            "model_dir": str(self.model_dir),
            "output_dir": str(self.model_dir / "output"),
        }

        sherpa_asr.ASRProvider(config, delete_audio_file=True)

        kind, kwargs = FakeRecognizer.calls[-1]
        self.assertEqual(kind, "sense_voice")
        self.assertEqual(kwargs["decoding_method"], "greedy_search")

    def test_domain_correction_requires_exact_text_or_control_context(self):
        self.touch("model.int8.onnx", "tokens.txt")
        config = {
            "model_type": "sense_voice",
            "model_dir": str(self.model_dir),
            "output_dir": str(self.model_dir / "output"),
            "text_corrections": [
                {
                    "source": "大市场",
                    "target": "大视场",
                    "contexts": ["切换", "白光", "热像"],
                },
                {
                    "source": "小市场",
                    "target": "小视场",
                    "contexts": ["切换", "白光", "热像"],
                },
                {
                    "source": "凝漂",
                    "target": "零漂",
                    "contexts": ["稳台", "转台", "云台"],
                },
                {
                    "source": "乙肝",
                    "target": "桅杆",
                    "match_exact": False,
                    "contexts": ["停止", "单次", "调平"],
                },
                {
                    "source": "为肝",
                    "target": "桅杆",
                    "match_exact": False,
                    "contexts": ["停止", "单次", "调平", "一键", "展开"],
                },
                {
                    "source": "唯甘",
                    "target": "桅杆",
                    "match_exact": False,
                    "contexts": ["停止", "单次", "调平", "一键", "展开"],
                },
                {
                    "source": "围杆",
                    "target": "桅杆",
                    "match_exact": False,
                    "contexts": ["停止", "单次", "调平", "一键", "展开"],
                },
                {
                    "source": "一箭",
                    "target": "一键",
                    "match_exact": False,
                    "contexts": ["桅杆", "为肝", "唯甘", "围杆", "展开", "撤收"],
                },
                {
                    "source": "单单单次",
                    "target": "单次",
                    "match_exact": False,
                    "contexts": ["桅杆", "吊瓶", "为肝", "唯甘", "围杆"],
                },
                {
                    "source": "单单次",
                    "target": "单次",
                    "match_exact": False,
                    "contexts": ["桅杆", "吊瓶", "为肝", "唯甘", "围杆"],
                },
                {
                    "source": "吊瓶",
                    "target": "调平",
                    "match_exact": False,
                    "contexts": ["单次", "桅杆"],
                },
                {
                    "source": "上殿",
                    "target": "上电",
                    "match_exact": False,
                    "contexts": ["白光", "热像", "红外", "稳台", "激光"],
                },
                {
                    "source": "同外",
                    "target": "红外",
                    "match_exact": False,
                    "contexts": ["上电", "下电", "打开", "关闭", "视场"],
                },
                {
                    "source": "调焦家家",
                    "target": "调焦加",
                    "match_exact": False,
                    "contexts": ["白光", "热像", "红外"],
                },
                {
                    "source": "调焦家加加",
                    "target": "调焦加",
                    "match_exact": False,
                    "contexts": ["白光", "热像", "红外"],
                },
                {
                    "source": "调焦家加",
                    "target": "调焦加",
                    "match_exact": False,
                    "contexts": ["白光", "热像", "红外"],
                },
                {
                    "source": "调焦加加",
                    "target": "调焦加",
                    "match_exact": False,
                    "contexts": ["白光", "热像", "红外"],
                },
                {
                    "source": "调焦增加",
                    "target": "调焦加",
                    "match_exact": False,
                    "contexts": ["白光", "热像", "红外"],
                },
                {
                    "source": "调焦茧",
                    "target": "调焦减",
                    "match_exact": False,
                    "contexts": ["白光", "热像", "红外"],
                },
                {
                    "source": "调焦剪",
                    "target": "调焦减",
                    "match_exact": False,
                    "contexts": ["白光", "热像", "红外"],
                },
                {
                    "source": "调焦键",
                    "target": "调焦减",
                    "match_exact": False,
                    "contexts": ["白光", "热像", "红外"],
                },
                {
                    "source": "调焦检",
                    "target": "调焦减",
                    "match_exact": False,
                    "contexts": ["白光", "热像", "红外"],
                },
                {
                    "source": "调焦件",
                    "target": "调焦减",
                    "match_exact": False,
                    "contexts": ["白光", "热像", "红外"],
                },
                {
                    "source": "调焦见",
                    "target": "调焦减",
                    "match_exact": False,
                    "contexts": ["白光", "热像", "红外"],
                },
                {
                    "source": "调焦减少",
                    "target": "调焦减",
                    "match_exact": False,
                    "contexts": ["白光", "热像", "红外"],
                },
            ],
        }
        provider = sherpa_asr.ASRProvider(config, delete_audio_file=True)

        self.assertEqual(provider._apply_text_corrections("大市场"), "大视场")
        self.assertEqual(
            provider._apply_text_corrections("切换大市场"), "切换大视场"
        )
        self.assertEqual(
            provider._apply_text_corrections("热像换成小市场"),
            "热像换成小视场",
        )
        self.assertEqual(
            provider._apply_text_corrections("查看全国大市场行情"),
            "查看全国大市场行情",
        )
        self.assertEqual(
            provider._apply_text_corrections("查看市场行情"), "查看市场行情"
        )
        self.assertEqual(
            provider._apply_text_corrections("转台凝漂"), "转台零漂"
        )
        self.assertEqual(
            provider._apply_text_corrections("稳台凝漂"), "稳台零漂"
        )
        self.assertEqual(
            provider._apply_text_corrections("乙肝停止"), "桅杆停止"
        )
        self.assertEqual(
            provider._apply_text_corrections("为肝一键展开"), "桅杆一键展开"
        )
        self.assertEqual(
            provider._apply_text_corrections("唯甘一箭展开"), "桅杆一键展开"
        )
        self.assertEqual(
            provider._apply_text_corrections("围杆一箭展开"), "桅杆一键展开"
        )
        self.assertEqual(
            provider._apply_text_corrections("乙肝单单单次吊瓶"),
            "桅杆单次调平",
        )
        self.assertEqual(
            provider._apply_text_corrections("乙肝单单次调平"),
            "桅杆单次调平",
        )
        self.assertEqual(
            provider._apply_text_corrections("乙肝是什么病"), "乙肝是什么病"
        )
        self.assertEqual(
            provider._apply_text_corrections("我今天要打吊瓶"), "我今天要打吊瓶"
        )
        self.assertEqual(
            provider._apply_text_corrections("我去医院检查为肝"),
            "我去医院检查为肝",
        )
        self.assertEqual(
            provider._apply_text_corrections("白光上殿红外上殿"),
            "白光上电红外上电",
        )
        self.assertEqual(
            provider._apply_text_corrections("皇帝让大臣上殿"),
            "皇帝让大臣上殿",
        )
        self.assertEqual(
            provider._apply_text_corrections("同外上电"), "红外上电"
        )
        self.assertEqual(
            provider._apply_text_corrections("他们两个求同存异"),
            "他们两个求同存异",
        )
        self.assertEqual(
            provider._apply_text_corrections("白光调焦家家"), "白光调焦加"
        )
        self.assertEqual(
            provider._apply_text_corrections("白光调焦家加加"), "白光调焦加"
        )
        self.assertEqual(
            provider._apply_text_corrections("白光调焦家加"), "白光调焦加"
        )
        self.assertEqual(
            provider._apply_text_corrections("热像调焦加加"), "热像调焦加"
        )
        self.assertEqual(
            provider._apply_text_corrections("白光调焦增加"), "白光调焦加"
        )
        self.assertEqual(
            provider._apply_text_corrections("热像调焦茧"), "热像调焦减"
        )
        self.assertEqual(
            provider._apply_text_corrections("白光调焦剪"), "白光调焦减"
        )
        self.assertEqual(
            provider._apply_text_corrections("白光调焦键"), "白光调焦减"
        )
        self.assertEqual(
            provider._apply_text_corrections("白光调焦检"), "白光调焦减"
        )
        self.assertEqual(
            provider._apply_text_corrections("热像调焦件"), "热像调焦减"
        )
        self.assertEqual(
            provider._apply_text_corrections("红外调焦见"), "红外调焦减"
        )
        self.assertEqual(
            provider._apply_text_corrections("白光调焦减少"), "白光调焦减"
        )
        self.assertEqual(
            provider._apply_text_corrections("我家家人增加了"),
            "我家家人增加了",
        )


if __name__ == "__main__":
    unittest.main()
