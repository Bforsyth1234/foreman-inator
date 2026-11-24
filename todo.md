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

Next ID: #40
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
| 3 | A: #8 · B: #9 · C: #10 · D: #11 · E: #12 · F: #13 | A `pkg/setup/manifest.py`, `pkg/setup/deps.py`, `pkg/setup/launchd.py`, `pkg/setup/__init__.py` · B `pkg/proxy/server.py`, `pkg/proxy/pricing.py`, `pkg/proxy/__init__.py`, `tests/fakes/models.py` · C `pkg/runner/opencode.py`, `tests/fakes/opencode.py` · D `pkg/github/poller.py`, `pkg/tickets/lifecycle.py`, `pkg/tickets/__init__.py` · E `pkg/pr/publisher.py`, `pkg/pr/__init__.py` · F `pkg/prompts/writer-v1.md`, `pkg/tickets/prompt.py` (+ own tests). Runner at close: `cli.py` (`setup`), `pyproject.toml` deps | [ ] |
| 4 | A: #14 (alone: the main loop) | `pkg/worker.py`, `pkg/cli.py` (`run`, `status`), `tests/test_worker_v1.py`, `tests/fakes/` (additive only) | [ ] |
| 5 | A: #15 · B: #16 · C: #17 · D: #18 · E: #19 · F: #20 | A `pkg/routing/rules.py`, `pkg/routing/__init__.py`, `pkg/prompts/router-v1.md` · B `pkg/routing/escalation.py` · C `pkg/review/reviewer.py`, `pkg/review/checks.py`, `pkg/review/__init__.py`, `pkg/prompts/reviewer-v1.md` · D `pkg/spend.py` · E `pkg/locks.py` · F `pkg/metrics/queries.py`, `pkg/metrics/web.py`, `pkg/metrics/__init__.py`, `pkg/pr/summary.py` (+ own tests). Runner at close: `pyproject.toml` deps | [ ] |
| 6 | A: #21 (alone: the main loop) | `pkg/worker.py`, `pkg/cli.py` (`metrics`), `pkg/pr/publisher.py` (ready-for-review + bot review posting), `tests/test_worker_v2.py` | [ ] |
| 7 | A: #22 (cross-repo, optional) | bot-inator repo only; nothing in this repo | [ ] |
| 8 | A: #23 · B: #24 · C: #25 · D: #26 · E: #27 · F: #28 | A `pkg/routing/knn.py`, `pkg/routing/explore.py` · B `pkg/experiments/variants.py`, `pkg/experiments/__init__.py`, `pkg/prompts/__init__.py` · C `pkg/experiments/replay_sets.py`, `pkg/experiments/replay_runner.py` · D `pkg/experiments/shadow.py` · E `pkg/tracing/failure_label.py`, `pkg/prompts/failure-label-v1.md` · F `pkg/tracing/replay.py`, `pkg/metrics/trace_view.py`, `tests/recordings/` (+ own tests). Runner at close: `pyproject.toml` deps | [ ] |
| 9 | A: #29 (alone: wiring) | `pkg/worker.py`, `pkg/cli.py` (`experiment`, `replay`), `pkg/metrics/web.py` (new routes and failure-step columns), `tests/test_worker_v3.py` | [ ] |
| 10 | A: #30 · B: #31 · C: #32 | A `pkg/export.py` · B `pyproject.toml` (metadata, entry points), `.github/workflows/release.yml`, `docs/releasing.md` · C `README.md`, `docs/setup.md`, `docs/how-it-works.md`, `docs/demo.gif` (+ own tests). Runner at close: `cli.py` (`export`) | [ ] |
| — | #33–#39 | unscheduled (SPEC milestone 5) | open |

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

None open yet. Milestone 1's checks are listed in #8 and #14;
milestone 2's in #17 and #21.

## Completed items (archived to DONE.md)

#1 — Repo scaffold: package, tooling, CI, secret guards [x] · 2026-11-23 · see DONE.md
#2 — Paths and config: `~/.foreman-inator`, config.toml, Keychain keys [x] · 2026-11-23 · see DONE.md
#3 — SQLite store: full schema and migrations [x] · 2026-11-23 · see DONE.md
#4 — GitHub App auth, REST client and a fake GitHub [x] · 2026-11-23 · see DONE.md
#5 — Tracing core and run recorder (fde-kit) [x] · 2026-11-23 · see DONE.md
#6 — Verify command detection and runner [x] · 2026-11-23 · see DONE.md
#7 — Worktree manager [x] · 2026-11-23 · see DONE.md

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

#8 — `fman setup`: App via manifest flow, dependency check, launchd [ ]
Wave: 3 · Agent: A · Added: 2026-11-23 · Source: spec
Ask: One command turns a fresh Mac into a working install, with the user's own private App.
DECISIONS (spec/user, 2026-11-23): each user registers their own App through GitHub's app-manifest flow; no shared App; launchd agent so the worker survives reboots.
DECISIONS (assistant, 2026-11-23): the manifest asks for exactly the permissions in SPEC (Issues RW, Pull requests RW, Contents RW, Metadata R), no webhook URL (polling). Setup checks for `git`, `opencode`, `ollama` and the tier 1/2 models, and offers the install command instead of running installers itself.
Files:
⚬ OWNED: `src/foreman_inator/setup/manifest.py`, `src/foreman_inator/setup/deps.py`, `src/foreman_inator/setup/launchd.py`, `src/foreman_inator/setup/__init__.py`, `tests/test_setup.py`
⚬ Do NOT touch: `cli.py` (runner registers `setup`), `config.py` (use its API)
Approach: Rule 12 first (manifest flow URL, the code-exchange endpoint, what it returns). Start a one-shot localhost callback server, open the browser to GitHub's create-from-manifest page, exchange the code, write `app.pem` + App ID, then open the install page and record the installation. launchd: write `~/Library/LaunchAgents/com.foreman-inator.worker.plist`, `launchctl bootstrap`; `fman setup --uninstall-service` reverses it.
Verify:
⚬ Manifest JSON matches SPEC permissions exactly (snapshot test).
⚬ Code exchange tested against a fake endpoint; key saved with mode 600.
⚬ Generated plist validates with `plutil -lint` (macOS CI job).
Live (user): run `fman setup` on your Mac, confirm the App appears under your GitHub settings with only those permissions, install it on one test repo.
Deviations:

#9 — Model proxy: OpenAI-compatible, traced, recorded, priced [ ]
Wave: 3 · Agent: B · Added: 2026-11-23 · Source: spec ("Capture")
Ask: Every model call from any coding agent is a span and a recording row, with cost.
DECISIONS (assistant, 2026-11-23):
1. One local server on 127.0.0.1 exposing `/v1/chat/completions` (streaming and not) and `/v1/models`; routes by model name to Ollama (local) or the configured cloud endpoint.
2. Each request carries the ticket/attempt via a header or a per-attempt port/path prefix (`/a/<attempt_id>/v1/...`), so spans attach to the right attempt. Path prefix is the default because OpenCode lets you set a base URL per provider (confirm, rule 12).
3. Cost from a pricing table (`pricing.py`); local models cost 0.
Files:
⚬ OWNED: `src/foreman_inator/proxy/server.py`, `src/foreman_inator/proxy/pricing.py`, `src/foreman_inator/proxy/__init__.py`, `tests/fakes/models.py`, `tests/test_proxy.py`
⚬ EDITED: uses #5's `tracing` API, no changes to it.
Approach: `starlette` app, `httpx` upstream with streaming passthrough; on completion write an `llm.call` span (tokens, latency, cost, prompt hash) and a recorder `llm` row. Spend caps hook point left as a no-op callable for #18.
Verify:
⚬ Streaming and non-streaming calls pass through byte-for-byte against `fake_models`.
⚬ A call under `/a/42/` produces a span tagged attempt 42 and a recording row.
⚬ A cloud model with no key configured fails closed (invariant 7c); non-vacuous: let it fall through, test fails.
Deviations:

#10 — OpenCode driver [ ]
Wave: 3 · Agent: C · Added: 2026-11-23 · Source: spec
Ask: Run one coding attempt with OpenCode in a worktree and get back what it did.
DECISIONS (assistant, 2026-11-23): drive `opencode serve` over HTTP (one server per attempt, cwd = worktree), provider base URL pointed at the proxy's `/a/<attempt>/v1`; tool calls mapped to `tool.call` spans and recorder `tool` rows from its event stream.
Files:
⚬ OWNED: `src/foreman_inator/runner/opencode.py`, `tests/fakes/opencode.py`, `tests/test_opencode_driver.py`
Approach: Rule 12 is critical here: read OpenCode's current server/SDK docs and record the endpoints used (create session, send prompt, event stream, abort). Wrap them in one `OpenCodeSession` adapter. Timeout per attempt (default 20 min) with abort. Fallback if the server API doesn't fit: `opencode run` non-interactive with JSON output, same adapter interface.
Verify:
⚬ Against `fake_opencode`: a scripted session that edits a file yields the right tool spans and a non-empty diff.
⚬ Timeout aborts the session and marks the attempt `failed_checks`; non-vacuous: remove the abort, test hangs → fails on pytest timeout.
Live (user): one real attempt on a toy repo with the tier 1 model, watch the spans appear.
Deviations:

#11 — Poller and ticket lifecycle [ ]
Wave: 3 · Agent: D · Added: 2026-11-23 · Source: spec ("GitHub mapping")
Ask: Turn GitHub state into ticket state, safely.
DECISIONS (spec, 2026-11-23): the event → action table in SPEC "Architecture and GitHub mapping"; only labels applied by `allowed_users` count (invariant 7b).
Files:
⚬ OWNED: `src/foreman_inator/github/poller.py`, `src/foreman_inator/tickets/lifecycle.py`, `src/foreman_inator/tickets/__init__.py`, `tests/test_lifecycle.py`
⚬ Do NOT touch: `github/client.py` (report any missing method to the runner)
Approach: `poll_once()` returns typed events (`TicketRequested`, `TierOverride`, `ReviewSubmitted(approve|changes, comment, kickback_tag)`, `Merged`, `ClosedUnmerged`). `lifecycle.apply(event)` is a pure state machine over the columns in SPEC; illegal transitions raise. Idempotent: polling the same state twice emits nothing new (store a cursor per repo).
Verify:
⚬ Table-driven tests: one row per SPEC mapping row.
⚬ A label applied by a non-allowed user creates no ticket; non-vacuous: skip the actor check, test fails.
⚬ Re-polling is idempotent.
Deviations:

#12 — PR publisher [ ]
Wave: 3 · Agent: E · Added: 2026-11-23 · Source: spec
Ask: Draft PRs that say what happened, linked to the issue.
DECISIONS (spec, 2026-11-23): PR stays draft through AI review; description shows model, tier and cost; posts from the App's bot account.
Files:
⚬ OWNED: `src/foreman_inator/pr/publisher.py`, `src/foreman_inator/pr/__init__.py`, `tests/test_publisher.py`
Approach: `open_or_update_draft(ticket, attempt)` → PR body from a template: "Closes #n", what changed (diff stats), model/tier/cost, verify result, attempt history table. Later attempts update the same PR (force-push to its `fman/` branch). `mark_ready(pr)` exists but is only called by the worker after review (wired in #21).
Verify:
⚬ Against `fake_github`: first attempt opens a draft; second updates it; body contains model, tier, cost.
⚬ `mark_ready` is never called by publisher code paths on its own; non-vacuous: call it in `open_or_update_draft`, test fails.
Deviations:

#13 — Writer prompt v1 and ticket prompt builder [ ]
Wave: 3 · Agent: F · Added: 2026-11-23 · Source: spec
Ask: What the coding agent is told for each attempt.
DECISIONS (spec, 2026-11-23): the agent gets the request, owned files and kickback notes in the session prompt; prompts are versioned files.
Files:
⚬ OWNED: `src/foreman_inator/prompts/writer-v1.md`, `src/foreman_inator/tickets/prompt.py`, `tests/test_ticket_prompt.py`
Approach: `build_writer_prompt(ticket, attempt, history)` fills `writer-v1.md` with: the issue title/body, repo conventions file if present (`AGENTS.md`/`CLAUDE.md`), owned files, verify command, previous attempt's failure output or kickback note + tag. Loaded with `fde_kit.prompts.load_prompt`; the prompt hash goes on the attempt.
Verify:
⚬ Snapshot tests for first attempt, after failed checks, after a kickback.
⚬ The reviewer's notes are included; the writer's own previous reasoning is not (keeps invariant 7f symmetric); non-vacuous: include it, test fails.
Deviations:

#14 — Worker loop v1: one tier, end to end (milestone 1) [ ]
Wave: 4 · Agent: A (alone) · Added: 2026-11-23 · Source: spec
Ask: Label an issue, get a draft PR from the local model, review it on GitHub.
DECISIONS (assistant, 2026-11-23): tier 1 only; no AI review yet (PR is marked ready as soon as checks pass, behind a `v1` flag removed in #21); one ticket per repo at a time.
Files:
⚬ OWNED: `src/foreman_inator/worker.py`, `src/foreman_inator/cli.py` (`run`, `status`, register `setup`), `tests/test_worker_v1.py`
⚬ EDITED (additive): `tests/fakes/*` only if a fake lacks a method.
Approach: loop: poll → for each new ticket: trace span → worktree → proxy route → OpenCode attempt → verify → retry once on failure with the output → publish draft → mark ready. Reviews → kickback (new attempt with the note) or accept (record, close issue, clean worktree). Ctrl-C finishes the current step and exits cleanly.
Verify:
⚬ Full loop against fakes: label → draft PR → "request changes" → second attempt → approve + merge → ticket done, attempts and kickback rows correct, one trace with the expected spans.
⚬ Two failed checks end with the issue flagged, no third attempt (invariant 7e); non-vacuous: raise the cap, test fails.
Live (user): on a toy repo, label 3 small issues, review the PRs, kick one back once. Check `fman status` and the trace.
Deviations:

### Milestone 2 — Tiers, escalation, AI review, caps, metrics

#15 — Router v1: rules, tiers, `tier:N` override [ ]
Wave: 5 · Agent: A · Added: 2026-11-23 · Source: spec
Ask: Start each ticket on the cheapest tier likely to work.
DECISIONS (spec, 2026-11-23): small/normal/large classification by a small local model → tier 1/2/3; `tier:N` label overrides and is recorded in `route_reason`.
Files:
⚬ OWNED: `src/foreman_inator/routing/rules.py`, `src/foreman_inator/routing/__init__.py`, `src/foreman_inator/prompts/router-v1.md`, `tests/test_routing_rules.py`
Approach: `route(ticket) -> Route(tier, reason)`; classification call goes through the proxy (traced as `route`). Malformed model output → tier 2 with reason "router fallback".
Verify:
⚬ Fixed classifier outputs map to the right tiers; override wins; fallback works.
⚬ Non-vacuous: ignore the override, its test fails.
Deviations:

#16 — Escalation state machine [ ]
Wave: 5 · Agent: B · Added: 2026-11-23 · Source: spec
Ask: The exact escalation rules in SPEC, with a hard stop.
DECISIONS (spec, 2026-11-23): checks fail → retry once same tier, then up; kicked back once → same tier with note; twice or tagged wrong → up; two AI kickbacks → writer up; tier 3 fails → flag the user.
Files:
⚬ OWNED: `src/foreman_inator/routing/escalation.py`, `tests/test_escalation.py`
Approach: pure function `next_step(ticket_history) -> Retry(tier) | Escalate(tier) | HandToUser(reason)`. No I/O.
Verify:
⚬ One test per SPEC rule, plus a property test: every history ends in HandToUser within a bounded number of attempts (invariant 7e).
⚬ Non-vacuous: remove the tier 3 stop, the property test fails.
Deviations:

#17 — AI reviewer [ ]
Wave: 5 · Agent: C · Added: 2026-11-23 · Source: user ("a tier 3 reviews the change … only after tier 3 reviews it do we ask the user")
Ask: A stronger model reviews every change before the user sees it.
DECISIONS (user/spec, 2026-11-23): five checks in order (tests, changed-line coverage, revert check, run when runnable, read diff vs issue); different model family; sees only issue + diff; posts a PR review from the bot; `review:skip` label skips it.
Files:
⚬ OWNED: `src/foreman_inator/review/reviewer.py`, `src/foreman_inator/review/checks.py`, `src/foreman_inator/review/__init__.py`, `src/foreman_inator/prompts/reviewer-v1.md`, `tests/test_reviewer.py`
Approach: checks 1–4 are code, not model calls: rerun verify; changed-line coverage via `coverage.py` (Python) / the repo's coverage output when present, else "unknown"; revert check = revert non-test files in a scratch worktree, run the new tests, expect failure; run = a CLI entry point or `npm run start` smoke when detectable, else `not_runnable`. Check 5 is the model call with `reviewer-v1.md`, structured verdict (approve / request_changes + tag + note). Family check: refuse a reviewer whose family equals the writer's.
Verify:
⚬ Fixture PRs: a good change approved; a change whose tests pass without it flagged `tests_nonvacuous=false`; uncovered new function flagged.
⚬ Reviewer input contains no writer transcript (invariant 7f); non-vacuous: pass it, test fails.
Live (user): 5 real tickets with review on; note whether its kickbacks were fair.
Deviations:

#18 — Spend caps [ ]
Wave: 5 · Agent: D · Added: 2026-11-23 · Source: spec
Ask: Cloud spend can't run away.
DECISIONS (spec, 2026-11-23): daily and monthly caps; at the cap, tier 3 waits for the user's approval; experiments have their own monthly budget.
Files:
⚬ OWNED: `src/foreman_inator/spend.py`, `tests/test_spend.py`
Approach: `Spend(store, config)`: `allow(cost_estimate, bucket) -> bool`, `record(cost, bucket)`. Buckets: `live`, `experiments`. The proxy's no-op hook (#9) is replaced at wave close by the runner. Over cap → issue comment asking for approval (`fman approve-spend <ticket>`).
Verify:
⚬ Crossing the daily cap blocks; next day resets; monthly cap holds across days.
⚬ Non-vacuous: compare with `<` instead of `<=`, boundary test fails.
Deviations:

#19 — File locks for parallel tickets [ ]
Wave: 5 · Agent: E · Added: 2026-11-23 · Source: spec (waves rule, enforced)
Ask: Two tickets never edit the same file at once.
DECISIONS (assistant, 2026-11-23): owned files are predicted from the issue (paths mentioned + router guess) and widened to the attempt's actual diff; a ticket whose diff touches a locked file is paused, not failed.
Files:
⚬ OWNED: `src/foreman_inator/locks.py`, `tests/test_locks.py`
Approach: per-repo lock table in SQLite; `acquire(ticket, files)`, `conflicts(ticket, diff_files)`, `release(ticket)`.
Verify:
⚬ Overlapping tickets: second waits; released on merge/drop.
⚬ Non-vacuous: skip the diff widening, the conflict test fails.
Deviations:

#20 — Metrics queries, local page, summary comment [ ]
Wave: 5 · Agent: F · Added: 2026-11-23 · Source: spec
Ask: See which model handles which work and what each accepted change costs.
DECISIONS (spec, 2026-11-23): the metrics table in SPEC (6 core + 3 review metrics + 2 trend lines), filterable by model, repo, date; served on localhost; one-line summary comment on close.
Files:
⚬ OWNED: `src/foreman_inator/metrics/queries.py`, `src/foreman_inator/metrics/web.py`, `src/foreman_inator/metrics/__init__.py`, `src/foreman_inator/pr/summary.py`, `tests/test_metrics.py`
Approach: one SQL query per metric with the exact definition in SPEC; `explore` attempts excluded from failure counts. Page: plain server-rendered HTML + a small chart library, bound to 127.0.0.1.
Verify:
⚬ Hand-built fixture DB with known answers for every metric.
⚬ Non-vacuous: count `explore` failures, the first-try test fails.
Deviations:

#21 — Worker loop v2: tiers, escalation, review, caps, locks (milestone 2) [ ]
Wave: 6 · Agent: A (alone) · Added: 2026-11-23 · Source: spec
Ask: The full flow in SPEC "Ticket lifecycle".
Files:
⚬ OWNED: `src/foreman_inator/worker.py`, `src/foreman_inator/cli.py` (`metrics`, `approve-spend`), `src/foreman_inator/pr/publisher.py` (ready-for-review after review approval, bot review posting), `tests/test_worker_v2.py`
Approach: replace v1's straight line with: route (#15) → write → verify → escalation (#16) → AI review (#17) → PR ready only on approval or handover → your review → escalation again. Wire spend (#18) into the proxy, locks (#19), metrics page + summary (#20). Remove the v1 "ready immediately" flag.
Verify:
⚬ Scenario tests against fakes for every row of the lifecycle diagram, including two AI kickbacks → writer escalates, tier 3 failure → handed to user.
⚬ PR is never marked ready before an approve verdict or handover (invariant 7a); non-vacuous: mark ready early, test fails.
Live (user): one week on 1–2 real repos. Then read the metrics page: share solved locally, cost per accepted ticket.
Deviations:

### Milestone 3 — bot-inator integration (optional)

#22 — Add a foreman-inator item to bot-inator's todo.md [ ]
Wave: 7 · Agent: A · Added: 2026-11-23 · Source: spec
Ask: Open issues from bot-inator chat or ideas, "PR ready" cards, approve or request changes from the iPhone remote.
DECISIONS (spec, 2026-11-23): bot-inator's consent rules apply (an idea becomes an issue only after its idea card); it talks to GitHub directly, not to the worker.
Files:
⚬ OWNED: none in this repo; the work is an item in `~/dev/bot-inator/todo.md`.
Approach: write the bot-inator item (its template, its next ID) when milestone 2 is live. Nothing here depends on it.
Verify: the bot-inator item exists and links to this SPEC.
Deviations:

### Milestone 4 — Learning router, experiments, trace analysis

#23 — Nearest-neighbor start rule and 10% exploration [ ]
Wave: 8 · Agent: A · Added: 2026-11-23 · Source: spec
Ask: The router learns from accepted tickets.
DECISIONS (spec, 2026-11-23): after ~50 accepted tickets, embed the request, take the 10 most similar finished tickets, start at the cheapest tier accepted for most; ~10% start one tier lower, marked `explore`.
Files:
⚬ OWNED: `src/foreman_inator/routing/knn.py`, `src/foreman_inator/routing/explore.py`, `tests/test_knn.py`
Approach: local embedding model via Ollama through the proxy; vectors in SQLite (`sqlite-vec` if available, else numpy brute force — fine at this scale). `route_reason` lists matched ticket IDs. Falls back to #15 below the threshold.
Verify:
⚬ Synthetic history where small tickets were always accepted on tier 1 → new similar ticket routes to tier 1.
⚬ Exploration rate is ~10% over 1,000 seeded draws and never goes below tier 1.
Deviations:

#24 — Variants and prompt versioning [ ]
Wave: 8 · Agent: B · Added: 2026-11-23 · Source: spec
Ask: Every attempt and review knows exactly which model + prompt made it.
DECISIONS (spec, 2026-11-23): variant = model + prompt version; one champion per role; prompts are versioned files.
Files:
⚬ OWNED: `src/foreman_inator/experiments/variants.py`, `src/foreman_inator/experiments/__init__.py`, `src/foreman_inator/prompts/__init__.py`, `tests/test_variants.py`
Approach: variant id = `<model>/<prompt-name>@<prompt_hash[:8]>` using `fde_kit.prompts.prompt_hash`; `champion(role)`, `set_champion(role, variant)` with a history row.
Verify:
⚬ Editing a prompt file creates a new variant id; the old one stays queryable.
Deviations:

#25 — Replay sets: writer replay and reviewer replay [ ]
Wave: 8 · Agent: C · Added: 2026-11-23 · Source: user ("test the prompts / models … for both the reviewers and the agents")
Ask: Compare a challenger to the champion on past tickets, overnight, with none of the user's time.
DECISIONS (spec, 2026-11-23): writer replay scores on passing the merged PR's tests, cost, time; reviewer replay scores catches vs false kickbacks against the user's merges and kickbacks; rebuild from the latest ~100 merges monthly; tier 3 replays use the experiments budget.
Files:
⚬ OWNED: `src/foreman_inator/experiments/replay_sets.py`, `src/foreman_inator/experiments/replay_runner.py`, `tests/test_replay_sets.py`
Approach: build cases from merged tickets (issue, base commit, merged PR's test files); run each through the normal attempt path in throwaway worktrees with no GitHub writes; write results to `experiments`. Consider fde-kit's eval runner for scoring and reports.
Verify:
⚬ A fake champion/challenger pair with known outcomes produces the expected comparison.
⚬ Replays never call the GitHub write API; non-vacuous: allow a write, the fake GitHub records it and the test fails.
Deviations:

#26 — Shadow reviewer [ ]
Wave: 8 · Agent: D · Added: 2026-11-23 · Source: spec
Ask: Test a reviewer on live PRs without changing anything the user sees.
DECISIONS (spec, 2026-11-23): challenger reviews every live PR next to the champion; only the champion's verdict counts; both scored against the user's eventual merge or kickback.
Files:
⚬ OWNED: `src/foreman_inator/experiments/shadow.py`, `tests/test_shadow.py`
Approach: `shadow_review(attempt)` runs the challenger with the same inputs, stores its review row with `variant_id`, posts nothing. Scoring job joins reviews to final outcomes.
Verify:
⚬ Shadow verdicts are stored and never posted; non-vacuous: post them, fake GitHub sees a second review, test fails.
Deviations:

#27 — Failure-step labeling [ ]
Wave: 8 · Agent: E · Added: 2026-11-23 · Source: user ("see where model made a miss")
Ask: For every kickback, which step went wrong.
DECISIONS (spec, 2026-11-23): labels: misread_issue, missed_file, wrong_edit, ignored_failure, weak_tests, reviewer_miss.
Files:
⚬ OWNED: `src/foreman_inator/tracing/failure_label.py`, `src/foreman_inator/prompts/failure-label-v1.md`, `tests/test_failure_label.py`
Approach: build a compact trace summary (files read, edits, test results, review checks) from the recording; a local model picks one label + the step index; `reviewer_miss` is set by rule (user kicked back a reviewer-approved PR), not by the model.
Verify:
⚬ Hand-made recordings for each label get the expected label (allow the model call to be faked).
⚬ `reviewer_miss` is rule-based; non-vacuous: route it through the model, the rule test fails.
Deviations:

#28 — Replay viewer and recorded tickets as worker regression tests [ ]
Wave: 8 · Agent: F · Added: 2026-11-23 · Source: user ("add replays to this like the fde kit")
Ask: Step through any run; check worker changes against real past runs without model calls.
DECISIONS (spec, 2026-11-23): replay uses no model calls; regression tests run recorded tickets through new router/escalation/review logic.
Files:
⚬ OWNED: `src/foreman_inator/tracing/replay.py`, `src/foreman_inator/metrics/trace_view.py`, `tests/recordings/` (a few anonymized recordings), `tests/test_replay.py`
Approach: replay = fde-kit-style exact playback of `llm` and `tool` rows into the trace view (a timeline per attempt). Regression = feed recorded outcomes into #15/#16/#17-check logic and diff the decisions against what was recorded. Known limit: OpenCode's own loop can't be re-executed from a recording; fork-from-failure (#36) restarts a fresh session at the failure commit instead.
Verify:
⚬ Replaying a recording reproduces its span tree exactly.
⚬ A deliberate escalation-rule change shows up as a decision diff on the recorded set; non-vacuous: no-op the diff, test fails.
Deviations:

#29 — Wiring for milestone 4 [ ]
Wave: 9 · Agent: A (alone) · Added: 2026-11-23 · Source: spec
Ask: Turn on the learning router, experiments and trace analysis in the running worker.
Files:
⚬ OWNED: `src/foreman_inator/worker.py`, `src/foreman_inator/cli.py` (`experiment run|status|promote`, `replay <ticket>`), `src/foreman_inator/metrics/web.py` (routes for trace view, failure steps per variant, experiments), `tests/test_worker_v3.py`
Approach: router picks #23 over #15 past the threshold; every kickback triggers #27; shadow reviewer (#26) runs when a challenger exists; `fman experiment run` drives #25 overnight via launchd calendar interval; promotion follows SPEC's rule (wins on replay and holds live).
Verify:
⚬ Scenario test: challenger wins replay, holds in shadow, `promote` switches the champion; a challenger that loses live is rejected.
Live (user): run one overnight replay of a prompt change; read the comparison.
Deviations:

### Release

#30 — `fman export --redact` [ ]
Wave: 10 · Agent: A · Added: 2026-11-23 · Source: spec
Ask: Share a trace in a bug report without leaking code.
Files:
⚬ OWNED: `src/foreman_inator/export.py`, `tests/test_export.py`
Approach: export a ticket's spans and recording with code, file contents, prompts and tool output replaced by hashes + lengths; keep structure, timings, models, verdicts.
Verify:
⚬ A fixture recording with secret-looking strings exports with none of them present (grep test); non-vacuous: skip tool output redaction, test fails.
Deviations:

#31 — Packaging, release workflow, name check [ ]
Wave: 10 · Agent: B · Added: 2026-11-23 · Source: user ("will this app be distributed on github?")
Ask: `uv tool install foreman-inator` works for anyone.
DECISIONS (user, 2026-11-23): name foreman-inator, command alias `fman`, MIT.
Files:
⚬ OWNED: `pyproject.toml` (metadata, classifiers, entry points), `.github/workflows/release.yml`, `docs/releasing.md`
Approach: confirm `foreman-inator` is free on PyPI and as a GitHub App name before the first publish (record the date); release on tag via PyPI trusted publishing; GitHub release with changelog.
Verify:
⚬ `uv build` produces a wheel; installing it in a clean venv gives working `foreman-inator` and `fman`.
Live (user): first release to TestPyPI, then PyPI.
Deviations:

#32 — README, docs and demo [ ]
Wave: 10 · Agent: C · Added: 2026-11-23 · Source: spec
Ask: A stranger understands it in 30 seconds and can set it up in 10 minutes.
Files:
⚬ OWNED: `README.md`, `docs/setup.md`, `docs/how-it-works.md`, `docs/demo.gif`
Approach: README top: one-sentence pitch, the demo gif (label → draft PR → AI review → merge), install in 3 commands, "personal project, no promised support". How it works: lifecycle + architecture diagrams from SPEC. Note the comparison: what it adds over "assign an issue to an agent" tools (local models, learning router, per-model metrics, experiments).
Verify:
⚬ Follow `docs/setup.md` on a second Mac account from scratch; every step works as written.
Deviations:

### Unscheduled (SPEC milestone 5)

#33 — Webhook relay for instant reactions [ ] · Added: 2026-11-23 · Source: spec
#34 — Traffic split and head-to-head (`ab:pair`) writer experiments [ ] · Added: 2026-11-23 · Source: spec
#35 — Seeded-bug reviewer test cases [ ] · Added: 2026-11-23 · Source: spec
#36 — Fork from failure: restart a recorded attempt at its failure step with a new prompt or model [ ] · Added: 2026-11-23 · Source: user/spec
#37 — Classifier router, then a LoRA router only if it beats the classifier on held-out tickets [ ] · Added: 2026-11-23 · Source: spec
#38 — Homebrew tap [ ] · Added: 2026-11-23 · Source: spec
#39 — Publish the App for others (each user still runs their own worker) [ ] · Added: 2026-11-23 · Source: spec
