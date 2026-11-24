"""Setup failures: "the attempt could not run" versus "the change failed its checks"
(todo #53, found in the first live run).

An attempt *could not run* when the writer never got to work for a reason no model can
fix: the OpenCode server did not start, its session could not be created, the prompt
could not be sent, the attempt's worktree could not be prepared, or a
``prepare_worktree`` step (dependency installation, #45) raised ``SetupFailed``. Such an
attempt is not a failure of its model: the worker sets it aside (stored ``superseded``,
kept out of ``Worker.history`` and out of the per-model metrics) and flags the ticket
once, ``setup_failed: <what failed>: <detail>``. Nothing climbs the ladder, nothing is
retried on the same rung or a higher one, and no cloud model is ever called for it.

This module is importable without importing the worker (slot B's install mixin and the
replay runner use it).

The rule (``could_not_run``), from what the OpenCode driver reports today
------------------------------------------------------------------------------
All of these must hold:

1. ``status == "errored"`` and the run was not cancelled by the worker (a cancelled run
   is ``errored`` too, and keeps its own path);
2. no sign that any work happened: no assistant message, no tool call, no edit, no edited
   file, 0 tokens in and out as OpenCode counted them, and 0 tokens recorded by the model
   proxy for the attempt (``proxy_tokens``, the attempt row's ``tokens_in + tokens_out``,
   which the proxy adds to on every model call that returned usage);
3. the error comes from before the model could be reached:

   * ``session_id is None``: the session never opened. The driver's error text then says
     whether the server did not start (``opencode serve exited ...``, ``... did not start
     in Ns``, the state-dir check) → ``opencode_start``, or whether ``POST /session``
     failed (a 500, a dropped connection) → ``session_create``;
   * or the error is the prompt request itself (``POST /session/<id>/prompt_async: ...``)
     → ``prompt_send``.

Anything else stays as it is today: a timeout, a finished session with no changes, a
model-server error after the session opened (an assistant message carries it), a server
that died after the prompt was accepted, or a run with any tool call or token. Elapsed
time is not used: a server that "did not start" fails only after the startup timeout,
and a fast failure with work evidence is still the model's.

Re-run (invariant 7e)
---------------------
A driver-side setup failure (``opencode_start``, ``session_create``, ``prompt_send``) gets
exactly one immediate re-run inside the same attempt row (``WRITER_SETUP_TRIES`` = 2
launches in all): a fresh ``opencode serve`` costs about a second and no model call, and
it covers the transient causes (a slow first start, a server that crashed on its first
session). A ``SetupFailed`` from ``prepare_worktree`` and a worktree that could not be
created are not re-run by the worker (an install that fails is slow and almost always
fails the same way; a ``prepare_worktree`` implementation may retry inside itself). So a
ticket makes at most one attempt row and two OpenCode launches, then it is flagged.

Release: the flag stops the ticket (a flagged ticket gets no job, before or after a
restart). The user fixes the cause and removes the ``agent`` label and adds it again,
which drops this ticket and starts a new one (the flag comment says so).
"""

from __future__ import annotations

import os
from typing import Any

#: Flag reason prefix: the attempt could not run (todo #53).
FLAG_SETUP = "setup_failed"

#: OpenCode launches per attempt before a driver-side setup failure flags the ticket.
WRITER_SETUP_TRIES = 2

# What failed (the second part of the flag reason).
OPENCODE_START = "opencode_start"
SESSION_CREATE = "session_create"
PROMPT_SEND = "prompt_send"
WORKTREE = "worktree"

#: Reasons the worker re-runs once (driver side); others flag at once.
RETRIED_ONCE = frozenset({OPENCODE_START, SESSION_CREATE, PROMPT_SEND})

_DETAIL_MAX = 500


class SetupFailed(Exception):
    """The attempt could not run, for a reason no model can fix.

    ``reason``: a short name of what failed (``dependency_install``, ``session_create``,
    ...; no colon). ``detail``: the error, for the user, shown in the issue comment (keep
    it short; it is clipped, and the home directory is written as ``~``)."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = (reason or "setup").replace(":", " ").strip() or "setup"
        self.detail = clean_detail(detail)
        super().__init__(f"{self.reason}: {self.detail}" if self.detail else self.reason)

    @property
    def flag_reason(self) -> str:
        """``setup_failed: <reason>: <detail>`` (the ticket's ``flag_reason``)."""
        return f"{FLAG_SETUP}: {self}"


def clean_detail(text: str | None) -> str:
    """One line, clipped, the home directory as ``~`` (the detail goes on the issue)."""
    one = " ".join(str(text or "").split())
    home = os.path.expanduser("~")
    if home and home != "~":
        one = one.replace(home, "~")
    return one if len(one) <= _DETAIL_MAX else one[: _DETAIL_MAX - 3] + "..."


def _work_seen(result: Any) -> bool:
    return bool(
        getattr(result, "assistant_messages", 0)
        or getattr(result, "tool_calls", 0)
        or getattr(result, "edits", 0)
        or getattr(result, "files_edited", ())
        or getattr(result, "tokens_in", 0)
        or getattr(result, "tokens_out", 0)
    )


def could_not_run(result: Any, *, proxy_tokens: int = 0) -> str | None:
    """What failed when the driver's ``result`` shows the writer never got to work (the
    rule in the module docstring), else None. ``proxy_tokens``: tokens the model proxy
    recorded for the attempt (0 when unknown)."""
    if getattr(result, "status", None) != "errored" or getattr(result, "cancelled", False):
        return None
    if proxy_tokens or _work_seen(result):
        return None
    error = str(getattr(result, "error", None) or "")
    if getattr(result, "session_id", None) is None:
        if error.startswith(("opencode serve", "the OpenCode state dir")):
            return OPENCODE_START
        return SESSION_CREATE
    method, _, rest = error.partition(" ")
    path = rest.split(" ", 1)[0].rstrip(":")
    if method == "POST" and path.startswith("/session/") and path.endswith("/prompt_async"):
        return PROMPT_SEND
    return None


def setup_failure(result: Any, *, proxy_tokens: int = 0) -> SetupFailed | None:
    """``could_not_run`` as an exception carrying the driver's error, or None."""
    why = could_not_run(result, proxy_tokens=proxy_tokens)
    if why is None:
        return None
    return SetupFailed(why, str(getattr(result, "error", None) or getattr(result, "status", "")))


__all__ = [
    "FLAG_SETUP",
    "OPENCODE_START",
    "PROMPT_SEND",
    "RETRIED_ONCE",
    "SESSION_CREATE",
    "WORKTREE",
    "WRITER_SETUP_TRIES",
    "SetupFailed",
    "clean_detail",
    "could_not_run",
    "setup_failure",
]
