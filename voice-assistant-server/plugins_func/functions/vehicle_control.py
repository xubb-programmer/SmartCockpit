"""
vehicle_control 车辆设备控制插件（闭环版）。

设计原则：
- target 合法集合 & action 白名单 来自 VehicleControlProfile（随车型 YAML 变化）
- 通过 HTTP POST 将 JSON 指令下发到设备控制端（cmd_url）
- 协议格式：{"type":"command","target":"...","action":"...","params":{...}}
- 设备端返回 {"success": bool, "state": str, "message": str}
- 根据响应生成差异化语音回复（已开机 / 已处于开机状态 / 开机失败）
"""
from __future__ import annotations

import json
import asyncio
import requests
from typing import Dict, Tuple, TYPE_CHECKING

from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, Action, ActionResponse

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

# ── 函数描述（供 RKLLM tool_call 解析用）─────────────────────────────────────
vehicle_control_function_desc = {
    "type": "function",
    "function": {
        "name": "vehicle_control",
        "description": (
            "控制车辆搭载的设备（白光摄像机/热像仪/转台/激光器/跟踪器/桅杆等）。"
            "只有用户明确下达控制指令时才调用，疑问句和状态查询不调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "anyOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": (
                        "设备标识：vis_cam / ir_cam / gimbal / laser / tracker / mast。"
                        "控制多个设备时传字符串数组；禁止使用 all。"
                        "数组中每个设备都必须支持该动作，否则整条指令不执行。"
                    ),
                },
                "action": {
                    "type": "string",
                    "description": (
                        "动作标识，如 enable / disable / zero_return / scan_circle / recon_start 等。"
                        "只能使用该设备支持的动作。"
                    ),
                },
                "params": {
                    "type": "object",
                    "description": (
                        "可选参数字典，仅传用户明确说出的值，不自行编造。所有参数值为字符串类型。"
                        "已知参数："
                        "digital_zoom 动作可传 zoom_level（倍数，如 '1'/'2'/'4'）；"
                        "scan_circle 动作可传 direction（方向，'cw'顺时针/'ccw'逆时针）、count（圈数）；"
                    ),
                },
            },
            "required": ["target", "action"],
        },
    },
}

# ── 动作中文回复模板 ──────────────────────────────────────────────────────────
_ACTION_CN: Dict[str, str] = {
    # 通用
    "enable":           "已开机",
    "disable":          "已关机",
    # 视场
    "view_large":       "大视场已切换",
    "view_small":       "小视场已切换",
    # 变倍
    "zoom_in":          "正在放大",
    "zoom_out":         "正在缩小",
    # 对焦
    "focus_auto":       "自动对焦中",
    "focus_in":         "调焦加",
    "focus_out":        "调焦减",
    # 十字丝
    "cross_show":       "十字丝已开启",
    "cross_hide":       "十字丝已关闭",
    # 亮度
    "brightness_up":    "亮度已增加",
    "brightness_down":  "亮度已降低",
    # 增强
    "enhance_on":       "增强已开启",
    "enhance_off":      "增强已关闭",
    # 透雾
    "fog_on":           "透雾已开启",
    "fog_off":          "透雾已关闭",
    # 数字变倍
    "digital_zoom":     "数字变倍调整中",
    # 热像 极性
    "polarity_black":   "已切换黑热",
    "polarity_white":   "已切换白热",
    # 热像 对比度
    "contrast_up":      "对比度已增加",
    "contrast_down":    "对比度已降低",
    "auto":             "已切换自动模式",
    # 热像 校正
    "gamma_correction": "图像校正中",
    "scene_correction": "场景校正中",
    # 转台
    "zero_return":      "归零中",
    "zero_drift":       "零漂校正中",
    "scan_circle":      "开始周扫",

    "scan_stop":        "扫描已停止",
    # 跟踪器
    "detect_on":        "识别已开启",
    "detect_off":       "识别已关闭",
    "track_on":         "已开启",
    "track_off":        "已关闭",
    # 桅杆
    "park_work":        "驻车展开中",
    "drive_work":       "行车展开中",
    "onekey_work":      "一键展开中",
    "end_work":         "正在撤收",
    "stop_work":        "已停止",
    "mast_raise":       "正在举升",
    "mast_lower":       "正在下降",
    "single_level":     "单次调平中",

    "recon_start":      "开始目标侦察",
}


# ── Profile 懒加载（缓存在 conn 上，避免重复解析 YAML）────────────────────────
def _get_vc_profile(conn: "ConnectionHandler"):
    """从 conn.config 提取 VehicleControlProfile，结果缓存在 conn._vc_profile。"""
    if not hasattr(conn, "_vc_profile") or conn._vc_profile is None:
        from plugins_func.functions.vehicle_profile_schema import VehicleControlProfile
        conn._vc_profile = VehicleControlProfile.from_profile(conn.config)
    return conn._vc_profile


# ── HTTP 指令下发（闭环版）────────────────────────────────────────────────────
def _send_command(conn: "ConnectionHandler", cmd: dict) -> Tuple[bool, str, str]:
    """
    通过 HTTP POST 将指令发送到设备控制端。
    目标地址从 conn.config["vehicle_control"]["cmd_url"] 读取。
    超时 1.0s，防止阻塞语音链路。

    返回 (success, state, message)：
      success: 设备端是否成功执行
      state:   执行后设备实际状态（如 "on"/"off"/"ok"）
      message: 附加信息（"" / "already_on" / "already_off" / 错误原因）
    """
    vc_config = conn.config.get("vehicle_control") or {}
    cmd_url = vc_config.get("cmd_url")
    if cmd_url and "{local_ip}" in cmd_url:
        from core.utils.util import get_local_ip
        local_ip = get_local_ip()
        cmd_url = cmd_url.replace("{local_ip}", local_ip)
        logger.bind(tag=TAG).info(f"[CMD] cmd_url {{local_ip}} -> {local_ip}, url={cmd_url}")

    if not cmd_url:
        # 无 cmd_url 配置时，仅打印模拟（Phase 1 验证用）
        payload = json.dumps(cmd, ensure_ascii=False)
        logger.bind(tag=TAG).info(f"[CMD MOCK] {payload}")
        print(f"[CMD MOCK] payload={payload}")
        return (True, "ok", "")

    payload = json.dumps(cmd, ensure_ascii=False)
    try:
        resp = requests.post(
            cmd_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            timeout=1.0,
        )
        if resp.status_code == 200:
            # 解析设备端返回的 JSON（兼容旧设备端无 body 的情况）
            try:
                body = resp.json()
                success = body.get("success", True)
                state = body.get("state", "ok")
                message = body.get("message", "")
            except (ValueError, AttributeError):
                # 旧设备端无 JSON body，退化为原行为
                success, state, message = True, "ok", ""
            logger.bind(tag=TAG).info(
                f"[CMD] {cmd_url} ← {payload} → success={success} state={state} msg={message}"
            )
            return (success, state, message)
        else:
            logger.bind(tag=TAG).warning(
                f"[CMD] {cmd_url} ← {payload} → HTTP {resp.status_code}: {resp.text}"
            )
            return (False, "unknown", f"HTTP {resp.status_code}")
    except requests.exceptions.Timeout:
        logger.bind(tag=TAG).warning(f"[CMD] {cmd_url} 超时（1s）")
        return (False, "unknown", "timeout")
    except Exception as e:
        logger.bind(tag=TAG).error(f"[CMD] {cmd_url} 请求异常: {e}")
        return (False, "unknown", str(e))


# ── WebSocket 前端通知 ─────────────────────────────────────────────────────────
def _notify_frontend(conn: "ConnectionHandler", cmd: dict, state: str = ""):
    """
    通过 WebSocket 向前端推送车辆控制指令通知。
    协议格式（device_protocol.md 第六节）：
      {"type": "vehicle_cmd", "device": "...", "action": "...", "state": "...", "params": {...}}
    注意字段名与 HTTP 不同：type=vehicle_cmd, device（非 target）。
    """
    ws_msg = {
        "type": "vehicle_cmd",
        "device": cmd.get("target", ""),
        "action": cmd.get("action", ""),
    }
    if state:
        ws_msg["state"] = state
    if "params" in cmd:
        ws_msg["params"] = cmd["params"]

    try:
        if conn.loop and not conn.loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                conn.websocket.send(json.dumps(ws_msg)),
                conn.loop
            )
            logger.bind(tag=TAG).debug(f"[WS] 前端通知: {ws_msg}")
        else:
            logger.bind(tag=TAG).debug("[WS] 事件循环未就绪，跳过前端通知")
    except Exception as e:
        # WebSocket 通知失败不影响主流程
        logger.bind(tag=TAG).warning(f"[WS] 前端通知失败（忽略）: {e}")


# ── "已处于目标状态"时的中文回复模板（仅有状态动作需要）───────────────────────
_ALREADY_CN: Dict[str, str] = {
    "enable":       "开机",
    "disable":      "关机",
    "cross_show":   "十字丝开启",
    "cross_hide":   "十字丝关闭",
    "enhance_on":   "增强开启",
    "enhance_off":  "增强关闭",
    "fog_on":       "透雾开启",
    "fog_off":      "透雾关闭",
    "detect_on":    "识别开启",
    "detect_off":   "识别关闭",
    "track_on":     "跟踪开启",
    "track_off":    "跟踪关闭",
    "recon_start":  "侦察",
}


# ── 语音回复生成（差异化）─────────────────────────────────────────────────────
def _build_response(vc, target: str, action: str, message: str = "") -> str:
    """
    生成≤15字的简洁中文回复。
    根据设备端返回的 message 区分：正常执行 / 已是目标状态 / 失败。
    """
    dev_cn = vc.device_cn_name(target)
    act_cn = _ACTION_CN.get(action, "操作中")

    if message.startswith("already_"):
        # 设备已处于目标状态，使用专用模板生成更自然的回复
        already_label = _ALREADY_CN.get(action, act_cn.replace("已", ""))
        return f"{dev_cn}已处于{already_label}状态"

    return f"{dev_cn}{act_cn}"


# ── 单设备执行（内部逻辑，不经过装饰器）────────────────────────────────────────
def _execute_single(
    conn: "ConnectionHandler",
    vc,
    target: str,
    action: str,
    params: dict = None,
) -> ActionResponse:
    """执行单个设备的控制指令，供主函数和批量分支共用。"""
    # 校验 target
    if not vc.is_valid_device(target):
        valid_devices = list(vc.devices.keys())
        msg = f"设备 '{target}' 不在当前车型支持列表 {valid_devices}"
        logger.bind(tag=TAG).warning(f"[vehicle_control] {msg}")
        return ActionResponse(
            action=Action.RESPONSE,
            result=json.dumps({"error": msg}, ensure_ascii=False),
            response="当前车型不支持该设备",
        )

    # 校验 action
    if not vc.is_valid_action(target, action):
        spec = vc.devices[target]
        msg = f"设备 '{target}' 不支持动作 '{action}'，支持：{spec.actions}"
        logger.bind(tag=TAG).warning(f"[vehicle_control] {msg}")
        dev_cn = vc.device_cn_name(target)
        return ActionResponse(
            action=Action.RESPONSE,
            result=json.dumps({"error": msg}, ensure_ascii=False),
            response=f"{dev_cn}不支持该操作",
        )

    # 构建协议指令
    cmd = {"type": "command", "target": target, "action": action}
    if params:
        cmd["params"] = params

    # HTTP 下发（闭环：解析设备端响应）
    success, state, message = _send_command(conn, cmd)
    if not success:
        dev_cn = vc.device_cn_name(target)
        act_cn = _ACTION_CN.get(action, "操作")
        fail_response = f"{dev_cn}{act_cn.replace('已', '')}失败"
        return ActionResponse(
            action=Action.RESPONSE,
            result=json.dumps({"error": message or "指令执行失败"}, ensure_ascii=False),
            response=fail_response,
        )

    # WebSocket 通知前端（含设备状态）
    _notify_frontend(conn, cmd, state=state)

    # 语音回复（差异化：正常 / 已是目标状态）
    response = _build_response(vc, target, action, message=message)
    logger.bind(tag=TAG).info(
        f"[vehicle_control] cmd={cmd} state={state} msg={message} reply={response!r}"
    )
    return ActionResponse(
        action=Action.RESPONSE,
        result=json.dumps({**cmd, "state": state, "message": message}, ensure_ascii=False),
        response=response,
    )


# ── 插件主函数 ────────────────────────────────────────────────────────────────
@register_function("vehicle_control", vehicle_control_function_desc, ToolType.SYSTEM_CTL)
def vehicle_control(
    conn: "ConnectionHandler",
    target: str = None,
    action: str = None,
    params: dict = None,
) -> ActionResponse:
    vc = _get_vc_profile(conn)

    # action 缺失时，LLM 可能误将状态查询路由到此函数，自动转给 vehicle_status
    if not action:
        logger.bind(tag=TAG).info(
            f"[vehicle_control] action 缺失，自动转 vehicle_status (target={target})"
        )
        try:
            from plugins_func.functions.vehicle_status import vehicle_status
            return vehicle_status(conn, target=target)
        except Exception as e:
            logger.bind(tag=TAG).warning(f"[vehicle_control] 转 vehicle_status 失败: {e}")
            return ActionResponse(
                action=Action.RESPONSE,
                result=json.dumps({"error": "缺少 action 参数"}, ensure_ascii=False),
                response="请告诉我需要执行什么操作",
            )

    # ── 批量指令：target 为列表时逐一执行 ─────────────────────────────────────
    if isinstance(target, list):
        targets = target
        actions = action if isinstance(action, list) else [action] * len(targets)

        if any(isinstance(t, str) and t.lower() == "all" for t in targets):
            logger.bind(tag=TAG).warning("[vehicle_control] 批量指令拒绝 target=all")
            return ActionResponse(
                action=Action.RESPONSE,
                result=json.dumps({"error": "禁止使用 target=all"}, ensure_ascii=False),
                response="请明确需要控制的设备",
            )

        if len(actions) != len(targets):
            return ActionResponse(
                action=Action.RESPONSE,
                result=json.dumps({"error": "target与action数量不匹配"}, ensure_ascii=False),
                response="指令参数错误",
            )

        # 原子预检：任何目标或动作不合法时，整批指令都不执行。
        invalid_pairs = []
        for t, a in zip(targets, actions):
            if not vc.is_valid_device(t) or not vc.is_valid_action(t, a):
                invalid_pairs.append({"target": t, "action": a})

        if invalid_pairs:
            logger.bind(tag=TAG).warning(
                f"[vehicle_control] 批量原子校验失败，整批拒绝: {invalid_pairs}"
            )
            return ActionResponse(
                action=Action.RESPONSE,
                result=json.dumps(
                    {"error": "批量指令包含不支持的设备或动作", "invalid": invalid_pairs},
                    ensure_ascii=False,
                ),
                response="当前组合不支持统一操作",
            )

        valid_pairs = list(zip(targets, actions))
        responses = []
        failed = []
        for t, a in valid_pairs:
            result = _execute_single(conn, vc, t, a, params)
            try:
                parsed = json.loads(result.result or "{}")
                if "error" in parsed:
                    failed.append(t)
                else:
                    responses.append(result.response)
            except (json.JSONDecodeError, TypeError):
                failed.append(t)

        if failed:
            failed_names = [vc.device_cn_name(f) for f in failed]
            summary = "、".join(responses) if responses else ""
            fail_summary = "、".join(failed_names) + "操作失败"
            response = f"{summary}，{fail_summary}" if summary else fail_summary
        else:
            response = "、".join(responses)

        logger.bind(tag=TAG).info(
            f"[vehicle_control] 批量执行 {len(valid_pairs)} 个设备, "
            f"成功 {len(responses)}, 失败 {len(failed)}"
        )
        return ActionResponse(
            action=Action.RESPONSE,
            result=json.dumps(
                {"batch": len(valid_pairs), "failed": failed},
                ensure_ascii=False,
            ),
            response=response,
        )

    # ── 单设备指令 ────────────────────────────────────────────────────────────
    if isinstance(target, str) and target.lower() == "all":
        logger.bind(tag=TAG).warning("[vehicle_control] 拒绝 target=all")
        return ActionResponse(
            action=Action.RESPONSE,
            result=json.dumps({"error": "禁止使用 target=all"}, ensure_ascii=False),
            response="请明确需要控制的设备",
        )

    return _execute_single(conn, vc, target, action, params)
