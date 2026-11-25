"""Escalation state machine (todo #16): one test per SPEC rule, plus the bound property.

Rows are built by hand (no store): ``Attempt``, ``Kickback`` and ``Review`` are the
store's own frozen row types, so the function sees exactly what #21 will pass it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from foreman_inator import worker_policy
from foreman_inator.config import local_tier2_models
from foreman_inator.db.store import Attempt, Kickback, Review
from foreman_inator.routing import escalation as esc
from foreman_inator.routing.escalation import (
    Escalate,
    HandToUser,
    History,
    Retry,
    Rung,
    build_ladder,
    next_step,
)

T0 = datetime(2026, 11, 23, tzinfo=UTC)
# tier 1 inkling, tier 2 qwen, then tier 3 in order: glm, claude opus, claude fable
LADDER = build_ladder(local_tier2_models())  # #70: the opt-in local tier 2 ladder
Q1, Q2, GLM, CLAUDE, FABLE = LADDER


@dataclass
class Ticket:
    """A hand-built history; ``add`` appends one finished attempt."""

    routed_tier: int | None = None
    explore: bool = False
    attempts: list[Attempt] = field(default_factory=list)
    kickbacks: list[Kickback] = field(default_factory=list)
    reviews: list[Review] = field(default_factory=list)

    def add(
        self,
        step_or_rung: Retry | Rung,
        outcome: str | None,
        *,
        passed: bool | None = None,
        ended: bool = True,
        trigger: str | None = None,
    ) -> Attempt:
        if isinstance(step_or_rung, Retry):
            tier, model, trig = step_or_rung.tier, step_or_rung.model, step_or_rung.trigger
        else:
            tier, model, trig = step_or_rung.tier, step_or_rung.model, "first"
        if passed is None:
            passed = outcome in (None, "kicked_back", "accepted")
        a = Attempt(
            id=len(self.attempts) + 1,
            ticket_id=1,
            model=model or "?",
            tier=tier,
            variant_id=None,
            trigger=trigger or trig,  # type: ignore[arg-type]
            worktree=None,
            branch=None,
            tests_passed=passed if outcome != "superseded" else None,
            build_passed=None,
            diff_files=None,
            diff_lines=None,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            seconds=None,
            outcome=outcome,  # type: ignore[arg-type]
            started_at=T0,
            ended_at=T0 if ended else None,
            base_commit=None,
            verify_output_path=None,
        )
        self.attempts.append(a)
        return a

    def kick(self, by: str = "user", tag: str | None = None, note: str = "fix it") -> None:
        self.kickbacks.append(
            Kickback(
                id=len(self.kickbacks) + 1,
                attempt_id=self.attempts[-1].id,
                by=by,  # type: ignore[arg-type]
                tag=tag,  # type: ignore[arg-type]
                note=note,
                failure_step=None,
                created_at=T0,
                failure_step_index=None,
            )
        )

    def review(self, verdict: str, *, shadow: bool = False, tag: str | None = None) -> None:
        self.reviews.append(
            Review(
                id=len(self.reviews) + 1,
                attempt_id=self.attempts[-1].id,
                reviewer_model="claude-sonnet",
                variant_id=None,
                verdict=verdict,  # type: ignore[arg-type]
                tests_passed=True,
                changed_line_coverage=None,
                tests_nonvacuous=None,
                ran_app=None,
                tag=tag,  # type: ignore[arg-type]
                note="reviewer note",
                tokens_in=0,
                tokens_out=0,
                cost_usd=0.0,
                created_at=T0,
                shadow=shadow,
            )
        )

    def history(self) -> History:
        return History(
            tuple(self.attempts),
            tuple(self.kickbacks),
            tuple(self.reviews),
            routed_tier=self.routed_tier,
            explore=self.explore,
        )

    def step(self, ladder: tuple[Rung, ...] = LADDER) -> esc.Step | None:
        return next_step(self.history(), ladder)


def where(step: esc.Step | None) -> tuple[int, str | None]:
    assert isinstance(step, Retry), step
    return step.tier, step.model


# --------------------------------------------------------------------------- SPEC rules


def test_checks_fail_retry_once_on_same_tier():
    """SPEC: "Checks fail → retry once on the same tier with the error output, then move
    up one tier." (first half: one same-tier retry, keeping the retried trigger, #14)."""
    t = Ticket(routed_tier=1)
    t.add(Q1, "failed_checks")
    step = t.step()
    assert type(step) is Retry
    assert where(step) == (1, Q1.model)
    assert step.trigger == "first"


def test_checks_fail_twice_moves_up_one_tier():
    """SPEC: "Checks fail → retry once on the same tier with the error output, then move
    up one tier." (second half)."""
    t = Ticket(routed_tier=1)
    t.add(Q1, "failed_checks")
    t.add(t.step(), "failed_checks")
    step = t.step()
    assert isinstance(step, Escalate)
    assert where(step) == (2, Q2.model)
    assert step.trigger == "escalation"
    assert step.reason.startswith("checks_failed:")


def test_kicked_back_once_retries_same_tier_with_note():
    """SPEC: "Kicked back once → retry on the same tier with your note.\""""
    t = Ticket(routed_tier=2)
    t.add(Q2, "kicked_back")
    t.kick("user", "incomplete")
    step = t.step()
    assert type(step) is Retry
    assert where(step) == (2, Q2.model)
    assert step.trigger == "kickback"


def test_kicked_back_twice_moves_up():
    """SPEC: "Kicked back twice, or tagged *wrong* → move up one tier." (twice)."""
    t = Ticket(routed_tier=1)
    t.add(Q1, "kicked_back")
    t.kick("user", "incomplete")
    t.add(t.step(), "kicked_back")
    t.kick("user", "style")
    step = t.step()
    assert isinstance(step, Escalate)
    assert where(step) == (2, Q2.model)
    assert step.reason.startswith("kickback_cap:")


def test_kicked_back_tagged_wrong_moves_up_at_once():
    """SPEC: "Kicked back twice, or tagged *wrong* → move up one tier." (tagged wrong)."""
    t = Ticket(routed_tier=1)
    t.add(Q1, "kicked_back")
    t.kick("user", "wrong")
    step = t.step()
    assert isinstance(step, Escalate)
    assert where(step) == (2, Q2.model)
    assert step.reason.startswith("kickback_wrong:")


def test_tier3_fails_hands_to_user():
    """SPEC: "Tier 3 fails → the ticket stays in progress and is flagged for you. No silent
    loops." Tier 3 is GLM, then Claude Opus, then Claude Fable, so "tier 3 fails" is
    Fable failing."""
    t = Ticket(routed_tier=3)
    t.add(GLM, "failed_checks")
    t.add(t.step(), "failed_checks")
    t.add(t.step(), "failed_checks")  # Claude Opus
    step = t.step()
    assert where(step) == (3, CLAUDE.model)
    t.add(step, "failed_checks")
    step = t.step()
    assert isinstance(step, Escalate)
    assert where(step) == (3, FABLE.model)
    t.add(step, "failed_checks")  # Claude Fable
    step = t.step()
    assert where(step) == (3, FABLE.model)
    t.add(step, "failed_checks")
    step = t.step()
    assert isinstance(step, HandToUser)
    assert isinstance(step, worker_policy.HandToUser)
    assert step.flag == "checks_failed"
    assert step.mark_ready is False


def test_tier3_claude_is_used_on_escalation_from_glm():
    """SPEC Decisions: "The GLM plan is tried first because it's a flat fee; the Claude API
    is used on escalation from GLM\""""
    t = Ticket(routed_tier=3)
    assert where(t.step()) == (3, GLM.model)
    t.add(GLM, "kicked_back")
    t.kick("user", "wrong")
    step = t.step()
    assert isinstance(step, Escalate)
    assert where(step) == (3, CLAUDE.model)


def test_ai_kickback_once_retries_same_tier():
    """SPEC: "The AI reviewer can kick a change back twice; after that the writer moves up
    one tier" (first kickback: same tier, trigger ai_kickback)."""
    t = Ticket(routed_tier=1)
    t.add(Q1, "kicked_back")
    t.kick("reviewer", "incomplete")
    step = t.step()
    assert type(step) is Retry
    assert where(step) == (1, Q1.model)
    assert step.trigger == "ai_kickback"


def test_two_ai_kickbacks_escalate_the_writer():
    """SPEC: "Two automatic kickbacks escalate the writer one tier.\""""
    t = Ticket(routed_tier=1)
    t.add(Q1, "kicked_back")
    t.kick("reviewer", "incomplete")
    t.add(t.step(), "kicked_back")
    t.kick("reviewer", "broke")
    step = t.step()
    assert isinstance(step, Escalate)
    assert where(step) == (2, Q2.model)
    assert step.reason.startswith("ai_kickback_cap:")


def test_two_ai_kickbacks_on_tier3_hand_the_pr_to_the_user():
    """SPEC: "if tier 3 is already writing, the PR goes to you with the reviewer's notes.\""""
    t = Ticket(routed_tier=3)
    t.add(GLM, "kicked_back")
    t.kick("reviewer")
    t.add(t.step(), "kicked_back")
    t.kick("reviewer")
    t.add(t.step(), "kicked_back")  # Claude Opus
    t.kick("reviewer")
    t.add(t.step(), "kicked_back")
    t.kick("reviewer")
    step = t.step()
    assert isinstance(step, Escalate)
    assert where(step) == (3, FABLE.model)
    t.add(step, "kicked_back")  # Claude Fable
    t.kick("reviewer")
    t.add(t.step(), "kicked_back")
    t.kick("reviewer")
    step = t.step()
    assert isinstance(step, HandToUser)
    assert step.flag == "ai_kickback_cap"
    assert step.mark_ready is True


def test_ai_wrong_tag_does_not_skip_a_rung():
    """SPEC: "Kicked back once → retry on the same tier with your note." The *wrong* rule
    is the user's; the reviewer's rule is "Two automatic kickbacks"."""
    t = Ticket(routed_tier=1)
    t.add(Q1, "kicked_back")
    t.kick("reviewer", "wrong")
    assert type(t.step()) is Retry


def test_ai_request_changes_review_without_kickback_row_counts_as_ai_kickback():
    """Kickback kinds: a non-shadow request_changes review with no reviewer kickback row is
    an AI kickback (same fallback as tickets.prompt.load_history); a shadow one is not."""
    t = Ticket(routed_tier=1)
    t.add(Q1, "kicked_back")
    t.review("request_changes")
    assert t.step().trigger == "ai_kickback"
    s = Ticket(routed_tier=1)
    s.add(Q1, "kicked_back")
    s.review("request_changes", shadow=True)
    assert s.step().trigger == "kickback"  # v1 rows: no kickback row → the user's


def test_explore_starts_one_tier_lower():
    """SPEC: "on about 10% of tickets, start one tier lower than the router picked ...
    These attempts are marked `explore`\""""
    step = Ticket(routed_tier=2, explore=True).step()
    assert where(step) == (1, Q1.model)
    assert step.trigger == "explore"
    # tier 3's lower group is tier 2; tier 1 has nothing lower, so no exploring there
    assert where(Ticket(routed_tier=3, explore=True).step()) == (2, Q2.model)
    step = Ticket(routed_tier=1, explore=True).step()
    assert where(step) == (1, Q1.model)
    assert step.trigger == "first"


@pytest.mark.parametrize("outcome", ["failed_checks", "kicked_back"])
def test_explore_failure_escalates_without_counting_against_the_model(outcome):
    """SPEC: explore attempts "never count against a model in the metrics if they fail":
    a failed explore attempt goes straight to the routed tier (no same-tier retry), and
    the routed tier starts with fresh counts."""
    t = Ticket(routed_tier=3, explore=True)
    t.add(t.step(), outcome)
    if outcome == "kicked_back":
        t.kick("user", "incomplete")
    step = t.step()
    assert isinstance(step, Escalate)
    assert where(step) == (3, GLM.model)
    assert step.reason.startswith("explore_failed:")
    t.add(step, "failed_checks")
    assert type(t.step()) is Retry  # fresh budget on GLM: one same-tier retry


def test_counts_reset_when_the_tier_changes():
    """Decision: counts are per rung. After moving up, one failed check or one kickback
    retries on the new tier."""
    t = Ticket(routed_tier=1)
    t.add(Q1, "kicked_back")
    t.kick("user")
    t.add(t.step(), "kicked_back")
    t.kick("user")
    t.add(t.step(), "kicked_back")  # tier 2, first user kickback there
    t.kick("user")
    step = t.step()
    assert type(step) is Retry
    assert where(step) == (2, Q2.model)


def test_kickback_attempt_that_fails_checks_gets_one_retry():
    """SPEC: "Checks fail → retry once on the same tier" applies after a kickback too:
    the segment restarts at each kickback."""
    t = Ticket(routed_tier=1)
    t.add(Q1, "failed_checks")
    t.add(t.step(), "kicked_back")
    t.kick("user")
    t.add(t.step(), "failed_checks")
    step = t.step()
    assert type(step) is Retry
    assert step.trigger == "kickback"


def test_tiers_without_a_model_are_skipped():
    """Decision: unconfigured tiers are skipped; a routed tier with no model starts on the
    next configured tier up."""
    ladder = (Q1, GLM)
    t = Ticket(routed_tier=1)
    t.add(Q1, "failed_checks")
    t.add(t.step(ladder), "failed_checks")
    assert where(t.step(ladder)) == (3, GLM.model)
    assert where(Ticket(routed_tier=2).step(ladder)) == (3, GLM.model)
    # nothing at or above the routed tier: start on the top rung
    assert where(Ticket(routed_tier=3).step((Q1, Q2))) == (2, Q2.model)


def test_top_configured_rung_is_the_stop_when_tier3_is_missing():
    """SPEC: "No silent loops." With no tier 3 configured, the top configured tier failing
    hands the ticket to the user."""
    ladder = (Q1, Q2)
    t = Ticket(routed_tier=2)
    t.add(Q2, "failed_checks")
    t.add(t.step(ladder), "failed_checks")
    assert isinstance(t.step(ladder), HandToUser)


def test_tier_override_is_the_start():
    """SPEC: "A `tier:N` label on the issue overrides the router" (the override reaches
    escalation as ``routed_tier``)."""
    step = Ticket(routed_tier=3).step()
    assert type(step) is Retry
    assert (step.trigger, step.tier, step.model) == ("first", 3, GLM.model)
    assert Ticket().step().tier == 1


def test_interrupted_attempts_rerun_then_hand_over():
    """Decision (kept from v1): a crash or shutdown re-runs the same rung and trigger, at
    most MAX_INTERRUPTED times per ticket, then the user."""
    t = Ticket(routed_tier=3)
    t.add(GLM, "failed_checks")
    t.add(t.step(), "failed_checks")
    claude = t.step()
    for _ in range(esc.MAX_INTERRUPTED):
        t.add(claude, "superseded", trigger="escalation")
        step = t.step()
        assert (step.trigger, step.tier, step.model) == ("escalation", 3, CLAUDE.model)
    t.add(claude, "superseded")
    step = t.step()
    assert isinstance(step, HandToUser)
    assert step.flag == "interrupted"


def test_interrupted_attempts_do_not_count_as_failures():
    """Decision: a superseded attempt is invisible to the check and kickback counts."""
    t = Ticket(routed_tier=1)
    t.add(Q1, "failed_checks")
    t.add(t.step(), "superseded")
    t.add(t.step(), "failed_checks")
    assert isinstance(t.step(), Escalate)  # two failed checks, not three


def test_nothing_to_start():
    """None while an attempt runs, waits on review (passed checks, no outcome) or was
    accepted; and an empty ladder hands over at once."""
    for outcome, ended in [(None, False), (None, True), ("accepted", True)]:
        t = Ticket()
        t.add(Q1, outcome, ended=ended)
        assert t.step() is None
    step = Ticket().step(())
    assert isinstance(step, HandToUser)
    assert step.flag == "no_model"


def test_v1_call_shape_and_types():
    """Drop-in: a bare attempt list works, and steps are v1's Retry / HandToUser."""
    policy = esc.make_policy(LADDER)
    step = policy([])
    assert isinstance(step, worker_policy.Retry)
    assert (step.trigger, step.tier, step.model) == ("first", 1, Q1.model)
    t = Ticket()
    t.add(Q1, "kicked_back")
    assert policy(t.attempts).trigger == "kickback"


def test_same_rows_same_decision():
    """Crash safety: the decision is a pure function of the rows."""
    rng = random.Random(7)
    for _ in range(200):
        t = Ticket(routed_tier=rng.choice([None, 1, 2, 3]))
        run(t, LADDER, rng)
        assert next_step(t.history(), LADDER) == next_step(t.history(), list(LADDER))


# --------------------------------------------------------------------------- the bound


def test_bound_constants():
    """Worst case: 6 attempts per rung (3 segments × 2) plus 3 interrupted re-runs."""
    assert esc.MAX_ATTEMPTS_PER_RUNG == 6
    assert esc.attempt_bound(5) == esc.SPEC_LADDER_WORST_CASE == 33
    assert len(LADDER) == 5


def run(t: Ticket, ladder, rng: random.Random, *, accept: bool = True) -> esc.Step | None:
    """Drive the worker loop over random outcomes until a terminal step; return it.

    Returns the HandToUser step, or None when the user merged. Fails past the bound."""
    bound = esc.attempt_bound(len(ladder))
    while True:
        step = next_step(t.history(), ladder)
        if isinstance(step, HandToUser):
            return step
        assert isinstance(step, Retry), step
        assert step.model in {r.model for r in ladder}
        assert len(t.attempts) < bound, f"attempt {len(t.attempts) + 1} exceeds bound {bound}"
        roll = rng.random()
        if roll < 0.1:
            t.add(step, "superseded")
            continue
        if roll < 0.5:
            t.add(step, "failed_checks")
            continue
        t.add(step, None)  # passed checks: waits on review
        assert next_step(t.history(), ladder) is None
        a = t.attempts.pop()
        if rng.random() < 0.8:  # AI review on (review:skip otherwise)
            if rng.random() < 0.5:
                t.attempts.append(_with(a, "kicked_back"))
                if rng.random() < 0.7:
                    t.kick("reviewer", rng.choice([None, "wrong", "incomplete", "broke"]))
                t.review("request_changes")
                continue
            t.attempts.append(a)
            t.review("approve")
            t.attempts.pop()
        if accept and rng.random() < 0.1:
            t.attempts.append(_with(a, "accepted"))
            return None
        t.attempts.append(_with(a, "kicked_back"))
        if rng.random() < 0.9:
            t.kick("user", rng.choice([None, "wrong", "incomplete", "broke", "style"]))


def _with(a: Attempt, outcome: str) -> Attempt:
    from dataclasses import replace

    return replace(a, outcome=outcome)  # type: ignore[arg-type]


def random_ladder(rng: random.Random) -> tuple[Rung, ...]:
    rungs = []
    for tier in (1, 2, 3):
        for k in range(rng.choice([0, 1, 1, 2])):
            rungs.append(Rung(tier, f"m{tier}{k}"))
    return tuple(rungs)


def test_every_history_ends_with_the_user_within_the_bound():
    """Invariant 7e: "Every retry and escalation chain is capped and ends with the user."
    Over many random histories, applying next_step reaches HandToUser (or a merge) within
    attempt_bound(len(ladder)) attempts; on SPEC's ladder that is SPEC_LADDER_WORST_CASE."""
    rng = random.Random(16)
    handed = 0
    for i in range(3000):
        ladder = LADDER if i % 2 else random_ladder(rng)
        t = Ticket(
            routed_tier=rng.choice([None, 1, 2, 3]),
            explore=rng.random() < 0.2,
        )
        end = run(t, ladder, rng, accept=i % 3 != 0)
        if ladder == LADDER:
            assert len(t.attempts) <= esc.SPEC_LADDER_WORST_CASE
        if end is not None:
            handed += 1
            assert end.flag in {
                "checks_failed",
                "kickback_cap",
                "kickback_wrong",
                "ai_kickback_cap",
                "explore_failed",
                "interrupted",
                "no_model",
            }
    assert handed > 1000  # the loop really runs into the stop, not only into merges


def test_worst_case_is_reached_exactly():
    """The bound is tight: the adversarial history uses all 33 attempts, then the user."""
    t = Ticket(routed_tier=1)
    cuts = 0
    for _rung in LADDER:
        for outcome, by in [
            ("failed_checks", None),
            ("kicked_back", "reviewer"),
            ("failed_checks", None),
            ("kicked_back", "user"),
            ("failed_checks", None),
            ("kicked_back", "reviewer"),
        ]:
            step = t.step()
            assert isinstance(step, Retry), step
            if cuts < esc.MAX_INTERRUPTED:
                t.add(step, "superseded")
                cuts += 1
                step = t.step()
            t.add(step, outcome)
            if by:
                t.kick(by, "incomplete")
    assert len(t.attempts) == esc.SPEC_LADDER_WORST_CASE
    step = t.step()
    assert isinstance(step, HandToUser)
    assert step.flag == "ai_kickback_cap"
