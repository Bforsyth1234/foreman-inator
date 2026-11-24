"""Shadow reviewer (todo #26): stored, never posted; failures swallowed; scored live.

Fixture PRs are tiny git repos under ``tmp_path`` (as in ``test_reviewer.py``), fetched
into a real ``RepoMirror``; the models are ``FakeModels`` behind the real ``ModelProxy``;
GitHub is ``FakeGitHub`` with the real ``Publisher`` posting the champion's review the way
the worker does. Nothing reaches a real model or GitHub.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fakes.github import FakeGitHub
from fakes.models import FakeModels
from foreman_inator.config import Config, ModelEndpoint, SpendCaps
from foreman_inator.db.store import Attempt, Experiment, Kickback, Review, Store, Ticket
from foreman_inator.experiments import shadow as S
from foreman_inator.metrics import queries as Q
from foreman_inator.pr.publisher import Publisher
from foreman_inator.proxy.server import ModelProxy
from foreman_inator.review import reviewer as rv
from foreman_inator.runner.worktree import RepoMirror, _base_env
from foreman_inator.spend import Spend

LOCAL = "http://127.0.0.1:11434/v1"
WRITER = ModelEndpoint("qwen-small", 1, "local", "qwen3.6:35b-a3b", LOCAL, "qwen")
CHAMPION = ModelEndpoint("glm-review", 3, "local", "glm-5.3", LOCAL, "glm")
CHALLENGER = ModelEndpoint("inkling-review", 3, "local", "inkling-2", LOCAL, "inkling")
SAME_AS_WRITER = ModelEndpoint("qwen-review", 3, "local", "qwen3.8:27b", LOCAL, "qwen")
CLOUD_CHALLENGER = ModelEndpoint(
    "claude-review",
    3,
    "cloud",
    "claude-x",
    "https://api.example.test/v1",
    "claude",
    "keychain:foreman-inator/claude-review",  # never resolved: the proxy gets a fake resolver
)
ENDPOINTS = [WRITER, CHAMPION, CHALLENGER, SAME_AS_WRITER, CLOUD_CHALLENGER]
PROMPT = "reviewer-v1"
REPO = "acme/demo"
ISSUE = rv.IssueText(1, "Add a mul function", "calc.ops needs mul(a, b) returning a * b.")
APPROVE = json.dumps({"verdict": "approve", "tag": None, "note": "Does what the issue asks."})
REQUEST = json.dumps({"verdict": "request_changes", "tag": "wrong", "note": "Wanted div."})
NOW = datetime(2026, 11, 23, 9, 0, tzinfo=UTC)

BASE_FILES = {
    "pyproject.toml": '[project]\nname = "calc"\nversion = "0.1.0"\n\n'
    '[tool.pytest.ini_options]\npythonpath = ["."]\n',
    "calc/__init__.py": "",
    "calc/ops.py": "def add(a, b):\n    return a + b\n",
    "tests/test_ops.py": (
        "from calc.ops import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    ),
}
GOOD = {
    "calc/ops.py": "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n",
    "tests/test_ops.py": BASE_FILES["tests/test_ops.py"]
    + "\n\ndef test_mul():\n    from calc.ops import mul\n\n    assert mul(2, 3) == 6\n",
}
VACUOUS = {
    "calc/ops.py": "def add(a, b):\n    if a is None:\n        a = 0\n    return a + b\n",
    "tests/test_ops.py": BASE_FILES["tests/test_ops.py"]
    + "\n\ndef test_add_none():\n    assert add(1, 2) == 3\n",
}
SLOW = {
    "calc/ops.py": GOOD["calc/ops.py"],
    "tests/test_slow.py": "import time\n\n\ndef test_slow():\n    time.sleep(20)\n",
}


# --------------------------------------------------------------------------- fixtures


def _git(cwd: Path, *args: str) -> str:
    env = _base_env(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        }
    )
    out = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


@pytest.fixture
def make_pr(tmp_path: Path):
    counter = iter(range(1, 100))

    def make(change: dict[str, str]) -> rv.ChangeRef:
        n = next(counter)
        src = tmp_path / f"origin{n}"
        src.mkdir()
        _git(src, "init", "-q", "-b", "trunk")
        _write(src, BASE_FILES)
        _git(src, "add", "-A")
        _git(src, "commit", "-q", "-m", "base")
        _git(src, "checkout", "-q", "-b", "fman/issue-1/a1")
        _write(src, change)
        _git(src, "add", "-A")
        _git(src, "commit", "-q", "-m", "change")
        mirror = RepoMirror(tmp_path / "worktrees", f"acme/demo{n}", str(src))
        mirror.ensure()
        return rv.ChangeRef(
            mirror, mirror.resolve("trunk"), mirror.resolve("origin/fman/issue-1/a1")
        )

    return make


class Hook:
    """A spend hook that records each estimate's bucket and can refuse one bucket."""

    def __init__(self) -> None:
        self.buckets: list[str] = []
        self.refuse: str | None = None

    def __call__(self, endpoint, cost_usd, context, *, phase="estimate"):
        if phase == "estimate":
            self.buckets.append(context.bucket)
            return context.bucket != self.refuse
        return True


@pytest.fixture
def models():
    return FakeModels()


@pytest.fixture
def hook():
    return Hook()


@pytest.fixture
def proxy(models, hook):
    p = ModelProxy(
        ENDPOINTS, transport=models.transport, spend_hook=hook, key_resolver=lambda ep: "sk-fake"
    )
    p.start()
    yield p
    p.stop()


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "fman.db", clock=lambda: NOW)
    yield s
    s.close()


@pytest.fixture
def gh():
    fake = FakeGitHub()
    fake.create_repo(REPO, default_branch="trunk")
    return fake


@pytest.fixture
def ticket(store, gh):
    """A ticket in ``testing`` with a draft PR opened by the bot, and one attempt."""
    gh.create_issue(REPO, ISSUE.title, ISSUE.body)
    gh.push_branch(REPO, "fman/issue-1/a1")
    pr = gh.create_draft_pr(REPO, head="fman/issue-1/a1", base="trunk", title="t", body="b")
    t = store.create_ticket(
        repo=REPO, issue_number=1, title=ISSUE.title, request=ISSUE.body, base_branch="trunk"
    )
    store.set_column(t.id, "testing")
    t = store.update_ticket(t.id, pr_number=pr.number)
    a = store.add_attempt(t.id, model=WRITER.name, tier=1, trigger="first")
    store.finish_attempt(a.id, tests_passed=True)
    return store.get_ticket(t.id), store.get_attempt(a.id)


def _variant(ep: ModelEndpoint) -> str:
    return S.variant_id_for(ep, PROMPT)


def _champion(store, change, attempt, **kw) -> rv.ReviewOutcome:
    """The live review the way the worker runs it: review, then record_outcome."""
    out = rv.review_change(
        issue=ISSUE,
        change=change,
        writer=WRITER,
        reviewer=CHAMPION,
        proxy=kw.pop("proxy"),
        attempt_id=attempt.id,
        ticket_id=attempt.ticket_id,
        python=sys.executable,
        timeout=50,
    )
    return rv.record_outcome(store, attempt.id, out)


def _shadow(store, change, attempt, proxy, **kw) -> S.ShadowResult:
    challenger = kw.pop("challenger", CHALLENGER)
    kw.setdefault("python", sys.executable)
    kw.setdefault("timeout", 50)
    return S.shadow_review(
        store=store,
        issue=kw.pop("issue", ISSUE),
        change=change,
        writer=kw.pop("writer", WRITER),
        challenger=challenger,
        challenger_prompt=kw.pop("prompt", PROMPT),
        challenger_variant=kw.pop("variant", None) or _variant(challenger),
        proxy=proxy,
        attempt_id=attempt.id,
        **kw,
    )


def _caller_finalize(publisher: Publisher, t: Ticket, result: S.ShadowResult) -> None:
    """What a careless caller (#29) might do with a result: post whatever body it got."""
    body = getattr(result, "comment_body", None)
    if body and result.review is not None:
        publisher.post_ai_review(t, result.review.id, body)


# --------------------------------------------------------------------------- never posted


def test_shadow_verdict_is_stored_and_never_posted(store, gh, ticket, make_pr, proxy, models, hook):
    t, att = ticket
    change = make_pr(GOOD)
    publisher = Publisher(gh, store)
    models.queue_reply(APPROVE)
    champ = _champion(store, change, att, proxy=proxy)
    assert champ.verdict == "approve"
    publisher.post_ai_review(t, champ.review.id, champ.comment_body)  # the worker's post
    assert len(gh.writes_named("post_review")) == 1
    writes_before = list(gh.writes)

    models.queue_reply(REQUEST)  # the challenger disagrees
    result = _shadow(store, change, att, proxy)
    _caller_finalize(publisher, t, result)

    assert result.status == "stored", result.reason
    assert result.review.shadow is True and result.verdict == "request_changes"
    assert result.review.variant_id == _variant(CHALLENGER)
    # Stored: one counted review and one shadow review on the attempt.
    assert [r.shadow for r in store.list_reviews(attempt_id=att.id)] == [False, True]
    # Never posted, never marked ready, no kickback, nothing on the attempt or ticket.
    assert gh.writes == writes_before
    assert len(gh.list_reviews(REPO, t.pr_number)) == 1
    assert gh.writes_named("mark_ready_for_review") == []
    assert gh.get_pr(REPO, t.pr_number).draft is True
    assert store.list_kickbacks(attempt_id=att.id) == []
    assert store.get_attempt(att.id).outcome is None
    assert store.get_ticket(t.id).column == "testing"
    # Nothing a caller could post: no comment body anywhere in the result.
    assert not hasattr(result, "comment_body")
    assert rv.COMMENT_MARKER not in repr(result) and "AI review" not in repr(result)
    # The shadow's model call was costed to the experiments bucket.
    assert hook.buckets == ["live", "experiments"]


def test_shadow_module_cannot_reach_github():
    src = Path(S.__file__).read_text()
    imported = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    banned = ("foreman_inator.github", "foreman_inator.pr", "httpx")
    assert not any(m == b or m.startswith(b + ".") for m in imported for b in banned)
    assert not any("worker" in m for m in imported)


def test_challenger_sees_exactly_the_champions_inputs(store, ticket, make_pr, proxy, models):
    _, att = ticket
    change = make_pr(GOOD)
    models.queue_reply(APPROVE)
    _champion(store, change, att, proxy=proxy)
    models.queue_reply(APPROVE)
    assert _shadow(store, change, att, proxy).status == "stored"
    champ_req, shadow_req = (r.json for r in models.requests)
    assert champ_req["messages"] == shadow_req["messages"]
    assert (champ_req["model"], shadow_req["model"]) == (CHAMPION.model, CHALLENGER.model)


def test_launch_gets_shadow_experiments_bucket_and_challenger_prompt(store, ticket):
    _, att = ticket
    store.record_review(att.id, reviewer_model=CHAMPION.name, verdict="approve", variant_id="c")
    seen: dict = {}
    change = object()

    def launch(**kw):
        seen.update(kw)
        raise RuntimeError("stop here")

    result = _shadow(store, change, att, proxy=None, launch=launch)
    assert result.status == "skipped" and result.code == "error"
    assert seen["shadow"] is True and seen["bucket"] == "experiments"
    assert seen["prompt_name"] == PROMPT and seen["reviewer"] is CHALLENGER
    assert seen["writer"] is WRITER and seen["issue"] is ISSUE and seen["change"] is change
    assert seen["store"] is None and seen["ticket_id"] == att.ticket_id


# --------------------------------------------------------------------------- never in the way


def test_family_conflict_is_skipped_recorded_and_runs_nothing(store, ticket, proxy, models):
    _, att = ticket
    store.record_review(att.id, reviewer_model=CHAMPION.name, verdict="approve", variant_id="c")
    calls: list = []
    result = _shadow(
        store, object(), att, proxy, challenger=SAME_AS_WRITER, launch=lambda **k: calls.append(k)
    )
    assert result.status == "skipped" and result.code == "family_conflict"
    assert result.reason.startswith("family_conflict: ")
    assert calls == [] and models.requests == []
    assert store.list_reviews(attempt_id=att.id, shadow=True) == []
    assert S.recorded_skip(store, REPO, _variant(SAME_AS_WRITER), att.id).startswith(
        "family_conflict"
    )


def test_runs_only_after_the_champion_is_stored(store, ticket, make_pr, proxy, models):
    _, att = ticket
    change = make_pr(GOOD)
    first = _shadow(store, change, att, proxy)
    assert first.status == "skipped" and first.code == "champion_not_stored"
    assert models.requests == []
    assert S.recorded_skip(store, REPO, _variant(CHALLENGER), att.id) is None  # retried later
    models.queue_reply(APPROVE)
    _champion(store, change, att, proxy=proxy)
    models.queue_reply(APPROVE)
    assert _shadow(store, change, att, proxy).status == "stored"


def test_experiments_budget_refusal_is_skipped_and_never_pauses_the_ticket(
    store, ticket, make_pr, proxy, models, hook
):
    _, att = ticket
    change = make_pr(GOOD)
    models.queue_reply(APPROVE)
    _champion(store, change, att, proxy=proxy)
    hook.refuse = "experiments"
    result = _shadow(store, change, att, proxy, challenger=CLOUD_CHALLENGER)
    assert result.status == "skipped" and result.code == "experiments_budget"
    assert store.list_reviews(attempt_id=att.id, shadow=True) == []
    assert len(models.requests) == 1  # only the champion's call went upstream


def test_spent_experiments_budget_skips_before_any_check_runs(store, ticket, tmp_path):
    """With a real ``Spend``: budget 0 -> refused up front; the live ticket is not paused."""
    _, att = ticket
    store.record_review(att.id, reviewer_model=CHAMPION.name, verdict="approve", variant_id="c")
    spend = Spend(store, SpendCaps(experiments_monthly_usd=0.0), clock=lambda: NOW)
    calls: list = []
    result = _shadow(
        store,
        object(),
        att,
        None,
        challenger=CLOUD_CHALLENGER,
        spend=spend,
        launch=lambda **k: calls.append(k),
    )
    assert result.code == "experiments_budget" and calls == []
    assert spend.refusal_for_attempt(att.id) is None


def test_experiments_refusal_in_spend_is_not_a_live_refusal(store, ticket, make_pr, models):
    """The real proxy + real Spend: the shadow's refused call leaves no refusal that the
    worker's ``spend_refusal`` would pause the ticket for (``needs_approval`` False)."""
    _, att = ticket
    store.record_review(att.id, reviewer_model=CHAMPION.name, verdict="approve", variant_id="c")
    spend = Spend(store, SpendCaps(experiments_monthly_usd=0.0), clock=lambda: NOW)
    p = ModelProxy(
        ENDPOINTS,
        transport=models.transport,
        spend_hook=spend.hook,
        key_resolver=lambda ep: "sk-fake",
        store=store,
    )
    p.start()
    try:
        result = _shadow(store, make_pr(GOOD), att, p, challenger=CLOUD_CHALLENGER)
    finally:
        p.stop()
    assert result.code == "experiments_budget"
    assert models.requests == []
    r = spend.refusal_for_attempt(att.id)
    assert r is not None and r.bucket == "experiments" and not r.needs_approval


def test_timeout_is_skipped_and_recorded(store, ticket, make_pr, proxy, models):
    _, att = ticket
    store.record_review(att.id, reviewer_model=CHAMPION.name, verdict="approve", variant_id="c")
    result = _shadow(store, make_pr(SLOW), att, proxy, timeout=2)
    assert result.status == "skipped" and result.code == "timeout"
    assert result.seconds < 15
    assert models.requests == []
    assert S.recorded_skip(store, REPO, _variant(CHALLENGER), att.id).startswith("timeout")


def test_model_error_is_skipped_not_stored_as_a_verdict(store, ticket, make_pr, proxy, models):
    _, att = ticket
    change = make_pr(GOOD)
    models.queue_reply(APPROVE)
    _champion(store, change, att, proxy=proxy)
    models.fail_next(500, {"error": {"message": "boom", "code": "upstream"}})
    result = _shadow(store, change, att, proxy)
    assert result.code == "model_error"
    assert store.list_reviews(attempt_id=att.id, shadow=True) == []


def test_malformed_twice_is_the_challengers_verdict(store, ticket, make_pr, proxy, models):
    _, att = ticket
    change = make_pr(GOOD)
    models.queue_reply(APPROVE)
    _champion(store, change, att, proxy=proxy)
    models.queue_reply("not json")
    models.queue_reply("still not json")
    result = _shadow(store, change, att, proxy)
    assert result.status == "stored" and result.verdict == "handed_to_user"


def test_any_exception_is_swallowed(store, ticket):
    _, att = ticket
    store.record_review(att.id, reviewer_model=CHAMPION.name, verdict="approve", variant_id="c")

    def boom(**kw):
        raise OSError("disk on fire")

    result = _shadow(store, object(), att, None, launch=boom)
    assert result.status == "skipped" and "disk on fire" in result.reason
    # Even a broken store call is swallowed (attempt does not exist).
    ghost = _shadow(store, object(), Attempt(**{**_attempt_kw(), "id": 999}), None)
    assert ghost.status == "skipped" and ghost.code == "error"


def test_cancel_is_not_recorded_and_runs_again(store, ticket, make_pr, proxy, models):
    _, att = ticket
    change = make_pr(GOOD)
    models.queue_reply(APPROVE)
    _champion(store, change, att, proxy=proxy)
    cancel = threading.Event()
    cancel.set()
    result = _shadow(store, change, att, proxy, cancel=cancel)
    assert result.status == "cancelled"
    assert S.recorded_skip(store, REPO, _variant(CHALLENGER), att.id) is None
    assert store.list_reviews(attempt_id=att.id, shadow=True) == []
    models.queue_reply(APPROVE)
    assert _shadow(store, change, att, proxy).status == "stored"


def test_idempotent_after_a_stored_review_or_a_skip(store, ticket, make_pr, proxy, models):
    _, att = ticket
    change = make_pr(GOOD)
    models.queue_reply(APPROVE)
    _champion(store, change, att, proxy=proxy)
    models.queue_reply(APPROVE)
    first = _shadow(store, change, att, proxy)
    n = len(models.requests)
    again = _shadow(store, change, att, proxy, launch=lambda **k: pytest.fail("ran twice"))
    assert again.status == "stored" and again.review.id == first.review.id
    assert len(models.requests) == n
    skipped = _shadow(store, change, att, proxy, challenger=SAME_AS_WRITER)
    assert skipped.code == "family_conflict"
    assert _shadow(store, change, att, proxy, challenger=SAME_AS_WRITER).reason == skipped.reason


def test_no_model_step_when_checks_decided_the_champion(store, ticket, make_pr, proxy, models):
    """A champion verdict from checks 1-4 alone: the challenger would run the same code to
    the same verdict, so the shadow does not rerun the suite."""
    _, att = ticket
    change = make_pr(VACUOUS)
    champ = _champion(store, change, att, proxy=proxy)
    assert champ.read_diff == "skipped" and champ.verdict == "request_changes"
    result = _shadow(
        store, change, att, proxy, champion=champ, launch=lambda **k: pytest.fail("reran")
    )
    assert result.code == "no_model_step" and models.requests == []


def test_variant_mismatch_and_challenger_is_champion(store, ticket):
    _, att = ticket
    store.record_review(
        att.id, reviewer_model=CHAMPION.name, verdict="approve", variant_id=_variant(CHAMPION)
    )
    stale = _shadow(store, object(), att, None, variant=f"{CHALLENGER.name}/{PROMPT}@00000000")
    assert stale.code == "variant_mismatch"
    same = _shadow(store, object(), att, None, challenger=CHAMPION)
    assert same.code == "challenger_is_champion"
    missing = _shadow(
        store, object(), att, None, prompt="reviewer-v999", variant="x/reviewer-v999@1"
    )
    assert missing.code == "prompt_missing"


def test_shadow_rows_stay_out_of_the_metrics_of_record(store, ticket, make_pr, proxy, models):
    t, att = ticket
    change = make_pr(GOOD)
    models.queue_reply(APPROVE)
    _champion(store, change, att, proxy=proxy)
    store.set_attempt_outcome(att.id, "kicked_back")
    store.record_kickback(att.id, by="user", tag="wrong", note="no")
    before = Q.reviewer_miss_rate(store)
    models.queue_reply(APPROVE)
    assert _shadow(store, change, att, proxy).status == "stored"
    assert Q.reviewer_miss_rate(store) == before
    assert store.get_attempt(att.id).tokens_in == 0  # shadow usage not on the writer


# --------------------------------------------------------------------------- the challenger


def test_find_challenger_takes_the_oldest_running_shadow_experiment(store):
    tier2 = ModelEndpoint("qwen-mid", 2, "local", "qwen3.8:27b", LOCAL, "qwen")
    cfg = Config(models=(*ENDPOINTS, tier2), reviewer_models=(CHAMPION.name,))
    assert S.find_challenger(store, cfg) is None
    store.create_experiment(
        role="reviewer", champion_variant="c", challenger_variant="x/p@1", mode="replay"
    )
    store.create_experiment(
        role="writer", champion_variant="c", challenger_variant="y/p@1", mode="shadow"
    )
    assert S.find_challenger(store, cfg) is None
    v1 = f"{CHALLENGER.name}/{PROMPT}@abcdef12"
    e1 = store.create_experiment(
        role="reviewer", champion_variant="c", challenger_variant=v1, mode="shadow"
    )
    e2 = store.create_experiment(
        role="reviewer",
        champion_variant="c",
        challenger_variant=f"{CLOUD_CHALLENGER.name}/{PROMPT}@12345678",
        mode="shadow",
    )
    found = S.find_challenger(store, cfg)
    assert found == S.Challenger(e1.id, v1, CHALLENGER, PROMPT)
    assert [e.id for e in S.running_shadow_experiments(store)] == [e1.id, e2.id]
    store.end_experiment(e1.id, "rejected")
    assert S.find_challenger(store, cfg).experiment_id == e2.id
    store.end_experiment(e2.id, "promoted")
    store.create_experiment(
        role="reviewer", champion_variant="c", challenger_variant="nope/p@1", mode="shadow"
    )
    assert S.find_challenger(store, cfg) is None  # model not configured


def test_parse_variant_id():
    assert S.parse_variant_id("hf.co/org/m/reviewer-v2@abcd1234") == (
        "hf.co/org/m",
        "reviewer-v2",
        "abcd1234",
    )
    for bad in ("reviewer-v1@abc", "m/reviewer-v1", "m/@abc"):
        with pytest.raises(ValueError):
            S.parse_variant_id(bad)


# --------------------------------------------------------------------------- scoring

CH, CO = "chal/reviewer-v2@1", "champ/reviewer-v1@1"
EXP = Experiment(1, "reviewer", CO, CH, "shadow", None, "running", NOW, None, None)


def _ticket(i: int, column="testing", accepted=None) -> Ticket:
    return Ticket(
        i, "t", "r", REPO, "trunk", i, i, (), column, "issue", None, None, accepted,
        NOW, None, 1, None, None,
    )  # fmt: skip


def _attempt_kw() -> dict:
    return dict(
        id=1, ticket_id=1, model="w", tier=1, variant_id=None, trigger="first", worktree=None,
        branch=None, tests_passed=True, build_passed=None, diff_files=1, diff_lines=1,
        tokens_in=0, tokens_out=0, cost_usd=0.0, seconds=None, outcome=None, started_at=NOW,
        ended_at=None, base_commit=None, verify_output_path=None,
    )  # fmt: skip


def _attempt(i: int, outcome=None) -> Attempt:
    return Attempt(**{**_attempt_kw(), "id": i, "ticket_id": i, "outcome": outcome})


_rid = iter(range(1, 10_000))


def _review(aid: int, verdict: str, *, shadow: bool, cost=0.0, at=NOW) -> Review:
    return Review(
        next(_rid), aid, "m", CH if shadow else CO, verdict, True, None, True, None, None,
        None, 0, 0, cost, at, shadow,
    )  # fmt: skip


def _kick(aid: int, by="user") -> Kickback:
    return Kickback(next(_rid), aid, by, "wrong", "n", None, NOW, None)


def _case(cases):
    """cases: (attempt_id, user, counted verdict | None, shadow verdict | None)."""
    tickets, attempts, reviews, kicks = [], [], [], []
    for aid, user, c, s in cases:
        column = {"merged": "done", "dropped": "dropped"}.get(user, "testing")
        tickets.append(_ticket(aid, column, aid if user == "merged" else None))
        outcome = {"merged": "accepted", "kicked_back": "kicked_back", "ai": "kicked_back"}
        attempts.append(_attempt(aid, outcome.get(user)))
        if user == "kicked_back":
            kicks.append(_kick(aid))
        if user == "ai":
            kicks.append(_kick(aid, by="reviewer"))
        if c:
            reviews.append(_review(aid, c, shadow=False, cost=0.0))
        if s:
            reviews.append(_review(aid, s, shadow=True, cost=0.02))
    return dict(tickets=tickets, attempts=attempts, reviews=reviews, kickbacks=kicks)


def test_score_rows_joins_reviews_to_the_users_decision():
    rows = _case(
        [
            (1, "merged", "approve", "approve"),  # both right
            (2, "merged", "approve", "request_changes"),  # challenger false kickback
            (3, "kicked_back", "approve", "request_changes"),  # champion miss, challenger catch
            (4, "kicked_back", "approve", "approve"),  # both miss
            (5, "pending", "approve", "approve"),  # undecided
            (6, "ai", "request_changes", "approve"),  # user never saw it
            (7, "merged", "handed_to_user", "approve"),  # champion no verdict
            (8, "merged", "approve", None),  # champion only, skipped
            (9, "merged", "approve", None),  # champion only, never run
            (10, "pending", None, "approve"),  # challenger only
            (11, "dropped", "approve", "approve"),  # excluded
        ]
    )
    comp = S.score_rows(EXP, **rows, skips={8: "family_conflict: same family"})
    ch, co = comp.challenger, comp.champion
    assert comp.paired == 7
    assert (ch.reviews, ch.decided, ch.undecided) == (7, 5, 2)
    assert (ch.correct_approvals, ch.catches, ch.misses, ch.false_kickbacks) == (2, 1, 1, 1)
    assert ch.agreement == pytest.approx(3 / 5)
    assert (co.correct_approvals, co.catches, co.misses, co.false_kickbacks) == (2, 0, 2, 0)
    assert co.no_verdict == 1 and co.agreement == pytest.approx(2 / 5)
    assert ch.cost_usd == pytest.approx(0.14) and ch.cost_per_review == pytest.approx(0.02)
    assert comp.champion_only == 2
    assert comp.champion_only_reasons == {"family_conflict": 1, "not_run": 1}
    assert comp.challenger_only == 1 and comp.dropped == 1
    assert comp.judgments == {"merged": 3, "kicked_back": 2, "pending": 1, "not_judged": 1}
    assert comp.counted_by_variant[CO].reviews == 9  # every counted review, paired or not


def test_score_rows_respects_the_experiment_window():
    exp = Experiment(1, "reviewer", CO, CH, "shadow", None, "promoted", NOW, NOW, None)
    late = NOW + timedelta(days=1)
    rows = _case([(1, "merged", "approve", "approve")])
    rows["reviews"].append(_review(1, "approve", shadow=True, at=late))
    rows["reviews"].append(_review(2, "approve", shadow=False, at=late))
    comp = S.score_rows(exp, **rows)
    assert comp.paired == 1 and comp.champion_only == 0 and comp.challenger_only == 0


def _comparison(right: int, wrong: int, *, champ_right: int, cost=0.0, champ_cost=0.0):
    ch = S.VariantScore(CH, reviews=right + wrong, decided=right + wrong, correct_approvals=right,
                        false_kickbacks=wrong, cost_usd=cost * (right + wrong))  # fmt: skip
    co = S.VariantScore(
        "counted",
        reviews=right + wrong,
        decided=right + wrong,
        correct_approvals=champ_right,
        misses=right + wrong - champ_right,
        cost_usd=champ_cost * (right + wrong),
    )
    return S.ShadowComparison(1, CO, CH, co, ch, {}, right + wrong, 0, {}, 0, 0, {})


def test_judge_live_needs_the_minimum_sample_then_applies_spec_rule():
    assert S.MIN_LIVE_SAMPLE == 30
    small = _comparison(20, 0, champ_right=10)
    assert S.judge_live(small).status == "insufficient"
    assert S.judge_live(small, min_sample=20).status == "holds"
    tie = _comparison(25, 5, champ_right=25, cost=0.01, champ_cost=0.01)
    assert S.judge_live(tie).status == "holds"  # "not below", "not higher"
    worse = S.judge_live(_comparison(24, 6, champ_right=25))
    assert worse.status == "fails" and "agreement" in worse.reasons[0]
    pricier = S.judge_live(_comparison(28, 2, champ_right=25, cost=0.05, champ_cost=0.01))
    assert pricier.status == "fails" and "cost per review" in pricier.reasons[0]
    with pytest.raises(ValueError):
        S.judge_live(small, min_sample=0)


def test_write_comparison_stores_the_result_on_the_experiment(
    store, ticket, make_pr, proxy, models
):
    t, att = ticket
    exp = store.create_experiment(
        role="reviewer",
        champion_variant=_variant(CHAMPION),
        challenger_variant=_variant(CHALLENGER),
        mode="shadow",
    )
    change = make_pr(GOOD)
    models.queue_reply(APPROVE)
    _champion(store, change, att, proxy=proxy)
    models.queue_reply(REQUEST)
    assert _shadow(store, change, att, proxy, experiment_id=exp.id).status == "stored"
    store.set_attempt_outcome(att.id, "accepted")
    store.set_accepted_attempt(t.id, att.id)
    store.set_column(t.id, "done")
    comp, judgment = S.write_comparison(store, exp.id, min_sample=1)
    assert comp.paired == 1 and comp.challenger.false_kickbacks == 1
    assert comp.champion.correct_approvals == 1
    assert judgment.status == "fails"
    saved = store.get_experiment(exp.id)
    assert saved.status == "running"  # never ends the experiment
    note = json.loads(saved.result_note)
    assert note["kind"] == "shadow-v1" and note["live"]["status"] == "fails"
    assert note["challenger"]["false_kickbacks"] == 1 and note["paired"] == 1
    with pytest.raises(ValueError):
        replay = store.create_experiment(
            role="reviewer", champion_variant="a", challenger_variant="b", mode="replay"
        )
        S.score_experiment(store, replay.id)


def test_user_judgment_rules():
    kb = [_kick(1)]
    assert S.user_judgment(_ticket(1), _attempt(1, "kicked_back"), kb) == "kicked_back"
    assert S.user_judgment(_ticket(1, "done", 1), _attempt(1, "accepted"), []) == "merged"
    assert S.user_judgment(_ticket(1, "dropped"), _attempt(1, "kicked_back"), kb) == "dropped"
    assert S.user_judgment(_ticket(1), _attempt(1, "superseded"), []) == "not_judged"
    assert S.user_judgment(_ticket(1, "done", 2), _attempt(1), []) == "not_judged"
    assert S.user_judgment(_ticket(1), _attempt(1), []) == "pending"
