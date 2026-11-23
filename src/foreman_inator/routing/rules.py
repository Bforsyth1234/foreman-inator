"""Router v1: pick a ticket's start tier by rules (todo #15, SPEC "Routing and escalation").

``route(ticket, config=..., proxy=...) -> Route`` decides the tier of a ticket's *first*
attempt only; escalation after that is #16's job. The order:

1. A ``tier:N`` label (N in 1-3) by an allowed user wins. No model call is made, and
   ``route_reason`` says which label and who applied it (``Override.from_event`` reads the
   poller's ``TicketRequested.tier_override`` / ``TierOverride`` events).
2. Otherwise a small local model classifies the issue as small / normal / large with
   ``prompts/router-v1.md``, mapped to tier 1 / 2 / 3.
3. Anything that goes wrong (no local classifier, a cloud classifier not explicitly
   allowed, an HTTP error, a timeout, an answer that is not exactly one of the three
   labels) gives tier 2 with a reason starting ``router fallback:`` and saying what
   happened. ``route`` never raises and never waits longer than ``timeout`` seconds.

Tiers with no model configured are skipped upward (tier 1 missing -> tier 2, ...). If no
tier at or above the wanted one has a model, the highest configured tier below it is used.
If no tier has a model at all, ``Route.has_model`` is False and the caller must flag the
ticket instead of starting an attempt (``Config`` already rejects such a config, so this
only happens with a hand-built model list). Every skip is written into the reason.

Invariant 7c: the classifier request goes only to the model proxy (a registered
``proxy.context(label="route")``, or a loopback ``base_url``), and a cloud classifier is
refused unless ``allow_cloud_classifier=True``. The proxy makes the call an ``llm.call``
span under the ``route`` span opened here, with the prompt version taken from the
``x-fman-prompt-version`` header (``router-v1@<hash[:8]>``) so #24 can tell router
variants apart.

The issue text is untrusted: it is fenced between BEGIN/END markers with look-alike
markers defused, and cut to a character budget. The answer is parsed strictly
(``parse_label``).

Later routers (#23 nearest neighbours) fall back to ``route`` below their threshold;
``resolve_tier`` and ``Override`` are shared so they skip tiers and honour overrides the
same way.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from importlib import resources
from string import Template
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

import httpx
from fde_kit.prompts import PromptRef, load_prompt

from foreman_inator import tracing
from foreman_inator.config import Config, ModelEndpoint
from foreman_inator.tickets import lifecycle
from foreman_inator.tickets.prompt import clip_head, defuse

log = logging.getLogger(__name__)

ROUTER_PROMPT_NAME = "router-v1"
LABEL_TIERS: Mapping[str, int] = {"small": 1, "normal": 2, "large": 3}
TIERS = (1, 2, 3)
FALLBACK_TIER = 2
DEFAULT_TIMEOUT = 10.0  # seconds, the whole classification call

# Budgets. A small local model needs only the gist of the issue to judge its size.
TITLE_BUDGET = 300
ISSUE_BODY_BUDGET = 4_000
MAX_ANSWER_CHARS = 200  # a longer answer is not one of the labels, whatever it says
MAX_RESPONSE_BYTES = 64 * 1024
MAX_TOKENS = 16
ERROR_DETAIL_BUDGET = 200

PROMPT_VERSION_HEADER = "x-fman-prompt-version"

Source = Literal["override", "classifier", "fallback"]

_SECTION = re.compile(r"^<!-- section: ([a-z_]+) -->$", re.MULTILINE)
_LABEL = re.compile(r"^(small|normal|large)$", re.IGNORECASE)
# Qwen-style models may put their thinking inline before the answer; one complete
# leading block is dropped, nothing else is.
_THINK = re.compile(r"^\s*<think>.*?</think>", re.DOTALL | re.IGNORECASE)


# --------------------------------------------------------------------------- types


@dataclass(frozen=True, slots=True)
class Override:
    """A ``tier:N`` label: the tier and the GitHub login that applied it (None if unknown)."""

    tier: int
    by: str | None = None

    def __post_init__(self) -> None:
        if self.tier not in TIERS:
            raise ValueError(f"tier override must be one of {TIERS}, not {self.tier!r}")

    @classmethod
    def from_event(cls, event: Any) -> Override | None:
        """From a poller event: ``TierOverride`` (tier + labeled_by) or ``TicketRequested``
        (``tier_override``; it does not carry who applied the tier label, so ``by`` is
        None). Anything else, or a tier outside 1-3, gives None."""
        if isinstance(event, lifecycle.TierOverride):
            tier, by = event.tier, (event.labeled_by or None)
        elif isinstance(event, lifecycle.TicketRequested):
            tier, by = event.tier_override, None
        else:
            return None
        if isinstance(tier, bool) or tier not in TIERS:
            return None
        return cls(int(tier), by)

    def describe(self) -> str:
        who = f"by {self.by}" if self.by else "by an allowed user"
        return f"tier:{self.tier} label {who}"


@dataclass(frozen=True, slots=True)
class Route:
    """The router's decision. #21 stores ``tier`` in ``tickets.routed_tier`` and ``reason``
    in ``tickets.route_reason``."""

    tier: int
    reason: str
    source: Source
    label: str | None = None  # parsed classifier label (small / normal / large)
    raw_label: str | None = None  # the classifier's answer as sent (bounded), for audits
    prompt_name: str | None = None  # None when no prompt was used (override)
    prompt_hash: str | None = None  # sha256 of router-v1.md (fde-kit prompt_hash)
    classifier: str | None = None  # config name of the model asked
    requested_tier: int | None = None  # before skipping tiers with no model
    has_model: bool = True  # False: no tier has a model; flag the ticket, do not start

    @property
    def prompt_version(self) -> str | None:
        """``router-v1@<hash[:8]>``, the form on the ``llm.call`` span and in variant ids."""
        return _prompt_version(self.prompt_name, self.prompt_hash)

    @property
    def is_fallback(self) -> bool:
        return self.source == "fallback"


class ProxyLike(Protocol):
    """What ``route`` needs from ``proxy.server.ModelProxy``."""

    def context(self, **kw: Any) -> Any: ...


class RouterError(Exception):
    """Classification failed; the message says how (it goes into the fallback reason)."""


def _prompt_version(name: str | None, digest: str | None) -> str | None:
    return f"{name}@{digest[:8]}" if name and digest else None


# --------------------------------------------------------------------------- template


def load_router_template(name: str = ROUTER_PROMPT_NAME) -> PromptRef:
    """Load a router prompt from the package's ``prompts/`` folder (package data)."""
    folder = resources.files("foreman_inator") / "prompts"
    with resources.as_file(folder / f"{name}.md") as path:
        return load_prompt(str(path))


def _sections(text: str) -> dict[str, Template]:
    parts = _SECTION.split(text)
    out: dict[str, Template] = {}
    for name, body in zip(parts[1::2], parts[2::2], strict=True):
        if name in out:
            raise ValueError(f"router template has section {name!r} twice")
        out[name] = Template(body.strip("\n"))
    missing = {"system", "user"} - set(out)
    if missing:
        raise ValueError(f"router template lacks section(s) {sorted(missing)}")
    return out


def build_messages(
    *, repo: str, title: str, body: str, template: PromptRef
) -> list[dict[str, str]]:
    """The chat messages for one classification: system rules, then the fenced issue."""
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


# --------------------------------------------------------------------------- parsing


def parse_label(text: Any) -> str:
    """The label in a classifier answer, lowercased. Accepted, and nothing else:

    * one of ``small`` / ``normal`` / ``large``, any case, surrounding whitespace;
    * a JSON object with one key, ``size``, whose value is such a word;
    * either of those after one complete leading ``<think>...</think>`` block.

    Raises ``RouterError`` otherwise (no prose, no punctuation, no lists, no guessing)."""
    if not isinstance(text, str):
        raise RouterError("classifier returned no text")
    if len(text) > MAX_ANSWER_CHARS * 20:
        raise RouterError(f"classifier answer too long ({len(text)} chars)")
    answer = _THINK.sub("", text, count=1).strip()
    if not answer:
        raise RouterError("classifier returned an empty answer")
    if len(answer) > MAX_ANSWER_CHARS:
        raise RouterError(f"classifier answer too long ({len(answer)} chars)")
    if answer.startswith("{"):
        try:
            obj = json.loads(answer)
        except ValueError as exc:
            raise RouterError(f"unparseable classifier answer {_show(answer)}") from exc
        if not (isinstance(obj, dict) and set(obj) == {"size"} and isinstance(obj["size"], str)):
            raise RouterError(f"unexpected classifier JSON {_show(answer)}")
        answer = obj["size"].strip()
    if not _LABEL.match(answer):
        raise RouterError(f"unparseable classifier answer {_show(answer)}")
    return answer.lower()


def _show(text: str) -> str:
    """A short, one-line, quoted view of untrusted text for a reason string."""
    flat = " ".join(text.split())
    if len(flat) > 60:
        flat = flat[:60] + "..."
    return json.dumps(flat, ensure_ascii=True)


# --------------------------------------------------------------------------- tiers


def configured_tiers(models: Config | Iterable[ModelEndpoint]) -> frozenset[int]:
    eps = models.models if isinstance(models, Config) else tuple(models)
    return frozenset(m.tier for m in eps if m.tier in TIERS)


def resolve_tier(wanted: int, tiers: Iterable[int]) -> tuple[int, str | None, bool]:
    """``(tier, note, has_model)``: ``wanted`` if it has a model, else the next configured
    tier up, else the highest configured tier below, else ``wanted`` with has_model False.
    ``note`` explains any change (None when there is none)."""
    have = set(tiers)
    if wanted in have:
        return wanted, None, True
    up = sorted(t for t in have if t > wanted)
    if up:
        return up[0], f"tier {wanted} has no model configured, using tier {up[0]}", True
    down = sorted((t for t in have if t < wanted), reverse=True)
    if down:
        return (
            down[0],
            f"no model configured at tier {wanted} or above, using tier {down[0]}",
            True,
        )
    return wanted, "no model is configured for any tier; flag the ticket", False


def default_classifier(models: Config | Iterable[ModelEndpoint]) -> ModelEndpoint | None:
    """The first local model of the lowest tier that has one (SPEC: "small local model")."""
    eps = models.models if isinstance(models, Config) else tuple(models)
    for tier in TIERS:
        for m in eps:
            if m.tier == tier and m.kind == "local":
                return m
    return None


# --------------------------------------------------------------------------- the call


def _is_loopback(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _post(url: str, body: dict[str, Any], headers: dict[str, str], timeout: float) -> Any:
    """POST JSON and return the decoded body; raises RouterError. Runs on a helper thread."""
    try:
        with (
            httpx.Client(timeout=httpx.Timeout(timeout), follow_redirects=False) as client,
            client.stream("POST", url, json=body, headers=headers) as resp,
        ):
            buf = bytearray()
            for chunk in resp.iter_bytes():
                buf.extend(chunk)
                if len(buf) > MAX_RESPONSE_BYTES:
                    raise RouterError(f"response larger than {MAX_RESPONSE_BYTES} bytes")
            status = resp.status_code
    except httpx.TimeoutException as exc:
        raise RouterError(f"timed out ({type(exc).__name__})") from exc
    except httpx.HTTPError as exc:
        raise RouterError(f"proxy unreachable ({type(exc).__name__})") from exc
    try:
        data = json.loads(bytes(buf))
    except ValueError:
        data = None
    if status >= 400:
        code = None
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            code = data["error"].get("code") or data["error"].get("type")
        detail = f" {clip_head(str(code), ERROR_DETAIL_BUDGET)}" if code else ""
        raise RouterError(f"HTTP {status}{detail}")
    if data is None:
        raise RouterError("response is not JSON")
    return data


def _post_with_deadline(
    url: str, body: dict[str, Any], headers: dict[str, str], timeout: float
) -> Any:
    """``_post`` with a hard wall-clock deadline: httpx's timeouts are per network phase,
    so a slow-dripping reply could otherwise take longer. A request still running at the
    deadline is abandoned on its daemon thread (its own timeouts end it)."""
    out: dict[str, Any] = {}

    def run() -> None:
        try:
            out["data"] = _post(url, body, headers, timeout)
        except RouterError as exc:
            out["error"] = exc
        except Exception as exc:  # never let the helper thread die silently
            out["error"] = RouterError(f"request failed ({type(exc).__name__})")

    t = threading.Thread(target=run, name="fman-route", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise RouterError(f"timed out after {timeout:g}s")
    if "error" in out:
        raise out["error"]
    return out["data"]


def _answer_text(data: Any) -> str:
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RouterError("response has no choices[0].message.content") from exc
    if not isinstance(content, str):
        raise RouterError("response content is not text")
    return content


def classify(
    *,
    url: str,
    classifier: ModelEndpoint,
    messages: list[dict[str, str]],
    prompt_version: str | None,
    timeout: float,
) -> str:
    """One chat-completions call to ``url`` (a proxy ``.../v1`` base); returns the raw
    answer text. Raises RouterError on anything but a 200 with a text answer."""
    body: dict[str, Any] = {
        "model": classifier.name,
        "messages": messages,
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "stream": False,
    }
    if classifier.kind == "local":
        body["reasoning_effort"] = "none"  # Ollama: turn thinking off where the model can
    headers = {"content-type": "application/json"}
    if prompt_version:
        headers[PROMPT_VERSION_HEADER] = prompt_version
    data = _post_with_deadline(url.rstrip("/") + "/chat/completions", body, headers, timeout)
    return _answer_text(data)


# --------------------------------------------------------------------------- route


def route(
    ticket: Any,
    *,
    config: Config | Iterable[ModelEndpoint],
    proxy: ProxyLike | None = None,
    base_url: str | None = None,
    override: Override | None = None,
    classifier: ModelEndpoint | None = None,
    allow_cloud_classifier: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    template: PromptRef | None = None,
) -> Route:
    """Pick the start tier for ``ticket`` (anything with ``title``, ``request``, ``repo``,
    e.g. ``db.store.Ticket``). Never raises; see the module docstring.

    ``proxy``: a ``ModelProxy``; the call goes through ``proxy.context(label="route")``
    opened inside the ``route`` span, so its ``llm.call`` is a child of ``route``. Or
    ``base_url``: an OpenAI base URL on loopback (the proxy's own ``.../v1``); anything
    else is refused. ``classifier`` defaults to ``default_classifier(config)``."""
    models = config.models if isinstance(config, Config) else tuple(config)
    tiers = configured_tiers(models)
    if override is not None:
        return _routed(
            Route(
                tier=override.tier,
                reason=f"override: {override.describe()}; router not asked",
                source="override",
                requested_tier=override.tier,
            ),
            tiers,
        )
    with tracing.route_span(FALLBACK_TIER, "routing") as span:
        result = _classify_route(
            ticket,
            models=models,
            tiers=tiers,
            proxy=proxy,
            base_url=base_url,
            classifier=classifier,
            allow_cloud_classifier=allow_cloud_classifier,
            timeout=timeout,
            template=template,
        )
        _annotate(span, result)
    return result


def _routed(r: Route, tiers: frozenset[int]) -> Route:
    """Apply tier skipping to ``r`` and, for an override, emit its ``route`` span."""
    tier, note, has_model = resolve_tier(r.requested_tier or r.tier, tiers)
    reason = r.reason if note is None else f"{r.reason}; {note}"
    out = Route(
        tier=tier,
        reason=reason,
        source=r.source,
        label=r.label,
        raw_label=r.raw_label,
        prompt_name=r.prompt_name,
        prompt_hash=r.prompt_hash,
        classifier=r.classifier,
        requested_tier=r.requested_tier,
        has_model=has_model,
    )
    if r.source == "override":
        with tracing.route_span(out.tier, out.reason) as span:
            _annotate(span, out)
    return out


def _annotate(span: Any, r: Route) -> None:
    attrs = {
        "route.tier": r.tier,
        "route.reason": r.reason,
        "route.source": r.source,
        "route.label": r.label,
        "route.classifier": r.classifier,
        "route.prompt_version": r.prompt_version,
        "route.requested_tier": r.requested_tier,
        "route.has_model": r.has_model,
    }
    for k, v in attrs.items():
        if v is not None:
            span.set_attribute(k, v)


def _classify_route(
    ticket: Any,
    *,
    models: tuple[ModelEndpoint, ...],
    tiers: frozenset[int],
    proxy: ProxyLike | None,
    base_url: str | None,
    classifier: ModelEndpoint | None,
    allow_cloud_classifier: bool,
    timeout: float,
    template: PromptRef | None,
) -> Route:
    name: str | None = None
    digest: str | None = None
    clf = classifier
    raw: str | None = None

    def fallback(why: str) -> Route:
        log.warning("router fallback for ticket %s: %s", getattr(ticket, "id", "?"), why)
        return _routed(
            Route(
                tier=FALLBACK_TIER,
                reason=f"router fallback: {why}; using tier {FALLBACK_TIER}",
                source="fallback",
                raw_label=None if raw is None else raw[:MAX_ANSWER_CHARS],
                prompt_name=name,
                prompt_hash=digest,
                classifier=clf.name if clf else None,
                requested_tier=FALLBACK_TIER,
            ),
            tiers,
        )

    try:
        ref = template or load_router_template()
        name, digest = ref.name, ref.sha256
        if clf is None:
            clf = default_classifier(models)
        if clf is None:
            return fallback("no local model configured to classify with")
        if clf.is_cloud and not allow_cloud_classifier:
            return fallback(
                f"classifier {clf.name} is a cloud model; classification stays local (7c)"
            )
        messages = build_messages(
            repo=str(getattr(ticket, "repo", "") or ""),
            title=str(getattr(ticket, "title", "") or ""),
            body=str(getattr(ticket, "request", "") or ""),
            template=ref,
        )
        pv = _prompt_version(name, digest)
        if proxy is not None:
            with proxy.context(label="route", prompt_version=pv) as ctx:
                raw = classify(
                    url=ctx.base_url,
                    classifier=clf,
                    messages=messages,
                    prompt_version=pv,
                    timeout=timeout,
                )
        elif base_url is not None:
            host = urlparse(base_url).hostname or ""
            if not _is_loopback(host):
                return fallback("base_url is not the local model proxy; refusing to send")
            raw = classify(
                url=base_url, classifier=clf, messages=messages, prompt_version=pv, timeout=timeout
            )
        else:
            return fallback("no model proxy given")
        label = parse_label(raw)
    except RouterError as exc:
        return fallback(str(exc))
    except Exception as exc:  # routing must never take the worker down
        return fallback(f"router error ({type(exc).__name__})")
    tier = LABEL_TIERS[label]
    return _routed(
        Route(
            tier=tier,
            reason=f"rules: {name} classified the issue as {label} -> tier {tier} "
            f"(classifier {clf.name})",
            source="classifier",
            label=label,
            raw_label=raw[:MAX_ANSWER_CHARS],
            prompt_name=name,
            prompt_hash=digest,
            classifier=clf.name,
            requested_tier=tier,
        ),
        tiers,
    )


__all__ = [
    "DEFAULT_TIMEOUT",
    "FALLBACK_TIER",
    "LABEL_TIERS",
    "PROMPT_VERSION_HEADER",
    "ROUTER_PROMPT_NAME",
    "Override",
    "Route",
    "RouterError",
    "build_messages",
    "classify",
    "configured_tiers",
    "default_classifier",
    "load_router_template",
    "parse_label",
    "resolve_tier",
    "route",
]
