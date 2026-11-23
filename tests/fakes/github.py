"""An in-memory GitHub for every test that would otherwise talk to GitHub (rule 8).

``FakeGitHub`` implements ``foreman_inator.github.GitHubAPI`` with the same method names and
the same dataclasses as the real ``GitHubClient``. Client-facing methods act as the App's bot
user; setup helpers (``create_repo``, ``create_issue``, ``apply_label``, ``submit_review``,
``merge_pr``, ``close_pr``, ``close_issue_as``, ``comment_as``, ``push_branch``) act as a human
and are never recorded as bot writes. CI (#41) is set up the same way: ``add_check_run``,
``set_check_run``, ``attach_job_log``, and ``deny_permission("checks" | "actions")`` for an
App registered before those permissions existed.

Every bot write is appended to ``fake.writes``, so a test can assert "no GitHub writes"
(``assert_no_writes()``) or forbid them outright (``forbid_writes()`` makes the next write
raise). There is no merge or approve method on the client side; ``merge_pr`` is a human helper
and ``post_review`` only produces ``COMMENTED`` reviews, as on the real client.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from foreman_inator.github import (
    WRITE_METHODS,
    Comment,
    ForbiddenOperation,
    GitHubError,
    GitHubNotFound,
    Issue,
    IssueEvent,
    PullRequest,
    Ref,
    Review,
)
from foreman_inator.github.client import (
    LOG_TAIL_CHARS,
    CheckRun,
    MissingPermission,
    ReviewComment,
    log_tail,
    require_fman_branch,
)

BOT_LOGIN = "foreman-inator[bot]"
FAKE_TOKEN = "ghs_fake_installation_token"
START = datetime(2026, 11, 23, 9, 0, 0, tzinfo=UTC)

_CLOSING = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b", re.IGNORECASE)


class TickClock:
    """Deterministic clock: each call returns one second later than the last."""

    def __init__(self, start: datetime = START) -> None:
        self.now = start - timedelta(seconds=1)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@dataclass(frozen=True)
class Call:
    method: str
    args: dict[str, Any]


@dataclass
class _Item:
    """An issue or a pull request (they share one number space, as on GitHub)."""

    number: int
    title: str
    body: str
    author: str
    created_at: datetime
    updated_at: datetime
    state: str = "open"
    state_reason: str | None = None
    closed_at: datetime | None = None
    labels: list[str] = field(default_factory=list)
    events: list[IssueEvent] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)
    # pull request fields
    is_pr: bool = False
    head: str = ""
    base: str = ""
    draft: bool = False
    merged_at: datetime | None = None
    reviews: list[Review] = field(default_factory=list)
    review_comments: dict[int, list[ReviewComment]] = field(default_factory=dict)
    mergeable: bool | None = None  # #43: None until set_conflict_state (GitHub "computing")


@dataclass
class _Repo:
    name: str
    default_branch: str
    branches: dict[str, str]
    items: dict[int, _Item] = field(default_factory=dict)
    next_number: int = 1
    #: sha -> check runs on that commit, in creation order (#41)
    checks: dict[str, list[CheckRun]] = field(default_factory=dict)
    job_logs: dict[int, str] = field(default_factory=dict)


class FakeGitHub:
    def __init__(
        self,
        *,
        bot_login: str = BOT_LOGIN,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._bot = bot_login
        self.clock = clock or TickClock()
        self.repos: dict[str, _Repo] = {}
        self.calls: list[Call] = []
        self.writes: list[Call] = []
        self.git_pushes: list[Call] = []
        self._writes_forbidden: str | None = None
        self._failures: dict[str, list[Exception]] = {}
        self._denied: set[str] = set()
        self._next_id = 1000

    # ------------------------------------------------------------------ test controls

    def forbid_writes(self, reason: str = "writes are forbidden in this test") -> None:
        """Any later bot write raises AssertionError (e.g. replays, shadow reviews)."""
        self._writes_forbidden = reason

    def allow_writes(self) -> None:
        self._writes_forbidden = None

    def assert_no_writes(self) -> None:
        assert not self.writes, f"unexpected GitHub writes: {self.writes}"

    def writes_named(self, method: str) -> list[Call]:
        return [c for c in self.writes if c.method == method]

    def fail_next(self, method: str, error: Exception | None = None) -> None:
        """Make the next call to ``method`` raise ``error`` (default: a 502 GitHubError)."""
        self._failures.setdefault(method, []).append(
            error or GitHubError(502, "fake failure", "FAKE", method)
        )

    def deny_permission(self, permission: str) -> None:
        """Answer 403 on endpoints needing ``permission`` ("checks", "actions"), as GitHub
        does for an App installed without it."""
        self._denied.add(permission)

    def _require(self, permission: str, method: str) -> None:
        if permission in self._denied:
            err = GitHubError(403, "Resource not accessible by integration", "GET", method)
            raise MissingPermission(permission, err)

    # ------------------------------------------------------------------ human-side CI (#41)

    def add_check_run(
        self,
        repo: str,
        sha: str,
        name: str,
        *,
        status: str = "queued",
        conclusion: str | None = None,
        app: str = "github-actions",
        log: str | None = None,
    ) -> int:
        """A check run on commit ``sha`` (an Actions job unless ``app`` says otherwise).
        Returns its id, which is also its job id for ``github-actions``."""
        r = self._repo(repo)
        run_id = self._id()
        now = self.clock()
        job = run_id if app == "github-actions" else None
        details = (
            f"https://github.com/{repo}/actions/runs/{run_id + 50000}/job/{run_id}"
            if job is not None
            else f"https://ci.example.com/{repo}/{run_id}"
        )
        run = CheckRun(
            id=run_id,
            name=name,
            status=status,
            conclusion=conclusion,
            head_sha=sha,
            app_slug=app,
            details_url=details,
            started_at=now if status != "queued" else None,
            completed_at=now if status == "completed" else None,
            job_id=job,
        )
        r.checks.setdefault(sha, []).append(run)
        if log is not None and job is not None:
            r.job_logs[job] = log
        return run_id

    def set_check_run(
        self, repo: str, run_id: int, *, status: str, conclusion: str | None = None
    ) -> None:
        """Move a check run on (queued -> in_progress -> completed with a conclusion)."""
        r = self._repo(repo)
        for runs in r.checks.values():
            for i, run in enumerate(runs):
                if run.id == run_id:
                    now = self.clock()
                    runs[i] = replace(
                        run,
                        status=status,
                        conclusion=conclusion if status == "completed" else None,
                        started_at=run.started_at or now,
                        completed_at=now if status == "completed" else None,
                    )
                    return
        raise KeyError(f"no check run {run_id} in {repo}")

    def attach_job_log(self, repo: str, job_id: int, text: str) -> None:
        self._repo(repo).job_logs[job_id] = text

    # ------------------------------------------------------------------ human-side setup

    def create_repo(
        self, repo: str, *, default_branch: str = "main", sha: str | None = None
    ) -> str:
        if repo in self.repos:
            raise ValueError(f"repo {repo} exists")
        self.repos[repo] = _Repo(repo, default_branch, {default_branch: sha or _sha(repo)})
        return repo

    def create_issue(
        self,
        repo: str,
        title: str,
        body: str = "",
        *,
        author: str = "brooks",
        labels: Sequence[str] = (),
    ) -> int:
        item = self._new_item(repo, title, body, author)
        for label in labels:
            self._label(item, label, author)
        return item.number

    def apply_label(self, repo: str, number: int, label: str, *, by: str) -> None:
        self._label(self._item(repo, number), label, by)

    def remove_label_as(self, repo: str, number: int, label: str, *, by: str) -> None:
        self._unlabel(self._item(repo, number), label, by)

    def comment_as(self, repo: str, number: int, body: str, *, by: str) -> Comment:
        return self._comment(self._item(repo, number), body, by)

    def submit_review(
        self,
        repo: str,
        number: int,
        *,
        by: str,
        state: str,
        body: str = "",
        comments: Sequence[dict[str, Any]] = (),
    ) -> Review:
        """A human review. ``state``: APPROVE | REQUEST_CHANGES | COMMENT (as the REST event).

        ``comments`` are its line comments (#42), each ``{"path", "line", "body"}`` plus an
        optional ``"diff_hunk"`` (default: a small made-up hunk ending at that line)."""
        pr = self._pr(repo, number)
        mapped = {
            "APPROVE": "APPROVED",
            "REQUEST_CHANGES": "CHANGES_REQUESTED",
            "COMMENT": "COMMENTED",
        }[state]
        if pr.author == by and mapped != "COMMENTED":
            raise GitHubError(422, "Can not approve or request changes on your own pull request")
        review = self._review(pr, by, mapped, body)
        pr.review_comments[review.id] = [
            ReviewComment(
                id=self._id(),
                review_id=review.id,
                author=by,
                path=c["path"],
                line=c.get("line"),
                start_line=c.get("start_line"),
                diff_hunk=c.get("diff_hunk")
                or f"@@ -1,3 +1,4 @@\n context\n+added at {c['path']}\n line {c.get('line')}",
                body=c["body"],
                commit_id=review.commit_id,
                created_at=review.submitted_at,
            )
            for c in comments
        ]
        return review

    def merge_pr(self, repo: str, number: int, *, by: str = "brooks") -> PullRequest:
        """The human merges. Closes issues named by closing keywords in the PR body."""
        pr = self._pr(repo, number)
        if pr.state != "open" or pr.draft:
            raise GitHubError(405, "Pull Request is not mergeable (closed or draft)")
        now = self.clock()
        pr.merged_at = now
        self._event(pr, "merged", by)
        self._close(pr, by, "completed")
        r = self.repos[repo]
        if pr.base == r.default_branch:
            r.branches[pr.base] = _sha(f"merge-{repo}-{number}")
            for n in _CLOSING.findall(pr.body):
                linked = r.items.get(int(n))
                if linked is not None and not linked.is_pr and linked.state == "open":
                    self._close(linked, by, "completed")
        return self._pr_dc(pr)

    def close_pr(self, repo: str, number: int, *, by: str = "brooks") -> None:
        self._close(self._pr(repo, number), by, None)

    def close_issue_as(
        self, repo: str, number: int, *, by: str = "brooks", reason: str = "not_planned"
    ) -> None:
        self._close(self._item(repo, number), by, reason)

    def reopen_issue_as(self, repo: str, number: int, *, by: str = "brooks") -> None:
        item = self._item(repo, number)
        item.state, item.state_reason, item.closed_at = "open", "reopened", None
        self._event(item, "reopened", by)

    def push_branch(self, repo: str, branch: str, sha: str | None = None) -> str:
        """Simulate the worker's ``git push`` of a ``fman/`` branch (recorded in git_pushes)."""
        require_fman_branch(branch)
        sha = sha or _sha(f"{repo}-{branch}-{len(self.git_pushes)}")
        self.repos[repo].branches[branch] = sha
        for item in self.repos[repo].items.values():
            if item.is_pr and item.head == branch:
                item.mergeable = None  # recomputed after a push
        self.git_pushes.append(Call("git_push", {"repo": repo, "branch": branch, "sha": sha}))
        return sha

    def set_conflict_state(self, repo: str, number: int, value: bool | None) -> None:
        """What GitHub computed as the PR's ``mergeable`` (#43): False = conflicts with its
        base, None = still computing. (Named so no helper name contains "merge" but the
        human ``merge_pr``: see test_no_merge_or_approve_surface.)"""
        self._pr(repo, number).mergeable = value

    def push_base(self, repo: str, branch: str | None = None) -> str:
        """Someone moves the base branch (e.g. merges another PR). Returns the new sha."""
        r = self._repo(repo)
        branch = branch or r.default_branch
        r.branches[branch] = _sha(f"{repo}-{branch}-moved-{self._id()}")
        for item in r.items.values():
            if item.is_pr and item.base == branch:
                item.mergeable = None  # GitHub recomputes after the base moves
        return r.branches[branch]

    def human_branch(self, repo: str, branch: str, sha: str | None = None) -> str:
        """A branch someone else created (the worker must not touch it)."""
        sha = sha or _sha(f"{repo}-{branch}")
        self.repos[repo].branches[branch] = sha
        return sha

    # ------------------------------------------------------------------ GitHubAPI: identity

    def token(self) -> str:
        self._call("token")
        return FAKE_TOKEN

    def bot_login(self) -> str:
        self._call("bot_login")
        return self._bot

    def get_default_branch(self, repo: str) -> str:
        self._call("get_default_branch", repo=repo)
        return self._repo(repo).default_branch

    # ------------------------------------------------------------------ GitHubAPI: issues

    def list_issues(
        self,
        repo: str,
        *,
        label: str | None = None,
        state: str = "open",
        since: datetime | None = None,
    ) -> list[Issue]:
        self._call("list_issues", repo=repo, label=label, state=state, since=since)
        out = []
        for item in self._repo(repo).items.values():
            if item.is_pr or (state != "all" and item.state != state):
                continue
            if label is not None and label.lower() not in {x.lower() for x in item.labels}:
                continue
            if since is not None and item.updated_at < since:
                continue
            out.append(self._issue_dc(item))
        return sorted(out, key=lambda i: -i.number)  # GitHub default: newest first

    def get_issue(self, repo: str, number: int) -> Issue:
        self._call("get_issue", repo=repo, number=number)
        return self._issue_dc(self._item(repo, number))

    def list_issue_events(self, repo: str, number: int) -> list[IssueEvent]:
        self._call("list_issue_events", repo=repo, number=number)
        return list(self._item(repo, number).events)

    def list_issue_comments(
        self, repo: str, number: int, *, since: datetime | None = None
    ) -> list[Comment]:
        self._call("list_issue_comments", repo=repo, number=number, since=since)
        return [
            c for c in self._item(repo, number).comments if since is None or c.updated_at >= since
        ]

    def post_comment(self, repo: str, number: int, body: str) -> Comment:
        self._write("post_comment", repo=repo, number=number, body=body)
        return self._comment(self._item(repo, number), body, self._bot)

    def add_labels(self, repo: str, number: int, labels: Sequence[str]) -> list[str]:
        self._write("add_labels", repo=repo, number=number, labels=list(labels))
        item = self._item(repo, number)
        for label in labels:
            self._label(item, label, self._bot)
        return list(item.labels)

    def remove_label(self, repo: str, number: int, label: str) -> list[str]:
        self._write("remove_label", repo=repo, number=number, label=label)
        item = self._item(repo, number)
        self._unlabel(item, label, self._bot)
        return list(item.labels)

    def close_issue(self, repo: str, number: int, *, reason: str = "completed") -> Issue:
        self._write("close_issue", repo=repo, number=number, reason=reason)
        item = self._item(repo, number)
        if item.state == "open":
            self._close(item, self._bot, reason)
        return self._issue_dc(item)

    # ------------------------------------------------------------------ GitHubAPI: branches

    def get_branch_sha(self, repo: str, branch: str) -> str:
        self._call("get_branch_sha", repo=repo, branch=branch)
        try:
            return self._repo(repo).branches[branch]
        except KeyError:
            raise GitHubNotFound(
                404, "Not Found", "GET", f"{repo}/git/ref/heads/{branch}"
            ) from None

    def create_branch(self, repo: str, branch: str, sha: str) -> Ref:
        require_fman_branch(branch)
        self._write("create_branch", repo=repo, branch=branch, sha=sha)
        r = self._repo(repo)
        if branch in r.branches:
            raise GitHubError(422, "Reference already exists", "POST", f"{repo}/git/refs")
        r.branches[branch] = sha
        return Ref(f"refs/heads/{branch}", sha)

    def update_branch(self, repo: str, branch: str, sha: str, *, force: bool = False) -> Ref:
        require_fman_branch(branch)
        self._write("update_branch", repo=repo, branch=branch, sha=sha, force=force)
        r = self._repo(repo)
        if branch not in r.branches:
            raise GitHubError(422, "Reference does not exist", "PATCH", f"{repo}/git/refs")
        r.branches[branch] = sha
        return Ref(f"refs/heads/{branch}", sha)

    # ------------------------------------------------------------------ GitHubAPI: pulls

    def create_draft_pr(
        self, repo: str, *, head: str, base: str, title: str, body: str
    ) -> PullRequest:
        require_fman_branch(head)
        self._write("create_draft_pr", repo=repo, head=head, base=base, title=title, body=body)
        r = self._repo(repo)
        if head not in r.branches or base not in r.branches:
            raise GitHubError(422, "Validation Failed: head or base does not exist")
        if any(i.is_pr and i.state == "open" and i.head == head for i in r.items.values()):
            raise GitHubError(422, f"A pull request already exists for {head}")
        pr = self._new_item(repo, title, body, self._bot)
        pr.is_pr, pr.head, pr.base, pr.draft = True, head, base, True
        return self._pr_dc(pr)

    def update_pr(
        self, repo: str, number: int, *, title: str | None = None, body: str | None = None
    ) -> PullRequest:
        self._write("update_pr", repo=repo, number=number, title=title, body=body)
        pr = self._pr(repo, number)
        if title is not None:
            pr.title = title
        if body is not None:
            pr.body = body
        pr.updated_at = self.clock()
        return self._pr_dc(pr)

    def get_pr(self, repo: str, number: int) -> PullRequest:
        self._call("get_pr", repo=repo, number=number)
        return self._pr_dc(self._pr(repo, number))

    def find_pr_for_branch(self, repo: str, branch: str) -> PullRequest | None:
        self._call("find_pr_for_branch", repo=repo, branch=branch)
        prs = [i for i in self._repo(repo).items.values() if i.is_pr and i.head == branch]
        return self._pr_dc(max(prs, key=lambda p: p.number)) if prs else None

    def mark_ready_for_review(self, repo: str, number: int) -> PullRequest:
        self._write("mark_ready_for_review", repo=repo, number=number)
        pr = self._pr(repo, number)
        if pr.draft:
            pr.draft = False
            self._event(pr, "ready_for_review", self._bot)
        return self._pr_dc(pr)

    def post_review(
        self, repo: str, number: int, body: str, *, commit_id: str | None = None
    ) -> Review:
        self._write("post_review", repo=repo, number=number, body=body, commit_id=commit_id)
        pr = self._pr(repo, number)
        return self._review(pr, self._bot, "COMMENTED", body, commit_id)

    def list_reviews(self, repo: str, number: int) -> list[Review]:
        self._call("list_reviews", repo=repo, number=number)
        return list(self._pr(repo, number).reviews)

    def list_review_comments(
        self, repo: str, pr_number: int, review_id: int
    ) -> list[ReviewComment]:
        self._call("list_review_comments", repo=repo, pr_number=pr_number, review_id=review_id)
        pr = self._pr(repo, pr_number)
        if not any(r.id == review_id for r in pr.reviews):
            raise GitHubNotFound(404, "Not Found", "GET", f"{repo}/reviews/{review_id}/comments")
        return list(pr.review_comments.get(review_id, []))

    # ------------------------------------------------------------------ GitHubAPI: CI (#41)

    def list_check_runs(self, repo: str, sha: str) -> list[CheckRun]:
        self._call("list_check_runs", repo=repo, sha=sha)
        self._require("checks", "list_check_runs")
        return list(self._repo(repo).checks.get(sha, []))

    def get_job_log_tail(self, repo: str, job_id: int, *, max_chars: int = LOG_TAIL_CHARS) -> str:
        self._call("get_job_log_tail", repo=repo, job_id=job_id, max_chars=max_chars)
        self._require("actions", "get_job_log_tail")
        logs = self._repo(repo).job_logs
        if job_id not in logs:
            raise GitHubNotFound(404, "Not Found", "GET", f"{repo}/actions/jobs/{job_id}/logs")
        return log_tail(logs[job_id], max_chars)

    # ------------------------------------------------------------------ internals

    def _call(self, method: str, **args: Any) -> None:
        self.calls.append(Call(method, args))
        queue = self._failures.get(method)
        if queue:
            raise queue.pop(0)

    def _write(self, method: str, **args: Any) -> None:
        assert method in WRITE_METHODS, method
        if self._writes_forbidden is not None:
            raise AssertionError(f"GitHub write {method}{args}: {self._writes_forbidden}")
        self._call(method, **args)
        self.writes.append(Call(method, args))

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _repo(self, repo: str) -> _Repo:
        try:
            return self.repos[repo]
        except KeyError:
            raise GitHubNotFound(404, "Not Found", "GET", f"/repos/{repo}") from None

    def _item(self, repo: str, number: int) -> _Item:
        try:
            return self._repo(repo).items[number]
        except KeyError:
            raise GitHubNotFound(
                404, "Not Found", "GET", f"/repos/{repo}/issues/{number}"
            ) from None

    def _pr(self, repo: str, number: int) -> _Item:
        item = self._item(repo, number)
        if not item.is_pr:
            raise GitHubNotFound(404, "Not Found", "GET", f"/repos/{repo}/pulls/{number}")
        return item

    def _new_item(self, repo: str, title: str, body: str, author: str) -> _Item:
        r = self._repo(repo)
        now = self.clock()
        item = _Item(r.next_number, title, body, author, created_at=now, updated_at=now)
        r.items[item.number] = item
        r.next_number += 1
        return item

    def _event(self, item: _Item, event: str, actor: str, label: str | None = None) -> None:
        now = self.clock()
        item.events.append(IssueEvent(self._id(), event, actor, label, now))
        item.updated_at = now

    def _label(self, item: _Item, label: str, actor: str) -> None:
        if label.lower() in {x.lower() for x in item.labels}:
            return  # GitHub emits no event for a label that is already there
        item.labels.append(label)
        self._event(item, "labeled", actor, label)

    def _unlabel(self, item: _Item, label: str, actor: str) -> None:
        for existing in list(item.labels):
            if existing.lower() == label.lower():
                item.labels.remove(existing)
                self._event(item, "unlabeled", actor, existing)

    def _comment(self, item: _Item, body: str, author: str) -> Comment:
        now = self.clock()
        c = Comment(self._id(), body, author, now, now)
        item.comments.append(c)
        item.updated_at = now
        return c

    def _review(
        self, pr: _Item, author: str, state: str, body: str, commit_id: str | None = None
    ) -> Review:
        if pr.state != "open":
            raise GitHubError(422, "Can not review a closed pull request")
        sha = commit_id or self.repos_for(pr).branches.get(pr.head)
        review = Review(self._id(), author, state, body, self.clock(), sha)
        pr.reviews.append(review)
        pr.updated_at = review.submitted_at  # type: ignore[assignment]
        return review

    def repos_for(self, item: _Item) -> _Repo:
        return next(r for r in self.repos.values() if r.items.get(item.number) is item)

    def _close(self, item: _Item, actor: str, reason: str | None) -> None:
        item.state = "closed"
        item.state_reason = reason if not item.is_pr else None
        self._event(item, "closed", actor)
        item.closed_at = item.updated_at

    def _issue_dc(self, item: _Item) -> Issue:
        return Issue(
            number=item.number,
            title=item.title,
            body=item.body,
            state=item.state,
            state_reason=item.state_reason,
            labels=tuple(item.labels),
            author=item.author,
            is_pull_request=item.is_pr,
            created_at=item.created_at,
            updated_at=item.updated_at,
            closed_at=item.closed_at,
        )

    def _pr_dc(self, item: _Item) -> PullRequest:
        repo = self.repos_for(item).name
        return PullRequest(
            number=item.number,
            node_id=f"PR_fake_{item.number}",
            title=item.title,
            body=item.body,
            state=item.state,
            draft=item.draft,
            merged=item.merged_at is not None,
            merged_at=item.merged_at,
            head_ref=item.head,
            base_ref=item.base,
            author=item.author,
            html_url=f"https://github.com/{repo}/pull/{item.number}",
            head_sha=self.repos[repo].branches.get(item.head, ""),
            mergeable=item.mergeable,
        )


def _sha(seed: str) -> str:
    import hashlib

    return hashlib.sha1(seed.encode()).hexdigest()


__all__ = ["BOT_LOGIN", "FAKE_TOKEN", "Call", "FakeGitHub", "ForbiddenOperation", "TickClock"]
