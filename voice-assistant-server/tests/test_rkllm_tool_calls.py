import importlib.util
import json
import sys
import types
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path


SERVER_ROOT = Path(__file__).parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))


def install_logger_stub():
    class DummyLogger:
        def bind(self, **kwargs):
            return self

        def __getattr__(self, name):
            return lambda *args, **kwargs: None

    logger_module = types.ModuleType("config.logger")
    logger_module.setup_logging = lambda: DummyLogger()
    sys.modules["config.logger"] = logger_module


def load_rkllm_module():
    requests = types.ModuleType("requests")
    requests.Session = object
    requests.exceptions = types.SimpleNamespace(
        ConnectionError=ConnectionError,
        Timeout=TimeoutError,
    )
    sys.modules["requests"] = requests
    install_logger_stub()

    module_path = SERVER_ROOT / "core" / "providers" / "llm" / "rkllm" / "rkllm.py"
    spec = importlib.util.spec_from_file_location("rkllm_under_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rkllm = load_rkllm_module()


class ToolCallExtractionTests(unittest.TestCase):
    def setUp(self):
        self.provider = object.__new__(rkllm.LLMProvider)

    def test_connection_close_sends_scoped_cancellation(self):
        self.provider.base_url = "http://127.0.0.1:18080/rkllm_chat"
        self.provider.verify_ssl = False
        response = MagicMock(status_code=200)
        with patch.object(rkllm.requests, "post", return_value=response, create=True) as post:
            self.assertTrue(self.provider.close_session("closed-session"))
        self.assertEqual(post.call_args.args[0], "http://127.0.0.1:18080/rkllm_abort")
        self.assertEqual(post.call_args.kwargs["json"], {
            "conversation_id": "closed-session", "close_session": True,
        })

    def test_finalize_preserves_multiple_tagged_calls_in_order(self):
        first = '{"name":"vehicle_control","arguments":{"target":"tracker","action":"track_on"}}'
        second = '{"name":"vehicle_control","arguments":{"target":"tracker","action":"detect_on"}}'

        result = self.provider._finalize_buffer(
            f"<tool_call>{first}</tool_call><tool_call>{second}</tool_call>"
        )

        self.assertEqual(
            result,
            (f"<tool_call>{first}</tool_call><tool_call>{second}</tool_call>", None),
        )

    def test_single_bare_json_remains_supported(self):
        tool_call = '{"name":"vehicle_control","arguments":{"target":"laser","action":"enable"}}'

        result = self.provider._finalize_buffer(tool_call)

        self.assertEqual(result, (f"<tool_call>{tool_call}</tool_call>", None))

    def test_compact_control_is_normalized_to_legacy_envelope(self):
        result = self.provider._finalize_buffer("@C|vis_cam|enable")

        expected = '{"name":"vehicle_control","arguments":{"target":"vis_cam","action":"enable"}}'
        self.assertEqual(result, (f"<tool_call>{expected}</tool_call>", None))

    def test_finalizer_discards_text_before_a_compact_tool_protocol(self):
        result = self.provider._finalize_buffer(
            "身份说明不应播报\n@C|tracker|detect_on"
        )

        expected = '{"name":"vehicle_control","arguments":{"target":"tracker","action":"detect_on"}}'
        self.assertEqual(result, (f"<tool_call>{expected}</tool_call>", None))

    def test_compact_multi_target_and_params_are_preserved(self):
        result = self.provider._finalize_buffer(
            "@C|vis_cam,ir_cam|enable\n"
            "@C|gimbal|scan_circle|direction=cw"
        )

        first = '{"name":"vehicle_control","arguments":{"target":["vis_cam","ir_cam"],"action":"enable"}}'
        second = '{"name":"vehicle_control","arguments":{"target":"gimbal","action":"scan_circle","params":{"direction":"cw"}}}'
        self.assertEqual(
            result,
            (f"<tool_call>{first}</tool_call><tool_call>{second}</tool_call>", None),
        )

    def test_digital_zoom_parameter_uses_the_fourth_protocol_segment(self):
        result = self.provider._finalize_buffer(
            "@C|vis_cam|digital_zoom|zoom_level=4"
        )

        expected = (
            '{"name":"vehicle_control","arguments":'
            '{"target":"vis_cam","action":"digital_zoom",'
            '"params":{"zoom_level":"4"}}}'
        )
        self.assertEqual(result, (f"<tool_call>{expected}</tool_call>", None))

    def test_digital_guide_preserves_decimal_angle_parameters(self):
        result = self.provider._finalize_buffer(
            "@C|gimbal|digital_guide|azimuth=82.67,pitch=10.5"
        )

        expected = (
            '{"name":"vehicle_control","arguments":'
            '{"target":"gimbal","action":"digital_guide",'
            '"params":{"azimuth":"82.67","pitch":"10.5"}}}'
        )
        self.assertEqual(result, (f"<tool_call>{expected}</tool_call>", None))

    def test_same_and_cross_device_actions_preserve_model_order(self):
        calls = self.provider._extract_tool_calls(
            "@C|vis_cam|enable\n"
            "@C|vis_cam|brightness_up\n"
            "@C|ir_cam|polarity_white\n"
            "@C|gimbal|zero_return"
        )

        actions = [
            json.loads(call)["arguments"]["action"]
            for call in calls
        ]
        self.assertEqual(
            actions,
            ["enable", "brightness_up", "polarity_white", "zero_return"],
        )

    def test_compact_status_and_other_tools_are_supported(self):
        calls = self.provider._extract_tool_calls(
            "@S|all\n@V|画面里有什么\n@M|next\n@D|all\n@X"
        )

        self.assertEqual(len(calls), 5)
        self.assertEqual(
            calls[0],
            '{"name":"vehicle_status","arguments":{}}',
        )
        self.assertIn('"name":"vision_analyze"', calls[1])
        self.assertIn('"song_name":"next"', calls[2])
        self.assertIn('"name":"get_datetime"', calls[3])
        self.assertEqual(calls[4], '{"name":"handle_exit_intent","arguments":{}}')

    def test_malformed_compact_control_is_not_executed(self):
        self.assertEqual(self.provider._extract_compact_tool_calls("@C|vis_cam"), [])
        self.assertEqual(
            self.provider._extract_compact_tool_calls(
                "@C|vis_cam|digital_zoom zoom_level=4"
            ),
            [],
        )
        self.assertEqual(
            self.provider._extract_compact_tool_calls(
                "@C|vis_cam|enable|brightness_up"
            ),
            [],
        )

    def test_streaming_punctuation_is_preserved(self):
        self.assertTrue(rkllm._is_spoken_punctuation("，"))
        self.assertTrue(rkllm._is_spoken_punctuation("。"))
        self.assertEqual(self.provider._finalize_buffer("。"), ("。", None))

    def test_english_words_are_removed_but_product_name_is_kept(self):
        self.assertEqual(
            rkllm._sanitize_spoken_text("Sure，我是Z1 assistant。"),
            "我是Z1。",
        )

    def test_payload_isolates_session_and_limits_output(self):
        self.provider.model_name = "qwen3.5-2b"
        self.provider.max_tokens = 256
        dialogue = [{"role": "system", "content": "系统"}, {"role": "user", "content": "你好"}]

        payload = self.provider._build_payload("session-a", dialogue, reset_kv=True)

        self.assertEqual(payload["conversation_id"], "session-a")
        self.assertEqual(payload["max_tokens"], 256)
        self.assertTrue(payload["reset_conversation"])


if __name__ == "__main__":
    unittest.main()
