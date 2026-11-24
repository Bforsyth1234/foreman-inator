"""Shadow reviewer (todo #26): a challenger reviews live PRs next to the champion.

SPEC "Experiments": the challenger reviews every live PR alongside the champion; only the
champion's verdict counts; both are scored against the user's eventual merge or kickback.

Finding the challenger (for #29: "a challenger exists")
    ``running_shadow_experiments(store)``: experiments rows with ``role = "reviewer"``,
    ``mode = "shadow"``, ``status = "running"``, oldest first. ``find_challenger(store,
    config)`` takes the OLDEST one and resolves its ``challenger_variant``
    (``<model>/<prompt-name>@<hash8>``, todo #24) to a ``Challenger`` (the model's
    ``ModelEndpoint`` from config, the prompt name, the variant id). Two or more running:
    only the oldest gets shadow traffic; the others wait, unstarved only once the oldest
    ends (promoted or rejected). One challenger per live review keeps the experiments
    spend and the extra wall-clock to one review per PR. A challenger whose model is not
    in config is skipped with a warning (``None``).

The shadow review (``shadow_review``)
    Runs the challenger through ``review_change(shadow=True, bucket="experiments",
    prompt_name=<challenger prompt>, reviewer=<challenger endpoint>)`` on exactly the
    champion's inputs (the same ``IssueText`` and ``ChangeRef``). Pass the worker's
    ``_launch_review`` as ``launch=`` so the checks run under the sandbox (7g).

    * It never posts, never marks anything ready and never writes a kickback: this module
      takes no GitHub client, the stored row is ``shadow=1`` (``record_outcome`` writes no
      kickback for a shadow row), the attempt's outcome and the ticket are not touched,
      and ``ShadowResult`` carries no comment body (there is nothing a caller could post).
    * It runs only after the champion's verdict is stored (a non-shadow ``reviews`` row on
      the attempt); otherwise it is skipped (``champion_not_stored``, not recorded, so it
      runs once the champion's row exists). #29 calls it after ``finalize_review``.
    * It never raises (``Exception``) into the worker. Every failure is a ``ShadowResult``
      with ``status="skipped"`` and a reason code: ``family_conflict`` (7f: the challenger
      is the writer's family), ``experiments_budget`` (the experiments bucket refused the
      call; nobody is asked), ``timeout``, ``model_error``, ``incomplete`` (the review
      could not finish), ``variant_mismatch`` (the prompt file changed since the
      experiment started: a new variant needs a new experiment), ``prompt_missing``,
      ``challenger_is_champion``, ``no_model_step`` (the champion's verdict came from
      checks 1-4 alone, which are the same code for every reviewer, so a challenger
      would reach the same verdict without a model call; nothing to compare), ``error``.
      Skips are recorded once per (variant, attempt) in ``poll_cursors`` under
      ``shadow-skip:<variant>:a<attempt>`` (value ``"<code>: <detail>"``), so the shadow
      is never retried in a loop (7e) and scoring can report why an attempt has only the
      champion's review. ``cancelled`` (the ``cancel`` event was set, e.g. shutdown) is
      not recorded and runs again.
    * Bounded by its own ``timeout`` (default ``SHADOW_TIMEOUT_S``, half the champion's),
      which ``review_change`` applies to checks 1-4 and the model call; cancellable
      through ``cancel``.
    * Idempotent: a stored shadow row or a recorded skip for (variant, attempt) is
      returned as is, with no second run.
    * A spend refusal in the experiments bucket is remembered by ``Spend`` under the
      attempt id, but ``needs_approval`` is False outside the live bucket, so the
      worker's ``spend_refusal`` never pauses the live ticket for it.

Scoring (``score_rows`` pure; ``score_experiment`` loads the rows)
    For one shadow experiment: every attempt with a counted (non-shadow) review or a
    challenger (shadow, ``variant_id = challenger_variant``) review created inside the
    experiment's window. What the user did with the attempt (``user_judgment``):

    * ``kicked_back``: a ``kickbacks`` row ``by = "user"`` on the attempt;
    * ``merged``: attempt outcome ``accepted`` (or the ticket's ``accepted_attempt_id``);
    * ``dropped``: the ticket was closed unmerged; excluded (SPEC excludes dropped tickets);
    * ``not_judged``: the user never saw this revision (the champion kicked it back, or it
      was superseded); the live data cannot say who was right; replay (#25) can;
    * ``pending``: the user has not acted yet.

    Per verdict on a decided attempt: merged + approve = correct approval; merged +
    request_changes = false kickback; kicked back + request_changes = catch; kicked back
    + approve = miss; handed_to_user = no verdict. Agreement with the user = (correct
    approvals + catches) / decided. Cost = the review rows' ``cost_usd``. The head-to-head
    numbers use only PAIRED attempts (both reviewed), so both are judged on the same
    PRs; attempts reviewed by only one side are counted separately (``champion_only`` with
    the recorded skip reason, or ``not_run``; ``challenger_only``), never dropped silently.

    Known bias: live scoring sees mostly the champion's approvals, because a champion
    ``request_changes`` sends the change back before the user sees it (``not_judged``).
    So live shadow mode measures misses and false kickbacks on what the champion let
    through; catches the champion made are scored by reviewer replay (#25).

"Holds live" (``judge_live``; SPEC's promotion rule, live half)
    ``holds`` when, over at least ``min_sample`` (default ``MIN_LIVE_SAMPLE`` = 30) paired
    decided attempts, the challenger's agreement with the user is not below the counted
    reviewer's and its mean cost per review is not higher. Below the sample:
    ``insufficient``; otherwise ``fails`` with the reasons. ``write_comparison`` stores
    the comparison as JSON in the experiment's ``result_note`` (it never ends the
    experiment; promotion also needs a replay win and is #29's call).

Nothing in here is counted in a metric of record: ``metrics/queries.py`` ignores
``shadow = 1`` rows, the experiments bucket never draws on the live caps, and the shadow
review's usage is not added to the writer's attempt (the proxy context carries a review
key).
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from threading import Event
from typing import Any, Literal

from foreman_inator.config import Config, ConfigError, ModelEndpoint
from foreman_inator.db.store import Attempt, Experiment, Kickback, Review, Store, Ticket
from foreman_inator.proxy import pricing
from foreman_inator.review import reviewer as rv

log = logging.getLogger("foreman_inator.experiments.shadow")

SHADOW_TIMEOUT_S = rv.DEFAULT_TIMEOUT_S / 2
"""Whole-review bound for one shadow review (checks 1-4 and the model call)."""
MIN_LIVE_SAMPLE = 30
"""Paired, user-decided attempts needed before ``judge_live`` says holds or fails (SPEC:
"dozens per variant before a live difference means anything")."""
SKIP_PREFIX = "shadow-skip:"
NOTE_KIND = "shadow-v1"

#: Skips that are not recorded: the shadow runs again on a later pass.
RETRYABLE = frozenset({"cancelled", "champion_not_stored"})

Status = Literal["stored", "skipped", "cancelled"]
UserJudgment = Literal["merged", "kicked_back", "pending", "not_judged", "dropped"]
DECIDED: tuple[str, ...] = ("merged", "kicked_back")


# --------------------------------------------------------------------------- challenger


def parse_variant_id(variant_id: str) -> tuple[str, str, str]:
    """``<model>/<prompt-name>@<hash8>`` -> (model, prompt name, hash). The model name may
    itself contain ``/``; the prompt part is after the last one."""
    model, sep, rest = variant_id.rpartition("/")
    prompt, at, digest = rest.partition("@")
    if not (sep and at and model and prompt and digest):
        raise ValueError(f"not a variant id: {variant_id!r}")
    return model, prompt, digest


def variant_id_for(endpoint: ModelEndpoint, prompt_name: str) -> str:
    """The variant id ``review_change`` will give a review by ``endpoint`` with this
    prompt file as it is now (the same formula as ``reviewer.review_change``)."""
    template = rv.load_reviewer_template(prompt_name)
    return f"{endpoint.name}/{template.name}@{template.sha256[:8]}"


@dataclass(frozen=True)
class Challenger:
    experiment_id: int
    variant_id: str
    endpoint: ModelEndpoint
    prompt_name: str


def running_shadow_experiments(store: Store) -> list[Experiment]:
    """Running reviewer experiments in shadow mode, oldest first."""
    return [
        e for e in store.list_experiments(role="reviewer", status="running") if e.mode == "shadow"
    ]


def find_challenger(store: Store, config: Config) -> Challenger | None:
    """The challenger that shadows live reviews now: the oldest running shadow experiment
    (see the module docstring for two or more). None when there is none, or its model
    is not configured."""
    running = running_shadow_experiments(store)
    if not running:
        return None
    exp = running[0]
    if len(running) > 1:
        log.info(
            "shadow: %d reviewer shadow experiments running; only #%d gets traffic",
            len(running),
            exp.id,
        )
    try:
        model, prompt, _ = parse_variant_id(exp.challenger_variant)
        endpoint = config.model(model)
    except (ValueError, ConfigError) as exc:
        log.warning("shadow: experiment #%d has no usable challenger: %s", exp.id, exc)
        return None
    return Challenger(exp.id, exp.challenger_variant, endpoint, prompt)


# --------------------------------------------------------------------------- the review


@dataclass(frozen=True)
class ShadowResult:
    """What one shadow review did. Deliberately no comment body: nothing here is for
    GitHub."""

    status: Status
    attempt_id: int
    variant_id: str
    experiment_id: int | None = None
    reason: str | None = None  # "<code>: <detail>" when skipped or cancelled
    review: Review | None = None  # the stored shadow row
    seconds: float = 0.0

    @property
    def code(self) -> str | None:
        return self.reason.split(":", 1)[0] if self.reason else None

    @property
    def verdict(self) -> str | None:
        return self.review.verdict if self.review else None


class _Skip(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.reason = str(self)


class _WatchedModel:
    """Wraps the review's model client to see how a model call failed (spend refusal,
    timeout, upstream error), which ``ReviewOutcome`` only reports as prose."""

    def __init__(self, inner: rv.ReviewModel):
        self.inner = inner
        self.failure: BaseException | None = None

    def complete(self, **kw: Any) -> rv.ModelReply:
        try:
            return self.inner.complete(**kw)
        except Exception as exc:
            self.failure = exc
            raise


def skip_key(variant_id: str, attempt_id: int) -> str:
    return f"{SKIP_PREFIX}{variant_id}:a{int(attempt_id)}"


def recorded_skip(store: Store, repo: str, variant_id: str, attempt_id: int) -> str | None:
    return store.get_cursor(repo, skip_key(variant_id, attempt_id))


def shadow_review(
    *,
    store: Store,
    issue: rv.IssueText,
    change: rv.ChangeRef,
    writer: ModelEndpoint,
    challenger: ModelEndpoint,
    challenger_prompt: str,
    challenger_variant: str,
    proxy: Any,
    attempt_id: int,
    ticket_id: int | None = None,
    experiment_id: int | None = None,
    champion: rv.ReviewOutcome | None = None,
    launch: Callable[..., rv.ReviewOutcome] = rv.review_change,
    spend: Any = None,
    verify_override: str | None = None,
    recordings_dir: Any = None,
    timeout: float = SHADOW_TIMEOUT_S,
    check_timeout: float | None = None,
    python: str | None = None,
    model_client: rv.ReviewModel | None = None,
    cancel: Event | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> ShadowResult:
    """Review ``change`` with the challenger, store the row as ``shadow=1``, post nothing.
    Never raises ``Exception``; see the module docstring for every rule."""
    t0 = clock()
    repo: str | None = None
    try:
        attempt = store.get_attempt(attempt_id)
        ticket_id = attempt.ticket_id if ticket_id is None else ticket_id
        repo = store.get_ticket(ticket_id).repo
        done = _already(store, repo, challenger_variant, attempt_id)
        if done is not None:
            return _result(done, attempt_id, challenger_variant, experiment_id, clock() - t0)
        _preconditions(
            store,
            attempt_id=attempt_id,
            writer=writer,
            challenger=challenger,
            prompt=challenger_prompt,
            variant=challenger_variant,
            champion=champion,
            spend=spend,
        )
        watched = _WatchedModel(model_client or rv.ProxyReviewModel())
        kw: dict[str, Any] = dict(
            issue=issue,
            change=change,
            writer=writer,
            reviewer=challenger,
            proxy=proxy,
            attempt_id=attempt_id,
            ticket_id=ticket_id,
            store=None,  # stored below, only when it is a real verdict
            shadow=True,
            bucket="experiments",
            prompt_name=challenger_prompt,
            verify_override=verify_override,
            recordings_dir=recordings_dir,
            timeout=timeout,
            model_client=watched,
            cancel=cancel,
            clock=clock,
        )
        if check_timeout is not None:
            kw["check_timeout"] = check_timeout
        if python is not None:
            kw["python"] = python
        try:
            outcome = launch(**kw)
        except rv.FamilyConflict as exc:
            raise _Skip("family_conflict", str(exc)) from None
        if cancel is not None and cancel.is_set():
            raise _Skip("cancelled", "the shadow review was cancelled")
        _classify(outcome, watched)
        if outcome.variant_id != challenger_variant or not outcome.shadow:
            raise _Skip("variant_mismatch", f"the review ran as {outcome.variant_id!r}")
        with store.transaction():
            if _shadow_row(store, attempt_id, challenger_variant) is not None:
                raise _Skip("error", "a shadow row appeared while the review ran")
            stored = rv.record_outcome(store, attempt_id, outcome)
        assert stored.kickback is None and stored.review is not None and stored.review.shadow
        log.info(
            "shadow: attempt %d reviewed by %s: %s",
            attempt_id,
            challenger_variant,
            stored.review.verdict,
        )
        return ShadowResult(
            "stored",
            attempt_id,
            challenger_variant,
            experiment_id,
            review=stored.review,
            seconds=clock() - t0,
        )
    except _Skip as skip:
        return _skipped(store, repo, skip, attempt_id, challenger_variant, experiment_id, t0, clock)
    except Exception as exc:  # never into the worker
        log.warning("shadow: attempt %d: %s", attempt_id, exc, exc_info=True)
        skip = _Skip("error", f"{type(exc).__name__}: {str(exc)[:300]}")
        return _skipped(store, repo, skip, attempt_id, challenger_variant, experiment_id, t0, clock)


def _already(store: Store, repo: str, variant: str, attempt_id: int) -> Review | str | None:
    row = _shadow_row(store, attempt_id, variant)
    if row is not None:
        return row
    return recorded_skip(store, repo, variant, attempt_id)


def _result(
    done: Review | str, attempt_id: int, variant: str, experiment_id: int | None, secs: float
) -> ShadowResult:
    if isinstance(done, str):
        return ShadowResult("skipped", attempt_id, variant, experiment_id, done, seconds=secs)
    return ShadowResult("stored", attempt_id, variant, experiment_id, review=done, seconds=secs)


def _shadow_row(store: Store, attempt_id: int, variant: str) -> Review | None:
    for r in store.list_reviews(attempt_id=attempt_id, shadow=True):
        if r.variant_id == variant:
            return r
    return None


def _preconditions(
    store: Store,
    *,
    attempt_id: int,
    writer: ModelEndpoint,
    challenger: ModelEndpoint,
    prompt: str,
    variant: str,
    champion: rv.ReviewOutcome | None,
    spend: Any,
) -> None:
    counted = store.list_reviews(attempt_id=attempt_id, shadow=False)
    if not counted:
        raise _Skip("champion_not_stored", "the champion's verdict is not stored yet")
    if counted[0].variant_id == variant:
        raise _Skip("challenger_is_champion", f"{variant} gave the counted review")
    try:
        rv.check_independent(challenger, writer)
    except rv.FamilyConflict as exc:
        raise _Skip("family_conflict", str(exc)) from None
    try:
        now = variant_id_for(challenger, prompt)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise _Skip("prompt_missing", f"{prompt}: {type(exc).__name__}") from None
    if now != variant:
        raise _Skip("variant_mismatch", f"the prompt is now {now}, the experiment is {variant}")
    if champion is not None and champion.read_diff == "skipped":
        raise _Skip("no_model_step", f"the champion's {champion.verdict} came from checks 1-4")
    if spend is not None and challenger.is_cloud:
        estimate = pricing.cost_usd(challenger, 20_000, rv.MODEL_MAX_TOKENS)
        if not spend.allow(estimate, "experiments"):
            raise _Skip("experiments_budget", "the monthly experiments budget is spent")


def _classify(outcome: rv.ReviewOutcome, watched: _WatchedModel) -> None:
    """Raise ``_Skip`` unless the outcome is the challenger's own verdict. A malformed
    reply twice IS its verdict (``handed_to_user``); infrastructure failures are not."""
    exc = watched.failure
    if exc is not None:
        text = str(exc)
        if isinstance(exc, rv.ModelCallError):
            if "spend_cap" in text or "429" in text:
                raise _Skip("experiments_budget", text)
            raise _Skip("model_error", text)
        raise _Skip("timeout", f"the model call did not finish ({type(exc).__name__})")
    if outcome.verdict != "handed_to_user":
        return
    if outcome.read_diff == "malformed" and outcome.model_calls == 2:
        return
    note = outcome.note or ""
    if "ran out of time" in note or (outcome.tests and outcome.tests.status == "timed_out"):
        raise _Skip("timeout", note[:300])
    raise _Skip("incomplete", note[:300])


def _skipped(
    store: Store,
    repo: str | None,
    skip: _Skip,
    attempt_id: int,
    variant: str,
    experiment_id: int | None,
    t0: float,
    clock: Callable[[], float],
) -> ShadowResult:
    status: Status = "cancelled" if skip.code == "cancelled" else "skipped"
    reason = f"shadow skipped: {skip.reason}"
    if skip.code not in RETRYABLE and repo is not None:
        try:
            store.set_cursor(repo, skip.reason[:1000], skip_key(variant, attempt_id))
        except Exception as exc:
            log.warning("shadow: skip for attempt %d not recorded: %s", attempt_id, exc)
    log.info("shadow: attempt %d: %s", attempt_id, reason)
    return ShadowResult(
        status, attempt_id, variant, experiment_id, skip.reason, seconds=clock() - t0
    )


# --------------------------------------------------------------------------- scoring


def user_judgment(ticket: Ticket, attempt: Attempt, kickbacks: Iterable[Kickback]) -> UserJudgment:
    """What the user finally did with this attempt (see the module docstring)."""
    if ticket.column == "dropped":
        return "dropped"
    if any(k.by == "user" and k.attempt_id == attempt.id for k in kickbacks):
        return "kicked_back"
    if attempt.outcome == "accepted" or ticket.accepted_attempt_id == attempt.id:
        return "merged"
    if attempt.outcome in ("kicked_back", "superseded", "failed_checks"):
        return "not_judged"
    if ticket.column == "done":
        return "not_judged"  # merged with another attempt
    return "pending"


Score = Literal["correct_approval", "catch", "miss", "false_kickback", "no_verdict"]


def score_verdict(verdict: str, judgment: str) -> Score | None:
    """One verdict against the user's decision; None while undecided."""
    if judgment not in DECIDED:
        return None
    if verdict not in ("approve", "request_changes"):
        return "no_verdict"
    if judgment == "merged":
        return "correct_approval" if verdict == "approve" else "false_kickback"
    return "catch" if verdict == "request_changes" else "miss"


@dataclass(frozen=True)
class VariantScore:
    variant_id: str
    reviews: int = 0
    decided: int = 0
    correct_approvals: int = 0
    catches: int = 0
    misses: int = 0
    false_kickbacks: int = 0
    no_verdict: int = 0
    undecided: int = 0
    cost_usd: float = 0.0

    @property
    def agree(self) -> int:
        return self.correct_approvals + self.catches

    @property
    def agreement(self) -> float | None:
        return self.agree / self.decided if self.decided else None

    @property
    def cost_per_review(self) -> float | None:
        return self.cost_usd / self.reviews if self.reviews else None

    def add(self, review: Review, judgment: str) -> VariantScore:
        s = score_verdict(review.verdict, judgment)
        d = asdict(self)
        d["reviews"] += 1
        d["cost_usd"] += review.cost_usd or 0.0
        if s is None:
            d["undecided"] += 1
        else:
            d["decided"] += 1
            key = {
                "correct_approval": "correct_approvals",
                "catch": "catches",
                "miss": "misses",
                "false_kickback": "false_kickbacks",
                "no_verdict": "no_verdict",
            }[s]
            d[key] += 1
        return VariantScore(**d)

    def as_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "agreement": self.agreement,
            "cost_per_review": self.cost_per_review,
        }


@dataclass(frozen=True)
class PairRow:
    attempt_id: int
    ticket_id: int
    judgment: str
    champion_variant: str | None
    champion_verdict: str | None
    challenger_verdict: str | None
    skip_reason: str | None = None


@dataclass(frozen=True)
class LiveJudgment:
    status: Literal["holds", "fails", "insufficient"]
    decided_pairs: int
    min_sample: int
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ShadowComparison:
    experiment_id: int
    champion_variant: str
    challenger_variant: str
    champion: VariantScore  # the counted review, on paired attempts
    challenger: VariantScore  # on paired attempts
    counted_by_variant: Mapping[str, VariantScore]  # the counted reviews, all attempts
    paired: int
    champion_only: int
    champion_only_reasons: Mapping[str, int]
    challenger_only: int
    dropped: int
    judgments: Mapping[str, int]  # paired attempts by user judgment
    rows: tuple[PairRow, ...] = field(repr=False, default=())

    def judge(self, min_sample: int = MIN_LIVE_SAMPLE) -> LiveJudgment:
        return judge_live(self, min_sample=min_sample)


def score_rows(
    experiment: Experiment,
    *,
    tickets: Iterable[Ticket],
    attempts: Iterable[Attempt],
    reviews: Iterable[Review],
    kickbacks: Iterable[Kickback],
    skips: Mapping[int, str] | None = None,
) -> ShadowComparison:
    """Pure: join each attempt's counted review and challenger review (inside the
    experiment's window) to what the user did with that attempt. ``skips`` maps an
    attempt id to its recorded shadow-skip reason."""
    skips = skips or {}
    t_by_id = {t.id: t for t in tickets}
    a_by_id = {a.id: a for a in attempts}
    kbs = list(kickbacks)
    counted: dict[int, Review] = {}
    shadow: dict[int, Review] = {}
    for r in sorted(reviews, key=lambda r: r.id):
        if not _in_window(experiment, r.created_at):
            continue
        if not r.shadow:
            counted.setdefault(r.attempt_id, r)
        elif r.variant_id == experiment.challenger_variant:
            shadow.setdefault(r.attempt_id, r)

    champ = VariantScore("counted")
    chall = VariantScore(experiment.challenger_variant)
    by_variant: dict[str, VariantScore] = {}
    only_reasons: dict[str, int] = {}
    judgments: dict[str, int] = {}
    rows: list[PairRow] = []
    paired = champion_only = challenger_only = dropped = 0
    for aid in sorted(set(counted) | set(shadow)):
        att = a_by_id.get(aid)
        ticket = t_by_id.get(att.ticket_id) if att else None
        if att is None or ticket is None:
            continue
        j = user_judgment(ticket, att, kbs)
        if j == "dropped":
            dropped += 1
            continue
        c, s = counted.get(aid), shadow.get(aid)
        if c is not None:
            v = c.variant_id or "unknown"
            by_variant[v] = by_variant.get(v, VariantScore(v)).add(c, j)
        skip = skips.get(aid)
        rows.append(
            PairRow(
                aid,
                ticket.id,
                j,
                c.variant_id if c else None,
                c.verdict if c else None,
                s.verdict if s else None,
                skip,
            )
        )
        if c is not None and s is not None:
            paired += 1
            judgments[j] = judgments.get(j, 0) + 1
            champ = champ.add(c, j)
            chall = chall.add(s, j)
        elif c is not None:
            champion_only += 1
            code = skip.split(":", 1)[0] if skip else "not_run"
            only_reasons[code] = only_reasons.get(code, 0) + 1
        else:
            challenger_only += 1
    return ShadowComparison(
        experiment_id=experiment.id,
        champion_variant=experiment.champion_variant,
        challenger_variant=experiment.challenger_variant,
        champion=champ,
        challenger=chall,
        counted_by_variant=by_variant,
        paired=paired,
        champion_only=champion_only,
        champion_only_reasons=only_reasons,
        challenger_only=challenger_only,
        dropped=dropped,
        judgments=judgments,
        rows=tuple(rows),
    )


def _in_window(exp: Experiment, at: datetime) -> bool:
    return exp.started_at <= at and (exp.ended_at is None or at <= exp.ended_at)


def judge_live(comparison: ShadowComparison, *, min_sample: int = MIN_LIVE_SAMPLE) -> LiveJudgment:
    """SPEC's live half of the promotion rule, on paired attempts only."""
    if min_sample < 1:
        raise ValueError("min_sample must be at least 1")
    ch, co = comparison.challenger, comparison.champion
    n = ch.decided
    if n < min_sample:
        return LiveJudgment(
            "insufficient",
            n,
            min_sample,
            (f"{n} paired attempts decided by the user; {min_sample} needed",),
        )
    reasons: list[str] = []
    if (ch.agreement or 0.0) < (co.agreement or 0.0):
        reasons.append(
            f"agreement with the user {ch.agreement:.0%} is below the champion's {co.agreement:.0%}"
        )
    if (ch.cost_per_review or 0.0) > (co.cost_per_review or 0.0):
        reasons.append(
            f"cost per review ${ch.cost_per_review or 0:.4f} is above the champion's "
            f"${co.cost_per_review or 0:.4f}"
        )
    return LiveJudgment("fails" if reasons else "holds", n, min_sample, tuple(reasons))


def load_skips(store: Store, variant_id: str) -> dict[int, str]:
    """Recorded shadow skips of ``variant_id``: attempt id -> reason."""
    prefix = f"{SKIP_PREFIX}{variant_id}:a"
    rows = store.query(
        "SELECT name, value FROM poll_cursors WHERE substr(name, 1, ?) = ?",
        (len(prefix), prefix),
    )
    out: dict[int, str] = {}
    for r in rows:
        with contextlib.suppress(ValueError):
            out[int(r["name"][len(prefix) :])] = r["value"]
    return out


def score_experiment(store: Store, experiment_id: int) -> ShadowComparison:
    """``score_rows`` over the store's rows for this experiment's window."""
    exp = store.get_experiment(experiment_id)
    if exp.mode != "shadow" or exp.role != "reviewer":
        raise ValueError(f"experiment {experiment_id} is not a reviewer shadow experiment")
    reviews = [r for r in store.list_reviews() if _in_window(exp, r.created_at)]
    ids = sorted({r.attempt_id for r in reviews})
    attempts = [store.get_attempt(i) for i in ids]
    tickets = [store.get_ticket(t) for t in sorted({a.ticket_id for a in attempts})]
    kickbacks = [k for t in tickets for k in store.list_kickbacks(ticket_id=t.id)]
    return score_rows(
        exp,
        tickets=tickets,
        attempts=attempts,
        reviews=reviews,
        kickbacks=kickbacks,
        skips=load_skips(store, exp.challenger_variant),
    )


def comparison_note(comparison: ShadowComparison, judgment: LiveJudgment) -> str:
    """The JSON stored in ``experiments.result_note``."""
    doc = {
        "kind": NOTE_KIND,
        "champion_variant": comparison.champion_variant,
        "challenger_variant": comparison.challenger_variant,
        "champion": comparison.champion.as_dict(),
        "challenger": comparison.challenger.as_dict(),
        "counted_by_variant": {k: v.as_dict() for k, v in comparison.counted_by_variant.items()},
        "paired": comparison.paired,
        "champion_only": comparison.champion_only,
        "champion_only_reasons": dict(comparison.champion_only_reasons),
        "challenger_only": comparison.challenger_only,
        "dropped": comparison.dropped,
        "judgments": dict(comparison.judgments),
        "live": asdict(judgment),
    }
    return json.dumps(doc, sort_keys=True)


def write_comparison(
    store: Store, experiment_id: int, *, min_sample: int = MIN_LIVE_SAMPLE
) -> tuple[ShadowComparison, LiveJudgment]:
    """Score the experiment and store the comparison in its ``result_note``. Does not end
    the experiment (promotion also needs a replay win: #29)."""
    comparison = score_experiment(store, experiment_id)
    judgment = judge_live(comparison, min_sample=min_sample)
    store.set_experiment_note(experiment_id, comparison_note(comparison, judgment))
    return comparison, judgment


__all__ = [
    "MIN_LIVE_SAMPLE",
    "SHADOW_TIMEOUT_S",
    "Challenger",
    "LiveJudgment",
    "PairRow",
    "ShadowComparison",
    "ShadowResult",
    "VariantScore",
    "comparison_note",
    "find_challenger",
    "judge_live",
    "load_skips",
    "parse_variant_id",
    "recorded_skip",
    "running_shadow_experiments",
    "score_experiment",
    "score_rows",
    "score_verdict",
    "shadow_review",
    "skip_key",
    "user_judgment",
    "variant_id_for",
]
