"""Wave 21 wiring (#71/#75 chat, design section 14 "Wave C4" folded in; #82 / GH #80).

Each slot's piece is reachable from where the user meets it: ``fman chat`` is a
registered command; ``fman metrics`` (``web.full_app``) mounts ``/chat`` with one lazily
built ``ChatManager`` (``chat_cli.default_manager``) that the app's shutdown ends, adds
"Chat" to the nav and shows the tickets · chat · experiments spend block with the
config's chat sub-cap; ``fman status`` carries the chat figure and a carried approval's
note; the worker's startup recovery carries cut-short approvals. The last test drives
the whole page end to end: the real engine from ``test_chat_engine``'s harness (real git,
proxy over the fake model transport, spend) behind ``full_app``: start, send, the reply
over SSE, then a draft PR on the fake GitHub.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from fakes.worker_env import REPO as ENV_REPO
from fakes.worker_env import Env
from foreman_inator import chat_cli, paths
from foreman_inator.cli import app
from foreman_inator.config import Config, ModelEndpoint, SpendCaps
from foreman_inator.db.store import Store
from foreman_inator.metrics import chat_page, forms, web
from foreman_inator.proxy.server import CallContext
from foreman_inator.spend import Spend
from foreman_inator.worker_status import render_status

BASE = "http://127.0.0.1:8642"
REPO = "acme/widgets"
CLOUD = ModelEndpoint("claude", 3, "cloud", "claude-x", "https://api.anthropic.com/v1", "claude")


def _cfg(**caps: float) -> Config:
    base = dict(daily_usd=5.0, monthly_usd=50.0, experiments_monthly_usd=10.0)
    return Config(repos=(REPO,), spend_caps=SpendCaps(**(base | caps)))


class FakeManager:
    """Enough of ``ChatManager`` for ``GET /chat``; records its build and shutdown."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.start_tier = 1
        self.shutdowns = 0

    def shutdown(self) -> None:
        self.shutdowns += 1


# --------------------------------------------------------------------------- cli


def test_fman_chat_is_registered() -> None:
    res = CliRunner().invoke(app, ["chat", "--help"])
    assert res.exit_code == 0, res.output
    for sub in ("list", "resume", "pr", "discard"):
        assert sub in res.output


def test_fman_chat_list_reaches_the_chat_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    @contextlib.contextmanager
    def factory() -> Iterator[Any]:
        seen.append("built")
        yield SimpleNamespace(sessions=lambda: [])

    monkeypatch.setattr(chat_cli, "manager_factory", factory)
    res = CliRunner().invoke(app, ["chat", "list"])
    assert res.exit_code == 0, res.output
    assert seen == ["built"]


# --------------------------------------------------------------------------- full_app


def test_full_app_mounts_chat_with_the_default_builder_and_shuts_it_down(
    foreman_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = paths.db_path()
    Store(db).close()
    cfg = _cfg()
    built: list[Path] = []
    managers: list[FakeManager] = []

    @contextlib.contextmanager
    def default_manager(db_path: Any = None) -> Iterator[Any]:
        built.append(Path(db_path))
        m = FakeManager(cfg)
        managers.append(m)
        try:
            yield m
        finally:
            m.shutdown()

    monkeypatch.setattr(chat_cli, "default_manager", default_manager)
    a = web.full_app(db, recordings_dir=paths.recordings_dir(), traces_dir=None, config=cfg)
    with TestClient(a, base_url=BASE) as c:
        assert built == []  # lazy: nothing is built until the page is used
        r = c.get("/chat")
        assert r.status_code == 200 and "Start a session" in r.text
        assert c.get("/chat").status_code == 200
        assert built == [db]  # one manager, over the metrics page's own database
        assert f'href="{chat_page.CHAT_PATH}">Chat</a>' in c.get("/").text  # shared nav
        assert managers[0].shutdowns == 0
    assert managers[0].shutdowns == 1  # the app's shutdown ended it


def test_chat_manager_is_reused_from_chat_cli() -> None:
    """The page builds its manager with ``chat_cli.default_manager``, not a copy."""
    import inspect

    src = inspect.getsource(web._default_chat_manager)
    assert "from foreman_inator.chat_cli import default_manager" in src
    assert "default_manager(store_path)" in src


def test_metrics_page_passes_the_chat_cap_and_month_and_shows_the_spend_block(
    foreman_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = paths.db_path()
    s = Store(db)
    sess = s.create_chat_session(REPO, base_branch="main", tier=1, model="qwen")
    s.record_spend("live", 1.25, model="cloud", chat_session_id=sess.id)
    t = s.create_ticket(repo=REPO, issue_number=1, title="t", request="r", base_branch="main")
    s.record_spend("live", 0.75, model="cloud", ticket_id=t.id)
    s.record_spend("experiments", 0.30, model="cloud", ticket_id=t.id)
    s.close()
    cfg = _cfg(chat_monthly_usd=7.0)
    seen: dict[str, Any] = {}
    real = web.q.report

    def spy(store: Store, *args: Any, **kw: Any) -> Any:
        seen.update(kw, expected_month=Spend(store, cfg).month_window())
        return real(store, *args, **kw)

    monkeypatch.setattr(web.q, "report", spy)
    a = web.full_app(db, recordings_dir=paths.recordings_dir(), traces_dir=None, config=cfg)
    page = TestClient(a, base_url=BASE).get("/").text
    assert seen["chat_monthly_usd"] == 7.0
    assert seen["month"] == seen["expected_month"]
    assert '<section class="card"><h2>Spend: tickets · chat · experiments</h2>' in page
    assert "<td>Chat (live)</td><td>$1.25</td>" in page
    assert "<td>Tickets (live)</td><td>$0.75</td>" in page
    assert "<td>Experiments</td><td>$0.30</td>" in page
    assert "<td>$1.25 of $7.00 (18%)</td>" in page


def test_spend_block_escapes_turn_statuses() -> None:
    from foreman_inator.metrics import queries as q

    sub = q.ChatSubCap("a", "b", 0.0, 20.0)
    split = q.SpendSplit(0.0, 0.0, 0.0, 1, 1, {"<b>x</b>": 1}, 0, sub)
    rep = SimpleNamespace(spend=split)
    out = web._spend_block(rep)  # type: ignore[arg-type]
    assert "<b>x</b>" not in out and "&lt;b&gt;x&lt;/b&gt;" in out


# --------------------------------------------------------------------------- status


def test_status_line_shows_chat_spend(foreman_home: Path) -> None:
    s = Store(paths.db_path())
    sess = s.create_chat_session(REPO, base_branch="main", tier=1, model="qwen")
    s.record_spend("live", 1.25, model="cloud", chat_session_id=sess.id)
    cfg = _cfg()
    summary = Spend(s, cfg).summary()
    assert summary.chat is not None and summary.chat.chat_today_usd == pytest.approx(1.25)
    assert summary.as_dict()["chat"]["chat_month_usd"] == pytest.approx(1.25)
    out = render_status(s, config=cfg)
    assert "; chat $1.25 today, $1.25 of $20.00 this month (tickets $0.00 today" in out
    s.close()


def test_status_shows_a_carried_approval_on_the_tickets_line(foreman_home: Path) -> None:
    s = Store(paths.db_path())
    cfg = _cfg()
    spend = Spend(s, cfg)
    tid = s.create_ticket(
        repo=REPO, issue_number=11, title="Carry me", request="r", base_branch="main"
    ).id
    assert spend.writer_gate(tid, approval_required=True).cap == "thrifty"
    spend.approve(tid)
    assert spend.writer_gate(tid, approval_required=True) is None
    aid = s.add_attempt(tid, model="claude", tier=3, trigger="first").id
    ctx = CallContext(kind="attempt", attempt_id=aid, bucket="live")
    assert spend.hook(CLOUD, 0.41, ctx, phase="estimate")
    spend.hook(CLOUD, 0.41, ctx, phase="actual")
    s.finish_attempt(aid, "superseded")
    assert spend.reopen_unspent_approvals([aid])[0].reopened
    note = spend.approval_note(tid)
    assert note == "approval #1: $4.59 of $5.00 left"
    lines = render_status(s, config=cfg).splitlines()
    at = next(i for i, ln in enumerate(lines) if ln.lstrip().startswith(f"#{tid} "))
    assert note in "\n".join(lines[at + 1 : at + 4])  # under the ticket, not only the tail
    s.close()


# --------------------------------------------------------------------------- worker


@pytest.fixture
def env(tmp_path: Path):
    e = Env(tmp_path)
    yield e
    e.close()


def test_recover_reopens_the_approvals_of_attempts_it_supersedes(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    n = env.label()
    tid = env.store.create_ticket(
        repo=ENV_REPO, issue_number=n, title="t", request="r", base_branch="main"
    ).id
    spend = env.worker.spend
    assert spend.writer_gate(tid, approval_required=True) is not None
    spend.approve(tid)
    assert spend.writer_gate(tid, approval_required=True) is None
    aid = env.store.add_attempt(tid, model="claude", tier=3, trigger="first").id
    assert env.store.open_spend_approval(tid) is None  # in use by the running attempt

    calls: list[list[int]] = []
    real = Spend.reopen_unspent_approvals

    def spy(self: Spend, ids: Any = None) -> Any:
        calls.append(list(ids))
        return real(self, ids)

    monkeypatch.setattr(Spend, "reopen_unspent_approvals", spy)
    assert env.restart() == [aid]
    assert calls == [[aid]]
    ap = env.store.open_spend_approval(tid)
    assert ap is not None and ap.reopened == 1  # carried to the next attempt


# --------------------------------------------------------------------------- end to end


@pytest.fixture
def h(tmp_path: Path):
    from test_chat_engine import Harness

    harness = Harness(tmp_path)
    yield harness
    harness.close()


def test_full_app_chat_end_to_end_start_send_sse_reply_then_draft_pr(h: Any) -> None:
    from test_chat_engine import REPO as ENGINE_REPO
    from test_chat_page import frames

    mgr = h.manager()
    shut: list[int] = []

    @contextlib.contextmanager
    def build() -> Iterator[Any]:
        try:
            yield mgr
        finally:
            shut.append(1)
            mgr.shutdown()

    token = forms.default_guard().token
    a = web.full_app(
        h.tmp / "fman.db", recordings_dir=h.tmp / "recordings", traces_dir=None,
        config=h.config, chat_manager=build,
    )  # fmt: skip

    def post(c: TestClient, path: str, data: dict[str, str]) -> Any:
        body = {forms.TOKEN_FIELD: token, **data}
        return c.post(path, data=body, headers={"Origin": BASE}, follow_redirects=False)

    with TestClient(a, base_url=BASE) as c:
        r = post(c, "/chat/start", {"repo": ENGINE_REPO, "base": "", "tier": ""})
        assert r.status_code == 303 and r.headers["location"] == "/chat/1"

        got: dict[str, Any] = {}

        def listen() -> None:  # the browser's EventSource, open across the turn
            got["r"] = TestClient(a, base_url=BASE).get("/chat/1/events")

        listener = threading.Thread(target=listen, daemon=True)
        listener.start()

        reply = "Hello from the fake model"
        h.scripts.append([("llm",), ("say", reply), ("edit", "app.py", "x = 21\n")])
        before = len(h.models.requests)
        assert post(c, "/chat/1/send", {"text": "please change x"}).status_code == 303
        turn = mgr.wait_turn(1, 60)
        assert turn is not None and turn.status == "finished"
        assert len(h.models.requests) > before  # the call went through the proxy's transport

        assert post(c, "/chat/1/pr", {"title": "Set x"}).status_code == 303
        listener.join(60)
        assert not listener.is_alive()
        sse = got["r"]
        assert sse.status_code == 200
        assert sse.headers["content-type"].startswith("text/event-stream")
        events = frames(sse.text)
        kinds = [f["event"] for f in events]
        texts = "".join(f["data"]["text"] for f in events if f["event"] == "text")
        assert reply in texts and kinds[-1] == "end"
        assert kinds.index("user") < kinds.index("text") < kinds.index("end")

        s = mgr.get(1)
        assert s.state == "opened_pr" and s.pr_number
        assert [w.method for w in h.github.writes] == ["create_draft_pr"]
        pr = h.github.get_pr(ENGINE_REPO, s.pr_number)
        assert pr.draft is True and pr.merged is False  # 7a
        assert h.github.writes[0].args["title"] == "Set x"
        assert shut == []
    assert shut == [1]
