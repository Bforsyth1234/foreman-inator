"""Run recordings: one file per attempt (and per AI review), fde-kit's JSONL row format.

On disk, under the recordings directory (`~/.foreman-inator/recordings/`, passed in):

    <ticket_id>/<attempt_id>.jsonl.gz          finished attempt (gzip of the JSONL below)
    <ticket_id>/<attempt_id>.jsonl             in progress, or the process died mid-attempt
    <ticket_id>/<attempt_id>.files/<seq>-<kind>.txt
                                               full text of any llm/tool output longer than
                                               CONTENT_LIMIT (spans carry only 4,000 chars)
    <ticket_id>/<attempt_id>.review.jsonl.gz   the AI review of that attempt (role="review")
    chat/<session_id>.jsonl.gz                 a chat session (role="chat", no ticket;
                                               docs/chat-design.md section 9), with
                                               .jsonl and .files/ as above

The JSONL is written by fde-kit's own `RunRecorder` (one object per line, `kind` first;
header first, result last) and gzipped on `close()`. `load_recording()` reads either file
back through fde-kit's `RunRecord.load`. Row shapes (fde-kit's fields first, then ours):

    header  kind, version, run_id ("t<ticket>-a<attempt>[-review]"), model, ticket_id,
            attempt_id, role, trace_id, base_commit, variant_id, tier, started_at, ...extra
    user    kind, turn, text, actor="user", by, ts                     (the session prompt)
    llm     kind, turn, actor="model", text, stop_reason, tool_calls[{id,name,input}],
            raw_content, usage{input_tokens,output_tokens}, seq, ts, model, request,
            latency_ms, cost_usd, prompt_version, output_file
    tool    kind, turn, tool_use_id, name, input, content, is_error, sources, approval,
            seq, ts, tool_kind, output_file
    result  kind, output, stopped_reason, turns, total_ms, sources, cited,
            fabricated_citations, outcome, ...extra (tests_passed, diff_lines, head_commit)

A chat recording's header has `run_id` "chat-<id>", `chat_session_id`, `role` "chat" and
no ticket or attempt id; `Recorder(dir, None, session_id, role="chat")` (or
`chat_recorder(dir, session_id)`) opens it and `get_recorder(session_id, "chat")` finds it.
The registry key includes the role, so a chat id never meets an attempt's.

`turn` is the number of `llm` rows so far (a tool row belongs to the model turn before it).

The model proxy and the OpenCode driver write to the same attempt concurrently, from
different threads or asyncio tasks: every method takes one lock, and `get_recorder(id)`
finds the open recorder for an attempt from anywhere in the process.

Retention (`prune`): recordings of merged tickets are kept; the rest are deleted once older
than 90 days. `foreman_inator.prune.run` calls it (the worker once a day, `fman prune` by
hand) and also keeps tickets that are still open. `prune` never looks inside `chat/` (not
an integer name); `prune_chats` applies the same rule there, keyed by session id (kept when
the session's PR was merged).
"""

from __future__ import annotations

import contextlib
import gzip
import logging
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Container, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fde_kit.record import RunRecord, RunRecorder, utc_now

log = logging.getLogger(__name__)

CONTENT_LIMIT = 4000
RETENTION_DAYS = 90
CHAT_ROLE = "chat"
CHAT_DIR = "chat"  # <recordings>/chat/<session_id>.jsonl.gz

_open: dict[tuple[int, str], Recorder] = {}
_open_lock = threading.Lock()


def _suffix(role: str) -> str:
    return "" if role == "attempt" else f".{role}"


def recording_path(
    recordings_dir: str | Path, ticket_id: int, attempt_id: int, role: str = "attempt"
) -> Path:
    """Where the finished (gzipped) recording of an attempt or review lives."""
    return Path(recordings_dir) / str(ticket_id) / f"{attempt_id}{_suffix(role)}.jsonl.gz"


def chat_recording_path(recordings_dir: str | Path, session_id: int) -> Path:
    """Where the finished (gzipped) recording of a chat session lives."""
    return Path(recordings_dir) / CHAT_DIR / f"{int(session_id)}.jsonl.gz"


def get_recorder(attempt_id: int, role: str = "attempt") -> Recorder | None:
    """The open recorder for that attempt (or its review), or None."""
    with _open_lock:
        return _open.get((attempt_id, role))


class Recorder:
    """Records one attempt (or review, or chat session). Opening registers it for
    `get_recorder`; `close` writes the result row, gzips the file and unregisters. Use as a
    context manager: an exception closes it with stopped_reason="error".

    ``role="chat"``: ``attempt_id`` is the chat session id and ``ticket_id`` must be None;
    the file is `chat_recording_path(recordings_dir, session_id)`."""

    def __init__(
        self,
        recordings_dir: str | Path,
        ticket_id: int | None,
        attempt_id: int,
        *,
        role: str = "attempt",
        trace_id: str | None = None,
        base_commit: str | None = None,
        model: str = "",
        variant_id: str | None = None,
        tier: int | None = None,
        **header_extra: Any,
    ):
        chat = role == CHAT_ROLE
        if chat and ticket_id is not None:
            raise ValueError("a chat recording has no ticket; pass ticket_id=None")
        self.ticket_id = ticket_id
        self.attempt_id = attempt_id
        self.role = role
        if chat:
            self.path = chat_recording_path(recordings_dir, attempt_id)
            self.files_dir = self.path.parent / f"{int(attempt_id)}.files"
        else:
            self.path = recording_path(recordings_dir, ticket_id, attempt_id, role)  # type: ignore[arg-type]
            self.files_dir = self.path.parent / f"{attempt_id}{_suffix(role)}.files"
        self.partial_path = self.path.with_suffix("")  # .jsonl
        self._lock = threading.RLock()
        self._seq = 0
        self._turns = 0
        self._t0 = time.monotonic()
        self._closed = False
        key = (attempt_id, role)
        with _open_lock:
            if key in _open:
                raise RuntimeError(f"a recorder for {role} {attempt_id} is already open")
            if chat:
                ids: dict[str, Any] = {
                    "run_id": f"chat-{int(attempt_id)}",
                    "ticket_id": None,
                    "attempt_id": None,
                    "chat_session_id": int(attempt_id),
                }
            else:
                ids = {
                    "run_id": f"t{ticket_id}-a{attempt_id}{_suffix(role).replace('.', '-')}",
                    "ticket_id": ticket_id,
                    "attempt_id": attempt_id,
                }
            header = {
                "kind": "header",
                "run_id": ids.pop("run_id"),
                "model": model,
                **ids,
                "role": role,
                "trace_id": trace_id,
                "base_commit": base_commit,
                "variant_id": variant_id,
                "tier": tier,
                "started_at": utc_now(),
                **header_extra,
            }
            self._rec = RunRecorder(self.partial_path, header)
            _open[key] = self

    # ------------------------------------------------------------------ rows

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def header(self) -> dict[str, Any]:
        return dict(self._rec.header)

    def _write(self, row: dict[str, Any]) -> dict[str, Any] | None:
        if self._closed:
            log.warning(
                "recorder %s %s is closed; dropping a late %s row",
                self.role,
                self.attempt_id,
                row["kind"],
            )
            return None
        self._seq += 1
        row["seq"] = self._seq
        row["ts"] = utc_now()
        self._rec.write(row)
        return row

    def _spill(self, text: str, kind: str) -> str | None:
        if len(text) <= CONTENT_LIMIT:
            return None
        self.files_dir.mkdir(parents=True, exist_ok=True)
        p = self.files_dir / f"{self._seq + 1:04d}-{kind}.txt"
        p.write_text(text, encoding="utf-8")
        return str(p)

    def user(self, text: str, by: str = "") -> dict[str, Any] | None:
        """The prompt the attempt started with (fde-kit `user` row)."""
        with self._lock:
            return self._write(
                {
                    "kind": "user",
                    "turn": self._turns + 1,
                    "text": text,
                    "actor": "user",
                    "by": by,
                }
            )

    def llm(
        self,
        *,
        text: str,
        tokens_in: int,
        tokens_out: int,
        model: str | None = None,
        stop_reason: str = "end_turn",
        tool_calls: Iterable[dict[str, Any]] = (),
        raw_content: list[dict[str, Any]] | None = None,
        request: Any = None,
        latency_ms: float | None = None,
        cost_usd: float | None = None,
        prompt_version: str | None = None,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> dict[str, Any] | None:
        """One model response, as the proxy saw it. `tool_calls` items: {id, name, input}.
        `cache_read_tokens` / `cache_write_tokens` (#52): input tokens read from / written
        to the provider's prompt cache (Anthropic route; 0 elsewhere).
        Returns the row written (its `output_file` is set when `text` was spilled to a
        file), or None if the recorder is already closed."""
        calls = [
            {"id": c.get("id", ""), "name": c["name"], "input": c.get("input", {})}
            for c in tool_calls
        ]
        if raw_content is None:
            raw_content = ([{"type": "text", "text": text}] if text else []) + [
                {"type": "tool_use", **c} for c in calls
            ]
        with self._lock:
            if self._closed:
                return self._write({"kind": "llm"})
            self._turns += 1
            return self._write(
                {
                    "kind": "llm",
                    "turn": self._turns,
                    "actor": "model",
                    "text": text,
                    "stop_reason": stop_reason,
                    "tool_calls": calls,
                    "raw_content": raw_content,
                    "usage": _usage(tokens_in, tokens_out, cache_read_tokens, cache_write_tokens),
                    "model": model,
                    "request": request,
                    "latency_ms": latency_ms,
                    "cost_usd": cost_usd,
                    "prompt_version": prompt_version,
                    "output_file": self._spill(text, "llm"),
                }
            )

    def tool(
        self,
        *,
        name: str,
        input: dict[str, Any],
        content: str,
        is_error: bool = False,
        tool_use_id: str = "",
        tool_kind: str | None = None,
    ) -> dict[str, Any] | None:
        """One tool result (file read, search, edit, shell command), full content."""
        with self._lock:
            if self._closed:
                return self._write({"kind": "tool"})
            return self._write(
                {
                    "kind": "tool",
                    "turn": max(self._turns, 1),
                    "tool_use_id": tool_use_id,
                    "name": name,
                    "input": input,
                    "content": content,
                    "is_error": is_error,
                    "sources": [],
                    "approval": None,
                    "tool_kind": tool_kind,
                    "output_file": self._spill(content, "tool"),
                }
            )

    def close(
        self,
        outcome: str = "",
        *,
        stopped_reason: str = "end_turn",
        output: str = "",
        **extra: Any,
    ) -> Path:
        """Write the result row, gzip to `self.path`, unregister. Idempotent."""
        with self._lock:
            if self._closed:
                return self.path
            self._rec.close(
                {
                    "kind": "result",
                    "output": output,
                    "stopped_reason": stopped_reason,
                    "turns": self._turns,
                    "total_ms": int((time.monotonic() - self._t0) * 1000),
                    "sources": [],
                    "cited": [],
                    "fabricated_citations": [],
                    "outcome": outcome,
                    **extra,
                }
            )
            self._closed = True
            try:
                with self.partial_path.open("rb") as src, gzip.open(self.path, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                self.partial_path.unlink()
            finally:
                with _open_lock:
                    _open.pop((self.attempt_id, self.role), None)
            return self.path

    def __enter__(self) -> Recorder:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._closed:
            return
        if exc_type is None:
            self.close()
        else:
            self.close(stopped_reason="error", error=exc_type.__name__, error_message=str(exc))


def chat_recorder(recordings_dir: str | Path, session_id: int, **kwargs: Any) -> Recorder:
    """Open the recording of chat session ``session_id`` (``Recorder`` keyword arguments:
    trace_id, base_commit, model, tier, header extras such as repo, ladder, sandbox)."""
    return Recorder(recordings_dir, None, int(session_id), role=CHAT_ROLE, **kwargs)


# --------------------------------------------------------------------------- reading


def load_recording(path: str | Path) -> RunRecord:
    """Load a recording (`.jsonl.gz` or `.jsonl`) with fde-kit's `RunRecord.load`."""
    path = Path(path)
    if path.suffix != ".gz":
        return RunRecord.load(path)
    with tempfile.TemporaryDirectory() as tmp:
        plain = Path(tmp) / path.with_suffix("").name
        with gzip.open(path, "rb") as src, plain.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        return RunRecord.load(plain)


def ticket_recordings(recordings_dir: str | Path, ticket_id: int) -> list[Path]:
    """Every recording file of a ticket, finished or partial, sorted by name."""
    d = Path(recordings_dir) / str(ticket_id)
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.name.endswith((".jsonl.gz", ".jsonl")))


# --------------------------------------------------------------------------- retention


def _is_merged(merged: Container[int] | Callable[[int], bool]) -> Callable[[int], bool]:
    if callable(merged):
        return merged
    return lambda ticket_id: ticket_id in merged


#: Files under `traces/<ticket_id>/` that follow the recordings' retention rule: span files
#: and the full check output saved next to them (verify, install and CI logs).
TRACE_SUFFIXES = (".jsonl", ".log")


def prune(
    recordings_dir: str | Path,
    *,
    merged: Container[int] | Callable[[int], bool],
    traces_dir: str | Path | None = None,
    max_age_days: float = RETENTION_DAYS,
    now: float | None = None,
    dry_run: bool = False,
) -> list[Path]:
    """Delete recordings (and their `.files/`) of tickets that are not merged once older
    than `max_age_days` by mtime; merged tickets' recordings are always kept.

    `merged` is a set of kept ticket ids or a `ticket_id -> bool` callable (the caller
    asks the store; `foreman_inator.prune` also keeps open tickets). With `traces_dir`,
    local span files and saved check logs (`<traces_dir>/<ticket_id>/*.jsonl`, `*.log`)
    follow the same rule. Returns the deleted paths; with `dry_run`, the paths that would
    be deleted, and nothing is touched. Directories not named by an integer ticket id are
    left alone.
    """
    is_merged = _is_merged(merged)
    cutoff = (now if now is not None else datetime.now(UTC).timestamp()) - max_age_days * 86400
    deleted: list[Path] = []
    roots = [(Path(recordings_dir), (".jsonl.gz", ".jsonl"))]
    if traces_dir is not None:
        roots.append((Path(traces_dir), TRACE_SUFFIXES))
    for root, suffixes in roots:
        if not root.is_dir():
            continue
        for tdir in sorted(root.iterdir()):
            if not tdir.is_dir() or not tdir.name.isdigit():
                continue
            if is_merged(int(tdir.name)):
                continue
            for f in sorted(tdir.iterdir()):
                if not f.is_file() or not f.name.endswith(suffixes):
                    continue
                if f.stat().st_mtime >= cutoff:
                    continue
                if not dry_run:
                    f.unlink()
                deleted.append(f)
                stem = f.name.removesuffix(".gz").removesuffix(".jsonl")
                side = tdir / f"{stem}.files"
                if side.is_dir():
                    if not dry_run:
                        shutil.rmtree(side)
                    deleted.append(side)
            if not dry_run:
                with contextlib.suppress(OSError):
                    os.rmdir(tdir)  # only if now empty
    return deleted


def prune_chats(
    recordings_dir: str | Path,
    *,
    merged: Container[int] | Callable[[int], bool],
    max_age_days: float = RETENTION_DAYS,
    now: float | None = None,
    dry_run: bool = False,
) -> list[Path]:
    """Delete chat recordings (`<recordings_dir>/chat/<id>.jsonl[.gz]` and their `.files/`)
    older than `max_age_days` by mtime, unless the session's PR was merged (`merged`: a set
    of session ids, or a `session_id -> bool` callable). The recording of a session whose
    recorder is still open in this process is never touched. Returns the deleted paths;
    with `dry_run`, the paths that would be deleted, and nothing is touched."""
    is_merged = _is_merged(merged)
    cutoff = (now if now is not None else datetime.now(UTC).timestamp()) - max_age_days * 86400
    root = Path(recordings_dir) / CHAT_DIR
    deleted: list[Path] = []
    if not root.is_dir():
        return deleted
    for f in sorted(root.iterdir()):
        if not f.is_file() or not f.name.endswith((".jsonl.gz", ".jsonl")):
            continue
        stem = f.name.removesuffix(".gz").removesuffix(".jsonl")
        if not stem.isdigit():
            continue
        sid = int(stem)
        if is_merged(sid) or get_recorder(sid, CHAT_ROLE) is not None:
            continue
        if f.stat().st_mtime >= cutoff:
            continue
        if not dry_run:
            f.unlink()
        deleted.append(f)
        side = root / f"{stem}.files"
        if side.is_dir() and side not in deleted:
            if not dry_run:
                shutil.rmtree(side)
            deleted.append(side)
    return deleted


def _usage(tokens_in: int, tokens_out: int, cache_read: int, cache_write: int) -> dict[str, int]:
    """An ``llm`` row's ``usage``; the #52 cache counts only when non-zero."""
    usage = {"input_tokens": tokens_in, "output_tokens": tokens_out}
    if cache_read:
        usage["cache_read_tokens"] = cache_read
    if cache_write:
        usage["cache_write_tokens"] = cache_write
    return usage
