import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


SERVER_ROOT = Path(__file__).parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))


def install_requests_stub():
    requests = types.ModuleType("requests")
    requests.post = lambda *args, **kwargs: None
    requests.exceptions = types.SimpleNamespace(
        ConnectionError=ConnectionError,
        Timeout=TimeoutError,
    )
    sys.modules["requests"] = requests


def install_logger_stub():
    class DummyLogger:
        def bind(self, **kwargs):
            return self

        def __getattr__(self, name):
            return lambda *args, **kwargs: None

    logger_module = types.ModuleType("config.logger")
    logger_module.setup_logging = lambda: DummyLogger()
    sys.modules["config.logger"] = logger_module


def load_vehicle_control_module():
    install_requests_stub()
    install_logger_stub()
    module_path = SERVER_ROOT / "plugins_func" / "functions" / "vehicle_control.py"
    spec = importlib.util.spec_from_file_location(
        "vehicle_control_under_test", module_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vehicle_control_module = load_vehicle_control_module()


class FakeConnection:
    def __init__(self):
        self.config = {
            "vehicle_id": "z1_001",
            "vehicle_control": {
                "all_targets": {
                    "enable": ["vis_cam", "ir_cam", "gimbal", "laser"],
                    "disable": ["vis_cam", "ir_cam", "gimbal", "laser"],
                },
                "devices": {
                    "vis_cam": {"cn_names": ["白光"], "actions": ["enable", "disable"]},
                    "ir_cam": {"cn_names": ["热像"], "actions": ["enable", "disable"]},
                    "gimbal": {"cn_names": ["转台"], "actions": ["enable", "disable"]},
                    "laser": {"cn_names": ["激光"], "actions": ["enable", "disable"]},
                    "tracker": {"cn_names": ["跟踪器"], "actions": ["detect_on"]},
                    "mast": {"cn_names": ["桅杆"], "actions": ["onekey_work"]},
                },
            },
        }
        self._vc_profile = None


class AllTargetTests(unittest.TestCase):
    def setUp(self):
        self.conn = FakeConnection()

    @staticmethod
    def successful_result(conn, vc, target, action, params=None):
        return vehicle_control_module.ActionResponse(
            action=vehicle_control_module.Action.RESPONSE,
            result=json.dumps({"target": target, "action": action}),
            response=f"{target}:{action}",
        )

    def execute(self, target, action):
        with patch.object(
            vehicle_control_module,
            "_execute_single",
            side_effect=self.successful_result,
        ) as execute_single:
            result = vehicle_control_module.vehicle_control(
                self.conn, target=target, action=action
            )
        calls = [(call.args[2], call.args[3]) for call in execute_single.call_args_list]
        return result, calls

    def test_all_alias_expands_to_configured_power_group(self):
        result, calls = self.execute("all", "enable")

        self.assertEqual(
            calls,
            [
                ("vis_cam", "enable"),
                ("ir_cam", "enable"),
                ("gimbal", "enable"),
                ("laser", "enable"),
            ],
        )
        self.assertEqual(json.loads(result.result)["batch"], 4)

    def test_full_inventory_from_model_is_treated_as_all_group(self):
        result, calls = self.execute(
            ["vis_cam", "ir_cam", "gimbal", "laser", "tracker", "mast"],
            "enable",
        )

        self.assertEqual(len(calls), 4)
        self.assertNotIn(("tracker", "enable"), calls)
        self.assertNotIn(("mast", "enable"), calls)
        self.assertEqual(json.loads(result.result)["failed"], [])

    def test_ordinary_invalid_combination_remains_atomic(self):
        result, calls = self.execute(["vis_cam", "tracker"], "enable")

        self.assertEqual(calls, [])
        self.assertEqual(result.response, "当前组合不支持统一操作")


if __name__ == "__main__":
    unittest.main()
