"""``fman status`` and ``fman repo set-verify`` (todo #14, #21).

``render_status`` reads the store only (open it with ``read_only=True``), so it works
whether or not the worker is running. Since #21 it also shows which tickets wait on file
locks held by other tickets (#19) or on a spend approval (#18), and ends with the cloud
spend line when given the config. Wave 6b: a ticket waiting on ``needs-info`` (#44) or on
GitHub CI (#41) says so, and with the config the sandbox line (#40) comes last, loudly
when the sandbox is off. Wave 9: the champion lines (#24, including "PROMPT CHANGED ...
(not promoted)") and the running experiments count. ``set_verify_command`` saves a repo's verify
command and lifts the "no verify command" flag from that repo's open tickets, so the
running worker picks them up on its next pass. #51: one ``Profile <repo>: ...`` line per
repo whose profile is not ``default``, and ``set_profile`` for ``fman repo set-profile``.
Wave 15: with ``ollama_host`` the "Ollama: N loaded (X GB)" / "Ollama: unreachable (...)"
line (#35, ``resources.ollama``), and with ``plugins`` one line per plugin (#45).
Wave 21: the spend line carries chat spend (#71) and a ticket with a carried spend
approval shows what it still allows (#82, ``Spend.approval_note``).
Wave 23 (#34 ``ab:pair``, GH #16): each side of a pair is its own ticket line, tagged
``pair #k A · <model>`` / ``pair #k B · <model>`` (``pair_tag``); once both sides' PRs
are ready for you, both lines say ``WAITING: on your pick: merge #x or #y``
(``pair_pick_wait``). Unpaired lines are unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

import httpx

from foreman_inator.config import Config
from foreman_inator.db.store import COLUMNS, Attempt, Pair, Store, Ticket
from foreman_inator.experiments import variants
from foreman_inator.locks import Locks
from foreman_inator.plugins import PluginSet
from foreman_inator.plugins import status_lines as plugin_status_lines
from foreman_inator.resources.ollama import STATUS_TIMEOUT_SECONDS, Unreachable, loaded_models
from foreman_inator.routing import profile as prof
from foreman_inator.runner import sandbox
from foreman_inator.spend import FLAG_SPEND_CAP, Spend
from foreman_inator.tickets.lifecycle import clarify_cursor
from foreman_inator.worker_policy import FLAG_NO_VERIFY
from foreman_inator.worker_queue import open_pr_wait_text

_TITLES = {
    "todo": "TODO",
    "in_progress": "IN PROGRESS",
    "testing": "TESTING (waiting for your review)",
    "done": "DONE",
    "dropped": "DROPPED",
}
TITLE_WIDTH = 40


def _attempt_text(attempts: Sequence[Attempt], budget_usd: float | None = None) -> str:
    if not attempts:
        return "not started"
    a = attempts[-1]
    if a.ended_at is None:
        state = "running"
    elif a.outcome is None:
        state = "checks passed" if a.tests_passed else "finished"
    else:
        state = a.outcome.replace("_", " ")
    cost = f", ${a.cost_usd:.2f}" if a.cost_usd else ""
    if cost and budget_usd is not None:  # #58: the attempt's spend against its budget
        cost += f" of ${budget_usd:.2f}"
    return f"attempt {len(attempts)} ({a.trigger}, {a.model}, tier {a.tier}{cost}): {state}"


def _line(
    t: Ticket,
    attempts: Sequence[Attempt],
    waits: dict[int, tuple[int, ...]] | None = None,
    store: Store | None = None,
    budget_usd: float | None = None,
    open_pr_cap: int | None = None,
    chat_waits: dict[int, tuple[int, ...]] | None = None,
    spend: Spend | None = None,
    pairs: dict[int, Pair] | None = None,
) -> str:
    title = " ".join(t.title.split())
    if len(title) > TITLE_WIDTH:
        title = title[: TITLE_WIDTH - 1] + "…"
    parts = [f"  #{t.id:<4} {t.repo}#{t.issue_number:<5} {title:<{TITLE_WIDTH}}"]
    pair = (pairs or {}).get(t.id)
    if pair is not None:
        parts.append(pair_tag(pair, t, attempts))
    parts.append(_attempt_text(attempts, budget_usd))
    if t.pr_number is not None:
        parts.append(f"PR #{t.pr_number}")
    line = "  ".join(parts)
    if t.column in ("done", "dropped"):
        return line  # #50: a closed ticket's flag is history, not something for the user
    if t.flag_reason and t.flag_reason.startswith(FLAG_SPEND_CAP):
        line += (
            f"\n         WAITING: spend approval ({t.flag_reason}); run `fman approve-spend {t.id}`"
        )
    elif t.flag_reason:
        line += f"\n         FLAGGED: {t.flag_reason}"
    note = spend.approval_note(t.id) if spend is not None else None
    if note:  # #82: a paused or cut-short ticket's carried approval: what it still allows
        line += f"\n         SPEND: {note} (carried to the next attempt)"
    blockers = (waits or {}).get(t.id)
    if blockers:
        held = ", ".join(f"#{b}" for b in blockers)
        line += f"\n         WAITING: on files held by ticket(s) {held}"
        line += _release_hint(blockers, store)
    chats = (chat_waits or {}).get(t.id)
    if chats:
        line += f"\n         WAITING: on files held by {_chat_holders(chats)}"
        line += " (released on /pr, /discard or when the session ends)"
    for why in _waits(t, attempts, store, open_pr_cap):
        line += f"\n         WAITING: {why}"
    pick = pair_pick_wait(store, pair) if pair is not None and store is not None else None
    if pick:
        line += f"\n         WAITING: {pick}"
    return line


# --------------------------------------------------------------------- ab:pair (#34)


def pair_map(store: Store) -> dict[int, Pair]:
    """Every pair (any status) by both of its ticket ids; {} on a database from before
    migration 016."""
    try:
        pairs = store.list_pairs()
    except Exception:
        return {}
    out: dict[int, Pair] = {}
    for p in pairs:
        out[p.ticket_a] = p
        out[p.ticket_b] = p
    return out


def pair_side(pair: Pair, ticket_id: int) -> str:
    """``A`` or ``B``."""
    return "A" if ticket_id == pair.ticket_a else "B"


def _opponent_model(pair: Pair) -> str:
    from foreman_inator.experiments.pair import _model_of

    return _model_of(pair.opponent_variant)


def pair_model(pair: Pair, t: Ticket, attempts: Sequence[Attempt]) -> str | None:
    """The side's model: its latest attempt's; before one, side B's opponent."""
    if attempts:
        return attempts[-1].model
    if t.id == pair.ticket_b:
        return _opponent_model(pair)
    return None


def pair_tag(pair: Pair, t: Ticket, attempts: Sequence[Attempt]) -> str:
    """``pair #3 A · qwen3-coder`` (``, won`` / ``, lost`` once decided)."""
    model = pair_model(pair, t, attempts)
    tag = f"pair #{pair.id} {pair_side(pair, t.id)}" + (
        f" · {' '.join(model.split())}" if model else ""
    )
    if pair.status == "decided" and pair.winner_ticket_id is not None:
        tag += ", won" if pair.winner_ticket_id == t.id else ", lost"
    return tag


def pair_side_ready(store: Store, t: Ticket) -> bool:
    """The side's PR is waiting on you: in testing with a PR, the attempt over and past
    CI and the AI review (reviewed, or handed over with a flag). A spend pause is not."""
    if t.column != "testing" or t.pr_number is None:
        return False
    if (t.flag_reason or "").startswith(FLAG_SPEND_CAP):
        return False
    attempts = store.list_attempts(t.id)
    last = attempts[-1] if attempts else None
    if last is None or last.ended_at is None:
        return False
    return bool(t.flag_reason) or bool(store.list_reviews(attempt_id=last.id, shadow=False))


def pair_pick_wait(store: Store, pair: Pair) -> str | None:
    """``on your pick: merge #12 or #13`` once both sides of a running pair are ready."""
    if pair.status != "running":
        return None
    a, b = store.get_ticket(pair.ticket_a), store.get_ticket(pair.ticket_b)
    if not (pair_side_ready(store, a) and pair_side_ready(store, b)):
        return None
    return f"on your pick: merge #{a.pr_number} or #{b.pr_number}"


def _chat_holders(chats: Sequence[int]) -> str:
    """#71 decision 6: ``chat session 3`` or ``chat sessions 3, 4``."""
    ids = ", ".join(str(c) for c in chats)
    return f"chat session {ids}" if len(chats) == 1 else f"chat sessions {ids}"


def _release_hint(blockers: Sequence[int], store: Store | None) -> str:
    """#55: for a blocker that is flagged for the user, say what releases its files.
    #83: a blocker paused at a spend cap is waiting, not flagged: name the approve command."""
    if store is None:
        return ""
    hints = []
    for b in blockers:
        bt = store.get_ticket(b)
        if bt.flag_reason and bt.flag_reason.startswith(FLAG_SPEND_CAP):
            hints.append(f"#{b} waits for spend approval: run `fman approve-spend {b}`")
        elif bt.flag_reason:
            hints.append(f"#{b} is flagged: close or re-label issue {bt.repo}#{bt.issue_number}")
    return f" ({'; '.join(hints)})" if hints else ""


def _waits(
    t: Ticket, attempts: Sequence[Attempt], store: Store | None, open_pr_cap: int | None = None
) -> list[str]:
    """#44's needs-info wait, #41's CI wait and (with ``open_pr_cap``, the config's
    ``max_open_prs_per_repo``) #77's open-PR cap wait, from the store alone."""
    if store is None or t.flag_reason:
        return []
    if t.column == "todo" and store.get_cursor(t.repo, clarify_cursor(t.id)) == "asked":
        return ["needs-info: answer the question on the issue, or remove the label"]
    last = attempts[-1] if attempts else None
    if (
        t.column == "testing"
        and last is not None
        and last.tests_passed
        and last.outcome is None
        and last.ci_status is None
        and not store.list_reviews(attempt_id=last.id, shadow=False)
    ):
        return ["GitHub CI on the PR head"]
    if open_pr_cap is not None:
        held = open_pr_wait_text(store, t, open_pr_cap)
        if held is not None:
            return [held]
    return []


def render_status(
    store: Store,
    *,
    closed_limit: int = 5,
    config: Config | None = None,
    ollama_host: str | None = None,
    ollama_transport: httpx.BaseTransport | None = None,
    plugins: PluginSet | None = None,
) -> str:
    """Tickets by column with their current attempt, flags, waits and PR numbers; with
    ``config``, the cloud spend line last. With ``ollama_host`` (see ``local_ollama_host``)
    the Ollama line, asked over ``ollama_transport`` (tests); with ``plugins``, the plugin
    lines. Both are off by default, so no caller asks Ollama by accident."""
    tickets = store.list_tickets()
    locks = Locks(store)
    waiters = [w for repo in sorted({t.repo for t in tickets}) for w in locks.waiting(repo)]
    waits = {w.ticket_id: w.blockers for w in waiters}  # () when only a chat holds the files
    chat_waits = {w.ticket_id: w.chats for w in waiters if w.chats}
    budget = config.attempt_budget_usd if config is not None else None
    cap = int(config.max_open_prs_per_repo) if config is not None else None
    spend = Spend(store, config) if config is not None else None
    pairs = pair_map(store)
    by_col: dict[str, list[Ticket]] = {c: [] for c in COLUMNS}
    for t in tickets:
        by_col[t.column].append(t)
    out: list[str] = []
    for col in COLUMNS:
        items = by_col[col]
        shown = items
        if col in ("done", "dropped") and closed_limit >= 0:
            shown = items[-closed_limit:] if closed_limit else []
        header = f"{_TITLES[col]} ({len(items)})"
        if len(shown) < len(items):
            header += f", latest {len(shown)}"
        out.append(header)
        out.extend(
            _line(t, store.list_attempts(t.id), waits, store, budget, cap, chat_waits, spend, pairs)
            for t in shown
        )
        if not items:
            out.append("  (none)")
        out.append("")
    flagged = sum(
        1
        for t in tickets
        if t.flag_reason
        and t.column not in ("done", "dropped")
        and not t.flag_reason.startswith(FLAG_SPEND_CAP)
    )
    waiting = sum(
        1
        for t in tickets
        if t.column not in ("done", "dropped")
        and (
            t.id in waits
            or (t.flag_reason or "").startswith(FLAG_SPEND_CAP)
            or _waits(t, store.list_attempts(t.id), store, cap)
            or (t.id in pairs and pair_pick_wait(store, pairs[t.id]))
        )
    )
    out.append(f"{len(tickets)} ticket(s), {flagged} flagged for you, {waiting} waiting.")
    out.extend(_champion_lines(store))
    out.extend(profile_lines(store, sorted({t.repo for t in tickets})))
    out.extend(local_only_lines(store))
    if config is not None and spend is not None:
        out.append(repos_line(config))
        out.append(spend.summary().status_line())  # wave 21: with the chat figure
        line = sandbox.describe(
            config.sandbox, dict(config.sandbox_network), dict(config.sandbox_install)
        )  # type: ignore[arg-type]
        out.append(f"!!! {line} !!!" if config.sandbox == "off" else line)
    if ollama_host is not None:
        out.append(ollama_line(ollama_host, transport=ollama_transport))
    if plugins is not None:
        out.extend(plugin_status_lines(plugins))
    return "\n".join(out)


def local_ollama_host(config: Config) -> str | None:
    """#35: the base URL of the first local model in ``config`` (its Ollama), or None
    when every model is cloud (then there is no Ollama to ask)."""
    for m in config.models:
        if m.kind == "local":
            return m.base_url
    return None


def ollama_line(host: str, *, transport: httpx.BaseTransport | None = None) -> str:
    """#35: ``Ollama: 2 loaded (21.3 GB): a, b``, ``Ollama: slow (no reply ... in 5s)``
    (it took the connection but did not answer in time: busy, not down) or ``Ollama:
    unreachable (reason)``. Neither is ever shown as zero models."""
    got = loaded_models(host, transport=transport, timeout=STATUS_TIMEOUT_SECONDS)
    if isinstance(got, Unreachable):
        return f"Ollama: {'slow' if got.slow else 'unreachable'} ({got.reason})"
    gb = sum(m.size for m in got) / 1e9
    line = f"Ollama: {len(got)} loaded ({gb:.1f} GB)"
    if got:
        line += ": " + ", ".join(m.name for m in got)
    return line


def repos_line(config: Config) -> str:
    """#56: how many repos the worker polls, loudly when more than the warning threshold."""
    from foreman_inator.config import REPOS_WARN_THRESHOLD, listing_requests_per_hour

    n = len(config.repos)
    line = f"Repos: {n} in config.toml (`fman repo list`)"
    if n > REPOS_WARN_THRESHOLD:
        per_hour = listing_requests_per_hour(n, config.poll_interval_seconds)
        line += f": about {per_hour:,} GitHub requests an hour; `fman repo remove` trims it"
    return line + "."


def _champion_lines(store: Store) -> list[str]:
    """#24: the champion per role, with the "PROMPT CHANGED ... (not promoted)" line when a
    champion's prompt file was edited since it was promoted; running experiments."""
    try:
        lines = [f"Champion {line}" for line in variants.status_lines(store)]
        running = store.list_experiments(status="running")
    except Exception as exc:  # an old database without the wave 8 tables
        return [f"(champions unavailable: {type(exc).__name__})"]
    if running:
        lines.append(f"Experiments running: {len(running)} (`fman experiment status` for details)")
    return lines


def profile_lines(store: Store, repos: Sequence[str]) -> list[str]:
    """#51: ``Profile me/toy: prototype (writer fast, review off)`` for each repo (of
    ``repos``, plus every repo with a stored profile) whose profile is not ``default``."""
    try:
        stored = [
            r["repo"]
            for r in store.query(
                "SELECT repo FROM repo_settings"
                " WHERE writer_mode IS NOT NULL OR review_mode IS NOT NULL"
            )
        ]
    except Exception:  # a database from before migration 008
        return []
    lines = []
    for repo in sorted(set(repos) | set(stored)):
        p = prof.profile_of(store.get_repo_settings(repo))
        if p != prof.DEFAULT:
            lines.append(f"Profile {repo}: {prof.describe(p)}")
    return lines


def local_only_lines(store: Store) -> list[str]:
    """#49: ``Local only (cloud = false): me/secret, me/other`` when any repo is."""
    try:
        repos = sorted(store.local_only_repos())
    except Exception:  # a database from before migration 010
        return []
    if not repos:
        return []
    return [f"Local only (cloud = false, `fman repo set-cloud`): {', '.join(repos)}"]


def set_profile(
    store: Store,
    repo: str,
    preset: str | None = None,
    *,
    writer: str | None = None,
    review: str | None = None,
) -> prof.Profile:
    """``fman repo set-profile <repo> [<preset>] [--writer W] [--review R]``: save the
    repo's profile and return it. ``ProfileError`` (an unknown preset or mode) names the
    valid ones; nothing is saved then. Takes effect on the worker's next decision."""
    current = prof.profile_of(store.get_repo_settings(repo))
    p = prof.resolve(preset, writer=writer, review=review, current=current)
    store.set_profile(repo, p.writer, p.review)
    return p


def set_verify_command(store: Store, repo: str, command: str | None) -> list[int]:
    """Save ``repo``'s verify command (None clears it) and unflag the open tickets that
    were waiting for one. Returns their ids."""
    cleared: list[int] = []
    with store.transaction():
        store.set_verify_command(repo, command.strip() if command else None)
        if command and command.strip():
            for t in store.list_tickets(column=("todo", "in_progress", "testing"), repo=repo):
                if t.flag_reason and t.flag_reason.startswith(FLAG_NO_VERIFY):
                    store.clear_flag(t.id)
                    cleared.append(t.id)
    return cleared


__all__ = [
    "local_ollama_host",
    "local_only_lines",
    "ollama_line",
    "pair_map",
    "pair_model",
    "pair_pick_wait",
    "pair_side",
    "pair_side_ready",
    "pair_tag",
    "profile_lines",
    "render_status",
    "repos_line",
    "set_profile",
    "set_verify_command",
]
