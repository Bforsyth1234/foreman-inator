"""The worker's GitHub surface: a Protocol, its dataclasses, and the real REST/GraphQL client.

Only what SPEC needs. There is deliberately no merge method and no way to approve a PR:
the bot's own reviews are always ``COMMENT`` reviews (GitHub also refuses an author's
approval of their own PR), and branches / PR heads outside ``fman/`` are refused
(invariant 7a).

Endpoints relied on (rule 12, read 2026-11-23, API version 2026-03-10):
- issues: ``GET /repos/{o}/{r}/issues`` (labels, state, since; PRs carry ``pull_request``),
  ``GET|PATCH /repos/{o}/{r}/issues/{n}`` (state, state_reason)
  https://docs.github.com/en/rest/issues/issues
- issue events: ``GET /repos/{o}/{r}/issues/{n}/events``; ``labeled`` rows have ``actor``
  and ``label.name``. https://docs.github.com/en/rest/issues/events
- comments: ``GET|POST /repos/{o}/{r}/issues/{n}/comments`` (since)
  https://docs.github.com/en/rest/issues/comments
- edit a comment (#77 / todo 81, rule 12, read 2026-11-25): "Update an issue comment",
  ``PATCH /repos/{o}/{r}/issues/comments/{comment_id}`` ``{"body": ...}`` (body required)
  -> 200 with the comment, 422 on validation failure or spam. The comment id is
  repo-scoped (no issue number in the path).
  https://docs.github.com/en/rest/issues/comments#update-an-issue-comment
- labels: ``POST /repos/{o}/{r}/issues/{n}/labels`` ``{"labels": [...]}``,
  ``DELETE .../labels/{name}`` (404 when absent) https://docs.github.com/en/rest/issues/labels
- refs: ``GET /repos/{o}/{r}/git/ref/heads/{b}``, ``POST .../git/refs``,
  ``PATCH .../git/refs/heads/{b}`` (force) https://docs.github.com/en/rest/git/refs
- pulls: ``POST|GET|PATCH /repos/{o}/{r}/pulls[/{n}]`` (``draft`` only on create; list
  filter ``head=owner:branch``) https://docs.github.com/en/rest/pulls/pulls
- ready for review: GraphQL ``markPullRequestReadyForReview(input: {pullRequestId})`` (no REST
  endpoint exists) https://docs.github.com/en/graphql/reference/pulls
- reviews: ``GET|POST /repos/{o}/{r}/pulls/{n}/reviews`` (event COMMENT)
  https://docs.github.com/en/rest/pulls/reviews

Added in wave 6b (rule 12, read 2026-11-23, same API version):
- check runs (#41): ``GET /repos/{o}/{r}/commits/{ref}/check-runs`` -> ``{total_count,
  check_runs: [...]}`` (``filter=latest`` is the default; ``per_page`` max 100; only the
  1000 most recent check suites of a ref). ``status`` is queued | in_progress | completed |
  waiting | requested | pending; ``conclusion`` success | failure | neutral | cancelled |
  skipped | timed_out | action_required | null. GitHub App permission: Checks (read).
  https://docs.github.com/en/rest/checks/runs#list-check-runs-for-a-git-reference
- job logs (#41): ``GET /repos/{o}/{r}/actions/jobs/{job_id}/logs`` -> 302 with a
  ``Location`` download URL that expires after 1 minute (fetched without our token).
  GitHub App permission: Actions (read).
  https://docs.github.com/en/rest/actions/workflow-jobs#download-job-logs-for-a-workflow-run
  The docs do not say a check run's id is its job's id; the job id is read from the check
  run's ``details_url`` (``.../actions/runs/<run>/job/<job>``), falling back to the check
  run id for the ``github-actions`` app.
- review comments (#42): ``GET /repos/{o}/{r}/pulls/{n}/reviews/{review_id}/comments``
  (paginated list of "Legacy Review Comment": ``id``, ``path``, ``line``,
  ``original_line``, ``start_line``, ``side``, ``position``, ``original_position``,
  ``diff_hunk``, ``body``, ``user``, ``commit_id``, ``subject_type`` line | file).
  GitHub App permission: Pull requests (read), which the App already has.
  https://docs.github.com/en/rest/pulls/reviews#list-comments-for-a-pull-request-review
- mergeable (#43): ``GET /repos/{o}/{r}/pulls/{n}`` has ``mergeable`` true | false | null;
  null means GitHub started a background job to compute it ("after giving the job time to
  complete, resubmit the request"). Read from the ``get_pr`` the poller already makes.
  https://docs.github.com/en/rest/pulls/pulls#get-a-pull-request
- permissions per endpoint:
  https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps
  A 403 that is not a rate limit is raised as ``MissingPermission`` (catchable: an App
  created before #41 lacks Checks/Actions read).

Added in wave 15 (#24 / todo 47, rule 12, read 2026-11-25):
- merge commit: ``GET /repos/{o}/{r}/pulls/{n}`` ``merge_commit_sha``. After a merge it is
  the merge commit; after a squash, "the SHA of the squashed commit on the base branch";
  after a rebase, "the commit that the base branch was updated to". Before merging it is the
  test merge commit, so it is read only when ``merged``. The schema does not say whether it
  is nullable; a missing or null value is kept as "".
  https://docs.github.com/en/rest/pulls/pulls#get-a-pull-request
- commits: ``GET /repos/{o}/{r}/commits`` with ``sha`` ("SHA or branch to start listing
  commits from", default the default branch) and ``since`` (ISO 8601
  ``YYYY-MM-DDTHH:MM:SSZ``); ``per_page`` max 100. Items have ``sha``, ``commit.message``,
  ``commit.committer.date`` and ``parents[].sha``. Pagination follows the ``Link`` header
  as for every other list here. https://docs.github.com/en/rest/commits/commits#list-commits

Added for ab:pair (#16 / todo 34, rule 12, read 2026-11-26, same API version):
- close a PR: "Update a pull request", ``PATCH /repos/{o}/{r}/pulls/{pull_number}``
  ``{"state": "closed"}`` (``state`` is ``open`` | ``closed``) -> 200 with the PR; 403,
  422 on validation failure or spam.
  https://docs.github.com/en/rest/pulls/pulls#update-a-pull-request
- delete a branch: "Delete a reference", ``DELETE /repos/{o}/{r}/git/refs/{ref}`` with
  ``ref`` = ``heads/<branch>`` -> 204; 409 conflict; 422 on validation failure (e.g. the
  default branch). Only ``fman/`` branches are ever deleted (invariant 7a); a ref that is
  already gone is not swallowed (the caller decides).
  https://docs.github.com/en/rest/git/refs#delete-a-reference
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote

import httpx

from .app_auth import (
    API_URL,
    USER_AGENT,
    AppAuth,
    Clock,
    ForbiddenOperation,
    GitHubError,
    GitHubNotFound,
    GitHubRateLimited,
    InstallationAuth,
    Sleep,
    default_headers,
    raise_for_status,
    send_with_retry,
)

BRANCH_PREFIX = "fman/"
#: Characters of a job log kept by ``get_job_log_tail`` (the prompt builder clips further).
LOG_TAIL_CHARS = 16_000


class MissingPermission(GitHubError):
    """403 on an endpoint the App has no permission for (e.g. ``checks`` or ``actions``
    on an App registered before #41). Never a rate limit (those are ``GitHubRateLimited``)."""

    def __init__(self, permission: str, err: GitHubError) -> None:
        super().__init__(err.status, err.message, err.method, err.url)
        self.permission = permission


# --------------------------------------------------------------------------- dataclasses


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    state: str  # open | closed
    state_reason: str | None  # completed | not_planned | duplicate | reopened | None
    labels: tuple[str, ...]
    author: str | None
    is_pull_request: bool
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None


@dataclass(frozen=True)
class IssueEvent:
    id: int
    event: str  # labeled | unlabeled | closed | reopened | merged | ready_for_review | ...
    actor: str | None
    label: str | None  # set for labeled / unlabeled
    created_at: datetime


@dataclass(frozen=True)
class Comment:
    id: int
    body: str
    author: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Ref:
    ref: str  # refs/heads/<branch>
    sha: str


@dataclass(frozen=True)
class PullRequest:
    number: int
    node_id: str
    title: str
    body: str
    state: str  # open | closed
    draft: bool
    merged: bool
    merged_at: datetime | None
    head_ref: str
    base_ref: str
    author: str | None
    html_url: str
    head_sha: str = ""  # the PR head commit (``head.sha``); CI runs on it (#41)
    #: #43: True | False (conflicts with its base) | None (GitHub is still computing it)
    mergeable: bool | None = None
    #: #24: the commit the merge put on the base branch ("" until merged; see module doc)
    merge_commit_sha: str = ""


@dataclass(frozen=True)
class Commit:
    """One commit from the list-commits endpoint (#24)."""

    sha: str
    message: str
    committed_at: datetime | None
    parents: tuple[str, ...] = ()


@dataclass(frozen=True)
class CheckRun:
    """One check run on a commit (#41)."""

    id: int
    name: str
    status: str  # queued | in_progress | completed | waiting | requested | pending
    conclusion: str | None  # success | failure | neutral | cancelled | skipped | timed_out | ...
    head_sha: str
    app_slug: str | None  # "github-actions" for Actions jobs
    details_url: str
    started_at: datetime | None
    completed_at: datetime | None
    job_id: int | None  # the Actions job behind it (for its log); None for other apps


@dataclass(frozen=True)
class ReviewComment:
    """One line comment inside a PR review (#42)."""

    id: int
    review_id: int | None
    author: str | None
    path: str
    line: int | None  # ``line``, else ``original_line``; None for a file-level comment
    start_line: int | None
    diff_hunk: str
    body: str
    commit_id: str | None
    created_at: datetime | None


@dataclass(frozen=True)
class Review:
    id: int
    author: str | None
    state: str  # APPROVED | CHANGES_REQUESTED | COMMENTED | DISMISSED | PENDING
    body: str
    submitted_at: datetime | None
    commit_id: str | None


# --------------------------------------------------------------------------- protocol


@runtime_checkable
class GitHubAPI(Protocol):
    """Everything the worker may do on GitHub. Implemented by GitHubClient and FakeGitHub.

    ``repo`` is always ``"owner/name"``.
    """

    def token(self) -> str: ...
    def bot_login(self) -> str: ...
    def get_default_branch(self, repo: str) -> str: ...

    def list_issues(
        self,
        repo: str,
        *,
        label: str | None = None,
        state: str = "open",
        since: datetime | None = None,
    ) -> list[Issue]: ...
    def get_issue(self, repo: str, number: int) -> Issue: ...
    def list_issue_events(self, repo: str, number: int) -> list[IssueEvent]: ...
    def list_issue_comments(
        self, repo: str, number: int, *, since: datetime | None = None
    ) -> list[Comment]: ...
    def post_comment(self, repo: str, number: int, body: str) -> Comment: ...
    def update_comment(self, repo: str, comment_id: int, body: str) -> Comment: ...
    def add_labels(self, repo: str, number: int, labels: Sequence[str]) -> list[str]: ...
    def remove_label(self, repo: str, number: int, label: str) -> list[str]: ...
    def close_issue(self, repo: str, number: int, *, reason: str = "completed") -> Issue: ...

    def get_branch_sha(self, repo: str, branch: str) -> str: ...
    def create_branch(self, repo: str, branch: str, sha: str) -> Ref: ...
    def update_branch(self, repo: str, branch: str, sha: str, *, force: bool = False) -> Ref: ...
    def delete_branch(self, repo: str, branch: str) -> None: ...

    def create_draft_pr(
        self, repo: str, *, head: str, base: str, title: str, body: str
    ) -> PullRequest: ...
    def update_pr(
        self, repo: str, number: int, *, title: str | None = None, body: str | None = None
    ) -> PullRequest: ...
    def get_pr(self, repo: str, number: int) -> PullRequest: ...
    def close_pr(self, repo: str, number: int) -> PullRequest: ...
    def find_pr_for_branch(self, repo: str, branch: str) -> PullRequest | None: ...
    def mark_ready_for_review(self, repo: str, number: int) -> PullRequest: ...
    def post_review(
        self, repo: str, number: int, body: str, *, commit_id: str | None = None
    ) -> Review: ...
    def list_reviews(self, repo: str, number: int) -> list[Review]: ...

    def list_review_comments(
        self, repo: str, pr_number: int, review_id: int
    ) -> list[ReviewComment]: ...

    def list_check_runs(self, repo: str, sha: str) -> list[CheckRun]: ...
    def list_commits(
        self, repo: str, *, sha: str | None = None, since: datetime | None = None
    ) -> list[Commit]: ...
    def get_job_log_tail(
        self, repo: str, job_id: int, *, max_chars: int = LOG_TAIL_CHARS
    ) -> str: ...


#: Client-facing methods that change GitHub state. The fake records each call to one of these.
WRITE_METHODS = frozenset(
    {
        "post_comment",
        "update_comment",
        "add_labels",
        "remove_label",
        "close_issue",
        "create_branch",
        "update_branch",
        "delete_branch",
        "create_draft_pr",
        "update_pr",
        "close_pr",
        "mark_ready_for_review",
        "post_review",
    }
)


# --------------------------------------------------------------------------- helpers


def label_applied_by(events: Iterable[IssueEvent], label: str) -> str | None:
    """Who applied ``label`` most recently (invariant 7b), or None if it is not applied now.

    Label names compare case-insensitively, as GitHub does.
    """
    actor: str | None = None
    for ev in sorted(events, key=lambda e: (e.created_at, e.id)):
        if ev.label is None or ev.label.lower() != label.lower():
            continue
        if ev.event == "labeled":
            actor = ev.actor
        elif ev.event == "unlabeled":
            actor = None
    return actor


def require_fman_branch(branch: str) -> None:
    if not branch.startswith(BRANCH_PREFIX) or ".." in branch:
        raise ForbiddenOperation(f"branch {branch!r} is not a worker branch ({BRANCH_PREFIX}*)")


def _split(repo: str) -> tuple[str, str]:
    owner, sep, name = repo.partition("/")
    if not sep or not owner or not name or "/" in name:
        raise ValueError(f"repo must be 'owner/name', got {repo!r}")
    return owner, name


def _ts(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _login(obj: dict[str, Any] | None) -> str | None:
    return None if not obj else obj.get("login")


def issue_from_json(d: dict[str, Any]) -> Issue:
    labels = tuple(lb["name"] if isinstance(lb, dict) else str(lb) for lb in d.get("labels", []))
    return Issue(
        number=int(d["number"]),
        title=d.get("title") or "",
        body=d.get("body") or "",
        state=d["state"],
        state_reason=d.get("state_reason"),
        labels=labels,
        author=_login(d.get("user")),
        is_pull_request="pull_request" in d and d["pull_request"] is not None,
        created_at=_ts(d["created_at"]),  # type: ignore[arg-type]
        updated_at=_ts(d["updated_at"]),  # type: ignore[arg-type]
        closed_at=_ts(d.get("closed_at")),
    )


def event_from_json(d: dict[str, Any]) -> IssueEvent:
    label = d.get("label")
    return IssueEvent(
        id=int(d["id"]),
        event=d["event"],
        actor=_login(d.get("actor")),
        label=label["name"] if isinstance(label, dict) else None,
        created_at=_ts(d["created_at"]),  # type: ignore[arg-type]
    )


def comment_from_json(d: dict[str, Any]) -> Comment:
    return Comment(
        id=int(d["id"]),
        body=d.get("body") or "",
        author=_login(d.get("user")),
        created_at=_ts(d["created_at"]),  # type: ignore[arg-type]
        updated_at=_ts(d["updated_at"]),  # type: ignore[arg-type]
    )


def ref_from_json(d: dict[str, Any]) -> Ref:
    return Ref(ref=d["ref"], sha=d["object"]["sha"])


def pr_from_json(d: dict[str, Any]) -> PullRequest:
    merged_at = _ts(d.get("merged_at"))
    return PullRequest(
        number=int(d["number"]),
        node_id=d["node_id"],
        title=d.get("title") or "",
        body=d.get("body") or "",
        state=d["state"],
        draft=bool(d.get("draft", False)),
        merged=bool(d["merged"]) if "merged" in d else merged_at is not None,
        merged_at=merged_at,
        head_ref=d["head"]["ref"],
        base_ref=d["base"]["ref"],
        author=_login(d.get("user")),
        html_url=d.get("html_url") or "",
        head_sha=(d.get("head") or {}).get("sha") or "",
        mergeable=d.get("mergeable") if isinstance(d.get("mergeable"), bool) else None,
        merge_commit_sha=(d.get("merge_commit_sha") or "") if merged_at is not None else "",
    )


def commit_from_json(d: dict[str, Any]) -> Commit:
    inner = d.get("commit") or {}
    committer = inner.get("committer") or {}
    return Commit(
        sha=d["sha"],
        message=inner.get("message") or "",
        committed_at=_ts(committer.get("date")),
        parents=tuple(p["sha"] for p in d.get("parents") or () if p.get("sha")),
    )


_JOB_URL = re.compile(r"/actions/runs/\d+/job/(\d+)")


def check_run_from_json(d: dict[str, Any]) -> CheckRun:
    app = d.get("app") or {}
    slug = app.get("slug") if isinstance(app, dict) else None
    details = d.get("details_url") or d.get("html_url") or ""
    m = _JOB_URL.search(details)
    job_id = int(m.group(1)) if m else (int(d["id"]) if slug == "github-actions" else None)
    return CheckRun(
        id=int(d["id"]),
        name=d.get("name") or "",
        status=d.get("status") or "queued",
        conclusion=d.get("conclusion"),
        head_sha=d.get("head_sha") or "",
        app_slug=slug,
        details_url=details,
        started_at=_ts(d.get("started_at")),
        completed_at=_ts(d.get("completed_at")),
        job_id=job_id,
    )


_LOG_STAMP = re.compile(r"^\ufeff?\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z ", re.MULTILINE)


def log_tail(text: str, max_chars: int) -> str:
    """The last ``max_chars`` characters of an Actions log, without per-line timestamps."""
    text = _LOG_STAMP.sub("", text.replace("\r\n", "\n"))
    return text[-max_chars:] if len(text) > max_chars else text


def _as_missing(permission: str, exc: GitHubError) -> GitHubError:
    if exc.status == 403 and not isinstance(exc, GitHubRateLimited):
        return MissingPermission(permission, exc)
    return exc


def review_from_json(d: dict[str, Any]) -> Review:
    return Review(
        id=int(d["id"]),
        author=_login(d.get("user")),
        state=d["state"],
        body=d.get("body") or "",
        submitted_at=_ts(d.get("submitted_at")),
        commit_id=d.get("commit_id"),
    )


def review_comment_from_json(d: dict[str, Any]) -> ReviewComment:
    line = d.get("line")
    if line is None:
        line = d.get("original_line")
    if d.get("subject_type") == "file":
        line = None
    return ReviewComment(
        id=int(d["id"]),
        review_id=d.get("pull_request_review_id"),
        author=_login(d.get("user")),
        path=d.get("path") or "",
        line=int(line) if line is not None else None,
        start_line=d.get("start_line"),
        diff_hunk=d.get("diff_hunk") or "",
        body=d.get("body") or "",
        commit_id=d.get("commit_id"),
        created_at=_ts(d.get("created_at")),
    )


_NEXT_LINK = re.compile(r'<([^>]+)>;\s*rel="next"')

MARK_READY_MUTATION = """
mutation($id: ID!) {
  markPullRequestReadyForReview(input: {pullRequestId: $id}) {
    pullRequest { number isDraft }
  }
}
""".strip()


# --------------------------------------------------------------------------- real client


class GitHubClient:
    """REST/GraphQL client acting as one App installation. Implements ``GitHubAPI``."""

    def __init__(
        self,
        auth: InstallationAuth,
        *,
        http: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        base_url: str = API_URL,
        clock: Clock = time.time,
        sleep: Sleep = time.sleep,
        bot_login: str | None = None,
    ) -> None:
        self.auth = auth
        self._http = http or httpx.Client(transport=transport, timeout=30.0)
        self.base_url = base_url.rstrip("/")
        self.clock = clock
        self.sleep = sleep
        self._bot_login = bot_login

    @classmethod
    def for_installation(
        cls,
        app_id: int | str,
        private_key: str | Path,
        installation_id: int,
        *,
        transport: httpx.BaseTransport | None = None,
        base_url: str = API_URL,
        clock: Clock = time.time,
        sleep: Sleep = time.sleep,
        bot_login: str | None = None,
    ) -> GitHubClient:
        http = httpx.Client(transport=transport, timeout=30.0)
        app = AppAuth(app_id, private_key, http=http, base_url=base_url, clock=clock, sleep=sleep)
        auth = InstallationAuth(app, installation_id)
        return cls(
            auth, http=http, base_url=base_url, clock=clock, sleep=sleep, bot_login=bot_login
        )

    # -- plumbing

    def _send(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        idempotent: bool | None = None,
    ) -> httpx.Response:
        if not url.startswith("http"):
            url = self.base_url + url
        for retry_auth in (True, False):
            headers = default_headers() | {"Authorization": f"Bearer {self.auth.token()}"}
            resp = send_with_retry(
                self._http,
                method,
                url,
                headers=headers,
                json=json,
                params=params,
                clock=self.clock,
                sleep=self.sleep,
                idempotent=idempotent,
            )
            if resp.status_code == 401 and retry_auth:
                self.auth.invalidate()  # revoked or expired early: mint a new token once
                continue
            return resp
        raise AssertionError("unreachable")  # pragma: no cover

    def _json(self, method: str, path: str, **kw: Any) -> Any:
        resp = self._send(method, path, **kw)
        raise_for_status(resp)
        return resp.json() if resp.content else None

    def _paginate(self, path: str, params: dict[str, Any] | None = None) -> list[Any]:
        params = {"per_page": 100, **(params or {})}
        out: list[Any] = []
        url: str | None = path
        while url is not None:
            resp = self._send("GET", url, params=params)
            raise_for_status(resp)
            out.extend(resp.json())
            match = _NEXT_LINK.search(resp.headers.get("link", ""))
            url, params = (match.group(1), None) if match else (None, None)
        return out

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        data = self._json(
            "POST", "/graphql", json={"query": query, "variables": variables}, idempotent=True
        )
        if data.get("errors"):
            msg = "; ".join(str(e.get("message")) for e in data["errors"])
            raise GitHubError(200, msg, "POST", self.base_url + "/graphql")
        return data["data"]

    # -- identity

    def token(self) -> str:
        """The current installation token (for ``git push`` over HTTPS)."""
        return self.auth.token()

    def bot_login(self) -> str:
        if self._bot_login is None:
            self._bot_login = f"{self.auth.app.app_slug()}[bot]"
        return self._bot_login

    def get_default_branch(self, repo: str) -> str:
        o, r = _split(repo)
        return str(self._json("GET", f"/repos/{o}/{r}")["default_branch"])

    # -- issues

    def list_issues(
        self,
        repo: str,
        *,
        label: str | None = None,
        state: str = "open",
        since: datetime | None = None,
    ) -> list[Issue]:
        o, r = _split(repo)
        params: dict[str, Any] = {"state": state}
        if label is not None:
            params["labels"] = label
        if since is not None:
            params["since"] = _utc_iso(since)
        rows = self._paginate(f"/repos/{o}/{r}/issues", params)
        return [issue_from_json(d) for d in rows if "pull_request" not in d]

    def get_issue(self, repo: str, number: int) -> Issue:
        o, r = _split(repo)
        return issue_from_json(self._json("GET", f"/repos/{o}/{r}/issues/{number}"))

    def list_issue_events(self, repo: str, number: int) -> list[IssueEvent]:
        o, r = _split(repo)
        return [
            event_from_json(d) for d in self._paginate(f"/repos/{o}/{r}/issues/{number}/events")
        ]

    def list_issue_comments(
        self, repo: str, number: int, *, since: datetime | None = None
    ) -> list[Comment]:
        o, r = _split(repo)
        params = {"since": _utc_iso(since)} if since is not None else None
        rows = self._paginate(f"/repos/{o}/{r}/issues/{number}/comments", params)
        return [comment_from_json(d) for d in rows]

    def post_comment(self, repo: str, number: int, body: str) -> Comment:
        o, r = _split(repo)
        data = self._json("POST", f"/repos/{o}/{r}/issues/{number}/comments", json={"body": body})
        return comment_from_json(data)

    def update_comment(self, repo: str, comment_id: int, body: str) -> Comment:
        """Replace the body of an issue comment (#77): ``PATCH .../issues/comments/{id}``."""
        o, r = _split(repo)
        data = self._json(
            "PATCH", f"/repos/{o}/{r}/issues/comments/{int(comment_id)}", json={"body": body}
        )
        return comment_from_json(data)

    def add_labels(self, repo: str, number: int, labels: Sequence[str]) -> list[str]:
        o, r = _split(repo)
        data = self._json(
            "POST", f"/repos/{o}/{r}/issues/{number}/labels", json={"labels": list(labels)}
        )
        return [lb["name"] for lb in data]

    def remove_label(self, repo: str, number: int, label: str) -> list[str]:
        """Idempotent: removing a label that is not there returns the current labels."""
        o, r = _split(repo)
        try:
            data = self._json(
                "DELETE", f"/repos/{o}/{r}/issues/{number}/labels/{quote(label, safe='')}"
            )
        except GitHubNotFound:
            return list(self.get_issue(repo, number).labels)
        return [lb["name"] for lb in data]

    def close_issue(self, repo: str, number: int, *, reason: str = "completed") -> Issue:
        o, r = _split(repo)
        data = self._json(
            "PATCH",
            f"/repos/{o}/{r}/issues/{number}",
            json={"state": "closed", "state_reason": reason},
        )
        return issue_from_json(data)

    # -- branches

    def get_branch_sha(self, repo: str, branch: str) -> str:
        o, r = _split(repo)
        return ref_from_json(self._json("GET", f"/repos/{o}/{r}/git/ref/heads/{branch}")).sha

    def create_branch(self, repo: str, branch: str, sha: str) -> Ref:
        require_fman_branch(branch)
        o, r = _split(repo)
        data = self._json(
            "POST", f"/repos/{o}/{r}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha}
        )
        return ref_from_json(data)

    def update_branch(self, repo: str, branch: str, sha: str, *, force: bool = False) -> Ref:
        require_fman_branch(branch)
        o, r = _split(repo)
        data = self._json(
            "PATCH", f"/repos/{o}/{r}/git/refs/heads/{branch}", json={"sha": sha, "force": force}
        )
        return ref_from_json(data)

    def delete_branch(self, repo: str, branch: str) -> None:
        """ab:pair (#16): delete the losing side's head branch. Refuses anything not under
        ``fman/`` (invariant 7a) before any request."""
        require_fman_branch(branch)
        if branch.rstrip("/") + "/" == BRANCH_PREFIX:
            raise ForbiddenOperation(f"branch {branch!r} names no worker branch")
        o, r = _split(repo)
        self._json("DELETE", f"/repos/{o}/{r}/git/refs/heads/{branch}")

    # -- pull requests

    def create_draft_pr(
        self, repo: str, *, head: str, base: str, title: str, body: str
    ) -> PullRequest:
        require_fman_branch(head)
        o, r = _split(repo)
        data = self._json(
            "POST",
            f"/repos/{o}/{r}/pulls",
            json={"title": title, "head": head, "base": base, "body": body, "draft": True},
        )
        return pr_from_json(data)

    def update_pr(
        self, repo: str, number: int, *, title: str | None = None, body: str | None = None
    ) -> PullRequest:
        o, r = _split(repo)
        payload = {k: v for k, v in (("title", title), ("body", body)) if v is not None}
        return pr_from_json(self._json("PATCH", f"/repos/{o}/{r}/pulls/{number}", json=payload))

    def get_pr(self, repo: str, number: int) -> PullRequest:
        o, r = _split(repo)
        return pr_from_json(self._json("GET", f"/repos/{o}/{r}/pulls/{number}"))

    def close_pr(self, repo: str, number: int) -> PullRequest:
        """ab:pair (#16): close the losing side's PR (never merges: there is no merge method)."""
        o, r = _split(repo)
        data = self._json("PATCH", f"/repos/{o}/{r}/pulls/{number}", json={"state": "closed"})
        return pr_from_json(data)

    def find_pr_for_branch(self, repo: str, branch: str) -> PullRequest | None:
        o, r = _split(repo)
        rows = self._paginate(f"/repos/{o}/{r}/pulls", {"head": f"{o}:{branch}", "state": "all"})
        prs = [pr_from_json(d) for d in rows if d["head"]["ref"] == branch]
        return max(prs, key=lambda p: p.number) if prs else None

    def mark_ready_for_review(self, repo: str, number: int) -> PullRequest:
        """Draft -> ready. GraphQL only. Callers: only after a reviewer approve or handover."""
        pr = self.get_pr(repo, number)
        if not pr.draft:
            return pr
        data = self._graphql(MARK_READY_MUTATION, {"id": pr.node_id})
        is_draft = bool(data["markPullRequestReadyForReview"]["pullRequest"]["isDraft"])
        return replace(pr, draft=is_draft)

    def post_review(
        self, repo: str, number: int, body: str, *, commit_id: str | None = None
    ) -> Review:
        """Post the bot's review. Always a COMMENT review: the bot never approves."""
        o, r = _split(repo)
        payload: dict[str, Any] = {"event": "COMMENT", "body": body}
        if commit_id is not None:
            payload["commit_id"] = commit_id
        data = self._json("POST", f"/repos/{o}/{r}/pulls/{number}/reviews", json=payload)
        return review_from_json(data)

    def list_reviews(self, repo: str, number: int) -> list[Review]:
        o, r = _split(repo)
        return [
            review_from_json(d) for d in self._paginate(f"/repos/{o}/{r}/pulls/{number}/reviews")
        ]

    def list_review_comments(
        self, repo: str, pr_number: int, review_id: int
    ) -> list[ReviewComment]:
        """The line comments of one review (#42), in GitHub's order."""
        o, r = _split(repo)
        rows = self._paginate(f"/repos/{o}/{r}/pulls/{pr_number}/reviews/{review_id}/comments")
        return [review_comment_from_json(d) for d in rows]

    # -- CI (#41)

    def list_check_runs(self, repo: str, sha: str) -> list[CheckRun]:
        """Latest check runs on commit ``sha``. ``MissingPermission("checks")`` on a 403."""
        o, r = _split(repo)
        params: dict[str, Any] | None = {"per_page": 100}
        url: str | None = f"/repos/{o}/{r}/commits/{sha}/check-runs"
        out: list[CheckRun] = []
        try:
            while url is not None:
                resp = self._send("GET", url, params=params)
                raise_for_status(resp)
                out.extend(check_run_from_json(d) for d in resp.json().get("check_runs", []))
                match = _NEXT_LINK.search(resp.headers.get("link", ""))
                url, params = (match.group(1), None) if match else (None, None)
        except GitHubError as exc:
            raise _as_missing("checks", exc) from None
        return out

    # -- commits (#24)

    def list_commits(
        self, repo: str, *, sha: str | None = None, since: datetime | None = None
    ) -> list[Commit]:
        """Commits reachable from ``sha`` (a branch or commit; default branch if None),
        newest first, only those after ``since`` when given."""
        o, r = _split(repo)
        params: dict[str, Any] = {}
        if sha is not None:
            params["sha"] = sha
        if since is not None:
            params["since"] = _utc_iso(since)
        return [commit_from_json(d) for d in self._paginate(f"/repos/{o}/{r}/commits", params)]

    def get_job_log_tail(self, repo: str, job_id: int, *, max_chars: int = LOG_TAIL_CHARS) -> str:
        """The end of an Actions job's log. GitHub answers with a 302 to a short-lived
        download URL, fetched without our token. ``MissingPermission("actions")`` on a 403;
        ``GitHubNotFound`` when the log is gone."""
        o, r = _split(repo)
        resp = self._send("GET", f"/repos/{o}/{r}/actions/jobs/{int(job_id)}/logs")
        if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
            tail = ""
            with self._http.stream(
                "GET", resp.headers["location"], headers={"User-Agent": USER_AGENT}
            ) as dl:
                if dl.status_code >= 400:
                    dl.read()
                    raise_for_status(dl)
                for chunk in dl.iter_text():
                    tail = (tail + chunk)[-(max_chars * 2) :]
            return log_tail(tail, max_chars)
        try:
            raise_for_status(resp)
        except GitHubError as exc:
            raise _as_missing("actions", exc) from None
        return log_tail(resp.text, max_chars)

    def close(self) -> None:
        self._http.close()


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
