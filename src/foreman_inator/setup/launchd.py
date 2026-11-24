"""Per-user launchd agents: ``fman run`` at login, restarted if it exits; and (#29) a
nightly ``fman experiment run --all`` (``StartCalendarInterval``, low priority).

Shapes relied on (rule 12, read 2026-11-23, launchd.plist(5) and launchctl(1)):
- Keys: ``Label`` (required), ``ProgramArguments``, ``RunAtLoad``, ``KeepAlive`` (true =
  keep the job alive unconditionally), ``StandardOutPath``, ``StandardErrorPath``,
  ``EnvironmentVariables``, ``WorkingDirectory``, ``ProcessType``; for the nightly agent
  ``StartCalendarInterval`` (a dict of ``Hour``/``Minute`` integers: run at that local
  time; a run missed while the Mac slept starts on wake), ``LowPriorityIO`` and ``Nice``.
- ``launchctl bootstrap gui/<uid> <plist>`` loads the agent into the user's login domain;
  ``launchctl bootout gui/<uid>/<label>`` removes it.
  https://keith.github.io/xcode-man-pages/launchd.plist.5.html
  https://keith.github.io/xcode-man-pages/launchctl.1.html

The launchctl runner and the LaunchAgents folder are injectable, so tests never load a
real agent.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from foreman_inator import paths

LABEL = "com.foreman-inator.worker"
PLIST_NAME = f"{LABEL}.plist"
NIGHTLY_LABEL = "com.foreman-inator.experiments"
NIGHTLY_PLIST_NAME = f"{NIGHTLY_LABEL}.plist"
LOGS_DIR_NAME = "logs"
DEFAULT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

Launchctl = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class ServiceError(RuntimeError):
    """launchctl refused, or the ``fman`` executable could not be found."""


def _launchctl(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["launchctl", *argv], capture_output=True, text=True, timeout=30)


def launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def resolve_fman(which: Callable[[str], str | None] = shutil.which) -> Path:
    """Absolute path of the ``fman`` executable, resolved now (launchd has no user PATH)."""
    candidates = [which("fman"), str(Path(sys.executable).parent / "fman")]
    argv0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if argv0 is not None and argv0.name in ("fman", "foreman-inator"):
        candidates.insert(0, str(argv0.with_name("fman")))
    for c in candidates:
        if c and os.access(c, os.X_OK) and Path(c).is_file():
            return Path(c).absolute()
    raise ServiceError("could not find the `fman` executable; install foreman-inator first")


def logs_dir(home: Path | None = None) -> Path:
    path = (home or paths.home()) / LOGS_DIR_NAME
    path.mkdir(mode=paths.DIR_MODE, parents=True, exist_ok=True)
    os.chmod(path, paths.DIR_MODE)
    return path


def plist_dict(fman: Path, *, home: Path | None = None, path_env: str | None = None) -> dict:
    """The agent definition. ``home`` is the foreman-inator home the worker must use."""
    home = home or paths.home()
    logs = logs_dir(home)
    env = {"PATH": path_env or _path_for_agent(fman)}
    if os.environ.get(paths.ENV_HOME):
        env[paths.ENV_HOME] = str(home)
    return {
        "Label": LABEL,
        "ProgramArguments": [str(fman), "run"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "WorkingDirectory": str(home),
        "StandardOutPath": str(logs / "worker.out.log"),
        "StandardErrorPath": str(logs / "worker.err.log"),
        "EnvironmentVariables": env,
        "ProcessType": "Background",
    }


def nightly_plist_dict(
    fman: Path,
    *,
    home: Path | None = None,
    path_env: str | None = None,
    hour: int = 2,
    minute: int = 0,
) -> dict:
    """The nightly experiments agent (#29): ``fman experiment run --all`` at ``hour:minute``
    local time, at low CPU and I/O priority so the live worker goes first."""
    home = home or paths.home()
    logs = logs_dir(home)
    env = {"PATH": path_env or _path_for_agent(fman)}
    if os.environ.get(paths.ENV_HOME):
        env[paths.ENV_HOME] = str(home)
    return {
        "Label": NIGHTLY_LABEL,
        "ProgramArguments": [str(fman), "experiment", "run", "--all"],
        "StartCalendarInterval": {"Hour": int(hour), "Minute": int(minute)},
        "WorkingDirectory": str(home),
        "StandardOutPath": str(logs / "experiments.out.log"),
        "StandardErrorPath": str(logs / "experiments.err.log"),
        "EnvironmentVariables": env,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
    }


def _path_for_agent(fman: Path) -> str:
    """fman's folder first, then the user's PATH, then the usual Homebrew/system folders."""
    parts: list[str] = [str(fman.parent)]
    for p in os.environ.get("PATH", "").split(os.pathsep) + DEFAULT_PATH.split(":"):
        if p and p not in parts:
            parts.append(p)
    return ":".join(parts)


@dataclass(frozen=True)
class ServiceResult:
    plist_path: Path
    loaded: bool
    message: str


def write_plist(data: dict, agents_dir: Path, name: str = PLIST_NAME) -> Path:
    agents_dir.mkdir(parents=True, exist_ok=True)
    path = agents_dir / name
    tmp = path.with_suffix(".plist.tmp")
    with open(tmp, "wb") as fh:
        plistlib.dump(data, fh, fmt=plistlib.FMT_XML)
    os.replace(tmp, path)
    return path


def install_service(
    *,
    fman: Path | None = None,
    agents_dir: Path | None = None,
    launchctl: Launchctl = _launchctl,
    uid: int | None = None,
    home: Path | None = None,
) -> ServiceResult:
    """Write the plist and (re)load it with ``launchctl bootstrap``."""
    fman = fman or resolve_fman()
    uid = os.getuid() if uid is None else uid
    path = write_plist(plist_dict(fman, home=home), agents_dir or launch_agents_dir())
    domain = f"gui/{uid}"
    launchctl(["bootout", f"{domain}/{LABEL}"])  # not loaded yet is fine
    res = launchctl(["bootstrap", domain, str(path)])
    if res.returncode != 0:
        err = (res.stderr or res.stdout or "").strip()
        return ServiceResult(path, False, f"launchctl bootstrap failed ({res.returncode}): {err}")
    return ServiceResult(path, True, f"loaded {LABEL} in {domain}; it starts `fman run` at login")


def uninstall_service(
    *,
    agents_dir: Path | None = None,
    launchctl: Launchctl = _launchctl,
    uid: int | None = None,
) -> ServiceResult:
    """``launchctl bootout`` the agent and delete its plist. Safe when not installed."""
    uid = os.getuid() if uid is None else uid
    path = (agents_dir or launch_agents_dir()) / PLIST_NAME
    res = launchctl(["bootout", f"gui/{uid}/{LABEL}"])
    existed = path.exists()
    path.unlink(missing_ok=True)
    if res.returncode == 0 or existed:
        return ServiceResult(path, False, f"removed {LABEL}")
    return ServiceResult(path, False, f"{LABEL} was not installed")


def install_nightly(
    *,
    fman: Path | None = None,
    agents_dir: Path | None = None,
    launchctl: Launchctl = _launchctl,
    uid: int | None = None,
    home: Path | None = None,
    hour: int = 2,
    minute: int = 0,
) -> ServiceResult:
    """Write the nightly experiments plist and (re)load it, next to the worker's agent."""
    fman = fman or resolve_fman()
    uid = os.getuid() if uid is None else uid
    data = nightly_plist_dict(fman, home=home, hour=hour, minute=minute)
    path = write_plist(data, agents_dir or launch_agents_dir(), NIGHTLY_PLIST_NAME)
    domain = f"gui/{uid}"
    launchctl(["bootout", f"{domain}/{NIGHTLY_LABEL}"])
    res = launchctl(["bootstrap", domain, str(path)])
    if res.returncode != 0:
        err = (res.stderr or res.stdout or "").strip()
        return ServiceResult(path, False, f"launchctl bootstrap failed ({res.returncode}): {err}")
    return ServiceResult(
        path,
        True,
        f"loaded {NIGHTLY_LABEL} in {domain}; it runs `fman experiment run --all` at "
        f"{int(hour):02d}:{int(minute):02d}",
    )


def uninstall_nightly(
    *,
    agents_dir: Path | None = None,
    launchctl: Launchctl = _launchctl,
    uid: int | None = None,
) -> ServiceResult:
    """``launchctl bootout`` the nightly agent and delete its plist. Safe when absent."""
    uid = os.getuid() if uid is None else uid
    path = (agents_dir or launch_agents_dir()) / NIGHTLY_PLIST_NAME
    res = launchctl(["bootout", f"gui/{uid}/{NIGHTLY_LABEL}"])
    existed = path.exists()
    path.unlink(missing_ok=True)
    if res.returncode == 0 or existed:
        return ServiceResult(path, False, f"removed {NIGHTLY_LABEL}")
    return ServiceResult(path, False, f"{NIGHTLY_LABEL} was not installed")


__all__ = [
    "LABEL",
    "NIGHTLY_LABEL",
    "NIGHTLY_PLIST_NAME",
    "PLIST_NAME",
    "ServiceError",
    "ServiceResult",
    "install_nightly",
    "install_service",
    "launch_agents_dir",
    "logs_dir",
    "nightly_plist_dict",
    "plist_dict",
    "resolve_fman",
    "uninstall_nightly",
    "uninstall_service",
    "write_plist",
]
