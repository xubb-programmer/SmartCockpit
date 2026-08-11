import os
import re
import yaml
from collections.abc import Mapping
from config.manage_api_client import init_service, get_server_config, get_agent_models

_VEHICLE_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def get_project_dir():
    """获取项目根目录"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/"


def read_config(config_path):
    with open(config_path, "r", encoding="utf-8") as file:
        config = yaml.safe_load(file)
    return config


def load_config():
    """加载配置文件"""
    from core.utils.cache.manager import cache_manager, CacheType

    # 检查缓存
    cached_config = cache_manager.get(CacheType.CONFIG, "main_config")
    if cached_config is not None:
        return cached_config

    default_config_path = get_project_dir() + "config.yaml"
    custom_config_path = get_project_dir() + "data/.config.yaml"

    # 加载默认配置
    default_config = read_config(default_config_path)
    custom_config = read_config(custom_config_path)

    if custom_config.get("manager-api", {}).get("url"):
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            # 如果已经在事件循环中，使用异步版本
            config = asyncio.run_coroutine_threadsafe(
                get_config_from_api_async(custom_config), loop
            ).result()
        except RuntimeError:
            # 如果不在事件循环中（启动时），创建新的事件循环
            config = asyncio.run(get_config_from_api_async(custom_config))
    else:
        # 合并配置
        config = merge_configs(default_config, custom_config)
    # 初始化目录
    ensure_directories(config)

    # ── 车型 Profile 覆盖 ──────────────────────────────────────
    config = _apply_vehicle_profile(config)

    # 缓存配置
    cache_manager.set(CacheType.CONFIG, "main_config", config)
    return config


async def get_config_from_api_async(config):
    """从Java API获取配置（异步版本）"""
    # 初始化API客户端
    init_service(config)

    # 获取服务器配置
    config_data = await get_server_config()
    if config_data is None:
        raise Exception("Failed to fetch server config from API")

    config_data["read_config_from_api"] = True
    config_data["manager-api"] = {
        "url": config["manager-api"].get("url", ""),
        "secret": config["manager-api"].get("secret", ""),
    }
    auth_enabled = config_data.get("server", {}).get("auth", {}).get("enabled", False)
    # server的配置以本地为准
    if config.get("server"):
        config_data["server"] = {
            "ip": config["server"].get("ip", ""),
            "port": config["server"].get("port", ""),
            "http_port": config["server"].get("http_port", ""),
            "vision_explain": config["server"].get("vision_explain", ""),
            "auth_key": config["server"].get("auth_key", ""),
        }
    config_data["server"]["auth"] = {"enabled": auth_enabled}
    # 如果服务器没有prompt_template，则从本地配置读取
    if not config_data.get("prompt_template"):
        config_data["prompt_template"] = config.get("prompt_template")
    return config_data


async def get_private_config_from_api(config, device_id, client_id):
    """从Java API获取私有配置"""
    return await get_agent_models(device_id, client_id, config["selected_module"])


def ensure_directories(config):
    """确保所有配置路径存在"""
    dirs_to_create = set()
    project_dir = get_project_dir()  # 获取项目根目录
    # 日志文件目录
    log_dir = config.get("log", {}).get("log_dir", "tmp")
    dirs_to_create.add(os.path.join(project_dir, log_dir))

    # ASR/TTS模块输出目录
    for module in ["ASR", "TTS"]:
        if config.get(module) is None:
            continue
        for provider in config.get(module, {}).values():
            output_dir = provider.get("output_dir", "")
            if output_dir:
                dirs_to_create.add(output_dir)

    # 根据selected_module创建模型目录
    selected_modules = config.get("selected_module", {})
    for module_type in ["ASR", "LLM", "TTS"]:
        selected_provider = selected_modules.get(module_type)
        if not selected_provider:
            continue
        if config.get(module_type) is None:
            continue
        if config[module_type].get(selected_provider) is None:
            continue
        provider_config = config.get(module_type, {}).get(selected_provider, {})
        output_dir = provider_config.get("output_dir")
        if output_dir:
            full_model_dir = os.path.join(project_dir, output_dir)
            dirs_to_create.add(full_model_dir)

    # 统一创建目录（保留原data目录创建）
    for dir_path in dirs_to_create:
        try:
            os.makedirs(dir_path, exist_ok=True)
        except PermissionError:
            print(f"警告：无法创建目录 {dir_path}，请检查写入权限")


def merge_configs(default_config, custom_config):
    """
    递归合并配置，custom_config优先级更高

    Args:
        default_config: 默认配置
        custom_config: 用户自定义配置

    Returns:
        合并后的配置
    """
    if not isinstance(default_config, Mapping) or not isinstance(
        custom_config, Mapping
    ):
        return custom_config

    merged = dict(default_config)

    for key, value in custom_config.items():
        if (
            key in merged
            and isinstance(merged[key], Mapping)
            and isinstance(value, Mapping)
        ):
            merged[key] = merge_configs(merged[key], value)
        else:
            merged[key] = value

    return merged


def _resolve_vehicle_profile_path(profile_name):
    if not isinstance(profile_name, str) or not _VEHICLE_PROFILE_ID_RE.fullmatch(
        profile_name
    ):
        return None

    profiles_dir = os.path.realpath(
        os.path.join(get_project_dir(), "data", "vehicle_profiles")
    )
    profile_path = os.path.realpath(os.path.join(profiles_dir, f"{profile_name}.yaml"))
    try:
        if os.path.commonpath([profiles_dir, profile_path]) != profiles_dir:
            return None
    except ValueError:
        return None
    return profile_path


def _apply_vehicle_profile(config):
    """
    如果配置中指定了 vehicle_profile，从 data/vehicle_profiles/<name>.yaml
    加载车型专属配置并覆盖 prompt / prompt_template / tool_calling_rules / functions。
    """
    profile_name = config.get("vehicle_profile")
    if not profile_name:
        return config

    profile_path = _resolve_vehicle_profile_path(profile_name)
    if not profile_path:
        print(f"[Profile] 警告：车型配置名称非法: {profile_name}")
        return config

    if not os.path.exists(profile_path):
        print(f"[Profile] 警告：车型配置文件不存在: {profile_path}")
        return config

    with open(profile_path, "r", encoding="utf-8") as f:
        profile = yaml.safe_load(f) or {}

    display_name = profile.get("name", profile_name)
    print(f"[Profile] 加载车型配置: {display_name} ({profile_path})")

    # 覆盖提示词相关字段
    for key in ("prompt_template", "prompt", "tool_calling_rules"):
        if key in profile:
            config[key] = profile[key]

    # 覆盖工具白名单
    if "functions" in profile:
        intent_key = config.get("selected_module", {}).get("Intent", "function_call")
        if "Intent" in config and intent_key in config["Intent"]:
            config["Intent"][intent_key]["functions"] = profile["functions"]

    # 透传车型专属字段（vehicle_control 插件运行时读取）
    for key in ("vehicle_id", "vehicle_control"):
        if key in profile:
            config[key] = profile[key]

    return config
