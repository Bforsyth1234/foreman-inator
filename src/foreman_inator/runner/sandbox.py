"""Sandbox for every process that runs model-written code (todo #40).

Covered launches: ``opencode serve`` (its bash tool inherits the sandbox), the repo's
verify command, and the reviewer's checks (tests, coverage, revert, run-app). Each goes
through ``wrap(argv, policy)`` and ``child_env(policy)``; a launch that is given no policy
runs as it always did.

Backend: macOS ``sandbox-exec`` (Seatbelt) with a profile generated per launch from the
policy (assistant decision, 2026-11-23). Apple marks ``sandbox-exec`` deprecated; it still
ships. Checked on this Mac 2026-11-23 (rule 12): macOS 26.6.2 (25G83); ``man sandbox-exec``
says "DEPRECATED" (page dated 2017) and nothing more; Apple's own profiles in
``/System/Library/Sandbox/Profiles`` still use ``(version 1)`` (476 of 509 files), which is
what this module emits. Observed behaviour this module relies on:

* ``(deny default)`` blocks everything, even ``execvp`` of the command (exit 71).
* A rule with a filter (``subpath``, ``literal``, ``regex``, ``remote ip``) beats a rule
  without one, whatever the order; among filtered rules for the same operation the
  LAST match wins. So the profile is written broad-to-narrow: unfiltered ``file-read*``,
  then filtered denies, then filtered re-allows, then the final denies and own paths.
* A denied file or socket operation fails in the child with ``EPERM`` ("Operation not
  permitted", errno 1); a denied connect is ``EPERM`` too, a denied DNS lookup is
  ``EAI_NONAME`` (errno 8). ``sandbox-exec`` itself exits 71 when it cannot exec or apply.
* A process inside a ``(deny default)`` sandbox cannot call ``sandbox-exec`` again:
  ``sandbox_apply: Operation not permitted`` (exit 71). Nesting only works when the outer
  profile allows it, so the worker itself must not run inside another such sandbox.
* ``(remote ip "localhost:<port>")`` limits outbound TCP to one loopback port; the host
  part may only be ``localhost`` or ``*``.

Fails closed (decision 5): with ``mode="on"`` and no usable ``sandbox-exec`` (not macOS,
binary missing), ``wrap`` raises ``SandboxUnavailable``; it never runs a command
unsandboxed. ``mode="off"`` (``sandbox = "off"`` in config) returns argv unchanged.

What the generated profile allows (``render_profile``):

* exec/fork, signals inside the sandbox, sysctl reads, POSIX semaphores
  (multiprocessing), one Mach service (opendirectoryd's libinfo: user lookups). The
  keychain (securityd), pasteboard, launchservices and every other service are denied.
* Reads: everything outside the denied roots (system and toolchain paths: /usr, /bin,
  /System, /Library, /opt, /Applications, /private/etc, ...). Denied roots: ``/Users``
  (every home), ``/Volumes``, ``/private/tmp``, ``/private/var/folders`` (other
  processes' temp files), the user's home and foreman-inator's home wherever they are.
  Re-allowed inside them: the toolchain dirs found from PATH and the command (``nvm``,
  ``~/.local/bin``, uv's managed Pythons, a venv), then ``SENSITIVE`` home dirs are denied
  again, then the policy's own paths are allowed last: the worktree, the attempt's scratch
  dir, the git paths the worktree needs, the policy's extra reads/writes, and xcrun's
  tool-path cache (read only).
* ``stat`` (file-read-metadata) anywhere, so path lookups work; no file contents, no
  directory listings outside the allowed paths.
* Writes: the worktree, the scratch dir, the policy's extra writes, ``/dev/null`` & co.,
  SwiftPM's atomic-write staging dirs, and in the mirror exactly: this worktree's private
  git dir, the object store, and this worktree's own branch ref and reflog. Never the
  worktree's ``.git`` file or the git dir's ``commondir``/``gitdir``/``fman-base``/
  ``config.worktree`` (they steer the worker's own, unsandboxed git calls). A commit
  inside works; git also prints "Unable to create .../packed-refs.lock" (harmless: the
  shared ``packed-refs`` stays read-only).
* Network: ``none`` = nothing (DNS fails too); with ``proxy_port`` set, outbound TCP to
  that loopback port only; ``listen_loopback`` lets a server (``opencode serve``) bind and
  accept on loopback; ``allow`` (``sandbox_network = "allow"`` for a repo) = all network
  plus the DNS and certificate-trust services.

``child_env`` moves HOME, TMPDIR and tool caches into the scratch dir and puts
``<scratch>/bin`` first on PATH (also in login shells, through the scratch HOME's
profiles): direct links to the ``/usr/bin`` xcrun shims (git, make, clang...) and a
``swift`` wrapper that adds ``--disable-sandbox``, because SwiftPM's own sandbox cannot
nest inside this one.

#54 (first live run): every child's environment drops the worker's own Python
environment (``scrub_worker_env``: ``VIRTUAL_ENV``, ``UV``, ``UV_RUN_RECURSION_DEPTH``,
``PYTHONPATH``, conda's variables, ...; the worker's venv ``bin`` off ``PATH``), sandbox on
or off; the profile denies the worker's venv, its uv-tool dir and its checkout
(``worker_paths``) and toolchain discovery never re-allows them. #55: zsh writes
here-documents at ``TMPPREFIX`` (default ``/tmp/zsh``, denied), so ``child_env`` points
``TMPPREFIX`` (and ``TMP``/``TEMP``) into the scratch dir; bash and ``/bin/sh`` already used
``TMPDIR``. #45: ``outbound_ports`` (the install step: any host, those ports, DNS) and
``cache`` (the tool cache root) serve ``runner.install``.

Residual risk (not covered): world-readable system paths are still readable (installed
software, ``/etc``, ``/Library``), and ``stat`` works everywhere, so a process can learn
which files exist and their sizes; CPU, memory, disk and process-count exhaustion are not
limited (only the caller's timeouts); the model server, the proxy and the worker are not
sandboxed, and the worker's own git calls in the worktree run outside it; with
``sandbox_network = "allow"`` code can send whatever it can read (the worktree) anywhere;
a process can add or rewrite loose objects in the repo's shared mirror (content-addressed,
so git detects a damaged one, but it could break another attempt of the same repo);
local processes can connect to a port the sandboxed server listens on; the writer can
send anything it reads in the worktree to the proxy, which forwards only model calls.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Protocol

Mode = Literal["on", "off"]
Network = Literal["none", "allow"]
MODES: tuple[str, ...] = ("on", "off")
NETWORKS: tuple[str, ...] = ("none", "allow")

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
PROFILE_VERSION = 1

#: Roots whose contents are not readable unless a later rule re-allows a path in them.
DENIED_ROOTS: tuple[str, ...] = ("/Users", "/Volumes", "/private/tmp", "/private/var/folders")

#: Paths under the user's home that are never readable, even if PATH points into them.
SENSITIVE: tuple[str, ...] = (
    ".ssh",
    ".aws",
    ".azure",
    ".config",
    ".gnupg",
    ".kube",
    ".docker",
    ".netrc",
    ".git-credentials",
    ".gitconfig",
    ".npmrc",
    ".pypirc",
    ".cargo/credentials",
    ".cargo/credentials.toml",
    ".vault-token",
    ".terraform.d",
    ".m2/settings.xml",
    ".gradle/gradle.properties",
    ".zsh_history",
    ".bash_history",
    ".python_history",
    ".foreman-inator",
    "Library",
    "Documents",
    "Desktop",
    "Downloads",
)

#: Toolchain install dirs under the home that are readable when they exist.
KNOWN_TOOLCHAINS: tuple[str, ...] = (
    ".local/share/uv/python",
    ".nvm/versions",
    ".pyenv/versions",
    ".rustup/toolchains",
    ".cargo/bin",
    ".bun/bin",
    ".volta/tools/image",
)

#: Mach services a sandboxed launch may look up. Found by test: python, node/npm, swift,
#: make, git, uv and ``opencode serve`` need none; user/group lookups (``getpwuid``,
#: ``whoami``) need opendirectoryd's libinfo. Everything else (the keychain's securityd,
#: the pasteboard, launchservices, ...) stays denied.
MACH_SERVICES: tuple[str, ...] = ("com.apple.system.opendirectoryd.libinfo",)

#: Extra Mach services with ``network = "allow"``: DNS (mDNSResponder) and certificate
#: trust for TLS through Apple's stack (curl, URLSession, ``swift package resolve``).
NETWORK_MACH_SERVICES: tuple[str, ...] = (
    "com.apple.dnssd.service",
    "com.apple.trustd",
    "com.apple.trustd.agent",
    "com.apple.SystemConfiguration.configd",
)

#: Variables that activate a Python environment: the worker's own when it runs from
#: ``uv run fman run`` (``VIRTUAL_ENV``, ``UV``, ``UV_RUN_RECURSION_DEPTH``), an activated
#: venv, conda, a poetry/pipenv shell or a uv tool. Never passed to a child, sandboxed or
#: not (#54): they made the repo's ``uv run`` warn and its ``python3`` resolve to the
#: worker's interpreter. ``WORKER_ENV_PREFIXES`` drops whole families (conda's).
WORKER_ENV_NAMES: tuple[str, ...] = (
    "VIRTUAL_ENV",
    "VIRTUAL_ENV_PROMPT",
    "_OLD_VIRTUAL_PATH",
    "_OLD_VIRTUAL_PYTHONHOME",
    "_OLD_VIRTUAL_PS1",
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "PYTHONEXECUTABLE",
    "__PYVENV_LAUNCHER__",
    "PYENV_VIRTUAL_ENV",
    "UV",
    "UV_ACTIVE",
    "UV_PROJECT",
    "UV_PROJECT_ENVIRONMENT",
    "UV_RUN_RECURSION_DEPTH",
    "UV_INTERNAL__PARENT_INTERPRETER",
    "POETRY_ACTIVE",
    "PIPENV_ACTIVE",
    "PDM_PROJECT_ROOT",
)
WORKER_ENV_PREFIXES: tuple[str, ...] = ("CONDA_", "_CONDA_", "MAMBA_", "_CE_")

#: The dependency-install step's outbound ports (#45): HTTPS and HTTP to any host.
#: ``sandbox-exec`` cannot filter by host ("host must be * or localhost in network
#: address", checked 2026-11-24), so this is the narrowest rule it can express.
INSTALL_PORTS: tuple[int, ...] = (443, 80)

#: Tool cache variables and their dir under the cache root (``<scratch>/cache``, or the
#: repo's shared package cache for the install step, #45). pnpm reads its store dir from
#: ``npm_config_store_dir``.
CACHE_VARS: dict[str, str] = {
    "UV_CACHE_DIR": "uv",
    "npm_config_cache": "npm",
    "npm_config_store_dir": "pnpm",
    "YARN_CACHE_FOLDER": "yarn",
    "POETRY_CACHE_DIR": "poetry",
    "PIP_CACHE_DIR": "pip",
}

#: Marker in a worktree's private git dir: the install step ran there (#45). Never
#: writable from inside the sandbox.
INSTALL_MARKER = "fman-installed"

#: Device files a shell and the toolchains write to.
_DEVICES = (
    "/dev/null",
    "/dev/zero",
    "/dev/tty",
    "/dev/dtracehelper",
    "/dev/random",
    "/dev/urandom",
)


class SandboxError(RuntimeError):
    """The sandbox could not be set up for a launch."""


class SandboxUnavailable(SandboxError):
    """``sandbox = "on"`` but this machine cannot sandbox. The attempt must not run: the
    worker flags the ticket (fail closed, decision 5)."""


# --------------------------------------------------------------------------- the policy


@dataclass(frozen=True)
class GitAccess:
    """The mirror paths a worktree needs for ``git status`` and ``git commit``."""

    reads: tuple[Path, ...] = ()
    writes: tuple[Path, ...] = ()
    write_files: tuple[Path, ...] = ()  # single files (a ref and its lock, a reflog)
    protected: tuple[Path, ...] = ()  # never writable: they redirect the worker's git


@dataclass(frozen=True)
class Policy:
    """What one sandboxed launch may touch. Build it with ``Policy.create``.

    ``worktree``: the attempt's worktree (read and write). ``scratch``: the attempt's own
    temp and cache dir (read and write; HOME, TMPDIR and tool caches point into it).
    ``proxy_port``: the one loopback port outbound TCP may reach (the writer's proxy);
    None = no outbound at all. ``network``: ``allow`` opens all network (a repo's
    ``sandbox_network``). ``listen_loopback``: may bind/accept on loopback (OpenCode's
    own server). ``mode``: ``off`` turns the sandbox off (``wrap`` is the identity)."""

    worktree: Path
    scratch: Path
    proxy_port: int | None = None
    network: Network = "none"
    listen_loopback: bool = False
    mode: Mode = "on"
    reads: tuple[Path, ...] = ()
    writes: tuple[Path, ...] = ()
    home: Path = field(default_factory=Path.home)
    foreman_home: Path | None = None
    toolchains: tuple[Path, ...] = ()
    read_files: tuple[Path, ...] = ()
    shims: tuple[tuple[str, Path], ...] = ()
    user_temp: Path | None = None
    sdkroot: Path | None = None
    git: GitAccess = field(default_factory=GitAccess)
    #: #54: the worker's own Python environment and checkout (``worker_paths``): denied
    #: like the home, and never re-allowed by toolchain discovery.
    worker: tuple[Path, ...] = ()
    #: #45: outbound TCP to any host on these ports (the install step), with DNS.
    outbound_ports: tuple[int, ...] = ()
    #: #45: the package cache root the tool cache variables point at (``child_env``);
    #: None = ``<scratch>/cache``. Only the install step points it at the shared cache.
    cache: Path | None = None
    #: #45: a ``runner.install.InstallPlan``: ``verify.run`` installs the worktree's
    #: dependencies under this policy before its first command there (reviewer checks).
    install: Any = None

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise SandboxError(f"sandbox mode must be one of {MODES}, not {self.mode!r}")
        if self.network not in NETWORKS:
            raise SandboxError(f"sandbox network must be one of {NETWORKS}, not {self.network!r}")
        if self.proxy_port is not None and not 0 < int(self.proxy_port) < 65536:
            raise SandboxError(f"bad proxy port {self.proxy_port!r}")
        for port in self.outbound_ports:
            if not 0 < int(port) < 65536:
                raise SandboxError(f"bad outbound port {port!r}")

    @classmethod
    def create(
        cls,
        worktree: Path | str,
        scratch: Path | str,
        *,
        proxy_port: int | None = None,
        network: Network = "none",
        listen_loopback: bool = False,
        mode: Mode = "on",
        reads: Iterable[Path | str] = (),
        writes: Iterable[Path | str] = (),
        argv0: str | None = None,
        path_env: str | None = None,
        home: Path | str | None = None,
        foreman_home: Path | str | None = None,
        install: Any = None,
    ) -> Policy:
        """A policy for ``worktree``, with the git paths and toolchain dirs found on disk.

        ``argv0`` (the command to run) adds its install prefix to the toolchain reads;
        ``path_env`` defaults to ``$PATH``; ``foreman_home`` defaults to
        ``FOREMAN_INATOR_HOME`` or ``~/.foreman-inator`` (never created here)."""
        home_p = _real(home if home is not None else Path.home())
        fhome = foreman_home
        if fhome is None:
            env_home = os.environ.get("FOREMAN_INATOR_HOME")
            fhome = Path(env_home).expanduser() if env_home else home_p / ".foreman-inator"
        wt = _real(worktree)
        scratch_p = _real(scratch)
        worker = worker_paths()
        path_value = os.environ.get("PATH", "") if path_env is None else path_env
        path_value = scrub_path(path_value, worker)
        return cls(
            worktree=wt,
            scratch=scratch_p,
            proxy_port=proxy_port,
            network=network,
            listen_loopback=listen_loopback,
            mode=mode,
            reads=tuple(_real(p) for p in reads),
            writes=tuple(_real(p) for p in writes),
            home=home_p,
            foreman_home=_real(fhome),
            toolchains=toolchain_reads(path_value, home_p, argv0=argv0, exclude=worker),
            read_files=xcrun_cache(),
            user_temp=_user_temp(),
            shims=xcrun_shims(path_value),
            sdkroot=_sdkroot(),
            git=git_access(wt),
            worker=worker,
            install=install,
        )

    def bind(
        self,
        worktree: Path | str | None = None,
        *,
        reads: Iterable[Path | str] = (),
        writes: Iterable[Path | str] = (),
        argv0: str | None = None,
    ) -> Policy:
        """This policy for another worktree (the reviewer's scratch worktrees; None keeps
        the current one), plus extra readable and writable paths (a check's work dir,
        OpenCode's state dirs) and the install prefix of ``argv0``."""
        wt = self.worktree if worktree is None else _real(worktree)
        more_reads = tuple(p for p in (_real(r) for r in reads) if p not in self.reads)
        more_writes = tuple(p for p in (_real(w) for w in writes) if p not in self.writes)
        tools = self.toolchains
        if argv0 is not None:
            found = toolchain_reads("", self.home, argv0=argv0, exclude=self.worker)
            tools = tools + tuple(p for p in found if p not in tools)
        return replace(
            self,
            worktree=wt,
            reads=self.reads + more_reads,
            writes=self.writes + more_writes,
            toolchains=tools,
            git=self.git if wt == self.worktree else git_access(wt),
        )

    def for_command(self, argv0: str) -> Policy:
        """This policy plus the install prefix of ``argv0`` (e.g. a venv's python)."""
        return self.bind(self.worktree, argv0=argv0)


# --------------------------------------------------------------------------- discovery


def _real(p: Path | str) -> Path:
    """Absolute, symlink-free path (Seatbelt matches the resolved path: /private/var)."""
    return Path(os.path.realpath(os.path.expanduser(str(p))))


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _user_temp() -> Path | None:
    """``$DARWIN_USER_TEMP_DIR`` (``/private/var/folders/../T``), or None off macOS."""
    if sys.platform != "darwin":
        return None
    try:
        tmp = os.confstr(65537)  # _CS_DARWIN_USER_TEMP_DIR (no name in os.confstr_names)
    except (ValueError, OSError):
        return None
    return _real(tmp) if tmp else None


def xcrun_cache() -> tuple[Path, ...]:
    """xcrun's tool-path cache (``$DARWIN_USER_TEMP_DIR/xcrun_db``), read-only.

    ``/usr/bin/git``, ``swift``, ``make`` and ``clang`` are xcrun shims. Without this read
    each call re-resolves (about 1.3 s, running xcodebuild) and prints "couldn't create
    cache file"; writing it is never allowed (it maps tool names to paths, so a poisoned
    cache would redirect the user's own, unsandboxed tools)."""
    tmp = _user_temp()
    return (tmp / "xcrun_db",) if tmp is not None else ()


#: ``/usr/bin`` tools that are xcrun shims; linked directly so xcrun is not consulted.
XCRUN_TOOLS: tuple[str, ...] = (
    "git",
    "make",
    "swift",
    "swiftc",
    "clang",
    "clang++",
    "cc",
    "c++",
    "ld",
    "ar",
)


@functools.lru_cache(maxsize=8)
def _xcrun_find(tool: str) -> str | None:
    try:
        out = subprocess.run(
            ["/usr/bin/xcrun", "--find", tool], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    path = out.stdout.strip()
    return path if out.returncode == 0 and path.startswith("/") else None


def xcrun_shims(path_env: str) -> tuple[tuple[str, Path], ...]:
    """``(name, real binary)`` for each ``XCRUN_TOOLS`` entry that PATH resolves to the
    ``/usr/bin`` shim. Found by test: xcrun keys its cache on HOME, so with HOME moved
    into the scratch dir every shim call missed the cache (about 1.3 s, a stray
    xcodebuild, and a "couldn't create cache file" line in the verify output)."""
    if sys.platform != "darwin" or not os.access("/usr/bin/xcrun", os.X_OK):
        return ()
    out: list[tuple[str, Path]] = []
    for tool in XCRUN_TOOLS:
        found = shutil.which(tool, path=path_env or None)
        if found is None or _real(found) != Path("/usr/bin") / tool:
            continue
        target = _xcrun_find(tool)
        if target is not None:
            out.append((tool, Path(target)))
    return tuple(out)


@functools.lru_cache(maxsize=1)
def _sdkroot() -> Path | None:
    """The macOS SDK path, so compilers do not ask xcrun for it on every call."""
    if sys.platform != "darwin" or not os.access("/usr/bin/xcrun", os.X_OK):
        return None
    try:
        out = subprocess.run(
            ["/usr/bin/xcrun", "--show-sdk-path"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    path = out.stdout.strip()
    return Path(path) if out.returncode == 0 and path.startswith("/") else None


def git_access(worktree: Path) -> GitAccess:
    """The mirror paths a linked worktree needs, from its ``.git`` file.

    Found by test (``tests/test_sandbox.py``): ``git status`` reads the common dir and
    writes the worktree's private git dir (index refresh); ``git commit`` also writes new
    objects, this worktree's branch ref (plus its ``.lock``) and its reflog. A worktree
    whose ``.git`` is a directory (a plain clone) needs nothing more than the worktree."""
    dotgit = worktree / ".git"
    if not dotgit.is_file():
        return GitAccess()
    try:
        text = dotgit.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return GitAccess()
    if not text.startswith("gitdir:"):
        return GitAccess()
    raw = Path(text.removeprefix("gitdir:").strip())
    gitdir = _real(raw if raw.is_absolute() else worktree / raw)
    common = gitdir
    with _suppress_os():
        rel = (gitdir / "commondir").read_text(encoding="utf-8").strip()
        common = _real(Path(rel) if Path(rel).is_absolute() else gitdir / rel)
    files: list[Path] = []
    with _suppress_os():
        head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: refs/heads/"):
            ref = head.removeprefix("ref: ")
            files += [common / ref, common / f"{ref}.lock"]
            # The reflog and the dirs leading to it may not exist yet (a bare mirror
            # keeps no reflogs; a worktree commit creates one).
            log = common / "logs" / ref
            for parent in reversed(log.parents):
                if _within(parent, common / "logs"):
                    files.append(parent)
            files += [log, common / "logs" / f"{ref}.lock"]
    return GitAccess(
        reads=(common,),
        writes=(gitdir, common / "objects"),
        write_files=tuple(files),
        protected=(
            worktree / ".git",
            gitdir / "commondir",
            gitdir / "gitdir",
            gitdir / "fman-base",
            gitdir / "config.worktree",
            gitdir / INSTALL_MARKER,
        ),
    )


class _suppress_os:
    def __enter__(self) -> None:
        return None

    def __exit__(self, typ: object, exc: object, tb: object) -> bool:
        return isinstance(exc, OSError)


def _prefix_of(path: Path, home: Path) -> Path:
    """The dir to allow for an executable at ``path``: its install prefix
    (``<prefix>/bin/<exe>`` -> ``<prefix>``) when that prefix is a venv (has
    ``pyvenv.cfg``) or lies inside a ``KNOWN_TOOLCHAINS`` dir or outside the home;
    otherwise only the executable's own dir. So ``~/.local/bin``, ``~/.cargo/bin`` or
    ``~/.orbstack/bin`` never open ``~/.local``, ``~/.cargo`` (credentials) or
    ``~/.orbstack`` (ssh keys)."""
    parent = path.parent
    if parent.name != "bin":
        return parent
    prefix = parent.parent
    if not _within(prefix, home):
        return prefix
    if (prefix / "pyvenv.cfg").is_file():
        return prefix
    if any(_within(prefix, home / k) and prefix != home / k for k in KNOWN_TOOLCHAINS):
        return prefix
    return parent


def _exe_chain(exe: Path) -> list[Path]:
    """``exe`` and every hop of its symlink chain, absolute."""
    out = [Path(os.path.abspath(exe))]
    cur = out[0]
    for _ in range(16):
        if not cur.is_symlink():
            break
        target = Path(os.readlink(cur))
        cur = Path(os.path.abspath(cur.parent / target))
        out.append(cur)
    return out


def worker_paths() -> tuple[Path, ...]:
    """The worker's own Python environment and source (#54), as real paths: the venv it
    runs in (``sys.prefix``, e.g. ``<checkout>/.venv`` under ``uv run``, or
    ``~/.local/share/uv/tools/foreman-inator`` as a uv tool), an activated
    ``VIRTUAL_ENV``/``CONDA_PREFIX``, foreman-inator's uv-tool and pipx installs when they
    exist, and the checkout holding the package source when it is not inside one of those
    (an editable install). Never the home itself or ``/``."""
    home = _real(Path.home())
    found: list[Path] = []

    def add(p: Path | str | None) -> None:
        if not p:
            return
        rp = _real(p)
        if rp in (Path("/"), home) or _within(home, rp) or rp in found:
            return
        found.append(rp)

    if sys.prefix != sys.base_prefix:
        add(sys.prefix)
    add(os.environ.get("VIRTUAL_ENV"))
    add(os.environ.get("CONDA_PREFIX"))
    tool_dir = os.environ.get("UV_TOOL_DIR") or str(home / ".local" / "share" / "uv" / "tools")
    pipx = home / ".local" / "pipx" / "venvs"
    for p in (Path(tool_dir) / "foreman-inator", pipx / "foreman-inator"):
        if p.is_dir():
            add(p)
    pkg = _real(Path(__file__)).parent.parent  # .../foreman_inator
    if not any(_within(pkg, p) for p in found):
        for parent in pkg.parents:
            if (parent / "pyproject.toml").is_file():
                add(parent)
                break
    return tuple(found)


def scrub_path(path_env: str, worker: Iterable[Path] | None = None) -> str:
    """``path_env`` without the entries inside the worker's own environment (#54):
    ``worker_paths()`` plus any extra ``worker`` dirs."""
    roots = (*worker_paths(), *(_real(w) for w in worker or ()))
    if not path_env or not roots:
        return path_env
    keep = [
        e for e in path_env.split(os.pathsep)
        if not (e and os.path.isabs(e) and any(_within(_real(e), r) for r in roots))
    ]  # fmt: skip
    return os.pathsep.join(keep)


def scrub_worker_env(
    env: Mapping[str, str], worker: Iterable[Path] | None = None
) -> dict[str, str]:
    """``env`` without the worker's own Python environment (#54): the activation variables
    (``WORKER_ENV_NAMES``, ``WORKER_ENV_PREFIXES``) dropped and the worker's venv ``bin``
    (and its checkout, its uv-tool dir) removed from ``PATH``. Used for every child that
    runs repo or model code, sandboxed or not (``sandbox = "off"`` too)."""
    out = {
        k: v
        for k, v in env.items()
        if k not in WORKER_ENV_NAMES and not k.startswith(WORKER_ENV_PREFIXES)
    }
    if "PATH" in out:
        out["PATH"] = scrub_path(out["PATH"], worker)
    return out


def toolchain_reads(
    path_env: str,
    home: Path,
    *,
    argv0: str | None = None,
    exclude: Iterable[Path] = (),
) -> tuple[Path, ...]:
    """Toolchain dirs inside the home (or other denied roots) that the launch may read.

    Every PATH entry inside a denied root, its install prefix (``~/.nvm/versions/node/v24``
    for ``.../v24/bin``), the prefix of each symlink in it that points elsewhere in the
    home (``~/.local/bin/x`` -> a uv tool), the known toolchain dirs that exist, and the
    whole symlink chain of ``argv0`` (a venv's ``bin/python`` -> the venv and the real
    interpreter). Anything in ``SENSITIVE`` is dropped, and so is anything found by
    discovery inside ``exclude`` (the worker's own environment, #54); only an ``argv0``
    given as an absolute path (an explicit choice of interpreter) is kept there. A bare
    ``argv0`` is looked up on ``path_env`` (else ``$PATH``) without the excluded dirs."""
    home = _real(home)
    roots = [Path(r) for r in DENIED_ROOTS] + [home]
    excluded = tuple(_real(e) for e in exclude)
    found: list[Path] = []

    def add(p: Path | None, *, explicit: bool = False) -> None:
        if p is None:
            return
        p = _real(p)
        if not any(_within(p, r) for r in roots):
            return  # readable anyway
        if p == home or any(_within(p, home / s) for s in SENSITIVE):
            return
        if not explicit and any(_within(p, e) or _within(e, p) for e in excluded):
            return
        if p not in found:
            found.append(p)

    for entry in path_env.split(os.pathsep) if path_env else ():
        if not entry:
            continue
        d = Path(entry).expanduser()
        if not d.is_absolute() or not d.is_dir():
            continue
        add(d)
        add(_prefix_of(_real(d) / "x", home))
        if _within(_real(d), home):
            with _suppress_os():
                for child in d.iterdir():
                    if child.is_symlink():
                        target = _real(child)
                        if _within(target, home):
                            add(_prefix_of(target, home))
    for rel in KNOWN_TOOLCHAINS:
        p = home / rel
        if p.is_dir():
            add(p)
    if argv0:
        exe = Path(argv0)
        explicit = exe.is_absolute()
        if not explicit:
            search = path_env or os.environ.get("PATH", "")
            which = shutil.which(argv0, path=scrub_path(search, excluded) or None)
            exe = Path(which) if which else exe
        if exe.is_absolute():
            for hop in _exe_chain(exe):
                add(_prefix_of(hop, home), explicit=explicit)
    return tuple(found)


# --------------------------------------------------------------------------- the profile


def _q(path: Path | str) -> str:
    """An SBPL string literal."""
    s = str(path).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _subpaths(paths: Iterable[Path | str]) -> str:
    return " ".join(f"(subpath {_q(p)})" for p in paths)


def _literals(paths: Iterable[Path | str]) -> str:
    return " ".join(f"(literal {_q(p)})" for p in paths)


def _globals(names: Iterable[str]) -> str:
    return " ".join(f"(global-name {_q(n)})" for n in names)


def _rule(action: str, ops: str, filters: str) -> list[str]:
    """One rule, or none when there are no filters: an allow without a filter would
    allow the operation everywhere."""
    return [f"({action} {ops} {filters})"] if filters else []


def render_profile(policy: Policy) -> str:
    """The Seatbelt profile text for ``policy``. Pure: reads nothing from disk."""
    home = policy.home
    own_reads = [policy.worktree, policy.scratch, *policy.git.reads, *policy.reads, *policy.writes]
    own_writes = [policy.worktree, policy.scratch, *policy.git.writes, *policy.writes]
    denied = [*DENIED_ROOTS, home, *policy.worker]
    if policy.foreman_home is not None:
        denied.append(policy.foreman_home)
    sensitive = [home / s for s in SENSITIVE]
    if policy.foreman_home is not None:
        sensitive.append(policy.foreman_home)
    lines = [
        f"(version {PROFILE_VERSION})",
        ";; foreman-inator sandbox (todo #40), generated per launch. Order matters: a",
        ";; filtered rule beats an unfiltered one; among filtered rules the last match wins.",
        "(deny default)",
        "(allow process-fork)",
        "(allow process-exec)",
        "(allow signal (target same-sandbox))",
        "(allow sysctl-read)",
        "(allow ipc-posix-sem)",  # multiprocessing locks and queues
        *_rule("allow", "mach-lookup", _globals(MACH_SERVICES)),
        ";; reads: system and toolchain paths, not homes or other processes' temp files",
        "(allow file-read*)",
        *_rule("deny", "file-read*", _subpaths(denied)),
        *_rule("allow", "file-read*", _subpaths(policy.toolchains)),
        *_rule("deny", "file-read*", _subpaths(sensitive)),
        '(allow file-read-metadata (subpath "/"))',
        *_rule("allow", "file-read*", _subpaths(own_reads)),
        *_rule("allow", "file-read*", _literals(policy.read_files)),
        ";; writes: the worktree, the scratch dir, this worktree's git paths",
        *_rule("allow", "file-write*", _subpaths(own_writes)),
        *_rule("allow", "file-write*", _literals(policy.git.write_files)),
        "(allow file-write-data file-ioctl " + _literals(_DEVICES) + ' (regex #"^/dev/fd/"))',
        *_atomic_writes(policy),
        *_rule("deny", "file-write*", _literals(policy.git.protected)),
        ";; network",
        *_network(policy),
    ]
    return "\n".join(lines) + "\n"


_REGEX_SPECIAL = set(".^$*+?()[]{}|\\")


def _re(path: Path | str) -> str:
    return "".join("\\" + c if c in _REGEX_SPECIAL else c for c in str(path))


def _atomic_writes(policy: Policy) -> list[str]:
    """Foundation's atomic writes (SwiftPM's build files) stage in
    ``$DARWIN_USER_TEMP_DIR/TemporaryItems/NSIRD_<process>_XXXX``; found by test. Only
    the swift tools' own staging dirs are writable, not other apps' items there."""
    if policy.user_temp is None:
        return []
    rx = "^" + _re(policy.user_temp / "TemporaryItems" / "NSIRD_swift") + "[^/]*(/|$)"
    return [f'(allow file-write* (regex #"{rx}"))']


#: DNS through libinfo goes to mDNSResponder over this socket (with the dnssd service).
_DNS_SOCKET = "/private/var/run/mDNSResponder"


def _network(policy: Policy) -> list[str]:
    if policy.network == "allow":
        return ["(allow network*)", *_rule("allow", "mach-lookup", _globals(NETWORK_MACH_SERVICES))]
    out: list[str] = []
    if policy.outbound_ports:
        # #45, the install step: any host, these ports only (the profile language cannot
        # name hosts), plus name resolution and certificate trust.
        out += [f'(allow network-outbound (remote ip "*:{int(p)}"))' for p in policy.outbound_ports]
        out.append(f"(allow network-outbound (literal {_q(_DNS_SOCKET)}))")
        out += _rule("allow", "mach-lookup", _globals(NETWORK_MACH_SERVICES))
    if policy.proxy_port is not None:
        out.append(f'(allow network-outbound (remote ip "localhost:{int(policy.proxy_port)}"))')
    if policy.listen_loopback:
        out += [
            '(allow network-bind (local ip "localhost:*"))',
            '(allow network-inbound (local ip "localhost:*"))',
        ]
    return out


# --------------------------------------------------------------------------- backends


class Backend(Protocol):
    """One way to sandbox a launch. ``wrap`` raises ``SandboxUnavailable`` when it
    cannot; it never returns an unsandboxed argv for an ``on`` policy."""

    name: str

    def available(self) -> bool: ...

    def wrap(self, argv: Sequence[str], policy: Policy) -> list[str]: ...


class SeatbeltBackend:
    """macOS ``sandbox-exec`` with a generated profile passed by ``-p``."""

    name = "sandbox-exec"

    def __init__(self, sandbox_exec: str = SANDBOX_EXEC, platform: str | None = None):
        self.sandbox_exec = sandbox_exec
        self.platform = sys.platform if platform is None else platform

    def available(self) -> bool:
        return self.platform == "darwin" and os.access(self.sandbox_exec, os.X_OK)

    def wrap(self, argv: Sequence[str], policy: Policy) -> list[str]:
        if not argv:
            raise SandboxError("nothing to run")
        if not self.available():
            raise SandboxUnavailable(
                f'sandbox = "on" but {self.sandbox_exec} is not usable on this machine '
                f'({self.platform}); the attempt was not run. Set sandbox = "off" in '
                "config.toml only if you accept running model-written code unsandboxed."
            )
        return [self.sandbox_exec, "-p", render_profile(policy), "--", *argv]


_backend: Backend = SeatbeltBackend()


def backend() -> Backend:
    return _backend


def set_backend(b: Backend) -> Backend:
    """Swap the backend (tests, or a future Linux one). Returns the previous one."""
    global _backend
    old, _backend = _backend, b
    return old


def wrap(argv: Sequence[str], policy: Policy | None) -> list[str]:
    """``argv`` to run under ``policy``. None or ``mode="off"``: argv unchanged."""
    if policy is None or policy.mode == "off":
        return list(argv)
    return _backend.wrap(argv, policy)


def ensure_available(policy: Policy | None) -> None:
    """Raise ``SandboxUnavailable`` now (before an attempt starts) if ``policy`` is on and
    the backend cannot run it. The worker turns this into "flag the ticket"."""
    if policy is not None and policy.mode == "on" and not _backend.available():
        _backend.wrap(["true"], policy)  # raises the backend's own message


def child_env(policy: Policy | None, base_path: str | None = None) -> dict[str, str]:
    """Environment overrides for a sandboxed child: HOME, TMPDIR and tool caches point
    into the scratch dir (created here), so nothing a tool writes by default lands outside
    it; ``PATH`` gains ``<scratch>/bin`` in front, holding direct links to the tools that
    ``/usr/bin`` only reaches through xcrun (see ``xcrun_shims``). ``base_path`` is the
    PATH to extend (default ``$PATH``). Empty for None or ``off``."""
    if policy is None or policy.mode == "off":
        return {}
    s = policy.scratch
    cache = policy.cache if policy.cache is not None else s / "cache"
    dirs = {
        "HOME": s / "home",
        "TMPDIR": s / "tmp",
        "XDG_CACHE_HOME": s / "cache",
        "XDG_CONFIG_HOME": s / "home" / ".config",
        "XDG_DATA_HOME": s / "home" / ".local" / "share",
        "XDG_STATE_HOME": s / "home" / ".local" / "state",
        "CLANG_MODULE_CACHE_PATH": s / "cache" / "clang",
        **{var: cache / sub for var, sub in CACHE_VARS.items()},
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    env = {k: str(v) for k, v in dirs.items()}
    # #55: temp files of every shell and runtime go to the scratch dir. zsh makes its
    # here-document and `=(...)` files at TMPPREFIX (default /tmp/zsh, which the profile
    # denies: "can't create temp file for here document"); bash and /bin/sh (bash 3.2 on
    # macOS) use TMPDIR; node's os.tmpdir() and some tools read TMP/TEMP.
    env["TMPPREFIX"] = str(dirs["TMPDIR"] / "zsh")
    env["TMP"] = env["TEMP"] = env["TMPDIR"]
    bin_dir = s / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, target in policy.shims:
        link = bin_dir / name
        if link.exists() or link.is_symlink():
            continue
        if name == "swift":
            link.write_text(_swift_wrapper(target, s), encoding="utf-8")
            link.chmod(0o755)
        else:
            link.symlink_to(target)
    path = os.environ.get("PATH", "") if base_path is None else base_path
    path = scrub_path(path, policy.worker)  # #54: never the worker's own venv
    env["PATH"] = os.pathsep.join([str(bin_dir), path]) if path else str(bin_dir)
    # A login shell (OpenCode runs commands with `$SHELL -l`) rebuilds PATH with
    # path_helper, putting /usr/bin first again; its profile in the scratch HOME puts the
    # direct links back in front. Found by test.
    line = f'export PATH={shlex.quote(str(bin_dir))}:"$PATH"\n'
    for name in (".zprofile", ".bash_profile", ".profile"):
        prof = dirs["HOME"] / name
        if not prof.exists():
            prof.write_text("# foreman-inator sandbox (todo #40)\n" + line, encoding="utf-8")
    if policy.sdkroot is not None:
        env["SDKROOT"] = str(policy.sdkroot)
    env["npm_config_update_notifier"] = "false"
    if policy.network == "none" and not policy.outbound_ports:
        env["UV_OFFLINE"] = "1"
        env["npm_config_offline"] = "true"
    return env


def _swift_wrapper(real: Path, scratch: Path) -> str:
    """``swift`` for inside the sandbox. SwiftPM sandboxes manifest and plugin builds with
    ``sandbox-exec`` itself, which cannot nest ("sandbox_apply: Operation not permitted"),
    and keeps caches under the real home (``getpwuid``, not ``$HOME``). So build, test,
    run and package get ``--disable-sandbox`` (the outer sandbox already applies) and
    their caches moved into the scratch dir."""
    flags = " ".join(
        shlex.quote(a)
        for a in (
            "--disable-sandbox",
            "--manifest-cache",
            "local",
            "--cache-path",
            str(scratch / "cache" / "swiftpm"),
            "--config-path",
            str(scratch / "home" / "swiftpm" / "config"),
            "--security-path",
            str(scratch / "home" / "swiftpm" / "security"),
        )
    )
    real_q = shlex.quote(str(real))
    return (
        "#!/bin/sh\n"
        "# foreman-inator sandbox: swift without SwiftPM's own (nested) sandbox (todo #40)\n"
        'case "$1" in\n'
        f'  build|test|run|package) sub="$1"; shift; exec {real_q} "$sub" {flags} "$@" ;;\n'
        "esac\n"
        f'exec {real_q} "$@"\n'
    )


def apply_env(base: Mapping[str, str], policy: Policy | None) -> dict[str, str]:
    """``base`` without the worker's own Python environment (``scrub_worker_env``, #54;
    also for None and ``off``), with ``child_env(policy)`` laid over it."""
    env = scrub_worker_env(base, policy.worker if policy is not None else ())
    env.update(child_env(policy, env.get("PATH", "")))
    return env


_current: contextvars.ContextVar[Policy | None] = contextvars.ContextVar(
    "foreman_inator_sandbox", default=None
)


@contextlib.contextmanager
def active(policy: Policy | None) -> Iterator[Policy | None]:
    """Make ``policy`` the default for launches in this thread/task that are given none
    explicitly (the reviewer's checks run inside ``review.reviewer.review_change``, which
    takes no sandbox argument: the worker wraps ``_launch_review`` in this)."""
    token = _current.set(policy)
    try:
        yield policy
    finally:
        _current.reset(token)


def current() -> Policy | None:
    """The policy set by the innermost ``active``, or None."""
    return _current.get()


def describe(
    mode: Mode,
    networks: Mapping[str, Network] | None = None,
    installs: Mapping[str, str] | None = None,
) -> str:
    """One line for ``fman status`` and the PR description (decision 5). With
    ``installs`` (``config.sandbox_install``, #45) it also says how dependencies are
    installed; None leaves that sentence out (the pre-#45 line)."""
    if mode == "off":
        line = 'Sandbox: OFF (sandbox = "off" in config): model-written code runs unsandboxed.'
    else:
        opened = sorted(r for r, n in (networks or {}).items() if n == "allow")
        extra = f"; network open for {', '.join(opened)}" if opened else ""
        line = f"Sandbox: on ({SeatbeltBackend.name}){extra}."
    if installs is None:
        return line
    return f"{line} {describe_install(mode, installs)}"


def describe_install(mode: Mode, installs: Mapping[str, str] | None = None) -> str:
    """The dependency-install sentence (#45) of the status / PR line."""
    off = sorted(r for r, v in (installs or {}).items() if v == "off")
    skipped = f"; skipped for {', '.join(off)} (sandbox_install = off)" if off else ""
    if mode == "off":
        return f"Dependency install: unsandboxed, with network{skipped}."
    ports = "/".join(str(p) for p in INSTALL_PORTS)
    return (
        f"Dependency install: sandboxed, network to any host on ports {ports} only for the "
        f"base branch's own files, offline after model edits{skipped}."
    )


__all__ = [
    "DENIED_ROOTS",
    "MACH_SERVICES",
    "CACHE_VARS",
    "INSTALL_MARKER",
    "INSTALL_PORTS",
    "SENSITIVE",
    "WORKER_ENV_NAMES",
    "Backend",
    "GitAccess",
    "Policy",
    "SandboxError",
    "SandboxUnavailable",
    "SeatbeltBackend",
    "active",
    "apply_env",
    "backend",
    "child_env",
    "describe_install",
    "scrub_path",
    "scrub_worker_env",
    "worker_paths",
    "current",
    "describe",
    "ensure_available",
    "git_access",
    "render_profile",
    "set_backend",
    "toolchain_reads",
    "wrap",
]
