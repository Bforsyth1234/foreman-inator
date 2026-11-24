"""The AI reviewer's checks 1-4 (todo #17): plain code, no model calls.

SPEC "AI review", cheapest and most objective first:

1. ``check_tests``: rerun the repo's verify command in a scratch worktree at the change.
2. ``check_coverage``: changed-line coverage for Python via the TARGET repo's ``coverage``
   (never this project's); ``percent=None`` (unknown) when it is not available. New
   functions none of whose lines any test executes are listed in ``uncovered_functions``.
3. ``check_revert``: in a scratch worktree at the change, restore every non-test file to
   the base version, run only the tests the change added or modified
   (``verify.detect_subset``), and expect them to fail. Still passing means the tests are
   vacuous (``nonvacuous=False``). No changed tests, nothing to revert, or no way to run a
   subset in this ecosystem gives ``nonvacuous=None`` (unknown).
4. ``check_run_app``: run the change on its own when a safe, detectable entry point exists
   (a Python ``[project.scripts]`` entry or ``python -m <pkg>`` with ``--help``; an npm
   ``start`` script as a smoke test), with a short timeout; otherwise ``not_runnable``.
   A failure is compared with the same command at the base, so pre-existing breakage is
   not charged to the change.

Each check returns a small frozen result with ``status`` (``passed`` / ``failed`` /
``unknown`` / ``skipped`` / ``timed_out``) and ``summary()`` (one or two lines for the
model input, the PR comment and the span). The pure helpers (``is_test_path``,
``changed_lines``, ``coverage_from_report``, ``hard_findings``) work on recorded data, so
#28's regression tests can replay the decisions without running anything.

``hard_findings`` is the verdict policy for checks 1-4; see its docstring. The model in
check 5 cannot overrule a hard finding.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Literal

from foreman_inator.runner import verify
from foreman_inator.runner.verify import VerifyCommand, VerifyResult
from foreman_inator.runner.worktree import _git

Status = Literal["passed", "failed", "unknown", "skipped", "timed_out"]
RanApp = Literal["yes", "no", "not_runnable"]

OUTPUT_TAIL_CHARS = 1_500
"""Characters of command output kept in a check result (the end says what failed)."""

RUN_APP_TIMEOUT_S = 30.0
"""``--help`` style runs must finish within this; ``npm start`` counts as up after it."""

START_SMOKE_S = 8.0
"""An ``npm start`` still running after this many seconds counts as started."""

_TEST_DIRS = ("tests", "test", "__tests__", "spec", "testing")
_TEST_FILE = re.compile(
    r"(^test_.*\.py$|.*_test\.py$|^conftest\.py$|.*\.(test|spec)\.[cm]?[jt]sx?$|.*Tests?\.swift$)"
)
_RUNNABLE_TEST = re.compile(r"(^test_.*\.py$|.*_test\.py$|.*\.(test|spec)\.[cm]?[jt]sx?$)")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


# --------------------------------------------------------------------------- results


@dataclass(frozen=True)
class TestsCheck:
    """Check 1. ``passed`` is None when no verify command exists or the run timed out."""

    status: Status
    passed: bool | None
    command: str | None = None
    tests_run: int | None = None
    tests_failed: int | None = None
    output_tail: str = ""
    reason: str = ""

    def summary(self) -> str:
        if self.status == "passed":
            counts = f" ({self.tests_run} tests)" if self.tests_run is not None else ""
            return f"Tests and build: passed{counts} with `{self.command}`."
        if self.status == "failed":
            n = f" ({self.tests_failed} failing)" if self.tests_failed else ""
            return f"Tests and build: FAILED{n} with `{self.command}`."
        return f"Tests and build: {self.status} ({self.reason})."


@dataclass(frozen=True)
class CoverageCheck:
    """Check 2. ``percent`` of changed executable lines run by tests, None = unknown."""

    status: Status
    percent: float | None
    measured_lines: int = 0
    covered_lines: int = 0
    uncovered: Mapping[str, tuple[int, ...]] = field(default_factory=dict)  # path -> lines
    uncovered_functions: tuple[str, ...] = ()  # "path:function", new and never executed
    reason: str = ""

    def summary(self) -> str:
        if self.percent is None:
            return f"Changed-line coverage: unknown ({self.reason})."
        text = (
            f"Changed-line coverage: {self.percent:.0f}% "
            f"({self.covered_lines} of {self.measured_lines} changed executable lines run)."
        )
        if self.uncovered_functions:
            text += " New code no test executes: " + ", ".join(self.uncovered_functions) + "."
        elif self.uncovered:
            spots = [f"{p}:{_ranges(ls)}" for p, ls in sorted(self.uncovered.items())]
            text += " Unexecuted changed lines: " + "; ".join(spots[:10]) + "."
        return text


@dataclass(frozen=True)
class RevertCheck:
    """Check 3. ``nonvacuous`` True = the changed tests fail without the source change."""

    status: Status
    nonvacuous: bool | None
    tests: tuple[str, ...] = ()
    reverted: tuple[str, ...] = ()
    command: str | None = None
    output_tail: str = ""
    reason: str = ""

    def summary(self) -> str:
        if self.nonvacuous is True:
            return "Revert check: the changed tests fail when the source change is reverted (good)."
        if self.nonvacuous is False:
            return (
                "Revert check: FAILED. The changed tests still pass with the source change "
                f"reverted, so they do not test it ({', '.join(self.tests)})."
            )
        return f"Revert check: unknown ({self.reason})."


@dataclass(frozen=True)
class RunAppCheck:
    """Check 4. ``regression`` True = it fails at the change but works at the base."""

    status: Status
    ran_app: RanApp
    command: str | None = None
    regression: bool = False
    output_tail: str = ""
    reason: str = ""

    def summary(self) -> str:
        if self.ran_app == "not_runnable":
            return f"Run the change: not runnable ({self.reason})."
        if self.ran_app == "yes":
            return f"Run the change: `{self.command}` ran fine."
        if self.regression:
            return f"Run the change: FAILED. `{self.command}` fails, and works at the base."
        return (
            f"Run the change: `{self.command}` fails, but it fails at the base too ({self.reason})."
        )


@dataclass(frozen=True)
class HardFinding:
    """A check result that forces ``request_changes`` with ``tag``; the model can't overrule it."""

    check: str  # tracing.REVIEW_CHECKS name
    tag: str  # store TAGS
    note: str


# --------------------------------------------------------------------------- pure helpers


def is_test_path(path: str) -> bool:
    """Test code or test support (kept at the change by the revert check)."""
    parts = Path(path).parts
    if any(p in _TEST_DIRS for p in parts[:-1]):
        return True
    return bool(_TEST_FILE.match(parts[-1])) if parts else False


def is_runnable_test(path: str) -> bool:
    """A test file worth passing to the test runner (not a helper or conftest)."""
    return bool(_RUNNABLE_TEST.match(Path(path).name))


def changed_lines(diff_text: str) -> dict[str, set[int]]:
    """Added or modified line numbers (in the new file) per path, from a unified diff."""
    out: dict[str, set[int]] = {}
    path: str | None = None
    line = 0
    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            path = None if target == "/dev/null" else target.removeprefix("b/")
            if path is not None:
                out.setdefault(path, set())
            continue
        if raw.startswith("--- ") or raw.startswith("diff --git"):
            continue
        m = _HUNK.match(raw)
        if m:
            line = int(m.group(1))
            continue
        if path is None or not raw:
            continue
        if raw[0] == "+":
            out[path].add(line)
            line += 1
        elif raw[0] == " ":
            line += 1
    return {p: ls for p, ls in out.items() if ls}


def new_functions(source: str, added: set[int]) -> list[tuple[str, set[int]]]:
    """``(qualified name, body lines)`` of functions whose ``def`` line was added."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    found: list[tuple[str, set[int]]] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = f"{prefix}{child.name}"
                if child.lineno in added and child.body:
                    first = child.body[0].lineno
                    end = child.end_lineno or first
                    found.append((name, set(range(first, end + 1))))
                walk(child, name + ".")
            elif isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")

    walk(tree, "")
    return found


def coverage_from_report(
    report: Mapping[str, object],
    changed: Mapping[str, set[int]],
    sources: Mapping[str, str],
) -> CoverageCheck:
    """Changed-line coverage from a ``coverage json`` report (paths relative to the repo).

    ``changed`` holds the changed lines of the non-test Python files; ``sources`` their text
    at the change, for finding new functions. Files the report does not list are skipped
    (their executable lines are unknown)."""
    files = report.get("files")
    if not isinstance(files, Mapping):
        return CoverageCheck("unknown", None, reason="coverage report has no files")
    measured = covered = 0
    uncovered: dict[str, tuple[int, ...]] = {}
    functions: list[str] = []
    for path, lines in sorted(changed.items()):
        data = files.get(path)
        if not isinstance(data, Mapping):
            continue
        executed = set(data.get("executed_lines") or ())
        missing = set(data.get("missing_lines") or ())
        executable = (executed | missing) & lines
        hit = executable & executed
        measured += len(executable)
        covered += len(hit)
        if executable - hit:
            uncovered[path] = tuple(sorted(executable - hit))
        for name, body in new_functions(sources.get(path, ""), lines):
            body_exec = body & (executed | missing)
            if body_exec and not body_exec & executed:
                functions.append(f"{path}:{name}")
    if measured == 0:
        return CoverageCheck("unknown", None, reason="no changed executable Python lines measured")
    pct = 100.0 * covered / measured
    status: Status = "failed" if functions else "passed"
    return CoverageCheck(
        status,
        round(pct, 1),
        measured_lines=measured,
        covered_lines=covered,
        uncovered=uncovered,
        uncovered_functions=tuple(functions),
    )


def hard_findings(
    tests: TestsCheck | None,
    coverage: CoverageCheck | None,
    revert: RevertCheck | None,
    run_app: RunAppCheck | None,
) -> list[HardFinding]:
    """The verdict policy for checks 1-4 (assistant decision for #17, 2026-11-23).

    Each of these forces ``request_changes`` and the model call is skipped:

    * check 1 failed (tests or build fail in the reviewer's own run) -> tag ``broke``;
    * check 2 found a new function none of whose lines any test executes -> ``incomplete``;
    * check 3 found the changed tests still pass with the source change reverted
      (``tests_nonvacuous=False``) -> ``incomplete``;
    * check 4 found the change fails to run where the base runs (a regression) -> ``broke``.

    Everything else is advisory and goes to the model in check 5: coverage below 100%,
    unknown results, an app that fails at the base too, ``not_runnable``. The first
    finding's tag is the review's tag; every finding goes into the note. A timed-out
    check 1 is not a finding: the review cannot complete and is handed to the user.
    """
    out: list[HardFinding] = []
    if tests is not None and tests.status == "failed":
        out.append(
            HardFinding(
                "tests",
                "broke",
                f"The tests or build fail in the reviewer's run of `{tests.command}`."
                + (f"\n{_last_lines(tests.output_tail, 15)}" if tests.output_tail else ""),
            )
        )
    if run_app is not None and run_app.regression:
        out.append(
            HardFinding(
                "run_app",
                "broke",
                f"`{run_app.command}` fails at this change but works at the base commit."
                + (f"\n{_last_lines(run_app.output_tail, 10)}" if run_app.output_tail else ""),
            )
        )
    if revert is not None and revert.nonvacuous is False:
        out.append(
            HardFinding(
                "test_revert",
                "incomplete",
                "The changed tests still pass when the source change is reverted, so they do "
                f"not test it: {', '.join(revert.tests)}. Add tests that fail without the fix.",
            )
        )
    if coverage is not None and coverage.uncovered_functions:
        out.append(
            HardFinding(
                "changed_line_coverage",
                "incomplete",
                "New code that no test executes: "
                + ", ".join(coverage.uncovered_functions)
                + ". Add tests that call it.",
            )
        )
    return out


# --------------------------------------------------------------------------- check 1


def check_tests(
    worktree: Path,
    command: VerifyCommand | None,
    *,
    timeout: float,
    output_path: Path,
    cancel: Event | None = None,
) -> TestsCheck:
    """Check 1: run ``command`` (from ``verify.detect``) in ``worktree``."""
    if command is None:
        return TestsCheck("unknown", None, reason="no verify command for this repo")
    res = verify.run(command, worktree, timeout=timeout, output_path=output_path, cancel=cancel)
    tail = res.output_tail(OUTPUT_TAIL_CHARS)
    if res.timed_out or res.exit_code is None:
        why = f"timed out after {timeout:.0f}s" if res.timed_out else "cancelled"
        return TestsCheck("timed_out", None, res.command, output_tail=tail, reason=why)
    return TestsCheck(
        "passed" if res.passed else "failed",
        res.passed,
        res.command,
        res.tests_run,
        res.tests_failed,
        tail,
    )


# --------------------------------------------------------------------------- check 2


def _python_prefix(repo: Path, python: str) -> list[str] | None:
    """How to run a module in the target repo's environment, or None if not Python."""
    names = set(_listdir(repo))
    tests = repo / "tests"
    is_python = (
        "pyproject.toml" in names
        or "pytest.ini" in names
        or "setup.py" in names
        or ("tests" in names and tests.is_dir() and any(tests.rglob("*.py")))
    )
    if not is_python:
        return None
    if "uv.lock" in names:
        return ["uv", "run", "python"]
    if "poetry.lock" in names:
        return ["poetry", "run", "python"]
    return [python]


def check_coverage(
    worktree: Path,
    diff_text: str,
    *,
    timeout: float,
    work_dir: Path,
    python: str = "python3",
    cancel: Event | None = None,
) -> CoverageCheck:
    """Check 2: run pytest under the target repo's ``coverage`` and measure changed lines.

    Unknown (never a guess) when the repo is not Python, no Python source changed, or
    ``coverage`` is not installed in the target environment."""
    changed = {
        p: ls
        for p, ls in changed_lines(diff_text).items()
        if p.endswith(".py") and not is_test_path(p)
    }
    if not changed:
        return CoverageCheck("unknown", None, reason="no Python source lines changed")
    prefix = _python_prefix(worktree, python)
    if prefix is None:
        return CoverageCheck("unknown", None, reason="not a Python project")
    sources = {p: _read(worktree / p) for p in changed}
    dirs = sorted({str(Path(p).parent) for p in changed})
    work_dir.mkdir(parents=True, exist_ok=True)
    data_file = work_dir / ".coverage"
    report_file = work_dir / "coverage.json"
    env = {"COVERAGE_FILE": str(data_file)}
    run_cmd = VerifyCommand.of(
        [*prefix, "-m", "coverage", "run", f"--source={','.join(dirs)}", "-m", "pytest", "-q"],
        "coverage",
    )
    res = verify.run(
        run_cmd,
        worktree,
        timeout=timeout,
        output_path=work_dir / "coverage-run.log",
        env=env,
        cancel=cancel,
    )
    if res.timed_out:
        return CoverageCheck(
            "timed_out", None, reason=f"coverage run timed out after {timeout:.0f}s"
        )
    if not data_file.exists():
        return CoverageCheck(
            "unknown", None, reason="coverage is not available in the target repo's environment"
        )
    json_cmd = VerifyCommand.of(
        [*prefix, "-m", "coverage", "json", "-q", "-i", "-o", str(report_file)], "coverage"
    )
    verify.run(
        json_cmd,
        worktree,
        timeout=60,
        output_path=work_dir / "coverage-json.log",
        env=env,
        cancel=cancel,
    )
    try:
        report = json.loads(report_file.read_text())
    except (OSError, ValueError):
        return CoverageCheck("unknown", None, reason="coverage produced no readable report")
    return coverage_from_report(_relative_report(report, worktree), changed, sources)


def _relative_report(report: dict, worktree: Path) -> dict:
    files = report.get("files")
    if not isinstance(files, dict):
        return report
    root = worktree.resolve()
    out = {}
    for name, data in files.items():
        p = Path(name)
        if p.is_absolute():
            try:
                name = p.resolve().relative_to(root).as_posix()
            except ValueError:
                continue
        out[Path(name).as_posix()] = data
    return {**report, "files": out}


# --------------------------------------------------------------------------- check 3


def revert_sources(worktree: Path, base_sha: str, changed_files: Sequence[str]) -> list[str]:
    """Restore every non-test changed file in ``worktree`` to ``base_sha`` (delete the ones
    the change added). Returns the reverted paths. Only for scratch worktrees."""
    reverted: list[str] = []
    for path in changed_files:
        if is_test_path(path):
            continue
        exists = (
            _git(["cat-file", "-e", f"{base_sha}:{path}"], cwd=worktree, check=False).returncode
            == 0
        )
        if exists:
            _git(["checkout", base_sha, "--", path], cwd=worktree)
        else:
            target = worktree / path
            if target.is_file() or target.is_symlink():
                target.unlink()
        reverted.append(path)
    return reverted


def check_revert(
    worktree: Path,
    base_sha: str,
    changed_files: Sequence[str],
    *,
    timeout: float,
    output_path: Path,
    python: str = "python3",
    cancel: Event | None = None,
) -> RevertCheck:
    """Check 3 in ``worktree``, a scratch worktree at the change (it is modified)."""
    tests = tuple(p for p in changed_files if is_runnable_test(p) and (worktree / p).is_file())
    if not tests:
        return RevertCheck("unknown", None, reason="the change adds or modifies no tests")
    if not any(not is_test_path(p) for p in changed_files):
        return RevertCheck("unknown", None, tests, reason="the change touches only tests")
    command = verify.detect_subset(worktree, tests, python=python)
    if command is None:
        return RevertCheck(
            "unknown", None, tests, reason="cannot run single test files in this ecosystem"
        )
    reverted = tuple(revert_sources(worktree, base_sha, changed_files))
    res = verify.run(command, worktree, timeout=timeout, output_path=output_path, cancel=cancel)
    tail = res.output_tail(OUTPUT_TAIL_CHARS)
    if res.timed_out or res.exit_code is None:
        return RevertCheck(
            "timed_out", None, tests, reverted, res.command, tail, reason="the test run timed out"
        )
    if res.passed:
        return RevertCheck("failed", False, tests, reverted, res.command, tail)
    return RevertCheck("passed", True, tests, reverted, res.command, tail)


# --------------------------------------------------------------------------- check 4


@dataclass(frozen=True)
class EntryPoint:
    """How to run the change on its own. ``smoke``: alive after the window counts as up."""

    argv: tuple[str, ...]
    display: str
    smoke: bool = False


def detect_entry_point(repo: Path, *, python: str = "python3") -> EntryPoint | None:
    """A safe, detectable way to run the repo, or None (``not_runnable``).

    Python: the first ``[project.scripts]`` entry, called in-process with ``--help`` (no
    install needed; ``src/`` and the root are on ``sys.path``), else ``python -m <pkg>
    --help`` for a top-level package with ``__main__.py``. Node: ``<npm|pnpm|yarn> run
    start`` as a smoke test. Nothing else is run."""
    names = set(_listdir(repo))
    prefix = _python_prefix(repo, python)
    if prefix is not None:
        script = _first_script(repo / "pyproject.toml") if "pyproject.toml" in names else None
        if script is not None:
            name, target = script
            module, _, func = target.partition(":")
            if re.fullmatch(r"[\w.]+", module) and re.fullmatch(r"[\w.]*", func):
                code = _python_launcher(name, module, func)
                return EntryPoint((*prefix, "-c", code), f"{name} --help")
        for base in (repo / "src", repo):
            for main in sorted(base.glob("*/__main__.py")):
                pkg = main.parent.name
                if re.fullmatch(r"[A-Za-z_]\w*", pkg):
                    code = _python_launcher(pkg, pkg, "")
                    return EntryPoint((*prefix, "-c", code), f"python -m {pkg} --help")
        return None
    if "package.json" in names:
        try:
            data = json.loads((repo / "package.json").read_text())
        except (OSError, ValueError):
            return None
        scripts = data.get("scripts") if isinstance(data, dict) else None
        if isinstance(scripts, dict) and isinstance(scripts.get("start"), str):
            runner = (
                "pnpm" if "pnpm-lock.yaml" in names else "yarn" if "yarn.lock" in names else "npm"
            )
            return EntryPoint((runner, "run", "start"), f"{runner} run start", smoke=True)
    return None


def _python_launcher(argv0: str, module: str, func: str) -> str:
    """``python -c`` code running ``module:func`` (or ``python -m module``) with ``--help``,
    with ``src/`` and the repo root importable, so nothing has to be installed."""
    head = f"import sys; sys.path[:0] = ['src', '.']; sys.argv = [{argv0!r}, '--help']; "
    if not func:
        return head + f"import runpy; runpy.run_module({module!r}, run_name='__main__')"
    first, *rest = func.split(".")
    attr = "".join(f".{a}" for a in rest)
    return head + f"from {module} import {first} as _f; sys.exit(_f{attr}())"


def _first_script(pyproject: Path) -> tuple[str, str] | None:
    try:
        data = tomllib.loads(pyproject.read_text())
    except (OSError, ValueError):
        return None
    scripts = (data.get("project") or {}).get("scripts")
    if isinstance(scripts, dict):
        for name, target in scripts.items():
            if isinstance(name, str) and isinstance(target, str):
                return name, target.strip()
    return None


def _run_entry(entry: EntryPoint, cwd: Path, timeout: float, out: Path, cancel: Event | None):
    window = min(START_SMOKE_S, timeout) if entry.smoke else timeout
    cmd = VerifyCommand(entry.argv, "run_app", entry.display)
    res: VerifyResult = verify.run(cmd, cwd, timeout=window, output_path=out, cancel=cancel)
    ok = res.passed or (entry.smoke and res.timed_out)
    return ok, res


def check_run_app(
    head: Path,
    base: Path | None,
    *,
    timeout: float = RUN_APP_TIMEOUT_S,
    work_dir: Path,
    python: str = "python3",
    cancel: Event | None = None,
) -> RunAppCheck:
    """Check 4 at ``head``; on failure, the same entry point at ``base`` (a worktree with
    the base's source, or None when the change touched no source) decides regression."""
    entry = detect_entry_point(head, python=python)
    if entry is None:
        return RunAppCheck("skipped", "not_runnable", reason="no safe entry point detected")
    work_dir.mkdir(parents=True, exist_ok=True)
    ok, res = _run_entry(entry, head, timeout, work_dir / "run-app-head.log", cancel)
    tail = res.output_tail(OUTPUT_TAIL_CHARS)
    if ok:
        return RunAppCheck("passed", "yes", entry.display, output_tail=tail)
    if base is None:
        return RunAppCheck(
            "unknown", "no", entry.display, output_tail=tail, reason="no source change to compare"
        )
    base_entry = detect_entry_point(base, python=python)
    base_ok = False
    if base_entry is not None:
        base_ok, _ = _run_entry(base_entry, base, timeout, work_dir / "run-app-base.log", cancel)
    if base_ok:
        return RunAppCheck("failed", "no", entry.display, regression=True, output_tail=tail)
    return RunAppCheck(
        "unknown", "no", entry.display, output_tail=tail, reason="it also fails at the base"
    )


# --------------------------------------------------------------------------- utilities


def _listdir(path: Path) -> list[str]:
    try:
        return [p.name for p in path.iterdir()]
    except OSError:
        return []


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def _ranges(lines: Sequence[int]) -> str:
    out: list[str] = []
    start = prev = None
    for n in lines:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"{start}" if start == prev else f"{start}-{prev}")
            start = prev = n
    if start is not None:
        out.append(f"{start}" if start == prev else f"{start}-{prev}")
    return ",".join(out)


def _last_lines(text: str, n: int) -> str:
    return "\n".join(text.strip().splitlines()[-n:])


def scratch_work_dir(prefix: str = "fman-review-") -> Path:
    """A private temp folder outside any worktree for logs and coverage data."""
    return Path(tempfile.mkdtemp(prefix=prefix))


def remove_work_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
