"""Verify command detection and runner (todo #6).

Detection order (assistant decision, 2026-11-23): the repo's saved override →
a ``Makefile`` ``test``/``check`` target → a ``package.json`` ``test`` script →
a Python project (``pyproject.toml``, ``pytest.ini`` or a ``tests/`` directory with
Python files) → ``Package.swift`` (``swift test``) → ``None``. When ``detect``
returns ``None`` the caller flags the ticket once and saves the command the user
enters as the repo's override.

``run`` never raises for a failing or broken command: a non-zero exit, a missing
executable or a timeout all come back as ``passed=False``. The full output is
written to a file the caller chooses (it becomes a trace attachment); the timeout
kills the command's whole process group.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT_S = 20 * 60
"""Default verify timeout in seconds."""

KILL_GRACE_S = 5.0
"""Seconds between SIGTERM and SIGKILL when a run times out."""

PARSE_TAIL_BYTES = 256 * 1024
"""Only the last this-many bytes of output are scanned for test counts."""

MAKEFILE_NAMES = ("GNUmakefile", "makefile", "Makefile")
MAKE_TARGETS = ("test", "check")
NPM_DEFAULT_TEST = "no test specified"
JS_TEST_SUFFIXES = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts")


@dataclass(frozen=True)
class VerifyCommand:
    """A command that verifies a repo.

    ``argv`` is run without a shell, except for a user-entered override, which
    is run as ``/bin/sh -c <override>``. ``source`` says how it was found:
    ``override``, ``makefile``, ``package.json``, ``python`` or ``swift``.
    """

    argv: tuple[str, ...]
    source: str
    display: str

    @classmethod
    def of(cls, argv: Sequence[str], source: str) -> VerifyCommand:
        return cls(tuple(argv), source, shlex.join(argv))

    @classmethod
    def from_override(cls, override: str) -> VerifyCommand:
        return cls(("/bin/sh", "-c", override), "override", override)


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of one verify run.

    ``passed`` is true only for exit code 0 without a timeout. ``tests_run`` and
    ``tests_failed`` are parsed from pytest, unittest, jest, vitest or XCTest /
    Swift Testing summaries and are ``None`` when no summary was recognised.
    ``tests_run`` counts executed tests (passed + failed + errors, xfail/xpass
    included for pytest; skipped excluded). ``exit_code`` is ``None`` on timeout
    and 127 when the executable was not found.
    """

    passed: bool
    tests_run: int | None
    tests_failed: int | None
    output_path: Path
    exit_code: int | None
    timed_out: bool
    seconds: float
    command: str

    def output_tail(self, max_chars: int = 8000) -> str:
        """The last ``max_chars`` characters of the saved output (for prompts)."""
        return _read_tail(self.output_path, max_chars * 4)[-max_chars:]


# --------------------------------------------------------------------------- detection


def detect(
    repo_dir: Path | str,
    override: str | None = None,
    *,
    python: str = "python3",
) -> VerifyCommand | None:
    """Find the verify command for ``repo_dir``, or ``None`` if nothing is found.

    ``override`` is the repo's saved command from ``repo_settings`` (blank means
    none). ``python`` is the interpreter used for ``python -m pytest`` in a Python
    repo that has no ``uv.lock`` / ``poetry.lock``.
    """
    if override and override.strip():
        return VerifyCommand.from_override(override.strip())
    repo = Path(repo_dir)
    names = _entries(repo)
    return (
        _detect_make(repo, names)
        or _detect_npm(repo, names)
        or _detect_python(repo, names, python)
        or _detect_swift(names)
    )


def detect_subset(
    repo_dir: Path | str,
    test_paths: Sequence[str],
    *,
    python: str = "python3",
) -> VerifyCommand | None:
    """A command that runs only ``test_paths`` (relative to the repo), or ``None``.

    Used by the reviewer's revert check. It ignores the override and the Makefile
    and goes straight to the test runner: pytest for ``.py`` paths in a Python
    repo; ``<npm|pnpm|yarn> test -- <paths>`` for JS/TS paths when the ``test``
    script uses jest or vitest. Swift (``--filter`` takes test names, not paths)
    and anything else return ``None``.
    """
    repo = Path(repo_dir)
    names = _entries(repo)
    py_paths = [p for p in test_paths if p.endswith(".py")]
    js_paths = [p for p in test_paths if p.endswith(JS_TEST_SUFFIXES)]
    if py_paths:
        base = _detect_python(repo, names, python)
        if base is not None:
            return VerifyCommand.of([*base.argv, *py_paths], "python")
    if js_paths:
        script = _npm_test_script(repo, names)
        if script is not None and re.search(r"\b(jest|vitest)\b", script):
            return VerifyCommand.of([*_js_runner(names), "test", "--", *js_paths], "package.json")
    return None


def _entries(repo: Path) -> set[str]:
    """Exact-case names in ``repo`` (macOS is case-insensitive: ``Tests`` ≠ ``tests``)."""
    try:
        return set(os.listdir(repo))
    except OSError:
        return set()


def _detect_make(repo: Path, names: set[str]) -> VerifyCommand | None:
    for name in MAKEFILE_NAMES:
        if name not in names:
            continue
        targets = makefile_targets((repo / name).read_text(errors="replace"))
        for target in MAKE_TARGETS:
            if target in targets:
                return VerifyCommand.of(["make", target], "makefile")
        return None  # make reads only the first of these names
    return None


def makefile_targets(text: str) -> set[str]:
    """Explicit rule targets in a Makefile (no variable assignments, no recipes)."""
    targets: set[str] = set()
    for line in text.splitlines():
        if not line or line[0] in "\t #":
            continue
        m = re.match(r"([^:#=]+?)\s*::?(?!=)", line)
        if m and "=" not in m.group(1):
            targets.update(m.group(1).split())
    return targets


def _npm_test_script(repo: Path, names: set[str]) -> str | None:
    if "package.json" not in names:
        return None
    try:
        data = json.loads((repo / "package.json").read_text())
    except (OSError, ValueError):
        return None
    scripts = data.get("scripts") if isinstance(data, dict) else None
    script = scripts.get("test") if isinstance(scripts, dict) else None
    if not isinstance(script, str) or not script.strip() or NPM_DEFAULT_TEST in script:
        return None
    return script


def _js_runner(names: set[str]) -> list[str]:
    if "pnpm-lock.yaml" in names:
        return ["pnpm"]
    if "yarn.lock" in names:
        return ["yarn"]
    return ["npm"]


def _detect_npm(repo: Path, names: set[str]) -> VerifyCommand | None:
    if _npm_test_script(repo, names) is None:
        return None
    return VerifyCommand.of([*_js_runner(names), "test"], "package.json")


def _has_python_tests(tests_dir: Path) -> bool:
    try:
        return tests_dir.is_dir() and any(tests_dir.rglob("*.py"))
    except OSError:
        return False


def _detect_python(repo: Path, names: set[str], python: str) -> VerifyCommand | None:
    is_python = (
        "pyproject.toml" in names
        or "pytest.ini" in names
        or ("tests" in names and _has_python_tests(repo / "tests"))
    )
    if not is_python:
        return None
    if "uv.lock" in names:
        return VerifyCommand.of(["uv", "run", "pytest"], "python")
    if "poetry.lock" in names:
        return VerifyCommand.of(["poetry", "run", "pytest"], "python")
    return VerifyCommand.of([python, "-m", "pytest"], "python")


def _detect_swift(names: set[str]) -> VerifyCommand | None:
    if "Package.swift" in names:
        return VerifyCommand.of(["swift", "test"], "swift")
    return None


# --------------------------------------------------------------------------- running


def run(
    cmd: VerifyCommand,
    cwd: Path | str,
    timeout: float = DEFAULT_TIMEOUT_S,
    output_path: Path | str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    cancel: threading.Event | None = None,
) -> VerifyResult:
    """Run ``cmd`` in ``cwd`` with stdout+stderr saved to ``output_path``.

    ``output_path`` defaults to ``<cwd>/.fman-verify.log`` but callers should pass a
    path next to the trace. ``env`` entries are added to the inherited
    environment (``CI=1`` and colour-off variables are always set). Never raises
    for a failing, missing or hanging command. ``cancel`` (the worker shutting down):
    once set, the command's process group is killed and the result is a failure.
    """
    cwd = Path(cwd)
    out = Path(output_path) if output_path is not None else cwd / ".fman-verify.log"
    out.parent.mkdir(parents=True, exist_ok=True)
    full_env = _child_env(env)
    start = time.monotonic()
    exit_code: int | None
    timed_out = False
    with out.open("wb") as fh:
        fh.write(f"$ {cmd.display}\n".encode())
        fh.flush()
        try:
            proc = subprocess.Popen(
                cmd.argv,
                cwd=cwd,
                env=full_env,
                stdin=subprocess.DEVNULL,
                stdout=fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            fh.write(f"foreman-inator: could not start verify command: {exc}\n".encode())
            exit_code = 127
        else:
            try:
                exit_code = _wait(proc, timeout, cancel)
            except _Cancelled:
                exit_code = None
                _kill_group(proc)
                fh.write(b"\nforeman-inator: verify cancelled\n")
            except subprocess.TimeoutExpired:
                timed_out = True
                exit_code = None
                _kill_group(proc)
                fh.write(f"\nforeman-inator: verify timed out after {timeout:g}s\n".encode())
            else:
                _signal_group(proc.pid, signal.SIGKILL)  # reap stray background children
    seconds = time.monotonic() - start
    tests_run, tests_failed = parse_counts(_read_tail(out, PARSE_TAIL_BYTES))
    return VerifyResult(
        passed=exit_code == 0 and not timed_out,
        tests_run=tests_run,
        tests_failed=tests_failed,
        output_path=out,
        exit_code=exit_code,
        timed_out=timed_out,
        seconds=seconds,
        command=cmd.display,
    )


class _Cancelled(Exception):
    pass


def _wait(proc: subprocess.Popen[bytes], timeout: float, cancel: threading.Event | None) -> int:
    """``proc.wait(timeout)``, polling ``cancel`` twice a second when one is given."""
    if cancel is None:
        return proc.wait(timeout=timeout)
    deadline = time.monotonic() + timeout
    while True:
        if cancel.is_set():
            raise _Cancelled
        left = deadline - time.monotonic()
        if left <= 0:
            raise subprocess.TimeoutExpired(proc.args, timeout)
        try:
            return proc.wait(timeout=min(0.5, left))
        except subprocess.TimeoutExpired:
            continue


def _child_env(extra: Mapping[str, str] | None) -> dict[str, str]:
    env = dict(os.environ)
    env.update({"CI": "1", "NO_COLOR": "1", "FORCE_COLOR": "0", "TERM": "dumb"})
    if extra:
        env.update(extra)
    return env


def _signal_group(pgid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    _signal_group(proc.pid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=KILL_GRACE_S)
    _signal_group(proc.pid, signal.SIGKILL)
    proc.wait()


def _read_tail(path: Path, max_bytes: int) -> str:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


# --------------------------------------------------------------------------- parsing

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_PYTEST = re.compile(
    r"^[=\s]*((?:\d+ [a-z]+(?:, )?)+|no tests ran)\s+in\s+\d+(?:\.\d+)?s\b", re.MULTILINE
)
_JEST = re.compile(r"^Tests:\s+(.*?\d+ total)\s*$", re.MULTILINE)
_VITEST = re.compile(r"^\s*Tests\s+(.*?)\((\d+)\)\s*$", re.MULTILINE)
_XCTEST = re.compile(r"Executed (\d+) tests?, with (\d+) failures?")
_SWIFT_TESTING = re.compile(
    r"Test run with (\d+) tests?(?: in \d+ suites?)? (passed|failed)"
    r"(?: after [\d.]+ seconds)?(?: with (\d+) issues?)?"
)
_UNITTEST = re.compile(r"^Ran (\d+) tests? in [\d.]+s\s*\n\s*\n(OK|FAILED)([^\n]*)", re.MULTILINE)


def _named_counts(text: str) -> dict[str, int]:
    return {word: int(n) for n, word in re.findall(r"(\d+) ([a-z]+)", text)}


def _pytest(m: re.Match[str]) -> tuple[int, int]:
    c = _named_counts(m.group(1))
    failed = c.get("failed", 0) + c.get("error", 0) + c.get("errors", 0)
    run = c.get("passed", 0) + failed + c.get("xfailed", 0) + c.get("xpassed", 0)
    return run, failed


def _jest(m: re.Match[str]) -> tuple[int, int]:
    c = _named_counts(m.group(1))
    return c.get("passed", 0) + c.get("failed", 0), c.get("failed", 0)


def _xctest(m: re.Match[str]) -> tuple[int, int]:
    return int(m.group(1)), int(m.group(2))


def _swift_testing(m: re.Match[str]) -> tuple[int, int]:
    total = int(m.group(1))
    if m.group(2) == "passed":
        return total, 0
    return total, min(total, int(m.group(3) or 1))


def _unittest(m: re.Match[str]) -> tuple[int, int]:
    c = {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", m.group(3))}
    return int(m.group(1)) - c.get("skipped", 0), c.get("failures", 0) + c.get("errors", 0)


_PARSERS = (
    (_PYTEST, _pytest),
    (_JEST, _jest),
    (_VITEST, _jest),
    (_XCTEST, _xctest),
    (_SWIFT_TESTING, _swift_testing),
    (_UNITTEST, _unittest),
)


def parse_counts(output: str) -> tuple[int | None, int | None]:
    """``(tests_run, tests_failed)`` from the last test summary in ``output``.

    Recognises pytest, jest, vitest, XCTest, Swift Testing and unittest summaries;
    the summary that appears last wins (a Makefile may run several suites, and
    XCTest prints a summary per suite before the overall one), except that an
    empty summary (0 run, 0 failed) never hides a non-empty one: ``swift test``
    prints Swift Testing's "0 tests" line after XCTest's real counts.
    ``(None, None)`` when nothing matches.
    """
    text = _ANSI.sub("", output)
    found: list[tuple[int, tuple[int, int]]] = []
    for pattern, convert in _PARSERS:
        found.extend((m.start(), convert(m)) for m in pattern.finditer(text))
    if not found:
        return None, None
    found.sort(key=lambda item: item[0])
    non_empty = [counts for _, counts in found if counts != (0, 0)]
    return non_empty[-1] if non_empty else found[-1][1]
