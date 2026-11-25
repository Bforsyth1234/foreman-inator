"""The tiers page: models and providers per tier (todo #74, GH #63).

``routes(store_path)`` adds to the metrics app:

* ``GET /tiers``: one card per tier listing its endpoints in try order (each with Up,
  Down, Edit, Remove and Test), the endpoint form, the reviewer order
  (``reviewer_models`` and ``reviewer_order``) and the warnings below. ``?preset=NAME``
  fills the form from a provider preset (``PRESETS``: Ollama on this Mac, GLM / Z.ai,
  Anthropic with ``wire = "anthropic"``, any OpenAI-compatible URL) and ``?edit=NAME``
  from a configured endpoint: plain links, no JavaScript. GETs never write;
* ``POST /tiers/endpoint`` adds or replaces an endpoint (``original`` names the one being
  edited; a rename follows into ``reviewer_models``); ``/tiers/endpoint/remove``,
  ``/tiers/endpoint/move`` (``dir`` up or down inside its tier) and
  ``/tiers/endpoint/test``;
* ``POST /tiers/reviewers`` with ``action`` add / remove / up / down (``name``) or order
  (``reviewer_order``).

Shared rules for pages that write (todo.md, above #73): every POST is a
``forms.FormGuard`` route (CSRF token, own loopback Origin/Host, 403 and no change
otherwise). Each save builds the new ``Config``, validates it with ``config.from_dict(
config.to_dict(...))`` (the exact checks ``config.load`` runs), then writes it with
``config.save`` and reads it back with ``config.load``; a refused change leaves the file
as it was and shows the same message ``config.load`` gives. API keys: the form's key
field is a password input with no value; a key typed there goes straight to
``config.set_key`` (what ``fman config set-key`` calls) after validation passes and before
the config is written, and the page only ever says "key set" / "no key"
(``keyring.get_password`` is truthy), never the value. Everything from the config, the
database or a provider is HTML-escaped.

Warnings (shown, never enforced): a writer whose reviewer pick
(``review.reviewer.pick_reviewer`` with ``reviewer_order``) finds no model of another
family (invariant 7f; its PRs would go to the user unreviewed), and a local-only repo
(#49, ``Store.local_only_repos``) when the ladder has no local model left
(``escalation.local_rungs``).

Test button: one tiny non-streaming ``chat/completions`` request (``max_tokens`` =
``PROBE_MAX_TOKENS``) through a ``proxy.server.ModelProxy`` exactly as the worker builds
it (the endpoint, the Keychain key resolver, the spend hook over the database, the shared
local-slot cap), the path the reviewer uses (``/c/<token>/v1``, a registered context
labelled ``tiers_test``). A non-200 is reported as an error with
``review.reviewer.refusal_text`` (the provider's message, scrubbed); a cloud test needs
the database so the spend caps apply (invariant 7c).
"""

from __future__ import annotations

import html
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import BaseRoute, Route

from foreman_inator import config as config_mod
from foreman_inator.config import Config, ConfigError, ModelEndpoint
from foreman_inator.metrics import forms, web

PATH = "/tiers"
#: The page's nav entry (the wiring step adds it to ``web.full_app``'s nav).
NAV_ENTRY = (PATH, "Tiers")
PROBE_MAX_TOKENS = 8
PROBE_TIMEOUT_S = 60.0
PROBE_PROMPT = "Reply with the single word: ok"
PROBE_REPLY_LIMIT = 200

e = html.escape


@dataclass(frozen=True)
class Preset:
    """What a provider preset fills into the endpoint form."""

    label: str
    kind: str
    base_url: str
    wire: str
    family: str
    model: str = ""
    key_name: str = ""


PRESETS: dict[str, Preset] = {
    "ollama": Preset("Ollama (this Mac)", "local", config_mod.OLLAMA_URL, "openai", ""),
    "glm": Preset("GLM / Z.ai", "cloud", config_mod.GLM_URL, "openai", "glm", "glm-5.3", "glm"),
    "anthropic": Preset(
        "Anthropic", "cloud", config_mod.ANTHROPIC_URL, "anthropic", "claude",
        "claude-opus-5-5", "anthropic",
    ),
    "openai": Preset("OpenAI-compatible URL", "cloud", "https://", "openai", ""),
}  # fmt: skip


@dataclass(frozen=True)
class Probe:
    """A test call's result: ``ok`` and the reply, or the error text."""

    ok: bool
    text: str


#: ``probe(config, endpoint)``: one tiny call through the proxy (tests pass fakes).
Prober = Callable[[Config, ModelEndpoint], Probe]


class _Fail(Exception):
    def __init__(self, text: str, status: int = 400) -> None:
        super().__init__(text)
        self.status = status


# =========================================================================== keys


def key_is_set(ref: str | None) -> bool:
    """Whether the Keychain holds the item ``ref`` names. Reads only presence."""
    if not ref:
        return False
    try:
        name = config_mod.parse_key_ref(ref)
        return bool(config_mod.keyring.get_password(config_mod.KEYCHAIN_SERVICE, name))
    except Exception:  # noqa: BLE001 - a locked or missing Keychain reads as "no key"
        return False


def _key_label(ref: str | None) -> str:
    if not ref:
        return "no key needed" if ref is None else "no key"
    name = ref.removeprefix(config_mod.KEY_REF_PREFIX)
    return f"{'key set' if key_is_set(ref) else 'no key'} ({name})"


# =========================================================================== warnings


def tier_warnings(cfg: Config, local_only_repos: Iterable[str] = ()) -> list[str]:
    """The page's warnings for ``cfg`` (see the module docstring)."""
    from foreman_inator.review.reviewer import NoIndependentReviewer, pick_reviewer
    from foreman_inator.routing import escalation

    out: list[str] = []
    for tier in config_mod.TIERS:
        for w in cfg.models_for_tier(tier):
            try:
                pick_reviewer(cfg, w, order=cfg.reviewer_order)
            except (NoIndependentReviewer, KeyError):
                out.append(
                    f"Tier {tier}: {w.name} (family {w.family}) has no reviewer from another"
                    " family, so its PRs would come to you unreviewed (invariant 7f). Add a"
                    " model of another family to the reviewer order."
                )
    local_only = sorted(set(local_only_repos))
    if local_only and not escalation.local_rungs(escalation.build_ladder(cfg.models)):
        out.append(
            f"No local model is left in any tier, so the local-only repos"
            f" ({', '.join(local_only)}) would have no tier to run on: their tickets would"
            " be handed to you. Add a local endpoint."
        )
    return out


# =========================================================================== edits


def validated(cfg: Config) -> Config:
    """``cfg`` through ``config.load``'s own checks. Raises ``ConfigError``."""
    return config_mod.from_dict(config_mod.to_dict(cfg))


def _find(cfg: Config, name: str) -> ModelEndpoint:
    for m in cfg.models:
        if m.name == name:
            return m
    raise _Fail(f"no endpoint named {name!r}")


def endpoint_from_form(form: Mapping[str, str]) -> ModelEndpoint:
    """The endpoint the form describes (``api_key_ref`` from ``key_name``). Raises
    ``ConfigError``/``_Fail`` on a malformed field; the rest is ``validated``'s job."""
    tier_text = form.get("tier", "").strip()
    if not tier_text.isdigit():
        raise _Fail("tier must be 1, 2 or 3")
    key_name = form.get("key_name", "").strip()
    return ModelEndpoint(
        name=form.get("name", "").strip(),
        tier=int(tier_text),
        kind=form.get("kind", "").strip(),  # type: ignore[arg-type]
        model=form.get("model", "").strip(),
        base_url=form.get("base_url", "").strip(),
        family=form.get("family", "").strip(),
        api_key_ref=config_mod.key_ref(key_name) if key_name else None,
        wire=form.get("wire", "openai").strip() or "openai",  # type: ignore[arg-type]
        vision=form.get("vision") == "1",
    )


def put_endpoint(cfg: Config, ep: ModelEndpoint, original: str = "") -> Config:
    """``cfg`` with ``ep`` added (at the end of its tier) or replacing ``original`` in
    place; a rename follows into ``reviewer_models``."""
    if not ep.name:
        raise _Fail("name is required")
    models = list(cfg.models)
    reviewers = cfg.reviewer_models
    if original:
        old = _find(cfg, original)
        if ep.name != original and any(m.name == ep.name for m in models):
            raise _Fail(f"an endpoint named {ep.name!r} already exists")
        models[models.index(old)] = ep
        reviewers = tuple(ep.name if r == original else r for r in reviewers)
    else:
        if any(m.name == ep.name for m in models):
            raise _Fail(f"an endpoint named {ep.name!r} already exists; edit it instead")
        models.append(ep)
    return replace(cfg, models=tuple(models), reviewer_models=reviewers)


def remove_endpoint(cfg: Config, name: str) -> Config:
    """``cfg`` without ``name`` (and without it in ``reviewer_models``)."""
    ep = _find(cfg, name)
    return replace(
        cfg,
        models=tuple(m for m in cfg.models if m is not ep),
        reviewer_models=tuple(r for r in cfg.reviewer_models if r != name),
    )


def move_endpoint(cfg: Config, name: str, direction: str) -> Config:
    """``cfg`` with ``name`` swapped with its neighbour of the same tier (try order)."""
    ep = _find(cfg, name)
    models = list(cfg.models)
    same = [i for i, m in enumerate(models) if m.tier == ep.tier]
    pos = same.index(models.index(ep))
    if direction not in ("up", "down"):
        raise _Fail("dir must be up or down")
    other = pos - 1 if direction == "up" else pos + 1
    if not 0 <= other < len(same):
        return cfg
    a, b = same[pos], same[other]
    models[a], models[b] = models[b], models[a]
    return replace(cfg, models=tuple(models))


def edit_reviewers(cfg: Config, action: str, name: str = "", order: str = "") -> Config:
    """``cfg`` with ``reviewer_models`` / ``reviewer_order`` changed by ``action``."""
    rev = list(cfg.reviewer_models)
    if action == "order":
        return replace(cfg, reviewer_order=order.strip())
    if action == "add":
        _find(cfg, name)
        if name in rev:
            raise _Fail(f"{name} is already a reviewer")
        return replace(cfg, reviewer_models=(*rev, name))
    if name not in rev:
        raise _Fail(f"{name!r} is not in the reviewer order")
    i = rev.index(name)
    if action == "remove":
        del rev[i]
    elif action in ("up", "down"):
        j = i - 1 if action == "up" else i + 1
        if 0 <= j < len(rev):
            rev[i], rev[j] = rev[j], rev[i]
    else:
        raise _Fail(f"unknown action {action!r}")
    return replace(cfg, reviewer_models=tuple(rev))


# =========================================================================== test call


def probe_endpoint(
    cfg: Config,
    ep: ModelEndpoint,
    *,
    store_path: str | Path | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    key_resolver: Callable[[ModelEndpoint], str | None] | None = None,
    timeout: float = PROBE_TIMEOUT_S,
) -> Probe:
    """One tiny ``chat/completions`` call to ``ep`` through a started ``ModelProxy`` built
    as the worker builds it. A cloud endpoint needs the database (``store_path``) so the
    spend caps apply; the call is costed to the live bucket like any other."""
    from foreman_inator.db.store import Store
    from foreman_inator.proxy.server import ModelProxy, shared_local_slots
    from foreman_inator.review.reviewer import refusal_text
    from foreman_inator.spend import Spend

    store = None
    db = Path(store_path) if store_path is not None else None
    if db is not None and db.exists():
        store = Store(db)
    elif ep.is_cloud:
        return Probe(False, "No database yet: run the worker once so the spend caps apply.")
    kw: dict[str, Any] = {"local_slots": shared_local_slots(int(cfg.max_local_inference))}
    if store is not None:
        kw["spend_hook"] = Spend(store, cfg).hook
    if transport is not None:
        kw["transport"] = transport
    if key_resolver is not None:
        kw["key_resolver"] = key_resolver
    body = {
        "model": ep.name,
        "messages": [{"role": "user", "content": PROBE_PROMPT}],
        "stream": False,
        "max_tokens": PROBE_MAX_TOKENS,
    }
    try:
        proxy = ModelProxy([ep], store=store, **kw).start()
        try:
            with (
                proxy.context(label="tiers_test") as ctx,
                httpx.Client(timeout=timeout) as client,
            ):
                resp = client.post(f"{ctx.base_url}/chat/completions", json=body)
        finally:
            proxy.stop()
    except httpx.HTTPError as exc:
        return Probe(False, f"the call failed: {type(exc).__name__}")
    finally:
        if store is not None:
            store.close()
    if resp.status_code != 200:
        return Probe(False, refusal_text(resp))
    try:
        text = resp.json()["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError):
        return Probe(False, "the reply is not a chat completion")
    from foreman_inator.review.reviewer import provider_text

    return Probe(True, provider_text(str(text), PROBE_REPLY_LIMIT) or "(empty reply)")


# =========================================================================== rendering


def _message(text: str, error: bool = False) -> str:
    cls = ' class="error"' if error else ""
    return f"<p{cls}>{e(text)}</p>"


def _hidden(name: str, value: str) -> str:
    return f'<input type="hidden" name="{e(name)}" value="{e(value)}">'


def _endpoint_row(guard: forms.FormGuard, m: ModelEndpoint, first: bool, last: bool) -> str:
    name = _hidden("name", m.name)
    flags = [m.kind, f"wire {m.wire}", f"family {m.family}"]
    if m.vision:
        flags.append("vision")
    up = "" if first else guard.form(f"{PATH}/endpoint/move", name + _hidden("dir", "up"),
                                     button="Up", cls="inline")  # fmt: skip
    down = "" if last else guard.form(f"{PATH}/endpoint/move", name + _hidden("dir", "down"),
                                      button="Down", cls="inline")  # fmt: skip
    return (
        f"<tr><td><code>{e(m.name)}</code></td><td><code>{e(m.model)}</code></td>"
        f"<td>{e(m.base_url)}</td><td>{e(', '.join(flags))}</td>"
        f"<td>{e(_key_label(m.api_key_ref))}</td><td>{up}{down}"
        f'<a href="{PATH}?edit={quote(m.name)}#endpoint">Edit</a> '
        + guard.form(f"{PATH}/endpoint/remove", name, button="Remove", cls="inline")
        + guard.form(f"{PATH}/endpoint/test", name, button="Test", cls="inline")
        + "</td></tr>"
    )


def _tier_cards(guard: forms.FormGuard, cfg: Config) -> str:
    out = []
    for tier in config_mod.TIERS:
        ms = cfg.models_for_tier(tier)
        rows = "".join(_endpoint_row(guard, m, i == 0, i == len(ms) - 1) for i, m in enumerate(ms))
        table = (
            "<table><tr><th>name</th><th>model</th><th>base URL</th><th>kind</th>"
            f"<th>API key</th><th></th></tr>{rows}</table>"
            if ms
            else "<p>No endpoints.</p>"
        )
        out.append(f'<section class="card"><h2>Tier {tier}</h2>{table}</section>')
    return "".join(out)


def _input(name: str, label: str, value: str, typ: str = "text") -> str:
    return f'<label>{e(label)} <input type="{typ}" name="{e(name)}" value="{e(value)}"></label> '


def _choice(name: str, label: str, options: Sequence[str], current: str) -> str:
    opts = "".join(
        f'<option value="{e(o)}"{" selected" if o == current else ""}>{e(o)}</option>'
        for o in options
    )
    return f'<label>{e(label)} <select name="{e(name)}">{opts}</select></label> '


def _endpoint_form(guard: forms.FormGuard, cfg: Config, request: Request) -> str:
    preset_name = request.query_params.get("preset", "")
    editing = request.query_params.get("edit", "")
    v: dict[str, str] = {
        "name": "",
        "tier": "1",
        "kind": "local",
        "model": "",
        "base_url": "",
        "family": "",
        "wire": "openai",
        "key_name": "",
    }
    vision = False
    key_ref: str | None = None
    title = "Add an endpoint"
    if editing:
        m = next((x for x in cfg.models if x.name == editing), None)
        if m is not None:
            title = f"Edit {m.name}"
            key_name = (
                m.api_key_ref.removeprefix(config_mod.KEY_REF_PREFIX) if m.api_key_ref else ""
            )
            v.update(
                name=m.name,
                tier=str(m.tier),
                kind=m.kind,
                model=m.model,
                base_url=m.base_url,
                family=m.family,
                wire=m.wire,
                key_name=key_name,
            )
            vision, key_ref = m.vision, m.api_key_ref
        else:
            editing = ""
    elif preset_name in PRESETS:
        p = PRESETS[preset_name]
        v.update(kind=p.kind, base_url=p.base_url, wire=p.wire, family=p.family,
                 model=p.model, key_name=p.key_name, name=p.key_name)  # fmt: skip
        title = f"Add an endpoint: {p.label}"
    links = " · ".join(
        f'<a href="{PATH}?preset={k}#endpoint">{e(p.label)}</a>' for k, p in PRESETS.items()
    )
    key_note = f"Now: {_key_label(key_ref)}. Leave blank to keep it." if key_ref else "Optional."
    inner = (
        _hidden("original", editing)
        + _input("name", "Name", v["name"])
        + _choice("tier", "Tier", [str(t) for t in config_mod.TIERS], v["tier"])
        + _choice("kind", "Kind", ["local", "cloud"], v["kind"])
        + _input("model", "Model id", v["model"])
        + _input("base_url", "Base URL", v["base_url"])
        + _choice("wire", "Wire", list(config_mod.WIRES), v["wire"])
        + _input("family", "Family", v["family"])
        + '<label><input type="checkbox" name="vision" value="1"'
        + (" checked" if vision else "")
        + "> Takes images (vision)</label> "
        + _input("key_name", "Keychain item", v["key_name"])
        # The key field never carries a value attribute: the page never shows a key.
        + '<label>API key <input type="password" name="api_key" autocomplete="off"></label> '
        + f"<small>{e(key_note)} A key typed here goes to the Keychain, not config.toml."
        + "</small> "
    )
    return (
        f'<section class="card" id="endpoint"><h2>{e(title)}</h2>'
        f"<p>Presets: {links}</p>"
        + guard.form(f"{PATH}/endpoint", inner, button="Save endpoint")
        + "</section>"
    )


def _reviewer_section(guard: forms.FormGuard, cfg: Config) -> str:
    from foreman_inator.review.reviewer import REVIEWER_ORDERS

    def act(action: str, name: str, button: str) -> str:
        inner = _hidden("action", action) + _hidden("name", name)
        return guard.form(f"{PATH}/reviewers", inner, button=button, cls="inline")

    rows = []
    rev = cfg.reviewer_models
    for i, name in enumerate(rev):
        fam = next((m.family for m in cfg.models if m.name == name), "?")
        rows.append(
            f"<li><code>{e(name)}</code> (family {e(fam)}) "
            + ("" if i == 0 else act("up", name, "Up"))
            + ("" if i == len(rev) - 1 else act("down", name, "Down"))
            + act("remove", name, "Remove")
            + "</li>"
        )
    others = [m.name for m in cfg.models if m.name not in rev]
    add = (
        guard.form(
            f"{PATH}/reviewers",
            _hidden("action", "add") + _choice("name", "Add reviewer", others, ""),
            button="Add",
        )
        if others
        else ""
    )
    order = guard.form(
        f"{PATH}/reviewers",
        _hidden("action", "order")
        + _choice("reviewer_order", "Reviewer order", list(REVIEWER_ORDERS), cfg.reviewer_order),
        button="Save order",
    )
    return (
        '<section class="card"><h2>Reviewers</h2>'
        "<p><code>configured</code>: the first model in this list of another family than"
        " the writer. <code>cheapest</code>: the cheapest model of another family.</p>"
        f"<ol>{''.join(rows)}</ol>{add}{order}</section>"
    )


def render_tiers(
    guard: forms.FormGuard,
    cfg: Config,
    request: Request,
    *,
    local_only: Iterable[str] = (),
    message: str = "",
    nav: Sequence[tuple[str, str]] = (),
) -> str:
    warns = "".join(f'<p class="error">Warning: {e(w)}</p>' for w in tier_warnings(cfg, local_only))
    body = (
        "<style>form.inline{display:inline;margin-right:.3em}"
        " section.card{margin-bottom:1.5em}</style><h1>Tiers</h1>"
        '<p class="lede">Each tier tries its endpoints in order. Changes are saved to'
        " config.toml with the same checks as the CLI; the worker reads them on its next"
        " start or config reload.</p>"
        + message
        + warns
        + _tier_cards(guard, cfg)
        + _endpoint_form(guard, cfg, request)
        + _reviewer_section(guard, cfg)
    )
    return web.render_page("Tiers", body, nav=nav)


# =========================================================================== routes


def routes(
    store_path: str | Path,
    *,
    config_path: Path | None = None,
    guard: forms.FormGuard | None = None,
    probe: Prober | None = None,
    store_key: Callable[[str, str], str] | None = None,
    nav: Sequence[tuple[str, str]] | None = None,
) -> list[BaseRoute]:
    """The page's routes. ``config_path`` defaults to ``paths.config_path()`` (read with
    ``config.load`` on every request); ``guard`` to ``forms.default_guard()``; ``probe``
    to ``probe_endpoint`` over ``store_path``; ``store_key`` to ``config.set_key``."""
    db = Path(store_path)
    guard = guard or forms.default_guard()
    nav = tuple(nav) if nav is not None else (*web.M4_NAV, NAV_ENTRY)
    set_key = store_key or config_mod.set_key

    def prober(cfg: Config, ep: ModelEndpoint) -> Probe:
        return probe_endpoint(cfg, ep, store_path=db)

    run_probe = probe or prober

    def load() -> Config:
        try:
            return config_mod.load(config_path)
        except ConfigError as exc:
            raise _Fail(f"config.toml: {exc}", 500) from None

    def local_only() -> frozenset[str]:
        try:
            store = web.open_store(db)
        except (FileNotFoundError, sqlite3.Error):
            return frozenset()
        try:
            return store.local_only_repos()
        finally:
            store.close()

    def page(request: Request, cfg: Config, message: str = "", status: int = 200) -> Response:
        html_ = render_tiers(guard, cfg, request, local_only=local_only(), message=message,
                             nav=nav)  # fmt: skip
        return HTMLResponse(html_, status)

    def save(new: Config) -> Config:
        try:
            checked = validated(new)
        except ConfigError as exc:
            raise _Fail(str(exc)) from None
        config_mod.save(checked, config_path)
        return config_mod.load(config_path)

    def guarded(fn: Callable[[Request, dict[str, str]], tuple[Config, str]]) -> Any:
        def run(request: Request, form: dict[str, str]) -> Response:
            try:
                try:
                    cfg, text = fn(request, form)
                except ConfigError as exc:  # Config() validates as config.load does
                    raise _Fail(str(exc)) from None
            except _Fail as f:
                try:
                    cfg = load()
                except _Fail as g:
                    body = f"<h1>Tiers</h1>{_message(str(g), True)}"
                    return HTMLResponse(web.render_page("Tiers", body, nav=nav), g.status)
                return page(request, cfg, _message(f"Not saved: {f}", True), f.status)
            return page(request, cfg, _message(text))

        return run

    def get(request: Request) -> Response:
        try:
            cfg = load()
        except _Fail as f:
            body = f"<h1>Tiers</h1>{_message(str(f), True)}"
            return HTMLResponse(web.render_page("Tiers", body, nav=nav), f.status)
        return page(request, cfg)

    @guarded
    def put(request: Request, form: dict[str, str]) -> tuple[Config, str]:
        cfg = load()
        original = form.get("original", "").strip()
        try:
            ep = endpoint_from_form(form)
        except ConfigError as exc:
            raise _Fail(str(exc)) from None
        key = form.get("api_key", "").strip()
        if key and ep.api_key_ref is None:
            raise _Fail("name the Keychain item the key is stored as")
        new = put_endpoint(cfg, ep, original)
        try:
            validated(new)
        except ConfigError as exc:
            raise _Fail(str(exc)) from None
        if key:
            try:
                set_key(config_mod.parse_key_ref(ep.api_key_ref or ""), key)
            except ConfigError as exc:
                raise _Fail(str(exc)) from None
            except Exception:  # noqa: BLE001 - never echo what the Keychain said about a key
                raise _Fail("the Keychain refused the key", 500) from None
        saved = save(new)
        what = "Updated" if original else "Added"
        stored = " Its key is in the Keychain." if key else ""
        return saved, f"{what} {ep.name} (tier {ep.tier}).{stored}"

    @guarded
    def remove(request: Request, form: dict[str, str]) -> tuple[Config, str]:
        name = form.get("name", "")
        return save(remove_endpoint(load(), name)), f"Removed {name}."

    @guarded
    def move(request: Request, form: dict[str, str]) -> tuple[Config, str]:
        name = form.get("name", "")
        return save(move_endpoint(load(), name, form.get("dir", ""))), f"Moved {name}."

    @guarded
    def reviewers(request: Request, form: dict[str, str]) -> tuple[Config, str]:
        action = form.get("action", "")
        new = edit_reviewers(load(), action, form.get("name", ""), form.get("reviewer_order", ""))
        return save(new), "Reviewers saved."

    def test(request: Request, form: dict[str, str]) -> Response:
        try:
            cfg = load()
            ep = _find(cfg, form.get("name", ""))
        except _Fail as f:
            return HTMLResponse(web.render_page("Tiers", _message(str(f), True), nav=nav),
                                f.status)  # fmt: skip
        result = run_probe(cfg, ep)
        if result.ok:
            msg = _message(f"Test of {ep.name}: OK. Reply: {result.text}")
        else:
            msg = _message(f"Test of {ep.name} FAILED: {result.text}", True)
        return page(request, cfg, msg, 200 if result.ok else 502)

    return [
        Route(PATH, get, methods=["GET"]),
        guard.post_route(f"{PATH}/endpoint", put),
        guard.post_route(f"{PATH}/endpoint/remove", remove),
        guard.post_route(f"{PATH}/endpoint/move", move),
        guard.post_route(f"{PATH}/endpoint/test", test),
        guard.post_route(f"{PATH}/reviewers", reviewers),
    ]


__all__ = [
    "NAV_ENTRY",
    "PATH",
    "PRESETS",
    "Preset",
    "Probe",
    "edit_reviewers",
    "endpoint_from_form",
    "key_is_set",
    "move_endpoint",
    "probe_endpoint",
    "put_endpoint",
    "remove_endpoint",
    "render_tiers",
    "routes",
    "tier_warnings",
    "validated",
]
