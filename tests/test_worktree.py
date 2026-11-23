"""Worktree manager tests. Every repo is a local bare repo under tmp_path; no global git config."""

from __future__ import annotations

import base64
import os
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest

from foreman_inator.runner import worktree as wt_mod
from foreman_inator.runner.worktree import (
    BranchRefused,
    GitIdentity,
    PushRefused,
    RepoMirror,
    Worktree,
    WorktreeError,
    WorktreeManager,
    branch_name,
    check_branch,
)

SEED_IDENT = {
    "GIT_AUTHOR_NAME": "Seed",
    "GIT_AUTHOR_EMAIL": "seed@example.invalid",
    "GIT_COMMITTER_NAME": "Seed",
    "GIT_COMMITTER_EMAIL": "seed@example.invalid",
}
BOT = GitIdentity(name="test-app[bot]", email="123+test-app[bot]@users.noreply.github.com")


def git(*args: str, cwd: Path) -> str:
    """Test-side git, isolated from any global/system config like the module itself."""
    env = (
        {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        | SEED_IDENT
        | {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    )
    proc = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture
def origin(tmp_path: Path) -> dict:
    """A bare 'GitHub' repo whose default branch is `trunk` (not main), plus a `main` branch."""
    remote = tmp_path / "remote" / "acme" / "widgets.git"
    remote.mkdir(parents=True)
    git("init", "-q", "--bare", "--initial-branch=trunk", cwd=remote)
    seed = tmp_path / "seed"
    seed.mkdir()
    git("init", "-q", "--initial-branch=trunk", cwd=seed)
    (seed / "app.py").write_text("def hello():\n    return 'hi'\n")
    (seed / "old.txt").write_text("remove me\n")
    git("add", "-A", cwd=seed)
    git("commit", "-q", "--no-gpg-sign", "-m", "first", cwd=seed)
    first = git("rev-parse", "HEAD", cwd=seed)
    (seed / "README.md").write_text("# widgets\n")
    git("add", "-A", cwd=seed)
    git("commit", "-q", "--no-gpg-sign", "-m", "second", cwd=seed)
    second = git("rev-parse", "HEAD", cwd=seed)
    git("push", "-q", str(remote), "trunk", "trunk:main", cwd=seed)
    return {"url": str(remote), "path": remote, "first": first, "second": second}


@pytest.fixture
def token() -> str:
    return "ghs_" + secrets.token_hex(18)  # fake, generated at test time (invariant 7d)


@pytest.fixture
def mgr(tmp_path: Path) -> WorktreeManager:
    return WorktreeManager(tmp_path / "home" / "worktrees", identity=BOT)


def remote_ref(origin: dict, ref: str) -> str | None:
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", ref],
        cwd=origin["path"],
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    )
    return proc.stdout.strip() if proc.returncode == 0 else None


# ----------------------------------------------------------------------------- happy path


def test_create_edit_commit_diff_push_remove(mgr, origin, token, tmp_path):
    wt = mgr.create("acme/widgets", "trunk", 7, 1, clone_url=origin["url"], token=token)

    root = tmp_path / "home" / "worktrees"
    assert (root / "acme" / "widgets" / ".mirror" / "HEAD").exists()
    assert wt.path == root / "acme" / "widgets" / "issue-7-a1"
    assert wt.branch == "fman/issue-7/a1" == branch_name(7, 1)
    assert wt.base_sha == origin["second"]
    assert (wt.path / "README.md").exists()
    assert wt.changed_files() == []

    (wt.path / "app.py").write_text("def hello():\n    return 'hello'\n\n\ndef bye():\n    pass\n")
    (wt.path / "new_mod.py").write_text("X = 1\n")  # untracked
    (wt.path / "old.txt").unlink()

    expected = ["app.py", "new_mod.py", "old.txt"]
    assert wt.changed_files() == expected  # uncommitted + untracked counted
    stats = wt.diff_stats()
    assert stats.files == tuple(expected)
    assert (stats.insertions, stats.deletions) == (6, 2)
    assert stats.diff_files == 3 and stats.diff_lines == 8
    text = wt.diff_text()
    assert "+def bye():" in text and "+X = 1" in text and "-remove me" in text
    # inspecting the diff does not stage anything in the real index
    assert git("diff", "--cached", "--name-only", cwd=wt.path) == ""

    sha = wt.commit_all("Fix #7: say hello")
    assert sha and sha == wt.head_sha()
    assert git("log", "-1", "--format=%an <%ae>|%cn <%ce>", cwd=wt.path) == (
        f"{BOT.name} <{BOT.email}>|{BOT.name} <{BOT.email}>"
    )
    assert wt.commit_all("nothing new") is None
    assert wt.changed_files() == expected  # still measured against the base commit

    wt.push(token)
    assert remote_ref(origin, "refs/heads/fman/issue-7/a1") == sha
    assert remote_ref(origin, "refs/heads/trunk") == origin["second"]

    wt.remove()
    assert not wt.path.exists()
    mirror = mgr.mirror("acme/widgets", origin["url"])
    assert mirror.worktrees() == []
    assert (
        git("branch", "--list", "--format=%(refname:short)", "fman/*", cwd=mirror.mirror_path) == ""
    )
    wt.remove()  # second call is a no-op


# ----------------------------------------------------------------------------- invariant 7a


def test_push_to_main_raises(mgr, origin, token):
    wt = mgr.create("acme/widgets", "main", 3, 1, clone_url=origin["url"], token=token)
    (wt.path / "app.py").write_text("evil = True\n")
    wt.commit_all("try main")
    with pytest.raises(BranchRefused):
        wt.push(token, remote_branch="main")
    with pytest.raises(BranchRefused):
        wt.push(token, remote_branch="main", force=True)
    assert remote_ref(origin, "refs/heads/main") == origin["second"]

    # a worktree object claiming a non-fman branch can neither commit nor push
    rogue = Worktree(wt.mirror, wt.path, "main", wt.base_sha)
    with pytest.raises(BranchRefused):
        rogue.push(token)
    with pytest.raises(BranchRefused):
        rogue.commit_all("x")
    assert remote_ref(origin, "refs/heads/main") == origin["second"]


@pytest.mark.parametrize(
    "name",
    [
        "main",
        "trunk",
        "feature/x",
        "fman",
        "fman/",
        "refs/heads/fman/x",
        "fman/../main",
        "fman/a..b",
        "Fman/x",
        " fman/x",
    ],
)
def test_check_branch_refuses(name):
    with pytest.raises(BranchRefused):
        check_branch(name)


def test_check_branch_accepts():
    assert check_branch("fman/issue-12/a3") == "fman/issue-12/a3"
    assert check_branch("fman/replay/case-1") == "fman/replay/case-1"


def test_force_push_later_attempt_to_pr_branch(mgr, origin, token):
    a1 = mgr.create("acme/widgets", "trunk", 5, 1, clone_url=origin["url"], token=token)
    (a1.path / "app.py").write_text("v = 1\n")
    a1.commit_all("attempt 1")
    a1.push(token)

    a2 = mgr.create("acme/widgets", "trunk", 5, 2, clone_url=origin["url"], token=token)
    (a2.path / "app.py").write_text("v = 2\n")
    sha2 = a2.commit_all("attempt 2")
    with pytest.raises(WorktreeError):  # diverged: needs force
        a2.push(token, remote_branch=a1.branch)
    a2.push(token, remote_branch=a1.branch, force=True)
    assert remote_ref(origin, "refs/heads/fman/issue-5/a1") == sha2
    with pytest.raises(BranchRefused):
        a2.push(token, remote_branch="trunk", force=True)
    assert remote_ref(origin, "refs/heads/trunk") == origin["second"]


def test_start_point_continues_previous_attempt(mgr, origin, token):
    a1 = mgr.create("acme/widgets", "trunk", 6, 1, clone_url=origin["url"], token=token)
    (a1.path / "one.py").write_text("1\n")
    a1.commit_all("a1")
    a1.push(token)
    a2 = mgr.create(
        "acme/widgets",
        "trunk",
        6,
        2,
        clone_url=origin["url"],
        token=token,
        start_point="origin/fman/issue-6/a1",
    )
    assert (a2.path / "one.py").exists()
    assert a2.base_sha == origin["second"]
    assert a2.changed_files() == ["one.py"]  # diff is the PR's whole change vs base


# ----------------------------------------------------------------------------- token handling


def test_token_reaches_git_via_env_only(mgr, origin, token, monkeypatch, tmp_path):
    seen_argv: list[list[str]] = []
    real_run = subprocess.run

    def spy(args, *a, **kw):
        seen_argv.append(list(args))
        return real_run(args, *a, **kw)

    monkeypatch.setattr(wt_mod.subprocess, "run", spy)
    wt = mgr.create("acme/widgets", "trunk", 9, 1, clone_url=origin["url"], token=token)
    (wt.path / "f.txt").write_text("x\n")
    wt.commit_all("x")
    wt.push(token)

    b64 = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    assert seen_argv and all(token not in a and b64 not in a for argv in seen_argv for a in argv)
    for f in (tmp_path / "home").rglob("*"):
        if f.is_file():
            data = f.read_bytes()
            assert token.encode() not in data and b64.encode() not in data, f
    # git does receive it, as an http.extraHeader from the environment
    proc = wt_mod._git(["config", "--get", "http.extraHeader"], cwd=wt.path, token=token)
    assert proc.stdout.strip() == f"Authorization: Basic {b64}"
    assert (
        wt_mod._git(["config", "--get", "http.extraHeader"], cwd=wt.path, check=False).stdout == ""
    )


def test_token_scrubbed_from_errors(mgr, origin, token):
    mirror = mgr.mirror("acme/widgets", origin["url"])
    mirror.ensure(token)
    b64 = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    with pytest.raises(WorktreeError) as err:  # git echoes the bad arguments back
        wt_mod._git(["rev-parse", token, b64], cwd=mirror.mirror_path, token=token)
    assert token not in str(err.value) and b64 not in str(err.value)
    assert "***" in str(err.value)


def test_clone_url_with_credentials_refused(mgr, token):
    with pytest.raises(WorktreeError):
        mgr.mirror("acme/widgets", f"https://x-access-token:{token}@github.com/acme/widgets.git")


@pytest.mark.parametrize("repo", ["acme", "acme/../x", "../acme", "a/b/c", "acme/.."])
def test_bad_repo_name(mgr, repo):
    with pytest.raises(ValueError):
        mgr.mirror(repo, "https://github.com/acme/widgets.git")


# ----------------------------------------------------------------------------- crash safety


def test_create_after_crash_and_stale_dirs(mgr, origin, token):
    wt = mgr.create("acme/widgets", "trunk", 4, 1, clone_url=origin["url"], token=token)
    (wt.path / "junk.txt").write_text("half done\n")
    shutil.rmtree(wt.path)  # crash: directory gone, git still has it registered
    again = mgr.create("acme/widgets", "trunk", 4, 1, clone_url=origin["url"], token=token)
    assert again.changed_files() == []

    # a stray, unregistered directory where the worktree goes
    shutil.rmtree(again.path)
    mirror = again.mirror
    mirror.prune()
    again.path.mkdir()
    (again.path / "stray").write_text("?")
    third = mgr.create("acme/widgets", "trunk", 4, 1, clone_url=origin["url"], token=token)
    assert not (third.path / "stray").exists()
    assert mirror.worktrees() == [third.path]

    third.remove()
    third.remove()
    shutil.rmtree(third.path, ignore_errors=True)
    third.remove()

    # a half-made mirror is rebuilt
    (mirror.mirror_path / "HEAD").unlink()
    mirror.ensure(token)
    mirror.ensure(token)
    assert mirror.resolve("trunk") == origin["second"]


def test_open_reattaches_and_cleanup_issue(mgr, origin, token):
    a1 = mgr.create("acme/widgets", "trunk", 8, 1, clone_url=origin["url"], token=token)
    a2 = mgr.create("acme/widgets", "trunk", 8, 2, clone_url=origin["url"], token=token)
    other = mgr.create("acme/widgets", "trunk", 80, 1, clone_url=origin["url"], token=token)
    (a2.path / "z.py").write_text("z\n")

    mirror = mgr.mirror("acme/widgets", origin["url"])
    back = mirror.open(8, 2)
    assert back.base_sha == a2.base_sha and back.branch == "fman/issue-8/a2"
    assert back.changed_files() == ["z.py"]
    with pytest.raises(WorktreeError):
        mirror.open(8, 99)

    mirror.cleanup_issue(8)
    mirror.cleanup_issue(8)
    assert not a1.path.exists() and not a2.path.exists()
    assert mirror.worktrees() == [other.path]
    assert (
        git("branch", "--list", "--format=%(refname:short)", "fman/*", cwd=mirror.mirror_path)
        == "fman/issue-80/a1"
    )
    assert list(mgr.repos()) == ["acme/widgets"]


# ----------------------------------------------------------------------------- scratch


def test_scratch_at_past_commit_never_pushes(mgr, origin, token):
    mirror = mgr.mirror("acme/widgets", origin["url"])
    mirror.ensure(token)
    s = mirror.scratch(origin["first"], "review-1")
    assert s.path.name == "scratch-review-1"
    assert not (s.path / "README.md").exists()  # really at the past commit
    assert s.base_sha == origin["first"] and s.branch is None and not s.can_push
    (s.path / "app.py").write_text("reverted\n")
    assert s.changed_files() == ["app.py"]
    with pytest.raises(BranchRefused):
        s.commit_all("detached")
    with pytest.raises(PushRefused):
        s.push(token)

    r = mirror.scratch(origin["first"], "replay-1", branch="fman/replay/1")
    (r.path / "a.py").write_text("a\n")
    assert r.commit_all("replay commit")
    with pytest.raises(PushRefused):
        r.push(token)
    with pytest.raises(BranchRefused):
        mirror.scratch(origin["first"], "bad", branch="main")
    assert remote_ref(origin, "refs/heads/fman/replay/1") is None

    s.remove()
    r.remove()
    r.remove()
    assert mirror.worktrees() == []
    assert (
        git("branch", "--list", "--format=%(refname:short)", "fman/*", cwd=mirror.mirror_path) == ""
    )


def test_scratch_name_and_mirror_required(mgr, origin):
    mirror = RepoMirror(mgr.root, "acme/widgets", origin["url"])
    with pytest.raises(WorktreeError):
        mirror.scratch(origin["first"], "x")
    with pytest.raises(ValueError):
        mirror.scratch(origin["first"], "../x")
