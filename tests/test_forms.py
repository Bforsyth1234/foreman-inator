"""The shared POST protection (``metrics/forms.py``, todo #67): CSRF token plus the page's
own loopback Origin/Host; 403 and the handler never runs otherwise."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.testclient import TestClient

from foreman_inator.metrics import forms

BASE = "http://127.0.0.1:8642"
TOKEN = "t0ken-for-tests"


@pytest.fixture
def calls() -> list[dict[str, str]]:
    return []


@pytest.fixture
def client(calls: list[dict[str, str]]) -> TestClient:
    guard = forms.FormGuard(TOKEN)

    def handler(request: Request, form: dict[str, str]) -> PlainTextResponse:
        calls.append(form)
        return PlainTextResponse("changed")

    # No LoopbackHostOnly around it: the guard checks Host on its own.
    return TestClient(Starlette(routes=[guard.post_route("/do", handler)]), base_url=BASE)


def post(client: TestClient, data: Any = None, **headers: str) -> Any:
    headers.setdefault("origin", BASE)
    return client.post("/do", data=data if data is not None else {}, headers=headers)


def test_a_good_post_runs_the_handler_without_the_token_field(client, calls) -> None:
    r = post(client, {"csrf_token": TOKEN, "x": "1"})
    assert r.status_code == 200 and r.text == "changed"
    assert calls == [{"x": "1"}]


def test_a_post_with_a_foreign_origin_is_refused_and_changes_nothing(client, calls) -> None:
    for origin in ("http://evil.example", "http://127.0.0.1:9999", "https://127.0.0.1:8642"):
        r = post(client, {"csrf_token": TOKEN}, origin=origin)
        assert r.status_code == 403, origin
        assert "another origin" in r.text
    assert calls == []


def test_a_post_without_the_token_or_with_a_wrong_one_is_refused(client, calls) -> None:
    assert post(client, {"x": "1"}).status_code == 403
    assert post(client, {"csrf_token": "guess"}).status_code == 403
    assert post(client, {"csrf_token": ""}).status_code == 403
    assert calls == []


def test_no_origin_falls_back_to_the_referer_and_neither_is_refused(client, calls) -> None:
    ok = client.post("/do", data={"csrf_token": TOKEN}, headers={"referer": f"{BASE}/x?y=1"})
    assert ok.status_code == 200
    bad = client.post("/do", data={"csrf_token": TOKEN}, headers={"referer": "http://e.example/"})
    assert bad.status_code == 403
    assert client.post("/do", data={"csrf_token": TOKEN}).status_code == 403
    assert post(client, {"csrf_token": TOKEN}, origin="null").status_code == 403
    assert len(calls) == 1


def test_a_non_loopback_host_is_refused_even_with_a_matching_origin(calls) -> None:
    guard = forms.FormGuard(TOKEN)
    app = Starlette(routes=[guard.post_route("/do", lambda r, f: calls.append(f))])
    c = TestClient(app, base_url="http://rebound.example:8642")
    r = c.post("/do", data={"csrf_token": TOKEN}, headers={"origin": "http://rebound.example:8642"})
    assert r.status_code == 403 and calls == []


def test_cross_site_fetch_metadata_is_refused(client, calls) -> None:
    r = post(client, {"csrf_token": TOKEN}, **{"sec-fetch-site": "cross-site"})
    assert r.status_code == 403
    assert (
        post(client, {"csrf_token": TOKEN}, **{"sec-fetch-site": "same-origin"}).status_code == 200
    )
    assert len(calls) == 1


def test_only_small_urlencoded_forms_are_read(client, calls) -> None:
    r = client.post(
        "/do",
        content=f"csrf_token={TOKEN}",
        headers={"origin": BASE, "content-type": "text/plain"},
    )
    assert r.status_code == 415
    big = {"csrf_token": TOKEN, "x": "a" * (forms.MAX_FORM_BYTES + 1)}
    assert post(client, big).status_code == 413
    assert calls == []


def test_get_on_a_post_route_is_not_allowed(client, calls) -> None:
    assert client.get("/do").status_code == 405
    assert calls == []


def test_the_token_is_per_process_random_and_rendered_escaped() -> None:
    assert forms.default_guard() is forms.default_guard()
    assert forms.FormGuard().token != forms.FormGuard().token
    assert len(forms.default_guard().token) >= 32
    g = forms.FormGuard('a"b<c')
    assert 'value="a&quot;b&lt;c"' in g.hidden_field()
    html = g.form("/x?a=<b>", '<input name="k">', button="<Go>")
    assert 'action="/x?a=&lt;b&gt;"' in html and "&lt;Go&gt;" in html
    assert 'method="post"' in html and g.hidden_field() in html


def test_check_origin_on_plain_headers() -> None:
    forms.check_origin({"host": "localhost:8642", "origin": "http://LOCALHOST:8642"})
    forms.check_origin({"host": "[::1]:8642", "origin": "http://[::1]:8642"})
    with pytest.raises(forms.Refused):
        forms.check_origin({"host": "localhost:8642", "origin": "http://127.0.0.1:8642"})
    with pytest.raises(forms.Refused):
        forms.check_origin({"origin": BASE})
