"""
vehicle_control 车型能力描述数据类。

从 vehicle_profile YAML 的 vehicle_control 节加载，
供 vehicle_control.py 运行时使用（设备校验 + 中文回复生成）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class DeviceSpec:
    device_id: str
    cn_names: List[str]   # 中文别名列表（语音回复用）
    actions: List[str]    # 合法 action 白名单（空列表 = 不限制）


@dataclass
class VehicleControlProfile:
    vehicle_id: str                  # 车辆标识
    devices: Dict[str, DeviceSpec]   # device_id → DeviceSpec
    all_targets: Dict[str, List[str]] = field(default_factory=dict)
    param_rules: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # ──────────────────────────────────────────────────────────────
    @classmethod
    def from_profile(cls, config: dict) -> "VehicleControlProfile":
        """
        从已合并的 config（经 _apply_vehicle_profile 处理后）
        提取 vehicle_control 配置并构建实例。
        """
        vc: dict = config.get("vehicle_control") or {}
        vehicle_id: str = config.get("vehicle_id", "unknown")

        devices: Dict[str, DeviceSpec] = {}
        for dev_id, spec in (vc.get("devices") or {}).items():
            devices[dev_id] = DeviceSpec(
                device_id=dev_id,
                cn_names=spec.get("cn_names") or [],
                actions=spec.get("actions") or [],
            )

        all_targets: Dict[str, List[str]] = {}
        for action, targets in (vc.get("all_targets") or {}).items():
            if isinstance(targets, list):
                all_targets[str(action)] = [
                    str(target) for target in targets if isinstance(target, str)
                ]

        param_rules = vc.get("param_rules") or {}
        if not isinstance(param_rules, dict):
            param_rules = {}

        return cls(
            vehicle_id=vehicle_id,
            devices=devices,
            all_targets=all_targets,
            param_rules=param_rules,
        )

    # ──────────────────────────────────────────────────────────────
    def is_valid_device(self, device_id: str) -> bool:
        return device_id in self.devices

    def is_valid_action(self, device_id: str, action: str) -> bool:
        """
        校验 device 支持该 action。
        若该 device 的 actions 列表为空，视为不限制（兼容旧配置）。
        """
        spec = self.devices.get(device_id)
        if spec is None:
            return False
        return (not spec.actions) or (action in spec.actions)

    def device_cn_name(self, device_id: str) -> str:
        """返回设备的第一个中文名，用于语音回复。"""
        spec = self.devices.get(device_id)
        if spec and spec.cn_names:
            return spec.cn_names[0]
        return device_id

    def targets_for_all(self, action: str) -> List[str]:
        """返回“所有设备”执行指定动作时应控制的设备，保持配置顺序。"""
        configured = self.all_targets.get(action)
        candidates = configured if configured is not None else list(self.devices.keys())

        targets = []
        for device_id in candidates:
            if device_id not in targets and self.is_valid_action(device_id, action):
                targets.append(device_id)
        return targets
