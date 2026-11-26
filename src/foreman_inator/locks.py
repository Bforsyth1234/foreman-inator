"""File locks for parallel tickets (todo #19): two tickets never edit the same file at once.

Model
-----
Each ticket has a *wanted* set and a *held* set of paths in its repo:

* **wanted** is ``tickets.owned_files``: the files predicted from the issue (paths it
  mentions plus the router's guess), widened after every attempt by the attempt's actual
  diff. It only grows while the ticket is open. The writer prompt already shows it (#13).
* **held** is the ticket's rows in ``file_locks`` (primary key ``(repo, path)``).

A ticket may start or publish only when it holds everything it wants. A ticket that
wants a path another ticket holds is **waiting** (paused, not failed): it keeps what it
holds and resumes when the holder releases. Acquisition is all-or-nothing, inside one
``BEGIN IMMEDIATE`` store transaction, so two tickets can never both hold one path, even
across threads, store connections or processes.

Rules
-----
* **Normalisation** (``normalise``): ``\\`` → ``/``; leading ``./`` and ``/`` stripped;
  ``.`` and ``..`` collapsed; anything that escapes the repo, points into ``.git/``,
  starts with ``~``, has a drive letter or a URL scheme is rejected; a trailing
  ``:12``/``:12:3``/``#L12`` line reference is dropped. The repo root itself is never a
  lock.
* **Case**: locks compare case-insensitively (the lock row stores the casefolded path;
  ``owned_files`` keeps the case as written). macOS file systems are case-insensitive
  by default, so ``Src/A.py`` and ``src/a.py`` are one file there. On a case-sensitive
  repo two files differing only by case block each other: an extra wait, never a double
  edit.
* **Directories**: a path ending in ``/`` is a directory lock. It conflicts with every
  path under it, and with any directory lock that contains it or that it contains.
  ``src/auth/`` does not block ``src/authz.py``. A glob is reduced to the directory
  before its first wildcard component (``src/**/*.py`` → ``src/``); a glob with no fixed
  directory (``*.py``) is ignored: no whole-repo locks.
* **Prediction** (``predict_files``): backticked paths, ``dir/file.ext`` tokens,
  ``dir/`` tokens, bare filenames with a common extension and well-known extensionless
  names (``Makefile``...) from the issue title and body, plus the router's list. URLs,
  e-mail addresses and product names such as ``Node.js`` are skipped. A false positive
  only costs a wait.
* **No prediction**: an issue that names no files holds no locks until its first diff
  exists. Two such tickets can run at once and may both edit one file in their own
  worktrees; the first to widen after its attempt wins, the second is paused before it
  publishes (its diff is kept) until the first releases.
* **Widening** (``widen``): after an attempt, wanted becomes wanted ∪ diff files
  (``Worktree.changed_files()`` already lists a rename as both paths; ``renames`` pairs
  can be passed too). If any new path is held by another ticket nothing new is locked
  and the conflict is returned.
* **Who wins**: the earlier holder. A lock is never taken away from a ticket that holds
  it, except to break a deadlock.
* **Deadlocks**: a waiting ticket keeps its locks, so A waiting on B while B waits on A
  is possible. Every new wait edge is created inside ``acquire`` (a ticket that succeeds
  wants nothing more, so it has no outgoing edge), and ``acquire`` checks the wait-for
  graph before returning: if the caller is on a cycle, the youngest ticket on the cycle
  (highest id) yields: all its locks are released and it waits, holding nothing, for its
  whole wanted set. A ticket holding nothing cannot be waited on, so the cycle is gone,
  and the oldest ticket in any chain never yields, so the repo always makes progress.
* **Release**: ``release`` on merge, drop, or handover-and-close (``RELEASE_LOCKS``);
  safe to call twice. **Sweeping**: lock rows of tickets that are ``done`` or
  ``dropped`` are stale (a crash between the column change and the release); ``sweep``
  deletes them, and every ``acquire`` sweeps its repo first, so a leaked lock never
  blocks anyone.
* **Dormant tickets** (#55, ``dormant``): an open ticket flagged with a reason that stops
  all further work and that has no pull request (``DORMANT_FLAGS``) will not run again
  until the user acts. Its lock rows are treated as absent: ``waiting`` ignores them,
  ``acquire`` sweeps them (``_sweep_repo``), and it is neither a waiter nor a node in the
  wait-for graph. Its ``owned_files`` are kept, so after ``clear_flag`` its next
  ``may_start`` re-acquires them (or waits, if another ticket took them meanwhile). A
  flagged ticket with an open pull request is never dormant: it keeps its locks.
* **Chat holders** (#71 decision 6, wave C1e): a ``file_locks`` row has exactly one
  holder: a ticket (``ticket_id``) or a chat session (``chat_session_id``, migration
  012, with a CHECK that exactly one is set). A chat takes a lock on each file it edits
  (``chat_touch``), so a ticket that wants that file waits exactly as it would on another
  ticket (``Conflict.kind == "chat"``; ``LockResult.chats`` / ``Waiter.chats``). A chat
  never waits: ``chat_touch`` locks the free paths and returns the tickets holding the
  others, for the page's warning; the chat edits them anyway (the user decides). A chat
  is never a node of the wait-for graph (it waits on nothing), so it cannot be on a
  deadlock cycle and never yields. ``release_chat`` drops its rows on ``/pr``,
  ``/discard`` or session end; ``acquire`` and ``sweep`` also drop the rows of a session
  whose ``chat_sessions.state`` is final (``CHAT_FINAL_STATES``). ``LockResult.blockers``
  and ``Waiter.blockers`` stay ticket ids only, so callers that look tickets up by them
  are unchanged. Before migration 012 there is no chat column and every chat call is a
  no-op (``chat_touch`` returns no holders and locks nothing).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from foreman_inator.db.store import Store, Ticket

OPEN_COLUMNS = ("todo", "in_progress", "testing")

#: Who holds a lock row: a ticket or an interactive chat session (#71 decision 6).
HolderKind = Literal["ticket", "chat"]
#: chat_sessions.state values (migration 012) after which a session holds no locks.
CHAT_FINAL_STATES = ("opened_pr", "discarded", "ended", "failed")

# Flag reasons (the part before the first ":") that stop all further work on a ticket
# with no pull request (#55). Literal strings, not imports: the flag constants live in
# worker modules that import this one.
DORMANT_FLAGS = frozenset(
    {
        "setup_failed",
        "sandbox_unavailable",
        "checks_failed",
        "no_verify_command",
        "attempt_budget",
        "local_only",  # #49: a local-only repo's ladder ran out of local rungs
        "split_proposed",  # #50: waiting on the user to create the sub-issues
    }
)


def flag_kind(reason: str | None) -> str:
    """``"setup_failed"`` for ``"setup_failed: install: ..."``; ``""`` for no flag."""
    return (reason or "").split(":", 1)[0].strip()


def dormant(t: Ticket) -> bool:
    """An open ticket that will not run again until the user acts, and has no PR (#55)."""
    return (
        t.column in OPEN_COLUMNS
        and t.pr_number is None
        and flag_kind(t.flag_reason) in DORMANT_FLAGS
    )


# Extensions that make a bare token (no slash, no backticks) look like a file name.
FILE_EXTENSIONS = frozenset(
    [
        "py",
        "pyi",
        "pyx",
        "ipynb",
        "md",
        "rst",
        "txt",
        "toml",
        "yaml",
        "yml",
        "json",
        "jsonc",
        "ini",
        "cfg",
        "conf",
        "lock",
        "xml",
        "csv",
        "sql",
        "js",
        "jsx",
        "mjs",
        "cjs",
        "ts",
        "tsx",
        "vue",
        "svelte",
        "css",
        "scss",
        "sass",
        "less",
        "html",
        "htm",
        "svg",
        "sh",
        "bash",
        "zsh",
        "fish",
        "ps1",
        "bat",
        "swift",
        "m",
        "mm",
        "h",
        "hpp",
        "c",
        "cc",
        "cpp",
        "cxx",
        "go",
        "rs",
        "rb",
        "java",
        "kt",
        "kts",
        "scala",
        "cs",
        "fs",
        "php",
        "pl",
        "lua",
        "r",
        "dart",
        "ex",
        "exs",
        "erl",
        "hs",
        "clj",
        "gradle",
        "tf",
        "proto",
        "graphql",
        "gql",
    ]
)
EXTENSIONLESS_NAMES = frozenset(
    {
        "makefile",
        "dockerfile",
        "justfile",
        "procfile",
        "gemfile",
        "rakefile",
        "brewfile",
        "podfile",
        "vagrantfile",
        "containerfile",
    }
)
# Words that look like files but are products.
NOT_FILES = frozenset(
    {
        "node.js",
        "next.js",
        "nuxt.js",
        "vue.js",
        "react.js",
        "express.js",
        "three.js",
        "d3.js",
        "chart.js",
        "deno.js",
        "ember.js",
        "angular.js",
        "socket.io",
        "asp.net",
    }
)

_GLOB_CHARS = frozenset("*?[")
_LINE_REF = re.compile(r"(?::\d+(?::\d+)?|#L\d+(?:-L?\d+)?)$")
_URL = re.compile(r"[A-Za-z][\w+.-]*://\S+")
_EMAIL = re.compile(r"\S+@\S+\.\w+")
_BACKTICKS = re.compile(r"`([^`\n]+)`")
_TOKEN = re.compile(r"[\w./\\*\-]+")


# ------------------------------------------------------------------------------ paths


def normalise(path: str) -> str | None:
    """A repo-relative POSIX path (``dir/`` for a directory), or None if it is not one."""
    p = path.strip().replace("\\", "/")
    p = _LINE_REF.sub("", p)
    if not p or "\0" in p or "://" in p or p.startswith("~") or re.match(r"^[A-Za-z]:/", p):
        return None
    is_dir = p.endswith("/")
    parts: list[str] = []
    for part in p.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None  # escapes the repo
            parts.pop()
            continue
        parts.append(part)
    if not parts or parts[0].casefold() == ".git":
        return None
    return "/".join(parts) + ("/" if is_dir else "")


def lock_key(path: str) -> str:
    """The stored lock key: a normalised path, casefolded (see module docstring)."""
    return path.casefold()


def is_dir(key: str) -> bool:
    return key.endswith("/")


def overlaps(a: str, b: str) -> bool:
    """Do two lock keys cover a common file? Directories cover everything under them."""
    if a == b:
        return True
    if is_dir(a) and b.startswith(a):
        return True
    return is_dir(b) and a.startswith(b)


def covered(key: str, held: Iterable[str]) -> bool:
    """Is ``key`` already inside one of the ticket's own locks?"""
    return any(h == key or (is_dir(h) and key.startswith(h)) for h in held)


def _deglob(path: str) -> str | None:
    """Reduce a glob to the directory before its first wildcard component."""
    if not _GLOB_CHARS & set(path):
        return path
    fixed: list[str] = []
    for part in path.replace("\\", "/").split("/"):
        if _GLOB_CHARS & set(part):
            break
        fixed.append(part)
    stem = "/".join(fixed).strip("/")
    return f"{stem}/" if stem else None


def clean_paths(paths: Iterable[str]) -> list[str]:
    """Normalise, de-glob and de-duplicate (case-insensitively, first spelling wins)."""
    out: dict[str, str] = {}
    for raw in paths:
        g = _deglob(raw.strip())
        n = normalise(g) if g else None
        if n is not None:
            out.setdefault(lock_key(n), n)
    return list(out.values())


# ------------------------------------------------------------------------- prediction


def _looks_like_path(token: str, *, quoted: bool) -> bool:
    """Is a stripped token a file or directory mention? (``quoted``: it was backticked.)"""
    if not token or token.casefold() in NOT_FILES or re.fullmatch(r"[\d.]+", token):
        return False
    has_slash = "/" in token.rstrip("/")
    base = token.rstrip("/").rsplit("/", 1)[-1]
    if not base or base in (".", ".."):
        return False
    if token.endswith("/"):
        return True  # an explicit directory mention
    if _GLOB_CHARS & set(token):
        return has_slash  # reduced to its directory later
    if base.casefold() in EXTENSIONLESS_NAMES:
        return True
    if re.fullmatch(r"\.[A-Za-z][\w.-]*", base):
        return True  # a dotfile such as .gitignore
    if "." not in base:
        return False
    ext = base.rsplit(".", 1)[-1].casefold()
    if ext in FILE_EXTENSIONS:
        return True
    # an unknown extension counts only for a quoted path with a directory part
    return quoted and has_slash and ext.isalnum() and not ext.isdigit()


_LEAD_PUNCT = "\"'([{<"
_TRAIL_PUNCT = "\"')]}>,;:!?."


def _strip_edges(token: str) -> str:
    return token.strip().lstrip(_LEAD_PUNCT).rstrip(_TRAIL_PUNCT)


def extract_paths(text: str) -> list[str]:
    """Path-like mentions in free text, in order, unnormalised."""
    found: list[str] = []
    text = _URL.sub(" ", text or "")
    text = _EMAIL.sub(" ", text)
    for span in _BACKTICKS.findall(text):
        token = _strip_edges(span)
        if token and not any(c.isspace() for c in token) and _looks_like_path(token, quoted=True):
            found.append(token)
    rest = _BACKTICKS.sub(" ", text)
    for m in _TOKEN.finditer(rest):
        token = _strip_edges(m.group(0))
        if _looks_like_path(token, quoted=False):
            found.append(token)
    return found


def predict_files(title: str, body: str, extra: Iterable[str] = ()) -> tuple[str, ...]:
    """Owned files predicted from the issue plus the router's guess (``extra``)."""
    return tuple(clean_paths([*extract_paths(title), *extract_paths(body), *extra]))


# ---------------------------------------------------------------------------- results


@dataclass(frozen=True, slots=True)
class Conflict:
    """``path`` (wanted by the ticket) is blocked by lock ``held`` of ``holder``: a ticket
    id, or a chat session id when ``kind == "chat"``."""

    path: str
    held: str
    holder: int
    kind: HolderKind = "ticket"


def _ticket_ids(conflicts: Iterable[Conflict]) -> tuple[int, ...]:
    return tuple(sorted({c.holder for c in conflicts if c.kind == "ticket"}))


def _chat_ids(conflicts: Iterable[Conflict]) -> tuple[int, ...]:
    return tuple(sorted({c.holder for c in conflicts if c.kind == "chat"}))


@dataclass(frozen=True, slots=True)
class LockResult:
    """What an ``acquire``/``widen``/``may_start`` call left behind."""

    ticket_id: int
    held: tuple[str, ...]  # the ticket's lock keys after the call
    conflicts: tuple[Conflict, ...] = ()
    yielded: tuple[int, ...] = ()  # tickets made to release to break a deadlock
    closed: bool = False  # the ticket is done/dropped: nothing was locked

    @property
    def ok(self) -> bool:
        """The ticket holds everything it wants and may start or publish."""
        return not self.conflicts and not self.closed

    @property
    def blockers(self) -> tuple[int, ...]:
        """Tickets holding what this ticket wants (chat sessions are in ``chats``)."""
        return _ticket_ids(self.conflicts)

    @property
    def chats(self) -> tuple[int, ...]:
        """Chat sessions holding what this ticket wants."""
        return _chat_ids(self.conflicts)


def holders_text(blockers: Iterable[int], chats: Iterable[int] = ()) -> str:
    """For log lines: ``ticket(s) #3, #5``, ``chat session 2`` or both, joined by "and"."""
    parts = []
    bs, cs = list(blockers), list(chats)
    if bs:
        parts.append("ticket(s) " + ", ".join(f"#{b}" for b in bs))
    if cs:
        word = "chat session" if len(cs) == 1 else "chat sessions"
        parts.append(f"{word} " + ", ".join(str(c) for c in cs))
    return " and ".join(parts) or "nobody"


@dataclass(frozen=True, slots=True)
class Waiter:
    """An open ticket that wants paths other tickets (``blockers``) or chat sessions
    (``chats``) hold."""

    ticket_id: int
    blockers: tuple[int, ...]
    conflicts: tuple[Conflict, ...]
    chats: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class TicketHolder:
    """A ticket holding files a chat edited (``chat_touch``): the page's warning."""

    ticket_id: int
    issue_number: int
    paths: tuple[str, ...]  # the chat's paths (lock keys) this ticket's locks cover


@dataclass(frozen=True, slots=True)
class _Row:
    """One ``file_locks`` row, with its single holder."""

    path: str
    ticket_id: int | None
    chat_session_id: int | None


# ------------------------------------------------------------------------------ locks


class Locks:
    """Per-repo file locks over the store's ``file_locks`` table."""

    def __init__(self, store: Store) -> None:
        self.store = store

    # -- reads --

    def held(self, ticket: Ticket | int) -> tuple[str, ...]:
        tid = _tid(ticket)
        return tuple(lk.path for lk in self.store.list_locks(ticket_id=tid))

    def conflicts(self, ticket: Ticket | int, diff_files: Iterable[str]) -> tuple[Conflict, ...]:
        """Which of ``diff_files`` other tickets hold. Read-only: locks nothing."""
        t = self._ticket(ticket)
        return _conflicts(t.id, [lock_key(p) for p in clean_paths(diff_files)], self._rows(t.repo))

    def waiting(self, repo: str) -> list[Waiter]:
        """Open tickets in ``repo`` that are paused on someone else's locks. Read-only:
        a dormant ticket's rows count as released (the next ``acquire`` sweeps them)."""
        tickets = self.store.list_tickets(column=OPEN_COLUMNS, repo=repo)
        asleep = {t.id for t in tickets if dormant(t)}
        rows = [r for r in self._rows(repo) if r.ticket_id not in asleep]
        out = []
        for t in tickets:
            if t.id in asleep:
                continue
            cs = _conflicts(t.id, _missing(t, rows), rows)
            if cs:
                out.append(Waiter(t.id, _ticket_ids(cs), cs, _chat_ids(cs)))
        return out

    # -- writes --

    def acquire(self, ticket: Ticket | int, files: Iterable[str] = ()) -> LockResult:
        """Add ``files`` to the ticket's wanted set and lock all of it, or nothing new.

        On conflict the ticket keeps what it already held, unless it had to yield to break
        a deadlock (see the module docstring); the result says which tickets yielded.
        """
        tid = _tid(ticket)
        add = clean_paths(files)
        yielded: list[int] = []
        with self.store.transaction():
            t = self.store.get_ticket(tid)
            if t.column not in OPEN_COLUMNS:
                return LockResult(tid, self.held(tid), closed=True)
            self._sweep_repo(t.repo, keep=t.id)
            t = self._want(t, add)
            while True:
                rows = self._rows(t.repo)
                missing = _missing(t, rows)
                cs = _conflicts(t.id, missing, rows)
                if not cs:
                    if missing:
                        left = self.store.acquire_locks(t.id, t.repo, missing)
                        if left:  # cannot happen: the overlap check covers exact paths
                            cs = tuple(
                                Conflict(r.path, r.path, r.ticket_id)
                                for r in left
                                if r.ticket_id is not None
                            )
                            return LockResult(t.id, self.held(t.id), cs, tuple(yielded))
                    return LockResult(t.id, self.held(t.id), yielded=tuple(yielded))
                if t.id in yielded:
                    return LockResult(t.id, (), tuple(cs), tuple(yielded))
                cycle = self._cycle_through(t)
                if cycle is None:
                    return LockResult(t.id, self.held(t.id), tuple(cs), tuple(yielded))
                victim = max(cycle)  # the youngest ticket on the cycle yields
                self.store.release_locks(victim)
                yielded.append(victim)

    def may_start(self, ticket: Ticket | int) -> LockResult:
        """Can this ticket's next job start now? Locks its wanted set if so."""
        return self.acquire(ticket, ())

    def widen(
        self,
        ticket: Ticket | int,
        diff_files: Iterable[str],
        renames: Iterable[tuple[str, str]] = (),
    ) -> LockResult:
        """After an attempt: wanted ∪ diff files (both sides of a rename), atomically."""
        paths = [*diff_files]
        for old, new in renames:
            paths += [old, new]
        return self.acquire(ticket, paths)

    def release(self, ticket: Ticket | int) -> int:
        """Drop every lock the ticket holds (merge, drop, handover-and-close). Idempotent."""
        return self.store.release_locks(_tid(ticket))

    def sweep(self) -> int:
        """Delete lock rows of tickets that are done, dropped or dormant, and of chat
        sessions in a final state; return how many."""
        n = 0
        with self.store.transaction():
            for repo in {lk.repo for lk in self.store.list_locks()}:
                n += self._sweep_repo(repo)
        return n

    # -- chat holders (#71 decision 6) --

    def chat_touch(self, session_id: int, repo: str, paths: Iterable[str]) -> list[TicketHolder]:
        """A chat session edited ``paths``: lock the free ones for it, never wait.

        Returns the tickets holding the others (oldest ticket first), whose locks the chat
        does not take; the chat goes on editing (the user decides) and the page warns.
        A path another chat session holds is left to it (one holder per path).
        """
        keys = [lock_key(p) for p in clean_paths(paths)]
        if not keys:
            return []
        by_ticket: dict[int, list[str]] = {}
        with self.store.transaction():
            self._sweep_repo(repo)
            rows = self._rows(repo)
            mine = [r.path for r in rows if r.chat_session_id == session_id]
            free: list[str] = []
            for k in keys:
                if covered(k, mine):
                    continue
                others = [
                    r for r in rows if r.chat_session_id != session_id and overlaps(k, r.path)
                ]
                if not others:
                    free.append(k)
                    rows.append(_Row(k, None, session_id))
                    mine.append(k)
                    continue
                for tid in sorted({r.ticket_id for r in others if r.ticket_id is not None}):
                    by_ticket.setdefault(tid, []).append(k)
            self.store.insert_chat_locks(session_id, repo, free)
        return [
            TicketHolder(tid, self.store.get_ticket(tid).issue_number, tuple(ps))
            for tid, ps in sorted(by_ticket.items())
        ]

    def chat_held(self, session_id: int) -> tuple[str, ...]:
        """The lock keys a chat session holds, in path order."""
        held = self.store.chat_lock_rows(session_id)
        return tuple(lk.path for lk in sorted(held, key=lambda lk: (lk.repo, lk.path)))

    def release_chat(self, session_id: int) -> int:
        """Drop every lock the chat session holds (``/pr``, ``/discard``, session end).
        Idempotent; returns how many rows went."""
        return self.store.delete_chat_locks(session_id)

    # -- internals --

    def _ticket(self, ticket: Ticket | int) -> Ticket:
        return self.store.get_ticket(ticket) if isinstance(ticket, int) else ticket

    def _rows(self, repo: str) -> list[_Row]:
        return [
            _Row(lk.path, lk.ticket_id, lk.chat_session_id)
            for lk in sorted(self.store.list_locks(repo=repo), key=lambda lk: lk.path)
        ]

    def _want(self, t: Ticket, add: list[str]) -> Ticket:
        wanted = clean_paths([*t.owned_files, *add])
        if wanted != list(t.owned_files):
            t = self.store.update_ticket(t.id, owned_files=wanted)
        return t

    def _sweep_repo(self, repo: str, keep: int | None = None) -> int:
        """Release the rows of closed and dormant tickets in ``repo`` (never ``keep``'s),
        and of chat sessions in a final state."""
        n = 0
        live = {
            t.id
            for t in self.store.list_tickets(column=OPEN_COLUMNS, repo=repo)
            if not dormant(t) or t.id == keep
        }
        rows = self._rows(repo)
        held = {r.ticket_id for r in rows if r.ticket_id is not None}
        for tid in sorted(held - live):
            n += self.store.release_locks(tid)
        chats = sorted({r.chat_session_id for r in rows if r.chat_session_id is not None})
        for sid in chats:
            if self.store.get_chat_session(sid).state in CHAT_FINAL_STATES:
                n += self.release_chat(sid)
        return n

    def _cycle_through(self, start: Ticket) -> list[int] | None:
        """A wait-for cycle through ``start`` (ticket ids), or None."""
        rows = self._rows(start.repo)
        edges: dict[int, set[int]] = {}
        for t in self.store.list_tickets(column=OPEN_COLUMNS, repo=start.repo):
            if dormant(t) and t.id != start.id:
                continue
            # a chat never waits, so it is never on a cycle: ticket edges only
            edges[t.id] = set(_ticket_ids(_conflicts(t.id, _missing(t, rows), rows)))
        parent: dict[int, int] = {}
        frontier = [start.id]
        seen = {start.id}
        while frontier:
            nxt = []
            for node in frontier:
                for to in sorted(edges.get(node, ())):
                    if to == start.id:
                        cycle, cur = [start.id], node
                        while cur != start.id:
                            cycle.append(cur)
                            cur = parent[cur]
                        return cycle
                    if to not in seen:
                        seen.add(to)
                        parent[to] = node
                        nxt.append(to)
            frontier = nxt
        return None


def _tid(ticket: Ticket | int) -> int:
    return ticket if isinstance(ticket, int) else ticket.id


def _missing(t: Ticket, rows: list[_Row]) -> list[str]:
    mine = [r.path for r in rows if r.ticket_id == t.id]
    keys = dict.fromkeys(lock_key(p) for p in t.owned_files)
    return [k for k in keys if not covered(k, mine)]


def _conflicts(tid: int, keys: Iterable[str], rows: list[_Row]) -> tuple[Conflict, ...]:
    out = []
    for k in keys:
        for r in rows:
            if r.ticket_id == tid or not overlaps(k, r.path):
                continue
            if r.chat_session_id is not None:
                out.append(Conflict(k, r.path, r.chat_session_id, "chat"))
            elif r.ticket_id is not None:
                out.append(Conflict(k, r.path, r.ticket_id))
    return tuple(out)
