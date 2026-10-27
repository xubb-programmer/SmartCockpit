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
        self.current_user_query = ""
        self._vc_profile = None
        self.config = {
            "vehicle_id": "z1_001",
            "vehicle_control": {
                "all_targets": {
                    "enable": [
                        "vis_cam", "ir_cam", "gimbal", "laser",
                    ],
                    "disable": [
                        "vis_cam", "ir_cam", "gimbal", "laser",
                    ],
                },
                "param_rules": {
                    "digital_zoom": {
                        "zoom_level": {
                            "allowed": ["1", "2", "4"],
                            "required": True,
                        },
                    },
                    "scan_circle": {
                        "direction": {"allowed": ["cw", "ccw"]},
                    },
                    "digital_guide": {
                        "azimuth": {"number": True, "required": True},
                        "pitch": {"number": True, "required": True},
                    },
                },
                "devices": {
                    "vis_cam": {
                        "cn_names": ["白光"],
                        "actions": [
                            "enable", "disable", "focus_in", "focus_out",
                            "brightness_up", "enhance_on", "digital_zoom",
                        ],
                    },
                    "ir_cam": {
                        "cn_names": ["热像"],
                        "actions": [
                            "enable", "disable", "brightness_up",
                            "polarity_white",
                        ],
                    },
                    "gimbal": {
                        "cn_names": ["稳台"],
                        "actions": [
                            "enable", "disable", "zero_return",
                            "scan_circle", "digital_guide", "scan_stop",
                        ],
                    },
                    "laser": {
                        "cn_names": ["激光"],
                        "actions": ["enable", "disable"],
                    },
                    "tracker": {
                        "cn_names": ["跟踪器"],
                        "actions": ["detect_on", "track_on"],
                    },
                    "mast": {
                        "cn_names": ["桅杆"],
                        "actions": ["onekey_work", "stop_work"],
                    },
                },
            },
        }


class VehicleControlTests(unittest.TestCase):
    def setUp(self):
        self.conn = FakeConnection()

    def test_tracker_responses_use_target_terms(self):
        vc = types.SimpleNamespace(device_cn_name=lambda _target: "跟踪")

        self.assertEqual(
            vehicle_control_module._build_response(vc, "tracker", "detect_on"),
            "目标检测已开启",
        )
        self.assertEqual(
            vehicle_control_module._build_response(vc, "tracker", "detect_off"),
            "目标检测已关闭",
        )
        self.assertEqual(
            vehicle_control_module._build_response(vc, "tracker", "track_on"),
            "目标跟踪已开启",
        )
        self.assertEqual(
            vehicle_control_module._build_response(
                vc, "tracker", "track_off", message="already_off"
            ),
            "目标跟踪已处于关闭状态",
        )

    @staticmethod
    def successful_result(conn, vc, target, action, params=None):
        return vehicle_control_module.ActionResponse(
            action=vehicle_control_module.Action.RESPONSE,
            result=json.dumps(
                {"target": target, "action": action, "params": params}
            ),
            response=f"{target}:{action}",
        )

    def execute(self, target, action, params=None, query="无关原话"):
        self.conn.current_user_query = query
        with patch.object(
            vehicle_control_module,
            "_execute_single",
            side_effect=self.successful_result,
        ) as execute_single:
            result = vehicle_control_module.vehicle_control(
                self.conn, target=target, action=action, params=params
            )
        calls = [
            (call.args[2], call.args[3], call.args[4])
            for call in execute_single.call_args_list
        ]
        return result, calls

    def execute_sequence(self, commands, query):
        self.conn.current_user_query = query
        with patch.object(
            vehicle_control_module,
            "_execute_single",
            side_effect=self.successful_result,
        ) as execute_single:
            results = [
                vehicle_control_module.vehicle_control(self.conn, **command)
                for command in commands
            ]
        calls = [
            (call.args[2], call.args[3])
            for call in execute_single.call_args_list
        ]
        return results, calls

    def test_all_expands_to_configured_action_group(self):
        result, calls = self.execute("all", "enable")

        self.assertEqual(
            [(target, action) for target, action, _ in calls],
            [
                ("vis_cam", "enable"),
                ("ir_cam", "enable"),
                ("gimbal", "enable"),
                ("laser", "enable"),
            ],
        )
        self.assertEqual(json.loads(result.result)["batch"], 4)

    def test_full_inventory_is_treated_as_logical_all_group(self):
        result, calls = self.execute(
            [
                "vis_cam", "ir_cam", "gimbal", "laser", "tracker", "mast",
            ],
            "disable",
        )

        self.assertEqual(len(calls), 4)
        self.assertEqual(json.loads(result.result)["failed"], [])

    def test_raw_query_does_not_override_model_protocol(self):
        result, calls = self.execute(
            "vis_cam", "focus_out", query="白光调焦加"
        )

        self.assertEqual(calls, [("vis_cam", "focus_out", None)])
        self.assertEqual(json.loads(result.result)["action"], "focus_out")

    def test_same_device_actions_execute_in_model_order(self):
        commands = [
            {"target": "vis_cam", "action": "enable"},
            {"target": "vis_cam", "action": "brightness_up"},
            {"target": "vis_cam", "action": "enhance_on"},
        ]

        _, calls = self.execute_sequence(
            commands, "白光先上电，再增加亮度并开启增强"
        )

        self.assertEqual(
            calls,
            [
                ("vis_cam", "enable"),
                ("vis_cam", "brightness_up"),
                ("vis_cam", "enhance_on"),
            ],
        )

    def test_cross_device_actions_execute_in_model_order(self):
        commands = [
            {"target": "vis_cam", "action": "enable"},
            {"target": "ir_cam", "action": "polarity_white"},
            {"target": "gimbal", "action": "zero_return"},
        ]

        _, calls = self.execute_sequence(
            commands, "白光上电，热像白热，然后稳台归零"
        )

        self.assertEqual(
            calls,
            [
                ("vis_cam", "enable"),
                ("ir_cam", "polarity_white"),
                ("gimbal", "zero_return"),
            ],
        )

    def test_same_action_multi_target_is_atomic(self):
        _, calls = self.execute(
            ["vis_cam", "ir_cam"], "brightness_up"
        )
        self.assertEqual(
            [(target, action) for target, action, _ in calls],
            [("vis_cam", "brightness_up"), ("ir_cam", "brightness_up")],
        )

    def test_unsupported_combination_is_rejected_before_dispatch(self):
        result, calls = self.execute(["vis_cam", "tracker"], "enable")

        self.assertEqual(calls, [])
        self.assertEqual(
            json.loads(result.result)["error"], "control_request_invalid"
        )

    def test_unknown_target_and_action_are_rejected(self):
        for target, action in (
            ("cover", "enable"),
            ("gimbal", "brightness_up"),
        ):
            result, calls = self.execute(target, action)
            self.assertEqual(calls, [])
            self.assertEqual(
                json.loads(result.result)["error"], "control_request_invalid"
            )

    def test_target_action_cardinality_must_match(self):
        result, calls = self.execute(
            ["vis_cam", "ir_cam"], ["enable", "disable", "enable"]
        )

        self.assertEqual(calls, [])
        self.assertEqual(
            json.loads(result.result)["error"], "control_request_invalid"
        )

    def test_digital_zoom_requires_a_valid_protocol_parameter(self):
        result, calls = self.execute("vis_cam", "digital_zoom")
        self.assertEqual(calls, [])
        self.assertEqual(
            json.loads(result.result)["error"], "control_params_invalid"
        )

        result, calls = self.execute(
            "vis_cam", "digital_zoom", {"zoom_level": "四"}
        )
        self.assertEqual(calls, [("vis_cam", "digital_zoom", {"zoom_level": "4"})])
        self.assertEqual(
            json.loads(result.result)["params"], {"zoom_level": "4"}
        )

        result, calls = self.execute(
            "vis_cam", "digital_zoom", {"zoom_level": "3"}
        )
        self.assertEqual(calls, [])
        self.assertEqual(
            json.loads(result.result)["error"], "control_params_invalid"
        )

    def test_scan_params_validate_values_without_matching_raw_query(self):
        result, calls = self.execute(
            "gimbal",
            "scan_circle",
            {"direction": "cw"},
            query="开始扫描",
        )

        self.assertEqual(
            calls,
            [("gimbal", "scan_circle", {"direction": "cw"})],
        )
        self.assertEqual(
            json.loads(result.result)["params"],
            {"direction": "cw"},
        )

        for params in ({"direction": "left"}, {"count": "3"}):
            result, calls = self.execute("gimbal", "scan_circle", params)
            self.assertEqual(calls, [])
            self.assertEqual(
                json.loads(result.result)["error"], "control_params_invalid"
            )

    def test_digital_guide_requires_numeric_azimuth_and_pitch(self):
        result, calls = self.execute(
            "gimbal",
            "digital_guide",
            {"azimuth": "82.67度", "pitch": "-10.5"},
            query="稳台转到方位八十二点六七度俯仰负十点五度",
        )

        expected_params = {"azimuth": 82.67, "pitch": -10.5}
        self.assertEqual(calls, [("gimbal", "digital_guide", expected_params)])
        self.assertEqual(json.loads(result.result)["params"], expected_params)

        for params in (
            {"azimuth": "82.67"},
            {"azimuth": "nan", "pitch": "10.5"},
            {"azimuth": "82.67", "pitch": "infinity"},
            {"azimuth": True, "pitch": "10.5"},
        ):
            result, calls = self.execute("gimbal", "digital_guide", params)
            self.assertEqual(calls, [])
            self.assertEqual(
                json.loads(result.result)["error"], "control_params_invalid"
            )

    def test_parameters_are_rejected_for_actions_without_rules(self):
        result, calls = self.execute(
            "vis_cam", "enable", {"zoom_level": "4"}
        )

        self.assertEqual(calls, [])
        self.assertEqual(
            json.loads(result.result)["error"], "control_params_invalid"
        )


if __name__ == "__main__":
    unittest.main()
