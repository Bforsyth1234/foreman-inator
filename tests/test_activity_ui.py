"""The Activity page (todo #76, GH #65) and its Retry button (todo #79, GH #73)."""

from __future__ import annotations

import html
import json
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from foreman_inator import retry as rt
from foreman_inator.config import Config
from foreman_inator.db.store import Store, Ticket
from foreman_inator.metrics import activity_ui as ui
from foreman_inator.metrics import forms
from foreman_inator.metrics.web import LoopbackHostOnly
from foreman_inator.resources import snapshot as res
from foreman_inator.worker_status import render_status

BASE = "http://127.0.0.1:8642"
TOKEN = "page-token"
T0 = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
CFG = Config()  # default ladder: inkling (1, local), glm (2, cloud), opus, fable (3, cloud)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "fman.db"
    Store(path, clock=lambda: T0).close()
    return path


def open_store(db: Path) -> Store:
    return Store(db, clock=lambda: T0)


def ticket(s: Store, n: int, title: str = "t", column: str = "in_progress") -> Ticket:
    t = s.create_ticket(repo="me/toy", issue_number=n, title=title, request="r",
                        base_branch="main", routed_tier=1)  # fmt: skip
    s.set_column(t.id, column)  # type: ignore[arg-type]
    return s.get_ticket(t.id)


def flagged(s: Store, n: int, flag: str = "checks_failed: the checks failed 2 times") -> Ticket:
    t = ticket(s, n)
    for i in range(2):
        a = s.add_attempt(t.id, model="inkling-2-mini", tier=1,
                          trigger="first" if i == 0 else "escalation")  # fmt: skip
        s.finish_attempt(a.id, "failed_checks")
    return s.flag_ticket(t.id, flag)


def client(db: Path, tmp_path: Path, **kw: Any) -> TestClient:
    routes = ui.routes(
        db, load_config=lambda: CFG, guard=forms.FormGuard(TOKEN),
        resources_path=kw.pop("resources_path", tmp_path / "none.json"),
        now=lambda: T0 + timedelta(minutes=5), **kw,
    )  # fmt: skip
    return TestClient(LoopbackHostOnly(Starlette(routes=routes)), base_url=BASE)


def post(c: TestClient, data: dict[str, str], *, token: str | None = TOKEN) -> Any:
    body = {**({"csrf_token": token} if token else {}), **data}
    return c.post(ui.RETRY_PATH, data=body, headers={"origin": BASE})


def running_rows(text: str) -> str:
    m = re.search(r'<table class="running">.*?</table>', text, re.S)
    return m.group(0) if m else ""


# =========================================================================== the page


def test_running_attempts_show_model_step_elapsed_tokens_cost_and_trace(db, tmp_path):
    with open_store(db) as s:
        t = ticket(s, 7, "Fix the widget")
        a = s.add_attempt(t.id, model="glm", tier=2, trigger="first")
        s.add_attempt_usage(a.id, tokens_in=1200, tokens_out=300, cost_usd=0.42)
        done = ticket(s, 8, "Already finished")
        b = s.add_attempt(done.id, model="inkling-2-mini", tier=1, trigger="first")
        s.finish_attempt(b.id, "failed_checks")
    r = client(db, tmp_path).get("/activity")
    assert r.status_code == 200
    rows = running_rows(r.text)
    assert "glm" in rows and "<td>2</td>" in rows and "writing" in rows
    assert "5m of 20m" in rows  # elapsed against writer_timeout_seconds (1200 s)
    assert "1,500" in rows and "$0.42" in rows
    assert f'href="/traces/{t.id}/{a.id}"' in rows
    assert 'href="https://github.com/me/toy/issues/7"' in rows
    assert "Already finished" not in rows  # a finished attempt is not running

    with open_store(db) as s:  # the attempt ends: it leaves the running list
        s.finish_attempt(a.id, "failed_checks")
    rows = running_rows(client(db, tmp_path).get("/activity").text)
    assert rows == ""


def test_steps_verify_publish_ci_review(db, tmp_path):
    traces = tmp_path / "traces"
    with open_store(db) as s:
        t1 = ticket(s, 1)
        a1 = s.add_attempt(t1.id, model="glm", tier=2, trigger="first")
        t2 = ticket(s, 2)
        a2 = s.add_attempt(t2.id, model="glm", tier=2, trigger="first")
        s.finish_attempt(a2.id, None, tests_passed=True)
        t3 = ticket(s, 3, column="testing")
        a3 = s.add_attempt(t3.id, model="glm", tier=2, trigger="first")
        s.finish_attempt(a3.id, None, tests_passed=True)
        t4 = ticket(s, 4, column="testing")
        a4 = s.add_attempt(t4.id, model="glm", tier=2, trigger="first")
        s.finish_attempt(a4.id, None, tests_passed=True)
        s.set_ci_result(a4.id, "passed")
        assert ui.step_of(s, t1, s.list_attempts(t1.id), traces) == "writing"
        (traces / str(t1.id)).mkdir(parents=True)
        (traces / str(t1.id) / f"attempt-{a1.id}-verify.log").write_text("$ pytest\n")
        assert ui.step_of(s, t1, s.list_attempts(t1.id), traces) == "verify"
        assert ui.step_of(s, t2, s.list_attempts(t2.id), traces) == "publish"
        assert ui.step_of(s, t3, s.list_attempts(t3.id), traces) == "CI"
        assert ui.step_of(s, t4, s.list_attempts(t4.id), traces) == "review"
        assert [r.step for r in ui.running(s, now=T0, traces_dir=traces)] == [
            "verify", "publish", "CI", "review",
        ]  # fmt: skip


def test_ticket_states_match_render_status(db, tmp_path):
    with open_store(db) as s:
        ticket(s, 1, column="todo")
        ticket(s, 2)
        flagged(s, 3)
        ticket(s, 4, column="done")
        s.flag_ticket(ticket(s, 5).id, "spend_cap: the daily cap is reached")
        status = render_status(s, config=CFG)
    text = client(db, tmp_path).get("/activity").text
    headers = [line for line in status.splitlines() if re.match(r"^[A-Z ]+(\(.*\))? \(\d+\)", line)]
    assert len(headers) == 5
    for h in headers:
        assert f">{html.escape(h)}</h3>" in text, h
    summary = next(line for line in status.splitlines() if "ticket(s)," in line)
    assert summary in text  # "5 ticket(s), 1 flagged for you, 1 waiting."
    assert "FLAGGED: checks_failed: the checks failed 2 times" in text
    assert "WAITING: spend approval (spend_cap: the daily cap is reached)" in text


def test_issue_titles_and_flags_are_escaped(db, tmp_path):
    with open_store(db) as s:
        t = ticket(s, 9, "<script>alert(1)</script> & co")
        s.add_attempt(t.id, model="glm", tier=2, trigger="first")
        flagged(s, 10, "checks_failed: <img src=x onerror=alert(2)>")
    text = client(db, tmp_path).get("/activity").text
    assert "<script>" not in text and "<img" not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; co" in text
    assert "&lt;img src=x onerror=alert(2)&gt;" in text


def test_meta_refresh_and_no_javascript(db, tmp_path):
    text = client(db, tmp_path).get("/activity").text
    head = text.split("</head>", 1)[0]
    assert f'<meta http-equiv="refresh" content="{ui.REFRESH_SECONDS}; url=/activity">' in head
    assert "<script" not in text.lower()


def test_loaded_models_and_memory_from_the_snapshot(db, tmp_path):
    snap = tmp_path / "resources.json"
    snap.write_text(json.dumps({
        "version": res.VERSION, "written_at": time.time(),
        "ollama": {"host": "http://127.0.0.1:11434",
                   "models": [{"name": "qwen<b>", "size": 21_300_000_000}]},
        "memory": {"used_bytes": 30e9, "total_bytes": 64e9, "free_pct": 53, "pressure": "normal"},
    }))  # fmt: skip
    text = client(db, tmp_path, resources_path=snap).get("/activity").text
    assert "Ollama: 1 loaded (21.3 GB)" in text
    assert "qwen&lt;b&gt; (21.3 GB)" in text
    assert "Memory: 30.0 GB of 64.0 GB used" in text


def test_no_database_yet(tmp_path):
    r = client(tmp_path / "missing.db", tmp_path).get("/activity")
    assert r.status_code == 503 and "run the worker first" in r.text


# =========================================================================== retry


def test_flagged_ticket_has_retry_forms_with_the_token(db, tmp_path):
    with open_store(db) as s:
        t = flagged(s, 3)
    text = client(db, tmp_path).get("/activity").text
    forms_html = re.findall(r'<form method="post" action="/activity/retry".*?</form>', text)
    assert len(forms_html) == 2
    for f in forms_html:
        assert f'name="csrf_token" value="{TOKEN}"' in f
        assert f'name="ticket" value="{t.id}"' in f
    assert "Retry (same tier)" in text and "Retry (move up a tier)" in text


def test_retry_queues_once_and_keeps_history(db, tmp_path):
    with open_store(db) as s:
        t = flagged(s, 3)
    c = client(db, tmp_path)
    r = post(c, {"ticket": str(t.id), "tier": "same"})
    assert r.status_code == 200, r.text
    assert "unflagged and queued" in r.text and "tier 1" in r.text
    again = post(c, {"ticket": str(t.id), "tier": "same"})  # a double submit
    assert again.status_code == 200 and "already queued" in again.text
    with open_store(db) as s:
        after = s.get_ticket(t.id)
        assert after.flag_reason is None and after.column == "in_progress"
        assert len(s.list_attempts(t.id)) == 2
        assert len(rt.retry_marks(s, after)) == 1


def test_retry_up_moves_one_tier_higher(db, tmp_path):
    with open_store(db) as s:
        t = flagged(s, 3)
    r = post(client(db, tmp_path), {"ticket": str(t.id), "tier": "up"})
    assert r.status_code == 200 and "tier 2 (glm)" in r.text
    with open_store(db) as s:
        (mark,) = rt.retry_marks(s, s.get_ticket(t.id))
        assert (mark.tier, mark.model, mark.moved) == (2, "glm", True)


def test_the_button_calls_retry_like_the_cli(db, tmp_path, monkeypatch):
    seen: list[dict[str, Any]] = []

    def fake(store, config, ref, *, tier, now, api=None, spend=None):
        seen.append({"config": config, "ref": ref, "tier": tier, "now": now, "api": api})
        return rt.RetryResult("queued", "ok", ref)

    monkeypatch.setattr(rt, "retry", fake)
    api = object()
    c = client(db, tmp_path, api_factory=lambda cfg: api)
    post(c, {"ticket": "5", "tier": "same"})
    post(c, {"ticket": "5", "tier": "up"})
    assert [(x["ref"], x["tier"]) for x in seen] == [(5, None), (5, "up")]
    assert all(x["config"] is CFG and x["api"] is api for x in seen)
    assert seen[0]["now"] == T0 + timedelta(minutes=5)


def test_a_refused_retry_shows_why_and_changes_nothing(db, tmp_path):
    with open_store(db) as s:
        t = flagged(s, 3, "no_verify_command: no verify command is set for me/toy")
    r = post(client(db, tmp_path), {"ticket": str(t.id), "tier": "same"})
    assert r.status_code == 409
    assert "Retry refused: a retry does not lift the no_verify_command flag" in r.text
    with open_store(db) as s:
        after = s.get_ticket(t.id)
        assert after.flag_reason == t.flag_reason and not rt.retry_marks(s, after)


def test_a_post_without_the_token_is_403_and_changes_nothing(db, tmp_path):
    with open_store(db) as s:
        t = flagged(s, 3)
    c = client(db, tmp_path)
    assert post(c, {"ticket": str(t.id), "tier": "same"}, token=None).status_code == 403
    assert post(c, {"ticket": str(t.id), "tier": "same"}, token="wrong").status_code == 403
    cross = c.post(ui.RETRY_PATH, data={"csrf_token": TOKEN, "ticket": str(t.id)},
                   headers={"origin": "https://evil.example"})  # fmt: skip
    assert cross.status_code == 403
    with open_store(db) as s:
        assert s.get_ticket(t.id).flag_reason == t.flag_reason


def test_bad_fields_are_400(db, tmp_path):
    c = client(db, tmp_path)
    assert post(c, {"ticket": "abc", "tier": "same"}).status_code == 400
    assert post(c, {"ticket": "1", "tier": "sideways"}).status_code == 400
