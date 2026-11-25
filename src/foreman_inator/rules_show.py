"""``fman rules show <repo>``: the rules block the writer and the reviewer will get (#42).

No model is called and nothing is written. For one commit of the repo (the worker's bare
mirror under ``paths.worktrees_dir()``, or a local git checkout given by path), it builds
the conventions the same way a real run does and prints, for each role:

* the prompt in use (the role's champion, or the packaged default, as the worker picks),
* each source file in display order with its character count, and ``CLIPPED at N chars``
  for every cut by the shared budget (``conventions.describe``),
* the conventions block exactly as the prompt builder renders it: the template's
  ``conventions`` section with ``conventions.render()`` substituted, as
  ``tickets.prompt.build_writer_prompt`` and ``review.reviewer.build_messages`` do.

Both roles read the repo files at the chosen commit with ``reviewer.base_conventions``
(the files ``gather`` could read are copied out of git, then ``gather`` runs over them):
the writer's worktree is a checkout of the base commit, and the reviewer reads the base
commit by design (#39). ``owned_files`` stands in for the writer's ticket files and the
reviewer's changed files, so nested ``AGENTS.md`` / ``CLAUDE.md`` can be previewed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fde_kit.prompts import PromptRef

from foreman_inator import prompts
from foreman_inator.review import reviewer as rv
from foreman_inator.runner.worktree import MIRROR_DIRNAME, WorktreeError, _git
from foreman_inator.tickets import conventions as conv_mod
from foreman_inator.tickets import prompt as prompt_mod

log = logging.getLogger(__name__)

#: Branches tried, in order, when no ref is given and the mirror has no ``origin/HEAD``.
DEFAULT_REFS = (
    "refs/remotes/origin/HEAD",
    "refs/remotes/origin/main",
    "refs/remotes/origin/master",
)


class RulesShowError(Exception):
    """The repo or ref could not be found; the message says what to do."""


@dataclass(frozen=True, slots=True)
class _GitDir:
    """What ``base_conventions`` needs of a ``RepoMirror``: a git dir to run git in."""

    mirror_path: Path


@dataclass(frozen=True, slots=True)
class RoleRules:
    """One role's preview: ``block`` is the exact prompt text ("" when none is shown)."""

    role: str  # writer | reviewer
    prompt_name: str
    conventions: conv_mod.Conventions | None  # None: the prompt has no conventions section
    block: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RulesShow:
    repo: str
    ref: str
    sha: str
    writer: RoleRules
    reviewer: RoleRules
    owned_files: tuple[str, ...] = field(default=())


# --------------------------------------------------------------------------- building


def writer_block(conv: conv_mod.Conventions, template: PromptRef) -> str:
    """The writer prompt's conventions block, as ``build_writer_prompt`` renders it."""
    s = prompt_mod._sections(template.text)
    if conv.empty or "conventions" not in s:
        return ""
    return s["conventions"].substitute(conventions_name=conv.names, conventions=conv.render())


def reviewer_block(conv: conv_mod.Conventions | None, template: PromptRef) -> str:
    """The reviewer's conventions block, as ``reviewer.build_messages`` renders it."""
    s = rv._sections(template.text)
    if "conventions" not in s:
        return ""
    if conv is None or conv.empty:
        return rv.NO_CONVENTIONS
    return s["conventions"].substitute(conventions_name=conv.names, conventions=conv.render())


def champion_prompts(store: Any | None) -> tuple[str, str]:
    """``(writer, reviewer)`` prompt names the worker would use now: each role's champion
    prompt when it has one whose file loads, else the packaged default (the same fallback
    as ``Worker.writer_template`` and ``champion_reviewer``)."""
    from foreman_inator.experiments import variants

    out = []
    for role, default in (
        ("writer", prompt_mod.WRITER_PROMPT_NAME),
        ("reviewer", rv.REVIEWER_PROMPT_NAME),
    ):
        name = default
        champ = variants.champion(store, role) if store is not None else None
        if champ is not None:
            try:
                prompts.load(champ.prompt_name)
                name = champ.prompt_name
            except (FileNotFoundError, ValueError):
                log.warning("%s champion's prompt %s is missing; using %s", role,
                            champ.prompt_name, default)  # fmt: skip
        out.append(name)
    return out[0], out[1]


def resolve_git_dir(repo: str, worktrees_root: Path | None = None) -> Path:
    """A local git checkout (``repo`` is a folder) or the worker's mirror of ``owner/name``."""
    local = Path(repo).expanduser()
    if local.is_dir() and ((local / ".git").exists() or (local / "HEAD").is_file()):
        return local
    if worktrees_root is None:
        from foreman_inator import paths

        worktrees_root = paths.worktrees_dir()
    mirror = Path(worktrees_root) / repo / MIRROR_DIRNAME
    if not (mirror / "HEAD").exists():
        raise RulesShowError(
            f"no mirror of {repo} under {worktrees_root}: the worker makes it on the repo's"
            " first ticket. Pass the path of a local checkout instead."
        )
    return mirror


def resolve_ref(git_dir: Path, ref: str | None) -> tuple[str, str]:
    """``(ref shown, commit sha)``. No ``ref``: the mirror's default branch, or ``HEAD``."""
    cands = [ref] if ref else [*DEFAULT_REFS, "HEAD"]
    for cand in cands:
        for spec in (f"refs/remotes/origin/{cand}", cand) if ref else (cand,):
            try:
                proc = _git(
                    ["rev-parse", "--verify", "-q", f"{spec}^{{commit}}"], cwd=git_dir, check=False
                )
            except WorktreeError:
                continue
            if proc.returncode == 0:
                return cand.removeprefix("refs/remotes/"), proc.stdout.strip()
    raise RulesShowError(f"unknown ref {ref or 'HEAD'!r} in {git_dir}; pass --ref BRANCH")


def build(
    repo: str,
    *,
    git_dir: Path,
    ref: str | None = None,
    owned_files: Sequence[str] = (),
    rules_file: str | Path | None = None,
    global_rules: str | Path | None = None,
    writer_prompt: str = prompt_mod.WRITER_PROMPT_NAME,
    reviewer_prompt: str = rv.REVIEWER_PROMPT_NAME,
) -> RulesShow:
    """Gather and render both roles' rules at ``ref`` of ``git_dir`` (see the module
    docstring). ``rules_file`` is the repo's configured file; ``global_rules`` overrides
    ``paths.rules_path()`` (tests)."""
    shown_ref, sha = resolve_ref(git_dir, ref)
    owned = tuple(owned_files)

    def gather() -> tuple[conv_mod.Conventions, tuple[str, ...]]:
        warns: list[str] = []
        c = rv.base_conventions(
            _GitDir(git_dir),
            sha,
            owned,
            rules_file=rules_file,
            global_rules=global_rules,
            warn=warns.append,
        )
        return c, tuple(warns)

    wt = prompts.load(writer_prompt)
    wconv, wwarn = gather()
    writer = RoleRules("writer", writer_prompt, wconv, writer_block(wconv, wt), wwarn)

    rt = prompts.load(reviewer_prompt)
    if rv.wants_conventions(rt):
        rconv, rwarn = gather()
    else:
        rconv, rwarn = None, ()
    reviewer = RoleRules("reviewer", reviewer_prompt, rconv, reviewer_block(rconv, rt), rwarn)
    return RulesShow(repo, shown_ref, sha, writer, reviewer, owned)


# --------------------------------------------------------------------------- output


def format_show(show: RulesShow) -> list[str]:
    """The lines ``fman rules show`` prints."""
    lines = [
        f"Rules for {show.repo} at {show.ref} ({show.sha[:12]}), no model called.",
        f"Shared budget: {conv_mod.CONVENTIONS_BUDGET} chars per role.",
    ]
    if show.owned_files:
        lines.append("Files standing in for the ticket's / change's files: "
                     + ", ".join(show.owned_files))  # fmt: skip
    for role in (show.writer, show.reviewer):
        lines += ["", *_role_lines(role)]
    return lines


def _role_lines(r: RoleRules) -> list[str]:
    where = "repo files at the base commit"
    out = [f"=== {r.role} (prompt {r.prompt_name}; {where}) ==="]
    if r.conventions is None:
        out.append(
            f"The {r.prompt_name} prompt has no conventions section: the {r.role} is shown no"
            " rules."
        )
        return out
    out.append("Sources:")
    described = conv_mod.describe(r.conventions)
    out += [f"  {d}" for d in described] or ["  (none found)"]
    out.append(f"Total shown: {sum(len(s.text) for s in r.conventions.sources)} chars")
    if not r.block:
        out.append(f"No conventions block: the {r.role} prompt leaves it out when no rules exist.")
        return out
    out += [f"--- {r.role} conventions block, exactly as rendered ---", r.block, "--- end ---"]
    return out


def run(
    repo: str,
    *,
    config: Any,
    store: Any | None,
    echo: Callable[[str], None],
    ref: str | None = None,
    owned_files: Sequence[str] = (),
    worktrees_root: Path | None = None,
    global_rules: str | Path | None = None,
) -> int:
    """The ``fman rules show`` command body. Returns the exit code (0 ok, 1 not found)."""
    try:
        git_dir = resolve_git_dir(repo, worktrees_root)
        writer_prompt, reviewer_prompt = champion_prompts(store)
        getter = getattr(config, "rules_file_for", None)
        show = build(
            repo,
            git_dir=git_dir,
            ref=ref,
            owned_files=owned_files,
            rules_file=getter(repo) if getter is not None else None,
            global_rules=global_rules,
            writer_prompt=writer_prompt,
            reviewer_prompt=reviewer_prompt,
        )
    except RulesShowError as exc:
        echo(f"error: {exc}")
        return 1
    echo("\n".join(format_show(show)))
    return 0
