"""The AI review stage, its GitHub side, and hand-overs (todo #21).

``ReviewSteps`` is mixed into ``Worker``. The stage sits between "checks passed + draft PR
pushed" (``testing``) and "PR marked ready for your review":

1. ``ci_gate`` (#41): wait until the poller stored GitHub CI's result for the attempt
   (``attempts.ci_status``). Red is charged to the attempt by the poll handler
   (``RECORD_CI`` + escalation), so the gate only ever opens on a green or an uncharged
   result (no CI, red on base too, pending past 30 minutes, no permission). It comes
   before the ``review:skip`` hand-over too, so nothing is marked ready on a red or an
   undecided run (7a).
2. ``review:skip`` by an allowed user → hand-over (the PR is marked ready, no review).
3. ``champion_reviewer`` (#24, wave 9): the reviewer champion's model when it is of
   another family than the writer, else ``pick_reviewer`` (#17) (7f), with the champion's
   prompt; none → hand-over. The stored row's variant is registered (``variants``).
4. ``_launch_review`` → ``review_change`` inside the ticket's trace: one ``review`` span
   with five ``review.check`` children. Nothing is stored while it runs.
5. Cut short (shutdown) → nothing is stored; the review runs again. A cloud call refused
   at the spend cap → the verdict is thrown away and the ticket pauses (``worker_pause``).
6. One transaction, only while the ticket is still in ``testing`` (a base conflict or
   a user's kickback may have moved it during the review: then nothing is stored): the
   ``reviews`` row (+ a ``kickbacks`` row ``by="reviewer"`` and the
   attempt's outcome ``kicked_back`` for request_changes) and ``AIReviewed`` (request
   changes moves the ticket back to ``in_progress``; escalation picks the next step).
7. ``finalize_review``: post the verdict as the bot's COMMENT review (once, by marker),
   then the lifecycle's ``AIReviewed`` actions: approve → PR body + mark ready;
   handed_to_user → hand-over; request_changes → the kickback's trace event.

``finalize_review`` is idempotent and is run again for every open ticket's latest review
after a restart (``Worker.recover`` empties the finalized set), which is what makes a
crash between the review row and the comment, or between the comment and marking ready,
come out right with no second comment.

Repo profiles (#51): the repo's ``review`` mode (``routing.profile.review_plan``) decides
steps 2-4. ``off`` hands over at step 2 with no review row and no model call, as
``review:skip`` does (``skip_reason``). ``light`` skips steps 3's pick and runs
``review_change(depth="light")``: checks 1-3 and a verdict from hard findings alone, no
model, so no variant is registered and no champion outcome is kept for shadows.
``strict`` ignores the ``review:skip`` label (its one-kickback escalation is
``profile.limits_for``'s, applied by the policy). ``normal`` is today's stage.

Local only (#49). For a repo set ``cloud = false`` (``Store.repo_allows_cloud``) the full
review's reviewer must be local: ``champion_reviewer(writer, local_only=True)`` keeps the
champion's model only when it is local, else picks ``local_reviewer``: the first local
model of another family than the writer, from ``reviewer_models`` first, then the other
configured models. None exists → the review is skipped and the ticket handed over like
``review:skip`` (flag ``review_skipped``, ``local_only_skip_reason``): the PR body's
hand-over section and the issue comment say why, and the PR is marked ready when its
checks passed. A ``light`` review calls no model, so it runs unchanged.

Hand-over (``hand_over``), always in this order: the PR body's hand-over section, the
issue comment saying why (once, by marker), marking the PR ready when the change passed
its checks, and last the ticket's flag. The flag is the "done" mark: a crash before it
redoes the hand-over, and every step before it is idempotent.
"""

from __future__ import annotations

import logging
import shutil
import threading
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from fde_kit.prompts import PromptRef

from foreman_inator import prompts
from foreman_inator.config import ConfigError
from foreman_inator.db.store import Attempt, Review, Ticket
from foreman_inator.experiments import variants
from foreman_inator.github.client import IssueEvent
from foreman_inator.github.poller import REVIEW_SKIP_LABEL
from foreman_inator.pr.publisher import Handover, PublishError, pr_head_branch
from foreman_inator.review import reviewer as rv
from foreman_inator.routing import profile as prof
from foreman_inator.runner import sandbox
from foreman_inator.runner.worktree import WorktreeError
from foreman_inator.tickets import lifecycle
from foreman_inator.tickets.lifecycle import AIReviewed
from foreman_inator.worker_actions import FLAG_NO_REVIEWER, FLAG_REVIEW_SKIPPED, Ctx, flag_comment
from foreman_inator.worker_policy import published_attempt
from foreman_inator.worker_sandbox import FLAG_SANDBOX

if TYPE_CHECKING:
    from foreman_inator.worker_queue import Job

log = logging.getLogger("foreman_inator.worker")


class ReviewSteps:
    # Provided by Worker.
    store: Any
    config: Any
    api: Any
    proxy: Any
    publisher: Any
    recordings_dir: Any
    review_timeout: float | None
    _finalized: set[int]

    # ------------------------------------------------------------------ seams

    def ci_gate(self, ticket: Ticket, attempt: Attempt) -> bool:
        """#41: between "local checks passed + draft PR pushed" and the AI review (or the
        ``review:skip`` hand-over). False: wait; the review job is derived again on the
        next pass, after the poller read CI. With CI reading off (``Poller(ci=False)``)
        the gate is open."""
        if not getattr(self.poller, "ci", False):  # type: ignore[attr-defined]
            return True
        return ci_decided(self.store.get_attempt(attempt.id))

    def _rules_file(self, repo: str) -> Any:
        """#39: the repo's configured ``rules_file`` (``Config.rules_file_for``), or None."""
        getter = getattr(self.config, "rules_file_for", None)
        return getter(repo) if getter is not None else None

    def _launch_review(self, **kw: Any) -> rv.ReviewOutcome:
        """Process launch point 3 of 3 (#40): the reviewer's checks (tests, coverage,
        revert, run-app) run inside ``review_change`` under ``sandbox.active``; each check
        re-binds the policy to its own scratch worktree."""
        t = self.store.get_ticket(kw["ticket_id"])
        key = f"r{kw['attempt_id']}"
        scratch = self._sandbox_scratch(f"{key}-work")  # type: ignore[attr-defined]
        policy = self._sandbox_policy(t, scratch, key, writer=False)  # type: ignore[attr-defined]
        try:
            with sandbox.active(policy):
                return rv.review_change(**kw)
        finally:
            self._drop_scratch(policy)  # type: ignore[attr-defined]
            shutil.rmtree(scratch, ignore_errors=True)

    # ------------------------------------------------------------------ jobs

    def review_job(self, job: Job, cancel: threading.Event) -> None:
        """Review the PR's attempt (after a crash, a pause, or a wait at the CI gate)."""
        t = self.store.get_ticket(job.ticket_id)
        if t.column != "testing" or t.flag_reason:
            return
        att = self.store.list_attempts(t.id)[-1]
        if not att.tests_passed or att.outcome is not None:
            return
        if self.store.list_reviews(attempt_id=att.id, shadow=False):
            return
        if not self.ci_gate(t, att):
            log.debug("ticket %d: waiting at the CI gate", t.id)
            return
        reason = self.skip_reason(t)
        if reason is not None:
            self.hand_over(t, reason, ready=True, attempt=att)
            return
        self.review_stage(t, att, cancel)

    def finalize_job(self, job: Job, cancel: threading.Event) -> None:
        t = self.store.get_ticket(job.ticket_id)
        reviews = self.store.list_reviews(ticket_id=t.id, shadow=False)
        if reviews and reviews[-1].id not in self._finalized:
            self.finalize_review(t, reviews[-1])

    # ------------------------------------------------------------------ the stage

    def review_stage(self, t: Ticket, att: Attempt, cancel: threading.Event | None = None) -> None:
        if not self.ci_gate(t, att):
            log.info("ticket %d: waiting on GitHub CI before the AI review", t.id)
            return
        plan = prof.review_plan(self.repo_profile(t))  # type: ignore[attr-defined]
        if plan.kind == "off":  # #51: e.g. the profile changed after the checks passed
            self.hand_over(t, review_off_reason(self.repo_profile(t)), ready=True, attempt=att)  # type: ignore[attr-defined]
            return
        why = self.sandbox_ready(self._mirror(t.repo).repo_dir)  # type: ignore[attr-defined]
        if why is not None:
            self.hand_over(t, f"{FLAG_SANDBOX}: {why}", ready=False, attempt=att)
            return
        writer = self._endpoint_named(att)
        light = plan.kind == "light"
        order = "configured"  # #72: no second (possibly cloud) opinion on a light review
        if light:  # #51: no model reads the diff, so no reviewer is picked (7f not in play)
            reviewer, prompt_name = writer, rv.REVIEWER_PROMPT_NAME
        else:
            local_only = not self.store.repo_allows_cloud(t.repo)
            if local_only and local_reviewer(self.config, writer) is None:
                # #49: no local reviewer of another family; the code stays on the Mac.
                reason = local_only_skip_reason(writer)
                self.hand_over(t, reason, ready=True, attempt=att)
                return
            if not local_only:  # #49: a local-only repo never escalates to a cloud reviewer
                order = self._reviewer_order()
            try:
                reviewer, prompt_name = self.champion_reviewer(writer, local_only=local_only)
            except rv.ReviewError as exc:
                self.hand_over(t, f"{FLAG_NO_REVIEWER}: {exc}", ready=True, attempt=att)
                return
        mirror = self._mirror(t.repo)  # type: ignore[attr-defined]
        head = self._head_sha(mirror, t, att)
        settings = self.store.get_repo_settings(t.repo)
        log.info(
            "ticket %d: %s review of attempt %d by %s",
            t.id,
            plan.kind,
            att.id,
            rv.LIGHT_REVIEWER_MODEL if light else reviewer.name,
        )
        with self._trace(t):  # type: ignore[attr-defined]
            outcome = rv.review_with_escalation(
                self._launch_review,
                config=self.config,
                order=order,
                issue=rv.IssueText(t.issue_number, t.title, t.request),
                change=rv.ChangeRef(mirror, att.base_commit or "", head),
                writer=writer,
                reviewer=reviewer,
                proxy=self.proxy,
                attempt_id=att.id,
                ticket_id=t.id,
                store=None,  # stored below, unless the review was cut short or refused
                prompt_name=prompt_name,
                verify_override=settings.verify_command if settings else None,
                recordings_dir=self.recordings_dir,
                timeout=self.review_timeout or rv.DEFAULT_TIMEOUT_S,
                cancel=cancel,
                depth=plan.depth,
                rules_file=self._rules_file(t.repo),  # #39: same rules file the writer gets
            ).outcome
        if (cancel is not None and cancel.is_set()) or self.stopping:  # type: ignore[attr-defined]
            log.info("ticket %d: AI review cut short; it runs again", t.id)
            return
        refusal = self.spend_refusal(att.id)  # type: ignore[attr-defined]
        if refusal is not None:
            self.pause_for_spend(t, refusal)  # type: ignore[attr-defined]
            return
        with self.store.transaction():
            if self.store.get_ticket(t.id).column != "testing":
                # A base conflict (#43) or a kickback moved it while the review ran.
                log.info("ticket %d: left testing during the AI review; verdict dropped", t.id)
                return
            if not light:  # a light review has no variant (no model, no prompt was used)
                variants.register(  # #24: every reviews.variant_id has a variants row
                    self.store,
                    "reviewer",
                    outcome.reviewer_model,
                    PromptRef(name=outcome.prompt_name, sha256=outcome.prompt_hash, text=""),
                )
            outcome = rv.record_outcome(self.store, att.id, outcome)
            if outcome.verdict == "request_changes":
                self.store.set_attempt_outcome(att.id, "kicked_back")
            self._commit(t.id, AIReviewed(t.id, outcome.verdict))  # type: ignore[arg-type, attr-defined]
        assert outcome.review is not None
        if not light:
            self.remember_champion(att.id, outcome)  # type: ignore[attr-defined]  # shadow (#26)
        if outcome.kickback is not None:
            self.queue_label(t.repo, outcome.kickback.id)  # type: ignore[attr-defined]  # #27
        log.info(
            "ticket %d: AI review %d of attempt %d: %s",
            t.id,
            outcome.review.id,
            att.id,
            outcome.verdict,
        )
        self.finalize_review(self.store.get_ticket(t.id), outcome.review, outcome.comment_body)

    def finalize_review(self, t: Ticket, review: Review, body: str | None = None) -> None:
        """The GitHub side of a stored review; safe to run any number of times."""
        att = self.store.get_attempt(review.attempt_id)
        text = body or comment_from_row(review)
        if t.pr_number is not None:
            self._gh(  # type: ignore[attr-defined]
                t,
                "ai_review_posted",
                lambda: self.publisher.post_ai_review(t, review.id, text),
                attempt_id=att.id,
                pr_number=t.pr_number,
            )
        counted = self.history(t).attempts  # type: ignore[attr-defined]
        att = self.store.get_attempt(att.id)
        cur = self.store.get_ticket(t.id)
        # Effects only while this review still decides the ticket: request_changes's are
        # idempotent; approve and hand-over only while nobody judged the change since.
        live = review.verdict == "request_changes" or (
            att.outcome is None and cur.column == "testing"
        )
        if counted and counted[-1].id == att.id and live:
            ev = AIReviewed(t.id, review.verdict)  # type: ignore[arg-type]
            plan = lifecycle.apply("testing", ev)  # the column the review was made in
            ctx = Ctx(self.store.get_ticket(t.id), ev, attempt=att, review=review)
            if review.verdict == "approve":
                self._update_body(ctx.ticket, att)  # type: ignore[arg-type]
            self._run_actions(ctx, plan.actions)  # type: ignore[attr-defined]
        self._finalized.add(review.id)

    def champion_reviewer(self, writer: Any, *, local_only: bool = False) -> tuple[Any, str]:
        """#24: the reviewer champion's model, when it is of another family than the
        writer (7f), else ``pick_reviewer``'s choice; with the champion's prompt (the
        packaged default when there is no champion or its file is gone). Raises
        ``ReviewError`` when no independent reviewer exists. ``local_only`` (#49): the
        champion's model counts only when it is local, and the fallback is
        ``local_reviewer``, never a cloud model."""
        champ = variants.champion(self.store, "reviewer")
        prompt_name = rv.REVIEWER_PROMPT_NAME
        reviewer = None
        if champ is not None:
            try:
                prompts.load(champ.prompt_name)
                prompt_name = champ.prompt_name
            except (FileNotFoundError, ValueError):
                log.warning("reviewer champion's prompt %s is missing; using the default",
                            champ.prompt_name)  # fmt: skip
        if not local_only and self._reviewer_order() == "cheapest":
            # #72: the cheapest model of another family reads the diff (champion's prompt).
            return rv.pick_reviewer(self.config, writer, order="cheapest"), prompt_name
        if champ is not None:
            try:
                candidate = self.config.model(champ.model)
                rv.check_independent(candidate, writer)
                if not (local_only and candidate.kind != "local"):
                    reviewer = candidate
            except (ConfigError, rv.FamilyConflict):
                reviewer = None
        if reviewer is None and local_only:
            reviewer = local_reviewer(self.config, writer)
            if reviewer is None:
                raise rv.NoIndependentReviewer(local_only_skip_reason(writer))
        return reviewer or rv.pick_reviewer(self.config, writer), prompt_name

    def _reviewer_order(self) -> str:
        return str(getattr(self.config, "reviewer_order", rv.DEFAULT_REVIEWER_ORDER))

    # ------------------------------------------------------------------ ready + hand-over

    def _mark_ready(self, t: Ticket, att: Attempt, reason: str, *, handover: bool = False) -> None:
        """The only caller of ``Publisher.mark_ready`` (invariant 7a)."""
        t = self.store.get_ticket(t.id)
        if reason == "reviewer_approved":
            att = self.store.get_attempt(att.id)
            approved = any(
                r.verdict == "approve"
                for r in self.store.list_reviews(attempt_id=att.id, shadow=False)
            )
            current = published_attempt(self.history(t).attempts)  # type: ignore[attr-defined]
            if not (approved and t.column == "testing" and att.outcome is None
                    and current is not None and current.id == att.id):  # fmt: skip
                log.warning("ticket %d: not marking the PR ready: no current approval", t.id)
                return
        elif not (reason == "handover" and handover):
            raise RuntimeError(f"refusing to mark a PR ready for {reason!r} (7a)")
        if not self._ci_ok(att):
            log.warning("ticket %d: not marking the PR ready: GitHub CI red or undecided", t.id)
            return
        if t.pr_number is None:
            return

        def ready() -> Any:
            try:
                return self.publisher.mark_ready(t, reason=reason)
            except PublishError as exc:
                log.warning("ticket %d: PR not marked ready: %s", t.id, exc)
                return None

        pr = self._gh(t, "pr_ready", ready, attempt_id=att.id, pr_number=t.pr_number)  # type: ignore[attr-defined]
        if pr is not None:
            log.info("ticket %d: PR #%d ready for your review (%s)", t.id, pr.number, reason)

    def hand_over(
        self, t: Ticket, reason: str, *, ready: bool, attempt: Attempt | None = None
    ) -> None:
        """Hand the ticket to the user and say why (invariant 7e). See the module docstring
        for the order; the flag is stored last, so a stored flag means "done"."""
        t = self.store.get_ticket(t.id)
        if t.flag_reason == reason:
            return
        att = attempt or published_attempt(self.history(t).attempts)  # type: ignore[attr-defined]
        ready = bool(
            ready
            and t.pr_number is not None
            and att is not None
            and att.tests_passed
            and self._ci_ok(att)
        )
        n = len(self.store.list_attempts(t.id))
        if ready:
            assert att is not None
            self._update_body(t, att, handover=Handover(n, reason.partition(":")[2].strip()))
        kind = reason.split(":", 1)[0]
        self.post_once(  # type: ignore[attr-defined]
            t,
            f"handover-a{n}-{kind}",
            flag_comment(t, reason, pr_number=t.pr_number if ready else None),
            name="issue_flagged",
        )
        if ready:
            assert att is not None
            self._mark_ready(t, att, "handover", handover=True)
        self.store.flag_ticket(t.id, reason)
        self.status_update(t, "handed_over", reason=reason)  # type: ignore[attr-defined]  # #77
        log.info("ticket %d: handed to the user (%s)", t.id, kind)

    # ------------------------------------------------------------------ helpers

    def _ci_ok(self, att: Attempt) -> bool:
        """Invariant 7a with #41: may this attempt's PR be marked ready? Never when CI
        was red and charged; never while CI is undecided (when CI is read at all)."""
        cur = self.store.get_attempt(att.id)
        if cur.ci_passed is False:
            return False
        return ci_decided(cur) or not getattr(self.poller, "ci", False)  # type: ignore[attr-defined]

    def skip_reason(self, ticket: Ticket) -> str | None:
        """#51: why the AI review is skipped for ``ticket`` (the hand-over's flag reason),
        or None to review. ``review = off`` always skips (no GitHub read); ``strict``
        ignores the ``review:skip`` label; otherwise an allowed user's label skips."""
        p = self.repo_profile(ticket)  # type: ignore[attr-defined]
        plan = prof.review_plan(p)
        if plan.kind == "off":
            return review_off_reason(p)
        if plan.skip_label_counts and self.skip_label_applied(ticket):
            return _SKIP_REASON
        return None

    def review_skipped(self, ticket: Ticket) -> bool:
        """Is the review skipped for ``ticket`` (``skip_reason``)? The repo profile's
        ``review`` mode decides first (#51), then the ``review:skip`` label."""
        return self.skip_reason(ticket) is not None

    def skip_label_applied(self, ticket: Ticket) -> bool:
        """Is the ``review:skip`` label on the issue, applied by an allowed user (7b)?
        Read from GitHub when it matters, so a label added after the ticket was created
        counts too."""
        issue = self.api.get_issue(ticket.repo, ticket.issue_number)
        if REVIEW_SKIP_LABEL not in {lb.lower() for lb in issue.labels}:
            return False
        events = self.api.list_issue_events(ticket.repo, ticket.issue_number)
        ev = _applied(events, REVIEW_SKIP_LABEL)
        bot = self.api.bot_login()
        return (
            ev is not None
            and ev.actor is not None
            and ev.actor != bot
            and self.config.is_allowed_user(ev.actor)
        )

    def _update_body(self, t: Ticket, att: Attempt, *, handover: Handover | None = None) -> None:
        """Re-render the PR body: the AI review section from the stored row, and the
        hand-over section when handing over."""
        if t.pr_number is None:
            return
        reviews = self.store.list_reviews(attempt_id=att.id, shadow=False)
        review = reviews[-1] if reviews else None
        self._gh(  # type: ignore[attr-defined]
            t,
            "pr_updated",
            lambda: self.publisher.open_or_update_draft(t, att, review=review, handover=handover),
            attempt_id=att.id,
            pr_number=t.pr_number,
        )

    def _endpoint_named(self, att: Attempt) -> Any:
        try:
            return self.config.model(att.model)
        except Exception:
            return self.config.models_for_tier(att.tier)[0]

    def _head_sha(self, mirror: Any, t: Ticket, att: Attempt) -> str:
        """The attempt's commit: its local branch in the mirror, else the PR head."""
        for ref in (f"refs/heads/{att.branch}", f"refs/remotes/origin/{pr_head_branch(t)}"):
            try:
                return mirror.resolve(ref)
            except WorktreeError:
                continue
        mirror.ensure(self._token(t.repo))  # type: ignore[attr-defined]
        return mirror.resolve(f"refs/remotes/origin/{pr_head_branch(t)}")


def ci_decided(att: Attempt) -> bool:
    """CI reached a result for the attempt that lets it go on (not red and charged)."""
    return att.ci_status is not None and att.ci_passed is not False


def _applied(events: Iterable[IssueEvent], label: str) -> IssueEvent | None:
    """The ``labeled`` event that applied ``label`` (if it is still on)."""
    current: IssueEvent | None = None
    for ev in sorted(events, key=lambda e: (e.created_at, e.id)):
        if ev.label is None or ev.label.lower() != label:
            continue
        current = ev if ev.event == "labeled" else None
    return current


_SKIP_REASON = f"{FLAG_REVIEW_SKIPPED}: the review:skip label was applied by an allowed user"


def review_off_reason(p: prof.Profile) -> str:
    """#51: the hand-over reason when the repo profile turns the review off."""
    return f"{FLAG_REVIEW_SKIPPED}: review is off in this repo's profile, {prof.describe(p)}"


def local_reviewer(config: Any, writer: Any) -> Any:
    """#49: a reviewer for a local-only repo: the first model with ``kind == "local"`` and
    a family other than the writer's (7f), from ``reviewer_models`` in order, then the
    other configured models in config order. None when there is none."""
    names = list(getattr(config, "reviewer_models", ()))
    names += [m.name for m in getattr(config, "models", ()) if m.name not in names]
    family = str(writer.family).strip().lower()
    for name in names:
        try:
            m = config.model(name)
        except ConfigError:
            continue
        if m.kind == "local" and str(m.family).strip().lower() != family:
            return m
    return None


def local_only_skip_reason(writer: Any) -> str:
    """#49: the hand-over reason when a local-only repo has no local reviewer."""
    return (
        f"{FLAG_REVIEW_SKIPPED}: AI review skipped: this repo is local only (cloud = false) "
        f"and no local reviewer of a family other than {writer.family!r} is configured"
    )


_YES_NO = {True: "passed", False: "failed", None: "unknown"}


def comment_from_row(review: Review) -> str:
    """The COMMENT review body rebuilt from a stored row (after a crash lost the
    reviewer's own text): the verdict, tag, note and the four stored check results."""
    cov = review.changed_line_coverage
    nonvac = {True: "the new tests fail without the change", False: "vacuous", None: "unknown"}
    rows = [
        ("1. Tests and build", _YES_NO[review.tests_passed]),
        ("2. Changed-line coverage", "unknown" if cov is None else f"{cov:.0f}%"),
        ("3. Revert check", nonvac[review.tests_nonvacuous]),
        ("4. Run it", review.ran_app or "unknown"),
        ("5. Read the diff", "stored verdict"),
    ]
    return rv.render_comment(
        verdict=review.verdict,
        tag=review.tag,
        note=review.note or "",
        reviewer_model=review.reviewer_model,
        variant_id=review.variant_id or "",
        check_lines=rows,
    )


__all__ = [
    "ReviewSteps",
    "ci_decided",
    "comment_from_row",
    "local_only_skip_reason",
    "local_reviewer",
    "review_off_reason",
]
