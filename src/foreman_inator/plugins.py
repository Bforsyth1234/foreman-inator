"""Plugin hooks: a separately installed package can extend the worker (todo #68, issue #45).

Discovery: Python entry points in the group ``foreman_inator.plugins`` of *installed*
distributions only (``importlib.metadata.entry_points(group=...)``). Nothing here takes a
path from config.toml or from a repo, and this package never imports a plugin by name, so
the dependency runs one way and the worker runs unchanged with no plugin installed.
Stdlib docs for the API used: https://docs.python.org/3.12/library/importlib.metadata.html
(``entry_points(group=)``, ``EntryPoint.load``, ``EntryPoint.dist``), checked 2026-11-25.

A plugin package declares, in its ``pyproject.toml``::

    [project.entry-points."foreman_inator.plugins"]
    myplugin = "my_package.fman_plugin"

The entry point names a module (or any object) with:

- ``API_VERSIONS``: the hook API versions it works with, e.g. ``(1,)``. A plugin that
  does not list ``API_VERSIONS`` is skipped (hooks are a public API; it must opt in).
- ``register(registry: Registry) -> None``: adds its hooks to ``registry``.

Hook API version 1 has one hook, added with its first use:

- ``registry.add_cli_command(name, callback, help=None)``: an extra ``fman <name>``
  command. ``callback`` is a plain function, turned into a Typer command by
  ``install_cli_commands``. Default (no plugin): no extra commands, today's CLI. A plugin
  command never replaces a built-in one: a name that is already taken is skipped.

Safety (SPEC invariants 7a-7g): hooks take and return data; none can replace the merge,
allowed-users, sandbox or reviewer-independence checks, and none is consulted by them. A
plugin that fails to load, declares no compatible API version, or raises in ``register``
is logged and skipped (its partial registrations are discarded); loading never raises.
``status_lines`` / ``startup_lines`` list every loaded plugin with its version.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from importlib import metadata
from typing import Any

log = logging.getLogger(__name__)

GROUP = "foreman_inator.plugins"
#: The hook API version this worker offers. Bump it when a hook changes shape; keep the
#: old one working while any plugin may list it.
API_VERSION = 1
SUPPORTED_API_VERSIONS: frozenset[int] = frozenset({1})


@dataclass(frozen=True)
class CliCommand:
    """One extra ``fman`` command from a plugin (hook API 1)."""

    name: str
    callback: Callable[..., Any]
    help: str | None
    plugin: str


class Registry:
    """What a plugin's ``register`` receives. Each method is one versioned hook."""

    api_version = API_VERSION

    def __init__(self, plugin: str = "") -> None:
        self._plugin = plugin
        self._cli: list[CliCommand] = []

    def add_cli_command(
        self, name: str, callback: Callable[..., Any], help: str | None = None
    ) -> None:
        """Hook (API 1): add ``fman <name>``, run by ``callback``."""
        if not isinstance(name, str) or not name or name.startswith("-") or " " in name:
            raise ValueError(f"bad command name {name!r}")
        if not callable(callback):
            raise TypeError(f"callback for {name!r} is not callable")
        if any(c.name == name for c in self._cli):
            raise ValueError(f"command {name!r} added twice")
        self._cli.append(CliCommand(name, callback, help, self._plugin))

    @property
    def cli_commands(self) -> tuple[CliCommand, ...]:
        return tuple(self._cli)

    def _merge(self, other: Registry) -> list[str]:
        """Add ``other``'s hooks; return the command names skipped as already taken."""
        skipped = []
        for c in other._cli:
            if any(have.name == c.name for have in self._cli):
                skipped.append(c.name)
                continue
            self._cli.append(c)
        return skipped


@dataclass(frozen=True)
class LoadedPlugin:
    name: str  # entry point name
    distribution: str  # the installed package it came from
    version: str
    api_versions: tuple[int, ...]


@dataclass(frozen=True)
class FailedPlugin:
    name: str
    distribution: str
    reason: str


@dataclass
class PluginSet:
    """The result of ``load_plugins``: what loaded, what was skipped, and their hooks."""

    loaded: list[LoadedPlugin] = field(default_factory=list)
    failed: list[FailedPlugin] = field(default_factory=list)
    registry: Registry = field(default_factory=Registry)


def _dist(ep: metadata.EntryPoint) -> tuple[str, str]:
    d = getattr(ep, "dist", None)
    if d is None:
        return "?", "?"
    try:
        return str(d.metadata["Name"] or d.name), str(d.version)
    except Exception:  # broken metadata must not stop the worker either
        return "?", "?"


def _api_versions(obj: object) -> tuple[int, ...]:
    raw = getattr(obj, "API_VERSIONS", None)
    if raw is None:
        raise ValueError("declares no API_VERSIONS")
    versions = tuple(int(v) for v in raw)
    if not set(versions) & SUPPORTED_API_VERSIONS:
        offered = ", ".join(str(v) for v in sorted(SUPPORTED_API_VERSIONS))
        raise ValueError(f"needs hook API {versions}, this worker offers {offered}")
    return versions


def _load_one(ep: metadata.EntryPoint, into: Registry) -> LoadedPlugin:
    dist_name, version = _dist(ep)
    obj = ep.load()
    versions = _api_versions(obj)
    register = getattr(obj, "register", None)
    if not callable(register):
        raise TypeError("has no register(registry) function")
    scratch = Registry(plugin=ep.name)
    register(scratch)  # a raise here discards everything it registered
    for name in into._merge(scratch):
        log.warning("plugin %s: command %r already taken by another plugin; skipped", ep.name, name)
    return LoadedPlugin(ep.name, dist_name, version, versions)


def load_plugins(
    entry_points: Callable[..., Iterable[metadata.EntryPoint]] | None = None,
) -> PluginSet:
    """Load every installed plugin in ``GROUP``. Never raises: a plugin that fails is
    logged and listed in ``PluginSet.failed``. ``entry_points`` defaults to
    ``importlib.metadata.entry_points`` (tests may pass a stand-in with the same
    ``(group=...)`` signature)."""
    find = entry_points or metadata.entry_points
    out = PluginSet()
    try:
        eps = sorted(find(group=GROUP), key=lambda e: e.name)
    except Exception as exc:
        log.warning("plugin discovery failed (%s); running without plugins", exc)
        return out
    for ep in eps:
        try:
            out.loaded.append(_load_one(ep, out.registry))
            log.info("plugin %s loaded", ep.name)
        except (Exception, SystemExit) as exc:
            reason = f"{type(exc).__name__}: {exc}"
            log.warning("plugin %s failed to load and is skipped: %s", ep.name, reason)
            out.failed.append(FailedPlugin(ep.name, _dist(ep)[0], reason))
    return out


def _taken_names(app: Any) -> set[str]:
    names: set[str] = set()
    for c in getattr(app, "registered_commands", []):
        cb = getattr(c, "callback", None)
        name = c.name or (cb.__name__.replace("_", "-") if cb else None)
        if name:
            names.add(name)
    for g in getattr(app, "registered_groups", []):
        inst = getattr(g, "typer_instance", None)
        name = g.name or getattr(getattr(inst, "info", None), "name", None)
        if isinstance(name, str):
            names.add(name)
    return names


def install_cli_commands(app: Any, plugins: PluginSet) -> list[str]:
    """Add the plugins' extra commands to the Typer ``app``. A name ``app`` already has
    (a built-in command or group) is never replaced; those names are returned."""
    taken = _taken_names(app)
    skipped = []
    for c in plugins.registry.cli_commands:
        if c.name in taken:
            log.warning("plugin %s: command %r is built in; skipped", c.plugin, c.name)
            skipped.append(c.name)
            continue
        app.command(c.name, help=c.help or f"(plugin {c.plugin})")(c.callback)
        taken.add(c.name)
    return skipped


def status_lines(plugins: PluginSet) -> list[str]:
    """``fman status``: one line per loaded or skipped plugin, or ``Plugins: none``."""
    if not plugins.loaded and not plugins.failed:
        return ["Plugins: none"]
    lines = []
    for p in plugins.loaded:
        api = ", ".join(str(v) for v in p.api_versions)
        lines.append(f"Plugin {p.name}: {p.distribution} {p.version} (hook API {api})")
    lines += [f"Plugin {p.name}: SKIPPED, failed to load ({p.reason})" for p in plugins.failed]
    return lines


def startup_lines(plugins: PluginSet) -> list[str]:
    """The worker's startup log lines (same content as ``status_lines``)."""
    return status_lines(plugins)


__all__ = [
    "API_VERSION",
    "GROUP",
    "CliCommand",
    "FailedPlugin",
    "LoadedPlugin",
    "PluginSet",
    "Registry",
    "install_cli_commands",
    "load_plugins",
    "startup_lines",
    "status_lines",
]
