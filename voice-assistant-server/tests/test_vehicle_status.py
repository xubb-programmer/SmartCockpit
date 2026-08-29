import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


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


def install_requests_stub():
    requests_module = types.ModuleType("requests")
    requests_module.post = lambda *args, **kwargs: None
    requests_module.exceptions = types.SimpleNamespace(Timeout=TimeoutError)
    sys.modules["requests"] = requests_module


def load_vehicle_status_module():
    install_requests_stub()
    install_logger_stub()
    module_path = SERVER_ROOT / "plugins_func" / "functions" / "vehicle_status.py"
    spec = importlib.util.spec_from_file_location(
        "vehicle_status_under_test", module_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vehicle_status_module = load_vehicle_status_module()


class DeviceSpec:
    def __init__(self, *cn_names):
        self.cn_names = list(cn_names)


class FakeProfile:
    def __init__(self):
        self.devices = {
            "gimbal": DeviceSpec("稳台", "转台"),
            "ir_cam": DeviceSpec("热像", "红外"),
            "laser": DeviceSpec("激光", "激光器"),
            "mast": DeviceSpec("桅杆"),
            "tracker": DeviceSpec("跟踪", "跟踪器"),
            "vis_cam": DeviceSpec("白光", "可见光"),
        }

    def is_valid_device(self, device_id):
        return device_id in self.devices

    def device_cn_name(self, device_id):
        return self.devices[device_id].cn_names[0]


class FakeConnection:
    def __init__(self, query):
        self.current_user_query = query
        self.config = {"vehicle_control": {"cmd_url": "http://board:9000/command"}}
        self._vc_profile = FakeProfile()


ALL_STATUS = {
    "devices": {
        "gimbal": {"power": "on"},
        "ir_cam": {"cross": "off", "enhance": "off", "power": "on"},
        "laser": {"power": "on"},
        "mast": {"cover": "closed", "power": "off", "recon": "off"},
        "tracker": {"detect": "on", "track": "on"},
        "vis_cam": {
            "cross": "off",
            "enhance": "off",
            "fog": "off",
            "night": "off",
            "power": "on",
        },
    }
}


class VehicleStatusTests(unittest.TestCase):
    def execute(self, query, target=None, result=None):
        conn = FakeConnection(query)
        status_result = result or ALL_STATUS
        with patch.object(
            vehicle_status_module,
            "_query_status",
            return_value=status_result,
        ) as query_status:
            response = vehicle_status_module.vehicle_status(conn, target)
        return response, query_status

    def test_missing_protocol_target_queries_all_without_reading_raw_query(self):
        response, query_status = self.execute("白光什么状态")

        self.assertIsNone(query_status.call_args.args[1])
        self.assertIn("白光", response.response)
        self.assertIn("热像", response.response)

    def test_canonical_target_filters_backend_all_response(self):
        response, query_status = self.execute(
            "这段原话不参与匹配", target="vis_cam"
        )

        self.assertEqual(query_status.call_args.args[1], "vis_cam")
        self.assertIn("白光", response.response)
        self.assertNotIn("热像", response.response)
        self.assertNotIn("桅杆", response.response)

    def test_canonical_target_list_filters_multiple_devices(self):
        response, query_status = self.execute(
            "任意原话", target=["vis_cam", "ir_cam"]
        )

        self.assertIsNone(query_status.call_args.args[1])
        self.assertIn("白光", response.response)
        self.assertIn("热像", response.response)
        self.assertNotIn("稳台", response.response)

    def test_noncanonical_chinese_target_is_rejected(self):
        response, query_status = self.execute("桅杆什么状态", target="桅杆")

        query_status.assert_not_called()
        self.assertIn("当前车型不支持", response.response)

    def test_unknown_internal_fields_and_devices_do_not_leak(self):
        result = {
            "devices": {
                "mast": {
                    "cover": "closed",
                    "power": "legacy_mode",
                    "firmware_mode": "factory_debug",
                },
                "debug_device": {"power": "on"},
            }
        }
        response, _ = self.execute("所有设备什么状态", result=result)

        self.assertEqual(response.response, "桅杆顶盖关，电源未知。")
        for leaked in ("cover", "legacy", "firmware", "factory", "debug"):
            self.assertNotIn(leaked, response.response.lower())

    def test_all_status_uses_supported_chinese_punctuation(self):
        response, query_status = self.execute("所有设备什么状态", target="all")

        self.assertIsNone(query_status.call_args.args[1])
        self.assertNotIn("；", response.response)
        self.assertNotIn(";", response.response)
        self.assertTrue(response.response.endswith("。"))


if __name__ == "__main__":
    unittest.main()
