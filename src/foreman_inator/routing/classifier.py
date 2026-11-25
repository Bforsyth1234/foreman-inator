"""Learned classifier router (todo #37 / GitHub #19): logistic regression over the
stored ticket embeddings, used only where it measurably beats the kNN rule.

What it predicts
----------------
The tier a ticket **got accepted on** (``attempts.tier`` of ``tickets.accepted_attempt_id``),
from the ticket's stored embedding (``ticket_embeddings``, migration 004). The examples
are exactly the kNN pool (``knn.load_pool``): accepted tickets with a vector of the
current embedding model and the query's dimension, **minus reverted tickets** (a
``reverted`` row in ``post_merge_signals``) and tickets started above the router's
pick, for the same reasons as kNN (``knn`` module docstring, step 1).

The model
---------
Multinomial (softmax) logistic regression in pure Python (no new dependency). Features
are the unit vector, standardised per dimension with the training set's mean and
standard deviation; one weight vector and bias per tier seen in training; L2 penalty
``L2``; full-batch Adam for ``EPOCHS`` epochs from zero weights, so training is
deterministic. At most the ``MAX_TRAIN`` most recent examples are used (bounded cost).

When it is used (the gate)
--------------------------
1. Enabled (config ``classifier_router``, default on) and the pool holds at least
   ``min_tickets`` (config ``classifier_min_tickets``, default 300) tickets. Below that,
   no training happens at all.
2. A **chronological held-out split**: examples in ticket-id order (creation order); the
   newest ``HOLDOUT_FRACTION`` (20%) are the test set. The classifier is trained on the
   older 80%; the kNN rule (``knn.nearest`` + ``knn.pick_tier``, the same ``k``, the same
   kickback votes) predicts each test ticket from the same 80%. Both are scored on
   accuracy against the accepted tier. **The classifier is used only if it gets strictly
   more right; a tie goes to kNN.**
3. When it wins, the model that routes is retrained on all examples (newest included).
4. The comparison (and the routing model) is cached in memory by ``Gate`` and recomputed
   at most once per ``RECHECK`` (a day), or sooner when the embedding model or the
   dimension changes. A worker restart recomputes once on the next route.

The classifier never makes a model call: it reuses the vector the kNN router just
stored for the ticket, so local-only repos (#49), the embedder's cloud gate (7c) and the
crash story stay exactly the kNN router's. Anything that fails here falls back to the
kNN pick; nothing is raised.

The ``Route`` it returns has ``source="classifier"``, ``label`` the tier's name
(small/normal/large, as the rules classifier sets it, so ``decompose.eligible`` treats a
tier 3 pick as before) and a ``reason`` starting ``"classifier: "``.

Not built: the LoRA router of the same todo (it needs a training pipeline and months of
accepted tickets before it could be compared against this classifier).
"""

from __future__ import annotations

import logging
import math
import operator
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from foreman_inator import tracing
from foreman_inator.config import Config, ModelEndpoint
from foreman_inator.routing import knn
from foreman_inator.routing.rules import LABEL_TIERS, Route, configured_tiers, resolve_tier

log = logging.getLogger(__name__)

DEFAULT_ENABLED = True
DEFAULT_MIN_TICKETS = 300
HOLDOUT_FRACTION = 0.2
RECHECK = timedelta(days=1)
EPOCHS = 150
LEARNING_RATE = 0.05
L2 = 1e-3
MAX_TRAIN = 2000  # ~15 s of training per fit at 768 dims
SOURCE = "classifier"

_TIER_LABELS = {tier: label for label, tier in LABEL_TIERS.items()}
_ADAM_B1, _ADAM_B2, _ADAM_EPS = 0.9, 0.999, 1e-8


# --------------------------------------------------------------------------- examples


@dataclass(frozen=True, slots=True)
class Example:
    ticket_id: int
    tier: int  # the accepted tier (the label)
    vector: Sequence[float]  # unit length
    kicked_back: bool = False  # for the kNN baseline's votes only
    reviewer_kicked: bool = False

    def pool_row(self) -> tuple[int, int, Sequence[float], bool, bool]:
        return (self.ticket_id, self.tier, self.vector, self.kicked_back, self.reviewer_kicked)


def load_examples(store: Any, *, model: str, dims: int, exclude: int = -1) -> list[Example]:
    """The kNN pool as examples, oldest ticket first (reverted tickets excluded)."""
    return [
        Example(tid, tier, vec, kicked, reviewer)
        for tid, tier, vec, kicked, reviewer in knn.load_pool(
            store, model=model, dims=dims, exclude=exclude
        )
    ]


def chronological_split(
    examples: Sequence[Example], holdout: float = HOLDOUT_FRACTION
) -> tuple[list[Example], list[Example]]:
    """``(train, test)``: the newest ``holdout`` share (by ticket id, at least one) is test."""
    ordered = sorted(examples, key=lambda e: e.ticket_id)
    n_test = max(1, math.ceil(len(ordered) * holdout)) if len(ordered) > 1 else 0
    cut = len(ordered) - n_test
    return ordered[:cut], ordered[cut:]


# --------------------------------------------------------------------------- the model


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(map(operator.mul, a, b))


@dataclass(frozen=True)
class LogReg:
    """A trained softmax regression. ``classes[c]`` is the tier of row ``weights[c]``."""

    classes: tuple[int, ...]
    weights: tuple[tuple[float, ...], ...]
    bias: tuple[float, ...]
    mean: tuple[float, ...]
    scale: tuple[float, ...]  # 1 / std per dimension

    def _features(self, vec: Sequence[float]) -> list[float]:
        return [(x - m) * s for x, m, s in zip(vec, self.mean, self.scale, strict=True)]

    def probabilities(self, vec: Sequence[float]) -> dict[int, float]:
        x = self._features(vec)
        logits = [_dot(w, x) + b for w, b in zip(self.weights, self.bias, strict=True)]
        return dict(zip(self.classes, _softmax(logits), strict=True))

    def predict(self, vec: Sequence[float]) -> int:
        """The most probable tier; a tie goes to the cheaper tier."""
        probs = self.probabilities(vec)
        best = max(probs.values())
        return min(t for t, p in probs.items() if p == best)


def _softmax(logits: Sequence[float]) -> list[float]:
    top = max(logits)
    exps = [math.exp(v - top) for v in logits]
    total = sum(exps)
    return [e / total for e in exps]


def train(
    examples: Sequence[Example],
    *,
    epochs: int = EPOCHS,
    learning_rate: float = LEARNING_RATE,
    l2: float = L2,
) -> LogReg:
    """Fit the softmax regression (see the module docstring). Raises ValueError for no
    examples or vectors of different lengths."""
    if not examples:
        raise ValueError("no examples to train on")
    examples = sorted(examples, key=lambda e: e.ticket_id)[-MAX_TRAIN:]
    dims = len(examples[0].vector)
    if any(len(e.vector) != dims for e in examples):
        raise ValueError("vectors differ in length")
    n = len(examples)
    cols_raw = list(zip(*(e.vector for e in examples), strict=True))
    mean = [sum(c) / n for c in cols_raw]
    scale = []
    for c, m in zip(cols_raw, mean, strict=True):
        std = math.sqrt(sum((x - m) ** 2 for x in c) / n)
        scale.append(1.0 / std if std > 1e-12 else 1.0)
    rows = [[(x - m) * s for x, m, s in zip(e.vector, mean, scale, strict=True)] for e in examples]
    cols = list(zip(*rows, strict=True))
    classes = tuple(sorted({e.tier for e in examples}))
    index = {t: i for i, t in enumerate(classes)}
    labels = [index[e.tier] for e in examples]
    n_cls = len(classes)
    w = [[0.0] * dims for _ in range(n_cls)]
    b = [0.0] * n_cls
    mw = [[0.0] * dims for _ in range(n_cls)]
    vw = [[0.0] * dims for _ in range(n_cls)]
    mb, vb = [0.0] * n_cls, [0.0] * n_cls
    for step in range(1, max(0, epochs) + 1):
        # residuals r[c][i] = p_ic - y_ic, averaged over the batch
        resid = [[0.0] * n for _ in range(n_cls)]
        for i, x in enumerate(rows):
            probs = _softmax([_dot(w[c], x) + b[c] for c in range(n_cls)])
            for c in range(n_cls):
                resid[c][i] = (probs[c] - (1.0 if labels[i] == c else 0.0)) / n
        c1 = 1.0 - _ADAM_B1**step
        c2 = 1.0 - _ADAM_B2**step
        for c in range(n_cls):
            rc, wc, mc, vc = resid[c], w[c], mw[c], vw[c]
            for j, col in enumerate(cols):
                g = _dot(col, rc) + l2 * wc[j]
                mc[j] = _ADAM_B1 * mc[j] + (1 - _ADAM_B1) * g
                vc[j] = _ADAM_B2 * vc[j] + (1 - _ADAM_B2) * g * g
                wc[j] -= learning_rate * (mc[j] / c1) / (math.sqrt(vc[j] / c2) + _ADAM_EPS)
            g = sum(rc)
            mb[c] = _ADAM_B1 * mb[c] + (1 - _ADAM_B1) * g
            vb[c] = _ADAM_B2 * vb[c] + (1 - _ADAM_B2) * g * g
            b[c] -= learning_rate * (mb[c] / c1) / (math.sqrt(vb[c] / c2) + _ADAM_EPS)
    return LogReg(
        classes=classes,
        weights=tuple(tuple(row) for row in w),
        bias=tuple(b),
        mean=tuple(mean),
        scale=tuple(scale),
    )


# --------------------------------------------------------------------------- comparison


def knn_predict(train_set: Sequence[Example], vec: Sequence[float], k: int) -> int:
    """The kNN rule's raw pick (before skipping tiers with no model) from ``train_set``."""
    found = knn.nearest(vec, (e.pool_row() for e in train_set), k)
    return knn.pick_tier(found)[0]


@dataclass(frozen=True)
class Comparison:
    """One held-out check. ``model`` is set (trained on all examples) only when the
    classifier won."""

    examples: int
    test: int
    classifier_correct: int
    knn_correct: int
    embed_model: str
    dims: int
    checked_at: datetime
    model: LogReg | None = field(default=None, repr=False)

    @property
    def use_classifier(self) -> bool:
        return self.model is not None

    def summary(self) -> str:
        return (
            f"held-out {self.classifier_correct}/{self.test} vs knn {self.knn_correct}/{self.test}"
        )


def compare(
    examples: Sequence[Example],
    *,
    k: int,
    embed_model: str,
    dims: int,
    now: datetime,
    epochs: int = EPOCHS,
) -> Comparison:
    """Step 2-3 of the gate: score both on the chronological held-out 20%; strictly
    better wins (a tie goes to kNN); a winner is retrained on everything."""
    train_set, test_set = chronological_split(examples)
    clf_ok = knn_ok = 0
    if train_set and test_set:
        clf = train(train_set, epochs=epochs)
        for e in test_set:
            clf_ok += clf.predict(e.vector) == e.tier
            knn_ok += knn_predict(train_set, e.vector, k) == e.tier
    model = train(examples, epochs=epochs) if clf_ok > knn_ok else None
    return Comparison(
        examples=len(examples),
        test=len(test_set),
        classifier_correct=clf_ok,
        knn_correct=knn_ok,
        embed_model=embed_model,
        dims=dims,
        checked_at=now,
        model=model,
    )


class Gate:
    """The cached comparison (step 4 of the gate). Thread-safe; one per worker."""

    def __init__(self, *, recheck: timedelta = RECHECK, epochs: int = EPOCHS) -> None:
        self.recheck = recheck
        self.epochs = epochs
        self._lock = threading.Lock()
        self._cached: Comparison | None = None
        self.computed = 0  # how many comparisons ran (for tests and the status line)

    @property
    def cached(self) -> Comparison | None:
        return self._cached

    def comparison(
        self, store: Any, *, embed_model: str, dims: int, k: int, exclude: int = -1
    ) -> Comparison:
        now = store.now()
        with self._lock:
            c = self._cached
            fresh = (
                c is not None
                and c.embed_model == embed_model
                and c.dims == dims
                and now - c.checked_at < self.recheck
            )
            if not fresh:
                examples = load_examples(store, model=embed_model, dims=dims, exclude=exclude)
                c = compare(
                    examples,
                    k=k,
                    embed_model=embed_model,
                    dims=dims,
                    now=now,
                    epochs=self.epochs,
                )
                self._cached = c
                self.computed += 1
                log.info(
                    "classifier router: %s on %d accepted tickets (%s, %d dims): %s",
                    "using the classifier" if c.use_classifier else "keeping knn",
                    c.examples,
                    embed_model,
                    dims,
                    c.summary(),
                )
            assert c is not None
            return c


# --------------------------------------------------------------------------- route


def route_classifier(
    ticket: Any,
    *,
    store: Any,
    config: Config | Sequence[ModelEndpoint],
    embedder: ModelEndpoint,
    gate: Gate,
    knn_route: Route,
    min_tickets: int = DEFAULT_MIN_TICKETS,
    k: int = knn.DEFAULT_K,
) -> Route | None:
    """The classifier's pick for ``ticket``, or None to keep ``knn_route``. Call it only
    after ``knn.route_knn`` returned ``knn_route`` (that call stored the ticket's vector).
    Never raises; makes no model call."""
    try:
        return _route_classifier(
            ticket,
            store=store,
            config=config,
            embedder=embedder,
            gate=gate,
            knn_route=knn_route,
            min_tickets=min_tickets,
            k=k,
        )
    except Exception as exc:  # routing must never take the worker down
        log.warning(
            "classifier router: keeping knn for ticket %s (%s)", getattr(ticket, "id", "?"), exc
        )
        return None


def _route_classifier(
    ticket: Any,
    *,
    store: Any,
    config: Config | Sequence[ModelEndpoint],
    embedder: ModelEndpoint,
    gate: Gate,
    knn_route: Route,
    min_tickets: int,
    k: int,
) -> Route | None:
    tid = getattr(ticket, "id", None)
    exclude = int(tid) if isinstance(tid, int) else -1
    if knn_route.source != knn.SOURCE:
        return None
    vec = knn.stored_vector(
        store, exclude, model=embedder.name, digest=knn.text_digest(knn.request_text(ticket))
    )
    if vec is None:
        return None
    dims = len(vec)
    pool = knn.pool_size(store, model=embedder.name, dims=dims, exclude=exclude)
    if pool < max(1, int(min_tickets)):
        return None
    c = gate.comparison(store, embed_model=embedder.name, dims=dims, k=k, exclude=exclude)
    if c.model is None:
        return None
    probs = c.model.probabilities(vec)
    wanted = c.model.predict(vec)
    models = config.models if isinstance(config, Config) else tuple(config)
    tier, note, has_model = resolve_tier(wanted, configured_tiers(models))
    shown = ", ".join(f"t{t} {p:.2f}" for t, p in sorted(probs.items()))
    reason = (
        f"classifier: logistic regression over {c.examples} accepted tickets "
        f"({embedder.name}; {c.summary()}, checked {c.checked_at:%Y-%m-%d}): {shown} "
        f"-> tier {wanted}; knn wanted tier {knn_route.requested_tier}"
    )
    if note:
        reason += f"; {note}"
    with tracing.route_span(tier, reason) as span:
        for key, value in {
            "route.source": SOURCE,
            "route.requested_tier": wanted,
            "route.has_model": has_model,
            "route.embed_model": embedder.name,
            "route.classifier.held_out": c.test,
            "route.classifier.correct": c.classifier_correct,
            "route.classifier.knn_correct": c.knn_correct,
        }.items():
            span.set_attribute(key, value)
    log.info("classifier router: ticket %s -> tier %d (%s)", tid, tier, reason)
    return Route(
        tier=tier,
        reason=reason,
        source=SOURCE,
        label=_TIER_LABELS.get(wanted),
        classifier=f"logreg:{embedder.name}",
        requested_tier=wanted,
        has_model=has_model,
    )


__all__ = [
    "DEFAULT_ENABLED",
    "DEFAULT_MIN_TICKETS",
    "HOLDOUT_FRACTION",
    "RECHECK",
    "SOURCE",
    "Comparison",
    "Example",
    "Gate",
    "LogReg",
    "chronological_split",
    "compare",
    "knn_predict",
    "load_examples",
    "route_classifier",
    "train",
]
