"""ab:pair (#16) names and GitHub surface: side-B branch/worktree names, per-side cleanup,
the shared ``fman/`` head-branch parser, and the client's ``close_pr`` / ``delete_branch``."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest

from fakes.github import FakeGitHub
from foreman_inator import sweep
from foreman_inator.github import WRITE_METHODS, ForbiddenOperation, GitHubClient
from foreman_inator.github.app_auth import GitHubNotFound
from foreman_inator.runner.worktree import (
    WorktreeManager,
    attempt_dir_name,
    branch_name,
    check_branch,
    parse_attempt_dir,
    parse_fman_branch,
)
from test_worktree import BOT, git, origin, token  # noqa: F401  (pytest fixtures)

REPO = "octo/widgets"
WREPO = "acme/widgets"


# --------------------------------------------------------------------------- names


@pytest.mark.parametrize("side", [None, "a"])
def test_side_none_and_a_keep_unpaired_names(side):
    assert branch_name(7, 3, side) == "fman/issue-7/a3" == branch_name(7, 3)
    assert attempt_dir_name(7, 3, side) == "issue-7-a3"


def test_side_b_names():
    assert branch_name(7, 3, "b") == "fman/issue-7-b/a3"
    assert check_branch(branch_name(7, 3, "b"))  # still under fman/ (7a)
    assert attempt_dir_name(7, 3, "b") == "issue-7-b-a3"


@pytest.mark.parametrize("side", ["c", "B", "", "ab"])
def test_bad_side_refused(side):
    with pytest.raises(ValueError):
        branch_name(1, 1, side)
    with pytest.raises(ValueError):
        attempt_dir_name(1, 1, side)


@pytest.mark.parametrize(
    ("name", "want"),
    [
        ("fman/issue-7/a3", (7, None, 3)),
        ("fman/issue-7-b/a3", (7, "b", 3)),
        ("fman/issue-12/a10", (12, None, 10)),
        ("fman/issue-7/other", (7, None, None)),
        ("fman/issue-7-b/a3x", (7, "b", None)),
        ("fman/issue-7-c/a1", None),
        ("fman/issue-7", None),
        ("fman/chat/4", None),
        ("someone/feature", None),
        ("fman/issue-x/a1", None),
        ("", None),
    ],
)
def test_parse_fman_branch(name, want):
    assert parse_fman_branch(name) == want


def test_parse_round_trips():
    for side in (None, "b"):
        assert parse_fman_branch(branch_name(42, 5, side)) == (42, side, 5)
        assert parse_attempt_dir(attempt_dir_name(42, 5, side)) == (42, side, 5)
    for bad in ("scratch-x", "chat-3", ".mirror", "issue-4-c-a1", "issue-4-a", "issue-4-b-a1x"):
        assert parse_attempt_dir(bad) is None


# --------------------------------------------------------------------------- worktrees


def _branches(mirror) -> set[str]:
    out = git("branch", "--list", "--format=%(refname:short)", "fman/*", cwd=mirror.mirror_path)
    return set(out.splitlines())


def test_side_b_worktree_and_per_side_cleanup(tmp_path, origin, token):  # noqa: F811
    mgr = WorktreeManager(tmp_path / "home" / "worktrees", identity=BOT)
    url = origin["url"]
    a1 = mgr.create(WREPO, "trunk", 8, 1, clone_url=url, token=token)
    b1 = mgr.create(WREPO, "trunk", 8, 1, clone_url=url, token=token, side="b")
    b2 = mgr.create(WREPO, "trunk", 8, 2, clone_url=url, token=token, side="b")
    other = mgr.create(WREPO, "trunk", 80, 1, clone_url=url, token=token, side="b")
    assert b1.path.name == "issue-8-b-a1" and b1.branch == "fman/issue-8-b/a1"
    assert a1.path.name == "issue-8-a1" and a1.branch == "fman/issue-8/a1"

    mirror = mgr.mirror(WREPO, url)
    back = mirror.open(8, 2, side="b")
    assert back.branch == "fman/issue-8-b/a2" and back.path == b2.path

    mirror.cleanup_issue(8, "b")  # side A and issue 80 stay
    assert a1.path.exists() and not b1.path.exists() and not b2.path.exists()
    assert other.path.exists()
    assert _branches(mirror) == {"fman/issue-8/a1", "fman/issue-80-b/a1"}

    b1 = mgr.create(WREPO, "trunk", 8, 1, clone_url=url, token=token, side="b")
    mirror.cleanup_issue(8)  # unpaired / side A: side B stays
    assert not a1.path.exists() and b1.path.exists()
    assert _branches(mirror) == {"fman/issue-8-b/a1", "fman/issue-80-b/a1"}
    mirror.cleanup_issue(8, "a")  # idempotent, same as None
    assert b1.path.exists()


# --------------------------------------------------------------------------- sweep


def test_sweep_counts_side_b_heads():
    prs = [
        {"number": 50, "headRefName": "fman/issue-6/a1"},
        {"number": 51, "headRefName": "fman/issue-9-b/a1"},
        {"number": 52, "headRefName": "someone/feature"},
        {"number": 53, "headRefName": "fman/chat/2"},
    ]

    def run(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        assert list(args[1:3]) == ["pr", "list"]
        return subprocess.CompletedProcess(args, 0, json.dumps(prs), "")

    assert sweep.Gh(run).fman_pr_issues(REPO) == {6, 9}


# --------------------------------------------------------------------------- GitHub


def test_write_methods_include_pair_writes():
    assert {"close_pr", "delete_branch"} <= WRITE_METHODS


class _Auth:
    def token(self) -> str:
        return "ghs_test"

    def invalidate(self) -> None:
        pass


def _pr_json(state: str) -> dict:
    return {
        "number": 5,
        "node_id": "PR_5",
        "title": "t",
        "body": "b",
        "state": state,
        "draft": True,
        "merged": False,
        "head": {"ref": "fman/issue-1-b/a1", "sha": "c" * 40},
        "base": {"ref": "main", "sha": "d" * 40},
        "user": {"login": "bot"},
        "html_url": "https://github.com/octo/widgets/pull/5",
    }


def test_client_close_pr_and_delete_branch_requests():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "PATCH":
            return httpx.Response(200, json=_pr_json("closed"))
        return httpx.Response(204)

    c = GitHubClient(_Auth(), transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    pr = c.close_pr(REPO, 5)
    assert pr.state == "closed"
    c.delete_branch(REPO, "fman/issue-1-b/a1")
    assert [(r.method, r.url.path) for r in seen] == [
        ("PATCH", "/repos/octo/widgets/pulls/5"),
        ("DELETE", "/repos/octo/widgets/git/refs/heads/fman/issue-1-b/a1"),
    ]
    assert json.loads(seen[0].content) == {"state": "closed"}


@pytest.mark.parametrize("branch", ["main", "feature/x", "fmanx/y", "fman/../main", "fman/"])
def test_delete_branch_refuses_non_fman_before_any_request(branch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    c = GitHubClient(_Auth(), transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    with pytest.raises(ForbiddenOperation):
        c.delete_branch(REPO, branch)
    assert seen == []

    fake = FakeGitHub()
    fake.create_repo(REPO)
    fake.human_branch(REPO, branch)
    with pytest.raises(ForbiddenOperation):
        fake.delete_branch(REPO, branch)
    fake.assert_no_writes()
    assert branch in fake.repos[REPO].branches


def test_fake_records_close_and_delete_and_404s_unknown():
    fake = FakeGitHub()
    fake.create_repo(REPO)
    n = fake.create_issue(REPO, "Fix it", author="brooks")
    head = branch_name(n, 1, "b")
    fake.push_branch(REPO, head)
    pr = fake.create_draft_pr(REPO, head=head, base="main", title="t", body=f"Closes #{n}")

    closed = fake.close_pr(REPO, pr.number)
    assert closed.state == "closed" and not closed.merged
    fake.close_pr(REPO, pr.number)  # already closed: no second record
    assert fake.closed_prs == [(REPO, pr.number)]
    assert fake.get_issue(REPO, n).state == "open"  # closing a PR closes no issue

    fake.delete_branch(REPO, head)
    assert fake.deleted_branches == [(REPO, head)]
    assert head not in fake.repos[REPO].branches
    assert {c.method for c in fake.writes} >= {"close_pr", "delete_branch"}

    with pytest.raises(GitHubNotFound):
        fake.delete_branch(REPO, head)  # gone
    with pytest.raises(GitHubNotFound):
        fake.close_pr(REPO, 999)
    with pytest.raises(GitHubNotFound):
        fake.close_pr(REPO, n)  # an issue, not a PR
    assert fake.deleted_branches == [(REPO, head)]


def test_close_pr_as_is_the_human_helper():
    fake = FakeGitHub()
    fake.create_repo(REPO)
    head = branch_name(1, 1)
    fake.push_branch(REPO, head)
    pr = fake.create_draft_pr(REPO, head=head, base="main", title="t", body="b")
    before = list(fake.writes)
    fake.close_pr_as(REPO, pr.number, by="brooks")
    assert fake.writes == before and fake.closed_prs == []
    assert fake.get_pr(REPO, pr.number).state == "closed"


def test_worktree_module_path_is_used(tmp_path: Path):
    # sweep's head regex is the shared parser (no private copy to drift)
    assert sweep._FMAN_HEAD is parse_fman_branch
