"""Wave 17 wiring: each slot's piece is actually called.

#37 resource snapshot per poll; #44 the experiments page in ``full_app``; #49 ``fman
experiment propose | approve-draft | discard-draft``, its config, the replay cutoff and the
page's "Propose an experiment"; #18 ``fman replay --fork``, the recording-name pattern and
the trace-view anchors; #19 the classifier config; #15 the relay (nudge, start, stop,
``fman setup --relay-url``); #71 ``vision`` and replayed image descriptions; #73 ``fman
retry`` and ``policy_for``; #77 the status comment's call sites.
"""

from __future__ import annotations

import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from fakes.worker_env import REPO, USER, Env, failing, passing
from foreman_inator import config as config_mod
from foreman_inator import export, paths
from foreman_inator import retry as retry_mod
from foreman_inator import setup as setup_mod
from foreman_inator import worker_experiments as wx
from foreman_inator import worker_promote as wp
from foreman_inator.cli import app
from foreman_inator.config import Config, ExperimentSettings
from foreman_inator.db.store import Store
from foreman_inator.experiments import propose as pp
from foreman_inator.experiments import replay_runner as rr
from foreman_inator.experiments import replay_sets as rs
from foreman_inator.experiments import variants
from foreman_inator.github import relay as relay_mod
from foreman_inator.metrics import experiments_ui as ui
from foreman_inator.metrics import forms, trace_view, web
from foreman_inator.routing import classifier, knn
from foreman_inator.tickets import images as images_mod
from foreman_inator.tracing import fork as fork_mod
from foreman_inator.worker import Worker
from foreman_inator.worker_resources import ResourceSteps
from foreman_inator.worker_status_comment import StatusCommentSteps

STATUS = "foreman-inator:status"
BASE = "http://127.0.0.1:8642"


@pytest.fixture
def env(tmp_path: Path):
    e = Env(tmp_path)
    yield e
    e.close()


def _invoke(*args: str) -> Any:
    return CliRunner().invoke(app, list(args))


def _status_bodies(gh: Any) -> list[str]:
    return [
        c.args["body"]
        for c in gh.writes
        if c.method in ("post_comment", "update_comment") and STATUS in c.args["body"]
    ]


# --------------------------------------------------------------------------- #77 status


def test_a_worked_ticket_keeps_one_status_comment_through_its_states(env: Env) -> None:
    """Non-vacuous: remove any of the attempt-start, verify, review or PR-opened
    ``status_update`` calls and its line is missing."""
    assert issubclass(Worker, StatusCommentSteps)
    n = env.label()
    env.once(passing())
    bodies = _status_bodies(env.gh)
    assert any("working on this: attempt 1 on tier 1" in b for b in bodies)
    assert any(b.endswith("verifying.") for b in bodies)
    assert any(b.endswith("in review.") for b in bodies)
    pr = env.ticket(n).pr_number
    assert pr is not None and any(f"opened PR #{pr}." in b for b in bodies)
    posted = [c for c in env.gh.writes_named("post_comment") if STATUS in c.args["body"]]
    assert len(posted) == 1  # one comment, edited in place
    assert [b for b in env.issue_comments(n) if STATUS in b] == [bodies[-1]]


def test_escalation_hand_over_and_drop_reach_the_status_comment(tmp_path: Path) -> None:
    e = Env(tmp_path)
    try:
        n = e.label()
        for _ in range(3):
            e.once(failing())
        assert any("escalated, attempt 3 on tier 2" in b for b in _status_bodies(e.gh))
        e.gh.close_issue_as(REPO, n, by=USER)
        e.once()
        assert e.ticket(n).column == "dropped"
        assert _status_bodies(e.gh)[-1].endswith("stopped working on this (dropped).")
    finally:
        e.close()
    e = Env(tmp_path / "b", verify=None)
    try:
        e.label()
        e.once()
        assert "handed this over: no_verify_command" in _status_bodies(e.gh)[-1]
    finally:
        e.close()


def test_status_comment_off_posts_nothing(tmp_path: Path) -> None:
    e = Env(tmp_path, status_comment=False)
    try:
        e.label()
        e.once(passing())
        assert _status_bodies(e.gh) == []
    finally:
        e.close()


# --------------------------------------------------------------------------- #37 resources


def test_each_poll_writes_the_resource_snapshot(env: Env) -> None:
    assert issubclass(Worker, ResourceSteps)
    target = paths.run_dir() / "resources.json"
    assert not target.exists()
    env.worker.poll_and_handle()
    assert target.exists() and '"memory"' in target.read_text()


# --------------------------------------------------------------------------- #44 page


def _full_client(cfg: Config) -> TestClient:
    db = paths.db_path()
    with Store(db) as s:
        variants.ensure_defaults(s, cfg)
    a = web.full_app(db, recordings_dir=paths.recordings_dir(), traces_dir=None, config=cfg)
    return TestClient(a, base_url=BASE)


def test_full_app_serves_the_experiments_page_and_links_it() -> None:
    c = _full_client(Config())
    page = c.get(ui.MANAGE_PATH)
    assert page.status_code == 200 and "Run experiments" in page.text
    assert "Propose an experiment" in page.text  # #69's section on it
    assert 'action="/experiments/propose"' in page.text  # enabled on a fresh database
    listing = c.get("/experiments")
    assert listing.status_code == 200
    assert f'<a href="{ui.MANAGE_PATH}">Start, stop, propose or promote' in listing.text
    assert f'<a href="{ui.MANAGE_PATH}"' in c.get("/failures").text  # in the nav


def test_the_page_propose_button_calls_propose_command(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Config()
    db = paths.db_path()
    with Store(db) as s:
        variants.ensure_defaults(s, cfg)
    seen: list[dict[str, Any]] = []

    def fake(**kw: Any) -> Any:
        seen.append(kw)
        return _draft(Path(paths.home()) / "writer-v2.md")

    monkeypatch.setattr(wx, "propose_command", fake)
    guard = forms.FormGuard("tok")
    routes = ui.routes(db, load_config=lambda: cfg, guard=guard, jobs=ui.Jobs())
    c = TestClient(web.LoopbackHostOnly(web.Starlette(routes=routes)), base_url=BASE)
    r = c.post("/experiments/propose", data={"csrf_token": "tok", "role": "writer"},
               headers={"origin": BASE})  # fmt: skip
    assert r.status_code == 200, r.text
    assert seen and seen[0]["role"] == "writer" and seen[0]["db"] == db
    assert "Approve and start" in r.text and "estimated cost" in r.text.lower()


def test_the_propose_button_is_disabled_with_the_reason() -> None:
    cfg = Config()
    db = paths.db_path()
    with Store(db) as s:
        variants.ensure_defaults(s, cfg)
        for role in variants.ROLES:
            champ = variants.champion(s, role)
            assert champ is not None
            s.create_prompt_draft(role=role, prompt_name=f"{role}-v9", prompt_hash="h",
                                  prompt_text="p", rationale="r", patterns="[]",
                                  base_variant=champ.id, model=champ.model, proposer_model="m",
                                  cutoff_at=datetime(2026, 10, 1, tzinfo=UTC),
                                  path=str(paths.home() / f"{role}-v9.md"))  # fmt: skip
    html = _full_client(cfg).get(ui.MANAGE_PATH).text
    assert '<button type="button" disabled>Propose</button>' in html
    assert "waits: approve or discard it" in html


# --------------------------------------------------------------------------- #49 CLI


def _draft(path: Path, **kw: Any) -> Any:
    base = dict(id=4, role="writer", prompt_name="writer-v2", base_variant="m/writer-v1@x",
                proposer_model="claude-opus", path=str(path),
                cutoff_at=datetime(2026, 10, 1, tzinfo=UTC), est_cost_usd=0.0123,
                cost_usd=0.01, rationale="Targets: weak_tests.", prompt_text="P")  # fmt: skip
    return SimpleNamespace(**{**base, **kw})


def test_cli_propose_approve_discard_call_the_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Any]] = []
    monkeypatch.setattr(wx, "propose_command", lambda **kw: calls.append(("propose", kw)))
    monkeypatch.setattr(wx, "approve_draft_command",
                        lambda d, **kw: calls.append(("approve", d)) or 0)  # fmt: skip
    monkeypatch.setattr(wx, "discard_draft_command", lambda d, **kw: calls.append(("discard", d)))
    assert _invoke("experiment", "propose", "--role", "writer", "--proposer", "x").exit_code == 0
    assert _invoke("experiment", "approve-draft", "4").exit_code == 0
    assert _invoke("experiment", "discard-draft", "5").exit_code == 0
    assert calls[0][0] == "propose" and calls[0][1]["role"] == "writer"
    assert calls[0][1]["proposer"] == "x"
    assert calls[1:] == [("approve", 4), ("discard", 5)]

    def refuse(**kw: Any) -> None:
        raise wx.ExperimentError("one at a time")

    monkeypatch.setattr(wx, "propose_command", refuse)
    r = _invoke("experiment", "propose", "--role", "writer")
    assert r.exit_code == 1 and "one at a time" in r.output


def test_propose_command_reads_the_experiments_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = config_mod.from_dict(
        {
            "experiments": {
                "proposer_holdout_days": 7,
                "proposer_lookback_days": 14,
                "proposer_model": "claude-fable",
            }
        }  # fmt: skip
    )
    Store(paths.db_path()).close()
    seen: dict[str, Any] = {}

    def fake_propose(store: Any, config: Any, role: str, **kw: Any) -> Any:
        seen.update(kw, role=role)
        return _draft(paths.home() / "writer-v2.md")

    stopped: list[bool] = []
    monkeypatch.setattr(pp, "propose", fake_propose)
    out: list[str] = []
    wx.propose_command(role="writer", echo=out.append, config=cfg,
                       proxy_factory=lambda c, s, sp: SimpleNamespace(
                           stop=lambda: stopped.append(True)))  # fmt: skip
    assert (seen["holdout_days"], seen["lookback_days"]) == (7, 14)
    assert seen["proposer"] == "claude-fable" and seen["spend"] is not None
    assert stopped == [True]
    text = "\n".join(out)
    assert "writer-v2.md" in text and "estimated cost $0.0123" in text
    assert "writer-v2.rationale.md" in text and "Targets: weak_tests." in text


def test_approve_draft_command_approves_then_runs_its_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Store(paths.db_path()).close()
    seen: dict[str, Any] = {}

    def fake_approve(store: Any, draft_id: int, **kw: Any) -> Any:
        seen["draft"] = draft_id
        seen["mirror_for"] = kw["mirror_for"]
        return pp.Approved(_draft(paths.home() / "w.md"), "m/writer-v2@abcd", Path("x"), 3, 9)

    def fake_run(**kw: Any) -> int:
        seen["run"] = kw
        return 0

    monkeypatch.setattr(pp, "approve_draft", fake_approve)
    monkeypatch.setattr(wx, "run_command", fake_run)
    assert wx.approve_draft_command(4, echo=lambda _l: None) == 0
    assert seen["draft"] == 4 and callable(seen["mirror_for"])
    assert seen["run"]["role"] == "writer" and seen["run"]["challenger"] == "m/writer-v2@abcd"
    assert seen["run"]["all_"] is False


def test_status_shows_the_origin_and_cutoff() -> None:
    cut = datetime(2026, 10, 1, tzinfo=UTC)
    with Store(paths.db_path()) as s:
        x = s.create_experiment(role="writer", champion_variant="a", challenger_variant="b",
                                mode="replay", origin="llm", cutoff_at=cut)  # fmt: skip
        detail = "\n".join(wp.status_lines(s, Config(), x.id))
        listing = "\n".join(wp.status_lines(s, Config()))
    assert "origin     LLM-drafted (approved by you), cutoff 2026-10-01" in detail
    assert "[LLM-drafted (approved by you), cutoff 2026-10-01]" in listing


# --------------------------------------------------------------------------- #49/#78 replay


def test_writer_replay_passes_the_experiment_and_stored_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_drive`` and ``compare`` pass ``experiment_id=`` to ``list_cases`` (the cutoff);
    the writer prompt carries ``images.stored_text`` for the case's ticket."""
    from dataclasses import replace

    from test_replay_sets import World, key_of, writer_pair
    from test_replay_sets import script as replay_script

    w = World(tmp_path)
    try:
        w.merged_ticket("a")
        w.sync()
        champ, chall = writer_pair(w)
        cases_calls: list[Any] = []
        real = rs.list_cases

        def spy(store: Any, set_id: int, **kw: Any) -> Any:
            cases_calls.append(kw.get("experiment_id"))
            return real(store, set_id, **kw)

        monkeypatch.setattr(rs, "list_cases", spy)
        asked: list[int] = []

        def stored(traces_dir: Any, ticket_id: int) -> str:
            asked.append(ticket_id)
            return "IMAGE-DESCRIPTION-MARKER"

        monkeypatch.setattr(images_mod, "stored_text", stored)
        w.driver.choose = lambda prompt, endpoint: replay_script("ok", key_of(prompt))
        deps = replace(w.deps(), traces_dir=tmp_path / "traces")
        c = rr.run_replay(deps, role="writer", champion=champ, challenger=chall)
        assert cases_calls and all(e == c.experiment_id for e in cases_calls)
        assert asked and all("IMAGE-DESCRIPTION-MARKER" in r["prompt"] for r in w.driver.runs)
        built = wx.build_deps(w.store, w.config, proxy=None, spend=None)
        assert built.traces_dir == paths.traces_dir()  # the CLI's runs read them too
    finally:
        w.close()


def test_vision_is_a_model_key() -> None:
    raw = config_mod.to_dict(Config())
    raw["models"][0]["vision"] = True
    cfg = config_mod.from_dict(raw)
    assert cfg.models[0].vision is True and cfg.models[1].vision is False
    assert config_mod.to_dict(cfg)["models"][0]["vision"] is True
    assert "vision" not in config_mod.to_dict(cfg)["models"][1]


# --------------------------------------------------------------------------- #18 fork


def test_cli_replay_fork_calls_fork_command(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    monkeypatch.setattr(wx, "fork_command",
                        lambda a, **kw: seen.append((a, kw["model"], kw["prompt"], kw["step"]))
                        or 2)  # fmt: skip
    r = _invoke("replay", "--fork", "15", "--model", "glm", "--step", "3")
    assert r.exit_code == 2 and seen == [(15, "glm", None, 3)]
    assert _invoke("replay", "--model", "glm").exit_code == 2  # --model needs --fork


def test_fork_command_uses_the_fork_config(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Config(experiments=ExperimentSettings(fork_budget_usd=0.25, fork_writer_timeout_s=99))
    Store(paths.db_path()).close()
    seen: dict[str, Any] = {}

    def fake_fork(deps: Any, attempt_id: int, **kw: Any) -> Any:
        seen.update(kw, attempt=attempt_id, driver=deps.driver)
        raise fork_mod.ForkError("no labelled failure step")

    monkeypatch.setattr(fork_mod, "fork_attempt", fake_fork)
    out: list[str] = []
    code = wx.fork_command(
        7, model="glm", prompt="writer-v1", step=None, echo=out.append, config=cfg,
        proxy_factory=lambda c, s, sp: SimpleNamespace(stop=lambda: None),
        driver_factory=lambda proxy: "DRIVER",
    )  # fmt: skip
    assert code == 2 and "no labelled failure step" in out[-1]
    assert seen["attempt"] == 7 and seen["model"] == "glm" and seen["driver"] == "DRIVER"
    assert (seen["settings"].budget_usd, seen["settings"].writer_timeout_s) == (0.25, 99.0)


def test_fork_recordings_export_and_anchors() -> None:
    assert export._RECORDING.fullmatch("5.fork_12.jsonl.gz")
    assert export._SIDE_DIR.fullmatch("5.fork_12.files")

    def rec(role: str, attempt: int) -> Any:
        return SimpleNamespace(role=role, attempt_id=attempt, model="m", tier=1, trigger=None,
                               outcome="passed", partial=False, tokens_in=0, tokens_out=0,
                               cost_usd=0, failure_steps=(), steps=[], variant_id=None)  # fmt: skip

    ids = {re.search(r'id="([^"]+)"', trace_view._recording(r)).group(1)  # type: ignore[union-attr]
           for r in (rec("fork_1", 15), rec("fork_11", 5), rec("attempt", 115))}  # fmt: skip
    assert len(ids) == 3 and "a115" in ids


# --------------------------------------------------------------------------- #19 classifier


@pytest.mark.parametrize("on", [True, False])
def test_classifier_keys_reach_the_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, on: bool
) -> None:
    e = Env(tmp_path, classifier_router=on, classifier_min_tickets=7)
    try:
        knn_route = SimpleNamespace(tier=1)
        monkeypatch.setattr(knn, "route_knn", lambda t, **kw: knn_route)
        seen: list[int] = []
        monkeypatch.setattr(classifier, "route_classifier",
                            lambda t, **kw: seen.append(kw["min_tickets"]))  # fmt: skip
        e.label()
        e.worker.poll_and_handle()
        assert e.worker._knn_route(e.ticket()) is knn_route
        assert seen == ([7] if on else [])
    finally:
        e.close()
    loaded = config_mod.from_dict({"classifier_router": False, "classifier_min_tickets": 9})
    assert (loaded.classifier_router, loaded.classifier_min_tickets) == (False, 9)


# --------------------------------------------------------------------------- #15 relay


def test_serve_forever_starts_the_relay_and_a_nudge_polls_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = Env(tmp_path, relay_url="https://smee.io/chan")
    started: list[tuple[Any, ...]] = []
    stopped: list[float] = []
    fake = SimpleNamespace(stop=lambda timeout=5.0: stopped.append(timeout))

    def fake_start(url: Any, ref: Any, nudge: Any, *, repos: Any = ()) -> Any:
        started.append((url, ref, nudge, tuple(repos)))
        return fake

    monkeypatch.setattr(relay_mod, "start_relay", fake_start)
    polls = threading.Semaphore(0)
    w = e.worker
    monkeypatch.setattr(w, "poll_and_handle", lambda: polls.release())
    monkeypatch.setattr(w, "dispatch", lambda inline: None)
    th = threading.Thread(target=w.serve_forever, daemon=True)
    th.start()
    try:
        assert polls.acquire(timeout=5)  # the first poll
        assert not polls.acquire(timeout=1.5)  # the clock stands still: no second poll
        assert started and started[0][0] == "https://smee.io/chan"
        assert started[0][1] == "keychain:foreman-inator/relay-webhook-secret"
        assert started[0][3] == (REPO,)
        started[0][2](REPO)  # a verified delivery
        assert polls.acquire(timeout=5)  # polled at once
    finally:
        w.request_stop("test")
        th.join(10)
        e.close()
    assert stopped and not th.is_alive()


def test_setup_relay_url_registers_the_hook_and_stores_the_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(config_mod.keyring, "set_password",
                        lambda svc, name, value: keys.__setitem__((svc, name), value))  # fmt: skip
    servers: list[dict[str, Any]] = []

    class Stop(Exception):
        pass

    def fake_server(**kw: Any) -> Any:
        servers.append(kw)
        raise Stop

    monkeypatch.setattr(setup_mod.manifest, "CallbackServer", fake_server)
    with pytest.raises(Stop):
        setup_mod.register_app(opener=lambda u: None, echo=lambda m: None,
                               relay_url="https://smee.io/c")  # fmt: skip
    assert servers[0]["relay_url"] == "https://smee.io/c"

    regs: list[Any] = []
    creds = SimpleNamespace(pem="PEM", webhook_secret="s3cret", app_id=11, slug="fi-x",
                            permissions={})  # fmt: skip

    def fake_register(**kw: Any) -> Any:
        regs.append(kw.get("relay_url"))
        return creds

    monkeypatch.setattr(setup_mod, "register_app", fake_register)
    monkeypatch.setattr(setup_mod.manifest, "apply_app",
                        lambda cfg, c, users: config_mod.replace(cfg, app_id=c.app_id))  # fmt: skip

    def no_auth(*a: Any, **kw: Any) -> Any:
        raise Stop

    monkeypatch.setattr(setup_mod, "AppAuth", no_auth)
    with pytest.raises(Stop):
        setup_mod.run_setup(echo=lambda m: None, relay_url="https://smee.io/c")
    assert regs == ["https://smee.io/c"]
    saved = config_mod.load()
    assert saved.relay_url == "https://smee.io/c"
    assert saved.relay_secret_ref == "keychain:foreman-inator/relay-webhook-secret"
    assert keys[("foreman-inator", relay_mod.DEFAULT_SECRET_NAME)] == "s3cret"
    assert "relay.md" in (Path(__file__).parents[1] / "docs" / "setup.md").read_text()


def test_setup_refuses_a_plain_http_relay() -> None:
    r = _invoke("setup", "--relay-url", "http://example.com/x", "--no-service")
    assert r.exit_code == 2


# --------------------------------------------------------------------------- #73 retry


def test_policy_for_applies_the_retry_mark(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.label()
    env.worker.poll_and_handle()
    t = env.ticket()
    marker = object()
    seen: list[Any] = []

    def fake(store: Any, ticket: Any, history: Any, step: Any) -> Any:
        seen.append((ticket.id, step))
        return marker

    monkeypatch.setattr(retry_mod, "with_manual_retry", fake)
    assert env.worker.policy_for(t) is marker
    assert seen and seen[0][0] == t.id


@pytest.mark.parametrize(
    ("args", "tier", "ok", "code"),
    [
        ((), None, True, 0),
        (("--up",), "up", True, 0),
        (("--tier", "3"), 3, True, 0),
        ((), None, False, 1),
    ],
)
def test_cli_retry_calls_retry(
    monkeypatch: pytest.MonkeyPatch, args: tuple[str, ...], tier: Any, ok: bool, code: int
) -> None:
    Store(paths.db_path()).close()
    seen: list[Any] = []

    def fake(store: Any, cfg: Any, ref: Any, *, tier: Any, now: datetime, api: Any) -> Any:
        seen.append((ref, tier, api))
        assert abs(now - datetime.now(UTC)) < timedelta(minutes=1)
        status = "queued" if ok else "refused"
        return retry_mod.RetryResult(status, "the reason", 1)  # type: ignore[arg-type]

    monkeypatch.setattr(retry_mod, "retry", fake)
    r = _invoke("retry", f"{REPO}#3", *args)
    assert r.exit_code == code and "the reason" in r.output
    assert seen == [(f"{REPO}#3", tier, None)]  # no App configured: no GitHub client
