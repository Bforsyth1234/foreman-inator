"""`fman sweep owner/repo` (todo #77): start every open issue of a repo, in dynamic waves.

The worker already runs tickets in parallel behind file locks (``locks.py``), so a
"wave" is not a barrier: a ticket starts as soon as its files are free and
``max_parallel_per_repo`` allows. What the sweep adds is the bulk start, plus a preview
of how the issues would group, and the worker's open-PR cap (``worker_queue``) keeps the
pile of draft PRs reviewable.

Steps (``run``):

1. **gh check.** Labels are applied with the user's own GitHub credentials through the
   ``gh`` CLI, never the App token: the poller counts an ``agent`` label only when a user
   on ``allowed_users`` applied it (invariant 7b, ``github/poller.py``), so a label put
   on by the bot would start nothing, and that rule stays. ``gh`` missing or not logged
   in: print the ``gh auth login`` step and stop. The ``gh`` account must itself be on
   ``allowed_users`` (its labels would be ignored otherwise).
2. **Select.** Open issues (``gh issue list`` never lists PRs) with no ``agent`` label,
   no open ticket in the store and no ``fman/issue-<n>/`` PR; ``--label``,
   ``--exclude-label``, ``--author``, ``--limit``. Issues by authors not on
   ``allowed_users`` are skipped unless ``include_any_author`` (the preview says so).
3. **Preview.** Per issue: predicted files (``locks.predict_files``), the router's tier,
   and its wave. Waves are greedy: an issue joins the first wave none of whose issues
   wants an overlapping path (``locks.overlaps``, the lock rules: a directory overlaps
   what is under it), the same rule as todo.md rule 3. Issues that predict no files go
   in an "unknown files" group: they hold no locks until their first diff, so they may
   collide later. Totals: issues, waves, estimated cloud spend (issues whose tier's
   first model is cloud × the repo's average cost per accepted ticket, or "unknown"
   with no history) against the remaining spend caps.
4. **Confirm** once (``yes`` skips it); ``dry_run`` labels nothing.
5. **Apply.** ``gh issue edit N --add-label agent`` per issue, paced, printing progress;
   the first auth error stops the run. Re-running is safe: labeled issues are skipped.

``gh`` docs checked 2026-11-25 against gh 2.72.0's own help (``gh issue list --help``,
``gh pr list --json``, ``gh issue edit --help``, ``gh auth status --help``) and
https://cli.github.com/manual/gh_issue_list: ``issue list`` takes ``--state open --json
number,title,body,labels,author --limit N`` (PRs are not issues there), ``pr list`` has
``headRefName``, ``issue edit`` takes ``--add-label``, and ``auth status`` exits 1 when
not logged in. ``gh api user --jq .login`` names the active account.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from foreman_inator import locks
from foreman_inator.config import Config
from foreman_inator.db.store import Store
from foreman_inator.runner.worktree import parse_fman_branch

AGENT_LABEL = "agent"  # github/poller.AGENT_LABEL (not imported: keeps sweep light)
GH_LOGIN_HINT = "gh auth login --hostname github.com --git-protocol https --web"
DEFAULT_PACE_SECONDS = 1.0  # between label calls: well under GitHub's secondary limits
LIST_LIMIT = 1000  # issues and PRs fetched per listing
FALLBACK_TIER = 2  # routing.rules.FALLBACK_TIER: what the router gives when not asked
_FMAN_HEAD = parse_fman_branch  # fman/issue-<n>/... and ab:pair side B fman/issue-<n>-b/...
_TIER_LABEL = re.compile(r"^tier:([123])$")
_AUTH_ERROR = re.compile(
    r"HTTP 401|Bad credentials|authentication|not logged in|gh auth login|"
    r"HTTP 403.*(?:permission|Resource not accessible)",
    re.IGNORECASE,
)

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class GhError(RuntimeError):
    """A ``gh`` call failed."""


class GhAuthError(GhError):
    """``gh`` is missing, not logged in, or its credentials were refused."""


def _subprocess_runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), capture_output=True, text=True, timeout=120, check=False)


class Gh:
    """The user's own ``gh`` CLI. ``runner`` runs one argv (tests pass a fake)."""

    def __init__(self, runner: Runner | None = None) -> None:
        self._run = runner or _subprocess_runner

    def _call(self, *args: str) -> str:
        try:
            proc = self._run(["gh", *args])
        except FileNotFoundError as exc:
            raise GhAuthError("the GitHub CLI `gh` is not installed") from exc
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()[:500]
            kind = GhAuthError if _AUTH_ERROR.search(err) else GhError
            raise kind(f"gh {' '.join(args[:2])} failed: {err or f'exit {proc.returncode}'}")
        return proc.stdout

    def login(self) -> str:
        """The logged-in account; GhAuthError when there is none."""
        try:
            self._call("auth", "status")
        except GhAuthError:
            raise
        except GhError as exc:  # exit 1 = not logged in (gh auth status --help)
            raise GhAuthError(str(exc)) from exc
        return self._call("api", "user", "--jq", ".login").strip()

    def list_issues(
        self, repo: str, *, label: str | None = None, author: str | None = None
    ) -> list[Issue]:
        args = ["issue", "list", "-R", repo, "--state", "open", "--limit", str(LIST_LIMIT)]
        args += ["--json", "number,title,body,labels,author"]
        if label:
            args += ["--label", label]
        if author:
            args += ["--author", author]
        return [Issue.from_gh(raw) for raw in json.loads(self._call(*args) or "[]")]

    def fman_pr_issues(self, repo: str) -> set[int]:
        """Issue numbers that already have an open ``fman/issue-<n>/`` PR (or a side-B
        ``fman/issue-<n>-b/`` one, #16)."""
        out = self._call(
            "pr", "list", "-R", repo, "--state", "open", "--limit", str(LIST_LIMIT),
            "--json", "number,headRefName",
        )  # fmt: skip
        found: set[int] = set()
        for raw in json.loads(out or "[]"):
            parsed = _FMAN_HEAD(str(raw.get("headRefName") or ""))
            if parsed is not None:
                found.add(parsed[0])
        return found

    def add_label(self, repo: str, number: int, label: str = AGENT_LABEL) -> None:
        self._call("issue", "edit", str(number), "-R", repo, "--add-label", label)


# --------------------------------------------------------------------------- selection


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    author: str
    labels: tuple[str, ...] = ()

    @classmethod
    def from_gh(cls, raw: dict[str, Any]) -> Issue:
        author = raw.get("author") or {}
        return cls(
            number=int(raw["number"]),
            title=str(raw.get("title") or ""),
            body=str(raw.get("body") or ""),
            author=str(author.get("login") or "") if isinstance(author, dict) else str(author),
            labels=tuple(str(lb.get("name", "")) for lb in raw.get("labels") or ()),
        )

    def has_label(self, name: str) -> bool:
        return name.lower() in {lb.lower() for lb in self.labels}


@dataclass(frozen=True)
class Filters:
    label: str | None = None  # only issues with this label
    exclude_label: tuple[str, ...] = ()  # skip issues with any of these
    author: str | None = None
    limit: int | None = None
    include_any_author: bool = False


@dataclass(frozen=True)
class Skipped:
    issue: Issue
    reason: str


def select(
    issues: Iterable[Issue],
    *,
    repo: str,
    config: Config,
    store: Store | None,
    fman_prs: set[int],
    filters: Filters = Filters(),
) -> tuple[list[Issue], list[Skipped]]:
    """The issues a sweep would label, oldest first, and why the others are left."""
    picked: list[Issue] = []
    skipped: list[Skipped] = []
    for issue in sorted(issues, key=lambda i: i.number):
        why = _skip_reason(issue, repo, config, store, fman_prs, filters)
        if why is None and filters.limit is not None and len(picked) >= filters.limit:
            why = f"over --limit {filters.limit}"
        if why is None:
            picked.append(issue)
        else:
            skipped.append(Skipped(issue, why))
    return picked, skipped


def _skip_reason(
    issue: Issue,
    repo: str,
    config: Config,
    store: Store | None,
    fman_prs: set[int],
    filters: Filters,
) -> str | None:
    if issue.has_label(AGENT_LABEL):
        return f"already labeled `{AGENT_LABEL}`"
    if store is not None and store.find_open_ticket(repo, issue.number) is not None:
        return "has an open ticket"
    if issue.number in fman_prs:
        return "has an open fman/ PR"
    if filters.label and not issue.has_label(filters.label):
        return f"no `{filters.label}` label"
    for lb in filters.exclude_label:
        if issue.has_label(lb):
            return f"has excluded label `{lb}`"
    if filters.author and issue.author.lower() != filters.author.lower():
        return f"author is not {filters.author}"
    if not filters.include_any_author and not config.is_allowed_user(issue.author):
        return f"author {issue.author or '?'} is not on allowed_users"
    return None


# --------------------------------------------------------------------------- the plan


#: (issue) -> (tier, reason). ``label_router`` asks no model; ``classifier_router``
#: asks the rules router's local classifier.
Router = Callable[[Issue], tuple[int, str]]


def label_router(issue: Issue) -> tuple[int, str]:
    """A ``tier:N`` label, else the router's fallback tier (no model is asked)."""
    for lb in issue.labels:
        m = _TIER_LABEL.match(lb.lower())
        if m:
            return int(m.group(1)), f"tier:{m.group(1)} label"
    return FALLBACK_TIER, "router not asked (decides at start)"


def classifier_router(config: Config, repo: str) -> Router:
    """The rules router (``routing.rules.route``) per issue: a ``tier:N`` label wins, else
    its small local classifier, called on its loopback ``base_url`` (local only, 7c: a
    cloud classifier is refused there, giving the fallback tier)."""
    from foreman_inator.routing import rules

    clf = rules.default_classifier(config)

    def route(issue: Issue) -> tuple[int, str]:
        tier, why = label_router(issue)
        if why.startswith("tier:"):
            return tier, why
        ticket = SimpleNamespace(title=issue.title, request=issue.body, repo=repo)
        r = rules.route(
            ticket,
            config=config,
            base_url=clf.base_url if clf is not None else None,
            timeout=config.router_timeout_seconds,
        )
        return r.tier, r.reason

    return route


@dataclass(frozen=True)
class Planned:
    issue: Issue
    files: tuple[str, ...]
    tier: int
    tier_reason: str
    cloud: bool
    wave: int | None  # 1-based; None = the "unknown files" group


@dataclass(frozen=True)
class Plan:
    repo: str
    items: tuple[Planned, ...]
    skipped: tuple[Skipped, ...]
    waves: int
    avg_cost_usd: float | None  # per accepted ticket in this repo (None: no history)
    remaining_today_usd: float | None = None
    remaining_month_usd: float | None = None
    include_any_author: bool = False
    notes: tuple[str, ...] = field(default=())

    @property
    def cloud_issues(self) -> int:
        return sum(1 for p in self.items if p.cloud)

    @property
    def estimate_usd(self) -> float | None:
        if not self.cloud_issues:
            return 0.0
        return None if self.avg_cost_usd is None else self.cloud_issues * self.avg_cost_usd


def group_waves(files_by_issue: Sequence[tuple[int, Sequence[str]]]) -> dict[int, int | None]:
    """Greedy waves: issue -> 1-based wave, or None when it predicts no files. No path
    (by the lock rules) is wanted by two issues of one wave."""
    waves: list[list[str]] = []
    out: dict[int, int | None] = {}
    for number, files in files_by_issue:
        keys = [locks.lock_key(f) for f in files]
        if not keys:
            out[number] = None
            continue
        for i, held in enumerate(waves):
            if not any(locks.overlaps(k, h) for k in keys for h in held):
                held.extend(keys)
                out[number] = i + 1
                break
        else:
            waves.append(list(keys))
            out[number] = len(waves)
    return out


def build_plan(
    picked: Sequence[Issue],
    skipped: Sequence[Skipped],
    *,
    repo: str,
    config: Config,
    store: Store | None = None,
    router: Router = label_router,
    filters: Filters = Filters(),
) -> Plan:
    files = {i.number: locks.predict_files(i.title, i.body) for i in picked}
    waves = group_waves([(i.number, files[i.number]) for i in picked])
    items = []
    for issue in picked:
        tier, why = router(issue)
        tier_models = config.models_for_tier(tier)
        cloud = bool(tier_models) and tier_models[0].is_cloud
        items.append(Planned(issue, files[issue.number], tier, why, cloud, waves[issue.number]))
    avg, today, month = _spend_numbers(store, config, repo)
    return Plan(
        repo=repo,
        items=tuple(items),
        skipped=tuple(skipped),
        waves=max((w for w in waves.values() if w is not None), default=0),
        avg_cost_usd=avg,
        remaining_today_usd=today,
        remaining_month_usd=month,
        include_any_author=filters.include_any_author,
    )


def _spend_numbers(
    store: Store | None, config: Config, repo: str
) -> tuple[float | None, float | None, float | None]:
    if store is None:
        return None, None, None
    from foreman_inator.metrics.queries import MetricsFilter, cost_per_accepted_ticket
    from foreman_inator.spend import Spend

    mean = cost_per_accepted_ticket(store, MetricsFilter(repo=repo))
    if not mean.count:
        mean = cost_per_accepted_ticket(store)
    s = Spend(store, config).summary()
    return mean.value, s.remaining_today_usd, s.remaining_month_usd


def render_preview(plan: Plan) -> str:
    out = [f"fman sweep {plan.repo}: {len(plan.items)} issue(s) to label `{AGENT_LABEL}`"]
    if plan.include_any_author:
        out.append("  --include-any-author: issues by authors not on allowed_users are included")
    groups: dict[int | None, list[Planned]] = {}
    for p in plan.items:
        groups.setdefault(p.wave, []).append(p)
    for wave in [*range(1, plan.waves + 1), None]:
        members = groups.get(wave)
        if not members:
            continue
        head = f"Wave {wave}" if wave is not None else "Unknown files (may collide later)"
        out.append(f"{head} ({len(members)}):")
        for p in members:
            where = "cloud" if p.cloud else "local"
            title = " ".join(p.issue.title.split())[:60]
            out.append(f"  #{p.issue.number:<5} tier {p.tier} ({where}; {p.tier_reason})  {title}")
            if p.files:
                out.append(f"         files: {', '.join(p.files)}")
    if plan.skipped:
        out.append(f"Skipped ({len(plan.skipped)}):")
        out.extend(f"  #{s.issue.number:<5} {s.reason}" for s in plan.skipped)
    est = plan.estimate_usd
    est_text = "unknown (no accepted tickets yet)" if est is None else f"${est:.2f}"
    out.append(
        f"Total: {len(plan.items)} issue(s) in {plan.waves} wave(s)"
        f" + {sum(1 for p in plan.items if p.wave is None)} with unknown files; "
        f"{plan.cloud_issues} start on a cloud tier, estimated cloud spend {est_text}"
    )
    if plan.remaining_month_usd is not None:
        out.append(
            f"Spend caps left: ${plan.remaining_today_usd or 0:.2f} today, "
            f"${plan.remaining_month_usd:.2f} this month (cloud work past a cap waits for you)"
        )
    out.append(
        "Waves are dynamic: a ticket starts once its files are free; new PRs wait at "
        "max_open_prs_per_repo."
    )
    return "\n".join(out)


# --------------------------------------------------------------------------- apply


@dataclass
class Applied:
    labeled: list[int] = field(default_factory=list)
    failed: list[tuple[int, str]] = field(default_factory=list)
    stopped: str | None = None  # the auth error that stopped the run


def apply(
    plan: Plan,
    gh: Gh,
    *,
    echo: Callable[[str], None] = print,
    pace_seconds: float = DEFAULT_PACE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> Applied:
    """Label each planned issue ``agent``; stop on the first auth error."""
    result = Applied()
    total = len(plan.items)
    for i, p in enumerate(plan.items, 1):
        if i > 1 and pace_seconds > 0:
            sleep(pace_seconds)
        try:
            gh.add_label(plan.repo, p.issue.number)
        except GhAuthError as exc:
            result.stopped = str(exc)
            echo(f"[{i}/{total}] #{p.issue.number}: {exc}; stopping. Run `{GH_LOGIN_HINT}`.")
            break
        except GhError as exc:
            result.failed.append((p.issue.number, str(exc)))
            echo(f"[{i}/{total}] #{p.issue.number}: not labeled ({exc})")
            continue
        result.labeled.append(p.issue.number)
        echo(f"[{i}/{total}] #{p.issue.number}: labeled `{AGENT_LABEL}`")
    return result


def run(
    repo: str,
    *,
    config: Config,
    store: Store | None,
    gh: Gh | None = None,
    filters: Filters = Filters(),
    dry_run: bool = False,
    yes: bool = False,
    confirm: Callable[[str], bool] | None = None,
    echo: Callable[[str], None] = print,
    router: Router = label_router,
    pace_seconds: float = DEFAULT_PACE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """The whole `fman sweep`; returns the exit code (0 ok, 1 stopped or failed)."""
    gh = gh or Gh()
    try:
        login = gh.login()
    except GhAuthError as exc:
        echo(f"{exc}.\nLog in to the GitHub CLI as yourself first:\n  {GH_LOGIN_HINT}")
        echo("The sweep never uses the App's token: the worker ignores labels the bot applies.")
        return 1
    if not config.is_allowed_user(login):
        echo(
            f"gh is logged in as {login}, who is not on allowed_users: the worker would "
            "ignore the labels. Log in as an allowed user, or add them to allowed_users."
        )
        return 1
    try:
        issues = gh.list_issues(repo, label=filters.label, author=filters.author)
        prs = gh.fman_pr_issues(repo)
    except GhError as exc:
        echo(str(exc))
        return 1
    picked, skipped = select(
        issues, repo=repo, config=config, store=store, fman_prs=prs, filters=filters
    )
    plan = build_plan(
        picked, skipped, repo=repo, config=config, store=store, router=router, filters=filters
    )
    echo(render_preview(plan))
    if not plan.items:
        echo("Nothing to label.")
        return 0
    if dry_run:
        echo("Dry run: nothing labeled.")
        return 0
    if not yes:
        ask = confirm or (lambda q: input(f"{q} [y/N] ").strip().lower() in ("y", "yes"))
        if not ask(f"Label {len(plan.items)} issue(s) `{AGENT_LABEL}` as {login}?"):
            echo("Nothing labeled.")
            return 0
    result = apply(plan, gh, echo=echo, pace_seconds=pace_seconds, sleep=sleep)
    echo(f"Labeled {len(result.labeled)} of {len(plan.items)} issue(s).")
    return 1 if result.stopped or result.failed else 0


__all__ = [
    "AGENT_LABEL",
    "GH_LOGIN_HINT",
    "Applied",
    "Filters",
    "Gh",
    "GhAuthError",
    "GhError",
    "Issue",
    "Plan",
    "Planned",
    "Skipped",
    "apply",
    "build_plan",
    "classifier_router",
    "group_waves",
    "label_router",
    "render_preview",
    "run",
    "select",
]
