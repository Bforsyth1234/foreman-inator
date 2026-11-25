"""#24 (todo 47): post-merge quality signal. A merged ticket that is reverted on its base
branch, or followed up by another accepted ticket on the same files within 7 days, gets
one ``post_merge_signals`` row; the scan is idempotent and its GitHub calls are bounded.

Non-vacuous: in ``metrics/quality.py`` make ``is_revert_of`` ignore the
``This reverts commit <sha>`` form (drop the ``_REVERTS_SHA`` loop) and
``test_git_revert_of_merge_commit_is_recorded_once`` fails (no signal recorded)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from fakes.github import FakeGitHub
from foreman_inator.db.store import Store, Ticket
from foreman_inator.github.client import Commit, GitHubClient, pr_from_json
from foreman_inator.metrics import quality
from foreman_inator.worker_quality import QUALITY_INTERVAL_SECONDS, QualitySteps

REPO = "octo/widgets"
#: FakeGitHub's TickClock starts at 2026-11-23 09:00; the store's "now" is after the merges.
NOW = datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

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
def gh() -> FakeGitHub:
    fake = FakeGitHub()
    fake.create_repo(REPO)
    return fake


def _merged(
    store: Store,
    gh: FakeGitHub,
    clock: Clock,
    n: int,
    *,
    files: tuple[str, ...] = ("src/a.py",),
    model: str = "qwen",
    title: str | None = None,
    done_at: datetime | None = None,
    created_at: datetime | None = None,
) -> Ticket:
    """A ticket whose PR the user merged on the fake; done_at on the store clock."""
    title = title or f"Fix thing {n}"
    issue_number = gh.create_issue(REPO, title=title, body="b", author="brooks")
    branch = f"fman/issue-{issue_number}/a1"
    gh.push_branch(REPO, branch)
    pr = gh.create_draft_pr(REPO, head=branch, base="main", title=title, body="Closes #1")
    gh.mark_ready_for_review(REPO, pr.number)
    gh.merge_pr(REPO, pr.number)
    saved = clock.now
    if created_at is not None:
        clock.now = created_at
    t = store.create_ticket(
        repo=REPO,
        issue_number=issue_number,
        title=title,
        request="r",
        base_branch="main",
        owned_files=files,
    )
    clock.now = done_at or saved
    a = store.add_attempt(t.id, model=model, tier=1, trigger="first")
    store.finish_attempt(a.id, "superseded")
    store.update_ticket(t.id, pr_number=pr.number)
    store.set_accepted_attempt(t.id, a.id)
    store.set_column(t.id, "done")
    clock.now = saved
    return store.get_ticket(t.id)


def _calls(gh: FakeGitHub, method: str) -> list[Any]:
    return [c for c in gh.calls if c.method == method]


# --------------------------------------------------------------------------- reverts


def test_git_revert_of_merge_commit_is_recorded_once(
    store: Store, gh: FakeGitHub, clock: Clock
) -> None:
    t = _merged(store, gh, clock, 1)
    other = _merged(store, gh, clock, 2, files=("src/b.py",))
    merge_sha = gh.get_pr(REPO, t.pr_number).merge_commit_sha  # type: ignore[arg-type]
    assert len(merge_sha) == 40
    gh.add_base_commit(REPO, "Unrelated change\n\nThis reverts commit " + "0" * 40 + ".")
    gh.add_base_commit(REPO, f'Revert "Fix thing 1"\n\nThis reverts commit {merge_sha}.')

    report = quality.scan(store, gh)

    assert report.recorded == [(t.id, "reverted")] and report.errors == []
    (sig,) = store.list_post_merge_signals()
    assert (sig.ticket_id, sig.kind, sig.attempt_id) == (t.id, "reverted", t.accepted_attempt_id)
    assert sig.detail.startswith(gh.repos[REPO].branches["main"][:12])
    assert store.list_post_merge_signals(ticket_id=other.id) == []

    again = quality.scan(store, gh)  # idempotent: nothing new, and t is not re-fetched
    assert again.recorded == [] and len(store.list_post_merge_signals()) == 1
    fetched = [c.args["number"] for c in _calls(gh, "get_pr")]
    assert fetched.count(t.pr_number) == 2  # once by the test, once by the first scan

    assert quality.quality_by_model(store) == [quality.ModelQuality("qwen", 2, 1, 0)]


def test_squash_merged_revert_pr_matches_title_and_pr_number(
    store: Store, gh: FakeGitHub, clock: Clock
) -> None:
    t = _merged(store, gh, clock, 1, title="Add widget cache")
    gh.add_base_commit(REPO, f'Revert "Add widget cache" (#99)\n\nReverts {REPO}#{t.pr_number}')
    assert quality.scan(store, gh).recorded == [(t.id, "reverted")]


def test_title_without_pr_number_is_not_a_revert() -> None:
    msg = 'Revert "Add widget cache" (#99)\n\nReverts octo/widgets#40'
    assert not quality.is_revert_of(msg, merge_sha="", pr_title="Add widget cache", pr_number=4)
    assert quality.is_revert_of(msg, merge_sha="", pr_title="Add widget cache", pr_number=40)
    assert quality.is_revert_of("This reverts commit abcdef1.", merge_sha="abcdef1" + "0" * 33,
                                pr_title="x", pr_number=1)  # fmt: skip


def test_revert_before_the_merge_does_not_count() -> None:
    merged_at = datetime(2026, 11, 23, tzinfo=UTC)
    early = Commit("c1", "This reverts commit " + "a" * 40, merged_at - timedelta(hours=1))
    assert (
        quality.find_revert(
            [early], merge_sha="a" * 40, pr_title="t", pr_number=1, merged_at=merged_at
        )
        is None
    )


def test_scan_is_bounded_to_window_and_one_list_commits_per_repo(
    store: Store, gh: FakeGitHub, clock: Clock
) -> None:
    old = _merged(store, gh, clock, 1, done_at=NOW - timedelta(days=20))
    _merged(store, gh, clock, 2)
    _merged(store, gh, clock, 3, files=("x.py",))
    gh.calls.clear()

    report = quality.scan(store, gh)

    assert report.tickets == 2
    assert old.pr_number not in [c.args["number"] for c in _calls(gh, "get_pr")]
    assert len(_calls(gh, "get_pr")) == 2
    (lc,) = _calls(gh, "list_commits")
    assert lc.args["sha"] == "main" and lc.args["since"] is not None


def test_a_failing_repo_is_reported_not_raised(store: Store, gh: FakeGitHub, clock: Clock) -> None:
    _merged(store, gh, clock, 1)
    gh.fail_next("list_commits", RuntimeError("boom"))
    report = quality.scan(store, gh)
    assert report.recorded == [] and len(report.errors) == 1 and "boom" in report.errors[0]


# --------------------------------------------------------------------------- follow-ups


def test_followup_on_overlapping_files_within_seven_days(
    store: Store, gh: FakeGitHub, clock: Clock
) -> None:
    base = NOW - timedelta(days=10)
    t = _merged(store, gh, clock, 1, files=("src/a.py", "src/b.py"), done_at=base,
                created_at=base - timedelta(hours=2))  # fmt: skip
    # different files: no follow-up
    _merged(store, gh, clock, 2, files=("docs/x.md",), created_at=base + timedelta(days=1))
    # same files but filed 8 days later: too late
    _merged(store, gh, clock, 3, files=("src/a.py",), created_at=base + timedelta(days=8))
    # same file, 3 days later: the follow-up
    f = _merged(store, gh, clock, 4, files=("src/b.py",), created_at=base + timedelta(days=3))

    report = quality.scan(store, gh)

    assert (t.id, "followup") in report.recorded
    sigs = store.list_post_merge_signals(kind="followup")
    assert [s.ticket_id for s in sigs] == [t.id]
    assert sigs[0].at == f.created_at and f"ticket {f.id}" in sigs[0].detail
    assert "src/b.py" in sigs[0].detail
    assert quality.scan(store, gh).recorded == []


def test_find_followup_needs_a_later_ticket() -> None:
    at = datetime(2026, 11, 1, tzinfo=UTC)

    def tk(i: int, created: datetime, files: tuple[str, ...]) -> Ticket:
        return Ticket(i, "t", "r", REPO, "main", i, i, files, "done", "issue", None, None,
                      i, created, created + timedelta(hours=1), 1, None, None)  # fmt: skip

    t = tk(1, at, ("a.py",))
    earlier = tk(2, at - timedelta(days=1), ("a.py",))  # filed before the merge
    assert quality.find_followup(t, [t, earlier]) is None
    later = tk(3, at + timedelta(days=2), ("a.py",))
    assert quality.find_followup(t, [t, earlier, later]) == (later, ("a.py",))


# --------------------------------------------------------------------------- store / schema


def test_store_records_a_signal_once_and_checks_kind(store: Store, gh: FakeGitHub, clock: Clock):
    t = _merged(store, gh, clock, 1)
    assert store.record_post_merge_signal(t.id, "reverted", at=NOW, detail="x") is True
    assert store.record_post_merge_signal(t.id, "reverted", at=NOW, detail="y") is False
    (sig,) = store.list_post_merge_signals(ticket_id=t.id)
    assert sig.detail == "x" and sig.at == NOW and sig.recorded_at == NOW
    with pytest.raises(ValueError):
        store.record_post_merge_signal(t.id, "broke", at=NOW)  # type: ignore[arg-type]
    assert [x.id for x in store.merged_tickets(since=NOW - timedelta(days=1))] == [t.id]
    assert store.merged_tickets(since=NOW + timedelta(seconds=1)) == []


# --------------------------------------------------------------------------- GitHub client


def test_pr_merge_commit_sha_only_after_merge() -> None:
    base = {
        "number": 5,
        "node_id": "n",
        "state": "open",
        "head": {"ref": "fman/x", "sha": "h"},
        "base": {"ref": "main"},
        "merge_commit_sha": "test-merge",
        "merged_at": None,
    }
    assert pr_from_json(base).merge_commit_sha == ""
    merged = {**base, "state": "closed", "merged_at": "2026-11-23T10:00:00Z"}
    assert pr_from_json(merged).merge_commit_sha == "test-merge"
    assert pr_from_json({**merged, "merge_commit_sha": None}).merge_commit_sha == ""


def test_client_list_commits_sends_sha_and_since(rsa_pem: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(
                201, json={"token": "ghs_t", "expires_at": "2099-01-01T00:00:00Z"}
            )
        seen.append(request)
        body = [
            {
                "sha": "c" * 40,
                "commit": {"message": "Revert x", "committer": {"date": "2026-11-23T11:00:00Z"}},
                "parents": [{"sha": "p" * 40}],
            }
        ]
        return httpx.Response(200, content=json.dumps(body))

    client = GitHubClient.for_installation(
        1, rsa_pem, 42, transport=httpx.MockTransport(handler), sleep=lambda s: None
    )
    since = datetime(2026, 11, 20, 8, 30, tzinfo=UTC)
    (c,) = client.list_commits(REPO, sha="main", since=since)
    (req,) = seen
    assert req.url.path == f"/repos/{REPO}/commits"
    assert dict(parse_qsl(req.url.query.decode())) == {
        "per_page": "100",
        "sha": "main",
        "since": "2026-11-20T08:30:00Z",
    }
    assert c == Commit("c" * 40, "Revert x", datetime(2026, 11, 23, 11, tzinfo=UTC), ("p" * 40,))


@pytest.fixture(scope="module")
def rsa_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


# --------------------------------------------------------------------------- worker mixin


class _W(QualitySteps):
    def __init__(self, store: Store, api: Any) -> None:
        self.store, self.api = store, api
        self.t = 1000.0
        self.clock = lambda: self.t


def test_worker_scans_at_most_once_a_day(store: Store, gh: FakeGitHub, clock: Clock) -> None:
    _merged(store, gh, clock, 1)
    w = _W(store, gh)
    assert w.maybe_scan_quality() is not None
    w.t += QUALITY_INTERVAL_SECONDS - 1
    assert w.maybe_scan_quality() is None
    w.t += 1
    assert w.maybe_scan_quality() is not None
    assert len(_calls(gh, "list_commits")) == 2


def test_worker_scan_failure_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    broken = SimpleNamespace(merged_tickets=lambda **k: 1 / 0, now=lambda: NOW)
    w = _W(broken, None)  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING, logger="foreman_inator.worker"):
        assert w.maybe_scan_quality() is None
    assert "post-merge scan failed" in caplog.text
