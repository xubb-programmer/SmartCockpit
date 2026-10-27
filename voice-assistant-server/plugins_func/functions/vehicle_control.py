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
import math
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
            "控制车辆搭载的设备（白光摄像机/热像仪/稳台/转台/激光器/跟踪器/桅杆等）。"
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
                        "控制多个设备时传字符串数组；控制整车所有相关设备时可使用 all。"
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
                        "scan_circle 动作可传 direction（方向，'cw'顺时针/'ccw'逆时针）；"
                        "digital_guide 动作必须传 azimuth（方位角）和 pitch（俯仰角），均为数值；"
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
    # 稳台/转台
    "zero_return":      "归零中",
    "zero_drift":       "零漂校正中",
    "scan_circle":      "开始周扫",
    "digital_guide":    "正在调整朝向",
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


_CN_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_SMALL_NUMBERS = {
    0: "零",
    1: "一",
    2: "二",
    3: "三",
    4: "四",
    5: "五",
    6: "六",
    7: "七",
    8: "八",
    9: "九",
    10: "十",
}


def _int_to_cn(value: int) -> str:
    if value <= 10:
        return _CN_SMALL_NUMBERS.get(value, str(value))
    if value < 20:
        return "十" + _CN_SMALL_NUMBERS[value - 10]
    if value < 100:
        ten, one = divmod(value, 10)
        return _CN_SMALL_NUMBERS[ten] + "十" + (
            _CN_SMALL_NUMBERS[one] if one else ""
        )
    return str(value)


def _parse_number_text(value: object):
    text = str(value or "").strip()
    for suffix in ("倍", "圈", "次"):
        if text.endswith(suffix):
            text = text[:-len(suffix)]
    if text.isdigit():
        return int(text)
    if text in _CN_DIGITS:
        return _CN_DIGITS[text]
    if text == "十":
        return 10
    if text.startswith("十") and len(text) == 2 and text[1] in _CN_DIGITS:
        return 10 + _CN_DIGITS[text[1]]
    if "十" in text:
        left, right = text.split("十", 1)
        if left in _CN_DIGITS and (not right or right in _CN_DIGITS):
            return _CN_DIGITS[left] * 10 + (_CN_DIGITS[right] if right else 0)
    return None


def _parse_finite_number(value: object):
    if isinstance(value, bool):
        return None
    text = str(value).strip()
    if text.endswith("度"):
        text = text[:-1].strip()
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def _canonical_param_value(value: object, rule: dict) -> str:
    text = str(value).strip()
    value_aliases = rule.get("value_aliases") or {}
    if isinstance(value_aliases, dict):
        for canonical, aliases in value_aliases.items():
            alias_values = aliases if isinstance(aliases, list) else [aliases]
            values = {str(canonical), *(str(item) for item in alias_values)}
            if text in values:
                return str(canonical)

    allowed = [str(item) for item in rule.get("allowed", [])]
    parsed = _parse_number_text(text)
    if parsed is not None and str(parsed) in allowed:
        return str(parsed)
    return text


def _validate_and_normalize_params(vc, action, params):
    actions = action if isinstance(action, list) else [action]
    unique_actions = list(dict.fromkeys(item for item in actions if isinstance(item, str)))
    if len(unique_actions) != 1:
        return False, "多动作参数不明确", None

    action_name = unique_actions[0]
    all_rules = getattr(vc, "param_rules", {}) or {}
    action_rules = all_rules.get(action_name, {})
    required_params = [
        name for name, rule in action_rules.items()
        if isinstance(rule, dict) and rule.get("required")
    ]
    if params is None or params == {}:
        if required_params:
            return False, f"缺少参数{required_params[0]}", None
        return True, "", None
    if not isinstance(params, dict):
        return False, "参数格式无效", None

    if all_rules and action_name not in all_rules:
        return False, "该动作不支持参数", None

    if not action_rules:
        return True, "", {
            str(key): str(value) for key, value in params.items()
            if value is not None
        }

    unknown_params = [str(key) for key in params if key not in action_rules]
    if unknown_params:
        return False, f"参数不支持: {','.join(unknown_params)}", None

    normalized = {}
    for name, rule in action_rules.items():
        if not isinstance(rule, dict):
            rule = {}
        if rule.get("required") and name not in params:
            return False, f"缺少参数{name}", None
        if name not in params or params[name] is None:
            continue

        value = _canonical_param_value(params[name], rule)
        parsed = _parse_number_text(value)
        if rule.get("integer"):
            if parsed is None:
                return False, f"参数{name}必须是整数", None
            min_value = rule.get("min")
            max_value = rule.get("max")
            if min_value is not None and parsed < int(min_value):
                return False, f"参数{name}过小", None
            if max_value is not None and parsed > int(max_value):
                return False, f"参数{name}过大", None
            value = str(parsed)
        elif rule.get("number"):
            number = _parse_finite_number(value)
            if number is None:
                return False, f"参数{name}必须是数值", None
            min_value = rule.get("min")
            max_value = rule.get("max")
            if min_value is not None and number < float(min_value):
                return False, f"参数{name}过小", None
            if max_value is not None and number > float(max_value):
                return False, f"参数{name}过大", None
            value = number

        allowed = [str(item) for item in rule.get("allowed", [])]
        if allowed and value not in allowed:
            return False, f"参数{name}取值不支持", None
        normalized[str(name)] = value

    return True, "", normalized or None


# ── Profile 懒加载（缓存在 conn 上，避免重复解析 YAML）────────────────────────
def _get_vc_profile(conn: "ConnectionHandler"):
    """从 conn.config 提取 VehicleControlProfile，结果缓存在 conn._vc_profile。"""
    if not hasattr(conn, "_vc_profile") or conn._vc_profile is None:
        from plugins_func.functions.vehicle_profile_schema import VehicleControlProfile
        conn._vc_profile = VehicleControlProfile.from_profile(conn.config)
    return conn._vc_profile


def _validate_model_request(vc, target, action) -> Tuple[bool, str]:
    """Validate only the model protocol and vehicle capability boundaries."""
    actions = action if isinstance(action, list) else [action]
    if not actions or any(not isinstance(item, str) or not item for item in actions):
        return False, "动作参数无效"

    targets = target if isinstance(target, list) else [target]
    if not targets or any(not isinstance(item, str) or not item for item in targets):
        return False, "设备参数无效"

    target_ids = [item for item in targets if item.lower() != "all"]
    is_all = len(target_ids) != len(targets) or set(target_ids) == set(vc.devices.keys())
    if is_all:
        unique_actions = set(actions)
        if len(unique_actions) != 1:
            return False, "所有设备指令只能执行一个统一动作"
        all_action = next(iter(unique_actions))
        if not vc.targets_for_all(all_action):
            return False, f"所有设备均不支持动作{all_action}"
        return True, ""

    if any(not vc.is_valid_device(item) for item in target_ids):
        return False, "设备参数无效"

    expanded_actions = actions if len(actions) > 1 else actions * len(target_ids)
    if len(expanded_actions) != len(target_ids):
        return False, "设备与动作数量不匹配"
    for item_target, item_action in zip(target_ids, expanded_actions):
        if not vc.is_valid_action(item_target, item_action):
            return False, f"设备{item_target}不支持动作{item_action}"
    return True, ""


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

_TRACKER_ACTION_CN: Dict[str, tuple] = {
    "detect_on":  ("目标检测", "开启"),
    "detect_off": ("目标检测", "关闭"),
    "track_on":   ("目标跟踪", "开启"),
    "track_off":  ("目标跟踪", "关闭"),
}


# ── 语音回复生成（差异化）─────────────────────────────────────────────────────
def _build_response(vc, target: str, action: str, message: str = "") -> str:
    """
    生成≤15字的简洁中文回复。
    根据设备端返回的 message 区分：正常执行 / 已是目标状态 / 失败。
    """
    tracker_action = _TRACKER_ACTION_CN.get(action) if target == "tracker" else None
    if tracker_action:
        subject, state = tracker_action
        if message.startswith("already_"):
            return f"{subject}已处于{state}状态"
        return f"{subject}已{state}"

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
        tracker_action = (
            _TRACKER_ACTION_CN.get(action) if target == "tracker" else None
        )
        if tracker_action:
            subject, target_state = tracker_action
            fail_response = f"{subject}{target_state}失败"
        else:
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

    request_ok, request_reason = _validate_model_request(vc, target, action)
    if not request_ok:
        logger.bind(tag=TAG).warning(
            f"[协议校验] 拒绝设备控制: target={target!r}, "
            f"action={action!r}, reason={request_reason}"
        )
        return ActionResponse(
            action=Action.RESPONSE,
            result=json.dumps(
                {"error": "control_request_invalid", "reason": request_reason},
                ensure_ascii=False,
            ),
            response="当前车型不支持该控制",
        )

    params_ok, params_reason, params = _validate_and_normalize_params(
        vc, action, params
    )
    if not params_ok:
        logger.bind(tag=TAG).warning(
            f"[协议校验] 拒绝设备控制参数: target={target!r}, "
            f"action={action!r}, params={params!r}, reason={params_reason}"
        )
        return ActionResponse(
            action=Action.RESPONSE,
            result=json.dumps(
                {"error": "control_params_invalid", "reason": params_reason},
                ensure_ascii=False,
            ),
            response="控制参数不明确，请重新说",
        )

    # 兼容旧提示词和模型输出：all 统一进入批量展开逻辑。
    if isinstance(target, str) and target.lower() == "all":
        target = [target]

    # ── 批量指令：target 为列表时逐一执行 ─────────────────────────────────────
    if isinstance(target, list):
        targets = list(target)
        actions = action if isinstance(action, list) else [action] * len(targets)

        target_ids = [t for t in targets if isinstance(t, str)]
        has_all_alias = any(t.lower() == "all" for t in target_ids)
        covers_full_inventory = (
            len(target_ids) == len(targets)
            and set(target_ids) == set(vc.devices.keys())
        )

        # “所有设备”是动作相关的逻辑组。例如 Z1 上电只包含具备整机电源动作的
        # 白光、热像、稳台和激光，不应因跟踪器/桅杆不支持 enable 而整批失败。
        if has_all_alias or covers_full_inventory:
            requested_actions = action if isinstance(action, list) else [action]
            unique_actions = {
                item for item in requested_actions if isinstance(item, str) and item
            }
            if (
                len(unique_actions) != 1
                or any(not isinstance(item, str) or not item for item in requested_actions)
            ):
                return ActionResponse(
                    action=Action.RESPONSE,
                    result=json.dumps(
                        {"error": "所有设备指令只能执行一个统一动作"},
                        ensure_ascii=False,
                    ),
                    response="所有设备请使用同一个操作",
                )

            all_action = next(iter(unique_actions))
            targets = vc.targets_for_all(all_action)
            if not targets:
                logger.bind(tag=TAG).warning(
                    f"[vehicle_control] 所有设备均不支持动作: {all_action}"
                )
                return ActionResponse(
                    action=Action.RESPONSE,
                    result=json.dumps(
                        {"error": f"所有设备均不支持动作 {all_action}"},
                        ensure_ascii=False,
                    ),
                    response="当前没有设备支持该操作",
                )
            actions = [all_action] * len(targets)
            logger.bind(tag=TAG).info(
                f"[vehicle_control] 所有设备动作组展开: action={all_action}, "
                f"targets={targets}"
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

    return _execute_single(conn, vc, target, action, params)
