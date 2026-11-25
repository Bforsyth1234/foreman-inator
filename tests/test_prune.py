"""#56: retention. Recordings/traces of unmerged tickets go after N days, merged ones
stay; scratch leftovers and orphaned package caches are swept; dry runs touch nothing;
the worker prunes at most once a day on its clock."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from foreman_inator import prune
from foreman_inator.db.store import Store
from foreman_inator.worker_prune import PRUNE_INTERVAL_SECONDS, PruneSteps

T0 = datetime(2026, 1, 1, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock):
    s = Store(tmp_path / "db.sqlite", clock=clock)
    yield s
    s.close()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


def _ticket(store: Store, n: int, *, repo: str = "you/project", column: str = "done"):
    t = store.create_ticket(
        repo=repo, issue_number=n, title=f"t{n}", request="r", base_branch="main"
    )
    if column != "todo":
        store.set_column(t.id, column)  # type: ignore[arg-type]
    return t


def _attempt(store: Store, ticket_id: int, *, ended: bool = True):
    a = store.add_attempt(ticket_id, model="m", tier=1, trigger="first")
    if ended:
        store.finish_attempt(a.id, "superseded")
    return a


def _file(p: Path, at: datetime, text: str = "x") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    _touch(p, at)
    return p


def _touch(p: Path, at: datetime) -> None:
    ts = at.timestamp()
    os.utime(p, (ts, ts), follow_symlinks=False)


def _dir(p: Path, at: datetime) -> Path:
    _file(p / "f.txt", at)
    _touch(p, at)
    return p


def _recording(home: Path, ticket_id: int, attempt_id: int, at: datetime) -> Path:
    rec = _file(home / "recordings" / str(ticket_id) / f"{attempt_id}.jsonl.gz", at)
    _file(home / "recordings" / str(ticket_id) / f"{attempt_id}.files" / "0001-llm.txt", at)
    return rec


def _side(rec: Path) -> Path:
    return rec.parent / (rec.name.removesuffix(".jsonl.gz") + ".files")


# --------------------------------------------------------------------------- recordings


def _merged_and_unmerged(store: Store, home: Path):
    merged = _ticket(store, 1)
    m_att = _attempt(store, merged.id)
    store.set_accepted_attempt(merged.id, m_att.id)
    unmerged = _ticket(store, 2)
    u_att = _attempt(store, unmerged.id)
    old = T0 - timedelta(days=1)
    return (
        _recording(home, merged.id, m_att.id, old),
        _recording(home, unmerged.id, u_att.id, old),
        _file(home / "traces" / str(unmerged.id) / f"attempt-{u_att.id}-verify.log", old),
        _file(home / "traces" / str(unmerged.id) / ("ab" * 16 + ".jsonl"), old),
    )


def test_worker_prunes_91_day_old_unmerged_and_keeps_merged(store, home, clock):
    merged_rec, unmerged_rec, verify_log, span_file = _merged_and_unmerged(store, home)
    mono = [1000.0]
    w = SimpleNamespace(
        store=store,
        config=SimpleNamespace(repos=("you/project",)),
        clock=lambda: mono[0],
        recordings_dir=home / "recordings",
        traces_dir=home / "traces",
    )

    class W(PruneSteps):
        def __init__(self) -> None:
            self.__dict__.update(vars(w))

    worker = W()
    clock.now = T0 + timedelta(days=89)
    first = worker.maybe_prune(home=home)
    assert first is not None and first.removed == []
    assert unmerged_rec.exists()

    clock.now = T0 + timedelta(days=90)  # store clock moved; worker clock did not
    assert worker.maybe_prune(home=home) is None  # not a day yet on the worker clock
    assert unmerged_rec.exists()

    mono[0] += PRUNE_INTERVAL_SECONDS
    report = worker.maybe_prune(home=home)
    assert report is not None
    assert not unmerged_rec.exists()
    assert not (home / "recordings" / "2").exists()  # side files and empty dir too
    assert not verify_log.exists() and not span_file.exists()
    assert merged_rec.exists()
    assert _side(merged_rec).exists()


def test_dry_run_lists_without_deleting(store, home, clock):
    merged_rec, unmerged_rec, verify_log, _ = _merged_and_unmerged(store, home)
    clock.now = T0 + timedelta(days=91)
    report = prune.run(store, home, days=90, dry_run=True)
    listed = {r.path for r in report.removed}
    assert unmerged_rec in listed and verify_log in listed
    assert _side(unmerged_rec) in listed
    assert merged_rec not in listed
    assert unmerged_rec.exists() and verify_log.exists()
    assert all(line.startswith("would delete") for line in report.lines()[:-1])

    real = prune.run(store, home, days=90)
    assert {r.path for r in real.removed} == listed
    assert not unmerged_rec.exists()


def test_open_ticket_and_running_attempt_are_kept(store, home, clock):
    open_t = _ticket(store, 3, column="testing")
    a_open = _attempt(store, open_t.id)
    dropped = _ticket(store, 4, column="dropped")
    a_running = _attempt(store, dropped.id, ended=False)
    old = T0 - timedelta(days=200)
    rec_open = _recording(home, open_t.id, a_open.id, old)
    rec_running = _recording(home, dropped.id, a_running.id, old)
    clock.now = T0 + timedelta(days=200)
    prune.run(store, home)
    assert rec_open.exists() and rec_running.exists()


def test_days_below_one_is_refused(store, home):
    with pytest.raises(ValueError):
        prune.run(store, home, days=0)


# --------------------------------------------------------------------------- scratch


def test_scratch_sweep_spares_running_and_recent(store, home, clock):
    done = _ticket(store, 5, column="dropped")
    ended = _attempt(store, done.id)
    running = _attempt(store, _ticket(store, 6, column="in_progress").id, ended=False)
    merged = _ticket(store, 7)
    m_att = _attempt(store, merged.id)
    store.set_accepted_attempt(merged.id, m_att.id)
    old = T0 - timedelta(days=2)
    sb = home / "sandbox"
    ended_writer = _dir(sb / f"a{ended.id}-writer", old)
    ended_review = _dir(sb / f"r{ended.id}-work", old)
    running_writer = _dir(sb / f"a{running.id}-writer", old)
    merged_scratch = _dir(sb / f"a{m_att.id}-verify", old)
    fresh_ended = _dir(sb / f"a{ended.id}-verify", T0 - timedelta(hours=1))
    odd_recent = _dir(sb / "something", old)  # no id: waits the full retention
    oc = home / "opencode" / "attempts"
    oc_ended = _dir(oc / f"a{ended.id}-3f9a1c", old)
    oc_running = _dir(oc / f"a{running.id}-0a0b0c", old)
    oc_warmup_old = _dir(oc / "warm-up-abcdef", T0 - timedelta(days=100))

    report = prune.run(store, home)
    gone = {r.path for r in report.removed}
    assert gone == {ended_writer, ended_review, oc_ended, oc_warmup_old}
    for p in (running_writer, merged_scratch, fresh_ended, odd_recent, oc_running):
        assert p.exists(), p
    for p in gone:
        assert not p.exists(), p


def test_read_only_scratch_tree_is_removed(store, home):
    ended = _attempt(store, _ticket(store, 8, column="dropped").id)
    p = home / "sandbox" / f"a{ended.id}-install"
    inner = _file(p / "cache" / "go" / "mod.go", T0 - timedelta(days=3))
    os.chmod(inner, 0o444)
    os.chmod(inner.parent, 0o555)
    _touch(p, T0 - timedelta(days=3))
    _touch(p / "cache", T0 - timedelta(days=3))
    report = prune.run(store, home)
    assert report.errors == [] and not p.exists()


def test_experiment_scratch_and_work(store, home, clock):
    store.upsert_variant("m/w@1", role="writer", model="m", prompt_name="w", prompt_hash="1")
    store.upsert_variant("m/w@2", role="writer", model="m", prompt_name="w", prompt_hash="2")
    live = store.create_experiment(
        role="writer", champion_variant="m/w@1", challenger_variant="m/w@2", mode="replay"
    )
    over = store.create_experiment(
        role="writer", champion_variant="m/w@1", challenger_variant="m/w@2", mode="replay"
    )
    store.end_experiment(over.id, status="rejected")
    old = T0 - timedelta(days=100)
    sb = home / "sandbox"
    live_sb = _dir(sb / f"replay-e{live.id}-c1-champion-verify", old)
    over_sb = _dir(sb / f"replay-e{over.id}-c1-champion-verify", old)
    work = home / "replays" / "work"
    live_work = _dir(work / f"e{live.id}", old)
    over_work = _dir(work / f"e{over.id}", old)
    over_recent = _dir(work / "e999", T0 - timedelta(days=10))

    gone = {r.path for r in prune.run(store, home).removed}
    assert gone == {over_sb, over_work}
    assert live_sb.exists() and live_work.exists() and over_recent.exists()


# --------------------------------------------------------------------------- caches


def test_caches_of_removed_repos_go(store, home):
    cache = home / "cache"
    kept = _dir(cache / "you" / "project", T0)
    orphan = _dir(cache / "you" / "old", T0)
    other_owner = _dir(cache / "someone" / "gone", T0)
    busy = _dir(cache / "busy" / "repo", T0)
    _attempt(store, _ticket(store, 9, repo="Busy/Repo", column="in_progress").id, ended=False)

    assert prune.run(store, home, repos=None).removed == []  # no repo list: nothing
    assert prune.run(store, home, repos=()).removed == []

    dry = prune.run(store, home, repos=("You/Project",), dry_run=True)
    assert {r.path for r in dry.removed} == {orphan, other_owner}
    assert orphan.exists()

    prune.run(store, home, repos=("You/Project",))
    assert kept.exists() and busy.exists()
    assert not orphan.exists() and not (cache / "someone").exists()
