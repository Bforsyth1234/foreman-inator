"""The /chat page (todo #75 / GH #64, wave 21 slot A): ``metrics/chat_page.py``.

Most tests drive the routes with a fake ``ChatManager`` over a real store (the page reads
the database read-only, the fake writes it, as the real manager does). The last tests use
the real ``ChatManager`` from ``test_chat_engine``'s harness (real git, proxy, spend, a
fake OpenCode server) for design section 11's checks end to end: messages stream in
order, a tier change applies to the next message only, the cost line is the ledger's.

Invariants the page touches: 7a (draft PR only; no merge or ready control), 7b (every
POST needs the token and the page's own Origin), 7c ("local only" shown; local tier 1 by
default), 7d (errors scrubbed; the secrets note), 7g (the sandbox-off banner).
"""

from __future__ import annotations

import gzip
import html
import json
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from fakes.github import FAKE_TOKEN
from foreman_inator import chat, paths
from foreman_inator.chat import ChatError, ChatManager
from foreman_inator.config import Config, ModelEndpoint, key_ref
from foreman_inator.db.store import NotFoundError, Store
from foreman_inator.metrics import chat_page, forms
from foreman_inator.metrics.web import LoopbackHostOnly
from foreman_inator.tracing.recorder import chat_recording_path

BASE = "http://127.0.0.1:8642"
TOKEN = "chat-page-token"
OLLAMA = "http://127.0.0.1:11434/v1"
SMALL = ModelEndpoint("qwen-small", 1, "local", "qwen3:8b", OLLAMA, "qwen")
BIG = ModelEndpoint("qwen-big", 2, "local", "qwen3:32b", OLLAMA, "qwen")
CLAUDE = ModelEndpoint(
    "claude", 3, "cloud", "claude-opus-4-5", "https://api.anthropic.com/v1", "claude",
    key_ref("anthropic"),
)  # fmt: skip
REPO = "acme/widgets"
LOCAL_REPO = "acme/secret"
EVIL = "<script>alert(1)</script>"


# --------------------------------------------------------------------------- the fake


class FakeManager:
    """The slice of ``ChatManager`` the page uses. ``events`` is the real generator over
    a real ``EventBuffer``, so SSE order and replay are the engine's own."""

    events = ChatManager.events

    def __init__(self, store: Store, config: Config, recordings: Path) -> None:
        self.store, self.config, self.recordings_dir = store, config, recordings
        self.spend = SimpleNamespace(approval_usd=5.0)
        self.turn_budget_usd = 1.5
        self.start_tier = 1
        self.calls: list[tuple] = []
        self.fail: str | None = None
        self.locks: list[str] = []
        self._buffers: dict[int, chat.EventBuffer] = {}

    def _buf(self, sid: int) -> chat.EventBuffer:
        return self._buffers.setdefault(sid, chat.EventBuffer())

    def emit(self, sid: int, type_: str, **data: Any) -> chat.ChatEvent:
        return self._buf(sid).add(type_, data)

    def scrub(self, text: str) -> str:
        return text.replace(FAKE_TOKEN, "[redacted]")

    def get(self, sid: int) -> chat.ChatSession:
        try:
            row = self.store.get_chat_session(sid)
        except NotFoundError as exc:
            raise ChatError(f"no chat session {sid}") from exc
        return chat.ChatSession(
            id=row.id, repo=row.repo, base_branch=row.base_branch, branch=row.branch,
            state=row.state, tier=row.tier, model=row.model, started_at=str(row.started_at),
        )  # fmt: skip

    def status(self, sid: int) -> dict[str, Any]:
        row = self.get(sid)
        return {
            "id": sid, "repo": row.repo, "state": row.state, "tier": row.tier,
            "model": row.model, "kind": "local" if row.tier == 1 else "cloud",
            "branch": row.branch, "base_branch": row.base_branch,
            "local_only": not self.store.repo_allows_cloud(row.repo),
            "network": False, "locks": list(self.locks), "approval_pending": False,
            "cost": self.cost(sid)["text"],
        }  # fmt: skip

    def cost(self, sid: int) -> dict[str, Any]:
        return {"text": f"session ${self.store.chat_spend(sid):.2f} · chat this month $0.00"}

    def start(self, repo: str, *, base_branch: str | None = None, tier: int | None = None):
        self.calls.append(("start", repo, base_branch, tier))
        if repo not in self.config.repos:
            raise ChatError(f"{repo} is not a watched repo (config repos)")
        row = self.store.create_chat_session(
            repo, base_branch=base_branch or "main", tier=tier or 1, model="qwen-small"
        )
        self.store.set_chat_state(row.id, "idle")
        return self.get(row.id)

    def _maybe_fail(self) -> None:
        if self.fail:
            raise ChatError(self.fail)

    def send(self, sid: int, text: str) -> None:
        self.calls.append(("send", sid, text))
        self._maybe_fail()

    def command(self, sid: int, cmd: chat.Command) -> str:
        self.calls.append(("command", sid, cmd.name, cmd.arg))
        self._maybe_fail()
        return "ok"


@pytest.fixture
def store() -> Store:
    s = Store(paths.db_path())
    s.set_cloud(LOCAL_REPO, False)
    yield s
    s.close()


@pytest.fixture
def mgr(store: Store, tmp_path: Path) -> FakeManager:
    cfg = Config(repos=(REPO, LOCAL_REPO), models=(SMALL, BIG, CLAUDE), reviewer_models=("claude",))
    return FakeManager(store, cfg, tmp_path / "recordings")


def make_client(db: Path, factory: Any, **kw: Any) -> TestClient:
    routes = chat_page.routes(db, manager_factory=factory, guard=forms.FormGuard(TOKEN), **kw)
    return TestClient(LoopbackHostOnly(Starlette(routes=routes)), base_url=BASE)


@pytest.fixture
def client(mgr: FakeManager) -> TestClient:
    return make_client(paths.db_path(), lambda: mgr, heartbeat=0.05)


def post(c: TestClient, path: str, data: dict[str, str] | None = None, **kw: Any):
    body = {forms.TOKEN_FIELD: TOKEN, **(data or {})}
    headers = {"Origin": BASE, **kw.pop("headers", {})}
    return c.post(path, data=body, headers=headers, follow_redirects=False, **kw)


def started(c: TestClient, mgr: FakeManager, repo: str = REPO) -> int:
    r = post(c, "/chat/start", {"repo": repo, "base": "", "tier": ""})
    assert r.status_code == 303
    return int(r.headers["location"].rsplit("/", 1)[1])


def nonce_of(resp: Any) -> str:
    m = re.fullmatch(
        r"default-src 'none'; script-src 'nonce-([A-Za-z0-9_-]+)'; style-src 'unsafe-inline'; "
        r"connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        resp.headers["content-security-policy"],
    )
    assert m, resp.headers["content-security-policy"]
    return m.group(1)


def frames(text: str) -> list[dict[str, Any]]:
    """Parse an SSE body as a browser would (WHATWG: lines, blank line ends a frame)."""
    out, cur = [], {}
    for line in text.split("\n"):
        if line == "":
            if "data" in cur:
                out.append(cur)
            cur = {}
        elif line.startswith(":"):
            cur.setdefault("comments", []).append(line)
        else:
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            cur[field] = cur[field] + "\n" + value if field in cur else value
    return [dict(f, data=json.loads(f["data"])) for f in out]


# --------------------------------------------------------------------------- GET /chat


def test_index_picks_a_watched_repo_marks_local_only_and_starts_a_session(
    client: TestClient, mgr: FakeManager
):
    r = client.get("/chat")
    assert r.status_code == 200
    nonce_of(r)
    assert f'<option value="{REPO}">{REPO}</option>' in r.text
    assert f'<option value="{LOCAL_REPO}">{LOCAL_REPO} (local only)</option>' in r.text  # 7c
    assert "default (tier 1)" in r.text and "tier 3: claude (cloud)" in r.text
    assert "<script" not in r.text  # no transcript, no script
    sid = started(client, mgr)
    assert mgr.calls[-1] == ("start", REPO, None, None)
    listing = client.get("/chat").text
    assert f'href="/chat/{sid}"' in listing and "idle" in listing


def test_start_refusals_are_shown_escaped(client: TestClient, mgr: FakeManager):
    r = post(client, "/chat/start", {"repo": "<b>evil/repo</b>", "tier": ""})
    assert r.status_code == 409
    assert "&lt;b&gt;evil/repo&lt;/b&gt; is not a watched repo" in r.text
    assert "<b>evil" not in r.text
    r = post(client, "/chat/start", {"repo": REPO, "tier": "two"})
    assert r.status_code == 400 and len(mgr.calls) == 1


# --------------------------------------------------------------------------- POST guard

POSTS = [
    ("/chat/start", {"repo": REPO}),
    ("/chat/1/send", {"text": "hello"}),
    ("/chat/1/tier", {"direction": "up"}),
    ("/chat/1/abort", {}),
    ("/chat/1/approve", {}),
    ("/chat/1/pr", {"title": "t"}),
    ("/chat/1/discard", {"confirm": "yes"}),
]


@pytest.mark.parametrize(("path", "data"), POSTS)
def test_every_post_needs_the_token_and_the_pages_own_origin(
    client: TestClient, mgr: FakeManager, store: Store, path: str, data: dict[str, str]
):
    """7b: a POST without the token, with a wrong one, from another origin or with no
    Origin is 403 and nothing reaches the manager."""
    store.create_chat_session(REPO, base_branch="main", tier=1, model="qwen-small")
    store.set_chat_state(1, "idle")
    bad = [
        dict(data=data, headers={"Origin": BASE}),
        dict(data={**data, forms.TOKEN_FIELD: "wrong"}, headers={"Origin": BASE}),
        dict(data={**data, forms.TOKEN_FIELD: TOKEN}, headers={"Origin": "http://evil.test"}),
        dict(data={**data, forms.TOKEN_FIELD: TOKEN}, headers={}),
        dict(
            data={**data, forms.TOKEN_FIELD: TOKEN},
            headers={"Origin": BASE, "Sec-Fetch-Site": "cross-site"},
        ),
    ]
    for kw in bad:
        r = client.post(path, follow_redirects=False, **kw)
        assert r.status_code == 403, (path, kw)
    assert mgr.calls == []
    assert len(store.list_chat_sessions()) == 1
    ok = post(client, path, data)  # the same request with the token and Origin goes through
    assert ok.status_code == 303 and len(mgr.calls) == 1


# --------------------------------------------------------------------------- session page


def test_session_page_has_one_script_with_this_responses_nonce(
    client: TestClient, mgr: FakeManager
):
    sid = started(client, mgr)
    a, b = client.get(f"/chat/{sid}"), client.get(f"/chat/{sid}")
    na, nb = nonce_of(a), nonce_of(b)
    assert na != nb  # per response
    assert a.text.count("<script") == 1
    assert f'<script nonce="{na}">' in a.text
    assert "src=" not in a.text.split("<script", 1)[1].split(">", 1)[0]  # inline only
    assert "innerHTML" not in chat_page.SCRIPT and "textContent" in chat_page.SCRIPT
    assert a.headers["cache-control"] == "no-store"


def test_model_output_and_user_text_are_inert(client: TestClient, mgr: FakeManager):
    sid = started(client, mgr)
    mgr.emit(sid, "user", text='<img src=x onerror="alert(1)">', turn=1)
    mgr.emit(sid, "text", part="p1", mode="append", text=f"</pre></div>{EVIL}")
    mgr.emit(sid, "text", part="p1", mode="append", text=" more")
    mgr.emit(sid, "tool", name="bash", kind="bash", summary=EVIL, is_error=False)
    mgr.emit(sid, "error", message=EVIL)
    page = client.get(f"/chat/{sid}").text
    assert page.count("<script") == 1  # only ours
    assert "<img" not in page and "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in page
    assert f"&lt;/pre&gt;&lt;/div&gt;{EVIL.replace('<', '&lt;').replace('>', '&gt;')} more" in page
    assert 'data-after="5"' in page  # the script resumes after what was rendered


def test_sse_streams_events_in_order_and_replays_after_last_event_id(
    client: TestClient, mgr: FakeManager
):
    sid = started(client, mgr)
    forged = 'ok</script><script>alert(1)</script>\n\ndata: {"forged":1}\nevent: end\n\n'
    mgr.emit(sid, "user", text="hi", turn=1)
    mgr.emit(sid, "status", state="busy", turn=1)
    mgr.emit(sid, "text", part="p1", mode="append", text="Hel")
    mgr.emit(sid, "text", part="p1", mode="append", text=forged)
    mgr.emit(sid, "status", state="idle", turn=1)
    mgr.emit(sid, "end", reason="discarded")
    r = client.get(f"/chat/{sid}/events")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.text.startswith(f"retry: {chat_page.RETRY_MS}\n\n")
    got = frames(r.text)
    assert [f["id"] for f in got] == ["1", "2", "3", "4", "5", "6"]
    assert [f["event"] for f in got] == ["user", "status", "text", "text", "status", "end"]
    assert got[3]["data"]["text"] == forged  # one inert text event, not a forged frame
    replay = frames(client.get(f"/chat/{sid}/events", headers={"Last-Event-ID": "3"}).text)
    assert [f["id"] for f in replay] == ["4", "5", "6"]
    by_query = frames(client.get(f"/chat/{sid}/events?after=4").text)
    assert [f["id"] for f in by_query] == ["5", "6"]


def test_sse_waits_for_live_events_with_keepalives(client: TestClient, mgr: FakeManager):
    sid = started(client, mgr)

    def later() -> None:
        time.sleep(0.3)
        mgr.emit(sid, "text", part="p1", mode="append", text="late")
        mgr.emit(sid, "end", reason="idle_timeout")

    t = threading.Thread(target=later)
    t.start()
    body = client.get(f"/chat/{sid}/events").text
    t.join()
    assert body.index(": keepalive") < body.index("late")
    assert [f["event"] for f in frames(body)] == ["text", "end"]


def test_sse_of_a_finished_session_with_nothing_new_is_204(
    client: TestClient, mgr: FakeManager, store: Store
):
    sid = started(client, mgr)
    mgr.emit(sid, "end", reason="discarded")
    store.set_chat_state(sid, "discarded", end_reason="discarded")
    assert client.get(f"/chat/{sid}/events", headers={"Last-Event-ID": "1"}).status_code == 204
    assert client.get("/chat/99/events").status_code == 404


def test_tier_buttons_and_commands(client: TestClient, mgr: FakeManager):
    sid = started(client, mgr)
    page = client.get(f"/chat/{sid}").text
    assert "Move up a tier" in page and "Tier down" in page
    assert post(client, f"/chat/{sid}/tier", {"direction": "up"}).status_code == 303
    assert post(client, f"/chat/{sid}/tier", {"tier": "3"}).status_code == 303
    assert post(client, f"/chat/{sid}/tier", {"direction": "sideways"}).status_code == 400
    assert post(client, f"/chat/{sid}/abort").status_code == 303
    assert mgr.calls[1:] == [
        ("command", sid, "tier", "up"), ("command", sid, "tier", "3"),
        ("command", sid, "abort", ""),
    ]  # fmt: skip


def test_send_busy_is_409_in_both_modes(client: TestClient, mgr: FakeManager):
    sid = started(client, mgr)
    assert post(client, f"/chat/{sid}/send", {"text": "  "}).status_code == 400
    assert len(mgr.calls) == 1
    r = post(client, f"/chat/{sid}/send", {"text": "fix it"})
    assert r.status_code == 303 and r.headers["location"] == f"/chat/{sid}"
    mgr.fail = "still working: /abort or wait"
    r = post(client, f"/chat/{sid}/send", {"text": "more"})
    assert r.status_code == 409 and "still working: /abort or wait" in r.text
    j = post(client, f"/chat/{sid}/send", {"text": "more"}, headers={"Accept": "application/json"})
    assert j.status_code == 409 and j.json() == {"ok": False, "error": mgr.fail}
    mgr.fail = None
    ok = post(client, f"/chat/{sid}/send", {"text": "x"}, headers={"Accept": "application/json"})
    assert ok.json() == {"ok": True}


def test_errors_shown_are_scrubbed(client: TestClient, mgr: FakeManager):
    """7d: a manager error that carries a token is scrubbed before it is shown."""
    sid = started(client, mgr)
    mgr.fail = f"push failed: https://x-access-token:{FAKE_TOKEN}@github.com"
    r = post(client, f"/chat/{sid}/pr", {"title": "t"})
    assert r.status_code == 409 and FAKE_TOKEN not in r.text and "[redacted]" in r.text
    assert html.escape(chat_page.SECRETS_NOTE) in r.text  # the note under the input box


def test_pr_is_a_draft_and_discard_needs_confirming(client: TestClient, mgr: FakeManager):
    """7a: the page offers a draft PR and discard, nothing that merges or marks ready."""
    sid = started(client, mgr)
    page = client.get(f"/chat/{sid}").text
    assert "Open as draft PR" in page and "not reviewed by the AI reviewer" in page
    assert not re.search(r"(?i)>\s*(merge|mark ready|approve pr)", page)
    assert post(client, f"/chat/{sid}/pr", {"title": "Fix <it>"}).status_code == 303
    r = post(client, f"/chat/{sid}/discard")
    assert r.status_code == 400 and "confirm" in r.text
    assert post(client, f"/chat/{sid}/discard", {"confirm": "yes"}).status_code == 303
    assert mgr.calls[1:] == [("command", sid, "pr", "Fix <it>"), ("command", sid, "discard", "yes")]


def test_approve_button_only_after_an_approvable_refusal(client: TestClient, mgr: FakeManager):
    sid = started(client, mgr)
    assert 'id="approve" hidden' in client.get(f"/chat/{sid}").text
    mgr.emit(sid, "user", text="big job", turn=1)
    mgr.emit(sid, "refused", cap="monthly", approvable=True, message="monthly cap", hint="h")
    page = client.get(f"/chat/{sid}").text
    assert 'id="approve" data-live' in page and "Allow this message (up to $5.00)" in page
    assert "refused: monthly cap - h" in page
    assert post(client, f"/chat/{sid}/approve").status_code == 303
    assert mgr.calls[-1] == ("command", sid, "approve", "")
    mgr.emit(sid, "refused", cap="local_only", approvable=False, message="local", hint="x")
    assert 'id="approve" hidden' in client.get(f"/chat/{sid}").text


def test_fallback_lock_warning_cost_and_budget_are_shown(
    client: TestClient, mgr: FakeManager, store: Store
):
    """Decisions 2 and 6, section 5's cost line and the per-message budget."""
    sid = started(client, mgr)
    store.record_spend("live", 0.4321, model="claude", chat_session_id=sid)
    mgr.locks = ["src/app.py"]
    mgr.emit(sid, "fallback", model="claude", message="local slot busy, used claude")
    mgr.emit(sid, "warning", kind="locks", message="files this chat edited are held by #7")
    page = client.get(f"/chat/{sid}").text
    assert '<div class="note warn">local slot busy, used claude</div>' in page
    assert 'id="lockwarn">files this chat edited are held by #7</p>' in page
    assert "Files this chat holds: src/app.py" in page
    assert '<span id="cost">session $0.43 · chat this month $0.00</span>' in page
    assert "per-message budget $1.50" in page
    assert '<span id="tier">tier 1 · qwen-small (local)</span>' in page


def test_works_without_javascript_busy_page_refreshes(
    client: TestClient, mgr: FakeManager, store: Store
):
    sid = started(client, mgr)
    idle = client.get(f"/chat/{sid}").text
    assert "http-equiv" not in idle
    for action in ("send", "tier", "abort", "approve", "pr", "discard"):
        assert f'<form method="post" action="/chat/{sid}/{action}"' in idle
    assert idle.count(f'name="{forms.TOKEN_FIELD}" value="{TOKEN}"') >= 7
    store.set_chat_state(sid, "busy")
    busy = client.get(f"/chat/{sid}").text
    assert f'<noscript><meta http-equiv="refresh" content="{chat_page.REFRESH_SECONDS}">' in busy
    send = busy.split(f'action="/chat/{sid}/send"', 1)[1].split("</form>", 1)[0]
    assert '<button type="submit" data-idle-only disabled>Send</button>' in send


def test_local_only_and_sandbox_off_are_shown(store: Store, tmp_path: Path):
    """7c ("local only" on the session) and 7g (the sandbox-off banner)."""
    cfg = Config(
        repos=(REPO, LOCAL_REPO),
        models=(SMALL, BIG, CLAUDE),
        reviewer_models=("claude",),
        sandbox="off",
    )
    mgr = FakeManager(store, cfg, tmp_path / "recordings")
    c = make_client(paths.db_path(), lambda: mgr)
    sid = started(c, mgr, LOCAL_REPO)
    page = c.get(f"/chat/{sid}").text
    assert '<span class="badge">local only</span>' in page
    assert '<p class="banner">Sandbox: OFF' in page


def test_ended_session_is_read_only_with_its_recorded_transcript(
    client: TestClient, mgr: FakeManager, store: Store
):
    sid = started(client, mgr)
    store.set_chat_state(sid, "ended", end_reason="host_restart")
    path = chat_recording_path(mgr.recordings_dir, sid)
    path.parent.mkdir(parents=True)
    rows = [{"kind": "header"}, {"kind": "user", "text": "make it fast"},
            {"kind": "llm", "text": f"done {EVIL}"}]  # fmt: skip
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in rows))
    page = client.get(f"/chat/{sid}").text
    assert "make it fast" in page and "done &lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert page.count("<script") == 1
    assert f'action="/chat/{sid}/send"' not in page and "read-only" in page
    assert f'action="/chat/{sid}/pr"' in page and f'action="/chat/{sid}/discard"' in page
    store.set_chat_state(sid, "discarded", end_reason="discarded")
    final = client.get(f"/chat/{sid}").text
    assert f'action="/chat/{sid}/pr"' not in final


def test_unknown_session_and_a_failing_factory(client: TestClient, tmp_path: Path):
    assert client.get("/chat/42").status_code == 404
    assert post(client, "/chat/42/send", {"text": "x"}).status_code == 404

    def broken() -> Any:
        raise RuntimeError("no config")

    c = make_client(tmp_path / "none.db", broken)
    r = c.get("/chat")
    assert r.status_code == 503 and "no config" in r.text
    assert c.get("/chat/1/events").status_code == 503


def test_nav_entry():
    assert chat_page.NAV_ENTRY == ("/chat", "Chat")


# --------------------------------------------------------------------------- real engine


@pytest.fixture
def h(tmp_path: Path):
    from test_chat_engine import Harness

    harness = Harness(tmp_path)
    yield harness
    harness.close()


def test_real_manager_streams_in_order_tier_change_applies_next_message_and_cost_is_ledger(
    h: Any,
):
    """Design section 11's tests against the real engine: messages stream in order,
    ``POST /chat/<id>/tier`` switches the model for the next message only, and the
    cost line equals ``SUM(spend_ledger.cost_usd)`` for the session."""
    from test_chat_engine import REPO as ENGINE_REPO

    mgr = h.manager()
    c = make_client(h.tmp / "fman.db", lambda: mgr, heartbeat=0.05)
    r = post(c, "/chat/start", {"repo": ENGINE_REPO, "base": "", "tier": ""})
    assert r.status_code == 303 and r.headers["location"] == "/chat/1"
    assert mgr.get(1).tier == 1  # 7c: local tier 1 by default

    h.scripts.append([("say", "Hel"), ("say", "Hello </script>")])
    assert post(c, "/chat/1/send", {"text": "first"}).status_code == 303
    mgr.wait_turn(1, 60)
    h.scripts.append([("llm",), ("say", "done")])
    assert post(c, "/chat/1/tier", {"tier": "3"}).status_code == 303
    assert post(c, "/chat/1/send", {"text": "second"}).status_code == 303
    mgr.wait_turn(1, 60)
    assert [m for _, m in h.servers[0].sent] == ["qwen3:8b", "claude-opus-4-5"]

    page = c.get("/chat/1").text
    spent = h.store.chat_spend(1)
    assert spent > 0 and f'<span id="cost">session ${spent:.2f} · ' in page
    assert "Hello &lt;/script&gt;" in page and page.count("<script") == 1

    assert post(c, "/chat/1/discard", {"confirm": "yes"}).status_code == 303
    got = frames(c.get("/chat/1/events").text)
    seqs = [int(f["id"]) for f in got]
    assert seqs == list(range(1, len(seqs) + 1))
    kinds = [f["event"] for f in got]
    first = kinds.index("user")
    assert kinds[first : first + 4] == ["user", "status", "text", "text"] and kinds[-1] == "end"
    texts = [f["data"]["text"] for f in got if f["event"] == "text"]
    assert "".join(texts[:2]) == "Hello </script>"
    users = [f["data"]["text"] for f in got if f["event"] == "user"]
    assert users == ["first", "second"]
    tier_at = kinds.index("tier")
    assert kinds.index("user", 1) > tier_at  # the move came before the second message
    turns = h.store.list_chat_turns(1)
    assert [(t.tier, t.model) for t in turns] == [(1, "qwen-small"), (3, "claude")]
    assert h.store.get_chat_session(1).state == "discarded"
