"""Replay sets (todo #25): past merged tickets turned into cases a variant can be rerun on.

SPEC "Experiments": writer replay reruns merged issues from their base commit and scores
on passing the merged PR's tests; reviewer replay replays past PRs, the user's merges
being good and the user's kickbacks bad. "Rebuild the replay set from the most recent
~100 merges each month."

Building a set reads the store and the repo mirror only: no GitHub call, no fetch (the
worker keeps the mirror fresh), and nothing is written to the live tables. Optionally
``recordings_dir`` (local files, #5) is read for an attempt's head commit, because the
worker deletes an attempt's local branch once its ticket closes, while the commit object
stays in the mirror.

A **writer case** (``WriterCase``) is one merged ticket: the issue title and body as
stored, the repo, the accepted attempt's base commit, the test files the merged change
added or modified with their content at the merge, the repo's saved verify command (None:
detected in the replay worktree at run time), and the clarification Q&A (#44) the writer
saw. Where the merged change is found, in order:

1. the merge on ``origin/<base_branch>`` (first-parent history): a commit whose subject is
   GitHub's ``Merge pull request #<pr> ...`` (the PR side is its second parent; test
   content is the merge commit's) or ends in ``(#<pr>)`` (a squash or rebase merge: the
   commit's own change, content at that commit);
2. the accepted attempt's local branch ``refs/heads/<attempt.branch>``;
3. the accepted attempt's ``head_commit`` in its recording, if it is still in the mirror.

A **reviewer case** (``ReviewerCase``) is one attempt of a merged ticket that reached the
user: the accepted attempt (``truth="merged"``) or an attempt the user kicked back
(``truth="kicked_back"``, with the tag of the user's kickback), with its base and head
commits. The head is the attempt's local branch, else its recording's ``head_commit``,
else, for the accepted attempt, the PR side of the merge found as above.

Cases whose base commit, head or tests can no longer be found are stored as skipped with
the reason; nothing is guessed. Test files must be UTF-8 text within
``MAX_TEST_FILE_BYTES`` each and ``MAX_TESTS_TOTAL_BYTES`` together.

Storage: migration ``005_replay.sql`` (tables ``replay_sets`` and ``replay_cases``, case
JSON in ``replay_cases.data``), so a set survives a restart, can be listed, and a replay
run can be pinned to the exact cases it compared on. The store already is the one place
the worker's durable state lives; files under a directory would need their own atomic
writes, listing and cleanup.

Public API: ``build_replay_set``, ``ensure_current_set`` (the monthly rebuild),
``latest_set``, ``rebuild_due``, ``list_sets``, ``get_set``, ``list_cases``,
``get_case``.
"""

from __future__ import annotations

import gzip
import json
import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from foreman_inator.db.store import Attempt, Store, Ticket
from foreman_inator.review.checks import is_test_path
from foreman_inator.runner.worktree import RepoMirror, WorktreeError, _git
from foreman_inator.tickets.lifecycle import clarify_qa_cursor

Role = Literal["writer", "reviewer"]
ROLES: tuple[Role, ...] = ("writer", "reviewer")

#: SPEC: "the most recent ~100 merges".
DEFAULT_SET_SIZE = 100
MAX_TEST_FILE_BYTES = 200_000
MAX_TESTS_TOTAL_BYTES = 1_000_000
#: How far back the first-parent history of the base branch is searched for a merge.
MERGE_SEARCH_DEPTH = 5_000

MirrorFor = Callable[[str], RepoMirror | None]


# --------------------------------------------------------------------------- shapes


@dataclass(frozen=True, slots=True)
class MergedTest:
    """One test file of the merged change, as it was at the merge."""

    path: str
    content: str
    executable: bool = False


@dataclass(frozen=True, slots=True)
class WriterCase:
    ticket_id: int
    repo: str
    base_branch: str
    issue_number: int
    title: str
    request: str
    owned_files: tuple[str, ...]
    base_commit: str
    merged_commit: str  # where the test content was taken from
    merged_via: str  # "merge" | "squash" | "branch" | "recording"
    test_files: tuple[MergedTest, ...]
    verify_command: str | None  # the repo's saved override; None = detect at run time
    clarification: str | None
    accepted_attempt_id: int
    accepted_model: str
    accepted_tier: int


@dataclass(frozen=True, slots=True)
class ReviewerCase:
    ticket_id: int
    attempt_id: int
    repo: str
    issue_number: int
    title: str
    request: str
    base_commit: str
    head_commit: str
    head_via: str  # "branch" | "recording" | "merge" | "squash"
    writer_model: str
    truth: Literal["merged", "kicked_back"]
    tag: str | None  # the user's kickback tag (None for merged, or no kickback:<tag> label)
    verify_command: str | None


Case = WriterCase | ReviewerCase


@dataclass(frozen=True, slots=True)
class StoredCase:
    id: int
    set_id: int
    role: Role
    ticket_id: int
    attempt_id: int | None
    repo: str
    status: Literal["ok", "skipped"]
    skip_reason: str | None
    case: Case | None  # None when skipped


@dataclass(frozen=True, slots=True)
class ReplaySet:
    id: int
    role: Role
    built_at: datetime
    max_merges: int
    case_count: int
    skipped_count: int


@dataclass(frozen=True, slots=True)
class _Skip:
    ticket_id: int
    attempt_id: int | None
    repo: str
    reason: str


class _Skipped(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------- (de)serialize


def case_to_json(case: Case) -> str:
    return json.dumps(asdict(case), sort_keys=True, separators=(",", ":"))


def case_from_json(role: Role, data: str) -> Case:
    raw = json.loads(data)
    if role == "writer":
        raw["owned_files"] = tuple(raw["owned_files"])
        raw["test_files"] = tuple(MergedTest(**t) for t in raw["test_files"])
        return WriterCase(**raw)
    return ReviewerCase(**raw)


def _ts(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


# --------------------------------------------------------------------------- git (read only)


def _out(mirror: RepoMirror, args: Sequence[str]) -> str | None:
    """stdout of a read-only git command in the mirror, or None when it fails."""
    try:
        proc = _git(list(args), cwd=mirror.mirror_path, check=False)
    except WorktreeError:
        return None
    return proc.stdout if proc.returncode == 0 else None


def _has_commit(mirror: RepoMirror, sha: str | None) -> bool:
    if not sha or not re.fullmatch(r"[0-9a-f]{7,64}", sha):
        return False
    return _out(mirror, ["cat-file", "-e", f"{sha}^{{commit}}"]) is not None


def _changed(mirror: RepoMirror, a: str, b: str) -> list[str] | None:
    out = _out(mirror, ["diff", "--name-only", "-z", "--no-renames", a, b])
    if out is None:
        return None
    return sorted(p for p in out.split("\0") if p)


def _merges(mirror: RepoMirror, base_branch: str) -> Iterator[tuple[str, list[str], str]]:
    """(sha, parents, subject) along the base branch's first-parent history, newest first."""
    ref = f"refs/remotes/origin/{base_branch}"
    out = _out(
        mirror,
        ["log", "--first-parent", f"-n{MERGE_SEARCH_DEPTH}", "--format=%H%x1f%P%x1f%s", ref],
    )
    for line in (out or "").splitlines():
        sha, parents, subject = (line.split("\x1f") + ["", ""])[:3]
        yield sha, parents.split(), subject


def _find_merge(mirror: RepoMirror, ticket: Ticket) -> tuple[str, str, str, str] | None:
    """(diff_from, diff_to, content_at, how) for the ticket's PR on its base branch."""
    if ticket.pr_number is None:
        return None
    pr = int(ticket.pr_number)
    merge_re = re.compile(rf"^Merge pull request #{pr}\b")
    squash_re = re.compile(rf"\(#{pr}\)\s*$")
    for sha, parents, subject in _merges(mirror, ticket.base_branch):
        if len(parents) == 2 and merge_re.match(subject):
            return parents[0], parents[1], sha, "merge"
        if len(parents) == 1 and squash_re.search(subject):
            return parents[0], sha, sha, "squash"
    return None


def _recorded_head(recordings_dir: Path | None, ticket_id: int, attempt_id: int) -> str | None:
    """``head_commit`` from the attempt's recording (result row) or its review recording."""
    if recordings_dir is None:
        return None
    folder = Path(recordings_dir) / str(ticket_id)
    names = (
        f"{attempt_id}.jsonl.gz",
        f"{attempt_id}.jsonl",
        f"{attempt_id}.review.jsonl.gz",
        f"{attempt_id}.review.jsonl",
    )
    for name in names:
        path = folder / name
        if not path.is_file():
            continue
        try:
            opener = gzip.open if name.endswith(".gz") else open
            with opener(path, "rt", encoding="utf-8") as fh:  # type: ignore[operator]
                head = None
                for line in fh:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and isinstance(row.get("head_commit"), str):
                        head = row["head_commit"]
        except (OSError, EOFError, gzip.BadGzipFile):
            continue
        if head:
            return head
    return None


def _attempt_head(
    mirror: RepoMirror, attempt: Attempt, recordings_dir: Path | None
) -> tuple[str, str] | None:
    """(head sha, how) for one attempt from its local branch or its recording."""
    if attempt.branch:
        out = _out(
            mirror, ["rev-parse", "--verify", "-q", f"refs/heads/{attempt.branch}^{{commit}}"]
        )
        if out and out.strip():
            return out.strip(), "branch"
    rec = _recorded_head(recordings_dir, attempt.ticket_id, attempt.id)
    if rec and _has_commit(mirror, rec):
        return rec, "recording"
    return None


def _read_tests(mirror: RepoMirror, paths: Sequence[str], at: str) -> tuple[MergedTest, ...]:
    files: list[MergedTest] = []
    total = 0
    for path in paths:
        listing = _out(mirror, ["ls-tree", at, "--", path])
        if not listing or not listing.strip():
            continue  # deleted by the merged change: nothing to copy in
        mode = listing.split(None, 1)[0]
        if mode not in ("100644", "100755"):
            raise _Skipped(f"test file {path} is not a regular file at {at[:8]} (mode {mode})")
        try:
            proc = _git(["cat-file", "blob", f"{at}:{path}"], cwd=mirror.mirror_path, check=False)
        except WorktreeError as exc:
            raise _Skipped(f"test file {path} could not be read: {exc}") from None
        if proc.returncode != 0:
            raise _Skipped(f"test file {path} could not be read at {at[:8]}")
        text = proc.stdout  # _git decodes with errors="replace"
        if "�" in text:
            raise _Skipped(f"test file {path} is not UTF-8 text")
        size = len(text.encode("utf-8"))
        if size > MAX_TEST_FILE_BYTES:
            raise _Skipped(f"test file {path} is too large ({size} bytes)")
        total += size
        if total > MAX_TESTS_TOTAL_BYTES:
            raise _Skipped(f"the merged change's tests are too large together ({total}+ bytes)")
        files.append(MergedTest(path, text, executable=mode == "100755"))
    return tuple(files)


# --------------------------------------------------------------------------- case builders


def _merged_tickets(store: Store, max_merges: int) -> list[Ticket]:
    rows = store.query(
        "SELECT id FROM tickets WHERE \"column\" = 'done' AND accepted_attempt_id IS NOT NULL"
        " ORDER BY done_at DESC, id DESC LIMIT ?",
        (int(max_merges),),
    )
    return sorted((store.get_ticket(int(r["id"])) for r in rows), key=lambda t: t.id)


def _mirror(mirror_for: MirrorFor, repo: str) -> RepoMirror:
    try:
        mirror = mirror_for(repo)
    except Exception as exc:  # a broken clone URL, an unknown repo
        raise _Skipped(f"no mirror for {repo}: {type(exc).__name__}") from None
    if mirror is None or not (mirror.mirror_path / "HEAD").exists():
        raise _Skipped(f"no mirror for {repo} on this Mac")
    return mirror


def _verify_override(store: Store, repo: str) -> str | None:
    settings = store.get_repo_settings(repo)
    return settings.verify_command if settings else None


def writer_case(
    store: Store, ticket: Ticket, mirror: RepoMirror, *, recordings_dir: Path | None = None
) -> WriterCase:
    """Build one writer case or raise ``_Skipped`` with the reason."""
    assert ticket.accepted_attempt_id is not None
    att = store.get_attempt(ticket.accepted_attempt_id)
    base = att.base_commit
    if not base:
        raise _Skipped("the accepted attempt has no recorded base commit")
    if not _has_commit(mirror, base):
        raise _Skipped(f"base commit {base[:8]} is no longer in the mirror")
    found = _find_merge(mirror, ticket)
    if found is not None:
        _parent, pr_side, content_at, how = found
        diff_from, diff_to = base, pr_side
        if how == "squash":
            diff_from, diff_to = _parent, content_at
    else:
        head = _attempt_head(mirror, att, recordings_dir)
        if head is None:
            raise _Skipped(
                "the merged change is not in the mirror (no merge of PR "
                f"#{ticket.pr_number} on {ticket.base_branch}, no branch or recorded head "
                f"for attempt {att.id})"
            )
        diff_from, diff_to, content_at, how = base, head[0], head[0], head[1]
    changed = _changed(mirror, diff_from, diff_to)
    if changed is None:
        raise _Skipped(f"could not diff {diff_from[:8]}..{diff_to[:8]}")
    tests = _read_tests(mirror, [p for p in changed if is_test_path(p)], content_at)
    if not tests:
        raise _Skipped("the merged change added or modified no test files")
    return WriterCase(
        ticket_id=ticket.id,
        repo=ticket.repo,
        base_branch=ticket.base_branch,
        issue_number=ticket.issue_number,
        title=ticket.title,
        request=ticket.request,
        owned_files=tuple(ticket.owned_files),
        base_commit=base,
        merged_commit=content_at,
        merged_via=how,
        test_files=tests,
        verify_command=_verify_override(store, ticket.repo),
        clarification=store.get_cursor(ticket.repo, clarify_qa_cursor(ticket.id)),
        accepted_attempt_id=att.id,
        accepted_model=att.model,
        accepted_tier=att.tier,
    )


def _judged_attempts(store: Store, ticket: Ticket) -> list[tuple[Attempt, str, str | None]]:
    """(attempt, truth, tag) for the ticket's attempts the user judged."""
    out: list[tuple[Attempt, str, str | None]] = []
    for att in store.list_attempts(ticket.id):
        if att.id == ticket.accepted_attempt_id:
            out.append((att, "merged", None))
            continue
        user = [k for k in store.list_kickbacks(attempt_id=att.id) if k.by == "user"]
        if user and att.tests_passed:
            out.append((att, "kicked_back", user[-1].tag))
    return out


def reviewer_case(
    store: Store,
    ticket: Ticket,
    att: Attempt,
    truth: str,
    tag: str | None,
    mirror: RepoMirror,
    *,
    recordings_dir: Path | None = None,
) -> ReviewerCase:
    """Build one reviewer case or raise ``_Skipped`` with the reason."""
    base = att.base_commit
    if not base:
        raise _Skipped(f"attempt {att.id} has no recorded base commit")
    if not _has_commit(mirror, base):
        raise _Skipped(f"base commit {base[:8]} is no longer in the mirror")
    head = _attempt_head(mirror, att, recordings_dir)
    if head is None and truth == "merged":
        found = _find_merge(mirror, ticket)
        if found is not None:
            parent, pr_side, content_at, how = found
            if how == "squash":
                base, head = parent, (content_at, how)
            else:
                head = (pr_side, how)
    if head is None:
        raise _Skipped(f"the head commit of attempt {att.id} is no longer in the mirror")
    return ReviewerCase(
        ticket_id=ticket.id,
        attempt_id=att.id,
        repo=ticket.repo,
        issue_number=ticket.issue_number,
        title=ticket.title,
        request=ticket.request,
        base_commit=base,
        head_commit=head[0],
        head_via=head[1],
        writer_model=att.model,
        truth=truth,  # type: ignore[arg-type]
        tag=tag,
        verify_command=_verify_override(store, ticket.repo),
    )


def _collect(
    store: Store, role: Role, mirror_for: MirrorFor, max_merges: int, recordings_dir: Path | None
) -> list[Case | _Skip]:
    out: list[Case | _Skip] = []
    for ticket in _merged_tickets(store, max_merges):
        if role == "writer":
            try:
                mirror = _mirror(mirror_for, ticket.repo)
                out.append(writer_case(store, ticket, mirror, recordings_dir=recordings_dir))
            except _Skipped as s:
                out.append(_Skip(ticket.id, ticket.accepted_attempt_id, ticket.repo, s.reason))
            continue
        judged = _judged_attempts(store, ticket)
        try:
            mirror = _mirror(mirror_for, ticket.repo)
        except _Skipped as s:
            out += [_Skip(ticket.id, a.id, ticket.repo, s.reason) for a, _, _ in judged]
            continue
        for att, truth, tag in judged:
            try:
                out.append(
                    reviewer_case(
                        store, ticket, att, truth, tag, mirror, recordings_dir=recordings_dir
                    )
                )
            except _Skipped as s:
                out.append(_Skip(ticket.id, att.id, ticket.repo, s.reason))
    return out


# --------------------------------------------------------------------------- sets


def build_replay_set(
    store: Store,
    role: Role,
    *,
    mirror_for: MirrorFor,
    max_merges: int = DEFAULT_SET_SIZE,
    recordings_dir: Path | str | None = None,
) -> ReplaySet:
    """Build and store a new set from the latest ``max_merges`` merged tickets.

    ``mirror_for(repo)`` returns the repo's ``RepoMirror`` (or None); it is read, never
    fetched. Skipped cases are stored with their reason."""
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}, not {role!r}")
    if max_merges <= 0:
        raise ValueError("max_merges must be positive")
    rec = Path(recordings_dir) if recordings_dir is not None else None
    items = _collect(store, role, mirror_for, max_merges, rec)
    now = _ts(store.now())
    ok = sum(1 for i in items if not isinstance(i, _Skip))
    with store.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO replay_sets (role, built_at, max_merges, case_count, skipped_count)"
            " VALUES (?, ?, ?, ?, ?)",
            (role, now, int(max_merges), ok, len(items) - ok),
        )
        set_id = int(cur.lastrowid)  # type: ignore[arg-type]
        for item in items:
            if isinstance(item, _Skip):
                values: tuple[Any, ...] = (
                    item.ticket_id, item.attempt_id, item.repo, "skipped", item.reason, None,
                )  # fmt: skip
            else:
                attempt_id = (
                    item.accepted_attempt_id if isinstance(item, WriterCase) else item.attempt_id
                )
                values = (item.ticket_id, attempt_id, item.repo, "ok", None, case_to_json(item))
            conn.execute(
                "INSERT INTO replay_cases (set_id, role, ticket_id, attempt_id, repo, status,"
                " skip_reason, data, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (set_id, role, *values, now),
            )
    return get_set(store, set_id)


def _set_from(row: Any) -> ReplaySet:
    return ReplaySet(
        id=int(row["id"]),
        role=row["role"],
        built_at=datetime.fromisoformat(row["built_at"]),
        max_merges=int(row["max_merges"]),
        case_count=int(row["case_count"]),
        skipped_count=int(row["skipped_count"]),
    )


def get_set(store: Store, set_id: int) -> ReplaySet:
    rows = store.query("SELECT * FROM replay_sets WHERE id = ?", (int(set_id),))
    if not rows:
        raise LookupError(f"no replay set {set_id}")
    return _set_from(rows[0])


def list_sets(store: Store, role: Role | None = None) -> list[ReplaySet]:
    if role is None:
        rows = store.query("SELECT * FROM replay_sets ORDER BY id")
    else:
        rows = store.query("SELECT * FROM replay_sets WHERE role = ? ORDER BY id", (role,))
    return [_set_from(r) for r in rows]


def latest_set(store: Store, role: Role) -> ReplaySet | None:
    rows = store.query("SELECT * FROM replay_sets WHERE role = ? ORDER BY id DESC LIMIT 1", (role,))
    return _set_from(rows[0]) if rows else None


def rebuild_due(rset: ReplaySet | None, now: datetime) -> bool:
    """SPEC's monthly rebuild: due when there is no set, or the latest was built in an
    earlier calendar month (the Mac's local time, like the spend windows)."""
    if rset is None:
        return True
    built = rset.built_at.astimezone()
    here = now.astimezone() if now.tzinfo else now.replace(tzinfo=UTC).astimezone()
    return (built.year, built.month) != (here.year, here.month)


def ensure_current_set(
    store: Store,
    role: Role,
    *,
    mirror_for: MirrorFor,
    max_merges: int = DEFAULT_SET_SIZE,
    recordings_dir: Path | str | None = None,
    now: datetime | None = None,
) -> ReplaySet:
    """The role's latest set, rebuilt first when ``rebuild_due`` (or when it was built
    with a different ``max_merges``)."""
    current = latest_set(store, role)
    if (
        rebuild_due(current, now or store.now())
        or current is None
        or current.max_merges != max_merges
    ):
        return build_replay_set(
            store, role, mirror_for=mirror_for, max_merges=max_merges, recordings_dir=recordings_dir
        )
    return current


def _case_from(row: Any) -> StoredCase:
    role = row["role"]
    data = row["data"]
    return StoredCase(
        id=int(row["id"]),
        set_id=int(row["set_id"]),
        role=role,
        ticket_id=int(row["ticket_id"]),
        attempt_id=None if row["attempt_id"] is None else int(row["attempt_id"]),
        repo=row["repo"],
        status=row["status"],
        skip_reason=row["skip_reason"],
        case=case_from_json(role, data) if data is not None else None,
    )


def list_cases(store: Store, set_id: int, *, include_skipped: bool = False) -> list[StoredCase]:
    sql = "SELECT * FROM replay_cases WHERE set_id = ?"
    if not include_skipped:
        sql += " AND status = 'ok'"
    return [_case_from(r) for r in store.query(sql + " ORDER BY id", (int(set_id),))]


def get_case(store: Store, case_id: int) -> StoredCase:
    rows = store.query("SELECT * FROM replay_cases WHERE id = ?", (int(case_id),))
    if not rows:
        raise LookupError(f"no replay case {case_id}")
    return _case_from(rows[0])


__all__ = [
    "DEFAULT_SET_SIZE",
    "MAX_TESTS_TOTAL_BYTES",
    "MAX_TEST_FILE_BYTES",
    "ROLES",
    "Case",
    "MirrorFor",
    "ReplaySet",
    "ReviewerCase",
    "StoredCase",
    "MergedTest",
    "WriterCase",
    "build_replay_set",
    "case_from_json",
    "case_to_json",
    "ensure_current_set",
    "get_case",
    "get_set",
    "latest_set",
    "list_cases",
    "list_sets",
    "rebuild_due",
    "reviewer_case",
    "writer_case",
]
