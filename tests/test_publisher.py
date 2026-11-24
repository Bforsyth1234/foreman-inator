"""PR publisher (#12): draft PRs against FakeGitHub, body rendering, invariant 7a."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from fakes.github import BOT_LOGIN, FakeGitHub
from foreman_inator.db import Store
from foreman_inator.pr import (
    HANDOVER_END,
    HANDOVER_START,
    REVIEW_END,
    REVIEW_START,
    Handover,
    Publisher,
    PublishError,
    format_cost,
    pr_head_branch,
    render_body,
)
from foreman_inator.runner.verify import VerifyResult
from foreman_inator.runner.worktree import DiffStats

REPO = "brooks/toy"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "fman.db", clock=lambda: datetime(2026, 11, 23, 9, 0, tzinfo=UTC))
    yield s
    s.close()


@pytest.fixture
def gh() -> FakeGitHub:
    fake = FakeGitHub()
    fake.create_repo(REPO)
    return fake


@pytest.fixture
def pub(gh: FakeGitHub, store: Store) -> Publisher:
    return Publisher(gh, store)


def _ticket(gh: FakeGitHub, store: Store, title: str = "Add a --version flag"):
    issue = gh.create_issue(REPO, title, "please", labels=["agent"])
    return store.create_ticket(
        repo=REPO, issue_number=issue, title=title, request="please", base_branch="main"
    )


def _attempt(store: Store, ticket, *, model="qwen3.6:35b-a3b", tier=1, trigger="first", cost=0.0):
    a = store.add_attempt(ticket.id, model=model, tier=tier, trigger=trigger)
    if cost:
        store.add_attempt_usage(a.id, tokens_in=1000, tokens_out=200, cost_usd=cost)
    return store.finish_attempt(a.id, tests_passed=True, diff_files=2, diff_lines=12)


def _verify(tmp_path: Path, *, passed=True, run=12, failed=0) -> VerifyResult:
    out = tmp_path / "verify.log"
    out.write_text("SECRET_VERIFY_OUTPUT line from the test runner\n")
    return VerifyResult(
        passed=passed,
        tests_run=run,
        tests_failed=failed,
        output_path=out,
        exit_code=0 if passed else 1,
        timed_out=False,
        seconds=3.25,
        command="uv run pytest",
    )


DIFF = DiffStats(files=("src/app.py", "tests/test_app.py"), insertions=10, deletions=2)


# --------------------------------------------------------------------------- publishing


def test_head_branch_is_first_attempts_branch(gh, store):
    t = _ticket(gh, store)
    assert pr_head_branch(t) == f"fman/issue-{t.issue_number}/a1"


def test_first_attempt_opens_draft_second_updates_it(gh, store, pub, tmp_path):
    t = _ticket(gh, store)
    a1 = _attempt(store, t)
    gh.push_branch(REPO, pr_head_branch(t))

    r1 = pub.open_or_update_draft(t, a1, verify=_verify(tmp_path), diff=DIFF)
    assert r1.action == "created"
    assert r1.pr.draft and r1.pr.state == "open"
    assert r1.pr.head_ref == "fman/issue-1/a1" and r1.pr.base_ref == "main"
    assert r1.pr.author == BOT_LOGIN
    assert store.get_ticket(t.id).pr_number == r1.pr.number
    body1 = gh.get_pr(REPO, r1.pr.number).body
    assert f"Closes #{t.issue_number}" in body1
    assert "`qwen3.6:35b-a3b`" in body1 and "| 1 of 1 |" in body1 and "$0.00" in body1

    # Attempt 2 escalates to a cloud model; the worker force-pushes onto the same head.
    store.set_attempt_outcome(a1.id, "failed_checks")
    a2 = _attempt(store, t, model="glm-4.6", tier=3, trigger="escalation", cost=0.0312)
    gh.push_branch(REPO, pr_head_branch(t))
    r2 = pub.open_or_update_draft(t, a2, verify=_verify(tmp_path), diff=DIFF)

    assert r2.action == "updated"
    assert r2.pr.number == r1.pr.number and r2.pr.draft
    assert len(gh.writes_named("create_draft_pr")) == 1
    assert len(gh.writes_named("update_pr")) == 1
    body2 = gh.get_pr(REPO, r1.pr.number).body
    assert "| 2 of 2 | `glm-4.6` | 3 | escalation | $0.03 |" in body2
    assert "| 1 | `qwen3.6:35b-a3b` | 1 | first | failed checks | $0.00 |" in body2
    assert "| 2 | `glm-4.6` | 3 | escalation | this attempt | $0.03 |" in body2
    assert "Total spend on this ticket: $0.03 over 2 attempts." in body2


def test_publish_never_marks_ready(gh, store, pub, tmp_path):
    """Invariant 7a: no publisher path marks the PR ready on its own."""
    t = _ticket(gh, store)
    a1 = _attempt(store, t)
    gh.push_branch(REPO, pr_head_branch(t))
    pub.open_or_update_draft(t, a1, verify=_verify(tmp_path), diff=DIFF)
    pub.open_or_update_draft(t, a1, verify=_verify(tmp_path), diff=DIFF)  # unchanged
    a2 = _attempt(store, t, trigger="kickback")
    pub.open_or_update_draft(t, a2, verify=_verify(tmp_path, passed=False, failed=1))
    review = store.record_review(a2.id, reviewer_model="claude-sonnet-4-5", verdict="approve")
    r = pub.open_or_update_draft(t, a2, review=review, handover=Handover(3, "loop cap"))

    assert gh.writes_named("mark_ready_for_review") == []
    assert gh.get_pr(REPO, r.pr.number).draft is True


def test_republish_same_state_writes_nothing(gh, store, pub, tmp_path):
    t = _ticket(gh, store)
    a1 = _attempt(store, t)
    gh.push_branch(REPO, pr_head_branch(t))
    pub.open_or_update_draft(t, a1, verify=_verify(tmp_path), diff=DIFF)
    before = list(gh.writes)
    r = pub.open_or_update_draft(t, a1, verify=_verify(tmp_path), diff=DIFF)
    assert r.action == "unchanged"
    assert gh.writes == before


def test_crash_between_create_and_save_adopts_the_pr(gh, store, pub, tmp_path, monkeypatch):
    t = _ticket(gh, store)
    a1 = _attempt(store, t)
    gh.push_branch(REPO, pr_head_branch(t))

    real_update = store.update_ticket

    def crash(*args, **kwargs):
        raise RuntimeError("worker died")

    monkeypatch.setattr(store, "update_ticket", crash)
    with pytest.raises(RuntimeError):
        pub.open_or_update_draft(t, a1, verify=_verify(tmp_path), diff=DIFF)
    monkeypatch.setattr(store, "update_ticket", real_update)
    assert store.get_ticket(t.id).pr_number is None
    assert len(gh.writes_named("create_draft_pr")) == 1

    r = pub.open_or_update_draft(t, a1, verify=_verify(tmp_path), diff=DIFF)
    assert r.action == "unchanged"
    assert len(gh.writes_named("create_draft_pr")) == 1
    assert store.get_ticket(t.id).pr_number == r.pr.number


def test_pr_taken_out_of_draft_by_a_person_is_updated_not_reverted(gh, store, pub, tmp_path):
    t = _ticket(gh, store)
    a1 = _attempt(store, t)
    gh.push_branch(REPO, pr_head_branch(t))
    r1 = pub.open_or_update_draft(t, a1, verify=_verify(tmp_path), diff=DIFF)
    gh.repos[REPO].items[r1.pr.number].draft = False  # the user clicked "Ready for review"

    a2 = _attempt(store, t, trigger="kickback")
    r2 = pub.open_or_update_draft(t, a2, verify=_verify(tmp_path), diff=DIFF)
    assert r2.action == "updated" and r2.pr.draft is False
    assert r2.note and "no longer a draft" in r2.note
    assert gh.writes_named("mark_ready_for_review") == []
    assert len(gh.writes_named("create_draft_pr")) == 1


def test_pr_closed_by_a_person_is_left_closed(gh, store, pub, tmp_path):
    t = _ticket(gh, store)
    a1 = _attempt(store, t)
    gh.push_branch(REPO, pr_head_branch(t))
    r1 = pub.open_or_update_draft(t, a1, verify=_verify(tmp_path), diff=DIFF)
    gh.close_pr(REPO, r1.pr.number)
    before = list(gh.writes)

    a2 = _attempt(store, t, trigger="kickback")
    r2 = pub.open_or_update_draft(t, a2, verify=_verify(tmp_path), diff=DIFF)
    assert r2.action == "closed" and r2.pr.state == "closed"
    assert gh.writes == before  # no reopen, no new PR, no body edit


def test_merged_pr_is_left_untouched(gh, store, pub, tmp_path):
    t = _ticket(gh, store)
    a1 = _attempt(store, t)
    gh.push_branch(REPO, pr_head_branch(t))
    r1 = pub.open_or_update_draft(t, a1, verify=_verify(tmp_path), diff=DIFF)
    pub.mark_ready(t, reason="reviewer_approved")
    gh.merge_pr(REPO, r1.pr.number)
    before = list(gh.writes)

    r2 = pub.open_or_update_draft(t, a1, verify=_verify(tmp_path, run=13), diff=DIFF)
    assert r2.action == "merged"
    assert gh.writes == before


def test_new_ticket_for_same_issue_ignores_earlier_tickets_closed_pr(gh, store, pub, tmp_path):
    t1 = _ticket(gh, store)
    a1 = _attempt(store, t1)
    gh.push_branch(REPO, pr_head_branch(t1))
    old = pub.open_or_update_draft(t1, a1, verify=_verify(tmp_path), diff=DIFF).pr
    gh.close_pr(REPO, old.number)
    store.set_column(t1.id, "dropped")

    t2 = store.create_ticket(
        repo=REPO, issue_number=t1.issue_number, title=t1.title, request="", base_branch="main"
    )
    b1 = _attempt(store, t2)
    r = pub.open_or_update_draft(t2, b1, verify=_verify(tmp_path), diff=DIFF)
    assert r.action == "created" and r.pr.number != old.number
    assert store.get_ticket(t2.id).pr_number == r.pr.number


def test_refuses_a_pr_a_person_opened_from_the_branch(gh, store, pub, tmp_path):
    t = _ticket(gh, store)
    a1 = _attempt(store, t)
    gh.push_branch(REPO, pr_head_branch(t))
    pr = gh.create_draft_pr(REPO, head=pr_head_branch(t), base="main", title="x", body="")
    gh.repos[REPO].items[pr.number].author = "someone"
    with pytest.raises(PublishError, match="opened by"):
        pub.open_or_update_draft(t, a1, verify=_verify(tmp_path))


def test_refuses_mismatched_or_non_worker_head(gh, store, pub, tmp_path):
    t = _ticket(gh, store)
    a1 = _attempt(store, t)
    with pytest.raises(PublishError, match="not a worker branch"):
        pub.open_or_update_draft(t, a1, head_branch="main")
    gh.push_branch(REPO, pr_head_branch(t))
    pub.open_or_update_draft(t, a1)
    gh.push_branch(REPO, "fman/issue-1/a2")
    with pytest.raises(PublishError, match="has head"):
        pub.open_or_update_draft(t, a1, head_branch="fman/issue-1/a2")


def test_refuses_attempt_of_another_ticket(gh, store, pub):
    t1, t2 = _ticket(gh, store), _ticket(gh, store, "Other")
    with pytest.raises(PublishError, match="belongs to ticket"):
        pub.open_or_update_draft(t1, _attempt(store, t2))


def test_mark_ready_only_with_a_reason_and_an_open_pr(gh, store, pub, tmp_path):
    t = _ticket(gh, store)
    a1 = _attempt(store, t)
    with pytest.raises(PublishError, match="no PR"):
        pub.mark_ready(t, reason="handover")
    gh.push_branch(REPO, pr_head_branch(t))
    r = pub.open_or_update_draft(t, a1, verify=_verify(tmp_path))
    with pytest.raises(PublishError, match="not a reason"):
        pub.mark_ready(t, reason="checks_passed")  # type: ignore[arg-type]
    assert gh.writes_named("mark_ready_for_review") == []

    pr = pub.mark_ready(t, reason="handover")
    assert pr.draft is False
    assert len(gh.writes_named("mark_ready_for_review")) == 1
    pub.mark_ready(t, reason="handover")  # already ready: no second write
    assert len(gh.writes_named("mark_ready_for_review")) == 1

    gh.close_pr(REPO, r.pr.number)
    with pytest.raises(PublishError, match="closed"):
        pub.mark_ready(t, reason="reviewer_approved")


# --------------------------------------------------------------------------- body


EXPECTED_BODY = """\
<!-- foreman-inator:pr ticket=1 -->
Closes #1

<!-- foreman-inator:handover:start -->
<!-- foreman-inator:handover:end -->

## What changed

2 files changed, +10 -2

- `src/app.py`
- `tests/test_app.py`

## This attempt

| Attempt | Model | Tier | Trigger | Cost |
| --- | --- | --- | --- | --- |
| 2 of 2 | `glm-4.6` | 3 | AI kickback | $0.0042 |

Total spend on this ticket: $0.0042 over 2 attempts.

## Verify

**Passed**: 12 tests run, 0 failed (`uv run pytest`, 3.2 s)
Build: passed.

<!-- foreman-inator:review:start -->
## AI review

_No AI review yet._
<!-- foreman-inator:review:end -->

## Attempt history

| # | Model | Tier | Trigger | Outcome | Cost |
| --- | --- | --- | --- | --- | --- |
| 1 | `qwen3.6:35b-a3b` | 1 | first | kicked back | $0.00 |
| 2 | `glm-4.6` | 3 | AI kickback | this attempt | $0.0042 |

---
_Opened by foreman-inator. Model reasoning and traces stay on the worker's machine._
"""


def _two_attempts(gh, store):
    t = _ticket(gh, store)
    a1 = _attempt(store, t)
    store.set_attempt_outcome(a1.id, "kicked_back")
    a2 = _attempt(store, t, model="glm-4.6", tier=3, trigger="ai_kickback", cost=0.0042)
    a2 = store.update_attempt(a2.id, build_passed=True)
    return t, store.list_attempts(t.id), a2


def test_body_snapshot(gh, store, tmp_path):
    t, attempts, a2 = _two_attempts(gh, store)
    body = render_body(t, attempts, a2, verify=_verify(tmp_path), diff=DIFF)
    assert body == EXPECTED_BODY
    assert render_body(t, attempts, a2, verify=_verify(tmp_path), diff=DIFF) == body  # pure


def test_body_review_and_handover_sections(gh, store, tmp_path):
    t, attempts, a2 = _two_attempts(gh, store)
    review = store.record_review(
        a2.id,
        reviewer_model="claude-sonnet-4-5",
        verdict="handed_to_user",
        tests_passed=True,
        changed_line_coverage=87.5,
        tests_nonvacuous=False,
        ran_app="not_runnable",
        tag="incomplete",
        note="Misses the --help text.\nInjected --> <!-- foreman-inator:review:end -->",
        cost_usd=0.012,
    )
    body = render_body(
        t, attempts, a2, verify=_verify(tmp_path), review=review, handover=Handover(4, "loop cap")
    )
    review_part = body[body.index(REVIEW_START) : body.index(REVIEW_END)]
    assert "**Handed to you** by `claude-sonnet-4-5`" in review_part
    assert "- Changed-line coverage: 88%" in review_part
    assert "- New tests fail without the change: no" in review_part
    assert "- Ran the change: not_runnable" in review_part
    assert "- Tag: incomplete" in review_part
    assert "- Review cost: $0.01" in review_part
    assert "> Misses the --help text." in review_part
    handover_part = body[body.index(HANDOVER_START) : body.index(HANDOVER_END)]
    assert "Handed to you after 4 attempts: loop cap" in handover_part
    for marker in (REVIEW_START, REVIEW_END, HANDOVER_START, HANDOVER_END):
        assert body.count(marker) == 1  # notes cannot forge or close a section


def test_body_never_contains_verify_output(gh, store, tmp_path):
    t, attempts, a2 = _two_attempts(gh, store)
    body = render_body(t, attempts, a2, verify=_verify(tmp_path, passed=False, failed=3))
    assert "SECRET_VERIFY_OUTPUT" not in body
    assert str(tmp_path) not in body
    assert "**Failed**: 12 tests run, 3 failed" in body


def test_body_falls_back_to_store_fields(gh, store):
    t = _ticket(gh, store)
    a = _attempt(store, t)
    body = render_body(t, [a], a)
    assert "2 files changed, 12 lines" in body
    assert "**Passed** (counts not recorded)" in body


def test_body_requires_current_attempt_in_list(gh, store):
    t, attempts, a2 = _two_attempts(gh, store)
    with pytest.raises(ValueError):
        render_body(t, attempts[:1], a2)


def test_format_cost():
    assert format_cost(0) == "$0.00"
    assert format_cost(0.0042) == "$0.0042"
    assert format_cost(1.5) == "$1.50"
