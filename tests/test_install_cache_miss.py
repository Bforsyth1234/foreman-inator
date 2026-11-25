"""An offline install that misses an empty cache refills it from the base commit (#57).

The pure parts (cache-miss detection, the dependency-file check, the base guard) run
everywhere; the end-to-end tests run on macOS with real ``sandbox-exec``, real ``uv`` and
the loopback package index of ``tests/fakes/pkgindex.py`` (as ``test_install.py``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import test_install
from foreman_inator import worker_install
from foreman_inator.runner import install
from foreman_inator.runner.sandbox import Policy
from test_install import FakeWorker, _Repo, _SetupFailed, _Ticket, _verify, macos, needs_uv

# test_install's fixtures, used here by name.
fake_setup = test_install.fake_setup
index = test_install.index
index_root = test_install.index_root

COLD = "o/cold"


# --------------------------------------------------------------------------- pure parts


@pytest.mark.parametrize(
    ("text", "miss"),
    [
        ("Failed to download pygments==2.21.0\n  Caused by: Network connectivity is disabled, "
         "but the requested data wasn't found in the cache for `https://x/p.whl`", True),
        ("npm error code ENOTCACHED\nnpm error request to https://r/x failed: cache mode is "
         "'only-if-cached' but no cached response is available.", True),
        ("error: The lockfile at `uv.lock` needs to be updated", False),
        ("npm error code E404", False),
        ("", False),
    ],
)  # fmt: skip
def test_is_cache_miss(text, miss):
    assert install.is_cache_miss(text) is miss


def _base(att: _Repo) -> str:
    return att.wt.base_sha


def test_dependency_files_changed(tmp_path):
    att = _Repo(tmp_path, "python_uv_dep")
    wt = att.wt.path
    base = _base(att)
    assert install.dependency_files_changed(wt, base) == []
    (wt / "model.txt").write_text("a model's edit\n")
    assert install.dependency_files_changed(wt, base) == []  # not a dependency file
    (wt / "yarn.lock").write_text("# new\n")  # untracked, new
    assert install.dependency_files_changed(wt, base) == ["yarn.lock"]
    (wt / "yarn.lock").unlink()
    with (wt / "uv.lock").open("a") as f:
        f.write("\n# edited\n")
    att._git("add", "-A", cwd=wt)
    att._git("commit", "-qm", "edit lock", cwd=wt)
    assert install.dependency_files_changed(wt, base) == ["uv.lock"]
    assert install.dependency_files_changed(wt, None) is None


def test_dependency_files_include_package_json_without_a_lockfile():
    no_lock = install.InstallCommand.of([("npm", "install", "--no-package-lock")], "npm")
    assert "package.json" in install.dependency_files(no_lock)
    npm_ci = install.InstallCommand.of([("npm", "ci")], "npm")
    assert "package.json" not in install.dependency_files(npm_ci)


def test_refill_refuses_a_scratch_that_is_not_the_base(tmp_path, monkeypatch):
    """The networked refill runs only in a checkout holding exactly the base's files."""
    att = _Repo(tmp_path, "python")
    (att.wt.path / "model.txt").write_text("a model's edit\n")
    ran: list[object] = []
    monkeypatch.setattr(install, "run", lambda *a, **k: ran.append(a))
    cmd = install.InstallCommand.from_override("true")
    with pytest.raises(install.InstallFailed, match="refill refused"):
        install.refill_cache(cmd, att.wt.path, _base(att), policy=Policy(att.wt.path, tmp_path),
                             cache=tmp_path / "c", log_path=tmp_path / "l.log")  # fmt: skip
    assert ran == []


class _Scratch:
    def __init__(self, path: Path):
        self.path = path
        self.removed = False

    def remove(self) -> None:
        self.removed = True


class _Mirror:
    def __init__(self, tmp: Path, fail: Exception | None):
        self.tmp, self.fail, self.made = tmp, fail, []

    def scratch(self, commit: str, name: str) -> _Scratch:
        if self.fail is not None:
            raise self.fail
        s = _Scratch(self.tmp / f"scratch-{name}")
        self.made.append(s)
        return s


@pytest.mark.parametrize("where", ["scratch", "policy"])
def test_refill_setup_errors_are_setup_failed_and_drop_the_scratch(tmp_path, monkeypatch, where):
    """A refill that cannot even be set up (the mirror's scratch worktree or the sandbox
    policy raises) is a ``setup_failed`` flag, not a job crash; a scratch that was made
    is removed."""
    from foreman_inator.runner.worktree import WorktreeError
    from foreman_inator.worker_setup import SetupFailed

    monkeypatch.setattr(install, "dependency_files_changed", lambda *a, **k: [])
    mirror = _Mirror(tmp_path, WorktreeError("no mirror") if where == "scratch" else None)
    w = FakeWorker(tmp_path)

    def bad_policy(*a, **k):
        raise OSError("scratch dir not writable")

    if where == "policy":
        monkeypatch.setattr(w, "_sandbox_policy", bad_policy, raising=False)
    wt = type("Wt", (), {"base_sha": "abc123", "mirror": mirror, "path": tmp_path})()
    first = install.InstallFailed("offline install missed the cache", "cache miss")
    monkeypatch.setattr(install, "dependency_files", lambda c: ["uv.lock"])
    with pytest.raises(SetupFailed) as err:
        w._recover_cache_miss(_Ticket(COLD), 2, wt, tmp_path, None, first)
    assert err.value.reason == "dependency_install"
    assert "could not set up filling the empty cache" in err.value.detail
    assert ("WorktreeError" if where == "scratch" else "OSError") in err.value.detail
    assert all(s.removed for s in mirror.made)
    assert len(mirror.made) == (1 if where == "policy" else 0)


# --------------------------------------------------------------------------- end to end


def _edited(att: _Repo, *, lock: bool = False):
    """Attempt 2's worktree on attempt 1's branch, which holds a model edit."""
    (att.wt.path / "model.txt").write_text("written by a model\n")
    if lock:
        with (att.wt.path / "uv.lock").open("a") as f:
            f.write("\n# the model touched the lockfile\n")
    att._git("add", "-A", cwd=att.wt.path)
    att._git("commit", "-qm", "attempt 1", cwd=att.wt.path)
    return att.attempt(start_point=f"refs/heads/{att.wt.branch}")


@pytest.fixture
def runs(monkeypatch):
    """Every ``install.run``: (worktree, network)."""
    seen: list[tuple[Path, str]] = []
    real = install.run

    def spy(command, worktree, **kw):
        seen.append((Path(worktree).resolve(), kw["network"]))
        return real(command, worktree, **kw)

    monkeypatch.setattr(install, "run", spy)
    return seen


@pytest.fixture
def refill_on(monkeypatch):
    monkeypatch.setattr(worker_install, "REFILL_ON_CACHE_MISS", True)


@macos
@needs_uv
@pytest.mark.timeout(300)
def test_empty_cache_and_model_edit_refills_from_base_then_installs_offline(
    tmp_path, index, fake_setup, runs, refill_on
):
    att = _Repo(tmp_path, "python_uv_dep", index, repo=COLD)
    wt2 = _edited(att)
    w = FakeWorker(tmp_path)
    hits = index.hits
    res = w.install_dependencies(_Ticket(COLD), 2, wt2)
    assert res is not None and res.ran and res.network == "none"
    assert index.hits > hits  # the refill fetched from the index
    edited = wt2.path.resolve()
    assert [n for p, n in runs if p == edited] == ["none", "none"]  # first try, retry
    networked = [p for p, n in runs if n != "none"]
    assert networked and edited not in networked
    assert all(not p.exists() for p in networked)  # the scratch worktree is gone
    trace = tmp_path / "traces" / "7"
    assert "fmantoydep" in (trace / "attempt-2-install-cachefill.log").read_text()
    assert install.installed(wt2.path, install.detect(wt2.path))
    index.server.shutdown()
    v = _verify(w, wt2.path, "a2-verify", out=tmp_path / "v.log")
    assert v.passed and v.tests_run == 1, v.output_path.read_text()[-2000:]


@macos
@needs_uv
@pytest.mark.timeout(300)
def test_changed_lockfile_is_flagged_new_dependencies_need_the_user(
    tmp_path, index, fake_setup, runs, refill_on
):
    att = _Repo(tmp_path, "python_uv_dep", index, repo=COLD)
    wt2 = _edited(att, lock=True)
    w = FakeWorker(tmp_path)
    hits = index.hits
    with pytest.raises(_SetupFailed) as err:
        w.install_dependencies(_Ticket(COLD), 2, wt2)
    assert err.value.reason == "dependency_install"
    assert "new dependencies need the user" in err.value.detail
    assert "uv.lock" in err.value.detail
    assert index.hits == hits  # no refill, no network anywhere
    assert [n for _, n in runs] == ["none"]
    left = subprocess.run(["git", "worktree", "list"], cwd=wt2.path, capture_output=True,
                          text=True).stdout  # fmt: skip
    assert "cachefill" not in left
