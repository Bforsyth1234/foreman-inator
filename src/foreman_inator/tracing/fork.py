"""Fork from failure (todo #36, GitHub #18; SPEC "Tracing" -> "Recording and replay").

``fork_attempt(deps, attempt_id, model=, prompt=)`` restarts a recorded attempt at its
labelled failure step (``kickbacks.failure_step`` / ``failure_step_index``, #27) with a
changed model or writer prompt, and writes what happened as a recording next to the
attempt's: ``<recordings>/<ticket>/<attempt>.fork_<n>.jsonl.gz`` (``replay.fork_role``).
The replay viewer (``replay.timeline``, the trace view) shows it like an attempt; it is
never span-tree or regression input, never an ``experiments``/``replay_results`` row, and
never promotion input. It makes no GitHub call and writes no live table.

Rule 12 (checked 2026-11-25 against the installed OpenCode 1.18.7: ``opencode --version``
and the server's own OpenAPI document at ``GET /doc``, plus https://opencode.ai/docs/server/):
can a session be seeded with an earlier transcript? No, not with one from a recording.

* ``POST /session/{id}/fork`` body ``{messageID}`` copies a session *that is still in that
  server's storage*. The driver gives every attempt its own state dir and deletes it when
  the run ends (``OpenCodeDriver(keep_state=False)``), and the session belonged to a
  worktree that is gone, so there is nothing left to fork.
* ``POST /session/{id}/message`` / ``prompt_async`` take ``parts`` of type text, file,
  agent or subtask only (``TextPartInput``, ...; ``noReply: true`` stores a user message
  without a reply). No input type creates an assistant message or a tool part, so the
  recorded model turns and tool results cannot be put back as history.
* The v2 ``/api/session`` routes (create, prompt, context, history) have no import either.

So the fallback, SPEC's "tools re-run in a fresh worktree at that point":

1. ``plan_fork``: the attempt's latest kickback with a ``failure_step_index`` (or an
   explicit ``step``), its recording, and the recorded file edits before that step:
   successful ``write`` / ``edit`` / ``multiedit`` tool calls (OpenCode's tool inputs:
   ``write {filePath, content}``, ``edit {filePath, oldString, newString, replaceAll}``,
   where an empty ``oldString`` writes the whole file, ``multiedit {filePath, edits}``;
   https://opencode.ai/docs/tools/, read 2026-11-25). ``patch`` / ``apply_patch`` and shell
   commands are *not* re-run: they are listed as not re-applied, and the summary says so.
2. A fresh scratch worktree at the attempt's base commit (``RepoMirror.scratch``, branch
   ``fman/fork/a<attempt>-<n>``, can never push); the edits are re-applied by this module
   (plain file writes confined to the worktree: absolute paths must be under the old
   worktree, no ``..``, no symlinks) and committed as the prefix commit.
3. A new writer session, run by the driver under the sandbox (``deps.sandbox(...,
   writer=True)``; ``sandbox.ensure_available`` first, and nothing runs unsandboxed), with
   the prompt: the recorded prompt, or with ``prompt=<template>`` the writer prompt rebuilt
   from the store with that template; then ``FORK_SECTION``: a defused, fenced summary of
   the transcript up to the failure step (``transcript_summary``).
4. Verify under the sandbox when the repo has a verify command; the result row carries
   ``tests_passed``, the diff counts and the head commit, and the fork's diff is a final
   ``fork.diff`` tool row (spilled to ``.files/`` when long).

Invariant 7f mirrored (``tickets/prompt.py``: the live writer never sees its own earlier
transcript) does not apply here: a fork exists to continue a transcript, its output never
becomes a PR, a review or an attempt row, and the transcript text is fenced and defused.

Spend and 7c: model calls go through the proxy in the ``experiments`` bucket (the real
spend hook's caps apply), capped per fork by ``ForkSettings.budget_usd`` (the replay
runner's spend tap); a cloud model is refused for a repo set local only
(``store.repo_allows_cloud``), and up front when the experiments budget has no headroom.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fde_kit.prompts import PromptRef

from foreman_inator.db.store import Attempt, Kickback, NotFoundError, Store
from foreman_inator.runner import sandbox
from foreman_inator.runner import verify as verify_mod
from foreman_inator.runner.worktree import Worktree, WorktreeError
from foreman_inator.tickets import conventions as conventions_mod
from foreman_inator.tickets.lifecycle import clarify_qa_cursor
from foreman_inator.tickets.prompt import (
    build_writer_prompt,
    clip_tail,
    defuse,
    load_history,
    load_writer_template,
)
from foreman_inator.tracing import recorder as recorder_mod
from foreman_inator.tracing import replay
from foreman_inator.tracing.failure_label import find_recording

log = logging.getLogger(__name__)

#: Cloud spend one fork may use (inside the experiments bucket's own caps).
FORK_BUDGET_USD = 1.0
FORK_WRITER_TIMEOUT_S = 20 * 60.0
FORK_VERIFY_TIMEOUT_S = 15 * 60.0
#: Characters of transcript summary the fork prompt carries (the latest steps are kept).
SUMMARY_BUDGET = 6000
STEP_LINE = 240
EDIT_TOOLS = frozenset({"write", "edit", "multiedit"})
UNREPLAYED_EDIT_TOOLS = frozenset({"patch", "apply_patch"})
DIFF_TOOL = "fork.diff"
PREFIX_TOOL_KIND = "fork_prefix"

FORK_SECTION = """\
## Restarting from an earlier attempt

An earlier attempt at this ticket went wrong at step {step}{label}. The file changes it
made before that step are already in your worktree. The summary below is what that
attempt did up to there. Continue from this point and take a different approach at the
step that went wrong; do not repeat it.

----- BEGIN EARLIER ATTEMPT -----
{summary}
----- END EARLIER ATTEMPT -----
"""


class ForkError(ValueError):
    """The fork cannot start (no recording, no labelled step, refused); nothing ran."""


@dataclass(frozen=True)
class ForkSettings:
    writer_timeout_s: float = FORK_WRITER_TIMEOUT_S
    verify_timeout_s: float = FORK_VERIFY_TIMEOUT_S
    budget_usd: float | None = FORK_BUDGET_USD
    summary_budget: int = SUMMARY_BUDGET
    keep_worktree: bool = False


@dataclass(frozen=True)
class RecordedEdit:
    """One recorded file-edit tool call before the failure step."""

    step_index: int  # 1-based row position (``failure_step_index`` numbering)
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class EditOutcome:
    step_index: int
    name: str
    path: str | None
    applied: bool
    detail: str = ""


@dataclass(frozen=True)
class ForkPlan:
    ticket_id: int
    attempt: Attempt
    repo: str
    base_commit: str
    step_index: int
    failure_step: str | None
    kickback_id: int | None
    recording: Path
    rows: tuple[dict[str, Any], ...]  # the recording's rows (not the header)
    edits: tuple[RecordedEdit, ...]
    unreplayed: tuple[tuple[int, str], ...]  # (step, tool) edits not re-run (patches)
    recorded_prompt: str | None

    @property
    def prefix(self) -> tuple[dict[str, Any], ...]:
        """The rows before the failure step."""
        return self.rows[: self.step_index - 1]


@dataclass(frozen=True)
class ForkResult:
    attempt_id: int
    ticket_id: int
    role: str
    recording: Path
    outcome: str
    """passed | failed | no_change | timed_out | agent_failed | error | stopped | cancelled"""
    model: str
    prompt_name: str | None
    step_index: int
    edits: tuple[EditOutcome, ...] = ()
    detail: str | None = None
    tests_passed: bool | None = None
    diff_files: int = 0
    diff_lines: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    worktree: Path | None = None

    def lines(self) -> list[str]:
        applied = sum(1 for e in self.edits if e.applied)
        out = [
            f"fork {self.role} of attempt {self.attempt_id} (ticket {self.ticket_id}) at step "
            f"{self.step_index}: {self.outcome}",
            f"  model {self.model}" + (f", prompt {self.prompt_name}" if self.prompt_name else ""),
            f"  re-applied {applied}/{len(self.edits)} recorded edits; "
            f"{self.diff_files} files, {self.diff_lines} lines changed",
            f"  {self.tokens_in}+{self.tokens_out} tokens, ${self.cost_usd:.4f}",
            f"  recording: {self.recording}",
        ]
        out += [f"  not re-applied: step {e.step_index} {e.name}: {e.detail}"
                for e in self.edits if not e.applied]  # fmt: skip
        if self.detail:
            out.append(f"  {self.detail}")
        return out


# --------------------------------------------------------------------------- planning


def failure_kickback(kickbacks: list[Kickback]) -> Kickback | None:
    """The latest kickback with a labelled failure step index, or None."""
    labelled = [k for k in kickbacks if k.failure_step_index is not None]
    return labelled[-1] if labelled else None


def recorded_edits(
    rows: tuple[dict[str, Any], ...] | list[dict[str, Any]], before: int
) -> tuple[list[RecordedEdit], list[tuple[int, str]]]:
    """Successful file-edit tool rows with step index < ``before``, and the edit-kind
    rows this module does not re-run (patches)."""
    edits: list[RecordedEdit] = []
    skipped: list[tuple[int, str]] = []
    for i, row in enumerate(rows[: max(0, before - 1)], start=1):
        if row.get("kind") != "tool" or row.get("is_error"):
            continue
        name = str(row.get("name") or "")
        inp = row.get("input") if isinstance(row.get("input"), dict) else {}
        if name in EDIT_TOOLS:
            edits.append(RecordedEdit(i, name, inp))
        elif name in UNREPLAYED_EDIT_TOOLS:
            skipped.append((i, name))
    return edits, skipped


def plan_fork(
    store: Store, recordings_dir: str | Path, attempt_id: int, *, step: int | None = None
) -> ForkPlan:
    """Everything a fork needs, read from the store and the attempt's recording."""
    aid = replay.parse_id(attempt_id)
    if aid is None:
        raise ForkError(f"not an attempt id: {attempt_id!r}")
    try:
        att = store.get_attempt(aid)
    except NotFoundError as exc:
        raise ForkError(f"no attempt {aid}") from exc
    ticket = store.get_ticket(att.ticket_id)
    path = find_recording(recordings_dir, att.ticket_id, aid)
    if path is None:
        raise ForkError(f"attempt {aid} has no recording (pruned, or it never started one)")
    try:
        record = replay._read_record(path)
    except (OSError, ValueError, EOFError) as exc:
        raise ForkError(f"attempt {aid}: recording unreadable: {exc}") from exc
    rows = tuple(record.rows)
    kick = failure_kickback(store.list_kickbacks(attempt_id=aid))
    if step is None:
        if kick is None:
            raise ForkError(
                f"attempt {aid} has no labelled failure step (kickbacks.failure_step_index);"
                " label it first or pass a step"
            )
        step = kick.failure_step_index
    if isinstance(step, bool) or not isinstance(step, int) or not 1 <= step <= len(rows):
        raise ForkError(f"step {step!r} is not a row of attempt {aid}'s recording")
    if rows[step - 1].get("kind") not in ("user", "llm", "tool"):
        raise ForkError(f"step {step} of attempt {aid} is not a prompt, model or tool row")
    base = att.base_commit or record.header.get("base_commit")
    if not base:
        raise ForkError(f"attempt {aid} has no base commit")
    edits, unreplayed = recorded_edits(rows, step)
    prompt = next((r.get("text") for r in rows if r.get("kind") == "user"), None)
    return ForkPlan(
        ticket_id=att.ticket_id,
        attempt=att,
        repo=ticket.repo,
        base_commit=str(base),
        step_index=step,
        failure_step=kick.failure_step
        if kick is not None and step == kick.failure_step_index
        else None,
        kickback_id=kick.id if kick is not None else None,
        recording=path,
        rows=rows,
        edits=tuple(edits),
        unreplayed=tuple(unreplayed),
        recorded_prompt=prompt if isinstance(prompt, str) else None,
    )


def next_fork_number(recordings_dir: str | Path, ticket_id: int, attempt_id: int) -> int:
    """1 + the highest ``fork_<n>`` recording (finished or partial) of the attempt."""
    d = Path(recordings_dir) / str(ticket_id)
    best = 0
    if d.is_dir():
        for p in d.iterdir():
            head, _, rest = p.name.partition(".")
            role = rest.removesuffix(".gz").removesuffix(".jsonl")
            if head == str(attempt_id) and replay.is_fork_role(role):
                best = max(best, int(role.removeprefix(replay.FORK_ROLE_PREFIX)))
    return best + 1


# --------------------------------------------------------------------------- re-applying


def _target(root: Path, old_root: Path | None, file_path: Any) -> Path:
    if not isinstance(file_path, str) or not file_path or "\0" in file_path:
        raise WorktreeError("no file path")
    p = Path(file_path)
    if p.is_absolute():
        if old_root is None:
            raise WorktreeError("absolute path and no recorded worktree")
        try:
            p = p.relative_to(old_root)
        except ValueError:
            try:
                p = p.resolve().relative_to(old_root.resolve())
            except (ValueError, OSError):
                raise WorktreeError("path outside the attempt's worktree") from None
    if ".." in p.parts or not p.parts or p.parts[0] == ".git":
        raise WorktreeError("path leaves the worktree")
    dest = root / p
    cur = root
    for part in p.parts:  # no symlink anywhere on the way (the attempt's or the repo's)
        cur = cur / part
        if cur.is_symlink():
            raise WorktreeError("path goes through a symlink")
    return dest


def _replace(text: str, old: Any, new: Any, replace_all: Any) -> str:
    if not isinstance(old, str) or not isinstance(new, str):
        raise WorktreeError("edit without oldString/newString")
    if old == "":
        return new  # OpenCode: an empty oldString writes the whole file
    n = text.count(old)
    if n == 0:
        raise WorktreeError("oldString not found")
    if n > 1 and not replace_all:
        raise WorktreeError("oldString is not unique")
    return text.replace(old, new) if replace_all else text.replace(old, new, 1)


def apply_edit(root: Path, old_root: Path | None, edit: RecordedEdit) -> EditOutcome:
    """Re-apply one recorded edit inside ``root``. Never raises."""
    inp = edit.input
    raw = inp.get("filePath") or inp.get("file_path") or inp.get("path")
    try:
        dest = _target(root, old_root, raw)
        rel = str(dest.relative_to(root))
        if dest.is_dir():
            raise WorktreeError("path is a directory")
        if edit.name == "write":
            content = inp.get("content")
            if not isinstance(content, str):
                raise WorktreeError("write without content")
        else:
            text = dest.read_text(encoding="utf-8") if dest.exists() else ""
            steps = inp.get("edits") if edit.name == "multiedit" else [inp]
            if not isinstance(steps, list) or not steps:
                raise WorktreeError("multiedit without edits")
            for s in steps:
                if not isinstance(s, dict):
                    raise WorktreeError("bad edit entry")
                text = _replace(text, s.get("oldString"), s.get("newString"), s.get("replaceAll"))
            content = text
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        return EditOutcome(edit.step_index, edit.name, rel, True)
    except (WorktreeError, OSError, UnicodeDecodeError) as exc:
        shown = raw if isinstance(raw, str) else None
        return EditOutcome(edit.step_index, edit.name, shown, False, str(exc) or "failed")


# --------------------------------------------------------------------------- the summary


def _one_line(value: Any, budget: int = STEP_LINE) -> str:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, default=str)
    flat = " ".join(value.split())
    return flat if len(flat) <= budget else flat[: budget - 3].rstrip() + "..."


def _tool_target(row: dict[str, Any], old_root: Path | None) -> str:
    inp = row.get("input") if isinstance(row.get("input"), dict) else {}
    for key in ("filePath", "file_path", "path", "pattern", "command"):
        v = inp.get(key)
        if isinstance(v, str) and v:
            if old_root is not None and key != "command":
                with contextlib.suppress(ValueError):
                    v = str(Path(v).relative_to(old_root))
            return _one_line(v, 120)
    return ""


def step_line(i: int, row: dict[str, Any], old_root: Path | None = None) -> str | None:
    kind = row.get("kind")
    if kind == "llm":
        calls = ", ".join(str(c.get("name")) for c in row.get("tool_calls") or [])
        text = _one_line(row.get("text") or "")
        return f"step {i}: model: {text or '(no text)'}" + (f" [calls {calls}]" if calls else "")
    if kind == "tool":
        target = _tool_target(row, old_root)
        err = "FAILED: " if row.get("is_error") else ""
        out = _one_line(row.get("content") or "", 160)
        return f"step {i}: {row.get('name')} {target}: {err}{out}".rstrip()
    return None


def transcript_summary(plan: ForkPlan, outcomes: tuple[EditOutcome, ...], budget: int) -> str:
    """What the attempt did before the failure step, one line per model or tool row,
    then the failing step and which edits were re-applied. Defused; the latest lines are
    kept when it is over ``budget``."""
    old_root = Path(plan.attempt.worktree) if plan.attempt.worktree else None
    lines = [ln for i, r in enumerate(plan.prefix, start=1) if (ln := step_line(i, r, old_root))]
    failed = step_line(plan.step_index, plan.rows[plan.step_index - 1], old_root)
    if failed:
        lines.append("went wrong at " + failed)
    done = [f"step {e.step_index} ({e.path})" for e in outcomes if e.applied]
    lines.append("re-applied: " + (", ".join(done) if done else "none"))
    missed = [f"step {e.step_index} {e.name} ({e.detail})" for e in outcomes if not e.applied]
    missed += [f"step {i} {n} (patch tools are not re-run)" for i, n in plan.unreplayed]
    if missed:
        lines.append("NOT re-applied, redo if needed: " + "; ".join(missed))
    lines.append("shell commands were not re-run.")
    return clip_tail(defuse("\n".join(lines)), budget)


def fork_prompt(base: str, plan: ForkPlan, summary: str) -> str:
    label = f" ({plan.failure_step})" if plan.failure_step else ""
    section = FORK_SECTION.format(step=plan.step_index, label=label, summary=summary)
    return f"{base.rstrip()}\n\n{section}"


# --------------------------------------------------------------------------- running


@contextlib.contextmanager
def _alias(rec: recorder_mod.Recorder, run_id: int):
    """Make ``rec`` the open recorder of synthetic attempt ``run_id`` as well, so the
    driver (``get_recorder(run_id)``) and the proxy write their rows into the fork's file.
    (``recorder.py`` has no public alias call; it is a two-line follow-up there.)"""
    key = (run_id, "attempt")
    with recorder_mod._open_lock:
        if key in recorder_mod._open:
            raise RuntimeError(f"a recorder for {run_id} is already open")
        recorder_mod._open[key] = rec
    try:
        yield rec
    finally:
        with recorder_mod._open_lock:
            if recorder_mod._open.get(key) is rec:
                del recorder_mod._open[key]


def _writer_template(prompt: str | PromptRef | None) -> PromptRef | None:
    if prompt is None or isinstance(prompt, PromptRef):
        return prompt
    if not isinstance(prompt, str) or not prompt.replace("-", "").replace("_", "").isalnum():
        raise ForkError(f"not a writer prompt name: {prompt!r}")
    try:
        return load_writer_template(prompt)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise ForkError(f"no writer prompt {prompt!r}") from exc


def _base_prompt(
    deps: Any, plan: ForkPlan, template: PromptRef | None, wt: Worktree, verify: Any
) -> str:
    if template is None:
        if plan.recorded_prompt is None:
            raise ForkError(f"attempt {plan.attempt.id}'s recording has no prompt row")
        return plan.recorded_prompt
    store: Store = deps.store
    ticket = store.get_ticket(plan.ticket_id)
    getter = getattr(deps.config, "rules_file_for", None)
    conv = conventions_mod.gather(
        wt.path, ticket.owned_files, rules_file=getter(ticket.repo) if getter else None
    )
    return build_writer_prompt(  # issue comments need GitHub; a fork makes no GitHub call
        ticket,
        plan.attempt,
        load_history(store, ticket.id, before=plan.attempt.id),
        verify_command=verify.display if verify is not None else None,
        conventions=conv,
        template=template,
        clarification=store.get_cursor(ticket.repo, clarify_qa_cursor(ticket.id)),
    ).text


def fork_attempt(
    deps: Any,
    attempt_id: int,
    *,
    model: str | None = None,
    prompt: str | PromptRef | None = None,
    step: int | None = None,
    settings: ForkSettings | None = None,
    cancel: threading.Event | None = None,
) -> ForkResult:
    """Restart ``attempt_id`` at its failure step (module docstring). ``deps`` is the
    replay runner's ``ReplayDeps`` (store, proxy, mirror_for, sandbox, driver,
    endpoint_for, spend, recordings_dir, work_dir, config). Raises ``ForkError`` when the
    fork cannot start; once it has started, the outcome is in the result and the
    recording."""
    from foreman_inator.experiments import replay_runner as rr

    settings = settings or ForkSettings()
    cancel = cancel or threading.Event()
    if deps.recordings_dir is None or deps.driver is None:
        raise ForkError("a fork needs deps.recordings_dir and deps.driver")
    store: Store = deps.store
    plan = plan_fork(store, deps.recordings_dir, attempt_id, step=step)
    template = _writer_template(prompt)
    name = model or plan.attempt.model
    try:
        endpoint = deps.endpoint_for(name)
    except (KeyError, ValueError) as exc:
        raise ForkError(f"no model {name!r} in the config") from exc
    if endpoint.is_cloud:
        if not store.repo_allows_cloud(plan.repo):
            raise ForkError(f"{plan.repo} is set local only; {endpoint.name} is a cloud model")
        if deps.spend is not None and not deps.spend.allow(0.0001, "experiments"):
            raise ForkError("the experiments spend budget has no headroom")
    mirror = deps.mirror_for(plan.repo)
    if mirror is None:
        raise ForkError(f"no mirror for {plan.repo} on this Mac")
    n = next_fork_number(deps.recordings_dir, plan.ticket_id, plan.attempt.id)
    role = replay.fork_role(n)
    label = f"fork-a{plan.attempt.id}-{n}"
    try:
        wt = mirror.scratch(plan.base_commit, label, branch=f"fman/fork/a{plan.attempt.id}-{n}")
    except (WorktreeError, ValueError) as exc:
        raise ForkError(f"could not check out base commit {plan.base_commit[:12]}: {exc}") from exc
    policies: list[sandbox.Policy] = []

    def policy(suffix: str, *, writer: bool) -> sandbox.Policy:
        p = deps.sandbox(plan.repo, Path(wt.path), f"{label}-{suffix}", writer=writer)
        policies.append(p)
        sandbox.ensure_available(p)  # SandboxUnavailable: nothing runs unsandboxed
        return p

    try:
        try:
            wpolicy = policy("writer", writer=True)
        except sandbox.SandboxUnavailable as exc:
            raise ForkError(f"sandbox unavailable: {exc}") from exc
        old_root = Path(plan.attempt.worktree) if plan.attempt.worktree else None
        outcomes = tuple(apply_edit(Path(wt.path), old_root, e) for e in plan.edits)
        prefix_commit = wt.commit_all(f"fork prefix: attempt {plan.attempt.id} to step "
                                      f"{plan.step_index}") or wt.base_sha  # fmt: skip
        rs = store.get_repo_settings(plan.repo)
        vcmd = verify_mod.detect(wt.path, override=rs.verify_command if rs else None)
        text = fork_prompt(
            _base_prompt(deps, plan, template, wt, vcmd),
            plan,
            transcript_summary(plan, outcomes, settings.summary_budget),
        )
        return _run(
            deps, rr, plan, settings, cancel, wt, endpoint, template, role, outcomes,
            prefix_commit, text, wpolicy, vcmd, policy,
        )  # fmt: skip
    finally:
        if not settings.keep_worktree:
            with contextlib.suppress(Exception):
                wt.remove()
        for p in policies:
            if p.mode == "on":
                shutil.rmtree(p.scratch, ignore_errors=True)


def _run(
    deps: Any,
    rr: Any,
    plan: ForkPlan,
    settings: ForkSettings,
    cancel: threading.Event,
    wt: Worktree,
    endpoint: Any,
    template: PromptRef | None,
    role: str,
    outcomes: tuple[EditOutcome, ...],
    prefix_commit: str,
    text: str,
    wpolicy: sandbox.Policy,
    vcmd: Any,
    policy: Any,
) -> ForkResult:
    prompt_name = template.name if template is not None else None
    rec = recorder_mod.Recorder(
        deps.recordings_dir,
        plan.ticket_id,
        plan.attempt.id,
        role=role,
        base_commit=plan.base_commit,
        model=endpoint.name,
        tier=endpoint.tier,
        variant_id=None,
        forked_from=plan.attempt.id,
        fork_step_index=plan.step_index,
        fork_failure_step=plan.failure_step,
        fork_kickback_id=plan.kickback_id,
        fork_model=endpoint.name if endpoint.name != plan.attempt.model else None,
        fork_prompt=prompt_name,
        prefix_commit=prefix_commit,
    )
    t0 = time.monotonic()
    run_id = rr._synthetic_id()
    tap = rr._tap_for(deps.proxy)
    budget = rr._Budget(settings.budget_usd)
    meter = None
    extra: dict[str, Any] = {}
    outcome, detail, passed = "error", None, None
    try:
        for e in outcomes:  # the re-applied prefix, as tool rows the viewer shows
            rec.tool(
                name=e.name,
                input={"filePath": e.path, "from_step": e.step_index},
                content="re-applied" if e.applied else f"not re-applied: {e.detail}",
                is_error=not e.applied,
                tool_kind=PREFIX_TOOL_KIND,
            )
        pv = f"{template.name}@{template.sha256[:12]}" if template is not None else None
        with (
            _alias(rec, run_id),
            tap.watch(budget) as meter,
            deps.proxy.context(
                attempt_id=run_id,
                review_id=run_id,  # no live attempt row: keeps the proxy off attempts usage
                recorder_role="attempt",
                bucket="experiments",
                label=f"fork:a{plan.attempt.id}",
                prompt_version=pv,
                writer_run=run_id,
            ) as pctx,
        ):
            meter.add_token(pctx.token)
            try:
                run = deps.driver.run(
                    attempt_id=run_id,
                    worktree=wt,
                    prompt=text,
                    endpoint=endpoint,
                    proxy_base_url=pctx.base_url,
                    timeout=settings.writer_timeout_s,
                    cancel=cancel,
                    sandbox=wpolicy,
                )
            finally:
                rr._forget_queue_wait(deps.proxy, run_id)
        if meter.refused:
            outcome, detail = "stopped", meter.refused
        elif cancel.is_set() or getattr(run, "cancelled", False):
            outcome = "cancelled"
        elif not run.finished:
            outcome = "timed_out" if run.status == "timed_out" else "agent_failed"
            detail = run.error or run.status
        else:
            head = wt.commit_all(f"fork of attempt {plan.attempt.id}") or prefix_commit
            extra["head_commit"] = head
            stats = wt.diff_stats()
            extra.update(diff_files=stats.diff_files, diff_lines=stats.diff_lines)
            rec.tool(name=DIFF_TOOL, input={"base": plan.base_commit, "head": head},
                     content=wt.diff_text(), tool_kind="fork_diff")  # fmt: skip
            if head == prefix_commit:
                outcome, detail = "no_change", "the writer changed no files after the fork"
            elif vcmd is None:
                outcome, detail = "finished", "no verify command for the repo"
            else:
                logs = Path(deps.work_dir) / "forks"
                out = logs / f"{plan.ticket_id}-a{plan.attempt.id}-{role}.log"
                out.parent.mkdir(parents=True, exist_ok=True)
                vr = verify_mod.run(
                    vcmd, wt.path, timeout=settings.verify_timeout_s, output_path=out,
                    cancel=cancel, sandbox=policy("verify", writer=False),
                )  # fmt: skip
                passed = bool(vr.passed)
                outcome = "passed" if passed else "failed"
                detail = None if passed else vr.output_tail(1500)
    except sandbox.SandboxUnavailable as exc:
        outcome, detail = "error", f"sandbox unavailable: {exc}"
    except Exception as exc:
        outcome, detail = "error", f"{type(exc).__name__}: {exc}"
        log.warning("fork of attempt %s failed: %s", plan.attempt.id, detail)
    finally:
        tokens_in = meter.tokens_in if meter else 0
        tokens_out = meter.tokens_out if meter else 0
        cost = meter.cost if meter else 0.0
        path = rec.close(
            outcome,
            output=detail or "",
            tests_passed=passed,
            fork_seconds=round(time.monotonic() - t0, 3),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
            **extra,
        )
    return ForkResult(
        attempt_id=plan.attempt.id,
        ticket_id=plan.ticket_id,
        role=role,
        recording=path,
        outcome=outcome,
        model=endpoint.name,
        prompt_name=prompt_name,
        step_index=plan.step_index,
        edits=outcomes,
        detail=detail,
        tests_passed=passed,
        diff_files=int(extra.get("diff_files", 0)),
        diff_lines=int(extra.get("diff_lines", 0)),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost,
        worktree=Path(wt.path) if settings.keep_worktree else None,
    )


__all__ = [
    "DIFF_TOOL",
    "FORK_BUDGET_USD",
    "FORK_SECTION",
    "EditOutcome",
    "ForkError",
    "ForkPlan",
    "ForkResult",
    "ForkSettings",
    "RecordedEdit",
    "apply_edit",
    "failure_kickback",
    "fork_attempt",
    "fork_prompt",
    "next_fork_number",
    "plan_fork",
    "recorded_edits",
    "step_line",
    "transcript_summary",
]
