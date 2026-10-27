"""
vehicle_status 车辆设备状态查询插件。

设计原则：
- 与 vehicle_control 平行的只读工具
- 通过 HTTP POST /status 向设备控制端查询设备实际状态
- status_url 从 cmd_url 自动推导（替换路径末段 /command → /status）
- 返回中文状态播报供 TTS 输出
"""
from __future__ import annotations

import json
import re
import requests
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, Action, ActionResponse

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

# ── 函数描述（供 LLM tool_call 解析用）────────────────────────────────────────
vehicle_status_function_desc = {
    "type": "function",
    "function": {
        "name": "vehicle_status",
        "description": (
            "查询车辆搭载设备的当前状态（白光摄像机/热像仪/稳台/转台/激光器/跟踪器等）。"
            "用户询问设备状态、是否开机、当前情况时调用。"
            "控制设备时不调用此工具，应使用 vehicle_control。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": (
                        "设备标识，如 vis_cam / ir_cam / gimbal / laser / tracker / mast。"
                        "不传或为空则查询所有设备状态。"
                    ),
                },
            },
            "required": [],
        },
    },
}

# ── 状态值中文映射 ────────────────────────────────────────────────────────────
_STATE_VALUE_CN: Dict[str, str] = {
    "on": "开",
    "off": "关",
    "open": "开",
    "closed": "关",
    "unknown": "未知",
}

# ── 状态键中文映射 ────────────────────────────────────────────────────────────
_STATE_KEY_CN: Dict[str, str] = {
    "azimuth": "方位角",
    "pitch": "俯仰角",
    "power": "电源",
    "cover": "顶盖",
    "cross": "十字丝",
    "enhance": "增强",
    "fog": "透雾",
    "night": "夜视",
    "detect": "识别",
    "track": "跟踪",
    "recon": "侦察",
}


# ── Profile 懒加载（复用 vehicle_control 的缓存）──────────────────────────────
def _get_vc_profile(conn: "ConnectionHandler"):
    """从 conn.config 提取 VehicleControlProfile，结果缓存在 conn._vc_profile。"""
    if not hasattr(conn, "_vc_profile") or conn._vc_profile is None:
        from plugins_func.functions.vehicle_profile_schema import VehicleControlProfile
        conn._vc_profile = VehicleControlProfile.from_profile(conn.config)
    return conn._vc_profile


def _derive_status_url(cmd_url: str) -> str:
    """从 cmd_url 推导 status_url：替换路径末段。"""
    # "http://x.x.x.x:9000/command" → "http://x.x.x.x:9000/status"
    if cmd_url.endswith("/command"):
        return cmd_url[:-len("/command")] + "/status"
    # 兜底：直接追加
    return cmd_url.rstrip("/") + "/status"


def _query_status(conn: "ConnectionHandler", target: str = None) -> dict:
    """
    向设备控制端查询状态。
    返回 {"devices": {dev_id: {key: value, ...}, ...}} 或 {"error": "..."}
    """
    vc_config = conn.config.get("vehicle_control") or {}
    cmd_url = vc_config.get("cmd_url")
    if cmd_url and "{local_ip}" in cmd_url:
        from core.utils.util import get_local_ip
        local_ip = get_local_ip()
        cmd_url = cmd_url.replace("{local_ip}", local_ip)
        logger.bind(tag=TAG).info(f"[STATUS] cmd_url {{local_ip}} -> {local_ip}, url={cmd_url}")

    if not cmd_url:
        return {"error": "未配置设备控制端（cmd_url），无法查询状态"}

    status_url = _derive_status_url(cmd_url)
    payload = {}
    if target:
        payload["target"] = target

    try:
        resp = requests.post(
            status_url,
            data=json.dumps(payload, ensure_ascii=False),
            headers={"Content-Type": "application/json"},
            timeout=1.0,
        )
        if resp.status_code == 200:
            body = resp.json()
            logger.bind(tag=TAG).info(
                f"[STATUS] {status_url} ← target={target or 'all'} → {body}"
            )
            return body
        else:
            logger.bind(tag=TAG).warning(
                f"[STATUS] {status_url} → HTTP {resp.status_code}: {resp.text}"
            )
            return {"error": "设备端查询失败"}
    except requests.exceptions.Timeout:
        logger.bind(tag=TAG).warning(f"[STATUS] {status_url} 超时（1s）")
        return {"error": "设备端响应超时"}
    except Exception as e:
        logger.bind(tag=TAG).error(f"[STATUS] {status_url} 请求异常: {e}")
        return {"error": "无法连接设备控制端"}


_EXT_STATE_VALUE_CN: Dict[str, str] = {
    "on": "开",
    "off": "关",
    "open": "开",
    "closed": "关",
    "unknown": "未知",
    "normal": "正常",
    "manual": "手动",
    "idle": "空闲",
    "": "未设置",
    "large": "大",
    "small": "小",
    "digital": "数字变倍",
    "in": "放大",
    "out": "缩小",
    "auto": "自动",
    "black": "黑热",
    "white": "白热",
    "up": "增加",
    "down": "降低",
    "scene": "场景校正",
    "gamma": "图像校正",
    "return": "回零",
    "drift": "零漂校正",
    "circle": "周扫中",
    "cw": "顺时针",
    "ccw": "逆时针",
    "park": "驻车展开",
    "drive": "行车展开",
    "onekey": "一键展开",
    "ended": "已撤收",
    "stopped": "已停止",
    "raised": "升起",
    "lowered": "降下",
    "single": "单次调平",
}

_EXT_STATE_KEY_CN: Dict[str, str] = {
    "azimuth": "方位角",
    "pitch": "俯仰角",
    "power": "电源",
    "cross": "十字丝",
    "enhance": "增强",
    "fog": "透雾",
    "detect": "识别",
    "track": "跟踪",
    "recon": "侦察",
    "view": "视场",
    "zoom": "变倍",
    "zoom_level": "变倍",
    "focus": "聚焦",
    "brightness": "亮度",
    "polarity": "极性",
    "contrast": "对比度",
    "mode": "模式",
    "correction": "校正",
    "zero": "零位",
    "scan": "扫描",
    "scan_direction": "方向",
    "scan_count": "圈数",
    "work": "作业",
    "mast": "桅杆",
    "level": "调平",
}

_SAFE_NUMBER_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
_CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")


def _resolve_status_scope(
    conn: "ConnectionHandler",
    vc,
    target: object,
) -> Tuple[Optional[str], Optional[List[str]]]:
    """Resolve only canonical targets emitted by the model protocol."""
    if target is None or target == "":
        return None, None

    if isinstance(target, str) and target.lower() in ("all", "*"):
        return None, None

    if isinstance(target, (list, tuple)):
        targets = list(target)
        if targets and all(
            isinstance(item, str) and vc.is_valid_device(item)
            for item in targets
        ):
            if len(targets) == 1:
                return targets[0], targets
            return None, list(dict.fromkeys(targets))
        return str(target), None

    if isinstance(target, str) and vc.is_valid_device(target):
        return target, [target]
    return str(target), None


def _safe_state_value(value: object) -> str:
    value_text = "" if value is None else str(value).strip()
    mapped = _EXT_STATE_VALUE_CN.get(
        value_text,
        _STATE_VALUE_CN.get(value_text),
    )
    if mapped is not None:
        return mapped
    if isinstance(value, bool):
        return "是" if value else "否"
    if _SAFE_NUMBER_RE.fullmatch(value_text) or _CHINESE_RE.search(value_text):
        return value_text
    return "未知"

def _should_skip_status_value(key: str, value: object, status: dict) -> bool:
    return False


def _format_status_part(key: str, value: object) -> Optional[str]:
    value_text = "" if value is None else str(value)
    value_cn = _safe_state_value(value)
    key_cn = _EXT_STATE_KEY_CN.get(key, _STATE_KEY_CN.get(key))
    if key_cn is None:
        if _CHINESE_RE.search(str(key)):
            key_cn = str(key)
        else:
            logger.bind(tag=TAG).warning(
                f"[STATUS] 跳过未映射的内部状态字段: key={key!r}, value={value!r}"
            )
            return None

    if key == "view":
        return f"视场{value_cn}"
    if key == "zoom":
        if value_text == "digital":
            return "数字变倍"
        return f"变倍{value_cn}"
    if key == "zoom_level":
        if value_text:
            return f"倍率{value_text}倍"
        return "倍率未设置"
    if key == "focus":
        if value_text == "auto":
            return "自动聚焦"
        return f"聚焦{value_cn}"
    if key == "brightness":
        return f"亮度{value_cn}"
    if key == "polarity":
        return f"极性{value_cn}"
    if key == "contrast":
        return f"对比度{value_cn}"
    if key == "mode":
        return f"{value_cn}模式"
    if key == "correction":
        if value_text in ("gamma", "scene"):
            return value_cn
        return f"校正{value_cn}"
    if key == "zero":
        if value_text in ("return", "drift"):
            return value_cn
        return f"零位{value_cn}"
    if key == "scan":
        if value_text == "circle":
            return value_cn
        return f"扫描{value_cn}"
    if key == "scan_direction":
        return f"方向{value_cn}"
    if key == "scan_count":
        if value_text:
            return f"圈数{value_text}圈"
        return "圈数未设置"
    if key == "work":
        if value_text in ("park", "drive", "onekey", "ended", "stopped"):
            return value_cn
        return f"作业{value_cn}"
    if key == "mast":
        return f"桅杆{value_cn}"
    if key == "level":
        if value_text == "single":
            return value_cn
        return f"调平{value_cn}"
    return f"{key_cn}{value_cn}"


def _format_device_status(vc, dev_id: str, status: dict) -> str:
    """将单个设备的状态字典格式化为中文播报。"""
    if not vc.is_valid_device(dev_id):
        logger.bind(tag=TAG).warning(
            f"[STATUS] 跳过车型未配置的设备状态: device={dev_id!r}"
        )
        return ""
    dev_cn = vc.device_cn_name(dev_id)
    parts = []
    for key, value in status.items():
        if _should_skip_status_value(key, value, status):
            continue
        part = _format_status_part(key, value)
        if part:
            parts.append(part)
    if not parts:
        return f"{dev_cn}无状态信息"
    return f"{dev_cn}{'，'.join(parts)}"


# ── 插件主函数 ────────────────────────────────────────────────────────────────
@register_function("vehicle_status", vehicle_status_function_desc, ToolType.SYSTEM_CTL)
def vehicle_status(
    conn: "ConnectionHandler",
    target: str = None,
) -> ActionResponse:
    vc = _get_vc_profile(conn)
    requested_target = target
    query_target, filter_targets = _resolve_status_scope(conn, vc, target)

    # 校验 target（如果指定了）
    if query_target and not vc.is_valid_device(query_target):
        dev_cn = query_target
        return ActionResponse(
            action=Action.RESPONSE,
            result=json.dumps({"error": f"未知设备 {query_target}"}, ensure_ascii=False),
            response=f"当前车型不支持{dev_cn}",
        )

    # 查询设备端
    result = _query_status(conn, query_target)

    if "error" in result:
        return ActionResponse(
            action=Action.RESPONSE,
            result=json.dumps(result, ensure_ascii=False),
            response=result["error"],
        )

    devices = result.get("devices", {})
    if filter_targets:
        allowed = set(filter_targets)
        devices = {k: v for k, v in devices.items() if k in allowed}
    # 剔除无状态信息的设备（如桅杆在无侦察状态时为空字典）
    devices = {k: v for k, v in devices.items() if v}

    if not devices:
        return ActionResponse(
            action=Action.RESPONSE,
            result=json.dumps(result, ensure_ascii=False),
            response="未获取到设备状态",
        )

    # 格式化播报
    lines = []
    for dev_id, status in devices.items():
        line = _format_device_status(vc, dev_id, status)
        if line:
            lines.append(line)

    if not lines:
        return ActionResponse(
            action=Action.RESPONSE,
            result=json.dumps(result, ensure_ascii=False),
            response="未获取到可播报的设备状态",
        )

    response = "。".join(lines) + "。"
    logger.bind(tag=TAG).info(
        f"[vehicle_status] requested={requested_target or 'all'} "
        f"query={query_target or 'all'} filter={filter_targets or 'all'} "
        f"reply={response!r}"
    )

    return ActionResponse(
        action=Action.RESPONSE,
        result=json.dumps(result, ensure_ascii=False),
        response=response,
    )
