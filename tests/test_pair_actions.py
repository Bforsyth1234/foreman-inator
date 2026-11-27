"""ab:pair (#34 / GH #16) action handlers (``worker_actions``) and the paired PR head and
body (``pr.publisher``), against the real Store, Locks and the fake GitHub."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fakes.github import FakeGitHub
from foreman_inator.config import ModelEndpoint, key_ref
from foreman_inator.db.store import Store, Ticket
from foreman_inator.experiments import split
from foreman_inator.locks import Locks
from foreman_inator.pr import Publisher, pr_head_branch, render_body
from foreman_inator.routing import escalation as esc
from foreman_inator.tickets import lifecycle
from foreman_inator.worker_actions import (
    PAIR_NO_OPPONENT_TEXT,
    PAIR_ONCE_TEXT,
    ActionHandlers,
    Ctx,
    pair_start_rung,
)

REPO = "acme/widgets"
LOCAL = "http://127.0.0.1:11434/v1"


def models(*, cloud: bool = True) -> tuple[ModelEndpoint, ...]:
    out = [
        ModelEndpoint("w-one", 1, "local", "w-one-up", LOCAL, "fam-w"),
        ModelEndpoint("w-two", 2, "local", "w-two-up", LOCAL, "fam-w2"),
    ]
    if cloud:
        out.append(
            ModelEndpoint(
                "c-three", 3, "cloud", "c3", "https://x.example.invalid/v1", "fam-x", key_ref("x")
            )
        )
    return tuple(out)


class Host(ActionHandlers):
    """The attributes the handlers use, with GitHub writes run at once."""

    def __init__(self, store: Store, api: FakeGitHub, mods: tuple[ModelEndpoint, ...]) -> None:
        self.store, self.api = store, api
        self.config = SimpleNamespace(models=mods)
        self.locks = Locks(store)
        self.publisher = Publisher(api, store)
        self.statuses: list[str] = []

    def _gh(self, ticket: Ticket, name: str, fn: Any, **_: Any) -> Any:
        return fn()

    def status_update(self, ticket: Ticket, what: str, **_: Any) -> None:
        self.statuses.append(what)

    def start_tier(self, ticket: Ticket) -> int | None:
        return None


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "fman.db")
    yield s
    s.close()


@pytest.fixture
def gh() -> FakeGitHub:
    fake = FakeGitHub()
    fake.create_repo(REPO)
    return fake


@pytest.fixture(autouse=True)
def no_split(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(split, "active_split", lambda store: None)


def side_a(store: Store, gh: FakeGitHub, *, tier: int | None = 2) -> Ticket:
    n = gh.create_issue(REPO, "Add a flag", "please", labels=["agent", "ab:pair"])
    t = store.create_ticket(
        repo=REPO, issue_number=n, title="Add a flag", request="please", base_branch="main",
        owned_files=("src/app.py",), routed_tier=tier,
        route_reason="router: tier 2, small change" if tier else None,
    )  # fmt: skip
    return t


def requested(t: Ticket) -> lifecycle.PairRequested:
    return lifecycle.PairRequested(REPO, t.issue_number, ticket_id=t.id, labeled_by="brooks",
                                   label_event_id=1)  # fmt: skip


def comments(gh: FakeGitHub, t: Ticket) -> list[str]:
    return [c.body for c in gh.list_issue_comments(REPO, t.issue_number)]


def make_pair(host: Host, store: Store, gh: FakeGitHub) -> tuple[Ticket, Ticket]:
    a = side_a(store, gh)
    host._a_create_pair_side(Ctx(a, requested(a)))
    b = store.sibling(a.id)
    assert b is not None
    return store.get_ticket(a.id), b


# ------------------------------------------------------------------ CREATE_PAIR_SIDE


def test_create_pair_side_copies_a_and_opens_the_pair(store: Store, gh: FakeGitHub) -> None:
    host = Host(store, gh, models())
    a, b = make_pair(host, store, gh)
    assert (b.pair_side, b.column, b.issue_number) == ("b", "todo", a.issue_number)
    assert (b.title, b.request, b.base_branch, b.owned_files) == (
        a.title, a.request, a.base_branch, a.owned_files,
    )  # fmt: skip
    assert b.routed_tier == 2 and b.route_reason == "pair: router: tier 2, small change"
    pair = store.pair_for_ticket(a.id)
    assert pair is not None and (pair.ticket_a, pair.ticket_b) == (a.id, b.id)
    assert (pair.opponent_variant, pair.opponent_reason) == ("c-three", "next_rung")
    assert a.pair_side == "a"
    assert host.locks.holder_id(b) == a.id  # the pair exists before B can take locks
    assert comments(gh, a) == []
    host._a_create_pair_side(Ctx(a, requested(a)))  # a re-read event: no second side B
    assert len(store.tickets_for_issue(REPO, a.issue_number)) == 2


def test_unrouted_a_gives_an_unrouted_b(store: Store, gh: FakeGitHub) -> None:
    host = Host(store, gh, models())
    a = side_a(store, gh, tier=None)
    host._a_create_pair_side(Ctx(a, requested(a)))
    b = store.sibling(a.id)
    assert b is not None and b.routed_tier is None and b.route_reason is None
    pair = store.pair_for_ticket(a.id)
    assert pair is not None and pair.opponent_variant == "w-two"  # next rung from the lowest


def test_challenger_is_the_opponent_when_it_fits(
    store: Store, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    mods = (*models(), ModelEndpoint("w-alt", 2, "local", "w-alt-up", LOCAL, "fam-w"))
    exp = store.create_experiment(
        role="writer", champion_variant="w-two", challenger_variant="w-alt", mode="split",
        traffic_share=0.5,
    )  # fmt: skip
    monkeypatch.setattr(split, "active_split", lambda store: exp)
    # wave 24: the pair counts toward a mode='pair' experiment (ensure_pair), not the split
    from types import SimpleNamespace

    from foreman_inator.experiments import variants

    monkeypatch.setattr(variants, "champion", lambda store, role: SimpleNamespace(id="w-two"))
    host = Host(store, gh, mods)
    a, _ = make_pair(host, store, gh)
    pair = store.pair_for_ticket(a.id)
    assert pair is not None
    assert (pair.opponent_variant, pair.opponent_reason) == ("w-alt", "challenger")
    assert pair.experiment_id is not None and pair.experiment_id != exp.id
    pexp = store.get_experiment(pair.experiment_id)
    assert (pexp.mode, pexp.champion_variant, pexp.challenger_variant) == (
        "pair", "w-two", "w-alt",
    )  # fmt: skip


def test_no_second_model_posts_one_comment_and_no_pair(store: Store, gh: FakeGitHub) -> None:
    store.set_cloud(REPO, False)
    host = Host(store, gh, (models()[0], models()[2]))  # one local writer, one cloud
    a = side_a(store, gh, tier=1)
    for _ in range(2):
        host._a_create_pair_side(Ctx(a, requested(a)))
    assert store.pair_for_ticket(a.id) is None
    assert len(store.tickets_for_issue(REPO, a.issue_number)) == 1
    notes = comments(gh, a)
    assert len(notes) == 1 and PAIR_NO_OPPONENT_TEXT in notes[0]
    assert "no second model to pair against; running as a single ticket" in notes[0]


def test_local_only_repo_pairs_on_local_rungs(store: Store, gh: FakeGitHub) -> None:
    store.set_cloud(REPO, False)
    host = Host(store, gh, models())
    a = side_a(store, gh, tier=3)  # starts on the last local rung (w-two)
    host._a_create_pair_side(Ctx(a, requested(a)))
    pair = store.pair_for_ticket(a.id)
    assert pair is not None and (pair.opponent_variant, pair.opponent_reason) == (
        "w-one", "lower_rung",
    )  # fmt: skip


def test_a_dropped_pair_is_not_paired_again(store: Store, gh: FakeGitHub) -> None:
    host = Host(store, gh, models())
    a, b = make_pair(host, store, gh)
    pair = store.pair_for_ticket(a.id)
    assert pair is not None
    store.drop_pair(pair.id)
    for _ in range(2):
        host._a_create_pair_side(Ctx(a, requested(a)))
    assert len(store.tickets_for_issue(REPO, a.issue_number)) == 2
    notes = comments(gh, a)
    assert len(notes) == 1 and PAIR_ONCE_TEXT in notes[0]


def test_pair_start_rung_mirrors_escalation() -> None:
    ladder = esc.build_ladder(models())
    assert pair_start_rung(ladder, None).model == "w-one"
    assert pair_start_rung(ladder, 2).model == "w-two"
    assert pair_start_rung(ladder, 3, local_only=True).model == "w-two"
    assert pair_start_rung(esc.build_ladder(models()[2:]), 1, local_only=True) is None


# ------------------------------------------------------------------ PR close / branch


def test_close_own_pr_and_delete_own_branch(store: Store, gh: FakeGitHub) -> None:
    host = Host(store, gh, models())
    a, b = make_pair(host, store, gh)
    for t in (a, b):
        gh.push_branch(REPO, pr_head_branch(t))
    pr_a = gh.create_draft_pr(REPO, head=pr_head_branch(a), base="main", title="a", body="a")
    pr_b = gh.create_draft_pr(REPO, head=pr_head_branch(b), base="main", title="b", body="b")
    store.update_ticket(a.id, pr_number=pr_a.number)
    b = store.update_ticket(b.id, pr_number=pr_b.number)
    ev = lifecycle.PairLost(REPO, b.issue_number, ticket_id=b.id, sibling_ticket_id=a.id,
                            sibling_pr_number=pr_a.number, pr_number=pr_b.number)  # fmt: skip
    host._a_close_own_pr(Ctx(b, ev))
    host._a_delete_own_branch(Ctx(b, ev))
    assert gh.closed_prs == [(REPO, pr_b.number)]
    assert gh.deleted_branches == [(REPO, f"fman/issue-{b.issue_number}-b/a1")]
    host._a_delete_own_branch(Ctx(b, ev))  # already gone: fine
    assert gh.get_pr(REPO, pr_a.number).state == "open"


# ------------------------------------------------------------------ RECORD_PAIR_RESULT


def test_pair_lost_decides_and_flags_without_a_comment(store: Store, gh: FakeGitHub) -> None:
    host = Host(store, gh, models())
    a, b = make_pair(host, store, gh)
    store.set_column(a.id, "done")
    b = store.set_column(b.id, "dropped")
    ev = lifecycle.PairLost(REPO, b.issue_number, ticket_id=b.id, sibling_ticket_id=a.id,
                            sibling_pr_number=5)  # fmt: skip
    host._a_record_pair_result(Ctx(b, ev))
    host._a_record_pair_result(Ctx(store.get_ticket(b.id), ev))  # re-read: no-op
    pair = store.pair_for_ticket(a.id)
    assert pair is not None
    assert (pair.status, pair.winner_ticket_id, pair.loser_outcome) == (
        "decided", a.id, "merged_other",
    )  # fmt: skip
    assert store.get_ticket(b.id).flag_reason == ev.flag_reason
    assert comments(gh, b) == []


def test_rejected_side_leaves_pair_running_then_merge_decides(store: Store, gh: FakeGitHub) -> None:
    host = Host(store, gh, models())
    a, b = make_pair(host, store, gh)
    a = store.set_column(a.id, "dropped")
    closed = lifecycle.ClosedUnmerged(REPO, a.issue_number, ticket_id=a.id, pr_number=3,
                                      what="pr", paired=True)  # fmt: skip
    host._a_record_pair_result(Ctx(a, closed))
    pair = store.pair_for_ticket(a.id)
    assert pair is not None and pair.status == "running"
    b = store.set_column(b.id, "done")
    merged = lifecycle.Merged(REPO, b.issue_number, ticket_id=b.id, pr_number=4, paired=True)
    host._a_record_pair_result(Ctx(b, merged))
    pair = store.get_pair(pair.id)
    assert (pair.status, pair.winner_ticket_id, pair.loser_outcome) == (
        "decided", b.id, "rejected",
    )  # fmt: skip


def test_pair_removed_drops_the_pair(store: Store, gh: FakeGitHub) -> None:
    host = Host(store, gh, models())
    a, b = make_pair(host, store, gh)
    b = store.set_column(b.id, "dropped")
    ev = lifecycle.PairRemoved(REPO, b.issue_number, ticket_id=b.id, removed_by="brooks")
    host._a_record_pair_result(Ctx(b, ev))
    pair = store.pair_for_ticket(a.id)
    assert pair is not None and pair.status == "dropped"
    assert store.get_ticket(b.id).flag_reason is None


# ------------------------------------------------------------------ RELEASE_LOCKS


def _locked_pair(host: Host, store: Store, gh: FakeGitHub) -> tuple[Ticket, Ticket]:
    a, b = make_pair(host, store, gh)
    a = store.set_column(a.id, "in_progress")
    b = store.set_column(b.id, "in_progress")
    assert host.locks.acquire(a, ["src/app.py"]).ok
    assert store.list_locks()
    return a, b


def test_release_waits_for_the_sibling_after_pair_removed(store: Store, gh: FakeGitHub) -> None:
    host = Host(store, gh, models())
    a, b = _locked_pair(host, store, gh)
    b = store.set_column(b.id, "dropped")
    ev = lifecycle.PairRemoved(REPO, b.issue_number, ticket_id=b.id, removed_by="brooks")
    host._a_record_pair_result(Ctx(b, ev))
    host._a_release_locks(Ctx(b, ev))
    assert store.list_locks()  # side A goes on alone and keeps the pair's locks


def test_release_frees_the_pair_once_it_is_decided(store: Store, gh: FakeGitHub) -> None:
    host = Host(store, gh, models())
    a, b = _locked_pair(host, store, gh)
    a = store.set_column(a.id, "done")
    merged = lifecycle.Merged(REPO, a.issue_number, ticket_id=a.id, pr_number=4, paired=True)
    host._a_record_pair_result(Ctx(a, merged))
    host._a_release_locks(Ctx(a, merged))
    assert store.list_locks() == []  # B is still open but lost: the whole pair is over


# ------------------------------------------------------------------ publisher


def test_paired_pr_head_and_body_name_the_sibling(store: Store, gh: FakeGitHub) -> None:
    host = Host(store, gh, models())
    a, b = make_pair(host, store, gh)
    assert pr_head_branch(a) == f"fman/issue-{a.issue_number}/a1"
    assert pr_head_branch(b) == f"fman/issue-{a.issue_number}-b/a1"
    gh.push_branch(REPO, pr_head_branch(a))
    gh.push_branch(REPO, pr_head_branch(b))
    att_a = store.finish_attempt(store.add_attempt(a.id, model="w-two", tier=2, trigger="first").id,
                                 tests_passed=True, diff_files=1, diff_lines=2)  # fmt: skip
    res_a = host.publisher.open_or_update_draft(a, att_a)
    assert "Head-to-head" not in res_a.body  # B has no PR yet
    att_b = store.finish_attempt(store.add_attempt(b.id, model="c-three", tier=3,
                                 trigger="first").id, tests_passed=True, diff_files=1,
                                 diff_lines=2)  # fmt: skip
    res_b = host.publisher.open_or_update_draft(b, att_b)
    assert res_b.pr.head_ref == pr_head_branch(b)
    line = (
        f"Head-to-head with #{res_a.pr.number} (ab:pair): merge the one you prefer; "
        "the other is closed."
    )
    assert line in res_b.body
    res_a = host.publisher.open_or_update_draft(a, att_a)
    assert f"Head-to-head with #{res_b.pr.number} (ab:pair)" in res_a.body
    pair = store.pair_for_ticket(a.id)
    assert pair is not None
    store.drop_pair(pair.id)
    assert "Head-to-head" not in host.publisher.open_or_update_draft(a, att_a).body


def test_unpaired_body_is_unchanged(store: Store, gh: FakeGitHub) -> None:
    a = side_a(store, gh)
    att = store.finish_attempt(store.add_attempt(a.id, model="w-two", tier=2, trigger="first").id,
                               tests_passed=True, diff_files=1, diff_lines=2)  # fmt: skip
    assert render_body(a, [att], att) == render_body(a, [att], att, sibling_pr=None)
    assert "Head-to-head" not in render_body(a, [att], att)
