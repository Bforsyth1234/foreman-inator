"""Poller + ticket lifecycle (todo #11), against FakeGitHub and a real Store under tmp_path."""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from fakes.github import FakeGitHub
from foreman_inator.config import Config
from foreman_inator.db.store import Store
from foreman_inator.github.poller import ISSUES_CURSOR, Poller, review_cursor
from foreman_inator.tickets.lifecycle import Action as A
from foreman_inator.tickets.lifecycle import (
    AgentLabelRemoved,
    AIReviewed,
    AttemptStarted,
    BaseConflict,
    ChecksFailed,
    ChecksPassed,
    CiResult,
    ClosedUnmerged,
    HandedToUser,
    IllegalTransition,
    IssueEdited,
    Merged,
    ReviewSubmitted,
    TicketRequested,
    TierOverride,
    Transition,
    apply,
)

REPO = "brooks/toy"
ME = "brooks"
STRANGER = "mallory"


# --------------------------------------------------------------------------- harness


class Driver:
    """A minimal stand-in for the worker loop: applies transitions to the store and fake."""

    def __init__(self, gh: FakeGitHub, store: Store, poller: Poller) -> None:
        self.gh, self.store, self.poller = gh, store, poller
        self.log: list[tuple[object, Transition]] = []

    def column(self, ev: object) -> str | None:
        if isinstance(ev, TicketRequested):
            t = self.store.find_open_ticket(ev.repo, ev.issue_number)
            return t.column if t else None
        return self.store.get_ticket(ev.ticket_id).column  # type: ignore[attr-defined]

    def handle(self, ev: object) -> Transition:
        tr = apply(self.column(ev), ev)  # type: ignore[arg-type]
        self.log.append((ev, tr))
        if A.CREATE_TICKET in tr.actions:
            assert isinstance(ev, TicketRequested)
            t = self.store.create_ticket(
                repo=ev.repo,
                issue_number=ev.issue_number,
                title=ev.title,
                request=ev.body,
                base_branch="main",
                routed_tier=ev.tier_override,
            )
            tid = t.id
        else:
            tid = ev.ticket_id  # type: ignore[attr-defined]
        t = self.store.get_ticket(tid)
        if A.UPDATE_REQUEST in tr.actions:
            self.store.update_ticket(tid, title=ev.title, request=ev.body)  # type: ignore[attr-defined]
        if A.APPLY_TIER_OVERRIDE in tr.actions and isinstance(ev, TierOverride):
            self.store.update_ticket(tid, routed_tier=ev.tier)
        if A.OPEN_OR_UPDATE_DRAFT_PR in tr.actions:
            branch = f"fman/issue-{t.issue_number}/a1"
            self.gh.push_branch(t.repo, branch)
            if t.pr_number is None:
                pr = self.gh.create_draft_pr(
                    t.repo,
                    head=branch,
                    base="main",
                    title=t.title,
                    body=f"Closes #{t.issue_number}",
                )
                self.store.update_ticket(tid, pr_number=pr.number)
        if A.MARK_READY_FOR_REVIEW in tr.actions:
            self.gh.mark_ready_for_review(t.repo, self.store.get_ticket(tid).pr_number)
        if A.CLEAR_KICKBACK_LABEL in tr.actions:
            self.gh.remove_label(t.repo, ev.pr_number, ev.kickback_label)  # type: ignore[attr-defined]
        if A.CLOSE_ISSUE in tr.actions:
            self.gh.close_issue(t.repo, t.issue_number)
        if tr.column != t.column:
            self.store.set_column(tid, tr.column)
        return tr

    def poll(self) -> list:
        return list(self.poller.process(self.handle))

    def ticket(self, issue: int = 1):
        return self.store.tickets_for_issue(REPO, issue)[-1]

    def to_testing(self, issue: int = 1) -> int:
        """Label → ticket → attempt → checks pass → AI approve: PR ready, ticket in testing."""
        self.gh.apply_label(REPO, issue, "agent", by=ME)
        self.poll()
        tid = self.ticket(issue).id
        for ev in (AttemptStarted(tid), ChecksPassed(tid), AIReviewed(tid, "approve")):
            self.handle(ev)
        assert self.store.get_ticket(tid).column == "testing"
        return tid


@pytest.fixture
def env(tmp_path: Path):
    gh = FakeGitHub()
    gh.create_repo(REPO)
    gh.create_issue(REPO, "Add a --version flag", "Print the version and exit.")
    store = Store(tmp_path / "fman.db")
    cfg = Config(allowed_users=(ME,), repos=(REPO,))
    d = Driver(gh, store, Poller(gh, store, cfg))
    yield d
    store.close()


def kinds(events) -> list[str]:
    return [type(e).__name__ for e in events]


# --------------------------------------------------------------------------- SPEC mapping table
# One row per row of SPEC "Architecture and GitHub mapping".


def row_agent_label(d: Driver):
    d.gh.apply_label(REPO, 1, "agent", by=ME)
    return None


def row_tier_label(d: Driver):
    d.gh.apply_label(REPO, 1, "agent", by=ME)
    d.poll()
    d.handle(AttemptStarted(d.ticket().id))
    d.gh.apply_label(REPO, 1, "tier:3", by=ME)
    return None


def row_checks_pass(d: Driver):
    d.gh.apply_label(REPO, 1, "agent", by=ME)
    d.poll()
    tid = d.ticket().id
    d.handle(AttemptStarted(tid))
    return ChecksPassed(tid)  # a worker event: nothing on GitHub to poll


def row_request_changes(d: Driver):
    d.to_testing()
    pr = d.ticket().pr_number
    d.gh.apply_label(REPO, pr, "kickback:incomplete", by=ME)
    d.gh.submit_review(REPO, pr, by=ME, state="REQUEST_CHANGES", body="Also handle -V.")
    return None


def row_approve_then_merge(d: Driver):
    d.to_testing()
    pr = d.ticket().pr_number
    d.gh.submit_review(REPO, pr, by=ME, state="APPROVE", body="LGTM")
    events = d.poll()
    assert kinds(events) == ["ReviewSubmitted"] and events[0].verdict == "approve"
    assert d.log[-1][1] == Transition("testing", (A.RECORD_APPROVAL,))
    d.gh.merge_pr(REPO, pr, by=ME)
    return None


def row_closed_unmerged(d: Driver):
    d.to_testing()
    d.gh.close_issue_as(REPO, 1, by=ME)
    return None


MAPPING = [
    # (SPEC row, setup, event type, column before, column after, actions)
    (
        "agent label added by an allowed user",
        row_agent_label,
        TicketRequested,
        None,
        "todo",
        (A.CREATE_TICKET, A.ROUTE, A.START_ATTEMPT),
    ),
    (
        "tier:N label on the issue",
        row_tier_label,
        TierOverride,
        "in_progress",
        "in_progress",
        (A.APPLY_TIER_OVERRIDE,),
    ),
    (
        "attempt passes checks",
        row_checks_pass,
        ChecksPassed,
        "in_progress",
        "testing",
        (A.OPEN_OR_UPDATE_DRAFT_PR, A.AI_REVIEW),
    ),
    (
        "PR review: request changes",
        row_request_changes,
        ReviewSubmitted,
        "testing",
        "in_progress",
        (A.RECORD_KICKBACK, A.CLEAR_KICKBACK_LABEL, A.NEXT_ATTEMPT),
    ),
    (
        "PR review: approve, then merge",
        row_approve_then_merge,
        Merged,
        "testing",
        "done",
        (A.RECORD_ACCEPT, A.CLOSE_ISSUE, A.POST_SUMMARY, A.RELEASE_LOCKS, A.CLEANUP_WORKTREE),
    ),
    (
        "issue closed without merge",
        row_closed_unmerged,
        ClosedUnmerged,
        "testing",
        "dropped",
        (A.RELEASE_LOCKS, A.CLEANUP_WORKTREE),
    ),
]


@pytest.mark.parametrize(
    ("row", "setup", "etype", "before", "after", "actions"), MAPPING, ids=[m[0] for m in MAPPING]
)
def test_spec_mapping_row(env: Driver, row, setup, etype, before, after, actions):
    worker_event = setup(env)
    if worker_event is not None:
        ev = worker_event
        assert env.column(ev) == before
        env.handle(ev)
    else:
        events = env.poll()
        assert kinds(events) == [etype.__name__], row
        ev = events[0]
    assert env.log[-1][0] == ev
    assert apply(before, ev) == Transition(after, actions)
    assert env.ticket().column == after
    assert env.poll() == []  # handled: nothing new


def test_ticket_requested_carries_issue_and_overrides(env: Driver):
    env.gh.apply_label(REPO, 1, "tier:2", by=ME)
    env.gh.apply_label(REPO, 1, "review:skip", by=ME)
    env.gh.apply_label(REPO, 1, "agent", by=ME)
    (ev,) = env.poll()
    assert isinstance(ev, TicketRequested)
    assert (ev.title, ev.body, ev.labeled_by) == (
        "Add a --version flag",
        "Print the version and exit.",
        ME,
    )
    assert (ev.tier_override, ev.review_skip) == (2, True)
    assert A.APPLY_TIER_OVERRIDE in env.log[-1][1].actions
    assert env.ticket().routed_tier == 2
    assert env.poll() == []  # the tier label was consumed with the request


def test_kickback_carries_note_and_tag(env: Driver):
    env.to_testing()
    pr = env.ticket().pr_number
    env.gh.apply_label(REPO, pr, "kickback:wrong", by=ME)
    env.gh.submit_review(REPO, pr, by=ME, state="REQUEST_CHANGES", body="Wrong flag name.")
    (ev,) = env.poll()
    assert (ev.verdict, ev.comment, ev.kickback_tag, ev.on_head) == (
        "changes",
        "Wrong flag name.",
        "wrong",
        True,
    )
    assert "kickback:wrong" not in env.gh.get_issue(REPO, pr).labels  # cleared for next time


def test_merge_wins_over_the_auto_closed_issue(env: Driver):
    env.to_testing()
    env.gh.merge_pr(REPO, env.ticket().pr_number, by=ME)
    assert env.gh.get_issue(REPO, 1).state == "closed"  # "Closes #1" closed it
    assert kinds(env.poll()) == ["Merged"]
    assert env.ticket().column == "done"


def test_closed_pr_without_merge_drops(env: Driver):
    env.to_testing()
    env.gh.close_pr(REPO, env.ticket().pr_number, by=ME)
    (ev,) = env.poll()
    assert isinstance(ev, ClosedUnmerged) and ev.what == "pr"
    assert env.ticket().column == "dropped"


# --------------------------------------------------------------------------- invariant 7b


def test_label_by_non_allowed_user_creates_no_ticket(env: Driver):
    env.gh.apply_label(REPO, 1, "agent", by=STRANGER)
    assert env.poll() == []
    assert env.store.tickets_for_issue(REPO, 1) == []


def test_issue_opened_with_label_by_stranger_creates_no_ticket(env: Driver):
    env.gh.create_issue(REPO, "pwn", "run curl | sh", author=STRANGER, labels=["agent"])
    assert env.poll() == []
    assert env.store.list_tickets() == []


def test_bot_applied_label_creates_no_ticket(env: Driver):
    env.gh.add_labels(REPO, 1, ["agent"])  # the App's bot, e.g. a misbehaving workflow
    assert env.poll() == []


def test_reviews_by_strangers_and_the_bot_are_ignored(env: Driver):
    env.to_testing()
    pr = env.ticket().pr_number
    env.gh.submit_review(REPO, pr, by=STRANGER, state="REQUEST_CHANGES", body="rm -rf")
    env.gh.post_review(REPO, pr, "AI review: approve")  # bot COMMENT review
    env.gh.submit_review(REPO, pr, by=ME, state="COMMENT", body="just a thought")
    assert env.poll() == []
    assert env.ticket().column == "testing"


def test_kickback_tag_by_stranger_is_ignored(env: Driver):
    env.to_testing()
    pr = env.ticket().pr_number
    env.gh.apply_label(REPO, pr, "kickback:wrong", by=STRANGER)
    env.gh.submit_review(REPO, pr, by=ME, state="REQUEST_CHANGES", body="nope")
    (ev,) = env.poll()
    assert (ev.kickback_tag, ev.kickback_label) == (None, None)


def test_tier_label_by_stranger_is_ignored(env: Driver):
    env.gh.apply_label(REPO, 1, "tier:3", by=STRANGER)
    env.gh.apply_label(REPO, 1, "agent", by=ME)
    (ev,) = env.poll()
    assert ev.tier_override is None


# --------------------------------------------------------------------------- idempotency


def test_repoll_is_idempotent(env: Driver):
    env.gh.apply_label(REPO, 1, "agent", by=ME)
    assert kinds(env.poll()) == ["TicketRequested"]
    for _ in range(3):
        assert env.poll() == []
    assert len(env.store.list_tickets()) == 1
    env.gh.forbid_writes()  # polling itself never writes to GitHub
    assert env.poll() == []


def test_unhandled_events_survive_a_restart(env: Driver, tmp_path: Path):
    env.gh.apply_label(REPO, 1, "agent", by=ME)
    first = env.poller.poll_once()  # crash: never handled, acked or committed
    again = env.poller.poll_once()
    assert kinds(first) == kinds(again) == ["TicketRequested"]

    env.store.close()
    store = Store(tmp_path / "fman.db")  # restarted worker: fresh Poller, same database
    cfg = Config(allowed_users=(ME,), repos=(REPO,))
    d2 = Driver(env.gh, store, Poller(env.gh, store, cfg))
    assert kinds(d2.poll()) == ["TicketRequested"]
    d3 = Driver(env.gh, store, Poller(env.gh, store, cfg))
    assert d3.poll() == []
    assert len(store.list_tickets()) == 1
    env.store = store  # fixture teardown closes this one


def test_failed_handler_rolls_back_and_redelivers(env: Driver):
    env.to_testing()
    pr = env.ticket().pr_number
    env.gh.submit_review(REPO, pr, by=ME, state="REQUEST_CHANGES", body="again")

    def boom(ev):
        env.handle(ev)
        raise RuntimeError("crash mid-handle")

    with pytest.raises(RuntimeError):
        env.poller.process(boom)
    assert env.ticket().column == "testing"  # rolled back with the ack
    assert env.store.get_cursor(REPO, review_cursor(pr)) is None
    assert kinds(env.poll()) == ["ReviewSubmitted"]
    assert env.ticket().column == "in_progress"


def test_event_arriving_mid_poll_is_not_lost(env: Driver):
    env.gh.apply_label(REPO, 1, "agent", by=ME)
    env.poll()
    env.gh.create_issue(REPO, "Second thing", "body")  # issue 2, unlabeled
    real = env.gh.list_issues

    def list_then_label(*a, **kw):
        out = real(*a, **kw)
        env.gh.apply_label(REPO, 2, "agent", by=ME)  # lands after the listing was read
        return out

    env.gh.list_issues = list_then_label  # type: ignore[method-assign]
    assert env.poll() == []  # this poll could not see it...
    env.gh.list_issues = real  # type: ignore[method-assign]
    (ev,) = env.poll()  # ...the next one does
    assert isinstance(ev, TicketRequested) and ev.issue_number == 2


def test_review_arriving_mid_poll_is_not_lost(env: Driver):
    env.to_testing()
    pr = env.ticket().pr_number
    real = env.gh.list_reviews

    def list_then_review(*a, **kw):
        out = real(*a, **kw)
        env.gh.list_reviews = real  # type: ignore[method-assign]
        env.gh.submit_review(REPO, pr, by=ME, state="APPROVE")
        return out

    env.gh.list_reviews = list_then_review  # type: ignore[method-assign]
    assert env.poll() == []
    assert kinds(env.poll()) == ["ReviewSubmitted"]


def test_cursor_lives_in_the_store(env: Driver):
    env.gh.apply_label(REPO, 1, "agent", by=ME)
    env.poll()
    assert env.store.get_cursor(REPO, ISSUES_CURSOR) is not None


# --------------------------------------------------------------------------- edge cases


def test_agent_label_removed_by_allowed_user_drops(env: Driver):
    env.gh.apply_label(REPO, 1, "agent", by=ME)
    env.poll()
    env.handle(AttemptStarted(env.ticket().id))
    env.gh.remove_label_as(REPO, 1, "agent", by=ME)
    (ev,) = env.poll()
    assert isinstance(ev, AgentLabelRemoved)
    assert env.log[-1][1] == Transition(
        "dropped", (A.CANCEL_ATTEMPT, A.RELEASE_LOCKS, A.CLEANUP_WORKTREE)
    )


def test_agent_label_removed_by_stranger_is_ignored(env: Driver):
    env.gh.apply_label(REPO, 1, "agent", by=ME)
    env.poll()
    env.gh.remove_label_as(REPO, 1, "agent", by=STRANGER)
    assert env.poll() == []
    assert env.ticket().column == "todo"


def test_label_reapplied_after_drop_starts_a_new_ticket(env: Driver):
    env.gh.apply_label(REPO, 1, "agent", by=ME)
    env.poll()
    env.gh.close_issue_as(REPO, 1, by=ME)
    assert kinds(env.poll()) == ["ClosedUnmerged"]
    env.gh.reopen_issue_as(REPO, 1, by=ME)
    assert env.poll() == []  # reopened, label still from the old request: no new ticket
    env.gh.remove_label_as(REPO, 1, "agent", by=ME)
    env.gh.apply_label(REPO, 1, "agent", by=ME)
    assert kinds(env.poll()) == ["TicketRequested"]
    cols = [t.column for t in env.store.tickets_for_issue(REPO, 1)]
    assert cols == ["dropped", "todo"]


def test_issue_edited_after_ticket_created(env: Driver):
    env.gh.apply_label(REPO, 1, "agent", by=ME)
    env.poll()
    env.gh._item(REPO, 1).body = "Print the version, then exit 0."
    env.gh.comment_as(REPO, 1, "edited", by=ME)  # bumps updated_at as an edit does
    (ev,) = env.poll()
    assert isinstance(ev, IssueEdited) and ev.body.endswith("exit 0.")
    assert env.ticket().request == "Print the version, then exit 0."
    assert env.ticket().column == "todo"
    assert env.poll() == []


def test_review_on_an_old_commit_is_ignored(env: Driver):
    env.to_testing()
    pr = env.ticket().pr_number
    env.gh.submit_review(REPO, pr, by=ME, state="REQUEST_CHANGES", body="old code")
    env.gh.push_branch(REPO, "fman/issue-1/a1")  # a newer commit landed on the PR
    (ev,) = env.poll()
    assert isinstance(ev, ReviewSubmitted) and not ev.on_head
    assert env.log[-1][1] == Transition("testing")
    assert env.ticket().column == "testing"
    assert env.poll() == []  # consumed


def test_only_newest_of_several_reviews_counts(env: Driver):
    env.to_testing()
    pr = env.ticket().pr_number
    env.gh.submit_review(REPO, pr, by=ME, state="REQUEST_CHANGES", body="first")
    env.gh.submit_review(REPO, pr, by=ME, state="APPROVE", body="never mind")
    (ev,) = env.poll()
    assert ev.verdict == "approve"
    assert env.poll() == []


def test_tier_override_mid_ticket_once(env: Driver):
    env.gh.apply_label(REPO, 1, "agent", by=ME)
    env.poll()
    env.gh.apply_label(REPO, 1, "tier:1", by=ME)
    env.gh.apply_label(REPO, 1, "tier:3", by=ME)
    (ev,) = env.poll()
    assert isinstance(ev, TierOverride) and ev.tier == 3
    assert env.ticket().routed_tier == 3
    assert env.poll() == []


# --------------------------------------------------------------------------- request budget


def test_requests_per_poll_are_bounded(env: Driver):
    env.to_testing()
    env.poll()
    idle = env.poller.poll_once()
    assert idle.requests[REPO] == 3  # list_issues + get_pr + list_reviews
    assert idle.events == []


# --------------------------------------------------------------------------- pure machine

LEGAL = {
    # (column, event type name) pairs the table allows, ignoring payload variants
    (None, "TicketRequested"),
    *((c, e) for c in ("todo", "in_progress", "testing") for e in ("TierOverride", "IssueEdited")),
    *((c, e) for c in ("todo", "in_progress", "testing") for e in ("AgentLabelRemoved",)),
    *((c, e) for c in ("todo", "in_progress", "testing") for e in ("ClosedUnmerged",)),
    *((c, "HandedToUser") for c in ("todo", "in_progress", "testing")),
    ("in_progress", "Merged"),
    ("testing", "Merged"),
    ("testing", "ReviewSubmitted"),
    ("todo", "AttemptStarted"),
    ("in_progress", "AttemptStarted"),
    ("in_progress", "ChecksPassed"),
    ("in_progress", "ChecksFailed"),
    ("testing", "AIReviewed"),
    ("testing", "CiResult"),  # #41
    ("testing", "BaseConflict"),  # #43
}

SAMPLES = [
    TicketRequested(REPO, 1, title="t", body="b", labeled_by=ME, label_event_id=1),
    TierOverride(REPO, 1, ticket_id=1, tier=2, labeled_by=ME),
    IssueEdited(REPO, 1, ticket_id=1, title="t", body="b"),
    AgentLabelRemoved(REPO, 1, ticket_id=1, removed_by=ME),
    ReviewSubmitted(
        REPO, 1, ticket_id=1, pr_number=2, review_id=1, reviewer=ME, verdict="approve", comment=""
    ),
    Merged(REPO, 1, ticket_id=1, pr_number=2),
    ClosedUnmerged(REPO, 1, ticket_id=1, pr_number=None, what="issue"),
    AttemptStarted(1),
    ChecksPassed(1),
    ChecksFailed(1),
    AIReviewed(1, "approve"),
    HandedToUser(1, "tier 3 failed"),
    CiResult(REPO, 1, ticket_id=1, attempt_id=1, head_sha="a" * 40, state="failed"),  # #41
    BaseConflict(REPO, 1, ticket_id=1, pr_number=2, head_sha="a" * 40),  # #43
]


@pytest.mark.parametrize(
    ("column", "event"),
    list(itertools.product([None, "todo", "in_progress", "testing", "done", "dropped"], SAMPLES)),
)
def test_every_unlisted_transition_raises(column, event):
    if (column, type(event).__name__) in LEGAL:
        tr = apply(column, event)
        assert tr.column in ("todo", "in_progress", "testing", "done", "dropped")
    else:
        with pytest.raises(IllegalTransition):
            apply(column, event)


@pytest.mark.parametrize(
    ("column", "event", "expected"),
    [
        (
            "in_progress",
            ChecksPassed(1, review_skip=True),
            Transition("testing", (A.OPEN_OR_UPDATE_DRAFT_PR, A.MARK_READY_FOR_REVIEW)),
        ),
        ("in_progress", ChecksFailed(1), Transition("in_progress", (A.NEXT_ATTEMPT,))),
        (
            "testing",
            AIReviewed(1, "request_changes"),
            Transition("in_progress", (A.RECORD_KICKBACK, A.NEXT_ATTEMPT)),
        ),
        (
            "testing",
            AIReviewed(1, "handed_to_user"),
            Transition("testing", (A.MARK_READY_FOR_REVIEW, A.FLAG_TICKET)),
        ),
        (
            "in_progress",
            HandedToUser(1, "tier 3 failed"),
            Transition("in_progress", (A.FLAG_TICKET,)),
        ),
        (
            "in_progress",
            Merged(REPO, 1, ticket_id=1, pr_number=2),
            Transition(
                "done",
                (
                    A.CANCEL_ATTEMPT,
                    A.RECORD_ACCEPT,
                    A.CLOSE_ISSUE,
                    A.POST_SUMMARY,
                    A.RELEASE_LOCKS,
                    A.CLEANUP_WORKTREE,
                ),
            ),
        ),
    ],
)
def test_worker_side_transitions(column, event, expected):
    assert apply(column, event) == expected


def test_mark_ready_only_after_approve_or_handover():
    """Invariant 7a: no transition marks the PR ready except AI approve/handover/review:skip."""
    for column, event in itertools.product(["todo", "in_progress", "testing"], SAMPLES):
        try:
            tr = apply(column, event)
        except IllegalTransition:
            continue
        if A.MARK_READY_FOR_REVIEW in tr.actions:
            assert isinstance(event, AIReviewed | ChecksPassed)
    assert A.MARK_READY_FOR_REVIEW not in apply("in_progress", ChecksPassed(1)).actions


def test_unknown_column_is_rejected():
    with pytest.raises(ValueError):
        apply("doing", ChecksFailed(1))
