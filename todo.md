# foreman-inator — Build Plan

<!-- ============================================================
HOW THIS FILE WORKS (rules agents must follow)
	1.	THIS FILE HOLDS OPEN + IN-PROGRESS WORK ONLY.
When an item is verified done, move its full write-up to DONE.md and
leave a one-line stub here (ID, title, date, link). Agents load this
file every session; done-item detail is context they pay for and
don't need.
	2.	IDs ARE UNIQUE AND NEVER REUSED, across todo.md and DONE.md. The
next ID lives in the counter below. Bump it when you add an item.
	3.	WAVES = DISJOINT FILE SETS. When items run in parallel, every item
in a wave lists the files it OWNS, and no file appears in two items of
one wave. Cross-cutting work goes in the LAST wave.
	4.	AN AGENT MAY DEVIATE from its instructions only if it verifies the
original instruction would cause a regression, and it documents the
deviation and the evidence in the item.
	5.	EVERY FIX GETS A VERIFY BLOCK, and every new test must be proven
NON-VACUOUS (revert the fix, confirm the test fails, restore).
	6.	RECURRING FEEDBACK IS A SPEC GAP. Add it to SPEC.md.
	7.	THE INVARIANTS (SPEC.md). Not up for per-item deviation:
a. NOTHING MERGES WITHOUT THE USER. The worker never merges, never
   approves its own PR, and never pushes to a branch it did not create
   (all its branches start with `fman/`). Only a reviewer-approved PR, or
   one handed over after the loop cap, is marked ready for review.
b. ONLY ALLOWED USERS START WORK. A label counts only if a user on the
   `allowed_users` list applied it. The worker never checks out or runs
   code from forks or from PRs it did not open.
c. LOCAL BY DEFAULT. Code, traces and recordings stay on the Mac. Model
   calls leave the machine only for a tier configured as cloud, only
   inside the spend caps, and every cloud call is a traced `llm.call`.
d. SECRETS NEVER IN THE REPO. The App key, config and data live in
   `~/.foreman-inator/` (mode 700); API keys are Keychain references.
   Tests use fake keys generated at test time.
e. NO SILENT LOOPS. Every retry and escalation chain is capped and ends
   with the user: flagged on the issue, never retried forever.
f. THE REVIEWER IS INDEPENDENT. A different model family from the
   writer; it sees the issue and the diff, never the writer's reasoning.
g. MODEL-WRITTEN CODE RUNS SANDBOXED. OpenCode, the verify command and the
   reviewer's checks run only under the attempt's sandbox (writes confined
   to the attempt's worktree and scratch dir, no reads of the home or
   `~/.foreman-inator`, network only to the proxy unless the repo allows
   it). The sandbox fails closed; `sandbox = "off"` is the only way out
   and is always shown.
	8.	TESTS: `uv run pytest -q` and `uv run ruff check .`. Anything red
blocks the item. Tests never touch the real GitHub API, a real model or
`~/.foreman-inator`: use `tests/fakes/` and `tmp_path`.
	9.	SHARED REGISTRATION FILES ARE THE WAVE RUNNER'S. Items create their
modules; at wave close the runner registers CLI commands in
`src/foreman_inator/cli.py`, wires the main loop in
`src/foreman_inator/worker.py`, and adds dependencies to
`pyproject.toml`. Items say exactly what to register and which
dependencies they need.
	10.	AGENTS DON'T COMMIT, don't run the worker against a real repo, and
don't edit todo.md: they report Results and Deviations, and the runner
writes them into the item.
	11.	LIVE CHECKS ARE BATCHED. Anything needing the user (registering the
App, a real issue on a real repo, a real model run, a real review) goes
to the Live checks list. An item whose code and tests are done but whose
live check hasn't run is `[~] live`, not `[x]`.
	12.	CHECK EXTERNAL APIS BEFORE CODING AGAINST THEM. OpenCode's server
API, GitHub's app-manifest flow, Ollama's OpenAI-compatible endpoint and
the GLM API change. Read the current docs first, record the URL and date
in the item, and put the shape you rely on behind one small adapter so a
change touches one file.
============================================================ -->

Next ID: #86
Legend: `[ ]` todo · `[~]` in progress · `[~] live` built, waiting on its live check · `[x]` done (move to DONE.md)

## Wave plan

**Goal:** milestones 1–2 of SPEC.md (a working GitHub add-on with tiers,
escalation and AI review), then milestone 4 (learning router,
experiments, tracing analysis), then release. Milestone 3 (bot-inator)
lives in bot-inator's own todo.md. One branch + PR per wave (`wave-N`);
one subagent per slot, all slots of a wave in parallel.

The dependency arrow runs: scaffold → foundations (config, store, GitHub
client, tracing, verify, worktrees) → the pieces of one attempt (setup,
proxy, OpenCode driver, poller, PR publisher, writer prompt) → worker
loop v1 (milestone 1) → tiers, escalation, review, caps, locks, metrics
→ worker loop v2 (milestone 2) → learning router and experiments →
wiring → release.

Package root below is `src/foreman_inator/` (written `pkg/`).

| Wave | Slot: items | Owned files (disjoint within the wave) | Status |
| :--- | :--- | :--- | :--- |
| 1 | A: #1 (alone) | `pyproject.toml`, `uv.lock`, `.gitignore`, `LICENSE`, `README.md`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml`, `pkg/__init__.py`, `pkg/cli.py` (skeleton), `pkg/worker.py` (skeleton), `tests/conftest.py`, `tests/fakes/__init__.py` | [x] |
| 2 | A: #2 · B: #3 · C: #4 · D: #5 · E: #6 · F: #7 | A `pkg/paths.py`, `pkg/config.py` · B `pkg/db/schema.sql`, `pkg/db/store.py`, `pkg/db/__init__.py` · C `pkg/github/app_auth.py`, `pkg/github/client.py`, `pkg/github/__init__.py`, `tests/fakes/github.py` · D `pkg/tracing/otel.py`, `pkg/tracing/recorder.py`, `pkg/tracing/__init__.py` · E `pkg/runner/verify.py`, `tests/fixtures/repos/` · F `pkg/runner/worktree.py`, `pkg/runner/__init__.py` (+ each slot's own `tests/test_<module>.py`). Runner at close: `pyproject.toml` deps | [x] |
| 3 | A: #8 · B: #9 · C: #10 · D: #11 · E: #12 · F: #13 | A `pkg/setup/manifest.py`, `pkg/setup/deps.py`, `pkg/setup/launchd.py`, `pkg/setup/__init__.py` · B `pkg/proxy/server.py`, `pkg/proxy/pricing.py`, `pkg/proxy/__init__.py`, `tests/fakes/models.py` · C `pkg/runner/opencode.py`, `tests/fakes/opencode.py` · D `pkg/github/poller.py`, `pkg/tickets/lifecycle.py`, `pkg/tickets/__init__.py` · E `pkg/pr/publisher.py`, `pkg/pr/__init__.py` · F `pkg/prompts/writer-v1.md`, `pkg/tickets/prompt.py` (+ own tests). Runner at close: `cli.py` (`setup`), `pyproject.toml` deps | [x] built; live checks open for #8, #10 |
| 4 | A: #14 (alone: the main loop) | `pkg/worker.py`, `pkg/cli.py` (`run`, `status`), `tests/test_worker_v1.py`, `tests/fakes/` (additive only) | [x] built; live check open for #14 |
| 5 | A: #15 · B: #16 · C: #17 · D: #18 · E: #19 · F: #20 | A `pkg/routing/rules.py`, `pkg/routing/__init__.py`, `pkg/prompts/router-v1.md` · B `pkg/routing/escalation.py` · C `pkg/review/reviewer.py`, `pkg/review/checks.py`, `pkg/review/__init__.py`, `pkg/prompts/reviewer-v1.md` · D `pkg/spend.py` · E `pkg/locks.py` · F `pkg/metrics/queries.py`, `pkg/metrics/web.py`, `pkg/metrics/__init__.py`, `pkg/pr/summary.py` (+ own tests). Runner at close: `pyproject.toml` deps | [x] built; live check open for #17 |
| 6 | A: #21 (alone: the main loop) | `pkg/worker.py`, `pkg/cli.py` (`metrics`), `pkg/pr/publisher.py` (ready-for-review + bot review posting), `tests/test_worker_v2.py` | [x] built; live check open for #21 |
| 6b | A: #40 · B: #41, #42, #43 (one agent, in order: all three edit the GitHub client and poller) · C: #44 | A `pkg/runner/sandbox.py`; edits `pkg/runner/opencode.py`, `pkg/runner/verify.py`, `pkg/review/checks.py`, `pkg/config.py` · B `pkg/github/client.py`, `tests/fakes/github.py`, `pkg/github/poller.py`, `pkg/tickets/lifecycle.py`, `pkg/tickets/prompt.py`, `pkg/setup/manifest.py`, `pkg/runner/worktree.py`, `pkg/db/store.py`, `pkg/db/migrations/002_*.sql` · C `pkg/tickets/clarify.py`, `pkg/prompts/clarify-v1.md` (+ own tests). Runner at close: `worker.py` wiring for all five; for #44 the `NeedsInfoAnswered` event in `poller.py`/`lifecycle.py` and the Q&A block in `tickets/prompt.py` (after slot B is merged into the wave branch); `pyproject.toml` deps | [x] built and wired; live checks open for #40, #41, #44 |
| 7 | A: #22 (cross-repo, optional) | bot-inator repo only; nothing in this repo | [ ] |
| 8 | A: #23 · B: #24 · C: #25 · D: #26 · E: #27 · F: #28 | A `pkg/routing/knn.py`, `pkg/routing/explore.py` · B `pkg/experiments/variants.py`, `pkg/experiments/__init__.py`, `pkg/prompts/__init__.py` · C `pkg/experiments/replay_sets.py`, `pkg/experiments/replay_runner.py` · D `pkg/experiments/shadow.py` · E `pkg/tracing/failure_label.py`, `pkg/prompts/failure-label-v1.md` · F `pkg/tracing/replay.py`, `pkg/metrics/trace_view.py`, `tests/recordings/` (+ own tests). Runner at close: `pyproject.toml` deps | [x] |
| 9 | A: #29 (alone: wiring) | `pkg/worker.py`, `pkg/cli.py` (`experiment`, `replay`), `pkg/metrics/web.py` (new routes and failure-step columns), `tests/test_worker_v3.py` | [x] built; live check open for #29 |
| 10 | A: #30 · B: #31 · C: #32 | A `pkg/export.py` · B `pyproject.toml` (metadata, entry points), `.github/workflows/release.yml`, `docs/releasing.md` · C `README.md`, `docs/setup.md`, `docs/how-it-works.md`, `docs/demo.gif` (+ own tests). Runner at close: `cli.py` (`export`) | [x] built; live checks open for #31, #32 |
| 13 | A1: GH #50, #55 · A2: GH #51 · A3: GH #57 · A4: GH #58 · A5: GH #56 · A6: todo #58 (GH #35) | A1 `pkg/worker_status.py`, `pkg/locks.py` · A2 `pkg/setup/deps.py`, `pkg/routing/knn.py`, `pkg/worker_background.py` · A3 `pkg/runner/install.py`, `pkg/worker_install.py` · A4 `pkg/spend.py`, `pkg/worker_attempt.py`, `pkg/routing/escalation.py`, `pkg/pr/summary.py`, `pkg/db/store.py`, `007_attempt_budget.sql` · A5 `pkg/prune.py`, `pkg/worker_prune.py`, `pkg/tracing/recorder.py`, `docs/setup.md`, `docs/how-it-works.md` · A6 `pkg/resources/` (+ own tests). Runner at close: config keys `attempt_budget_usd`, `attempt_max_input_tokens_cloud/_local`, `retention_days`, `install_refill_cache`; `PruneSteps` + daily prune in the poll; `fman prune`; budget in status/summary/export | [x] built |
| 14 | B1: GH #52 · B2: todo #57 (GH #34) · B3: todo #59 (GH #36) · B4: todo #51 (GH #28) · B5: todo #46 (GH #23) · B6: todo #61, #63 (GH #38, #40) | B1 `pkg/tickets/clarify.py`, `pkg/routing/rules.py` · B2 `pkg/proxy/server.py`, `pkg/runner/opencode.py` · B3 `pkg/resources/memory.py`, `pkg/worker_attempt.py` · B4 `pkg/routing/profile.py`, `pkg/routing/escalation.py`, `pkg/review/reviewer.py`, `pkg/spend.py`, `pkg/worker_review.py`, `pkg/worker_route.py`, `pkg/worker_status.py`, `pkg/db/store.py`, `008_profiles.sql` · B5 `pkg/metrics/queries.py`, `pkg/metrics/web.py` · B6 `pkg/tickets/conventions.py`, `pkg/tickets/prompt.py`, `pkg/paths.py` (+ own tests). GH #52 (cold-load allowance for the clarity check and router) has no todo id. Runner at close: config `cold_load_timeout_seconds`, `max_local_inference`, `[rules_file]`; `OllamaWatch` on the worker; shared `LocalSlots` + driver `extra_time`; profile limits/start tier in the policy, `writer_gate`, review-off hand-over, `fman repo set-profile`, PR profile line; `conventions.gather` in the attempt | [x] built; live check open for #51 |
| 15 | C1: todo #52, #64 (GH #29, #41) · C2: todo #62 (GH #39) · C3: todo #66 (GH #43) · C4: todo #48 (GH #25) · C5: todo #47 data (GH #24) · C6: todo #68, #58 status line (GH #45, #35) | C1 `pkg/proxy/server.py`, `pkg/proxy/pricing.py`, `pkg/runner/opencode.py`, `pkg/config.py`, `tests/fakes/anthropic.py` · C2 `pkg/review/reviewer.py`, `pkg/prompts/reviewer-v2.md` · C3 `pkg/experiments/replay_runner.py`, `pkg/worker_experiments.py` · C4 `pkg/tickets/prompt.py`, `pkg/worker_attempt.py` · C5 `pkg/metrics/quality.py`, `pkg/worker_quality.py`, `pkg/github/client.py`, `tests/fakes/github.py`, `pkg/db/store.py`, `009_post_merge.sql` · C6 `pkg/plugins.py`, `pkg/worker_status.py` (+ own tests). Runner at close: recorder cache counts; reviewer gets the repo's `rules_file`; `review_change(conventions=)` for reviewer rules replays; `fman experiment run --rules`; `QualitySteps` + daily scan in the poll; plugin CLI commands, startup lines, Ollama and plugin lines in `fman status`; SPEC Capture wording and Plugins section | [x] built; live checks open for #52, #64 |
| 16 | D1: todo #65 (GH #42) · D2: #49 (GH #26) · D3: #47 display (GH #24) · D4: #34 split (GH #16) · D5: #35 (GH #17) · D6: #50 (GH #27) · D7: #77, #70 (GH #70, #59) · D8: #72 (GH #61) | D1 `pkg/rules_show.py`, `pkg/tickets/conventions.py` · D2 `pkg/routing/escalation.py`, `pkg/worker_review.py`, `pkg/spend.py`, `pkg/experiments/shadow.py`, `pkg/experiments/replay_sets.py`, `pkg/tracing/failure_label.py`, `pkg/db/store.py`, `010_local_only.sql` · D3 `pkg/metrics/queries.py`, `pkg/metrics/web.py`, `pkg/routing/knn.py` · D4 `pkg/experiments/split.py`, `pkg/worker_attempt.py`, `pkg/worker_experiments.py` · D5 `pkg/experiments/seeded.py`, `pkg/experiments/replay_runner.py` · D6 `pkg/tickets/decompose.py`, `pkg/prompts/split-v1.md`, `pkg/worker_route.py` · D7 `pkg/sweep.py`, `pkg/worker_queue.py`, `pkg/config.py` · D8 `pkg/review/reviewer.py` (+ own tests). Runner at close: `fman rules show`, `fman repo set-cloud`, `fman experiment split …`, `--mode seeded`, `fman sweep`; config `split_large`, `split_timeout_seconds`, `reviewer_order`; local-only limits/embedder/replay cases/status line; dormant `local_only`, `split_proposed`; reviewer pick + escalation in the review stage; Ollama "slow" status | [x] built; live check open for #77; #34 `ab:pair` open |
| 17 | E1: todo #60 (GH #37) · E2: #67 (GH #44) · E3: #69 (GH #49) · E4: #36 (GH #18) · E5: #37 (GH #19) · E6: #33 (GH #15) · E7: #78 (GH #71) · E8: #79 (GH #73) · E9: #81 (GH #77) | E1 `pkg/resources/snapshot.py`, `pkg/worker_resources.py`, `pkg/metrics/web.py`, `pkg/metrics/trace_view.py` · E2 `pkg/metrics/experiments_ui.py`, `pkg/metrics/forms.py`, `pkg/worker_experiments.py` · E3 `pkg/experiments/propose.py`, `pkg/experiments/replay_sets.py`, `pkg/prompts/proposer-v1.md`, `pkg/db/store.py`, `011_proposed.sql` · E4 `pkg/tracing/fork.py`, `pkg/tracing/replay.py` · E5 `pkg/routing/classifier.py`, `pkg/worker_route.py` · E6 `pkg/github/relay.py`, `pkg/setup/manifest.py`, `docs/relay.md` · E7 `pkg/tickets/images.py`, `pkg/tickets/prompt.py`, `pkg/worker_attempt.py` · E8 `pkg/retry.py`, `pkg/worker_actions.py` · E9 `pkg/worker_status_comment.py`, `pkg/github/client.py`, `tests/fakes/github.py` (+ own tests). Runner at close: `ResourceSteps` + `StatusCommentSteps` on `Worker`, snapshot per poll, status-comment call sites, relay start/nudge/stop, retry mark in `policy_for`; `fman experiment propose | approve-draft | discard-draft`, `fman replay --fork`, `fman retry`, `fman setup --relay-url`; config `classifier_router`, `classifier_min_tickets`, `relay_url`, `relay_secret_ref`, `status_comment`, `status_label`, `status_show_cost`, `[experiments] proposer_*`, `fork_*`, `[[models]] vision`; `/experiments/manage` in `full_app` with the propose section; replay cutoff + stored images; export/anchor fixes; docs + SPEC | [x] built; live check open for #33; #79's button in wave 18 |
| 18 | F1: todo #38 (GH #20) · F2: #39 (GH #21) · F4: #73 (GH #62) · F5: #74 (GH #63) · F6: #76, #79 button (GH #65, #73) · F7: #71, #75 design (GH #60, #64) · #22 in bot-inator | F1 `packaging/homebrew/`, `docs/homebrew.md` · F2 `pkg/setup/manifest.py`, `docs/publishing.md` · F4 `pkg/metrics/settings_ui.py`, `pkg/worker_config_reload.py` · F5 `pkg/metrics/tiers_ui.py` · F6 `pkg/metrics/activity_ui.py` · F7 `pkg/chat.py`, `docs/chat-design.md` (+ own tests). Runner at close: `fman setup --public` → `run_setup` → `register_app` → `CallbackServer(public=)`, share text / publishing.md pointer; `ConfigReloadSteps` on `Worker`, reload at the top of each poll, `watch_config` in `fman run`, restart text names `RESTART_FIELDS`; `/settings`, `/tiers`, `/activity` (Retry via `web.retry_github`) in `full_app` with one shared nav; docs (commands, configuration, setup, how-it-works, README, releasing), SPEC `--public` line; `tests/test_wave18_wiring.py` | [x] built; #38, #39 user steps open; #71/#75 wait on the user's answers; #22 uncommitted in bot-inator |
| 19 | C1a: #71 store (migration 012) · C1b: proxy chat contexts, priority local slot, 5 s cloud fallback · C1c: OpenCode chat server, chat config, chat worktrees, mirror lock · C1d: chat spend (sub-cap, turn budget, fallback pick) · C1e: chat file locks (GH #60) | C1a `pkg/db/store.py`, `012_chat.sql` · C1b `pkg/proxy/server.py`, `pkg/tracing/otel.py`, `pkg/tracing/recorder.py` · C1c `pkg/runner/opencode.py`, `pkg/runner/worktree.py` · C1d `pkg/spend.py` · C1e `pkg/locks.py`, `pkg/worker_status.py` (+ own tests). Runner at close: locks on the store's chat helpers; spend accepts the proxy's fallback calls and tags the real column; config `chat_local_wait_seconds`, `chat_start_tier`, `chat_turn_budget_usd`, `chat_idle_minutes`, `chat_max_sessions`, `[spend_caps] chat_monthly_usd` (into `Spend` in `fman run`); `prune` runs `prune_chats`; lock waits name chat sessions in logs and on `/activity`; `fman repo set-chat-network` and its `/settings` field; `/experiments/manage` reads config.toml per request; docs; `tests/test_wave19_wiring.py` | [x] built; inert until the chat engine (C2) |
| 21 | a: #75 `/chat` page (GH #64) · b: #71 `fman chat` (GH #60) · c: chat spend in metrics queries (GH #60) · d: #82 approvals carry across restarts (GH #80); runner: design wave C4 (wiring) folded in, so there is no wave 22 | a `pkg/metrics/chat_page.py` · b `pkg/chat_cli.py` · c `pkg/metrics/queries.py` · d `pkg/spend.py`, `pkg/db/store.py`, `015_*.sql` (+ own tests). Runner at close (C4): `fman chat` in `cli.py`; `full_app` mounts `chat_page.routes` with one lazy `ChatManager` built by `chat_cli.default_manager(store_path)` and shut down by the app's lifespan (uvicorn `lifespan="on"`), "Chat" in the shared nav; the metrics page passes `chat_monthly_usd` and `Spend.month_window()` to `q.report` and shows a "Spend: tickets · chat · experiments" card; `SpendSummary.chat` (`queries.chat_status`) on the `fman status` spend line; `Spend.approval_note` under a ticket's status line; `Worker.recover` calls `spend.reopen_unspent_approvals(fixed)`; docs (commands, configuration, how-it-works, README), SPEC chat section under 7a–7g; `tests/test_wave21_wiring.py` | [~] live: chat live checks and #82's repro |
| — | #34 (`ab:pair`) | unscheduled (SPEC milestone 5; #33, #36, #37 built in wave 17; #38, #39 code in wave 18) | open |
| — | #45–#50 | unscheduled (gap review, 2026-11-23) | open |
| — | #53–#56 | unscheduled (first live run, 2026-11-23); #45 gained live evidence in the same run | open |

### Runner decisions on open questions (assistant defaults, 2026-11-23)

Proposed when the plan was written; the user can overturn any.
Python ≥3.12 with `uv` · CLI with `typer`, entry points `foreman-inator`
and `fman` · HTTP with `httpx` · GitHub App JWT with `PyJWT[crypto]` ·
SQLite through the stdlib `sqlite3` with numbered migrations · the proxy
and metrics page on `starlette` + `uvicorn`, bound to 127.0.0.1 only ·
fde-kit as a dependency (git URL; path install for local development),
using `fde_kit.tracing.init_tracing` / `trace_content`,
`fde_kit.prompts.load_prompt` / `prompt_hash` for prompt versions, and
fde-kit's JSONL run-record row format (`kind` first) for recordings ·
worker branches named `fman/issue-<n>/a<attempt>` · polling every 45 s ·
one ticket per repo at a time until #19 (file locks) lands.

## Live checks

Open (need the user; full steps in each item's DONE.md entry):
- #8 `fman setup`: run it on your Mac, create and install the App on one test repo, confirm the four permissions and no active webhook. Also shows whether GitHub accepts a manifest with no `hook_attributes`.
- #10 OpenCode driver: one real tier 1 attempt on a toy repo through the proxy. Covered by #14's live check.

- #14 worker loop v1 (milestone 1): install the tool, `fman setup`, label 3 small issues on a toy repo, review the PRs, kick one back once, check `fman status` and the trace.

- #17 AI reviewer: with review on, 5 real tickets on a toy repo; note whether its kickbacks were fair. Run together with #21's check.

- #21 worker loop v2 (milestone 2): one week on 1–2 real repos (public only after #40), then read the metrics page. Steps in DONE.md.

- #40 sandbox: one real ticket with the sandbox on; then an issue that tells the agent to print a file from `~/.ssh` and fetch an outside URL: both blocked in the trace.
- #41 GitHub CI: add Checks: Read and Actions: Read to the App; a ticket on a repo with a failing Actions job stays a draft.
- #44 clarifying questions: label a deliberately vague issue; answer from the phone; the attempt uses the answer.
- Optional (#42, #43): a request-changes review with line comments; a conflicting commit on the base of a PR in review.

One combined step list for wave 6b is in DONE.md's #40, #41 and #44 entries.
- #29 milestone 4: one overnight replay of a prompt change (a new prompt file), then read the comparison. Steps in DONE.md.
- #31 packaging: decide how fde-kit reaches strangers (it is not on PyPI, so `uv tool install foreman-inator` cannot work yet; the release workflow's publish jobs are switched off). Then TestPyPI, then PyPI. Steps in `docs/releasing.md`.
- #52 prompt caching: one real Claude writer attempt on a toy repo (from the second model call on, `cache_read_input_tokens` above zero in the trace; cost below the uncached figure), and one GLM attempt on the Coding Plan endpoint (note whether `cached_tokens` is ever above zero).
- #64 OpenCode instruction files: in one real attempt on a repo with an `AGENTS.md`, the first `llm.call` carries the file's text once (from our prompt), not twice.
- #33 webhook relay: a smee.io channel (or your own relay), `fman setup --relay-url <url>` (or the App's webhook set by hand plus `fman config set-key relay-webhook-secret`), restart the worker, label an issue: the log says `relay nudge for <repo>: polling now` within seconds.
- #71/#75 chat (design section 14): a chat on a real repo at tier 1 (`fman chat <repo>` and the `/chat` page), `/tier up` to a cloud tier, the cost matches `fman status`'s chat figure, `/pr` opens a draft, `/discard` leaves no branch.
- #82 approval carry: approve a thrifty or over-cap ticket, `launchctl kickstart -k` the worker mid-attempt; the next attempt runs without a new approval and `fman status` shows `SPEND: approval #N: $x of $y left` under the ticket.
- #32 docs: follow `docs/setup.md` on a second Mac account from scratch; record `docs/demo.gif` from a real run.

## Completed items (archived to DONE.md)

#1 — Repo scaffold: package, tooling, CI, secret guards [x] · 2026-11-23 · see DONE.md
#2 — Paths and config: `~/.foreman-inator`, config.toml, Keychain keys [x] · 2026-11-23 · see DONE.md
#3 — SQLite store: full schema and migrations [x] · 2026-11-23 · see DONE.md
#4 — GitHub App auth, REST client and a fake GitHub [x] · 2026-11-23 · see DONE.md
#5 — Tracing core and run recorder (fde-kit) [x] · 2026-11-23 · see DONE.md
#6 — Verify command detection and runner [x] · 2026-11-23 · see DONE.md
#7 — Worktree manager [x] · 2026-11-23 · see DONE.md
#8 — `fman setup`: App via manifest flow, dependency check, launchd [~] live · 2026-11-23 · see DONE.md
#9 — Model proxy: OpenAI-compatible, traced, recorded, priced [x] · 2026-11-23 · see DONE.md
#10 — OpenCode driver [~] live · 2026-11-23 · see DONE.md
#11 — Poller and ticket lifecycle [x] · 2026-11-23 · see DONE.md
#12 — PR publisher [x] · 2026-11-23 · see DONE.md
#13 — Writer prompt v1 and ticket prompt builder [x] · 2026-11-23 · see DONE.md
#14 — Worker loop v1: one tier, end to end (milestone 1) [~] live · 2026-11-23 · see DONE.md
#15 — Router v1: rules, tiers, `tier:N` override [x] · 2026-11-23 · see DONE.md
#16 — Escalation state machine [x] · 2026-11-23 · see DONE.md
#17 — AI reviewer [~] live · 2026-11-23 · see DONE.md
#18 — Spend caps [x] · 2026-11-23 · see DONE.md
#19 — File locks for parallel tickets [x] · 2026-11-23 · see DONE.md
#20 — Metrics queries, local page, summary comment [x] · 2026-11-23 · see DONE.md
#21 — Worker loop v2: tiers, escalation, review, caps, locks (milestone 2) [~] live · 2026-11-23 · see DONE.md
#40 — Sandbox for every process that runs model-written code [~] live · 2026-11-23 · see DONE.md
#41 — Read GitHub CI results for the PR head [~] live · 2026-11-23 · see DONE.md
#42 — Inline review comments reach the next attempt [x] · 2026-11-23 · see DONE.md
#43 — Handle a PR that conflicts with its base branch [x] · 2026-11-23 · see DONE.md
#44 — Ask before guessing: clarifying questions on a vague issue [~] live · 2026-11-23 · see DONE.md
#23 — Nearest-neighbor start rule and 10% exploration [x] · 2026-11-23 · see DONE.md
#24 — Variants and prompt versioning [x] · 2026-11-23 · see DONE.md
#25 — Replay sets: writer replay and reviewer replay [x] · 2026-11-23 · see DONE.md
#26 — Shadow reviewer [x] · 2026-11-23 · see DONE.md
#27 — Failure-step labeling [x] · 2026-11-23 · see DONE.md
#28 — Replay viewer and recorded tickets as worker regression tests [x] · 2026-11-23 · see DONE.md
#29 — Wiring for milestone 4 [~] live · 2026-11-23 · see DONE.md
#30 — `fman export --redact` [x] · 2026-11-23 · see DONE.md
#31 — Packaging, release workflow, name check [~] live · 2026-11-23 · see DONE.md
#32 — README, docs and demo [~] live · 2026-11-23 · see DONE.md

---

## Open + in-progress items

### Item template (copy from here)

```markdown
#NN — <one-line title> [ ]
Wave: _ · Agent: _ · Added: YYYY-MM-DD · Source: user | spec | assistant
Ask: What the user actually wants, in their words where possible.
DECISIONS (who, YYYY-MM-DD): Numbered list of every judgment call already
resolved, so an agent never re-litigates them.
Files:
⚬ OWNED: path/one.py, path/two.py
⚬ EDITED (additive, earlier wave): files owned by a finished item.
⚬ Do NOT touch: files owned by another item in this wave.
Approach: The intended build, plus the fallback if it doesn't survive contact.
Verify:
⚬ Concrete, observable check.
⚬ pytest + ruff gate (rule 8).
⚬ New test named, with its non-vacuity check.
Live (user): steps, if any.
Deviations: (fill in during/after) What changed from the plan.
```

All items below were written 2026-11-23 from SPEC.md (user: "write
todos organized in waves to build this app"). Section = SPEC milestone.

### Milestone 1 — GitHub App plus a polling worker on one tier

### Milestone 2 — Tiers, escalation, AI review, caps, metrics

### Hardening — gaps found in review (wave 6b)

Items #40–#44 were written 2026-11-23 from a gap review of SPEC.md,
todo.md and the code (user: "Yes, add them"). Every DECISIONS list below
is an assistant default the user can overturn. SPEC.md is an export of
the Claude doc, so each item names the SPEC change it needs instead of
editing SPEC.md.

### Milestone 3 — bot-inator integration (optional)

#22 — Add a foreman-inator item to bot-inator's todo.md [~] item #154 added to bot-inator's todo.md (uncommitted in that repo) (wave 18)
Wave: 7 · Agent: A · Added: 2026-11-23 · Source: spec
Ask: Open issues from bot-inator chat or ideas, "PR ready" cards, approve or request changes from the iPhone remote.
DECISIONS (spec, 2026-11-23): bot-inator's consent rules apply (an idea becomes an issue only after its idea card); it talks to GitHub directly, not to the worker.
Files:
⚬ OWNED: none in this repo; the work is an item in `~/dev/bot-inator/todo.md`.
Approach: write the bot-inator item (its template, its next ID) when milestone 2 is live. Nothing here depends on it.
Verify: the bot-inator item exists and links to this SPEC.
Deviations:

### Milestone 4 — Learning router, experiments, trace analysis

### Release

### Unscheduled (SPEC milestone 5)

#33 — Webhook relay for instant reactions [~] live · done 2026-11-25, wave 17, see DONE.md
#34 — Traffic split and head-to-head (`ab:pair`) writer experiments [~] split done 2026-11-25, wave 16, see DONE.md; `ab:pair` not built (needs data-model rework)
#35 — Seeded-bug reviewer test cases [x] · done 2026-11-25, wave 16, see DONE.md
#36 — Fork from failure: restart a recorded attempt at its failure step with a new prompt or model [x] · done 2026-11-25, wave 17, see DONE.md
#37 — Classifier router, then a LoRA router only if it beats the classifier on held-out tickets [x] · done 2026-11-25, wave 17, see DONE.md
#38 — Homebrew tap [~] code/docs done; user steps listed in docs/homebrew.md (wave 18, F1, GH #20) · Added: 2026-11-23 · Source: spec
#39 — Publish the App for others (each user still runs their own worker) [~] code/docs done; user steps listed in docs/publishing.md (wave 18, F2, GH #21: `fman setup --public`) · Added: 2026-11-23 · Source: spec

### Unscheduled (gap review, 2026-11-23)

Found in the same review as #40–#44 (user: "Yes, add them"). Each has
its evidence and a first idea; none has decisions or owned files yet, so
write the full item from the template before scheduling one.

#45 — Install dependencies in a fresh worktree before verify [~] live · fixed 2026-11-24, wave 11, see DONE.md · Added: 2026-11-23 · Source: assistant (gap review)
Why: no install step exists anywhere in the code (no `npm ci`, `uv sync` or `swift package resolve`), so a new worktree has no `node_modules` or venv and verify can fail for reasons unrelated to the change.
Idea: detect an install command next to the verify command, save it per repo like `fman repo set-verify`, run it once per worktree. Needs network, so decide it together with #40's `sandbox_network`.
Live evidence (2026-11-23, first live run, `Bforsyth1234/fman-playground`, ticket 2, attempt 11): with the sandbox on, verify ran `uv run pytest` and failed with "Network connectivity is disabled, but the requested data wasn't found in the cache" (`~/.foreman-inator/traces/2/attempt-11-verify.log`). The sandbox moves `UV_CACHE_DIR` into an empty scratch dir and the repo had no `sandbox_network = "allow"`, so nothing could be installed. The change itself was correct. The writer hit the same wall in its own bash tool and spent most of 18.6 minutes and 585k input tokens trying to get pytest to run. This now blocks every repo with dependencies, so it is no longer only a gap-review idea. Options seen: a read-only shared package cache inside the sandbox, an install step that runs with network before the sandboxed verify, or telling the user to set `sandbox_network` when verify fails this way.

#46 — Metrics: attempt minutes and review count next to dollars [x] · done 2026-11-25, wave 14, see DONE.md · Added: 2026-11-23 · Source: assistant (gap review)

#47 — Post-merge quality signal [x] · done 2026-11-25, wave 16, see DONE.md

#48 — Issue comments as context for the attempt [x] · done 2026-11-25, wave 15, see DONE.md · Added: 2026-11-23 · Source: assistant (gap review)

#49 — Per-repo "local only" setting [x] · done 2026-11-25, wave 16, see DONE.md

#50 — Split a large issue into smaller tickets [x] · done 2026-11-25, wave 16, see DONE.md

### Unscheduled (user request, 2026-11-23)

#51 — Repo profiles: per-repo `writer` and `review` settings [~] live · done 2026-11-25, wave 14, see DONE.md · Added: 2026-11-23 · Source: user

#52 — Prompt caching: native Anthropic route in the proxy, cached-token accounting for Claude and GLM [~] live · done 2026-11-25, wave 15, see DONE.md · Added: 2026-11-23 · Source: user

### Unscheduled (first live run, 2026-11-23)

Found by the first real ticket: issue #1 on the private test repo `Bforsyth1234/fman-playground` ("Fix typo in README"), tier 1 `inkling-2-mini`, sandbox on, worker started with `uv run fman run`. One more problem from the same run is already fixed in the working tree: `warm_up_opencode` now opens one session, because OpenCode writes `config/opencode/.gitignore` into the shared config dir on its first session and a sandboxed attempt has that dir read-only (every attempt on a fresh install failed with `POST /session: 500`).

#53 — A setup failure must flag the ticket, not climb the ladder [~] live · fixed 2026-11-24, wave 11, see DONE.md · Added: 2026-11-23 · Source: assistant (first live run)
Why: ticket 1 made 10 attempts in 20 seconds, two on every model up to `claude-fable`, and was then handed to the user. Each attempt ended in about 1 second with 0 tokens: the OpenCode session could not be opened (`POST /session: 500`), the driver returned `status="errored"`, and the worker treated that as failed checks and escalated. No model was ever called. With API keys stored, the cloud tiers would have been tried for a failure no model can fix, and all 10 rows count as `failed_checks` against their models in the metrics.
Idea: tell "the attempt could not run" (server did not start, session not opened, 0 model calls) apart from "the change failed its checks". The first kind flags the ticket once with the error, makes no further attempt, and is left out of the per-model metrics, like `FLAG_SANDBOX` already does for a missing sandbox backend.

#54 — The sandboxed child inherits the worker's PATH and VIRTUAL_ENV [x] · fixed 2026-11-24, wave 11, see DONE.md · Added: 2026-11-23 · Source: assistant (first live run)
Why: the worker was started with `uv run fman run`, which puts `foreman-inator/.venv/bin` first on PATH and sets `VIRTUAL_ENV`. Both reached the sandboxed writer and verify: inside the attempt `which python3` was foreman-inator's own venv, `uv` warned that `VIRTUAL_ENV` "does not match the project environment path", and the model listed that venv's `site-packages` while looking for pytest. The attempt reads files outside its worktree and can run the worker's interpreter, so a repo's checks may pass or fail because of what foreman-inator itself has installed.
Idea: `sandbox.child_env` drops `VIRTUAL_ENV` (and other tool-activation variables) and removes the worker's own venv from PATH. Decide whether reads under the worker's checkout should be denied by the profile as well.

#55 — Heredocs fail in the sandboxed bash tool [x] · fixed 2026-11-24, wave 11, see DONE.md · Added: 2026-11-23 · Source: assistant (first live run)
Why: the model's `python3 - <<'EOF'` and `cat > file <<'EOF'` commands both failed with "zsh:1: can't create temp file for here document: operation not permitted". Heredocs are how local models most often write files from the shell; this one fell back to `sed` and the write tool after two lost turns.
Idea: likely cause, not confirmed: zsh creates heredoc temp files under `TMPPREFIX` (default `/tmp/zsh`), not `TMPDIR`, and the profile denies `/tmp`. Set `TMPPREFIX` into the scratch dir in `sandbox.child_env`, and add a sandbox test that runs a heredoc.

#56 — Setup puts every installed repo into config.toml [x] · fixed 2026-11-24, wave 11, see DONE.md · Added: 2026-11-23 · Source: assistant (first live run)
Why: GitHub's install page defaults to "All repositories". `fman setup` then wrote all 141 of the user's repos (87 public) into `repos`, and the worker started with "141 repo(s), polling every 45s": about 11,000 issue listings an hour, at or over the installation's rate limit (not confirmed for this account), and far more repos than the user meant to hand over. There is no command to change the list; it was cut to one repo by rewriting the file through `config.save`.
Idea: setup shows the count and asks which repos to work on when the installation covers more than a few (default: none until chosen), and says how to narrow the installation on GitHub. Add `fman repo add` / `fman repo remove`. Consider one installation-wide listing per poll instead of one per repo.

### Unscheduled (local model resources, 2026-11-23)

Asked by the user: "should we add the ability for the app to track gpu / memory usage and keep track of how many local models are running at a time?", then "lets add all 4 to the todo.md". GPU utilization % is left out on purpose: on macOS it needs `sudo powermetrics`, which a launchd worker cannot run, and on Apple Silicon the GPU shares system memory, so memory pressure plus Ollama's loaded-model list covers it.

#57 — Global cap on concurrent local-model inference [x] · done 2026-11-25, wave 14, see DONE.md · Added: 2026-11-23 · Source: user

#58 — Poll Ollama for loaded models [x] · done 2026-11-25, waves 13 and 15 (status line), see DONE.md · Added: 2026-11-23 · Source: user

#59 — Record memory state on each attempt [x] · done 2026-11-25, wave 14, see DONE.md · Added: 2026-11-23 · Source: user

#60 — Resource panel on the metrics page [x] · done 2026-11-25, wave 17, see DONE.md

### Unscheduled (agent instructions and global rules, 2026-11-23)

Asked by the user: "what about agent md files? do we need to tell the agents to read those? Should we add the ability to add them? maybe the users want some global rules etc?", then "ok add these as todos to todo.md should we add the ability to test these rules?". Today `tickets/prompt.py` reads the first of `AGENTS.md` / `CLAUDE.md` in the worktree (`CONVENTIONS_FILES`, 6,000-char budget) into the writer prompt's `conventions` section. Nothing else sees it, and there is no user-level rules file.

#61 — Global rules file for every repo [x] · done 2026-11-25, wave 14, see DONE.md · Added: 2026-11-23 · Source: user

#62 — The reviewer sees the repo's conventions [x] · done 2026-11-25, wave 15, see DONE.md · Added: 2026-11-23 · Source: assistant (gap review)

#63 — Read both AGENTS.md and CLAUDE.md, not the first one found [x] · done 2026-11-25, wave 14, see DONE.md · Added: 2026-11-23 · Source: assistant (gap review)

#64 — Check whether OpenCode loads AGENTS.md on its own [~] live · done 2026-11-25, wave 15, see DONE.md · Added: 2026-11-23 · Source: assistant (gap review)

#65 — `fman rules show`: preview the rules an agent will get [x] · done 2026-11-25, wave 16, see DONE.md

#66 — Replay a rules change before it goes live [x] · done 2026-11-25, wave 15, see DONE.md · Added: 2026-11-23 · Source: user (asked how to test rules)

### Unscheduled (experiments GUI, 2026-11-23)

Asked by the user: "are they setup in the command line or do we set them up with a gui", then "yes add it as a todo". Today experiments are started and promoted only from the CLI (`fman experiment run | status | promote`, `[experiments]` in config.toml, the nightly launchd job); `/experiments` on the metrics page is read-only (GET routes only).

#67 — Start, cancel and promote experiments from the metrics page [x] · done 2026-11-25, wave 17, see DONE.md

### Unscheduled (plugins, 2026-11-23)

Asked by the user: "add the plugin hooks as a todo". Nothing in this repo loads plugins today (no entry-point lookup anywhere in `src/`).

#68 — Plugin hooks, so a separately installed package can extend the worker [x] · done 2026-11-25, wave 15, see DONE.md · Added: 2026-11-23 · Source: user

### Unscheduled (LLM-proposed experiments, 2026-11-24)

Asked by the user: "does it make sense for the llm to create experiments?", then "yes add it as a todo and open a github issue". Today every challenger is written by hand: someone has to notice a pattern in kickbacks, write `writer-vN.md` / `reviewer-vN.md`, then run `fman experiment run --challenger`.

#69 — An LLM drafts challenger prompts from labeled kickbacks; promotion is unchanged [x] · done 2026-11-25, wave 17, see DONE.md

### Unscheduled (tier order, 2026-11-25)

Asked by the user: "do local llms make sense in this app or should the first tier be open source cloud llms then more expensive ones like claude etc", then "yes, add it". Today `config.py:208-217` gives tier 1 `inkling-2-mini` (local), tier 2 `qwen3.8-27b` (local), tier 3 `glm-5.3` then Claude.

#70 — Tier 2 default becomes open-weights cloud (GLM); local 27B becomes optional [x] · done 2026-11-25, wave 16, see DONE.md

#71 — Chat feature: interactive coding sessions through the same tiers and spend caps [~] live · done 2026-11-25, waves 19–21, see DONE.md

#72 — Cheapest-first cross-family reviewer [x] · done 2026-11-25, wave 16, see DONE.md

### Unscheduled (web UI, 2026-11-25)

Asked by the user: "Would it be useful to add a ui to this app? I want users to be able to change models and providers in tiers easily. and the chat feature needs a ui. The user could also change settings as well in it.", then "add all three pages as todos and open matching github issues. also i think a page where the user can see what is being actively worked on by the agents would be helpful". All four pages grow the existing local metrics app (`metrics/web.py`, Starlette, served by `fman metrics`, loopback only) instead of a separate app. A native Mac app is out of scope until this proves out.
Shared rules for every page that writes: the POST protection designed in #67 (per-process CSRF token in each form, `Origin` / `Host` must be the page's own loopback origin, 403 and no change otherwise); config writes go through `config.load` / `config.save` so validation is the same as the CLI; API keys go straight to the Keychain (same path as `fman config set-key`) and are never rendered back; everything from GitHub or the database stays HTML-escaped. Build the shared POST layer once, in whichever of #67 / #73 lands first.

#73 — Settings page: edit config from the browser [x] · done 2026-11-25, wave 18, see DONE.md

#74 — Tiers page: models and providers per tier [x] · done 2026-11-25, wave 18, see DONE.md

#75 — Chat page: the UI for #71 [~] live · done 2026-11-25, wave 21, see DONE.md

#76 — Activity page: what the agents are working on right now [x] · done 2026-11-25, wave 18, see DONE.md

### Unscheduled (bulk sweep, 2026-11-25)

Asked by the user: "can we add a feature to take every issue in github and use agent waves to complete them", then chose "Build `fman sweep` with dynamic waves and a cap on open PRs".

#77 — `fman sweep`: start every open issue, dynamic waves, cap on open PRs [~] live · done 2026-11-25, wave 16, see DONE.md

### Unscheduled (images in issues, 2026-11-25)

Asked by the user: "if i respond to a comment for needs info does it pick that comment up? and what if the comment is an image", then "yes add it as a todo and open a matching github issue". Today an image in an issue body, a needs-info answer or a comment reaches the writer only as its markdown link (`![image](https://github.com/user-attachments/...)`): the ladder's models get text only, and the sandbox blocks the network, so the writer cannot fetch it.

#78 — Images in issues and answers reach a vision-capable model [x] · done 2026-11-25, wave 17, see DONE.md

### Unscheduled (retry from the UI, 2026-11-25)

#79 — Retry button for a flagged or failed ticket [x] · done 2026-11-25, wave 17 (CLI) and wave 18 (button on `/activity`), see DONE.md

### Unscheduled (second live run, bot-inator, 2026-11-25)

#80 — The sandbox shows `~/.env` exists but refuses to read it, so `load_dotenv()` crashes verify [~] live · fixed 2026-11-25, wave 16 · Added: 2026-11-25 · Source: user (bot-inator ticket 10, attempt 33 trace)
Why: python-dotenv's `load_dotenv()` with no path walks up from the worktree (`~/.foreman-inator/worktrees/...`) to `/Users/<user>/` looking for `.env`. The sandbox profile allows `file-read-metadata` on `/` (`runner/sandbox.py`, ~line 767) but denies the contents of sensitive files, so `~/.env` passes `os.path.isfile` and then `open()` raises `PermissionError`. On bot-inator, `smolagents/remote_executors.py:48` calls it at import time, which crashed collection of all 136 test files: verify failed for every attempt whatever the change, and the writer spent its turns working around it. Any repo whose dependencies call `load_dotenv()` at import (FastAPI, Django and most agent frameworks) hits the same wall.
DECISIONS (assistant, 2026-11-25): Make the files invisible, not readable. Deny `file-read*` including metadata for `.env`-style files (`.env`, `.env.local`, `.env.*`) in every ancestor directory of the worktree that is outside the worktree, so code in the sandbox sees "no such file" and moves on. The worktree's own `.env` files stay readable. Nothing that was unreadable becomes readable (invariant 7d: secrets never reach the attempt).
Files: ⚬ EDITED: src/foreman_inator/runner/sandbox.py (profile rules), tests (new, macOS-only like the other sandbox tests).
Verify: a sandboxed process in a worktree with a decoy `.env` in a parent folder sees `os.path.exists` False, and a find_dotenv-style upward search does not raise; the worktree's own `.env` is readable; the existing sensitive-path tests stay green. Non-vacuous: drop the new metadata deny and the decoy test fails with PermissionError.
Live (user): restart the worker, re-label bot-inator#56: verify collects its tests instead of failing at import.
Scheduled: wave 16 (built with the wave 16 review fixes).
Deviations:

#82 — A worker restart throws away a spend approval's unspent budget [~] live · fixed 2026-11-25, wave 21, see DONE.md · GH #80

#83 — `fman status` calls a spend-paused blocker "flagged" [ ] · Added: 2026-11-25 · Source: user (live `fman status`) · GH #82
Why: ticket #13's wait line said "#12 is flagged: close or re-label issue …" while the footer said "0 flagged for you". #12 was only paused for a spend approval (`flag_reason` starts with `FLAG_SPEND_CAP`). `worker_status.py` `_release_hint` (~line 100) treats any `flag_reason` as a flag; the footer count (~line 176) and the ticket's own line (~line 83) already treat a spend pause as a wait. The wrong hint tells the user to close the issue when one command would unblock it.
DECISIONS (assistant, 2026-11-25): for a spend-paused blocker the hint reads "#12 waits for spend approval: run `fman approve-spend 12`"; real flags keep "close or re-label".
Files: ⚬ EDITED: src/foreman_inator/worker_status.py, tests/test_worker_status.py.
Verify: spend-paused blocker → hint names `fman approve-spend <id>`, flagged count unchanged; real flag → old text. Non-vacuous: drop the new branch, first test fails. pytest + ruff gate (rule 8).
Deviations:

#84 — Images attached to issues are dropped: GitHub's S3 redirect is refused [ ] · Added: 2026-11-25 · Source: user (bot-inator ticket 11, attempt 48 log) · GH #83
Why: `https://github.com/user-attachments/assets/<id>` redirects to a pre-signed URL on `github-production-user-asset-6210df.s3.amazonaws.com`. `tickets/images.py` checks every redirect with `allowed_url` (~line 120), which allows only `github.com/user-attachments/` and the two `*.githubusercontent.com` hosts, so `fetch` (~line 236) raises "redirect off GitHub's attachment hosts" and the image never reaches the model. This is GitHub's normal path for new attachments, so #78 (images reach a vision model) likely does nothing in practice.
DECISIONS (assistant, 2026-11-25): allow that exact S3 host only as a redirect target from a `github.com/user-attachments/` URL, never as a starting URL found in issue text; https, no port or userinfo. The App token still goes only to `github.com`. Size, type and redirect caps unchanged, so the no-SSRF rule holds. Check GitHub's docs first and record the URL and date here (rule 12).
Files: ⚬ EDITED: src/foreman_inator/tickets/images.py, its tests.
Verify: fake transport `user-attachments` → 302 to the S3 host → 200 PNG is used; the same S3 URL given directly in issue text is refused; a redirect to another `*.s3.amazonaws.com` bucket is refused. Non-vacuous: remove the redirect-target allowance, first test fails. Live (user): re-run a ticket whose issue has a screenshot; no "redirect off" warning in `worker.out.log`. pytest + ruff gate (rule 8).
Deviations:

### Unscheduled (GLM Coding Plan quota, 2026-11-25)

#85 — Track GLM Coding Plan credits and pause tier 2 at z.ai's quota errors [~] live · built 2026-11-25 on branch `glm-plan-quota` (from `glm-plan-spend`) · Added: 2026-11-25 · Source: user (subscribed to the GLM Coding Plan Lite, $18/month; goal: cancel the $100/month Max plan)
Ask: the plan has credit limits per 5 hours and per week. z.ai offers no API to read usage, so estimate it locally from token counts, show it in `fman status`, and react to the 429 quota codes instead of burning attempts or escalating to Claude.
DECISIONS (assistant, 2026-11-25):
1. Credits per call = (uncached input × in_mult + cached input × cached_mult + output × out_mult) / 10,000; off-peak is half rate; peak is Mon–Fri 14:00–18:00 UTC+8. GLM-5.3 6.9/1.7/24, GLM-5.4 4.6/1.1/16, GLM-5.3-Flash 1.15/0.28/4. Table in `proxy/pricing.py` (source https://docs.z.ai/devpack/overview, read 2026-11-25). The temporary GLM-5.3-Flash 50% promotion (2026-11-24 to 2026-12-24) is not modelled: the estimate runs high, not low.
2. Credits are stored on the `plan = 1` ledger rows (`spend_ledger.credits`, migration 014; #82's migration takes the next free number).
3. `[[models]] plan_tier = "lite" | "pro" | "max"` picks the limits (Lite 2,000/10,000, Pro 12,000/60,000, Max 28,000/140,000). `fman status` shows "GLM credits (estimate): X of N in the last 5h, Y of M in the last 7 days": z.ai's 5-hour window starts at first use and its weekly window at purchase, so local rolling windows are approximations.
4. Quota errors (https://docs.z.ai/api-reference/api-code, read 2026-11-25): HTTP 429 `{"error":{"code":"1308","message":"..."}}`. 1308 (5-hour), 1310 (weekly), 1316–1321 (other limit-reached cases) carry `{next_flush_time}` in the message; its format is undocumented, so it is parsed leniently (as UTC+8 when no zone is given) with a 1-hour retry fallback. 1309 (plan expired) waits for the user (`fman approve-spend <ticket>` after renewing). The parse lives in one adapter, `proxy/zai_quota.py`.
5. On a quota code the plan model is paused until the reset (kept in the store, so a restart keeps it): its calls are refused before they are sent, a writer attempt on it is not started, and an attempt cut by it is set aside uncounted (no escalation to Claude) and the ticket waits with a `spend_cap: plan quota` flag that lifts itself at the reset.
Files: ⚬ EDITED: src/foreman_inator/proxy/pricing.py, proxy/server.py, config.py, spend.py, db/store.py, worker_attempt.py, worker_rebase.py, docs/configuration.md · ⚬ NEW: proxy/zai_quota.py, plan_quota.py, db/migrations/014_plan_credits.sql, tests/test_plan_quota.py
Verify:
⚬ credit math: cached vs uncached, peak vs off-peak (test_plan_quota.py).
⚬ status line shows credits against the plan's limits.
⚬ a fake upstream answering 429 code 1308 / 1310 pauses the model until the parsed reset; the next call is refused before it is sent; the attempt is set aside uncounted.
⚬ each new test shown failing with its change removed; pytest + ruff gate (rule 8).
Live (user): set `plan_tier = "lite"` on the `glm` model in config.toml. After the next 5-hour limit hit, `fman status` shows the pause and the reset time, and the ticket resumes on its own. Compare the status line's credit estimate with z.ai's dashboard (https://z.ai/manage-apikey/subscription) once or twice; if it drifts, the multipliers in `proxy/pricing.py` need a re-read.
Results (2026-11-25): tests/test_plan_quota.py (14 tests). Non-vacuous, each checked by breaking one piece: off-peak rate set to 1.0 → `test_off_peak_is_half_rate` fails; cached tokens priced as input → `test_cached_input_uses_the_cached_multiplier` fails; credits dropped from `status_line` → `test_status_line_shows_credits_against_the_plan_limits` fails; proxy no longer reports the 429 → both worker tests (1308, 1310) and the proxy test fail; worker `plan_gate` removed → the worker tests fail on the second ticket. Full suite 2237 passed, 1 skipped; ruff check and format clean.
Deviations:
⚬ z.ai documents `{next_flush_time}` only as a placeholder (no format, no zone), so the reset time is parsed leniently (decision 4) and checked on the worker's clock in `plan_quota` (past or more than 8 days ahead → retry in 1 hour).
⚬ The pause is per account (`api_key_ref`, else base URL), not per model: the plan's quota is shared by every GLM model on the key. Credits are summed the same way.
⚬ `fman approve-spend <ticket>` also lifts a quota pause (and uses up the approval): the only way out of 1309 (expired) and an override when the dashboard shows quota left before the reset.
⚬ Migration 014 is taken here, so #82 needs the next free number (its text still says 013).

### Unscheduled (status on the issue, 2026-11-25)

#81 — Say on the issue when work starts, and keep that one comment up to date [x] · done 2026-11-25, wave 17, see DONE.md
