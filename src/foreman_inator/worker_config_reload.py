"""The worker re-reads config.toml when it changes (todo #73, GH #62): ``ConfigReloadSteps``
is mixed into ``Worker``.

``watch_config(path)`` (once, at start) remembers the file's stamp (``st_mtime_ns`` and
size). ``maybe_reload_config()`` (once per poll) does nothing while the stamp is the same.
When it changed, the file is read with ``config.load`` (the CLI's validation, nothing
else) and:

* invalid TOML or a value ``config.load`` refuses: the old config stays, and the error is
  logged once (the stamp is remembered, so the same broken file is not re-read each poll);
* a missing file: the old config stays (``config.load`` would give the defaults, which
  is never what a running worker should silently switch to);
* an empty file, or one listing no repos / allowed_users while the running config has
  some: refused like an invalid one (most likely a half-written or wiped file);
* valid: it replaces ``self.config``, except the fields in ``RESTART_FIELDS`` (things
  built once at start: the model ladder and proxy, the sandbox line, the App identity,
  the relay), which keep their running values and are logged as needing a restart. If
  holding them back makes the mix invalid, nothing is swapped. Every changed key is
  logged (``changes``), and the pieces the worker copied out of the config at start are
  updated when they still held the old config's value: ``spend.caps`` (and the per-attempt
  limits, and ``spend.approval_usd`` when it was the daily-cap default),
  ``poller.config`` / ``poller.repos`` / ``self.repos``, ``self.parallel``.

Everything else reads ``self.config`` when it needs it (``writer_timeout_seconds``,
``allowed_users``, ``max_open_prs_per_repo``, ...), so a new value applies from the next
decision. A reload never raises into the poll loop.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from foreman_inator import config as config_mod
from foreman_inator.config import Config, ConfigError, SpendCaps

log = logging.getLogger("foreman_inator.worker")

#: Config fields read once when the worker starts; a change waits for a restart.
RESTART_FIELDS: tuple[str, ...] = (
    "app_id",
    "app_slug",
    "installation_ids",
    "models",
    "sandbox",
    "sandbox_network",
    "sandbox_install",
    "embedding_model",
    "embedding_base_url",
    "allow_cloud_embedder",
    "embedding_api_key_ref",
    "max_local_inference",
    "relay_url",
    "relay_secret_ref",
)

Stamp = tuple[int, int]


def _stamp(path: Path) -> Stamp | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _flat(data: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(_flat(value, f"{name}."))
        else:
            out[name] = value
    return out


def _show(value: Any) -> str:
    if value is None:
        return "(unset)"
    if isinstance(value, list) and all(not isinstance(v, dict) for v in value):
        return "[" + ", ".join(str(v) for v in value) + "]"
    if isinstance(value, list):
        return f"({len(value)} entries)"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def changes(old: Config, new: Config) -> list[str]:
    """One ``"key: old -> new"`` line per config key that differs (dotted for tables,
    ``config.to_dict`` spelling; never a secret: the config holds only key references)."""
    a, b = _flat(config_mod.to_dict(old)), _flat(config_mod.to_dict(new))
    lines = []
    for key in sorted(set(a) | set(b)):
        if a.get(key) != b.get(key):
            lines.append(f"{key}: {_show(a.get(key))} -> {_show(b.get(key))}")
    return lines


def hold_restart_fields(old: Config, new: Config) -> tuple[Config, list[str]]:
    """``new`` with ``RESTART_FIELDS`` set back to ``old``'s, and the names held back.
    Raises ``ConfigError`` when that mix does not validate."""
    held = [f for f in RESTART_FIELDS if getattr(old, f) != getattr(new, f)]
    if not held:
        return new, []
    return replace(new, **{f: getattr(old, f) for f in held}), held


def _refuse_reason(path: Path, old: Config, new: Config) -> str | None:
    """Why a freshly read file looks like a half-written or wiped config rather than an
    edit (defence in depth: ``config.save`` writes atomically, but an editor may not)."""
    try:
        if not path.read_bytes().strip():
            return f"{path} is empty"
    except OSError as exc:
        return f"{path} could not be read: {exc}"
    if old.repos and not new.repos:
        return "it lists no repos (the running config has some); restart the worker to apply it"
    if old.allowed_users and not new.allowed_users:
        return (
            "it lists no allowed_users (the running config has some); "
            "restart the worker to apply it"
        )
    return None


def _default_approval_usd(cfg: Config) -> float:
    """``Spend``'s default ``approval_usd`` for ``cfg`` (the daily cap, or the default cap)."""
    daily = cfg.spend_caps.daily_usd
    return float(daily if daily > 0 else SpendCaps.daily_usd)


class ConfigReloadSteps:
    # Provided by Worker (each read with getattr: tests may build a partial worker).
    config: Any

    _config_watch: Path | None = None
    _config_stamp: Stamp | None = None

    def watch_config(self, path: Path) -> None:
        """Watch ``path`` (the file ``self.config`` was loaded from) from now on."""
        self._config_watch = Path(path)
        self._config_stamp = _stamp(self._config_watch)

    def maybe_reload_config(
        self, *, load: Callable[[Path], Config] = config_mod.load
    ) -> list[str] | None:
        """Re-read the watched file if it changed. Returns the applied change lines
        (``[]`` when the file changed but no value did), or None when nothing was read
        or the new file was refused (the old config stays)."""
        path = self._config_watch
        if path is None:
            return None
        stamp = _stamp(path)
        if stamp == self._config_stamp:
            return None
        self._config_stamp = stamp
        if stamp is None:
            log.warning("%s is gone; keeping the config the worker has", path)
            return None
        try:
            new = load(path)
            old = self.config
            refused = _refuse_reason(path, old, new)
            if refused:
                log.warning(
                    "config.toml changed but was not applied (old config kept): %s", refused
                )
                return None
            applied, held = hold_restart_fields(old, new)
        except (ConfigError, OSError) as exc:
            log.warning("config.toml changed but was not applied (old config kept): %s", exc)
            return None
        except Exception as exc:  # never into the poll loop
            log.warning("config reload failed (old config kept): %s: %s", type(exc).__name__, exc)
            return None
        lines = changes(old, applied)
        for name in held:
            log.warning("config.toml: %s changed; restart the worker to apply it", name)
        if not lines:
            return []
        self.config = applied
        self._propagate_config(old, applied)
        log.info("config.toml reloaded: %s", "; ".join(lines))
        return lines

    def _propagate_config(self, old: Config, new: Config) -> None:
        """Update what the worker copied out of ``old`` at start (only where it still
        holds ``old``'s value, so an explicit constructor argument is left alone)."""
        spend = getattr(self, "spend", None)
        if spend is not None:
            if getattr(spend, "caps", None) == old.spend_caps:
                spend.caps = new.spend_caps
            if getattr(spend, "approval_usd", None) == _default_approval_usd(old):
                spend.approval_usd = _default_approval_usd(new)
            if getattr(spend, "attempt_budget_usd", None) == old.attempt_budget_usd:
                spend.attempt_budget_usd = new.attempt_budget_usd
            limits = getattr(spend, "attempt_max_input_tokens", None)
            if isinstance(limits, dict):
                for kind in ("cloud", "local"):
                    key = f"attempt_max_input_tokens_{kind}"
                    if limits.get(kind) == getattr(old, key):
                        limits[kind] = getattr(new, key)
        poller = getattr(self, "poller", None)
        if poller is not None:
            if getattr(poller, "config", None) is old:
                poller.config = new
            if tuple(getattr(poller, "repos", ())) == tuple(old.repos):
                poller.repos = tuple(new.repos)
        if tuple(getattr(self, "repos", ()) or ()) == tuple(old.repos):
            self.repos = tuple(new.repos)
        if getattr(self, "parallel", None) == old.max_parallel_per_repo:
            self.parallel = int(new.max_parallel_per_repo)


__all__ = ["RESTART_FIELDS", "ConfigReloadSteps", "changes", "hold_restart_fields"]
