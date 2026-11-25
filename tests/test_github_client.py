"""GitHub App auth, the real client (against recorded HTTP fixtures) and FakeGitHub.

No test here reaches the network: the real client only ever sees ``httpx.MockTransport``.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from fakes.github import BOT_LOGIN, FakeGitHub
from foreman_inator.github import (
    WRITE_METHODS,
    Comment,
    ForbiddenOperation,
    GitHubAPI,
    GitHubClient,
    GitHubError,
    GitHubRateLimited,
    Issue,
    IssueEvent,
    PullRequest,
    Ref,
    Review,
    app_auth,
    label_applied_by,
)
from foreman_inator.github.app_auth import AppAuth, InstallationAuth

FIXTURES = Path(__file__).parent / "fixtures" / "github"
REPO = "octo/widgets"
BRANCH = "fman/issue-1/a1"
T0 = datetime(2026, 11, 23, 9, 0, 0, tzinfo=UTC).timestamp()


# --------------------------------------------------------------------------- helpers


@pytest.fixture(scope="session")
def rsa_pem() -> str:
    """A throwaway App key, generated at test time (invariant 7d: no key files in the repo)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture(scope="session")
def rsa_public(rsa_pem: str) -> Any:
    return serialization.load_pem_private_key(rsa_pem.encode(), password=None).public_key()


class FakeClock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Sleeps(list[float]):
    def __call__(self, seconds: float) -> None:
        self.append(seconds)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def token_handler(counter: list[int], clock: FakeClock, lifetime: float = 3600) -> Callable:
    def handle(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.endswith("/access_tokens"):
            counter.append(1)
            return httpx.Response(
                201,
                json={"token": f"ghs_tok{len(counter)}", "expires_at": _iso(clock() + lifetime)},
            )
        return None

    return handle


def make_client(
    rsa_pem: str,
    route: Callable[[httpx.Request], httpx.Response],
    clock: FakeClock | None = None,
    sleep: Sleeps | None = None,
) -> tuple[GitHubClient, list[int], Sleeps]:
    clock = clock or FakeClock()
    sleep = sleep if sleep is not None else Sleeps()
    tokens: list[int] = []
    tok = token_handler(tokens, clock)

    def handler(request: httpx.Request) -> httpx.Response:
        return tok(request) or route(request)

    client = GitHubClient.for_installation(
        12345, rsa_pem, 42, transport=httpx.MockTransport(handler), clock=clock, sleep=sleep
    )
    return client, tokens, sleep


# --------------------------------------------------------------------------- auth


def test_app_jwt_claims_and_signature(rsa_pem: str, rsa_public: Any) -> None:
    clock = FakeClock()
    auth = AppAuth(
        12345, rsa_pem, transport=httpx.MockTransport(lambda r: httpx.Response(500)), clock=clock
    )
    token = auth.app_jwt()
    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    claims = jwt.decode(
        token, rsa_public, algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False}
    )
    assert claims["iss"] == "12345"
    assert claims["iat"] == int(T0) - 60
    assert claims["exp"] - int(T0) <= 600  # GitHub's 10-minute cap
    assert claims["exp"] > int(T0)


def test_private_key_from_path(tmp_path: Path, rsa_pem: str, rsa_public: Any) -> None:
    pem = tmp_path / "app.pem"
    pem.write_text(rsa_pem)
    auth = AppAuth("Iv1.abc", pem, transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    claims = jwt.decode(auth.app_jwt(), rsa_public, algorithms=["RS256"])
    assert claims["iss"] == "Iv1.abc"


def test_token_request_uses_app_jwt(rsa_pem: str, rsa_public: Any) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"token": "ghs_x", "expires_at": "2026-11-23T10:00:00Z"})

    clock = FakeClock()
    app = AppAuth(12345, rsa_pem, transport=httpx.MockTransport(handler), clock=clock)
    assert InstallationAuth(app, 42).token() == "ghs_x"
    (req,) = seen
    assert req.method == "POST" and req.url.path == "/app/installations/42/access_tokens"
    assert req.headers["x-github-api-version"] == app_auth.API_VERSION
    bearer = req.headers["authorization"].removeprefix("Bearer ")
    assert jwt.decode(
        bearer, rsa_public, algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False}
    )


def test_installation_token_cached_until_one_minute_before_expiry(rsa_pem: str) -> None:
    """Non-vacuous: set EXPIRY_MARGIN_SECONDS to 0 and the refresh at T+3541 fails."""
    clock = FakeClock()
    tokens: list[int] = []
    handler = token_handler(tokens, clock)
    app = AppAuth(1, rsa_pem, transport=httpx.MockTransport(handler), clock=clock)  # type: ignore[arg-type]
    auth = InstallationAuth(app, 42)

    assert auth.token() == "ghs_tok1"  # expires at T0 + 3600
    clock.now = T0 + 3600 - 61
    assert auth.token() == "ghs_tok1" and len(tokens) == 1  # still > 1 min left: cached
    clock.now = T0 + 3600 - 59
    assert auth.token() == "ghs_tok2" and len(tokens) == 2  # inside the 1-minute margin
    clock.now = T0 + 3600 - 30
    assert auth.token() == "ghs_tok2" and len(tokens) == 2  # new token cached again


def test_401_invalidates_token_and_retries_once(rsa_pem: str) -> None:
    calls: list[str] = []

    def route(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["authorization"])
        if len(calls) == 1:
            return httpx.Response(401, json={"message": "Bad credentials"})
        return httpx.Response(200, json={"default_branch": "main"})

    client, tokens, _ = make_client(rsa_pem, route)
    assert client.get_default_branch(REPO) == "main"
    assert calls == ["Bearer ghs_tok1", "Bearer ghs_tok2"]


# --------------------------------------------------------------------------- retries


def _seq(*responses: httpx.Response) -> Callable[[httpx.Request], httpx.Response]:
    queue = list(responses)
    seen: list[httpx.Request] = []

    def route(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return queue.pop(0)

    route.seen = seen  # type: ignore[attr-defined]
    return route


OK_REPO = httpx.Response(200, json={"default_branch": "main"})


def test_retries_5xx_with_backoff(rsa_pem: str) -> None:
    route = _seq(httpx.Response(502), httpx.Response(503), OK_REPO)
    client, _, sleeps = make_client(rsa_pem, route)
    assert client.get_default_branch(REPO) == "main"
    assert sleeps == [1.0, 2.0]


def test_secondary_rate_limit_honours_retry_after(rsa_pem: str) -> None:
    limited = httpx.Response(
        403,
        headers={"retry-after": "7"},
        json={"message": "You have exceeded a secondary rate limit."},
    )
    client, _, sleeps = make_client(rsa_pem, _seq(limited, OK_REPO))
    assert client.get_default_branch(REPO) == "main"
    assert sleeps == [7.0]


def test_primary_rate_limit_waits_until_reset(rsa_pem: str) -> None:
    limited = httpx.Response(
        403,
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(T0) + 120)},
        json={"message": "API rate limit exceeded"},
    )
    client, _, sleeps = make_client(rsa_pem, _seq(limited, OK_REPO))
    assert client.get_default_branch(REPO) == "main"
    assert sleeps == [120.0]


def test_secondary_rate_limit_without_headers_waits_a_minute_then_backs_off(rsa_pem: str) -> None:
    def limited() -> httpx.Response:
        return httpx.Response(403, json={"message": "You have exceeded a secondary rate limit"})

    client, _, sleeps = make_client(rsa_pem, _seq(*(limited() for _ in range(5))))
    with pytest.raises(GitHubRateLimited):
        client.get_default_branch(REPO)
    assert sleeps == [60.0, 120.0, 240.0, 480.0]  # capped attempts, no silent loop (7e)


def test_plain_403_is_not_retried(rsa_pem: str) -> None:
    client, _, sleeps = make_client(
        rsa_pem, _seq(httpx.Response(403, json={"message": "Resource not accessible"}))
    )
    with pytest.raises(GitHubError) as err:
        client.get_default_branch(REPO)
    assert err.value.status == 403 and sleeps == []


def test_post_is_not_retried_on_5xx(rsa_pem: str) -> None:
    """A 5xx POST may have been applied; retrying could post a duplicate comment."""
    route = _seq(httpx.Response(502), httpx.Response(201))
    client, _, sleeps = make_client(rsa_pem, route)
    with pytest.raises(GitHubError):
        client.post_comment(REPO, 1, "hi")
    assert sleeps == [] and len(route.seen) == 1  # type: ignore[attr-defined]


def test_pagination_follows_link_header(rsa_pem: str) -> None:
    ev = {
        "id": 1,
        "event": "labeled",
        "actor": {"login": "brooks"},
        "label": {"name": "agent"},
        "created_at": "2026-11-23T09:00:00Z",
    }
    page2 = "https://api.github.com/repositories/300/issues/1/events?per_page=100&page=2"
    route = _seq(
        httpx.Response(
            200, json=[ev], headers={"link": f'<{page2}>; rel="next", <{page2}>; rel="last"'}
        ),
        httpx.Response(200, json=[{**ev, "id": 2, "event": "unlabeled"}]),
    )
    client, _, _ = make_client(rsa_pem, route)
    events = client.list_issue_events(REPO, 1)
    assert [e.id for e in events] == [1, 2]
    assert str(route.seen[1].url) == page2  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- invariant 7a


def test_no_merge_or_approve_surface() -> None:
    for cls in (GitHubAPI, GitHubClient, FakeGitHub):
        names = {n for n, _ in inspect.getmembers(cls, callable)}
        assert not {n for n in names if "approve" in n}, cls
        merge = {n for n in names if "merge" in n}
        assert merge <= ({"merge_pr"} if cls is FakeGitHub else set()), cls  # human helper only


@pytest.mark.parametrize("branch", ["main", "feature/x", "fmanx/y", "fman/../main"])
def test_non_fman_branches_refused_before_any_request(rsa_pem: str, branch: str) -> None:
    route = _seq()
    client, tokens, _ = make_client(rsa_pem, route)
    with pytest.raises(ForbiddenOperation):
        client.create_branch(REPO, branch, "a" * 40)
    with pytest.raises(ForbiddenOperation):
        client.update_branch(REPO, branch, "a" * 40, force=True)
    with pytest.raises(ForbiddenOperation):
        client.create_draft_pr(REPO, head=branch, base="main", title="t", body="b")
    assert route.seen == [] and tokens == []  # type: ignore[attr-defined]

    fake = FakeGitHub()
    fake.create_repo(REPO)
    with pytest.raises(ForbiddenOperation):
        fake.create_branch(REPO, branch, "a" * 40)
    with pytest.raises(ForbiddenOperation):
        fake.push_branch(REPO, branch)
    fake.assert_no_writes()


def test_label_applied_by_uses_latest_application() -> None:
    def ev(i: int, event: str, actor: str, label: str = "agent") -> IssueEvent:
        return IssueEvent(i, event, actor, label, datetime(2026, 1, 1, 0, 0, i, tzinfo=UTC))

    assert label_applied_by([ev(1, "labeled", "brooks")], "agent") == "brooks"
    assert label_applied_by([ev(1, "labeled", "brooks")], "AGENT") == "brooks"
    assert label_applied_by([ev(1, "labeled", "brooks"), ev(2, "unlabeled", "x")], "agent") is None
    assert (
        label_applied_by([ev(2, "labeled", "mallory"), ev(1, "labeled", "brooks")], "agent")
        == "mallory"
    )
    assert label_applied_by([ev(1, "labeled", "brooks", "bug")], "agent") is None


# --------------------------------------------------------------------------- recorded fixtures


class Recorded:
    """Serves tests/fixtures/github/scenario.json in order and checks each request."""

    def __init__(self) -> None:
        doc = json.loads((FIXTURES / "scenario.json").read_text())
        self.exchanges: list[dict[str, Any]] = doc["exchanges"]
        self.i = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert self.i < len(self.exchanges), (
            f"unexpected extra request {request.method} {request.url}"
        )
        x = self.exchanges[self.i]
        self.i += 1
        got = (request.method, unquote(request.url.path))
        assert got == (x["method"], x["path"]), f"exchange {self.i}: {got} != {x['path']}"
        if "query" in x:
            assert dict(parse_qsl(request.url.query.decode())) == x["query"]
        if "request_json" in x:
            assert json.loads(request.content) == x["request_json"]
        if x["path"] == "/graphql":
            payload = json.loads(request.content)
            assert "markPullRequestReadyForReview" in payload["query"]
            assert payload["variables"] == {"id": "PR_kwDOfake2"}
        if x.get("no_auth"):
            # A signed download URL (job logs, #41): our token must never be sent there.
            assert "authorization" not in request.headers
            return httpx.Response(x["status"], text=x["text"], headers=x.get("headers"))
        assert request.headers["authorization"].startswith("Bearer ")
        assert request.headers["x-github-api-version"] == app_auth.API_VERSION
        return httpx.Response(x["status"], json=x["body"], headers=x.get("headers"))

    def done(self) -> None:
        assert self.i == len(self.exchanges), f"only {self.i}/{len(self.exchanges)} requests made"


def _human_noop(*_a: Any, **_k: Any) -> None:
    return None


def _setup_fake() -> tuple[FakeGitHub, dict[str, Callable[..., Any]]]:
    """FakeGitHub in the same state the recorded fixtures describe."""
    fake = FakeGitHub()
    fake.create_repo(REPO, sha="a" * 40)
    n = fake.create_issue(REPO, "Add a --verbose flag", "Print each step.", author="brooks")
    fake.apply_label(REPO, n, "agent", by="mallory")
    fake.remove_label_as(REPO, n, "agent", by="brooks")
    fake.apply_label(REPO, n, "agent", by="brooks")
    human = {
        "approve": lambda: fake.submit_review(
            REPO,
            2,
            by="brooks",
            state="APPROVE",
            body="LGTM",
            comments=[
                {
                    "path": "src/widgets/cli.py",
                    "line": 22,
                    "body": "Use the logger's verbose helper here.",
                    "diff_hunk": "@@ -20,4 +20,6 @@ def main(argv):\n     args = parse(argv)\n"
                    "+    if args.verbose:\n+        log.setLevel(DEBUG)",
                }
            ],
        ),
        "merge": lambda: fake.merge_pr(REPO, 2, by="brooks"),
    }
    fake.add_check_run(REPO, "a" * 40, "lint", status="completed", conclusion="success")
    fake.add_check_run(
        REPO,
        "a" * 40,
        "test",
        status="completed",
        conclusion="failure",
        log="##[group]Run uv run pytest\n"
        "FAILED tests/test_cli.py::test_verbose - AssertionError\n"
        "##[error]Process completed with exit code 1.\n",
    )
    return fake, human


VOLATILE = {
    "id",
    "created_at",
    "updated_at",
    "submitted_at",
    "node_id",
    "commit_id",
    "html_url",
    # wave 6b: commit shas and CI ids/urls/times differ between the fixture and the fake
    "head_sha",
    "job_id",
    "details_url",
    "started_at",
    "completed_at",
    "review_id",
    # wave 15 (#24): the merge commit's sha (the 2026-11-23 recording predates the field)
    "merge_commit_sha",
}


def norm(value: Any) -> Any:
    """Dataclass -> (class name, stable fields): ids and timestamps differ, shape must not."""
    if isinstance(value, list):
        return [norm(v) for v in value]
    if dataclasses.is_dataclass(value):
        fields = {}
        for f in dataclasses.fields(value):
            v = getattr(value, f.name)
            if f.name in VOLATILE:
                fields[f.name] = type(v).__name__
            elif f.name in {"closed_at", "merged_at"}:
                fields[f.name] = v is not None
            else:
                fields[f.name] = v
        return (type(value).__name__, fields)
    return value


def scenario(api: GitHubAPI, human: dict[str, Callable[..., Any]]) -> list[Any]:
    """One poll -> attempt -> review -> merge cycle, exercising every GitHubAPI method."""
    out: list[Any] = []
    tok = api.token()
    out.append(isinstance(tok, str) and len(tok) > 0)
    out.append(api.get_default_branch(REPO))
    issues = api.list_issues(REPO, label="agent")
    out.append(norm(issues))
    events = api.list_issue_events(REPO, 1)
    out.append([(e.event, e.actor, e.label) for e in events])
    out.append(label_applied_by(events, "agent"))
    sha = api.get_branch_sha(REPO, "main")
    out.append(norm(api.create_branch(REPO, BRANCH, sha)))
    pr = api.create_draft_pr(
        REPO, head=BRANCH, base="main", title="Add a --verbose flag (#1)", body="Closes #1"
    )
    out.append(norm(pr))
    out.append(norm(api.update_pr(REPO, pr.number, body="Closes #1\n\nAttempt 1")))
    out.append(
        norm(api.post_review(REPO, pr.number, "AI review: approve\n\nAll five checks passed."))
    )
    out.append(norm(api.mark_ready_for_review(REPO, pr.number)))
    human["approve"]()
    reviews = api.list_reviews(REPO, pr.number)
    out.append(norm(reviews))
    out.append(api.add_labels(REPO, 1, ["fman:working"]))
    out.append(api.remove_label(REPO, 1, "fman:working"))
    out.append(api.remove_label(REPO, 1, "fman:working"))  # already gone: idempotent
    out.append(norm(api.post_comment(REPO, 1, "Started on tier 1.")))
    out.append(norm(api.list_issue_comments(REPO, 1)))
    out.append(norm(api.find_pr_for_branch(REPO, BRANCH)))
    human["merge"]()
    out.append(norm(api.get_pr(REPO, pr.number)))
    out.append(norm(api.get_issue(REPO, 1)))
    out.append(norm(api.close_issue(REPO, 1)))
    # wave 6b (#41): CI on the PR head and the failing job's log
    runs = api.list_check_runs(REPO, sha)
    out.append(norm(runs))
    red = next(r for r in runs if r.conclusion == "failure")
    out.append(api.get_job_log_tail(REPO, red.job_id))
    # wave 6b (#42): line comments of the human review
    out.append(norm(api.list_review_comments(REPO, pr.number, reviews[-1].id)))
    return out


def test_fake_and_real_client_share_the_interface(rsa_pem: str) -> None:
    """Same calls, same dataclasses, same answers: FakeGitHub vs recorded GitHub HTTP."""
    recorded = Recorded()
    client = GitHubClient.for_installation(
        12345,
        rsa_pem,
        42,
        transport=httpx.MockTransport(recorded),
        clock=FakeClock(),
        sleep=Sleeps(),
        bot_login=BOT_LOGIN,
    )
    real = scenario(client, {"approve": _human_noop, "merge": _human_noop})
    recorded.done()

    fake, human = _setup_fake()
    from_fake = scenario(fake, human)

    assert isinstance(client, GitHubAPI) and isinstance(fake, GitHubAPI)
    for i, (a, b) in enumerate(zip(real, from_fake, strict=True)):
        assert a == b, f"step {i}: real={a!r} fake={b!r}"

    # spot checks on content, so a shared bug in both cannot pass silently
    assert real[4] == "brooks"
    assert real[6][1]["draft"] is True and real[6][1]["author"] == BOT_LOGIN
    assert real[8][1]["state"] == "COMMENTED"
    assert real[9][1]["draft"] is False
    assert [r[1]["state"] for r in real[10]] == ["COMMENTED", "APPROVED"]
    assert real[17][1]["merged"] is True
    assert real[18][1]["state"] == "closed" and real[18][1]["state_reason"] == "completed"
    assert [(r[1]["name"], r[1]["conclusion"], r[1]["app_slug"]) for r in real[20]] == [
        ("lint", "success", "github-actions"),
        ("test", "failure", "github-actions"),
    ]
    assert "FAILED tests/test_cli.py" in real[21] and "2026-11-23T" not in real[21]
    assert [(c[1]["path"], c[1]["line"], c[1]["author"]) for c in real[22]] == [
        ("src/widgets/cli.py", 22, "brooks")
    ]


def test_protocol_signatures_match() -> None:
    """Every GitHubAPI method exists on both with the same parameters."""
    for name, proto in inspect.getmembers(GitHubAPI, inspect.isfunction):
        if name.startswith("_"):
            continue
        want = list(inspect.signature(proto).parameters)
        for cls in (GitHubClient, FakeGitHub):
            assert list(inspect.signature(getattr(cls, name)).parameters) == want, (cls, name)
    assert {n for n, _ in inspect.getmembers(GitHubAPI, inspect.isfunction)} >= WRITE_METHODS


def test_dataclass_types_are_shared() -> None:
    fake, human = _setup_fake()
    assert type(fake.get_issue(REPO, 1)) is Issue
    assert type(fake.list_issue_events(REPO, 1)[0]) is IssueEvent
    assert type(fake.post_comment(REPO, 1, "x")) is Comment
    assert type(fake.create_branch(REPO, BRANCH, "a" * 40)) is Ref
    pr = fake.create_draft_pr(REPO, head=BRANCH, base="main", title="t", body="b")
    assert type(pr) is PullRequest
    assert type(fake.post_review(REPO, pr.number, "r")) is Review


# --------------------------------------------------------------------------- the fake itself


def test_fake_records_every_write_and_can_forbid_them() -> None:
    fake, _ = _setup_fake()
    fake.assert_no_writes()  # human setup is not a bot write
    sha = fake.get_branch_sha(REPO, "main")
    fake.list_issues(REPO, label="agent")
    fake.assert_no_writes()  # reads are not writes

    fake.create_branch(REPO, BRANCH, sha)
    pr = fake.create_draft_pr(REPO, head=BRANCH, base="main", title="t", body="Closes #1")
    fake.update_pr(REPO, pr.number, body="Closes #1 v2")
    fake.update_branch(REPO, BRANCH, "b" * 40, force=True)
    fake.post_review(REPO, pr.number, "looks fine")
    fake.mark_ready_for_review(REPO, pr.number)
    fake.add_labels(REPO, 1, ["x"])
    fake.remove_label(REPO, 1, "x")
    fake.post_comment(REPO, 1, "hi")
    fake.close_issue(REPO, 1)
    assert {c.method for c in fake.writes} == WRITE_METHODS
    with pytest.raises(AssertionError):
        fake.assert_no_writes()

    fake.forbid_writes("replays never write")
    with pytest.raises(AssertionError, match="replays never write"):
        fake.post_review(REPO, pr.number, "shadow verdict")
    assert len(fake.writes_named("post_review")) == 1


def test_fake_human_flow_and_label_actor() -> None:
    fake = FakeGitHub()
    fake.create_repo(REPO)
    n = fake.create_issue(REPO, "Fix it", author="stranger")
    fake.apply_label(REPO, n, "agent", by="stranger")
    assert label_applied_by(fake.list_issue_events(REPO, n), "agent") == "stranger"
    fake.apply_label(REPO, n, "agent", by="brooks")  # already applied: no new event (as GitHub)
    assert label_applied_by(fake.list_issue_events(REPO, n), "agent") == "stranger"

    fake.push_branch(REPO, BRANCH)
    pr = fake.create_draft_pr(REPO, head=BRANCH, base="main", title="t", body="Fixes #1")
    with pytest.raises(GitHubError):
        fake.merge_pr(REPO, pr.number)  # drafts cannot be merged
    with pytest.raises(GitHubError):  # the bot cannot approve its own PR
        fake.submit_review(REPO, pr.number, by=BOT_LOGIN, state="APPROVE")
    fake.submit_review(REPO, pr.number, by="brooks", state="REQUEST_CHANGES", body="kickback")
    fake.apply_label(REPO, pr.number, "kickback:wrong", by="brooks")
    assert "kickback:wrong" in fake.get_issue(REPO, pr.number).labels
    assert fake.list_issues(REPO, label="kickback:wrong") == []  # PRs are not issues
    fake.mark_ready_for_review(REPO, pr.number)
    fake.merge_pr(REPO, pr.number)
    assert fake.get_issue(REPO, n).state == "closed"
    assert fake.get_pr(REPO, pr.number).merged

    m = fake.create_issue(REPO, "Never mind", labels=["agent"])
    fake.close_issue_as(REPO, m, by="brooks")
    assert fake.get_issue(REPO, m).state_reason == "not_planned"


def test_fake_fail_next() -> None:
    fake, _ = _setup_fake()
    fake.fail_next("get_issue")
    with pytest.raises(GitHubError):
        fake.get_issue(REPO, 1)
    assert fake.get_issue(REPO, 1).number == 1


def test_fake_since_filters() -> None:
    fake, _ = _setup_fake()
    first = fake.post_comment(REPO, 1, "a")
    second = fake.post_comment(REPO, 1, "b")
    assert fake.list_issue_comments(REPO, 1, since=second.updated_at) == [second]
    assert first in fake.list_issue_comments(REPO, 1)
    later = fake.clock()
    assert fake.list_issues(REPO, since=later) == []
