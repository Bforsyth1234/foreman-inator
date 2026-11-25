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

Next ID: #77
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
| — | #33–#39 | unscheduled (SPEC milestone 5) | open |
| — | #45–#50 | unscheduled (gap review, 2026-11-23) | open |
| — | #51 | unscheduled (user request, 2026-11-23); proposed as its own wave after 6b | open |
| — | #52 | unscheduled (user request, 2026-11-23); after 6b, can share a wave with #51 (disjoint files) | open |
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

### Release

### Unscheduled (SPEC milestone 5)

#33 — Webhook relay for instant reactions [ ] · Added: 2026-11-23 · Source: spec
#34 — Traffic split and head-to-head (`ab:pair`) writer experiments [ ] · Added: 2026-11-23 · Source: spec
#35 — Seeded-bug reviewer test cases [ ] · Added: 2026-11-23 · Source: spec
#36 — Fork from failure: restart a recorded attempt at its failure step with a new prompt or model [ ] · Added: 2026-11-23 · Source: user/spec
#37 — Classifier router, then a LoRA router only if it beats the classifier on held-out tickets [ ] · Added: 2026-11-23 · Source: spec
#38 — Homebrew tap [ ] · Added: 2026-11-23 · Source: spec
#39 — Publish the App for others (each user still runs their own worker) [ ] · Added: 2026-11-23 · Source: spec

### Unscheduled (gap review, 2026-11-23)

Found in the same review as #40–#44 (user: "Yes, add them"). Each has
its evidence and a first idea; none has decisions or owned files yet, so
write the full item from the template before scheduling one.

#45 — Install dependencies in a fresh worktree before verify [~] live · fixed 2026-11-24, wave 11, see DONE.md · Added: 2026-11-23 · Source: assistant (gap review)
Why: no install step exists anywhere in the code (no `npm ci`, `uv sync` or `swift package resolve`), so a new worktree has no `node_modules` or venv and verify can fail for reasons unrelated to the change.
Idea: detect an install command next to the verify command, save it per repo like `fman repo set-verify`, run it once per worktree. Needs network, so decide it together with #40's `sandbox_network`.
Live evidence (2026-11-23, first live run, `Bforsyth1234/fman-playground`, ticket 2, attempt 11): with the sandbox on, verify ran `uv run pytest` and failed with "Network connectivity is disabled, but the requested data wasn't found in the cache" (`~/.foreman-inator/traces/2/attempt-11-verify.log`). The sandbox moves `UV_CACHE_DIR` into an empty scratch dir and the repo had no `sandbox_network = "allow"`, so nothing could be installed. The change itself was correct. The writer hit the same wall in its own bash tool and spent most of 18.6 minutes and 585k input tokens trying to get pytest to run. This now blocks every repo with dependencies, so it is no longer only a gap-review idea. Options seen: a read-only shared package cache inside the sandbox, an install step that runs with network before the sandboxed verify, or telling the user to set `sandbox_network` when verify fails this way.

#46 — Metrics: attempt minutes and review count next to dollars [ ] · Added: 2026-11-23 · Source: assistant (gap review; user asked "why wouldnt local llms be priced at 0")
Why: local models cost $0 per token (correct), so "cost per accepted ticket" shows a local model that took 3 attempts and 2 reviews as free.
Idea: metrics page only. `attempts.seconds` is already recorded; add total attempt minutes and the user's review count per accepted ticket, per model.

#47 — Post-merge quality signal [ ] · Added: 2026-11-23 · Source: assistant (gap review)
Why: a merge counts as success forever. A merged change that is reverted, or fixed by a follow-up ticket a few days later, still counts as accepted for its model and still trains the router.
Idea: detect a revert of the merge commit, and a later ticket touching the same files within N days; record it against the accepted attempt and show "accepted, then reverted" per model.

#48 — Issue comments as context for the attempt [ ] · Added: 2026-11-23 · Source: assistant (gap review)
Why: `list_issue_comments` is called only by `pr/summary.py`. Details the user adds in a comment after filing never reach the writer (an edit to the issue body does, through `IssueEdited`).
Idea: comments from allowed users go into the prompt after the issue body, defused and clipped like it. Overlaps #44's Q&A block; build after #44.

#49 — Per-repo "local only" setting [ ] · Added: 2026-11-23 · Source: assistant (gap review)
Why: config has one global model list and no per-repo settings, so any repo's code can go to a cloud tier on escalation or AI review (invariant 7c limits cloud by tier and spend, not by repo).
Idea: `cloud = false` for a repo: escalation stops at tier 2 and flags the user, and AI review uses a local reviewer or is skipped with a note in the PR.

#50 — Split a large issue into smaller tickets [ ] · Added: 2026-11-23 · Source: assistant (gap review)
Why: an issue classified `large` goes straight to tier 3 as one attempt. Nothing breaks it into pieces a local model could do, which is the work the v1 goal (replace Max usage with local models) most needs.
Idea: for `large`, a tier 3 model proposes sub-issues in a comment; the user approves by labeling them; each runs as a normal ticket. No sub-issue is created without the user (same consent idea as invariant 7b).

### Unscheduled (user request, 2026-11-23)

#51 — Repo profiles: per-repo `writer` and `review` settings [ ]
Wave: _ (proposed: its own wave after 6b) · Agent: _ · Added: 2026-11-23 · Source: user
Ask: "a setting per repo that prioritizes speed over cost saving and correctness over cost … personal project can take a lot of time and have bugs but cost little", and "sometimes i build prototypes and i want speed to be most important minor bugs dont matter".
Why: the retry and kickback limits are module constants (`routing/escalation.py:75-79`), the router's pick is used as is, and AI review is the same for every repo. `repo_settings` holds only the verify command.
DECISIONS (assistant, 2026-11-23; the user approved the two settings, `review = off` and the four presets, and can overturn the rest):
1. Two settings per repo, not three sliders: cost is what the other two are bought with. `writer` = `thrifty` | `normal` | `fast`. `review` = `off` | `light` | `normal` | `strict`. No setting means `normal` + `normal`, which must behave exactly as the code does today.
2. `writer`: `thrifty` allows 2 retries on a tier before moving up (3 check attempts) and makes a cloud writer attempt wait for approval. `fast` starts one tier above the router's pick (capped at the top tier) and moves up on the first failed check. User-kickback rules are the same in all three.
3. `review`: `off` takes the existing `review:skip` path (checks pass → PR marked ready). `light` runs review checks 1–3 (tests, coverage, revert) and gives its verdict from hard findings alone: no run-app, no model call. `strict` ignores `review:skip` and moves the writer up on the first AI kickback.
4. Presets: `prototype` = fast + off · `personal` = thrifty + light · `default` = normal + normal · `production` = fast + strict.
5. Stored in `repo_settings` (`writer_mode`, `review_mode`), set by `fman repo set-profile <repo> <preset>` or `--writer` / `--review`, shown by `fman status` and in the PR description. Same home as `fman repo set-verify`; #49 (`cloud = false`) and #45 (install command) should use it too.
6. A `tier:N` label is never shifted. The shift is applied after the router's guess; `route_reason` records both ("profile fast: tier 1 → 2"), and every attempt records `writer_mode` and `review_mode`. #23 must learn from the router's own pick, not the shifted start, and #20's metrics can split by mode.
7. Exploration rate per mode (thrifty 25%, normal 10%, fast 0%) is defined here and read by #23 (`routing/explore.py`, wave 8); nothing explores until #23 is built.
8. No per-repo budget in this item. The global daily and monthly caps apply to every profile. `thrifty`'s approval reuses the refusal and `approve` path in `spend.py` with its own reason.
9. Invariants: every mode keeps a finite attempt bound (7e); `light` and `off` run no model reviewer, so 7f is not in play; `off` marks the PR ready with no review, as `review:skip` already does.
SPEC change needed: "Routing and escalation" gains "Repo profiles" and `attempts` gains `writer_mode, review_mode`. Both are already in SPEC.md (2026-11-23); the Claude doc, the source of truth, still needs them.
Files:
⚬ OWNED: `src/foreman_inator/routing/profile.py`, `tests/test_profile.py`, one migration with the next free number when this is built (002 and 003 are claimed by #41 and #43)
⚬ EDITED (additive, earlier waves): `src/foreman_inator/routing/escalation.py` (limits passed in; today's constants stay as the defaults), `src/foreman_inator/review/reviewer.py` (a `depth` argument on `review_change`), `src/foreman_inator/db/store.py` (`RepoSettings` fields, `set_profile`, the two attempt columns), `src/foreman_inator/spend.py` (the approval reason)
⚬ Do NOT touch: `cli.py`, `worker.py`, `pr/publisher.py` (rule 9: the runner wires them at close).
Scheduling: `store.py` and the migrations belong to wave 6b slot B, and the wiring needs #21's loop, so this runs after 6b. Because `normal` is the default and equals today's behaviour, #21 needs no change to be built first.
Approach: `profile.py` is pure: `Profile(writer, review)`, `PRESETS`, `limits_for(profile) -> Limits` (check attempts, user and AI kickback limits, start shift, explore rate, cloud approval), `shift_start(tier, ladder, limits)`, `review_plan(profile)` (off | hard checks only | full; whether the skip label counts). `make_policy(ladder, limits=...)`, `next_step` and `attempt_bound` take `Limits`. Runner at close: `fman repo set-profile`, the profile line in `fman status` and the PR description, and three call sites in `worker.py` (route, policy, review). Fallback if `light` can't reuse the reviewer cleanly: `light` calls the check functions in `review/checks.py` directly and builds the verdict in `profile.py`'s caller.
Verify:
⚬ `default` gives the same decision as today on every existing escalation test, and `attempt_bound(4)` is unchanged.
⚬ `fast`: tier 1 pick starts on tier 2, tier 3 stays 3, a `tier:N` label is not shifted, the first failed check escalates.
⚬ `thrifty`: three failed checks before moving up; a cloud writer attempt is refused until approved.
⚬ For every `writer` × `review` pair, any history ends with the user within `attempt_bound` (invariant 7e).
⚬ `off`: no review row, no model call, PR ready. `light`: the fake reviewer model is called 0 times and a hard finding still gives `request_changes`. `strict`: `review:skip` is ignored and one AI kickback escalates.
⚬ `set_profile` round-trips through the store; an unknown preset is rejected with the valid names; an attempt row records both modes.
⚬ pytest + ruff gate (rule 8).
⚬ Non-vacuous: make `limits_for(fast)` return the default limits, the `fast` tests fail; let `light` call the model, the zero-calls test fails.
Live (user): set a toy repo to `prototype` and label 2 issues: both PRs arrive ready with no bot review. Set it to `production`, label one issue with `review:skip`: the review still runs.
Deviations:

#52 — Prompt caching: native Anthropic route in the proxy, cached-token accounting for Claude and GLM [ ]
Wave: _ (after 6b; can share a wave with #51) · Agent: _ · Added: 2026-11-23 · Source: user
Ask: "how could we add prompt caching?", then "add it as a todo item. Also look into z.ai caching capabilities".
Why: the driver gives OpenCode one `@ai-sdk/openai-compatible` provider (`runner/opencode.py` `build_config`) and the proxy forwards Claude calls to Anthropic's OpenAI-compatible endpoint, which has no prompt caching (#9's rule 12 note). A writer attempt re-sends its whole history on every turn at full input price, so a few cloud attempts can use up the $5 daily cap. `proxy/pricing.py` says itself that cache prices are not used, and the proxy reads only `prompt_tokens` and `completion_tokens`, so GLM's cache discount is not counted either.
DECISIONS (assistant, 2026-11-23; the user chose the passthrough approach by asking for this item and can overturn the rest):
1. Passthrough, not translation. The proxy gains a `messages` route under the same prefixes (`/a/<attempt_id>/v1/messages`, `/x/...`, `/c/...`) that forwards the request body unchanged to `<base_url>/messages` and streams the response bytes back unchanged. No OpenAI-to-Anthropic conversion is written.
2. OpenCode places the cache markers; the proxy adds none. Checked in the installed OpenCode 1.18.7 binary (2026-11-23): it marks the first two system messages and the last two messages with `cacheControl: {type: "ephemeral"}` when the provider package is `@ai-sdk/anthropic` or the model id contains `claude`. If the live check shows no cache reads, the fallback is the proxy adding a top-level `cache_control: {"type": "ephemeral"}` to the forwarded body (the one allowed body change).
3. `ModelEndpoint` gains `wire = "openai" | "anthropic"`, default `"openai"`. The default Claude endpoint is `"anthropic"`. `build_config` uses `@ai-sdk/anthropic` for an `"anthropic"` endpoint and stays as it is for the rest.
4. The proxy sets `x-api-key` upstream and forwards the client's `anthropic-version` and `anthropic-beta` headers; every other client auth header is dropped. The fail-closed rules of #9 apply to the new route unchanged (unknown model 404, no key 403, https only, key never in spans, recordings, logs or error bodies; invariant 7c).
5. Usage: read `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` (and the `cache_creation` split by TTL) from `message_start` and `message_delta` when streaming, and from the body when not. Anthropic's `input_tokens` excludes cached tokens, so `tokens_in` stored on the attempt is the sum of all three input kinds. Cache read and write counts go on the `llm.call` span and on `CallUsage`. No schema change.
6. Pricing: `Price` gains a cache read rate and cache write multipliers. Claude writes are 1.25× input (5-minute) and 2× (1-hour). Claude reads per million: Sonnet 5.5 $0.10, Opus 5.5 $0.20, Fable 5.2 $0.20 (from the Claude API reference dated 2026-11-03; confirm on the pricing page, rule 12). A model with no cache rate is priced as today.
7. GLM needs no request change: Z.ai caching is implicit. The proxy reads `usage.prompt_tokens_details.cached_tokens` on the existing `chat/completions` path and prices those tokens at the cached rate (GLM-5.3 and GLM-5.2 $0.26, GLM-5.3-Flash $0.03 per million). `prompt_tokens` is treated as including the cached tokens: the docs do not say so outright, but compute the hit rate as `cached_tokens / prompt_tokens`. Cached-input storage is "Limited-time Free" and is priced at 0 with the date in a comment. GLM stays priced at the pay-as-you-go list price on the Coding Plan (#9's deviation).
8. The spend preflight estimate stays at the full input price (it must not under-reserve); only the settled actual cost uses cache rates.
9. The reviewer and the router keep the `chat/completions` path. Each makes one call, so caching gains nothing there.
10. Recordings: the new route's tee builds the same recorded view (text, tool calls, usage, finish reason) as the OpenAI tee, so fde-kit replay reads both.
SPEC change needed: Tracing "Capture" says "a thin OpenAI-compatible proxy"; it becomes OpenAI-compatible plus an Anthropic Messages route for Claude models.
Rule 12 (read 2026-11-23):
⚬ Z.ai: docs.z.ai/guides/capabilities/cache (implicit, no request field; match is "identical or highly similar" content, exact-prefix not stated; no minimum size or TTL given), docs.z.ai/guides/overview/pricing (the rates in decision 7), docs.z.ai/devpack/overview and /devpack/faq (Coding Plan credits = (input × 6.9 + cached input × 1.7 + output × 24) / 10,000 for GLM-5.3; whether caching applies on `api/coding/paas/v4` is not stated). The cache guide says hits are "usually 50% of standard price" while the pricing table gives about 19%; the table is used.
⚬ OpenCode: 1.18.7 binary inspected, see decision 2. opencode.ai/docs/providers now shows a `package` / `settings` provider schema next to the `npm` / `options` one this driver writes, and `setup/deps.py` prints an `opencode-v2` install command: check which schema the installed version takes before changing `build_config`.
⚬ Still to read by the building agent: platform.claude.com prompt caching and pricing pages, the Messages streaming event list, and where `@ai-sdk/anthropic` sends requests relative to `baseURL` (expected `{baseURL}/messages`, not confirmed).
Files:
⚬ OWNED: `tests/test_proxy_anthropic.py`, `tests/fakes/anthropic.py` (a scriptable Messages-format upstream)
⚬ EDITED (additive, earlier waves): `src/foreman_inator/proxy/server.py` (route, headers, Anthropic tee, `cached_tokens` on the OpenAI tee), `src/foreman_inator/proxy/pricing.py` (cache rates), `src/foreman_inator/runner/opencode.py` (`build_config`), `src/foreman_inator/config.py` (`wire`, the default Claude endpoint), `tests/test_proxy.py`, `tests/test_config.py`
⚬ Do NOT touch: `spend.py`, `db/store.py`, the migrations, `review/reviewer.py`, `worker.py`.
Scheduling: `runner/opencode.py` and `config.py` are being edited by #40 in wave 6b, so this runs after 6b. Its files are disjoint from #51's.
Approach: add `"messages"` to the upstream paths behind a wire check (a `messages` call to an `"openai"` endpoint, or a `chat/completions` writer call that should have been native, is still served; only the path decides the upstream format). Factor the tee into one interface with two parsers. `Price.cost(tokens_in, tokens_out)` keeps its signature and gains optional cache arguments. Fallback if `@ai-sdk/anthropic` cannot be pointed at the proxy: keep the OpenAI-compatible provider and build the translation for requests only as far as the writer needs (text, tools, tool results, streaming), which is a larger item and should be re-planned first.
Verify:
⚬ A `messages` request through the proxy reaches the fake upstream byte-for-byte with `x-api-key` set, and the streamed response reaches the client byte-for-byte.
⚬ A streamed response with 1,000 uncached, 9,000 cache-read and 2,000 cache-write input tokens stores `tokens_in` = 12,000 and a cost equal to the hand-computed figure for Sonnet 5.5, Opus 5.5 and Fable 5.2.
⚬ A GLM response with `prompt_tokens` 1,200 and `cached_tokens` 800 costs 400 × input + 800 × cached rate; with no `prompt_tokens_details` the cost is unchanged from today.
⚬ The `llm.call` span and the recording row carry the cache counts; the API key appears in neither.
⚬ A keyless Claude endpoint is refused with 403 on the new route and nothing is sent upstream; a spend-cap refusal returns 429 on the new route.
⚬ `build_config` gives `@ai-sdk/anthropic` for a `wire = "anthropic"` endpoint and is unchanged for local and GLM endpoints (snapshot).
⚬ pytest + ruff gate (rule 8).
⚬ Non-vacuous: price cache reads at the full input rate and the cost test fails; drop `cache_read_input_tokens` from the sum and the `tokens_in` test fails; send `Authorization: Bearer` instead of `x-api-key` and the header test fails.
Live (user): one real Claude writer attempt on a toy repo: from the second model call on, `cache_read_input_tokens` is above zero in the trace, and the attempt's cost is below the same token count at the uncached rate. One real GLM attempt on the Coding Plan endpoint: note whether `cached_tokens` is ever above zero, since the docs do not say caching applies there.
Deviations:

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

#57 — Global cap on concurrent local-model inference [ ] · Added: 2026-11-23 · Source: user
Why: `max_parallel_per_repo` (default 1, "one local GPU") is counted per repo, and nothing limits local work across repos. Three watched repos can send three tickets to Ollama at once, each loading `qwen3.8:27b` or `inkling-2-mini`: models get evicted and reloaded, the machine swaps, and attempts slow down or time out. Those runs are then recorded as model failures in the metrics and the kNN router's data.
Idea: a `max_local_inference` config setting (default 1). The proxy holds one shared semaphore for requests to `local` tier endpoints; cloud tiers are not limited. Time spent waiting for a slot is recorded on the `llm.call` span and not counted against the attempt's timeout. Estimate: about 2 hours with tests.

#58 — Poll Ollama for loaded models [x] · done 2026-11-25, wave 13, see DONE.md · Added: 2026-11-23 · Source: user

#59 — Record memory state on each attempt [ ] · Added: 2026-11-23 · Source: user
Why: a slow or failed local attempt can't be told apart from a starved machine. Without this, resource contention looks like model quality in the metrics and replay sets.
Idea: at attempt start (and optionally at end), store the loaded models from #58 plus system memory used and memory pressure (`psutil`, or `vm_stat` / `memory_pressure` on macOS) on the attempt's trace. Decide whether it belongs on a span attribute (no schema change) or in an attempt column. Estimate: about 2 hours.

#60 — Resource panel on the metrics page [ ] · Added: 2026-11-23 · Source: user
Why: makes #57–#59 visible: current loaded models and their memory, system memory used, and local inference slots in use vs. the cap.
Idea: a panel in `metrics/web.py` fed by #58's poller and the #57 semaphore; per-attempt memory from #59 shown on the attempt view. Depends on #57–#59. Estimate: about 2 hours.

### Unscheduled (agent instructions and global rules, 2026-11-23)

Asked by the user: "what about agent md files? do we need to tell the agents to read those? Should we add the ability to add them? maybe the users want some global rules etc?", then "ok add these as todos to todo.md should we add the ability to test these rules?". Today `tickets/prompt.py` reads the first of `AGENTS.md` / `CLAUDE.md` in the worktree (`CONVENTIONS_FILES`, 6,000-char budget) into the writer prompt's `conventions` section. Nothing else sees it, and there is no user-level rules file.

#61 — Global rules file for every repo [ ] · Added: 2026-11-23 · Source: user
Why: users want rules that apply to every repo the worker touches (commit style, "never add dependencies", language preferences). There is nowhere to put them: OpenCode's own `~/.config/opencode/AGENTS.md` is never read because `XDG_CONFIG_HOME` points at foreman-inator's dirs, on purpose (`runner/opencode.py`).
Idea: `~/.foreman-inator/AGENTS.md` (mode 600, invariant d) plus an optional per-repo `rules_file` in config. Shown in the prompt before the repo's own file, under its own heading; the prompt says the repo's file wins on a conflict. Shares `CONVENTIONS_BUDGET` with the repo file (global clipped first) or gets its own budget: decide and record. Replays record which global text was used so a later edit doesn't change old results. Estimate: about 1 hour with tests.

#62 — The reviewer sees the repo's conventions [ ] · Added: 2026-11-23 · Source: assistant (gap review)
Why: `reviewer-v1.md` defines the `style` tag as "ignores the repository's conventions", but `review/` never calls `read_conventions`, so the reviewer judges style with no conventions to compare against. Style kickbacks are guesses.
Idea: pass the same conventions text (repo + #61 global) into the reviewer prompt, same budget and `defuse`. Invariant f is unaffected: conventions are repo input, not the writer's reasoning. Needs a reviewer prompt bump (`reviewer-v2`) so replays can compare it to v1. Estimate: about 45 minutes.

#63 — Read both AGENTS.md and CLAUDE.md, not the first one found [ ] · Added: 2026-11-23 · Source: assistant (gap review)
Why: `CONVENTIONS_FILES` is "first one found wins". A repo with both files loses every `CLAUDE.md` rule without a warning. Nested files (`pkg/AGENTS.md`) are also ignored.
Idea: read both root files, skip one that is identical to or a symlink of the other, and label each in the prompt. Nested files: include those on the path of the ticket's `owned_files` when there are any, within the budget. Log a warning when the budget clips the text. Estimate: about 45 minutes.

#64 — Check whether OpenCode loads AGENTS.md on its own [ ] · Added: 2026-11-23 · Source: assistant (gap review)
Why: OpenCode reads `AGENTS.md` from the project dir by default (not confirmed for 1.18.7). If it does, the writer gets the file twice: once from OpenCode, once from our prompt (clipped). That doubles its token cost and makes replays depend on OpenCode's behaviour too.
Idea: read the current OpenCode docs (rule 12) and check one live attempt's first `llm.call` for the file text. If it is there, turn it off in `build_config` (or the instructions setting) so our prompt is the only source. Estimate: about 15 minutes plus the live check.

#65 — `fman rules show`: preview the rules an agent will get [ ] · Added: 2026-11-23 · Source: user (asked how to test rules)
Why: there is no way to see what a rules file turns into before a real ticket runs: which files were found, which order, and whether the budget clipped them.
Idea: `fman rules show <repo>` prints the assembled conventions block for the writer and the reviewer exactly as the prompt builder renders it, with each source file, its character count and a clear "CLIPPED at N chars" line. No model call. Estimate: about 30 minutes. Depends on #61–#63.

#66 — Replay a rules change before it goes live [ ] · Added: 2026-11-23 · Source: user (asked how to test rules)
Why: a rules edit changes every future attempt, and there is no way to measure whether it helped. Replays already compare champion vs challenger, but `replay_runner` reads `AGENTS.md` from the worktree at the case's base commit, so both sides always get the same rules.
Idea: a conventions override on the challenger side (`fman experiment run --rules <file>`), keeping model and prompt fixed, so the comparison isolates the rules. Writer side: pass rate, cost, tokens. Reviewer side: catch / false-kickback rates, `style` tag rate in particular. Record the override's text hash on the experiment. Estimate: about 2 hours. Depends on #61.

### Unscheduled (experiments GUI, 2026-11-23)

Asked by the user: "are they setup in the command line or do we set them up with a gui", then "yes add it as a todo". Today experiments are started and promoted only from the CLI (`fman experiment run | status | promote`, `[experiments]` in config.toml, the nightly launchd job); `/experiments` on the metrics page is read-only (GET routes only).

#67 — Start, cancel and promote experiments from the metrics page [ ] · Added: 2026-11-23 · Source: user
Why: running an experiment means remembering variant ids (`MODEL/PROMPT@HASH8`) and typing them into the CLI, while the results are already on the web page. Starting and promoting from where the results are shown cuts that step.
Idea: on `/experiments`: a "new experiment" form (role, challenger model and prompt picked from lists, later the #66 rules override), cancel on a running one, promote on a finished one. Each button calls the same functions as the CLI (`worker_experiments.run_command`, promote), so nothing new decides anything. Promote shows the comparison and asks for a confirm step, because it changes the live writer or reviewer.
Safety: the page binds to loopback only (`metrics/web.py`, `DEFAULT_HOST`) and has no login, so any website open in the browser can send a POST to it. Every POST needs a CSRF token (per-process random, in the form) and an `Origin` / `Host` check that rejects anything but the page's own loopback origin; GETs stay side-effect free. A started run is a background job (the page does not block on a replay), and the cloud spend caps apply as on the CLI path.
Verify: a POST without the token, or with a foreign `Origin`, is refused with 403 and changes nothing; promote without the confirm step changes nothing; a form-started run gives the same `experiments` row as the CLI with the same arguments. Non-vacuous: drop the Origin check and the foreign-origin test fails.
Estimate: about 2–3 hours with tests.

### Unscheduled (plugins, 2026-11-23)

Asked by the user: "add the plugin hooks as a todo". Nothing in this repo loads plugins today (no entry-point lookup anywhere in `src/`).

#68 — Plugin hooks, so a separately installed package can extend the worker [ ] · Added: 2026-11-23 · Source: user
Why: some features do not belong in every install (the metrics page on the network with a login, per-person numbers and spend caps, several boxes on one queue). They need named places to attach to, or each one ends up as a fork of this repo.
Idea: a small loader (`plugins.py`) reads Python entry points in one group (`foreman_inator.plugins`) at startup and hands each plugin a registry. A hook is a narrow interface whose default is today's behaviour. Add hooks one at a time, each when a real feature needs it; first candidates: who may view the metrics page and which host it binds to (`metrics/web.py`, loopback today), how the spend caps are split (`spend.py`, one shared cap today), extra migrations, extra CLI commands. The dependency runs one way: this repo never imports a plugin and runs unchanged without one. Build the loader together with the first hook, not ahead of it.
Safety: a plugin is code running inside the worker with its keys. Load only installed packages (never a path from config or from a repo), list every loaded plugin and its version in `fman status` and at startup, and log and skip a plugin that fails to load (never fatal). No hook can relax an invariant (7a–7g): hooks take and return data; they do not replace the merge, allowed-users, sandbox or reviewer-independence checks. Hooks become a public API once a plugin depends on them, so version them, and a plugin states which versions it works with.
Verify: with no plugin installed every existing test passes unchanged; a fake plugin registered in a test is loaded, listed in status and its hook is called; a plugin that raises on load is skipped and the worker still starts; each hook's default gives today's behaviour. Non-vacuous: make the loader ignore the entry-point group and the fake-plugin test fails.
SPEC change needed: a short "Plugins" section naming the group, the hooks and the one-way dependency.
Estimate: about half a day with the first hook and tests.

### Unscheduled (LLM-proposed experiments, 2026-11-24)

Asked by the user: "does it make sense for the llm to create experiments?", then "yes add it as a todo and open a github issue". Today every challenger is written by hand: someone has to notice a pattern in kickbacks, write `writer-vN.md` / `reviewer-vN.md`, then run `fman experiment run --challenger`.

#69 — An LLM drafts challenger prompts from labeled kickbacks; promotion is unchanged [ ] · Added: 2026-11-24 · Source: user
Why: the experiment machinery (replays, shadow reviewer, promotion rule) exists, but nothing proposes the next variant, so experiments only happen when someone thinks one up. Kickbacks already carry a failure step (`kickbacks.failure_step`, #27), which is exactly what a prompt author needs to know.
Idea: `fman experiment propose --role writer|reviewer`. It groups recent kickbacks by failure step (and reviewer catch / false-kickback misses for the reviewer role), and a tier 3 model reads the current champion prompt plus the top patterns and drafts the next prompt version (`pkg/prompts/<role>-vN.md`, plus a short rationale naming the patterns it targets). The draft goes out as a PR or draft for the user to approve; only after approval is it registered as a challenger through the existing `run_command`. Model choice stays human (the list is short and known); the LLM writes prompts only. Promotion stays the existing rule-based gate (`promote`: wins on replay AND holds live); the LLM never promotes.
Safety: (1) Overfitting: the proposer reads only kickbacks older than a cutoff, and the replay that scores the challenger uses only cases after that cutoff, so it is never scored on tickets it was written from; record the cutoff on the experiment. (2) Volume: at most one LLM-proposed challenger running per role, and the proposer call counts toward `spend_caps.experiments_monthly_usd`. (3) Independence: the proposer model is not the reviewer that scores the challenger (same idea as the reviewer-independence invariant). (4) Consent: nothing runs or changes until the user approves the prompt change, same idea as #51's sub-issues. Kickback text and issue bodies are untrusted input to the proposer: `defuse` them as in the writer prompt.
Verify: with fake kickbacks clustered on one failure step, `propose` drafts a prompt whose rationale names that step and registers nothing until approval; a second `propose` while one LLM challenger is running for the role exits without a model call; the replay for a proposed challenger contains no case at or before the cutoff; `promote` behaves identically for proposed and hand-written challengers. Non-vacuous: drop the cutoff filter and the no-overlap test fails.
UI (user, 2026-11-25: "ok can we have a button that has a tier 3 llm generate an expirment too"): a "Propose an experiment" button on `/experiments`, next to #67's "new experiment" form. Pick the role (writer or reviewer), press the button, and it runs the same code as `fman experiment propose`. The page then shows the drafted prompt, its rationale (the kickback patterns it targets), the cutoff and the estimated proposer cost, with "Approve and start" (registers the challenger through `run_command`, as #67's form does) and "Discard". Nothing starts without that approve click. The button is disabled with the reason shown when an LLM challenger is already running for that role or the experiments spend cap is reached. Uses #67's POST protection. Build after #67 and the CLI part of this item.
SPEC change needed: one paragraph under "Experiments" saying challengers may be LLM-drafted, with the cutoff and the one-at-a-time rule.
Estimate: about half a day with tests, assuming `run_command` and the replay path are reused unchanged. Related: #66, #67.

### Unscheduled (tier order, 2026-11-25)

Asked by the user: "do local llms make sense in this app or should the first tier be open source cloud llms then more expensive ones like claude etc", then "yes, add it". Today `config.py:208-217` gives tier 1 `inkling-2-mini` (local), tier 2 `qwen3.8-27b` (local), tier 3 `glm-5.3` then Claude.

#70 — Tier 2 default becomes open-weights cloud (GLM); local 27B becomes optional [ ] · Added: 2026-11-25 · Source: user
Goal (user, 2026-11-25): "the main goal of this app is to cancel my max plan at 100 a month." So the test is total monthly cost (cloud spend, with the $50 default `spend_caps.monthly_usd` well under $100) at an acceptable merge rate, not the share of tickets that run locally.
Why: a 27B model on a Mac is slow on agent loops, which resend long prompts every turn (the writer timeout had to become configurable). Local runs also compete for memory (#57–#59), and those slowdowns get recorded as model failures. An open-weights cloud attempt costs a few cents, so a failed 15-minute local attempt that escalates anyway costs more in time than the cloud call costs in money. Tier 1 stays local: small mechanical changes are where small local models pass, and it keeps the v1 goal (replace Max usage with local models) and "local by default" for the easy work.
Idea: default ladder becomes tier 1 `inkling-2-mini` (local), tier 2 `glm-5.3` (cloud), tier 3 Claude Opus then Fable. `qwen3.8-27b` stays in the code as an opt-in local tier 2 for users who want it (documented in docs/setup.md). Check that reviewer independence still holds: a GLM tier 2 writer must get `claude-opus` as reviewer, which `reviewer_models` order already gives. #49 (`cloud = false`) must still work: with no local tier 2, a local-only repo stops at tier 1 and flags the user, or uses the opt-in qwen tier if configured.
Decide with data first: before changing the default, run tier 1 local and GLM on the same mix of tickets for about a week and compare accept rate and attempt minutes per accepted PR (needs #46). If qwen 27B matches GLM's accept rate within a reasonable time, keep it.
Verify: default config lists tiers 1/2/3 as local/cloud/cloud; an existing config.toml with qwen at tier 2 loads unchanged; escalation from tier 1 goes to GLM; a GLM writer gets a non-GLM reviewer; a `cloud = false` repo never calls a cloud endpoint (once #49 lands). Non-vacuous: put GLM in both writer and reviewer slots and the independence test fails.
SPEC change needed: the tier table in SPEC.md and README.md (tier 2 "Local, Ollama" → "Cloud, pay per use"), and the spend-cap note, since tier 2 now spends money.
Estimate: about 30 minutes for config and docs, about 2 hours with tests. Related: #46, #49, #57.

#71 — Chat feature: interactive coding sessions through the same tiers and spend caps [ ] · Added: 2026-11-25 · Source: user
Ask: after "the main goal of this app is to cancel my max plan at 100 a month", the user said Max also covers interactive Claude Code sessions, then: "yeah ill have to add a chat feature later".
Why: the worker handles only labeled issues. Interactive coding work also runs on Max today, so canceling Max needs a replacement for that too, or that work moves to pay-per-use API calls with no cap.
Idea (not designed yet): a chat entry point (`fman chat` or similar) that drives OpenCode in a worktree, using the same model ladder, proxy and spend caps as tickets. It starts on a cheap tier, a command moves it up, and its spend shows next to ticket spend in metrics. Decide later: its own spend cap vs. a share of `monthly_usd`, and whether chat sessions feed the router's data.
Estimate: not estimated; needs a design pass first. Related: #70.

#72 — Cheapest-first cross-family reviewer [ ] · Added: 2026-11-25 · Source: user
Ask: "having different families of models reviewing code should help raise the quality of the code", then "yes, add it" to cheapest-first reviewer picking.
Why: the family rule (invariant 7f, `review/reviewer.py:322`) already makes the reviewer independent. But `pick_reviewer` → `config.reviewer_for` takes the first entry of `reviewer_models` from a different family, and the default order is `("claude-opus", "glm")`. So every Inkling, Qwen or GLM writer gets Claude Opus as its reviewer, at Opus prices on even the smallest ticket. Against the goal in #70 (cancel the $100/month Max plan), review is likely the largest cloud cost.
Idea: pick the cheapest reviewer from a different family, with Opus last. Local/Qwen writer → GLM; GLM writer → a cheap cloud model of a third family (DeepSeek or Kimi; needs a new endpoint and Keychain key); Claude writer → GLM. Opus reviews only tier 3 work, or when the cheap reviewer's verdict is low confidence (a second review, at most one per attempt). Keep `reviewer_models` as an override for users who want a fixed order.
Measure first: run the cheap reviewer as a shadow reviewer (`experiments/shadow.py`) next to Opus on live tickets, and compare catch rate and false kickbacks before changing the default.
Verify: a local writer gets GLM; a GLM writer gets the third-family model, never GLM; with only Opus and GLM configured, behaviour matches today; a low-confidence cheap verdict triggers exactly one Opus review; review cost per ticket shows in metrics. Non-vacuous: drop the family check and the GLM-writer test picks GLM and fails.
SPEC change needed: reviewer selection paragraph (cost order, family rule unchanged, the one-escalation limit).
Estimate: about half a day with tests, plus a week of shadow data before switching the default. Related: #70, #46.

### Unscheduled (web UI, 2026-11-25)

Asked by the user: "Would it be useful to add a ui to this app? I want users to be able to change models and providers in tiers easily. and the chat feature needs a ui. The user could also change settings as well in it.", then "add all three pages as todos and open matching github issues. also i think a page where the user can see what is being actively worked on by the agents would be helpful". All four pages grow the existing local metrics app (`metrics/web.py`, Starlette, served by `fman metrics`, loopback only) instead of a separate app. A native Mac app is out of scope until this proves out.
Shared rules for every page that writes: the POST protection designed in #67 (per-process CSRF token in each form, `Origin` / `Host` must be the page's own loopback origin, 403 and no change otherwise); config writes go through `config.load` / `config.save` so validation is the same as the CLI; API keys go straight to the Keychain (same path as `fman config set-key`) and are never rendered back; everything from GitHub or the database stays HTML-escaped. Build the shared POST layer once, in whichever of #67 / #73 lands first.

#73 — Settings page: edit config from the browser [ ] · Added: 2026-11-25 · Source: user
Why: every setting is changed today by hand-editing `~/.foreman-inator/config.toml` or a CLI command, and a typo is found only when the worker next loads it.
Idea: `/settings`: spend caps (`daily_usd`, `monthly_usd`, `experiments_monthly_usd`), `writer_timeout_seconds`, `max_parallel_per_repo`, `allowed_users`, watched repos, and per-repo settings (verify command; the #51 profile and #49 `cloud = false` once they exist). Plain HTML form, no JavaScript. On save: validate, write, and show what changed. The running worker picks the change up on its next pass (re-read config when the file's mtime changes) instead of needing a restart; say so on the page.
Verify: an invalid value (negative cap, unknown repo) is refused with the same message as `config.load` and the file is unchanged; a valid save round-trips through `config.load`; a POST without the token or with a foreign `Origin` is refused with 403; the worker sees a changed cap without restarting. Non-vacuous: skip validation and the negative-cap test writes the file and fails.
Estimate: about 1 day with tests.

#74 — Tiers page: models and providers per tier [ ] · Added: 2026-11-25 · Source: user
Why: changing the ladder (for example #70's GLM at tier 2, or #72's reviewer order) means editing `ModelEndpoint` entries by hand: tier, kind, model id, base URL, family, key reference. That is the main setting the user wants to change easily.
Idea: `/tiers`: one card per tier listing its endpoints in order, with add / remove / reorder; an endpoint form with provider presets (Ollama, GLM, Anthropic, an OpenAI-compatible URL) that fill in base URL, wire format and family; an API key field that writes to the Keychain and shows only "key set". A "test" button sends one tiny request through the proxy and shows the reply or the provider's error text. The reviewer order (`reviewer_models`) is edited on the same page, and the page warns when a tier has no reviewer from another family (invariant 7f) or when a local-only repo (#49) would have no tier left.
Verify: added, reordered and removed endpoints round-trip through `config.load`; a key entered on the page lands in the Keychain and never appears in the HTML or the config file; the test button reports a refused key as an error, not success; a config whose writers all share the only reviewer's family shows the warning. Non-vacuous: render the key into the form's value and the "never appears in the HTML" test fails.
Estimate: about 1 day with tests. Related: #70, #72.

#75 — Chat page: the UI for #71 [ ] · Added: 2026-11-25 · Source: user
Why: #71's chat feature needs a place to type, and the rest of the UI lives on this local page.
Idea: `/chat`: pick a repo, start a session in its own worktree, send messages and see streamed replies (the one page that needs JavaScript: a small inline script reading server-sent events, no external scripts). Shows the current tier and model, a "move up a tier" button, and the running cost against the spend cap. Ends with "open as PR" or "discard worktree". Design depends on #71; do not build before #71's design pass.
Verify: (after #71's design) a session's messages stream in order; moving up a tier switches the model for the next message only; cost shown matches the spend table; a POST without the token is refused.
Estimate: several days; re-estimate after #71's design. Related: #71.

#76 — Activity page: what the agents are working on right now [ ] · Added: 2026-11-25 · Source: user
Why: `fman status` (`worker_status.render_status`) shows the queue only in a terminal and only when run. There is no view that stays open and shows live work: which ticket each agent is on, which model, which step, and for how long.
Idea: `/activity`: read-only, same data as `render_status` (tickets by state, locks, spend approvals, needs-info, CI waits, flags) plus, for each running attempt, the repo and issue link, model and tier, current step (writing, verify, review, CI), elapsed time against `writer_timeout_seconds`, tokens and cost so far, and a link to its trace (`/traces`). Show loaded local models and memory once #58 / #59 exist. Refresh with a `<meta http-equiv="refresh">` every few seconds so the page needs no JavaScript. Optional later: pause or cancel buttons through the shared POST layer.
Verify: with fake running attempts in a test store, the page lists each with its model, step and elapsed time, and matches `render_status`'s ticket states; a finished attempt leaves the running list; issue titles are escaped. Non-vacuous: drop the escape and the `<script>`-in-title test fails.
Estimate: about half a day with tests (read-only). Related: #58, #59, #60.
