"""Check the tools the worker needs. Prints install/pull commands; never runs them.

Checked: ``git`` >= 2.31 (worktree manager), ``opencode``, ``ollama``, and whether each
local tier 1/2 model in the config is pulled in Ollama.

Shapes relied on (rule 12, read 2026-11-23):
- Ollama ``GET /api/tags`` -> ``{"models": [{"name": ..., "model": ...}, ...]}`` on the
  Ollama host (the config's base URL minus its ``/v1`` OpenAI-compatible suffix).
  https://docs.ollama.com/api/tags
- OpenCode install commands: https://opencode.ai/docs/
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from foreman_inator.config import Config

MIN_GIT = (2, 31)

INSTALL_HINTS = {
    "git": "xcode-select --install   (or: brew install git)",
    "opencode": "curl -fsSL https://opencode.ai/v2/install | bash   "
    "(or: brew install anomalyco/tap/opencode-v2)",
    "ollama": "brew install ollama   (or the app from https://ollama.com/download)",
}

Runner = Callable[[Sequence[str]], str]  # returns stdout; raises OSError/CalledProcessError
Which = Callable[[str], str | None]


def _run(argv: Sequence[str]) -> str:
    return subprocess.run(list(argv), capture_output=True, text=True, check=True, timeout=30).stdout


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str  # version found, or why it failed
    fix: str | None = None  # the command to run, when not ok


def parse_git_version(text: str) -> tuple[int, int, int] | None:
    m = re.search(r"git version (\d+)\.(\d+)(?:\.(\d+))?", text)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


def check_git(*, run: Runner = _run, which: Which = shutil.which) -> Check:
    if not which("git"):
        return Check("git", False, "not found on PATH", INSTALL_HINTS["git"])
    try:
        version = parse_git_version(run(["git", "--version"]))
    except (OSError, subprocess.SubprocessError) as exc:
        return Check("git", False, f"`git --version` failed: {exc}", INSTALL_HINTS["git"])
    if version is None:
        return Check("git", False, "could not read the version", INSTALL_HINTS["git"])
    shown = ".".join(map(str, version))
    if version[:2] < MIN_GIT:
        need = ".".join(map(str, MIN_GIT))
        return Check("git", False, f"{shown} is older than {need}", "brew install git")
    return Check("git", True, shown)


def check_tool(name: str, *, run: Runner = _run, which: Which = shutil.which) -> Check:
    path = which(name)
    if not path:
        return Check(name, False, "not found on PATH", INSTALL_HINTS.get(name))
    try:
        out = run([name, "--version"]).strip().splitlines()
        detail = out[0] if out else path
    except (OSError, subprocess.SubprocessError):
        detail = path
    return Check(name, True, detail)


def ollama_host(base_url: str) -> str:
    """``http://127.0.0.1:11434/v1`` -> ``http://127.0.0.1:11434``."""
    u = urlparse(base_url)
    return f"{u.scheme}://{u.netloc}"


def _matches(wanted: str, pulled: set[str]) -> bool:
    # Ollama names an untagged model "<name>:latest".
    return wanted in pulled or (":" not in wanted and f"{wanted}:latest" in pulled)


def check_models(
    cfg: Config, *, transport: httpx.BaseTransport | None = None, tiers: Sequence[int] = (1, 2)
) -> list[Check]:
    """One check per local model of ``tiers``: is it pulled in Ollama?"""
    wanted = [m for t in tiers for m in cfg.models_for_tier(t) if m.kind == "local"]
    checks: list[Check] = []
    pulled_by_host: dict[str, set[str] | str] = {}
    for m in wanted:
        host = ollama_host(m.base_url)
        if host not in pulled_by_host:
            try:
                with httpx.Client(transport=transport, timeout=5.0) as http:
                    resp = http.get(f"{host}/api/tags")
                    resp.raise_for_status()
                    data = resp.json()
                names: set[str] = set()
                for entry in data.get("models", []):
                    names.update(str(entry[k]) for k in ("name", "model") if entry.get(k))
                pulled_by_host[host] = names
            except (httpx.HTTPError, ValueError) as exc:
                pulled_by_host[host] = f"Ollama not reachable at {host} ({exc.__class__.__name__})"
        pulled = pulled_by_host[host]
        label = f"model tier {m.tier}: {m.model}"
        if isinstance(pulled, str):
            fix = f"start Ollama (open the app or `ollama serve`), then: ollama pull {m.model}"
            checks.append(Check(label, False, pulled, fix))
        elif _matches(m.model, pulled):
            checks.append(Check(label, True, "pulled"))
        else:
            checks.append(Check(label, False, "not pulled", f"ollama pull {m.model}"))
    return checks


def check_all(
    cfg: Config,
    *,
    run: Runner = _run,
    which: Which = shutil.which,
    transport: httpx.BaseTransport | None = None,
) -> list[Check]:
    checks = [
        check_git(run=run, which=which),
        check_tool("opencode", run=run, which=which),
        check_tool("ollama", run=run, which=which),
    ]
    return checks + check_models(cfg, transport=transport)


def format_report(checks: Sequence[Check]) -> str:
    lines = []
    for c in checks:
        mark = "ok " if c.ok else "MISSING"
        lines.append(f"  [{mark}] {c.name}: {c.detail}")
        if not c.ok and c.fix:
            lines.append(f"            run: {c.fix}")
    return "\n".join(lines)


__all__ = [
    "INSTALL_HINTS",
    "MIN_GIT",
    "Check",
    "check_all",
    "check_git",
    "check_models",
    "check_tool",
    "format_report",
    "ollama_host",
    "parse_git_version",
]
