"""Install a worktree's dependencies before the writer and verify run (todo #45).

A fresh worktree has no ``.venv`` or ``node_modules``, and the sandbox gives the writer and
verify no network (#40), so a repo with dependencies could never pass verify (first live
run: ``uv run pytest`` failed "Network connectivity is disabled" on a README typo fix). The
install step runs once per worktree, before the writer, so the writer's own test runs and
the worker's verify both find the dependencies installed.

Detection (``detect``), next to verify's: the repo's saved override (``fman repo
set-install``) → by lockfile, every ecosystem found: ``uv.lock`` → ``uv sync --locked``
(the default groups, ``dev`` included, exactly what ``uv run`` syncs); ``poetry.lock`` →
``poetry install --no-interaction``; ``package-lock.json``/``npm-shrinkwrap.json`` →
``npm ci``; ``pnpm-lock.yaml`` → ``pnpm install --frozen-lockfile``; ``yarn.lock`` →
``yarn install --frozen-lockfile`` (``--immutable`` with Yarn 2+'s ``.yarnrc.yml``); a
``package.json`` with dependencies and no lockfile → ``npm install --no-package-lock``;
``Package.swift`` → ``swift package resolve``. Nothing for a bare ``python3 -m pytest``
repo or a Makefile-only repo unless the user saves a command.

The install policy (``install_policy``), what it does
------------------------------------------------------
It runs model-adjacent code: lockfile packages, npm lifecycle scripts, Python build
backends, ``setup.py``. So it runs under the sandbox (invariant 7g) with a policy of its
own, derived from the launch policy:

* writes: the worktree, the step's own scratch dir, and the repo's package cache (when it
  has the network); not the mirror's git dirs, not this module's marker;
* reads: as every launch (no home, no ``~/.foreman-inator``, no other worktree, not the
  worker's own venv or checkout);
* network, decided per worktree (``network_for``): **only when the worktree's files are
  exactly the base branch's** (no model edit yet: ``git diff --quiet <base>``), outbound
  TCP to *any host* on ports 443 and 80, plus DNS and certificate trust. After a model
  edit (an attempt building on a previous attempt's branch, a rebase, a reviewer's scratch
  worktree at the change) the install runs **offline**, from a private copy of the cache,
  so code a model wrote never runs with the network. A repo with ``sandbox_network =
  "allow"`` (the user opened the network for its checks) installs with full network.

What that protects against: the install cannot read the user's files or secrets (keys,
``app.pem``, tokens, config, other repos); it cannot write outside the worktree and the
repo's own cache; model-written code never gets network through it.

What it does not protect against: during a networked install, the base branch's own
dependency code (a malicious or compromised package, a postinstall script) can fetch
anything from any host on 443/80 and can send what it can read (the worktree, i.e. the
repo's code, and the repo's cache) to any host on those ports. ``sandbox-exec`` cannot
restrict outbound traffic by host: a profile naming one fails with "host must be * or
localhost in network address" (checked on macOS 26.6.2, 2026-11-24), so registries cannot
be allow-listed; only ports can. What it installs then runs, without network, in the
writer and in verify. Private registries on other ports, git dependencies over ssh, and
interpreters that must be downloaded (``UV_PYTHON_DOWNLOADS=never``) fail the install;
the user sets ``sandbox_network = "allow"`` or a saved command for such a repo.

The package cache (``cache_root``)
----------------------------------
``~/.foreman-inator/cache/<owner>/<name>/{uv,npm,pnpm,yarn,poetry,pip}``, one per repo,
never the user's own ``~/.cache`` and never shared between repos. Only a networked install
(base-branch files) writes it. Every other sandboxed launch of that repo (the writer,
verify, the reviewer's checks, an offline install) gets a copy-on-write snapshot of it
in its own scratch dir (``snapshot``: APFS clones, ``cp -c``) and read access to the
original (uv's cache keeps absolute symlinks into it), so ``uv run`` and ``npm`` resolve
from it offline and anything they write lands in the throwaway copy. uv refuses a
read-only cache outright ("Failed to initialize cache ... Operation not permitted"), so a
plain read-only mount would not work. Installs link packages by clone or copy, never by
hard link (``UV_LINK_MODE=clone``, pnpm ``clone-or-copy``), so a later write to a file in
the worktree cannot change the cached original.

A poisoned cache stays inside its repo: a malicious dependency in the base branch could
plant a modified package in its own repo's cache, which later attempts and reviews of that
repo would install (and, for example, make its tests look green). It cannot reach another
repo or the user's own tools. Deleting ``~/.foreman-inator/cache/<owner>/<name>`` resets it.

Marker: a worktree that was installed gets ``<git dir>/fman-installed`` (the private git
dir; never in the tree, never writable from the sandbox), so the reviewer's checks
(``verify.run`` with ``Policy.install`` set) install each scratch worktree once and the
attempt's verify does not install again.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from foreman_inator.runner import sandbox as sandbox_mod
from foreman_inator.runner.sandbox import GitAccess, Policy

DEFAULT_TIMEOUT_S = 15 * 60
"""Default install timeout in seconds (``install_timeout_seconds`` in config)."""

TAIL_CHARS = 4000
CACHE_SUBDIRS: tuple[str, ...] = tuple(sandbox_mod.CACHE_VARS.values())

#: Set for every install: no interpreter downloads (the scratch dir that would hold it is
#: deleted afterwards), clone-or-copy links (never hard links into the shared cache),
#: in-project poetry venvs (verify's ``poetry run`` finds ``.venv``), no npm audit/fund.
INSTALL_ENV: dict[str, str] = {
    "UV_PYTHON_DOWNLOADS": "never",
    "UV_LINK_MODE": "clone",
    "npm_config_package_import_method": "clone-or-copy",
    "POETRY_VIRTUALENVS_IN_PROJECT": "true",
    "POETRY_NO_INTERACTION": "1",
    "npm_config_audit": "false",
    "npm_config_fund": "false",
}


class InstallFailed(RuntimeError):
    """The install command failed or timed out. ``detail`` is the tail of its log."""

    def __init__(self, reason: str, detail: str, log_path: Path | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail
        self.log_path = log_path


@dataclass(frozen=True)
class InstallCommand:
    """One or more argv steps run in order (``&&``); a saved override is one ``/bin/sh -c``."""

    steps: tuple[tuple[str, ...], ...]
    source: str  # override | uv | poetry | npm | pnpm | yarn | swift (joined with "+")
    display: str

    @classmethod
    def of(cls, steps: Sequence[Sequence[str]], source: str) -> InstallCommand:
        st = tuple(tuple(s) for s in steps)
        return cls(st, source, " && ".join(shlex.join(s) for s in st))

    @classmethod
    def from_override(cls, override: str) -> InstallCommand:
        return cls((("/bin/sh", "-c", override),), "override", override)

    def argv(self) -> tuple[str, ...]:
        """A single argv: the step itself, or ``/bin/sh -c 'a && b'`` for several."""
        if len(self.steps) == 1:
            return self.steps[0]
        return ("/bin/sh", "-c", self.display)


@dataclass(frozen=True)
class InstallResult:
    ran: bool  # False: nothing to install, or this worktree was installed already
    command: str | None
    network: str  # "ports" | "allow" | "none" | "off" (sandbox off)
    seconds: float = 0.0
    log_path: Path | None = None


@dataclass(frozen=True)
class InstallPlan:
    """What a launch policy carries so ``verify.run`` installs a worktree first (the
    reviewer's scratch worktrees, #45 requirement 4). ``override``: the repo's saved
    command; ``cache``: the repo's package cache (only read: the launch's scratch holds
    its snapshot)."""

    override: str | None = None
    timeout: float = DEFAULT_TIMEOUT_S
    cache: Path | None = None


# --------------------------------------------------------------------------- detection

_NPM_LOCKS = ("package-lock.json", "npm-shrinkwrap.json")


def _names(repo: Path) -> set[str]:
    try:
        return set(os.listdir(repo))
    except OSError:
        return set()


def _package_json_has_deps(repo: Path) -> bool:
    try:
        data = json.loads((repo / "package.json").read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    return any(
        isinstance(data.get(k), dict) and data[k]
        for k in ("dependencies", "devDependencies", "optionalDependencies")
    )


def _js_step(repo: Path, names: set[str]) -> tuple[str, tuple[str, ...]] | None:
    if "package.json" not in names:
        return None
    if "pnpm-lock.yaml" in names:
        return "pnpm", ("pnpm", "install", "--frozen-lockfile")
    if "yarn.lock" in names:
        if ".yarnrc.yml" in names:  # Yarn 2+ (berry): --frozen-lockfile is deprecated
            return "yarn", ("yarn", "install", "--immutable")
        return "yarn", ("yarn", "install", "--frozen-lockfile")
    if any(lock in names for lock in _NPM_LOCKS):
        return "npm", ("npm", "ci")
    if _package_json_has_deps(repo):
        return "npm", ("npm", "install", "--no-package-lock")
    return None


def detect(repo_dir: Path | str, override: str | None = None) -> InstallCommand | None:
    """The install command for ``repo_dir``, or None when nothing needs installing."""
    if override and override.strip():
        return InstallCommand.from_override(override.strip())
    repo = Path(repo_dir)
    names = _names(repo)
    steps: list[tuple[str, tuple[str, ...]]] = []
    if "uv.lock" in names:
        steps.append(("uv", ("uv", "sync", "--locked")))
    elif "poetry.lock" in names:
        steps.append(("poetry", ("poetry", "install", "--no-interaction")))
    js = _js_step(repo, names)
    if js is not None:
        steps.append(js)
    if "Package.swift" in names:
        steps.append(("swift", ("swift", "package", "resolve")))
    if not steps:
        return None
    return InstallCommand.of([s for _, s in steps], "+".join(src for src, _ in steps))


# --------------------------------------------------------------------------- the cache

_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


def cache_root(repo: str, home: Path | None = None) -> Path:
    """``<foreman home>/cache/<owner>/<name>`` (lowercased; created, mode 700)."""
    owner, _, name = repo.lower().partition("/")
    for part in (owner, name):
        if not _NAME.match(part) or part in (".", ".."):
            raise ValueError(f"repo {repo!r} must look like 'owner/name'")
    if home is None:
        from foreman_inator import paths

        home = paths.home()
    root = Path(home) / "cache" / owner / name
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for sub in CACHE_SUBDIRS:
        (root / sub).mkdir(exist_ok=True)
    return root


def snapshot(cache: Path, scratch_cache: Path) -> float:
    """Copy-on-write clones of ``cache``'s tool dirs into ``scratch_cache`` (the launch's
    ``<scratch>/cache``). Returns the seconds it took. Never touches ``cache``."""
    start = time.monotonic()
    scratch_cache.mkdir(parents=True, exist_ok=True)
    for sub in CACHE_SUBDIRS:
        src = cache / sub
        dst = scratch_cache / sub
        if not src.is_dir() or not any(src.iterdir()):
            continue
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
        cmd = ["/bin/cp", "-c", "-R", str(src), str(dst)]
        try:
            ok = subprocess.run(cmd, capture_output=True, timeout=300).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        if not ok:  # not APFS (no clonefile): a plain copy
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst, symlinks=True)
    return time.monotonic() - start


# --------------------------------------------------------------------------- the policy


def marker_path(worktree: Path) -> Path | None:
    """``<private git dir>/fman-installed`` for a linked worktree, ``.git/fman-installed``
    for a plain clone, None for a directory that is not a git checkout."""
    dotgit = Path(worktree) / ".git"
    if dotgit.is_dir():
        return dotgit / sandbox_mod.INSTALL_MARKER
    if dotgit.is_file():
        try:
            text = dotgit.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if text.startswith("gitdir:"):
            raw = Path(text.removeprefix("gitdir:").strip())
            gitdir = raw if raw.is_absolute() else Path(worktree) / raw
            return gitdir / sandbox_mod.INSTALL_MARKER
    return None


def installed(worktree: Path, command: InstallCommand) -> bool:
    m = marker_path(worktree)
    try:
        return m is not None and m.read_text(encoding="utf-8").strip() == command.display
    except OSError:
        return False


def _mark(worktree: Path, command: InstallCommand) -> None:
    m = marker_path(worktree)
    if m is not None:
        m.write_text(command.display + "\n", encoding="utf-8")


def is_base(worktree: Path, base_sha: str | None) -> bool:
    """True when ``worktree``'s tracked files are exactly ``base_sha``'s (no model edit)."""
    if not base_sha:
        return False
    try:
        out = subprocess.run(
            ["git", "diff", "--quiet", base_sha, "--"],
            cwd=worktree,
            capture_output=True,
            timeout=120,
            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
        )
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0 and untracked.returncode == 0 and not untracked.stdout.strip()


def network_for(policy: Policy, *, base: bool) -> str:
    """``allow`` (the repo opened the network), ``ports`` (base-branch files: 443/80 to
    any host) or ``none`` (after a model edit: offline from the cache snapshot)."""
    if policy.network == "allow":
        return "allow"
    return "ports" if base else "none"


def install_policy(
    policy: Policy,
    worktree: Path,
    *,
    network: str,
    cache: Path | None,
    ports: Sequence[int] | None = None,
) -> Policy:
    """The install step's policy (see the module docstring): ``policy`` bound to
    ``worktree``, the mirror's git dirs not writable, the marker protected, no proxy or
    listening; ``network``: ``ports`` (outbound to ``ports`` with DNS), ``allow`` (the
    repo's own setting) or ``none``. ``cache`` given (base-branch files only): the shared
    package cache is writable and the tool cache variables point at it; None: they point
    at the launch's scratch cache (a snapshot)."""
    bound = policy.bind(worktree)
    git = GitAccess(reads=bound.git.reads, protected=bound.git.protected)
    out = dataclasses.replace(
        bound, git=git, install=None, proxy_port=None, listen_loopback=False,
        outbound_ports=tuple(sandbox_mod.INSTALL_PORTS if ports is None else ports)
        if network == "ports" else (),
        network="allow" if network == "allow" else "none",
    )  # fmt: skip
    if cache is not None:
        real = sandbox_mod._real(cache)
        out = dataclasses.replace(out, cache=real, writes=(*out.writes, real))
    return out


def cache_env(cache: Path) -> dict[str, str]:
    """The tool cache variables pointing at ``cache`` (an unsandboxed install)."""
    return {var: str(cache / sub) for var, sub in sandbox_mod.CACHE_VARS.items()}


# --------------------------------------------------------------------------- running


def run(
    command: InstallCommand,
    worktree: Path | str,
    *,
    policy: Policy | None,
    network: str,
    cache: Path | None,
    log_path: Path | str,
    timeout: float = DEFAULT_TIMEOUT_S,
    cancel: threading.Event | None = None,
    ports: Sequence[int] | None = None,
) -> InstallResult:
    """Run ``command`` in ``worktree`` under the install policy; output to ``log_path``.

    Raises ``InstallFailed`` (with the log's tail) on a non-zero exit, a timeout or a
    cancel, and ``sandbox.SandboxUnavailable`` when the sandbox is on and cannot run. On
    success the worktree is marked installed. ``policy`` None or ``off``: unsandboxed,
    with the repo's cache in the environment."""
    from foreman_inator.runner import verify  # verify imports this module lazily too

    wt = Path(worktree)
    env = dict(INSTALL_ENV)
    if policy is None or policy.mode == "off":
        run_policy = None if policy is None else dataclasses.replace(policy, install=None)
        network = "off"
        if cache is not None:
            env.update(cache_env(cache))
    else:
        run_policy = install_policy(policy, wt, network=network, cache=cache, ports=ports)
    cmd = verify.VerifyCommand(command.argv(), "install", command.display)
    res = verify.run(cmd, wt, timeout=timeout, output_path=log_path, env=env, cancel=cancel,
                     sandbox=run_policy)  # fmt: skip
    if not res.passed:
        why = "timed out" if res.timed_out else f"exit code {res.exit_code}"
        raise InstallFailed(
            f"dependency install failed ({why}): {command.display}",
            res.output_tail(TAIL_CHARS),
            res.output_path,
        )
    _mark(wt, command)
    return InstallResult(True, command.display, network, res.seconds, res.output_path)


def ensure(
    plan: InstallPlan,
    worktree: Path | str,
    policy: Policy,
    *,
    log_path: Path | str,
    cancel: threading.Event | None = None,
) -> InstallResult:
    """``verify.run``'s first step when its policy carries a plan (the reviewer's checks):
    install ``worktree`` once, with ``policy``'s own network (none, unless the repo set
    ``sandbox_network = "allow"``: reviewer worktrees hold the model's change) and its
    scratch cache snapshot. A no-op when nothing is detected or it was installed."""
    wt = Path(worktree)
    command = detect(wt, plan.override)
    if command is None or installed(wt, command):
        return InstallResult(False, command.display if command else None, "none")
    network = "allow" if policy.network == "allow" else "none"
    return run(command, wt, policy=policy, network=network, cache=None, log_path=log_path,
               timeout=plan.timeout, cancel=cancel)  # fmt: skip


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "InstallCommand",
    "InstallFailed",
    "InstallPlan",
    "InstallResult",
    "cache_root",
    "detect",
    "ensure",
    "install_policy",
    "installed",
    "is_base",
    "marker_path",
    "network_for",
    "run",
    "snapshot",
]
