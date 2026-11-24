"""Replay sets and replay runs (todo #25).

Everything is real except the outside world: a real ``Store``, real git repos under
``tmp_path`` (a bare "GitHub remote", the worker's mirror of it), the real
``OpenCodeDriver`` running ``FakeOpenCode``'s shim, the real ``ModelProxy`` with the real
``Spend`` caps as its hook in front of ``FakeModels``, the real reviewer. ``FakeGitHub``
(writes forbidden) stands where the worker's GitHub client would be.
"""

from __future__ import annotations

import ast
import hashlib
import itertools
import json
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fde_kit.prompts import PromptRef

from fakes.github import FakeGitHub
from fakes.models import FakeModels, Reply
from fakes.opencode import FakeOpenCode, Script, edit, hang, say
from fakes.worker_env import proxy_call
from foreman_inator.config import Config, SpendCaps
from foreman_inator.db.store import Store
from foreman_inator.experiments import replay_runner as rr
from foreman_inator.experiments import replay_sets as rs
from foreman_inator.proxy.server import ModelProxy
from foreman_inator.review.reviewer import MODEL_MAX_TOKENS, REVIEWER_PROMPT_NAME
from foreman_inator.runner import sandbox as sb
from foreman_inator.runner.sandbox import SeatbeltBackend
from foreman_inator.runner.worktree import GitIdentity, Worktree, WorktreeManager
from foreman_inator.spend import Spend
from foreman_inator.tickets.prompt import load_writer_template

REPO = "acme/widgets"
VERIFY = "sh run_tests.sh"
RUN_TESTS = 'for f in tests/test_*.sh; do [ -f "$f" ] || continue; sh "$f" || exit 1; done\n'
SEED = {
    "GIT_AUTHOR_NAME": "Seed",
    "GIT_AUTHOR_EMAIL": "seed@example.invalid",
    "GIT_COMMITTER_NAME": "Seed",
    "GIT_COMMITTER_EMAIL": "seed@example.invalid",
}
LIVE_TABLES = ("tickets", "attempts", "reviews", "kickbacks")
FAKES = Path(__file__).parent / "fakes"
CHALLENGER_MARKER = "CHALLENGER-PROMPT-MARKER"


def git(*args: str, cwd: Path) -> str:
    env = (
        {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        | SEED
        | {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    )
    p = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


# =========================================================================== the world


class World:
    """A repo with merged tickets in it, as the live flow leaves them."""

    def __init__(self, tmp: Path, *, caps: SpendCaps | None = None, sandbox: str = "off"):
        self.tmp = tmp
        self.remote = tmp / "remote.git"
        git("init", "-q", "--bare", "-b", "main", str(self.remote), cwd=tmp)
        self.seed = tmp / "seed"
        self.seed.mkdir()
        git("init", "-q", "-b", "main", cwd=self.seed)
        (self.seed / "README.md").write_text("# widgets\n")
        (self.seed / "run_tests.sh").write_text(RUN_TESTS)
        (self.seed / "tests").mkdir()
        (self.seed / "tests" / "test_base.sh").write_text("exit 0\n")
        git("add", "-A", cwd=self.seed)
        git("commit", "-q", "-m", "seed", cwd=self.seed)
        git("remote", "add", "origin", str(self.remote), cwd=self.seed)
        git("push", "-q", "origin", "main", cwd=self.seed)
        t0 = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
        ticks = itertools.count()
        self.store = Store(tmp / "fman.db", clock=lambda: t0 + timedelta(seconds=next(ticks)))
        self.store.set_verify_command(REPO, VERIFY)
        self.worktrees = WorktreeManager(tmp / "worktrees", identity=GitIdentity())
        self.mirror = self.worktrees.mirror(REPO, str(self.remote))
        self.issue = 0
        self.local_branches: dict[str, str] = {}
        self.recordings = tmp / "recordings"
        self.config = Config(
            app_id=1, repos=(REPO,), spend_caps=caps or SpendCaps(), sandbox=sandbox
        )
        self.models = FakeModels()
        self.models.responder = self.responder
        self.review_rule: Any = lambda model, text: "approve"
        self.spend = Spend(self.store, self.config)
        self.proxy = ModelProxy(
            self.config,
            store=self.store,
            transport=self.models.transport,
            spend_hook=self.spend.hook,
            key_resolver=lambda ep: "fake-test-key",
        )
        self.proxy.start()
        self.oc = FakeOpenCode(tmp / "oc", Script([say("idle")]))
        self.driver = ScriptedDriver(self.oc.driver(timeout=30)).bind(self.oc)
        self.gh = FakeGitHub()
        self.gh.create_repo(REPO, default_branch="main", sha=self.head())
        self.gh.forbid_writes("replays never write to GitHub (#25)")

    # -- git -----------------------------------------------------------------

    def head(self) -> str:
        return git("rev-parse", "HEAD", cwd=self.seed)

    def branch(self, name: str, files: dict[str, str], start: str) -> str:
        git("checkout", "-q", "-B", name, start, cwd=self.seed)
        for path, text in files.items():
            f = self.seed / path
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        git("add", "-A", cwd=self.seed)
        git("commit", "-q", "-m", f"foreman-inator: {name}", cwd=self.seed)
        sha = self.head()
        git("push", "-q", "origin", name, cwd=self.seed)
        git("checkout", "-q", "main", cwd=self.seed)
        return sha

    def merge(self, branch: str, pr: int, how: str) -> None:
        if how == "merge":
            git("merge", "-q", "--no-ff", "-m", f"Merge pull request #{pr} from acme/{branch}",
                branch, cwd=self.seed)  # fmt: skip
        else:
            git("merge", "-q", "--squash", branch, cwd=self.seed)
            git("commit", "-q", "-m", f"Add the feature (#{pr})", cwd=self.seed)
        git("push", "-q", "origin", "main", cwd=self.seed)

    def sync(self) -> None:
        """The worker's mirror as the live flow leaves it: fetched, plus the local attempt
        branches that still exist."""
        self.mirror.ensure()
        for name, sha in self.local_branches.items():
            git("update-ref", f"refs/heads/{name}", sha, cwd=self.mirror.mirror_path)

    def record_head(self, ticket_id: int, attempt_id: int, sha: str) -> None:
        d = self.recordings / str(ticket_id)
        d.mkdir(parents=True, exist_ok=True)
        rows = [{"kind": "header", "run_id": "x"}, {"kind": "result", "head_commit": sha}]
        (d / f"{attempt_id}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    # -- tickets -------------------------------------------------------------

    def merged_ticket(
        self,
        key: str,
        *,
        how: str = "merge",
        kickback: str | None = None,
        kicked_head: str = "branch",
        merge: bool = True,
        tests: bool = True,
        model: str = "inkling-2-mini",
    ) -> int:
        self.issue += 1
        n = self.gh.create_issue(REPO, f"Add feature {key}", "...", author="brooks")
        assert n == self.issue
        pr = 100 + n
        base = self.head()
        s = self.store
        t = s.create_ticket(
            repo=REPO,
            issue_number=n,
            title=f"Add feature {key}",
            request=f"Create feature_{key}.txt containing {key}-ok.",
            base_branch="main",
        )
        s.update_ticket(t.id, pr_number=pr)
        ordinal = 1
        test = {f"tests/test_{key}.sh": f"grep -q {key}-ok feature_{key}.txt\n"} if tests else {}
        if kickback:
            b1 = f"fman/issue-{n}/a1"
            sha1 = self.branch(
                b1,
                {
                    f"feature_{key}.txt": f"{key}-half TODO\n",
                    f"tests/test_{key}.sh": f"grep -q {key} feature_{key}.txt\n",
                },
                base,
            )
            a1 = s.add_attempt(t.id, model=model, tier=1, trigger="first", branch=b1,
                               base_commit=base)  # fmt: skip
            s.finish_attempt(a1.id, "kicked_back", tests_passed=True)
            s.record_kickback(a1.id, by="user", tag=kickback, note="Not finished.")
            if kicked_head == "branch":
                self.local_branches[b1] = sha1
            elif kicked_head == "recording":
                self.record_head(t.id, a1.id, sha1)
            ordinal = 2
        b = f"fman/issue-{n}/a{ordinal}"
        self.branch(b, {f"feature_{key}.txt": f"{key}-ok\n", **test}, base)
        a = s.add_attempt(t.id, model=model, tier=1, trigger="kickback" if kickback else "first",
                          branch=b, base_commit=base)  # fmt: skip
        s.finish_attempt(a.id, None, tests_passed=True)
        s.set_accepted_attempt(t.id, a.id)
        s.set_column(t.id, "done")
        if merge:
            self.merge(b, pr, how)
        else:
            git("branch", "-q", "-D", b, cwd=self.seed)
        return t.id

    # -- models --------------------------------------------------------------

    def responder(self, body: dict[str, Any]) -> Any:
        if body.get("max_tokens") == MODEL_MAX_TOKENS:  # the reviewer's check 5
            text = json.dumps(body.get("messages"))
            verdict = self.review_rule(body.get("model"), text)
            if verdict == "approve":
                return Reply(json.dumps({"verdict": "approve", "tag": None, "note": "Fine."}))
            return Reply(
                json.dumps({"verdict": "request_changes", "tag": "incomplete", "note": "TODO."})
            )
        return None

    # -- replay plumbing -------------------------------------------------------

    def deps(self, *, spend: bool = True, sandbox: rr.SandboxFactory | None = None):
        return rr.ReplayDeps(
            store=self.store,
            proxy=self.proxy,
            mirror_for=lambda repo: self.mirror if repo == REPO else None,
            sandbox=sandbox or rr.config_sandbox(self.config, self.tmp / "sbx"),
            work_dir=self.tmp / "replay-work",
            driver=self.driver,
            endpoint_for=self.config.model,
            spend=self.spend if spend else None,
            recordings_dir=self.recordings,
        )

    def live_snapshot(self) -> dict[str, list[tuple]]:
        return {t: [tuple(r) for r in self.store.query(f"SELECT * FROM {t} ORDER BY id")]
                for t in LIVE_TABLES}  # fmt: skip

    def remote_refs(self) -> str:
        return git("for-each-ref", "--format=%(refname) %(objectname)", cwd=self.remote)

    def leftovers(self) -> list[str]:
        scratch = sorted(p.name for p in self.mirror.repo_dir.glob("scratch-*"))
        branches = git("for-each-ref", "--format=%(refname)", "refs/heads/fman/replay",
                       cwd=self.mirror.mirror_path)  # fmt: skip
        return scratch + [b for b in branches.splitlines() if b]

    def close(self) -> None:
        self.proxy.stop()
        self.store.close()


class SimulatedKill(BaseException):
    """Not an Exception: nothing in the runner catches it, like SIGKILL."""


class ScriptedDriver:
    """The real fake-backed OpenCodeDriver; the script for each run is picked from the
    prompt (which variant's template, which issue) by ``choose``."""

    def __init__(self, inner: Any):
        self.inner = inner
        self.runs: list[dict[str, Any]] = []
        self.choose: Any = lambda prompt, endpoint: Script([say("idle")])
        self.kill_on: int | None = None

    def run(self, **kw: Any) -> Any:
        self.runs.append(kw)
        if self.kill_on is not None and len(self.runs) == self.kill_on:
            raise SimulatedKill("killed mid-case")
        self.inner_set(self.choose(kw["prompt"], kw["endpoint"]))
        return self.inner.run(**kw)

    def inner_set(self, script: Script) -> None:
        self._oc.set_script(script)

    def bind(self, oc: FakeOpenCode) -> ScriptedDriver:
        self._oc = oc
        return self


def key_of(prompt: str) -> str:
    for line in prompt.splitlines():
        if "Add feature " in line:
            return line.split("Add feature ", 1)[1].strip()[0]
    raise AssertionError("no issue title in the prompt")


def script(kind: str, k: str, *, model_call: bool = True) -> Script:
    call = [proxy_call()] if model_call else []
    feature = f"feature_{k}.txt"
    return Script(
        {
            "ok": [*call, edit(feature, f"{k}-ok\n"), say("Done.")],
            "wrong": [*call, edit(feature, f"{k}-wrong\n"), say("Done.")],
            # wrong code, and the test edited to pass: the merged test must still win
            "tamper": [
                *call,
                edit(feature, f"{k}-wrong\n"),
                edit(f"tests/test_{k}.sh", "exit 0\n"),
                say("Done."),
            ],  # fmt: skip
            "none": [*call, say("I could not do it.")],
            "hang": [hang()],
        }[kind]
    )


def challenger_template() -> PromptRef:
    base = load_writer_template()
    text = base.text.replace(
        "<!-- section: intro -->\n", f"<!-- section: intro -->\n{CHALLENGER_MARKER}\n", 1
    )
    assert text != base.text
    return PromptRef("writer-v2", hashlib.sha256(text.encode()).hexdigest(), text)


def writer_pair(w: World, *, challenger_model: str = "inkling-2-mini"):
    champ_t = load_writer_template()
    chall_t = challenger_template()
    champ = rr.WriterVariant(
        f"inkling-2-mini/{champ_t.name}@{champ_t.sha256[:8]}", w.config.model("inkling-2-mini"),
        champ_t,
    )  # fmt: skip
    chall = rr.WriterVariant(
        f"{challenger_model}/{chall_t.name}@{chall_t.sha256[:8]}",
        w.config.model(challenger_model),
        chall_t,
    )
    return champ, chall


def plan(outcomes: dict[str, dict[str, str]]):
    def choose(prompt: str, endpoint: Any) -> Script:
        side = "challenger" if CHALLENGER_MARKER in prompt else "champion"
        return script(outcomes[side][key_of(prompt)], key_of(prompt))

    return choose


@pytest.fixture
def world(tmp_path: Path):
    w = World(tmp_path)
    yield w
    w.close()


def three_merges(w: World) -> None:
    w.merged_ticket("a")
    w.merged_ticket("b", how="squash")
    w.merged_ticket("c")
    w.sync()


KNOWN = {
    "champion": {"a": "ok", "b": "tamper", "c": "none"},
    "challenger": {"a": "ok", "b": "ok", "c": "wrong"},
}


# =========================================================================== sets


def test_writer_set_holds_the_merged_tests_and_records_skips(world: World) -> None:
    w = world
    a = w.merged_ticket("a")  # merge commit
    b = w.merged_ticket("b", how="squash")  # squash merge
    c = w.merged_ticket("c", merge=False)  # PR not on main, no branch: cannot be found
    d = w.merged_ticket("d", tests=False)  # merged change has no test files
    e = w.merged_ticket("e")
    w.store.update_attempt(w.store.get_ticket(e).accepted_attempt_id, base_commit="f" * 40)
    w.store.set_verify_command(REPO, VERIFY)
    w.sync()
    before = w.live_snapshot()
    rset = rs.build_replay_set(w.store, "writer", mirror_for=lambda r: w.mirror)
    assert (rset.case_count, rset.skipped_count) == (2, 3)
    cases = rs.list_cases(w.store, rset.id, include_skipped=True)
    by_ticket = {sc.ticket_id: sc for sc in cases}
    ca = by_ticket[a].case
    assert isinstance(ca, rs.WriterCase)
    assert ca.merged_via == "merge" and ca.verify_command == VERIFY
    assert ca.title == "Add feature a" and ca.request.startswith("Create feature_a.txt")
    assert (
        ca.base_commit == w.store.get_attempt(w.store.get_ticket(a).accepted_attempt_id).base_commit
    )
    assert ca.test_files == (rs.MergedTest("tests/test_a.sh", "grep -q a-ok feature_a.txt\n"),)
    assert by_ticket[b].case.merged_via == "squash"
    assert [t.path for t in by_ticket[b].case.test_files] == ["tests/test_b.sh"]
    assert "not in the mirror" in by_ticket[c].skip_reason
    assert by_ticket[d].skip_reason == "the merged change added or modified no test files"
    assert by_ticket[e].skip_reason == "base commit ffffffff is no longer in the mirror"
    assert w.live_snapshot() == before
    # survives a restart and can be listed
    w.store.close()
    w.store = Store(w.tmp / "fman.db")
    assert [s.id for s in rs.list_sets(w.store, "writer")] == [rset.id]
    again = rs.list_cases(w.store, rset.id)
    assert [sc.case for sc in again] == [by_ticket[a].case, by_ticket[b].case]


def test_writer_case_falls_back_to_the_branch_or_the_recording(world: World) -> None:
    w = world
    t1 = w.merged_ticket("a", merge=False)
    t2 = w.merged_ticket("b", merge=False)
    a1 = w.store.get_attempt(w.store.get_ticket(t1).accepted_attempt_id)
    a2 = w.store.get_attempt(w.store.get_ticket(t2).accepted_attempt_id)
    sha1 = git("rev-parse", f"origin/{a1.branch}", cwd=w.seed)
    sha2 = git("rev-parse", f"origin/{a2.branch}", cwd=w.seed)
    w.local_branches[a1.branch] = sha1
    w.record_head(t2, a2.id, sha2)
    w.sync()
    rset = rs.build_replay_set(w.store, "writer", mirror_for=lambda r: w.mirror,
                               recordings_dir=w.recordings)  # fmt: skip
    vias = [sc.case.merged_via for sc in rs.list_cases(w.store, rset.id)]
    assert vias == ["branch", "recording"]


def test_reviewer_set_scores_against_the_users_merges_and_kickbacks(world: World) -> None:
    w = world
    a = w.merged_ticket("a", kickback="incomplete", kicked_head="branch")
    b = w.merged_ticket("b", kickback="wrong", kicked_head="recording")
    c = w.merged_ticket("c", kickback="broke", kicked_head="gone")
    w.sync()
    rset = rs.build_replay_set(w.store, "reviewer", mirror_for=lambda r: w.mirror,
                               recordings_dir=w.recordings)  # fmt: skip
    cases = rs.list_cases(w.store, rset.id, include_skipped=True)
    got = [
        (sc.ticket_id, sc.status, sc.case and (sc.case.truth, sc.case.tag, sc.case.head_via))
        for sc in cases
    ]
    assert got == [
        (a, "ok", ("kicked_back", "incomplete", "branch")),
        (a, "ok", ("merged", None, "merge")),
        (b, "ok", ("kicked_back", "wrong", "recording")),
        (b, "ok", ("merged", None, "merge")),
        (c, "skipped", None),
        (c, "ok", ("merged", None, "merge")),
    ]
    skipped = [sc for sc in cases if sc.status == "skipped"]
    assert "head commit of attempt" in skipped[0].skip_reason
    kicked = cases[0].case
    assert git("show", f"{kicked.head_commit}:feature_a.txt", cwd=w.seed) == "a-half TODO"


def test_a_set_keeps_the_latest_merges_and_is_rebuilt_monthly(world: World) -> None:
    w = world
    ids = [w.merged_ticket(k) for k in "abcd"]
    w.sync()
    mf = lambda r: w.mirror  # noqa: E731
    s1 = rs.ensure_current_set(w.store, "writer", mirror_for=mf, max_merges=2)
    assert [sc.ticket_id for sc in rs.list_cases(w.store, s1.id)] == ids[2:]
    same_month = s1.built_at + timedelta(days=5)
    assert rs.ensure_current_set(w.store, "writer", mirror_for=mf, max_merges=2,
                                 now=same_month).id == s1.id  # fmt: skip
    next_month = s1.built_at + timedelta(days=40)
    s2 = rs.ensure_current_set(w.store, "writer", mirror_for=mf, max_merges=2, now=next_month)
    assert s2.id != s1.id and rs.latest_set(w.store, "writer").id == s2.id
    assert rs.rebuild_due(None, next_month)


# =========================================================================== writer replay


def test_writer_replay_known_outcomes_give_the_expected_comparison(world: World) -> None:
    """Verify bullet 1: a fake champion/challenger pair with known outcomes."""
    w = world
    three_merges(w)
    champ, chall = writer_pair(w)
    w.driver.choose = plan(KNOWN)
    before = w.live_snapshot()
    c = rr.run_replay(w.deps(), role="writer", champion=champ, challenger=chall,
                      settings=rr.ReplaySettings(min_cases=3))  # fmt: skip
    statuses = [(r.side, r.status) for r in rr.results(w.store, c.experiment_id)]
    assert statuses == [
        ("challenger", "passed"), ("champion", "passed"),
        ("challenger", "passed"), ("champion", "failed"),  # champion tampered with the test
        ("challenger", "failed"), ("champion", "no_change"),
    ]  # fmt: skip
    assert (c.wins, c.losses, c.ties, c.scored_cases) == (1, 0, 2, 3)
    assert (c.champion.passed, c.challenger.passed) == (1, 2)
    assert c.verdict == "wins" and c.state == "finished" and c.stop_reason is None
    exp = w.store.get_experiment(c.experiment_id)
    assert (exp.mode, exp.role, exp.status) == ("replay", "writer", "running")
    assert (exp.champion_variant, exp.challenger_variant) == (champ.variant_id, chall.variant_id)
    note = exp.result_note
    assert "Verdict: challenger wins on replay" in note
    assert "Per case: 1 win(s), 0 loss(es), 2 tie(s)." in note
    assert "champion 1/3, challenger 2/3" in note
    assert "must still hold up live" in note
    # the writer saw the variant's prompt, built from the case, for each case and side
    assert len(w.driver.runs) == 6
    assert sum(CHALLENGER_MARKER in r["prompt"] for r in w.driver.runs) == 3
    assert all(r["attempt_id"] < 0 and "/c/" in r["proxy_base_url"] for r in w.driver.runs)
    # model calls went through the proxy; nothing reached the live tables
    assert len(w.models.requests) == 6
    assert w.live_snapshot() == before
    assert w.leftovers() == []
    assert rr.compare(w.store, c.experiment_id, min_cases=3) == c  # stable re-read
    st = rr.replay_status(w.store, c.experiment_id, min_cases=3)
    assert (st.state, st.done_sides, st.total_sides) == ("finished", 6, 6)
    # a second call finds nothing to resume: a new run, same outcomes, same comparison
    c2 = rr.run_replay(w.deps(), role="writer", champion=champ, challenger=chall,
                       settings=rr.ReplaySettings(min_cases=3))  # fmt: skip
    assert c2.experiment_id != c.experiment_id
    assert (c2.wins, c2.losses, c2.ties, c2.verdict) == (1, 0, 2, "wins")


def test_replays_never_call_the_github_write_api(world: World, monkeypatch) -> None:
    """Verify bullet 2. FakeGitHub forbids writes (a write would raise) and records any;
    the remote's refs stay as they were (nothing pushed), no Worktree push is tried, and
    the replay modules import no GitHub or PR code at all."""
    w = world
    three_merges(w)
    w.merged_ticket("d", kickback="incomplete")
    w.sync()
    pushes: list[Any] = []
    real_push = Worktree.push
    monkeypatch.setattr(Worktree, "push", lambda self, *a, **k: pushes.append(self.path)
                        or real_push(self, *a, **k))  # fmt: skip
    refs = w.remote_refs()
    champ, chall = writer_pair(w)
    w.driver.choose = plan(KNOWN | {"champion": KNOWN["champion"] | {"d": "ok"},
                                    "challenger": KNOWN["challenger"] | {"d": "ok"}})  # fmt: skip
    rr.run_replay(w.deps(), role="writer", champion=champ, challenger=chall)
    rv_champ, rv_chall = reviewer_pair(w)
    rr.run_replay(w.deps(), role="reviewer", champion=rv_champ, challenger=rv_chall)
    assert w.gh.writes == [] and w.gh.git_pushes == []
    w.gh.assert_no_writes()
    assert pushes == []
    assert w.remote_refs() == refs
    for mod in (rr, rs):
        tree = ast.parse(Path(mod.__file__).read_text())
        imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
            a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
        }
        assert not {m for m in imported if m.startswith(("foreman_inator.github",
                                                         "foreman_inator.pr"))}  # fmt: skip


def test_cloud_challenger_costs_count_to_the_experiments_bucket(world: World) -> None:
    w = world
    three_merges(w)
    champ, chall = writer_pair(w, challenger_model="glm")
    w.driver.choose = plan({"champion": {"a": "ok", "b": "wrong", "c": "wrong"},
                            "challenger": {"a": "ok", "b": "ok", "c": "ok"}})  # fmt: skip
    c = rr.run_replay(w.deps(), role="writer", champion=champ, challenger=chall,
                      settings=rr.ReplaySettings(min_cases=3))  # fmt: skip
    assert (c.champion.passed, c.challenger.passed, c.wins) == (1, 3, 2)
    assert c.champion.cost_usd == 0 and c.challenger.cost_usd > 0
    assert c.challenger.tokens_in > 0
    # more passes, but a free champion: cost per pass is higher, so no win on replay
    assert c.verdict == "does_not_win" and "cost per passed case is higher" in c.reason
    ledger = w.store.query("SELECT bucket, ticket_id, attempt_id FROM spend_ledger")
    assert ledger and {tuple(r) for r in ledger} == {("experiments", None, None)}


def test_spend_refusal_stops_the_run_cleanly_and_a_resume_finishes_it(tmp_path: Path) -> None:
    """The proxy's 429 spend_cap_reached in the experiments bucket: the run stops, the
    cut-short side is not stored, nobody is asked, nothing moves to the live bucket; next
    month (budget again) the resume runs only what is missing."""
    w = World(tmp_path, caps=SpendCaps(experiments_monthly_usd=0.0))
    try:
        three_merges(w)
        champ, chall = writer_pair(w, challenger_model="glm")
        w.driver.choose = plan(KNOWN)
        # spend=None: no pre-check, so the refusal comes from the proxy's own 429
        c = rr.run_replay(w.deps(spend=False), role="writer", champion=champ,
                          challenger=chall, settings=rr.ReplaySettings(min_cases=3))  # fmt: skip
        assert (c.state, c.stop_reason) == ("stopped", rr.STOP_EXPERIMENTS_BUDGET)
        first = rs.list_cases(w.store, c.set_id)[0].id
        assert [(r.case_id, r.side) for r in rr.results(w.store, c.experiment_id)] == [
            (first, "champion")
        ]
        note = w.store.get_experiment(c.experiment_id).result_note
        assert note.startswith("stopped: experiments budget")
        assert w.store.query("SELECT * FROM spend_ledger") == []
        assert w.store.query("SELECT * FROM spend_approvals") == []
        assert all(t.flag_reason is None for t in w.store.list_tickets())
        assert w.leftovers() == []
        # with the pre-check (spend given), a run stops before even launching the writer
        runs = len(w.driver.runs)
        c1 = rr.run_replay(w.deps(), role="writer", champion=champ, challenger=chall)
        assert c1.experiment_id == c.experiment_id and c1.stop_reason == rr.STOP_EXPERIMENTS_BUDGET
        assert len(w.driver.runs) == runs
        # the budget is back: resume runs only the 5 missing sides
        w.spend = Spend(w.store, Config(spend_caps=SpendCaps(experiments_monthly_usd=10.0)))
        w.proxy.spend_hook = w.spend.hook
        c2 = rr.run_replay(w.deps(), role="writer", champion=champ, challenger=chall,
                           settings=rr.ReplaySettings(min_cases=3))  # fmt: skip
        assert c2.experiment_id == c.experiment_id and c2.state == "finished"
        assert len(w.driver.runs) == runs + 5
        assert (c2.wins, c2.losses, c2.ties) == (1, 0, 2)
        assert {r["bucket"] for r in w.store.query("SELECT bucket FROM spend_ledger")} == {
            "experiments"
        }
    finally:
        w.close()


def test_the_run_budget_stops_a_run(world: World) -> None:
    w = world
    three_merges(w)
    champ, chall = writer_pair(w, challenger_model="glm")
    w.driver.choose = plan(KNOWN)
    c = rr.run_replay(w.deps(), role="writer", champion=champ, challenger=chall,
                      settings=rr.ReplaySettings(run_budget_usd=1e-7))  # fmt: skip
    assert (c.state, c.stop_reason) == ("stopped", rr.STOP_RUN_BUDGET)
    assert [r.side for r in rr.results(w.store, c.experiment_id)] == ["champion"]
    assert w.store.query("SELECT * FROM spend_ledger") == []


def test_a_killed_run_resumes_without_rerunning_scored_cases(world: World) -> None:
    w = world
    three_merges(w)
    champ, chall = writer_pair(w)
    w.driver.choose = plan(KNOWN)
    w.driver.kill_on = 4  # case 2, challenger
    with pytest.raises(SimulatedKill):
        rr.run_replay(w.deps(), role="writer", champion=champ, challenger=chall,
                      settings=rr.ReplaySettings(min_cases=3))  # fmt: skip
    (exp,) = w.store.list_experiments(role="writer")
    assert len(rr.results(w.store, exp.id)) == 3
    assert w.store.query("SELECT state FROM replay_runs")[0]["state"] == "running"
    w.driver.kill_on = None
    w.driver.runs.clear()
    c = rr.run_replay(w.deps(), role="writer", champion=champ, challenger=chall,
                      settings=rr.ReplaySettings(min_cases=3))  # fmt: skip
    assert c.experiment_id == exp.id and c.state == "finished"
    assert len(w.driver.runs) == 3  # only the sides with no stored result
    assert (c.wins, c.losses, c.ties) == (1, 0, 2)
    assert w.leftovers() == []


def test_cancel_stops_kills_and_cleans_up(world: World) -> None:
    w = world
    three_merges(w)
    champ, chall = writer_pair(w)
    w.driver.choose = lambda prompt, ep: script("hang", key_of(prompt))
    cancel = threading.Event()
    threading.Timer(1.5, cancel.set).start()
    c = rr.run_replay(w.deps(), role="writer", champion=champ, challenger=chall, cancel=cancel)
    assert (c.state, c.stop_reason) == ("cancelled", rr.STOP_CANCELLED)
    assert rr.results(w.store, c.experiment_id) == []
    assert len(w.driver.runs) == 1
    assert w.leftovers() == []
    launch = w.oc.launches()[-1]
    pid = launch.get("pid")
    if pid:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


# =========================================================================== reviewer replay


def reviewer_pair(w: World, *, challenger: str = "glm"):
    champ = rr.ReviewerVariant(f"claude-opus/{REVIEWER_PROMPT_NAME}@x",
                               w.config.model("claude-opus"))  # fmt: skip
    chall = rr.ReviewerVariant(f"{challenger}/{REVIEWER_PROMPT_NAME}@x",
                               w.config.model(challenger))  # fmt: skip
    return champ, chall


def test_reviewer_replay_scores_catches_and_false_kickbacks(world: World) -> None:
    w = world
    w.merged_ticket("a", kickback="incomplete")  # cases: a1 kicked back, a2 merged
    w.merged_ticket("b")  # b1 merged
    w.sync()
    before = w.live_snapshot()
    champ, chall = reviewer_pair(w)

    def rule(model: str, text: str) -> str:
        if model == "glm-5.3":  # the challenger reads the diff and spots the TODO
            return "request_changes" if "TODO" in text else "approve"
        return "approve"  # the champion approves everything

    w.review_rule = rule
    c = rr.run_replay(w.deps(), role="reviewer", champion=champ, challenger=chall,
                      settings=rr.ReplaySettings(min_cases=3))  # fmt: skip
    scores = [(r.side, r.score) for r in rr.results(w.store, c.experiment_id)]
    assert scores == [
        ("challenger", "catch"), ("champion", "miss"),
        ("challenger", "correct_approve"), ("champion", "correct_approve"),
        ("challenger", "correct_approve"), ("champion", "correct_approve"),
    ]  # fmt: skip
    assert (c.wins, c.losses, c.ties) == (1, 0, 2)
    assert (c.challenger.catches, c.champion.catches, c.challenger.false_kickbacks) == (1, 0, 0)
    assert c.challenger.cost_usd > 0 and c.champion.cost_usd > 0
    assert c.verdict == "wins", c.reason
    note = w.store.get_experiment(c.experiment_id).result_note
    assert "Catches: champion 0, challenger 1; false kickbacks: champion 0, challenger 0" in note
    assert w.live_snapshot() == before  # store=None: no reviews or kickbacks rows
    ledger = {tuple(r) for r in w.store.query("SELECT bucket, attempt_id FROM spend_ledger")}
    assert ledger == {("experiments", None)}

    # a challenger that also kicks back a change the user merged does not win
    def eager(model: str, text: str) -> str:
        if model == "glm-5.3":
            return "request_changes" if ("TODO" in text or "b-ok" in text) else "approve"
        return "approve"

    w.review_rule = eager
    champ2, chall2 = champ, rr.ReviewerVariant("glm/reviewer-v1@eager", w.config.model("glm"))
    c2 = rr.run_replay(w.deps(), role="reviewer", champion=champ2, challenger=chall2,
                       settings=rr.ReplaySettings(min_cases=3))  # fmt: skip
    assert (c2.wins, c2.losses, c2.ties) == (1, 1, 1)
    assert (c2.challenger.catches, c2.challenger.false_kickbacks) == (1, 1)
    assert c2.verdict == "does_not_win" and "kicked back more changes" in c2.reason


def test_reviewer_family_conflict_is_an_error_not_a_score(world: World) -> None:
    w = world
    w.merged_ticket("a", model="glm")  # written by glm: glm cannot review it
    w.sync()
    champ, chall = reviewer_pair(w)
    c = rr.run_replay(w.deps(), role="reviewer", champion=champ, challenger=chall,
                      settings=rr.ReplaySettings(min_cases=1))  # fmt: skip
    rows = {r.side: r for r in rr.results(w.store, c.experiment_id)}
    assert rows["challenger"].status == "error" and "same model family" in rows["challenger"].detail
    assert rows["champion"].status == "scored"
    assert (c.scored_cases, c.errored_cases, c.verdict) == (0, 1, "inconclusive")


# =========================================================================== sandbox


def test_every_launch_gets_a_policy_from_the_caller(world: World) -> None:
    w = world
    w.merged_ticket("a")
    w.sync()
    asked: list[tuple[str, bool]] = []
    base = rr.config_sandbox(w.config, w.tmp / "sbx")

    def spy(repo: str, path: Path, key: str, *, writer: bool) -> sb.Policy:
        asked.append((key.rsplit("-", 1)[1], writer))
        return base(repo, path, key, writer=writer)

    champ, chall = writer_pair(w)
    w.driver.choose = lambda prompt, ep: script("ok", key_of(prompt))
    rr.run_replay(w.deps(sandbox=spy), role="writer", champion=champ, challenger=chall)
    assert asked == [("writer", True), ("verify", False)] * 2
    assert all(isinstance(r["sandbox"], sb.Policy) for r in w.driver.runs)


def test_no_sandbox_means_no_replay(tmp_path: Path) -> None:
    """sandbox = "on" where it cannot run: the run stops, nothing is launched (fail
    closed), and nothing runs unsandboxed."""
    w = World(tmp_path, sandbox="on")
    old = sb.set_backend(SeatbeltBackend(platform="linux"))
    try:
        w.merged_ticket("a")
        w.sync()
        champ, chall = writer_pair(w)
        c = rr.run_replay(w.deps(), role="writer", champion=champ, challenger=chall)
        assert (c.state, c.stop_reason) == ("stopped", rr.STOP_SANDBOX)
        assert w.driver.runs == [] and w.oc.launches() == []
        assert w.leftovers() == []
    finally:
        sb.set_backend(old)
        w.close()


@pytest.mark.skipif(not SeatbeltBackend().available(), reason="the real sandbox is macOS only")
def test_a_writer_replay_runs_under_the_real_sandbox(tmp_path: Path) -> None:
    w = World(tmp_path, sandbox="on")
    outside = tmp_path / "outside.txt"
    base = rr.config_sandbox(w.config, tmp_path / "sbx")
    modes: list[str] = []

    def policy(repo: str, path: Path, key: str, *, writer: bool) -> sb.Policy:
        p = base(repo, path, key, writer=writer).bind(
            reads=[FAKES], writes=[w.oc.root], argv0=sys.executable
        )
        modes.append(p.mode)
        return p

    try:
        w.merged_ticket("a")
        w.sync()
        champ, chall = writer_pair(w)
        from fakes.opencode import bash

        w.driver.choose = lambda prompt, ep: Script(
            [bash(f"echo leak > {outside} || true"), *script("ok", key_of(prompt)).steps]
        )
        c = rr.run_replay(w.deps(sandbox=policy), role="writer", champion=champ,
                          challenger=chall, settings=rr.ReplaySettings(min_cases=1))  # fmt: skip
        assert [r.status for r in rr.results(w.store, c.experiment_id)] == ["passed", "passed"]
        assert not outside.exists()
        assert set(modes) == {"on"} and len(modes) == 4
        assert w.models.requests, "the writer's call did not reach the proxy"
    finally:
        w.close()


# =========================================================================== comparison


def test_the_comparison_is_a_pure_function_of_the_case_outcomes(world: World) -> None:
    w = world
    three_merges(w)
    sid = rs.build_replay_set(w.store, "writer", mirror_for=lambda r: w.mirror).id
    ids = [sc.id for sc in rs.list_cases(w.store, sid)]
    outcomes = [
        (ids[0], "champion", "passed", 0.0, 10.0), (ids[0], "challenger", "passed", 0.0, 8.0),
        (ids[1], "champion", "failed", 0.0, 12.0), (ids[1], "challenger", "passed", 0.0, 9.0),
        (ids[2], "champion", "timed_out", 0.0, 30.0),
        (ids[2], "challenger", "agent_failed", 0.0, 3.0),
    ]  # fmt: skip
    notes = []
    for order in (outcomes, list(reversed(outcomes))):
        exp_id = rr.start_replay(w.store, role="writer", champion_variant="champ-v",
                                 challenger_variant="chall-v", set_id=sid)  # fmt: skip
        for case_id, side, status, cost, secs in order:
            rr._save(w.store, rr.CaseResult(exp_id, case_id, side, f"{side}-v", status, None,
                                            None, 0, 0, cost, secs, None))  # fmt: skip
        c = rr.compare(w.store, exp_id, min_cases=3)
        notes.append(rr.comparison_note(c).replace(f"set {sid}", "set N"))
        assert (c.wins, c.losses, c.ties, c.verdict) == (1, 0, 2, "wins")
    assert notes[0] == notes[1]


def T(**kw: Any) -> rr.SideTotals:
    return rr.SideTotals(variant_id="v", **kw)


@pytest.mark.parametrize(
    ("champ", "chall", "verdict"),
    [
        (T(passed=5, cost_usd=1.0), T(passed=6, cost_usd=1.0), "wins"),
        (T(passed=5, cost_usd=1.0), T(passed=5, cost_usd=0.5), "wins"),
        (T(passed=5, seconds=10.0), T(passed=5, seconds=9.0), "wins"),
        (T(passed=5), T(passed=5), "does_not_win"),
        (T(passed=5), T(passed=4), "does_not_win"),
        (T(passed=2, cost_usd=0.0), T(passed=3, cost_usd=0.3), "does_not_win"),
        (T(passed=0), T(passed=0), "does_not_win"),
    ],
)
def test_writer_verdict_rule(champ, chall, verdict) -> None:
    assert rr.verdict_for("writer", champ, chall, 10, 10)[0] == verdict


@pytest.mark.parametrize(
    ("champ", "chall", "verdict"),
    [
        (T(catches=2), T(catches=3), "wins"),
        (T(false_kickbacks=2), T(false_kickbacks=1), "wins"),
        (T(catches=2, cost_usd=1.0), T(catches=2, cost_usd=0.5), "wins"),
        (T(catches=2), T(catches=3, false_kickbacks=1), "does_not_win"),
        (T(catches=2), T(catches=1), "does_not_win"),
        (T(catches=2, cost_usd=1.0), T(catches=3, cost_usd=2.0), "does_not_win"),
        (T(catches=2), T(catches=2), "does_not_win"),
    ],
)
def test_reviewer_verdict_rule(champ, chall, verdict) -> None:
    assert rr.verdict_for("reviewer", champ, chall, 10, 10)[0] == verdict


def test_too_few_cases_is_inconclusive() -> None:
    assert rr.verdict_for("writer", T(passed=1), T(passed=9), 9, 10)[0] == "inconclusive"


@pytest.mark.parametrize(
    ("truth", "verdict", "score"),
    [
        ("kicked_back", "request_changes", "catch"),
        ("kicked_back", "approve", "miss"),
        ("merged", "request_changes", "false_kickback"),
        ("merged", "approve", "correct_approve"),
        ("merged", "handed_to_user", "handed"),
        ("kicked_back", "handed_to_user", "handed"),
    ],
)
def test_review_scores(truth: str, verdict: str, score: str) -> None:
    assert rr.review_score(truth, verdict) == score


def test_champion_and_challenger_must_differ(world: World) -> None:
    champ, _ = writer_pair(world)
    with pytest.raises(ValueError, match="different variants"):
        rr.run_replay(world.deps(), role="writer", champion=champ, challenger=champ)
