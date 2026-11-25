"""Per-repo "local only" (todo #49, GH #26): a ``cloud = false`` repo's code never goes to
a cloud model: escalation, the spend gate, AI review, shadow reviews and replay sets."""

from __future__ import annotations

import random
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from foreman_inator.config import (
    Config,
    ModelEndpoint,
    SpendCaps,
    local_tier2_models,
)
from foreman_inator.db.store import Attempt, Kickback, Store
from foreman_inator.experiments import replay_sets as rs
from foreman_inator.experiments import shadow as S
from foreman_inator.proxy.server import CallContext
from foreman_inator.review import reviewer as rv
from foreman_inator.routing import escalation as esc
from foreman_inator.routing import profile as prof
from foreman_inator.routing.escalation import (
    Escalate,
    HandToUser,
    History,
    Limits,
    Retry,
    Rung,
    build_ladder,
    next_step,
    with_cloud,
)
from foreman_inator.spend import FLAG_LOCAL_ONLY, Spend
from foreman_inator.worker_review import (
    ReviewSteps,
    local_only_skip_reason,
    local_reviewer,
)

T0 = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
LOCAL_URL = "http://127.0.0.1:11434/v1"
LADDER = build_ladder(local_tier2_models())  # inkling (1, local), qwen (2, local), 3 cloud rungs
Q1, Q2, GLM, OPUS, FABLE = LADDER
LOCAL = with_cloud(None, False)
REPO = "me/secret"
OTHER = "me/open"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "fman.db", clock=lambda: T0)
    yield s
    s.close()


# --------------------------------------------------------------------------- store


def test_cloud_defaults_on_and_set_cloud_round_trips(store):
    assert store.repo_allows_cloud(REPO) and store.repo_allows_cloud(None)
    store.set_verify_command(REPO, "make test")  # a row from before the setting
    assert store.get_repo_settings(REPO).cloud is True
    s = store.set_cloud(REPO, False)
    assert s.cloud is False and s.verify_command == "make test"  # other columns kept
    assert not store.repo_allows_cloud(REPO) and store.repo_allows_cloud(OTHER)
    assert store.local_only_repos() == {REPO}
    store.set_profile(REPO, "fast", None)  # another setter leaves cloud alone
    assert store.get_repo_settings(REPO).cloud is False
    assert store.set_cloud(REPO, True).cloud is True
    assert store.local_only_repos() == frozenset()


def test_cloud_column_rejects_other_values(store):
    store.set_cloud(REPO, True)
    with pytest.raises(Exception, match="CHECK"), store.transaction() as conn:
        conn.execute("UPDATE repo_settings SET cloud = 2 WHERE repo = ?", (REPO,))


# --------------------------------------------------------------------------- escalation


def _attempt(i: int, rung: Rung, outcome: str | None, trigger: str = "first", **kw) -> Attempt:
    return Attempt(
        id=i,
        ticket_id=1,
        model=rung.model,
        tier=rung.tier,
        variant_id=None,
        trigger=trigger,  # type: ignore[arg-type]
        worktree=None,
        branch=None,
        tests_passed=kw.pop("passed", outcome in (None, "kicked_back", "accepted")),
        build_passed=None,
        diff_files=None,
        diff_lines=None,
        tokens_in=0,
        tokens_out=0,
        cost_usd=0.0,
        seconds=None,
        outcome=outcome,  # type: ignore[arg-type]
        started_at=T0,
        ended_at=T0,
        base_commit=None,
        verify_output_path=None,
        **kw,
    )


def test_ladder_rungs_know_their_kind_and_still_compare_by_tier_and_model():
    assert [r.cloud for r in LADDER] == [False, False, True, True, True]
    assert Rung(1, "inkling-2-mini") == Q1  # equality ignores the kind
    assert esc.local_rungs(LADDER) == (Q1, Q2)


def test_with_cloud_keeps_default_limits_and_counts():
    assert with_cloud(None, True) is None
    lim = prof.limits_for(prof.preset("personal"))
    assert with_cloud(lim, True) is lim
    off = with_cloud(lim, False)
    assert off is not None and off.cloud is False
    assert replace(off, cloud=True) == lim
    assert replace(esc.DEFAULT_LIMITS, cloud=False) == LOCAL


def test_failed_checks_on_last_local_tier_hand_over_local_only():
    h = History((_attempt(1, Q2, "failed_checks"), _attempt(2, Q2, "failed_checks")), routed_tier=2)
    assert isinstance(next_step(h, LADDER), Escalate)  # cloud allowed: moves up to GLM
    step = next_step(h, LADDER, LOCAL)
    assert isinstance(step, HandToUser) and step.flag == "local_only"
    assert "checks_failed" in step.reason and "glm" in step.reason
    assert "cloud = false" in step.reason and step.mark_ready is False


def test_kickback_cap_on_last_local_tier_hands_over_ready():
    atts = (_attempt(1, Q2, "kicked_back"), _attempt(2, Q2, "kicked_back", trigger="kickback"))
    kicks = tuple(
        Kickback(i, i, "user", None, "no", None, T0, failure_step_index=None) for i in (1, 2)
    )
    step = next_step(History(atts, kicks, routed_tier=2), LADDER, LOCAL)
    assert isinstance(step, HandToUser) and step.flag == "local_only"
    assert "kickback_cap" in step.reason and step.mark_ready is True


def test_lower_local_tier_still_escalates_to_the_next_local_tier():
    h = History((_attempt(1, Q1, "failed_checks"), _attempt(2, Q1, "failed_checks")), routed_tier=1)
    step = next_step(h, LADDER, LOCAL)
    assert isinstance(step, Escalate) and step.model == Q2.model


def test_routed_to_a_cloud_tier_starts_on_the_last_local_tier():
    step = next_step(History((), routed_tier=3), LADDER, LOCAL)
    assert isinstance(step, Retry) and step.model == Q2.model and step.trigger == "first"
    assert "local only" in step.reason
    assert next_step(History((), routed_tier=3), LADDER).model == GLM.model


def test_no_local_rung_hands_over_at_once():
    cloud_only = (GLM, OPUS)
    step = next_step(History(()), cloud_only, LOCAL)
    assert isinstance(step, HandToUser) and step.flag == "local_only"
    # a hand-built rung of unknown kind counts as cloud (fails closed)
    step = next_step(History(()), (Rung(1, "mystery"),), LOCAL)
    assert isinstance(step, HandToUser) and step.flag == "local_only"


def test_running_attempt_on_a_cloud_rung_is_left_alone_then_moves_local():
    running = replace(_attempt(1, GLM, None), ended_at=None)
    assert next_step(History((running,)), LADDER, LOCAL) is None
    cut = _attempt(1, GLM, "superseded")
    step = next_step(History((cut,)), LADDER, LOCAL)
    assert isinstance(step, Retry) and step.model == Q2.model  # never back to GLM


def test_attempt_budget_hand_over_still_comes_first():
    a = _attempt(1, Q2, "failed_checks", end_reason="budget", end_detail="spent $1 of $1")
    step = next_step(History((a,)), LADDER, LOCAL)
    assert isinstance(step, HandToUser) and step.flag == "attempt_budget"


# Every profile, local only, ends with the user within its bound and never on a cloud rung.


def _run(ladder, limits: Limits, rng: random.Random, routed: int | None) -> HandToUser | None:
    attempts: list[Attempt] = []
    kicks: list[Kickback] = []
    rungs = esc.local_rungs(ladder) if not limits.cloud else tuple(ladder)
    bound = esc.attempt_bound(len(rungs), limits)
    assert bound <= esc.attempt_bound(len(ladder), limits)
    while True:
        step = next_step(History(tuple(attempts), tuple(kicks), routed_tier=routed), ladder, limits)
        if isinstance(step, HandToUser):
            return step
        assert isinstance(step, Retry), step
        assert step.model in {r.model for r in rungs}, "a local-only repo ran a cloud rung"
        assert len(attempts) < bound, f"attempt {len(attempts) + 1} exceeds bound {bound}"
        rung = next(r for r in ladder if r.model == step.model)
        i = len(attempts) + 1
        roll = rng.random()
        if roll < 0.1:
            attempts.append(_attempt(i, rung, "superseded", step.trigger))
        elif roll < 0.55:
            attempts.append(_attempt(i, rung, "failed_checks", step.trigger))
        elif roll < 0.6:
            attempts.append(_attempt(i, rung, "accepted", step.trigger))
            return None
        else:
            attempts.append(_attempt(i, rung, "kicked_back", step.trigger))
            by = rng.choice(["user", "reviewer"])
            tag = rng.choice([None, "wrong", "incomplete"])
            kicks.append(Kickback(len(kicks) + 1, i, by, tag, "n", None, T0, None))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "p",
    [prof.Profile(w, r) for w in prof.WRITER_MODES for r in prof.REVIEW_MODES],
    ids=lambda p: f"{p.writer}-{p.review}",
)
def test_every_profile_local_only_ends_within_attempt_bound(p):
    rng = random.Random(49)
    limits = with_cloud(prof.limits_for(p), False)
    assert limits is not None
    handed = 0
    for _ in range(300):
        end = _run(LADDER, limits, rng, rng.choice([None, 1, 2, 3]))
        if end is not None:
            handed += 1
            assert end.flag in {
                "local_only",
                "checks_failed",
                "kickback_cap",
                "kickback_wrong",
                "ai_kickback_cap",
                "interrupted",
            }
    assert handed > 100


# --------------------------------------------------------------------------- spend


CLOUD = ModelEndpoint("claude", 3, "cloud", "claude-x", "https://api.anthropic.com/v1", "claude")
QWEN = ModelEndpoint("qwen", 1, "local", "qwen3", LOCAL_URL, "qwen")


def _ticket_attempt(store: Store, repo: str, issue: int = 1) -> tuple[int, int]:
    t = store.create_ticket(repo=repo, issue_number=issue, title="t", request="r", base_branch="m")
    a = store.add_attempt(t.id, model="claude", tier=3, trigger="first")
    return t.id, a.id


def _spend(store: Store) -> Spend:
    caps = SpendCaps(daily_usd=5.0, monthly_usd=50.0, experiments_monthly_usd=10.0)
    return Spend(store, Config(spend_caps=caps), tz="UTC")


def test_spend_refuses_every_cloud_call_of_a_local_only_repo(store):
    spend = _spend(store)
    store.set_cloud(REPO, False)
    tid, aid = _ticket_attempt(store, REPO)
    _, other = _ticket_attempt(store, OTHER, 2)
    c = CallContext(kind="attempt", attempt_id=aid)
    assert spend.hook(CLOUD, 0.01, c, phase="estimate") is False
    r = spend.refusal_for_attempt(aid)
    assert r is not None and r.cap == "local_only" and r.needs_approval is False
    assert r.flag_reason.startswith(f"{FLAG_LOCAL_ONLY}:") and r.ticket_id == tid
    assert spend.refusal_for_ticket(tid) is None  # never a spend pause
    # an approval cannot lift it; experiments bucket refused too
    spend.approve(tid)
    assert spend.hook(CLOUD, 0.01, c, phase="estimate") is False
    exp = CallContext(kind="attempt", attempt_id=aid, bucket="experiments")
    assert spend.hook(CLOUD, 0.01, exp, phase="estimate") is False
    # a review of that repo's attempt (review key only) is refused too
    review = store.record_review(aid, reviewer_model="claude", verdict="approve")
    rc = CallContext(kind="registered", review_id=review.id)
    assert spend.hook(CLOUD, 0.01, rc, phase="estimate") is False
    # local calls still run; other repos still reach the cloud
    assert spend.hook(QWEN, 0.0, c, phase="estimate") is True
    oc = CallContext(kind="attempt", attempt_id=other)
    assert spend.hook(CLOUD, 0.01, oc, phase="estimate") is True
    assert store.query("SELECT COUNT(*) AS n FROM spend_ledger")[0]["n"] == 0


def test_spend_reads_the_setting_on_every_call(store):
    spend = _spend(store)
    _, aid = _ticket_attempt(store, REPO)
    c = CallContext(kind="attempt", attempt_id=aid)
    assert spend.hook(CLOUD, 0.01, c, phase="estimate") is True
    spend.hook(CLOUD, 0.01, c, phase="actual")
    store.set_cloud(REPO, False)
    assert spend.hook(CLOUD, 0.01, c, phase="estimate") is False


# --------------------------------------------------------------------------- AI review

INKLING = ModelEndpoint("inkling", 1, "local", "inkling-2", LOCAL_URL, "inkling")
QWEN2 = ModelEndpoint("qwen-big", 2, "local", "qwen3.8", LOCAL_URL, "qwen")
GLM_EP = ModelEndpoint(
    "glm", 3, "cloud", "glm-5.3", "https://api.z.ai/v4", "glm", "keychain:foreman-inator/glm"
)
OPUS_EP = ModelEndpoint(
    "claude-opus",
    3,
    "cloud",
    "opus",
    "https://api.anthropic.com/v1",
    "claude",
    "keychain:foreman-inator/claude-opus",
)


GLM_T2 = replace(GLM_EP, tier=2)


def _cfg(*models: ModelEndpoint, reviewers=("claude-opus", "glm")) -> Config:
    return Config(models=models, reviewer_models=reviewers)


def test_local_reviewer_picks_a_local_model_of_another_family():
    cfg = _cfg(INKLING, QWEN2, GLM_EP, OPUS_EP)
    assert local_reviewer(cfg, INKLING) == QWEN2
    assert local_reviewer(cfg, QWEN2) == INKLING
    assert local_reviewer(_cfg(INKLING, GLM_T2, OPUS_EP), INKLING) is None
    # reviewer_models order comes first when it names a local model
    cfg = _cfg(INKLING, QWEN2, GLM_EP, OPUS_EP, reviewers=("qwen-big", "claude-opus"))
    assert local_reviewer(cfg, GLM_EP) == QWEN2


class _Steps(ReviewSteps):
    """Just enough of the worker to run ``review_stage`` up to the reviewer pick."""

    def __init__(self, store: Store, config: Config) -> None:
        self.store, self.config = store, config
        self.handed: list[tuple[str, bool]] = []
        self.launched: list[dict] = []
        self.poller = SimpleNamespace(ci=False)
        self.proxy = self.recordings_dir = self.review_timeout = None

    def repo_profile(self, ticket):
        return prof.DEFAULT

    def sandbox_ready(self, _dir):
        return None

    def _mirror(self, repo):
        return SimpleNamespace(repo_dir=Path("."), resolve=lambda ref: "h" * 40)

    def hand_over(self, t, reason, *, ready, attempt=None):
        self.handed.append((reason, ready))

    def _launch_review(self, **kw):
        self.launched.append(kw)
        raise _Stop

    def _trace(self, t):
        import contextlib

        return contextlib.nullcontext()


class _Stop(Exception):
    pass


def _review_setup(store: Store, writer: ModelEndpoint, cloud: bool):
    store.set_cloud(REPO, cloud)
    t = store.create_ticket(repo=REPO, issue_number=1, title="t", request="r", base_branch="m")
    a = store.add_attempt(t.id, model=writer.name, tier=writer.tier, trigger="first")
    store.finish_attempt(a.id, tests_passed=True)
    return store.get_ticket(t.id), store.get_attempt(a.id)


def test_local_only_review_without_a_local_reviewer_is_skipped_with_a_note(store):
    steps = _Steps(store, _cfg(INKLING, GLM_T2, OPUS_EP))
    t, a = _review_setup(store, INKLING, cloud=False)
    steps.review_stage(t, a, threading.Event())
    assert steps.launched == []  # no model call, nothing left the Mac
    assert steps.handed == [(local_only_skip_reason(INKLING), True)]
    assert steps.handed[0][0].startswith("review_skipped:")


def test_local_only_review_uses_the_local_reviewer(store):
    steps = _Steps(store, _cfg(INKLING, QWEN2, GLM_EP, OPUS_EP))
    t, a = _review_setup(store, INKLING, cloud=False)
    with pytest.raises(_Stop):
        steps.review_stage(t, a, threading.Event())
    assert steps.launched[0]["reviewer"] == QWEN2 and steps.handed == []


def test_cloud_repo_review_is_unchanged(store):
    steps = _Steps(store, _cfg(INKLING, QWEN2, GLM_EP, OPUS_EP))
    t, a = _review_setup(store, INKLING, cloud=True)
    with pytest.raises(_Stop):
        steps.review_stage(t, a, threading.Event())
    assert steps.launched[0]["reviewer"] == OPUS_EP


def test_champion_on_a_cloud_model_is_not_used_for_a_local_only_repo(store):
    from fde_kit.prompts import PromptRef

    from foreman_inator.experiments import variants

    ref = rv.load_reviewer_template()
    vid = variants.register(store, "reviewer", "glm", PromptRef(ref.name, ref.sha256, ""))
    store.set_champion("reviewer", vid if isinstance(vid, str) else vid.id)
    steps = _Steps(store, _cfg(INKLING, QWEN2, GLM_EP, OPUS_EP))
    assert steps.champion_reviewer(INKLING)[0] == GLM_EP
    assert steps.champion_reviewer(INKLING, local_only=True)[0] == QWEN2
    with pytest.raises(rv.NoIndependentReviewer):
        _Steps(store, _cfg(INKLING, GLM_T2, OPUS_EP)).champion_reviewer(INKLING, local_only=True)


# --------------------------------------------------------------------------- shadow


def test_shadow_with_a_cloud_challenger_skips_a_local_only_repo(store):
    t, a = _review_setup(store, INKLING, cloud=False)
    store.record_review(a.id, reviewer_model="qwen-big", verdict="approve", variant_id="champ")
    calls: list = []
    res = S.shadow_review(
        store=store,
        issue=rv.IssueText(1, "t", "r"),
        change=object(),  # type: ignore[arg-type]
        writer=INKLING,
        challenger=OPUS_EP,
        challenger_prompt=rv.REVIEWER_PROMPT_NAME,
        challenger_variant=S.variant_id_for(OPUS_EP, rv.REVIEWER_PROMPT_NAME),
        proxy=None,
        attempt_id=a.id,
        launch=lambda **k: calls.append(k),
    )
    assert res.status == "skipped" and res.code == "local_only" and calls == []
    # recorded once (7e): a second call returns the recorded skip
    assert S.recorded_skip(store, REPO, res.variant_id, a.id) is not None


def test_shadow_with_a_local_challenger_still_runs_for_a_local_only_repo(store):
    t, a = _review_setup(store, INKLING, cloud=False)
    store.record_review(a.id, reviewer_model="glm", verdict="approve", variant_id="champ")
    calls: list = []

    def launch(**k):
        calls.append(k)
        raise _Stop

    res = S.shadow_review(
        store=store,
        issue=rv.IssueText(1, "t", "r"),
        change=object(),  # type: ignore[arg-type]
        writer=INKLING,
        challenger=QWEN2,
        challenger_prompt=rv.REVIEWER_PROMPT_NAME,
        challenger_variant=S.variant_id_for(QWEN2, rv.REVIEWER_PROMPT_NAME),
        proxy=None,
        attempt_id=a.id,
        launch=launch,
    )
    assert len(calls) == 1 and res.code == "error"  # it got as far as the launch


# --------------------------------------------------------------------------- replay sets


def _cases(store: Store) -> int:
    now = T0.isoformat()
    t1 = store.create_ticket(repo=REPO, issue_number=1, title="a", request="r", base_branch="m")
    t2 = store.create_ticket(repo=OTHER, issue_number=2, title="b", request="r", base_branch="m")
    with store.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO replay_sets (role, built_at, max_merges, case_count, skipped_count)"
            " VALUES ('writer', ?, 100, 0, 2)",
            (now,),
        )
        set_id = int(cur.lastrowid)
        for t in (t1, t2):
            conn.execute(
                "INSERT INTO replay_cases (set_id, role, ticket_id, attempt_id, repo, status,"
                " skip_reason, data, created_at) VALUES (?, 'writer', ?, NULL, ?, 'skipped',"
                " 'x', NULL, ?)",
                (set_id, t.id, t.repo, now),
            )
    return set_id


def test_replay_cases_of_a_local_only_repo_are_left_out_unless_all_models_are_local(store):
    set_id = _cases(store)
    both = {c.repo for c in rs.list_cases(store, set_id, include_skipped=True)}
    assert both == {REPO, OTHER}
    store.set_cloud(REPO, False)  # after the set was built
    assert {c.repo for c in rs.list_cases(store, set_id, include_skipped=True)} == {OTHER}
    local = rs.list_cases(store, set_id, include_skipped=True, cloud_models=False)
    assert {c.repo for c in local} == {REPO, OTHER}
