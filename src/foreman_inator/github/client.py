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
    AppAuth,
    Clock,
    ForbiddenOperation,
    GitHubError,
    GitHubNotFound,
    InstallationAuth,
    Sleep,
    default_headers,
    raise_for_status,
    send_with_retry,
)

BRANCH_PREFIX = "fman/"


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
    def add_labels(self, repo: str, number: int, labels: Sequence[str]) -> list[str]: ...
    def remove_label(self, repo: str, number: int, label: str) -> list[str]: ...
    def close_issue(self, repo: str, number: int, *, reason: str = "completed") -> Issue: ...

    def get_branch_sha(self, repo: str, branch: str) -> str: ...
    def create_branch(self, repo: str, branch: str, sha: str) -> Ref: ...
    def update_branch(self, repo: str, branch: str, sha: str, *, force: bool = False) -> Ref: ...

    def create_draft_pr(
        self, repo: str, *, head: str, base: str, title: str, body: str
    ) -> PullRequest: ...
    def update_pr(
        self, repo: str, number: int, *, title: str | None = None, body: str | None = None
    ) -> PullRequest: ...
    def get_pr(self, repo: str, number: int) -> PullRequest: ...
    def find_pr_for_branch(self, repo: str, branch: str) -> PullRequest | None: ...
    def mark_ready_for_review(self, repo: str, number: int) -> PullRequest: ...
    def post_review(
        self, repo: str, number: int, body: str, *, commit_id: str | None = None
    ) -> Review: ...
    def list_reviews(self, repo: str, number: int) -> list[Review]: ...


#: Client-facing methods that change GitHub state. The fake records each call to one of these.
WRITE_METHODS = frozenset(
    {
        "post_comment",
        "add_labels",
        "remove_label",
        "close_issue",
        "create_branch",
        "update_branch",
        "create_draft_pr",
        "update_pr",
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
    )


def review_from_json(d: dict[str, Any]) -> Review:
    return Review(
        id=int(d["id"]),
        author=_login(d.get("user")),
        state=d["state"],
        body=d.get("body") or "",
        submitted_at=_ts(d.get("submitted_at")),
        commit_id=d.get("commit_id"),
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

    def close(self) -> None:
        self._http.close()


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
