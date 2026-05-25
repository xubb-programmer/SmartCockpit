"""
mock_vehicle_server.py — 带内存状态的模拟车辆设备控制服务。

功能：
  POST /command  — 执行设备指令，返回 {success, state, message}
  POST /status   — 查询设备状态

启动：
  python scripts/mock_vehicle_server.py [port]   # 默认 19000
"""
import json
import copy
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── 设备初始状态定义 ─────────────────────────────────────────────────────────
# 每个设备维护若干状态键（key → value）。
# value 只用字符串："on"/"off"/"open"/"closed" 等。
_INITIAL_STATES = {
    "vis_cam": {
        "power": "off",
        "cross": "off",
        "enhance": "off",
        "fog": "off",
    },
    "ir_cam": {
        "power": "off",
        "cross": "off",
        "enhance": "off",
    },
    "gimbal": {
        "power": "off",
    },
    "laser": {
        "power": "off",
    },
    "tracker": {"detect": "off", "track": "off"},
    "mast": {},
}

# ── 动作 → 状态变更映射 ─────────────────────────────────────────────────────
# 格式：action → (state_key, new_value, on_value)
#   state_key:  操作的状态键
#   new_value:  执行后的状态值
#   on_value:   用于判断"已经是目标状态"时的比较值（即 already_on / already_off）
# 不在此表中的动作视为"无状态动作"（如 zoom_in），直接返回 success。
_ACTION_STATE_MAP = {
    # 电源
    "enable":       ("power",   "on",     "on"),
    "disable":      ("power",   "off",    "off"),
    # 十字丝
    "cross_show":   ("cross",   "on",     "on"),
    "cross_hide":   ("cross",   "off",    "off"),
    # 增强
    "enhance_on":   ("enhance", "on",     "on"),
    "enhance_off":  ("enhance", "off",    "off"),
    # 透雾
    "fog_on":       ("fog",     "on",     "on"),
    "fog_off":      ("fog",     "off",    "off"),
    # 识别
    "detect_on":    ("detect",  "on",     "on"),
    "detect_off":   ("detect",  "off",    "off"),
    # 跟踪
    "track_on":     ("track",   "on",     "on"),
    "track_off":    ("track",   "off",    "off"),
    # 侦察
    "recon_start":  ("recon",   "on",     "on"),
}

# ── 全局状态（进程内共享）─────────────────────────────────────────────────────
device_states = copy.deepcopy(_INITIAL_STATES)


def _handle_command(payload: dict) -> dict:
    """处理 /command 请求，返回响应字典。"""
    target = payload.get("target", "")
    action = payload.get("action", "")

    if target not in device_states:
        return {"success": False, "state": "unknown", "message": f"unknown device: {target}"}

    state_map = device_states[target]

    # 有状态动作
    if action in _ACTION_STATE_MAP:
        state_key, new_value, on_value = _ACTION_STATE_MAP[action]

        if state_key not in state_map:
            # 该设备不支持此状态键，仍然执行（宽松模式）
            state_map[state_key] = new_value
            return {"success": True, "state": new_value, "message": ""}

        current = state_map[state_key]
        if current == on_value:
            # 已经是目标状态
            already_msg = "already_on" if on_value in ("on", "open") else "already_off"
            return {"success": True, "state": current, "message": already_msg}

        # 执行状态变更
        state_map[state_key] = new_value
        return {"success": True, "state": new_value, "message": ""}

    # 无状态动作（zoom_in, focus_auto, scan_circle 等）
    return {"success": True, "state": "ok", "message": ""}


def _handle_status(payload: dict) -> dict:
    """处理 /status 请求，返回设备状态。"""
    target = payload.get("target")

    if target:
        if target not in device_states:
            return {"devices": {}, "error": f"unknown device: {target}"}
        return {"devices": {target: dict(device_states[target])}}

    # 返回全部
    return {"devices": {k: dict(v) for k, v in device_states.items()}}


class VehicleCmdHandler(BaseHTTPRequestHandler):

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _send_json(self, code: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            payload = self._read_json()
        except (json.JSONDecodeError, Exception):
            self._send_json(400, {"error": "invalid JSON"})
            return

        if self.path == "/command":
            result = _handle_command(payload)
            tag = "CMD"
            print(f"[{tag}] target={payload.get('target')} action={payload.get('action')} → {result}")
            self._send_json(200, result)

        elif self.path == "/status":
            result = _handle_status(payload)
            tag = "STATUS"
            target = payload.get("target", "all")
            print(f"[{tag}] query={target} → {json.dumps(result, ensure_ascii=False)}")
            self._send_json(200, result)

        else:
            self._send_json(404, {"error": "not found"})

    def log_message(self, format, *args):
        pass  # 抑制默认 HTTP 日志


def run(port=19000):
    httpd = HTTPServer(("0.0.0.0", port), VehicleCmdHandler)
    print("=" * 58)
    print(f"[MOCK] 模拟车辆控制服务已启动")
    print(f"[MOCK] POST http://0.0.0.0:{port}/command  — 设备指令")
    print(f"[MOCK] POST http://0.0.0.0:{port}/status   — 状态查询")
    print(f"[MOCK] 设备初始状态（全部 OFF）:")
    for dev, st in device_states.items():
        print(f"       {dev}: {st}")
    print(f"[MOCK] Ctrl+C 停止")
    print("=" * 58)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[MOCK] 停止中...")
    finally:
        httpd.server_close()
        print("[MOCK] 已停止。")


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 19000
    run(port)
