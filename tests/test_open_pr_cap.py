"""The open-PR cap (todo #77): a passed attempt waits to open its PR while the repo has
``max_open_prs_per_repo`` open ``fman/`` PRs; waiting is not a failure; 0 = no cap."""

from __future__ import annotations

import contextlib
import dataclasses
import threading
from pathlib import Path

import pytest

from fakes.opencode import Script, bash, say
from fakes.worker_env import REPO, USER, Env
from foreman_inator.config import Config, ConfigError, from_dict, to_dict
from foreman_inator.worker_queue import open_fman_prs, open_pr_wait_text

# Each attempt writes mod_<issue>.py, and each issue names that file, so the three
# tickets never wait on each other's file locks: only the PR cap can hold them.
SCRIPT = Script(
    [bash('n=$(basename "$PWD" | cut -d- -f2); echo "x = $n" > "mod_$n.py"'), say("Done.")]
)


def make_env(tmp_path: Path, cap: int) -> Env:
    e = Env(tmp_path, max_open_prs_per_repo=cap)
    e.store.set_verify_command(REPO, "ls mod_*.py")
    e.oc.set_script(SCRIPT)
    return e


def open_prs(e: Env) -> list[int]:
    return [i.number for i in e.gh.repos[REPO].items.values() if i.is_pr and i.state == "open"]


def label_three(e: Env) -> list[int]:
    return [e.label(f"Issue {i}", f"Add `mod_{i}.py`.") for i in (1, 2, 3)]


def settle(e: Env, rounds: int = 6) -> None:
    """Run passes until one changes nothing (at most ``rounds``)."""
    before = None
    for _ in range(rounds):
        e.once()
        now = [
            (t.column, t.pr_number, len(e.store.list_attempts(t.id)))
            for t in e.store.list_tickets()
        ]
        if now == before:
            return
        before = now


@pytest.mark.timeout(300)  # three full ticket runs on a real git repo
def test_third_pr_waits_at_the_cap_then_publishes_after_a_merge(tmp_path: Path) -> None:
    e = make_env(tmp_path, cap=2)
    try:
        a, b, c = label_three(e)
        settle(e)
        assert [e.ticket(n).column for n in (a, b)] == ["testing", "testing"]
        assert open_fman_prs(e.store, REPO) == 2
        assert len(open_prs(e)) == 2  # the third PR was not opened
        tc = e.ticket(c)
        (att,) = e.attempts(c)  # the attempt ran and passed: only the PR waits
        assert att.tests_passed and att.outcome is None and att.ended_at is not None
        assert tc.column == "in_progress" and tc.pr_number is None
        assert tc.flag_reason is None  # waiting is not a failure
        assert e.worker.job_for(tc) is None
        assert open_pr_wait_text(e.store, tc, 2) == "open-PR cap (2/2)"
        e.gh.merge_pr(REPO, e.ticket(a).pr_number, by=USER)
        settle(e, 3)
        assert e.ticket(a).column == "done"
        tc = e.ticket(c)
        assert tc.column == "testing" and tc.pr_number is not None
        assert len(e.attempts(c)) == 1  # published the kept attempt; no new attempt
        assert open_pr_wait_text(e.store, tc, 2) is None
    finally:
        e.close()


@pytest.mark.timeout(300)
def test_cap_zero_means_no_limit(tmp_path: Path) -> None:
    e = make_env(tmp_path, cap=0)
    try:
        ns = label_three(e)
        settle(e)
        assert [e.ticket(n).column for n in ns] == ["testing"] * 3
        assert open_fman_prs(e.store, REPO) == 3
    finally:
        e.close()


@pytest.mark.timeout(300)
def test_a_ticket_with_a_pr_is_never_held(tmp_path: Path) -> None:
    """A kickback's retry pushes onto its existing PR: it opens no new PR, so the cap
    (already full, counting that PR) does not apply to it."""
    e = make_env(tmp_path, cap=1)
    try:
        a = e.label("Issue 1", "Add `mod_1.py`.")
        settle(e, 3)
        t = e.ticket(a)
        assert t.pr_number is not None and open_fman_prs(e.store, REPO) == 1
        assert e.worker.open_pr_gate(t)
    finally:
        e.close()


@pytest.mark.timeout(300)
def test_two_concurrent_publishes_at_cap_minus_one_open_exactly_one_pr(tmp_path: Path) -> None:
    """max_parallel_per_repo > 1: two passed tickets publishing at once at cap - 1 must not
    both pass the gate; the check and the PR's creation are atomic per repo."""
    e = make_env(tmp_path, cap=1)
    try:
        a, b, c = label_three(e)
        settle(e)
        assert e.ticket(a).column == "testing" and open_fman_prs(e.store, REPO) == 1
        held = [e.ticket(n) for n in (b, c)]
        assert all(t.column == "in_progress" and t.pr_number is None for t in held)
        e.worker.config = dataclasses.replace(e.worker.config, max_open_prs_per_repo=2)
        # Both publishes reach PR creation together if nothing stops the second one.
        meet = threading.Barrier(2)
        real = e.gh.create_draft_pr

        def create(repo: str, **kw: object) -> object:
            with contextlib.suppress(threading.BrokenBarrierError):
                meet.wait(timeout=3)  # broken: the other publish is held at the gate
            return real(repo, **kw)

        e.gh.create_draft_pr = create  # type: ignore[method-assign]
        errors: list[BaseException] = []

        def publish(t: object) -> None:
            try:
                e.worker.publish_job_for(t.id, e.store.list_attempts(t.id)[-1])  # type: ignore[attr-defined]
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [threading.Thread(target=publish, args=(t,)) for t in held]
        for th in threads:
            th.start()
        for th in threads:
            th.join(60)
        assert not errors
        assert open_fman_prs(e.store, REPO) == 2
        assert len(open_prs(e)) == 2
        cols = sorted(e.ticket(n).column for n in (b, c))
        assert cols == ["in_progress", "testing"]
        waiting = next(e.ticket(n) for n in (b, c) if e.ticket(n).column == "in_progress")
        assert waiting.pr_number is None and waiting.flag_reason is None
    finally:
        e.close()


def test_config_key_default_validation_and_round_trip() -> None:
    assert Config().max_open_prs_per_repo == 5
    assert from_dict({"max_open_prs_per_repo": 0}).max_open_prs_per_repo == 0
    cfg = Config(max_open_prs_per_repo=3)
    assert from_dict(to_dict(cfg)) == cfg
    for bad in (-1, 101):
        with pytest.raises(ConfigError, match="max_open_prs_per_repo"):
            Config(max_open_prs_per_repo=bad)
    with pytest.raises(ConfigError, match="max_open_prs_per_repo"):
        from_dict({"max_open_prs_per_repo": "5"})
