"""Dependency install before the writer and verify (todo #45).

Detection, policy text, config, store and the worker mixin run everywhere; the
enforcement tests run on macOS with real ``sandbox-exec``, real ``uv`` and a local package
index served on loopback by the test (``tests/fakes/pkgindex.py``): never pypi.org, the
user's caches or ``~/.foreman-inator``. The index is on a random loopback port, so the
tests add that port to ``sandbox.INSTALL_PORTS`` (443/80 in production): the install
reaches it only through the install policy's port rule, and verify (network none) cannot.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from fakes.pkgindex import PackageIndex, build_index
from foreman_inator import config as config_mod
from foreman_inator.config import Config, ConfigError
from foreman_inator.db.store import Store
from foreman_inator.review import checks
from foreman_inator.runner import install, verify
from foreman_inator.runner import sandbox as sb
from foreman_inator.runner.sandbox import Policy, SeatbeltBackend
from foreman_inator.runner.worktree import GitIdentity, WorktreeManager
from foreman_inator.worker_install import InstallSteps
from foreman_inator.worker_sandbox import SandboxSteps

FIXTURES = Path(__file__).parent / "fixtures" / "repos"
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Seed",
    "GIT_AUTHOR_EMAIL": "seed@example.com",
    "GIT_COMMITTER_NAME": "Seed",
    "GIT_COMMITTER_EMAIL": "seed@example.com",
}
REPO = "o/r"

macos = pytest.mark.skipif(
    not SeatbeltBackend().available(), reason="sandbox-exec enforcement tests run on macOS only"
)
needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")


# --------------------------------------------------------------------------- detection


def _repo(tmp: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        p = tmp / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp


@pytest.mark.parametrize(
    ("files", "display", "source"),
    [
        ({"pyproject.toml": "", "uv.lock": ""}, "uv sync --locked", "uv"),
        ({"pyproject.toml": "", "poetry.lock": ""}, "poetry install --no-interaction", "poetry"),
        ({"package.json": "{}", "package-lock.json": "{}"}, "npm ci", "npm"),
        ({"package.json": "{}", "npm-shrinkwrap.json": "{}"}, "npm ci", "npm"),
        ({"package.json": "{}", "pnpm-lock.yaml": ""}, "pnpm install --frozen-lockfile", "pnpm"),
        ({"package.json": "{}", "yarn.lock": ""}, "yarn install --frozen-lockfile", "yarn"),
        ({"package.json": "{}", "yarn.lock": "", ".yarnrc.yml": ""}, "yarn install --immutable",
         "yarn"),
        ({"package.json": '{"devDependencies": {"jest": "^30"}}'},
         "npm install --no-package-lock", "npm"),
        ({"Package.swift": ""}, "swift package resolve", "swift"),
        ({"uv.lock": "", "package.json": "{}", "package-lock.json": "{}"},
         "uv sync --locked && npm ci", "uv+npm"),
    ],
)  # fmt: skip
def test_detect_by_lockfile(tmp_path, files, display, source):
    cmd = install.detect(_repo(tmp_path, files))
    assert cmd is not None and cmd.display == display and cmd.source == source
    if "&&" in display:
        assert cmd.argv() == ("/bin/sh", "-c", display)
    else:
        assert cmd.argv() == cmd.steps[0]


@pytest.mark.parametrize(
    "files",
    [
        {"pyproject.toml": "[project]\nname='x'\n", "tests/test_x.py": ""},  # python3 -m pytest
        {"Makefile": "test:\n\ttrue\n"},  # Makefile only
        {"package.json": '{"scripts": {"test": "node t.js"}}'},  # no dependencies
        {"README.md": "x"},
    ],
)
def test_detect_nothing_to_install(tmp_path, files):
    assert install.detect(_repo(tmp_path, files)) is None


def test_fixture_repos_detect_their_install(tmp_path):
    assert install.detect(FIXTURES / "python") is None
    assert install.detect(FIXTURES / "makefile") is None
    assert install.detect(FIXTURES / "node") is None
    assert install.detect(FIXTURES / "swift").display == "swift package resolve"
    shutil.copytree(FIXTURES / "python_uv_dep", tmp_path / "r")
    assert install.detect(tmp_path / "r") is None  # the lock is written at test time
    (tmp_path / "r" / "uv.lock").write_text("")
    assert install.detect(tmp_path / "r").display == "uv sync --locked"


def test_override_wins_and_runs_in_a_shell(tmp_path):
    repo = _repo(tmp_path, {"uv.lock": ""})
    cmd = install.detect(repo, override="  make deps  ")
    assert cmd.source == "override" and cmd.argv() == ("/bin/sh", "-c", "make deps")
    assert install.detect(repo, override="  ").display == "uv sync --locked"


# --------------------------------------------------------------------------- cache, policy


def test_cache_root_is_per_repo_under_the_foreman_home(foreman_home):
    a = install.cache_root("Me/Toy")
    assert a == foreman_home / "cache" / "me" / "toy"
    assert install.cache_root("me/other") != a
    assert all((a / sub).is_dir() for sub in install.CACHE_SUBDIRS)
    assert oct(a.stat().st_mode & 0o777) == oct(0o700)
    for bad in ("../x", "me/..", "me", "me/../../etc", "./x"):
        with pytest.raises(ValueError):
            install.cache_root(bad)


def test_snapshot_is_a_private_copy(tmp_path):
    cache = tmp_path / "cache"
    (cache / "uv" / "wheels").mkdir(parents=True)
    (cache / "uv" / "wheels" / "a.whl").write_text("A")
    took = install.snapshot(cache, tmp_path / "scratch" / "cache")
    copy = tmp_path / "scratch" / "cache" / "uv" / "wheels" / "a.whl"
    assert copy.read_text() == "A" and took >= 0
    copy.write_text("POISON")
    assert (cache / "uv" / "wheels" / "a.whl").read_text() == "A"


def _wt_policy(tmp_path: Path) -> Policy:
    wt = tmp_path / "wt"
    (wt / ".git").mkdir(parents=True)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    return Policy.create(wt, scratch, home=tmp_path / "home", foreman_home=tmp_path / "fh")


def test_install_policy_ports_only_for_base_and_cache_writable_only_then(tmp_path):
    pol = _wt_policy(tmp_path)
    cache = tmp_path / "fh" / "cache" / "o" / "r"
    cache.mkdir(parents=True)
    net = install.install_policy(pol, pol.worktree, network="ports", cache=cache)
    text = sb.render_profile(net)
    assert '(remote ip "*:443")' in text and '(remote ip "*:80")' in text
    assert "(allow network*)" not in text
    assert f'(subpath "{cache.resolve()}")' in text.split(";; writes")[1]
    env = sb.child_env(net)
    assert env["UV_CACHE_DIR"] == str(cache.resolve() / "uv") and "UV_OFFLINE" not in env
    off = install.install_policy(pol, pol.worktree, network="none", cache=None)
    otext = sb.render_profile(off)
    assert "network-outbound" not in otext and str(cache.resolve()) not in otext
    oenv = sb.child_env(off)
    assert oenv["UV_OFFLINE"] == "1" and oenv["UV_CACHE_DIR"].startswith(str(pol.scratch))
    assert '"*:8443"' in sb.render_profile(
        install.install_policy(pol, pol.worktree, network="ports", cache=None, ports=[8443])
    )


def test_install_policy_cannot_write_the_mirror_or_its_marker(tmp_path):
    att = _Repo(tmp_path)
    pol = Policy.create(att.wt.path, tmp_path / "s", home=tmp_path / "home",
                        foreman_home=att.fhome)  # fmt: skip
    assert pol.git.writes  # a normal launch may commit
    inst = install.install_policy(pol, att.wt.path, network="ports", cache=None)
    assert inst.git.writes == () and inst.git.write_files == ()
    marker = install.marker_path(att.wt.path)
    assert marker is not None and marker in inst.git.protected and marker in pol.git.protected
    assert inst.install is None and inst.proxy_port is None and not inst.listen_loopback


def test_network_for():
    pol = Policy(worktree=Path("/w"), scratch=Path("/s"))
    assert install.network_for(pol, base=True) == "ports"
    assert install.network_for(pol, base=False) == "none"
    allow = Policy(worktree=Path("/w"), scratch=Path("/s"), network="allow")
    assert install.network_for(allow, base=False) == "allow"


# --------------------------------------------------------------------------- config, status


def test_config_sandbox_install_round_trip(tmp_path):
    cfg = Config(sandbox_install={"me/toy": "off"}, install_timeout_seconds=60)
    assert cfg.sandbox_install_for("Me/Toy") == "off"
    assert cfg.sandbox_install_for("me/other") == "on"  # default on
    path = config_mod.save(cfg, tmp_path / "c.toml")
    again = config_mod.load(path)
    assert again.sandbox_install == (("me/toy", "off"),) and again.install_timeout_seconds == 60
    with pytest.raises(ConfigError):
        Config(sandbox_install={"me/toy": "maybe"})
    with pytest.raises(ConfigError):
        Config(sandbox_install={"nope": "off"})
    with pytest.raises(ConfigError):
        Config(install_timeout_seconds=0)


def test_status_line_says_how_dependencies_are_installed():
    on = sb.describe("on", {}, {})
    assert on.startswith("Sandbox: on") and "Dependency install: sandboxed" in on
    assert "ports 443/80" in on and "offline after model edits" in on
    off_repo = sb.describe("on", {}, {"me/toy": "off"})
    assert "skipped for me/toy (sandbox_install = off)" in off_repo
    assert "unsandboxed, with network" in sb.describe("off", {}, {})
    assert "Dependency" not in sb.describe("on", {})  # callers that pass no installs


# --------------------------------------------------------------------------- the mixin


class _SetupFailed(Exception):
    """Defined like slot A's ``worker_setup.SetupFailed(reason, detail)``."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


@pytest.fixture
def fake_setup(monkeypatch):
    mod = types.ModuleType("foreman_inator.worker_setup")
    mod.SetupFailed = _SetupFailed  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "foreman_inator.worker_setup", mod)
    return mod


class FakeWorker(InstallSteps, SandboxSteps):
    def __init__(self, tmp: Path, **cfg):
        self.store = Store(tmp / "db.sqlite")
        self.config = Config(**cfg)
        self.traces_dir = tmp / "traces"


class _Ticket:
    def __init__(self, repo: str = REPO, id: int = 7):
        self.repo = repo
        self.id = id


class _Repo:
    """A bare origin seeded from a fixture (optionally edited first), its mirror and
    attempt worktrees under a fake foreman-inator home."""

    def __init__(self, tmp: Path, fixture: str = "python", index: PackageIndex | None = None,
                 repo: str = REPO):  # fmt: skip
        self.tmp = tmp
        self.fhome = tmp / "fhome"
        self.fhome.mkdir(exist_ok=True)
        src = tmp / f"src-{repo.replace('/', '-')}"
        shutil.copytree(FIXTURES / fixture, src)
        if index is not None:
            with (src / "pyproject.toml").open("a") as f:
                f.write(f'\n[[tool.uv.index]]\nname = "local"\nurl = "{index.url}"\n'
                        "default = true\n")  # fmt: skip
            env = {
                "PATH": os.environ["PATH"],
                "HOME": str(tmp / "lock-home"),
                "UV_CACHE_DIR": str(tmp / "lock-cache"),
                "UV_PYTHON_DOWNLOADS": "never",
            }
            out = subprocess.run(["uv", "lock"], cwd=src, env=env, capture_output=True, text=True)
            assert out.returncode == 0, out.stderr
        self._git("init", "-q", "-b", "trunk", cwd=src)
        self._git("add", "-A", cwd=src)
        self._git("commit", "-qm", "seed", cwd=src)
        self.origin = tmp / f"origin-{repo.replace('/', '-')}.git"
        self._git("clone", "-q", "--bare", str(src), str(self.origin), cwd=tmp)
        self.mgr = WorktreeManager(self.fhome / "worktrees", identity=GitIdentity())
        self.repo = repo
        self.n = 0
        self.wt = self.attempt()

    @staticmethod
    def _git(*args: str, cwd: Path) -> None:
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                       env={**os.environ, **GIT_ENV})  # fmt: skip

    def attempt(self, start_point: str | None = None):
        self.n += 1
        return self.mgr.create(self.repo, "trunk", 1, self.n, clone_url=str(self.origin),
                               start_point=start_point)  # fmt: skip


def test_install_off_skips_and_the_status_line_says_so(tmp_path, fake_setup):
    att = _Repo(tmp_path, "python")
    (att.wt.path / "uv.lock").write_text("")  # would be detected
    w = FakeWorker(tmp_path, sandbox_install={REPO: "off"})
    assert w.install_dependencies(_Ticket(), 1, att.wt) is None
    w.prepare_worktree(_Ticket(), 1, att.wt)
    assert not (tmp_path / "traces").exists() and not (att.wt.path / ".venv").exists()
    assert "skipped for o/r (sandbox_install = off)" in w.sandbox_line()
    assert "skipped for o/r" in w.install_line()


def test_nothing_detected_runs_nothing(tmp_path, fake_setup):
    att = _Repo(tmp_path, "python")
    w = FakeWorker(tmp_path, sandbox="off")
    assert w.install_dependencies(_Ticket(), 1, att.wt) is None
    assert not (tmp_path / "traces").exists()


def test_install_failure_raises_setup_failed_with_the_log_tail(tmp_path, fake_setup):
    att = _Repo(tmp_path, "python")
    w = FakeWorker(tmp_path, sandbox="off")
    w.store.set_install_command(REPO, "echo first-line; echo boom-marker >&2; exit 3")
    with pytest.raises(_SetupFailed) as err:
        w.prepare_worktree(_Ticket(), 11, att.wt)
    assert err.value.reason == "dependency_install"
    assert "exit code 3" in err.value.detail and "boom-marker" in err.value.detail
    assert "traces/7/attempt-11-install.log" in err.value.detail
    log = tmp_path / "traces" / "7" / "attempt-11-install.log"  # beside the verify log
    assert "boom-marker" in log.read_text()
    assert not install.installed(att.wt.path, install.detect(att.wt.path, "x"))


def test_install_timeout_raises_setup_failed(tmp_path, fake_setup):
    att = _Repo(tmp_path, "python")
    w = FakeWorker(tmp_path, sandbox="off", install_timeout_seconds=1)
    w.store.set_install_command(REPO, "sleep 30")
    start = time.monotonic()
    with pytest.raises(_SetupFailed) as err:
        w.prepare_worktree(_Ticket(), 3, att.wt)
    assert "timed out" in err.value.detail and time.monotonic() - start < 20


def test_success_marks_the_worktree_once(tmp_path, fake_setup):
    att = _Repo(tmp_path, "python")
    w = FakeWorker(tmp_path, sandbox="off")
    w.store.set_install_command(REPO, "echo ran >> ../ran.txt")
    res = w.install_dependencies(_Ticket(), 1, att.wt)
    assert res.ran and res.network == "off"
    assert w.install_dependencies(_Ticket(), 1, att.wt) is None  # marker: not again
    assert (att.wt.path.parent / "ran.txt").read_text() == "ran\n"
    excl = (att.fhome / "worktrees" / "o" / "r" / ".mirror" / "info" / "exclude").read_text()
    assert ".venv/" in excl and ".build/" in excl


def test_prepare_worktree_calls_the_parent_seam_first(tmp_path, fake_setup):
    calls: list[str] = []

    class Base:
        def prepare_worktree(self, ticket, attempt, worktree):
            calls.append("base")

    class W(InstallSteps, SandboxSteps, Base):
        def __init__(self) -> None:
            self.store = Store(tmp_path / "db.sqlite")
            self.config = Config(sandbox_install={REPO: "off"})
            self.traces_dir = tmp_path / "traces"

    W().prepare_worktree(_Ticket(), 1, tmp_path)
    assert calls == ["base"]


# --------------------------------------------------------------------------- enforcement


@pytest.fixture(scope="session")
def index_root(tmp_path_factory) -> Path:
    return build_index(tmp_path_factory.mktemp("pkgindex"))


@pytest.fixture
def index(index_root, monkeypatch):
    with PackageIndex(index_root) as idx:
        # Production: 443/80 only. The test index listens on a random loopback port.
        monkeypatch.setattr(sb, "INSTALL_PORTS", (443, 80, idx.port))
        yield idx


def _verify(w: FakeWorker, wt_path: Path, key: str, *, cmd=None, out: Path):
    pol = w._sandbox_policy(_Ticket(), wt_path, key, writer=False)
    try:
        return verify.run(cmd or verify.detect(wt_path), wt_path, timeout=300,
                          output_path=out, sandbox=pol)  # fmt: skip
    finally:
        w._drop_scratch(pol)


def _writer_style(w: FakeWorker, wt_path: Path, key: str, command: str, out: Path):
    """A command as the writer's bash tool runs it: the writer's policy (no network but
    the proxy port, none here), its scratch with the cache snapshot."""
    pol = w._sandbox_policy(_Ticket(), wt_path, key, writer=True)
    try:
        return verify.run(verify.VerifyCommand.from_override(command), wt_path, timeout=300,
                          output_path=out, sandbox=pol)  # fmt: skip
    finally:
        w._drop_scratch(pol)


@macos
@needs_uv
@pytest.mark.timeout(300)
def test_install_then_writer_then_verify_twice_second_from_the_cache(tmp_path, index,
                                                                     fake_setup):  # fmt: skip
    att = _Repo(tmp_path, "python_uv_dep", index)
    w = FakeWorker(tmp_path)
    t = _Ticket()
    # 1. install (network: the index's port only), then the writer, then verify (none)
    hits = index.hits
    first = w.install_dependencies(t, 1, att.wt)
    assert first is not None and first.ran and first.network == "ports"
    assert index.hits > hits  # it really fetched from the index
    log = (tmp_path / "traces" / "7" / "attempt-1-install.log").read_text()
    assert "fmantoydep" in log
    wr = _writer_style(w, att.wt.path, "a1-writer", "uv run pytest -q && "
                       ".venv/bin/python -c 'import fmantoydep; print(fmantoydep.GREETING)'",
                       tmp_path / "w1.log")  # fmt: skip
    assert wr.passed, wr.output_path.read_text()[-2000:]
    assert "hello from fmantoydep" in wr.output_path.read_text()
    v1 = _verify(w, att.wt.path, "a1-verify", out=tmp_path / "v1.log")
    assert v1.passed and v1.tests_run == 1, v1.output_path.read_text()[-2000:]
    assert "Network connectivity is disabled" not in v1.output_path.read_text()
    assert "VIRTUAL_ENV" not in v1.output_path.read_text()  # #54: no "does not match" warning
    # 2. a fresh worktree for the next attempt, with the index gone: the cache serves it
    index.server.shutdown()
    wt2 = att.attempt()
    second = w.install_dependencies(t, 2, wt2)
    assert second is not None and second.ran, "second install"
    v2 = _verify(w, wt2.path, "a2-verify", out=tmp_path / "v2.log")
    assert v2.passed and v2.tests_run == 1, v2.output_path.read_text()[-2000:]
    print(f"install 1: {first.seconds:.2f}s (index), install 2: {second.seconds:.2f}s (cache)")
    # nothing the install wrote is committed by the worker's `git add -A`
    status = subprocess.run(["git", "status", "--porcelain"], cwd=wt2.path, capture_output=True,
                            text=True)  # fmt: skip
    # The install must leave nothing untracked (.venv/ is excluded). Python's own
    # __pycache__/ from running the tests is excluded by the worker (worker_attempt.EXCLUDES),
    # which this test does not set up, so it is not the install's to answer for.
    left = [ln for ln in status.stdout.splitlines() if not ln.rstrip("/").endswith("__pycache__")]
    assert left == []


@macos
@needs_uv
@pytest.mark.timeout(300)
def test_verify_and_writer_have_no_network_and_cannot_write_the_shared_cache(tmp_path, index,
                                                                           fake_setup):  # fmt: skip
    att = _Repo(tmp_path, "python_uv_dep", index)
    w = FakeWorker(tmp_path)
    w.install_dependencies(_Ticket(), 1, att.wt)
    cache = install.cache_root(REPO)
    probe = (
        '.venv/bin/python -c "import socket,sys\n'
        "s=socket.socket(); s.settimeout(3)\n"
        f"try:\n s.connect(('127.0.0.1', {index.port})); print('CONN' + 'ECTED')\n"
        "except OSError as e: print('REF' + 'USED', e.errno)\"; "
        f"echo poison > {cache}/uv/POISON 2>&1 || echo WRITE-DEN''IED; "
        f"ls {cache}/uv >/dev/null && echo CACHE-READ''ABLE"
    )
    as_verify = verify.VerifyCommand.from_override(probe)
    for name in ("verify", "writer"):
        out = tmp_path / f"{name}.log"
        if name == "verify":
            res = _verify(w, att.wt.path, "a1-verify", cmd=as_verify, out=out)
        else:
            res = _writer_style(w, att.wt.path, "a1-writer", probe, out)
        text = res.output_path.read_text()
        assert "REFUSED 1" in text and "CONNECTED" not in text, (name, text)
        assert "WRITE-DENIED" in text and "CACHE-READABLE" in text, (name, text)
    assert not (cache / "uv" / "POISON").exists()


@macos
@needs_uv
@pytest.mark.timeout(300)
def test_after_a_model_edit_the_install_is_offline(tmp_path, index, fake_setup):
    """Attempt 2 builds on attempt 1's branch (a model edit): no network for its install.
    With a cold cache (a repo never installed) it fails even though the index is up."""
    att = _Repo(tmp_path, "python_uv_dep", index, repo="o/cold")
    (att.wt.path / "model.txt").write_text("written by a model\n")
    att._git("add", "-A", cwd=att.wt.path)
    att._git("commit", "-qm", "attempt 1", cwd=att.wt.path)
    wt2 = att.attempt(start_point=f"refs/heads/{att.wt.branch}")
    w = FakeWorker(tmp_path)
    hits = index.hits
    with pytest.raises(_SetupFailed) as err:
        w.install_dependencies(_Ticket("o/cold"), 2, wt2)
    assert index.hits == hits  # it never reached the index
    assert "network" in err.value.detail.lower() or "offline" in err.value.detail.lower()


@macos
@needs_uv
@pytest.mark.timeout(300)
def test_reviewer_scratch_worktree_is_installed_by_the_policy(tmp_path, index, fake_setup):
    """``review/checks.py`` unchanged: the review policy carries the plan, and the check's
    verify.run installs the scratch worktree first (offline, from the cache snapshot)."""
    att = _Repo(tmp_path, "python_uv_dep", index)
    w = FakeWorker(tmp_path)
    w.install_dependencies(_Ticket(), 1, att.wt)  # warms the repo's cache
    index.server.shutdown()
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=att.wt.path, capture_output=True,
                          text=True).stdout.strip()  # fmt: skip
    scratch = att.wt.mirror.scratch(head, "review-1-head")
    work = tmp_path / "review-work"
    work.mkdir()
    pol = w._sandbox_policy(_Ticket(), work, "r1", writer=False)
    try:
        command = verify.detect(scratch.path)
        start = time.monotonic()
        with sb.active(pol):
            res = checks.check_tests(scratch.path, command, timeout=300,
                                     output_path=tmp_path / "review.log")  # fmt: skip
        took = time.monotonic() - start
    finally:
        w._drop_scratch(pol)
    assert res.status == "passed", (tmp_path / "review.log").read_text()[-2000:]
    assert (tmp_path / "review-install.log").exists()
    assert install.installed(scratch.path, install.detect(scratch.path))
    print(f"review scratch install + tests: {took:.2f}s")


@macos
def test_install_cannot_read_secrets_or_write_outside(tmp_path, fake_setup, foreman_home):
    att = _Repo(tmp_path, "python")
    foreman_home.mkdir(parents=True, exist_ok=True)
    (foreman_home / "app.pem").write_text("FAKE-PEM\n")
    outside = tmp_path / "outside.txt"
    w = FakeWorker(tmp_path)
    w.store.set_install_command(
        REPO, f"cat {foreman_home / 'app.pem'}; echo x > {outside}; echo done-marker"
    )
    res = w.install_dependencies(_Ticket(), 1, att.wt)
    log = res.log_path.read_text()
    assert "FAKE-PEM" not in log and log.count("Operation not permitted") == 2
    assert not outside.exists() and "done-marker" in log


def test_raises_the_real_setup_failed_when_slot_a_module_is_there(tmp_path):
    """Joint check with ``worker_setup`` (wave 11, slot A), when it is importable."""
    ws = pytest.importorskip("foreman_inator.worker_setup")
    att = _Repo(tmp_path, "python")
    w = FakeWorker(tmp_path, sandbox="off")
    w.store.set_install_command(REPO, "echo tail-marker; exit 2")
    with pytest.raises(ws.SetupFailed) as err:
        w.prepare_worktree(_Ticket(), 4, att.wt)
    assert err.value.flag_reason.startswith("setup_failed: dependency_install: ")
    assert "tail-marker" in err.value.flag_reason
