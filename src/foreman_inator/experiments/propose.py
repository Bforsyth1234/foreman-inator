"""LLM-drafted challenger prompts (todo #69, GH #49).

Today every challenger is written by hand. ``propose`` drafts the next prompt version of a
role's champion from the recent kickbacks' patterns; the user approves or discards it;
only an approved draft becomes a challenger, and promotion stays the rule-based gate
(``worker_promote.promote``: wins on replay AND holds live). The LLM writes prompts only:
the challenger keeps the champion's model, and the LLM never registers, runs or promotes.

Flow
----
1. ``propose(store, config, role, proxy=...)``:

   * **One at a time.** Refused, before any model call, while the role has an open draft
     or a running experiment whose challenger an LLM drafted (``experiments.origin =
     'llm'``, migration 011). ``availability`` says the same for a UI button.
   * **Cutoff.** ``cutoff_at`` defaults to ``now - holdout_days``. The proposer reads only
     kickbacks created before it (and after ``cutoff - lookback_days``); the challenger's
     replay uses only tickets created after it (``replay_sets`` cutoff), so it is never
     scored on the tickets it was written from.
   * **Patterns.** writer: kickbacks (user and reviewer) grouped by ``failure_step``
     (#27; unlabeled ones as ``unlabeled``), ``reviewer_miss`` left out. reviewer:
     ``reviewer_miss`` (a user kickback after an approving AI review) and
     ``false_kickback`` (an AI kickback on the attempt that was merged). The top
     ``max_patterns`` groups go to the model with up to ``EXAMPLES_PER_PATTERN`` examples
     each: issue title and body, kickback tag and note, all clipped, ``defuse``d and
     fenced (untrusted text).
   * **Proposer.** A tier 3 model (``proposer=`` or the first configured one) that is
     not the reviewer that scores the challenger: neither the reviewer champion's model
     nor of its family (the reviewer-independence idea, 7f). A named proposer that breaks
     this is refused.
   * **Local only (#49).** With a cloud proposer, kickbacks of repos set ``cloud =
     false`` (``store.repo_allows_cloud``) are left out and counted; their text never
     leaves the Mac.
   * **Spend (7c).** The call goes through ``proxy.context(bucket="experiments")``, so it
     is a traced ``llm.call`` charged to ``spend_caps.experiments_monthly_usd`` by the
     proxy's spend hook. With ``spend=`` a cloud proposer is also refused up front when
     the estimate does not fit the experiments budget.
   * **Draft.** The answer must hold a rationale and a whole prompt keeping the
     champion's ``<!-- section: -->`` markers and ``$placeholders`` exactly; anything else
     is refused and nothing is written. The draft is written to
     ``~/.foreman-inator/prompts/drafts/<role>-vN.md`` with ``<role>-vN.rationale.md``
     (mode 600) and a ``prompt_drafts`` row. The rationale names the targeted patterns
     (computed from the data, then the model's own words). **Nothing is registered.**

2. ``approve_draft(store, draft_id, mirror_for=...)`` (the user's consent): writes the
   draft file (as the user left it) into the prompts folder as ``<role>-vN.md``,
   registers the variant ``<champion model>/<role>-vN@<hash8>``, builds the challenger's
   cutoff replay set and starts (without running) its replay experiment pinned to it,
   tagged ``origin = 'llm'`` with the cutoff (``Store.create_experiment`` tags every
   experiment of an approved draft's variant). ``fman experiment run --role R
   --challenger V`` then resumes exactly that replay (``replay_runner.find_resumable``).
3. ``discard_draft(store, draft_id)``: marks it discarded and removes its files.

Wire format (rule 12): OpenAI-compatible ``POST {base}/chat/completions`` with ``model``,
``messages`` [{role, content}], ``temperature``, ``max_tokens``, ``stream``; answer in
``choices[0].message.content``, usage in ``usage.prompt_tokens`` /
``usage.completion_tokens``. Checked 2026-11-25 against
https://developers.openai.com/api/reference/resources/chat (formerly
https://platform.openai.com/docs/api-reference/chat/create). ``max_tokens`` is
deprecated there in favour of ``max_completion_tokens``, but Ollama and the proxy's spend
estimate read ``max_tokens``, as ``routing.rules.classify`` sends it.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from importlib import resources
from pathlib import Path
from string import Template
from typing import Any, Literal

from fde_kit.prompts import PromptRef, load_prompt

from foreman_inator import paths, prompts, tracing
from foreman_inator.config import Config, ConfigError, ModelEndpoint
from foreman_inator.db.store import Kickback, NotFoundError, PromptDraft, Store, StoreError
from foreman_inator.experiments import variants
from foreman_inator.experiments.replay_sets import DEFAULT_SET_SIZE, MirrorFor, build_replay_set
from foreman_inator.proxy import pricing
from foreman_inator.routing.rules import PROMPT_VERSION_HEADER, RouterError, post_classifier
from foreman_inator.tickets.prompt import clip_head, defuse

Role = Literal["writer", "reviewer"]

PROMPT_NAME = "proposer-v1"
PROXY_LABEL = "propose"
SPAN_NAME = "experiment.propose"
PROPOSER_TIER = 3
#: Kickbacks newer than this many days are held out of the proposer (the replay's cases).
DEFAULT_HOLDOUT_DAYS = 30
#: How far before the cutoff the proposer looks.
DEFAULT_LOOKBACK_DAYS = 90
DEFAULT_MAX_PATTERNS = 3
EXAMPLES_PER_PATTERN = 3
#: Fewer kickbacks than this before the cutoff: nothing to learn from, no model call.
DEFAULT_MIN_KICKBACKS = 3
DEFAULT_MAX_TOKENS = 8_000
DEFAULT_TIMEOUT = 300.0
TITLE_BUDGET = 200
BODY_BUDGET = 600
NOTE_BUDGET = 600
#: A draft longer than this many times the champion is refused.
MAX_GROWTH = 3

_SECTION_RE = re.compile(r"^<!-- section: [a-z_]+ -->$", re.MULTILINE)
_PLACEHOLDER_RE = re.compile(r"\$(?:\$|\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
_THINK_RE = re.compile(r"^\s*<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_ANSWER_RE = re.compile(
    r"^=== RATIONALE ===\s*\n(?P<rationale>.*?)\n=== PROMPT ===\s*\n(?P<prompt>.*?)"
    r"(?:\n=== END ===\s*)?\Z",
    re.DOTALL,
)
_OUR_MARKERS = re.compile(r"={3,}|<{5,}|>{5,}")


class ProposeError(RuntimeError):
    """The proposal could not be made; nothing was written or registered."""


class ProposeRefused(ProposeError):
    """Refused before any model call (one at a time, spend, independence, no data)."""


# --------------------------------------------------------------------------- shapes


@dataclass(frozen=True, slots=True)
class Pattern:
    key: str  # a failure step, "unlabeled", "reviewer_miss" or "false_kickback"
    kickbacks: tuple[Kickback, ...]

    @property
    def count(self) -> int:
        return len(self.kickbacks)

    def as_json(self) -> dict[str, Any]:
        return {"key": self.key, "count": self.count,
                "kickback_ids": [k.id for k in self.kickbacks]}  # fmt: skip


@dataclass(frozen=True, slots=True)
class Gathered:
    patterns: tuple[Pattern, ...]  # every group, most common first
    read: int  # kickbacks the proposer may see
    left_out_local: int  # left out: local-only repo and a cloud proposer
    since: datetime
    cutoff_at: datetime


@dataclass(frozen=True, slots=True)
class Availability:
    ok: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Approved:
    draft: PromptDraft
    variant_id: str
    prompt_path: Path
    set_id: int
    experiment_id: int
    run_hint: str = field(default="")


# --------------------------------------------------------------------------- paths


def drafts_root(home: Path | None = None) -> Path:
    """``~/.foreman-inator/prompts/drafts`` (mode 700)."""
    root = (home or paths.home()) / "prompts" / "drafts"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    os.chmod(root.parent, 0o700)
    return root


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o600)


def _package_prompts_dir() -> Path:
    folder = resources.files(prompts.__name__)
    if not isinstance(folder, Path):
        raise ProposeError("the packaged prompts folder is not a directory; pass prompts_dir")
    return folder


# --------------------------------------------------------------------------- gating


def _check_role(role: str) -> Role:
    if role not in variants.ROLES:
        raise ProposeError(f"role must be one of {', '.join(variants.ROLES)}, not {role!r}")
    return role  # type: ignore[return-value]


def availability(store: Store, role: str) -> Availability:
    """May ``propose`` run for ``role`` now? (One LLM challenger per role at a time.)"""
    r = _check_role(role)
    open_ = store.list_prompt_drafts(role=r, status="draft")
    if open_:
        return Availability(
            False, f"draft #{open_[-1].id} ({open_[-1].prompt_name}) waits: approve or discard it"
        )
    running = store.running_llm_experiments(r)
    if running:
        return Availability(
            False,
            f"experiment #{running[0].id} runs an LLM-drafted {r} challenger "
            f"({running[0].challenger_variant}); one at a time per role",
        )
    return Availability(True)


def scoring_reviewer(store: Store, config: Config) -> ModelEndpoint | str | None:
    """The reviewer that scores a challenger: the reviewer champion's model (for a
    reviewer challenger it is the challenger's own model too). A name not in config is
    returned as the bare name; None when no reviewer champion exists yet."""
    champ = variants.champion(store, "reviewer")
    if champ is None:
        return None
    try:
        return config.model(champ.model)
    except ConfigError:
        return champ.model


def _independent(proposer: ModelEndpoint, reviewer: ModelEndpoint | str | None) -> str | None:
    if reviewer is None:
        return None
    name = reviewer if isinstance(reviewer, str) else reviewer.name
    if proposer.name == name:
        return f"proposer {proposer.name} is the reviewer that scores the challenger"
    if not isinstance(reviewer, str) and proposer.family.lower() == reviewer.family.lower():
        return (
            f"proposer {proposer.name} is of the {proposer.family} family, like the scoring "
            f"reviewer {reviewer.name}"
        )
    return None


def pick_proposer(
    store: Store, config: Config, proposer: ModelEndpoint | str | None = None
) -> ModelEndpoint:
    """``proposer`` (checked), else the first tier 3 model independent of the scoring
    reviewer. Raises ``ProposeRefused``."""
    reviewer = scoring_reviewer(store, config)
    if proposer is not None:
        ep = config.model(proposer) if isinstance(proposer, str) else proposer
        why = _independent(ep, reviewer)
        if why:
            raise ProposeRefused(why + " (7f: the proposer must not be the scorer)")
        return ep
    for ep in config.models_for_tier(PROPOSER_TIER):
        if _independent(ep, reviewer) is None:
            return ep
    raise ProposeRefused(
        f"no tier {PROPOSER_TIER} model independent of the scoring reviewer "
        f"{reviewer if isinstance(reviewer, str) else getattr(reviewer, 'name', None)}"
    )


# --------------------------------------------------------------------------- patterns


def _kickbacks_between(store: Store, since: datetime, until: datetime) -> list[Kickback]:
    return [k for k in store.list_kickbacks() if since <= k.created_at < until]


def _repo_of(store: Store, kickback: Kickback) -> tuple[str, Any, Any]:
    att = store.get_attempt(kickback.attempt_id)
    ticket = store.get_ticket(att.ticket_id)
    return ticket.repo, att, ticket


def _approved_by_ai(store: Store, attempt_id: int) -> bool:
    return any(
        r.verdict == "approve" for r in store.list_reviews(attempt_id=attempt_id, shadow=False)
    )


def _key(store: Store, role: Role, k: Kickback, att: Any, ticket: Any) -> str | None:
    if role == "writer":
        if k.failure_step == "reviewer_miss":
            return None
        return k.failure_step or "unlabeled"
    if k.by == "user" and (k.failure_step == "reviewer_miss" or _approved_by_ai(store, att.id)):
        return "reviewer_miss"
    if k.by == "reviewer" and ticket.accepted_attempt_id == att.id:
        return "false_kickback"
    return None


def gather(
    store: Store,
    role: str,
    *,
    cutoff_at: datetime,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    cloud_proposer: bool = True,
) -> Gathered:
    """Kickbacks created in ``[cutoff - lookback, cutoff)`` grouped into patterns, most
    common first. With ``cloud_proposer`` a local-only repo's kickbacks are left out."""
    r = _check_role(role)
    since = cutoff_at - timedelta(days=lookback_days)
    groups: dict[str, list[Kickback]] = {}
    read = left_out = 0
    for k in _kickbacks_between(store, since, cutoff_at):
        try:
            repo, att, ticket = _repo_of(store, k)
        except NotFoundError:
            continue
        key = _key(store, r, k, att, ticket)
        if key is None:
            continue
        if cloud_proposer and not store.repo_allows_cloud(repo):
            left_out += 1
            continue
        read += 1
        groups.setdefault(key, []).append(k)
    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    pats = tuple(Pattern(key, tuple(ks)) for key, ks in ordered)
    return Gathered(pats, read, left_out, since, cutoff_at)


def _clean(text: str, budget: int) -> str:
    """Untrusted text, clipped, with fence look-alikes (theirs and ours) neutralized."""
    return _OUR_MARKERS.sub("==", defuse(clip_head(text or "", budget)))


def patterns_text(store: Store, patterns: Sequence[Pattern]) -> str:
    lines: list[str] = []
    for i, p in enumerate(patterns, 1):
        lines.append(f"{i}. {p.key}: {p.count} kickback{'s' if p.count != 1 else ''}")
        for k in p.kickbacks[-EXAMPLES_PER_PATTERN:]:
            _repo, _att, ticket = _repo_of(store, k)
            by = "the user" if k.by == "user" else "the AI reviewer"
            lines += [
                f"   Example (kickback by {by}, tag {k.tag or 'none'}):",
                "----- BEGIN ISSUE -----",
                "Title: " + _clean(ticket.title, TITLE_BUDGET).replace("\n", " "),
                _clean(ticket.request, BODY_BUDGET) or "(no description)",
                "----- END ISSUE -----",
                "----- BEGIN KICKBACK NOTE -----",
                _clean(k.note or "", NOTE_BUDGET) or "(no note)",
                "----- END KICKBACK NOTE -----",
            ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- prompt / answer


def load_template(name: str = PROMPT_NAME) -> PromptRef:
    folder = resources.files("foreman_inator") / "prompts"
    with resources.as_file(folder / f"{name}.md") as path:
        return load_prompt(str(path))


def prompt_version(ref: PromptRef) -> str:
    return f"{ref.name}@{ref.sha256[:8]}"


def build_messages(
    *,
    role: Role,
    champion_variant: str,
    champion_text: str,
    patterns: str,
    cutoff_at: datetime,
    template: PromptRef,
) -> list[dict[str, str]]:
    parts = re.split(r"^<!-- section: ([a-z_]+) -->$", template.text, flags=re.MULTILINE)
    sec = {n: Template(b.strip("\n")) for n, b in zip(parts[1::2], parts[2::2], strict=True)}
    job = "coding agent (the writer)" if role == "writer" else "code reviewer"
    return [
        {"role": "system", "content": sec["system"].substitute(role_job=job)},
        {
            "role": "user",
            "content": sec["user"].substitute(
                role=role,
                champion_variant=champion_variant,
                cutoff=cutoff_at.isoformat(timespec="seconds"),
                patterns=patterns,
                champion_prompt=champion_text,
            ),
        },
    ]


def _placeholders(text: str) -> set[str]:
    return {a or b for a, b in _PLACEHOLDER_RE.findall(text) if a or b}


def parse_answer(text: str, champion_text: str) -> tuple[str, str]:
    """``(rationale, prompt)`` or ``ProposeError``. The prompt must keep the champion's
    section markers (in order) and exactly its placeholders."""
    body = _THINK_RE.sub("", text or "", count=1).strip()
    m = _ANSWER_RE.match(body)
    if not m:
        raise ProposeError("the answer is not in the RATIONALE / PROMPT form")
    rationale, draft = m.group("rationale").strip(), m.group("prompt").strip()
    if not rationale or not draft:
        raise ProposeError("the answer has an empty rationale or prompt")
    if _SECTION_RE.findall(draft) != _SECTION_RE.findall(champion_text):
        raise ProposeError("the drafted prompt does not keep the champion's sections")
    want, got = _placeholders(champion_text), _placeholders(draft)
    if want != got:
        raise ProposeError(
            "the drafted prompt changes the placeholders: "
            f"missing {sorted(want - got)}, added {sorted(got - want)}"
        )
    if len(draft) > MAX_GROWTH * max(len(champion_text), 1):
        raise ProposeError(f"the drafted prompt is over {MAX_GROWTH}x the champion's length")
    if draft.strip() == champion_text.strip():
        raise ProposeError("the drafted prompt is the champion unchanged")
    return rationale, draft + "\n"


def next_prompt_name(store: Store, role: Role, *, prompts_dir: Path, drafts_dir: Path) -> str:
    rx = re.compile(rf"^{role}-v(\d+)$")
    names = set(prompts.list_prompts(prompts_dir)) | set(prompts.list_prompts(prompts_dir=None))
    names |= {p.stem for p in drafts_dir.glob("*.md") if not p.name.endswith(".rationale.md")}
    names |= {d.prompt_name for d in store.list_prompt_drafts(role=role)}
    nums = [int(m.group(1)) for n in names if (m := rx.match(n))]
    return f"{role}-v{max(nums, default=0) + 1}"


def _rationale_doc(
    *,
    name: str,
    role: Role,
    champion: str,
    proposer: ModelEndpoint,
    g: Gathered,
    targeted: Sequence[Pattern],
    model_rationale: str,
    est: float,
    cost: float,
) -> str:
    lines = [
        f"# {name}: a drafted {role} prompt (LLM-proposed, #69)",
        "",
        f"Drafted from the {role} champion `{champion}` by `{proposer.name}` "
        f"(tier {proposer.tier}, {proposer.kind}, family {proposer.family}).",
        f"Cutoff: {g.cutoff_at.isoformat(timespec='seconds')}. Read {g.read} kickbacks from "
        f"{g.since.isoformat(timespec='seconds')} to the cutoff"
        + (f"; left out {g.left_out_local} of local-only repos." if g.left_out_local else "."),
        "Its replay uses only tickets created after the cutoff.",
        "",
        "## Patterns targeted",
    ]
    for p in targeted:
        ids = ", ".join(f"#{k.id}" for k in p.kickbacks)
        lines.append(f"- {p.key}: {p.count} kickback{'s' if p.count != 1 else ''} ({ids})")
    lines += [
        "",
        "## Proposer's rationale",
        _clean(model_rationale, 4_000),
        "",
        f"Estimated proposer cost: ${est:.4f}; reported: ${cost:.4f} (experiments bucket).",
        "Nothing is registered until `fman experiment approve-draft`.",
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- propose


Post = Callable[[str, dict[str, Any], dict[str, str], float], Any]


def _champion(
    store: Store, config: Config, role: Role, prompts_dir: Path | None
) -> tuple[Any, str]:
    champ = variants.champion(store, role)
    if champ is None:
        variants.ensure_defaults(store, config)
        champ = variants.champion(store, role)
    if champ is None:
        raise ProposeRefused(f"no {role} champion")
    try:
        ref = prompts.load(champ.prompt_name, prompts_dir)
    except FileNotFoundError:
        ref = prompts.load(champ.prompt_name)
    if ref.sha256 != champ.prompt_hash:
        raise ProposeRefused(
            f"the {role} champion's prompt file {champ.prompt_name}.md changed since it was "
            "promoted; revert it before proposing"
        )
    return champ, ref.text


def propose(
    store: Store,
    config: Config,
    role: str,
    *,
    proxy: Any,
    proposer: ModelEndpoint | str | None = None,
    spend: Any = None,
    cutoff_at: datetime | None = None,
    holdout_days: int = DEFAULT_HOLDOUT_DAYS,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    max_patterns: int = DEFAULT_MAX_PATTERNS,
    min_kickbacks: int = DEFAULT_MIN_KICKBACKS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout: float = DEFAULT_TIMEOUT,
    drafts_dir: Path | None = None,
    prompts_dir: Path | None = None,
    template: PromptRef | None = None,
    post: Post | None = None,
) -> PromptDraft:
    """Draft the next prompt of ``role``'s champion (see the module docstring). Returns the
    stored draft; registers nothing. ``ProposeRefused`` before any model call,
    ``ProposeError`` after one (nothing written)."""
    r = _check_role(role)
    avail = availability(store, r)
    if not avail.ok:
        raise ProposeRefused(avail.reason or "not available")
    ep = pick_proposer(store, config, proposer)
    champ, champ_text = _champion(store, config, r, prompts_dir)
    cutoff = cutoff_at or (store.now() - timedelta(days=holdout_days))
    g = gather(store, r, cutoff_at=cutoff, lookback_days=lookback_days, cloud_proposer=ep.is_cloud)
    if g.read < max(1, min_kickbacks):
        raise ProposeRefused(
            f"only {g.read} {r} kickback(s) before the cutoff {cutoff:%Y-%m-%d}; need "
            f"{min_kickbacks} to propose from"
        )
    targeted = g.patterns[: max(1, max_patterns)]
    ref = template or load_template()
    pv = prompt_version(ref)
    messages = build_messages(role=r, champion_variant=champ.id, champion_text=champ_text,
                              patterns=patterns_text(store, targeted), cutoff_at=cutoff,
                              template=ref)  # fmt: skip
    est = pricing.cost_usd(ep, pricing.estimate_tokens(json.dumps(messages)), max_tokens)
    if spend is not None and ep.is_cloud and not spend.allow(est, "experiments"):
        raise ProposeRefused(
            f"the experiments budget has no room for the proposer call (~${est:.4f})"
        )
    body = {"model": ep.name, "messages": messages, "temperature": 0.2,
            "max_tokens": int(max_tokens), "stream": False}  # fmt: skip
    headers = {"content-type": "application/json", PROMPT_VERSION_HEADER: pv}
    with tracing.span(SPAN_NAME, {"experiment.role": r, "proposer.model": ep.name,
                                  "kickbacks.read": g.read}):  # fmt: skip
        try:
            with proxy.context(bucket="experiments", label=PROXY_LABEL, prompt_version=pv) as ctx:
                data = post_classifier(ctx.base_url.rstrip("/") + "/chat/completions", body,
                                       headers, classifier=ep, timeout=timeout,
                                       post=post)  # fmt: skip
        except RouterError as exc:
            raise ProposeError(f"proposer call failed: {exc}") from None
    try:
        answer = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ProposeError("the proposer's response has no choices[0].message.content") from None
    usage = data.get("usage") or {} if isinstance(data, dict) else {}
    cost = pricing.cost_usd(ep, int(usage.get("prompt_tokens") or 0),
                            int(usage.get("completion_tokens") or 0))  # fmt: skip
    rationale, draft = parse_answer(str(answer or ""), champ_text)

    folder = drafts_dir or drafts_root()
    pdir = prompts_dir or _package_prompts_dir()
    name = next_prompt_name(store, r, prompts_dir=pdir, drafts_dir=folder)
    doc = _rationale_doc(name=name, role=r, champion=champ.id, proposer=ep, g=g,
                         targeted=targeted, model_rationale=rationale, est=est,
                         cost=cost)  # fmt: skip
    path = folder / f"{name}{prompts.PROMPT_SUFFIX}"
    _write_private(path, draft)
    _write_private(folder / f"{name}.rationale.md", doc)
    draft_ref = load_prompt(str(path))
    try:
        return store.create_prompt_draft(
            role=r,
            prompt_name=name,
            prompt_hash=draft_ref.sha256,
            prompt_text=draft,
            rationale=doc,
            patterns=json.dumps([p.as_json() for p in targeted]),
            base_variant=champ.id,
            model=champ.model,
            proposer_model=ep.name,
            cutoff_at=cutoff,
            path=str(path),
            est_cost_usd=est,
            cost_usd=cost,
        )
    except Exception:
        _remove_files(path)
        raise


# --------------------------------------------------------------------------- approve / discard


def _remove_files(path: Path) -> None:
    for p in (path, path.with_name(path.stem + ".rationale.md")):
        with contextlib.suppress(FileNotFoundError):
            p.unlink()


def _open_draft(store: Store, draft_id: int) -> PromptDraft:
    try:
        draft = store.get_prompt_draft(draft_id)
    except NotFoundError as exc:
        raise ProposeError(str(exc)) from None
    if draft.status != "draft":
        raise ProposeError(f"draft #{draft.id} is already {draft.status}")
    return draft


def approve_draft(
    store: Store,
    draft_id: int,
    *,
    mirror_for: MirrorFor,
    prompts_dir: Path | None = None,
    max_merges: int = DEFAULT_SET_SIZE,
    recordings_dir: Path | str | None = None,
) -> Approved:
    """The user approved draft ``draft_id``: install its prompt file, register the
    challenger and start (not run) its cutoff replay. See the module docstring."""
    from foreman_inator.experiments import replay_runner as rr

    draft = _open_draft(store, draft_id)
    running = store.running_llm_experiments(draft.role)
    if running:
        raise ProposeError(
            f"experiment #{running[0].id} already runs an LLM {draft.role} challenger"
        )
    champ = variants.champion(store, draft.role)
    if champ is None or champ.id != draft.base_variant:
        raise ProposeError(
            f"the {draft.role} champion is now {champ.id if champ else '(none)'}, not "
            f"{draft.base_variant} the draft was written from; discard it and propose again"
        )
    src = Path(draft.path)
    text = src.read_text(encoding="utf-8") if src.is_file() else draft.prompt_text
    base = store.get_variant(draft.base_variant)
    try:
        base_text = prompts.load(base.prompt_name, prompts_dir).text
    except FileNotFoundError:
        base_text = prompts.load(base.prompt_name).text
    parse_answer(f"=== RATIONALE ===\nok\n=== PROMPT ===\n{text.strip()}", base_text)  # re-check
    pdir = prompts_dir or _package_prompts_dir()
    dest = pdir / f"{draft.prompt_name}{prompts.PROMPT_SUFFIX}"
    if dest.exists() and dest.read_text(encoding="utf-8").strip() != text.strip():
        raise ProposeError(f"{dest} exists with other text; rename the draft")
    dest.write_text(text, encoding="utf-8")
    ref = prompts.load(draft.prompt_name, pdir)
    draft = store.decide_prompt_draft(draft.id, "approved", prompt_text=text,
                                      prompt_hash=ref.sha256)  # fmt: skip
    variant = variants.register(store, draft.role, draft.model, ref)
    rset = build_replay_set(store, draft.role, mirror_for=mirror_for, max_merges=max_merges,
                            recordings_dir=recordings_dir, cutoff_at=draft.cutoff_at)  # fmt: skip
    exp_id = rr.start_replay(store, role=draft.role, champion_variant=draft.base_variant,
                             challenger_variant=variant.id, set_id=rset.id)  # fmt: skip
    draft = store.set_prompt_draft_experiment(draft.id, exp_id)
    hint = f"fman experiment run --role {draft.role} --challenger {variant.id}"
    return Approved(draft, variant.id, dest, rset.id, exp_id, hint)


def discard_draft(store: Store, draft_id: int) -> PromptDraft:
    """Discard an open draft and remove its files (the row keeps the text)."""
    draft = _open_draft(store, draft_id)
    try:
        draft = store.decide_prompt_draft(draft.id, "discarded")
    except StoreError as exc:
        raise ProposeError(str(exc)) from None
    _remove_files(Path(draft.path))
    return draft


__all__ = [
    "DEFAULT_HOLDOUT_DAYS",
    "DEFAULT_LOOKBACK_DAYS",
    "PROMPT_NAME",
    "Approved",
    "Availability",
    "Gathered",
    "Pattern",
    "ProposeError",
    "ProposeRefused",
    "approve_draft",
    "availability",
    "discard_draft",
    "drafts_root",
    "gather",
    "next_prompt_name",
    "parse_answer",
    "pick_proposer",
    "propose",
    "scoring_reviewer",
]
