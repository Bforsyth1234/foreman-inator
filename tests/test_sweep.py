"""`fman sweep` (todo #77): selection, wave preview, dry run, applying labels via a fake gh."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from foreman_inator import locks, sweep
from foreman_inator.config import Config
from foreman_inator.db.store import Store

REPO = "acme/widgets"
ME = "brooks"


class FakeGh:
    """A fake `gh` CLI: open issues, open PRs, and the labels it was asked to add."""

    def __init__(self, issues: list[dict], prs: list[dict] | None = None, login: str = ME):
        self.issues = issues
        self.prs = prs or []
        self.login = login
        self.logged_in = True
        self.calls: list[list[str]] = []
        self.label_calls: list[int] = []
        self.auth_fail_on: int | None = None  # issue number whose edit gets HTTP 401

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args = list(args)
        self.calls.append(args)
        assert args[0] == "gh"
        cmd = args[1:3]

        def ok(out: str = "") -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 0, out, "")

        def err(text: str) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 1, "", text)

        if cmd == ["auth", "status"]:
            return ok() if self.logged_in else err("You are not logged into any GitHub hosts.")
        if cmd == ["api", "user"]:
            return ok(self.login + "\n")
        if cmd == ["issue", "list"]:
            return ok(json.dumps(self.issues))
        if cmd == ["pr", "list"]:
            return ok(json.dumps(self.prs))
        if cmd[0] == "issue" and cmd[1] == "edit":
            n = int(args[3])
            if n == self.auth_fail_on:
                return err("HTTP 401: Bad credentials (https://api.github.com/graphql)")
            assert args[4:] == ["-R", REPO, "--add-label", "agent"]
            self.label_calls.append(n)
            for i in self.issues:
                if i["number"] == n:
                    i["labels"].append({"name": "agent"})
            return ok()
        raise AssertionError(f"unexpected gh call {args}")


def issue(n: int, body: str, *, author: str = ME, labels: Sequence[str] = ()) -> dict:
    return {
        "number": n,
        "title": f"Issue {n}",
        "body": body,
        "author": {"login": author},
        "labels": [{"name": lb} for lb in labels],
    }


def ten_issues() -> tuple[list[dict], list[dict]]:
    """10 issues: 2 already labeled, 1 with an open fman/ PR, 1 by a non-allowed author;
    the 6 left share files so they need more than one wave, and one names no files."""
    issues = [
        issue(1, "Fix `src/a.py`."),
        issue(2, "Change `src/a.py` and `src/b.py`.", labels=["agent"]),
        issue(3, "Update `src/b.py`."),
        issue(4, "Tweak `src/a.py` again."),
        issue(5, "Edit `docs/` pages.", labels=["agent", "bug"]),
        issue(6, "Touch `src/c.py`."),  # has a PR below
        issue(7, "Refactor `src/b.py` and `src/c.py`."),
        issue(8, "Something vague with no paths at all."),
        issue(9, "Clean up `src/d.py`.", author="mallory"),
        issue(10, "Rename `src/a.py` to `src/aa.py`.", labels=["tier:3"]),
    ]
    prs = [
        {"number": 50, "headRefName": "fman/issue-6/a1"},
        {"number": 51, "headRefName": "someone/feature"},
    ]
    return issues, prs


@pytest.fixture
def cfg() -> Config:
    return Config(allowed_users=(ME,), repos=(REPO,))


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "fman.db")
    yield s
    s.close()


def run(fake: FakeGh, cfg: Config, store: Store | None, **kw) -> tuple[int, str]:
    out: list[str] = []
    code = sweep.run(
        REPO,
        config=cfg,
        store=store,
        gh=sweep.Gh(fake),
        echo=out.append,
        pace_seconds=0,
        **kw,
    )
    return code, "\n".join(out)


def plan_for(fake: FakeGh, cfg: Config, store: Store | None = None) -> sweep.Plan:
    gh = sweep.Gh(fake)
    picked, skipped = sweep.select(
        gh.list_issues(REPO), repo=REPO, config=cfg, store=store, fman_prs=gh.fman_pr_issues(REPO)
    )
    return sweep.build_plan(picked, skipped, repo=REPO, config=cfg, store=store)


# --------------------------------------------------------------------------- selection


def test_preview_lists_six_and_waves_share_no_file(cfg: Config, store: Store) -> None:
    issues, prs = ten_issues()
    plan = plan_for(FakeGh(issues, prs), cfg, store)
    assert [p.issue.number for p in plan.items] == [1, 3, 4, 7, 8, 10]
    reasons = {s.issue.number: s.reason for s in plan.skipped}
    assert reasons[2].startswith("already labeled") and reasons[5].startswith("already labeled")
    assert reasons[6] == "has an open fman/ PR"
    assert "mallory is not on allowed_users" in reasons[9]
    # Waves: no lock key wanted by two issues of one wave.
    by_wave: dict[int, list[str]] = {}
    for p in plan.items:
        if p.wave is not None:
            keys = [locks.lock_key(f) for f in p.files]
            held = by_wave.setdefault(p.wave, [])
            assert not any(locks.overlaps(k, h) for k in keys for h in held), (p, held)
            held.extend(keys)
    assert plan.waves >= 3  # src/a.py is wanted by 1, 4 and 10
    (unknown,) = [p for p in plan.items if p.wave is None]
    assert unknown.issue.number == 8 and unknown.files == ()
    tiers = {p.issue.number: p.tier for p in plan.items}
    assert tiers[10] == 3 and tiers[1] == 2  # a tier label; else the router's fallback


def test_group_waves_is_greedy_first_fit() -> None:
    waves = sweep.group_waves(
        [(1, ["src/a.py"]), (2, ["src/a.py"]), (3, ["src/b.py"]), (4, ["src/"]), (5, [])]
    )
    assert waves == {1: 1, 2: 2, 3: 1, 4: 3, 5: None}  # src/ overlaps src/a.py and b.py


def test_open_ticket_and_filters_are_skipped(cfg: Config, store: Store) -> None:
    issues, prs = ten_issues()
    store.create_ticket(title="Issue 3", request="", repo=REPO, base_branch="main", issue_number=3)
    gh = sweep.Gh(FakeGh(issues, prs))
    picked, skipped = sweep.select(
        gh.list_issues(REPO),
        repo=REPO,
        config=cfg,
        store=store,
        fman_prs=gh.fman_pr_issues(REPO),
        filters=sweep.Filters(exclude_label=("tier:3",), limit=2, include_any_author=True),
    )
    assert [i.number for i in picked] == [1, 4]
    reasons = {s.issue.number: s.reason for s in skipped}
    assert reasons[3] == "has an open ticket"
    assert reasons[10] == "has excluded label `tier:3`"
    assert reasons[9] == "over --limit 2"  # mallory included by --include-any-author


def test_preview_text_shows_groups_and_spend(cfg: Config, store: Store) -> None:
    issues, prs = ten_issues()
    text = sweep.render_preview(plan_for(FakeGh(issues, prs), cfg, store))
    assert "Wave 1" in text and "Unknown files (may collide later) (1):" in text
    assert "files: src/a.py" in text
    assert "estimated cloud spend unknown" in text  # no accepted tickets yet
    assert "Spend caps left:" in text


# --------------------------------------------------------------------------- run / apply


def test_dry_run_labels_nothing(cfg: Config, store: Store) -> None:
    fake = FakeGh(*ten_issues())
    code, out = run(fake, cfg, store, dry_run=True)
    assert code == 0 and "Dry run: nothing labeled." in out
    assert fake.label_calls == []
    assert not any(c[1:3] == ["issue", "edit"] for c in fake.calls)


def test_apply_labels_through_gh_and_rerun_labels_nothing_new(cfg: Config, store: Store) -> None:
    fake = FakeGh(*ten_issues())
    asked: list[str] = []
    code, out = run(fake, cfg, store, confirm=lambda q: asked.append(q) or True)
    assert code == 0
    assert fake.label_calls == [1, 3, 4, 7, 8, 10]
    assert asked == [f"Label 6 issue(s) `agent` as {ME}?"]
    assert "[6/6] #10: labeled `agent`" in out
    code, out = run(fake, cfg, store, yes=True)
    assert code == 0 and "Nothing to label." in out
    assert fake.label_calls == [1, 3, 4, 7, 8, 10]


def test_declined_confirmation_labels_nothing(cfg: Config, store: Store) -> None:
    fake = FakeGh(*ten_issues())
    code, out = run(fake, cfg, store, confirm=lambda q: False)
    assert code == 0 and fake.label_calls == [] and out.endswith("Nothing labeled.")


def test_first_auth_error_stops_the_run(cfg: Config, store: Store) -> None:
    fake = FakeGh(*ten_issues())
    fake.auth_fail_on = 3
    code, out = run(fake, cfg, store, yes=True)
    assert code == 1
    assert fake.label_calls == [1]  # stopped at #3, nothing after it
    assert "stopping" in out and sweep.GH_LOGIN_HINT in out


def test_paced_between_labels(cfg: Config, store: Store) -> None:
    fake = FakeGh(*ten_issues())
    slept: list[float] = []
    plan = plan_for(fake, cfg, store)
    sweep.apply(plan, sweep.Gh(fake), echo=lambda s: None, pace_seconds=0.5, sleep=slept.append)
    assert slept == [0.5] * 5


def test_gh_not_logged_in_prints_login_step_and_stops(cfg: Config, store: Store) -> None:
    fake = FakeGh(*ten_issues())
    fake.logged_in = False
    code, out = run(fake, cfg, store, yes=True)
    assert code == 1 and sweep.GH_LOGIN_HINT in out
    assert [c[1:3] for c in fake.calls] == [["auth", "status"]]


def test_gh_missing_prints_login_step(cfg: Config) -> None:
    def missing(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("gh")

    out: list[str] = []
    code = sweep.run(REPO, config=cfg, store=None, gh=sweep.Gh(missing), echo=out.append)
    assert code == 1 and "not installed" in out[0] and sweep.GH_LOGIN_HINT in out[0]


def test_gh_account_must_be_an_allowed_user(cfg: Config, store: Store) -> None:
    """Labels by anyone else would be ignored by the poller (7b), so stop first."""
    fake = FakeGh(*ten_issues(), login="the-bot[bot]")
    code, out = run(fake, cfg, store, yes=True)
    assert code == 1 and "not on allowed_users" in out and fake.label_calls == []


def test_cloud_estimate_uses_tier_kind(cfg: Config) -> None:
    issues, prs = ten_issues()
    plan = plan_for(FakeGh(issues, prs), cfg)
    # #70 defaults: tier 2 (GLM) and tier 3 are cloud, so all six start on a cloud tier.
    assert plan.cloud_issues == 6 and plan.estimate_usd is None
    with_avg = sweep.Plan(REPO, plan.items, plan.skipped, plan.waves, avg_cost_usd=0.25)
    assert with_avg.estimate_usd == pytest.approx(1.5)
