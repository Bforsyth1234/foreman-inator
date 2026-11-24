"""#43: a PR that conflicts with its base branch.

Fake GitHub: ``mergeable=false`` in testing → one ``rebase`` attempt; ``null`` → nothing; a
third conflict → flagged, no attempt. A real git fixture with a conflicting base commit: the
worktree holds the markers and the listed files match. Migration 003 on a database that
already holds attempts keeps every row, index and foreign key.

Snapshot: tests/snapshots/ticket_prompt/rebase.txt (regenerate with
``UPDATE_SNAPSHOTS=1 uv run pytest -q tests/test_base_conflict.py``).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest

from fakes.github import FakeGitHub
from foreman_inator.config import Config
from foreman_inator.db import store as store_mod
from foreman_inator.db.store import Store
from foreman_inator.github.client import GitHubClient
from foreman_inator.github.poller import Poller, conflict_cursor
from foreman_inator.runner.worktree import WorktreeError, WorktreeManager
from foreman_inator.tickets.lifecycle import (
    REBASE_CAP,
    BaseConflict,
    IllegalTransition,
    ReviewSubmitted,
    Transition,
    apply,
    count_rebase_attempts,
    rebase_allowed,
    rebase_uncounted,
)
from foreman_inator.tickets.lifecycle import Action as A
from foreman_inator.tickets.prompt import PriorAttempt
from test_ticket_prompt import FAILED_OUTPUT, attempt, build, check_snapshot, ticket
from test_worktree import BOT, git, origin, remote_ref  # noqa: F401 (origin is a fixture)

REPO = "brooks/toy"
ME = "brooks"


# --------------------------------------------------------------------------- fake GitHub


class Env:
    """A ticket in testing with a draft PR, and a stand-in for the worker's handling."""

    def __init__(self, tmp_path: Path, *, conflicts: bool = True) -> None:
        self.gh = FakeGitHub()
        self.gh.create_repo(REPO)
        n = self.gh.create_issue(REPO, "Add a flag", "Please add --verbose.")
        self.store = Store(tmp_path / "fman.db")
        t = self.store.create_ticket(
            repo=REPO,
            issue_number=n,
            title="Add a flag",
            request="Please add --verbose.",
            base_branch="main",
        )
        self.branch = f"fman/issue-{n}/a1"
        self.gh.push_branch(REPO, self.branch)
        self.pr = self.gh.create_draft_pr(
            REPO, head=self.branch, base="main", title="t", body="b"
        ).number
        self.store.update_ticket(t.id, pr_number=self.pr)
        att = self.store.add_attempt(t.id, model="qwen", tier=2, trigger="first")
        self.store.finish_attempt(att.id, None, tests_passed=True)
        self.store.set_column(t.id, "in_progress")
        self.store.set_column(t.id, "testing")
        self.tid = t.id
        cfg = Config(allowed_users=(ME,), repos=(REPO,))
        self.poller = Poller(self.gh, self.store, cfg, conflicts=conflicts)
        self.log: list[tuple[object, Transition]] = []

    def handle(self, ev) -> None:
        t = self.store.get_ticket(self.tid)
        tr = apply(t.column, ev)
        self.log.append((ev, tr))
        if A.START_REBASE in tr.actions:
            # The worker's START_REBASE (runner wiring): one attempt with trigger
            # "rebase" on the tier of the PR's attempt, then the usual checks.
            pr_att = self.store.list_attempts(self.tid)[-1]
            self.store.add_attempt(self.tid, model=pr_att.model, tier=pr_att.tier, trigger="rebase")
        if A.FLAG_TICKET in tr.actions:
            self.store.flag_ticket(self.tid, "rebase_cap: the PR still conflicts with main")
        if tr.column != t.column:
            self.store.set_column(self.tid, tr.column)

    def poll(self) -> list:
        return list(self.poller.process(self.handle))

    def rebase_passes(self) -> None:
        """The rebase attempt resolved the conflicts and passed; force-pushed to the PR."""
        att = self.store.list_attempts(self.tid)[-1]
        assert att.trigger == "rebase"
        self.store.finish_attempt(att.id, None, tests_passed=True)
        self.gh.push_branch(REPO, self.branch)  # new PR head; GitHub recomputes mergeable
        self.store.set_column(self.tid, "testing")

    def rebases(self) -> int:
        return count_rebase_attempts(self.store.list_attempts(self.tid))


@pytest.fixture
def env(tmp_path: Path):
    e = Env(tmp_path)
    yield e
    e.store.close()


def test_conflict_in_testing_starts_one_rebase_attempt(env: Env):
    env.gh.push_base(REPO)
    env.gh.set_conflict_state(REPO, env.pr, False)
    (ev,) = env.poll()
    assert isinstance(ev, BaseConflict) and ev.rebases_done == 0
    assert env.log[-1][1] == Transition("in_progress", (A.START_REBASE,))
    assert env.rebases() == 1
    assert env.store.list_attempts(env.tid)[-1].tier == 2  # the tier that wrote the change
    assert env.store.get_ticket(env.tid).column == "in_progress"
    assert env.poll() == []  # same head: handled once (and not in testing any more)
    env.rebase_passes()
    env.gh.set_conflict_state(REPO, env.pr, True)
    assert env.poll() == []
    assert env.rebases() == 1


@pytest.mark.parametrize("value", [None, True])
def test_null_or_mergeable_does_nothing(env: Env, value):
    env.gh.push_base(REPO)  # "behind but clean" is mergeable=True
    env.gh.set_conflict_state(REPO, env.pr, value)
    assert env.poll() == []
    assert env.rebases() == 0
    assert env.store.get_ticket(env.tid).column == "testing"


def test_a_third_conflict_is_flagged_with_no_attempt(env: Env):
    for n in range(REBASE_CAP):
        env.gh.push_base(REPO)
        env.gh.set_conflict_state(REPO, env.pr, False)
        (ev,) = env.poll()
        assert ev.rebases_done == n
        env.rebase_passes()
    assert env.rebases() == REBASE_CAP
    env.gh.push_base(REPO)
    env.gh.set_conflict_state(REPO, env.pr, False)
    (ev,) = env.poll()
    assert ev.rebases_done == REBASE_CAP
    assert ev.reason.startswith("rebase_cap: PR #") and "after 2 rebase attempts" in ev.reason
    assert env.log[-1][1] == Transition("testing", (A.FLAG_TICKET,))
    assert env.rebases() == REBASE_CAP  # no third attempt
    t = env.store.get_ticket(env.tid)
    assert t.column == "testing" and t.flag_reason.startswith("rebase_cap")
    assert env.poll() == []  # flagged once, not on every poll
    assert (
        env.store.get_cursor(REPO, conflict_cursor(env.pr)) == env.gh.get_pr(REPO, env.pr).head_sha
    )


def test_conflict_outside_testing_is_ignored(env: Env):
    env.store.set_column(env.tid, "in_progress")
    env.gh.set_conflict_state(REPO, env.pr, False)
    assert env.poll() == []
    ev = BaseConflict(REPO, 1, ticket_id=1, pr_number=2, head_sha="a")
    for column in (None, "todo", "in_progress", "done", "dropped"):
        with pytest.raises(IllegalTransition):
            apply(column, ev)


def test_a_new_kickback_wins_over_a_conflict_in_the_same_poll(env: Env):
    env.gh.set_conflict_state(REPO, env.pr, False)
    env.gh.submit_review(REPO, env.pr, by=ME, state="REQUEST_CHANGES", body="also -q")
    events = env.poller.poll_once().events
    assert [type(e) for e in events] == [ReviewSubmitted]


def test_conflict_reads_are_off_unless_enabled(tmp_path: Path):
    e = Env(tmp_path, conflicts=False)
    e.gh.set_conflict_state(REPO, e.pr, False)
    assert e.poll() == []
    e.store.close()


def test_no_extra_requests(env: Env):
    quiet = env.poller.poll_once().requests[REPO]
    env.gh.set_conflict_state(REPO, env.pr, False)
    assert env.poller.poll_once().requests[REPO] == quiet


def test_real_client_reads_mergeable():
    def handler(request: httpx.Request) -> httpx.Response:
        n = int(request.url.path.rsplit("/", 1)[1])
        value = {1: False, 2: None, 3: True}[n]
        return httpx.Response(
            200,
            json={
                "number": n,
                "node_id": f"PR_{n}",
                "state": "open",
                "draft": True,
                "merged": False,
                "head": {"ref": "fman/issue-1/a1", "sha": "c" * 40},
                "base": {"ref": "main", "sha": "d" * 40},
                "mergeable": value,
                "mergeable_state": {1: "dirty", 2: "unknown", 3: "behind"}[n],
            },
        )

    class _Auth:
        def token(self) -> str:
            return "ghs_test"

        def invalidate(self) -> None:
            pass

    c = GitHubClient(_Auth(), transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    assert [c.get_pr(REPO, n).mergeable for n in (1, 2, 3)] == [False, None, True]
    assert c.get_pr(REPO, 1).head_sha == "c" * 40


# --------------------------------------------------------------------------- the cap


class Att:
    def __init__(self, id: int, trigger: str, outcome: str | None = None) -> None:
        self.id, self.trigger, self.outcome = id, trigger, outcome


def test_rebase_cap_counts_every_rebase_attempt():
    assert rebase_allowed([])
    assert rebase_allowed([Att(1, "first"), Att(2, "rebase", "failed_checks")])
    assert not rebase_allowed([Att(1, "first"), Att(2, "rebase"), Att(3, "rebase", "superseded")])
    assert count_rebase_attempts([Att(1, "kickback"), Att(2, "rebase")]) == 1


def test_rebase_attempts_kept_out_of_escalation_counts():
    history = [
        Att(1, "first", "failed_checks"),
        Att(2, "first", None),  # passed, on the PR
        Att(3, "rebase", "failed_checks"),  # not charged
        Att(4, "rebase", None),  # passed: carries the change on
    ]
    assert rebase_uncounted(history) == {2, 3}
    assert rebase_uncounted([Att(1, "first"), Att(2, "kickback")]) == frozenset()


# --------------------------------------------------------------------------- prompt


def test_rebase_prompt_snapshot_lists_the_conflicted_files():
    prior = PriorAttempt(attempt(1, outcome=None, tests_passed=True))
    wp = build(
        ticket(),
        attempt(2, "rebase"),
        (prior,),
        conflicted_files=("src/widgets/summary.py", "tests/test_summary.py"),
    )
    check_snapshot("rebase", wp.text)
    assert wp.situation == "rebase"
    assert "- src/widgets/summary.py\n- tests/test_summary.py" in wp.text
    assert "Earlier attempts" not in wp.text


def test_rebase_prompt_clean_replay_and_retry_after_failed_checks():
    clean = build(ticket(), attempt(2, "rebase"), (PriorAttempt(attempt(1)),)).text
    assert "git found no conflicting lines" in clean
    failed = PriorAttempt(
        attempt(2, "rebase", outcome="failed_checks"), failure_output=FAILED_OUTPUT
    )
    text = build(
        ticket(), attempt(3, "rebase"), (PriorAttempt(attempt(1)), failed), conflicted_files=("a",)
    ).text
    assert "IndexError: list index out of range" in text


# --------------------------------------------------------------------------- real git


@pytest.fixture
def remote(origin: dict) -> dict:  # noqa: F811 (test_worktree's bare repo, branch trunk)
    return origin


def _pr_and_moved_base(remote: dict, tmp_path: Path) -> tuple[str, str]:
    """The PR branch edits app.py and adds docs.md; then the base edits the same line."""
    work = tmp_path / "work"
    git("clone", "-q", "--branch", "trunk", remote["url"], str(work), cwd=tmp_path)
    git("checkout", "-q", "-b", "fman/issue-1/a1", cwd=work)
    (work / "app.py").write_text("def hello():\n    return 'hello from the PR'\n")
    (work / "docs.md").write_text("# docs\n")
    git("add", "-A", cwd=work)
    git("commit", "-q", "--no-gpg-sign", "-m", "pr change", cwd=work)
    git("push", "-q", "origin", "fman/issue-1/a1", cwd=work)
    pr_head = git("rev-parse", "HEAD", cwd=work)
    git("checkout", "-q", "trunk", cwd=work)
    (work / "app.py").write_text("def hello():\n    return 'hello from trunk'\n")
    (work / "README.md").write_text("# widgets, moved on\n")
    git("add", "-A", cwd=work)
    git("commit", "-q", "--no-gpg-sign", "-m", "base moves", cwd=work)
    git("push", "-q", "origin", "trunk", cwd=work)
    return pr_head, git("rev-parse", "HEAD", cwd=work)


def test_replay_leaves_conflict_markers_and_lists_the_files(remote, tmp_path: Path):
    pr_head, new_base = _pr_and_moved_base(remote, tmp_path)
    mgr = WorktreeManager(tmp_path / "home" / "worktrees", identity=BOT)
    mirror = mgr.mirror("acme/widgets", remote["url"])
    rp = mirror.replay_onto_base("trunk", 1, 2, previous="fman/issue-1/a1")
    wt = rp.worktree
    assert rp.previous_sha == pr_head and not rp.clean
    assert rp.conflicted == ("app.py",)
    assert rp.conflicted == tuple(wt.conflicted_files())
    text = (wt.path / "app.py").read_text()
    assert "<<<<<<<" in text and "=======" in text and ">>>>>>>" in text
    assert "hello from the PR" in text and "hello from trunk" in text
    # the non-conflicting parts of the change are there, on top of the new base
    assert (wt.path / "docs.md").read_text() == "# docs\n"
    assert (wt.path / "README.md").read_text() == "# widgets, moved on\n"
    assert wt.base_sha == new_base and wt.branch == "fman/issue-1/a2"
    assert git("rev-parse", "HEAD", cwd=wt.path) == new_base  # nothing committed

    # The model resolves; the worker commits and force-pushes to the PR's own branch.
    (wt.path / "app.py").write_text("def hello():\n    return 'hello from both'\n")
    sha = wt.commit_all("rebase onto trunk")
    assert sha and wt.conflicted_files() == []
    assert set(wt.changed_files()) == {"app.py", "docs.md"}
    wt.push(None, remote_branch="fman/issue-1/a1", force=True)
    assert remote_ref(remote, "refs/heads/fman/issue-1/a1") == sha
    parents = git("rev-list", "--parents", "-n", "1", sha, cwd=remote["path"]).split()
    assert parents[1:] == [new_base]  # linear on the new base, no merge commit


def test_clean_replay_has_no_conflicts(remote, tmp_path: Path):
    work = tmp_path / "work"
    git("clone", "-q", "--branch", "trunk", remote["url"], str(work), cwd=tmp_path)
    git("checkout", "-q", "-b", "fman/issue-1/a1", cwd=work)
    (work / "docs.md").write_text("# docs\n")
    git("add", "-A", cwd=work)
    git("commit", "-q", "--no-gpg-sign", "-m", "pr", cwd=work)
    git("push", "-q", "origin", "fman/issue-1/a1", cwd=work)
    git("checkout", "-q", "trunk", cwd=work)
    (work / "README.md").write_text("# moved\n")
    git("commit", "-q", "--no-gpg-sign", "-am", "base", cwd=work)
    git("push", "-q", "origin", "trunk", cwd=work)
    mirror = WorktreeManager(tmp_path / "wt", identity=BOT).mirror("acme/widgets", remote["url"])
    rp = mirror.replay_onto_base("trunk", 1, 2, previous="fman/issue-1/a1")
    assert rp.clean and rp.conflicted == ()
    assert (rp.worktree.path / "docs.md").exists()
    assert rp.worktree.changed_files() == ["docs.md"]


def test_replay_of_an_unknown_ref_raises(remote, tmp_path: Path):
    mirror = WorktreeManager(tmp_path / "wt", identity=BOT).mirror("acme/widgets", remote["url"])
    with pytest.raises(WorktreeError):
        mirror.replay_onto_base("trunk", 1, 2, previous="fman/issue-9/a1")


# --------------------------------------------------------------------------- migration 003


def _copy(folder: Path, *names: str) -> None:
    for n in names:
        src = store_mod.default_migrations().joinpath(n)
        (folder / n).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def _schema(path: Path, sql: str) -> list:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_migration_003_keeps_rows_indexes_and_foreign_keys(tmp_path: Path):
    folder = tmp_path / "migrations"
    folder.mkdir()
    _copy(folder, "001_initial.sql", "002_ci.sql")
    db = tmp_path / "fman.db"
    with Store(db, migrations=folder) as s:
        assert s.schema_version() == 2
        t = s.create_ticket(repo=REPO, issue_number=1, title="t", request="r", base_branch="main")
        a1 = s.add_attempt(t.id, model="qwen", tier=1, trigger="first")
        s.finish_attempt(a1.id, "failed_checks", tests_passed=False, verify_output_path="/v")
        a2 = s.add_attempt(t.id, model="qwen", tier=1, trigger="kickback", base_commit="b" * 40)
        s.add_attempt_usage(a2.id, tokens_in=10, tokens_out=5, cost_usd=0.25)
        s.finish_attempt(a2.id, None, tests_passed=True)
        s.set_ci_result(a2.id, "passed")
        s.record_kickback(a2.id, by="user", tag="style", note="n")
        s.record_review(a2.id, reviewer_model="glm", verdict="approve")
        s.record_spend("live", 0.25, model="glm", ticket_id=t.id, attempt_id=a2.id)
        s.set_accepted_attempt(t.id, a2.id)
        a3 = s.add_attempt(t.id, model="qwen", tier=2, trigger="escalation")
        with pytest.raises(sqlite3.IntegrityError):
            s.add_attempt(t.id, model="qwen", tier=1, trigger="rebase")  # not yet allowed
        before = s.query("SELECT * FROM attempts ORDER BY id")
        before_rows = [tuple(r) for r in before]
        s.query("DELETE FROM attempts WHERE id = ?", (a3.id,))  # newest row gone
    fks_before = {
        tbl: _schema(db, f"PRAGMA foreign_key_list({tbl})")
        for tbl in ("tickets", "attempts", "kickbacks", "reviews", "spend_ledger")
    }
    _copy(folder, "003_rebase.sql")
    with Store(db, migrations=folder, auto_migrate=False) as s:
        assert s.migrate() == [3]
        after = [tuple(r) for r in s.query("SELECT * FROM attempts ORDER BY id")]
        assert after == before_rows[:2]  # every row kept, ids and values unchanged
        assert s.query("PRAGMA foreign_keys")[0][0] == 1  # back on after the rebuild
        assert s.query("PRAGMA foreign_key_check") == []
        new = s.add_attempt(1, model="qwen", tier=2, trigger="rebase")
        assert new.id == a3.id + 1  # AUTOINCREMENT high-water mark kept: no id reuse
        assert new.trigger == "rebase"
        with pytest.raises(sqlite3.IntegrityError):
            s.add_attempt(1, model="qwen", tier=1, trigger="redo")
        with pytest.raises(sqlite3.IntegrityError):
            s.add_attempt(999, model="qwen", tier=1, trigger="first")  # FK still enforced
        with pytest.raises(sqlite3.IntegrityError):
            s.record_kickback(12345, by="user")  # children still reference attempts
        assert s.get_attempt(a2.id).ci_status == "passed"
        assert s.get_ticket(1).accepted_attempt_id == a2.id
        assert s.list_kickbacks(attempt_id=a2.id)[0].note == "n"
    indexes = {
        r[0]
        for r in _schema(
            db, "SELECT name FROM sqlite_master WHERE tbl_name='attempts' AND type='index'"
        )
    }
    assert {"attempts_ticket", "attempts_model"} <= indexes
    fks_after = {tbl: _schema(db, f"PRAGMA foreign_key_list({tbl})") for tbl in fks_before}
    assert fks_after == fks_before
    tables = {r[0] for r in _schema(db, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "attempts_new" not in tables


def test_migration_003_rolls_back_when_foreign_keys_break(tmp_path: Path):
    folder = tmp_path / "migrations"
    folder.mkdir()
    _copy(folder, "001_initial.sql", "002_ci.sql")
    db = tmp_path / "fman.db"
    with Store(db, migrations=folder) as s:
        t = s.create_ticket(repo=REPO, issue_number=1, title="t", request="r", base_branch="main")
        a = s.add_attempt(t.id, model="qwen", tier=1, trigger="first")
        s.record_kickback(a.id, by="user")
    broken = store_mod.default_migrations().joinpath("003_rebase.sql").read_text()
    broken = broken.replace("FROM attempts;", "FROM attempts WHERE 0;")  # loses the rows
    (folder / "003_rebase.sql").write_text(broken)
    with Store(db, migrations=folder, auto_migrate=False) as s:
        with pytest.raises(store_mod.StoreError, match="broke foreign keys"):
            s.migrate()
        assert s.schema_version() == 2
        assert len(s.list_attempts(t.id)) == 1
        assert s.query("PRAGMA foreign_keys")[0][0] == 1
