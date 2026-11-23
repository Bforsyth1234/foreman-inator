"""Where foreman-inator's prompt files live, and how to load one (#24).

Every prompt is a Markdown file shipped in this package (``foreman_inator/prompts/<name>.md``).
A prompt version is its fde-kit ``PromptRef``: the name (the file stem) and the sha256 of the
file's text as ``fde_kit.prompts.load_prompt`` reads it (stripped), so one file revision is one
hash, the same hash the writer, router, reviewer and clarify loaders already record.

Lookup is always package data (or an explicit ``prompts_dir``, for tests and replays); it never
falls back to a ``./prompts`` folder in the current directory, so a stray ``prompts/`` in the
worker's cwd (fde-kit's own default) cannot shadow a packaged prompt.

Import-light on purpose: stdlib and fde-kit only.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

from fde_kit.prompts import PromptRef, load_prompt

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = ["PROMPT_SUFFIX", "PromptRef", "list_prompts", "load", "prompt_path", "version"]

PROMPT_SUFFIX = ".md"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _check_name(name: str) -> None:
    if not isinstance(name, str) or not _NAME.match(name) or name.endswith(PROMPT_SUFFIX):
        raise ValueError(f"bad prompt name {name!r}: use the file stem, e.g. 'writer-v1'")


def _folder(prompts_dir: str | Path | None):  # Traversable or Path
    if prompts_dir is not None:
        return Path(prompts_dir)
    return resources.files(__name__)


@contextmanager
def prompt_path(name: str, prompts_dir: str | Path | None = None) -> Iterator[Path]:
    """A real filesystem path to prompt ``name`` for the duration of the ``with`` block.

    Package data may sit inside a zip, so this is a context manager (``importlib.resources
    .as_file``). Raises ``FileNotFoundError`` if there is no such prompt.
    """
    _check_name(name)
    entry = _folder(prompts_dir) / f"{name}{PROMPT_SUFFIX}"
    if not entry.is_file():
        where = prompts_dir if prompts_dir is not None else "the foreman_inator package"
        raise FileNotFoundError(f"no prompt {name!r} in {where}")
    with resources.as_file(entry) as path:
        yield Path(path)


def load(name: str, prompts_dir: str | Path | None = None) -> PromptRef:
    """Load prompt ``name`` -> ``PromptRef(name, sha256, text)`` via fde-kit's ``load_prompt``.

    ``prompts_dir`` replaces the packaged folder (a temp copy in tests); it is never merged
    with it and never defaults to the current directory.
    """
    with prompt_path(name, prompts_dir) as path:
        # A path with a suffix that is a file resolves to exactly that file in fde-kit.
        ref = load_prompt(str(path))
    return PromptRef(name=name, sha256=ref.sha256, text=ref.text)


def version(name: str, prompts_dir: str | Path | None = None) -> str:
    """``<name>@<hash[:8]>``, the short prompt version recorded on spans and route reasons."""
    ref = load(name, prompts_dir)
    return f"{ref.name}@{ref.sha256[:8]}"


def list_prompts(prompts_dir: str | Path | None = None) -> tuple[str, ...]:
    """Names of every prompt file (sorted), e.g. ``('clarify-v1', 'reviewer-v1', ...)``."""
    folder = _folder(prompts_dir)
    if not folder.is_dir():
        return ()
    names = (
        entry.name[: -len(PROMPT_SUFFIX)]
        for entry in folder.iterdir()
        if entry.is_file() and entry.name.endswith(PROMPT_SUFFIX)
    )
    return tuple(sorted(n for n in names if _NAME.match(n)))
