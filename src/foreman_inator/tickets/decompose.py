"""Split a large issue into smaller tickets (todo #50, GH #27).

An issue the router classifies ``large`` would go straight to tier 3 as one attempt.
Instead, one tier 3 model call proposes 2 to 6 sub-issues (title, body, rough files),
the worker posts them as ONE comment on the issue and flags the ticket
``split_proposed``; the ticket is not worked. Nothing here, or in the worker, creates a
sub-issue or applies a label: the user creates the sub-issues they want and labels them
``agent`` themselves, and each runs as a normal ticket (a label applied by the bot never
counts, invariant 7b).

Running it as one ticket instead (decision, documented in SPEC/docs by the wiring step):
**re-label** the issue (remove ``agent`` and add it again). The new ticket finds the
bot's proposal comment for an *earlier* ticket of the same issue (``find_proposal`` /
``proposal_ticket``) and goes to tier 3 as before, with no second model call. A
``tier:N`` label is an explicit route and is never split. No ``no-split`` label is read.

Everything here is a pure function or a single model call; nothing writes to GitHub or
the store. ``worker_route`` wires the pieces:

* ``eligible(route)``: the router's own pick (not an override, not a fallback) is tier 3:
  the rules classifier said ``large``, or the kNN router wanted tier 3.
* ``skip_reason(...)``: the cheap, offline reasons not to split (turned off, the repo
  profile's writer mode is ``fast``, no tier 3 model, a cloud tier 3 model on a repo that
  may not use cloud models).
* ``propose(ticket, ...) -> Proposal | Skipped``: one call to the tier 3 model through the
  model proxy (``proxy.context(label="split")``, so a cloud call is a traced
  ``llm.call`` inside the spend caps, 7c) inside a ``split`` span, with the prompt version
  (``split-v1@<hash[:8]>``) on the call. The per-attempt budget (#58) is checked before
  anything is sent, on the proxy's own high estimate (prompt chars/3 tokens in,
  ``MAX_TOKENS`` out). A spend-cap refusal comes back from the proxy as HTTP 429. Never
  raises; any failure is ``Skipped`` with a ``split fallback:`` reason, and the worker
  then does today's thing (a tier 3 attempt) and logs it.
* ``proposal_comment(ticket_id, proposal)``: the comment body. A hidden marker on its
  first line names the ticket. The model's text is cleaned (one-line titles, no HTML so
  no forged marker, no BEGIN/END look-alikes, no control characters, bounded) and the
  whole proposal sits inside one fenced code block whose fence is longer than any
  backtick run in it, so nothing in it renders: no mentions, links or ``#123``
  references.

External docs checked 2026-11-25 (rule 12):

* GitHub fenced code blocks: https://github.github.com/gfm/#fenced-code-blocks (a closing
  fence must be at least as long as the opening one; content is literal text, so no
  mentions, links or references render inside it).
* GitHub "Create an issue comment": https://docs.github.com/en/rest/issues/comments
  (``body`` is markdown; the existing ``GitHubClient.post_comment`` is used).
* OpenAI chat completions (what the proxy speaks): https://platform.openai.com/docs/api-reference/chat/create
  (``messages``, ``max_tokens``, ``temperature``, ``choices[0].message.content``).
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from string import Template
from typing import Any
from urllib.parse import urlparse

from fde_kit.prompts import PromptRef, load_prompt

from foreman_inator import tracing
from foreman_inator.config import (
    DEFAULT_ATTEMPT_BUDGET_USD,
    DEFAULT_ATTEMPT_MAX_INPUT_TOKENS_CLOUD,
    DEFAULT_ATTEMPT_MAX_INPUT_TOKENS_LOCAL,
    Config,
    ModelEndpoint,
)
from foreman_inator.github.client import Comment
from foreman_inator.proxy import pricing
from foreman_inator.routing import rules
from foreman_inator.tickets.prompt import clip_head, defuse

log = logging.getLogger(__name__)

SPLIT_PROMPT_NAME = "split-v1"
SPAN_NAME = "split"
PROXY_LABEL = "split"
#: ``flag_reason`` prefix of a ticket whose split was proposed (``locks.flag_kind``).
FLAG_SPLIT_PROPOSED = "split_proposed"
#: Module default for the (future) ``split_large`` config key.
DEFAULT_ENABLED = True
#: The tier whose model proposes the split, and the tier a large ticket would start on.
SPLIT_TIER = 3

MIN_PARTS = 2
MAX_PARTS = 6
MAX_FILES = 8
DEFAULT_TIMEOUT = 180.0  # seconds, the whole call; a tier 3 model writing a plan
DEFAULT_COLD_LOAD_ALLOWANCE = rules.DEFAULT_COLD_LOAD_ALLOWANCE
MAX_TOKENS = 3_000  # six sub-issues of ~800 characters fit with room to spare

# Budgets (characters).
TITLE_BUDGET = 300
ISSUE_BODY_BUDGET = 12_000  # a large issue: give the planner the whole ask
SUB_TITLE_BUDGET = 120
SUB_BODY_BUDGET = 1_000
FILE_BUDGET = 200
MAX_ANSWER_CHARS = 20_000  # a longer answer is malformed, whatever it says

PROPOSAL_MARKER = "<!-- foreman-inator:split-proposed ticket={ticket_id} -->"
_MARKER_LINE = re.compile(r"^<!-- foreman-inator:split-proposed ticket=(\d+) -->$")

_SECTION = re.compile(r"^<!-- section: ([a-z_]+) -->$", re.MULTILINE)
_THINK = re.compile(r"^\s*<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"^```(?:json)?\s*\n(.*)\n```$", re.DOTALL | re.IGNORECASE)
_BACKTICKS = re.compile(r"`+")


# --------------------------------------------------------------------------- results


@dataclass(frozen=True, slots=True)
class SubIssue:
    """One proposed sub-issue, already cleaned (``clean_title`` / ``clean_body``)."""

    title: str
    body: str
    files: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Proposal:
    """``MIN_PARTS`` to ``MAX_PARTS`` cleaned sub-issues, safe to post in a comment."""

    sub_issues: tuple[SubIssue, ...]
    splitter: str | None = None  # config name of the model asked
    prompt_name: str | None = None
    prompt_hash: str | None = None
    raw: str | None = None  # the model's answer as sent (bounded), for audits

    def __post_init__(self) -> None:
        if not MIN_PARTS <= len(self.sub_issues) <= MAX_PARTS:
            raise ValueError(f"a proposal takes {MIN_PARTS} to {MAX_PARTS} sub-issues")

    @property
    def prompt_version(self) -> str | None:
        return _prompt_version(self.prompt_name, self.prompt_hash)


@dataclass(frozen=True, slots=True)
class Skipped:
    """No proposal: run the ticket as one, as before. ``reason`` says why; a reason
    starting ``split fallback:`` means a call was tried (or refused) and failed."""

    reason: str
    splitter: str | None = None
    prompt_name: str | None = None
    prompt_hash: str | None = None

    @property
    def is_fallback(self) -> bool:
        return self.reason.startswith("split fallback:")


Outcome = Proposal | Skipped


class SplitError(Exception):
    """The answer could not be used; the message goes into the fallback reason."""


def _prompt_version(name: str | None, digest: str | None) -> str | None:
    return f"{name}@{digest[:8]}" if name and digest else None


def _always(_repo: str) -> bool:
    return True


# --------------------------------------------------------------------------- deciding


def eligible(route: Any) -> bool:
    """The router's own pick sends the ticket straight to tier 3 (``large``). Never for a
    ``tier:N`` override (an explicit route) or a router fallback."""
    source = getattr(route, "source", None)
    if source == "classifier":
        return getattr(route, "label", None) == "large"
    if source == "knn":
        return getattr(route, "requested_tier", None) == SPLIT_TIER
    return False


def splitter_for(models: Config | Iterable[ModelEndpoint]) -> ModelEndpoint | None:
    """The first configured tier 3 model (config order), or None."""
    eps = models.models if isinstance(models, Config) else tuple(models)
    return next((m for m in eps if m.tier == SPLIT_TIER), None)


def skip_reason(
    *,
    repo: str,
    models: Config | Iterable[ModelEndpoint],
    enabled: bool = DEFAULT_ENABLED,
    writer_mode: str = "normal",
    cloud_allowed: Callable[[str], bool] = _always,
) -> str | None:
    """Why not to try a split (no model call), or None to try one.

    ``writer_mode`` is the repo profile's (#51): ``fast`` trades cost for speed and does
    not wait on the user, so it is never split. ``cloud_allowed(repo)`` is False for a
    local-only repo (``repo_settings.cloud = 0``, #49): a cloud tier 3 model is then never
    sent the issue."""
    if not enabled:
        return "splitting is off"
    if writer_mode == "fast":
        return "the repo profile's writer mode is fast; not splitting"
    ep = splitter_for(models)
    if ep is None:
        return f"no tier {SPLIT_TIER} model configured to propose a split"
    try:
        allowed = bool(cloud_allowed(repo))
    except Exception as exc:  # fail closed: an unreadable setting keeps the issue local
        log.warning("split: cloud check for %s raised %s; treating as local-only", repo, exc)
        allowed = False
    if ep.is_cloud and not allowed:
        return f"model {ep.name} is a cloud model and {repo} is local-only; not splitting"
    return None


# --------------------------------------------------------------------------- template


def load_split_template(name: str = SPLIT_PROMPT_NAME) -> PromptRef:
    """Load the split prompt from the package's ``prompts/`` folder (package data)."""
    folder = resources.files("foreman_inator") / "prompts"
    with resources.as_file(folder / f"{name}.md") as path:
        return load_prompt(str(path))


def _sections(text: str) -> dict[str, Template]:
    parts = _SECTION.split(text)
    out: dict[str, Template] = {}
    for name, body in zip(parts[1::2], parts[2::2], strict=True):
        if name in out:
            raise ValueError(f"split template has section {name!r} twice")
        out[name] = Template(body.strip("\n"))
    missing = {"system", "user"} - set(out)
    if missing:
        raise ValueError(f"split template lacks section(s) {sorted(missing)}")
    return out


def build_messages(
    *, repo: str, title: str, body: str, template: PromptRef
) -> list[dict[str, str]]:
    """The chat messages: system rules, then the fenced, defused, bounded issue."""
    s = _sections(template.text)
    system = s["system"].substitute(min_parts=MIN_PARTS, max_parts=MAX_PARTS)
    user = s["user"].substitute(
        repo=defuse(clip_head(repo, TITLE_BUDGET)),
        issue_title=defuse(clip_head(title, TITLE_BUDGET).replace("\n", " ")),
        issue_body=defuse(clip_head(body, ISSUE_BODY_BUDGET)) or "(no description)",
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# --------------------------------------------------------------------------- cleaning


def _printable(text: str, *, keep_newlines: bool) -> str:
    out = []
    for ch in text:
        if ch == "\n" and keep_newlines:
            out.append(ch)
        elif unicodedata.category(ch)[0] == "C":
            out.append(" " if ch in "\t\r\n" else "")
        else:
            out.append(ch)
    return "".join(out)


def _neutral(text: str) -> str:
    """No HTML (so no forged marker), no fence look-alikes."""
    return defuse(text.replace("<", "‹").replace(">", "›"))


def _bounded(text: str, budget: int) -> str:
    return text if len(text) <= budget else text[: budget - 3].rstrip() + "..."


def clean_title(text: Any) -> str:
    flat = " ".join(_printable(str(text), keep_newlines=False).split())
    return _bounded(_neutral(flat).strip(), SUB_TITLE_BUDGET)


def clean_body(text: Any) -> str:
    raw = _printable(str(text).replace("\r\n", "\n"), keep_newlines=True)
    lines = [" ".join(line.split()) for line in raw.split("\n")]
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return _bounded(_neutral(body), SUB_BODY_BUDGET)


def clean_file(text: Any) -> str:
    flat = "".join(_printable(str(text), keep_newlines=False).split())
    return _bounded(_neutral(flat), FILE_BUDGET)


# --------------------------------------------------------------------------- parsing


def parse_answer(text: Any) -> tuple[SubIssue, ...]:
    """The sub-issues in a model answer. Accepted, and nothing else: a JSON object with
    one key, ``sub_issues``, a list of ``MIN_PARTS`` to ``MAX_PARTS`` objects with a
    non-empty ``title`` string, a ``body`` string and an optional ``files`` list of
    strings (at most ``MAX_FILES`` kept); optionally inside one ```` ```json ```` fence
    and/or after one complete leading ``<think>...</think>`` block. Everything is cleaned;
    a sub-issue whose title cleans to nothing, or repeats an earlier title, is dropped,
    and fewer than ``MIN_PARTS`` left is an error. Raises ``SplitError``."""
    if not isinstance(text, str):
        raise SplitError("model returned no text")
    if len(text) > MAX_ANSWER_CHARS * 4:
        raise SplitError(f"answer too long ({len(text)} chars)")
    answer = _THINK.sub("", text, count=1).strip()
    fenced = _FENCE.match(answer)
    if fenced:
        answer = fenced.group(1).strip()
    if not answer:
        raise SplitError("model returned an empty answer")
    if len(answer) > MAX_ANSWER_CHARS:
        raise SplitError(f"answer too long ({len(answer)} chars)")
    try:
        obj = json.loads(answer)
    except ValueError as exc:
        raise SplitError(f"unparseable answer {rules._show(answer)}") from exc
    if not (isinstance(obj, dict) and set(obj) == {"sub_issues"}):
        raise SplitError(f"unexpected answer JSON {rules._show(answer)}")
    items = obj["sub_issues"]
    if not isinstance(items, list):
        raise SplitError("sub_issues is not a list")
    if not MIN_PARTS <= len(items) <= MAX_PARTS:
        raise SplitError(f"{len(items)} sub-issues, not {MIN_PARTS} to {MAX_PARTS}")
    out: list[SubIssue] = []
    seen: set[str] = set()
    for item in items:
        if not (isinstance(item, dict) and {"title", "body"} <= set(item)):
            raise SplitError("a sub-issue lacks a title or a body")
        if not set(item) <= {"title", "body", "files"}:
            raise SplitError(f"a sub-issue has unexpected keys {sorted(set(item))}")
        title, body, files = item["title"], item["body"], item.get("files", [])
        if not (isinstance(title, str) and isinstance(body, str)):
            raise SplitError("a sub-issue's title or body is not text")
        if not (isinstance(files, list) and all(isinstance(f, str) for f in files)):
            raise SplitError("a sub-issue's files is not a list of strings")
        t = clean_title(title)
        if not t or t.lower() in seen:
            continue
        seen.add(t.lower())
        paths = tuple(dict.fromkeys(p for p in (clean_file(f) for f in files) if p))
        out.append(SubIssue(t, clean_body(body), paths[:MAX_FILES]))
    if len(out) < MIN_PARTS:
        raise SplitError(f"only {len(out)} usable sub-issue(s) after cleaning")
    return tuple(out)


# --------------------------------------------------------------------------- budget


def budget_refusal(
    ep: ModelEndpoint,
    body: dict[str, Any],
    *,
    attempt_budget_usd: float | None = DEFAULT_ATTEMPT_BUDGET_USD,
    max_input_tokens_cloud: int | None = DEFAULT_ATTEMPT_MAX_INPUT_TOKENS_CLOUD,
    max_input_tokens_local: int | None = DEFAULT_ATTEMPT_MAX_INPUT_TOKENS_LOCAL,
) -> str | None:
    """#58's per-attempt budget applied to this one call, before it is sent, on the
    proxy's own estimate (``ModelProxy._estimate``: request bytes / 3 tokens in,
    ``max_tokens`` out). None when it fits; ``None`` limits are off."""
    raw = json.dumps(body).encode()
    tokens_in = pricing.estimate_tokens(raw)
    limit = max_input_tokens_cloud if ep.is_cloud else max_input_tokens_local
    if limit is not None and tokens_in > int(limit):
        return f"about {tokens_in} input tokens, over the per-attempt limit of {int(limit)}"
    if ep.is_cloud and attempt_budget_usd is not None:
        cost = pricing.cost_usd(ep, tokens_in, int(body.get("max_tokens") or MAX_TOKENS))
        if cost > float(attempt_budget_usd) + 1e-9:
            return (
                f"estimated ${cost:.2f}, over the per-attempt budget of "
                f"${float(attempt_budget_usd):.2f}"
            )
    return None


# --------------------------------------------------------------------------- propose


def propose(
    ticket: Any,
    *,
    config: Config | Iterable[ModelEndpoint],
    proxy: rules.ProxyLike | None = None,
    base_url: str | None = None,
    splitter: ModelEndpoint | None = None,
    cloud_allowed: Callable[[str], bool] = _always,
    timeout: float = DEFAULT_TIMEOUT,
    template: PromptRef | None = None,
    watch: rules.ModelWatch | None = None,
    cold_load_allowance: float = DEFAULT_COLD_LOAD_ALLOWANCE,
    attempt_budget_usd: float | None = DEFAULT_ATTEMPT_BUDGET_USD,
    max_input_tokens_cloud: int | None = DEFAULT_ATTEMPT_MAX_INPUT_TOKENS_CLOUD,
    max_input_tokens_local: int | None = DEFAULT_ATTEMPT_MAX_INPUT_TOKENS_LOCAL,
) -> Outcome:
    """Propose sub-issues for ``ticket`` (anything with ``title``, ``request``, ``repo``).
    One model call, inside a ``split`` span; never raises. ``proxy`` / ``base_url`` /
    ``watch`` / ``cold_load_allowance`` work as in ``routing.route``."""
    models = config.models if isinstance(config, Config) else tuple(config)
    with tracing.span(SPAN_NAME) as span:
        result = _propose(
            ticket,
            models=models,
            proxy=proxy,
            base_url=base_url,
            splitter=splitter,
            cloud_allowed=cloud_allowed,
            timeout=timeout,
            template=template,
            watch=watch,
            cold_load_allowance=cold_load_allowance,
            limits=(attempt_budget_usd, max_input_tokens_cloud, max_input_tokens_local),
        )
        attrs = {
            "split.result": "proposed" if isinstance(result, Proposal) else "skipped",
            "split.reason": result.reason if isinstance(result, Skipped) else None,
            "split.parts": len(result.sub_issues) if isinstance(result, Proposal) else 0,
            "split.splitter": result.splitter,
            "split.prompt_version": _prompt_version(result.prompt_name, result.prompt_hash),
        }
        for k, v in attrs.items():
            if v is not None:
                span.set_attribute(k, v)
    return result


def _propose(
    ticket: Any,
    *,
    models: tuple[ModelEndpoint, ...],
    proxy: rules.ProxyLike | None,
    base_url: str | None,
    splitter: ModelEndpoint | None,
    cloud_allowed: Callable[[str], bool],
    timeout: float,
    template: PromptRef | None,
    watch: rules.ModelWatch | None,
    cold_load_allowance: float,
    limits: tuple[float | None, int | None, int | None],
) -> Outcome:
    name: str | None = None
    digest: str | None = None
    ep = splitter
    repo = str(getattr(ticket, "repo", "") or "")

    def fallback(why: str) -> Skipped:
        log.warning("split fallback for ticket %s: %s", getattr(ticket, "id", "?"), why)
        return Skipped(
            reason=f"split fallback: {why}; running it as one ticket",
            splitter=ep.name if ep else None,
            prompt_name=name,
            prompt_hash=digest,
        )

    raw: str | None = None
    try:
        ref = template or load_split_template()
        name, digest = ref.name, ref.sha256
        if ep is None:
            ep = splitter_for(models)
        if ep is None:
            return fallback(f"no tier {SPLIT_TIER} model configured")
        if ep.is_cloud:
            try:
                allowed = bool(cloud_allowed(repo))
            except Exception:
                allowed = False
            if not allowed:  # 7c and #49: a local-only repo's issue never leaves the Mac
                return fallback(f"model {ep.name} is a cloud model and the repo is local-only")
        messages = build_messages(
            repo=repo,
            title=str(getattr(ticket, "title", "") or ""),
            body=str(getattr(ticket, "request", "") or ""),
            template=ref,
        )
        pv = _prompt_version(name, digest)
        body = _body(ep, messages)
        over = budget_refusal(
            ep,
            body,
            attempt_budget_usd=limits[0],
            max_input_tokens_cloud=limits[1],
            max_input_tokens_local=limits[2],
        )
        if over is not None:
            return fallback(over)
        wait = (timeout, watch, cold_load_allowance)
        if proxy is not None:
            with proxy.context(label=PROXY_LABEL, prompt_version=pv) as ctx:
                raw = _call(ctx.base_url, ep, body, pv, *wait)
        elif base_url is not None:
            if not rules._is_loopback(urlparse(base_url).hostname or ""):
                return fallback("base_url is not the local model proxy; refusing to send")
            raw = _call(base_url, ep, body, pv, *wait)
        else:
            return fallback("no model proxy given")
        subs = parse_answer(raw)
    except (SplitError, rules.RouterError) as exc:
        return fallback(str(exc))
    except Exception as exc:  # the split must never take the worker down
        return fallback(f"error ({type(exc).__name__})")
    return Proposal(
        sub_issues=subs,
        splitter=ep.name,
        prompt_name=name,
        prompt_hash=digest,
        raw=raw[:MAX_ANSWER_CHARS],
    )


def _body(ep: ModelEndpoint, messages: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "model": ep.name,
        "messages": messages,
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "stream": False,
    }


def _call(
    url: str,
    ep: ModelEndpoint,
    body: dict[str, Any],
    prompt_version: str | None,
    timeout: float,
    watch: rules.ModelWatch | None,
    cold_load_allowance: float,
) -> str:
    headers = {"content-type": "application/json"}
    if prompt_version:
        headers[rules.PROMPT_VERSION_HEADER] = prompt_version
    data = rules.post_classifier(
        url.rstrip("/") + "/chat/completions",
        body,
        headers,
        classifier=ep,
        timeout=timeout,
        watch=watch,
        cold_load_allowance=cold_load_allowance,
    )
    return rules._answer_text(data)


# --------------------------------------------------------------------------- the comment


def _fence_for(text: str) -> str:
    longest = max((len(m.group(0)) for m in _BACKTICKS.finditer(text)), default=0)
    return "`" * max(3, longest + 1)


def render(sub_issues: Iterable[SubIssue]) -> str:
    """The proposal as plain text (what sits inside the comment's code block)."""
    parts: list[str] = []
    for i, s in enumerate(sub_issues, 1):
        block = [f"{i}. {clean_title(s.title)}", ""]
        body = clean_body(s.body)
        if body:
            block += ["   " + line if line else "" for line in body.split("\n")]
            block.append("")
        files = [f for f in (clean_file(f) for f in s.files) if f][:MAX_FILES]
        block.append("   Files (rough guess): " + (", ".join(files) if files else "not known"))
        parts.append("\n".join(block))
    return "\n\n".join(parts)


def proposal_comment(ticket_id: int, proposal: Proposal) -> str:
    """The one issue comment proposing the split. The marker is its first line (so text
    inside the proposal can never be read as a marker); the model's text is cleaned and
    fenced (see the module docstring)."""
    text = render(proposal.sub_issues)
    fence = _fence_for(text)
    model = clean_title(proposal.splitter or "the tier 3 model")
    return "\n".join(
        [
            PROPOSAL_MARKER.format(ticket_id=int(ticket_id)),
            "This issue looks too large to do well in one change, so I have not started "
            f"on it. Here is a proposed split into {len(proposal.sub_issues)} smaller "
            f"issues (written by {model}; check it before using it):",
            "",
            fence + "text",
            text,
            fence,
            "",
            "Nothing has been created. To use the split, create the issues you want "
            "yourself and add the `agent` label to each: each one then runs as its own "
            "ticket. Labels the bot applies never start work.",
            "",
            "To have this issue done as one change instead, remove the `agent` label and "
            "add it again (or add a `tier:N` label first to choose the tier). I propose a "
            "split only once per issue.",
        ]
    )


def _same_login(a: str | None, b: str | None) -> bool:
    return a is not None and b is not None and a.lower() == b.lower()


def _order(c: Comment) -> tuple[datetime, int]:
    return (c.created_at, c.id)


def proposal_ticket(comment: Comment) -> int | None:
    """The ticket id in a proposal comment's first-line marker, or None."""
    first = (comment.body or "").split("\n", 1)[0].strip()
    m = _MARKER_LINE.match(first)
    return int(m.group(1)) if m else None


def find_proposal(comments: Iterable[Comment], bot_login: str) -> Comment | None:
    """The bot's earliest split proposal on the issue, or None. Only a comment by the
    bot whose first line is the marker counts; a user quoting it does not."""
    found = [
        c for c in comments if _same_login(c.author, bot_login) and proposal_ticket(c) is not None
    ]
    return min(found, key=_order) if found else None


def flag_reason(proposal: Proposal | None = None) -> str:
    """``split_proposed: ...`` for ``store.flag_ticket``; None: the count is not known (a
    proposal found on the issue after a crash)."""
    n = f"{len(proposal.sub_issues)} sub-issues" if proposal is not None else "sub-issues"
    return (
        f"{FLAG_SPLIT_PROPOSED}: {n} proposed on the issue; create and label the ones you "
        "want, or re-label this issue to run it as one"
    )


__all__ = [
    "DEFAULT_ENABLED",
    "DEFAULT_TIMEOUT",
    "FLAG_SPLIT_PROPOSED",
    "MAX_PARTS",
    "MAX_TOKENS",
    "MIN_PARTS",
    "PROPOSAL_MARKER",
    "SPLIT_PROMPT_NAME",
    "SPLIT_TIER",
    "Outcome",
    "Proposal",
    "Skipped",
    "SplitError",
    "SubIssue",
    "budget_refusal",
    "build_messages",
    "clean_body",
    "clean_file",
    "clean_title",
    "eligible",
    "find_proposal",
    "flag_reason",
    "load_split_template",
    "parse_answer",
    "propose",
    "proposal_comment",
    "proposal_ticket",
    "render",
    "skip_reason",
    "splitter_for",
]
