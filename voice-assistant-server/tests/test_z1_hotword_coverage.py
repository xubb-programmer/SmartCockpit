import ast
from pathlib import Path
import re


ROOT = Path(__file__).parents[1]
PROFILE_PATH = ROOT / "data" / "vehicle_profiles" / "z1_scout.yaml"
HOTWORDS_PATH = ROOT / "config" / "hotwords_cn.txt"
OTA_HANDLER_PATH = ROOT / "core" / "api" / "ota_handler.py"


ACTION_HOTWORDS = {
    "enable": ("上电",),
    "disable": ("下电",),
    "view_large": ("大视场",),
    "view_small": ("小视场",),
    "zoom_in": ("白光放大", "热像放大"),
    "zoom_out": ("白光缩小", "热像缩小"),
    "focus_auto": ("自动聚焦", "自动对焦"),
    "focus_in": ("调焦加", "聚焦加"),
    "focus_out": ("调焦减", "聚焦减"),
    "cross_show": ("十字丝",),
    "cross_hide": ("十字丝",),
    "brightness_up": ("亮度增加",),
    "brightness_down": ("亮度降低",),
    "enhance_on": ("白光增强", "热像增强"),
    "enhance_off": ("白光增强", "热像增强"),
    "fog_on": ("透雾",),
    "fog_off": ("透雾",),
    "digital_zoom": ("数字变倍",),
    "polarity_black": ("黑极性", "黑热"),
    "polarity_white": ("白极性", "白热"),
    "contrast_up": ("对比度增加",),
    "contrast_down": ("对比度降低",),
    "auto": ("自动模式",),
    "gamma_correction": ("图像校正",),
    "scene_correction": ("场景校正",),
    "zero_return": ("回零",),
    "zero_drift": ("零漂",),
    "scan_circle": ("顺时针周扫", "逆时针周扫"),
    "digital_guide": ("数字引导", "控制朝向"),
    "scan_stop": ("停止周扫", "停止扫描"),
    "detect_on": ("开启目标检测",),
    "detect_off": ("关闭目标检测",),
    "track_on": ("开启目标跟踪",),
    "track_off": ("关闭目标跟踪",),
    "park_work": ("驻车展开",),
    "drive_work": ("行车展开",),
    "onekey_work": ("一键展开",),
    "end_work": ("一键撤收",),
    "stop_work": ("桅杆停止",),
    "mast_raise": ("升桅",),
    "mast_lower": ("降桅",),
    "single_level": ("单次调平",),
    "recon_start": ("目标侦察", "开始侦察"),
}


EXPECTED_DEVICE_ACTIONS = {
    "vis_cam": {
        "enable", "disable", "view_large", "view_small", "zoom_in", "zoom_out",
        "focus_auto", "focus_in", "focus_out", "cross_show", "cross_hide",
        "brightness_up", "brightness_down", "enhance_on", "enhance_off",
        "fog_on", "fog_off", "digital_zoom",
    },
    "ir_cam": {
        "enable", "disable", "view_large", "view_small", "zoom_in", "zoom_out",
        "focus_auto", "focus_in", "focus_out", "cross_show", "cross_hide",
        "brightness_up", "brightness_down", "enhance_on", "enhance_off",
        "polarity_black", "polarity_white", "contrast_up",
        "contrast_down", "auto", "gamma_correction", "scene_correction",
    },
    "gimbal": {
        "enable", "disable", "zero_return", "zero_drift", "scan_circle",
        "digital_guide", "scan_stop",
    },
    "laser": {"enable", "disable"},
    "tracker": {"detect_on", "detect_off", "track_on", "track_off"},
    "mast": {
        "park_work", "drive_work", "onekey_work", "end_work", "stop_work",
        "mast_raise", "mast_lower", "single_level", "recon_start",
    },
}


def _load_hotwords():
    return {
        line.rsplit(":", 1)[0].strip()
        for line in HOTWORDS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def _load_devices():
    profile_text = PROFILE_PATH.read_text(encoding="utf-8")
    devices_text = profile_text.split("\n  devices:\n", 1)[1]
    devices = {}
    pattern = re.compile(
        r"^    (?P<name>[a-z_]+):\n(?P<body>.*?)(?=^    [a-z_]+:\n|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    for match in pattern.finditer(devices_text):
        body = match.group("body")
        names_match = re.search(r"^      cn_names: (?P<names>\[.*\])$", body, re.MULTILINE)
        if not names_match:
            continue
        devices[match.group("name")] = {
            "cn_names": ast.literal_eval(names_match.group("names")),
            "actions": re.findall(r"^        - ([a-z_]+)$", body, re.MULTILINE),
        }
    return devices


def _load_module_constant(path, name):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"未找到模块常量: {name}")


def test_z1_device_names_have_asr_hotwords():
    hotwords = _load_hotwords()

    for device in _load_devices().values():
        for cn_name in device["cn_names"]:
            assert cn_name in hotwords, f"Z1 设备名未配置 ASR 热词: {cn_name}"


def test_z1_actions_have_asr_hotword_coverage():
    hotwords = _load_hotwords()
    actions = {
        action
        for device in _load_devices().values()
        for action in device["actions"]
    }

    assert actions <= ACTION_HOTWORDS.keys(), (
        f"Z1 动作未定义热词覆盖: {sorted(actions - ACTION_HOTWORDS.keys())}"
    )
    for action in actions:
        expected = ACTION_HOTWORDS[action]
        assert any(term in hotwords for term in expected), (
            f"Z1 动作 {action} 缺少 ASR 热词，应至少包含: {expected}"
        )


def test_z1_protocol_action_matrix_is_complete():
    devices = _load_devices()

    assert set(devices) == set(EXPECTED_DEVICE_ACTIONS), (
        f"Z1 设备与 1501 控制协议不一致: {sorted(devices)}"
    )
    for device_id, expected_actions in EXPECTED_DEVICE_ACTIONS.items():
        actual_actions = set(devices[device_id]["actions"])
        assert actual_actions == expected_actions, (
            f"Z1 设备 {device_id} 动作与 1501 控制协议不一致，"
            f"缺少={sorted(expected_actions - actual_actions)}，"
            f"多出={sorted(actual_actions - expected_actions)}"
        )


def test_scan_directions_are_balanced_and_explicit():
    hotwords = _load_hotwords()

    for term in ("顺时针", "逆时针", "顺时针周扫", "逆时针周扫"):
        assert term in hotwords


def test_web_help_labels_cover_current_z1_actions():
    devices = _load_devices()
    action_labels = _load_module_constant(OTA_HANDLER_PATH, "_VEHICLE_ACTION_CN")
    overrides = _load_module_constant(
        OTA_HANDLER_PATH, "_VEHICLE_HELP_COMMAND_OVERRIDES"
    )
    configured_actions = {
        action for device in devices.values() for action in device["actions"]
    }
    configured_pairs = {
        (device_id, action)
        for device_id, device in devices.items()
        for action in device["actions"]
    }

    assert configured_actions <= action_labels.keys(), (
        f"Web 帮助缺少动作中文名: {sorted(configured_actions - action_labels.keys())}"
    )
    assert overrides.keys() <= configured_pairs, (
        f"Web 帮助包含车型不支持的特殊指令: {sorted(overrides.keys() - configured_pairs)}"
    )
    assert overrides[("tracker", "detect_on")] == "开启目标检测"
    assert overrides[("tracker", "track_on")] == "开启目标跟踪"
