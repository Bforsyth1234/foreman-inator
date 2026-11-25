"""#77 / todo 81: one status comment on the issue, posted once and edited in place."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from fakes.github import BOT_LOGIN, FakeGitHub
from foreman_inator.github import GitHubError
from foreman_inator.tickets import clarify, prompt
from foreman_inator.worker_status_comment import (
    WORKING_LABEL,
    StatusCommentSteps,
    render_status,
    status_marker,
)

REPO = "octo/widgets"
STARTED = datetime(2026, 11, 25, 14, 5, tzinfo=UTC)


class _Settings:
    def __init__(self, rows: dict[str, Any] | None = None) -> None:
        self.rows = rows or {}

    def get_repo_settings(self, repo: str) -> Any:
        return self.rows.get(repo)


class W(StatusCommentSteps):
    def __init__(self, api: Any, store: Any = None, **cfg: Any) -> None:
        self.api = api
        self.store = store or _Settings()
        self.config = SimpleNamespace(**cfg)


@pytest.fixture
def gh() -> FakeGitHub:
    g = FakeGitHub()
    g.create_repo(REPO)
    g.create_issue(REPO, "Add a flag", "please", labels=["agent"])
    return g


def _ticket(tid: int = 7, number: int = 1) -> Any:
    return SimpleNamespace(id=tid, repo=REPO, issue_number=number)


def _status_comments(gh: FakeGitHub, tid: int = 7) -> list[Any]:
    marker = status_marker(tid)
    return [c for c in gh.list_issue_comments(REPO, 1) if marker in c.body]


def test_first_start_posts_one_then_escalation_and_pr_edit_it(gh: FakeGitHub) -> None:
    w, t = W(gh), _ticket()
    w.status_update(t, "attempt_started", attempt=1, tier=1, model="qwen3", started_at=STARTED)
    (c,) = _status_comments(gh)
    assert "attempt 1 on tier 1 (qwen3), started 14:05 UTC" in c.body
    w.status_update(t, "escalated", attempt=2, tier=2, model="sonnet", started_at=STARTED)
    w.status_update(t, "pr_opened", pr_number=12)
    comments = gh.list_issue_comments(REPO, 1)
    assert len(comments) == 1 and comments[0].id == c.id
    assert "opened PR #12" in comments[0].body
    assert [x.method for x in gh.writes] == ["post_comment", "update_comment", "update_comment"]


def test_same_state_twice_makes_no_second_edit(gh: FakeGitHub) -> None:
    w, t = W(gh), _ticket()
    for _ in range(3):
        w.status_update(t, "attempt_started", attempt=1, tier=1, model="m", started_at=STARTED)
    assert len(gh.writes) == 1


def test_restart_finds_comment_by_marker_and_edits(gh: FakeGitHub) -> None:
    t = _ticket()
    W(gh).status_update(t, "attempt_started", attempt=1, tier=1, model="m", started_at=STARTED)
    gh.comment_as(REPO, 1, f"quoting {status_marker(7)}", by="brooks")  # a user's is ignored
    restarted = W(gh)
    restarted.status_update(t, "escalated", attempt=2, tier=2, model="m2", started_at=STARTED)
    assert len(_status_comments(gh)) == 2  # the bot's one + the user's quote, no new bot post
    assert [x.method for x in gh.writes] == ["post_comment", "update_comment"]
    bot = [c for c in _status_comments(gh) if c.author == BOT_LOGIN]
    assert len(bot) == 1 and "escalated" in bot[0].body


def test_restart_with_unchanged_state_writes_nothing(gh: FakeGitHub) -> None:
    t = _ticket()
    W(gh).status_update(t, "verifying", attempt=1, tier=1, model="m")
    W(gh).status_update(t, "verifying", attempt=1, tier=1, model="m")
    assert len(gh.writes) == 1


def test_off_posts_nothing(gh: FakeGitHub) -> None:
    W(gh, status_comment=False).status_update(_ticket(), "attempt_started", attempt=1, tier=1)
    off_repo = _Settings({REPO: SimpleNamespace(status_comment=False)})
    W(gh, store=off_repo).status_update(_ticket(), "attempt_started", attempt=1, tier=1)
    assert gh.writes == []


def test_failure_is_logged_never_raised(gh: FakeGitHub, caplog: pytest.LogCaptureFixture) -> None:
    gh.fail_next("post_comment", GitHubError(502, "Bad Gateway"))
    w, t = W(gh), _ticket()
    with caplog.at_level(logging.WARNING, "foreman_inator.worker"):
        w.status_update(t, "attempt_started", attempt=1, tier=1)
        w.status_update(t, "bogus_state")
    assert "status comment for ticket 7 failed" in caplog.text
    assert "skipped" in caplog.text
    w.status_update(t, "attempt_started", attempt=1, tier=1)  # retried on the next change
    assert len(_status_comments(gh)) == 1


def test_deleted_comment_gets_reposted(gh: FakeGitHub) -> None:
    w, t = W(gh), _ticket()
    w.status_update(t, "attempt_started", attempt=1, tier=1)
    gh.repos[REPO].items[1].comments.clear()
    w.status_update(t, "handed_over", reason="checks_failed: tests red")
    (c,) = _status_comments(gh)
    assert "handed this over: checks_failed: tests red." in c.body


def test_label_behind_flag(gh: FakeGitHub) -> None:
    t = _ticket()
    W(gh).status_update(t, "attempt_started", attempt=1, tier=1)
    assert WORKING_LABEL not in gh.get_issue(REPO, 1).labels
    w = W(gh, status_label=True)
    w.status_update(t, "verifying", attempt=1, tier=1)
    w.status_update(t, "reviewing", attempt=1, tier=1)
    assert WORKING_LABEL in gh.get_issue(REPO, 1).labels
    assert len(gh.writes_named("add_labels")) == 1
    w.status_update(t, "dropped")
    assert WORKING_LABEL not in gh.get_issue(REPO, 1).labels


def test_text_is_the_workers_own_and_cost_is_opt_in() -> None:
    text = render_status("handed_over", reason="<!-- x --> ping @brooks\nsee <b>this</b>")
    assert "<!--" not in text and "@brooks" not in text and "\n" not in text and "<b>" not in text
    assert "$" not in render_status("attempt_started", attempt=1, cost_usd=1.5)
    assert "$1.50" in render_status("attempt_started", attempt=1, cost_usd=1.5, show_cost=True)


def test_uses_worker_gh_span_when_present(gh: FakeGitHub) -> None:
    names: list[str] = []

    class G(W):
        def _gh(self, ticket: Any, name: str, fn: Any) -> Any:
            names.append(name)
            return fn()

    G(gh).status_update(_ticket(), "attempt_started", attempt=1, tier=1)
    assert names == ["status_comment"] and len(gh.writes) == 1


def test_status_comment_is_not_an_answer_nor_in_comments_context(gh: FakeGitHub) -> None:
    allowed = {"brooks"}.__contains__
    asked = gh.post_comment(REPO, 1, clarify.QUESTION_MARKER.format(ticket_id=7) + "\nQ?")
    W(gh).status_update(_ticket(), "attempt_started", attempt=1, tier=1)
    comments = gh.list_issue_comments(REPO, 1)
    assert clarify.answers(comments, asked, allowed) == []
    assert prompt.issue_comments_text(comments, is_allowed=allowed, bot_login=BOT_LOGIN) is None


def test_client_update_comment_patches_the_comment() -> None:
    """Real client against MockTransport: PATCH .../issues/comments/{id} with the body."""
    from foreman_inator.github import GitHubClient

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "id": 55,
                "body": "new",
                "user": {"login": BOT_LOGIN},
                "created_at": "2026-11-25T14:00:00Z",
                "updated_at": "2026-11-25T14:05:00Z",
            },
        )

    auth = SimpleNamespace(token=lambda: "t", invalidate=lambda: None)
    client = GitHubClient(auth, transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    c = client.update_comment(REPO, 55, "new")
    (req,) = seen
    assert req.method == "PATCH"
    assert req.url.path == "/repos/octo/widgets/issues/comments/55"
    assert json.loads(req.content) == {"body": "new"}
    assert c.id == 55 and c.body == "new" and c.author == BOT_LOGIN
