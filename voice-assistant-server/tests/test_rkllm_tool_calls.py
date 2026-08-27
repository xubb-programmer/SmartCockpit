import importlib.util
import sys
import types
import unittest
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


if __name__ == "__main__":
    unittest.main()
