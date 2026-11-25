"""Milestone 4 wired into the worker, the CLI and the metrics page (todo #29).

Scenario tests on ``fakes.worker_env`` (FakeGitHub linked to a real bare git repo,
FakeOpenCode behind the real driver, FakeModels behind the real proxy, a real store):

* #23 router: kNN past the threshold, the rules below it or on an embedding failure; the
  embedding model is served but never a rung; the explore decision is stored once,
  survives a restart and a config change, and a ``tier:N`` label clears it; the backfill
  never blocks an attempt; crash between the kNN route and the stored decision.
* #24 variants: every attempt and review registers its variant; the writer loads the
  champion's prompt name; ``fman status`` shows the champion and the drift line.
* #27 labels: after a user kickback (by rule: reviewer_miss) and an AI kickback (by the
  model), on the background lane, queued only after the transaction committed, never
  delaying the next attempt; the step it stores is the step the trace view highlights;
  killed mid-label; the tries cap.
* #26 shadow: the challenger reviews after the champion's review is finalized, never
  posts, never takes a live slot; a failure is recorded once and forgotten; killed
  mid-shadow.
* #25/#29 experiments: the Verify scenario (wins on replay, holds in shadow, promote
  switches the champion; a challenger that fails live is rejected), writer promotion only
  with ``--replay-only``, ``fman experiment run`` beside the worker (own proxy, own
  mirrors, the run lock, signals), a kill mid-replay; ``fman replay`` on the checked-in
  recordings; the nightly launchd agent; the metrics pages.
"""

from __future__ import annotations

import gzip
import json
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from fakes.models import Reply
from fakes.opencode import Script, edit, say
from fakes.worker_env import (
    APPROVE,
    REPO,
    USER,
    Env,
    SimulatedCrash,
    failing,
    git,
    passing,
    request_changes,
)
from foreman_inator import worker_experiments as wx
from foreman_inator.config import Config, ConfigError, ModelEndpoint, key_ref
from foreman_inator.db.store import Store
from foreman_inator.experiments import replay_runner as rr
from foreman_inator.experiments import shadow, variants
from foreman_inator.metrics import queries as q
from foreman_inator.metrics.web import full_app
from foreman_inator.routing import escalation, explore, knn
from foreman_inator.setup import launchd
from foreman_inator.tracing import replay as tr
from foreman_inator.worker_background import LABEL_TRIES, label_tries_cursor
from foreman_inator.worker_status import render_status

RECORDINGS = Path(__file__).parent / "recordings"
BASE_URL = "http://127.0.0.1"


@pytest.fixture
def env(tmp_path: Path):
    e = Env(tmp_path)
    yield e
    e.close()


def embed_calls(env: Env) -> int:
    return sum(1 for r in env.models.requests if r.url.endswith("/embeddings"))


def seed_accepted(env: Env, n: int, tier: int, *, start: int = 900, vectors: bool = True):
    """``n`` accepted tickets on ``tier`` (routed there), each with a stored vector equal
    to what the fake embedder returns, so every one is a nearest neighbour."""
    model = env.config.models_for_tier(tier)[0].name
    out = []
    for i in range(n):
        t = env.store.create_ticket(
            repo=REPO,
            issue_number=start + i,
            title=f"Old fix {i}",
            request="Fix the typo in the README.",
            base_branch="main",
        )
        a = env.store.add_attempt(t.id, model=model, tier=tier, trigger="first")
        env.store.finish_attempt(a.id, None, tests_passed=True)
        env.store.update_ticket(t.id, routed_tier=tier)
        env.store.set_accepted_attempt(t.id, a.id)
        t = env.store.set_column(t.id, "done")
        if vectors:
            knn.store_vector(
                env.store,
                t.id,
                model=env.config.embedding_model,
                vector=knn.normalise([0.1, 0.2, 0.3]),
                digest=knn.text_digest(knn.request_text(t)),
            )
        out.append(t)
    return out


def explore_cursor(env: Env, n: int = 1) -> str | None:
    t = env.ticket(n)
    return env.store.get_cursor(t.repo, explore.explore_cursor(t.id))


def gh_writes(env: Env) -> int:
    return len(env.gh.write_spans)


# =========================================================================== #23 router


def test_knn_routes_past_the_threshold(tmp_path: Path) -> None:
    """3 accepted tickets on tier 2 (threshold 3): a new ticket the rules would put on
    tier 1 starts on tier 2, the reason lists its neighbours, one embedding call."""
    e = Env(tmp_path, knn_threshold=3, knn_k=3)
    try:
        old = seed_accepted(e, 3, tier=2)
        n = e.label("Typo", "Fix the typo.")
        e.once(failing())  # routing only: no review needed
        t = e.ticket(n)
        assert t.routed_tier == 2 and t.route_reason.startswith("knn: 3 nearest of 3 accepted")
        assert all(f"#{o.id} t2" in t.route_reason for o in old)
        assert e.route_calls == 0 and embed_calls(e) == 1  # the rules router was not asked
        (a,) = e.attempts(n)
        assert (a.tier, a.trigger) == (2, "first")
        assert knn.stored_vector(
            e.store,
            t.id,
            model=e.config.embedding_model,
            digest=knn.text_digest(knn.request_text(t)),
        )
    finally:
        e.close()


def test_rules_router_below_the_threshold_and_on_an_embedding_failure(tmp_path: Path) -> None:
    e = Env(tmp_path, knn_threshold=4)
    try:
        seed_accepted(e, 3, tier=2)
        n = e.label("Typo", "Fix the typo.")
        e.once(failing())  # routing only: no review needed
        assert e.ticket(n).routed_tier == 1 and e.route_calls == 1
        assert embed_calls(e) == 0  # below the threshold: no embedding call at all
    finally:
        e.close()
    (tmp_path / "second").mkdir()
    f = Env(tmp_path / "second", knn_threshold=3, clarify=False)
    try:
        seed_accepted(f, 3, tier=2)
        f.models.fail_next(500)  # the next model call, the embedding, fails
        n = f.label("Typo", "Fix the typo.")
        f.once(failing())  # routing only: no review needed
        t = f.ticket(n)
        assert t.routed_tier == 1 and t.route_reason.startswith("rules:")
        assert f.route_calls == 1
    finally:
        f.close()


def test_embedding_model_is_served_by_the_proxy_but_never_a_rung(env: Env) -> None:
    emb = env.config.embedding_model
    assert emb in env.proxy.models and env.proxy.models[emb].family == "embedding"
    assert emb not in {r.model for r in env.worker.ladder}
    assert emb not in {m.name for m in env.config.models}
    with pytest.raises(ConfigError, match="differ from every writer model"):
        Config(embedding_model=env.config.models[0].name)
    with pytest.raises(ConfigError, match="stays local"):
        Config(embedding_base_url="https://embed.example.com/v1")


def test_explore_decision_is_stored_once_and_survives_a_restart(tmp_path: Path) -> None:
    e = Env(tmp_path, explore_rate=1.0)
    try:
        e.route_label = "normal"  # tier 2: there is a tier below to explore
        n = e.label()
        e.once(failing())  # routing only: no review needed
        assert explore_cursor(e, n) == "1"
        (a,) = e.attempts(n)
        assert (a.trigger, a.tier) == ("explore", 1)
        e.config = replace(e.config, explore_rate=0.0)  # the config changes meanwhile
        e.restart()
        assert e.worker.explore(e.ticket(n)) is True  # the stored decision, not a new draw
        assert e.worker.history(e.ticket(n)).explore is True
    finally:
        e.close()


def test_a_tier_label_never_explores_and_clears_a_stored_decision(tmp_path: Path) -> None:
    e = Env(tmp_path, explore_rate=1.0)
    try:
        e.route_label = "normal"
        n = e.label("Big", "Rework it.", "tier:2")
        e.once(failing())  # routing only: no review needed
        assert explore_cursor(e, n) == "0" and e.attempts(n)[0].trigger == "first"
        # Routed (explores), then a tier:N label arrives before any attempt.
        m = e.label("Feature", "Add it.")
        e.worker.poll_and_handle()
        t = e.worker._route_ticket(e.ticket(m))
        assert explore_cursor(e, m) == "1" and t.routed_tier == 2
        e.gh.apply_label(REPO, m, "tier:3", by=USER)
        e.worker.poll_and_handle()
        assert explore_cursor(e, m) == "0" and e.ticket(m).routed_tier == 3
        assert e.worker.explore(e.ticket(m)) is False
    finally:
        e.close()


def test_killed_after_the_knn_route_before_the_explore_decision_is_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crash safety (a): nothing is stored, nothing reaches GitHub; after the restart the
    ticket is routed again with the stored vector (no second embedding call), and the
    same deterministic explore decision is stored."""
    e = Env(tmp_path, knn_threshold=3, explore_rate=1.0)
    try:
        seed_accepted(e, 3, tier=2)
        n = e.label()
        real = explore.should_explore

        def killed(*a: Any, **k: Any) -> bool:
            monkeypatch.setattr(explore, "should_explore", real)
            raise SimulatedCrash("killed after route_knn")

        monkeypatch.setattr(explore, "should_explore", killed)
        writes = gh_writes(e)
        with pytest.raises(SimulatedCrash):
            e.once(failing())  # routing only: no review needed
        t = e.ticket(n)
        assert t.routed_tier is None and explore_cursor(e, n) is None and not e.attempts(n)
        assert gh_writes(e) == writes and embed_calls(e) == 1
        e.restart()
        e.once(failing())  # routing only: no review needed
        t = e.ticket(n)
        assert t.routed_tier == 2 and t.route_reason.startswith("knn:")
        assert explore_cursor(e, n) == "1" and e.attempts(n)[0].trigger == "explore"
        assert embed_calls(e) == 1  # the vector stored before the crash was reused
    finally:
        e.close()


def test_backfill_runs_in_the_background_and_never_blocks_an_attempt(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = seed_accepted(env, 2, tier=1, vectors=False)
    gate, started = threading.Event(), threading.Event()
    real = knn.backfill

    def slow(*a: Any, **k: Any) -> Any:
        started.set()
        gate.wait(20)
        return real(*a, **k)

    monkeypatch.setattr(knn, "backfill", slow)
    n = env.label()
    env.once(failing())  # the poll queues the backfill, which hangs
    assert started.wait(10)
    (a,) = env.attempts(n)
    assert a.ended_at is not None and a.outcome == "failed_checks"  # it ran: not blocked
    gate.set()
    env.drain()
    for t in old:
        assert knn.stored_vector(
            env.store, t.id, model=env.config.embedding_model,
            digest=knn.text_digest(knn.request_text(t)),
        )  # fmt: skip


# =========================================================================== #24 variants


def test_every_attempt_and_review_has_a_variants_row(env: Env) -> None:
    n = env.label()
    env.once(passing())
    (a,) = env.attempts(n)
    (r,) = env.reviews(n)
    assert variants.lookup(env.store, a.variant_id).role == "writer"
    assert variants.lookup(env.store, r.variant_id).role == "reviewer"
    assert variants.champion(env.store, "writer") is not None
    champ = variants.champion(env.store, "reviewer")
    assert r.variant_id == champ.id and r.reviewer_model == champ.model


def test_the_writer_loads_the_champions_prompt_name(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from foreman_inator import prompts
    from foreman_inator import worker_attempt as wa

    real = prompts.load("writer-v1")
    asked: list[str] = []

    def load(name: str, prompts_dir: Any = None) -> Any:
        asked.append(name)
        if name == "writer-v9":
            return prompts.PromptRef(name="writer-v9", sha256="9" * 64, text=real.text)
        return prompts.load(name, prompts_dir)

    model = env.config.models_for_tier(1)[0].name
    v = env.store.upsert_variant(
        f"{model}/writer-v9@99999999", role="writer", model=model, prompt_name="writer-v9",
        prompt_hash="9" * 64,
    )  # fmt: skip
    variants.set_champion(env.store, "writer", v.id, note="test")
    monkeypatch.setattr(wa.prompts, "load", load)
    n = env.label()
    env.once(failing())  # routing only: no review needed
    assert "writer-v9" in asked
    assert env.attempts(n)[0].variant_id == f"{model}/writer-v9@99999999"


def test_status_shows_the_champions_and_the_prompt_drift_line(env: Env) -> None:
    text = render_status(env.store, config=env.config)
    champ = variants.champion(env.store, "writer")
    assert f"Champion writer: champion {champ.id}" in text
    assert text.splitlines()[-1].startswith("!!! Sandbox: OFF")  # still last
    # The champion's prompt file was edited since it was promoted:
    model = champ.model
    old = env.store.upsert_variant(
        f"{model}/writer-v1@0badcafe", role="writer", model=model, prompt_name="writer-v1",
        prompt_hash="0badcafe" + "0" * 56,
    )  # fmt: skip
    variants.set_champion(env.store, "writer", old.id)
    text = render_status(env.store, config=env.config)
    assert f"champion {old.id}; PROMPT CHANGED, running {champ.id} (not promoted)" in text


# =========================================================================== #27 labels


def label_on(edit_of: str = "feature.py"):
    """A fake labeler: ``wrong_edit`` at the listed step that edits ``edit_of``."""
    chosen: list[int] = []

    def reply(body: dict[str, Any]) -> str:
        text = "\n".join(str(m.get("content")) for m in body["messages"])
        steps = re.findall(r"^\[(\d+)\] (.*)$", text, re.M)
        pick = next((int(i) for i, line in steps if edit_of in line), int(steps[-1][0]))
        chosen.append(pick)
        return json.dumps({"label": "wrong_edit", "step": pick})

    return reply, chosen


def test_ai_kickback_is_labeled_in_the_background_and_the_viewer_marks_that_step(
    tmp_path: Path,
) -> None:
    """The #27/#28 cross-module check: the step the labeler stored is the step the trace
    view highlights (the edit of feature.py), with the worker's real recordings."""
    e = Env(tmp_path, failure_labels=True)
    try:
        e.label_reply, chosen = label_on()
        e.review_replies.append(request_changes("wrong", "It edits the wrong function."))
        queued_in_tx: list[int] = []
        real = e.worker.queue_label
        e.worker.queue_label = lambda repo, k: (queued_in_tx.append(e.store._tx_depth),
                                                real(repo, k))  # fmt: skip
        n = e.label()
        e.once(passing())
        e.drain()
        (k,) = e.store.list_kickbacks(ticket_id=e.ticket(n).id)
        assert (k.by, k.failure_step, k.failure_step_index) == ("reviewer", "wrong_edit", chosen[0])
        assert e.label_calls == 1 and queued_in_tx == [0]
        tl = tr.timeline(e.tmp / "recordings", e.ticket(n).id, rows=tr.ticket_rows(
            e.store, e.ticket(n).id, ladder=e.worker.ladder))  # fmt: skip
        rec = next(r for r in tl.recordings if r.attempt_id == k.attempt_id and r.role == "attempt")
        (marked,) = rec.failure_steps
        raw = tr.load_ticket(e.tmp / "recordings", e.ticket(n).id)[0].record
        assert raw.rows[marked.row_index]["seq"] == k.failure_step_index
        assert "feature.py" in json.dumps(raw.rows[marked.row_index])
    finally:
        e.close()


def test_user_kickback_after_an_ai_approve_is_a_reviewer_miss_by_rule(tmp_path: Path) -> None:
    """Labeled after the poll transaction committed, with no model call (the rule), and
    the next attempt does not wait for the labeler."""
    e = Env(tmp_path, failure_labels=True)
    try:
        gate = threading.Event()
        real = e.worker.label_fn
        e.worker.label_fn = lambda *a, **k: (gate.wait(20), real(*a, **k))[1]
        queued_in_tx: list[int] = []
        queue = e.worker.queue_label
        e.worker.queue_label = lambda repo, k: (queued_in_tx.append(e.store._tx_depth),
                                                queue(repo, k))  # fmt: skip
        n = e.label()
        e.once(passing())
        pr = e.ticket(n).pr_number
        e.gh.submit_review(REPO, pr, by=USER, state="REQUEST_CHANGES", body="Not this.")
        e.once(passing("def feature():\n    return 2\n"))
        assert len(e.attempts(n)) == 2  # the next attempt ran while the label waits
        (k,) = e.store.list_kickbacks(ticket_id=e.ticket(n).id)
        assert k.failure_step is None
        gate.set()
        e.drain()
        k = e.store.get_kickback(k.id)
        assert (k.failure_step, k.failure_step_index) == ("reviewer_miss", None)
        assert e.label_calls == 0 and queued_in_tx == [0]  # queued after the poll's commit
    finally:
        e.close()


def test_killed_while_labeling_relabels_after_the_restart_within_the_cap(
    tmp_path: Path,
) -> None:
    """Crash safety (c): the kickback stays unlabeled, nothing on GitHub or the ticket
    changes; the restart labels it (its second and last try); a rejected answer then
    stays NULL with no third call (7e)."""
    e = Env(tmp_path, failure_labels=True)
    try:
        hang = threading.Event()
        e.worker.label_fn = lambda *a, **k: hang.wait(30)  # the process "dies" here
        e.label_reply = "not json"  # the model's answer is rejected
        e.review_replies.append(request_changes())
        n = e.label()
        e.once(passing())
        (k,) = e.store.list_kickbacks(ticket_id=e.ticket(n).id)
        repo = e.ticket(n).repo
        assert e.store.get_cursor(repo, label_tries_cursor(k.id)) == "1"
        before = (e.ticket(n), gh_writes(e))
        e.restart()  # the old worker is gone mid-label
        e.drain()
        assert e.store.get_cursor(repo, label_tries_cursor(k.id)) == str(LABEL_TRIES)
        assert e.label_calls == 1 and e.store.get_kickback(k.id).failure_step is None
        assert (e.ticket(n), gh_writes(e)) == before
        e.restart()
        e.drain()
        assert e.label_calls == 1  # no third try
        hang.set()
    finally:
        e.close()


def test_the_lanes_stop_cleanly_on_shutdown(env: Env) -> None:
    gate = threading.Event()
    env.worker.local_lane.submit("hang", lambda: gate.wait(30))
    t = threading.Thread(target=env.worker.shutdown, kwargs={"timeout": 2})
    t.start()
    t.join(10)
    assert not t.is_alive()  # shutdown is bounded even with a task still running
    assert env.worker.local_lane.cancel.is_set() and env.worker.shadow_lane.cancel.is_set()
    assert env.worker.local_lane.submit("late", lambda: None) is False
    gate.set()


# =========================================================================== experiments env

LOCAL_URL = "http://127.0.0.1:11434/v1"


def exp_models() -> tuple[ModelEndpoint, ...]:
    """Explicit test models (never the config's defaults): two local writers and two cloud
    reviewers of other families; the champion reviewer is rev-x, the challenger rev-y."""
    return (
        ModelEndpoint("w-one", 1, "local", "w-one-up", LOCAL_URL, "fam-w"),
        ModelEndpoint("w-two", 2, "local", "w-two-up", LOCAL_URL, "fam-w2"),
        ModelEndpoint(
            "rev-x", 3, "cloud", "rev-x-up", "https://x.example.invalid/v1", "fam-x", key_ref("x")
        ),
        ModelEndpoint(
            "rev-y", 3, "cloud", "rev-y-up", "https://y.example.invalid/v1", "fam-y", key_ref("y")
        ),
    )


def exp_env(tmp: Path, **kw: Any) -> Env:
    from foreman_inator.config import ExperimentSettings

    kw.setdefault("experiments", ExperimentSettings(min_cases=2, live_min_sample=2))
    e = Env(tmp, models=exp_models(), reviewer_models=("rev-x", "rev-y"), **kw)
    #: what each reviewer says; the champion's reviews cost 100x the challenger's
    e.says = {"rev-x-up": APPROVE, "rev-y-up": APPROVE}
    e.review_by_model["rev-x-up"] = lambda b: Reply(e.says["rev-x-up"], tokens=(500, 500))
    e.review_by_model["rev-y-up"] = lambda b: Reply(e.says["rev-y-up"], tokens=(5, 5))
    return e


def calls_to(env: Env, upstream: str) -> int:
    return sum(1 for r in env.models.requests if (r.json or {}).get("model") == upstream
               and (r.json or {}).get("max_tokens"))  # fmt: skip


def ticket_to_testing(env: Env, i: int, script: Script | None = None) -> int:
    n = env.label(f"Feature {i}", f"Please add feature{i}().")
    env.once(script or passing(f"def feature{i}():\n    return {i}\n"))
    assert env.ticket(n).column == "testing", env.ticket(n)
    return n


def merge(env: Env, n: int) -> None:
    pr = env.ticket(n).pr_number
    env.gh.submit_review(REPO, pr, by=USER, state="APPROVE")
    env.gh.merge_pr(REPO, pr, by=USER)
    env.once()
    env.once()
    assert env.ticket(n).column == "done"


def experiment_process(env: Env, driver: Any = None) -> tuple[wx.RunEnv, Any]:
    """What `fman experiment run` builds: its own Spend and its own proxy (another port)
    in front of the same fake upstream, its own mirrors under ~/.foreman-inator/replays."""
    from foreman_inator.proxy.server import ModelProxy
    from foreman_inator.spend import Spend

    spend = Spend(env.store, env.config)
    proxy = ModelProxy(
        env.config.proxy_models(),
        store=env.store,
        transport=env.models.transport,
        spend_hook=spend.hook,
        key_resolver=lambda ep: "fake-test-key",
    ).start()
    deps = wx.build_deps(
        env.store, env.config, proxy=proxy, spend=spend, driver=driver,
        worker_root=env.tmp / "worktrees", recordings_dir=env.tmp / "recordings",
    )  # fmt: skip
    return wx.RunEnv(env.store, env.config, deps), proxy


LIVE_TABLES = ("tickets", "attempts", "reviews", "kickbacks")


def live_rows(store: Store) -> list[Any]:
    return [tuple(r) for t in LIVE_TABLES for r in store.query(f"SELECT * FROM {t} ORDER BY id")]


def history_and_replay(e: Env, monkeypatch: pytest.MonkeyPatch) -> tuple[wx.RunEnv, Any, int]:
    """Two merged tickets (the reviewer replay set), then `fman experiment run --role
    reviewer --challenger rev-y`: killed on its third case side (crash safety d), resumed
    by a second run, and won (same verdicts, 1/100th of the cost)."""
    for i in (1, 2):
        merge(e, ticket_to_testing(e, i))
    run, proxy = experiment_process(e)
    assert proxy is not e.proxy and proxy.base_url(0) != e.proxy.base_url(0)
    live, writes = live_rows(e.store), gh_writes(e)
    real, seen = rr.rv.review_change, []

    def killed_on_third(**kw: Any) -> Any:
        seen.append(kw["reviewer"].name)
        if len(seen) == 3:
            raise SimulatedCrash("experiment process killed mid-replay")
        return real(**kw)

    monkeypatch.setattr(rr.rv, "review_change", killed_on_third)
    with pytest.raises(SimulatedCrash):
        wx.run_experiment(run, "reviewer", "rev-y/reviewer-v1")
    (exp,) = [x for x in e.store.list_experiments() if x.mode == "replay"]
    assert len(rr.results(e.store, exp.id)) == 2  # the scored sides only
    monkeypatch.setattr(rr.rv, "review_change", real)
    c = wx.run_experiment(run, "reviewer", "rev-y/reviewer-v1")  # resumes
    assert c.experiment_id == exp.id and (c.state, c.scored_cases) == ("finished", 2)
    assert c.verdict == "wins", c.reason
    assert len(rr.results(e.store, exp.id)) == 4
    assert live_rows(e.store) == live and gh_writes(e) == writes  # nothing live touched
    worker_mirror = e.worker._mirror(REPO)
    assert not [p for p in worker_mirror.worktrees() if "replay" in p.name]
    (sh,) = shadow.running_shadow_experiments(e.store)  # started with the experiment
    return run, proxy, sh.id


def live_shadowed_ticket(e: Env, i: int) -> int:
    n = ticket_to_testing(e, i)
    pr = e.ticket(n).pr_number
    before = (len(e.bot_reviews(pr)), len(e.issue_comments(n)), gh_writes(e))
    e.once()  # nothing live is due: the shadow review is queued on its lane
    e.drain()
    t = e.ticket(n)
    (att,) = [a for a in e.attempts(n) if a.tests_passed]
    (row,) = e.store.list_reviews(attempt_id=att.id, shadow=True)
    assert row.reviewer_model == "rev-y" and e.store.list_kickbacks(ticket_id=t.id) == []
    assert (len(e.bot_reviews(pr)), len(e.issue_comments(n)), gh_writes(e)) == before
    assert t.column == "testing" and t.flag_reason is None
    return n


# =========================================================================== #29 Verify


def test_a_challenger_wins_replay_holds_in_shadow_and_promote_switches_the_champion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = exp_env(tmp_path)
    try:
        champion = variants.champion(e.store, "reviewer").id
        run, proxy, sh = history_and_replay(e, monkeypatch)
        refused = wx.promote(e.store, e.config, "rev-y/reviewer-v1@" + champion[-8:])
        assert refused.outcome == "refused"  # (not a registered variant id: wrong hash)
        early = wx.promote(e.store, e.config, str(sh))
        assert early.outcome == "refused" and "needed" in early.message  # no live data yet
        assert variants.champion(e.store, "reviewer").id == champion
        for i in (3, 4):
            merge(e, live_shadowed_ticket(e, i))
        assert calls_to(e, "rev-y-up") == 2 + 2  # two replay sides, two shadows (checks reused)
        res = wx.promote(e.store, e.config, str(sh))
        assert res.outcome == "promoted", res.message
        new = variants.champion(e.store, "reviewer")
        assert new.model == "rev-y" and new.id != champion
        (last,) = variants.champion_history(e.store, "reviewer")[-1:]
        assert "wins replay" in last.note and "holds live in shadow" in last.note
        assert {x.status for x in e.store.list_experiments()} == {"promoted"}
        n = ticket_to_testing(e, 5)  # the worker uses the new champion from its next review
        assert e.reviews(n)[-1].reviewer_model == "rev-y"
        proxy.stop()
    finally:
        e.close()


def test_a_challenger_that_loses_live_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = exp_env(tmp_path)
    try:
        champion = variants.champion(e.store, "reviewer").id
        run, proxy, sh = history_and_replay(e, monkeypatch)
        e.says["rev-y-up"] = request_changes("style", "Rename it.")  # live: kicks back merges
        for i in (3, 4):
            merge(e, live_shadowed_ticket(e, i))
        res = wx.promote(e.store, e.config, "rev-y/reviewer-v1@" + _hash8(e, "rev-y"))
        assert res.outcome == "rejected" and "does not hold live" in res.message
        assert variants.champion(e.store, "reviewer").id == champion  # unchanged
        assert {x.status for x in e.store.list_experiments()} == {"rejected"}
        note = json.loads(e.store.get_experiment(sh).result_note)
        assert note["live"]["status"] == "fails" and note["challenger"]["false_kickbacks"] == 2
        assert shadow.find_challenger(e.store, e.config) is None  # no more shadow traffic
        proxy.stop()
    finally:
        e.close()


def _hash8(e: Env, model: str) -> str:
    return next(v.id for v in e.store.list_variants(role="reviewer") if v.model == model)[-8:]


# =========================================================================== #26 shadow


def test_the_shadow_never_takes_a_live_slot_and_a_failure_is_recorded_once(
    tmp_path: Path,
) -> None:
    e = exp_env(tmp_path)
    try:
        champ = variants.champion(e.store, "reviewer").id
        ref = variants.register(e.store, "reviewer", "rev-y", __import__(
            "foreman_inator.prompts", fromlist=["load"]).load("reviewer-v1"))  # fmt: skip
        wx.ensure_shadow(e.store, champ, ref.id)
        gate, real = threading.Event(), e.worker._launch_shadow
        e.worker._launch_shadow = lambda **kw: (gate.wait(20), real(**kw))[1]
        n = ticket_to_testing(e, 1)
        e.once()  # the shadow starts on its lane and hangs on the challenger's model call
        merge(e, n)  # live work goes on meanwhile: the merge, then a new ticket
        m = ticket_to_testing(e, 2)
        assert e.reviews(m)[-1].reviewer_model == "rev-x"
        assert not e.worker.shadow_lane.idle  # the shadow of #1 is still hanging
        gate.set()
        e.drain()
        (row,) = e.store.list_reviews(ticket_id=e.ticket(n).id, shadow=True)
        assert row.reviewer_model == "rev-y"
        # A failure of any kind: logged, recorded once, never retried (7e).
        e.review_by_model["rev-y-up"] = lambda b: (_ for _ in ()).throw(RuntimeError("down"))
        e.once()
        e.drain()
        aid = e.attempts(m)[-1].id
        skip = shadow.recorded_skip(e.store, REPO, ref.id, aid)
        assert skip is not None and e.store.list_reviews(attempt_id=aid, shadow=True) == []
        before = calls_to(e, "rev-y-up")
        e.once()
        e.drain()
        assert calls_to(e, "rev-y-up") == before
        assert e.ticket(m).column == "testing" and e.ticket(m).flag_reason is None
    finally:
        e.close()


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_killed_mid_shadow_runs_it_again_and_stores_one_row(tmp_path: Path) -> None:
    """Crash safety (b): nothing is stored for a cut-short shadow; the live ticket is not
    touched; after the restart the shadow runs again and stores exactly one row."""
    e = exp_env(tmp_path)
    try:
        champ = variants.champion(e.store, "reviewer").id
        from foreman_inator import prompts

        ref = variants.register(e.store, "reviewer", "rev-y", prompts.load("reviewer-v1"))
        wx.ensure_shadow(e.store, champ, ref.id)
        n = ticket_to_testing(e, 1)
        e.worker._launch_shadow = lambda **kw: (_ for _ in ()).throw(SimulatedCrash("kill"))
        snapshot = (e.ticket(n), gh_writes(e), len(e.bot_reviews(e.ticket(n).pr_number)))
        e.once()
        e.drain()
        (att,) = e.attempts(n)
        assert e.store.list_reviews(attempt_id=att.id, shadow=True) == []
        assert shadow.recorded_skip(e.store, REPO, ref.id, att.id) is None
        assert (e.ticket(n), gh_writes(e), len(e.bot_reviews(e.ticket(n).pr_number))) == snapshot
        e.restart()
        e.once()  # finalize again (idempotent: no second comment), then idle
        e.once()
        e.drain()
        assert len(e.store.list_reviews(attempt_id=att.id, shadow=True)) == 1
        assert len(e.bot_reviews(e.ticket(n).pr_number)) == snapshot[2]
    finally:
        e.close()


# =========================================================================== writers


class ByModel:
    """The replay's writer driver: a script per writer model."""

    def __init__(self, env: Env, scripts: dict[str, Script]) -> None:
        self.env, self.scripts = env, scripts

    def run(self, **kw: Any) -> Any:
        self.env.oc.set_script(self.scripts[kw["endpoint"].name])
        return self.env.driver.inner.run(**kw)


def really_merge(env: Env, n: int) -> None:
    """GitHub's "Merge pull request" commit on the remote's main, as a real merge does."""
    t = env.ticket(n)
    work = env.tmp / f"merge-{n}"
    git("clone", "-q", str(env.remote), str(work), cwd=env.tmp)
    branch = next(b for b in env.gh.repos[REPO].branches if b.startswith(f"fman/issue-{n}/"))
    git("fetch", "-q", "origin", branch, cwd=work)
    git("merge", "-q", "--no-ff", "FETCH_HEAD", "-m", f"Merge pull request #{t.pr_number} from x",
        cwd=work)  # fmt: skip
    git("push", "-q", "origin", "main", cwd=work)
    merge(env, n)
    env.worker._mirror(REPO).fetch()


def test_a_writer_challenger_is_promoted_on_replay_only_with_the_flag(
    tmp_path: Path,
) -> None:
    e = exp_env(tmp_path)
    try:
        for i in (1, 2):
            test_file = edit(f"tests/test_f{i}.py", f"x = {i}\n")
            with_tests = Script([edit("status.txt", "pass\n"), test_file, say("Done.")])
            really_merge(e, ticket_to_testing(e, i, with_tests))
        loses = Script([edit("status.txt", "fail\n"), edit("feature.py", "x = 0\n"),
                        say("Tried.")])  # fmt: skip
        wins = Script([edit("status.txt", "pass\n"), edit("feature.py", "x = 1\n"), say("Done.")])
        run, proxy = experiment_process(e, ByModel(e, {"w-one": loses, "w-two": wins}))
        c = wx.run_experiment(run, "writer", "w-two/writer-v1")
        assert (c.verdict, c.champion.passed, c.challenger.passed) == ("wins", 0, 2), c.reason
        champion = variants.champion(e.store, "writer").id
        res = wx.promote(e.store, e.config, str(c.experiment_id))
        assert res.outcome == "refused" and "--replay-only" in res.message
        assert variants.champion(e.store, "writer").id == champion
        res = wx.promote(e.store, e.config, str(c.experiment_id), replay_only=True)
        assert res.outcome == "promoted"
        new = variants.champion(e.store, "writer")
        assert new.model == "w-two" and "replay only" in variants.champion_history(
            e.store, "writer")[-1].note  # fmt: skip
        assert e.store.get_experiment(c.experiment_id).status == "promoted"
        assert run_all_never_promotes(e, run)
        proxy.stop()
    finally:
        e.close()


def run_all_never_promotes(e: Env, run: wx.RunEnv) -> bool:
    before = [variants.champion(e.store, r).id for r in variants.ROLES]
    wx.run_all(run, echo=lambda s: None)
    return [variants.champion(e.store, r).id for r in variants.ROLES] == before


# =========================================================================== the process


def test_experiment_run_cli_lock_and_signals(tmp_path: Path, monkeypatch) -> None:
    from foreman_inator import paths
    from foreman_inator.cli import app

    Store(paths.db_path()).close()
    proxies: list[Any] = []

    def fake_proxy(cfg: Any, store: Any, spend: Any) -> Any:
        class P:
            spend_hook = on_usage = None

            def stop(self) -> None:
                proxies.append("stopped")

        return P()

    monkeypatch.setattr(wx, "make_proxy", fake_proxy)
    monkeypatch.setattr("foreman_inator.runner.opencode.OpenCodeDriver", lambda **kw: object())
    r = CliRunner().invoke(app, ["experiment", "run", "--all"])
    assert r.exit_code == 0, r.output
    assert "No unfinished replays" in r.output and proxies == ["stopped"]
    assert CliRunner().invoke(app, ["experiment", "run"]).exit_code == 1
    with wx.run_lock():  # another run holds the lock
        r = CliRunner().invoke(app, ["experiment", "run", "--all"])
        assert r.exit_code == 1 and "another `fman experiment run`" in r.output
    cancel = threading.Event()
    old = wx.install_cancel_handlers(cancel)
    try:
        signal.raise_signal(signal.SIGTERM)
        assert cancel.is_set()
    finally:
        for s, h in old.items():
            signal.signal(s, h)
    r = CliRunner().invoke(app, ["experiment", "status"])
    assert (
        r.exit_code == 0 and "Champion writer" in r.output and "Running experiments (0)" in r.output
    )
    r = CliRunner().invoke(app, ["experiment", "promote", "nope/reviewer-v1@00000000"])
    assert r.exit_code == 1 and "no experiment or registered variant" in r.output


def test_settings_and_config_fields_round_trip(tmp_path: Path) -> None:
    from foreman_inator import config as cfgmod
    from foreman_inator.config import ExperimentSettings

    cfg = Config(knn_threshold=7, explore_rate=0.25, failure_labels=False,
                 experiments=ExperimentSettings(run_budget_usd=1.5, nightly_hour=4))  # fmt: skip
    cfgmod.save(cfg)
    assert cfgmod.load() == cfg
    s = wx.settings_from(cfg)
    assert (s.run_budget_usd, s.min_cases, s.set_size) == (1.5, 10, 100)
    d = Config()
    assert (d.knn_threshold, d.knn_k, d.explore_rate, d.failure_labels) == (50, 10, 0.10, True)
    assert d.experiments.live_min_sample == shadow.MIN_LIVE_SAMPLE
    with pytest.raises(ConfigError):
        Config(explore_rate=1.5)


# =========================================================================== fman replay


@pytest.fixture
def cases(tmp_path: Path) -> Path:
    root = tmp_path / "cases"
    shutil.copytree(RECORDINGS, root, ignore=shutil.ignore_patterns("README.md"))
    for p in (root / "recordings").rglob("*.jsonl"):
        p.with_name(p.name + ".gz").write_bytes(gzip.compress(p.read_bytes()))
        p.unlink()
    return root


def test_fman_replay_on_the_checked_in_recordings(cases: Path, monkeypatch) -> None:
    from foreman_inator.cli import app

    args = ["--recordings", str(cases / "recordings"), "--traces", str(cases / "traces")]
    for tid in (1, 2):
        r = CliRunner().invoke(app, ["replay", str(tid), *args, "--rows",
                                     str(cases / "rows" / f"{tid}.json")])  # fmt: skip
        assert r.exit_code == 0, r.output
        assert "span tree: replay matches the trace" in r.output
        assert "decisions checked" in r.output and f"/traces/{tid}" in r.output
    r = CliRunner().invoke(app, ["replay", "1", *args, "--rows", str(cases / "rows" / "1.json")])
    assert "failure step wrong_edit at row" in r.output
    assert CliRunner().invoke(app, ["replay", "1x", *args]).exit_code == 2
    assert CliRunner().invoke(app, ["replay", "9", *args]).exit_code == 1
    monkeypatch.setattr(escalation, "MAX_CHECK_ATTEMPTS", 1)  # today's logic would differ
    r = CliRunner().invoke(app, ["replay", "1", *args, "--rows", str(cases / "rows" / "1.json")])
    assert r.exit_code == 1 and "tier 2" in r.output


# =========================================================================== launchd


def _fman(tmp: Path) -> Path:
    exe = tmp / "bin" / "fman"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return exe


class Launchctl:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")


def test_the_nightly_agent_runs_experiment_run_all(tmp_path: Path) -> None:
    lc = Launchctl()
    res = launchd.install_nightly(
        fman=_fman(tmp_path), agents_dir=tmp_path / "LA", launchctl=lc, uid=501, hour=3, minute=15
    )
    data = plistlib.loads(res.plist_path.read_bytes())
    assert data["Label"] == launchd.NIGHTLY_LABEL != launchd.LABEL
    assert data["ProgramArguments"][1:] == ["experiment", "run", "--all"]
    assert data["StartCalendarInterval"] == {"Hour": 3, "Minute": 15}
    assert "KeepAlive" not in data and "RunAtLoad" not in data and data["Nice"] > 0
    assert lc.calls[-1] == ["bootstrap", "gui/501", str(res.plist_path)]
    if sys.platform == "darwin" and shutil.which("plutil"):
        out = subprocess.run(["plutil", "-lint", str(res.plist_path)], capture_output=True,
                             text=True)  # fmt: skip
        assert out.returncode == 0, out.stdout + out.stderr
    gone = launchd.uninstall_nightly(agents_dir=tmp_path / "LA", launchctl=lc, uid=501)
    assert "removed" in gone.message and not res.plist_path.exists()


# =========================================================================== metrics page


def test_the_metrics_page_mounts_traces_failure_steps_and_experiments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = Env(tmp_path, failure_labels=True)
    try:
        e.label_reply, _ = label_on()
        e.review_replies.append(request_changes())
        n = e.label("Fix <script>alert(1)</script>", "x")
        e.once(passing())
        e.drain()
        champ = variants.champion(e.store, "reviewer").id
        e.store.create_experiment(role="reviewer", champion_variant=champ,
                                  challenger_variant="<b>x</b>/reviewer-v1@00000000",
                                  mode="shadow")  # fmt: skip
        app = full_app(e.tmp / "fman.db", recordings_dir=e.tmp / "recordings",
                       traces_dir=e.tmp / "traces", config=e.config)  # fmt: skip
        c = TestClient(app, base_url=BASE_URL)
        tid = e.ticket(n).id
        page = c.get(f"/traces/{tid}")
        assert page.status_code == 200 and "failure step: wrong_edit" in page.text
        assert "<script>alert" not in page.text
        f = c.get("/failures")
        assert f.status_code == 200 and "wrong_edit" in f.text and "Unlabeled:" in f.text
        assert e.attempts(n)[0].variant_id in f.text
        x = c.get("/experiments")
        assert (
            x.status_code == 200 and "&lt;b&gt;x&lt;/b&gt;" in x.text and "<b>x</b>" not in x.text
        )
        assert "live insufficient" in x.text
        for path in ("/", "/traces", "/failures", "/experiments"):
            body = c.get(path).text
            assert "<script" not in body.replace("&lt;script", "")
        evil = TestClient(app, base_url="http://evil.example")
        assert evil.get("/experiments").status_code == 403
        rows = q.failure_steps_by_variant(e.store)
        assert [(r.variant_id, r.steps, r.unlabeled) for r in rows] == [
            (e.attempts(n)[0].variant_id, {"wrong_edit": 1}, 0)
        ]
    finally:
        e.close()


def test_a_failing_backfill_backs_off_instead_of_retrying_every_poll(env: Env) -> None:
    seed_accepted(env, 1, tier=1, vectors=False)
    env.models.fail_next(500)  # the embedder is down
    env.worker.poll_and_handle()
    env.drain()
    assert embed_calls(env) == 1
    env.worker.poll_and_handle()  # the next poll: no new call (7e: no loop)
    env.drain()
    assert embed_calls(env) == 1
    env.clock_now += 5 * 60 + 1
    env.worker.poll_and_handle()
    env.drain()
    assert embed_calls(env) == 2  # retried after the back-off, and it works now
    t = env.store.list_tickets(repo=REPO)[0]
    digest = knn.text_digest(knn.request_text(t))
    assert knn.stored_vector(env.store, t.id, model=env.config.embedding_model, digest=digest)


def test_a_shadow_that_cannot_start_is_recorded_once_not_retried(tmp_path: Path) -> None:
    from foreman_inator import prompts

    e = exp_env(tmp_path)
    try:
        champ = variants.champion(e.store, "reviewer").id
        ref = variants.register(e.store, "reviewer", "rev-y", prompts.load("reviewer-v1"))
        wx.ensure_shadow(e.store, champ, ref.id)
        n = ticket_to_testing(e, 1)
        tries: list[int] = []

        def gone(*a: Any) -> str:
            tries.append(1)
            raise RuntimeError("the head commit is gone")

        e.worker._head_sha = gone
        for _ in range(3):
            e.once()
            e.drain()
        (att,) = e.attempts(n)
        assert tries == [1]  # one try, then the recorded skip stops it (7e)
        assert shadow.recorded_skip(e.store, REPO, ref.id, att.id).startswith("error:")
        assert e.ticket(n).column == "testing" and e.ticket(n).flag_reason is None
    finally:
        e.close()
