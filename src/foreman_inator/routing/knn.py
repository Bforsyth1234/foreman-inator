"""Nearest-neighbour start rule (todo #23, SPEC "Routing and escalation", start rule 2).

Once enough tickets have been accepted, the router embeds the new request, finds the
``k`` most similar accepted tickets and starts at the cheapest tier accepted for most of
them. Below the threshold, or when anything goes wrong, ``route_knn`` returns None and the
caller falls back to router v1 (``rules.route``).

The neighbour rule (exact)
--------------------------
1. **Pool.** Tickets other than this one with ``accepted_attempt_id`` set (you accepted
   them) and a stored vector from the configured embedding model with the same number
   of dimensions as the new ticket's vector. Excluded: a ticket *started above the
   router's pick* (every one of its attempts ran on a tier above ``routed_tier``: a
   profile shift such as #51's ``fast``). Its acceptance says nothing about whether the
   router's tier would have worked, so learning from it would push every start upward.
   Tickets with ``routed_tier`` NULL (older rows) are kept.
2. **Threshold.** If the pool holds fewer than ``threshold`` tickets (default 50),
   return None (fall back).
3. **Nearest.** Rank the pool by cosine similarity to the new ticket, highest first;
   equal similarities go to the older ticket (lower id). Take the first ``k`` (default 10).
4. **Votes.** Each neighbour votes once, for the tier of its *accepted attempt* (the tier
   where it was finally accepted) when that tier was accepted cleanly. So a neighbour
   accepted only after escalation counts at the tier it escalated to; an ``explore``
   attempt accepted one tier down counts at that lower tier (that is what exploration is
   for); a failed ``explore`` start counts nothing (the ticket counts where it was later
   accepted). **A user kickback on the accepted tier moves the vote up one tier** (user
   decision, 2026-11-23: a kickback costs review time, so a tier that is only accepted
   after one is not the cheapest tier that works): a neighbour the user kicked back on
   tier 1 and then accepted on tier 1 votes for tier 2. The top tier has nowhere to move
   to and votes for itself. **An AI reviewer kickback on the accepted tier splits the
   vote** (user decision, 2026-11-23: it costs wall-clock time and one more cloud review
   call, but none of the user's time): half a vote for the accepted tier and half for the
   tier above. A user kickback on the same tier wins over it (one whole vote up), and on
   the top tier the vote stays whole. Only kickbacks on an attempt of the accepted tier
   count: kickbacks on a lower tier are already paid for by the escalation (two reviewer
   kickbacks escalate, so the split covers the one-kickback case). Votes are not weighted
   by similarity.
5. **Pick.** The tier with the most votes; a tie goes to the cheapest (lowest) of the
   tied tiers. That tier then goes through ``rules.resolve_tier`` (a tier with no model
   configured is skipped upward, as in #15).

What it learns from: where tickets were finally accepted and whether the user had to kick
them back there first (step 4), measured against the
router's own pick (``tickets.routed_tier``, step 1). It never reads an attempt's start
tier, so neither an exploration start nor a profile-shifted start is learned as a pick.
#51 must therefore keep ``routed_tier`` the router's own (unshifted) pick.

A ``tier:N`` override wins: ``route_knn`` hands it to ``rules.route`` with no embedding
call (and no store read).

Vectors
-------
One row per ticket in ``ticket_embeddings`` (migration 004): the embedding model's config
name, the dimension, an L2-normalised little-endian float32 blob, and the sha256 of the
embedded text. A ticket's vector is written when ``route_knn`` embeds it (only when the
pool already meets the threshold) and by ``backfill`` for accepted tickets with no
current vector. **A changed model** (a new config name) is never compared against old
vectors: the pool and the threshold count only rows of the current model and the query's
dimension, so right after a switch ``route_knn`` falls back to the rules until
``backfill`` has re-embedded at least ``threshold`` accepted tickets (it replaces rows of
other models and rows whose text changed). Renaming the model in config is how to force
a re-embed when the same name gets new weights.

Embedding calls (invariant 7c, rule 12)
---------------------------------------
Through the model proxy only (``proxy.context(label="embed")``, OpenAI ``POST
/embeddings`` with ``input`` as an array of strings), so every call is an ``llm.call``
span (no recorder row). A cloud embedding model is refused unless
``allow_cloud_embedder=True``. The request text is the title plus the body, bounded
(``TITLE_BUDGET`` + ``BODY_BUDGET`` characters, like the router). Every call has a hard
wall-clock deadline (``timeout``, default 5 s for routing); a failure, timeout or odd
answer is never raised out of ``route_knn`` or ``backfill``.

Rule 12 record (2026-11-23): docs.ollama.com/api/openai-compatibility lists
``/v1/embeddings`` with ``model``, ``input`` (string or array of strings),
``encoding_format`` and ``dimensions`` supported; docs.ollama.com/capabilities/embeddings
recommends ``embeddinggemma`` and says vectors are L2-normalised, 384-1024 dimensions
depending on the model. The response shape relied on is OpenAI's
(``data[i].embedding`` list of numbers, ``data[i].index``); ``_parse_embeddings`` is the
one adapter that reads it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import operator
import sys
import threading
from array import array
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from foreman_inator import tracing
from foreman_inator.config import OLLAMA_URL, Config, ModelEndpoint
from foreman_inator.routing import rules
from foreman_inator.routing.rules import Override, Route, configured_tiers, resolve_tier
from foreman_inator.tickets.prompt import clip_head

log = logging.getLogger(__name__)

DEFAULT_EMBED_MODEL = "embeddinggemma"
DEFAULT_THRESHOLD = 50
DEFAULT_K = 10
DEFAULT_TIMEOUT = 5.0  # seconds: one route-time embedding call, wall clock
BACKFILL_TIMEOUT = 15.0  # seconds: one backfill batch
BACKFILL_BATCH = 8

TITLE_BUDGET = rules.TITLE_BUDGET
BODY_BUDGET = rules.ISSUE_BODY_BUDGET
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_DIMS = 16_384
REASON_NEIGHBOURS = 10  # neighbours listed in route_reason (k above this is summarised)

SOURCE = "knn"


class EmbedError(Exception):
    """An embedding call failed; the message says how. ``status`` is the HTTP status when
    the call got an error answer."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status, self.code = status, code


# --------------------------------------------------------------------------- endpoint


def embedding_endpoint(
    model: str = DEFAULT_EMBED_MODEL,
    *,
    name: str | None = None,
    base_url: str = OLLAMA_URL,
    kind: str = "local",
) -> ModelEndpoint:
    """A ``ModelEndpoint`` for the embedding model, to add to the proxy's model list.

    It is *not* a writer model: never put it in ``config.models`` or the ladder. ``tier``
    is a placeholder the proxy ignores."""
    return ModelEndpoint(
        name=name or model,
        tier=1,
        kind=kind,  # type: ignore[arg-type]
        model=model,
        base_url=base_url,
        family="embedding",
    )


# --------------------------------------------------------------------------- text + vectors


def request_text(ticket: Any) -> str:
    """The text embedded for a ticket: title, blank line, body, each bounded."""
    title = clip_head(str(getattr(ticket, "title", "") or ""), TITLE_BUDGET).replace("\n", " ")
    body = clip_head(str(getattr(ticket, "request", "") or ""), BODY_BUDGET)
    return f"{title}\n\n{body}".strip() or "(empty issue)"


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalise(vec: Sequence[float]) -> list[float]:
    """Unit length; raises EmbedError for a zero or non-finite vector."""
    total = 0.0
    for x in vec:
        if not math.isfinite(x):
            raise EmbedError("embedding has a non-finite value")
        total += x * x
    norm = math.sqrt(total)
    if norm == 0.0:
        raise EmbedError("embedding is all zeros")
    return [x / norm for x in vec]


def pack(vec: Sequence[float]) -> bytes:
    arr = array("f", vec)
    if sys.byteorder != "little":
        arr.byteswap()
    return arr.tobytes()


def unpack(blob: bytes) -> array:
    arr = array("f")
    arr.frombytes(blob)
    if sys.byteorder != "little":
        arr.byteswap()
    return arr


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Dot product of two unit vectors."""
    if len(a) != len(b):
        raise ValueError("vectors differ in length")
    return sum(map(operator.mul, a, b))


# --------------------------------------------------------------------------- the call


def _parse_embeddings(data: Any, count: int) -> list[list[float]]:
    """The rule-12 adapter: OpenAI ``{"data": [{"index", "embedding"}]}`` → vectors in
    input order, all one dimension, normalised."""
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list) or len(items) != count:
        raise EmbedError(f"expected {count} embedding(s) in data[]")
    out: list[list[float] | None] = [None] * count
    for pos, item in enumerate(items):
        if not isinstance(item, dict):
            raise EmbedError("data[] item is not an object")
        idx = item.get("index", pos)
        vec = item.get("embedding")
        if isinstance(idx, bool) or not isinstance(idx, int) or not 0 <= idx < count:
            raise EmbedError("data[] item has a bad index")
        if out[idx] is not None:
            raise EmbedError("data[] repeats an index")
        if (
            not isinstance(vec, list)
            or not 0 < len(vec) <= MAX_DIMS
            or not all(isinstance(x, int | float) and not isinstance(x, bool) for x in vec)
        ):
            raise EmbedError("embedding is not a list of numbers")
        out[idx] = normalise([float(x) for x in vec])
    dims = {len(v) for v in out if v is not None}
    if len(dims) != 1:
        raise EmbedError("embeddings have different dimensions")
    return [v for v in out if v is not None]


def _post(url: str, body: dict[str, Any], timeout: float) -> Any:
    try:
        with (
            httpx.Client(timeout=httpx.Timeout(timeout), follow_redirects=False) as client,
            client.stream("POST", url, json=body) as resp,
        ):
            buf = bytearray()
            for chunk in resp.iter_bytes():
                buf.extend(chunk)
                if len(buf) > MAX_RESPONSE_BYTES:
                    raise EmbedError(f"response larger than {MAX_RESPONSE_BYTES} bytes")
            status = resp.status_code
    except httpx.TimeoutException as exc:
        raise EmbedError(f"timed out ({type(exc).__name__})") from exc
    except httpx.HTTPError as exc:
        raise EmbedError(f"proxy unreachable ({type(exc).__name__})") from exc
    if status >= 400:
        raise EmbedError(f"HTTP {status}", status=status, code=_error_code(bytes(buf)))
    try:
        return json.loads(bytes(buf))
    except ValueError as exc:
        raise EmbedError("response is not JSON") from exc


def _post_with_deadline(url: str, body: dict[str, Any], timeout: float) -> Any:
    """``_post`` with a hard wall-clock deadline (httpx times each phase separately)."""
    out: dict[str, Any] = {}

    def run() -> None:
        try:
            out["data"] = _post(url, body, timeout)
        except EmbedError as exc:
            out["error"] = exc
        except Exception as exc:
            out["error"] = EmbedError(f"request failed ({type(exc).__name__})")

    t = threading.Thread(target=run, name="fman-embed", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise EmbedError(f"timed out after {timeout:g}s")
    if "error" in out:
        raise out["error"]
    return out["data"]


def embed_texts(
    texts: Sequence[str],
    *,
    proxy: Any,
    embedder: ModelEndpoint,
    allow_cloud_embedder: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[list[float]]:
    """Embed ``texts`` in one call through ``proxy.context(label="embed")``. Returns unit
    vectors in input order. Raises EmbedError (callers here never let it escape)."""
    if not texts:
        return []
    if embedder.is_cloud and not allow_cloud_embedder:
        raise EmbedError(f"embedding model {embedder.name} is a cloud model; stays local (7c)")
    if proxy is None:
        raise EmbedError("no model proxy given")
    body = {"model": embedder.name, "input": list(texts)}
    with proxy.context(label="embed") as ctx:
        try:
            data = _post_with_deadline(ctx.base_url.rstrip("/") + "/embeddings", body, timeout)
        except EmbedError as exc:
            if exc.status == 404 and exc.code not in PROXY_404_CODES:
                raise EmbedError(_not_found(embedder), status=404) from exc
            raise
    return _parse_embeddings(data, len(texts))


#: 404s the proxy itself answers (a dead context, a bad path): not a missing model.
PROXY_404_CODES = frozenset({"unknown_context", "unknown_endpoint"})


def _error_code(data: bytes) -> str | None:
    """``error.code`` of an OpenAI-style error body, if there is one."""
    try:
        err = json.loads(data).get("error")
    except (ValueError, AttributeError):
        return None
    code = err.get("code") if isinstance(err, dict) else None
    return code if isinstance(code, str) else None


def _not_found(embedder: ModelEndpoint) -> str:
    """#51: a 404 from the embeddings call means the model is not there (Ollama answers
    404 for a model that is not pulled). Name it and say how to fix it."""
    if embedder.is_cloud:
        return f"HTTP 404: embedding model {embedder.model} not found at {embedder.base_url}"
    return (
        f"HTTP 404: embedding model {embedder.model} is not pulled in Ollama; "
        f"run: ollama pull {embedder.model}"
    )


# --------------------------------------------------------------------------- store


def store_vector(store: Any, ticket_id: int, *, model: str, vector: Sequence[float], digest: str):
    """Write (or replace) ``ticket_id``'s vector. ``vector`` must already be unit length."""
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO ticket_embeddings (ticket_id, model, dims, vector, text_sha256,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (ticket_id) DO UPDATE SET model = excluded.model,"
            " dims = excluded.dims, vector = excluded.vector,"
            " text_sha256 = excluded.text_sha256, created_at = excluded.created_at",
            (
                int(ticket_id),
                model,
                len(vector),
                pack(vector),
                digest,
                _ts(store.now()),
            ),
        )


def _ts(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def stored_vector(store: Any, ticket_id: int, *, model: str, digest: str) -> list[float] | None:
    """The ticket's vector if it is from ``model`` and of exactly this text, else None."""
    rows = store.query(
        "SELECT vector FROM ticket_embeddings WHERE ticket_id = ? AND model = ?"
        " AND text_sha256 = ?",
        (int(ticket_id), model, digest),
    )
    return list(unpack(rows[0]["vector"])) if rows else None


# Accepted tickets with their accepted tier and whether the start was shifted above the
# router's pick (step 1 of the rule).
_POOL_FROM = """
    FROM tickets t
    JOIN attempts a ON a.id = t.accepted_attempt_id AND a.ticket_id = t.id
    JOIN ticket_embeddings e ON e.ticket_id = t.id
    WHERE e.model = :model
      AND t.id != :exclude
      AND (t.routed_tier IS NULL
           OR (SELECT MIN(x.tier) FROM attempts x WHERE x.ticket_id = t.id) <= t.routed_tier)
"""


def pool_size(store: Any, *, model: str, dims: int | None = None, exclude: int = -1) -> int:
    """How many tickets ``route_knn`` may learn from (step 1), without decoding vectors."""
    sql = "SELECT COUNT(*) AS n " + _POOL_FROM
    params: dict[str, Any] = {"model": model, "exclude": exclude}
    if dims is not None:
        sql += " AND e.dims = :dims"
        params["dims"] = dims
    return int(store.query(sql, params)[0]["n"])


TOP_TIER = max(rules.TIERS)


@dataclass(frozen=True, slots=True)
class Neighbour:
    ticket_id: int
    similarity: float
    accepted_tier: int
    kicked_back: bool = False  # the user kicked it back on the accepted tier first
    reviewer_kicked: bool = False  # the AI reviewer kicked it back on the accepted tier

    @property
    def vote_tier(self) -> int:
        """Step 4: the accepted tier, or one tier up when the user kicked it back there."""
        if self.kicked_back:
            return min(self.accepted_tier + 1, TOP_TIER)
        return self.accepted_tier

    @property
    def split(self) -> bool:
        """Step 4: an AI reviewer kickback (and no user kickback) below the top tier."""
        return self.reviewer_kicked and not self.kicked_back and self.accepted_tier < TOP_TIER

    def votes(self) -> tuple[tuple[int, float], ...]:
        """Step 4: ``(tier, weight)`` pairs, weights summing to 1."""
        if self.split:
            return ((self.accepted_tier, 0.5), (self.accepted_tier + 1, 0.5))
        return ((self.vote_tier, 1.0),)


# Step 4: did the user kick the ticket back on an attempt of the tier it was accepted on?
_KICKED_BACK = """
    EXISTS (SELECT 1 FROM kickbacks k JOIN attempts x ON x.id = k.attempt_id
            WHERE x.ticket_id = t.id AND x.tier = a.tier AND k."by" = 'user')
"""
_REVIEWER_KICKED = _KICKED_BACK.replace("'user'", "'reviewer'")


def load_pool(
    store: Any, *, model: str, dims: int, exclude: int = -1
) -> list[tuple[int, int, array, bool, bool]]:
    """``(ticket_id, accepted_tier, vector, kicked_back, reviewer_kicked)`` for every
    ticket in the pool."""
    rows = store.query(
        "SELECT t.id AS id, a.tier AS tier, e.vector AS vector,"
        + _KICKED_BACK
        + " AS kicked,"
        + _REVIEWER_KICKED
        + " AS reviewer_kicked "
        + _POOL_FROM
        + " AND e.dims = :dims ORDER BY t.id",
        {"model": model, "exclude": exclude, "dims": dims},
    )
    return [
        (
            int(r["id"]),
            int(r["tier"]),
            unpack(r["vector"]),
            bool(r["kicked"]),
            bool(r["reviewer_kicked"]),
        )
        for r in rows
    ]


def nearest(
    query: Sequence[float], pool: Iterable[Sequence[Any]], k: int = DEFAULT_K
) -> list[Neighbour]:
    """Step 3: the ``k`` most similar, highest similarity first, ties to the lower id.
    A pool row is ``(ticket_id, accepted_tier, vector)`` or ``load_pool``'s five fields."""
    scored = [
        Neighbour(tid, cosine(query, vec), tier, *(bool(x) for x in rest[:2]))
        for tid, tier, vec, *rest in pool
    ]
    scored.sort(key=lambda n: (-n.similarity, n.ticket_id))
    return scored[: max(0, k)]


def pick_tier(neighbours: Sequence[Neighbour]) -> tuple[int, float]:
    """Steps 4-5: ``(tier, votes)``: the tier with the most votes (``Neighbour.votes``, so
    a count that may end in .5); a tie goes to the cheapest tied tier. Raises ValueError
    for no neighbours."""
    if not neighbours:
        raise ValueError("no neighbours to vote")
    votes: dict[int, float] = {}
    for n in neighbours:
        for tier, weight in n.votes():
            votes[tier] = votes.get(tier, 0.0) + weight  # halves are exact floats
    best = max(votes.values())
    tier = min(t for t, v in votes.items() if v == best)
    return tier, best


# --------------------------------------------------------------------------- route


def route_knn(
    ticket: Any,
    *,
    store: Any,
    config: Config | Iterable[ModelEndpoint],
    proxy: Any,
    embedder: ModelEndpoint | None = None,
    override: Override | None = None,
    threshold: int = DEFAULT_THRESHOLD,
    k: int = DEFAULT_K,
    timeout: float = DEFAULT_TIMEOUT,
    allow_cloud_embedder: bool = False,
) -> Route | None:
    """The nearest-neighbour pick for ``ticket`` (a ``db.store.Ticket`` or anything with
    ``id``, ``title``, ``request``), or None to fall back to ``rules.route``. Never raises;
    never waits on the model longer than ``timeout``. See the module docstring.

    ``override``: a ``tier:N`` label wins, through ``rules.route``, with no embedding call.
    ``embedder``: default ``embedding_endpoint()``; the proxy must know its name."""
    if override is not None:
        return rules.route(ticket, config=config, override=override)
    try:
        return _route_knn(
            ticket,
            store=store,
            config=config,
            proxy=proxy,
            embedder=embedder or embedding_endpoint(),
            threshold=threshold,
            k=k,
            timeout=timeout,
            allow_cloud_embedder=allow_cloud_embedder,
        )
    except Exception as exc:  # routing must never take the worker down
        log.warning("knn router: falling back for ticket %s (%s)", getattr(ticket, "id", "?"), exc)
        return None


def _route_knn(
    ticket: Any,
    *,
    store: Any,
    config: Config | Iterable[ModelEndpoint],
    proxy: Any,
    embedder: ModelEndpoint,
    threshold: int,
    k: int,
    timeout: float,
    allow_cloud_embedder: bool,
) -> Route | None:
    tid = getattr(ticket, "id", None)
    exclude = int(tid) if isinstance(tid, int) else -1
    # Cheap gate first: below the threshold there is no model call and no span.
    if pool_size(store, model=embedder.name, exclude=exclude) < max(1, threshold):
        return None
    if embedder.is_cloud and not allow_cloud_embedder:
        log.warning("knn router: embedding model %s is cloud; not used (7c)", embedder.name)
        return None
    models = config.models if isinstance(config, Config) else tuple(config)
    text = request_text(ticket)
    digest = text_digest(text)
    with tracing.route_span(rules.FALLBACK_TIER, "routing (knn)") as span:
        span.set_attribute("route.embed_model", embedder.name)
        vec = stored_vector(store, exclude, model=embedder.name, digest=digest)
        if vec is None:
            try:
                vec = embed_texts(
                    [text],
                    proxy=proxy,
                    embedder=embedder,
                    allow_cloud_embedder=allow_cloud_embedder,
                    timeout=timeout,
                )[0]
            except EmbedError as exc:
                _fallback(span, f"embedding failed: {exc}")
                return None
            if exclude >= 0:
                try:
                    store_vector(store, exclude, model=embedder.name, vector=vec, digest=digest)
                except Exception as exc:  # the pick still stands without the stored row
                    log.warning("knn router: could not store ticket %s's vector: %s", tid, exc)
        pool = load_pool(store, model=embedder.name, dims=len(vec), exclude=exclude)
        if len(pool) < max(1, threshold):
            _fallback(span, f"{len(pool)} accepted tickets with {len(vec)}-dim vectors")
            return None
        found = nearest(vec, pool, k)
        wanted, votes = pick_tier(found)
        tier, note, has_model = resolve_tier(wanted, configured_tiers(models))
        listed = ", ".join(_listed(n) for n in found[:REASON_NEIGHBOURS])
        if len(found) > REASON_NEIGHBOURS:
            listed += f", +{len(found) - REASON_NEIGHBOURS} more"
        moved = sum(1 for n in found if n.vote_tier == wanted != n.accepted_tier)
        halves = sum(1 for n in found if n.split and wanted in dict(n.votes()))
        tally = f"tier {wanted} accepted for {votes:g} of {len(found)}"
        notes = []
        if moved:
            notes.append(f"{moved} moved up after a user kickback")
        if halves:
            notes.append(f"{halves} half votes after an AI reviewer kickback")
        if notes:
            tally = f"tier {wanted} voted by {votes:g} of {len(found)} ({'; '.join(notes)})"
        reason = (
            f"knn: {len(found)} nearest of {len(pool)} accepted tickets ({embedder.name}): "
            f"{listed}; {tally} -> tier {wanted}"
        )
        if note:
            reason += f"; {note}"
        result = Route(
            tier=tier,
            reason=reason,
            source=SOURCE,  # type: ignore[arg-type]  # rules.Source gains "knn" (#29)
            classifier=embedder.name,
            requested_tier=wanted,
            has_model=has_model,
        )
        for key, value in {
            "route.tier": tier,
            "route.reason": reason,
            "route.source": SOURCE,
            "route.requested_tier": wanted,
            "route.has_model": has_model,
            "route.matched_tickets": [n.ticket_id for n in found],
        }.items():
            span.set_attribute(key, value)
    log.info("knn router: ticket %s -> tier %d (%s)", tid, tier, reason)
    return result


def _listed(n: Neighbour) -> str:
    """One neighbour in ``route_reason``: ``#12 t1``, ``#12 t1 kicked back>t2`` when a
    user kickback moved its vote, or ``#12 t1 ai kickback>half t2`` when a reviewer
    kickback split it. No ", " inside: ``matched_tickets`` splits on it."""
    if n.split:
        return f"#{n.ticket_id} t{n.accepted_tier} ai kickback>half t{n.accepted_tier + 1}"
    if n.vote_tier != n.accepted_tier:
        return f"#{n.ticket_id} t{n.accepted_tier} kicked back>t{n.vote_tier}"
    return f"#{n.ticket_id} t{n.accepted_tier}"


def _fallback(span: Any, why: str) -> None:
    log.warning("knn router fallback: %s", why)
    span.set_attribute("route.source", "knn_fallback")
    span.set_attribute("route.reason", f"knn fallback: {why}; rules router decides")


def matched_tickets(route: Route) -> list[int]:
    """The ticket ids a knn ``route_reason`` lists (for the metrics page and #28)."""
    if route.source != SOURCE:
        return []
    head = route.reason.split(": ", 2)[-1].split(";", 1)[0]
    out = []
    for part in head.split(", "):
        if part.startswith("#"):
            out.append(int(part[1:].split(" ", 1)[0]))
    return out


# --------------------------------------------------------------------------- backfill


@dataclass(frozen=True, slots=True)
class BackfillResult:
    embedded: tuple[int, ...]  # ticket ids written this call
    remaining: int  # accepted tickets still without a current vector
    error: str | None = None  # why the batch failed (nothing was written), if it did


def backfill(
    store: Any,
    *,
    proxy: Any,
    embedder: ModelEndpoint | None = None,
    batch_size: int = BACKFILL_BATCH,
    timeout: float = BACKFILL_TIMEOUT,
    allow_cloud_embedder: bool = False,
) -> BackfillResult:
    """Embed up to ``batch_size`` accepted tickets that have no current vector (none, one
    from another model, or one of older issue text), oldest first, in one call. State is
    the table itself, so it is safe to call on every tick and after a restart. Never
    raises; a failed batch writes nothing and is retried on the next call."""
    emb = embedder or embedding_endpoint()
    try:
        todo = _stale(store, emb.name)
    except Exception as exc:
        return BackfillResult((), 0, f"store read failed ({type(exc).__name__})")
    if not todo:
        return BackfillResult((), 0)
    batch = todo[: max(1, batch_size)]
    try:
        with tracing.span("embed.backfill", {"embed.model": emb.name, "embed.count": len(batch)}):
            vecs = embed_texts(
                [text for _, text, _ in batch],
                proxy=proxy,
                embedder=emb,
                allow_cloud_embedder=allow_cloud_embedder,
                timeout=timeout,
            )
        with store.transaction():
            for (tid, _, digest), vec in zip(batch, vecs, strict=True):
                store_vector(store, tid, model=emb.name, vector=vec, digest=digest)
    except Exception as exc:
        log.warning("knn backfill: batch of %d failed: %s", len(batch), exc)
        return BackfillResult((), len(todo), str(exc) or type(exc).__name__)
    return BackfillResult(tuple(t for t, _, _ in batch), len(todo) - len(batch))


def _stale(store: Any, model: str) -> list[tuple[int, str, str]]:
    """``(ticket_id, text, digest)`` of accepted tickets needing a (new) vector, by id."""
    rows = store.query(
        "SELECT t.id AS id, t.title AS title, t.request AS request,"
        " e.model AS model, e.text_sha256 AS digest"
        " FROM tickets t LEFT JOIN ticket_embeddings e ON e.ticket_id = t.id"
        " WHERE t.accepted_attempt_id IS NOT NULL ORDER BY t.id"
    )
    out = []
    for r in rows:
        text = request_text(_Text(r["title"], r["request"]))
        digest = text_digest(text)
        if r["model"] != model or r["digest"] != digest:
            out.append((int(r["id"]), text, digest))
    return out


@dataclass(frozen=True, slots=True)
class _Text:
    title: str
    request: str


__all__ = [
    "BACKFILL_BATCH",
    "DEFAULT_EMBED_MODEL",
    "DEFAULT_K",
    "DEFAULT_THRESHOLD",
    "DEFAULT_TIMEOUT",
    "SOURCE",
    "BackfillResult",
    "EmbedError",
    "Neighbour",
    "backfill",
    "embed_texts",
    "embedding_endpoint",
    "load_pool",
    "matched_tickets",
    "nearest",
    "pick_tier",
    "pool_size",
    "request_text",
    "route_knn",
    "store_vector",
    "stored_vector",
]
