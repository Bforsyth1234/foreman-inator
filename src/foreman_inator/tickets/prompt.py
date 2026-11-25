"""The writer's session prompt for one attempt (todo #13).

``build_writer_prompt`` is a pure function: store dataclasses and a few strings in, a
``WriterPrompt`` out. It reads no files, so tests snapshot it and replays (#25) call it
with recorded inputs. The thin helpers below it (``load_writer_template``,
``read_conventions``, ``read_failure_output``, ``load_history``) do the reading.

What the writer sees, by situation (SPEC "Ticket lifecycle", "Routing and escalation"):

* first attempt (trigger ``first`` or ``explore``, no history): the issue and the rules.
* retry after failed checks (history's last attempt ended ``failed_checks`` and the new
  attempt is not an escalation): plus the end of the check output.
* kickback (trigger ``kickback`` or ``ai_kickback``): plus the latest note, its tag and
  what the tag means; and when the previous attempt (a kickback-triggered one, retried
  with its trigger kept) failed its checks, the end of that check output too (#21).
* escalation (trigger ``escalation``): plus the facts of every earlier attempt, the last
  check output if it failed, and every review note.

* rebase (trigger ``rebase``, #43): the base branch moved and the PR conflicts; the change
  was replayed onto the new base (``worktree.replay_onto_base``). Plus the list of files
  with conflict markers (``conflicted_files``), and the check output when the previous
  rebase attempt failed its checks. No history or notes: the job is to keep the change.

A user's kickback note can carry the review's line comments (#42): the stored note is the
review body, ``lifecycle.LINE_COMMENTS_MARKER``, then the comments already rendered as
``path:line``, the last 3 hunk lines and the text (``lifecycle.format_line_comments``).
The builder splits it there and shows the comments in their own fenced block under the
note, defused and cut to ``LINE_COMMENTS_BUDGET`` with a visible marker.

Invariant 7f, mirrored: the writer gets the reviewer's and the user's notes, never its
own earlier reasoning or transcript. ``PriorAttempt`` has no field that could carry one,
``build_writer_prompt`` takes keyword-only arguments, and ``load_history`` reads only the
store and the saved verify output, never recordings or traces.

Every variable-length input is cut to a character budget (``*_BUDGET`` below) with a
visible marker, so a huge issue cannot blow a local model's context. Untrusted text is
fenced between BEGIN/END markers, and marker look-alikes inside it are defused.

All wording lives in ``prompts/writer-v1.md``; this module only picks and fills its
sections. The prompt hash is of that file, not of the rendered text, so one file
version is one hash and one variant (``<model>/<prompt-name>@<hash[:8]>``, #24).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from string import Template
from typing import TYPE_CHECKING, Any, Literal

from fde_kit.prompts import PromptRef, load_prompt

from foreman_inator.db.store import KICKBACK_BY, TAGS, Attempt, Kickback, Review, Store, Ticket
from foreman_inator.tickets.lifecycle import clarify_qa_cursor, split_kickback_note

if TYPE_CHECKING:  # conventions imports this module's bounding helpers
    from foreman_inator.github.client import Comment
    from foreman_inator.tickets.conventions import Conventions

log = logging.getLogger(__name__)

WRITER_PROMPT_NAME = "writer-v1"

# Character budgets. Worst case all together is about 40k characters (~10k tokens),
# which leaves room in a 32k-token local model for the files the agent reads.
TITLE_BUDGET = 300
ISSUE_BODY_BUDGET = 12_000
CONVENTIONS_BUDGET = 6_000  # shared by every rules source (tickets/conventions.py)
FAILURE_OUTPUT_BUDGET = 4_000  # the tail is kept: the end of a test run says what failed
NOTE_BUDGET = 2_000  # per note
NOTES_TOTAL_BUDGET = 6_000  # all notes; the newest are kept
CLARIFICATION_BUDGET = 5_100  # the Q&A text (#44; ``clarify.QA_BUDGET`` plus room)
LINE_COMMENTS_BUDGET = 6_000  # a review's line comments (#42), the first ones kept
ISSUE_COMMENT_BUDGET = 2_000  # per issue comment (#25 / todo #48)
ISSUE_COMMENTS_BUDGET = 6_000  # all issue comments; the newest are kept
IMAGES_BUDGET = 14_000  # the images block (#78, ``images.render``: up to 8 descriptions)
OWNED_FILES_MAX = 40  # list entries
OWNED_FILE_BUDGET = 200  # per path
HISTORY_MAX = 10  # attempts listed; the newest are kept

CONVENTIONS_FILES = ("AGENTS.md", "CLAUDE.md")  # legacy read_conventions: first one wins

Situation = Literal["first", "retry", "kickback", "escalation", "rebase"]

_SECTION = re.compile(r"^<!-- section: ([a-z_]+) -->$", re.MULTILINE)
_MARKER = re.compile(r"-{3,}\s*(?:BEGIN|END)\b", re.IGNORECASE)
_HTML_COMMENT = re.compile(r"<!--.*?(?:-->|$)", re.DOTALL)
_QA_MARKER = "<!-- foreman-inator:needs-info"  # ``clarify.QUESTION_MARKER``'s prefix (#44)


# --------------------------------------------------------------------------- inputs


@dataclass(frozen=True, slots=True)
class Note:
    """One review note sent to the writer: who wrote it, its tag, its text."""

    by: str  # "user" or "reviewer" (store KICKBACK_BY)
    tag: str | None  # store TAGS, or None
    text: str

    def __post_init__(self) -> None:
        if self.by not in KICKBACK_BY:
            raise ValueError(f"note by must be one of {KICKBACK_BY}, not {self.by!r}")
        if self.tag is not None and self.tag not in TAGS:
            raise ValueError(f"note tag must be one of {TAGS} or None, not {self.tag!r}")
        if not isinstance(self.text, str):
            raise TypeError("note text must be a string")

    @classmethod
    def from_kickback(cls, k: Kickback) -> Note:
        return cls(by=k.by, tag=k.tag, text=k.note or "")

    @classmethod
    def from_review(cls, r: Review) -> Note:
        """An AI review that asked for changes, for when no reviewer kickback row exists."""
        return cls(by="reviewer", tag=r.tag, text=r.note or "")


@dataclass(frozen=True, slots=True)
class PriorAttempt:
    """What the writer may know about one earlier attempt: its stored row, the notes
    that judged it, and the end of its check output. Deliberately nothing else: no
    transcript, no model messages, no reasoning (invariant 7f, mirrored)."""

    attempt: Attempt
    notes: tuple[Note, ...] = ()
    failure_output: str | None = None  # verify output tail, when its checks failed

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, Attempt):
            raise TypeError("PriorAttempt.attempt must be a store Attempt")
        if not isinstance(self.notes, tuple) or not all(isinstance(n, Note) for n in self.notes):
            raise TypeError("PriorAttempt.notes must be a tuple of Note")
        if self.failure_output is not None and not isinstance(self.failure_output, str):
            raise TypeError("PriorAttempt.failure_output must be a string or None")


@dataclass(frozen=True, slots=True)
class WriterPrompt:
    """The rendered session prompt plus the template's identity for the attempt row."""

    text: str
    prompt_name: str
    prompt_hash: str  # sha256 of the template file (fde-kit prompt_hash)
    situation: Situation

    def variant_id(self, model: str) -> str:
        """``<model>/<prompt-name>@<hash[:8]>``, the format #24 uses for variants."""
        return f"{model}/{self.prompt_name}@{self.prompt_hash[:8]}"


# --------------------------------------------------------------------------- template


def load_writer_template(name: str = WRITER_PROMPT_NAME) -> PromptRef:
    """Load a writer prompt from the package's ``prompts/`` folder (package data)."""
    folder = resources.files("foreman_inator") / "prompts"
    with resources.as_file(folder / f"{name}.md") as path:
        return load_prompt(str(path))


def _sections(text: str) -> dict[str, Template]:
    parts = _SECTION.split(text)
    # parts = [preamble, name1, body1, name2, body2, ...]; the preamble is a comment.
    out: dict[str, Template] = {}
    for name, body in zip(parts[1::2], parts[2::2], strict=True):
        if name in out:
            raise ValueError(f"writer template has section {name!r} twice")
        out[name] = Template(body.strip("\n"))
    return out


# --------------------------------------------------------------------------- bounding


def clip_head(text: str, budget: int) -> str:
    """Keep the first ``budget`` characters, with a marker saying how much was cut."""
    text = text.replace("\r\n", "\n").strip()
    if len(text) <= budget:
        return text
    cut = len(text) - budget
    return f"{text[:budget].rstrip()}\n[... truncated: {cut} more characters not shown ...]"


def clip_tail(text: str, budget: int) -> str:
    """Keep the last ``budget`` characters, with a marker saying how much was cut."""
    text = text.replace("\r\n", "\n").strip()
    if len(text) <= budget:
        return text
    cut = len(text) - budget
    return f"[... truncated: {cut} earlier characters not shown ...]\n{text[-budget:].lstrip()}"


def defuse(text: str) -> str:
    """Neutralize BEGIN/END fence look-alikes so fenced text cannot close its fence."""
    return _MARKER.sub("[marker removed]", text)


# --------------------------------------------------------------------------- builder


def situation_for(attempt: Attempt, history: Sequence[PriorAttempt]) -> Situation:
    """Which section the attempt gets. The store's trigger enum has no "retry" value, so
    a same-tier retry after failed checks is recognized from the previous attempt."""
    if attempt.trigger == "rebase":
        return "rebase"
    if attempt.trigger == "escalation":
        return "escalation"
    if attempt.trigger in ("kickback", "ai_kickback"):
        return "kickback"
    if history and history[-1].attempt.outcome == "failed_checks":
        return "retry"
    return "first"


def build_writer_prompt(
    ticket: Ticket,
    attempt: Attempt,
    history: Sequence[PriorAttempt] = (),
    *,
    verify_command: str | None,
    conventions: str | Conventions | None = None,
    conventions_name: str = "AGENTS.md",
    template: PromptRef | None = None,
    conflicted_files: Sequence[str] = (),
    clarification: str | None = None,
    issue_comments: str | None = None,
    images: str | None = None,
) -> WriterPrompt:
    """Render the writer's session prompt for ``attempt`` of ``ticket``.

    ``history`` is the ticket's earlier attempts, oldest first (``load_history``).
    ``verify_command`` is ``VerifyCommand.display`` (None when the repo has none).
    ``conventions`` is either ``conventions.gather``'s result (#38, #40: every source,
    labelled, already fitted to the shared budget; ``conventions_name`` is then ignored)
    or, the legacy form, the text of one file from ``read_conventions``.
    ``template`` defaults to the packaged ``writer-v1``; replays pass a recorded one.
    ``conflicted_files`` is ``Replay.conflicted`` for a ``rebase`` attempt (#43).
    ``clarification`` is the Q&A text of the worker's clarifying questions (#44,
    ``clarify.answered``), shown right after the issue on every attempt.
    ``issue_comments`` is ``issue_comments_text``'s result (#25, todo #48): the allowed
    users' comments, already clipped to ``ISSUE_COMMENTS_BUDGET``; it goes after the
    issue body inside the issue fence, defused.
    ``images`` is ``images.images_text``'s result (#78): a description of each image an
    allowed user attached, labelled with its source; it goes last inside the issue
    fence, defused, so text read off an image is fenced like the issue body.
    """
    if attempt.ticket_id != ticket.id:
        raise ValueError(f"attempt {attempt.id} belongs to ticket {attempt.ticket_id}")
    for prior in history:
        if not isinstance(prior, PriorAttempt):
            raise TypeError("history items must be PriorAttempt")
        if prior.attempt.ticket_id != ticket.id or prior.attempt.id >= attempt.id:
            raise ValueError(f"attempt {prior.attempt.id} is not an earlier attempt here")

    ref = template or load_writer_template()
    s = _sections(ref.text)
    situation = situation_for(attempt, history)
    numbers = {p.attempt.id: i for i, p in enumerate(history, start=1)}
    blocks: list[str] = []

    blocks.append(s["intro"].substitute(repo=ticket.repo))
    blocks.append(
        s["issue"].substitute(
            issue_number=ticket.issue_number,
            issue_title=defuse(clip_head(" ".join(ticket.title.split()), TITLE_BUDGET)),
            issue_body=_issue_body(ticket.request, issue_comments, images),
        )
    )
    if clarification and clarification.strip():
        blocks.append(
            s["clarification"].substitute(
                clarification=defuse(clip_head(clarification.strip(), CLARIFICATION_BUDGET))
            )
        )
    if isinstance(conventions, str):
        if conventions.strip():
            blocks.append(
                s["conventions"].substitute(
                    conventions_name=conventions_name,
                    conventions=defuse(clip_head(conventions, CONVENTIONS_BUDGET)),
                )
            )
    elif conventions is not None and not conventions.empty:
        blocks.append(
            s["conventions"].substitute(
                conventions_name=conventions.names, conventions=conventions.render()
            )
        )
    if ticket.owned_files:
        blocks.append(s["owned_files"].substitute(owned_files=_owned_files(ticket.owned_files)))

    shown_note: tuple[int, int] | None = None  # (history index, note index) shown above
    last = history[-1] if history else None
    if situation == "first":
        blocks.append(s["first"].substitute())
    elif situation == "retry":
        blocks.append(s["retry"].substitute(failure_output=_failure(last)))
    elif situation == "kickback":
        found = _latest_note(history)
        if found is None:
            raise ValueError(f"attempt {attempt.id} is a kickback but no note was given")
        shown_note, note = found
        blocks.append(
            s["kickback"].substitute(
                kickback_by=s[f"by_{note.by}"].substitute(),
                kickback_tag=note.tag or "none",
                tag_meaning=s[f"tag_{note.tag or 'none'}"].substitute(),
                kickback_note=_note_text(s, note),
            )
        )
        comments = _line_comments(s, note)
        if comments:
            blocks.append(comments)
        if last is not None and last.attempt.outcome == "failed_checks":
            # A same-tier retry of a kickback-triggered attempt keeps the trigger, so it
            # is still a "kickback" situation, but it must also see why its checks failed.
            blocks.append(s["escalation_failure"].substitute(failure_output=_failure(last)))
    elif situation == "rebase":
        if conflicted_files:
            blocks.append(s["rebase"].substitute(conflicted_files=_owned_files(conflicted_files)))
        else:
            blocks.append(s["rebase_clean"].substitute())
        if last is not None and last.attempt.outcome == "failed_checks":
            blocks.append(s["escalation_failure"].substitute(failure_output=_failure(last)))
    else:
        blocks.append(s["escalation"].substitute())
        if last is not None and last.attempt.outcome == "failed_checks":
            blocks.append(s["escalation_failure"].substitute(failure_output=_failure(last)))

    if situation not in ("first", "rebase") and history:
        lines = [
            s["history_line"].substitute(
                number=numbers[p.attempt.id],
                tier=p.attempt.tier,
                model=p.attempt.model,
                result=s[f"result_{p.attempt.outcome or 'unknown'}"].substitute(),
            )
            for p in history[-HISTORY_MAX:]
        ]
        blocks.append(s["history"].substitute(history="\n".join(lines)))
        notes = _notes_block(s, history, numbers, skip=shown_note)
        if notes:
            blocks.append(s["notes"].substitute(notes=notes))

    blocks.append(
        s["steps"].substitute(
            verify_command=verify_command.strip()
            if verify_command and verify_command.strip()
            else s["no_verify"].substitute()
        )
    )
    return WriterPrompt(
        text="\n\n".join(b.strip("\n") for b in blocks) + "\n",
        prompt_name=ref.name,
        prompt_hash=ref.sha256,
        situation=situation,
    )


def _issue_body(request: str, comments: str | None, images: str | None = None) -> str:
    body = defuse(clip_head(request, ISSUE_BODY_BUDGET)) or "(no body)"
    if comments and comments.strip():
        body += "\n\n" + defuse(clip_head(comments, ISSUE_COMMENTS_BUDGET + 200))
    if images and images.strip():
        body += "\n\n" + defuse(clip_head(images, IMAGES_BUDGET))
    return body


def _stamp(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _same_login(a: str | None, b: str | None) -> bool:
    return a is not None and b is not None and a.lower() == b.lower()


def issue_comments_text(
    comments: Iterable[Comment],
    *,
    is_allowed: Callable[[str], bool],
    bot_login: str | None,
    clarification: str | None = None,
) -> str | None:
    """The issue's comments for the writer (#25, todo #48), or None when none count.

    Only comments by allowed users count (invariant 7b); the bot's own comments never
    do, nor any comment carrying the #44 question marker. An answer already shown in
    the clarification block (``clarification``, the stored Q&A text, names each answer
    ``[author, stamp]``) is left out here. Hidden HTML comments are stripped, each
    comment is clipped to ``ISSUE_COMMENT_BUDGET`` and the newest are kept within
    ``ISSUE_COMMENTS_BUDGET``, with a marker for what was dropped. The result is
    defused; ``build_writer_prompt`` defuses it again inside the issue fence."""
    qa = clarification or ""
    kept: list[tuple[datetime, int, str]] = []
    for c in comments:
        author = c.author
        if author is None or _same_login(author, bot_login) or not is_allowed(author):
            continue
        raw = c.body or ""
        if _QA_MARKER in raw:
            continue
        header = f"[{author}, {_stamp(c.created_at)}]"
        if qa and header in qa:
            continue
        text = _HTML_COMMENT.sub("", raw).strip()
        if not text:
            continue
        kept.append((c.created_at, c.id, f"{header}\n{clip_head(text, ISSUE_COMMENT_BUDGET)}"))
    if not kept:
        return None
    kept.sort(key=lambda k: (k[0], k[1]))
    shown: list[str] = []
    used = 0
    for _, _, item in reversed(kept):
        if shown and used + len(item) > ISSUE_COMMENTS_BUDGET:
            break
        shown.append(item)
        used += len(item) + 2
    shown.reverse()
    head = "Comments on the issue from allowed users, oldest first:"
    if len(shown) < len(kept):
        head += f"\n[... {len(kept) - len(shown)} earlier comments not shown ...]"
    return defuse("\n\n".join([head, *shown]))


def fetch_issue_comments(
    api: Any,
    ticket: Ticket,
    *,
    is_allowed: Callable[[str], bool],
    clarification: str | None = None,
) -> str | None:
    """List the ticket's issue comments and render them (``issue_comments_text``). A
    GitHub failure is logged and the attempt goes on without comments."""
    try:
        comments = api.list_issue_comments(ticket.repo, ticket.issue_number)
        bot = api.bot_login()
    except Exception as exc:  # noqa: BLE001 - comments are optional context
        log.warning(
            "ticket %d: could not list issue comments (%s); the attempt goes on without them",
            ticket.id,
            exc,
        )
        return None
    return issue_comments_text(
        comments, is_allowed=is_allowed, bot_login=bot, clarification=clarification
    )


def _owned_files(paths: Sequence[str]) -> str:
    shown = [f"- {defuse(clip_head(p, OWNED_FILE_BUDGET))}" for p in paths[:OWNED_FILES_MAX]]
    if len(paths) > OWNED_FILES_MAX:
        shown.append(f"[... truncated: {len(paths) - OWNED_FILES_MAX} more files not shown ...]")
    return "\n".join(shown)


def _failure(prior: PriorAttempt | None) -> str:
    text = prior.failure_output if prior is not None else None
    if not text or not text.strip():
        return "(no check output was saved)"
    return defuse(clip_tail(text, FAILURE_OUTPUT_BUDGET))


def _note_text(s: dict[str, Template], note: Note) -> str:
    body, comments = split_kickback_note(note.text)
    text = defuse(clip_head(body, NOTE_BUDGET))
    if not text and comments:
        return s["note_only_line_comments"].substitute()
    return text or "(the note was empty)"


def _line_comments(s: dict[str, Template], note: Note, budget: int = LINE_COMMENTS_BUDGET) -> str:
    """The note's line comments block (#42), or "" when it has none. The note being
    answered gets ``LINE_COMMENTS_BUDGET``; older notes in the history get ``NOTE_BUDGET``."""
    _body, comments = split_kickback_note(note.text)
    if not comments:
        return ""
    return s["line_comments"].substitute(line_comments=defuse(clip_head(comments, budget)))


def _latest_note(history: Sequence[PriorAttempt]) -> tuple[tuple[int, int], Note] | None:
    for hi in range(len(history) - 1, -1, -1):
        notes = history[hi].notes
        if notes:
            return (hi, len(notes) - 1), notes[-1]
    return None


def _notes_block(
    s: dict[str, Template],
    history: Sequence[PriorAttempt],
    numbers: dict[int, int],
    *,
    skip: tuple[int, int] | None,
) -> str:
    items: list[str] = []
    for hi, prior in enumerate(history):
        for ni, note in enumerate(prior.notes):
            if (hi, ni) == skip:
                continue
            item = s["note_item"].substitute(
                number=numbers[prior.attempt.id],
                by=s[f"by_{note.by}"].substitute(),
                tag=note.tag or "none",
                note=_note_text(s, note),
            )
            comments = _line_comments(s, note, NOTE_BUDGET)
            items.append(f"{item}\n{comments}" if comments else item)
    kept: list[str] = []
    used = 0
    for item in reversed(items):  # newest first, until the budget is spent
        if used + len(item) > NOTES_TOTAL_BUDGET and kept:
            kept.append(f"[... truncated: {len(items) - len(kept)} older notes not shown ...]")
            break
        kept.append(item)
        used += len(item)
    return "\n".join(reversed(kept))


# --------------------------------------------------------------------------- caller side


def read_conventions(worktree: str | Path) -> tuple[str, str] | None:
    """``(file name, text)`` of the repo's AGENTS.md or CLAUDE.md, or None.

    Legacy, kept for callers that still pass one file's text (replay_runner); new code
    uses ``conventions.gather``, which reads both files, nested ones and the global
    rules. Reads at most a little over the budget, so a huge file costs nothing extra."""
    for name in CONVENTIONS_FILES:
        path = Path(worktree) / name
        if path.is_file():
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                return name, fh.read(CONVENTIONS_BUDGET + 1_000)
    return None


def read_failure_output(attempt: Attempt) -> str | None:
    """The tail of an attempt's saved verify output, if its checks failed."""
    if attempt.outcome != "failed_checks" or not attempt.verify_output_path:
        return None
    path = Path(attempt.verify_output_path)
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - FAILURE_OUTPUT_BUDGET * 4))
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    if size > FAILURE_OUTPUT_BUDGET * 4:
        data = "[... earlier output not read ...]\n" + data
    return data


def load_history(store: Store, ticket_id: int, *, before: int) -> tuple[PriorAttempt, ...]:
    """The ticket's attempts with id < ``before``, oldest first, with their notes.

    Notes come from kickback rows (user and reviewer); a non-shadow AI review that asked
    for changes is added only when that attempt has no reviewer kickback row. Reads the
    store and saved verify output only: never recordings, traces or transcripts."""
    out: list[PriorAttempt] = []
    for att in store.list_attempts(ticket_id):
        if att.id >= before:
            continue
        kicks = store.list_kickbacks(attempt_id=att.id)
        notes = [Note.from_kickback(k) for k in kicks]
        if not any(k.by == "reviewer" for k in kicks):
            notes += [
                Note.from_review(r)
                for r in store.list_reviews(attempt_id=att.id, shadow=False)
                if r.verdict == "request_changes"
            ]
        out.append(PriorAttempt(att, tuple(notes), read_failure_output(att)))
    return tuple(out)


def writer_prompt_for(
    store: Store,
    ticket: Ticket,
    attempt: Attempt,
    *,
    worktree: str | Path,
    verify_command: str | None,
    conflicted_files: Sequence[str] = (),
    rules_file: str | Path | None = None,
    global_rules: str | Path | None = None,
    issue_comments: str | None = None,
    images: str | None = None,
) -> WriterPrompt:
    """Caller-side convenience for the worker (#14): read everything, then build.
    ``conflicted_files``: ``Replay.conflicted`` for a ``rebase`` attempt (#43). The
    clarification Q&A (#44) is read from the store's ``clarify-qa:<ticket>`` cursor.
    ``rules_file`` is the repo's configured rules file (#38); ``global_rules`` overrides
    ``paths.rules_path()`` (tests). ``issue_comments``: ``fetch_issue_comments`` (#25).
    ``images``: ``images.images_text``, or for a replay ``images.stored_text`` (#78)."""
    from foreman_inator.tickets.conventions import gather

    conv = gather(worktree, ticket.owned_files, global_path=global_rules, rules_file=rules_file)
    return build_writer_prompt(
        ticket,
        attempt,
        load_history(store, ticket.id, before=attempt.id),
        verify_command=verify_command,
        conventions=conv,
        conflicted_files=conflicted_files,
        clarification=store.get_cursor(ticket.repo, clarify_qa_cursor(ticket.id)),
        issue_comments=issue_comments,
        images=images,
    )
