"""ab:pair (#16) in ``fman retry`` and the worker queue.

retry: ``owner/repo#n`` naming a pair is refused with both ticket ids; a side is named by
its ticket id or ``owner/repo#n:a|b``; retrying one side never touches the other.
queue: ``SerialMirror.cleanup_issue`` passes ``side``; the worktree sweep attributes
side-B dirs (``issue-<n>-b-a<k>``) to side B; the open-PR cap counts each side's PR.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from foreman_inator import retry as rt
from foreman_inator.config import Config
from foreman_inator.db.store import Store, Ticket
from foreman_inator.runner.worktree import RepoMirror
from foreman_inator.worker_queue import (
    QueueSteps,
    SerialMirror,
    open_fman_prs,
    open_pr_cap_wait,
)

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
CFG = Config()
REPO = "me/toy"


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "fman.db")
    yield s
    s.close()


def _ticket(store: Store, n: int, side: str | None = None) -> Ticket:
    t = store.create_ticket(repo=REPO, issue_number=n, title="t", request="r",
                            base_branch="main", routed_tier=1, pair_side=side)  # fmt: skip
    store.set_column(t.id, "in_progress")
    return store.get_ticket(t.id)


def _failed(store: Store, t: Ticket, side: str | None = None) -> Ticket:
    tag = "-b" if side == "b" else ""
    a = store.add_attempt(t.id, model="inkling-2-mini", tier=1, trigger="first",
                          branch=f"fman/issue-{t.issue_number}{tag}/a1")  # fmt: skip
    store.finish_attempt(a.id, "failed_checks")
    return store.flag_ticket(t.id, "checks_failed: the checks failed 2 times in a row")


def _pair(store: Store, n: int = 5, *, flag: bool = True) -> tuple[Ticket, Ticket]:
    a = _ticket(store, n)
    b = _ticket(store, n, "b")
    store.create_pair(ticket_a=a.id, ticket_b=b.id, opponent_variant="w@glm",
                      opponent_reason="next_rung")  # fmt: skip
    if flag:
        _failed(store, a)
        _failed(store, b, "b")
    return store.get_ticket(a.id), store.get_ticket(b.id)


# --------------------------------------------------------------------------- retry


def test_issue_ref_for_a_pair_is_ambiguous_and_changes_nothing(store):
    a, b = _pair(store)
    res = rt.retry(store, CFG, f"{REPO}#5", now=NOW)
    assert res.status == "refused"
    assert f"ticket {a.id}" in res.reason and f"ticket {b.id}" in res.reason
    assert f"{REPO}#5:a" in res.reason and f"{REPO}#5:b" in res.reason
    for t in (a, b):
        after = store.get_ticket(t.id)
        assert after.flag_reason == t.flag_reason
        assert rt.retry_marks(store, after) == []


def test_issue_ref_is_ambiguous_when_neither_side_is_open(store):
    a, b = _pair(store, flag=False)
    store.set_column(a.id, "dropped")
    store.set_column(b.id, "dropped")
    with pytest.raises(LookupError, match=f"ticket {a.id}.*ticket {b.id}"):
        rt.resolve_ticket(store, f"{REPO}#5")


@pytest.mark.parametrize("how", ["id", "side"])
def test_retrying_side_b_never_touches_side_a(store, how):
    a, b = _pair(store)
    ref = b.id if how == "id" else f"{REPO}#5:B"
    res = rt.retry(store, CFG, ref, now=NOW)
    assert res.status == "queued" and res.ticket_id == b.id, res.reason
    assert store.get_ticket(b.id).flag_reason is None
    assert len(rt.retry_marks(store, store.get_ticket(b.id))) == 1
    after_a = store.get_ticket(a.id)
    assert after_a.flag_reason == a.flag_reason and after_a.column == a.column
    assert rt.retry_marks(store, after_a) == []
    # side A after: its own retry, independent of B's
    res_a = rt.retry(store, CFG, f"{REPO}#5:a", now=NOW)
    assert res_a.status == "queued" and res_a.ticket_id == a.id
    assert len(rt.retry_marks(store, store.get_ticket(b.id))) == 1


def test_side_ref_on_an_unpaired_issue_is_refused(store):
    t = _failed(store, _ticket(store, 6))
    with pytest.raises(LookupError, match="no ab:pair side B"):
        rt.resolve_ticket(store, f"{REPO}#6:b")
    assert rt.resolve_ticket(store, f"{REPO}#6") == t  # unpaired: unchanged


def test_one_open_side_left_is_what_the_issue_ref_means(store):
    a, b = _pair(store)
    store.set_column(b.id, "dropped")
    assert rt.resolve_ticket(store, f"{REPO}#5").id == a.id


def test_a_dropped_side_is_not_put_back(store):
    a, b = _pair(store, flag=False)
    store.set_column(a.id, "dropped")
    store.set_column(b.id, "dropped")
    api = SimpleNamespace(
        get_issue=lambda repo, n: SimpleNamespace(state="open", labels=["agent"]),
        get_pr=None,
    )
    res = rt.retry(store, CFG, b.id, now=NOW, api=api)
    assert res.status == "refused" and "ab:pair" in res.reason and str(a.id) in res.reason
    assert store.get_ticket(b.id).column == "dropped"
    assert store.get_ticket(a.id).column == "dropped"


# --------------------------------------------------------------------------- queue


def test_serial_mirror_cleanup_issue_passes_the_side(tmp_path, monkeypatch):
    seen: list[tuple[int, str | None]] = []

    def record(self, n, side=None):
        seen.append((n, side))

    monkeypatch.setattr(RepoMirror, "cleanup_issue", record)
    m = SerialMirror(tmp_path, REPO, "https://github.com/me/toy.git", serial=threading.RLock())
    m.cleanup_issue(4)
    m.cleanup_issue(4, "b")
    assert seen == [(4, None), (4, "b")]


class _FakeMirror:
    def __init__(self, root: Path) -> None:
        self.repo_dir = root
        self.mirror_path = root / "mirror"
        self.cleaned: list[tuple[int, str | None]] = []

    def cleanup_issue(self, n: int, side: str | None = None) -> None:
        self.cleaned.append((n, side))

    def remove_scratch(self) -> int:
        return 0


class _Sweeper(QueueSteps):
    def __init__(self, store: Store, mirror: _FakeMirror) -> None:
        self.store = store
        self._fake = mirror

    def _mirror(self, repo: str):  # type: ignore[override]
        return self._fake


def _sweep(store: Store, tmp_path: Path, dirs: list[str]) -> list[tuple[int, str | None]]:
    root = tmp_path / "wt"
    for d in dirs:
        (root / d).mkdir(parents=True)
    m = _FakeMirror(root)
    _Sweeper(store, m)._sweep_worktrees(REPO)
    return m.cleaned


def test_sweep_cleans_a_dropped_side_b_while_side_a_runs(store, tmp_path):
    a, b = _pair(store, flag=False)
    store.set_column(b.id, "dropped")
    cleaned = _sweep(store, tmp_path, ["issue-5-a1", "issue-5-b-a1", "issue-5-b-a2"])
    assert cleaned == [(5, "b")]


def test_sweep_cleans_side_a_and_keeps_a_running_side_b(store, tmp_path):
    a, b = _pair(store, flag=False)
    store.set_column(a.id, "dropped")
    cleaned = _sweep(store, tmp_path, ["issue-5-a1", "issue-5-b-a1"])
    assert cleaned == [(5, None)]


def test_sweep_keeps_both_running_sides_and_cleans_closed_issues(store, tmp_path):
    _pair(store, flag=False)
    done = _ticket(store, 7)
    store.set_column(done.id, "done")
    dirs = ["issue-5-a1", "issue-5-b-a1", "issue-7-a1", "issue-8-b-a1", "scratch-1", "chat-2"]
    assert _sweep(store, tmp_path, dirs) == [(7, None), (8, "b")]


def test_open_pr_cap_counts_each_sides_pr(store):
    a, b = _pair(store, flag=False)
    store.update_ticket(a.id, pr_number=11)
    assert open_fman_prs(store, REPO) == 1
    assert open_pr_cap_wait(store, store.get_ticket(b.id), 1) == (1, 1)  # B waits
    store.update_ticket(b.id, pr_number=12)
    assert open_fman_prs(store, REPO) == 2
