"""Variants and champions (#24): which model + prompt version made an attempt or a review.

A variant is a model (its config name, e.g. ``inkling-2-mini``) plus a prompt version, with id
``<model>/<prompt-name>@<sha256[:8]>`` (the hash is fde-kit's ``prompt_hash`` of the prompt file,
exactly what ``WriterPrompt.variant_id`` and the reviewer already write on attempts and reviews).
Editing a prompt file changes its hash, so it is a new variant; the old variant row stays in the
store with its role, model, prompt name and full hash.

Roles are the store's ``ROLES``: ``writer`` (prompt ``writer-v1``) and ``reviewer``
(``reviewer-v1``). One champion per role, the latest ``champion_history`` row.

The router (``router-v1``), clarify (``clarify-v1``) and failure-label (``failure-label-v1``)
prompts are versioned too, but they are not store roles: their version is the short
``<name>@<hash[:8]>`` recorded on the ``llm.call`` span (and in ``route_reason`` for the
router). They have no variant rows and no champion; ``prompt_versions()`` lists them for status.

What runs when a prompt file changes: prompt files are code in this repo, so **the file on disk
is what runs**. The champion row records what was promoted. The worker loads the champion's
prompt *name* from disk; if that file's hash no longer matches the champion's, the attempt runs
as a new, non-champion variant (registered on use) and ``role_status`` / ``status_lines`` say so,
for ``fman status``, until someone promotes it or reverts the file.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from fde_kit.prompts import PromptRef

from foreman_inator import prompts
from foreman_inator.db.store import ROLES, ChampionChange, NotFoundError, Store, Variant

if TYPE_CHECKING:
    from foreman_inator.config import Config

__all__ = [
    "ROLES",
    "EnsureResult",
    "ParsedVariant",
    "RoleStatus",
    "VariantError",
    "champion",
    "champion_history",
    "default_prompt_name",
    "ensure_defaults",
    "lookup",
    "parse_variant_id",
    "prompt_versions",
    "register",
    "role_status",
    "set_champion",
    "status_lines",
    "variant_id",
]

HASH_LEN = 8


class VariantError(ValueError):
    """A malformed variant id, an unknown role, or a bad model name."""


@dataclass(frozen=True, slots=True)
class ParsedVariant:
    model: str
    prompt_name: str
    hash8: str


def _check_role(role: str) -> None:
    if role not in ROLES:
        raise VariantError(f"unknown role {role!r}; roles are {ROLES}")


def _check_model(model: str) -> None:
    if not isinstance(model, str) or not model.strip() or "@" in model or model != model.strip():
        raise VariantError(f"bad model name {model!r}")


def variant_id(model: str, prompt: PromptRef) -> str:
    """``<model>/<prompt-name>@<prompt hash[:8]>``."""
    _check_model(model)
    return f"{model}/{prompt.name}@{prompt.sha256[:HASH_LEN]}"


def parse_variant_id(vid: str) -> ParsedVariant:
    """Split a variant id. The model may contain ``/``; the prompt name may not."""
    head, at, hash8 = vid.rpartition("@")
    model, slash, name = head.rpartition("/")
    if not (at and slash and model and name and len(hash8) == HASH_LEN):
        raise VariantError(f"bad variant id {vid!r}: want <model>/<prompt-name>@<hash8>")
    if any(c not in "0123456789abcdef" for c in hash8):
        raise VariantError(f"bad variant id {vid!r}: hash is not lowercase hex")
    return ParsedVariant(model=model, prompt_name=name, hash8=hash8)


def default_prompt_name(role: str) -> str:
    """The prompt each role uses on a fresh install (the loaders' own constants)."""
    _check_role(role)
    if role == "writer":
        from foreman_inator.tickets.prompt import WRITER_PROMPT_NAME

        return WRITER_PROMPT_NAME
    from foreman_inator.review.reviewer import REVIEWER_PROMPT_NAME

    return REVIEWER_PROMPT_NAME


def register(store: Store, role: str, model: str, prompt: PromptRef) -> Variant:
    """Insert the variant for ``model`` + ``prompt`` if new; idempotent."""
    _check_role(role)
    vid = variant_id(model, prompt)
    return store.upsert_variant(
        vid, role=role, model=model, prompt_name=prompt.name, prompt_hash=prompt.sha256
    )


def champion(store: Store, role: str) -> Variant | None:
    """The current champion of ``role``, or None if none was ever set."""
    _check_role(role)
    vid = store.champion(role)
    return store.get_variant(vid) if vid else None


def set_champion(
    store: Store, role: str, variant_id: str, *, note: str | None = None
) -> ChampionChange:
    """Make a registered variant the champion of ``role``; appends a history row."""
    _check_role(role)
    parse_variant_id(variant_id)
    return store.set_champion(role, variant_id, note=note)


def champion_history(store: Store, role: str) -> list[ChampionChange]:
    _check_role(role)
    return store.champion_history(role)


# --------------------------------------------------------------------------- defaults


@dataclass(frozen=True, slots=True)
class EnsureResult:
    registered: tuple[Variant, ...]  # every variant ensure_defaults upserted (new or not)
    champions_set: tuple[ChampionChange, ...]  # only roles that had no champion


def _writer_models(config: Config) -> list[str]:
    # Every configured model can write (it is a rung on the tier ladder); lowest tier first.
    return [m.name for m in sorted(config.models, key=lambda m: m.tier)]


def _reviewer_models(config: Config) -> list[str]:
    names = {m.name for m in config.models}
    return [n for n in config.reviewer_models if n in names]


def ensure_defaults(
    store: Store, config: Config, *, prompts_dir: str | Path | None = None
) -> EnsureResult:
    """Register the current writer and reviewer variants for the configured models, and make
    one per role champion only where that role has none.

    Writer champion on a fresh install: the first model of the lowest tier with the current
    writer prompt. Reviewer champion: the first ``reviewer_models`` entry with the current
    reviewer prompt. An existing champion is never replaced, so an edited prompt file shows up
    as new, non-champion variants until promoted.
    """
    plan: dict[str, list[str]] = {
        "writer": _writer_models(config),
        "reviewer": _reviewer_models(config),
    }
    registered: list[Variant] = []
    changes: list[ChampionChange] = []
    for role, models in plan.items():
        prompt = prompts.load(_champion_prompt_name(store, role), prompts_dir)
        role_variants = [register(store, role, m, prompt) for m in models]
        registered.extend(role_variants)
        if role_variants and store.champion(role) is None:
            changes.append(
                store.set_champion(role, role_variants[0].id, note="default (ensure_defaults)")
            )
    return EnsureResult(registered=tuple(registered), champions_set=tuple(changes))


def _champion_prompt_name(store: Store, role: str) -> str:
    current = champion(store, role)
    return current.prompt_name if current else default_prompt_name(role)


# --------------------------------------------------------------------------- status


@dataclass(frozen=True, slots=True)
class RoleStatus:
    role: str
    champion: Variant | None
    on_disk_hash: str | None  # sha256 of the champion's prompt file now; None if missing
    running_variant_id: str | None  # what the worker runs now (champion model, file on disk)

    @property
    def matches(self) -> bool:
        return self.champion is not None and self.on_disk_hash == self.champion.prompt_hash

    def line(self) -> str:
        if self.champion is None:
            return f"{self.role}: no champion (ensure_defaults has not run)"
        c = self.champion
        if self.on_disk_hash is None:
            return f"{self.role}: champion {c.id}; PROMPT FILE {c.prompt_name}.md MISSING"
        if self.matches:
            return f"{self.role}: champion {c.id}"
        return (
            f"{self.role}: champion {c.id}; PROMPT CHANGED, running {self.running_variant_id}"
            f" (not promoted)"
        )


def role_status(store: Store, role: str, *, prompts_dir: str | Path | None = None) -> RoleStatus:
    """Compare the champion's recorded prompt hash with the prompt file on disk."""
    current = champion(store, role)
    if current is None:
        return RoleStatus(role, None, None, None)
    try:
        ref = prompts.load(current.prompt_name, prompts_dir)
    except FileNotFoundError:
        return RoleStatus(role, current, None, None)
    return RoleStatus(role, current, ref.sha256, variant_id(current.model, ref))


def prompt_versions(
    names: Iterable[str] | None = None, *, prompts_dir: str | Path | None = None
) -> dict[str, str]:
    """``{name: "<name>@<hash8>"}`` for every prompt file (or just ``names``)."""
    wanted = list(names) if names is not None else list(prompts.list_prompts(prompts_dir))
    out: dict[str, str] = {}
    for name in wanted:
        try:
            out[name] = prompts.version(name, prompts_dir)
        except FileNotFoundError:
            continue
    return out


def status_lines(store: Store, *, prompts_dir: str | Path | None = None) -> list[str]:
    """Lines for ``fman status``: one per role, then the role-less prompt versions."""
    lines = [role_status(store, r, prompts_dir=prompts_dir).line() for r in ROLES]
    role_prompts: set[str] = set()
    for r in ROLES:
        c = champion(store, r)
        role_prompts.add(c.prompt_name if c else default_prompt_name(r))
    versions = prompt_versions(prompts_dir=prompts_dir)
    others = {n: v for n, v in versions.items() if n not in role_prompts}
    if others:
        lines.append("other prompts: " + ", ".join(others[n] for n in sorted(others)))
    return lines


def lookup(store: Store, vid: str) -> Variant | None:
    """A registered variant, or None."""
    try:
        return store.get_variant(vid)
    except NotFoundError:
        return None
