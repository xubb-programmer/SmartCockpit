"""
In-memory mock vehicle device server for local control/status testing.

Endpoints:
  POST /command  Execute a vehicle command and return {success, state, message}
  POST /status   Query one device or all device states
  POST /reset    Reset all device states to their initial values
  GET  /health   Basic server health check

The mock follows the Z1 action whitelist used by z1_scout.yaml. Invalid target
or action combinations return success=false so integration tests do not hide
bad tool calls.
"""

from __future__ import annotations

import copy
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


DEFAULT_PORT = 9000


DEVICE_ACTIONS: dict[str, set[str]] = {
    "vis_cam": {
        "enable",
        "disable",
        "view_large",
        "view_small",
        "zoom_in",
        "zoom_out",
        "focus_auto",
        "focus_in",
        "focus_out",
        "cross_show",
        "cross_hide",
        "brightness_up",
        "brightness_down",
        "enhance_on",
        "enhance_off",
        "fog_on",
        "fog_off",
        "digital_zoom",
    },
    "ir_cam": {
        "enable",
        "disable",
        "view_large",
        "view_small",
        "zoom_in",
        "zoom_out",
        "focus_auto",
        "focus_in",
        "focus_out",
        "cross_show",
        "cross_hide",
        "brightness_up",
        "brightness_down",
        "enhance_on",
        "enhance_off",
        "polarity_black",
        "polarity_white",
        "contrast_up",
        "contrast_down",
        "auto",
        "gamma_correction",
        "scene_correction",
    },
    "gimbal": {
        "enable",
        "disable",
        "zero_return",
        "zero_drift",
        "scan_circle",
        "scan_stop",
    },
    "laser": {
        "enable",
        "disable",
    },
    "tracker": {
        "detect_on",
        "detect_off",
        "track_on",
        "track_off",
    },
    "mast": {
        "park_work",
        "drive_work",
        "onekey_work",
        "end_work",
        "stop_work",
        "mast_raise",
        "mast_lower",
        "single_level",
        "recon_start",
    },
}


_INITIAL_STATES: dict[str, dict[str, str]] = {
    "vis_cam": {
        "power": "off",
        "view": "unknown",
        "zoom_level": "1",
        "zoom": "idle",
        "focus": "idle",
        "cross": "off",
        "brightness": "normal",
        "enhance": "off",
        "fog": "off",
    },
    "ir_cam": {
        "power": "off",
        "view": "unknown",
        "zoom_level": "1",
        "zoom": "idle",
        "focus": "idle",
        "cross": "off",
        "brightness": "normal",
        "enhance": "off",
        "polarity": "unknown",
        "contrast": "normal",
        "mode": "manual",
        "correction": "idle",
    },
    "gimbal": {
        "power": "off",
        "zero": "unknown",
        "scan": "off",
        "scan_direction": "",
        "scan_count": "",
    },
    "laser": {
        "power": "off",
    },
    "tracker": {
        "detect": "off",
        "track": "off",
    },
    "mast": {
        "work": "idle",
        "mast": "idle",
        "level": "idle",
        "recon": "off",
    },
}


_ON_OFF_ACTIONS: dict[str, tuple[str, str]] = {
    "enable": ("power", "on"),
    "disable": ("power", "off"),
    "cross_show": ("cross", "on"),
    "cross_hide": ("cross", "off"),
    "enhance_on": ("enhance", "on"),
    "enhance_off": ("enhance", "off"),
    "fog_on": ("fog", "on"),
    "fog_off": ("fog", "off"),
    "detect_on": ("detect", "on"),
    "detect_off": ("detect", "off"),
    "track_on": ("track", "on"),
    "track_off": ("track", "off"),
    "recon_start": ("recon", "on"),
}


device_states = copy.deepcopy(_INITIAL_STATES)


def _error(message: str, state: str = "unknown") -> dict[str, Any]:
    return {"success": False, "state": state, "message": message}


def _success(state: str = "ok", message: str = "") -> dict[str, Any]:
    return {"success": True, "state": state, "message": message}


def _normalize_params(payload: dict[str, Any]) -> dict[str, str]:
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        return {}
    return {str(key): str(value) for key, value in params.items() if value is not None}


def _supported_targets_for_action(action: str) -> list[str]:
    return [
        target
        for target, actions in DEVICE_ACTIONS.items()
        if action in actions
    ]


def _validate_target_action(target: str, action: str) -> dict[str, Any] | None:
    if not target:
        return _error("missing target")
    if not action:
        return _error("missing action")
    if target not in DEVICE_ACTIONS:
        return _error(f"unknown device: {target}")
    if action not in DEVICE_ACTIONS[target]:
        return _error(f"unsupported action for {target}: {action}")
    return None


def _apply_on_off_action(state_map: dict[str, str], action: str) -> dict[str, Any] | None:
    if action not in _ON_OFF_ACTIONS:
        return None

    state_key, new_value = _ON_OFF_ACTIONS[action]
    current = state_map.get(state_key)
    if current == new_value:
        message = "already_on" if new_value == "on" else "already_off"
        return _success(new_value, message)

    state_map[state_key] = new_value
    return _success(new_value)


def _apply_action_state(target: str, action: str, params: dict[str, str]) -> dict[str, Any]:
    state_map = device_states[target]

    on_off_result = _apply_on_off_action(state_map, action)
    if on_off_result is not None:
        return on_off_result

    if action == "view_large":
        state_map["view"] = "large"
    elif action == "view_small":
        state_map["view"] = "small"
    elif action == "zoom_in":
        state_map["zoom"] = "in"
    elif action == "zoom_out":
        state_map["zoom"] = "out"
    elif action == "digital_zoom":
        state_map["zoom"] = "digital"
        if "zoom_level" in params:
            state_map["zoom_level"] = params["zoom_level"]
    elif action == "focus_auto":
        state_map["focus"] = "auto"
    elif action == "focus_in":
        state_map["focus"] = "in"
    elif action == "focus_out":
        state_map["focus"] = "out"
    elif action == "brightness_up":
        state_map["brightness"] = "up"
    elif action == "brightness_down":
        state_map["brightness"] = "down"
    elif action == "polarity_black":
        state_map["polarity"] = "black"
    elif action == "polarity_white":
        state_map["polarity"] = "white"
    elif action == "contrast_up":
        state_map["contrast"] = "up"
    elif action == "contrast_down":
        state_map["contrast"] = "down"
    elif action == "auto":
        state_map["mode"] = "auto"
    elif action == "gamma_correction":
        state_map["correction"] = "gamma"
    elif action == "scene_correction":
        state_map["correction"] = "scene"
    elif action == "zero_return":
        state_map["zero"] = "return"
    elif action == "zero_drift":
        state_map["zero"] = "drift"
    elif action == "scan_circle":
        state_map["scan"] = "circle"
        if "direction" in params:
            state_map["scan_direction"] = params["direction"]
        if "count" in params:
            state_map["scan_count"] = params["count"]
    elif action == "scan_stop":
        state_map["scan"] = "off"
    elif action == "park_work":
        state_map["work"] = "park"
    elif action == "drive_work":
        state_map["work"] = "drive"
    elif action == "onekey_work":
        state_map["work"] = "onekey"
    elif action == "end_work":
        state_map["work"] = "ended"
        state_map["recon"] = "off"
    elif action == "stop_work":
        state_map["work"] = "stopped"
    elif action == "mast_raise":
        state_map["mast"] = "raised"
    elif action == "mast_lower":
        state_map["mast"] = "lowered"
    elif action == "single_level":
        state_map["level"] = "single"
    else:
        return _error(f"unhandled action for {target}: {action}")

    return _success("ok")


def _handle_command(payload: dict[str, Any]) -> dict[str, Any]:
    target = payload.get("target", "")
    action = payload.get("action", "")
    params = _normalize_params(payload)

    if isinstance(target, list):
        results = []
        for item in target:
            next_payload = dict(payload)
            next_payload["target"] = item
            results.append(_handle_command(next_payload))
        success = all(item.get("success") for item in results)
        return {
            "success": success,
            "state": "ok" if success else "partial_failed",
            "message": "",
            "results": results,
        }

    target = str(target) if target is not None else ""
    action = str(action) if action is not None else ""

    if target.lower() == "all":
        targets = _supported_targets_for_action(action)
        if not targets:
            return _error(f"unsupported action for all devices: {action}")
        return _handle_command({**payload, "target": targets})

    validation_error = _validate_target_action(target, action)
    if validation_error is not None:
        return validation_error

    return _apply_action_state(target, action, params)


def _handle_status(payload: dict[str, Any]) -> dict[str, Any]:
    target = payload.get("target")

    if target:
        if isinstance(target, list):
            unknown = [item for item in target if item not in device_states]
            if unknown:
                return {"devices": {}, "error": f"unknown device: {unknown[0]}"}
            return {"devices": {item: dict(device_states[item]) for item in target}}
        if target not in device_states:
            return {"devices": {}, "error": f"unknown device: {target}"}
        return {"devices": {target: dict(device_states[target])}}

    return {"devices": {key: dict(value) for key, value in device_states.items()}}


def _handle_reset() -> dict[str, Any]:
    global device_states
    device_states = copy.deepcopy(_INITIAL_STATES)
    return _success("reset")


class VehicleCmdHandler(BaseHTTPRequestHandler):
    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _send_json(self, code: int, data: dict[str, Any]) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"status": "ok", "devices": sorted(device_states)})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        try:
            payload = self._read_json()
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON"})
            return

        if self.path == "/command":
            result = _handle_command(payload)
            print(
                "[CMD] "
                f"target={payload.get('target')} action={payload.get('action')} -> {result}",
                flush=True,
            )
            self._send_json(200, result)
        elif self.path == "/status":
            result = _handle_status(payload)
            print(
                f"[STATUS] query={payload.get('target', 'all')} -> "
                f"{json.dumps(result, ensure_ascii=False)}",
                flush=True,
            )
            self._send_json(200, result)
        elif self.path == "/reset":
            result = _handle_reset()
            print("[RESET] device states restored", flush=True)
            self._send_json(200, result)
        else:
            self._send_json(404, {"error": "not found"})

    def log_message(self, format: str, *args: Any) -> None:
        pass


def run(port: int = DEFAULT_PORT) -> None:
    httpd = ThreadingHTTPServer(("0.0.0.0", port), VehicleCmdHandler)
    print("=" * 58)
    print(f"[MOCK] vehicle control server started on port {port}")
    print(f"[MOCK] POST http://0.0.0.0:{port}/command  command")
    print(f"[MOCK] POST http://0.0.0.0:{port}/status   status query")
    print(f"[MOCK] POST http://0.0.0.0:{port}/reset    reset states")
    print(f"[MOCK] GET  http://0.0.0.0:{port}/health   health check")
    print("[MOCK] initial states:")
    for device, state in device_states.items():
        print(f"       {device}: {state}")
    print("[MOCK] Ctrl+C to stop")
    print("=" * 58)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[MOCK] stopping...")
    finally:
        httpd.server_close()
        print("[MOCK] stopped")


if __name__ == "__main__":
    import sys

    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    run(port)
