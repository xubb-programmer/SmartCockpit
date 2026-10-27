import json
import time
import base64
import hashlib
import hmac
import os
import re
import glob
from typing import Dict, List, Tuple
from aiohttp import web

from core.auth import AuthManager
from core.utils.util import get_local_ip, get_vision_url
from core.api.base_handler import BaseHandler

TAG = __name__

_VEHICLE_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_VEHICLE_ACTION_CN = {
    "enable": "上电", "disable": "下电",
    "view_large": "切大视场", "view_small": "切小视场",
    "zoom_in": "放大", "zoom_out": "缩小",
    "focus_auto": "自动聚焦", "focus_in": "调焦加", "focus_out": "调焦减",
    "cross_show": "开十字丝", "cross_hide": "关十字丝",
    "brightness_up": "亮度加", "brightness_down": "亮度减",
    "enhance_on": "开增强", "enhance_off": "关增强",
    "fog_on": "开透雾", "fog_off": "关透雾",
    "digital_zoom": "数字变倍",
    "polarity_black": "切黑热", "polarity_white": "切白热",
    "contrast_up": "对比度加", "contrast_down": "对比度减",
    "auto": "自动模式", "gamma_correction": "图像校正",
    "scene_correction": "场景校正",
    "zero_return": "回零", "zero_drift": "零漂校正",
    "scan_circle": "周扫", "digital_guide": "数字引导", "scan_stop": "停扫",
    "detect_on": "开识别", "detect_off": "关识别",
    "track_on": "开跟踪", "track_off": "关跟踪",
    "park_work": "驻车展开", "drive_work": "行车展开",
    "onekey_work": "一键展开", "end_work": "一键撤收",
    "stop_work": "停止", "mast_raise": "升桅", "mast_lower": "降桅",
    "single_level": "单次调平", "recon_start": "开始侦察",
}

_VEHICLE_HELP_COMMAND_OVERRIDES = {
    ("tracker", "detect_on"): "开启目标检测",
    ("tracker", "detect_off"): "关闭目标检测",
    ("tracker", "track_on"): "开启目标跟踪",
    ("tracker", "track_off"): "关闭目标跟踪",
    ("mast", "recon_start"): "目标侦察",
}


def _safe_basename(filename: str) -> str:
    # Prevent directory traversal
    return os.path.basename(filename)


def _is_safe_vehicle_profile_id(profile_id: str) -> bool:
    return bool(_VEHICLE_PROFILE_ID_RE.fullmatch(profile_id or ""))


def _resolve_vehicle_profile_path(profile_id: str) -> Tuple[str, str]:
    if not _is_safe_vehicle_profile_id(profile_id):
        raise ValueError("invalid profile_id")

    profiles_dir = os.path.realpath(
        os.path.join(os.getcwd(), "data", "vehicle_profiles")
    )
    profile_path = os.path.realpath(os.path.join(profiles_dir, f"{profile_id}.yaml"))
    try:
        if os.path.commonpath([profiles_dir, profile_path]) != profiles_dir:
            raise ValueError("invalid profile path")
    except ValueError:
        raise ValueError("invalid profile path")
    return profiles_dir, profile_path


def _parse_version(ver: str) -> Tuple[int, ...]:
    # conservative parser: split by non-digit, keep numeric parts
    parts = re.findall(r"\d+", ver)
    return tuple(int(p) for p in parts) if parts else (0,)


def _is_higher_version(a: str, b: str) -> bool:
    """Return True if version string a > b (semver-like numeric compare)."""
    ta = _parse_version(a)
    tb = _parse_version(b)
    # compare tuple lexicographically, but allow different lengths
    maxlen = max(len(ta), len(tb))
    for i in range(maxlen):
        ai = ta[i] if i < len(ta) else 0
        bi = tb[i] if i < len(tb) else 0
        if ai > bi:
            return True
        if ai < bi:
            return False
    return False


class OTAHandler(BaseHandler):
    def __init__(self, config: dict):
        super().__init__(config)
        auth_config = config["server"].get("auth", {})
        self.auth_enable = auth_config.get("enabled", False)
        # 设备白名单
        self.allowed_devices = set(auth_config.get("allowed_devices", []))
        secret_key = config["server"]["auth_key"]
        expire_seconds = auth_config.get("expire_seconds")
        self.auth = AuthManager(secret_key=secret_key, expire_seconds=expire_seconds)

        # firmware storage
        self.bin_dir = os.path.join(os.getcwd(), "data", "bin")
        # cache structure: { 'updated_at': timestamp, 'ttl': seconds, 'files_by_model': { model: [(version, filename), ...] } }
        self._bin_cache: Dict = {
            "updated_at": 0,
            "ttl": config.get("firmware_cache_ttl", 30),
            "files_by_model": {},
        }

    def _refresh_bin_cache_if_needed(self):
        now = int(time.time())
        ttl = int(self._bin_cache.get("ttl", 30))
        if now - int(
            self._bin_cache.get("updated_at", 0)
        ) < ttl and self._bin_cache.get("files_by_model"):
            return

        files_by_model: Dict[str, List[Tuple[str, str]]] = {}
        try:
            if not os.path.isdir(self.bin_dir):
                os.makedirs(self.bin_dir, exist_ok=True)

            # match files like model_1.2.3.bin (allow dots, dashes, underscores in model and version)
            pattern = os.path.join(self.bin_dir, "*.bin")
            for path in glob.glob(pattern):
                fname = os.path.basename(path)
                # filename format: {model}_{version}.bin
                m = re.match(r"^(.+?)_([0-9][A-Za-z0-9\.\-_]*)\.bin$", fname)
                if not m:
                    # skip files not conforming to naming rule
                    continue
                model = m.group(1)
                version = m.group(2)
                files_by_model.setdefault(model, []).append((version, fname))

            # sort versions for each model descending
            for model, items in files_by_model.items():
                items.sort(key=lambda it: _parse_version(it[0]), reverse=True)

            self._bin_cache["files_by_model"] = files_by_model
            self._bin_cache["updated_at"] = now
            self.logger.bind(tag=TAG).info(
                f"Firmware cache refreshed: {len(files_by_model)} models"
            )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"刷新固件缓存失败: {e}")
            # keep previous cache if any

    def generate_password_signature(self, content: str, secret_key: str) -> str:
        """生成MQTT密码签名

        Args:
            content: 签名内容 (clientId + '|' + username)
            secret_key: 密钥

        Returns:
            str: Base64编码的HMAC-SHA256签名
        """
        try:
            hmac_obj = hmac.new(
                secret_key.encode("utf-8"), content.encode("utf-8"), hashlib.sha256
            )
            signature = hmac_obj.digest()
            return base64.b64encode(signature).decode("utf-8")
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"生成MQTT密码签名失败: {e}")
            return ""

    def _get_websocket_url(self, local_ip: str, port: int) -> str:
        """获取websocket地址

        Args:
            local_ip: 本地IP地址
            port: 端口号

        Returns:
            str: websocket地址
        """
        server_config = self.config["server"]
        websocket_config = server_config.get("websocket", "")

        if "你的" not in websocket_config:
            return websocket_config
        else:
            return f"ws://{local_ip}:{port}/voice_assistant/v1/"

    async def handle_post(self, request):
        """处理 OTA POST 请求

        This handler will:
        - read device id/client id (as before)
        - attempt to determine device model and current firmware version (prefer headers, fallback to body)
        - check data/bin for newer firmware for that model
        - if found a newer firmware, set firmware.url to the download endpoint
        """
        try:
            data = await request.text()
            self.logger.bind(tag=TAG).debug(f"OTA请求方法: {request.method}")
            self.logger.bind(tag=TAG).debug(f"OTA请求头: {request.headers}")
            self.logger.bind(tag=TAG).debug(f"OTA请求数据: {data}")

            device_id = request.headers.get("device-id", "")
            if device_id:
                self.logger.bind(tag=TAG).info(f"OTA请求设备ID: {device_id}")
            else:
                raise Exception("OTA请求设备ID为空")

            client_id = request.headers.get("client-id", "")
            if client_id:
                self.logger.bind(tag=TAG).info(f"OTA请求ClientID: {client_id}")
            else:
                raise Exception("OTA请求ClientID为空")

            if self.auth_enable and self.allowed_devices and device_id not in self.allowed_devices:
                self.logger.bind(tag=TAG).warning(
                    f"OTA credential request rejected for non-allowlisted device: {device_id}"
                )
                response = web.Response(
                    text=json.dumps(
                        {"success": False, "message": "device not allowed."},
                        separators=(",", ":"),
                    ),
                    content_type="application/json",
                    status=403,
                )
                self._add_cors_headers(response)
                return response

            data_json = {}
            try:
                data_json = json.loads(data) if data else {}
            except Exception:
                data_json = {}

            server_config = self.config["server"]
            # Distinguish ports:
            # - websocket_port is used to construct websocket URL (server["port"])
            # - http_port is used to construct OTA download URLs (server["http_port"])
            websocket_port = int(server_config.get("port", 8000))
            http_port = int(server_config.get("http_port", 8003))
            local_ip = get_local_ip()

            # Determine device model (prefer headers)
            device_model = ""
            # header candidates
            for h in ("device-model", "device_model", "model"):
                if h in request.headers:
                    device_model = request.headers.get(h, "").strip()
                    break
            # body fallback
            if not device_model:
                try:
                    if "board" in data_json and isinstance(data_json["board"], dict):
                        device_model = data_json["board"].get("type", "")
                    elif "model" in data_json:
                        device_model = data_json.get("model", "")
                except Exception:
                    device_model = ""
            if not device_model:
                device_model = "default"

            # Determine device current version (prefer headers)
            device_version = ""
            for h in (
                "device-version",
                "device_version",
                "firmware-version",
                "app-version",
                "application-version",
            ):
                if h in request.headers:
                    device_version = request.headers.get(h, "").strip()
                    break
            if not device_version:
                try:
                    device_version = data_json.get("application", {}).get("version", "")
                except Exception:
                    device_version = ""
            if not device_version:
                device_version = "0.0.0"

            return_json = {
                "server_time": {
                    "timestamp": int(round(time.time() * 1000)),
                    "timezone_offset": server_config.get("timezone_offset", 8) * 60,
                },
                "firmware": {
                    "version": device_version,
                    "url": "",
                },
            }

            # existing mqtt/websocket logic (unchanged)
            mqtt_gateway_endpoint = server_config.get("mqtt_gateway")

            if mqtt_gateway_endpoint:  # 如果配置了非空字符串
                # 尝试从请求数据中获取设备型号（已解析 above）
                try:
                    group_id = f"GID_{device_model}".replace(":", "_").replace(" ", "_")
                except Exception as e:
                    self.logger.bind(tag=TAG).error(f"获取设备型号失败: {e}")
                    group_id = "GID_default"

                mac_address_safe = device_id.replace(":", "_")
                mqtt_client_id = f"{group_id}@@@{mac_address_safe}@@@{mac_address_safe}"

                # 构建用户数据
                user_data = {"ip": "unknown"}
                try:
                    user_data_json = json.dumps(user_data)
                    username = base64.b64encode(user_data_json.encode("utf-8")).decode(
                        "utf-8"
                    )
                except Exception as e:
                    self.logger.bind(tag=TAG).error(f"生成用户名失败: {e}")
                    username = ""

                # 生成密码
                password = ""
                signature_key = server_config.get("mqtt_signature_key", "")
                if signature_key:
                    password = self.generate_password_signature(
                        mqtt_client_id + "|" + username, signature_key
                    )
                    if not password:
                        password = ""  # 签名失败则留空，由设备决定是否允许无密码
                else:
                    self.logger.bind(tag=TAG).warning("缺少MQTT签名密钥，密码留空")

                # 构建MQTT配置（直接使用 mqtt_gateway 字符串）
                return_json["mqtt"] = {
                    "endpoint": mqtt_gateway_endpoint,
                    "client_id": mqtt_client_id,
                    "username": username,
                    "password": password,
                    "publish_topic": "device-server",
                    "subscribe_topic": f"devices/p2p/{mac_address_safe}",
                }
                self.logger.bind(tag=TAG).info(f"为设备 {device_id} 下发MQTT网关配置")

            else:  # 未配置 mqtt_gateway，下发 WebSocket
                # 如果开启了认证，则进行认证校验
                token = ""
                if self.auth_enable and not self.allowed_devices:
                    token = self.auth.generate_token(client_id, device_id)
                # NOTE: use websocket_port here
                return_json["websocket"] = {
                    "url": self._get_websocket_url(local_ip, websocket_port),
                    "token": token,
                }
                self.logger.bind(tag=TAG).info(
                    f"未配置MQTT网关，为设备 {device_id} 下发WebSocket配置"
                )

            # Now check firmware files for updates
            try:
                self._refresh_bin_cache_if_needed()
                files_by_model = self._bin_cache.get("files_by_model", {})
                candidates = files_by_model.get(device_model, [])

                self.logger.bind(tag=TAG).info(
                    f"查找型号 {device_model} 的固件，找到 {len(candidates)} 个候选"
                )

                chosen_url = ""
                chosen_version = device_version

                # candidates are sorted descending by version
                for ver, fname in candidates:
                    if _is_higher_version(ver, device_version):
                        # build download url (only allow download via our download endpoint)
                        chosen_version = ver
                        # Build download URL directly (do not rely on vision URL)
                        chosen_url = f"http://{local_ip}:{http_port}/voice_assistant/ota/download/{fname}"
                        break

                if chosen_url:
                    return_json["firmware"]["version"] = chosen_version
                    return_json["firmware"]["url"] = chosen_url
                    self.logger.bind(tag=TAG).info(
                        f"为设备 {device_id} 下发固件 {chosen_version} [如果地址前缀有误，请检查配置文件中的server.vision_explain]-> {chosen_url} "
                    )
                else:
                    self.logger.bind(tag=TAG).info(
                        f"设备 {device_id} 固件已是最新: {device_version}"
                    )

            except Exception as e:
                self.logger.bind(tag=TAG).error(f"检查固件版本时出错: {e}")

            response = web.Response(
                text=json.dumps(return_json, separators=(",", ":")),
                content_type="application/json",
            )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"OTA POST处理异常: {e}")
            return_json = {"success": False, "message": "request error."}
            response = web.Response(
                text=json.dumps(return_json, separators=(",", ":")),
                content_type="application/json",
            )
        finally:
            self._add_cors_headers(response)
            return response

    async def handle_get(self, request):
        """处理 OTA GET 请求"""
        try:
            server_config = self.config["server"]
            local_ip = get_local_ip()
            # use websocket port for websocket URL
            websocket_port = int(server_config.get("port", 8000))
            websocket_url = self._get_websocket_url(local_ip, websocket_port)
            message = f"OTA接口运行正常，向设备发送的websocket地址是：{websocket_url}"
            response = web.Response(text=message, content_type="text/plain")
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"OTA GET请求异常: {e}")
            response = web.Response(text="OTA接口异常", content_type="text/plain")
        finally:
            self._add_cors_headers(response)
            return response

    async def handle_download(self, request):
        """
        下载固件接口
        URL: /voice_assistant/ota/download/{filename}
        - 只允许下载 data/bin 目录下的 .bin 文件
        - filename 必须是 basename 且匹配安全的模式
        """
        try:
            fname = request.match_info.get("filename", "")
            if not fname:
                raise web.HTTPBadRequest(text="filename required")

            # sanitize
            fname = _safe_basename(fname)
            # pattern: allow letters, numbers, dot, underscore, dash
            if not re.match(r"^[A-Za-z0-9\.\-_]+\.bin$", fname):
                raise web.HTTPBadRequest(text="invalid filename")

            file_path = os.path.join(self.bin_dir, fname)
            # ensure realpath is under bin_dir
            file_real = os.path.realpath(file_path)
            bin_dir_real = os.path.realpath(self.bin_dir)
            if (
                not file_real.startswith(bin_dir_real + os.sep)
                and file_real != bin_dir_real
            ):
                raise web.HTTPForbidden(text="forbidden")

            if not os.path.isfile(file_real):
                raise web.HTTPNotFound(text="file not found")

            # use FileResponse to stream file
            resp = web.FileResponse(path=file_real)
        except web.HTTPError as e:
            resp = e
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"固件下载异常: {e}")
            resp = web.Response(text="download error", status=500)
        finally:
            try:
                self._add_cors_headers(resp)
            except Exception:
                pass
            return resp

    async def handle_vehicle_profiles(self, request):
        """GET /voice_assistant/vehicle/profiles — 列出所有可用车型配置"""
        try:
            import yaml
            profiles_dir = os.path.join(os.getcwd(), "data", "vehicle_profiles")
            profiles = []
            current = self.config.get("vehicle_profile", "")

            if os.path.isdir(profiles_dir):
                for fname in sorted(os.listdir(profiles_dir)):
                    if not fname.endswith(".yaml"):
                        continue
                    profile_id = fname[:-5]  # 去掉 .yaml
                    if not _is_safe_vehicle_profile_id(profile_id):
                        continue
                    try:
                        with open(os.path.join(profiles_dir, fname), "r", encoding="utf-8") as f:
                            data = yaml.safe_load(f) or {}
                        profiles.append({
                            "id": profile_id,
                            "name": data.get("name", profile_id),
                            "active": profile_id == current,
                        })
                    except Exception:
                        profiles.append({"id": profile_id, "name": profile_id, "active": profile_id == current})

            resp = web.Response(
                text=json.dumps({"profiles": profiles, "current": current}, ensure_ascii=False),
                content_type="application/json",
            )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"获取车型列表失败: {e}")
            resp = web.Response(text=json.dumps({"error": str(e)}), content_type="application/json", status=500)
        finally:
            self._add_cors_headers(resp)
            return resp

    async def handle_vehicle_switch(self, request):
        """POST /voice_assistant/vehicle/switch — 切换车型配置（重启生效）"""
        try:
            import yaml
            body = await request.json()
            profile_id = body.get("profile_id", "").strip()

            if not profile_id:
                raise ValueError("profile_id 不能为空")

            # 校验 profile 文件存在
            profiles_dir, profile_path = _resolve_vehicle_profile_path(profile_id)
            if not os.path.exists(profile_path):
                raise FileNotFoundError(f"车型配置文件不存在: {profile_id}.yaml")

            # 读取 .config.yaml，更新 vehicle_profile 字段，写回
            config_path = os.path.join(os.getcwd(), "data", ".config.yaml")
            with open(config_path, "r", encoding="utf-8") as f:
                raw = f.read()

            import re as _re
            if _re.search(r"^vehicle_profile\s*:", raw, _re.MULTILINE):
                # 替换已有行
                raw = _re.sub(
                    r"^(vehicle_profile\s*:).*$",
                    f"vehicle_profile: {profile_id}",
                    raw, flags=_re.MULTILINE
                )
            else:
                # 追加到文件末尾
                raw += f"\nvehicle_profile: {profile_id}\n"

            with open(config_path, "w", encoding="utf-8") as f:
                f.write(raw)

            # 同步更新内存中的 config，使 handle_vehicle_profiles 立即反映
            self.config["vehicle_profile"] = profile_id

            # 清除所有相关缓存，使下次 load_config 重新加载
            try:
                from core.utils.cache.manager import cache_manager, CacheType
                cache_manager.delete(CacheType.CONFIG, "main_config")
                # 清除模板和设备提示词缓存
                cache_manager.clear(CacheType.CONFIG)
                cache_manager.clear(CacheType.DEVICE_PROMPT)
            except Exception:
                pass

            self.logger.bind(tag=TAG).info(f"车型已切换为: {profile_id}")
            resp = web.Response(
                text=json.dumps({"success": True, "current": profile_id}, ensure_ascii=False),
                content_type="application/json",
            )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"切换车型失败: {e}")
            resp = web.Response(
                text=json.dumps({"success": False, "error": str(e)}, ensure_ascii=False),
                content_type="application/json",
                status=400,
            )
        finally:
            self._add_cors_headers(resp)
            return resp

    async def handle_vehicle_commands(self, request):
        """GET /voice_assistant/vehicle/commands — 返回当前车型的语音指令列表"""
        try:
            import yaml
            profile_name = self.config.get("vehicle_profile", "")
            if not profile_name:
                resp = web.Response(
                    text=json.dumps({"error": "未配置车型"}, ensure_ascii=False),
                    content_type="application/json", status=400,
                )
                self._add_cors_headers(resp)
                return resp

            _, profile_path = _resolve_vehicle_profile_path(profile_name)
            if not os.path.exists(profile_path):
                resp = web.Response(
                    text=json.dumps({"error": f"配置文件不存在: {profile_name}"}, ensure_ascii=False),
                    content_type="application/json", status=404,
                )
                self._add_cors_headers(resp)
                return resp

            with open(profile_path, "r", encoding="utf-8") as f:
                profile = yaml.safe_load(f) or {}

            vehicle_name = profile.get("name", profile_name)
            vc = profile.get("vehicle_control", {})
            devices = vc.get("devices", {})
            param_rules = vc.get("param_rules") or {}
            actions_requiring_params = {
                action
                for action, rules in param_rules.items()
                if isinstance(rules, dict)
                and any(
                    isinstance(rule, dict) and rule.get("required")
                    for rule in rules.values()
                )
            }

            # 生成指令列表
            commands = []

            # 设备控制指令
            device_cmds = []
            for dev_id, spec in devices.items():
                cn_name = (spec.get("cn_names") or [dev_id])[0]
                for act in (spec.get("actions") or []):
                    if act in actions_requiring_params:
                        continue
                    act_cn = _VEHICLE_ACTION_CN.get(act, act)
                    command_text = _VEHICLE_HELP_COMMAND_OVERRIDES.get(
                        (dev_id, act), f"{cn_name}{act_cn}"
                    )
                    device_cmds.append({
                        "text": command_text,
                        "description": f"执行{command_text}",
                    })
            if device_cmds:
                commands.append({
                    "category": "设备控制",
                    "icon": "🎛️",
                    "items": device_cmds,
                })

            # 批量指令
            batch_items = []
            all_targets = vc.get("all_targets") or {}
            for action, command_text in (
                ("enable", "所有设备上电"),
                ("disable", "所有设备下电"),
            ):
                target_ids = [
                    target_id for target_id in (all_targets.get(action) or [])
                    if target_id in devices
                    and action in (devices[target_id].get("actions") or [])
                ]
                if not target_ids:
                    continue
                target_names = [
                    (devices[target_id].get("cn_names") or [target_id])[0]
                    for target_id in target_ids
                ]
                batch_items.append({
                    "text": command_text,
                    "description": (
                        f"同时控制{'、'.join(target_names)}"
                        f"{_VEHICLE_ACTION_CN[action]}"
                    ),
                })
            if batch_items:
                commands.append({
                    "category": "批量操作",
                    "icon": "⚡",
                    "items": batch_items,
                })

            # 带参数指令
            param_items = []
            # 检查是否有转台设备
            if "gimbal" in devices:
                gimbal_cn = (devices["gimbal"].get("cn_names") or ["转台"])[0]
                gimbal_actions = devices["gimbal"].get("actions") or []
                if "scan_circle" in gimbal_actions:
                    param_items.append({"text": f"{gimbal_cn}顺时针周扫", "description": "顺时针全周扫描"})
                    param_items.append({"text": f"{gimbal_cn}逆时针周扫", "description": "逆时针全周扫描"})
                if "digital_guide" in gimbal_actions:
                    param_items.append({
                        "text": f"{gimbal_cn}转到方位82.67度俯仰10.5度",
                        "description": "按方位角和俯仰角控制朝向",
                    })
            # 检查是否有白光数字变倍
            if "vis_cam" in devices:
                vis_cn = (devices["vis_cam"].get("cn_names") or ["白光"])[0]
                vis_actions = devices["vis_cam"].get("actions") or []
                if "digital_zoom" in vis_actions:
                    param_items.append({"text": f"{vis_cn}数字变倍4倍", "description": "数字变倍（倍数）"})
            if param_items:
                commands.append({
                    "category": "带参数指令",
                    "icon": "📐",
                    "items": param_items,
                })

            # 状态查询
            functions = profile.get("functions") or []
            if "vehicle_status" in functions:
                status_items = []
                # 各设备状态查询
                for dev_id, spec in devices.items():
                    cn_name = (spec.get("cn_names") or [dev_id])[0]
                    status_items.append({
                        "text": f"{cn_name}什么状态",
                        "description": f"查询{cn_name}当前状态",
                    })
                # 全部设备状态
                status_items.append({"text": "所有设备什么状态", "description": "查询全部设备当前状态"})
                commands.append({
                    "category": "状态查询",
                    "icon": "📊",
                    "items": status_items,
                })

            # 通用指令
            general_items = []
            if "play_music" in functions:
                general_items.extend([
                    {"text": "播放音乐", "description": "随机播放一首歌"},
                    {"text": "下一首", "description": "切换到下一首歌"},
                    {"text": "停止播放", "description": "停止当前音乐"},
                ])
            if "get_datetime" in functions:
                general_items.extend([
                    {"text": "现在几点了", "description": "查询当前时间"},
                    {"text": "今天几号星期几", "description": "查询日期和星期"},
                ])
            vision_enabled = bool(
                (self.config.get("runtime_capabilities") or {}).get(
                    "vision_enabled", False
                )
            )
            if "vision_analyze" in functions and vision_enabled:
                general_items.append({
                    "text": "分析前方画面",
                    "description": "分析当前视频画面",
                })
            if general_items:
                commands.append({
                    "category": "通用功能",
                    "icon": "🔧",
                    "items": general_items,
                })

            # 对话示例
            commands.append({
                "category": "对话示例",
                "icon": "💬",
                "items": [
                    {"text": "你好", "description": "打招呼"},
                    {"text": "你是谁", "description": "查询助手身份"},
                    {"text": "你能控制哪些设备", "description": "查看设备列表"},
                    {"text": "再见", "description": "结束对话"},
                ],
            })

            resp = web.Response(
                text=json.dumps({
                    "vehicle_name": vehicle_name,
                    "profile_id": profile_name,
                    "commands": commands,
                }, ensure_ascii=False),
                content_type="application/json",
            )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"获取语音指令列表失败: {e}")
            resp = web.Response(
                text=json.dumps({"error": str(e)}, ensure_ascii=False),
                content_type="application/json", status=500,
            )
        finally:
            self._add_cors_headers(resp)
            return resp
