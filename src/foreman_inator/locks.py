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
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from foreman_inator.db.store import FileLock, Store, Ticket

OPEN_COLUMNS = ("todo", "in_progress", "testing")

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
    """``path`` (wanted by the ticket) is blocked by lock ``held`` of ticket ``holder``."""

    path: str
    held: str
    holder: int


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
        return tuple(sorted({c.holder for c in self.conflicts}))


@dataclass(frozen=True, slots=True)
class Waiter:
    """An open ticket that wants paths other tickets hold."""

    ticket_id: int
    blockers: tuple[int, ...]
    conflicts: tuple[Conflict, ...]


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
        """Open tickets in ``repo`` that are paused on someone else's locks."""
        rows = self._rows(repo)
        out = []
        for t in self.store.list_tickets(column=OPEN_COLUMNS, repo=repo):
            cs = _conflicts(t.id, _missing(t, rows), rows)
            if cs:
                out.append(Waiter(t.id, tuple(sorted({c.holder for c in cs})), cs))
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
            self._sweep_repo(t.repo)
            t = self._want(t, add)
            while True:
                rows = self._rows(t.repo)
                missing = _missing(t, rows)
                cs = _conflicts(t.id, missing, rows)
                if not cs:
                    if missing:
                        left = self.store.acquire_locks(t.id, t.repo, missing)
                        if left:  # cannot happen: the overlap check covers exact paths
                            cs = tuple(Conflict(r.path, r.path, r.ticket_id) for r in left)
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
        """Delete lock rows of tickets that are done or dropped; return how many."""
        n = 0
        with self.store.transaction():
            for repo in {lk.repo for lk in self.store.list_locks()}:
                n += self._sweep_repo(repo)
        return n

    # -- internals --

    def _ticket(self, ticket: Ticket | int) -> Ticket:
        return self.store.get_ticket(ticket) if isinstance(ticket, int) else ticket

    def _rows(self, repo: str) -> list[FileLock]:
        return self.store.list_locks(repo=repo)

    def _want(self, t: Ticket, add: list[str]) -> Ticket:
        wanted = clean_paths([*t.owned_files, *add])
        if wanted != list(t.owned_files):
            t = self.store.update_ticket(t.id, owned_files=wanted)
        return t

    def _sweep_repo(self, repo: str) -> int:
        n = 0
        open_ids = {t.id for t in self.store.list_tickets(column=OPEN_COLUMNS, repo=repo)}
        for tid in sorted({lk.ticket_id for lk in self._rows(repo)} - open_ids):
            n += self.store.release_locks(tid)
        return n

    def _cycle_through(self, start: Ticket) -> list[int] | None:
        """A wait-for cycle through ``start`` (ticket ids), or None."""
        rows = self._rows(start.repo)
        edges: dict[int, set[int]] = {}
        for t in self.store.list_tickets(column=OPEN_COLUMNS, repo=start.repo):
            edges[t.id] = {c.holder for c in _conflicts(t.id, _missing(t, rows), rows)}
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


def _missing(t: Ticket, rows: list[FileLock]) -> list[str]:
    mine = [r.path for r in rows if r.ticket_id == t.id]
    keys = dict.fromkeys(lock_key(p) for p in t.owned_files)
    return [k for k in keys if not covered(k, mine)]


def _conflicts(tid: int, keys: Iterable[str], rows: list[FileLock]) -> tuple[Conflict, ...]:
    out = []
    for k in keys:
        for r in rows:
            if r.ticket_id != tid and overlaps(k, r.path):
                out.append(Conflict(k, r.path, r.ticket_id))
    return tuple(out)
