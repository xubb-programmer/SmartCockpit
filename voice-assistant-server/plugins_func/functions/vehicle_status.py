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
import requests
from typing import Dict, TYPE_CHECKING

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
            "查询车辆搭载设备的当前状态（白光摄像机/热像仪/转台/激光器/跟踪器等）。"
            "用户询问设备状态、是否开机、当前情况时调用。"
            "控制设备时不调用此工具，应使用 vehicle_control。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": (
                        "设备标识，如 vis_cam / ir_cam / gimbal / laser / tracker。"
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
    "power": "电源",
    "cross": "十字丝",
    "enhance": "增强",
    "fog": "透雾",
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


def _format_device_status(vc, dev_id: str, status: dict) -> str:
    """将单个设备的状态字典格式化为中文播报。"""
    dev_cn = vc.device_cn_name(dev_id)
    parts = []
    for key, value in status.items():
        key_cn = _STATE_KEY_CN.get(key, key)
        val_cn = _STATE_VALUE_CN.get(value, value)
        parts.append(f"{key_cn}{val_cn}")
    if not parts:
        return f"{dev_cn}无状态信息"
    return f"{dev_cn}：{'，'.join(parts)}"


# ── 插件主函数 ────────────────────────────────────────────────────────────────
@register_function("vehicle_status", vehicle_status_function_desc, ToolType.SYSTEM_CTL)
def vehicle_status(
    conn: "ConnectionHandler",
    target: str = None,
) -> ActionResponse:
    vc = _get_vc_profile(conn)

    # 校验 target（如果指定了）

    if target and not vc.is_valid_device(target):
        valid_devices = list(vc.devices.keys())
        dev_cn = target
        return ActionResponse(
            action=Action.RESPONSE,
            result=json.dumps({"error": f"未知设备 {target}"}, ensure_ascii=False),
            response=f"当前车型不支持{dev_cn}",
        )

    # 查询设备端
    result = _query_status(conn, target)

    if "error" in result:
        return ActionResponse(
            action=Action.RESPONSE,
            result=json.dumps(result, ensure_ascii=False),
            response=result["error"],
        )

    devices = result.get("devices", {})
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
        lines.append(_format_device_status(vc, dev_id, status))

    response = "；".join(lines)
    logger.bind(tag=TAG).info(f"[vehicle_status] target={target or 'all'} reply={response!r}")

    return ActionResponse(
        action=Action.RESPONSE,
        result=json.dumps(result, ensure_ascii=False),
        response=response,
    )
