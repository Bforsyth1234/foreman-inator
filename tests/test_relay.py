"""Webhook relay client (todo #33): verify, nudge only, reconnect, never crash."""

from __future__ import annotations

import json
import threading

import httpx
import pytest

from foreman_inator.github import relay
from foreman_inator.setup import manifest

SECRET = "test-secret-" + "x" * 8  # generated at test time, not a real key


def smee_message(
    body: dict,
    *,
    secret: str | None = SECRET,
    event: str = "issues",
    delivery: str = "72d3162e-cc78-11e3-81ab-4c9367dc0958",
) -> str:
    """One SSE ``data`` payload exactly as smee.io sends it."""
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
    data = {
        "host": "smee.io",
        "x-github-event": event,
        "x-github-delivery": delivery,
        "content-type": "application/json",
        "body": body,
        "query": {},
        "timestamp": 1,
    }
    if secret is not None:
        data["x-hub-signature-256"] = relay.sign(secret, raw)
    return json.dumps(data)


def issue_body(repo: str = "you/project") -> dict:
    return {"action": "labeled", "repository": {"full_name": repo}, "issue": {"title": "é <x>"}}


# ---------------------------------------------------------------- signature


def test_github_test_vector():
    # https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries
    expected = "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"
    assert relay.sign("It's a Secret to Everybody", b"Hello, World!") == expected
    assert relay.verify_signature("It's a Secret to Everybody", b"Hello, World!", expected)


@pytest.mark.parametrize(
    "header",
    [None, "", "sha1=abc", "sha256=" + "0" * 64, "sha256=ZZ", relay.sign("other", b"Hello")],
)
def test_bad_signatures_rejected(header):
    assert not relay.verify_signature(SECRET, b"Hello", header)


def test_empty_secret_never_verifies():
    assert not relay.verify_signature("", b"x", relay.sign("", b"x"))


def test_verified_delivery_returns_repo_only():
    got = relay.verified_delivery(smee_message(issue_body()), SECRET)
    assert got == relay.Delivery(
        repo="you/project", event="issues", delivery_id="72d3162e-cc78-11e3-81ab-4c9367dc0958"
    )


def test_unsigned_wrong_key_and_tampered_dropped():
    assert relay.verified_delivery(smee_message(issue_body(), secret=None), SECRET) is None
    assert relay.verified_delivery(smee_message(issue_body(), secret="nope"), SECRET) is None
    data = json.loads(smee_message(issue_body()))
    data["body"]["repository"]["full_name"] = "evil/repo"
    assert relay.verified_delivery(json.dumps(data), SECRET) is None


def test_raw_body_from_self_hosted_relay_is_tried_first():
    raw = '{"repository": {"full_name": "you/project"}}'  # not compact: smee form would fail
    data = {
        "x-github-event": "push",
        "x-hub-signature-256": relay.sign(SECRET, raw.encode()),
        "raw_body": raw,
        "body": json.loads(raw),
    }
    assert relay.verified_delivery(json.dumps(data), SECRET).repo == "you/project"


def test_repo_comes_from_the_raw_body_that_verified_not_the_unsigned_body():
    raw = '{"repository": {"full_name": "you/a"}}'  # what the signature covers
    data = {
        "x-github-event": "push",
        "x-github-delivery": "d-1",
        "x-hub-signature-256": relay.sign(SECRET, raw.encode()),
        "raw_body": raw,
        "body": {"repository": {"full_name": "you/b"}},  # unsigned: must not be read
    }
    assert relay.verified_delivery(json.dumps(data), SECRET).repo == "you/a"
    stream = sse_stream(json.dumps(data))
    client, _, nudged = make_client([httpx.Response(200, content=stream)],
                                    repos=["you/a", "you/b"])  # fmt: skip
    client.run()
    assert nudged == ["you/a"]


@pytest.mark.parametrize(
    "msg",
    ["not json", "[]", smee_message({"zen": "hi"}), smee_message(issue_body("../../etc"))],
)
def test_garbage_or_repoless_dropped(msg):
    assert relay.verified_delivery(msg, SECRET) is None


def test_ping_event_ignored():
    assert relay.verified_delivery(smee_message(issue_body(), event="ping"), SECRET) is None


# ---------------------------------------------------------------- SSE parsing


def test_parse_sse_smee_stream():
    lines = [
        "id: 0",
        "event: ready",
        "data: {}",
        "",
        ": comment",
        "id: 1",
        'data: {"a":',
        "data: 1}",
        "",
        "id: 2",
        "event: ping",
        "data: {}",
        "",
    ]
    got = list(relay.parse_sse(lines))
    assert got == [
        relay.SSEMessage("ready", "{}", "0"),
        relay.SSEMessage("message", '{"a":\n1}', "1"),
        relay.SSEMessage("ping", "{}", "2"),
    ]


def test_parse_sse_skips_oversized_message():
    lines = ["data: " + "x" * 50, "", "data: ok", ""]
    assert [m.data for m in relay.parse_sse(lines, max_bytes=20)] == ["ok"]


# ---------------------------------------------------------------- client


def sse_stream(*messages: str, ready: bool = True) -> bytes:
    parts = ["id: 0\nevent: ready\ndata: {}\n\n"] if ready else []
    parts += [f"id: {i + 1}\ndata: {m}\n\n" for i, m in enumerate(messages)]
    return "".join(parts).encode()


class Script:
    """A MockTransport that plays one response per connection, then stops the client."""

    def __init__(self, responses, client_ref):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []
        self.client_ref = client_ref

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.responses:
            self.client_ref[0]._stop.set()
            return httpx.Response(503)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def make_client(responses, *, repos=(), secret_loader=lambda: SECRET, nudge=None):
    nudged: list[str] = []
    ref: list[relay.RelayClient] = []
    script = Script(responses, ref)
    client = relay.RelayClient(
        "https://smee.io/abc123",
        secret_loader,
        nudge or nudged.append,
        repos=repos,
        transport=httpx.MockTransport(script),
        backoff_start=0.0,
        backoff_max=0.0,
    )
    ref.append(client)
    return client, script, nudged


def test_client_nudges_verified_event_and_drops_forged():
    stream = sse_stream(
        smee_message(issue_body("You/Project")),
        smee_message(issue_body(), secret="forged"),
        smee_message(issue_body("other/repo")),
    )
    client, script, nudged = make_client(
        [httpx.Response(200, content=stream, headers={"content-type": "text/event-stream"})],
        repos=["you/project"],
    )
    client.run()
    assert nudged == ["you/project"]  # configured spelling; unknown repo not nudged
    assert client.rejected == 1
    assert script.requests[0].headers["accept"] == "text/event-stream"


def test_client_reconnects_after_errors_and_never_raises():
    good = httpx.Response(200, content=sse_stream(smee_message(issue_body())))
    client, script, nudged = make_client(
        [httpx.ConnectError("down"), httpx.Response(500), httpx.ReadTimeout("slow"), good]
    )
    client.run()  # returns (stopped by the script), no exception
    assert nudged == ["you/project"]
    assert len(script.requests) == 5


def test_missing_secret_does_not_connect_or_crash():
    calls = []

    def loader():
        calls.append(1)
        if len(calls) >= 3:
            client._stop.set()
        raise RuntimeError("no Keychain item")

    client, script, nudged = make_client([], secret_loader=loader)
    client.run()
    assert script.requests == [] and nudged == []


def test_nudge_exception_is_contained():
    def boom(repo):
        raise RuntimeError("worker busy")

    good = httpx.Response(200, content=sse_stream(smee_message(issue_body())))
    client, _, _ = make_client([good], nudge=boom)
    client.run()
    assert client.nudges == 1


def test_backoff_doubles_and_caps():
    waits: list[float] = []
    client = relay.RelayClient(
        "https://relay.example/x",
        lambda: SECRET,
        lambda r: None,
        transport=httpx.MockTransport(lambda req: httpx.Response(502)),
        backoff_start=1.0,
        backoff_max=4.0,
    )

    def wait(t):
        waits.append(t)
        if len(waits) >= 5:
            client._stop.set()
        return client._stop.is_set()

    client._stop.wait = wait  # type: ignore[method-assign]
    client.run()
    assert waits == [1.0, 2.0, 4.0, 4.0, 4.0]


def test_start_and_stop_thread():
    gate = threading.Event()

    def handler(req):
        gate.wait(2)
        return httpx.Response(502)

    client = relay.RelayClient(
        "https://relay.example/x",
        lambda: SECRET,
        lambda r: None,
        transport=httpx.MockTransport(handler),
    )
    t = client.start()
    assert t.daemon
    gate.set()
    client.stop(timeout=5)
    assert not t.is_alive()


def test_no_relay_configured_starts_nothing():
    assert relay.start_relay(None, "keychain:foreman-inator/x", lambda r: None) is None
    assert relay.start_relay("", None, lambda r: None) is None
    assert relay.start_relay("https://smee.io/x", None, lambda r: None) is None
    assert relay.start_relay("ftp://smee.io/x", "keychain:foreman-inator/x", print) is None


@pytest.mark.parametrize("url", ["http://smee.io/x", "ftp://a/b", "https://", "not a url"])
def test_relay_url_must_be_https(url):
    with pytest.raises(relay.RelayConfigError):
        relay.check_relay_url(url)


def test_http_loopback_relay_allowed():
    assert relay.check_relay_url("http://127.0.0.1:3000/ch") == "http://127.0.0.1:3000/ch"


def test_keychain_loader_reads_through_config(monkeypatch):
    seen = []
    monkeypatch.setattr("foreman_inator.config.resolve_key", lambda ref: seen.append(ref) or SECRET)
    assert relay.keychain_secret_loader("keychain:foreman-inator/relay-webhook-secret")() == SECRET
    assert seen == ["keychain:foreman-inator/relay-webhook-secret"]


def test_secret_and_channel_path_never_logged(caplog):
    caplog.set_level("DEBUG", logger="foreman_inator.relay")
    stream = sse_stream(smee_message(issue_body()), smee_message(issue_body(), secret="x"))
    client, _, _ = make_client([httpx.Response(200, content=stream), httpx.Response(500)])
    client.run()
    assert SECRET not in caplog.text and "abc123" not in caplog.text


# ---------------------------------------------------------------- manifest


def test_manifest_has_no_webhook_without_relay():
    built = manifest.build_manifest("http://127.0.0.1:1/callback", "x")
    assert "hook_attributes" not in built and "default_events" not in built


def test_manifest_gains_hook_only_with_relay():
    built = manifest.build_manifest(
        "http://127.0.0.1:1/callback", "x", relay_url="https://smee.io/c"
    )
    assert built["hook_attributes"] == {"url": "https://smee.io/c", "active": True}
    assert built["default_events"] == list(manifest.RELAY_EVENTS)
    assert built["default_permissions"] == manifest.PERMISSIONS
    with pytest.raises(relay.RelayConfigError):
        manifest.build_manifest("http://127.0.0.1:1/callback", "x", relay_url="http://smee.io/c")


def test_callback_server_passes_relay_url():
    with manifest.CallbackServer(app_name="x", relay_url="https://smee.io/c") as server:
        assert server.manifest["hook_attributes"]["url"] == "https://smee.io/c"


def test_convert_code_keeps_webhook_secret_out_of_repr():
    def handler(req):
        return httpx.Response(
            201,
            json={
                "id": 1,
                "slug": "s",
                "pem": "PEM",
                "owner": {"login": "you", "type": "User"},
                "webhook_secret": "whsec-test",
            },
        )

    creds = manifest.convert_code("c", transport=httpx.MockTransport(handler))
    assert creds.webhook_secret == "whsec-test"
    assert "whsec-test" not in repr(creds)


def test_a_replayed_delivery_id_is_dropped():
    msg = smee_message(issue_body(), delivery="d-1")
    stream = sse_stream(msg, msg, smee_message(issue_body(), delivery="d-2"), msg)
    client, _, nudged = make_client([httpx.Response(200, content=stream)])
    client.run()
    assert nudged == ["you/project"] * 2  # d-1 once, d-2 once
    assert client.duplicates == 2


def test_seen_delivery_ids_are_a_bounded_lru(monkeypatch):
    monkeypatch.setattr(relay, "SEEN_DELIVERIES", 2)
    ids = ["a", "b", "c", "a", "c"]  # "a" was evicted by "c"; "c" is still remembered
    stream = sse_stream(*(smee_message(issue_body(), delivery=i) for i in ids))
    client, _, nudged = make_client([httpx.Response(200, content=stream)])
    client.run()
    assert len(nudged) == 4 and client.duplicates == 1
    assert list(client._seen) == ["a", "c"]
