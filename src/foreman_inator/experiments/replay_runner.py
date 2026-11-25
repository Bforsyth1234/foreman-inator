"""Writer and reviewer replay runs (todo #25): champion vs challenger on one replay set.

``run_replay(deps, role=..., champion=..., challenger=...)`` is what ``fman experiment
run`` and the overnight launchd job call (#29): it takes the role's current set (rebuilt
monthly, ``replay_sets.ensure_current_set``), resumes an unfinished run of the same pair
or starts one (an ``experiments`` row, mode ``replay``, pinned to the set in
``replay_runs``), runs every case for both sides, and writes the comparison to the
experiment's ``result_note``. ``replay_status`` and ``compare`` read it back.

Writer replay, one case side (``_writer_side``), the live attempt path minus everything
that touches GitHub or the live tables:

1. a scratch worktree at the case's base commit on ``fman/replay/e<exp>-c<case>-<side>``
   (``RepoMirror.scratch``: can commit, can never push);
2. the writer prompt from the case's recorded inputs and the variant's template
   (``build_writer_prompt(..., template=)``, situation "first"), the repo's
   AGENTS.md/CLAUDE.md read from the worktree at the base commit, as live;
3. the driver (OpenCode), with the model reached through a proxy context costed to the
   ``experiments`` bucket (``proxy.context(bucket="experiments")``; no attempt id, so the
   proxy writes no usage to the live ``attempts`` table), under the caller's sandbox
   policy (``writer=True``: network to the proxy only);
4. scoring: the case's test files (the merged PR's tests, content at the merge) are
   written over whatever the writer left at those paths, then the verify command runs
   under the sandbox. passed / failed; ``no_change``, ``agent_failed`` and
   ``timed_out`` also count as not passed. A side that could not run at all
   (``worker_setup.could_not_run``: OpenCode's server, session or prompt failed before
   any model call, #53) is an ``error``, which keeps the case out of the comparison.
   Cost and tokens are the proxy's numbers for that context; time is the side's wall
   time.

Reviewer replay, one case side (``_reviewer_side``): ``review_change`` with ``store=None``
(nothing written), ``bucket="experiments"`` and the variant's prompt on the case's base
and head commits, under ``sandbox.active(policy)``. Scores against what the user did:

=============  ===================  =================  =========
truth          request_changes      approve            handed_to_user
=============  ===================  =================  =========
kicked_back    ``catch``            ``miss``           ``handed``
merged         ``false_kickback``   ``correct_approve`` ``handed``
=============  ===================  =================  =========

Comparison (``compare``), over the cases both sides scored (an ``error`` side keeps the
case out): per case the writer side scores 1 when it passed, the reviewer side 1 for a
catch or a correct approve; the challenger wins the case when it scores higher, loses
when lower, else a tie. The verdict is the "wins on replay" half of SPEC's promotion
rule, mirroring its live half (accept rate not lower, cost per accepted not higher):

* writer: challenger passes >= champion passes, cost per passed case not higher, and
  strictly better on at least one of passes, cost per passed case, total time;
* reviewer: challenger catches >= champion's, false kickbacks <= champion's, total cost
  not higher, and strictly better on at least one of catches, false kickbacks, cost;
* fewer than ``min_cases`` scored on both sides: inconclusive (SPEC "Sample size").

The comparison is a pure function of the stored result rows (sorted by case), so the
same case outcomes give the same note. Promotion itself (``holds live``) is #29's.

Unattended (overnight):

* bounds: ``writer_timeout_s`` for the driver, ``verify_timeout_s`` for scoring,
  ``review_timeout_s`` per review; ``run_budget_usd`` caps one invocation's cloud spend;
* a spend refusal in the experiments bucket (the proxy's 429 ``spend_cap_reached``)
  stops the run cleanly: the cut-short side is thrown away, the run row says
  ``stopped`` and the note starts ``stopped: experiments budget``. Nobody is asked, and
  the contexts are only ever ``experiments``-bucket (a non-experiments context of a
  replay is refused, fail closed). The run budget stops it the same way (``stopped:
  run budget``);
* resumable after a kill: a side is written to ``replay_results`` only once it is
  scored (UNIQUE experiment, case, side), and a rerun skips every (case, side) that has
  a row; a killed side's scratch worktree is replaced when the side is rerun, and every
  leftover ``scratch-replay-e<exp>-*`` is swept when the run ends;
* cancellable: ``cancel`` (a ``threading.Event``) is passed to the driver, verify and
  the reviewer, which kill their process groups; the side is thrown away, the worktree
  removed, the run row says ``cancelled``;
* sandbox: every launch gets a policy from ``deps.sandbox`` and
  ``sandbox.ensure_available`` is checked first; if the sandbox cannot run, the run stops
  (``stopped: sandbox unavailable``) and nothing runs unsandboxed.

Rules override (#43, todo #66), ``fman experiment run --rules <file>``: a
``RulesOverride`` on the challenger (``with_rules``) replaces the gathered conventions
(global rules, rules_file, AGENTS.md/CLAUDE.md, nested files) on the challenger side
only; the champion keeps the production rules, and model and prompt stay the same on
both sides, so the comparison isolates the rules. The override text's hash is part of
the challenger's variant id (``<model>/<prompt>@<hash8>+rules@<rules hash8>``) and its
full sha256 is in the experiment's result note, so a later edit of the file is a new
variant and never changes an old result. The reviewer side passes the override as
``review_change``'s ``conventions`` argument; it reaches the model only through a
template with a ``conventions`` section (``reviewer-v2``), so a reviewer rules replay
whose challenger prompt has none is refused up front (``check_reviewer_rules``). Both
reviewer sides read the repo's ``rules_file``, as the live review does. A reviewer
result's ``detail`` ends with ``[tag: <tag>]`` when the review tagged a kickback, so
``SideTotals.style_tags`` counts ``style`` kickbacks.

Seeded bugs (#35, todo #35), ``run_seeded``: the merged cases of a reviewer set, each with
one bug planted by ``seeded.seed_case`` (an added if-guard dropped, an added check
neutralised, a comparison flipped; only patches that pass ``git apply --check`` against
the base), run through ``_reviewer_side`` on both variants with expected verdict
``request_changes``; an ``experiments`` row with mode ``seeded``. The note reports the
catch rate per variant and per mutation kind (see the section comment).

Never: a push, a PR, a GitHub call of any kind (this module takes no GitHub client), or a
write to ``tickets``/``attempts``/``reviews``/``kickbacks``. Its writes go to
``experiments`` (the row and its ``result_note``) and the 005 replay tables only.
"""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import logging
import re
import shutil
import threading
import time
import weakref
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from fde_kit.prompts import PromptRef
from opentelemetry import trace as otel_trace

from foreman_inator import paths
from foreman_inator.config import Config, ModelEndpoint
from foreman_inator.db.store import Attempt, Store, Ticket
from foreman_inator.review import reviewer as rv
from foreman_inator.runner import sandbox
from foreman_inator.runner import verify as verify_mod
from foreman_inator.runner.worktree import RepoMirror, Worktree, WorktreeError
from foreman_inator.tickets import conventions as conventions_mod
from foreman_inator.tickets import images as images_mod
from foreman_inator.tickets.prompt import CONVENTIONS_BUDGET, build_writer_prompt, clip_head
from foreman_inator.worker_setup import setup_failure

from . import replay_sets as rs
from . import seeded as sd

log = logging.getLogger("foreman_inator.experiments.replay")
_tracer = otel_trace.get_tracer("foreman_inator.experiments.replay")

Role = Literal["writer", "reviewer"]
Side = Literal["champion", "challenger"]
SIDES: tuple[Side, ...] = ("champion", "challenger")
Verdict = Literal["wins", "does_not_win", "inconclusive"]

STOP_EXPERIMENTS_BUDGET = "stopped: experiments budget"
STOP_RUN_BUDGET = "stopped: run budget"
STOP_SANDBOX = "stopped: sandbox unavailable"
STOP_CANCELLED = "cancelled"

WRITER_FAIL_STATUSES = ("failed", "no_change", "agent_failed", "timed_out")
RULES_SUFFIX = "+rules@"  # challenger variant id: <model>/<prompt>@<hash8>+rules@<hash8>
RULES_HASH_LEN = 8
TAG_MARK = "[tag: {tag}]"  # appended to a reviewer result's detail when the review tagged
#: Why a reviewer rules replay with a prompt lacking a ``conventions`` section is refused.
NO_CONVENTIONS_SECTION = (
    "reviewer rules replays need a prompt with a conventions section, such as reviewer-v2;"
    " {prompt} has none, so the override would reach no model and the replay would compare"
    " nothing. Pass --challenger <model>/reviewer-v2"
)


def check_reviewer_rules(prompt_name: str) -> None:
    """#43: refuse a reviewer rules override whose prompt never shows the conventions."""
    if not rv.wants_conventions(rv.load_reviewer_template(prompt_name)):
        raise ValueError(NO_CONVENTIONS_SECTION.format(prompt=prompt_name))


_EPS = 1e-9


# --------------------------------------------------------------------------- settings


@dataclass(frozen=True)
class ReplaySettings:
    """What #29 puts under ``[experiments]`` in config.toml (defaults here)."""

    set_size: int = rs.DEFAULT_SET_SIZE  # latest merges a set is built from
    run_budget_usd: float = 2.0  # cloud spend one run may use (inside the monthly budget)
    writer_timeout_s: float = 20 * 60.0  # the writer's OpenCode session, per case side
    verify_timeout_s: float = 10 * 60.0  # scoring: the merged tests, per case side
    review_timeout_s: float = 30 * 60.0  # one replayed review, per case side
    min_cases: int = 10  # cases scored on both sides before a verdict


@dataclass(frozen=True, slots=True)
class RulesOverride:
    """A rules file replayed on the challenger side in place of the gathered conventions
    (#43). ``text`` is the file's content when the run started; ``name`` is shown as the
    source's path in the prompt."""

    text: str
    name: str = "AGENTS.md"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def hash8(self) -> str:
        return self.sha256[:RULES_HASH_LEN]

    @classmethod
    def from_path(cls, path: str | Path) -> RulesOverride:
        p = Path(path).expanduser()
        return cls(p.read_text(encoding="utf-8"), p.name)

    def conventions(self, budget: int = CONVENTIONS_BUDGET) -> conventions_mod.Conventions:
        """The override as the only rules source, clipped to the shared budget."""
        text = self.text.replace("\r\n", "\n").strip()
        if not text:
            return conventions_mod.Conventions(budget=budget)
        shown = clip_head(text, budget)
        clipped = shown != text
        src = conventions_mod.Source(
            kind="repo",
            path=self.name,
            label=f"Repository file {self.name}.",
            text=shown,
            chars=len(text),
            clipped=clipped,
        )
        clips = (conventions_mod.Clip("repo", self.name, len(text), len(shown)),) if clipped else ()
        if clipped:
            log.warning("rules override %s clipped to %d characters", self.name, budget)
        return conventions_mod.Conventions(sources=(src,), clips=clips, budget=budget)


def split_rules_id(variant_id: str) -> tuple[str, str | None]:
    """``(base variant id, rules hash8 or None)`` for a possibly ``+rules@`` id."""
    base, sep, h = variant_id.partition(RULES_SUFFIX)
    return (base, h) if sep else (variant_id, None)


@dataclass(frozen=True)
class WriterVariant:
    """A writer variant as the replay needs it: the id (#24's format), the model, and the
    prompt template (``PromptRef``; ``tickets.prompt.load_writer_template(name)``)."""

    variant_id: str
    endpoint: ModelEndpoint
    template: PromptRef
    rules: RulesOverride | None = None  # #43: challenger-side conventions override


@dataclass(frozen=True)
class ReviewerVariant:
    """A reviewer variant: the id, the reviewer model, the reviewer prompt's name."""

    variant_id: str
    endpoint: ModelEndpoint
    prompt_name: str = rv.REVIEWER_PROMPT_NAME
    rules: RulesOverride | None = None  # #43: challenger-side conventions override


Variant = WriterVariant | ReviewerVariant


def with_rules(variant: Variant, rules: RulesOverride) -> Variant:
    """``variant`` (same model, same prompt) with a rules override; its id gains
    ``+rules@<hash8>`` so a different rules text is a different variant."""
    from dataclasses import replace

    base, _ = split_rules_id(variant.variant_id)
    return replace(variant, variant_id=f"{base}{RULES_SUFFIX}{rules.hash8}", rules=rules)


class SandboxFactory(Protocol):
    """One launch's policy: ``path`` read/write, its own scratch dir named by ``key``,
    network for the writer (``writer=True``: the proxy port only) or for checks."""

    def __call__(self, repo: str, path: Path, key: str, *, writer: bool) -> sandbox.Policy: ...


def config_sandbox(config: Config, scratch_root: Path | None = None) -> SandboxFactory:
    """The worker's policy rules (``worker_sandbox._sandbox_policy``) from ``config``:
    ``config.sandbox`` on/off, writer network none, checks the repo's
    ``sandbox_network``; scratch dirs under ``~/.foreman-inator/sandbox/`` by default."""

    def factory(repo: str, path: Path, key: str, *, writer: bool) -> sandbox.Policy:
        root = scratch_root if scratch_root is not None else paths.home() / "sandbox"
        scratch = Path(root) / key
        if config.sandbox == "off":
            return sandbox.Policy(worktree=Path(path), scratch=scratch, mode="off")
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
        network = "none" if writer else config.sandbox_network_for(repo)
        return sandbox.Policy.create(path, scratch, mode="on", network=network)  # type: ignore[arg-type]

    return factory


@dataclass
class ReplayDeps:
    """The collaborators a replay run uses. ``proxy`` is the running ``ModelProxy``
    (its spend hook is the real ``Spend.hook``); ``mirror_for(repo)`` gives the repo's
    mirror (read, never fetched); ``driver`` runs the writer (OpenCode); ``endpoint_for``
    looks up a recorded writer model by name (the reviewer's family check);
    ``work_dir`` holds verify logs; ``spend`` (optional) lets a run stop before a cloud
    case when the experiments budget has no headroom left."""

    store: Store
    proxy: Any
    mirror_for: rs.MirrorFor
    sandbox: SandboxFactory
    work_dir: Path
    driver: Any = None
    endpoint_for: Callable[[str], ModelEndpoint] | None = None
    spend: Any = None
    recordings_dir: Path | None = None  # read for head commits when building sets
    clock: Callable[[], float] = time.monotonic
    config: Any = None  # the repos' ``rules_file`` (``rules_file_for``); None = no config
    #: #78: where the live attempts stored the issue's described images
    #: (``images.stored_text``); None = no image block in replayed prompts.
    traces_dir: Path | None = None


def _forget_queue_wait(proxy: Any, run_id: int) -> None:
    """#34: the run is over; drop its queue-wait total so the proxy's map stays small."""
    forget = getattr(proxy, "forget_queue_wait", None)
    if forget is not None:
        forget(run_id)


def _rules_file(config: Any, repo: str) -> Any:
    getter = getattr(config, "rules_file_for", None)
    return getter(repo) if getter is not None else None


# --------------------------------------------------------------------------- results


@dataclass(frozen=True, slots=True)
class CaseResult:
    experiment_id: int
    case_id: int
    side: Side
    variant_id: str
    status: str
    verdict: str | None
    score: str | None
    tokens_in: int
    tokens_out: int
    cost_usd: float
    seconds: float
    detail: str | None


@dataclass(frozen=True, slots=True)
class SideTotals:
    variant_id: str
    cases: int = 0
    passed: int = 0  # writer
    catches: int = 0  # reviewer
    misses: int = 0
    false_kickbacks: int = 0
    correct_approves: int = 0
    handed: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    style_tags: int = 0  # reviewer: kickbacks tagged ``style`` (#43)

    @property
    def correct(self) -> int:
        return self.catches + self.correct_approves

    @property
    def cost_per_pass(self) -> float:
        return self.cost_usd / self.passed if self.passed else float("inf")


@dataclass(frozen=True, slots=True)
class Comparison:
    role: Role
    experiment_id: int
    set_id: int
    total_cases: int  # ok cases in the set
    scored_cases: int  # scored on both sides
    errored_cases: int  # at least one side could not be run
    wins: int
    losses: int
    ties: int
    champion: SideTotals
    challenger: SideTotals
    verdict: Verdict
    reason: str
    state: str
    stop_reason: str | None


@dataclass(frozen=True, slots=True)
class RunStatus:
    experiment_id: int
    role: Role
    set_id: int
    state: str
    stop_reason: str | None
    total_sides: int
    done_sides: int
    comparison: Comparison
    result_note: str | None


class _Stop(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------- spend tap


class _Budget:
    def __init__(self, limit: float | None):
        self.limit = limit
        self.spent = 0.0
        self.lock = threading.Lock()

    def exhausted(self) -> bool:
        return self.limit is not None and self.spent >= self.limit - _EPS


class _Meter:
    def __init__(self, budget: _Budget):
        self.budget = budget
        self.tokens_in = self.tokens_out = self.calls = 0
        self.cost = 0.0
        self.refused: str | None = None


class _Tap:
    """Wraps a proxy's spend hook and usage callback (both public attributes read per
    call) so a replay can meter its own calls, enforce the per-run budget, and see a
    refusal. Calls of anything else pass straight through to the originals."""

    def __init__(self, proxy: Any):
        self._orig_hook = proxy.spend_hook
        self._orig_usage = proxy.on_usage
        self._lock = threading.Lock()
        self._by_token: dict[str, _Meter] = {}
        self._by_attempt: dict[int, _Meter] = {}
        self.hook_fn = self.hook
        self.usage_fn = self.usage
        proxy.spend_hook = self.hook_fn
        proxy.on_usage = self.usage_fn

    def _meter(self, ctx: Any) -> _Meter | None:
        with self._lock:
            token = getattr(ctx, "token", None)
            if token is not None and token in self._by_token:
                return self._by_token[token]
            aid = getattr(ctx, "attempt_id", None)
            return self._by_attempt.get(aid) if aid is not None else None

    @contextlib.contextmanager
    def watch(self, budget: _Budget, *, attempt_id: int | None = None) -> Iterator[_Meter]:
        meter = _Meter(budget)
        tokens: list[str] = []
        meter.add_token = lambda t: self._add_token(t, meter, tokens)  # type: ignore[attr-defined]
        if attempt_id is not None:
            with self._lock:
                self._by_attempt[attempt_id] = meter
        try:
            yield meter
        finally:
            with self._lock:
                for t in tokens:
                    self._by_token.pop(t, None)
                if attempt_id is not None:
                    self._by_attempt.pop(attempt_id, None)

    def _add_token(self, token: str, meter: _Meter, tokens: list[str]) -> None:
        with self._lock:
            self._by_token[token] = meter
            tokens.append(token)

    def hook(self, endpoint: Any, cost_usd: float, context: Any, *, phase: str = "estimate"):
        m = self._meter(context)
        if m is not None and phase == "estimate":
            if getattr(context, "bucket", None) != "experiments":
                m.refused = STOP_EXPERIMENTS_BUDGET  # never the live bucket: fail closed
                return False
            b = m.budget
            if b.limit is not None and cost_usd > 0:
                with b.lock:
                    if b.spent + cost_usd > b.limit + _EPS:
                        m.refused = STOP_RUN_BUDGET
                        return False
            try:
                allowed = self._orig_hook(endpoint, cost_usd, context, phase=phase)
            except Exception:
                m.refused = STOP_EXPERIMENTS_BUDGET
                raise
            if allowed is False:
                m.refused = STOP_EXPERIMENTS_BUDGET
            return allowed
        return self._orig_hook(endpoint, cost_usd, context, phase=phase)

    def usage(self, record: Any) -> None:
        m = self._meter(record.context)
        if m is not None:
            m.calls += 1
            m.tokens_in += int(record.tokens_in)
            m.tokens_out += int(record.tokens_out)
            m.cost += float(record.cost_usd)
            with m.budget.lock:
                m.budget.spent += float(record.cost_usd)
        if self._orig_usage is not None:
            self._orig_usage(record)


_taps: weakref.WeakKeyDictionary[Any, _Tap] = weakref.WeakKeyDictionary()
_taps_lock = threading.Lock()


def _tap_for(proxy: Any) -> _Tap:
    with _taps_lock:
        tap = _taps.get(proxy)
        if tap is None or proxy.spend_hook is not tap.hook_fn or proxy.on_usage is not tap.usage_fn:
            tap = _Tap(proxy)
            _taps[proxy] = tap
        return tap


#: Replay "attempt ids" for the driver's state dir and the reviewer's proxy context:
#: negative, so they never match a live attempt (live ids are positive).
_synthetic = itertools.count(1)


def _synthetic_id() -> int:
    return -(1_000_000 + next(_synthetic))


# --------------------------------------------------------------------------- store rows


def _ts(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def start_replay(
    store: Store, *, role: Role, champion_variant: str, challenger_variant: str, set_id: int
) -> int:
    """A new ``experiments`` row (mode replay) pinned to ``set_id``. Returns its id."""
    if champion_variant == challenger_variant:
        raise ValueError("champion and challenger must be different variants")
    rset = rs.get_set(store, set_id)
    if rset.role != role:
        raise ValueError(f"set {set_id} holds {rset.role} cases, not {role}")
    now = _ts(store.now())
    with store.transaction() as conn:
        exp = store.create_experiment(
            role=role,
            champion_variant=champion_variant,
            challenger_variant=challenger_variant,
            mode="replay",
        )
        conn.execute(
            "INSERT INTO replay_runs (experiment_id, set_id, role, state, started_at, updated_at)"
            " VALUES (?, ?, ?, 'running', ?, ?)",
            (exp.id, set_id, role, now, now),
        )
    return exp.id


def find_resumable(
    store: Store, *, role: Role, champion_variant: str, challenger_variant: str
) -> int | None:
    """The newest unfinished replay run of this pair (killed, stopped or cancelled)."""
    rows = store.query(
        "SELECT e.id FROM experiments e JOIN replay_runs r ON r.experiment_id = e.id"
        " WHERE e.role = ? AND e.mode = 'replay' AND e.status = 'running'"
        " AND e.champion_variant = ? AND e.challenger_variant = ? AND r.state != 'finished'"
        " ORDER BY e.id DESC LIMIT 1",
        (role, champion_variant, challenger_variant),
    )
    return int(rows[0]["id"]) if rows else None


def _run_row(store: Store, experiment_id: int) -> Any:
    rows = store.query("SELECT * FROM replay_runs WHERE experiment_id = ?", (experiment_id,))
    if not rows:
        raise LookupError(f"experiment {experiment_id} is not a replay run")
    return rows[0]


def _set_state(store: Store, experiment_id: int, state: str, stop_reason: str | None) -> None:
    with store.transaction() as conn:
        conn.execute(
            "UPDATE replay_runs SET state = ?, stop_reason = ?, updated_at = ?"
            " WHERE experiment_id = ?",
            (state, stop_reason, _ts(store.now()), experiment_id),
        )


def _save(store: Store, r: CaseResult) -> None:
    with store.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO replay_results (experiment_id, case_id, side, variant_id,"
            " status, verdict, score, tokens_in, tokens_out, cost_usd, seconds, detail,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r.experiment_id, r.case_id, r.side, r.variant_id, r.status, r.verdict, r.score,
                r.tokens_in, r.tokens_out, round(r.cost_usd, 9), round(r.seconds, 3),
                r.detail, _ts(store.now()),
            ),
        )  # fmt: skip


def results(store: Store, experiment_id: int) -> list[CaseResult]:
    rows = store.query(
        "SELECT * FROM replay_results WHERE experiment_id = ? ORDER BY case_id, side",
        (experiment_id,),
    )
    return [
        CaseResult(
            experiment_id=int(r["experiment_id"]),
            case_id=int(r["case_id"]),
            side=r["side"],
            variant_id=r["variant_id"],
            status=r["status"],
            verdict=r["verdict"],
            score=r["score"],
            tokens_in=int(r["tokens_in"]),
            tokens_out=int(r["tokens_out"]),
            cost_usd=float(r["cost_usd"]),
            seconds=float(r["seconds"]),
            detail=r["detail"],
        )
        for r in rows
    ]


# --------------------------------------------------------------------------- comparison


def _point(role: Role, r: CaseResult) -> int:
    if role == "writer":
        return 1 if r.status == "passed" else 0
    return 1 if r.score in ("catch", "correct_approve") else 0


def _totals(variant_id: str, rows: Sequence[CaseResult]) -> SideTotals:
    def n(pred: Callable[[CaseResult], bool]) -> int:
        return sum(1 for r in rows if pred(r))

    return SideTotals(
        variant_id=variant_id,
        cases=len(rows),
        passed=n(lambda r: r.status == "passed"),
        catches=n(lambda r: r.score == "catch"),
        misses=n(lambda r: r.score == "miss"),
        false_kickbacks=n(lambda r: r.score == "false_kickback"),
        correct_approves=n(lambda r: r.score == "correct_approve"),
        handed=n(lambda r: r.score == "handed"),
        cost_usd=round(sum(r.cost_usd for r in rows), 9),
        seconds=round(sum(r.seconds for r in rows), 3),
        tokens_in=sum(r.tokens_in for r in rows),
        tokens_out=sum(r.tokens_out for r in rows),
        style_tags=n(lambda r: (r.detail or "").endswith(TAG_MARK.format(tag="style"))),
    )


def _lt(a: float, b: float) -> bool:
    if a == b:  # both inf included
        return False
    return a < b - _EPS


def _le(a: float, b: float) -> bool:
    return a == b or a <= b + _EPS


def verdict_for(
    role: Role, champ: SideTotals, chall: SideTotals, scored: int, min_cases: int
) -> tuple[Verdict, str]:
    """The "wins on replay" half of SPEC's promotion rule (module docstring)."""
    if scored < max(1, min_cases):
        return "inconclusive", f"only {scored} case(s) scored on both sides; {min_cases} needed"
    if role == "writer":
        if chall.passed < champ.passed:
            return "does_not_win", "it passed fewer of the merged PRs' tests"
        if not _le(chall.cost_per_pass, champ.cost_per_pass):
            return "does_not_win", "its cost per passed case is higher"
        if chall.passed > champ.passed:
            return "wins", "it passed more of the merged PRs' tests at no higher cost per pass"
        if _lt(chall.cost_per_pass, champ.cost_per_pass):
            return "wins", "it passed as many cases at a lower cost per pass"
        if _lt(chall.seconds, champ.seconds):
            return "wins", "it passed as many cases at the same cost, faster"
        return "does_not_win", "no better than the champion on passes, cost or time"
    if chall.catches < champ.catches:
        return "does_not_win", "it caught fewer of the changes the user kicked back"
    if chall.false_kickbacks > champ.false_kickbacks:
        return "does_not_win", "it kicked back more changes the user merged"
    if not _le(chall.cost_usd, champ.cost_usd):
        return "does_not_win", "it costs more"
    if chall.catches > champ.catches or chall.false_kickbacks < champ.false_kickbacks:
        return "wins", "more catches or fewer false kickbacks, at no higher cost"
    if _lt(chall.cost_usd, champ.cost_usd):
        return "wins", "the same catches and false kickbacks at a lower cost"
    return "does_not_win", "no better than the champion on catches, false kickbacks or cost"


def compare(store: Store, experiment_id: int, *, min_cases: int = 10) -> Comparison:
    """The comparison so far, from the stored rows only (deterministic)."""
    exp = store.get_experiment(experiment_id)
    run = _run_row(store, experiment_id)
    role: Role = run["role"]
    by_case: dict[int, dict[str, CaseResult]] = {}
    for r in results(store, experiment_id):
        by_case.setdefault(r.case_id, {})[r.side] = r
    paired: list[tuple[CaseResult, CaseResult]] = []
    errored = 0
    for case_id in sorted(by_case):
        sides = by_case[case_id]
        if any(s.status == "error" for s in sides.values()):
            errored += 1
            continue
        if "champion" in sides and "challenger" in sides:
            paired.append((sides["champion"], sides["challenger"]))
    wins = sum(1 for a, b in paired if _point(role, b) > _point(role, a))
    losses = sum(1 for a, b in paired if _point(role, b) < _point(role, a))
    champ = _totals(exp.champion_variant, [a for a, _ in paired])
    chall = _totals(exp.challenger_variant, [b for _, b in paired])
    verdict, reason = verdict_for(role, champ, chall, len(paired), min_cases)
    total = len(rs.list_cases(store, int(run["set_id"]), experiment_id=experiment_id))
    return Comparison(
        role=role,
        experiment_id=experiment_id,
        set_id=int(run["set_id"]),
        total_cases=total,
        scored_cases=len(paired),
        errored_cases=errored,
        wins=wins,
        losses=losses,
        ties=len(paired) - wins - losses,
        champion=champ,
        challenger=chall,
        verdict=verdict,
        reason=reason,
        state=run["state"],
        stop_reason=run["stop_reason"],
    )


def _money(x: float) -> str:
    return "n/a" if x == float("inf") else f"${x:.4f}"


def comparison_note(c: Comparison) -> str:
    """The plain-text comparison written to ``experiments.result_note``."""
    lines: list[str] = []
    if c.stop_reason:
        lines.append(f"{c.stop_reason} (resume to finish the set).")
    elif c.state == "running":
        lines.append("running.")
    verdict = {
        "wins": "challenger wins on replay",
        "does_not_win": "challenger does not win on replay",
        "inconclusive": "inconclusive",
    }[c.verdict]
    lines.append(
        f"{c.role} replay, set {c.set_id}: {c.scored_cases} of {c.total_cases} case(s) scored on"
        f" both sides ({c.errored_cases} could not run). Verdict: {verdict} ({c.reason})."
    )
    lines.append(f"Per case: {c.wins} win(s), {c.losses} loss(es), {c.ties} tie(s).")
    a, b = c.champion, c.challenger
    if c.role == "writer":
        lines.append(
            f"Passed the merged tests: champion {a.passed}/{a.cases}, challenger"
            f" {b.passed}/{b.cases}."
        )
        lines.append(
            f"Cost: champion {_money(a.cost_usd)} ({_money(a.cost_per_pass)} per pass),"
            f" challenger {_money(b.cost_usd)} ({_money(b.cost_per_pass)} per pass)."
        )
        lines.append(
            f"Tokens in/out: champion {a.tokens_in}/{a.tokens_out}, challenger"
            f" {b.tokens_in}/{b.tokens_out}."
        )
    else:
        lines.append(
            f"Catches: champion {a.catches}, challenger {b.catches}; false kickbacks:"
            f" champion {a.false_kickbacks}, challenger {b.false_kickbacks}; misses:"
            f" champion {a.misses}, challenger {b.misses}; handed to user: champion"
            f" {a.handed}, challenger {b.handed}."
        )
        lines.append(
            f"Style-tagged kickbacks: champion {a.style_tags}/{a.cases}, challenger"
            f" {b.style_tags}/{b.cases}."
        )
        lines.append(f"Cost: champion {_money(a.cost_usd)}, challenger {_money(b.cost_usd)}.")
    lines.append(f"Time: champion {a.seconds:.1f}s, challenger {b.seconds:.1f}s.")
    lines.append(f"Champion {a.variant_id}; challenger {b.variant_id}.")
    if c.verdict == "wins":
        lines.append(
            "Promotion: wins on replay; it must still hold up live (shadow or traffic split)."
        )
    return "\n".join(lines)


def replay_status(store: Store, experiment_id: int, *, min_cases: int = 10) -> RunStatus:
    """What ``fman experiment status`` shows for a replay run."""
    run = _run_row(store, experiment_id)
    cmp_ = compare(store, experiment_id, min_cases=min_cases)
    done = store.query(
        "SELECT COUNT(*) AS n FROM replay_results WHERE experiment_id = ?", (experiment_id,)
    )[0]["n"]
    return RunStatus(
        experiment_id=experiment_id,
        role=run["role"],
        set_id=int(run["set_id"]),
        state=run["state"],
        stop_reason=run["stop_reason"],
        total_sides=2 * cmp_.total_cases,
        done_sides=int(done),
        comparison=cmp_,
        result_note=store.get_experiment(experiment_id).result_note,
    )


# --------------------------------------------------------------------------- the run


def run_replay(
    deps: ReplayDeps,
    *,
    role: Role,
    champion: Variant,
    challenger: Variant,
    settings: ReplaySettings | None = None,
    experiment_id: int | None = None,
    set_id: int | None = None,
    cancel: threading.Event | None = None,
) -> Comparison:
    """Run (or resume) one replay comparison to the end, a stop or a cancel.

    With no ``experiment_id``: resume the newest unfinished run of this pair, else start
    one on ``set_id`` or on the role's current set (``ensure_current_set``, which rebuilds
    it monthly). Returns the comparison, also written to the experiment's result_note."""
    settings = settings or ReplaySettings()
    cancel = cancel or threading.Event()
    store = deps.store
    _check_variants(role, champion, challenger)
    if experiment_id is None:
        experiment_id = find_resumable(
            store,
            role=role,
            champion_variant=champion.variant_id,
            challenger_variant=challenger.variant_id,
        )
    if experiment_id is None:
        if set_id is None:
            set_id = rs.ensure_current_set(
                store,
                role,
                mirror_for=deps.mirror_for,
                max_merges=settings.set_size,
                recordings_dir=deps.recordings_dir,
            ).id
        experiment_id = start_replay(
            store,
            role=role,
            champion_variant=champion.variant_id,
            challenger_variant=challenger.variant_id,
            set_id=set_id,
        )
    exp = store.get_experiment(experiment_id)
    if (exp.champion_variant, exp.challenger_variant) != (
        champion.variant_id,
        challenger.variant_id,
    ):
        raise ValueError(f"experiment {experiment_id} compares other variants")
    run = _run_row(store, experiment_id)
    if run["role"] != role:
        raise ValueError(f"experiment {experiment_id} is a {run['role']} replay")
    if run["state"] != "finished":
        _set_state(store, experiment_id, "running", None)
        state, reason = _drive(deps, settings, experiment_id, int(run["set_id"]), role,
                               champion, challenger, cancel)  # fmt: skip
        _set_state(store, experiment_id, state, reason)
    c = compare(store, experiment_id, min_cases=settings.min_cases)
    store.set_experiment_note(experiment_id, comparison_note(c) + _rules_note(challenger))
    return c


def _rules_note(challenger: Variant) -> str:
    if challenger.rules is None:
        return ""
    return (
        f"\nRules override {challenger.rules.name} sha256 {challenger.rules.sha256}: challenger"
        " only, in place of the gathered rules; same model and prompt on both sides."
    )


def _check_variants(role: Role, champion: Variant, challenger: Variant) -> None:
    kind = WriterVariant if role == "writer" else ReviewerVariant
    for v in (champion, challenger):
        if not isinstance(v, kind):
            raise TypeError(f"a {role} replay needs {kind.__name__}s")
    if champion.variant_id == challenger.variant_id:
        raise ValueError("champion and challenger must be different variants")
    if champion.rules is not None:
        raise ValueError("a rules override goes on the challenger only")
    if challenger.rules is not None:
        if not challenger.variant_id.endswith(f"{RULES_SUFFIX}{challenger.rules.hash8}"):
            raise ValueError("the challenger's id must name its rules hash (with_rules)")
        if role == "reviewer":
            check_reviewer_rules(challenger.prompt_name)  # type: ignore[union-attr]


def _drive(
    deps: ReplayDeps,
    settings: ReplaySettings,
    experiment_id: int,
    set_id: int,
    role: Role,
    champion: Variant,
    challenger: Variant,
    cancel: threading.Event,
    cases: Sequence[rs.StoredCase] | None = None,
) -> tuple[str, str | None]:
    """Every unscored (case, side) in order. Returns the run's (state, stop_reason).
    ``cases`` (#35, seeded runs): the cases to run in place of the set's own."""
    store = deps.store
    done = {(r.case_id, r.side) for r in results(store, experiment_id)}
    budget = _Budget(settings.run_budget_usd)
    tap = _tap_for(deps.proxy)
    mirrors: dict[str, RepoMirror] = {}
    try:
        # #49: a local-only repo's cases run only when both sides are local models.
        cloud = bool(champion.endpoint.is_cloud or challenger.endpoint.is_cloud)
        if cases is None:
            cases = rs.list_cases(store, set_id, cloud_models=cloud, experiment_id=experiment_id)
        else:
            local = store.local_only_repos() if cloud else frozenset()
            cases = [c for c in cases if c.repo not in local]
        for case in cases:
            for side, variant in (("champion", champion), ("challenger", challenger)):
                if (case.id, side) in done:
                    continue
                if cancel.is_set():
                    return "cancelled", STOP_CANCELLED
                if budget.exhausted():
                    return "stopped", STOP_RUN_BUDGET
                if (
                    variant.endpoint.is_cloud
                    and deps.spend is not None
                    and not deps.spend.allow(0.0001, "experiments")
                ):
                    return "stopped", STOP_EXPERIMENTS_BUDGET
                # #43: the override is the challenger's alone; the champion always sees
                # the production rules.
                rules = challenger.rules if side == "challenger" else None
                ctx = _SideCtx(deps, settings, experiment_id, case, side, variant, cancel, tap,
                               budget, mirrors, rules=rules)  # fmt: skip
                try:
                    with _tracer.start_as_current_span(
                        "replay.case",
                        attributes={
                            "replay.experiment_id": experiment_id,
                            "replay.case_id": case.id,
                            "replay.side": side,
                            "replay.variant_id": variant.variant_id,
                            "replay.rules_sha256": rules.sha256 if rules else "",
                        },
                    ):
                        result = _writer_side(ctx) if role == "writer" else _reviewer_side(ctx)
                except _Stop as stop:
                    if stop.reason == STOP_CANCELLED:
                        return "cancelled", STOP_CANCELLED
                    return "stopped", stop.reason
                except sandbox.SandboxUnavailable as exc:
                    log.warning("replay %d: sandbox unavailable: %s", experiment_id, exc)
                    return "stopped", STOP_SANDBOX
                _save(store, result)
                log.info(
                    "replay %d: case %d %s %s%s",
                    experiment_id, case.id, side, result.score or result.status,
                    f" (conventions sha256 {ctx.conventions_sha256})"
                    if ctx.conventions_sha256 else "",
                )  # fmt: skip
        return "finished", None
    finally:
        for mirror in mirrors.values():
            _sweep(mirror, experiment_id)


@dataclass
class _SideCtx:
    deps: ReplayDeps
    settings: ReplaySettings
    experiment_id: int
    case: rs.StoredCase
    side: Side
    variant: Any
    cancel: threading.Event
    tap: _Tap
    budget: _Budget
    mirrors: dict[str, RepoMirror]
    policies: list[sandbox.Policy] = field(default_factory=list)
    rules: RulesOverride | None = None  # #43: replaces the gathered rules on this side
    conventions_sha256: str | None = None  # writer side: hash of the rules in its prompt

    @property
    def name(self) -> str:
        return f"replay-e{self.experiment_id}-c{self.case.id}-{self.side}"

    def result(self, status: str, t0: float, *, detail: str | None = None, **kw: Any) -> CaseResult:
        return CaseResult(
            experiment_id=self.experiment_id,
            case_id=self.case.id,
            side=self.side,
            variant_id=self.variant.variant_id,
            status=status,
            verdict=kw.get("verdict"),
            score=kw.get("score"),
            tokens_in=int(kw.get("tokens_in", 0)),
            tokens_out=int(kw.get("tokens_out", 0)),
            cost_usd=float(kw.get("cost_usd", 0.0)),
            seconds=max(0.0, self.deps.clock() - t0),
            detail=(detail or None) and detail[-2000:],
        )

    def mirror(self) -> RepoMirror | None:
        repo = self.case.repo
        if repo not in self.mirrors:
            try:
                m = self.deps.mirror_for(repo)
            except Exception:
                m = None
            if m is None or not (m.mirror_path / "HEAD").exists():
                return None
            self.mirrors[repo] = m
        return self.mirrors[repo]

    def policy(self, path: Path, suffix: str, *, writer: bool) -> sandbox.Policy:
        p = self.deps.sandbox(self.case.repo, Path(path), f"{self.name}-{suffix}", writer=writer)
        self.policies.append(p)
        sandbox.ensure_available(p)
        return p

    def drop_policies(self) -> None:
        for p in self.policies:
            if p.mode == "on":
                shutil.rmtree(p.scratch, ignore_errors=True)
        self.policies.clear()


def _sweep(mirror: RepoMirror, experiment_id: int) -> None:
    """Remove any leftover scratch worktree (and branch) of this experiment's replays."""
    for path in sorted(mirror.repo_dir.glob(f"scratch-replay-e{experiment_id}-*")):
        branch = f"fman/replay/{path.name.removeprefix('scratch-replay-')}"
        with contextlib.suppress(Exception):
            Worktree(mirror, path, branch, "", can_push=False).remove()
    with contextlib.suppress(Exception):
        mirror.prune()


# --------------------------------------------------------------------------- writer side


def _replay_ticket(wc: rs.WriterCase, now: datetime) -> Ticket:
    return Ticket(
        id=wc.ticket_id,
        title=wc.title,
        request=wc.request,
        repo=wc.repo,
        base_branch=wc.base_branch,
        issue_number=wc.issue_number,
        pr_number=None,
        owned_files=tuple(wc.owned_files),
        column="in_progress",
        source="issue",
        route_reason=None,
        trace_id=None,
        accepted_attempt_id=None,
        created_at=now,
        done_at=None,
        routed_tier=None,
        flag_reason=None,
        flagged_at=None,
    )


def _replay_attempt(wc: rs.WriterCase, ep: ModelEndpoint, wt: Worktree, now: datetime) -> Attempt:
    return Attempt(
        id=1,
        ticket_id=wc.ticket_id,
        model=ep.name,
        tier=ep.tier,
        variant_id=None,
        trigger="first",
        worktree=str(wt.path),
        branch=wt.branch,
        tests_passed=None,
        build_passed=None,
        diff_files=None,
        diff_lines=None,
        tokens_in=0,
        tokens_out=0,
        cost_usd=0.0,
        seconds=None,
        outcome=None,
        started_at=now,
        ended_at=None,
        base_commit=wt.base_sha,
        verify_output_path=None,
    )


def _copy_tests(wt: Worktree, tests: Sequence[rs.MergedTest]) -> None:
    root = Path(wt.path).resolve()
    for t in tests:
        dest = (root / t.path).resolve()
        if root not in dest.parents:
            raise WorktreeError(f"test path {t.path!r} leaves the worktree")
        if dest.is_symlink() or dest.is_dir():
            # the writer may have put a link or a folder there; the merged test wins
            if dest.is_dir() and not dest.is_symlink():
                shutil.rmtree(dest)
            else:
                dest.unlink()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(t.content, encoding="utf-8")
        dest.chmod(0o755 if t.executable else 0o644)


def _writer_side(ctx: _SideCtx) -> CaseResult:
    deps, settings = ctx.deps, ctx.settings
    wc = ctx.case.case
    assert isinstance(wc, rs.WriterCase)
    variant: WriterVariant = ctx.variant
    t0 = deps.clock()
    mirror = ctx.mirror()
    if mirror is None:
        return ctx.result("error", t0, detail=f"no mirror for {wc.repo} on this Mac")
    if deps.driver is None:
        raise ValueError("a writer replay needs deps.driver")
    wt: Worktree | None = None
    try:
        try:
            wt = mirror.scratch(wc.base_commit, ctx.name, branch=f"fman/replay/{ctx.name[7:]}")
        except (WorktreeError, ValueError) as exc:
            return ctx.result("error", t0, detail=f"could not check out the base commit: {exc}")
        cmd = (
            verify_mod.VerifyCommand.from_override(wc.verify_command)
            if wc.verify_command
            else verify_mod.detect(wt.path)
        )
        if cmd is None:
            return ctx.result("error", t0, detail="no verify command for the repo")
        now = deps.store.now()
        # The production prompt's rules (#38, #40): global rules, the repo's rules_file,
        # AGENTS.md + CLAUDE.md and nested files, as ``Worker._run_attempt`` gathers them.
        # #43: a challenger rules override replaces them, model and prompt unchanged.
        conv = (
            ctx.rules.conventions()
            if ctx.rules is not None
            else conventions_mod.gather(
                wt.path, wc.owned_files, rules_file=_rules_file(deps.config, wc.repo)
            )
        )
        ctx.conventions_sha256 = conv.sha256  # determinism: which rules it saw (logged)
        prompt = build_writer_prompt(
            _replay_ticket(wc, now),
            _replay_attempt(wc, variant.endpoint, wt, now),
            (),
            verify_command=cmd.display,
            conventions=conv,
            template=variant.template,
            clarification=wc.clarification,
            # #78: the descriptions the live attempt stored; no fetch, no model call.
            images=(
                images_mod.stored_text(deps.traces_dir, wc.ticket_id)
                if deps.traces_dir is not None
                else None
            ),
        )
        run_id = _synthetic_id()
        with (
            ctx.tap.watch(ctx.budget) as meter,
            deps.proxy.context(
                bucket="experiments",
                label=f"replay:{ctx.name}",
                prompt_version=f"{variant.template.name}@{variant.template.sha256[:12]}",
                writer_run=run_id,  # #34: queues for the local slot like a live writer
            ) as pctx,
        ):
            meter.add_token(pctx.token)  # type: ignore[attr-defined]
            policy = ctx.policy(wt.path, "writer", writer=True)
            try:
                run = deps.driver.run(
                    attempt_id=run_id,
                    worktree=wt,
                    prompt=prompt.text,
                    endpoint=variant.endpoint,
                    proxy_base_url=pctx.base_url,
                    timeout=settings.writer_timeout_s,
                    cancel=ctx.cancel,
                    sandbox=policy,
                )
            finally:
                _forget_queue_wait(deps.proxy, run_id)
        if meter.refused:
            raise _Stop(meter.refused)
        if ctx.cancel.is_set() or getattr(run, "cancelled", False):
            raise _Stop(STOP_CANCELLED)
        usage = {"tokens_in": meter.tokens_in, "tokens_out": meter.tokens_out,
                 "cost_usd": meter.cost}  # fmt: skip
        setup = setup_failure(run, proxy_tokens=meter.tokens_in + meter.tokens_out)
        if setup is not None:
            # #53: the side could not run (no server, session or prompt; no model call).
            # Not the variant's failure: an ``error`` side keeps the case out of the
            # comparison instead of scoring it 0 as ``agent_failed`` would.
            return ctx.result("error", t0, detail=setup.flag_reason, **usage)
        if not run.finished:
            status = "timed_out" if run.status == "timed_out" else "agent_failed"
            return ctx.result(status, t0, detail=run.error or run.status, **usage)
        if wt.diff_stats().diff_files == 0:
            return ctx.result("no_change", t0, detail="the writer changed no files", **usage)
        _copy_tests(wt, wc.test_files)
        out = Path(deps.work_dir) / f"e{ctx.experiment_id}" / f"c{ctx.case.id}-{ctx.side}.log"
        out.parent.mkdir(parents=True, exist_ok=True)
        vpolicy = ctx.policy(wt.path, "verify", writer=False)
        vr = verify_mod.run(
            cmd, wt.path, timeout=settings.verify_timeout_s, output_path=out,
            cancel=ctx.cancel, sandbox=vpolicy,
        )  # fmt: skip
        if ctx.cancel.is_set():
            raise _Stop(STOP_CANCELLED)
        detail = None if vr.passed else vr.output_tail(1500)
        return ctx.result("passed" if vr.passed else "failed", t0, detail=detail, **usage)
    finally:
        if wt is not None:
            with contextlib.suppress(Exception):
                wt.remove()
        ctx.drop_policies()


# --------------------------------------------------------------------------- reviewer side


def review_score(truth: str, verdict: str) -> str:
    """catch · miss · false_kickback · correct_approve · handed (module docstring)."""
    if verdict == "handed_to_user":
        return "handed"
    if truth == "kicked_back":
        return "catch" if verdict == "request_changes" else "miss"
    return "false_kickback" if verdict == "request_changes" else "correct_approve"


def _reviewer_side(ctx: _SideCtx) -> CaseResult:
    deps, settings = ctx.deps, ctx.settings
    rc = ctx.case.case
    assert isinstance(rc, rs.ReviewerCase)
    variant: ReviewerVariant = ctx.variant
    t0 = deps.clock()
    mirror = ctx.mirror()
    if mirror is None:
        return ctx.result("error", t0, detail=f"no mirror for {rc.repo} on this Mac")
    if deps.endpoint_for is None:
        raise ValueError("a reviewer replay needs deps.endpoint_for")
    try:
        writer = deps.endpoint_for(rc.writer_model)
    except Exception as exc:
        return ctx.result("error", t0, detail=f"writer model {rc.writer_model!r}: {exc}")
    try:
        rv.check_independent(variant.endpoint, writer)
    except rv.FamilyConflict as exc:
        return ctx.result("error", t0, detail=str(exc))
    work = Path(deps.work_dir) / f"e{ctx.experiment_id}" / f"c{ctx.case.id}-{ctx.side}-review"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    aid = _synthetic_id()
    try:
        policy = ctx.policy(work, "review", writer=False)
        extra: dict[str, Any] = {}
        if ctx.rules is not None:  # #43 (_check_variants: the prompt shows conventions)
            extra["conventions"] = ctx.rules.conventions()
            ctx.conventions_sha256 = extra["conventions"].sha256
        with ctx.tap.watch(ctx.budget, attempt_id=aid) as meter, sandbox.active(policy):
            outcome = rv.review_change(
                issue=rv.IssueText(rc.issue_number, rc.title, rc.request),
                change=rv.ChangeRef(mirror, rc.base_commit, rc.head_commit),
                writer=writer,
                reviewer=variant.endpoint,
                proxy=deps.proxy,
                attempt_id=aid,
                store=None,
                bucket="experiments",
                prompt_name=variant.prompt_name,
                verify_override=rc.verify_command,
                timeout=settings.review_timeout_s,
                check_timeout=min(settings.review_timeout_s, settings.verify_timeout_s),
                cancel=ctx.cancel,
                rules_file=_rules_file(deps.config, rc.repo),  # production's, as live
                **extra,
            )
        if meter.refused:
            raise _Stop(meter.refused)
        if ctx.cancel.is_set():
            raise _Stop(STOP_CANCELLED)
        note = outcome.note or ""
        if getattr(outcome, "tag", None):
            # kept at the end: CaseResult keeps the last 2000 characters of detail
            note = f"{note[-1900:]}\n{TAG_MARK.format(tag=outcome.tag)}".lstrip("\n")
        return ctx.result(
            "scored",
            t0,
            detail=note,
            verdict=outcome.verdict,
            score=review_score(rc.truth, outcome.verdict),
            tokens_in=outcome.tokens_in,
            tokens_out=outcome.tokens_out,
            cost_usd=outcome.cost_usd,
        )
    finally:
        ctx.drop_policies()
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- seeded bugs
#
# #35 (GH #17): a reviewer run on the merged cases of a reviewer set, each with one bug
# planted (``seeded.seed_case``), expected verdict ``request_changes``. One
# ``experiments`` row with mode ``seeded`` (the 001 CHECK already allows it), a
# ``replay_runs`` row pinned to the source set, and ``replay_results`` keyed by the
# source case's id (one seeded case per merged case, so UNIQUE(experiment, case, side)
# holds). The seed is the first line of the result note (``SEEDED_NOTE_HEAD``) and is
# read back on a resume; the same seed and commits rebuild the same seeded commits.
# Scoring: request_changes = ``catch``, approve = ``miss``, handed_to_user = ``handed``.
# Catch rate = catches / scored cases, per side and per mutation kind. Seeded runs never
# feed promotion (only mode ``replay`` and ``shadow`` do) and measure catches only.

SEEDED_NOTE_HEAD = "seeded run, seed {seed}, set {set_id}."
_SEEDED_HEAD_RE = re.compile(r"^seeded run, seed (-?\d+), set (\d+)\.")


@dataclass(frozen=True, slots=True)
class KindTotals:
    kind: str
    champion_catches: int = 0
    challenger_catches: int = 0
    cases: int = 0  # scored on both sides


@dataclass(frozen=True, slots=True)
class SeededComparison:
    experiment_id: int
    set_id: int
    seed: int
    merged_cases: int  # merged cases of the source set
    seeded_cases: int  # of them, with a bug planted
    skipped: tuple[tuple[int, str], ...]  # (case id, reason) for merged cases left out
    scored_cases: int  # scored on both sides
    errored_cases: int
    champion: SideTotals
    challenger: SideTotals
    by_kind: tuple[KindTotals, ...]
    verdict: Verdict
    reason: str
    state: str
    stop_reason: str | None


def catch_rate(t: SideTotals) -> float:
    """Catches over scored seeded cases (0.0 with none scored)."""
    return t.catches / t.cases if t.cases else 0.0


def seeded_seed(store: Store, experiment_id: int) -> int | None:
    """The seed a seeded run was started with (from its note's first line)."""
    note = store.get_experiment(experiment_id).result_note or ""
    m = _SEEDED_HEAD_RE.match(note)
    return int(m[1]) if m else None


def start_seeded(
    store: Store, *, champion_variant: str, challenger_variant: str, set_id: int, seed: int
) -> int:
    """A new ``experiments`` row (mode seeded) pinned to reviewer set ``set_id``."""
    if champion_variant == challenger_variant:
        raise ValueError("champion and challenger must be different variants")
    rset = rs.get_set(store, set_id)
    if rset.role != "reviewer":
        raise ValueError(f"set {set_id} holds {rset.role} cases; seeded runs need reviewer cases")
    now = _ts(store.now())
    with store.transaction() as conn:
        exp = store.create_experiment(
            role="reviewer",
            champion_variant=champion_variant,
            challenger_variant=challenger_variant,
            mode="seeded",
        )
        conn.execute(
            "INSERT INTO replay_runs (experiment_id, set_id, role, state, started_at, updated_at)"
            " VALUES (?, ?, 'reviewer', 'running', ?, ?)",
            (exp.id, set_id, now, now),
        )
    store.set_experiment_note(exp.id, SEEDED_NOTE_HEAD.format(seed=int(seed), set_id=set_id))
    return exp.id


def find_resumable_seeded(
    store: Store, *, champion_variant: str, challenger_variant: str, seed: int
) -> int | None:
    """The newest unfinished seeded run of this pair with this seed."""
    rows = store.query(
        "SELECT e.id FROM experiments e JOIN replay_runs r ON r.experiment_id = e.id"
        " WHERE e.role = 'reviewer' AND e.mode = 'seeded' AND e.status = 'running'"
        " AND e.champion_variant = ? AND e.challenger_variant = ? AND r.state != 'finished'"
        " ORDER BY e.id DESC",
        (champion_variant, challenger_variant),
    )
    for r in rows:
        if seeded_seed(store, int(r["id"])) == int(seed):
            return int(r["id"])
    return None


def seeded_verdict(
    champ: SideTotals, chall: SideTotals, scored: int, min_cases: int
) -> tuple[Verdict, str]:
    """Catches first, then cost; seeded cases carry no false-kickback signal."""
    if scored < max(1, min_cases):
        return "inconclusive", f"only {scored} case(s) scored on both sides; {min_cases} needed"
    if chall.catches < champ.catches:
        return "does_not_win", "it caught fewer of the seeded bugs"
    if not _le(chall.cost_usd, champ.cost_usd):
        return "does_not_win", "it costs more"
    if chall.catches > champ.catches:
        return "wins", "it caught more of the seeded bugs at no higher cost"
    if _lt(chall.cost_usd, champ.cost_usd):
        return "wins", "the same catches at a lower cost"
    return "does_not_win", "no better than the champion on catches or cost"


def compare_seeded(
    store: Store,
    experiment_id: int,
    built: Sequence[Any] = (),
    *,
    min_cases: int = 10,
) -> SeededComparison:
    """The seeded comparison from the stored rows; ``built`` (``seeded.seeded_cases``)
    gives each case's mutation kind and the skipped merged cases."""
    exp = store.get_experiment(experiment_id)
    run = _run_row(store, experiment_id)
    kinds = {b.case_id: b.kind for b in built if isinstance(b, sd.SeededCase)}
    skipped = tuple((b.case_id, b.reason) for b in built if isinstance(b, sd.SeededSkip))
    by_case: dict[int, dict[str, CaseResult]] = {}
    for r in results(store, experiment_id):
        by_case.setdefault(r.case_id, {})[r.side] = r
    paired: list[tuple[CaseResult, CaseResult]] = []
    errored = 0
    for case_id in sorted(by_case):
        sides = by_case[case_id]
        if any(x.status == "error" for x in sides.values()):
            errored += 1
        elif "champion" in sides and "challenger" in sides:
            paired.append((sides["champion"], sides["challenger"]))
    champ = _totals(exp.champion_variant, [a for a, _ in paired])
    chall = _totals(exp.challenger_variant, [b for _, b in paired])
    per: dict[str, list[int]] = {}
    for a, b in paired:
        k = per.setdefault(kinds.get(a.case_id, "unknown"), [0, 0, 0])
        k[0] += a.score == "catch"
        k[1] += b.score == "catch"
        k[2] += 1
    by_kind = tuple(KindTotals(k, *per[k]) for k in sorted(per))
    verdict, reason = seeded_verdict(champ, chall, len(paired), min_cases)
    seed = seeded_seed(store, experiment_id)
    return SeededComparison(
        experiment_id=experiment_id,
        set_id=int(run["set_id"]),
        seed=seed if seed is not None else -1,
        merged_cases=len(kinds) + len(skipped),
        seeded_cases=len(kinds),
        skipped=skipped,
        scored_cases=len(paired),
        errored_cases=errored,
        champion=champ,
        challenger=chall,
        by_kind=by_kind,
        verdict=verdict,
        reason=reason,
        state=run["state"],
        stop_reason=run["stop_reason"],
    )


def _pct(n: int, d: int) -> str:
    return f"{n}/{d} ({100.0 * n / d:.0f}%)" if d else "0/0"


def seeded_note(c: SeededComparison) -> str:
    """The plain-text seeded result written to ``experiments.result_note``."""
    a, b = c.champion, c.challenger
    verdict = {"wins": "challenger catches more", "does_not_win": "challenger does not win",
               "inconclusive": "inconclusive"}[c.verdict]  # fmt: skip
    lines = [SEEDED_NOTE_HEAD.format(seed=c.seed, set_id=c.set_id)]
    if c.stop_reason:
        lines.append(f"{c.stop_reason} (resume to finish the set).")
    elif c.state == "running":
        lines.append("running.")
    lines += [
        f"{c.seeded_cases} of {c.merged_cases} merged case(s) seeded with a bug"
        f" ({len(c.skipped)} had no mutation that applies); {c.scored_cases} scored on both"
        f" sides ({c.errored_cases} could not run). Expected verdict: request_changes.",
        f"Catch rate: champion {_pct(a.catches, a.cases)}, challenger"
        f" {_pct(b.catches, b.cases)}; handed to user: champion {a.handed}, challenger"
        f" {b.handed}.",
    ]
    for k in c.by_kind:
        lines.append(
            f"  {k.kind}: champion {_pct(k.champion_catches, k.cases)}, challenger"
            f" {_pct(k.challenger_catches, k.cases)}"
        )
    lines += [
        f"Verdict: {verdict} ({c.reason}). Seeded cases measure catches only; false"
        " kickbacks come from the reviewer replay.",
        f"Cost: champion {_money(a.cost_usd)}, challenger {_money(b.cost_usd)}.",
        f"Time: champion {a.seconds:.1f}s, challenger {b.seconds:.1f}s.",
        f"Champion {a.variant_id}; challenger {b.variant_id}.",
    ]
    return "\n".join(lines)


def run_seeded(
    deps: ReplayDeps,
    *,
    champion: ReviewerVariant,
    challenger: ReviewerVariant,
    seed: int | None = None,
    kinds: Sequence[str] | None = None,
    settings: ReplaySettings | None = None,
    experiment_id: int | None = None,
    set_id: int | None = None,
    cancel: threading.Event | None = None,
) -> SeededComparison:
    """Run (or resume) one seeded-bug reviewer comparison (section comment above).

    Without ``experiment_id``: resume the newest unfinished seeded run of this pair and
    seed, else start one on ``set_id`` or the current reviewer set. ``seed`` defaults to
    ``seeded.DEFAULT_SEED``; resuming a run with another seed is refused."""
    settings = settings or ReplaySettings()
    cancel = cancel or threading.Event()
    store = deps.store
    _check_variants("reviewer", champion, challenger)
    want = sd.KINDS if kinds is None else tuple(kinds)
    if not want or any(k not in sd.KINDS for k in want):
        raise ValueError(f"mutation kinds must be among {sd.KINDS}")
    if experiment_id is not None:
        stored_seed = seeded_seed(store, experiment_id)
        if store.get_experiment(experiment_id).mode != "seeded" or stored_seed is None:
            raise ValueError(f"experiment {experiment_id} is not a seeded run")
        if seed is not None and int(seed) != stored_seed:
            raise ValueError(f"experiment {experiment_id} was seeded with {stored_seed}")
        seed = stored_seed
    seed = sd.DEFAULT_SEED if seed is None else int(seed)
    if experiment_id is None:
        experiment_id = find_resumable_seeded(
            store,
            champion_variant=champion.variant_id,
            challenger_variant=challenger.variant_id,
            seed=seed,
        )
    if experiment_id is None:
        if set_id is None:
            set_id = rs.ensure_current_set(
                store,
                "reviewer",
                mirror_for=deps.mirror_for,
                max_merges=settings.set_size,
                recordings_dir=deps.recordings_dir,
            ).id
        experiment_id = start_seeded(
            store,
            champion_variant=champion.variant_id,
            challenger_variant=challenger.variant_id,
            set_id=set_id,
            seed=seed,
        )
    exp = store.get_experiment(experiment_id)
    if (exp.champion_variant, exp.challenger_variant) != (
        champion.variant_id,
        challenger.variant_id,
    ):
        raise ValueError(f"experiment {experiment_id} compares other variants")
    run = _run_row(store, experiment_id)
    run_set = int(run["set_id"])
    built = sd.seeded_cases(store, run_set, deps.mirror_for, seed=seed, kinds=want)  # type: ignore[arg-type]
    stored = {
        c.id: c
        for c in rs.list_cases(
            store,
            run_set,
            cloud_models=champion.endpoint.is_cloud or challenger.endpoint.is_cloud,
            experiment_id=experiment_id,
        )
    }
    cases = [b.stored(stored[b.case_id]) for b in built if isinstance(b, sd.SeededCase)]
    for b in built:
        if isinstance(b, sd.SeededCase):
            log.info("seeded %d: case %d: %s", experiment_id, b.case_id, b.description)
    if run["state"] != "finished":
        _set_state(store, experiment_id, "running", None)
        state, reason = _drive(deps, settings, experiment_id, run_set, "reviewer", champion,
                               challenger, cancel, cases=cases)  # fmt: skip
        _set_state(store, experiment_id, state, reason)
    c = compare_seeded(store, experiment_id, built, min_cases=settings.min_cases)
    store.set_experiment_note(experiment_id, seeded_note(c) + _rules_note(challenger))
    return c


__all__ = [
    "SIDES",
    "STOP_CANCELLED",
    "STOP_EXPERIMENTS_BUDGET",
    "STOP_RUN_BUDGET",
    "STOP_SANDBOX",
    "CaseResult",
    "Comparison",
    "KindTotals",
    "ReplayDeps",
    "ReplaySettings",
    "ReviewerVariant",
    "RulesOverride",
    "RunStatus",
    "SandboxFactory",
    "SeededComparison",
    "SideTotals",
    "WriterVariant",
    "catch_rate",
    "compare",
    "compare_seeded",
    "comparison_note",
    "config_sandbox",
    "find_resumable",
    "find_resumable_seeded",
    "replay_status",
    "results",
    "review_score",
    "run_replay",
    "run_seeded",
    "seeded_note",
    "seeded_seed",
    "seeded_verdict",
    "split_rules_id",
    "start_replay",
    "start_seeded",
    "verdict_for",
    "with_rules",
]
