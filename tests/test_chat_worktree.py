"""Chat worktrees and the cross-process mirror lock (todo #71, wave C1c).

`RepoMirror.create_chat/open_chat/chat_path`: ``fman/chat/<id>`` from the base branch
only (decision 5: never an ``fman/`` branch), kept worktrees never replaced. The mirror
lock: ``flock`` beside the mirror, held by another process -> this one waits."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from foreman_inator.chat import chat_branch
from foreman_inator.runner.worktree import (
    LOCK_NAME,
    BranchRefused,
    GitIdentity,
    MirrorLock,
    RepoMirror,
    WorktreeError,
    WorktreeManager,
    chat_branch_name,
    check_chat_base,
    mirror_lock,
)

SEED = {
    "GIT_AUTHOR_NAME": "Seed",
    "GIT_AUTHOR_EMAIL": "seed@example.invalid",
    "GIT_COMMITTER_NAME": "Seed",
    "GIT_COMMITTER_EMAIL": "seed@example.invalid",
}


def git(*args: str, cwd: Path) -> str:
    env = (
        {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        | SEED
        | {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    )
    p = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote" / "acme" / "widgets.git"
    remote.mkdir(parents=True)
    git("init", "-q", "--bare", "--initial-branch=trunk", cwd=remote)
    seed = tmp_path / "seed"
    seed.mkdir()
    git("init", "-q", "--initial-branch=trunk", cwd=seed)
    (seed / "app.py").write_text("x = 0\n")
    git("add", "-A", cwd=seed)
    git("commit", "-q", "--no-gpg-sign", "-m", "first", cwd=seed)
    git("push", "-q", str(remote), "trunk", cwd=seed)
    # a ticket's PR branch on the remote: a chat must never start from it
    git("checkout", "-q", "-b", "fman/issue-7/a1", cwd=seed)
    (seed / "app.py").write_text("x = 7\n")
    git("commit", "-q", "--no-gpg-sign", "-am", "ticket work", cwd=seed)
    git("push", "-q", str(remote), "fman/issue-7/a1", cwd=seed)
    return remote


@pytest.fixture
def mirror(tmp_path: Path, remote: Path) -> RepoMirror:
    mgr = WorktreeManager(tmp_path / "wts", identity=GitIdentity("bot", "b@x.y"))
    return mgr.mirror("acme/widgets", str(remote))


def test_chat_worktree_is_on_its_fman_chat_branch_from_the_base(mirror: RepoMirror, remote):
    wt = mirror.create_chat("trunk", 5)
    assert wt.path == mirror.chat_path(5) == mirror.repo_dir / "chat-5"
    assert wt.branch == "fman/chat/5" == chat_branch(5) == chat_branch_name(5)
    trunk = git("rev-parse", "trunk", cwd=remote)
    assert wt.base_sha == trunk == wt.head_sha()
    assert (wt.path / "app.py").read_text() == "x = 0\n"
    (wt.path / "app.py").write_text("x = 1\n")
    assert wt.changed_files() == ["app.py"]
    assert wt.commit_all("chat change")
    wt.push(None)
    assert git("rev-parse", "fman/chat/5", cwd=remote) == wt.head_sha()
    wt.remove(delete_branch=True)
    assert not wt.path.exists()
    assert "fman/chat/5" not in git("branch", "--list", cwd=mirror.mirror_path)


@pytest.mark.parametrize(
    "base",
    [
        "fman/issue-7/a1",
        "origin/fman/issue-7/a1",
        "refs/heads/fman/issue-7/a1",
        "refs/remotes/origin/fman/issue-7/a1",
        "fman/chat/1",
        "origin/origin/trunk",
        "",
        "bad..name",
    ],
)
def test_a_chat_never_starts_from_an_fman_branch(mirror: RepoMirror, base: str):
    """Decision 5: base branch only. The ticket's branch exists on the remote and is
    still refused, and nothing is created."""
    with pytest.raises(BranchRefused):
        mirror.create_chat(base, 6)
    assert not mirror.chat_path(6).exists()


def test_base_spellings_and_unknown_bases(mirror: RepoMirror, remote: Path):
    assert check_chat_base("origin/trunk") == check_chat_base("refs/heads/trunk") == "trunk"
    wt = mirror.create_chat("origin/trunk", 8)
    assert wt.base_sha == git("rev-parse", "trunk", cwd=remote)
    sha = git("rev-parse", "trunk", cwd=remote)
    for base in ("nope", sha):  # not a branch on the remote (a commit is not a branch)
        with pytest.raises(WorktreeError, match="no branch"):
            mirror.create_chat(base, 9)
    with pytest.raises(ValueError):
        mirror.chat_path(0)


def test_a_kept_chat_worktree_is_never_replaced_and_reopens(mirror: RepoMirror):
    wt = mirror.create_chat("trunk", 11)
    (wt.path / "notes.md").write_text("user work\n")
    with pytest.raises(WorktreeError, match="open_chat"):
        mirror.create_chat("trunk", 11)
    assert (wt.path / "notes.md").read_text() == "user work\n"
    again = mirror.open_chat(11)
    assert (again.path, again.branch, again.base_sha) == (wt.path, wt.branch, wt.base_sha)
    assert again.changed_files() == ["notes.md"]
    with pytest.raises(WorktreeError):
        mirror.open_chat(12)
    git("checkout", "-q", "-b", "elsewhere", cwd=wt.path)
    with pytest.raises(BranchRefused):
        mirror.open_chat(11)


def test_a_half_made_chat_dir_is_cleared(mirror: RepoMirror):
    mirror.ensure()
    stale = mirror.chat_path(13)
    stale.mkdir(parents=True)
    (stale / "junk").write_text("x")
    wt = mirror.create_chat("trunk", 13)
    assert not (wt.path / "junk").exists() and (wt.path / "app.py").exists()


# --------------------------------------------------------------------------- the lock

HOLD = r"""
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
open(sys.argv[2], "w").write("held")
time.sleep(float(sys.argv[3]))
"""


def hold_in_other_process(lock: Path, tmp_path: Path, seconds: float) -> subprocess.Popen:
    ready = tmp_path / "held.flag"
    ready.unlink(missing_ok=True)  # a flag left by an earlier holder must not count
    lock.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([sys.executable, "-c", HOLD, str(lock), str(ready), str(seconds)])
    end = time.monotonic() + 10
    while not ready.exists():
        assert time.monotonic() < end and proc.poll() is None
        time.sleep(0.02)
    return proc


def test_mirror_changes_wait_for_another_process_holding_the_lock(mirror, tmp_path: Path):
    """Verify (#71): the worker and a chat host never run git on one mirror at once."""
    mirror.ensure()
    lock_file = mirror.repo_dir / LOCK_NAME
    assert mirror.lock.path == lock_file
    proc = hold_in_other_process(lock_file, tmp_path, 1.5)
    try:
        t0 = time.monotonic()
        wt = mirror.create_chat("trunk", 21)  # fetch + worktree add
        waited = time.monotonic() - t0
    finally:
        proc.wait(10)
    assert waited >= 1.0, f"create_chat did not wait for the lock ({waited:.2f}s)"
    assert wt.path.exists()
    proc = hold_in_other_process(lock_file, tmp_path, 1.5)
    try:
        t0 = time.monotonic()
        wt.remove()  # worktree remove + branch delete
        assert time.monotonic() - t0 >= 1.0
    finally:
        proc.wait(10)


def test_the_lock_is_reentrant_per_thread_exclusive_across_threads_and_times_out(tmp_path):
    lock = mirror_lock(tmp_path / "x" / LOCK_NAME)
    assert mirror_lock(tmp_path / "x" / LOCK_NAME) is lock  # one per path per process
    order: list[str] = []

    def other() -> None:
        with lock.held():
            order.append("other")

    with lock.held(), lock.held():  # nested on one thread: fine
        t = threading.Thread(target=other)
        t.start()
        time.sleep(0.3)
        order.append("first")
    t.join(5)
    assert order == ["first", "other"]
    short = MirrorLock(tmp_path / "y.lock", timeout=0.3)
    proc = hold_in_other_process(short.path, tmp_path, 2.0)
    try:
        with pytest.raises(WorktreeError, match="timed out"), short.held():
            pass
    finally:
        proc.wait(10)
    with short.held():  # free again once the other process is gone
        pass
