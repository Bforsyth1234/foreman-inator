"""Typed ``config.toml`` with API keys held in the macOS Keychain.

The file lives at ``paths.config_path()``. It never holds a secret: a cloud
model names its key with a reference ``keychain:foreman-inator/<name>``, and
the key itself is stored with ``set_key(name, value)`` (CLI:
``fman config set-key <name>``) and read through ``keyring`` only when that
model is actually called (``ModelEndpoint.api_key()``).

Layout (every key optional; missing keys take the defaults below)::

    app_id = 123456
    app_slug = "foreman-inator-you"
    installation_ids = [7890]
    allowed_users = ["you"]
    repos = ["you/project"]
    poll_interval_seconds = 45
    reviewer_models = ["claude-opus", "glm"]
    max_parallel_per_repo = 1   # tickets worked at once per repo (file locks permit)
    router_timeout_seconds = 30
    sandbox = "on"       # on | off: model-written code runs under sandbox-exec (#40)
    clarify = true       # ask on a vague issue before working on it (#44)

    [sandbox_network]    # per repo: "allow" opens outbound network for its verify + checks
    "you/project" = "none"

    [spend_caps]
    daily_usd = 5.0
    monthly_usd = 50.0
    experiments_monthly_usd = 10.0

    [[models]]           # one table per model; order inside a tier = try order
    name = "glm"
    tier = 3
    kind = "cloud"       # local | cloud
    model = "glm-5.3"
    base_url = "https://api.z.ai/api/coding/paas/v4"
    family = "glm"       # reviewer must be a different family from the writer
    api_key_ref = "keychain:foreman-inator/glm"

Unknown keys are an error, and so is anything that looks like a plain-text key.
"""

import ipaddress
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import keyring
import tomli_w

from foreman_inator import paths

KEYCHAIN_SERVICE = "foreman-inator"
KEY_REF_PREFIX = f"keychain:{KEYCHAIN_SERVICE}/"
KEY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TIERS = (1, 2, 3)
MIN_POLL_SECONDS = 5
MAX_PARALLEL_PER_REPO = 16
SANDBOX_MODES = ("on", "off")
SANDBOX_NETWORKS = ("none", "allow")

Kind = Literal["local", "cloud"]

# Keys whose presence means someone pasted a secret into the file.
_SECRET_KEYS = {"api_key", "apikey", "key", "token", "secret", "password"}
# Value shapes of common API keys, caught wherever they appear.
_SECRET_VALUE_RE = re.compile(r"^(sk-|sk_|ghp_|gho_|ghs_|github_pat_|xox[bp]-|AKIA)")


class ConfigError(ValueError):
    """config.toml is invalid, or a key it references cannot be resolved."""


def _set_key_hint(name: str) -> str:
    return (
        f"Store the key in the Keychain with `fman config set-key {name}` and set "
        f'api_key_ref = "{KEY_REF_PREFIX}{name}" instead.'
    )


# --------------------------------------------------------------------------- Keychain


def key_ref(name: str) -> str:
    """The config reference for Keychain item ``name``."""
    _check_key_name(name)
    return KEY_REF_PREFIX + name


def parse_key_ref(ref: str) -> str:
    """Return the Keychain item name inside ``ref``, or raise ConfigError."""
    if not ref.startswith(KEY_REF_PREFIX):
        raise ConfigError(
            f"api_key_ref must look like '{KEY_REF_PREFIX}<name>'; a plain-text API key is "
            f"not allowed in config.toml. {_set_key_hint('<name>')}"
        )
    name = ref[len(KEY_REF_PREFIX) :]
    _check_key_name(name)
    return name


def _check_key_name(name: str) -> None:
    if not KEY_NAME_RE.match(name):
        raise ConfigError(
            f"invalid key name {name!r}: use letters, digits, '.', '_' or '-' (max 64)"
        )


def set_key(name: str, value: str) -> str:
    """Store API key ``value`` in the Keychain as ``name``; return its config reference."""
    ref = key_ref(name)
    if not value.strip():
        raise ConfigError("refusing to store an empty API key")
    keyring.set_password(KEYCHAIN_SERVICE, name, value.strip())
    return ref


def resolve_key(ref: str) -> str:
    """Look up the key behind ``ref`` in the Keychain. Raises ConfigError if missing."""
    name = parse_key_ref(ref)
    value = keyring.get_password(KEYCHAIN_SERVICE, name)
    if not value:
        raise ConfigError(
            f"no Keychain item '{name}' in service '{KEYCHAIN_SERVICE}'. "
            f"Add it with `fman config set-key {name}`."
        )
    return value


# --------------------------------------------------------------------------- model


@dataclass(frozen=True)
class ModelEndpoint:
    """One model the worker can call, through an OpenAI-compatible endpoint."""

    name: str  # unique handle used in config and attempts, e.g. "inkling-2-mini"
    tier: int  # 1, 2 or 3
    kind: Kind  # local (this Mac) or cloud (pay per use, counts toward spend caps)
    model: str  # model id sent to the endpoint
    base_url: str  # OpenAI-compatible base URL
    family: str  # e.g. "inkling", "qwen", "glm", "claude"; reviewer independence compares this
    api_key_ref: str | None = None  # keychain:foreman-inator/<name>

    @property
    def is_cloud(self) -> bool:
        return self.kind == "cloud"

    def api_key(self) -> str | None:
        """Resolve the key lazily from the Keychain; None for a model without one."""
        return resolve_key(self.api_key_ref) if self.api_key_ref else None


@dataclass(frozen=True)
class SpendCaps:
    """Cloud spend caps in USD. When one is hit, tier 3 waits for the user."""

    daily_usd: float = 5.0
    monthly_usd: float = 50.0
    experiments_monthly_usd: float = 10.0


OLLAMA_URL = "http://127.0.0.1:11434/v1"


def default_models() -> tuple[ModelEndpoint, ...]:
    """The default tier picks. Tier 3 is tried in this order: GLM, Claude Opus, Claude Fable."""
    return (
        ModelEndpoint("inkling-2-mini", 1, "local", "inkling-2-mini", OLLAMA_URL, "inkling"),
        ModelEndpoint("qwen3.8-27b", 2, "local", "qwen3.8:27b", OLLAMA_URL, "qwen"),
        ModelEndpoint(
            "glm",
            3,
            "cloud",
            "glm-5.3",
            "https://api.z.ai/api/coding/paas/v4",
            "glm",
            key_ref("glm"),
        ),
        ModelEndpoint(
            "claude-opus",
            3,
            "cloud",
            "claude-opus-5-5",
            "https://api.anthropic.com/v1",
            "claude",
            key_ref("anthropic"),
        ),
        ModelEndpoint(
            "claude-fable",
            3,
            "cloud",
            "claude-fable-5-2",
            "https://api.anthropic.com/v1",
            "claude",
            key_ref("anthropic"),
        ),
    )


@dataclass(frozen=True)
class Config:
    """The whole of config.toml. Every later module reads this."""

    app_id: int | None = None
    app_slug: str | None = None
    installation_ids: tuple[int, ...] = ()
    allowed_users: tuple[str, ...] = ()
    repos: tuple[str, ...] = ()  # "owner/name"
    poll_interval_seconds: int = 45
    spend_caps: SpendCaps = field(default_factory=SpendCaps)
    models: tuple[ModelEndpoint, ...] = field(default_factory=default_models)
    reviewer_models: tuple[str, ...] = ("claude-opus", "glm")  # preference order
    #: Tickets of one repo worked at the same time, as far as file locks permit (#21).
    #: 1 (the default) suits one local GPU.
    max_parallel_per_repo: int = 1
    #: Whole-call bound for the router's classification (#15); a cold local model load
    #: can take longer than the router's own 10 s default.
    router_timeout_seconds: float = 30.0
    #: "on" (default): OpenCode, verify and the reviewer's checks run under the sandbox
    #: (#40), and an attempt that cannot be sandboxed is not run. "off" runs them
    #: unsandboxed; `fman status` and the PR description say so.
    sandbox: str = "on"
    #: Per repo ("owner/name" -> "none" | "allow"): "allow" opens outbound network for
    #: that repo's verify command and reviewer checks (never for the writer, which only
    #: reaches the proxy). Repos not listed get "none".
    #: Stored as sorted ``(repo, network)`` pairs so Config stays hashable; a dict is
    #: accepted and converted.
    sandbox_network: tuple[tuple[str, str], ...] = ()
    #: #44: before a ticket is routed, a small local model checks the issue can be worked
    #: on as written; if not, the worker asks on the issue (``needs-info``) and waits for
    #: an allowed user. False turns the check off.
    clarify: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.sandbox_network, Mapping):
            pairs = tuple(sorted(self.sandbox_network.items()))
            object.__setattr__(self, "sandbox_network", pairs)
        _validate(self)

    def model(self, name: str) -> ModelEndpoint:
        for m in self.models:
            if m.name == name:
                return m
        raise ConfigError(f"no model named {name!r} in config")

    def models_for_tier(self, tier: int) -> tuple[ModelEndpoint, ...]:
        """Models of ``tier`` in try order (tier 3: GLM first, then Claude)."""
        return tuple(m for m in self.models if m.tier == tier)

    def reviewer_for(self, writer: ModelEndpoint) -> ModelEndpoint | None:
        """First reviewer whose family differs from the writer's, or None (hand to user)."""
        for name in self.reviewer_models:
            candidate = self.model(name)
            if candidate.family != writer.family:
                return candidate
        return None

    def is_allowed_user(self, login: str) -> bool:
        return login.lower() in {u.lower() for u in self.allowed_users}

    def sandbox_network_for(self, repo: str) -> str:
        """``"allow"`` or ``"none"``: the verify/check network for ``repo`` (#40)."""
        lowered = {k.lower(): v for k, v in self.sandbox_network}
        return lowered.get(repo.lower(), "none")


# --------------------------------------------------------------------------- validation


def _validate(cfg: Config) -> None:
    if cfg.poll_interval_seconds < MIN_POLL_SECONDS:
        raise ConfigError(f"poll_interval_seconds must be >= {MIN_POLL_SECONDS}")
    if not 1 <= cfg.max_parallel_per_repo <= MAX_PARALLEL_PER_REPO:
        raise ConfigError(f"max_parallel_per_repo must be between 1 and {MAX_PARALLEL_PER_REPO}")
    if not 0 < cfg.router_timeout_seconds <= 600:
        raise ConfigError("router_timeout_seconds must be > 0 and <= 600")
    for cap in fields(SpendCaps):
        if getattr(cfg.spend_caps, cap.name) < 0:
            raise ConfigError(f"spend_caps.{cap.name} must be >= 0")
    for repo in cfg.repos:
        if not re.match(r"^[\w.-]+/[\w.-]+$", repo):
            raise ConfigError(f"repo {repo!r} must look like 'owner/name'")
    if not isinstance(cfg.clarify, bool):
        raise ConfigError("clarify must be true or false")
    if cfg.sandbox not in SANDBOX_MODES:
        raise ConfigError(f"sandbox must be one of {', '.join(SANDBOX_MODES)}")
    for repo, net in cfg.sandbox_network:
        if not re.match(r"^[\w.-]+/[\w.-]+$", repo):
            raise ConfigError(f"sandbox_network: repo {repo!r} must look like 'owner/name'")
        if net not in SANDBOX_NETWORKS:
            raise ConfigError(
                f"sandbox_network.{repo} must be one of {', '.join(SANDBOX_NETWORKS)}"
            )
    names = [m.name for m in cfg.models]
    if len(set(names)) != len(names):
        raise ConfigError("model names must be unique")
    for m in cfg.models:
        _validate_model(m)
    for tier in TIERS:
        if not cfg.models_for_tier(tier):
            raise ConfigError(f"tier {tier} has no model")
    for name in cfg.reviewer_models:
        if name not in names:
            raise ConfigError(f"reviewer_models names unknown model {name!r}")


def _validate_model(m: ModelEndpoint) -> None:
    where = f"model {m.name!r}"
    if m.tier not in TIERS:
        raise ConfigError(f"{where}: tier must be one of {TIERS}")
    if m.kind not in ("local", "cloud"):
        raise ConfigError(f"{where}: kind must be 'local' or 'cloud'")
    if not m.family:
        raise ConfigError(f"{where}: family is required")
    url = urlparse(m.base_url)
    if url.scheme not in ("http", "https") or not url.hostname:
        raise ConfigError(f"{where}: base_url must be an http(s) URL")
    if m.kind == "local" and not _is_loopback(url.hostname):
        raise ConfigError(f"{where}: a local model must be on localhost; use kind = 'cloud'")
    if m.kind == "cloud":
        if url.scheme != "https":
            raise ConfigError(f"{where}: a cloud base_url must use https")
        if not m.api_key_ref:
            raise ConfigError(f"{where}: a cloud model needs api_key_ref. {_set_key_hint(m.name)}")
    if m.api_key_ref is not None:
        parse_key_ref(m.api_key_ref)


def _is_loopback(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _reject_plaintext_secrets(data: Any, where: str = "config") -> None:
    """Fail on any key named like a secret or any value shaped like an API key."""
    if isinstance(data, Mapping):
        for k, v in data.items():
            if str(k).lower() in _SECRET_KEYS:
                raise ConfigError(
                    f"{where}.{k}: plain-text API keys are not allowed in config.toml. "
                    f"{_set_key_hint('<name>')}"
                )
            _reject_plaintext_secrets(v, f"{where}.{k}")
    elif isinstance(data, list):
        for i, v in enumerate(data):
            _reject_plaintext_secrets(v, f"{where}[{i}]")
    elif isinstance(data, str) and _SECRET_VALUE_RE.match(data):
        raise ConfigError(
            f"{where}: looks like a plain-text API key, which is not allowed in config.toml. "
            f"{_set_key_hint('<name>')}"
        )


# --------------------------------------------------------------------------- (de)serialize


def _check_keys(data: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(unknown)}")


def _typed(value: Any, typ: type | tuple[type, ...], where: str) -> Any:
    if isinstance(value, bool) and bool not in (typ if isinstance(typ, tuple) else (typ,)):
        raise ConfigError(f"{where}: wrong type {type(value).__name__}")
    if not isinstance(value, typ):
        raise ConfigError(f"{where}: wrong type {type(value).__name__}")
    return value


def _str_list(value: Any, where: str) -> tuple[str, ...]:
    _typed(value, list, where)
    return tuple(_typed(v, str, f"{where}[]") for v in value)


def _model_from_dict(raw: Any, where: str) -> ModelEndpoint:
    _typed(raw, dict, where)
    allowed = {f.name for f in fields(ModelEndpoint)}
    _check_keys(raw, allowed, where)
    missing = sorted(allowed - {"api_key_ref"} - set(raw))
    if missing:
        raise ConfigError(f"{where}: missing key(s) {', '.join(missing)}")
    return ModelEndpoint(
        name=_typed(raw["name"], str, f"{where}.name"),
        tier=_typed(raw["tier"], int, f"{where}.tier"),
        kind=_typed(raw["kind"], str, f"{where}.kind"),  # type: ignore[arg-type]
        model=_typed(raw["model"], str, f"{where}.model"),
        base_url=_typed(raw["base_url"], str, f"{where}.base_url"),
        family=_typed(raw["family"], str, f"{where}.family"),
        api_key_ref=_typed(raw.get("api_key_ref"), (str, type(None)), f"{where}.api_key_ref"),
    )


def from_dict(data: Mapping[str, Any]) -> Config:
    """Build a Config from parsed TOML, applying defaults and strict validation."""
    _reject_plaintext_secrets(data)
    _check_keys(data, {f.name for f in fields(Config)}, "config")
    kw: dict[str, Any] = {}
    if "app_id" in data:
        kw["app_id"] = _typed(data["app_id"], int, "app_id")
    if "app_slug" in data:
        kw["app_slug"] = _typed(data["app_slug"], str, "app_slug")
    if "installation_ids" in data:
        ids = _typed(data["installation_ids"], list, "installation_ids")
        kw["installation_ids"] = tuple(_typed(i, int, "installation_ids[]") for i in ids)
    for key in ("allowed_users", "repos", "reviewer_models"):
        if key in data:
            kw[key] = _str_list(data[key], key)
    if "poll_interval_seconds" in data:
        kw["poll_interval_seconds"] = _typed(
            data["poll_interval_seconds"], int, "poll_interval_seconds"
        )
    if "max_parallel_per_repo" in data:
        kw["max_parallel_per_repo"] = _typed(
            data["max_parallel_per_repo"], int, "max_parallel_per_repo"
        )
    if "router_timeout_seconds" in data:
        kw["router_timeout_seconds"] = float(
            _typed(data["router_timeout_seconds"], (int, float), "router_timeout_seconds")
        )
    if "sandbox" in data:
        kw["sandbox"] = _typed(data["sandbox"], str, "sandbox")
    if "clarify" in data:
        kw["clarify"] = _typed(data["clarify"], bool, "clarify")
    if "sandbox_network" in data:
        nets = _typed(data["sandbox_network"], dict, "sandbox_network")
        kw["sandbox_network"] = {
            _typed(k, str, "sandbox_network"): _typed(v, str, f"sandbox_network.{k}")
            for k, v in nets.items()
        }
    if "spend_caps" in data:
        caps = _typed(data["spend_caps"], dict, "spend_caps")
        _check_keys(caps, {f.name for f in fields(SpendCaps)}, "spend_caps")
        kw["spend_caps"] = SpendCaps(
            **{k: float(_typed(v, (int, float), f"spend_caps.{k}")) for k, v in caps.items()}
        )
    if "models" in data:
        raw_models = _typed(data["models"], list, "models")
        kw["models"] = tuple(_model_from_dict(m, f"models[{i}]") for i, m in enumerate(raw_models))
    return Config(**kw)


def to_dict(cfg: Config) -> dict[str, Any]:
    """The TOML-ready form of ``cfg`` (None values omitted)."""
    out: dict[str, Any] = {}
    if cfg.app_id is not None:
        out["app_id"] = cfg.app_id
    if cfg.app_slug is not None:
        out["app_slug"] = cfg.app_slug
    out["installation_ids"] = list(cfg.installation_ids)
    out["allowed_users"] = list(cfg.allowed_users)
    out["repos"] = list(cfg.repos)
    out["poll_interval_seconds"] = cfg.poll_interval_seconds
    out["reviewer_models"] = list(cfg.reviewer_models)
    out["max_parallel_per_repo"] = cfg.max_parallel_per_repo
    out["router_timeout_seconds"] = cfg.router_timeout_seconds
    out["sandbox"] = cfg.sandbox
    out["clarify"] = cfg.clarify
    if cfg.sandbox_network:
        out["sandbox_network"] = dict(cfg.sandbox_network)
    out["spend_caps"] = {f.name: getattr(cfg.spend_caps, f.name) for f in fields(SpendCaps)}
    out["models"] = [
        {
            f.name: getattr(m, f.name)
            for f in fields(ModelEndpoint)
            if getattr(m, f.name) is not None
        }
        for m in cfg.models
    ]
    return out


def load(path: Path | None = None) -> Config:
    """Read config.toml; a missing file gives the defaults."""
    path = path or paths.config_path()
    if not path.exists():
        return Config()
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    try:
        return from_dict(data)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def save(cfg: Config, path: Path | None = None) -> Path:
    """Write ``cfg`` to config.toml with mode 600."""
    path = path or paths.config_path()
    return paths.write_secret_file(path, tomli_w.dumps(to_dict(cfg)))


__all__ = [
    "KEYCHAIN_SERVICE",
    "KEY_REF_PREFIX",
    "Config",
    "ConfigError",
    "ModelEndpoint",
    "SpendCaps",
    "default_models",
    "from_dict",
    "key_ref",
    "load",
    "parse_key_ref",
    "resolve_key",
    "save",
    "set_key",
    "to_dict",
]
