"""ab:pair (todo #34, GH #16, wave 23 D): how a pair shows to the user.

``fman status`` tags both sides' lines and says when both PRs wait on your pick; /activity
groups the sides under a head-to-head badge with won/lost once decided; the issue keeps ONE
status comment with a heading line per side, one post-once "Two PRs" note, and
``fman:working`` stays on while either side is active; the summary comment adds the pair
total. Runs on the real ``Store`` Pair API (migration 016) and the fake GitHub.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fakes.github import BOT_LOGIN, FakeGitHub
from foreman_inator.db.store import Pair, PairSide, Store, Ticket
from foreman_inator.metrics import activity_ui as ui
from foreman_inator.metrics import forms
from foreman_inator.pr import summary
from foreman_inator.worker_status import pair_pick_wait, render_status
from foreman_inator.worker_status_comment import (
    NOTE_MARKER,
    PAIR_NOTE_KEY,
    WORKING_LABEL,
    StatusCommentSteps,
    status_marker,
)

REPO = "octo/widgets"
NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "fman.db")
    yield s
    s.close()


@pytest.fixture
def gh() -> FakeGitHub:
    g = FakeGitHub()
    g.create_repo(REPO)
    g.create_issue(REPO, "Add a flag", "please", labels=["agent"])
    return g


def _ticket(store: Store, n: int = 1, side: PairSide | None = None, title: str = "T") -> Ticket:
    return store.create_ticket(
        repo=REPO,
        issue_number=n,
        title=title,
        request="Do it.",
        base_branch="main",
        routed_tier=1,
        pair_side=side,
    )


def _pair(store: Store, n: int = 1, title: str = "T") -> tuple[Ticket, Ticket, Pair]:
    a = _ticket(store, n, title=title)
    b = _ticket(store, n, "b", title=title)
    p = store.create_pair(
        ticket_a=a.id, ticket_b=b.id, opponent_variant="w-alt", opponent_reason="next_rung"
    )
    return store.get_ticket(a.id), store.get_ticket(b.id), p


def _ready(store: Store, t: Ticket, model: str, pr: int, *, review: bool = True) -> None:
    """The side's PR waits on you: attempt passed, PR open, AI review in."""
    store.set_column(t.id, "in_progress")
    a = store.add_attempt(t.id, model=model, tier=1, trigger="first")
    store.add_attempt_usage(a.id, cost_usd=0.25)
    store.finish_attempt(a.id, None, tests_passed=True)
    store.update_ticket(t.id, pr_number=pr)
    store.set_column(t.id, "testing")
    if review:
        store.record_review(a.id, reviewer_model="rev", verdict="approve", cost_usd=0.05)


# =========================================================================== fman status


def test_status_tags_both_sides_and_waits_on_your_pick_once_both_ready(store: Store) -> None:
    a, b, p = _pair(store)
    other = _ticket(store, 2)
    out = render_status(store)
    a_line = next(x for x in out.splitlines() if x.startswith(f"  #{a.id:<4}"))
    b_line = next(x for x in out.splitlines() if x.startswith(f"  #{b.id:<4}"))
    assert f"pair #{p.id} A  not started" in a_line  # no attempt yet: no model to name
    assert f"pair #{p.id} B · w-alt  not started" in b_line  # B's opponent before it runs
    o_line = next(x for x in out.splitlines() if x.startswith(f"  #{other.id:<4}"))
    assert "pair #" not in o_line  # unpaired line unchanged

    _ready(store, a, "w-one", 12)
    _ready(store, b, "w-alt", 13, review=False)  # B still in AI review
    assert "on your pick" not in render_status(store)
    assert pair_pick_wait(store, p) is None

    rev = store.list_attempts(b.id)[-1]
    store.record_review(rev.id, reviewer_model="rev", verdict="approve")
    out = render_status(store)
    assert f"pair #{p.id} A · w-one  attempt 1" in out
    assert out.count("WAITING: on your pick: merge #12 or #13") == 2
    assert "2 waiting." in out

    store.decide_pair(p.id, winner_ticket_id=a.id, loser_outcome="merged_other")
    out = render_status(store)
    assert "on your pick" not in out
    assert f"pair #{p.id} A · w-one, won" in out
    assert f"pair #{p.id} B · w-alt, lost" in out


# =========================================================================== /activity


def _page(store: Store) -> str:
    return ui.render_activity(store, guard=forms.FormGuard("tok"), now=NOW)


def test_activity_groups_sides_with_badge_and_won_lost(store: Store) -> None:
    plain_page = _page(store)
    assert "badge" not in plain_page  # no pair: no pair styles
    a = _ticket(store, 1, title="<script>x</script>")
    mid = _ticket(store, 3)  # an unpaired ticket created between the two sides
    b = _ticket(store, 1, "b", title="<script>x</script>")
    p = store.create_pair(
        ticket_a=a.id, ticket_b=b.id, opponent_variant="w-alt", opponent_reason="next_rung"
    )
    page = _page(store)
    assert ui.PAIR_CSS in page
    assert "<script>x</script>" not in page and "&lt;script&gt;" in page
    assert f'data-pair="{p.id}"' in page
    assert f"pair #{p.id} · A vs #{b.id}</span>" in page
    assert f"pair #{p.id} · B vs #{a.id}</span>" in page
    rows = [r for r in page.split("<tr ") if r.startswith("data-ticket")]
    order = [int(r.split('"')[1]) for r in rows]
    assert order.index(b.id) == order.index(a.id) + 1  # side B right after side A
    assert order.index(mid.id) > order.index(b.id)
    row_mid = next(r for r in page.split("<tr ") if r.startswith(f'data-ticket="{mid.id}"'))
    assert "badge" not in row_mid and "data-pair" not in row_mid

    _ready(store, a, "w-one", 12)
    _ready(store, b, "w-alt", 13)
    page = _page(store)
    assert page.count("WAITING: on your pick: merge #12 or #13") == 2

    store.decide_pair(p.id, winner_ticket_id=b.id, loser_outcome="rejected")
    page = _page(store)
    row_a = next(r for r in page.split("<tr ") if r.startswith(f'data-ticket="{a.id}"'))
    row_b = next(r for r in page.split("<tr ") if r.startswith(f'data-ticket="{b.id}"'))
    assert '<span class="badge lost">lost</span>' in row_a
    assert '<span class="badge won">won</span>' in row_b
    assert "on your pick" not in page


def test_group_pairs_keeps_order_and_pulls_sibling_up() -> None:
    t = [SimpleNamespace(id=i) for i in (1, 2, 3, 4)]
    p: Any = SimpleNamespace(ticket_a=1, ticket_b=3)
    got = ui.group_pairs(t, {1: p, 3: p})  # type: ignore[arg-type]
    assert [x.id for x in got] == [1, 3, 2, 4]
    assert [x.id for x in ui.group_pairs(t[1:], {1: p, 3: p})] == [2, 3, 4]  # A not shown


# =========================================================================== status comment


class W(StatusCommentSteps):
    def __init__(self, api: Any, store: Store, **cfg: Any) -> None:
        self.api = api
        self.store = store
        self.config = SimpleNamespace(**cfg)


def _bot_status(gh: FakeGitHub, tid: int) -> list[Any]:
    return [
        c
        for c in gh.list_issue_comments(REPO, 1)
        if c.author == BOT_LOGIN and status_marker(tid) in c.body
    ]


def test_one_comment_per_issue_with_a_line_per_side(store: Store, gh: FakeGitHub) -> None:
    a = _ticket(store)
    w = W(gh, store)
    w.status_update(a, "attempt_started", attempt=1, tier=1, model="w-one")  # unpaired first
    (c,) = _bot_status(gh, a.id)
    assert "**Side" not in c.body  # unpaired text unchanged

    b = _ticket(store, 1, "b")
    store.create_pair(
        ticket_a=a.id, ticket_b=b.id, opponent_variant="w-alt", opponent_reason="next_rung"
    )
    w.status_update(b, "attempt_started", attempt=1, tier=1, model="w-alt")
    (c,) = _bot_status(gh, a.id)  # A's comment became the pair's: no second status comment
    assert not _bot_status(gh, b.id)
    assert "**Side A:** foreman-inator is working on this: attempt 1 on tier 1 (w-one)." in c.body
    assert "**Side B:** foreman-inator is working on this: attempt 1 on tier 1 (w-alt)." in c.body

    # a restarted worker keeps the other side's line from the comment's body
    W(gh, store).status_update(a, "verifying", attempt=1, tier=1, model="w-one")
    (c,) = _bot_status(gh, a.id)
    assert (
        "**Side A:** foreman-inator is working on this: attempt 1 on tier 1 (w-one), verifying"
        in (c.body)
    )
    assert "(w-alt)." in c.body
    assert [x.method for x in gh.writes] == ["post_comment", "update_comment", "update_comment"]


def test_label_stays_while_a_side_is_active_and_note_posts_once(
    store: Store, gh: FakeGitHub
) -> None:
    a, b, p = _pair(store)
    w = W(gh, store, status_label=True)
    w.status_update(a, "attempt_started", attempt=1, tier=1, model="w-one")
    w.status_update(b, "attempt_started", attempt=1, tier=1, model="w-alt")
    assert len(gh.writes_named("add_labels")) == 1

    store.update_ticket(a.id, pr_number=12)
    w.status_update(a, "pr_opened", pr_number=12)
    assert WORKING_LABEL in gh.get_issue(REPO, 1).labels  # B still working
    marker = NOTE_MARKER.format(ticket_id=a.id, key=PAIR_NOTE_KEY)
    assert not [c for c in gh.list_issue_comments(REPO, 1) if marker in c.body]  # one PR only

    store.update_ticket(b.id, pr_number=13)
    w.status_update(b, "pr_opened", pr_number=13)
    assert WORKING_LABEL not in gh.get_issue(REPO, 1).labels  # neither side active
    notes = [c for c in gh.list_issue_comments(REPO, 1) if marker in c.body]
    assert len(notes) == 1
    assert "Two PRs: #12, #13. Merge the one you prefer; the other is closed." in notes[0].body

    # a restart re-sending pr_opened finds the note: no second one
    W(gh, store).status_update(b, "pr_opened", pr_number=13)
    assert len([c for c in gh.list_issue_comments(REPO, 1) if marker in c.body]) == 1
    assert len(_bot_status(gh, a.id)) == 1 and not _bot_status(gh, b.id)
    assert p.ticket_a == a.id  # the pair's comment is keyed by side A


# =========================================================================== summary


def test_summary_adds_pair_total_for_a_side(store: Store, gh: FakeGitHub) -> None:
    a, b, _ = _pair(store)
    _ready(store, a, "w-one", 12)  # 0.25 + 0.05 review
    _ready(store, b, "w-alt", 13)  # 0.25 + 0.05 review
    res = summary.post_summary(gh, store, a.id)
    assert "· cost $0.30 · pair total $0.60 (both sides) ·" in res.body
    gh.create_issue(REPO, "Other", "x", labels=["agent"])
    solo = _ticket(store, 2)
    assert "pair total" not in summary.post_summary(gh, store, solo.id).body
