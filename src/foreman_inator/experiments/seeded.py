"""Seeded-bug reviewer cases (todo #35, GH #17): a merged change with one known bug planted.

SPEC "Experiments", Seeded bugs: "Take merged PRs, plant a bug ...; a good reviewer
should catch it". A seeded case starts from a reviewer replay case whose truth is
``merged`` (the user accepted that diff), mutates the diff, and expects the reviewer to
answer ``request_changes``. The run itself (an ``experiments`` row with mode ``seeded``,
champion vs challenger, catch rate per variant) is ``replay_runner.run_seeded``.

Mutations, all on lines the merged change *added* to a non-test source file (Python, or a
brace language by extension; ``review.checks.is_test_path`` files are left alone):

* ``drop_if_guard``: remove an added ``if`` line and its whole body (Python: the
  indented block under ``if ...:``; brace languages: ``if ... {`` to its closing ``}`` at
  the same indent). Only when every line of the block was added, no ``elif``/``else``
  follows, and the enclosing Python block is not left empty;
* ``neutralise_check``: an added single-line ``assert``/``raise`` becomes ``pass``
  (Python); an added single-line ``throw ...;`` is removed (brace languages);
* ``flip_comparison``: one spaced comparison operator on an added line, outside strings
  and comments, is inverted (``<`` and ``>=``, ``>`` and ``<=``, ``==`` and ``!=``,
  ``===`` and ``!==``).

Choice, deterministic: every candidate site is listed in diff order; a
``random.Random`` seeded with ``(seed, base commit, head commit)`` shuffles the kinds,
then the sites of each kind, and the first candidate whose patch passes ``git apply
--cached --check`` against the base tree (a throwaway index, nothing else touched) wins.
The same seed and the same merged commits always give the same mutation and, because the
seeded commit is written with a fixed identity and dates, the same seeded commit sha. A
case with no applicable candidate is skipped with the reason.

The seeded commit is ``git commit-tree`` of the patched base tree with the base as its
parent, written into the mirror's object store with no ref (``ChangeRef`` only needs the
commit object; the same sha is rebuilt on a resume). Nothing is pushed and no worktree,
branch or ref is created.

git docs checked 2026-11-25 (git 2.50.1): https://git-scm.com/docs/git-apply (``--cached``
applies to the index only, ``--check`` only tests applicability),
https://git-scm.com/docs/git-commit-tree, https://git-scm.com/docs/git-read-tree.
"""

from __future__ import annotations

import random
import re
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from foreman_inator.review.checks import is_test_path
from foreman_inator.runner.worktree import RepoMirror, WorktreeError, _git

from . import replay_sets as rs

MutationKind = Literal["drop_if_guard", "neutralise_check", "flip_comparison"]
KINDS: tuple[MutationKind, ...] = ("drop_if_guard", "neutralise_check", "flip_comparison")
#: The default seed for ``fman experiment run --mode seeded`` (any int works).
DEFAULT_SEED = 17
#: The verdict a seeded case expects, and the truth its reviewer side is scored against.
EXPECTED_VERDICT = "request_changes"
SEEDED_TRUTH = "kicked_back"
HEAD_VIA_PREFIX = "seeded:"  # ReviewerCase.head_via of a seeded case: seeded:<kind>

PY_EXT = frozenset({".py"})
BRACE_EXT = frozenset(
    {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".go", ".c", ".h", ".cc",
     ".cpp", ".hpp", ".cs", ".swift", ".kt", ".rs", ".php", ".scala", ".dart"}
)  # fmt: skip

#: Fixed identity and dates: the seeded commit's sha depends only on tree, parent, message.
_COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "foreman-inator seeded",
    "GIT_AUTHOR_EMAIL": "seeded@foreman-inator.invalid",
    "GIT_COMMITTER_NAME": "foreman-inator seeded",
    "GIT_COMMITTER_EMAIL": "seeded@foreman-inator.invalid",
    "GIT_AUTHOR_DATE": "946684800 +0000",
    "GIT_COMMITTER_DATE": "946684800 +0000",
}

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_PY_IF = re.compile(r"^(\s*)if\b.*:\s*(#.*)?$")
_BRACE_IF = re.compile(r"^(\s*)if\b.*\{\s*$")
_PY_CHECK = re.compile(r"^(\s*)(assert\b|raise\b)")
_BRACE_THROW = re.compile(r"^(\s*)throw\b.*;\s*$")
_FLIPS = {"===": "!==", "!==": "===", "==": "!=", "!=": "==", "<=": ">", ">=": "<",
          "<": ">=", ">": "<="}  # fmt: skip
_OP_RE = re.compile(r"(?<= )(===|!==|==|!=|<=|>=|<|>)(?= )")


# --------------------------------------------------------------------------- diff model


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    tail: str  # text after the second ``@@`` (function context)
    lines: list[str]  # each with its prefix: " ", "+", "-" or "\\"

    def header(self) -> str:
        return f"@@ -{self.old_start},{self.old_len} +{self.new_start},{self.new_len} @@{self.tail}"


@dataclass
class FilePatch:
    head: list[str]  # ``diff --git`` .. ``+++`` lines
    path: str | None  # the new path (None: deleted file or no ``+++``)
    hunks: list[Hunk]
    new_file: bool = False

    def text_lines(self) -> list[str]:
        out = list(self.head)
        for h in self.hunks:
            out.append(h.header())
            out.extend(h.lines)
        return out


def parse_diff(text: str) -> list[FilePatch]:
    """A ``git diff`` as file patches; anything without hunks (binary, mode-only) is kept
    verbatim in ``head``."""
    files: list[FilePatch] = []
    cur: FilePatch | None = None
    hunk: Hunk | None = None
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            cur = FilePatch([line], None, [])
            files.append(cur)
            hunk = None
            continue
        if cur is None:
            continue  # preamble before the first file (none in ``git diff`` output)
        m = _HUNK_RE.match(line) if line.startswith("@@") else None
        if m:
            hunk = Hunk(int(m[1]), int(m[2] or 1), int(m[3]), int(m[4] or 1), m[5], [])
            cur.hunks.append(hunk)
            continue
        if hunk is None:
            cur.head.append(line)
            if line.startswith("+++ "):
                p = line[4:]
                cur.path = p[2:] if p.startswith("b/") else None
            elif line.startswith("--- /dev/null"):
                cur.new_file = True
            continue
        if line[:1] in (" ", "+", "-", "\\"):
            hunk.lines.append(line)
        elif line == "" and hunk is not None:
            hunk.lines.append(line)  # the final newline's empty tail; trimmed below
        else:
            hunk = None
            cur.head.append(line)
    for f in files:  # the text's last "\n" gives one empty trailing element
        if f.hunks and f.hunks[-1].lines and f.hunks[-1].lines[-1] == "":
            f.hunks[-1].lines.pop()
        while f.head and f.head[-1] == "" and not f.hunks:
            f.head.pop()
    return files


def render_diff(files: Sequence[FilePatch]) -> str:
    return "\n".join(line for f in files for line in f.text_lines()) + "\n"


# --------------------------------------------------------------------------- candidates


@dataclass(frozen=True, slots=True)
class Mutation:
    kind: MutationKind
    file_index: int
    hunk_index: int
    line_index: int  # the first mutated line in the hunk
    path: str
    new_line: int  # line number in the merged file (for the description)
    end_index: int = 0  # drop_if_guard: one past the block's last line
    op: str = ""  # flip_comparison: the operator replaced
    op_col: int = 0  # flip_comparison: its column in the line's content

    def describe(self) -> str:
        what = {
            "drop_if_guard": "dropped the added if-guard",
            "neutralise_check": "neutralised the added check",
            "flip_comparison": f"flipped {self.op} to {_FLIPS.get(self.op, '?')}",
        }[self.kind]
        return f"{what} at {self.path}:{self.new_line}"


def _lang(path: str) -> str | None:
    ext = Path(path).suffix.lower()
    if ext in PY_EXT:
        return "py"
    if ext in BRACE_EXT:
        return "brace"
    return None


def _indent(content: str) -> int:
    return len(content) - len(content.lstrip(" \t"))


def _code_part(content: str, lang: str) -> str:
    """``content`` with string literals blanked and any trailing comment cut off."""
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(content):
        ch = content[i]
        if quote:
            if ch == "\\":
                out.append("xx"[: len(content[i : i + 2])])
                i += 2
                continue
            out.append(ch if ch == quote else "x")
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
            out.append(ch)
        elif (lang == "py" and ch == "#") or (lang == "brace" and content.startswith("//", i)):
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _balanced(code: str) -> bool:
    depth = 0
    for ch in code:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _new_numbers(h: Hunk) -> list[int]:
    """The new-file line number of each hunk line (0 for removed / marker lines)."""
    n = h.new_start
    out: list[int] = []
    for line in h.lines:
        if line[:1] in (" ", "+", ""):
            out.append(n)
            n += 1
        else:
            out.append(0)
    return out


def _py_guard_end(h: Hunk, i: int, base: int) -> int | None:
    """One past the guard block's last line, or None when it cannot be dropped cleanly."""
    lines = h.lines
    j = i + 1
    last = i
    while j < len(lines):
        p, c = lines[j][:1], lines[j][1:]
        if p == "-":
            return None  # a removal inside the block: the guard is not wholly new
        if p == "\\":
            return None
        if not c.strip():
            if p != "+":
                return None
            j += 1
            continue
        if _indent(c) <= base:
            break
        if p != "+":
            return None  # the guard wraps context lines
        last = j
        j += 1
    if last == i:
        return None  # no body seen in the hunk
    end = last + 1
    nxt = next((x for x in lines[end:] if x[:1] != "-" and x[1:].strip()), None)
    if nxt is not None and nxt[:1] == "\\":
        return None
    if nxt is not None and re.match(r"\s*(elif\b|else\b)", nxt[1:]):
        return None
    prev = next((x for x in reversed(lines[:i]) if x[:1] != "-" and x[1:].strip()), None)
    opens = prev is not None and prev[1:].rstrip().endswith(":") and _indent(prev[1:]) < base
    if opens and (nxt is None or _indent(nxt[1:]) < base):
        return None  # the enclosing block would be left empty
    return end


def _brace_guard_end(h: Hunk, i: int, base: int) -> int | None:
    lines = h.lines
    for j in range(i + 1, len(lines)):
        p, c = lines[j][:1], lines[j][1:]
        if p != "+":
            return None
        if c.strip().startswith("}") and _indent(c) == base:
            if c.strip() != "}":
                return None  # ``} else {`` and the like
            nxt = next((x for x in lines[j + 1 :] if x[:1] != "-" and x[1:].strip()), None)
            if nxt is not None and (nxt[:1] == "\\" or nxt[1:].strip().startswith("else")):
                return None
            return j + 1
    return None


def candidates(files: Sequence[FilePatch]) -> list[Mutation]:
    """Every mutation site in diff order (file, hunk, line, kind)."""
    out: list[Mutation] = []
    for fi, f in enumerate(files):
        if f.path is None or is_test_path(f.path):
            continue
        lang = _lang(f.path)
        if lang is None:
            continue
        for hi, h in enumerate(f.hunks):
            nums = _new_numbers(h)
            for li, line in enumerate(h.lines):
                if line[:1] != "+":
                    continue
                content = line[1:]
                code = _code_part(content, lang)
                stripped = code.strip()
                if not stripped or stripped.startswith(("*", "/*")):
                    continue
                base = dict(file_index=fi, hunk_index=hi, line_index=li, path=f.path,
                            new_line=nums[li])  # fmt: skip
                if lang == "py":
                    guard = bool(_PY_IF.match(content)) and _balanced(code)
                else:
                    guard = bool(_BRACE_IF.match(code.rstrip())) and _balanced(code.rstrip()[:-1])
                if guard:
                    ind = _indent(content)
                    end = (_py_guard_end if lang == "py" else _brace_guard_end)(h, li, ind)
                    if end is not None:
                        out.append(Mutation("drop_if_guard", end_index=end, **base))  # type: ignore[arg-type]
                if lang == "py":
                    single = (
                        _PY_CHECK.match(code)
                        and _balanced(code)
                        and not code.rstrip().endswith(("\\", ","))
                    )
                else:
                    single = _BRACE_THROW.match(code.rstrip()) and _balanced(code)
                if single:
                    out.append(Mutation("neutralise_check", **base))  # type: ignore[arg-type]
                for m in _OP_RE.finditer(code):
                    out.append(
                        Mutation("flip_comparison", op=m[1], op_col=m.start(), **base)  # type: ignore[arg-type]
                    )
    return out


def apply_mutation(files: Sequence[FilePatch], mut: Mutation) -> list[FilePatch]:
    """A copy of ``files`` with ``mut`` applied; hunk counts kept consistent."""
    out = [replace(f, head=list(f.head), hunks=[replace(h, lines=list(h.lines)) for h in f.hunks])
           for f in files]  # fmt: skip
    f = out[mut.file_index]
    h = f.hunks[mut.hunk_index]
    line = h.lines[mut.line_index]
    content = line[1:]
    if mut.kind == "flip_comparison":
        col = mut.op_col
        assert content[col : col + len(mut.op)] == mut.op
        h.lines[mut.line_index] = (
            "+" + content[:col] + _FLIPS[mut.op] + content[col + len(mut.op) :]
        )
        return out
    if mut.kind == "neutralise_check" and _lang(mut.path) == "py":
        h.lines[mut.line_index] = "+" + content[: _indent(content)] + "pass"
        return out
    end = mut.end_index if mut.kind == "drop_if_guard" else mut.line_index + 1
    dropped = end - mut.line_index
    del h.lines[mut.line_index : end]
    h.new_len -= dropped
    for later in f.hunks[mut.hunk_index + 1 :]:
        later.new_start -= dropped
    return out


# --------------------------------------------------------------------------- git


def _diff(mirror: RepoMirror, base: str, head: str) -> str | None:
    try:
        proc = _git(
            ["diff", "--no-color", "--no-ext-diff", "--no-renames", "--binary", "-U3", base, head],
            cwd=mirror.mirror_path,
            check=False,
        )
    except WorktreeError:
        return None
    return proc.stdout if proc.returncode == 0 else None


def _applied_commit(
    mirror: RepoMirror, base: str, patch: str, message: str, *, commit: bool
) -> tuple[bool, str | None]:
    """(applies to base, seeded commit sha when ``commit``), in a throwaway index."""
    with tempfile.TemporaryDirectory(prefix="fman-seeded-") as d:
        env = {"GIT_INDEX_FILE": str(Path(d) / "index")}
        pfile = Path(d) / "seeded.patch"
        pfile.write_text(patch, encoding="utf-8")
        cwd = mirror.mirror_path
        try:
            _git(["read-tree", base], cwd=cwd, env=env)
            check = _git(["apply", "--cached", "--check", str(pfile)], cwd=cwd, env=env,
                         check=False)  # fmt: skip
            if check.returncode != 0:
                return False, None
            if not commit:
                return True, None
            _git(["apply", "--cached", str(pfile)], cwd=cwd, env=env)
            tree = _git(["write-tree"], cwd=cwd, env=env).stdout.strip()
            sha = _git(["commit-tree", tree, "-p", base, "-m", message], cwd=cwd,
                       env=env | _COMMIT_ENV).stdout.strip()  # fmt: skip
        except WorktreeError:
            return False, None
        return True, sha


def applies(mirror: RepoMirror, base: str, patch: str) -> bool:
    """``git apply --cached --check`` of ``patch`` against ``base``'s tree."""
    return _applied_commit(mirror, base, patch, "", commit=False)[0]


# --------------------------------------------------------------------------- cases


@dataclass(frozen=True, slots=True)
class SeededCase:
    """One seeded case: the source replay case (``case_id`` names its ``replay_cases``
    row, which the run's results reference) and the reviewer case with the seeded head."""

    case_id: int
    source: rs.ReviewerCase
    case: rs.ReviewerCase  # head_commit = the seeded commit, truth = kicked_back
    mutation: Mutation
    description: str

    @property
    def kind(self) -> MutationKind:
        return self.mutation.kind

    def stored(self, stored: rs.StoredCase) -> rs.StoredCase:
        return replace(stored, case=self.case)


@dataclass(frozen=True, slots=True)
class SeededSkip:
    case_id: int
    reason: str


def _rng(seed: int, base: str, head: str) -> random.Random:
    return random.Random(f"fman-seeded:{int(seed)}:{base}:{head}")


def choose(
    files: Sequence[FilePatch],
    rng: random.Random,
    check: Callable[[str], bool],
    kinds: Iterable[MutationKind] = KINDS,
) -> tuple[Mutation, str] | None:
    """The first candidate (kinds shuffled, then each kind's sites) whose patch passes
    ``check``; returns it with the mutated patch."""
    wanted = list(dict.fromkeys(kinds))
    by_kind = {k: [m for m in candidates(files) if m.kind == k] for k in wanted}
    order = list(wanted)
    rng.shuffle(order)
    for k in order:
        sites = by_kind[k]
        rng.shuffle(sites)
        for mut in sites:
            patch = render_diff(apply_mutation(files, mut))
            if check(patch):
                return mut, patch
    return None


def seed_case(
    mirror: RepoMirror,
    stored: rs.StoredCase,
    *,
    seed: int = DEFAULT_SEED,
    kinds: Iterable[MutationKind] = KINDS,
) -> SeededCase | SeededSkip:
    """Plant one bug in a merged reviewer case (module docstring)."""
    rc = stored.case
    if not isinstance(rc, rs.ReviewerCase):
        return SeededSkip(stored.id, "not a reviewer case")
    if rc.truth != "merged":
        return SeededSkip(stored.id, "only merged (accepted) changes are seeded")
    diff = _diff(mirror, rc.base_commit, rc.head_commit)
    if diff is None:
        return SeededSkip(stored.id, "could not read the merged diff from the mirror")
    files = parse_diff(diff)
    picked = choose(files, _rng(seed, rc.base_commit, rc.head_commit),
                    lambda p: applies(mirror, rc.base_commit, p), kinds)  # fmt: skip
    if picked is None:
        return SeededSkip(stored.id, "no mutation site that still applies to the base")
    mut, patch = picked
    message = f"seeded: {mut.describe()} (seed {int(seed)})"
    ok, sha = _applied_commit(mirror, rc.base_commit, patch, message, commit=True)
    if not ok or not sha:
        return SeededSkip(stored.id, "the seeded patch could not be committed")
    case = replace(rc, head_commit=sha, head_via=f"{HEAD_VIA_PREFIX}{mut.kind}",
                   truth=SEEDED_TRUTH, tag=None)  # fmt: skip
    return SeededCase(stored.id, rc, case, mut, mut.describe())


def seeded_cases(
    store: object,
    set_id: int,
    mirror_for: rs.MirrorFor,
    *,
    seed: int = DEFAULT_SEED,
    kinds: Iterable[MutationKind] = KINDS,
) -> list[SeededCase | SeededSkip]:
    """A seeded case (or a skip) for every merged case of reviewer set ``set_id``."""
    out: list[SeededCase | SeededSkip] = []
    mirrors: dict[str, RepoMirror | None] = {}
    kinds = tuple(kinds)
    for stored in rs.list_cases(store, set_id):  # type: ignore[arg-type]
        rc = stored.case
        if not isinstance(rc, rs.ReviewerCase) or rc.truth != "merged":
            continue
        if rc.repo not in mirrors:
            try:
                mirrors[rc.repo] = mirror_for(rc.repo)
            except Exception:
                mirrors[rc.repo] = None
        mirror = mirrors[rc.repo]
        if mirror is None or not (mirror.mirror_path / "HEAD").exists():
            out.append(SeededSkip(stored.id, f"no mirror for {rc.repo} on this Mac"))
            continue
        out.append(seed_case(mirror, stored, seed=seed, kinds=kinds))
    return out


__all__ = [
    "DEFAULT_SEED",
    "EXPECTED_VERDICT",
    "KINDS",
    "FilePatch",
    "Hunk",
    "Mutation",
    "MutationKind",
    "SeededCase",
    "SeededSkip",
    "applies",
    "apply_mutation",
    "candidates",
    "choose",
    "parse_diff",
    "render_diff",
    "seed_case",
    "seeded_cases",
]
