"""The tiers page: models and providers per tier (todo #74, GH #63)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from foreman_inator import config, paths
from foreman_inator.config import Config, ModelEndpoint, key_ref
from foreman_inator.db.store import Store
from foreman_inator.metrics import forms
from foreman_inator.metrics import tiers_ui as ui
from foreman_inator.metrics.web import LoopbackHostOnly

BASE = "http://127.0.0.1:8642"
TOKEN = "page-token"
LOCAL_URL = "http://127.0.0.1:11434/v1"
CLOUD_URL = "https://a.example.invalid/v1"
SECRET = "fake-" + "k3y-" + "generated-at-test-time-0123456789"


class FakeKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, name: str, value: str) -> None:
        self.store[(service, name)] = value

    def get_password(self, service: str, name: str) -> str | None:
        return self.store.get((service, name))


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    fake = FakeKeyring()
    monkeypatch.setattr(config.keyring, "set_password", fake.set_password)
    monkeypatch.setattr(config.keyring, "get_password", fake.get_password)
    return fake


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
    )  # fmt: skip


@pytest.fixture
def cfg_path() -> Path:
    config.save(make_config())
    return paths.config_path()


def make_client(probe: Any = None, store_key: Any = None) -> TestClient:
    routes = ui.routes(
        paths.db_path(), guard=forms.FormGuard(TOKEN), probe=probe, store_key=store_key
    )
    return TestClient(LoopbackHostOnly(Starlette(routes=routes)), base_url=BASE)


def post(c: TestClient, path: str, data: dict[str, str] | None = None, **kw: Any) -> Any:
    data = {"csrf_token": TOKEN, **(data or {})}
    headers = {"origin": BASE, **kw.pop("headers", {})}
    return c.post(path, data=data, headers=headers, **kw)


def names(cfg: Config, tier: int) -> list[str]:
    return [m.name for m in cfg.models_for_tier(tier)]


NEW = {
    "original": "",
    "name": "glm-b",
    "tier": "2",
    "kind": "cloud",
    "model": "glm-5.3",
    "base_url": config.GLM_URL,
    "wire": "openai",
    "family": "glm",
    "key_name": "glm-b",
    "api_key": "",
}


# =========================================================================== round trips


def test_add_reorder_and_remove_round_trip_through_config_load(cfg_path: Path) -> None:
    c = make_client()
    r = post(c, "/tiers/endpoint", {**NEW, "api_key": SECRET, "vision": "1"})
    assert r.status_code == 200, r.text
    cfg = config.load()
    assert names(cfg, 2) == ["w-two", "glm-b"]
    added = cfg.model("glm-b")
    assert (added.kind, added.wire, added.family, added.vision) == ("cloud", "openai", "glm", True)
    assert added.api_key_ref == key_ref("glm-b")

    assert post(c, "/tiers/endpoint/move", {"name": "glm-b", "dir": "up"}).status_code == 200
    assert names(config.load(), 2) == ["glm-b", "w-two"]
    assert names(config.load(), 1) == ["w-one", "w-alt"]  # other tiers untouched
    post(c, "/tiers/endpoint/move", {"name": "w-alt", "dir": "up"})
    assert names(config.load(), 1) == ["w-alt", "w-one"]

    assert post(c, "/tiers/endpoint/remove", {"name": "w-two"}).status_code == 200
    assert names(config.load(), 2) == ["glm-b"]


def test_edit_renames_and_follows_into_reviewer_models(cfg_path: Path) -> None:
    c = make_client()
    form = {"original": "rev-x", "name": "rev-y", "tier": "3", "kind": "cloud",
            "model": "rev-y-up", "base_url": "https://x.example.invalid/v1", "wire":
            "anthropic", "family": "fam-x", "key_name": "x"}  # fmt: skip
    assert post(c, "/tiers/endpoint", form).status_code == 200
    cfg = config.load()
    assert cfg.reviewer_models == ("rev-y",)
    assert cfg.model("rev-y").wire == "anthropic"
    assert [m.name for m in cfg.models] == ["w-one", "w-alt", "w-two", "rev-y"]


def test_invalid_change_is_refused_with_config_loads_message_and_file_unchanged(
    cfg_path: Path, fake_keyring: FakeKeyring
) -> None:
    before = cfg_path.read_bytes()
    c = make_client()
    bad = {**NEW, "base_url": "http://plain.example.invalid/v1", "api_key": SECRET}
    r = post(c, "/tiers/endpoint", bad)
    assert r.status_code == 400
    with pytest.raises(config.ConfigError) as exc:  # what `config.load` says
        ui.validated(ui.put_endpoint(config.load(), ui.endpoint_from_form(bad)))
    assert "a cloud base_url must use https" in str(exc.value)
    assert str(exc.value).replace("'", "&#x27;") in r.text
    assert cfg_path.read_bytes() == before
    assert fake_keyring.store == {}  # a refused change stores no key either
    # The last endpoint of a tier cannot go (config.load: "tier 3 has no model").
    r = post(c, "/tiers/endpoint/remove", {"name": "rev-x"})
    assert r.status_code == 400 and "tier 3 has no model" in r.text
    assert cfg_path.read_bytes() == before


def test_reviewer_order_is_edited_on_the_same_page(cfg_path: Path) -> None:
    c = make_client()
    post(c, "/tiers/reviewers", {"action": "add", "name": "w-two"})
    assert config.load().reviewer_models == ("rev-x", "w-two")
    post(c, "/tiers/reviewers", {"action": "up", "name": "w-two"})
    assert config.load().reviewer_models == ("w-two", "rev-x")
    post(c, "/tiers/reviewers", {"action": "remove", "name": "rev-x"})
    assert config.load().reviewer_models == ("w-two",)
    assert post(c, "/tiers/reviewers", {"action": "order", "reviewer_order": "cheapest"}
                ).status_code == 200  # fmt: skip
    assert config.load().reviewer_order == "cheapest"
    r = post(c, "/tiers/reviewers", {"action": "order", "reviewer_order": "random"})
    assert r.status_code == 400 and "reviewer_order must be one of" in r.text
    assert config.load().reviewer_order == "cheapest"


# =========================================================================== keys


def test_key_lands_in_keychain_and_never_in_the_html_or_config(
    cfg_path: Path, fake_keyring: FakeKeyring
) -> None:
    c = make_client()
    r = post(c, "/tiers/endpoint", {**NEW, "api_key": SECRET})
    assert r.status_code == 200
    assert fake_keyring.store == {("foreman-inator", "glm-b"): SECRET}
    assert SECRET not in cfg_path.read_text()
    assert SECRET not in r.text
    for url in ("/tiers", "/tiers?edit=glm-b", "/tiers?preset=glm"):
        page = c.get(url).text
        assert SECRET not in page
        assert '<input type="password" name="api_key" autocomplete="off">' in page
    assert "key set (glm-b)" in c.get("/tiers").text
    assert "no key (x)" in c.get("/tiers").text


def test_blank_key_on_edit_keeps_the_stored_key(cfg_path: Path, fake_keyring: FakeKeyring) -> None:
    c = make_client()
    post(c, "/tiers/endpoint", {**NEW, "api_key": SECRET})
    post(c, "/tiers/endpoint", {**NEW, "original": "glm-b", "model": "glm-5.4"})
    assert fake_keyring.store == {("foreman-inator", "glm-b"): SECRET}
    assert config.load().model("glm-b").model == "glm-5.4"


def test_page_uses_the_cli_key_helper(cfg_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The default ``store_key`` is ``config.set_key``, what `fman config set-key` calls."""
    calls: list[str] = []
    real = config.set_key

    def spy(name: str, value: str) -> str:
        calls.append(name)
        return real(name, value)

    monkeypatch.setattr(config, "set_key", spy)
    post(make_client(), "/tiers/endpoint", {**NEW, "api_key": SECRET})
    assert calls == ["glm-b"]


# =========================================================================== presets


@pytest.mark.parametrize(
    ("preset", "base", "wire", "family", "kind"),
    [
        ("ollama", config.OLLAMA_URL, "openai", "", "local"),
        ("glm", config.GLM_URL, "openai", "glm", "cloud"),
        ("anthropic", config.ANTHROPIC_URL, "anthropic", "claude", "cloud"),
        ("openai", "https://", "openai", "", "cloud"),
    ],
)
def test_presets_fill_the_form(cfg_path: Path, preset, base, wire, family, kind) -> None:
    page = make_client().get(f"/tiers?preset={preset}").text
    assert f'name="base_url" value="{base}"' in page
    assert f'<option value="{wire}" selected>' in page
    assert f'name="family" value="{family}"' in page
    assert f'<option value="{kind}" selected>' in page


# =========================================================================== warnings


def test_warns_when_writers_share_the_only_reviewers_family(cfg_path: Path) -> None:
    cfg = make_config()
    same = Config(
        models=tuple(m if m.name != "rev-x" else replace(m, family="fam-w") for m in cfg.models),
        reviewer_models=("rev-x",),
    )  # fmt: skip
    warns = ui.tier_warnings(same)
    assert any("Tier 1: w-one" in w and "invariant 7f" in w for w in warns)
    assert not any("w-two" in w for w in warns)  # fam-w2 has a reviewer of another family
    config.save(same)
    assert "invariant 7f" in make_client().get("/tiers").text
    assert [w.split(":")[0] for w in ui.tier_warnings(make_config())] == ["Tier 3"]  # rev-x itself


def test_warns_when_a_local_only_repo_has_no_tier_left(cfg_path: Path) -> None:
    with Store(paths.db_path()) as s:
        s.set_cloud("acme/widgets", False)
    c = make_client()
    assert "local-only repos" not in c.get("/tiers").text
    cloud = Config(
        models=(
            ModelEndpoint("c1", 1, "cloud", "c1", CLOUD_URL, "fa", key_ref("a")),
            ModelEndpoint("c2", 2, "cloud", "c2", CLOUD_URL, "fb", key_ref("a")),
            ModelEndpoint("c3", 3, "cloud", "c3", CLOUD_URL, "fc", key_ref("a")),
        ),
        reviewer_models=("c2", "c1"),
    )  # fmt: skip
    config.save(cloud)
    page = c.get("/tiers").text
    assert "local-only repos (acme/widgets) would have no tier" in page
    assert ui.tier_warnings(cloud) == []  # no local-only repo, no warning


# =========================================================================== guard


def test_post_without_token_or_from_a_foreign_origin_is_refused(cfg_path: Path) -> None:
    before = cfg_path.read_bytes()
    c = make_client()
    r = c.post("/tiers/endpoint/remove", data={"name": "w-alt"}, headers={"origin": BASE})
    assert r.status_code == 403
    r = post(c, "/tiers/endpoint/remove", {"name": "w-alt"},
             headers={"origin": "http://evil.example"})  # fmt: skip
    assert r.status_code == 403
    assert cfg_path.read_bytes() == before


def test_everything_from_config_is_escaped(cfg_path: Path) -> None:
    cfg = make_config()
    evil = ModelEndpoint("w-three", 1, "local", "<script>x</script>", LOCAL_URL, "fam<b>")
    config.save(Config(models=(*cfg.models, evil), reviewer_models=cfg.reviewer_models))
    page = make_client().get("/tiers").text
    assert "<script>x</script>" not in page and "&lt;script&gt;" in page
    assert "fam<b>" not in page


# =========================================================================== test button


def chat_ok(text: str = "ok") -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1}}  # fmt: skip


def test_probe_goes_through_the_proxy_and_reports_the_reply(cfg_path: Path) -> None:
    seen: list[httpx.Request] = []

    def upstream(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=chat_ok("ok"))

    cfg = make_config()
    res = ui.probe_endpoint(cfg, cfg.model("w-one"), transport=httpx.MockTransport(upstream))
    assert res == ui.Probe(True, "ok")
    (req,) = seen
    assert str(req.url) == f"{LOCAL_URL}/chat/completions"
    body = json.loads(req.content)
    assert body["model"] == "w-one-up"  # the proxy rewrote the config name
    assert body["max_tokens"] == ui.PROBE_MAX_TOKENS


def test_probe_reports_a_refused_key_as_an_error(cfg_path: Path, fake_keyring: FakeKeyring) -> None:
    config.set_key("x", SECRET)
    with Store(paths.db_path()):
        pass

    def upstream(req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == f"Bearer {SECRET}"
        return httpx.Response(
            401, json={"error": {"message": "Invalid API key provided", "type": "auth"}}
        )

    cfg = make_config()
    res = ui.probe_endpoint(cfg, cfg.model("rev-x"), store_path=paths.db_path(),
                            transport=httpx.MockTransport(upstream))  # fmt: skip
    assert not res.ok
    assert "HTTP 401" in res.text and "Invalid API key provided" in res.text
    assert SECRET not in res.text


def test_probe_of_a_cloud_model_needs_the_database(cfg_path: Path) -> None:
    cfg = make_config()
    res = ui.probe_endpoint(
        cfg,
        cfg.model("rev-x"),
        store_path=paths.db_path(),
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
    )
    assert not res.ok and "No database yet" in res.text


def test_test_button_shows_failure_and_success(cfg_path: Path) -> None:
    results = {"w-one": ui.Probe(True, "ok"), "rev-x": ui.Probe(False, "HTTP 401: bad key")}
    c = make_client(probe=lambda cfg, ep: results[ep.name])
    r = post(c, "/tiers/endpoint/test", {"name": "rev-x"})
    assert r.status_code == 502 and "Test of rev-x FAILED: HTTP 401: bad key" in r.text
    r = post(c, "/tiers/endpoint/test", {"name": "w-one"})
    assert r.status_code == 200 and "Test of w-one: OK" in r.text
