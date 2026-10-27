import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


SERVER_ROOT = Path(__file__).parents[1]


def load_config_loader_module():
    manage_api_client = types.ModuleType("config.manage_api_client")
    manage_api_client.init_service = lambda *_args, **_kwargs: None
    manage_api_client.get_server_config = lambda *_args, **_kwargs: None
    manage_api_client.get_agent_models = lambda *_args, **_kwargs: None
    yaml_module = types.ModuleType("yaml")
    yaml_module.safe_load = lambda *_args, **_kwargs: {}

    sys.path.insert(0, str(SERVER_ROOT))
    original = sys.modules.get("config.manage_api_client")
    original_yaml = sys.modules.get("yaml")
    sys.modules["config.manage_api_client"] = manage_api_client
    sys.modules["yaml"] = yaml_module
    try:
        module_path = SERVER_ROOT / "config" / "config_loader.py"
        spec = importlib.util.spec_from_file_location(
            "config_loader_runtime_capability_test",
            module_path,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SERVER_ROOT))
        if original is None:
            sys.modules.pop("config.manage_api_client", None)
        else:
            sys.modules["config.manage_api_client"] = original
        if original_yaml is None:
            sys.modules.pop("yaml", None)
        else:
            sys.modules["yaml"] = original_yaml


config_loader = load_config_loader_module()


def make_config():
    return {
        "selected_module": {"Intent": "function_call"},
        "Intent": {
            "function_call": {
                "functions": ["vehicle_control", "vision_analyze", "play_music"]
            }
        },
        "prompt": (
            "控制：@C|target|action\n"
            "视觉：@V|问题\n"
            "明确要求分析画面用@V；播放音乐用@M。\n"
        ),
        "tool_calling_rules": "设备控制输出@C，视觉分析输出@V，播放音乐输出@M。",
        "capability_summary": "我可以控制设备、播放音乐、分析前方画面。",
    }


class RuntimeVisionCapabilityTests(unittest.TestCase):
    def test_disabled_vision_is_removed_from_tools_and_prompts(self):
        with patch.dict(os.environ, {"VOICE_ASSISTANT_VISION_ENABLED": "false"}):
            config = config_loader._apply_runtime_capabilities(make_config())

        self.assertFalse(config["runtime_capabilities"]["vision_enabled"])
        self.assertNotIn(
            "vision_analyze",
            config["Intent"]["function_call"]["functions"],
        )
        self.assertNotIn("@V", config["prompt"])
        self.assertNotIn("@V", config["tool_calling_rules"])
        self.assertNotIn("分析前方画面", config["capability_summary"])

    def test_enabled_vision_keeps_tool_and_prompts(self):
        with patch.dict(os.environ, {"VOICE_ASSISTANT_VISION_ENABLED": "true"}):
            config = config_loader._apply_runtime_capabilities(make_config())

        self.assertTrue(config["runtime_capabilities"]["vision_enabled"])
        self.assertIn(
            "vision_analyze",
            config["Intent"]["function_call"]["functions"],
        )
        self.assertIn("@V", config["prompt"])
        self.assertIn("分析前方画面", config["capability_summary"])

    def test_intent_handler_has_fixed_disabled_response(self):
        source = (
            SERVER_ROOT / "core" / "handle" / "intentHandler.py"
        ).read_text(encoding="utf-8")

        self.assertIn('"分析前方画面"', source)
        self.assertIn('speak_txt(conn, "当前未启用视觉分析功能。")', source)

    def test_startup_log_reflects_disabled_vision(self):
        source = (SERVER_ROOT / "app.py").read_text(encoding="utf-8")

        self.assertIn("视觉分析功能未启用，未向模型注册视觉工具", source)

    def test_reconnaissance_prompt_maps_to_mast(self):
        source = (
            SERVER_ROOT / "data" / "vehicle_profiles" / "z1_scout.yaml"
        ).read_text(encoding="utf-8")

        self.assertIn("目标侦察=>@C|mast|recon_start", source)

    def test_mast_onekey_actions_are_explicitly_distinguished(self):
        source = (
            SERVER_ROOT / "data" / "vehicle_profiles" / "z1_scout.yaml"
        ).read_text(encoding="utf-8")

        self.assertIn("桅杆一键展开=>@C|mast|onekey_work", source)
        self.assertIn("桅杆一键撤收=>@C|mast|end_work", source)
        self.assertNotIn("一键展开撤收", source)


if __name__ == "__main__":
    unittest.main()
