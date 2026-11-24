"""Tests for verify command detection and the verify runner (todo #6)."""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from foreman_inator.runner import verify
from foreman_inator.runner.verify import VerifyCommand, detect, detect_subset, parse_counts, run

FIXTURES = Path(__file__).parent / "fixtures" / "repos"
PY = sys.executable


def copy_fixture(name: str, tmp_path: Path) -> Path:
    """Copy a fixture repo into tmp_path so no caches or build output land in the repo."""
    dest = tmp_path / name
    shutil.copytree(FIXTURES / name, dest)
    return dest


# --------------------------------------------------------------------------- detection


@pytest.mark.parametrize(
    ("fixture", "argv", "source"),
    [
        ("makefile", ("make", "test"), "makefile"),
        ("node", ("npm", "test"), "package.json"),
        ("python", (PY, "-m", "pytest"), "python"),
        ("python_failing", (PY, "-m", "pytest"), "python"),
        ("swift", ("swift", "test"), "swift"),
    ],
)
def test_each_fixture_detects_its_command(fixture, argv, source, tmp_path):
    cmd = detect(copy_fixture(fixture, tmp_path), python=PY)
    assert cmd is not None
    assert cmd.argv == argv
    assert cmd.source == source


def test_empty_repo_detects_nothing(tmp_path):
    assert detect(copy_fixture("empty", tmp_path)) is None


def test_missing_dir_detects_nothing(tmp_path):
    assert detect(tmp_path / "nope") is None


def test_fixtures_have_no_nested_git():
    assert not list(FIXTURES.rglob(".git"))


def test_override_wins_over_detection(tmp_path):
    cmd = detect(copy_fixture("makefile", tmp_path), override="  ./run-tests.sh --fast ")
    assert cmd == VerifyCommand(
        ("/bin/sh", "-c", "./run-tests.sh --fast"), "override", "./run-tests.sh --fast"
    )


def test_blank_override_is_ignored(tmp_path):
    cmd = detect(copy_fixture("makefile", tmp_path), override="   ")
    assert cmd is not None and cmd.source == "makefile"


def test_override_on_empty_repo(tmp_path):
    cmd = detect(copy_fixture("empty", tmp_path), override="make ci")
    assert cmd is not None and cmd.display == "make ci"


def test_detection_order_makefile_then_npm_then_python_then_swift(tmp_path):
    repo = tmp_path / "mixed"
    repo.mkdir()
    (repo / "Package.swift").write_text("// swift\n")
    assert detect(repo).source == "swift"
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert detect(repo).source == "python"
    (repo / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
    assert detect(repo).source == "package.json"
    (repo / "Makefile").write_text("test:\n\techo hi\n")
    assert detect(repo).source == "makefile"


def test_makefile_check_target_and_fallthrough(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "Makefile").write_text("build:\n\tcc x.c\n")
    (repo / "pytest.ini").write_text("[pytest]\n")
    assert detect(repo, python="py").argv == ("py", "-m", "pytest")
    (repo / "Makefile").write_text("lint check: build\n\truff .\nbuild:\n\tcc\n")
    assert detect(repo).argv == ("make", "check")
    (repo / "Makefile").write_text("check:\n\ta\ntest:\n\tb\n")
    assert detect(repo).argv == ("make", "test")


def test_makefile_targets_skip_assignments_and_recipes():
    text = (
        "TEST := pytest\n"
        "check ?= no\n"
        "test=1\n"
        "# test: commented\n"
        "\ttest: inside a recipe\n"
        "all:: build\n"
        "build: dep.o\n"
    )
    assert verify.makefile_targets(text) == {"all", "build"}


def test_npm_default_test_script_is_not_a_test(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    script = 'echo "Error: no test specified" && exit 1'
    (repo / "package.json").write_text(json.dumps({"scripts": {"test": script}}))
    assert detect(repo) is None
    (repo / "package.json").write_text("{not json")
    assert detect(repo) is None


@pytest.mark.parametrize(("lock", "runner"), [("yarn.lock", "yarn"), ("pnpm-lock.yaml", "pnpm")])
def test_js_lockfile_picks_runner(lock, runner, tmp_path):
    repo = copy_fixture("node", tmp_path)
    (repo / lock).write_text("")
    assert detect(repo).argv == (runner, "test")


@pytest.mark.parametrize(
    ("lock", "argv"),
    [("uv.lock", ("uv", "run", "pytest")), ("poetry.lock", ("poetry", "run", "pytest"))],
)
def test_python_lockfile_uses_project_env(lock, argv, tmp_path):
    repo = copy_fixture("python", tmp_path)
    (repo / lock).write_text("")
    assert detect(repo).argv == argv


def test_tests_dir_needs_python_files(tmp_path):
    repo = tmp_path / "r"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "a.sh").write_text("true\n")
    assert detect(repo) is None
    (repo / "tests" / "sub").mkdir()
    (repo / "tests" / "sub" / "test_a.py").write_text("def test_a(): pass\n")
    assert detect(repo).source == "python"


def test_swift_capital_tests_dir_is_not_python(tmp_path):
    # macOS is case-insensitive: Tests/ must not count as tests/.
    repo = copy_fixture("swift", tmp_path)
    (repo / "Tests" / "helper.py").write_text("")
    assert detect(repo).source == "swift"


# --------------------------------------------------------------------------- running


def test_passing_python_fixture(tmp_path):
    repo = copy_fixture("python", tmp_path)
    out = tmp_path / "trace" / "verify.log"
    result = run(detect(repo, python=PY), repo, timeout=60, output_path=out)
    assert result.passed is True
    assert result.exit_code == 0
    assert (result.tests_run, result.tests_failed) == (3, 0)
    assert result.output_path == out
    assert "3 passed" in out.read_text()


def test_failing_fixture_is_not_passed_and_output_is_saved(tmp_path):
    repo = copy_fixture("python_failing", tmp_path)
    out = tmp_path / "attach" / "verify-a1.log"
    result = run(detect(repo, python=PY), repo, timeout=60, output_path=out)
    assert result.passed is False
    assert result.exit_code not in (0, None)
    assert result.timed_out is False
    assert (result.tests_run, result.tests_failed) == (2, 1)
    saved = out.read_text()
    assert "deliberately failing fixture test" in saved
    assert "1 failed, 1 passed" in saved
    assert "deliberately failing" in result.output_tail(4000)


def test_makefile_fixture_runs(tmp_path):
    repo = copy_fixture("makefile", tmp_path)
    result = run(detect(repo), repo, timeout=60, output_path=tmp_path / "out.log")
    assert result.passed is True
    assert "make test ran" in result.output_path.read_text()
    assert (result.tests_run, result.tests_failed) == (None, None)


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm not on PATH")
def test_node_fixture_runs(tmp_path):
    repo = copy_fixture("node", tmp_path)
    result = run(detect(repo), repo, timeout=60, output_path=tmp_path / "out.log")
    assert result.passed is True, result.output_path.read_text()
    assert (result.tests_run, result.tests_failed) == (1, 0)


@pytest.mark.timeout(600)
@pytest.mark.skipif(shutil.which("swift") is None, reason="swift not on PATH")
def test_swift_fixture_runs(tmp_path):
    repo = copy_fixture("swift", tmp_path)
    result = run(detect(repo), repo, timeout=540, output_path=tmp_path / "out.log")
    log = result.output_path.read_text()
    if not result.passed and "no such module 'XCTest'" in log:
        pytest.skip("XCTest not available in this toolchain")
    assert result.passed is True, log[-3000:]
    assert (result.tests_run, result.tests_failed) == (1, 0)


def test_nonzero_exit_never_raises(tmp_path):
    result = run(VerifyCommand.from_override("echo boom; exit 3"), tmp_path, 30, tmp_path / "o.log")
    assert (result.passed, result.exit_code, result.timed_out) == (False, 3, False)
    assert "boom" in result.output_path.read_text()


def test_missing_executable_never_raises(tmp_path):
    cmd = VerifyCommand.of(["definitely-not-a-real-tool-xyz", "test"], "python")
    result = run(cmd, tmp_path, 30, tmp_path / "o.log")
    assert result.passed is False
    assert result.exit_code == 127
    assert "could not start" in result.output_path.read_text()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_timeout_kills_the_whole_process_group(tmp_path):
    pidfile = tmp_path / "child.pid"
    cmd = VerifyCommand.from_override(f"sleep 120 & echo $! > {pidfile}; echo started; wait")
    started = time.monotonic()
    result = run(cmd, tmp_path, timeout=1, output_path=tmp_path / "o.log")
    assert time.monotonic() - started < 15
    assert result.passed is False
    assert result.timed_out is True
    assert result.exit_code is None
    log = result.output_path.read_text()
    assert "started" in log and "timed out after 1s" in log
    child = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while _alive(child) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(child), "background child of the verify command survived the timeout"


def test_env_is_passed_and_ci_set(tmp_path):
    cmd = VerifyCommand.from_override('echo "ci=$CI extra=$FMAN_EXTRA"')
    result = run(cmd, tmp_path, 30, tmp_path / "o.log", env={"FMAN_EXTRA": "yes"})
    assert "ci=1 extra=yes" in result.output_path.read_text()


# --------------------------------------------------------------------------- subsets


def test_subset_runs_only_the_given_pytest_files(tmp_path):
    repo = copy_fixture("python", tmp_path)
    cmd = detect_subset(repo, ["tests/test_other.py", "README.md"], python=PY)
    assert cmd is not None
    assert cmd.argv == (PY, "-m", "pytest", "tests/test_other.py")
    result = run(cmd, repo, 60, tmp_path / "o.log")
    assert result.passed is True
    assert (result.tests_run, result.tests_failed) == (1, 0)


def test_subset_for_jest_and_unsupported_ecosystems(tmp_path):
    repo = tmp_path / "js"
    repo.mkdir()
    (repo / "package.json").write_text(json.dumps({"scripts": {"test": "jest --ci"}}))
    assert detect_subset(repo, ["src/a.test.ts"]).argv == ("npm", "test", "--", "src/a.test.ts")
    assert detect_subset(copy_fixture("node", tmp_path), ["test.js"]) is None  # not jest/vitest
    assert detect_subset(copy_fixture("swift", tmp_path), ["Tests/X.swift"]) is None
    assert detect_subset(copy_fixture("makefile", tmp_path), ["tests/test_a.py"]) is None
    assert detect_subset(copy_fixture("python", tmp_path), []) is None


# --------------------------------------------------------------------------- parsing

JEST_SAMPLE = """\
> fixture@1.0.0 test
> jest

 FAIL  src/sum.test.js
  ● sum › adds negatives

    expect(received).toBe(expected) // Object.is equality

 PASS  src/other.test.js

Test Suites: 1 failed, 1 passed, 2 total
Tests:       1 failed, 1 skipped, 4 passed, 6 total
Snapshots:   0 total
Time:        0.512 s
Ran all test suites.
npm ERR! Test failed.  See above for more details.
"""

VITEST_SAMPLE = """\
 \x1b[31m❯\x1b[39m src/sum.test.ts (3)
 Test Files  1 failed | 1 passed (2)
      Tests  2 failed | 5 passed (7)
   Start at  10:00:00
"""

SWIFT_SAMPLE = """\
Test Suite 'FixtureTests' failed at 2026-11-23 13:49:12.530.
\t Executed 3 tests, with 1 failure (0 unexpected) in 0.001 (0.001) seconds
Test Suite 'All tests' failed at 2026-11-23 13:49:12.530.
\t Executed 4 tests, with 2 failures (0 unexpected) in 0.001 (0.003) seconds
✔ Test run with 0 tests in 0 suites passed after 0.001 seconds.
"""

UNITTEST_SAMPLE = """\
..F.s
----------------------------------------------------------------------
Ran 5 tests in 0.003s

FAILED (failures=1, skipped=1)
"""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (JEST_SAMPLE, (5, 1)),
        (VITEST_SAMPLE, (7, 2)),
        (SWIFT_SAMPLE, (4, 2)),
        ("✘ Test run with 5 tests in 2 suites failed after 0.01 seconds with 2 issues.", (5, 2)),
        (UNITTEST_SAMPLE, (4, 1)),
        ("==== 1 failed, 2 passed, 1 skipped, 3 warnings in 0.12s ====", (3, 1)),
        ("\x1b[32m==== 4 passed in 1.02s ====\x1b[0m", (4, 0)),
        ("1 passed, 1 error, 1 xfailed in 0.40s", (3, 1)),
        ("===== no tests ran in 0.01s =====", (0, 0)),
        ("make: Nothing to be done for `test'.", (None, None)),
    ],
)
def test_parse_counts(text, expected):
    assert parse_counts(text) == expected


def test_parse_counts_last_summary_wins():
    text = "==== 3 passed in 0.1s ====\n" + JEST_SAMPLE
    assert parse_counts(text) == (5, 1)
