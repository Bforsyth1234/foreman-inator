"""#72: cheapest-first cross-family reviewer pick, and the one low-confidence escalation.

The cost order reads ``proxy/pricing.py``; families come from the configured endpoints.
``review_with_escalation`` is driven with a fake ``review`` callable (the worker passes
its sandboxed ``_launch_review``), so no repo, model or proxy is needed here.
"""

from __future__ import annotations

import dataclasses
from threading import Event
from typing import Any

import pytest

from foreman_inator.config import Config as _Config
from foreman_inator.config import ModelEndpoint, key_ref, local_tier2_models
from foreman_inator.review import checks as C
from foreman_inator.review import reviewer as R
from foreman_inator.review.reviewer import (
    FamilyConflict,
    NoIndependentReviewer,
    ReviewOutcome,
    cheapest_shadow_challenger,
    escalation_reviewer,
    low_confidence,
    pick_reviewer,
    review_with_escalation,
    reviewer_candidates,
)


def Config(**kw: Any) -> _Config:  # noqa: N802 - stands in for the class
    """The pre-#70 local tier 2 ladder these cases were written against (Qwen at tier 2)."""
    return _Config(**({"models": local_tier2_models()} | kw))


# A cheap third family (DeepSeek-like). Its id must be priced (an unpriced one gets the
# conservative, highest price), so it borrows glm-4.5-air's price under its own family.
THIRD = ModelEndpoint(
    "deepseek", 3, "cloud", "glm-4.5-air", "https://api.example.test/v1", "deepseek",
    key_ref("deepseek"),
)  # fmt: skip
# A third family priced between GLM and Opus (borrows claude-sonnet-5-5's price): GLM is
# then the cheapest candidate overall, so only the family rule keeps it off GLM's work.
MIDDLE = ModelEndpoint(
    "kimi", 3, "cloud", "claude-sonnet-5-5", "https://api.example.test/v1", "kimi",
    key_ref("kimi"),
)  # fmt: skip
UNPRICED = ModelEndpoint(
    "mystery", 3, "cloud", "mystery-model-9", "https://api.example.test/v1", "mystery",
    key_ref("mystery"),
)  # fmt: skip


def _cfg(*extra: ModelEndpoint, **kw: Any) -> Config:
    base = Config()
    return Config(models=(*base.models, *extra), **kw)


# --------------------------------------------------------------------------- pick


def test_configured_order_is_todays_pick_for_every_writer():
    cfg = Config()
    for writer in cfg.models:
        assert pick_reviewer(cfg, writer) == cfg.reviewer_for(writer)
        assert pick_reviewer(cfg, writer, order="configured") == cfg.reviewer_for(writer)


def test_configured_order_keeps_an_explicit_reviewer_models_override():
    cfg = _cfg(THIRD, reviewer_models=("claude-opus", "deepseek", "glm"))
    assert pick_reviewer(cfg, cfg.model("glm")) == cfg.model("claude-opus")
    assert pick_reviewer(cfg, cfg.model("claude-opus")) == cfg.model("deepseek")


def test_cheapest_local_writer_gets_glm():
    cfg = Config()
    for name in ("inkling-2-mini", "qwen3.8-27b"):
        assert pick_reviewer(cfg, cfg.model(name), order="cheapest") == cfg.model("glm")


def test_cheapest_glm_writer_gets_the_third_family_never_glm():
    cfg = _cfg(THIRD)
    got = pick_reviewer(cfg, cfg.model("glm"), order="cheapest")
    assert got == THIRD
    assert got.family != "glm"


def test_cheapest_glm_writer_skips_glm_even_when_glm_is_cheapest():
    cfg = _cfg(MIDDLE)
    assert reviewer_candidates(cfg)[0].name == "glm"
    assert pick_reviewer(cfg, cfg.model("glm"), order="cheapest") == MIDDLE
    # Every non-GLM writer gets the cheapest, GLM.
    assert pick_reviewer(cfg, cfg.model("qwen3.8-27b"), order="cheapest").name == "glm"


def test_cheapest_claude_writer_gets_glm():
    cfg = _cfg(THIRD)
    # deepseek (glm-4.5-air prices) is cheaper than glm-5.3, so a Claude writer gets it;
    # with only Opus and GLM it gets GLM, as today.
    assert pick_reviewer(cfg, cfg.model("claude-opus"), order="cheapest") == THIRD
    plain = Config()
    for name in ("claude-opus", "claude-fable"):
        assert pick_reviewer(plain, plain.model(name), order="cheapest") == plain.model("glm")


def test_with_only_opus_and_glm_cloud_writers_match_today():
    cfg = Config()
    for name in ("glm", "claude-opus", "claude-fable"):
        w = cfg.model(name)
        assert pick_reviewer(cfg, w, order="cheapest") == cfg.reviewer_for(w)


def test_candidates_are_priced_cheapest_first_and_unpriced_last():
    cfg = _cfg(UNPRICED, THIRD)
    names = [m.name for m in reviewer_candidates(cfg)]
    assert names == ["deepseek", "glm", "claude-opus", "claude-fable", "mystery"]
    assert "inkling-2-mini" not in names  # local writers are not reviewers unless named


def test_a_local_reviewer_named_in_reviewer_models_is_a_candidate():
    cfg = Config(reviewer_models=("claude-opus", "inkling-2-mini"))
    assert reviewer_candidates(cfg)[0].name == "inkling-2-mini"  # free sorts first
    assert pick_reviewer(cfg, cfg.model("glm"), order="cheapest").name == "inkling-2-mini"


def test_cheapest_refuses_when_no_family_differs():
    all_glm = tuple(dataclasses.replace(m, family="glm") for m in Config().models)
    cfg = Config(models=all_glm, reviewer_models=("glm",))
    with pytest.raises(NoIndependentReviewer):
        pick_reviewer(cfg, cfg.model("glm"), order="cheapest")


def test_unknown_order_is_refused():
    cfg = Config()
    with pytest.raises(ValueError, match="reviewer order"):
        pick_reviewer(cfg, cfg.model("glm"), order="random")


def test_family_rule_is_checked_on_the_result(monkeypatch):
    cfg = Config()
    glm = cfg.model("glm")
    # A candidate list that offers the writer's own family must still be refused (7f).
    monkeypatch.setattr(R, "_independent", lambda reviewer, writer: True)
    with pytest.raises(FamilyConflict):
        pick_reviewer(cfg, glm, order="cheapest")


# --------------------------------------------------------------------------- confidence


def _outcome(
    *,
    reviewer: str = "glm",
    verdict: str = "approve",
    read_diff: str = "approve",
    nonvacuous: bool | None = True,
    coverage: float | None = 90.0,
    model_calls: int = 1,
    cost: float = 0.01,
) -> ReviewOutcome:
    return ReviewOutcome(
        verdict=verdict,
        tag=None if verdict != "request_changes" else "wrong",
        note="n",
        tests=C.TestsCheck("passed", True),
        coverage=C.CoverageCheck("passed", coverage),
        revert=C.RevertCheck("passed", nonvacuous),
        run_app=C.RunAppCheck("not_runnable", "not_runnable"),
        read_diff=read_diff,
        findings=(),
        tokens_in=1000,
        tokens_out=100,
        cost_usd=cost,
        model_calls=model_calls,
        reviewer_model=reviewer,
        variant_id=f"{reviewer}/reviewer-v1@abcdef12",
        prompt_name="reviewer-v1",
        prompt_hash="abcdef12" * 8,
        seconds=2.0,
        comment_body="<!-- foreman-inator:ai-review -->\nbody\n",
    )


def test_low_confidence_rules():
    assert low_confidence(_outcome()) is None
    assert low_confidence(_outcome(nonvacuous=None, coverage=80.0)) is None
    assert "coverage unknown" in low_confidence(_outcome(nonvacuous=None, coverage=None))
    assert "20%" in low_confidence(_outcome(nonvacuous=False, coverage=20.0))
    assert "correction" in low_confidence(_outcome(model_calls=2))
    assert "parsed" in low_confidence(
        _outcome(verdict="handed_to_user", read_diff="malformed", model_calls=2)
    )
    # Verdicts the model did not make are never low confidence.
    for rd in ("skipped", "error"):
        assert low_confidence(_outcome(verdict="handed_to_user", read_diff=rd, coverage=None,
                                       nonvacuous=None)) is None  # fmt: skip
    # A request_changes with weak checks is the model's call; only a retry makes it weak.
    rc = _outcome(verdict="request_changes", read_diff="request_changes", coverage=None)
    assert low_confidence(rc) is None


def test_escalation_reviewer_is_todays_pick_and_never_the_writers_family():
    cfg = _cfg(THIRD)
    glm, opus, local = cfg.model("glm"), cfg.model("claude-opus"), cfg.model("qwen3.8-27b")
    assert escalation_reviewer(cfg, local, glm) == opus
    assert escalation_reviewer(cfg, glm, THIRD) == opus
    assert escalation_reviewer(cfg, opus, glm) is None  # Opus may not review Claude
    assert escalation_reviewer(cfg, local, opus) is None  # already the strong reviewer


# --------------------------------------------------------------------------- escalation


class FakeReview:
    def __init__(self, *outcomes: ReviewOutcome):
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kw: Any) -> ReviewOutcome:
        self.calls.append(kw)
        return self.outcomes.pop(0)


def test_low_confidence_cheap_verdict_triggers_exactly_one_opus_review():
    cfg = Config()
    local, glm, opus = cfg.model("qwen3.8-27b"), cfg.model("glm"), cfg.model("claude-opus")
    cheap = _outcome(reviewer="glm", nonvacuous=None, coverage=None, cost=0.02)
    # The Opus review is low confidence too: still no third review.
    strong = _outcome(reviewer="claude-opus", nonvacuous=None, coverage=None, cost=0.30)
    fake = FakeReview(cheap, strong)
    res = review_with_escalation(
        fake, config=cfg, writer=local, reviewer=glm, order="cheapest", attempt_id=7
    )
    assert len(fake.calls) == 2
    assert [c["reviewer"] for c in fake.calls] == [glm, opus]
    assert all(c["store"] is None for c in fake.calls)
    assert fake.calls[1]["precomputed"] == (
        cheap.tests,
        cheap.coverage,
        cheap.revert,
        cheap.run_app,
    )
    assert res.first is cheap and res.second is strong
    assert res.outcome.reviewer_model == "claude-opus"
    assert res.outcome.cost_usd == pytest.approx(0.32)
    assert res.outcome.model_calls == 2 and res.outcome.tokens_in == 2000
    assert "Second review by `claude-opus`" in res.outcome.comment_body
    assert res.reason and "coverage unknown" in res.reason


def test_confident_cheap_verdict_is_not_escalated():
    cfg = Config()
    fake = FakeReview(_outcome())
    res = review_with_escalation(
        fake, config=cfg, writer=cfg.model("qwen3.8-27b"), reviewer=cfg.model("glm"),
        order="cheapest", attempt_id=1,
    )  # fmt: skip
    assert len(fake.calls) == 1 and res.second is None and res.outcome is res.first


def test_configured_order_never_escalates():
    cfg = Config()
    fake = FakeReview(_outcome(reviewer="claude-opus", nonvacuous=None, coverage=None))
    res = review_with_escalation(
        fake, config=cfg, writer=cfg.model("glm"), reviewer=cfg.model("claude-opus"),
        attempt_id=1,
    )  # fmt: skip
    assert len(fake.calls) == 1 and res.second is None


def test_claude_writer_low_confidence_has_nobody_to_escalate_to():
    cfg = Config()
    fake = FakeReview(_outcome(nonvacuous=None, coverage=None))
    res = review_with_escalation(
        fake, config=cfg, writer=cfg.model("claude-opus"), reviewer=cfg.model("glm"),
        order="cheapest", attempt_id=1,
    )  # fmt: skip
    assert len(fake.calls) == 1 and res.second is None


def test_cancel_skips_the_second_review_and_store_is_refused():
    cfg = Config()
    cancel = Event()
    cancel.set()
    fake = FakeReview(_outcome(nonvacuous=None, coverage=None))
    review_with_escalation(
        fake, config=cfg, writer=cfg.model("qwen3.8-27b"), reviewer=cfg.model("glm"),
        order="cheapest", attempt_id=1, cancel=cancel,
    )  # fmt: skip
    assert len(fake.calls) == 1
    with pytest.raises(ValueError, match="store"):
        review_with_escalation(
            FakeReview(_outcome()), config=cfg, writer=cfg.model("glm"),
            reviewer=cfg.model("claude-opus"), attempt_id=1, store=object(),
        )  # fmt: skip


# --------------------------------------------------------------------------- measure first


def test_cheapest_shadow_challenger_is_glm_against_opus_by_default():
    assert cheapest_shadow_challenger(Config()).name == "glm"
    assert cheapest_shadow_challenger(_cfg(THIRD)).name == "deepseek"
    base = Config()
    keep = ("inkling-2-mini", "qwen3.8-27b", "claude-opus")
    only = Config(models=tuple(base.model(n) for n in keep), reviewer_models=("claude-opus",))
    assert cheapest_shadow_challenger(only) is None
