import unittest
from pathlib import Path


SERVER_ROOT = Path(__file__).parents[1]


class ConnectionInitializationContractTests(unittest.TestCase):
    def test_listen_waits_for_components(self):
        text = (
            SERVER_ROOT / "core" / "handle" / "textHandler"
            / "listenMessageHandler.py"
        ).read_text(encoding="utf-8")
        self.assertIn("wait_until_components_ready", text)
        self.assertIn("语音服务正在初始化，请稍后重试", text)

    def test_background_initialization_always_signals_completion(self):
        text = (SERVER_ROOT / "core" / "connection.py").read_text(encoding="utf-8")
        self.assertIn("self.components_ready_event.set()", text)
        self.assertIn("tts_open_future.result(timeout=15)", text)
        self.assertIn("asr_open_future.result(timeout=15)", text)

    def test_server_plugin_tool_order_is_stable(self):
        text = (
            SERVER_ROOT / "core" / "providers" / "tools" / "server_plugins"
            / "plugin_executor.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("list(set(", text)
        self.assertIn("dict.fromkeys(config_functions + necessary_functions)", text)

    def test_vehicle_capability_reply_is_profile_backed(self):
        intent_text = (
            SERVER_ROOT / "core" / "handle" / "intentHandler.py"
        ).read_text(encoding="utf-8")
        profile_text = (
            SERVER_ROOT / "data" / "vehicle_profiles" / "z1_scout.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn("_get_direct_capability_response", intent_text)
        self.assertIn("capability:", profile_text)


if __name__ == "__main__":
    unittest.main()
