"""Per-attempt spend and input-token budget (#58).

Unit level: ``Spend``'s hook refuses a writer call (cap ``attempt``) once the attempt's
own spend or input tokens would pass its limit, without asking anyone; escalation turns
a budget stop into one hand-over; the summary line shows per-attempt spend vs the limit.

End to end (``fakes.worker_env``: real proxy, real spend hook, fake OpenCode whose bash
steps call the attempt's proxy URL): a cloud model that keeps answering is stopped at
the dollar budget, a local one at the token limit; each ends with ``end_reason`` budget,
exactly one hand-over, and no further attempt on any tier.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fakes.opencode import Script, bash, edit, say
from fakes.worker_env import Env
from foreman_inator.config import Config, ModelEndpoint, SpendCaps
from foreman_inator.db.store import Store
from foreman_inator.pr.summary import summary_line
from foreman_inator.proxy.server import CallContext
from foreman_inator.routing import escalation as esc
from foreman_inator.spend import FLAG_ATTEMPT_BUDGET, Spend

LOCAL = ModelEndpoint("qwen", 1, "local", "qwen3.6:35b-a3b", "http://127.0.0.1:11434/v1", "qwen")
CLOUD = ModelEndpoint("claude", 3, "cloud", "claude-x", "https://api.anthropic.com/v1", "claude")
SINCE = datetime(2000, 1, 1, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "fman.db")
    yield s
    s.close()


def attempt(store: Store, model: str = "claude", tier: int = 3) -> int:
    """A new ticket (next issue number) with one running attempt."""
    n = store.query("SELECT COUNT(*) AS n FROM tickets")[0]["n"] + 1
    t = store.create_ticket(repo="me/toy", issue_number=n, title="t", request="r",
                            base_branch="main")  # fmt: skip
    return store.add_attempt(t.id, model=model, tier=tier, trigger="first").id


def ctx(attempt_id: int, **kw) -> CallContext:
    return CallContext(kind="attempt", attempt_id=attempt_id, **kw)


def handover_comments(env: Env, n: int) -> list[str]:
    return [c for c in env.issue_comments(n) if "key=handover-" in c]


def call(spend: Spend, store: Store, ep: ModelEndpoint, c: CallContext, cost: float,
         tokens_in: int = 0) -> bool:  # fmt: skip
    """One call as the proxy makes it: estimate, then usage on the attempt row, then the
    actual (estimate == actual cost)."""
    if not spend.hook(ep, cost, c, phase="estimate"):
        return False
    if c.review_id is None:
        store.add_attempt_usage(c.attempt_id, tokens_in=tokens_in, cost_usd=cost)
    spend.hook(ep, cost, c, phase="actual")
    return True


# --------------------------------------------------------------------------- Spend


def test_cloud_attempt_stops_at_its_dollar_budget_without_asking(store):
    spend = Spend(store, Config(), attempt_budget_usd=1.0)
    aid = attempt(store)
    assert [call(spend, store, CLOUD, ctx(aid), 0.3) for _ in range(4)] == [
        True, True, True, False,  # 0.9 + 0.3 > 1.0: refused before it is sent
    ]  # fmt: skip
    stop = spend.budget_stop(aid)
    assert stop is not None and stop.cap == "attempt" and not stop.needs_approval
    assert stop.flag_reason.startswith(f"{FLAG_ATTEMPT_BUDGET}: spent $0.90 of $1.00")
    assert spend.refusal_for_attempt(aid) is None  # no spend-cap pause either
    assert store.spend_total("live", since=SINCE) == pytest.approx(0.9)
    # Reaching the budget exactly is allowed (the caps' boundary).
    other = attempt(store)
    assert call(spend, store, CLOUD, ctx(other), 1.0)
    assert spend.budget_stop(other) is None


def test_budget_counts_in_flight_reservations_of_the_attempt(store):
    spend = Spend(store, Config(), attempt_budget_usd=1.0)
    aid = attempt(store)
    assert spend.hook(CLOUD, 0.6, ctx(aid, token="a"), phase="estimate")
    assert not spend.hook(CLOUD, 0.6, ctx(aid, token="b"), phase="estimate")


def test_reviews_are_outside_the_attempt_budget(store):
    spend = Spend(store, Config(), attempt_budget_usd=0.5)
    aid = attempt(store)
    rid = store.record_review(aid, reviewer_model="claude", verdict="approve").id
    assert call(spend, store, CLOUD, ctx(aid, review_id=rid), 2.0)
    assert spend.budget_stop(aid) is None


def test_local_attempt_stops_at_its_input_token_limit(store):
    spend = Spend(store, Config(), attempt_max_input_tokens_local=250_000)
    aid = attempt(store, model="qwen", tier=1)
    got = [call(spend, store, LOCAL, ctx(aid), 0.0, tokens_in=100_000) for _ in range(4)]
    assert got == [True, True, True, False]  # 300,000 >= 250,000 before the 4th call
    stop = spend.budget_stop(aid)
    assert stop is not None and stop.detail == "sent 300,000 input tokens of 250,000"
    assert stop.flag_reason.startswith(FLAG_ATTEMPT_BUDGET)
    assert store.spend_total("live", since=SINCE) == 0  # local writes no ledger row


def test_limits_are_off_by_default_and_the_day_cap_still_pauses(store):
    spend = Spend(store, Config(spend_caps=SpendCaps(daily_usd=1.0)))
    aid = attempt(store)
    assert call(spend, store, CLOUD, ctx(aid), 0.9)
    assert call(spend, store, LOCAL, ctx(aid), 0.0, tokens_in=10**9)
    assert not call(spend, store, CLOUD, ctx(aid), 0.2)
    assert spend.budget_stop(aid) is None
    r = spend.refusal_for_attempt(aid)
    assert r is not None and r.cap == "daily" and r.needs_approval


# --------------------------------------------------------------------------- store


def test_migration_007_end_reason_is_checked(store):
    aid = attempt(store)
    a = store.set_attempt_end_reason(aid, "budget", "spent $1.02 of $1.00")
    assert (a.end_reason, a.end_detail) == ("budget", "spent $1.02 of $1.00")
    with pytest.raises(sqlite3.IntegrityError), store.transaction() as conn:
        conn.execute("UPDATE attempts SET end_reason = 'bored' WHERE id = ?", (aid,))


# --------------------------------------------------------------------------- escalation


LADDER = esc.build_ladder(
    [ModelEndpoint(n, t, "local", n, "http://x", n) for n, t in [("a", 1), ("b", 2), ("c", 3)]]
)


def _failed(store: Store, model: str, tier: int, end_reason: str | None):
    aid = attempt(store, model, tier)
    store.finish_attempt(aid, "failed_checks", tests_passed=False)
    if end_reason:
        store.set_attempt_end_reason(aid, "budget", "spent $1.60 of $1.50")
    return store.get_attempt(aid)


def test_a_budget_stop_hands_over_with_no_retry_on_any_tier(store):
    stopped = _failed(store, "a", 1, "budget")
    step = esc.next_step(esc.History((stopped,)), LADDER)
    assert isinstance(step, esc.HandToUser) and not step.mark_ready
    assert step.reason.startswith("attempt_budget: spent $1.60 of $1.50 on tier 1 (a)")
    plain = _failed(store, "a", 1, None)
    assert isinstance(esc.next_step(esc.History((plain,)), LADDER), esc.Retry)


# --------------------------------------------------------------------------- summary


def test_summary_shows_per_attempt_spend_against_the_limit(store):
    a1 = _failed(store, "a", 1, "budget")
    store.add_attempt_usage(a1.id, cost_usd=1.6)
    t = store.get_ticket(a1.ticket_id)
    line = summary_line(t, [store.get_attempt(a1.id)], [], [], attempt_budget_usd=1.5)
    assert "max $1.60 per attempt of $1.50 limit, 1 stopped at the attempt budget" in line
    assert "per attempt" not in summary_line(t, [a1], [], [])


# --------------------------------------------------------------------------- end to end


def big_call() -> dict:
    """A bash step that sends one ~100,000-input-token chat call through the attempt's
    proxy URL (300,000 prompt chars; ``max_tokens`` 37 keeps the estimate near actual)."""
    code = (
        "import json,os,urllib.request as u;"
        "c=json.load(open(os.environ['OPENCODE_CONFIG']))['provider']['fman'];"
        "m=list(c['models'])[0];"
        "r=u.Request(c['options']['baseURL']+'/chat/completions',"
        "data=json.dumps({'model':m,'max_tokens':37,"
        "'messages':[{'role':'user','content':'x'*300000}]}).encode(),"
        "headers={'Content-Type':'application/json'});"
        "print(u.urlopen(r).status)"
    )
    return bash(f'"{sys.executable}" -c "{code}"')


def keeps_answering(calls: int = 12) -> Script:
    """A writer that would keep calling the model, then fails its checks."""
    failing = edit("feature.py", "def feature():\n    return 0\n")
    return Script([*(big_call() for _ in range(calls)), failing, say("Tried.")])


def budget_env(tmp: Path, **limits) -> Env:
    e = Env(tmp)
    e.worker.shutdown(timeout=5)
    e.spend = Spend(e.store, e.config, **limits)
    e.proxy.spend_hook = e.spend.hook
    e.worker = e.new_worker()
    for _ in range(40):  # the fake model reports what a long conversation would
        e.models.queue_reply("working", tokens=(100_000, 50))
    return e


def test_cloud_writer_that_keeps_answering_stops_at_the_attempt_budget(tmp_path: Path):
    e = budget_env(tmp_path, attempt_budget_usd=1.0)
    try:
        n = e.label("Big", "Rework it.", "tier:3")  # tier 3: GLM, $1.40/M in, $4.40/M out
        e.once(keeps_answering())
        (a,) = e.attempts(n)
        assert (a.model, a.outcome, a.end_reason) == ("glm", "failed_checks", "budget")
        # Each call costs ~$0.1402 and is estimated at ~$0.1403: seven fit in $1.00.
        assert a.tokens_in == 700_000
        spent = e.store.spend_total("live", since=SINCE)
        assert spent == pytest.approx(a.cost_usd) and 0.98 <= spent <= 1.0
        for _ in range(3):
            e.once(keeps_answering())
        assert len(e.attempts(n)) == 1  # no retry on this or any other tier
        t = e.ticket(n)
        assert t.flag_reason.startswith("attempt_budget: spent $0.98 of $1.00 on tier 3 (glm)")
        assert len(handover_comments(e, n)) == 1
        assert e.store.spend_total("live", since=SINCE) == pytest.approx(spent)
    finally:
        e.close()


def test_local_writer_stops_at_the_input_token_limit(tmp_path: Path):
    e = budget_env(tmp_path, attempt_max_input_tokens_local=250_000)
    try:
        n = e.label()  # tier 1: a local model
        e.once(keeps_answering())
        (a,) = e.attempts(n)
        assert (a.tier, a.outcome, a.end_reason) == (1, "failed_checks", "budget")
        assert a.tokens_in == 300_000 and a.end_detail == "sent 300,000 input tokens of 250,000"
        e.once(keeps_answering())
        e.once(keeps_answering())
        assert len(e.attempts(n)) == 1
        assert e.ticket(n).flag_reason.startswith("attempt_budget: sent 300,000 input tokens")
        assert len(handover_comments(e, n)) == 1
    finally:
        e.close()
