"""The settings page (todo #73, GH #62): edit config.toml and repo settings in the browser."""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

import pytest
import tomli_w
from starlette.applications import Starlette
from starlette.testclient import TestClient

from foreman_inator import config as config_mod
from foreman_inator import paths
from foreman_inator.config import Config, ConfigError
from foreman_inator.db.store import Store
from foreman_inator.metrics import forms
from foreman_inator.metrics import settings_ui as ui
from foreman_inator.metrics.web import LoopbackHostOnly
from foreman_inator.routing import profile as prof

BASE = "http://127.0.0.1:8642"
TOKEN = "settings-token"
#: What the fake App installation can see (GitHub's spelling).
SEEN = ["acme/widgets", "acme/gadgets", "Acme/Gizmos"]


@pytest.fixture
def cfg_path() -> Path:
    path = paths.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    config_mod.save(Config(allowed_users=("brooks",), repos=("acme/widgets",)), path)
    return path


@pytest.fixture
def db() -> Path:
    db = paths.db_path()
    with Store(db):
        pass
    return db


@pytest.fixture
def client(cfg_path: Path, db: Path) -> TestClient:
    routes = ui.routes(
        db, config_path=cfg_path, guard=forms.FormGuard(TOKEN), access=lambda cfg: SEEN
    )
    return TestClient(LoopbackHostOnly(Starlette(routes=routes)), base_url=BASE)


def page_form(c: TestClient) -> dict[str, str]:
    """The config form's fields as the page renders them (what a browser would send)."""
    text = c.get("/settings").text
    form = text.split('action="/settings"', 1)[1].split("</form>", 1)[0]
    out: dict[str, str] = {}
    for m in re.finditer(r'<input type="(text|hidden)" name="([^"]+)"[^>]*value="([^"]*)"', form):
        out[m.group(2)] = m.group(3)
    for m in re.finditer(r'<textarea name="([^"]+)"[^>]*>([^<]*)</textarea>', form):
        out[m.group(1)] = m.group(2)
    if re.search(r'name="status_comment" value="on" checked', form):
        out["status_comment"] = "on"
    out.pop("csrf_token", None)
    return out


def post(c: TestClient, path: str, data: dict[str, str], **kw: Any) -> Any:
    data = {"csrf_token": TOKEN, **data}
    headers = {"origin": BASE, **kw.pop("headers", {})}
    return c.post(path, data=data, headers=headers, **kw)


def load_message(path: Path, raw: dict[str, Any], tmp: Path) -> str:
    """What ``config.load`` says about ``raw`` written at ``path``'s name."""
    other = tmp / "same-name" / path.name
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text(tomli_w.dumps(raw))
    with pytest.raises(ConfigError) as info:
        config_mod.load(other)
    return str(info.value).replace(str(other), str(path))


# =========================================================================== config form


def _client_with(cfg_path: Path, db: Path, access: Any) -> TestClient:
    kw = {} if access is None else {"access": access}
    routes = ui.routes(db, config_path=cfg_path, guard=forms.FormGuard(TOKEN), **kw)
    return TestClient(LoopbackHostOnly(Starlette(routes=routes)), base_url=BASE)


def test_adding_a_repo_the_installation_cannot_see_is_refused(
    client: TestClient, cfg_path: Path
) -> None:
    before = cfg_path.read_bytes()
    f = page_form(client)
    f["repos"] = "acme/widgets\nacme/secret"
    r = post(client, "/settings", f)
    assert r.status_code == 400
    assert "cannot see acme/secret" in r.text
    assert cfg_path.read_bytes() == before


def test_an_added_repo_takes_githubs_spelling(client: TestClient, cfg_path: Path) -> None:
    f = page_form(client)
    f["repos"] = "acme/widgets\nacme/gizmos"
    r = post(client, "/settings", f)
    assert r.status_code == 200, r.text
    assert config_mod.load(cfg_path).repos == ("acme/widgets", "Acme/Gizmos")


@pytest.mark.parametrize("access", [None, "boom"], ids=["no-credentials", "github-error"])
def test_without_an_installation_check_adding_is_refused_but_removing_works(
    cfg_path: Path, db: Path, access: Any
) -> None:
    def broken(cfg: Config) -> list[str]:
        raise OSError("network down")

    c = _client_with(cfg_path, db, broken if access == "boom" else None)
    before = cfg_path.read_bytes()
    f = page_form(c)
    f["repos"] = "acme/widgets\nacme/gadgets"
    r = post(c, "/settings", f)
    assert r.status_code == 400
    assert "fman repo add" in r.text
    assert cfg_path.read_bytes() == before
    f = page_form(c)
    f["repos"] = ""
    r = post(c, "/settings", f)
    assert r.status_code == 200, r.text
    assert config_mod.load(cfg_path).repos == ()


def test_get_shows_current_values_and_the_reload_note(client: TestClient) -> None:
    r = client.get("/settings")
    assert r.status_code == 200
    assert ui.RELOAD_NOTE in r.text
    f = page_form(client)
    assert f["daily_usd"] == "5.0"
    assert f["allowed_users"] == "brooks"
    assert f["repos"] == "acme/widgets"
    assert f["status_comment"] == "on"
    assert "<script" not in r.text


def test_a_valid_save_round_trips_through_config_load_and_lists_changes(
    client: TestClient, cfg_path: Path
) -> None:
    f = page_form(client)
    f.update(
        daily_usd="3",
        monthly_usd="40.5",
        writer_timeout_seconds="1800",
        max_parallel_per_repo="2",
        max_open_prs_per_repo="0",
        allowed_users="brooks\nalice, bob",
        repos="acme/widgets\nacme/gadgets",
    )
    del f["status_comment"]  # unchecked
    r = post(client, "/settings", f)
    assert r.status_code == 200, r.text
    assert "Saved. Changed:" in r.text
    assert "spend_caps.daily_usd: 5.0 -&gt; 3.0" in r.text
    assert "repos: [acme/widgets] -&gt; [acme/widgets, acme/gadgets]" in r.text
    cfg = config_mod.load(cfg_path)
    assert cfg.spend_caps.daily_usd == 3.0
    assert cfg.spend_caps.monthly_usd == 40.5
    assert cfg.writer_timeout_seconds == 1800.0
    assert cfg.max_parallel_per_repo == 2
    assert cfg.max_open_prs_per_repo == 0
    assert cfg.allowed_users == ("brooks", "alice", "bob")
    assert cfg.repos == ("acme/widgets", "acme/gadgets")
    assert cfg.status_comment is False
    # The page now shows the saved values and nothing changes on a second save.
    again = page_form(client)
    assert again["daily_usd"] == "3.0"
    r2 = post(client, "/settings", again)
    assert "Nothing changed." in r2.text


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("daily_usd", "-1"),
        ("max_parallel_per_repo", "0"),
        ("max_parallel_per_repo", "two"),
        ("writer_timeout_seconds", "-5"),
        ("repos", "not-a-repo"),
    ],
)
def test_an_invalid_value_is_refused_with_config_loads_message_and_the_file_is_unchanged(
    client: TestClient, cfg_path: Path, tmp_path: Path, field: str, value: str
) -> None:
    before = cfg_path.read_bytes()
    f = page_form(client)
    f[field] = value
    r = post(client, "/settings", f)
    assert r.status_code == 400
    assert cfg_path.read_bytes() == before
    raw = ui.apply_form(ui.read_raw(cfg_path), f)
    expected = load_message(cfg_path, raw, tmp_path)
    assert html.escape(f"Not saved: {expected}") in r.text
    assert "config.toml was not changed" in r.text
    # The user's input is shown back, escaped.
    assert value in r.text


def test_negative_cap_message_is_the_config_load_text(client: TestClient, cfg_path: Path) -> None:
    f = page_form(client)
    f["daily_usd"] = "-1"
    r = post(client, "/settings", f)
    assert f"{cfg_path}: spend_caps.daily_usd must be &gt;= 0" in r.text


def test_a_stale_page_is_refused_and_a_hand_edit_is_kept(
    client: TestClient, cfg_path: Path
) -> None:
    f = page_form(client)
    cfg = config_mod.load(cfg_path)
    from dataclasses import replace

    config_mod.save(replace(cfg, poll_interval_seconds=60), cfg_path)  # a hand edit
    hand = cfg_path.read_bytes()
    f["daily_usd"] = "1"
    r = post(client, "/settings", f)
    assert r.status_code == 409
    assert cfg_path.read_bytes() == hand


def test_post_without_token_or_from_a_foreign_origin_is_403_and_nothing_changes(
    client: TestClient, cfg_path: Path, db: Path
) -> None:
    before = cfg_path.read_bytes()
    f = page_form(client)
    f["daily_usd"] = "1"
    r = client.post("/settings", data=f, headers={"origin": BASE})
    assert r.status_code == 403
    r = post(client, "/settings", f, headers={"origin": "http://evil.example"})
    assert r.status_code == 403
    r = post(client, "/settings", {**f, "csrf_token": "wrong"})
    assert r.status_code == 403
    r = post(client, "/settings/repo", {"repo": "acme/widgets", "cloud": "off"},
             headers={"origin": "http://evil.example"})  # fmt: skip
    assert r.status_code == 403
    assert cfg_path.read_bytes() == before
    with Store(db, read_only=True) as s:
        assert s.get_repo_settings("acme/widgets") is None


def test_values_from_the_file_are_escaped(client: TestClient, cfg_path: Path) -> None:
    raw = ui.read_raw(cfg_path)
    raw["allowed_users"] = ['<script>alert("x")</script>']
    cfg_path.write_text(tomli_w.dumps(raw))
    r = client.get("/settings")
    assert '<script>alert("x")</script>' not in r.text
    assert "&lt;script&gt;" in r.text


def test_an_unreadable_file_is_shown_not_overwritten(client: TestClient, cfg_path: Path) -> None:
    cfg_path.write_text("daily_usd = [")
    r = client.get("/settings")
    assert r.status_code == 500
    assert "invalid TOML" in r.text
    assert 'action="/settings"' not in r.text
    r = post(client, "/settings", {"config_hash": ui.file_hash(cfg_path), "daily_usd": "1"})
    assert r.status_code == 400
    assert cfg_path.read_text() == "daily_usd = ["


# =========================================================================== repo form


def test_repo_settings_go_through_the_cli_store_methods(
    client: TestClient, db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from foreman_inator import worker_status

    calls: list[str] = []
    real_verify, real_profile = worker_status.set_verify_command, worker_status.set_profile

    def spy_verify(*a: Any, **kw: Any) -> Any:
        calls.append("verify")
        return real_verify(*a, **kw)

    def spy_profile(*a: Any, **kw: Any) -> Any:
        calls.append("profile")
        return real_profile(*a, **kw)

    monkeypatch.setattr(worker_status, "set_verify_command", spy_verify)
    monkeypatch.setattr(worker_status, "set_profile", spy_profile)
    r = post(
        client,
        "/settings/repo",
        {
            "repo": "acme/widgets",
            "verify_command": "make test",
            "install_command": "uv sync --frozen",
            "writer_mode": "thrifty",
            "review_mode": "light",
            "cloud": "off",
        },
    )
    assert r.status_code == 200, r.text
    assert calls == ["verify", "profile"]
    assert "verify command: (none) -&gt; &#x27;make test&#x27;" in r.text
    assert "cloud models: on -&gt; off" in r.text
    with Store(db, read_only=True) as s:
        rs = s.get_repo_settings("acme/widgets")
    assert rs is not None
    assert rs.verify_command == "make test"
    assert rs.install_command == "uv sync --frozen"
    assert prof.profile_of(rs) == prof.Profile("thrifty", "light")
    assert rs.cloud is False
    # The page now shows them; clearing the verify command clears it.
    text = client.get("/settings").text
    assert 'value="make test"' in text
    r = post(client, "/settings/repo", {"repo": "acme/widgets", "verify_command": ""})
    with Store(db, read_only=True) as s:
        rs = s.get_repo_settings("acme/widgets")
    assert rs is not None and rs.verify_command is None
    assert rs.cloud is False  # fields not sent are left alone


def test_repo_form_refuses_unknown_repo_and_bad_mode(client: TestClient, db: Path) -> None:
    r = post(client, "/settings/repo", {"repo": "acme/other", "cloud": "off"})
    assert r.status_code == 400
    assert "not a watched repo" in r.text
    r = post(client, "/settings/repo", {"repo": "acme/widgets", "writer_mode": "turbo"})
    assert r.status_code == 400
    assert "unknown writer mode" in r.text
    with Store(db, read_only=True) as s:
        assert s.get_repo_settings("acme/other") is None
        assert s.get_repo_settings("acme/widgets") is None


def test_repo_values_are_escaped(client: TestClient, db: Path) -> None:
    with Store(db) as s:
        s.set_verify_command("acme/widgets", '"><script>x</script>')
    text = client.get("/settings").text
    assert "<script>x</script>" not in text
    assert "&quot;&gt;&lt;script&gt;" in text


def test_nav_entry_and_routes_shape() -> None:
    assert ui.NAV_ENTRY == ("/settings", "Settings")
    paths_ = [(r.path, sorted(r.methods or ())) for r in ui.routes("x.db")]  # type: ignore[attr-defined]
    assert ("/settings", ["GET", "HEAD"]) in paths_
    assert ("/settings", ["POST"]) in paths_
    assert ("/settings/repo", ["POST"]) in paths_
