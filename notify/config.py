"""YAML configuration with ${ENV} expansion. Secrets never live in the file."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import KIND_OPT_IN_DEFAULT, Scope, Severity

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
# client names the service uses for itself; a configured client must not impersonate them
RESERVED_CLIENT_NAMES = frozenset({"admin-ui", "hnl-notify"})
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
TARGET_KINDS = ("user", "device", "group", "channel")


class ConfigError(Exception):
    pass


def expand_env(value: Any, missing: list[str], env: dict[str, str] | None = None) -> Any:
    env = os.environ if env is None else env
    if isinstance(value, str):
        def sub(m: re.Match[str]) -> str:
            name, default = m.group(1), m.group(2)
            if name in env and env[name] != "":
                return env[name]
            if default is not None:
                return default
            missing.append(name)
            return ""
        return _ENV_RE.sub(sub, value)
    if isinstance(value, list):
        return [expand_env(v, missing, env) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v, missing, env) for k, v in value.items()}
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServerConfig(_Strict):
    public_base_url: str | None = Field(default=None, description="Externally reachable base URL for provider callbacks")
    callback_secret: str | None = None
    max_attempts: int = Field(default=8, ge=1, le=50)
    retry_base_seconds: float = Field(default=10, ge=5)
    retry_max_seconds: float = Field(default=900, ge=5)
    lease_seconds: int = Field(default=60, ge=10)
    retention_days: int = Field(default=90, ge=1)
    webhook_max_attempts: int = Field(default=10, ge=1, le=50)
    webhook_timeout_seconds: float = Field(default=10, gt=0, le=60)
    webhook_disable_after_failures: int = Field(default=50, ge=1)
    # links in notifications sent by hnl-notify itself (e.g. access requests); falls back to NOTIFY_EXTERNAL_URL
    external_url: str | None = None
    attachment_url_hosts: list[str] = Field(default_factory=list, description="hosts, host:port or CIDRs URL attachments may be fetched from; empty = disabled")
    attachment_retention_days: int = Field(default=7, ge=1, le=90)

    @field_validator("attachment_url_hosts")
    @classmethod
    def _hosts(cls, v: list[str]) -> list[str]:
        import ipaddress
        out = []
        for h in v:
            h = h.strip().lower().replace(".:", ":")
            h = h[:-1] if h.endswith(".") else h
            try:
                out.append(str(ipaddress.ip_network(h, strict=False)))
                continue
            except ValueError:
                pass
            m = re.fullmatch(r"[a-z0-9.-]+(?::(\d{1,5}))?", h)
            if not m or (m.group(1) and not 0 < int(m.group(1)) < 65536):
                raise ValueError(f"{h!r} is not a host, host:port or CIDR")
            out.append(h)
        return out


class RegistrationConfig(_Strict):
    """Self-registration: apps request a token, the admin approves it."""
    enabled: bool = True
    notify_targets: list[str] = Field(default_factory=list, description="who is alerted; empty = every person")
    auto_approve_subnets: list[str] = Field(default_factory=list, description="CIDRs; empty = always ask")
    auto_approve_scopes: list[Scope] = Field(default_factory=lambda: [Scope.send])

    @field_validator("auto_approve_subnets")
    @classmethod
    def _cidrs(cls, v: list[str]) -> list[str]:
        import ipaddress
        out = []
        for c in v:
            try:
                out.append(str(ipaddress.ip_network(c.strip(), strict=False)))
            except ValueError:
                raise ValueError(f"{c!r} is not a subnet like 192.168.1.0/24") from None
        return out

    @field_validator("auto_approve_scopes")
    @classmethod
    def _no_admin(cls, v: list[Scope]) -> list[Scope]:
        if Scope.admin in v:
            raise ValueError("admin can never be granted automatically")
        return v


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: str


class DeviceConfig(_Strict):
    provider: str
    owner: str | None = None
    description: str | None = None
    enabled: bool = True
    opt_in: bool | None = Field(default=None, description="True = only reached when its kind is requested (or targeted directly); default per kind")
    min_severity: Severity = Severity.min
    address: dict[str, Any] = Field(default_factory=dict)
    capabilities: dict[str, Any] = Field(default_factory=dict, description="Narrow/override provider capabilities for this device")


class UserConfig(_Strict):
    display_name: str | None = None
    devices: list[str] = Field(default_factory=list)


class GroupConfig(_Strict):
    description: str | None = None
    members: list[str] = Field(default_factory=list)


class ChannelConfig(_Strict):
    description: str | None = None
    targets: list[str] = Field(default_factory=list)
    min_severity: Severity = Severity.min


class ClientConfig(_Strict):
    token_sha256: str
    scopes: list[Scope]
    description: str | None = None

    @field_validator("token_sha256", mode="before")
    @classmethod
    def _quoted(cls, v: Any) -> Any:
        if not isinstance(v, str):
            raise ValueError("token_sha256 must be a quoted string in YAML (unquoted digits are parsed as a number)")
        return v

    @field_validator("token_sha256")
    @classmethod
    def _hex(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", v):
            raise ValueError("token_sha256 must be 64 hex characters (use: python -m notify new-token NAME)")
        return v


class NotifyConfig(_Strict):
    server: ServerConfig = Field(default_factory=ServerConfig)
    registration: RegistrationConfig = Field(default_factory=RegistrationConfig)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    devices: dict[str, DeviceConfig] = Field(default_factory=dict)
    users: dict[str, UserConfig] = Field(default_factory=dict)
    groups: dict[str, GroupConfig] = Field(default_factory=dict)
    channels: dict[str, ChannelConfig] = Field(default_factory=dict)
    clients: dict[str, ClientConfig] = Field(default_factory=dict)

    # populated by load_config, not part of the file
    missing_env: list[str] = Field(default_factory=list, exclude=True)

    @model_validator(mode="after")
    def _references(self) -> "NotifyConfig":
        for kind in ("devices", "users", "groups", "channels", "clients"):
            for name in getattr(self, kind):
                if not _NAME_RE.match(name):
                    raise ValueError(f"{kind}.{name}: names must match {_NAME_RE.pattern}")
                if kind == "clients" and name in RESERVED_CLIENT_NAMES:
                    raise ValueError(f"clients.{name}: name is reserved")
        for name, d in self.devices.items():
            if d.provider not in self.providers:
                raise ValueError(f"device {name}: unknown provider {d.provider!r}")
            if d.owner and d.owner not in self.users:
                raise ValueError(f"device {name}: unknown owner {d.owner!r}")
        for name, u in self.users.items():
            for dev in u.devices:
                if dev not in self.devices:
                    raise ValueError(f"user {name}: unknown device {dev!r}")
        for name, g in self.groups.items():
            for t in g.members:
                self._check_ref(f"group {name}", t)
        for name, c in self.channels.items():
            for t in c.targets:
                self._check_ref(f"channel {name}", t)
        for t in self.registration.notify_targets:
            if t.startswith("channel:"):
                if t.split(":", 1)[1] not in self.channels:
                    raise ValueError(f"registration.notify_targets: unknown channel {t[8:]!r}")
            else:
                self._check_ref("registration.notify_targets", t)
        return self

    def _check_ref(self, where: str, ref: str) -> None:
        kind, _, name = ref.partition(":")
        if kind not in ("user", "device", "group") or not name:
            raise ValueError(f"{where}: member {ref!r} must be user:NAME, device:NAME or group:NAME")
        if name not in getattr(self, kind + "s"):
            raise ValueError(f"{where}: unknown {kind} {name!r}")

    def device_kind(self, name: str) -> str:
        from .providers import REGISTRY
        cls = REGISTRY.get(self.providers[self.devices[name].provider].type)
        return cls.kind if cls else ""

    def device_opt_in(self, name: str) -> bool:
        opt_in = self.devices[name].opt_in
        return opt_in if opt_in is not None else KIND_OPT_IN_DEFAULT.get(self.device_kind(name), False)

    def devices_of_user(self, user: str) -> list[str]:
        """Devices listed on the user plus devices declaring the user as owner."""
        out = list(self.users[user].devices)
        for name, d in self.devices.items():
            if d.owner == user and name not in out:
                out.append(name)
        return out


def load_config(path: str | Path, env: dict[str, str] | None = None) -> NotifyConfig:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {p}: {e}") from e
    missing: list[str] = []
    expanded = expand_env(raw, missing, env)
    try:
        cfg = NotifyConfig.model_validate(expanded)
    except Exception as e:  # pydantic.ValidationError
        raise ConfigError(f"invalid config {p}: {e}") from e
    cfg.missing_env = sorted(set(missing))
    return cfg
