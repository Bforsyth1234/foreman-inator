# Chat sessions: design (todo #71 / GH #60, page todo #75 / GH #64)

Status: **foundations built** (wave 19: store, proxy, OpenCode chat server, spend, locks,
config keys; all inert until a chat session exists). No chat engine or front end yet. `src/foreman_inator/chat.py`
holds the types, the settled pure helpers and the `ChatManager` signatures (they raise
`NotImplementedError`). The open questions at the end need the user before wave C2 starts.

## Goal

Interactive coding work runs on the Claude Max plan today. The worker only takes labeled
issues, so canceling Max needs a second entry point: a chat that drives OpenCode in a
worktree through the **same** model ladder, proxy, spend caps and sandbox as a ticket. It
starts on a cheap tier, a command moves it up, and its spend shows next to ticket spend.

Two front ends, one engine:

* `fman chat <repo>`: a terminal REPL (stdin lines, streamed text on stdout).
* `/chat` on the local metrics page (`fman metrics`), the one page with a small inline
  script (SSE).

Both call one `chat.ChatManager`. Neither talks to OpenCode or the proxy directly.

## 1. Where it runs

A `ChatManager` lives in the process that shows the chat: `fman chat` for the terminal,
`fman metrics` for the page. It owns its own `ModelProxy` and `Spend`, the same way
`fman experiment run` and the page's experiment jobs (`metrics/experiments_ui.py`,
`worker_experiments.py` line ~503) already do. It does **not** run inside the worker:

* the worker is a long-running background service, the chat is a foreground thing the user
  opens and closes;
* a crash in the chat must not take ticket work down with it, and vice versa.

What this costs, and how it is covered:

| Shared resource | Today | For chat |
|---|---|---|
| Spend ledger | SQLite, shared by every process | shared; caps hold across processes |
| Spend reservations | in memory, per process (`spend.py` docstring, "Reservation") | the chat's in-flight calls are invisible to the worker's `Spend` until they settle. Worst case overshoot = one chat call's estimate; same class of gap as `fman experiment run` today |
| Local GPU | `shared_local_slots` lock files under `<home>/run` (#34) | chat's local calls queue on the same slots, so chat and tickets never load Ollama twice |
| Repo mirror | `RepoMirror` git calls, an in-process lock in the worker | a cross-process lock file per mirror (`<worktrees>/<owner>/<repo>/.mirror.lock`, `flock`) around fetch and `worktree add/remove`: wave C1 |
| Store | the metrics app opens it read-only | chat routes open their own writable `Store` |

## 2. Session lifecycle

States (`chat.ChatState`):

```
starting -> idle <-> busy -> closing -> opened_pr | discarded
     \                \-> (host stops / idle timeout) -> ended
      \-> failed (worktree, sandbox or OpenCode would not start)
```

**Start** (`ChatManager.start(repo, base_branch=None, tier=None)`):

1. `repo` must be in `config.repos` (watched repos only). Base = the repo's default branch
   unless given. Refuse when `chat_max_sessions` (default 1) are already open in this process.
2. Sandbox gate first, as `worker_sandbox.sandbox_ready` does: `sandbox = "on"` and no
   backend -> refuse to start (`failed`, nothing created). Fail closed (7g).
3. A `chat_sessions` row (`starting`), then under the mirror lock: fetch and
   `RepoMirror.create_chat(base_branch, session_id)`: worktree
   `<worktrees>/<owner>/<repo>/chat-<id>` on branch `fman/chat/<id>` (`chat.chat_branch`),
   from `origin/<base>`; the base sha goes in `fman-base` as for attempts, so diffs are
   measured against it. `worker_install.prepare_worktree`'s install runs here (sandboxed,
   offline from the repo's cache, #45) so tests work in the chat.
4. Pick the starting tier (section 4), register the proxy context, start one
   `opencode serve` under the writer's sandbox policy, create one OpenCode session. Row ->
   `idle`, a `chat` root span opens (its trace id stored on the row), the recorder opens.

**One server for the whole session.** An attempt's `OpenCodeDriver.run` starts a server,
sends one prompt and kills the process on return. A chat sends many prompts to one
OpenCode session, so it keeps the server alive between messages. The new class
`runner.opencode.ChatServer` (wave C1) reuses `OpenCodeServer`, `OpenCodeSession`,
`parse_event` and `server_env` unchanged and adds: `start()`, `send(text, model_id)`
-> iterator of `DriverEvent` until idle / error / turn timeout, `abort()`, `stop()`. The
event pump, permission/question rejection and tool recording are the driver's
(`_handle`, `record_tool`), factored out so both use them.

**Ends:**

* **Open as PR** (`open_pr(session_id, title=None)`): the manager (unsandboxed, the
  worker's own git identity, as the worker does after a writer run) runs
  `Worktree.commit_all`, then the repo's verify command under the verify policy (the
  repo's `sandbox_network`), then pushes `fman/chat/<id>` with the App token and opens a
  **draft** PR (`GitHubClient.create_draft_pr`). Title: the user's, else the first line of
  the first message (defused, 72 chars). Body: "Opened from chat session N", tiers and models
  used, cost, the verify result, `sandbox.describe(...)`, and "Not reviewed by the AI
  reviewer". A failing verify does not block (the user decided) but the body says so in its
  first line. The body carries no ticket marker (`publisher.body_ticket_id` must return
  None), so the worker never adopts the PR. Then server stopped, worktree removed (the
  remote branch stays), row -> `opened_pr` with `pr_number`.
* **Discard** (`discard`): server stopped, `Worktree.remove(delete_branch=True)`, nothing
  pushed, row -> `discarded`. Asked to confirm on the page; `/discard` in the terminal
  asks "y/N".
* **Idle timeout** (`chat_idle_minutes`, default 30) or the host process exiting: server
  stopped, worktree **kept**, row -> `ended`. An `ended` session can still be opened as a PR
  or discarded from the page or `fman chat list / pr <id> / discard <id>`. On startup the
  manager marks any `idle`/`busy` row with no live server as `ended` (`end_reason =
  'host_restart'`).
* **Resume** of an `ended` session: start a new server in the kept worktree, on the kept
  state dir (`<home>/opencode/chats/<id>/`). Checked against OpenCode 1.18.7 (wave C1c,
  `ChatServer` docstring): sessions persist in `XDG_DATA_HOME/opencode/opencode.db`, and a
  server reopened on the same data dir resumes the old session id with its full message
  list (`ChatServer.start(resume=<id>)`); no summary prompt is needed. The chat config
  must always set `enabled_providers`, or OpenCode falls back to its own hosted model.

**File locks (#19):** decision 6 below: a chat locks the files it edits (holder kind
"chat", `Locks.chat_touch`), never waits on a ticket's locks, and releases them on `/pr`,
`/discard` or the session's end.

## 3. Messages in, events out

```
page (fetch POST /chat/<id>/send)      terminal (stdin line)
            \                           /
             ChatManager.send(id, text)            one turn at a time per session
                  |  parse_command(text): "/tier up" etc. handled here, never sent
                  |  recorder.user(text, by="user"); chat_turns row (tier, model)
                  v
             ChatServer.send(text, model_id=<current tier's model>)
                  |  POST /session/{id}/prompt_async  {parts, model:{providerID:"fman", modelID}}
                  v
             opencode serve (sandboxed) --LLM--> proxy /c/<token>/v1 --> Ollama / cloud
                  |  GET /event (SSE): message.part.updated, message.updated, session.idle ...
                  v
             parse_event -> DriverEvent -> ChatManager -> ChatEvent(seq, type, data)
                  |  ring buffer per session (last 2,000 events) + recording
                  v
     GET /chat/<id>/events (text/event-stream)      terminal printer
```

* A message sent while the session is `busy` is refused (409 on the page, "still working:
  /abort or wait" in the terminal). No queueing: one turn, one model, one budget.
* OpenCode's text parts arrive as the **whole part text so far** (`TextEvent.text`), not
  as deltas. `chat.text_delta(prev, new)` turns that into an append (the usual case) or a
  replace (OpenCode rewrote the part), so the page appends instead of re-rendering.
* Event types (`chat.EVENT_TYPES`): `text` (part id, mode append|replace, text), `tool`
  (name, kind, one-line input summary, is_error; output not streamed, it is in the
  recording), `status` (busy|idle), `tier` (tier, model), `cost` (session cost, cap,
  left), `refused` (spend / budget refusal with what the user can do), `error`, `end`.
* SSE framing (`chat.ChatEvent.to_sse`): `id: <seq>`, `event: <type>`, one `data:` line of
  JSON (JSON escapes newlines, so a model's text can never inject a frame). The browser's
  `EventSource` sends `Last-Event-ID` on reconnect; the route replays from the buffer after
  that seq, so a dropped connection loses nothing.
* `/abort` (and the Abort button): `ChatServer.abort()`; the turn ends `aborted`.
* A turn ends at `writer_timeout_seconds` (+ local queue wait, as `OpenCodeDriver` does)
  with an abort. OpenCode's own provider retries (`session.status` = retry) are inside that
  limit.
* Permission and question asks are rejected exactly as in attempts (`PERMISSIONS`
  unchanged). They are **not** turned into a "do you allow?" prompt for the user: an allow
  button for `external_directory` or `webfetch` would be a way around 7g. The model asks in
  plain text instead, and the user answers in the next message.

## 4. Tiers and "move up a tier"

* The ladder is `chat.tier_ladder(config.models, local_only=...)`: the **first** model of
  each tier in try order (`Config.models_for_tier`), tiers without a model skipped. A
  local-only repo (#49) keeps only `kind == "local"` models.
* Start tier: `chat_start_tier` (default 1, the cheap local tier), or `--tier N` / the
  page's picker. The router is not used: there is no issue to classify and the stated idea
  is "start cheap".
* Commands (`chat.parse_command`; a line starting `//` is sent as text with one `/`
  removed):

  | Command | Effect |
  |---|---|
  | `/tier up`, `/up` | next tier with a model; at the top (or the last local tier) an error says so |
  | `/tier down` | previous tier |
  | `/tier N` | that tier, if it has a model in this session's ladder |
  | `/abort` | stop the running turn |
  | `/cost` | session cost, the chat cap and what is left |
  | `/verify` | run the repo's verify command (verify policy) and show the tail |
  | `/pr [title]` | open as PR (section 2) |
  | `/discard` | discard (asks to confirm) |
  | `/approve` | allow the turn that was refused at a cap (section 5) |
  | `/help` | the list |

* A tier change applies to the **next message only**: the running turn keeps its model.
  Mechanism: the per-session `opencode.json` (`runner.opencode.build_chat_config`, wave
  C1) lists every model of the session's ladder under the `fman` provider, and each
  `prompt_async` names the current tier's `modelID`. No server restart, the OpenCode
  session keeps its history. `small_model` is the session's lowest-tier model, so
  OpenCode's own title/summary calls stay cheap.
* Defence in depth: the chat's spend gate refuses any call whose endpoint is not the
  session's current model (so a subagent or a stale request cannot pick a pricier model),
  except `small_model`.

## 5. Spend

Every chat call is a cloud or local call through the proxy with a registered context:
`proxy.context(chat_session_id=<id>, label="chat")` (a new `CallContext` field, wave C1).
`chat_session_id` means: no `add_attempt_usage` (there is no attempt row), recorder
`get_recorder(id, "chat")`, local-slot key `("chat", id)` so it never collides with an
attempt id in `queue_wait`, and the ledger row carries `chat_session_id`.

### Options for the cap

| | A. Share of the live caps | B. Own bucket | C. Live caps plus a chat sub-cap (**recommended**) |
|---|---|---|---|
| Rule | chat rows are bucket `live`; `daily_usd` / `monthly_usd` cover tickets and chat together | new bucket `chat` with `chat_daily_usd` / `chat_monthly_usd`, independent of live like `experiments` | bucket `live` (counts toward `daily_usd` and `monthly_usd`) **and** at most `chat_monthly_usd` of it may be chat |
| Total ceiling | `monthly_usd` | `monthly_usd + experiments + chat` | `monthly_usd` (+ experiments, as today) |
| Chat can starve tickets | yes, all of it | no | only up to `chat_monthly_usd` |
| Schema | `ALTER TABLE spend_ledger ADD COLUMN chat_session_id` | `spend_ledger.bucket` CHECK lists only live/experiments: a table rebuild (like 003_rebase), and `Bucket` changes in proxy and spend | the ADD COLUMN only |
| Code | tag rows | new bucket through proxy, spend, metrics | one extra check in `Spend._estimate` for chat contexts |

**Recommendation: C.** The user's goal is one monthly number that replaces $100 of Max;
B breaks that (two ceilings to add up), A lets a long chat evening pause every ticket. C
keeps one ceiling and bounds how much of it chat can take. Proposed default
`chat_monthly_usd = 20.0` against the default `monthly_usd = 50.0` (open question 1).

Also, per turn (7e, no runaway agent loop inside one message): `chat_turn_budget_usd`
(default = `attempt_budget_usd`, $1.50) and the existing `attempt_max_input_tokens_*`
limits, checked like #58's per-attempt budget against the turn's running totals. Over it,
the call is refused, the turn is aborted and the page shows `refused` with the numbers. The
next message starts a new turn budget: the user acted.

**At a cap.** The user is present, so there is no issue comment: the page shows the
refusal with an "Allow this message (up to $X)" button and the terminal says "type
/approve". One approval covers one turn up to `approval_usd`, the same grant as
`fman approve-spend` gives one attempt, held in memory for that session (the
`spend_approvals` table needs a ticket id). Nothing past a cap happens without a user
action, and the refused message is re-sent only when the user approves.

**Shown:** the page header and `/cost` show `session $x · chat this month $y of $z ·
month $m of $M`, all read from the ledger (`SUM(cost_usd) WHERE chat_session_id = ?`), so
"cost shown matches the spend table" (#75 Verify) holds by construction. `fman status`'s
spend line and the metrics page gain a "chat" figure next to tickets (`queries`, wave C3).

## 6. Router and kNN data

**Recommendation: chat sessions do not feed the router.** kNN and the classifier learn
"the cheapest tier at which an issue was accepted", from `tickets.accepted_attempt_id`,
kickbacks and `post_merge_signals` (`routing/knn.py` docstring, step 1). A chat has none
of that: its first message is not an issue, tier moves are the user steering mid-task, and
a session that ends as a PR has no reviewer verdict. Mixing it in would teach the router
from a different distribution. So: no `tickets` row, no `ticket_embeddings` row, no
attempt rows; replays and shadow reviews never see chat.

Kept for later, at no cost: `chat_turns` records tier and model per turn, so a future
"where do chats end up" report (for example, "most chats move to tier 2 within 3 messages;
start there?") can be built from it without touching the router.

## 7. Sandbox and network

* `opencode serve` runs under the **writer's** policy (`worker_sandbox._sandbox_policy(...,
  writer=True)`): writes only to the chat worktree, its scratch dir
  (`<home>/sandbox/chat-<id>-writer`) and its OpenCode state dir; outbound network only to
  the chat proxy's port; `listen_loopback` for its own server. Its bash tool inherits it.
* `/verify` and the verify before a PR run under the **verify** policy with the repo's
  `sandbox_network`, like an attempt.
* `server_env` strips keys, tokens, `SSH_AUTH_SOCK` and git askpass, as for attempts: the
  model cannot push or reach GitHub. Only the manager pushes, on `/pr`.
* `sandbox = "off"` runs everything unsandboxed, as for tickets, and the page shows a
  red banner with `sandbox.describe(...)`; the PR body says so.
* No chat-only network allowance in v1 (no "let the model fetch docs"). Open question 4.

## 8. Local-only repos (#49)

For a repo set `cloud = false`:

* the ladder holds local models only; `/tier up` stops at the last local tier and says
  why; a repo with no local model at all cannot start a chat;
* the chat gate refuses every cloud call for the session (cap `local_only`, no approval
  can lift it), read from `store.repo_allows_cloud` on every call, so a change made while
  a chat runs takes effect at once;
* recordings, transcripts and spans stay on the Mac, as for every repo; the page shows
  "local only".

## 9. Recordings and replay

* One recording per session: `<recordings>/chat/<session_id>.jsonl.gz`, role `chat`, the
  fde-kit row format the recorder already writes. `tracing.recorder` gains
  `chat_recording_path` and a `Recorder(..., role="chat")` keyed `(session_id, "chat")`;
  the registry key includes the role, so it never meets an attempt's `(id, "attempt")`.
* Rows: header (repo, base commit, ladder, sandbox line); `user` per message (`by="user"`);
  `llm` rows written by the proxy (it finds the recorder through `chat_session_id`); `tool`
  rows from driver events (`record_tool`); a `result` row at the end with the outcome
  (`opened_pr` / `discarded` / `ended`), `pr_number` and the head commit.
* Spans: a `chat` root span per session (`chat.id`, `chat.repo`); each turn a `chat.turn`
  span (`chat.turn`, `chat.tier`, `chat.model`). The proxy context is registered once per
  session, so `llm.call` spans parent to the session span (a context captures the span
  current at registration), carrying `chat.turn` as an attribute. Readable in `/traces`.
* **Replay: none.** Replay sets are built from merged tickets with a known outcome; a chat
  is multi-turn, user-steered and has no acceptance label, so it is not a replay case and
  `experiments/replay_sets.py` never reads `chat/`. An ended session's page shows its
  transcript read back from the recording (`load_recording`).
* Retention: `recorder.prune` skips directories not named by an integer, so `chat/` is
  untouched today. Wave C1 adds `prune_chats`: delete chat recordings older than 90 days
  unless the session's PR was merged (the same rule as tickets).
* Secrets: whatever the user pastes into the chat goes to the model and the recording.
  The page says so under the input box; recordings pass `proxy.scrub_secrets` on user rows
  (catches key-shaped strings, not everything).

## 10. Invariants 7a-7g

* **7a Nothing merges without the user.** Chat branches are `fman/chat/<id>`
  (`check_branch` passes, the worktree manager refuses anything else). `/pr` opens a
  **draft** and the bot never marks it ready (no reviewer approval exists for it in v1).
  Nothing merges or approves. The model has no token, so only the manager pushes, and only
  that branch.
* **7b Only allowed users start work.** A chat starts only from loopback: the terminal
  (the user) or the metrics page (loopback bind, Host check, and every POST through
  `FormGuard`: CSRF token, own Origin). It starts only on a watched repo's
  `origin/<base>`, never on a fork or a PR it did not open. No GitHub text enters a chat
  unless the user pastes it.
* **7c Local by default.** The default start tier is tier 1 (local). Cloud only for a
  tier configured cloud, inside the live caps and the chat sub-cap, and every call goes
  through the proxy as a traced `llm.call`. Recordings and traces stay on the Mac;
  local-only repos never reach a cloud model (section 8).
* **7d Secrets never in the repo.** Keys stay in the Keychain behind the proxy; OpenCode
  gets a dummy key and a scrubbed env. Chat state lives in `~/.foreman-inator/` (mode 700:
  `chat/` recordings, `opencode/chats/<id>`, the sandbox scratch). Tests use fakes.
* **7e No silent loops.** One user message = one turn; a turn is capped by
  `writer_timeout_seconds`, `chat_turn_budget_usd` and the input-token limits; a refused
  or failed turn is never re-sent by the manager (only `/approve` or the user's next
  message continues); the idle timeout ends a forgotten session.
* **7f The reviewer is independent.** v1 runs no AI review on chat PRs (open question 3).
  If it is added, the reviewer sees the PR title plus the user's first message as "the
  issue" and the diff, never the transcript (that is the writer's reasoning), and comes
  from a family different from **every** model the session used.
* **7g Model-written code runs sandboxed.** OpenCode and its bash tool run under the
  writer policy, `/verify` under the verify policy; no sandbox backend means no chat
  starts; `sandbox = "off"` is the only way out and is shown on the page and in the PR.

## 11. The `/chat` page

Routes (new `metrics/chat_page.py`, mounted by `web.full_app`, nav entry "Chat"):

| Route | What |
|---|---|
| `GET /chat` | repo picker (watched repos; "local only" marked), base branch, start tier; the list of open and ended sessions with links |
| `POST /chat/start` | `ChatManager.start`; redirects (303) to `/chat/<id>` |
| `GET /chat/<id>` | the session: tier and model, cost line, transcript so far (server-rendered, escaped), input box, buttons. An ended session is read-only plus "Open as PR" / "Discard" |
| `GET /chat/<id>/events` | `text/event-stream` (Starlette `StreamingResponse`); replays after `Last-Event-ID` |
| `POST /chat/<id>/send` | form field `text` (`MAX_FORM_BYTES`, 64 KB); 409 while busy |
| `POST /chat/<id>/tier` | `direction=up|down` or `tier=N` |
| `POST /chat/<id>/abort`, `/approve`, `/pr`, `/discard` | as the commands; `/discard` needs `confirm=yes` |

* Every POST is a `FormGuard.post_route` (token, own Origin/Host, form-urlencoded) and
  works without JavaScript (a plain form, 303 back to the page), so the protection is the
  existing one. The script only upgrades the forms: `fetch(form.action, {method: "POST",
  body: new URLSearchParams(new FormData(form))})`, which sends the same
  form-urlencoded body with the token and the browser's own `Origin`.
* **The one inline script** (about 60 lines, no external scripts, no framework): opens
  `new EventSource("/chat/<id>/events")`, appends `text` events to the current reply with
  `textContent` (never `innerHTML`: model output is untrusted), adds one line per `tool`
  event, updates the tier and cost lines, disables Send while busy, re-enables on `status
  idle`. Rendered with a per-response nonce and the route sets `Content-Security-Policy:
  default-src 'none'; script-src 'nonce-<n>'; style-src 'unsafe-inline'; connect-src
  'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'` on `/chat` pages
  only. Every other page keeps no JavaScript at all.
* Server-rendered text (the transcript on reload, repo names, errors) goes through
  `html.escape` like every other page.
* The SSE route is a GET with no side effects and sits behind the loopback Host guard.
* Tests (wave C3): messages stream in order (fake `ChatServer` events -> SSE frames);
  `POST /chat/<id>/tier` switches the model for the next message only (the running turn
  keeps its model); the cost line equals `SUM(spend_ledger.cost_usd)` for the session; a
  POST without the token or with a foreign Origin is 403 and changes nothing; a model text
  containing `</script><script>` and `\n\ndata:` arrives as one inert `text` event.

## 12. Data model (migration `012_chat.sql`, next free number when C1 starts)

```sql
CREATE TABLE chat_sessions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    repo                TEXT NOT NULL,
    base_branch         TEXT NOT NULL,
    base_commit         TEXT,
    branch              TEXT NOT NULL CHECK (branch LIKE 'fman/chat/%'),
    worktree            TEXT,
    state               TEXT NOT NULL DEFAULT 'starting'
                        CHECK (state IN ('starting','idle','busy','closing','opened_pr',
                                         'discarded','ended','failed')),
    tier                INTEGER NOT NULL CHECK (tier BETWEEN 1 AND 3),
    model               TEXT NOT NULL,
    opencode_session_id TEXT,
    trace_id            TEXT,
    pr_number           INTEGER,
    end_reason          TEXT,
    started_at          TEXT NOT NULL,
    ended_at            TEXT
) STRICT;

CREATE TABLE chat_turns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES chat_sessions (id),
    seq         INTEGER NOT NULL CHECK (seq >= 1),
    tier        INTEGER NOT NULL CHECK (tier BETWEEN 1 AND 3),
    model       TEXT NOT NULL,
    status      TEXT CHECK (status IN ('finished','aborted','timed_out','errored','refused')),
    tokens_in   INTEGER NOT NULL DEFAULT 0,
    tokens_out  INTEGER NOT NULL DEFAULT 0,
    cost_usd    REAL NOT NULL DEFAULT 0,
    tool_calls  INTEGER NOT NULL DEFAULT 0,
    edits       INTEGER NOT NULL DEFAULT 0,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    UNIQUE (session_id, seq)
) STRICT;

ALTER TABLE spend_ledger ADD COLUMN chat_session_id INTEGER REFERENCES chat_sessions (id);
```

Message text is not stored in SQLite (it is in the recording, which has a retention rule).

## 13. Config keys (wiring adds them; module constants in `chat.py` are the defaults)

| Key | Default | Meaning |
|---|---|---|
| `chat_start_tier` | 1 | tier a new session starts on |
| `chat_monthly_usd` (in `[spend_caps]`) | 20.0 | most of the month's live spend chat may take (option C) |
| `chat_turn_budget_usd` | = `attempt_budget_usd` (1.50) | cloud spend one message may use |
| `chat_idle_minutes` | 30 | idle session -> `ended` (worktree kept) |
| `chat_max_sessions` | 1 | open sessions per host process |
| `chat_local_wait_seconds` | 5.0 | wait for the local slot before a turn falls back to cloud (decision 2) |

Per repo (database, not config.toml): `chat_network`, set with `fman repo set-chat-network
<repo> on|off` or on `/settings` (decision 4).

## 14. Implementation plan (waves, disjoint files)

**Wave C1: foundations (4 parallel slots)**

| Slot | Owns | Delivers |
|---|---|---|
| C1a | `db/migrations/012_chat.sql`, `db/store.py` | the tables above; `create_chat_session`, `set_chat_state`, `add_chat_turn`, `finish_chat_turn`, `chat_spend(session_id)`, `chat_spend_total(since, until)`; `record_spend(..., chat_session_id=)` |
| C1b | `proxy/server.py`, `tracing/recorder.py`, `tracing/otel.py` | `CallContext.chat_session_id` + `context(chat_session_id=)`: no attempt usage, recorder `(id, "chat")`, slot key `("chat", id)`; `chat_recording_path`, `prune_chats`; `chat_span`, `chat_turn_span` |
| C1c | `runner/opencode.py`, `runner/worktree.py` | `build_chat_config(ladder, small_model, proxy_url)`; `ChatServer` (factor `_handle`/pump out of `_drive`, attempts unchanged); `RepoMirror.create_chat/open_chat/chat_path`; cross-process mirror lock |
| C1d | `spend.py` | chat contexts: option C sub-cap check, per-turn budget, current-model check, local-only check by repo, in-memory one-turn approvals, ledger `chat_session_id` |

**Wave C2: the engine (1 slot)**: owns `chat.py` (+ tests): `ChatManager` on top of C1,
with a fake `ChatServer` and the fake model transport for tests. Rule-12 check of OpenCode
session reload (section 2, Resume) recorded in its docstring.

**Wave C3: front ends (3 parallel slots)**

| Slot | Owns | Delivers |
|---|---|---|
| C3a | `metrics/chat_page.py` | the routes, the inline script, CSP, the page tests in section 11 |
| C3b | `chat_cli.py` | `fman chat <repo> [--tier N] [--base B]` REPL, `fman chat list / pr <id> / discard <id>` |
| C3c | `metrics/queries.py` | chat spend next to ticket spend (page and `/api/metrics`), `SpendSummary` chat line (`fman status`) |

**Wave C4: wiring (the runner)**: `config.py` (section 13 keys, validation: start tier
has a model, `chat_monthly_usd <= monthly_usd`), `cli.py` (register `chat`),
`metrics/web.py` (`full_app` mounts `chat_page.routes`, nav "Chat"), `docs/commands.md`,
`docs/configuration.md`, `docs/how-it-works.md`, SPEC.md (chat under 7a-7g), todo.md.

Live checks (user): a chat on a real repo at tier 1, `/tier up` to a cloud tier, cost
matches `fman status`, `/pr` opens a draft, `/discard` leaves no branch.

## 15. Open questions for the user

1. Spend: option C (chat counts toward `monthly_usd`, capped at `chat_monthly_usd`)? What
   default for `chat_monthly_usd`: $20 of the $50 month?
2. Local GPU: tickets and chat share one Ollama slot. Should a waiting chat message jump
   ahead of a ticket's queued local call (you are watching), or wait its turn?
3. Opening as PR: draft with no AI review (proposed), or run the cross-family reviewer
   first like a ticket?
4. Network: chat's model gets no network except the proxy (same as tickets). Do you want a
   per-repo opt-in for chat to fetch docs, or keep it closed?
5. Should a chat be able to continue an existing `fman/` PR branch (fix a ticket's PR by
   hand), or only start from the base branch?
6. Should a chat take file locks on the files it edits, so tickets wait instead of
   producing conflicting PRs?
7. Is one open session at a time enough (`chat_max_sessions = 1`)?

### Decisions (user, 2026-11-25)

These answer the questions above and override any earlier recommendation in this doc.

1. **Spend:** option C. Chat counts toward `monthly_usd`, capped by `chat_monthly_usd`, default **$20**.
2. **Local GPU: chat jumps the queue, on a timer.** A chat turn that needs the local slot goes ahead of queued ticket calls (priority, not preemption of a call already running). If the turn would wait more than **5 seconds** (`chat_local_wait_seconds = 5`) for the slot, that turn goes to a **cloud model** instead: the first cloud rung at or above the session's tier. The fallback never applies to a local-only repo (#49, invariant 7c): there the turn keeps waiting. It is also refused, and the turn waits, when the chat sub-cap, the monthly cap or the per-turn budget would be exceeded. The turn's trace and the page say "local slot busy, used <cloud model>". The session's own tier does not change; the next turn tries local again.
3. **Opening as PR:** a draft PR with **no AI review**: the user was in the loop for every change.
4. **Network:** closed by default, with a **per-repo opt-in** for chat only (`chat_network`, stored in `repo_settings`, set with `fman repo set-chat-network <repo> on|off` and on `/settings`). When on, the chat sandbox gets the same outbound policy as a first-attempt dependency install (ports 443/80), never the user's credentials or Keychain. Tickets are unaffected.
5. **Base branch only** at first: a chat never continues an existing `fman/` ticket branch (it could collide with the worker's next attempt).
6. **File locks: yes.** A chat session takes locks on files it edits (lock holder kind "chat"), so tickets wait instead of producing conflicting PRs; locks are released on `/pr`, `/discard`, or when the session ends. A chat never waits on a ticket's locks to edit (the user decides), but the page shows a warning when a file it edits is held by a ticket.
7. **One open session** at a time (`chat_max_sessions = 1`).
