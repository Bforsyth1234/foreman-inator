"""Repo profiles (todo #51, GH #28): per-repo ``writer`` and ``review`` settings.

Pure parts (``routing.profile``, escalation with ``Limits``) are tested on hand-built rows
from ``test_escalation``; the review modes run through the real worker (``fakes.worker_env``)
and the real reviewer (``test_reviewer``'s fixture PRs); thrifty's approval through the
real ``Spend`` and store.
"""

from __future__ import annotations

import dataclasses
import random
import sys
from pathlib import Path

import pytest

import test_escalation as te
from fakes.worker_env import REPO, Env, passing
from foreman_inator.config import SpendCaps
from foreman_inator.db.store import Store
from foreman_inator.routing import escalation as esc
from foreman_inator.routing import profile as prof
from foreman_inator.routing.escalation import Escalate, HandToUser, Retry, next_step
from foreman_inator.spend import FLAG_SPEND_CAP, FLAG_THRIFTY, Spend
from foreman_inator.worker_route import start_cursor
from foreman_inator.worker_status import render_status, set_profile
from test_reviewer import (  # noqa: F401  (fixtures)
    GOOD,
    VACUOUS,
    cov_python,
    make_pr,
    models,
    proxy,
    run,
)

LADDER = te.LADDER
Q1, Q2, GLM, CLAUDE, FABLE = LADDER
TIERS = (1, 2, 3)
ALL_PROFILES = [prof.Profile(w, r) for w in prof.WRITER_MODES for r in prof.REVIEW_MODES]


def limits(name: str) -> esc.Limits:
    return prof.limits_for(prof.preset(name))


def step(t: te.Ticket, lim: esc.Limits | None = None, start: int | None = None):
    return next_step(dataclasses.replace(t.history(), start_tier=start), LADDER, lim)


# =========================================================================== profiles


def test_presets_and_modes():
    assert {
        "prototype": prof.Profile("fast", "off"),
        "personal": prof.Profile("thrifty", "light"),
        "default": prof.Profile("normal", "normal"),
        "production": prof.Profile("fast", "strict"),
    } == prof.PRESETS
    assert prof.profile_of(None) == prof.DEFAULT
    assert prof.describe(prof.preset("Prototype")) == "prototype (writer fast, review off)"
    assert prof.describe(prof.Profile("fast", "light")) == "writer fast, review light"
    assert prof.pr_line(prof.DEFAULT) == "Repo profile: default (writer normal, review normal)"
    assert prof.resolve("personal", review="strict") == prof.Profile("thrifty", "strict")
    assert prof.resolve(writer="fast", current=prof.Profile("thrifty", "off")) == prof.Profile(
        "fast", "off"
    )


def test_unknown_preset_or_mode_is_rejected_with_the_valid_names():
    with pytest.raises(prof.ProfileError, match="prototype, personal, default, production"):
        prof.preset("yolo")
    with pytest.raises(prof.ProfileError, match="thrifty, normal, fast"):
        prof.resolve(writer="turbo")
    with pytest.raises(prof.ProfileError, match="off, light, normal, strict"):
        prof.resolve(review="lax")


def test_review_plans():
    plan = {r: prof.review_plan(prof.Profile("normal", r)) for r in prof.REVIEW_MODES}
    assert (plan["off"].kind, plan["light"].kind, plan["normal"].kind) == ("off", "light", "full")
    assert plan["light"].depth == "light" and plan["strict"].depth == "full"
    assert plan["normal"].skip_label_counts and not plan["strict"].skip_label_counts


# =========================================================================== default


def test_default_is_todays_rules():
    """``default`` gives the module constants exactly, and ``attempt_bound(4)`` is unchanged."""
    assert limits("default") == esc.DEFAULT_LIMITS == esc._limits(None)
    assert esc.attempt_bound(4) == 4 * 6 + esc.MAX_INTERRUPTED == 27
    assert esc.attempt_bound(4, limits("default")) == 27
    assert esc.attempt_bound(5) == esc.SPEC_LADDER_WORST_CASE == 33
    for r in ("off", "light", "normal"):  # review modes that keep the counts
        assert prof.limits_for(prof.Profile("normal", r)) == esc.DEFAULT_LIMITS


def test_default_decides_like_today_on_every_step_of_random_histories():
    """Every decision along 1,500 random histories (the escalation suite's generator) is the
    same with ``limits_for(default)`` as with no limits at all."""
    rng = random.Random(51)
    lim = limits("default")
    checked = 0
    for i in range(1500):
        ladder = LADDER if i % 2 else te.random_ladder(rng)
        t = te.Ticket(routed_tier=rng.choice([None, 1, 2, 3]), explore=rng.random() < 0.2)
        real = esc.next_step

        def both(h, ladder, limits=None, _real=real):
            nonlocal checked
            a, b = _real(h, ladder), _real(h, ladder, lim)
            assert a == b
            checked += 1
            return a

        te.next_step = both  # type: ignore[assignment]
        try:
            te.run(t, ladder, rng, accept=i % 3 != 0)
        finally:
            te.next_step = real  # type: ignore[assignment]
    assert checked > 10_000


# =========================================================================== fast


def test_fast_shifts_the_start_one_tier_up_capped_at_the_top():
    fast = limits("prototype")
    assert prof.shift_start(1, TIERS, fast) == 2
    assert prof.shift_start(2, TIERS, fast) == 3
    assert prof.shift_start(3, TIERS, fast) == 3
    assert prof.shift_start(1, (1, 3), fast) == 3  # tiers without a model are skipped
    assert prof.shift_start(1, TIERS, fast, override=True) == 1  # a tier:N label: never
    assert prof.shift_start(1, TIERS, limits("default")) == 1
    assert prof.shift_reason(prof.preset("prototype"), 1, 2) == "; profile fast: tier 1 -> 2"


def test_fast_starts_on_the_shifted_tier_and_moves_up_on_the_first_failed_check():
    fast = limits("production")
    t = te.Ticket(routed_tier=1)
    first = step(t, fast, start=prof.shift_start(1, TIERS, fast))
    assert isinstance(first, Retry) and (first.tier, first.model) == (2, Q2.model)
    assert "moved the start up from tier 1" in first.reason
    t.add(first, "failed_checks")
    up = step(t, fast, start=2)
    assert isinstance(up, Escalate) and up.tier == 3 and up.reason.startswith("checks_failed")
    # normal retries once on the same tier instead
    assert te.where(step(t, None, start=2)) == (2, Q2.model)


def test_fast_does_not_explore():
    assert prof.explore_rate(limits("prototype"), 0.10) == 0.0
    assert prof.explore_rate(limits("default"), 0.07) == 0.07  # normal: the config's rate
    assert prof.explore_rate(limits("personal"), 0.10) == prof.EXPLORE_RATE_THRIFTY == 0.25


# =========================================================================== thrifty


def test_thrifty_takes_three_failed_checks_before_moving_up():
    thrifty = limits("personal")
    t = te.Ticket(routed_tier=1)
    for _ in range(3):
        s = step(t, thrifty)
        assert isinstance(s, Retry) and not isinstance(s, Escalate) and s.tier == 1
        t.add(s, "failed_checks")
    up = step(t, thrifty)
    assert isinstance(up, Escalate) and up.tier == 2
    assert "failed 3 times" in up.reason


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "fman.db")
    yield s
    s.close()


def _ticket(store: Store, repo: str = REPO):
    return store.create_ticket(
        repo=repo, issue_number=1, title="t", request="r", base_branch="main"
    )


def test_thrifty_cloud_writer_attempt_is_refused_until_approved(store: Store):
    spend = Spend(store, SpendCaps())
    t = _ticket(store)
    assert limits("personal").cloud_approval and not limits("default").cloud_approval
    assert spend.writer_gate(t.id) is None  # not thrifty, caps clear: start
    refusal = spend.writer_gate(t.id, approval_required=True)
    assert refusal is not None and refusal.cap == "thrifty" and refusal.needs_approval
    assert refusal.flag_reason.startswith(FLAG_THRIFTY) and FLAG_THRIFTY.startswith(FLAG_SPEND_CAP)
    assert "profile is thrifty" in spend.approval_comment(t, refusal)
    # paused: the caps being clear does not lift it; an approval does
    paused = store.flag_ticket(t.id, refusal.flag_reason)
    assert spend.resume_reason(paused) is None
    spend.approve(t.id)
    assert spend.resume_reason(store.get_ticket(t.id)) == "approved"
    assert spend.writer_gate(t.id, approval_required=True) is None  # consumes it
    assert store.open_spend_approval(t.id) is None
    assert spend.writer_gate(t.id, approval_required=True) is not None  # the next one asks


def test_thrifty_approval_at_a_reached_cap_is_left_for_the_cap_path(store: Store):
    spend = Spend(store, SpendCaps(daily_usd=0.0, monthly_usd=0.0))
    t = _ticket(store)
    assert spend.writer_gate(t.id, approval_required=True).cap == "thrifty"
    spend.approve(t.id)
    assert spend.writer_gate(t.id, approval_required=True) is None
    assert store.open_spend_approval(t.id) is not None  # consumed by the first over-cap call


# =========================================================================== strict


def test_strict_escalates_on_the_first_ai_kickback():
    strict = limits("production")
    for lim, moves in ((strict, True), (limits("default"), False)):
        t = te.Ticket(routed_tier=1)
        t.add(Retry("first", 1, "x", Q1.model), "kicked_back")
        t.kick("reviewer", "incomplete")
        s = step(t, lim)
        assert isinstance(s, Escalate) is moves, s
        if moves:
            assert s.reason.startswith("ai_kickback_cap")
    # user kickbacks are the same in every mode
    t = te.Ticket(routed_tier=1)
    t.add(Retry("first", 1, "x", Q1.model), "kicked_back")
    t.kick("user")
    for p in ALL_PROFILES:
        assert te.where(step(t, prof.limits_for(p))) == (1, Q1.model)


# =========================================================================== bound (7e)


def _drive(t: te.Ticket, ladder, rng: random.Random, p: prof.Profile, lim: esc.Limits):
    """``test_escalation.run`` with a profile: ``off`` never gets an AI kickback."""
    bound = esc.attempt_bound(len(ladder), lim)
    start = prof.shift_start(t.routed_tier or 1, {r.tier for r in ladder}, lim)
    while True:
        h = dataclasses.replace(t.history(), start_tier=start if t.routed_tier else None)
        s = next_step(h, ladder, lim)
        if isinstance(s, HandToUser):
            return s
        assert isinstance(s, Retry), s
        assert len(t.attempts) < bound, f"{p}: attempt {len(t.attempts) + 1} > bound {bound}"
        roll = rng.random()
        if roll < 0.1:
            t.add(s, "superseded")
            continue
        if roll < 0.5:
            t.add(s, "failed_checks")
            continue
        a = t.add(s, None)
        t.attempts.pop()
        if p.review != "off" and rng.random() < 0.5:
            t.attempts.append(te._with(a, "kicked_back"))
            t.kick("reviewer", rng.choice([None, "wrong", "incomplete"]))
            t.review("request_changes")
            continue
        if rng.random() < 0.05:
            t.attempts.append(te._with(a, "accepted"))
            return None
        t.attempts.append(te._with(a, "kicked_back"))
        t.kick("user", rng.choice([None, "wrong", "style"]))


@pytest.mark.parametrize("p", ALL_PROFILES, ids=lambda p: f"{p.writer}-{p.review}")
def test_every_profile_ends_with_the_user_within_its_bound(p: prof.Profile):
    rng = random.Random(ALL_PROFILES.index(p))
    lim = prof.limits_for(p)
    handed = 0
    for i in range(400):
        ladder = LADDER if i % 2 else te.random_ladder(rng)
        t = te.Ticket(routed_tier=rng.choice([None, 1, 2, 3]))
        if _drive(t, ladder, rng, p, lim) is not None:
            handed += 1
    assert handed > 300
    assert esc.attempt_bound(5, lim) <= esc.attempt_bound(5, limits("personal")) == 48


def test_limits_keep_a_finite_bound():
    with pytest.raises(ValueError, match="at least 1"):
        esc.Limits(max_check_attempts=0)


# =========================================================================== the worker


@pytest.fixture
def env(tmp_path: Path):
    e = Env(tmp_path)
    yield e
    e.close()


def test_off_hands_over_with_no_review_row_no_model_call_and_the_pr_ready(env: Env):
    env.store.set_profile(REPO, "fast", "off")
    n = env.label()
    env.once(passing(model_call=False))
    t = env.ticket(n)
    assert env.reviews(n) == [] and env.review_calls == 0
    assert t.flag_reason.startswith("review_skipped")
    pr = env.gh.get_pr(REPO, t.pr_number)
    assert not pr.draft and env.gh.ready_violations == []


def test_light_review_calls_no_model_and_marks_ready_on_no_finding(env: Env):
    env.store.set_profile(REPO, "thrifty", "light")
    n = env.label()
    env.once(passing(model_call=False))
    (r,) = env.reviews(n)
    assert env.review_calls == 0
    assert (r.verdict, r.reviewer_model, r.variant_id) == ("approve", "hard-checks", None)
    assert not env.gh.get_pr(REPO, env.ticket(n).pr_number).draft
    (a,) = env.attempts(n)
    assert (a.writer_mode, a.review_mode) == ("thrifty", "light")


def test_strict_ignores_review_skip(env: Env):
    env.store.set_profile(REPO, "normal", "strict")
    n = env.label("Typo", "Fix the typo.", "review:skip")
    env.once(passing(model_call=False))
    assert env.review_calls == 1 and len(env.reviews(n)) == 1
    assert not (env.ticket(n).flag_reason or "").startswith("review_skipped")


def test_fast_route_keeps_the_routers_pick_and_stores_the_shifted_start(env: Env):
    env.store.set_profile(REPO, "fast", "normal")
    n = env.label()  # the fake router says "small": tier 1
    env.once(passing(model_call=False))
    t = env.ticket(n)
    assert t.routed_tier == 1 and t.route_reason.endswith("; profile fast: tier 1 -> 2")
    assert env.store.get_cursor(REPO, start_cursor(t.id)) == "2"
    assert env.worker.start_tier(t) == 2 and env.worker.limits(t) == limits("prototype")
    # a tier:N label is not shifted
    m = env.label("Other", "Please add other().", "tier:1")
    env.once(passing(model_call=False))
    assert env.worker.start_tier(env.ticket(m)) == 1
    assert "profile fast" not in env.ticket(m).route_reason


def test_fast_route_with_the_wiring_starts_on_tier_2(env: Env, monkeypatch):
    """What the runner wires into ``Worker.policy_for`` / ``history`` (start_tier and
    limits), shown end to end: the first attempt runs on tier 2."""
    w = env.worker
    real_history = w.history

    def history(ticket):
        return dataclasses.replace(real_history(ticket), start_tier=w.start_tier(ticket))

    monkeypatch.setattr(w, "history", history)
    monkeypatch.setattr(
        w, "policy_for", lambda t: esc.next_step(w.history(t), w.ladder, w.limits(t))
    )
    env.store.set_profile(REPO, "fast", "normal")
    n = env.label()
    env.once(passing(model_call=False))
    assert [(a.tier, a.trigger, a.writer_mode) for a in env.attempts(n)] == [(2, "first", "fast")]


# =========================================================================== reviewer depth


def test_light_hard_finding_still_requests_changes_without_the_model(make_pr, run, models):  # noqa: F811
    change = make_pr(VACUOUS)
    out = run(change, depth="light")
    assert (out.verdict, out.tag) == ("request_changes", "incomplete")
    assert models.requests == [] and out.model_calls == 0
    assert out.run_app is None and out.read_diff == "skipped"


def test_light_calls_the_reviewer_model_zero_times(make_pr, run, models):  # noqa: F811
    change = make_pr(GOOD)
    out = run(change, depth="light", python=sys.executable)
    assert out.verdict == "approve" and out.reviewer_model == "hard-checks"
    assert models.requests == [] and out.model_calls == 0
    assert "light review" in out.comment_body


def test_unknown_depth_is_refused(make_pr, run):  # noqa: F811
    with pytest.raises(ValueError, match="full, light"):
        run(make_pr(GOOD), depth="deep")


# =========================================================================== store + status


def test_set_profile_round_trips_and_attempts_record_both_modes(store: Store):
    t = _ticket(store)
    a = store.add_attempt(t.id, model="m", tier=1, trigger="first")
    assert (a.writer_mode, a.review_mode) == ("normal", "normal")  # no profile: normal
    assert set_profile(store, REPO, "prototype") == prof.Profile("fast", "off")
    s = store.get_repo_settings(REPO)
    assert (s.writer_mode, s.review_mode) == ("fast", "off")
    assert prof.profile_of(s) == prof.preset("prototype")
    assert set_profile(store, REPO, review="strict") == prof.Profile("fast", "strict")
    b = store.add_attempt(t.id, model="m", tier=1, trigger="kickback")
    assert (b.writer_mode, b.review_mode) == ("fast", "strict")
    assert store.get_attempt(b.id).writer_mode == "fast"
    c = store.add_attempt(t.id, model="m", tier=1, trigger="first", writer_mode="thrifty")
    assert (c.writer_mode, c.review_mode) == ("thrifty", "strict")
    with pytest.raises(prof.ProfileError, match="valid: prototype"):
        set_profile(store, REPO, "nope")
    with pytest.raises(ValueError):
        store.set_profile(REPO, "turbo", None)  # type: ignore[arg-type]
    assert prof.profile_of(store.get_repo_settings(REPO)) == prof.Profile("fast", "strict")
    store.set_verify_command(REPO, "make test")  # other settings keep the profile
    assert store.get_repo_settings(REPO).writer_mode == "fast"


def test_status_shows_non_default_profiles(store: Store):
    _ticket(store)
    assert "Profile " not in render_status(store)
    set_profile(store, REPO, "personal")
    set_profile(store, "acme/other", "default")
    out = render_status(store)
    assert f"Profile {REPO}: personal (writer thrifty, review light)" in out
    assert "acme/other" not in out
