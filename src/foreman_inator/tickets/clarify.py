"""Ask before guessing: clarifying questions on a vague issue (todo #44).

Before a ticket is routed, a small local model reads the issue and says whether it can
be worked on as written. If it cannot (no way to tell what to change or what "done"
means), the worker posts at most three questions in one issue comment and adds the
``needs-info`` label; the ticket stays in ``todo``. Decisions (todo #44): one round per
ticket (invariant 7e), the label and the comment are the state (no schema change), the
ticket resumes when an allowed user comments or removes ``needs-info`` (invariant 7b),
and ``clarify = false`` in config turns the check off.

Everything here is a pure function or a single model call; nothing writes to GitHub or
the store. The worker's ``before_route`` seam (#21) wires the pieces:

* ``assess(ticket, ...) -> Clear | NeedsInfo``: one small local-model call through the
  model proxy (``proxy.context(label="clarify")``) inside a ``clarify`` span, with the
  prompt version (``clarify-v1@<hash[:8]>``) on the ``llm.call``. Same discipline as the
  router (#15): the issue is fenced, defused and bounded; the answer is parsed strictly;
  a cloud classifier is refused unless explicitly allowed (7c); there is a hard timeout;
  it never raises. Any failure, timeout or malformed answer is ``Clear``: asking is
  optional, a wrong guess costs one attempt, blocking forever costs the user.
* ``question_comment(ticket_id, questions)``: the comment body, with a hidden marker
  naming the ticket, so "already asked once" is read back from the issue's comments and
  survives restarts (``find_question``).
* ``answered(comments, asked, is_allowed)``: the Q&A text for the writer prompt: the
  bot's questions, then the allowed users' answers after them, in order, defused and
  clipped to ``QA_BUDGET``. Comments from the bot and from users not on the allowlist
  are ignored.
* ``status(...)``: from the issue's labels, label events and comments, which state the
  ticket is in: ``disabled`` / ``not_asked`` / ``waiting`` / ``answered`` / ``released``.

Questions are posted as a comment by the bot, so each one is cleaned first
(``clean_question``): one line, at most ``QUESTION_BUDGET`` characters, no ``@`` mentions,
no ``#123`` / ``GH-123`` cross-references, no links, no HTML (so no forged markers), no
control or invisible format characters, and no BEGIN/END fence look-alikes.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from string import Template
from typing import Any, Literal
from urllib.parse import urlparse

from fde_kit.prompts import PromptRef, load_prompt

from foreman_inator import tracing
from foreman_inator.config import Config, ModelEndpoint
from foreman_inator.github.client import Comment, IssueEvent
from foreman_inator.routing import rules
from foreman_inator.tickets.prompt import clip_head, defuse

log = logging.getLogger(__name__)

CLARIFY_PROMPT_NAME = "clarify-v1"
NEEDS_INFO_LABEL = "needs-info"
SPAN_NAME = "clarify"
PROXY_LABEL = "clarify"

MAX_QUESTIONS = 3
DEFAULT_TIMEOUT = 30.0  # seconds, the whole call; the runner passes router_timeout_seconds

# Budgets (characters).
TITLE_BUDGET = 300
ISSUE_BODY_BUDGET = 6_000  # vagueness is judged on the whole ask, so more than the router
QUESTION_BUDGET = 300  # per question, after cleaning
MAX_ANSWER_CHARS = 4_000  # a longer model answer is malformed, whatever it says
ANSWER_BUDGET = 2_000  # per user answer in the Q&A text
QA_BUDGET = 5_000  # the whole Q&A text
MAX_TOKENS = 300  # three short questions in JSON fit easily

QUESTION_MARKER = "<!-- foreman-inator:needs-info ticket={ticket_id} -->"
_HTML_COMMENT = re.compile(r"<!--.*?(?:-->|$)", re.DOTALL)
_QUESTION_LINE = re.compile(r"^([1-9])\. (.+)$", re.MULTILINE)

_SECTION = re.compile(r"^<!-- section: ([a-z_]+) -->$", re.MULTILINE)
_THINK = re.compile(r"^\s*<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"^```(?:json)?\s*\n(.*)\n```$", re.DOTALL | re.IGNORECASE)
_CLEAR = re.compile(r"^clear$", re.IGNORECASE)

_URL = re.compile(r"(?i)\b(?:[a-z][a-z0-9+.-]*://|www\.)\S+")
_ISSUE_REF = re.compile(r"#(?=\d)")
_GH_REF = re.compile(r"(?i)\bGH-(?=\d)")

State = Literal["disabled", "not_asked", "waiting", "answered", "released"]


# --------------------------------------------------------------------------- results


@dataclass(frozen=True, slots=True)
class Clear:
    """Go ahead as written. ``reason`` says why: the model's answer, or what went wrong
    (``clarify fallback: ...``), so a fallback is visible in logs and on the span."""

    reason: str
    prompt_name: str | None = None
    prompt_hash: str | None = None
    classifier: str | None = None
    raw: str | None = None  # the model's answer as sent (bounded), for audits

    @property
    def is_fallback(self) -> bool:
        return self.reason.startswith("clarify fallback:")

    @property
    def prompt_version(self) -> str | None:
        return _prompt_version(self.prompt_name, self.prompt_hash)


@dataclass(frozen=True, slots=True)
class NeedsInfo:
    """Ask first: 1 to ``MAX_QUESTIONS`` cleaned questions, safe to post as a comment."""

    questions: tuple[str, ...]
    prompt_name: str | None = None
    prompt_hash: str | None = None
    classifier: str | None = None
    raw: str | None = None

    def __post_init__(self) -> None:
        if not 1 <= len(self.questions) <= MAX_QUESTIONS:
            raise ValueError(f"NeedsInfo takes 1 to {MAX_QUESTIONS} questions")

    @property
    def prompt_version(self) -> str | None:
        return _prompt_version(self.prompt_name, self.prompt_hash)


Assessment = Clear | NeedsInfo


class ClarifyError(Exception):
    """The answer could not be used; the message goes into the fallback reason."""


def _prompt_version(name: str | None, digest: str | None) -> str | None:
    return f"{name}@{digest[:8]}" if name and digest else None


# --------------------------------------------------------------------------- template


def load_clarify_template(name: str = CLARIFY_PROMPT_NAME) -> PromptRef:
    """Load the clarify prompt from the package's ``prompts/`` folder (package data)."""
    folder = resources.files("foreman_inator") / "prompts"
    with resources.as_file(folder / f"{name}.md") as path:
        return load_prompt(str(path))


def _sections(text: str) -> dict[str, Template]:
    parts = _SECTION.split(text)
    out: dict[str, Template] = {}
    for name, body in zip(parts[1::2], parts[2::2], strict=True):
        if name in out:
            raise ValueError(f"clarify template has section {name!r} twice")
        out[name] = Template(body.strip("\n"))
    missing = {"system", "user"} - set(out)
    if missing:
        raise ValueError(f"clarify template lacks section(s) {sorted(missing)}")
    return out


def build_messages(
    *, repo: str, title: str, body: str, template: PromptRef
) -> list[dict[str, str]]:
    """The chat messages for one check: system rules, then the fenced issue."""
    s = _sections(template.text)
    user = s["user"].substitute(
        repo=defuse(clip_head(repo, TITLE_BUDGET)),
        issue_title=defuse(clip_head(title, TITLE_BUDGET).replace("\n", " ")),
        issue_body=defuse(clip_head(body, ISSUE_BODY_BUDGET)) or "(no description)",
    )
    return [
        {"role": "system", "content": s["system"].substitute()},
        {"role": "user", "content": user},
    ]


# --------------------------------------------------------------------------- cleaning


def clean_question(text: str) -> str:
    """One question made safe to post as the bot and to fence in a prompt: one line,
    no mentions, cross-references, links, HTML or invisible characters, bounded."""
    flat = " ".join(str(text).split())
    flat = "".join(ch for ch in flat if unicodedata.category(ch)[0] != "C")
    flat = _URL.sub("[link removed]", flat)
    flat = flat.replace("<", "&lt;").replace(">", "&gt;")
    flat = flat.replace("@", "＠")  # fullwidth at: readable, never a mention
    flat = _ISSUE_REF.sub("＃", flat)  # fullwidth number sign: no cross-reference
    flat = _GH_REF.sub("GH‑", flat)  # non-breaking hyphen: no GH-123 reference
    flat = defuse(flat).strip()
    if len(flat) > QUESTION_BUDGET:
        flat = flat[: QUESTION_BUDGET - 3].rstrip() + "..."
    return flat


# --------------------------------------------------------------------------- parsing


def parse_answer(text: Any) -> tuple[str, ...]:
    """The questions in a model answer; an empty tuple means clear. Accepted, and
    nothing else:

    * the word ``clear`` (any case, surrounding whitespace);
    * a JSON object with one key, ``questions``, a list of 0 to ``MAX_QUESTIONS``
      strings (an empty list is clear);
    * either of those inside one ```` ```json ```` fence, and/or after one complete
      leading ``<think>...</think>`` block.

    Questions are cleaned (``clean_question``), and empty or duplicate ones dropped.
    Raises ``ClarifyError`` on anything else, including more than ``MAX_QUESTIONS``
    questions (the prompt allows three; more is not an answer to it)."""
    if not isinstance(text, str):
        raise ClarifyError("model returned no text")
    if len(text) > MAX_ANSWER_CHARS * 4:
        raise ClarifyError(f"answer too long ({len(text)} chars)")
    answer = _THINK.sub("", text, count=1).strip()
    fenced = _FENCE.match(answer)
    if fenced:
        answer = fenced.group(1).strip()
    if not answer:
        raise ClarifyError("model returned an empty answer")
    if len(answer) > MAX_ANSWER_CHARS:
        raise ClarifyError(f"answer too long ({len(answer)} chars)")
    if _CLEAR.match(answer):
        return ()
    if not answer.startswith("{"):
        raise ClarifyError(f"unparseable answer {rules._show(answer)}")
    try:
        obj = json.loads(answer)
    except ValueError as exc:
        raise ClarifyError(f"unparseable answer {rules._show(answer)}") from exc
    if not (isinstance(obj, dict) and set(obj) == {"questions"}):
        raise ClarifyError(f"unexpected answer JSON {rules._show(answer)}")
    qs = obj["questions"]
    if not (isinstance(qs, list) and all(isinstance(q, str) for q in qs)):
        raise ClarifyError("questions is not a list of strings")
    if len(qs) > MAX_QUESTIONS:
        raise ClarifyError(f"{len(qs)} questions, more than {MAX_QUESTIONS}")
    out: list[str] = []
    for q in qs:
        c = clean_question(q)
        if c and c not in out:
            out.append(c)
    return tuple(out)


# --------------------------------------------------------------------------- assess


def assess(
    ticket: Any,
    *,
    config: Config | Iterable[ModelEndpoint],
    proxy: rules.ProxyLike | None = None,
    base_url: str | None = None,
    classifier: ModelEndpoint | None = None,
    allow_cloud_classifier: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    template: PromptRef | None = None,
) -> Assessment:
    """Can ``ticket`` (anything with ``title``, ``request``, ``repo``) be worked on as
    written? One model call, inside a ``clarify`` span. Never raises; any failure is
    ``Clear`` with a ``clarify fallback:`` reason. ``proxy`` / ``base_url`` /
    ``classifier`` / ``allow_cloud_classifier`` work as in ``routing.route``."""
    models = config.models if isinstance(config, Config) else tuple(config)
    with tracing.span(SPAN_NAME) as span:
        result = _assess(
            ticket,
            models=models,
            proxy=proxy,
            base_url=base_url,
            classifier=classifier,
            allow_cloud_classifier=allow_cloud_classifier,
            timeout=timeout,
            template=template,
        )
        attrs = {
            "clarify.result": "needs_info" if isinstance(result, NeedsInfo) else "clear",
            "clarify.reason": result.reason if isinstance(result, Clear) else None,
            "clarify.questions": len(result.questions) if isinstance(result, NeedsInfo) else 0,
            "clarify.classifier": result.classifier,
            "clarify.prompt_version": result.prompt_version,
        }
        for k, v in attrs.items():
            if v is not None:
                span.set_attribute(k, v)
    return result


def _assess(
    ticket: Any,
    *,
    models: tuple[ModelEndpoint, ...],
    proxy: rules.ProxyLike | None,
    base_url: str | None,
    classifier: ModelEndpoint | None,
    allow_cloud_classifier: bool,
    timeout: float,
    template: PromptRef | None,
) -> Assessment:
    name: str | None = None
    digest: str | None = None
    clf = classifier
    raw: str | None = None

    def fallback(why: str) -> Clear:
        log.warning("clarify fallback for ticket %s: %s", getattr(ticket, "id", "?"), why)
        return Clear(
            reason=f"clarify fallback: {why}; proceeding as written",
            prompt_name=name,
            prompt_hash=digest,
            classifier=clf.name if clf else None,
            raw=None if raw is None else raw[:MAX_ANSWER_CHARS],
        )

    try:
        ref = template or load_clarify_template()
        name, digest = ref.name, ref.sha256
        if clf is None:
            clf = rules.default_classifier(models)
        if clf is None:
            return fallback("no local model configured")
        if clf.is_cloud and not allow_cloud_classifier:
            return fallback(f"model {clf.name} is a cloud model; the check stays local (7c)")
        messages = build_messages(
            repo=str(getattr(ticket, "repo", "") or ""),
            title=str(getattr(ticket, "title", "") or ""),
            body=str(getattr(ticket, "request", "") or ""),
            template=ref,
        )
        pv = _prompt_version(name, digest)
        if proxy is not None:
            with proxy.context(label=PROXY_LABEL, prompt_version=pv) as ctx:
                raw = _call(ctx.base_url, clf, messages, pv, timeout)
        elif base_url is not None:
            if not rules._is_loopback(urlparse(base_url).hostname or ""):
                return fallback("base_url is not the local model proxy; refusing to send")
            raw = _call(base_url, clf, messages, pv, timeout)
        else:
            return fallback("no model proxy given")
        questions = parse_answer(raw)
    except (ClarifyError, rules.RouterError) as exc:
        return fallback(str(exc))
    except Exception as exc:  # the check must never take the worker down
        return fallback(f"error ({type(exc).__name__})")
    bounded = raw[:MAX_ANSWER_CHARS]
    if not questions:
        return Clear(
            reason=f"{name}: the issue can be worked on as written (model {clf.name})",
            prompt_name=name,
            prompt_hash=digest,
            classifier=clf.name,
            raw=bounded,
        )
    return NeedsInfo(
        questions=questions, prompt_name=name, prompt_hash=digest, classifier=clf.name, raw=bounded
    )


def _call(
    url: str,
    clf: ModelEndpoint,
    messages: list[dict[str, str]],
    prompt_version: str | None,
    timeout: float,
) -> str:
    body: dict[str, Any] = {
        "model": clf.name,
        "messages": messages,
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "stream": False,
    }
    if clf.kind == "local":
        body["reasoning_effort"] = "none"  # as the router: thinking off where possible
    headers = {"content-type": "application/json"}
    if prompt_version:
        headers[rules.PROMPT_VERSION_HEADER] = prompt_version
    data = rules._post_with_deadline(url.rstrip("/") + "/chat/completions", body, headers, timeout)
    return rules._answer_text(data)


# --------------------------------------------------------------------------- the comment


def question_comment(ticket_id: int, questions: Sequence[str]) -> str:
    """The issue comment that asks. The hidden marker names the ticket, so a later pass
    (or a restarted worker) finds it with ``find_question`` and never asks again."""
    qs = [c for c in (clean_question(q) for q in questions) if c]
    if not 1 <= len(qs) <= MAX_QUESTIONS:
        raise ValueError(f"a question comment takes 1 to {MAX_QUESTIONS} questions")
    lines = [
        QUESTION_MARKER.format(ticket_id=int(ticket_id)),
        "Before I start on this issue, I need a little more to go on:",
        "",
        *(f"{i}. {q}" for i, q in enumerate(qs, 1)),
        "",
        "Reply in a comment on this issue and I will start, using your answer. "
        f"Or remove the `{NEEDS_INFO_LABEL}` label to have me go ahead as written. "
        "I ask only once per ticket.",
    ]
    return "\n".join(lines)


def _same_login(a: str | None, b: str | None) -> bool:
    return a is not None and b is not None and a.lower() == b.lower()


def _order(c: Comment) -> tuple[datetime, int]:
    return (c.created_at, c.id)


def find_question(comments: Iterable[Comment], ticket_id: int, bot_login: str) -> Comment | None:
    """The bot's question comment for this ticket, or None. Only a comment by the bot
    with this ticket's marker counts; a user quoting the marker does not."""
    marker = QUESTION_MARKER.format(ticket_id=int(ticket_id))
    found = [c for c in comments if _same_login(c.author, bot_login) and marker in (c.body or "")]
    return min(found, key=_order) if found else None


def questions_in(comment: Comment) -> tuple[str, ...]:
    """The numbered questions in a question comment, as posted."""
    return tuple(m.group(2).strip() for m in _QUESTION_LINE.finditer(comment.body or ""))[
        :MAX_QUESTIONS
    ]


# --------------------------------------------------------------------------- answers


def _stamp(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _answer_body(c: Comment) -> str:
    """A reply's text without hidden HTML comments (no marker can ride along)."""
    return _HTML_COMMENT.sub("", c.body or "").strip()


def answers(
    comments: Iterable[Comment], asked: Comment, is_allowed: Callable[[str], bool]
) -> list[Comment]:
    """The comments that answer ``asked``: after it, by an allowed user, not by the bot
    that asked, not empty, oldest first."""
    out = []
    for c in comments:
        if _order(c) <= _order(asked) or c.author is None:
            continue
        if _same_login(c.author, asked.author) or not is_allowed(c.author):
            continue
        if not _answer_body(c):
            continue
        out.append(c)
    return sorted(out, key=_order)


def answered(
    comments: Iterable[Comment], asked: Comment, is_allowed: Callable[[str], bool]
) -> str | None:
    """The Q&A text for the writer prompt, or None when no allowed user has answered.

    ``asked`` is the bot's question comment (``find_question``); its author is the bot,
    whose comments never count as answers. ``is_allowed`` is the allowlist check
    (``config.is_allowed_user``, or the poller's ``_allowed``, which also rejects the
    bot). Each answer is clipped to ``ANSWER_BUDGET``, the whole to ``QA_BUDGET``, and
    everything is defused so it can sit inside the prompt's BEGIN/END fence."""
    replies = answers(comments, asked, is_allowed)
    if not replies:
        return None
    parts = ["Questions the worker asked on the issue before starting:"]
    parts += [f"{i}. {q}" for i, q in enumerate(questions_in(asked), 1)]
    parts += ["", "Answers from allowed users, oldest first:"]
    for c in replies:
        parts += [
            "",
            f"[{c.author}, {_stamp(c.created_at)}]",
            clip_head(_answer_body(c), ANSWER_BUDGET),
        ]
    return defuse(clip_head("\n".join(parts), QA_BUDGET))


# --------------------------------------------------------------------------- state


@dataclass(frozen=True, slots=True)
class Status:
    """Where a ticket stands on clarification, read from GitHub alone.

    ``state``: ``disabled`` (``clarify = false``) · ``not_asked`` (no question comment
    for this ticket: run ``assess``) · ``waiting`` (asked, no answer, not released) ·
    ``answered`` (an allowed user replied; ``qa`` holds the text) · ``released`` (an
    allowed user removed ``needs-info``: go ahead as written)."""

    state: State
    asked: Comment | None = None
    qa: str | None = None
    released_by: str | None = None
    label_present: bool = False

    @property
    def proceed(self) -> bool:
        """Route the ticket now."""
        return self.state in ("disabled", "answered", "released")

    @property
    def must_assess(self) -> bool:
        return self.state == "not_asked"

    @property
    def add_label(self) -> bool:
        """Waiting but the label is gone (a crash between comment and label, or removed
        by someone not on the allowlist, which does not count): put it back."""
        return self.state == "waiting" and not self.label_present

    @property
    def remove_label(self) -> bool:
        """Resumed by an answer: the worker removes ``needs-info`` itself."""
        return self.state == "answered" and self.label_present


def _has_label(labels: Iterable[str]) -> bool:
    return any(lb.lower() == NEEDS_INFO_LABEL for lb in labels)


def _last_label_event(events: Iterable[IssueEvent]) -> IssueEvent | None:
    mine = [
        e
        for e in events
        if e.label is not None
        and e.label.lower() == NEEDS_INFO_LABEL
        and e.event in ("labeled", "unlabeled")
    ]
    return max(mine, key=lambda e: (e.created_at, e.id)) if mine else None


def status(
    *,
    enabled: bool,
    ticket_id: int,
    labels: Iterable[str],
    events: Iterable[IssueEvent],
    comments: Iterable[Comment],
    bot_login: str,
    is_allowed: Callable[[str], bool],
) -> Status:
    """Which state the ticket is in (see ``Status``). Pure and idempotent: the same
    GitHub state always gives the same answer, so it is safe on every pass and after a
    restart. Invariant 7b: only an allowed user's comment answers, and only an allowed
    user's removal of ``needs-info`` releases; the bot's own removal never does."""
    if not enabled:
        return Status("disabled")
    comments = list(comments)
    present = _has_label(labels)
    asked = find_question(comments, ticket_id, bot_login)
    if asked is None:
        return Status("not_asked", label_present=present)
    qa = answered(comments, asked, lambda u: not _same_login(u, bot_login) and is_allowed(u))
    if qa is not None:
        return Status("answered", asked=asked, qa=qa, label_present=present)
    if not present:
        ev = _last_label_event(events)
        if (
            ev is not None
            and ev.event == "unlabeled"
            and ev.created_at >= asked.created_at
            and ev.actor is not None
            and not _same_login(ev.actor, bot_login)
            and is_allowed(ev.actor)
        ):
            return Status("released", asked=asked, released_by=ev.actor)
    return Status("waiting", asked=asked, label_present=present)


__all__ = [
    "ANSWER_BUDGET",
    "CLARIFY_PROMPT_NAME",
    "DEFAULT_TIMEOUT",
    "MAX_QUESTIONS",
    "NEEDS_INFO_LABEL",
    "QA_BUDGET",
    "QUESTION_BUDGET",
    "QUESTION_MARKER",
    "Assessment",
    "ClarifyError",
    "Clear",
    "NeedsInfo",
    "Status",
    "answered",
    "answers",
    "assess",
    "build_messages",
    "clean_question",
    "find_question",
    "load_clarify_template",
    "parse_answer",
    "question_comment",
    "questions_in",
    "status",
]
