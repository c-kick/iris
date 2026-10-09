"""Resolve logical targets (user/device/group/channel) into devices."""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import TARGET_KINDS, NotifyConfig
from .models import Severity


class RoutingError(ValueError):
    pass


@dataclass
class RoutePlan:
    devices: dict[str, list[str]] = field(default_factory=dict)  # device -> via targets
    suppressed: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"devices": [{"device": d, "via": v} for d, v in self.devices.items()],
                "suppressed": self.suppressed}


class Router:
    def __init__(self, config: NotifyConfig):
        self.config = config

    def normalize(self, target: str) -> tuple[str, str]:
        target = target.strip()
        kind, sep, name = target.partition(":")
        if sep:
            if kind not in TARGET_KINDS:
                raise RoutingError(f"unknown target kind {kind!r} in {target!r}; use {', '.join(k + ':NAME' for k in TARGET_KINDS)}")
            if name not in getattr(self.config, kind + "s"):
                raise RoutingError(f"unknown {kind} {name!r}")
            return kind, name
        matches = [k for k in TARGET_KINDS if target in getattr(self.config, k + "s")]
        if not matches:
            raise RoutingError(f"unknown target {target!r}")
        if len(matches) > 1:
            raise RoutingError(f"ambiguous target {target!r}: matches {', '.join(m + ':' + target for m in matches)}")
        return matches[0], target

    def resolve(self, targets: list[str], severity: Severity, kinds: list[str] | None = None) -> RoutePlan:
        plan = RoutePlan()
        for t in targets:
            kind, name = self.normalize(t)
            top = f"{kind}:{name}"
            found: set[str] = set()  # kinds of devices reached (before severity/enabled filtering)
            self._expand(kind, name, severity, top, plan, set(), kinds, kind == "device", found)
            cut = kind == "channel" and severity.rank < self.config.channels[name].min_severity.rank
            for k in () if cut else kinds or ():
                if k not in found:
                    plan.suppressed.append({"target": top, "via": top, "reason": f"no {k} device"})
        return plan

    def _expand(self, kind: str, name: str, severity: Severity, via: str, plan: RoutePlan, seen: set[str],
                kinds: list[str] | None, top_level: bool, found: set[str]) -> None:
        key = f"{kind}:{name}"
        if key in seen:
            return
        seen = seen | {key}
        cfg = self.config
        if kind == "device":
            dev = cfg.devices[name]
            dk = cfg.device_kind(name)
            if kinds is None or dk in kinds:
                found.add(dk)
            if kinds is not None and dk not in kinds:
                if top_level:
                    plan.suppressed.append({"target": key, "via": via, "reason": f"device kind {dk} not requested"})
                return
            if kinds is None and cfg.device_opt_in(name) and not top_level:
                return
            if not dev.enabled:
                plan.suppressed.append({"target": key, "via": via, "reason": "device disabled"})
                return
            if severity.rank < dev.min_severity.rank:
                plan.suppressed.append({"target": key, "via": via,
                                        "reason": f"below device min_severity {dev.min_severity.value}"})
                return
            vias = plan.devices.setdefault(name, [])
            if via not in vias:
                vias.append(via)
        elif kind == "user":
            devices = cfg.devices_of_user(name)
            if not devices:
                plan.suppressed.append({"target": key, "via": via, "reason": "user has no devices"})
            for d in devices:
                self._expand("device", d, severity, via, plan, seen, kinds, False, found)
        elif kind == "group":
            for m in cfg.groups[name].members:
                k, n = m.split(":", 1)
                self._expand(k, n, severity, via, plan, seen, kinds, False, found)
        elif kind == "channel":
            ch = cfg.channels[name]
            if severity.rank < ch.min_severity.rank:
                plan.suppressed.append({"target": key, "via": via,
                                        "reason": f"below channel min_severity {ch.min_severity.value}"})
                return
            for m in ch.targets:
                k, n = m.split(":", 1)
                self._expand(k, n, severity, via, plan, seen, kinds, False, found)
