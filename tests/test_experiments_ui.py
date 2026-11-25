"""Start, stop, cancel and promote experiments from the metrics page (todo #67, GH #44)."""

from __future__ import annotations

import functools
import re
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from foreman_inator import paths, prompts
from foreman_inator import worker_experiments as wx
from foreman_inator.config import Config, ExperimentSettings, ModelEndpoint, key_ref
from foreman_inator.db.store import Store
from foreman_inator.experiments import replay_runner as rr
from foreman_inator.experiments import variants
from foreman_inator.metrics import experiments_ui as ui
from foreman_inator.metrics import forms
from foreman_inator.metrics.web import LoopbackHostOnly

BASE = "http://127.0.0.1:8642"
TOKEN = "page-token"
LOCAL_URL = "http://127.0.0.1:11434/v1"


def make_config() -> Config:
    return Config(
        app_id=1,
        allowed_users=("brooks",),
        repos=("acme/widgets",),
        models=(
            ModelEndpoint("w-one", 1, "local", "w-one-up", LOCAL_URL, "fam-w"),
            ModelEndpoint("w-alt", 1, "local", "w-alt-up", LOCAL_URL, "fam-w"),
            ModelEndpoint("w-two", 2, "local", "w-two-up", LOCAL_URL, "fam-w2"),
            ModelEndpoint(
                "rev-x", 3, "cloud", "rev-x-up", "https://x.example.invalid/v1", "fam-x",
                key_ref("x"),
            ),
        ),
        reviewer_models=("rev-x",),
        experiments=ExperimentSettings(min_cases=2, live_min_sample=2),
    )  # fmt: skip


def vid(model: str) -> str:
    return variants.variant_id(model, prompts.load("writer-v1"))


class FakeProxy:
    """Enough of ``ModelProxy`` for a replay with no cases (no socket, no model)."""

    spend_hook = None
    on_usage = None

    def stop(self) -> None:
        pass


def cli_like(cfg: Config) -> Any:
    """``run_command`` with the test's config and no proxy or OpenCode."""
    return functools.partial(
        wx.run_command, config=cfg, proxy_factory=lambda c, s, sp: FakeProxy(),
        driver_factory=lambda: None,
    )  # fmt: skip


def new_db(cfg: Config) -> Path:
    db = paths.db_path()
    with Store(db) as s:
        variants.ensure_defaults(s, cfg)
    return db


@pytest.fixture
def cfg() -> Config:
    return make_config()


@pytest.fixture
def db(cfg: Config) -> Path:
    return new_db(cfg)


def make_client(db: Path, cfg: Config, jobs: ui.Jobs) -> TestClient:
    routes = ui.routes(db, load_config=lambda: cfg, guard=forms.FormGuard(TOKEN), jobs=jobs)
    return TestClient(LoopbackHostOnly(Starlette(routes=routes)), base_url=BASE)


def post(c: TestClient, path: str, data: dict[str, str] | None = None, **kw: Any) -> Any:
    data = {"csrf_token": TOKEN, **(data or {})}
    headers = {"origin": BASE, **kw.pop("headers", {})}
    return c.post(path, data=data, headers=headers, **kw)


class Blocking:
    """A runner that waits until released or cancelled (a long replay)."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kw: Any) -> int:
        self.calls.append(kw)
        self.started.set()
        while not (self.release.is_set() or kw["cancel"].is_set()):
            self.release.wait(0.01)
        return 0


def row(store: Store) -> tuple[Any, ...]:
    (x,) = store.list_experiments()
    return (x.role, x.champion_variant, x.challenger_variant, x.mode, x.status, x.result_note)


# =========================================================================== start


def test_a_form_started_run_gives_the_same_experiments_row_as_the_cli(
    cfg: Config, db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = ui.Jobs(run=lambda **kw: cli_like(cfg)(**kw))
    c = make_client(db, cfg, jobs)
    r = post(c, "/experiments/start", {"role": "writer", "model": "w-alt", "prompt": "writer-v1"})
    assert r.status_code == 200, r.text
    assert "Started run #1" in r.text
    jobs.wait(30)
    (job,) = jobs.list()
    assert job.state == "finished", list(job.lines)
    with Store(db, read_only=True) as s:
        from_page = row(s)

    # The CLI: `fman experiment run --role writer --challenger w-alt/writer-v1`, own home.
    monkeypatch.setenv("FOREMAN_INATOR_HOME", str(tmp_path / "cli-home"))
    cli_db = new_db(cfg)
    assert cli_db != db
    cli_like(cfg)(role="writer", challenger="w-alt/writer-v1", all_=False, echo=lambda s: None)
    with Store(cli_db, read_only=True) as s:
        assert row(s) == from_page
    assert from_page[2] == vid("w-alt") and from_page[3] == "replay"


def test_start_runs_in_the_background_and_one_at_a_time(cfg: Config, db: Path) -> None:
    runner = Blocking()
    jobs = ui.Jobs(run=runner)
    c = make_client(db, cfg, jobs)
    try:
        r = post(c, "/experiments/start", {"role": "writer", "model": "w-alt", "prompt": ""})
        assert r.status_code == 200  # answered while the run is still going
        assert runner.started.wait(5) and jobs.running() is not None
        (kw,) = runner.calls
        assert (kw["role"], kw["challenger"], kw["all_"]) == ("writer", "w-alt", False)
        again = post(c, "/experiments/start", {"role": "writer", "model": "w-alt"})
        assert again.status_code == 409 and "still running" in again.text
        assert len(runner.calls) == 1
        page = c.get(ui.MANAGE_PATH).text
        assert 'action="/experiments/jobs/1/stop"' in page
    finally:
        runner.release.set()
        jobs.wait(5)
    assert jobs.list()[0].state == "finished"


def test_stop_sets_the_runs_cancel_event(cfg: Config, db: Path) -> None:
    runner = Blocking()
    jobs = ui.Jobs(run=runner)
    c = make_client(db, cfg, jobs)
    post(c, "/experiments/start", {"role": "writer", "model": "w-alt"})
    assert runner.started.wait(5)
    assert post(c, "/experiments/jobs/1/stop", headers={"origin": "http://evil.example"}
                ).status_code == 403  # fmt: skip
    assert jobs.running() is not None
    assert post(c, "/experiments/jobs/1/stop").status_code == 200
    jobs.wait(5)
    assert jobs.list()[0].state == "stopped"


def test_run_command_stops_on_the_given_cancel_event(cfg: Config, db: Path) -> None:
    cancel = threading.Event()
    cancel.set()
    out: list[str] = []
    cli_like(cfg)(role="writer", challenger="w-alt", all_=False, echo=out.append, cancel=cancel)
    assert any(line.startswith("Stopped;") for line in out)


@pytest.mark.parametrize(
    ("data", "why"),
    [
        ({"role": "boss", "model": "w-alt"}, "role must be"),
        ({"role": "writer", "model": "nope"}, "unknown model"),
        ({"role": "writer", "model": "w-alt", "prompt": "reviewer-v1"}, "not a writer prompt"),
        ({"role": "writer", "model": "w-one", "prompt": "writer-v1"}, "is the writer champion"),
    ],
)
def test_start_refuses_bad_choices_and_starts_nothing(cfg, db, data, why) -> None:
    runner = Blocking()
    jobs = ui.Jobs(run=runner)
    r = post(make_client(db, cfg, jobs), "/experiments/start", data)
    assert r.status_code == 400 and why in r.text
    assert jobs.list() == [] and runner.calls == []


def test_start_without_the_token_or_from_another_origin_starts_nothing(cfg, db) -> None:
    runner = Blocking()
    jobs = ui.Jobs(run=runner)
    c = make_client(db, cfg, jobs)
    form = {"role": "writer", "model": "w-alt"}
    assert c.post("/experiments/start", data=form, headers={"origin": BASE}).status_code == 403
    evil = post(c, "/experiments/start", form, headers={"origin": "http://evil.example"})
    assert evil.status_code == 403
    assert jobs.list() == [] and runner.calls == []
    with Store(db, read_only=True) as s:
        assert s.list_experiments() == []


def test_start_is_refused_while_a_cli_run_holds_the_lock(cfg: Config, db: Path) -> None:
    runner = Blocking()
    jobs = ui.Jobs(run=runner)
    with wx.run_lock():
        r = post(
            make_client(db, cfg, jobs), "/experiments/start", {"role": "writer", "model": "w-alt"}
        )
    assert r.status_code == 409 and "another `fman experiment run`" in r.text
    assert runner.calls == []


# =========================================================================== page


def test_the_page_renders_forms_with_the_token_and_escapes_the_database(cfg, db) -> None:
    with Store(db) as s:
        s.create_experiment(
            role="writer", champion_variant=vid("w-one"),
            challenger_variant="<script>alert(1)</script>", mode="replay",
        )  # fmt: skip
    page = make_client(db, cfg, ui.Jobs(run=Blocking())).get(ui.MANAGE_PATH)
    assert page.status_code == 200
    assert "<script>alert" not in page.text and "&lt;script&gt;" in page.text
    assert page.text.count(f'name="csrf_token" value="{TOKEN}"') >= 2  # start + cancel
    assert 'action="/experiments/start"' in page.text
    assert '<option value="w-alt">' in page.text and '<option value="writer-v1">' in page.text


def test_gets_change_nothing(cfg: Config, db: Path) -> None:
    with Store(db) as s:
        exp = s.create_experiment(
            role="writer", champion_variant=vid("w-one"), challenger_variant=vid("w-alt"),
            mode="replay",
        )  # fmt: skip
    c = make_client(db, cfg, ui.Jobs(run=Blocking()))
    for path in (f"/experiments/{exp.id}/cancel", "/experiments/start"):
        assert c.get(path).status_code == 405
    assert c.get(f"/experiments/{exp.id}/promote").status_code == 200
    with Store(db, read_only=True) as s:
        assert s.get_experiment(exp.id).status == "running"
        assert variants.champion(s, "writer").id == vid("w-one")


# =========================================================================== cancel


def _exp(db: Path, mode: str = "replay", role: str = "writer") -> int:
    with Store(db) as s:
        champ = variants.champion(s, role).id
        return s.create_experiment(
            role=role, champion_variant=champ, challenger_variant=vid("w-alt"), mode=mode
        ).id


def test_cancel_ends_a_running_experiment_as_rejected(cfg: Config, db: Path) -> None:
    exp_id = _exp(db)
    c = make_client(db, cfg, ui.Jobs(run=Blocking()))
    assert c.post(f"/experiments/{exp_id}/cancel", data={}, headers={"origin": BASE}
                  ).status_code == 403  # fmt: skip
    with Store(db, read_only=True) as s:
        assert s.get_experiment(exp_id).status == "running"
    r = post(c, f"/experiments/{exp_id}/cancel")
    assert r.status_code == 200, r.text
    with Store(db, read_only=True) as s:
        x = s.get_experiment(exp_id)
    assert x.status == "rejected" and wx.CANCEL_NOTE in (x.result_note or "")
    assert post(c, f"/experiments/{exp_id}/cancel").status_code == 409  # not running now


def test_cancel_of_a_replay_is_refused_while_a_run_holds_the_lock(cfg, db) -> None:
    exp_id = _exp(db)
    with wx.run_lock():
        r = post(make_client(db, cfg, ui.Jobs(run=Blocking())), f"/experiments/{exp_id}/cancel")
    assert r.status_code == 409 and "stop it first" in r.text
    with Store(db, read_only=True) as s:
        assert s.get_experiment(exp_id).status == "running"


def test_cancel_experiment_ends_a_split_with_its_comparison(cfg: Config, db: Path) -> None:
    exp_id = _exp(db, mode="split")
    with Store(db) as s:
        x = wx.cancel_experiment(s, exp_id)
    assert x.status == "rejected" and (x.result_note or "").startswith(wx.CANCEL_NOTE + "\n")


# =========================================================================== promote


@pytest.fixture
def wins(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_compare(store: Store, experiment_id: int, *, min_cases: int = 10) -> Any:
        return SimpleNamespace(
            state="finished", verdict="wins", reason="2 of 2", wins=2, losses=0, ties=0
        )

    monkeypatch.setattr(rr, "compare", fake_compare)


def _champion(db: Path) -> str:
    with Store(db, read_only=True) as s:
        return variants.champion(s, "writer").id


def test_promote_shows_the_comparison_and_needs_the_confirm_step(cfg, db, wins) -> None:
    with Store(db) as s:
        variants.register(s, "writer", "w-alt", prompts.load("writer-v1"))
    exp_id = _exp(db)
    c = make_client(db, cfg, ui.Jobs(run=Blocking()))
    page = c.get(f"/experiments/{exp_id}/promote")
    assert page.status_code == 200
    assert f"Experiment #{exp_id}: writer replay, running" in page.text  # status_lines
    assert 'name="confirm" value="yes"' in page.text
    champ = re.search(r'name="champion" value="([^"]*)"', page.text).group(1)
    assert champ == vid("w-one")

    no_confirm = post(c, f"/experiments/{exp_id}/promote", {"champion": champ, "replay_only": "1"})
    assert no_confirm.status_code == 400 and "confirm on this page first" in no_confirm.text
    other = "x/writer-v1@00000000"
    stale = post(c, f"/experiments/{exp_id}/promote",
                 {"confirm": "yes", "champion": other, "replay_only": "1"})  # fmt: skip
    assert stale.status_code == 400 and "champion changed" in stale.text
    forged = c.post(f"/experiments/{exp_id}/promote", headers={"origin": "http://evil.example"},
                    data={"csrf_token": TOKEN, "confirm": "yes", "champion": champ,
                          "replay_only": "1"})  # fmt: skip
    assert forged.status_code == 403
    assert _champion(db) == vid("w-one")  # nothing changed so far

    refused = post(c, f"/experiments/{exp_id}/promote", {"confirm": "yes", "champion": champ})
    assert refused.status_code == 409 and "REFUSED" in refused.text  # needs a live check
    assert _champion(db) == vid("w-one")

    ok = post(c, f"/experiments/{exp_id}/promote",
              {"confirm": "yes", "champion": champ, "replay_only": "1"})  # fmt: skip
    assert ok.status_code == 200 and "PROMOTED" in ok.text, ok.text
    assert _champion(db) == vid("w-alt")
    with Store(db, read_only=True) as s:
        assert s.get_experiment(exp_id).status == "promoted"
