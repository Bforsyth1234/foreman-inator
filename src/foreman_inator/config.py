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
    writer_timeout_seconds = 1200  # one writer session; raise it for large repos
    sandbox = "on"       # on | off: model-written code runs under sandbox-exec (#40)
    clarify = true       # ask on a vague issue before working on it (#44)
    embedding_model = "embeddinggemma"   # kNN router (#23); local, never a writer rung
    knn_threshold = 50   # accepted tickets before the kNN start rule replaces the rules
    explore_rate = 0.10  # share of tickets started one tier lower (#23)
    failure_labels = true  # label every kickback's failure step (#27), local model
    attempt_budget_usd = 1.50   # one attempt's cloud spend; over it the ticket is handed over (#58)
    retention_days = 90  # `fman prune` and the worker's daily prune keep this long (#56)
    install_refill_cache = true  # refill an empty install cache from the base commit (#57)

    [experiments]        # replays (#25) and the shadow reviewer (#26), wave 9 (#29)
    run_budget_usd = 2.0
    min_cases = 10       # replay cases scored on both sides before a verdict
    live_min_sample = 30 # paired, user-decided PRs before "holds live" is judged
    nightly_hour = 2     # the launchd agent runs `fman experiment run --all` then

    [sandbox_network]    # per repo: "allow" opens outbound network for its verify + checks
    "you/project" = "none"

    [sandbox_install]    # per repo (#45): "off" skips the dependency-install step
    "you/project" = "on" # (default on: sandboxed, network to ports 443/80 for base files)

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
SANDBOX_INSTALLS = ("on", "off")
#: #58 per-attempt limits (``spend.Spend`` re-exports them as ``DEFAULT_ATTEMPT_*``).
DEFAULT_ATTEMPT_BUDGET_USD = 1.50
DEFAULT_ATTEMPT_MAX_INPUT_TOKENS_CLOUD = 1_500_000
DEFAULT_ATTEMPT_MAX_INPUT_TOKENS_LOCAL = 4_000_000
#: #56: days an unmerged ticket's recordings, traces and stale scratch are kept.
DEFAULT_RETENTION_DAYS = 90.0

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


@dataclass(frozen=True)
class ExperimentSettings:
    """``[experiments]`` in config.toml (#29): replays (#25) and the shadow reviewer (#26).

    The monthly experiments budget stays ``spend_caps.experiments_monthly_usd``."""

    set_size: int = 100  # latest merges a replay set is built from
    run_budget_usd: float = 2.0  # cloud spend one replay run may use (inside the budget)
    writer_timeout_s: float = 20 * 60.0  # one writer replay side (OpenCode)
    verify_timeout_s: float = 10 * 60.0  # scoring one writer side (the merged tests)
    review_timeout_s: float = 30 * 60.0  # one replayed review
    min_cases: int = 10  # cases scored on both sides before a replay verdict
    live_min_sample: int = 30  # paired, user-decided PRs before the shadow is judged
    shadow: bool = True  # run a running shadow experiment's challenger on live PRs
    shadow_timeout_s: float = 15 * 60.0  # one shadow review, checks and model call
    nightly_hour: int = 2  # local time of the nightly `fman experiment run --all`
    nightly_minute: int = 0


OLLAMA_URL = "http://127.0.0.1:11434/v1"
DEFAULT_EMBEDDING_MODEL = "embeddinggemma"


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
    #: #45, per repo ("owner/name" -> "on" | "off"): "on" (the default for every repo)
    #: installs a fresh worktree's dependencies before the writer starts, sandboxed, with
    #: outbound network to ports 443/80 only while the worktree holds the base branch's
    #: own files (offline after any model edit); "off" skips the step (no network at all
    #: for that repo; verify then needs the dependencies some other way). Sorted pairs,
    #: like ``sandbox_network``.
    sandbox_install: tuple[tuple[str, str], ...] = ()
    #: #45: bound on one dependency install (the step fails, and the ticket is flagged).
    install_timeout_seconds: float = 900.0
    #: #57: when an offline install (after a model edit) misses the package cache and the
    #: lockfiles are unchanged from the base commit, fill the cache once from a scratch
    #: checkout of the base commit (with network) and retry offline. False flags instead.
    install_refill_cache: bool = True
    #: #58: one attempt may spend at most this on cloud models, and send at most the
    #: token limits below (by the endpoint's kind). Over either, the attempt ends with
    #: ``end_reason = 'budget'`` and the ticket is handed to the user (no retry).
    attempt_budget_usd: float = DEFAULT_ATTEMPT_BUDGET_USD
    attempt_max_input_tokens_cloud: int = DEFAULT_ATTEMPT_MAX_INPUT_TOKENS_CLOUD
    attempt_max_input_tokens_local: int = DEFAULT_ATTEMPT_MAX_INPUT_TOKENS_LOCAL
    #: #56: `fman prune` and the worker's daily prune remove an unmerged ticket's
    #: recordings and traces, and ended attempts' scratch, older than this many days.
    retention_days: float = DEFAULT_RETENTION_DAYS
    #: Bound on one writer session (OpenCode). A large repo on local models can need more
    #: than the old fixed 20 minutes just to read its way in (live run 2026-11-24).
    writer_timeout_seconds: float = 1200.0
    #: #44: before a ticket is routed, a small local model checks the issue can be worked
    #: on as written; if not, the worker asks on the issue (``needs-info``) and waits for
    #: an allowed user. False turns the check off.
    clarify: bool = True
    #: #23 nearest-neighbour router. The embedding model is served by the proxy next to
    #: the writer models but is never a writer rung (``embedder()``). Local by default
    #: (7c): a non-loopback ``embedding_base_url`` needs ``allow_cloud_embedder = true``,
    #: https and ``embedding_api_key_ref``.
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_base_url: str = OLLAMA_URL
    allow_cloud_embedder: bool = False
    embedding_api_key_ref: str | None = None
    knn_threshold: int = 50  # accepted tickets before kNN replaces the rules router
    knn_k: int = 10  # neighbours that vote
    knn_timeout_seconds: float = 5.0  # one route-time embedding call, wall clock
    #: #23: share of tickets that start one tier below the router's pick (0 turns it off).
    explore_rate: float = 0.10
    #: #27: label each kickback's failure step with a small local model, in the background.
    failure_labels: bool = True
    failure_label_timeout_seconds: float = 30.0
    allow_cloud_labeler: bool = False
    experiments: ExperimentSettings = field(default_factory=ExperimentSettings)

    def __post_init__(self) -> None:
        for name in ("sandbox_network", "sandbox_install"):
            value = getattr(self, name)
            if isinstance(value, Mapping):
                object.__setattr__(self, name, tuple(sorted(value.items())))
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

    def embedder(self) -> ModelEndpoint:
        """The embedding model as a proxy endpoint (#23). Add it to the proxy's model list
        (``proxy_models``); never to ``models`` or the escalation ladder."""
        host = urlparse(self.embedding_base_url).hostname or ""
        local = _is_loopback(host)
        return ModelEndpoint(
            name=self.embedding_model,
            tier=1,  # a placeholder: the proxy ignores it, and it is never a rung
            kind="local" if local else "cloud",
            model=self.embedding_model,
            base_url=self.embedding_base_url,
            family="embedding",
            api_key_ref=None if local else self.embedding_api_key_ref,
        )

    def proxy_models(self) -> tuple[ModelEndpoint, ...]:
        """Every model the worker's proxy serves: the writers and reviewers, plus the
        embedding model."""
        return (*self.models, self.embedder())

    def sandbox_network_for(self, repo: str) -> str:
        """``"allow"`` or ``"none"``: the verify/check network for ``repo`` (#40)."""
        lowered = {k.lower(): v for k, v in self.sandbox_network}
        return lowered.get(repo.lower(), "none")

    def sandbox_install_for(self, repo: str) -> str:
        """``"on"`` (default) or ``"off"``: whether ``repo``'s worktrees get the
        dependency-install step (#45)."""
        lowered = {k.lower(): v for k, v in self.sandbox_install}
        return lowered.get(repo.lower(), "on")


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
    for repo, inst in cfg.sandbox_install:
        if not re.match(r"^[\w.-]+/[\w.-]+$", repo):
            raise ConfigError(f"sandbox_install: repo {repo!r} must look like 'owner/name'")
        if inst not in SANDBOX_INSTALLS:
            raise ConfigError(
                f"sandbox_install.{repo} must be one of {', '.join(SANDBOX_INSTALLS)}"
            )
    if not 0 < cfg.install_timeout_seconds <= 24 * 3600:
        raise ConfigError("install_timeout_seconds must be > 0 and <= 86400")
    if not cfg.attempt_budget_usd > 0:
        raise ConfigError("attempt_budget_usd must be > 0")
    for key in ("attempt_max_input_tokens_cloud", "attempt_max_input_tokens_local"):
        if getattr(cfg, key) < 1:
            raise ConfigError(f"{key} must be at least 1")
    if not cfg.retention_days >= 1:
        raise ConfigError("retention_days must be at least 1")
    if not 0 < cfg.writer_timeout_seconds <= 24 * 3600:
        raise ConfigError("writer_timeout_seconds must be > 0 and <= 86400")
    _validate_learning(cfg)
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


def _validate_learning(cfg: Config) -> None:
    """#23, #27 and ``[experiments]`` (#29)."""
    if not cfg.embedding_model.strip() or cfg.embedding_model in {m.name for m in cfg.models}:
        raise ConfigError("embedding_model must be set and differ from every writer model name")
    url = urlparse(cfg.embedding_base_url)
    if url.scheme not in ("http", "https") or not url.hostname:
        raise ConfigError("embedding_base_url must be an http(s) URL")
    if not _is_loopback(url.hostname):
        if not cfg.allow_cloud_embedder:
            raise ConfigError(
                "embedding_base_url is not on localhost: the embedding model stays local "
                "(set allow_cloud_embedder = true to send issue text to it)"
            )
        if url.scheme != "https" or not cfg.embedding_api_key_ref:
            raise ConfigError("a cloud embedding model needs https and embedding_api_key_ref")
    if cfg.embedding_api_key_ref is not None:
        parse_key_ref(cfg.embedding_api_key_ref)
    for name in ("knn_threshold", "knn_k"):
        if getattr(cfg, name) < 1:
            raise ConfigError(f"{name} must be >= 1")
    for name in ("knn_timeout_seconds", "failure_label_timeout_seconds"):
        if not 0 < getattr(cfg, name) <= 600:
            raise ConfigError(f"{name} must be > 0 and <= 600")
    if not 0.0 <= cfg.explore_rate <= 1.0:
        raise ConfigError("explore_rate must be between 0 and 1")
    for name in ("allow_cloud_embedder", "failure_labels", "allow_cloud_labeler"):
        if not isinstance(getattr(cfg, name), bool):
            raise ConfigError(f"{name} must be true or false")
    ex = cfg.experiments
    for name in ("set_size", "min_cases", "live_min_sample"):
        if getattr(ex, name) < 1:
            raise ConfigError(f"experiments.{name} must be >= 1")
    for name in ("writer_timeout_s", "verify_timeout_s", "review_timeout_s", "shadow_timeout_s"):
        if getattr(ex, name) <= 0:
            raise ConfigError(f"experiments.{name} must be > 0")
    if ex.run_budget_usd < 0:
        raise ConfigError("experiments.run_budget_usd must be >= 0")
    if not 0 <= ex.nightly_hour <= 23 or not 0 <= ex.nightly_minute <= 59:
        raise ConfigError("experiments.nightly_hour must be 0-23 and nightly_minute 0-59")
    if not isinstance(ex.shadow, bool):
        raise ConfigError("experiments.shadow must be true or false")


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
    if "install_refill_cache" in data:
        kw["install_refill_cache"] = _typed(
            data["install_refill_cache"], bool, "install_refill_cache"
        )
    for key in ("attempt_budget_usd", "retention_days"):
        if key in data:
            kw[key] = float(_typed(data[key], (int, float), key))
    for key in ("attempt_max_input_tokens_cloud", "attempt_max_input_tokens_local"):
        if key in data:
            kw[key] = _typed(data[key], int, key)
    for table in ("sandbox_network", "sandbox_install"):
        if table in data:
            raw = _typed(data[table], dict, table)
            kw[table] = {
                _typed(k, str, table): _typed(v, str, f"{table}.{k}") for k, v in raw.items()
            }
    if "install_timeout_seconds" in data:
        kw["install_timeout_seconds"] = float(
            _typed(data["install_timeout_seconds"], (int, float), "install_timeout_seconds")
        )
    if "writer_timeout_seconds" in data:
        kw["writer_timeout_seconds"] = float(
            _typed(data["writer_timeout_seconds"], (int, float), "writer_timeout_seconds")
        )
    _learning_from_dict(data, kw)
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


_LEARNING_TYPES: dict[str, Any] = {
    "embedding_model": str,
    "embedding_base_url": str,
    "allow_cloud_embedder": bool,
    "embedding_api_key_ref": str,
    "knn_threshold": int,
    "knn_k": int,
    "knn_timeout_seconds": float,
    "explore_rate": float,
    "failure_labels": bool,
    "failure_label_timeout_seconds": float,
    "allow_cloud_labeler": bool,
}


def _field_value(value: Any, typ: Any, where: str) -> Any:
    if typ is float:
        return float(_typed(value, (int, float), where))
    return _typed(value, typ, where)


def _learning_from_dict(data: Mapping[str, Any], kw: dict[str, Any]) -> None:
    for key, typ in _LEARNING_TYPES.items():
        if key in data:
            kw[key] = _field_value(data[key], typ, key)
    if "experiments" in data:
        raw = _typed(data["experiments"], dict, "experiments")
        types = {f.name: f.type for f in fields(ExperimentSettings)}
        _check_keys(raw, set(types), "experiments")
        kw["experiments"] = ExperimentSettings(
            **{k: _field_value(v, types[k], f"experiments.{k}") for k, v in raw.items()}
        )


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
    if cfg.sandbox_install:
        out["sandbox_install"] = dict(cfg.sandbox_install)
    out["install_timeout_seconds"] = cfg.install_timeout_seconds
    out["install_refill_cache"] = cfg.install_refill_cache
    out["attempt_budget_usd"] = cfg.attempt_budget_usd
    out["attempt_max_input_tokens_cloud"] = cfg.attempt_max_input_tokens_cloud
    out["attempt_max_input_tokens_local"] = cfg.attempt_max_input_tokens_local
    out["retention_days"] = cfg.retention_days
    out["writer_timeout_seconds"] = cfg.writer_timeout_seconds
    for key in _LEARNING_TYPES:
        if getattr(cfg, key) is not None:
            out[key] = getattr(cfg, key)
    out["experiments"] = {
        f.name: getattr(cfg.experiments, f.name) for f in fields(ExperimentSettings)
    }
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


#: #56: above this many repos, the worker's startup log warns and setup says so. Each
#: configured repo costs at least one issue listing per poll (80 an hour at the default
#: 45 s), so 20 repos are about 1,600 requests an hour while idle: a third of an
#: installation's 5,000-an-hour floor, and more repos than one person reviews PRs for.
REPOS_WARN_THRESHOLD = 20


def listing_requests_per_hour(repos: int, poll_interval_seconds: float) -> int:
    """GitHub requests an hour spent on the per-repo issue listing alone (one per repo
    per poll; ``github/poller.py`` adds more per open ticket)."""
    return round(repos * 3600 / max(1.0, float(poll_interval_seconds)))


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
    "REPOS_WARN_THRESHOLD",
    "Config",
    "ConfigError",
    "ExperimentSettings",
    "ModelEndpoint",
    "SpendCaps",
    "default_models",
    "from_dict",
    "key_ref",
    "listing_requests_per_hour",
    "load",
    "parse_key_ref",
    "resolve_key",
    "save",
    "set_key",
    "to_dict",
]
