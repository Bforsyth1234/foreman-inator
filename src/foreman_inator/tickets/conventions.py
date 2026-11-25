"""The rules a model is told to follow in a repo: global, per-repo and repo files (#38, #40).

``gather`` collects every source, in the order they are shown:

1. ``global``: the user's rules for every repo, ``~/.foreman-inator/AGENTS.md``
   (``paths.rules_path()``, mode 600; a looser mode is tightened on read).
2. ``rules_file``: an optional per-repo rules file from the user's config.
3. ``repo``: the worktree's root ``AGENTS.md`` and ``CLAUDE.md``, both of them. One that
   is a symlink of, or identical to, an earlier source is skipped.
4. ``nested``: ``AGENTS.md`` / ``CLAUDE.md`` in folders on the path of the ticket's
   ``owned_files`` (``pkg/AGENTS.md`` for ``pkg/sub/x.py``), shallow folders first.

All sources share one budget (``CONVENTIONS_BUDGET``). When the text does not fit, the
global file is clipped first, then the per-repo rules file, then nested files (deepest
first), then root ``CLAUDE.md``, and ``AGENTS.md`` last: a repo's own file wins on a
conflict, so it is the last thing cut. Every cut is a ``Clip`` record and a warning.

Repo files are read only from inside the worktree: a path (or a symlink) that resolves
outside it is ignored, since ``owned_files`` comes from issue text.

``render`` turns the result into the text that goes inside the writer prompt's
``conventions`` fence; the reviewer (#39) and ``fman rules show`` (#42) use the same
function, so all three show exactly the same block. ``Conventions.sha256`` is the hash of
that block, for the attempt span and replay records.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

from foreman_inator.tickets.prompt import CONVENTIONS_BUDGET, clip_head, defuse

log = logging.getLogger(__name__)

Kind = Literal["global", "rules_file", "repo", "nested"]

FILE_NAMES = ("AGENTS.md", "CLAUDE.md")  # both are read; AGENTS.md is shown first
READ_CAP = 256_000  # characters read from any one file, whatever the budget
GLOBAL_DISPLAY = "~/.foreman-inator/AGENTS.md"
RULES_MODE = 0o600

# Lower number = cut first when the budget is short.
_CLIP_RANK: dict[Kind, int] = {"global": 0, "rules_file": 1, "nested": 2, "repo": 3}

_HEADERS: dict[Kind, str] = {
    "global": "Global rules ({path}), the user's rules for every repo. Where a repository"
    " file below disagrees, the repository file wins.",
    "rules_file": "Rules for this repo from the user's config ({path}). Where a repository"
    " file below disagrees, the repository file wins.",
    "repo": "Repository file {path}.",
    "nested": "Repository file {path}, for files under {folder}/.",
}


@dataclass(frozen=True, slots=True)
class Source:
    """One rules file as shown: ``text`` is what fits the budget (with a marker if cut)."""

    kind: Kind
    path: str  # display path: repo-relative for repo files, as configured otherwise
    label: str  # the header line render() puts above the text
    text: str
    chars: int  # characters in the file (up to READ_CAP)
    clipped: bool = False


@dataclass(frozen=True, slots=True)
class Clip:
    """A source the budget cut: ``shown`` of ``chars`` characters kept (0 = left out)."""

    kind: Kind
    path: str
    chars: int
    shown: int


@dataclass(frozen=True, slots=True)
class Conventions:
    """What ``gather`` found, in display order, plus a record of every cut."""

    sources: tuple[Source, ...] = ()
    clips: tuple[Clip, ...] = ()
    budget: int = CONVENTIONS_BUDGET
    skipped: tuple[str, ...] = field(default=())  # duplicate repo files left out

    @property
    def empty(self) -> bool:
        return not self.sources

    @property
    def names(self) -> str:
        """Short list for the prompt's heading, e.g. ``global rules, AGENTS.md``."""
        out = []
        for s in self.sources:
            out.append(
                {"global": "global rules", "rules_file": "repo rules file"}.get(s.kind, s.path)
            )
        return ", ".join(out)

    @property
    def sha256(self) -> str:
        """Hash of ``render(self)``: what the model actually saw."""
        return hashlib.sha256(render(self).encode("utf-8")).hexdigest()

    def render(self) -> str:
        return render(self)


def render(conv: Conventions) -> str:
    """The text inside the conventions fence: each source under its label, defused.

    Empty string when there is nothing to show. Shared by the writer prompt, the
    reviewer and ``fman rules show``."""
    parts = [f"[{s.label}]\n{defuse(s.text)}" for s in conv.sources]
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- gathering


@dataclass(slots=True)
class _Found:
    kind: Kind
    path: str
    folder: str
    text: str
    order: int


def gather(
    worktree: str | Path,
    owned_files: Sequence[str] = (),
    *,
    global_path: str | Path | None = None,
    rules_file: str | Path | None = None,
    budget: int = CONVENTIONS_BUDGET,
    warn: Callable[[str], None] | None = None,
) -> Conventions:
    """Collect the rules for one attempt in ``worktree`` (see the module docstring).

    ``global_path`` defaults to ``paths.rules_path()``; tests pass a ``tmp_path`` file.
    ``rules_file`` is the repo's configured rules file, or None. ``warn`` receives one
    message per clipped source (default: ``logging.warning``)."""
    warn = warn or log.warning
    root = Path(worktree).resolve()
    found: list[_Found] = []
    seen_paths: set[Path] = set()
    seen_text: set[str] = set()
    skipped: list[str] = []

    def add(kind: Kind, display: str, folder: str, path: Path, *, inside: bool) -> None:
        real = _resolve_file(path, root if inside else None)
        if real is None:
            return
        text = _read(real)
        if text is None:
            return
        key = text.replace("\r\n", "\n").strip()
        if real in seen_paths or (kind in ("repo", "nested") and key in seen_text):
            skipped.append(display)
            return
        seen_paths.add(real)
        if not key:
            return
        if kind in ("repo", "nested"):
            seen_text.add(key)
        found.append(_Found(kind, display, folder, key, len(found)))

    if global_path is None:
        from foreman_inator.paths import rules_path

        gp, shown_as = rules_path(), GLOBAL_DISPLAY
    else:
        gp = Path(global_path).expanduser()
        shown_as = _tilde(gp)
    _tighten_mode(gp)
    add("global", shown_as, "", gp, inside=False)
    if rules_file is not None:
        rp = Path(rules_file).expanduser()
        add("rules_file", _tilde(rp), "", rp, inside=False)
    for name in FILE_NAMES:
        add("repo", name, "", root / name, inside=True)
    for folder in _nested_folders(owned_files):
        for name in FILE_NAMES:
            rel = f"{folder}/{name}"
            add("nested", rel, folder, root / folder / name, inside=True)

    return _fit(found, budget, warn, tuple(skipped))


def _fit(
    found: list[_Found], budget: int, warn: Callable[[str], None], skipped: tuple[str, ...]
) -> Conventions:
    # Keep order: highest rank first; within a rank, earlier (shallower) first.
    keep_order = sorted(found, key=lambda f: (-_CLIP_RANK[f.kind], f.order))
    allot: dict[int, int] = {}
    left = budget
    for f in keep_order:
        allot[f.order] = min(len(f.text), left)
        left -= allot[f.order]
    sources: list[Source] = []
    clips: list[Clip] = []
    for f in found:
        shown = allot[f.order]
        if shown < len(f.text):
            clips.append(Clip(f.kind, f.path, len(f.text), shown))
            warn(
                f"rules file {f.path} clipped to {shown} of {len(f.text)} characters"
                f" (shared budget {budget})"
            )
        if shown == 0:
            continue
        sources.append(
            Source(
                kind=f.kind,
                path=f.path,
                label=_HEADERS[f.kind].format(path=f.path, folder=f.folder),
                text=clip_head(f.text, shown),
                chars=len(f.text),
                clipped=shown < len(f.text),
            )
        )
    return Conventions(tuple(sources), tuple(clips), budget, skipped)


def _nested_folders(owned_files: Sequence[str]) -> list[str]:
    """Folders on the path of each owned file, shallow first, never the root, never
    anything with ``..`` or an absolute path."""
    out: set[str] = set()
    for raw in owned_files:
        p = PurePosixPath(str(raw).strip().replace("\\", "/"))
        if p.is_absolute() or ".." in p.parts or not p.parts:
            continue
        parents = p.parts[:-1]
        for i in range(1, len(parents) + 1):
            out.add("/".join(parents[:i]))
    return sorted(out, key=lambda f: (f.count("/"), f))


def _resolve_file(path: Path, inside: Path | None) -> Path | None:
    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if inside is not None and not real.is_relative_to(inside):
        log.warning("ignoring rules file %s: it points outside the worktree", path)
        return None
    return real if real.is_file() else None


def _read(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(READ_CAP)
    except OSError:
        return None


def _tighten_mode(path: Path) -> None:
    """Invariant 7d: the global rules file is the user's alone. Tighten a looser mode."""
    try:
        st = path.stat()
        if stat.S_ISREG(st.st_mode) and stat.S_IMODE(st.st_mode) & 0o077:
            os.chmod(path, RULES_MODE)
            log.warning("tightened %s to mode 600", path)
    except OSError:
        pass


def _tilde(path: Path) -> str:
    """``~/...`` for a path under the home folder, so the prompt carries no user name."""
    try:
        return "~/" + path.absolute().relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)
