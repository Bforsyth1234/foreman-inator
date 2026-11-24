"""Routing, one attempt, and publishing its draft PR (todo #21; v1 was #14).

``AttemptSteps`` is mixed into ``Worker`` and uses its collaborators and transition
helpers. No store transaction is held across slow work.

Route (``route_job``): the ``before_route`` seam (#44), then ``routing.route`` (#15)
inside the ticket's trace (it opens its own ``route`` span), stored as ``routed_tier`` +
``route_reason`` unless a ``tier:N`` label routed the ticket meanwhile. Then, in the same
job, the first attempt if the queue allows it.

Attempt (``attempt_job``), in order:

1. escalation decides from the stored rows (``Worker.policy_for``): a step, a hand-over,
   or nothing;
2. the writer is ``step.model`` (``config.model``), so tier 3's GLM → Claude step runs
   Claude; a cloud writer at the spend cap pauses the ticket before anything runs;
3. under the repo's mirror lock: fetch, and the attempt's worktree on
   ``fman/issue-<n>/a<k>``, starting from the previous attempt's work when there is one;
4. find the verify command (the repo's saved override first); none → hand over once;
5. ``AttemptStarted`` + the attempt row in one transaction, then the writer prompt;
6. inside ``attempt_span`` and a ``Recorder``: ``_launch_writer`` (OpenCode), the worker's
   own commit, the diff, ``_launch_verify`` inside ``verify_span``;
7. a spend refusal during the run → the attempt is set aside (uncounted) and the ticket
   pauses; otherwise the attempt's diff widens its file locks (#19), and if another
   ticket holds one of those files the attempt waits before publishing;
8. ``ChecksFailed`` → escalation on the next pass, or publish → AI review.

Publish (``publish_job_for``): force-push the attempt's branch to the PR head
(``pr_head_branch``), open or update the draft PR, move to ``testing``, then the AI review
(or the ``review:skip`` hand-over) in the same job. Re-running it after a crash is
harmless: the push is the same commit and the publisher adopts the open PR.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from foreman_inator import routing, tracing
from foreman_inator.config import ConfigError, ModelEndpoint
from foreman_inator.db.store import Attempt, Ticket
from foreman_inator.pr.publisher import PublishError, pr_head_branch
from foreman_inator.routing import escalation as esc
from foreman_inator.runner import verify as verify_mod
from foreman_inator.runner.worktree import RepoMirror, Worktree, WorktreeError
from foreman_inator.tickets import lifecycle
from foreman_inator.tickets.lifecycle import AttemptStarted, ChecksFailed, ChecksPassed
from foreman_inator.tickets.prompt import writer_prompt_for
from foreman_inator.worker_actions import Ctx
from foreman_inator.worker_policy import (
    FLAG_NO_VERIFY,
    FLAG_PUBLISH_FAILED,
    published_attempt,
    start_from,
)

if TYPE_CHECKING:
    from foreman_inator.worker_queue import Job

log = logging.getLogger("foreman_inator.worker")

#: Never committed by the worker, whatever the repo's .gitignore says (written to the
#: mirror's info/exclude, shared by every worktree).
EXCLUDES = (
    "__pycache__/",
    "*.pyc",
    ".pytest_cache/",
    ".ruff_cache/",
    ".mypy_cache/",
    ".coverage",
    "node_modules/",
    ".DS_Store",
    ".fman-verify.log",
)


class _Cancelled(Exception):
    pass


class AttemptSteps:
    # Provided by Worker.
    store: Any
    config: Any
    driver: Any
    proxy: Any
    publisher: Any
    api: Any
    spend: Any
    locks: Any
    recordings_dir: Path
    traces_dir: Path
    attempt_timeout: float | None
    verify_timeout: float

    # ------------------------------------------------------------------ seams

    def before_route(self, ticket: Ticket) -> bool:
        """Seam for #44 (clarifying questions): called before a ticket is routed. Return
        False to park it (e.g. a ``needs-info`` question was posted); the route job is
        derived again on the next pass. No behaviour yet: always route."""
        return True

    def _launch_writer(self, **kw: Any) -> Any:
        """Process launch point 1 of 3 (#40's sandbox): the OpenCode session."""
        return self.driver.run(**kw)

    def _launch_verify(self, cmd: verify_mod.VerifyCommand, cwd: Path, **kw: Any) -> Any:
        """Process launch point 2 of 3 (#40's sandbox): the repo's verify command."""
        return verify_mod.run(cmd, cwd, **kw)

    # ------------------------------------------------------------------ route

    def route_job(self, job: Job, cancel: threading.Event) -> None:
        t = self.store.get_ticket(job.ticket_id)
        if t.column != "todo" or t.flag_reason or t.routed_tier is not None:
            return
        if not self.before_route(t):
            log.info("ticket %d: parked before routing", t.id)
            return
        t = self._route_ticket(t)
        if cancel.is_set() or self.stopping:  # type: ignore[attr-defined]
            return
        if self.job_for(t) == "attempt":  # type: ignore[attr-defined]
            self.attempt_job(job, cancel)  # carry the ticket on in the same job

    def _route_ticket(self, t: Ticket, *, override: routing.Override | None = None) -> Ticket:
        """Route ``t`` (#15) and store the pick. An override makes no model call (it runs
        inside a poll transaction); a classification does, so it runs in a job and keeps
        a pick that a ``tier:N`` label stored meanwhile."""
        with self._trace(t):  # type: ignore[attr-defined]
            r = routing.route(
                t,
                config=self.config,
                proxy=None if override is not None else self.proxy,
                override=override,
                timeout=float(self.config.router_timeout_seconds),
            )
        with self.store.transaction():
            cur = self.store.get_ticket(t.id)
            if override is None and cur.routed_tier is not None:
                return cur
            cur = self.store.update_ticket(t.id, routed_tier=r.tier, route_reason=r.reason)
        log.info("ticket %d: routed to tier %d (%s)", t.id, r.tier, r.source)
        return cur

    # ------------------------------------------------------------------ attempt

    def _writer_endpoint(self, step: esc.Retry) -> ModelEndpoint:
        """The step's model (#16 picks the rung, e.g. tier 3's Claude after GLM)."""
        name = getattr(step, "model", None)
        if name:
            try:
                return self.config.model(name)
            except ConfigError:
                log.warning("model %r is no longer configured; using tier %d", name, step.tier)
        return self.config.models_for_tier(step.tier)[0]

    def attempt_job(self, job: Job, cancel: threading.Event) -> None:
        ticket = self.store.get_ticket(job.ticket_id)
        if ticket.column not in ("todo", "in_progress") or ticket.flag_reason:
            return
        step = self.policy_for(ticket)  # type: ignore[attr-defined]
        if step is None:
            return
        if isinstance(step, esc.HandToUser):
            counted = self.history(ticket).attempts  # type: ignore[attr-defined]
            self._event(  # type: ignore[attr-defined]
                ticket.id,
                lifecycle.HandedToUser(ticket.id, step.reason),
                mark_ready=bool(step.mark_ready and ticket.pr_number is not None),
                attempt=published_attempt(counted),
            )
            return
        endpoint = self._writer_endpoint(step)
        if endpoint.is_cloud:
            refusal = self.spend.blocked(ticket.id)
            if refusal is not None:
                self.pause_for_spend(ticket, refusal)  # type: ignore[attr-defined]
                return
        mirror = self._mirror(ticket.repo)  # type: ignore[attr-defined]
        token = self._token(ticket.repo)
        mirror.ensure(token)
        _write_excludes(mirror)
        ordinal = len(self.store.list_attempts(ticket.id)) + 1
        counted = list(self.history(ticket).attempts)  # type: ignore[attr-defined]
        wt = mirror.create(
            ticket.base_branch,
            ticket.issue_number,
            ordinal,
            token=token,
            fetch=False,
            start_point=_start_point(mirror, ticket, counted),
        )
        _remove_old_worktrees(mirror, ticket, ordinal)
        settings = self.store.get_repo_settings(ticket.repo)
        cmd = verify_mod.detect(wt.path, override=settings.verify_command if settings else None)
        if cmd is None:
            wt.remove()
            self.store.mark_verify_prompted(ticket.repo)
            self._event(  # type: ignore[attr-defined]
                ticket.id,
                lifecycle.HandedToUser(ticket.id, f"{FLAG_NO_VERIFY}: no verify command found"),
            )
            return
        with self.store.transaction():
            if self._commit(ticket.id, AttemptStarted(ticket.id, trigger=step.trigger)) is None:  # type: ignore[attr-defined]
                wt.remove()
                return
            att = self.store.add_attempt(
                ticket.id,
                model=endpoint.name,
                tier=endpoint.tier,
                trigger=step.trigger,
                worktree=str(wt.path),
                branch=wt.branch,
                base_commit=wt.base_sha,
            )
        ticket = self.store.get_ticket(ticket.id)
        log.info(
            "ticket %d: attempt %d started (%s, tier %d, %s): %s",
            ticket.id,
            att.id,
            step.trigger,
            endpoint.tier,
            endpoint.name,
            step.reason,
        )
        try:
            att, passed, vr, diff = self._run_attempt(ticket, att, wt, endpoint, cmd, cancel)
        except _Cancelled:
            log.info("ticket %d: attempt %d cancelled; marked superseded", ticket.id, att.id)
            return
        except Exception:
            if self.store.get_attempt(att.id).ended_at is None:
                self.store.finish_attempt(att.id, "superseded")
            raise
        refusal = self.spend_refusal(att.id)  # type: ignore[attr-defined]
        if refusal is not None:
            self.set_aside(ticket, att, "a cloud call was refused at the spend cap")  # type: ignore[attr-defined]
            self.pause_for_spend(ticket, refusal)  # type: ignore[attr-defined]
            return
        widened = self.locks.widen(ticket, wt.changed_files())
        if not passed:
            self._event(ticket.id, ChecksFailed(ticket.id))  # type: ignore[attr-defined]
            return
        if ticket.id in widened.yielded:
            self.set_aside(ticket, att, "it yielded its file locks to break a deadlock")  # type: ignore[attr-defined]
            return
        if not widened.ok:
            self.set_lock_wait(ticket, att, widened.blockers)  # type: ignore[attr-defined]
            log.info(
                "ticket %d: attempt %d passed but changed files held by ticket(s) %s; "
                "publishing when they are free",
                ticket.id,
                att.id,
                ", ".join(f"#{b}" for b in widened.blockers),
            )
            return
        self.publish_job_for(ticket.id, att, verify=vr, diff=diff, cancel=cancel)

    def _run_attempt(
        self,
        ticket: Ticket,
        att: Attempt,
        wt: Worktree,
        endpoint: Any,
        cmd: verify_mod.VerifyCommand,
        cancel: threading.Event,
    ) -> tuple[Attempt, bool, Any, Any]:
        prompt = writer_prompt_for(
            self.store, ticket, att, worktree=wt.path, verify_command=cmd.display
        )
        variant = prompt.variant_id(endpoint.name)
        att = self.store.update_attempt(att.id, variant_id=variant)
        out_path = self.traces_dir / str(ticket.id) / f"attempt-{att.id}-verify.log"
        vr = None
        with (
            self._trace(ticket),  # type: ignore[attr-defined]
            tracing.attempt_span(
                att.id,
                variant_id=variant,
                model=endpoint.name,
                tier=att.tier,
                trigger=att.trigger,
                branch=wt.branch,
                base_commit=wt.base_sha,
            ) as span,
            tracing.Recorder(
                self.recordings_dir,
                ticket.id,
                att.id,
                trace_id=ticket.trace_id,
                base_commit=wt.base_sha,
                model=endpoint.name,
                variant_id=variant,
                tier=att.tier,
            ) as rec,
        ):
            result = self._launch_writer(
                attempt_id=att.id,
                worktree=wt,
                prompt=prompt.text,
                endpoint=endpoint,
                proxy_base_url=self.proxy.base_url(att.id),
                timeout=self.attempt_timeout,
                cancel=cancel,
            )
            if cancel.is_set() or getattr(result, "cancelled", False):
                self._supersede(att, rec)
            head = wt.commit_all(
                f"foreman-inator: issue #{ticket.issue_number}, attempt {att.id}\n\n"
                f"Model {endpoint.name} (tier {att.tier}). Refs #{ticket.issue_number}."
            )
            diff = wt.diff_stats()
            if not result.finished:
                why = result.error or result.status
                _write(out_path, f"The coding agent did not finish: {why}")
                passed = False
            elif diff.diff_files == 0:
                _write(out_path, "The attempt changed no files.")
                passed = False
            else:
                with tracing.verify_span(cmd.display) as vspan:
                    vr = self._launch_verify(
                        cmd, wt.path, timeout=self.verify_timeout, output_path=out_path,
                        cancel=cancel,
                    )  # fmt: skip
                    tracing.set_verify_result(
                        vspan,
                        passed=vr.passed,
                        tests_run=vr.tests_run,
                        tests_failed=vr.tests_failed,
                        output=None if vr.passed else vr.output_tail(4000),
                    )
                if cancel.is_set():
                    self._supersede(att, rec)
                passed = vr.passed
            outcome = None if passed else "failed_checks"
            att = self.store.finish_attempt(
                att.id,
                outcome,
                tests_passed=passed,
                diff_files=diff.diff_files,
                diff_lines=diff.diff_lines,
                verify_output_path=None if passed else str(out_path),
            )
            span.set_attribute("attempt.outcome", outcome or "checks_passed")
            span.set_attribute("attempt.diff_lines", diff.diff_lines)
            rec.close(
                outcome or "checks_passed",
                tests_passed=passed,
                diff_lines=diff.diff_lines,
                head_commit=head,
            )
        log.info(
            "ticket %d: attempt %d %s (%d files, %d lines)",
            ticket.id,
            att.id,
            "passed its checks" if passed else "failed its checks",
            diff.diff_files,
            diff.diff_lines,
        )
        return att, passed, vr, diff

    def _supersede(self, att: Attempt, rec: Any) -> None:
        self.store.finish_attempt(att.id, "superseded")
        rec.close("superseded", stopped_reason="cancelled")
        raise _Cancelled

    # ------------------------------------------------------------------ publish

    def publish_job(self, job: Job, cancel: threading.Event) -> None:
        """Publish an attempt that passed its checks but has not been published (after a
        crash, an error, or a wait on file locks)."""
        ticket = self.store.get_ticket(job.ticket_id)
        att = self.store.list_attempts(ticket.id)[-1]
        merged = self.blocker_merged(ticket, att)  # type: ignore[attr-defined]
        if merged is not None:
            self.set_aside(  # type: ignore[attr-defined]
                ticket, att, f"ticket #{merged} merged changes to its files; starting over"
            )
            return
        self.publish_job_for(job.ticket_id, att, cancel=cancel)

    def publish_job_for(
        self,
        ticket_id: int,
        att: Attempt,
        *,
        verify: Any = None,
        diff: Any = None,
        cancel: threading.Event | None = None,
    ) -> None:
        ticket = self.store.get_ticket(ticket_id)
        if ticket.column != "in_progress":
            log.info("ticket %d: no longer in progress; not publishing", ticket.id)
            return
        if cancel is not None and cancel.is_set():
            return
        mirror = self._mirror(ticket.repo)  # type: ignore[attr-defined]
        ordinal = [a.id for a in self.store.list_attempts(ticket.id)].index(att.id) + 1
        try:
            wt = mirror.open(ticket.issue_number, ordinal)
        except WorktreeError:
            log.warning("ticket %d: attempt %d's worktree is gone; re-running", ticket.id, att.id)
            self.store.set_attempt_outcome(att.id, "superseded")
            return
        head = pr_head_branch(ticket)
        with (
            self._trace(ticket),  # type: ignore[attr-defined]
            tracing.github_span("branch_pushed", attempt_id=att.id),
        ):
            wt.push(self._token(ticket.repo), remote_branch=head, force=True)
        event = ChecksPassed(ticket.id, review_skip=self.review_skipped(ticket))
        plan = lifecycle.apply(ticket.column, event)
        ctx = Ctx(ticket, event, attempt=att, verify=verify, diff=diff or wt.diff_stats())
        ctx.cancel = cancel
        try:
            self._run_actions(ctx, plan.actions[:1])  # type: ignore[attr-defined]
        except PublishError as exc:
            self._event(  # type: ignore[attr-defined]
                ticket.id, lifecycle.HandedToUser(ticket.id, f"{FLAG_PUBLISH_FAILED}: {exc}")
            )
            return
        if ctx.stop:  # a person closed or merged the PR: wait for the poller to say so
            self.hold_repo(ticket.repo)  # type: ignore[attr-defined]
            return
        # The draft PR is open: now the ticket is in testing (the PR awaits review).
        if self._commit(ticket.id, event) is None:  # type: ignore[attr-defined]
            return
        ctx.ticket = self.store.get_ticket(ticket.id)
        self._run_actions(ctx, plan.actions[1:])  # type: ignore[attr-defined]  # review / skip

    # ------------------------------------------------------------------ helpers

    def _token(self, repo: str) -> str:
        token_for = getattr(self.api, "token_for", None)
        return token_for(repo) if token_for is not None else self.api.token()


# --------------------------------------------------------------------------- helpers


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")


def _write_excludes(mirror: RepoMirror) -> None:
    info = mirror.mirror_path / "info"
    info.mkdir(exist_ok=True)
    f = info / "exclude"
    have = f.read_text().splitlines() if f.exists() else []
    missing = [e for e in EXCLUDES if e not in have]
    if missing:
        f.write_text("\n".join([*have, "# foreman-inator", *missing]) + "\n")


def _branch_exists(mirror: RepoMirror, ref: str) -> bool:
    try:
        mirror.resolve(ref)
    except WorktreeError:
        return False
    return True


def _start_point(mirror: RepoMirror, ticket: Ticket, attempts: list[Attempt]) -> str | None:
    """Build on the previous counted attempt's committed work (its local branch), else on
    the PR head on GitHub, else start fresh from the base branch."""
    prev = start_from(attempts)
    if prev is not None and prev.branch:
        ref = f"refs/heads/{prev.branch}"
        if _branch_exists(mirror, ref):
            return ref
    if ticket.pr_number is not None:
        ref = f"refs/remotes/origin/{pr_head_branch(ticket)}"
        if _branch_exists(mirror, ref):
            return ref
    return None


def _remove_old_worktrees(mirror: RepoMirror, ticket: Ticket, ordinal: int) -> None:
    """Earlier attempts' checkouts are not needed once the next one exists; their local
    branches stay (the next attempt may build on them)."""
    for k in range(1, ordinal):
        if mirror.attempt_path(ticket.issue_number, k).exists():
            try:
                mirror.open(ticket.issue_number, k).remove(delete_branch=False)
            except WorktreeError as exc:
                log.debug("could not remove attempt %d worktree: %s", k, exc)


__all__ = ["EXCLUDES", "AttemptSteps"]
