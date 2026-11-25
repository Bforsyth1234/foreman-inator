"""One attempt (todo #21; v1 was #14; sandbox #40 and rebase hooks #43 in wave 6b).

``AttemptSteps`` is mixed into ``Worker`` and uses its collaborators and transition
helpers. No store transaction is held across slow work. Routing lives in
``worker_route``, publishing in ``worker_publish``, rebase attempts in ``worker_rebase``.

Attempt (``attempt_job``), in order:

1. escalation decides from the stored rows (``Worker.policy_for``): a step, a hand-over,
   or nothing;
2. the writer is ``step.model`` (``config.model``), so tier 3's GLM → Claude step runs
   Claude; a running writer traffic split (#34, ``experiments.split.pick``) may give a
   ticket's first attempt to the challenger's model and prompt (never a retry,
   escalation, explore or ``tier:N`` ticket; the attempt's ``variant_id`` is then the
   challenger's), and sends a retry after one back to the champion's model; a cloud
   writer at the spend cap pauses the ticket before anything runs;
3. under the repo's mirror lock: fetch, and the attempt's worktree on
   ``fman/issue-<n>/a<k>``, starting from the previous attempt's work when there is one;
4. find the verify command (the repo's saved override first); none → hand over once;
5. the sandbox (#40) must be able to run (``sandbox = "on"`` and no backend → the ticket
   is flagged ``sandbox_unavailable``, no attempt row, nothing retried);
6. ``AttemptStarted`` + the attempt row in one transaction, then the writer prompt from
   the writer champion's prompt file (#24, ``writer_template``) and the attempt's
   registered variant id;
7. inside ``attempt_span`` and a ``Recorder``: ``prepare_worktree`` (the seam for #45's
   dependency install; a no-op here), ``_launch_writer`` (OpenCode, sandboxed), the
   worker's own commit, the diff, ``_launch_verify`` (sandboxed) inside ``verify_span``;
   a rebase attempt whose conflicted files still hold markers fails its checks without
   running verify;
8. a writer call refused at the attempt's own budget (#58, ``Spend.budget_stop``) on an
   attempt that failed → the attempt records ``end_reason = 'budget'`` and goes on as
   failed checks, so escalation hands the ticket over; a spend-cap refusal during the
   run → the attempt is set aside (uncounted) and the ticket pauses; otherwise the
   attempt's diff widens its file locks (#19), and if another ticket holds one of those
   files the attempt waits before publishing;
9. ``ChecksFailed`` → escalation on the next pass (a rebase attempt: another rebase or
   the flag), or publish → CI gate → AI review.

Could not run (#53, ``worker_setup``): a worktree that cannot be created, a
``SetupFailed`` from ``prepare_worktree``, or a writer run that never reached the model
(``worker_setup.could_not_run``; one immediate re-run first) is not failed checks: the
attempt is set aside (uncounted, like a spend pause) and the ticket is flagged once
``setup_failed: <what>: <detail>``. No escalation, no retry, no cloud call.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fde_kit.prompts import PromptRef

from foreman_inator import prompts, tracing
from foreman_inator.config import ConfigError, ModelEndpoint
from foreman_inator.db.store import Attempt, Ticket
from foreman_inator.experiments import split as split_mod
from foreman_inator.experiments import variants
from foreman_inator.pr.publisher import pr_head_branch
from foreman_inator.resources import memory
from foreman_inator.routing import escalation as esc
from foreman_inator.routing import profile as prof
from foreman_inator.runner import sandbox
from foreman_inator.runner import verify as verify_mod
from foreman_inator.runner.worktree import RepoMirror, Worktree, WorktreeError
from foreman_inator.tickets import conventions as conventions_mod
from foreman_inator.tickets import images as images_mod
from foreman_inator.tickets import lifecycle
from foreman_inator.tickets.lifecycle import AttemptStarted, ChecksFailed, clarify_qa_cursor
from foreman_inator.tickets.prompt import (
    WRITER_PROMPT_NAME,
    build_writer_prompt,
    fetch_issue_comments,
    load_history,
)
from foreman_inator.worker_policy import (
    FLAG_NO_VERIFY,
    published_attempt,
    start_from,
)
from foreman_inator.worker_sandbox import FLAG_SANDBOX
from foreman_inator.worker_setup import (
    RETRIED_ONCE,
    WORKTREE,
    WRITER_SETUP_TRIES,
    SetupFailed,
    setup_failure,
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
    #: #59: ``ollama host -> span attributes``; an instance attribute replaces it in tests.
    resource_snapshot = staticmethod(memory.snapshot)
    #: #78: the httpx transport image fetches use (None: the network); tests replace it.
    image_transport: Any = None

    # ------------------------------------------------------------------ seams
    # ``before_route`` (#44) lives in ``worker_clarify``; the sandbox policy in
    # ``worker_sandbox``.

    def prepare_worktree(self, ticket: Ticket, attempt: Attempt, worktree: Worktree) -> None:
        """Seam for #45 (dependency install): called once per attempt (and per rebase
        attempt), after the attempt's worktree exists and its row is stored, before the
        writer is launched. A no-op here; a mixin earlier in ``Worker``'s bases overrides
        it. Raise ``worker_setup.SetupFailed(reason, detail)`` when the worktree cannot
        be made ready: the attempt is set aside (not counted against its model) and the
        ticket is flagged ``setup_failed: <reason>: <detail>`` once, with no escalation
        and no re-run. Any other exception is a crash of the job, as before."""
        return None

    def _launch_writer(self, *, ticket: Ticket, **kw: Any) -> Any:
        """Process launch point 1 of 3 (#40): the OpenCode session, under the attempt's
        sandbox (network: the proxy port only). ``SandboxUnavailable`` propagates."""
        wt = kw["worktree"]
        policy = self._sandbox_policy(  # type: ignore[attr-defined]
            ticket, getattr(wt, "path", wt), f"a{kw['attempt_id']}-writer", writer=True
        )
        try:
            sandbox.ensure_available(policy)
            kw.setdefault("timeout", float(self.config.writer_timeout_seconds))  # type: ignore[attr-defined]
            return self.driver.run(**kw, sandbox=policy)
        finally:
            self._drop_scratch(policy)  # type: ignore[attr-defined]
            # #34: the writer run is over; its queue-wait total is no longer read.
            forget = getattr(getattr(self, "proxy", None), "forget_queue_wait", None)
            if forget is not None:
                forget(kw["attempt_id"])

    def _launch_verify(
        self, cmd: verify_mod.VerifyCommand, cwd: Path, *, ticket: Ticket, attempt_id: int,
        **kw: Any,
    ) -> Any:  # fmt: skip
        """Process launch point 2 of 3 (#40): the repo's verify command, under the
        attempt's sandbox (network as the repo's ``sandbox_network`` says)."""
        policy = self._sandbox_policy(ticket, cwd, f"a{attempt_id}-verify", writer=False)  # type: ignore[attr-defined]
        try:
            return verify_mod.run(cmd, cwd, sandbox=policy, **kw)
        finally:
            self._drop_scratch(policy)  # type: ignore[attr-defined]

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
        # #34: a running traffic split may give this first attempt to the challenger
        # (its model and prompt), or send a retry after one back to the champion.
        split_pick = self._split_pick(ticket, step, endpoint)
        template = None
        if split_pick is not None:
            endpoint, template = split_pick.endpoint, split_pick.template
        if endpoint.is_cloud:
            # #51: a thrifty repo's cloud writer attempt also waits for an approval.
            refusal = self.spend.writer_gate(
                ticket.id,
                approval_required=prof.limits_for(self.repo_profile(ticket)).cloud_approval,  # type: ignore[attr-defined]
            )
            if refusal is not None:
                self.pause_for_spend(ticket, refusal)  # type: ignore[attr-defined]
                return
        mirror = self._mirror(ticket.repo)  # type: ignore[attr-defined]
        token = self._token(ticket.repo)
        mirror.ensure(token)
        _write_excludes(mirror)
        ordinal = len(self.store.list_attempts(ticket.id)) + 1
        counted = list(self.history(ticket).attempts)  # type: ignore[attr-defined]
        try:
            wt = mirror.create(
                ticket.base_branch,
                ticket.issue_number,
                ordinal,
                token=token,
                fetch=False,
                start_point=_start_point(mirror, ticket, counted),
            )
        except WorktreeError as exc:
            # #53: no attempt row yet; nothing to set aside, the flag stops the ticket.
            self.flag_setup_failed(ticket, None, SetupFailed(WORKTREE, str(exc)))
            return
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
        if self._sandbox_refused(ticket, wt):
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
        self.status_update(  # type: ignore[attr-defined]  # #77
            ticket,
            "escalated" if step.trigger == "escalation" else "attempt_started",
            attempt=ordinal,
            tier=endpoint.tier,
            model=endpoint.name,
            started_at=att.started_at,
        )
        if split_pick is not None:
            log.info("ticket %d: attempt %d: %s", ticket.id, att.id, split_pick.reason)
        self._run_and_finish(ticket, att, wt, endpoint, cmd, cancel, template=template)

    def _split_pick(self, ticket: Ticket, step: esc.Retry, endpoint: Any) -> Any:
        """#34 (``experiments.split.pick``): None when no split applies. Never raises: a
        broken split leaves the attempt as escalation chose it."""
        ex = getattr(self.config, "experiments", None)
        try:
            return split_mod.pick(
                self.store,
                self.config,
                ticket,
                step,
                endpoint,
                self.history(ticket).attempts,  # type: ignore[attr-defined]
                min_cases=int(getattr(ex, "min_cases", 10)),
            )
        except Exception:
            log.exception("ticket %d: traffic split check failed; no split", ticket.id)
            return None

    def _run_and_finish(
        self,
        ticket: Ticket,
        att: Attempt,
        wt: Worktree,
        endpoint: Any,
        cmd: verify_mod.VerifyCommand,
        cancel: threading.Event,
        *,
        conflicted: Sequence[str] = (),
        template: PromptRef | None = None,
    ) -> bool | None:
        """Run a started attempt (``_run_attempt``), then what follows it: a spend pause,
        file locks, ``ChecksFailed``, or the publish. Returns whether the checks passed
        (None: cancelled, set aside or refused). ``template``: the writer prompt (#34's
        split challenger); None is the champion's."""
        try:
            att, passed, vr, diff = self._run_attempt(
                ticket, att, wt, endpoint, cmd, cancel, conflicted=conflicted, template=template
            )
        except _Cancelled:
            log.info("ticket %d: attempt %d cancelled; marked superseded", ticket.id, att.id)
            return None
        except sandbox.SandboxUnavailable as exc:
            # Fail closed (#40): nothing ran; the attempt is not counted, and the flag
            # stops the ticket until the user acts.
            self.set_aside(ticket, att, "the sandbox could not start")  # type: ignore[attr-defined]
            self._event(  # type: ignore[attr-defined]
                ticket.id, lifecycle.HandedToUser(ticket.id, f"{FLAG_SANDBOX}: {exc}")
            )
            return None
        except SetupFailed as exc:
            # #53: the attempt could not run; no model is to blame, so nothing escalates.
            self.flag_setup_failed(ticket, att, exc)
            return None
        except Exception:
            if self.store.get_attempt(att.id).ended_at is None:
                self.store.finish_attempt(att.id, "superseded")
            raise
        if self._after_run(ticket, att, wt, passed, vr, diff, cancel):
            return passed
        return None

    def _after_run(
        self,
        ticket: Ticket,
        att: Attempt,
        wt: Worktree,
        passed: bool,
        vr: Any,
        diff: Any,
        cancel: threading.Event,
    ) -> bool:
        """False when the attempt was set aside or paused (spend, lock yield)."""
        stop = _budget_stop(self.spend, att.id) if not passed else None
        if stop is not None:
            # #58: the attempt reached its own budget. Not a pause (nobody is asked) and
            # not a plain check failure: it keeps ``failed_checks`` (the outcome set has
            # no "budget") and records why; escalation then hands the ticket over.
            row = self.store.get_attempt(att.id)  # the final totals, not the refused call's
            detail = replace(stop, spent_usd=row.cost_usd, tokens_in=row.tokens_in).detail
            self.store.set_attempt_end_reason(att.id, "budget", detail)
            log.info("ticket %d: attempt %d stopped at its budget (%s)", ticket.id, att.id, detail)
        refusal = None if stop is not None else self.spend_refusal(att.id)  # type: ignore[attr-defined]
        if refusal is not None:
            self.set_aside(ticket, att, "a cloud call was refused at the spend cap")  # type: ignore[attr-defined]
            self.pause_for_spend(ticket, refusal)  # type: ignore[attr-defined]
            return False
        widened = self.locks.widen(ticket, wt.changed_files())
        if not passed:
            self._event(ticket.id, ChecksFailed(ticket.id))  # type: ignore[attr-defined]
            return True
        if ticket.id in widened.yielded:
            self.set_aside(ticket, att, "it yielded its file locks to break a deadlock")  # type: ignore[attr-defined]
            return False
        if not widened.ok:
            self.set_lock_wait(ticket, att, widened.blockers)  # type: ignore[attr-defined]
            log.info(
                "ticket %d: attempt %d passed but changed files held by ticket(s) %s; "
                "publishing when they are free",
                ticket.id,
                att.id,
                ", ".join(f"#{b}" for b in widened.blockers),
            )
            return True
        self.publish_job_for(ticket.id, att, verify=vr, diff=diff, cancel=cancel)  # type: ignore[attr-defined]
        return True

    def flag_setup_failed(self, ticket: Ticket, att: Attempt | None, exc: SetupFailed) -> None:
        """#53: set the attempt aside (stored ``superseded``, uncounted: out of
        ``Worker.history`` and the per-model metrics) and flag the ticket once with what
        failed. A flagged ticket gets no job until the user acts."""
        if att is not None:
            self.set_aside(ticket, att, f"it could not run ({exc.reason})")  # type: ignore[attr-defined]
        log.warning("ticket %d: the attempt could not run (%s); flagged for you", ticket.id, exc)
        self._event(  # type: ignore[attr-defined]
            ticket.id, lifecycle.HandedToUser(ticket.id, exc.flag_reason)
        )

    def _sandbox_refused(self, ticket: Ticket, wt: Worktree) -> bool:
        """#40 fail closed, before an attempt row exists: flag the ticket, count nothing."""
        why = self.sandbox_ready(wt.path)  # type: ignore[attr-defined]
        if why is None:
            return False
        wt.remove()
        log.warning("ticket %d: sandbox unavailable; not running the attempt", ticket.id)
        self._event(  # type: ignore[attr-defined]
            ticket.id, lifecycle.HandedToUser(ticket.id, f"{FLAG_SANDBOX}: {why}")
        )
        return True

    def _run_attempt(
        self,
        ticket: Ticket,
        att: Attempt,
        wt: Worktree,
        endpoint: Any,
        cmd: verify_mod.VerifyCommand,
        cancel: threading.Event,
        *,
        conflicted: Sequence[str] = (),
        template: PromptRef | None = None,
    ) -> tuple[Attempt, bool, Any, Any]:
        if template is None:
            template = self.writer_template()
        # #38, #40: global rules, the repo's rules_file, AGENTS.md + CLAUDE.md, nested files.
        conv = conventions_mod.gather(
            wt.path, ticket.owned_files, rules_file=_rules_file(self.config, ticket.repo)
        )
        qa = self.store.get_cursor(ticket.repo, clarify_qa_cursor(ticket.id))
        prompt = build_writer_prompt(  # ``writer_prompt_for`` with the champion's template
            ticket,
            att,
            load_history(self.store, ticket.id, before=att.id),
            verify_command=cmd.display,
            conventions=conv,
            template=template,
            conflicted_files=conflicted,
            clarification=qa,
            issue_comments=fetch_issue_comments(  # #25: allowed users' comments, not the Q&A
                self.api, ticket, is_allowed=self.config.is_allowed_user, clarification=qa
            ),
            images=self.ticket_images(ticket),  # #78: described attachments
        )
        variant = variants.register(self.store, "writer", endpoint.name, template).id
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
            memory.attempt_snapshots(  # #59: memory + Ollama state at start and end
                span,
                memory.local_ollama_host(endpoint, getattr(self.config, "models", ())),
                snap=self.resource_snapshot,
            ),
        ):
            span.set_attribute("conventions.sha256", conv.sha256)  # #38: which rules
            try:
                self.prepare_worktree(ticket, att, wt)
                result = self._launch_writer_once_more(
                    ticket=ticket,
                    attempt_id=att.id,
                    worktree=wt,
                    prompt=prompt.text,
                    endpoint=endpoint,
                    proxy_base_url=self.proxy.base_url(att.id),
                    timeout=self.attempt_timeout,
                    cancel=cancel,
                )
            except SetupFailed as exc:
                span.set_attribute("attempt.outcome", "setup_failed")
                rec.close("superseded", stopped_reason="setup_failed", error=str(exc))
                raise
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
            elif left := leftover_markers(wt.path, conflicted):
                _write(
                    out_path,
                    "Git conflict markers (<<<<<<< or >>>>>>>) are still in: "
                    + ", ".join(left)
                    + ". Resolve every conflict and remove all markers.",
                )
                passed = False
            else:
                self.status_update(  # type: ignore[attr-defined]  # #77
                    ticket,
                    "verifying",
                    attempt=len(self.store.list_attempts(ticket.id)),
                    tier=att.tier,
                    model=att.model,
                    started_at=att.started_at,
                )
                with tracing.verify_span(cmd.display) as vspan:
                    vr = self._launch_verify(
                        cmd, wt.path, ticket=ticket, attempt_id=att.id,
                        timeout=self.verify_timeout, output_path=out_path, cancel=cancel,
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

    def ticket_images(self, ticket: Ticket) -> str | None:
        """#78: the images allowed users attached to the issue body, their needs-info
        answers and comments, fetched here in the worker (never in the sandbox) from
        GitHub's attachment hosts only, stored under the ticket's trace dir, and
        described by a vision model (local first; cloud only when the repo allows it,
        through the proxy and its spend caps). None when there are none. Never raises:
        a failure costs the attempt its image descriptions, nothing else."""
        try:
            comments = list(self.api.list_issue_comments(ticket.repo, ticket.issue_number))
            if not images_mod.image_urls(ticket.request) and not any(
                images_mod.image_urls(c.body or "") for c in comments
            ):
                return None  # the common case: no GitHub attachment links, no more calls
            issue = self.api.get_issue(ticket.repo, ticket.issue_number)
            srcs = images_mod.sources(
                body=ticket.request,
                body_author=issue.author,
                comments=comments,
                is_allowed=self.config.is_allowed_user,
                bot_login=self.api.bot_login(),
            )
            return images_mod.images_text(
                srcs,
                images_mod.images_dir(self.traces_dir, ticket.id),
                models=getattr(self.config, "models", ()),
                allow_cloud=self.store.repo_allows_cloud(ticket.repo),
                proxy=self.proxy,
                token=self._token(ticket.repo),
                transport=self.image_transport,
            )
        except Exception as exc:  # noqa: BLE001 - images are optional context
            log.warning(
                "ticket %d: could not gather the issue's images (%s); going on without them",
                ticket.id,
                exc,
            )
            return None

    def _launch_writer_once_more(self, **kw: Any) -> Any:
        """``_launch_writer``, with exactly one immediate re-run when the run could not
        start (``worker_setup.could_not_run``: no server, no session, the prompt not
        sent, and no model call). Raises ``SetupFailed`` when the last launch could not
        run either; returns the result otherwise (a cancelled run is returned as is)."""
        failure: SetupFailed | None = None
        for n in range(1, WRITER_SETUP_TRIES + 1):
            result = self._launch_writer(**kw)
            cancel = kw.get("cancel")
            if cancel is not None and cancel.is_set():
                return result
            failure = setup_failure(result, proxy_tokens=self._proxy_tokens(kw["attempt_id"]))
            if failure is None:
                return result
            if failure.reason not in RETRIED_ONCE:
                break
            if n < WRITER_SETUP_TRIES:
                log.warning(
                    "ticket %d: attempt %d could not start the coding agent (%s); "
                    "re-running it once",
                    kw["ticket"].id,
                    kw["attempt_id"],
                    failure,
                )
        assert failure is not None
        raise failure

    def _proxy_tokens(self, attempt_id: int) -> int:
        """Tokens the model proxy recorded for the attempt (any model call that returned
        usage adds to the row)."""
        a = self.store.get_attempt(attempt_id)
        return int(a.tokens_in or 0) + int(a.tokens_out or 0)

    def _supersede(self, att: Attempt, rec: Any) -> None:
        self.store.finish_attempt(att.id, "superseded")
        rec.close("superseded", stopped_reason="cancelled")
        raise _Cancelled

    # ------------------------------------------------------------------ helpers

    def writer_template(self) -> PromptRef:
        """#24: the writer champion's prompt, read from the file on disk (what runs is the
        file; an edited file is a new, unpromoted variant, and ``fman status`` says so).
        No champion, or its file gone: the packaged default."""
        champ = variants.champion(self.store, "writer")
        name = champ.prompt_name if champ is not None else WRITER_PROMPT_NAME
        try:
            return prompts.load(name)
        except (FileNotFoundError, ValueError):
            log.warning("writer champion's prompt %s is missing; using %s", name,
                        WRITER_PROMPT_NAME)  # fmt: skip
            return prompts.load(WRITER_PROMPT_NAME)

    def _token(self, repo: str) -> str:
        token_for = getattr(self.api, "token_for", None)
        return token_for(repo) if token_for is not None else self.api.token()


# --------------------------------------------------------------------------- helpers


def leftover_markers(root: Path, files: Sequence[str]) -> list[str]:
    """The ``files`` (from a rebase replay, #43) that still hold a conflict marker line."""
    left = []
    for name in files:
        try:
            text = (Path(root) / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue  # deleted while resolving: no markers
        if any(ln.startswith(("<<<<<<<", ">>>>>>>")) for ln in text.splitlines()):
            left.append(name)
    return left


def _budget_stop(spend: Any, attempt_id: int) -> Any:
    """``Spend.budget_stop`` (#58); None for a spend stand-in without it."""
    fn = getattr(spend, "budget_stop", None)
    return fn(attempt_id) if fn is not None else None


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")


def _rules_file(config: Any, repo: str) -> Any:
    """#38: the repo's configured ``rules_file`` (None when unset or no such config)."""
    getter = getattr(config, "rules_file_for", None)
    return getter(repo) if getter is not None else None


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


__all__ = ["EXCLUDES", "AttemptSteps", "leftover_markers"]
