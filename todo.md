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

Next ID: #52
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
| 6 | A: #21 (alone: the main loop) | `pkg/worker.py`, `pkg/cli.py` (`metrics`), `pkg/pr/publisher.py` (ready-for-review + bot review posting), `tests/test_worker_v2.py` | [ ] |
| 6b | A: #40 · B: #41, #42, #43 (one agent, in order: all three edit the GitHub client and poller) · C: #44 | A `pkg/runner/sandbox.py`; edits `pkg/runner/opencode.py`, `pkg/runner/verify.py`, `pkg/review/checks.py`, `pkg/config.py` · B `pkg/github/client.py`, `tests/fakes/github.py`, `pkg/github/poller.py`, `pkg/tickets/lifecycle.py`, `pkg/tickets/prompt.py`, `pkg/setup/manifest.py`, `pkg/runner/worktree.py`, `pkg/db/store.py`, `pkg/db/migrations/002_*.sql` · C `pkg/tickets/clarify.py`, `pkg/prompts/clarify-v1.md` (+ own tests). Runner at close: `worker.py` wiring for all five; for #44 the `NeedsInfoAnswered` event in `poller.py`/`lifecycle.py` and the Q&A block in `tickets/prompt.py` (after slot B is merged into the wave branch); `pyproject.toml` deps | [ ] |
| 7 | A: #22 (cross-repo, optional) | bot-inator repo only; nothing in this repo | [ ] |
| 8 | A: #23 · B: #24 · C: #25 · D: #26 · E: #27 · F: #28 | A `pkg/routing/knn.py`, `pkg/routing/explore.py` · B `pkg/experiments/variants.py`, `pkg/experiments/__init__.py`, `pkg/prompts/__init__.py` · C `pkg/experiments/replay_sets.py`, `pkg/experiments/replay_runner.py` · D `pkg/experiments/shadow.py` · E `pkg/tracing/failure_label.py`, `pkg/prompts/failure-label-v1.md` · F `pkg/tracing/replay.py`, `pkg/metrics/trace_view.py`, `tests/recordings/` (+ own tests). Runner at close: `pyproject.toml` deps | [ ] |
| 9 | A: #29 (alone: wiring) | `pkg/worker.py`, `pkg/cli.py` (`experiment`, `replay`), `pkg/metrics/web.py` (new routes and failure-step columns), `tests/test_worker_v3.py` | [ ] |
| 10 | A: #30 · B: #31 · C: #32 | A `pkg/export.py` · B `pyproject.toml` (metadata, entry points), `.github/workflows/release.yml`, `docs/releasing.md` · C `README.md`, `docs/setup.md`, `docs/how-it-works.md`, `docs/demo.gif` (+ own tests). Runner at close: `cli.py` (`export`) | [ ] |
| — | #33–#39 | unscheduled (SPEC milestone 5) | open |
| — | #45–#50 | unscheduled (gap review, 2026-11-23) | open |
| — | #51 | unscheduled (user request, 2026-11-23); proposed as its own wave after 6b | open |

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

Still to come: #21 (milestone 2); #40, #41 and #44 (wave 6b).

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

#21 — Worker loop v2: tiers, escalation, review, caps, locks (milestone 2) [ ]
Wave: 6 · Agent: A (alone) · Added: 2026-11-23 · Source: spec
Ask: The full flow in SPEC "Ticket lifecycle".
Files:
⚬ OWNED: `src/foreman_inator/worker.py`, `src/foreman_inator/cli.py` (`metrics`, `approve-spend`), `src/foreman_inator/pr/publisher.py` (ready-for-review after review approval, bot review posting), `tests/test_worker_v2.py`
⚬ EDITED (additive, from wave 4): `src/foreman_inator/tickets/prompt.py`: a kickback-triggered attempt that fails checks must get a retry prompt with both the note and the check output.
Approach: replace v1's straight line with: route (#15) → write → verify → escalation (#16) → AI review (#17) → PR ready only on approval or handover → your review → escalation again. Wire spend (#18) into the proxy, locks (#19), metrics page + summary (#20). Remove the v1 "ready immediately" flag.
Verify:
⚬ Scenario tests against fakes for every row of the lifecycle diagram, including two AI kickbacks → writer escalates, tier 3 failure → handed to user.
⚬ PR is never marked ready before an approve verdict or handover (invariant 7a); non-vacuous: mark ready early, test fails.
Live (user): one week on 1–2 real repos. Then read the metrics page: share solved locally, cost per accepted ticket. On a public repo, run this week only after #40 (sandbox) is built.
Deviations:

### Hardening — gaps found in review (wave 6b)

Items #40–#44 were written 2026-11-23 from a gap review of SPEC.md,
todo.md and the code (user: "Yes, add them"). Every DECISIONS list below
is an assistant default the user can overturn. SPEC.md is an export of
the Claude doc, so each item names the SPEC change it needs instead of
editing SPEC.md.

#40 — Sandbox for every process that runs model-written code [ ]
Wave: 6b · Agent: A · Added: 2026-11-23 · Source: assistant (gap review; user approved)
Ask: A command the model writes can't read or write outside the attempt's worktree, and can't reach the internet.
Why: `runner/opencode.py` sets `bash: allow` and says itself "this is not a sandbox". An allowed user can label an issue a stranger wrote on a public repo, so untrusted text reaches a model with a shell on the Mac.
DECISIONS (assistant, 2026-11-23):
1. macOS `sandbox-exec` with a profile generated per attempt. Chosen over a container because OpenCode, the toolchains (uv, node, swift) and the model server already run on the Mac, and Swift builds need macOS. Apple marks `sandbox-exec` deprecated; it still ships. Check it on the current macOS first (rule 12) and record the date.
2. Covers `opencode serve` (its bash tool inherits it), the verify command, and the reviewer's checks (tests, coverage, revert, run-app).
3. Files: writes only to the worktree and a per-attempt temp and cache directory (tool caches are pointed there by env). Reads: system and toolchain paths plus the worktree and the git paths the attempt needs (found by test, nothing wider). No reads of `~/.foreman-inator/app.pem`, config, the database, other attempts' worktrees, or the rest of the home directory.
4. Network: loopback to the attempt's proxy port only; none for verify and reviewer checks. A repo whose tests or installs need the internet sets `sandbox_network = "allow"` in config.
5. Fails closed: if the sandbox can't start, the attempt doesn't run and the ticket is flagged. The only way out is an explicit `sandbox = "off"` in config, which `fman status` and the PR description show.
6. Linux is out of scope (launchd and Keychain are already macOS-only); the launch goes through one adapter so another backend can be added.
SPEC change needed: "Safety on public repos" gains the sandbox; todo.md rule 7 gains an invariant for it.
Files:
⚬ OWNED: `src/foreman_inator/runner/sandbox.py`, `tests/test_sandbox.py`
⚬ EDITED (additive, earlier waves): `src/foreman_inator/runner/opencode.py`, `src/foreman_inator/runner/verify.py`, `src/foreman_inator/review/checks.py` (each process launch goes through the adapter), `src/foreman_inator/config.py` (`sandbox`, per-repo `sandbox_network`)
⚬ Do NOT touch: the files of slots B and C.
Approach: `sandbox.wrap(argv, policy) -> argv` plus `Policy(worktree, proxy_port, network)`; the three launch points call it. Fallback if `sandbox-exec` doesn't survive contact: a dedicated low-privilege macOS user for attempts, with the same file rules through permissions.
Verify:
⚬ A fixture script inside the sandbox, with a fake home in `tmp_path`: reading a key file outside the worktree, writing outside the worktree, and connecting to a non-proxy address all fail; writing in the worktree and calling the proxy port succeed. These tests run on macOS only; the profile-text tests run everywhere.
⚬ The fixture repos (python, node, swift, makefile) still pass verify under the sandbox.
⚬ pytest + ruff gate (rule 8).
⚬ Non-vacuous: remove the write rule, the outside-write test fails.
Live (user): one real ticket on a toy repo with the sandbox on. Then an issue whose body tells the agent to print a file from `~/.ssh` and fetch an outside URL: both show as blocked in the trace.
Deviations:

#41 — Read GitHub CI results for the PR head [ ]
Wave: 6b · Agent: B (first of three) · Added: 2026-11-23 · Source: assistant (gap review; user approved)
Ask: A PR is never marked ready for my review while its Actions run is red.
Why: `github/client.py` has no check-runs call; only the local verify command counts today.
DECISIONS (assistant, 2026-11-23):
1. After local checks pass and the draft PR is pushed, wait for the head commit's check runs before AI review (CI is free; the reviewer is not).
2. A failed check run counts as failed checks in the escalation rules (#16), with the failing job's log tail as the error output.
3. No check runs after a 2-minute grace period → the repo has no CI; skip. Still pending after 30 minutes → continue and say so in the PR description.
4. If the same job is also red on the base branch head, the failure isn't charged to the attempt; the PR notes "CI already red on base".
5. New App permissions: `checks: read`, and `actions: read` for job logs. Confirm both against the current GitHub docs first (rule 12). An App created before this change lacks them: the worker skips the CI step with one warning, and `fman setup` prints how to add them.
SPEC change needed: lifecycle (CI between checks and AI review) and the permissions list.
Files:
⚬ OWNED (slot B): `src/foreman_inator/github/client.py`, `tests/fakes/github.py`, `src/foreman_inator/github/poller.py`, `src/foreman_inator/tickets/lifecycle.py`, `src/foreman_inator/setup/manifest.py`, `src/foreman_inator/db/store.py`, `src/foreman_inator/db/migrations/002_ci.sql`, `tests/test_ci_checks.py`
⚬ Do NOT touch: the files of slots A and C.
Approach: `list_check_runs(repo, sha)` and `get_job_log_tail(repo, job_id)` on the client and the fake; a `CiResult` event from the poller (+1 request per poll per ticket waiting on CI); `attempts.ci_passed` in a new migration.
Verify:
⚬ Fake GitHub scenarios: green → continues; red → `failed_checks` with the log tail in the retry prompt; none → skipped; pending past the limit → continues with the note; red on base too → not charged.
⚬ Missing permission → one warning, no crash.
⚬ pytest + ruff gate (rule 8).
⚬ Non-vacuous: treat a red run as green, the red scenario test fails.
Live (user): add the two permissions to the App; open a ticket on a repo with a failing Actions job and confirm the PR stays a draft.
Deviations:

#42 — Inline review comments reach the next attempt [ ]
Wave: 6b · Agent: B (second of three) · Added: 2026-11-23 · Source: assistant (gap review; user approved)
Ask: Comments I leave on specific lines in a "Request changes" review are part of the kickback note.
Why: the client only has `list_reviews`, which returns the review body; line comments are never fetched.
DECISIONS (assistant, 2026-11-23):
1. Fetched only when a new request-changes review from an allowed user arrives (+1 request per kickback).
2. Each comment goes into the note as `path:line`, the last 3 lines of its diff hunk, then the comment text, in file order.
3. Same handling as the issue body: defused, and clipped to a 6,000-character budget with a visible marker.
4. A review with line comments but no "Request changes" still changes nothing.
Files:
⚬ OWNED (slot B): `src/foreman_inator/github/client.py`, `tests/fakes/github.py`, `src/foreman_inator/github/poller.py`, `src/foreman_inator/tickets/prompt.py`, `tests/snapshots/ticket_prompt/kickback_user_inline.txt`, `tests/test_review_comments.py`
Approach: `list_review_comments(repo, pr_number, review_id)`; `ReviewSubmitted` carries them; the prompt builder renders them under the note.
Verify:
⚬ Snapshot: a kickback with two line comments renders both with path and line.
⚬ An oversized comment is clipped with the marker.
⚬ pytest + ruff gate (rule 8).
⚬ Non-vacuous: drop the line comments from the note, the snapshot test fails.
Deviations:

#43 — Handle a PR that conflicts with its base branch [ ]
Wave: 6b · Agent: B (third of three) · Added: 2026-11-23 · Source: assistant (gap review; user approved)
Ask: A PR waiting for my review never sits there unmergeable without anyone noticing.
Why: nothing in the code reads `mergeable` or rebases; the base can move for days while a PR waits.
DECISIONS (assistant, 2026-11-23):
1. Read `mergeable` from the `get_pr` call the poller already makes (no extra requests). Only `false` acts; `null` (GitHub still computing) and "behind but clean" do nothing.
2. A conflict on a ticket in `testing` starts one attempt with a new trigger `rebase`, on the tier that wrote the change: the previous diff is replayed onto the new base, the model resolves the conflicts, then checks and AI review run as for any attempt. The result is force-pushed to the ticket's own `fman/` branch (invariant 7a allows it).
3. Not a kickback, and a failed `rebase` attempt doesn't count against the model in metrics.
4. Capped at 2 rebase attempts per ticket, then the issue is flagged for the user (invariant 7e).
SPEC change needed: `attempts.trigger` gains `rebase`; lifecycle gains the conflict loop.
Files:
⚬ OWNED (slot B): `src/foreman_inator/github/client.py`, `tests/fakes/github.py`, `src/foreman_inator/github/poller.py`, `src/foreman_inator/tickets/lifecycle.py`, `src/foreman_inator/tickets/prompt.py`, `src/foreman_inator/runner/worktree.py`, `src/foreman_inator/db/store.py`, `src/foreman_inator/db/migrations/003_rebase.sql`, `tests/snapshots/ticket_prompt/rebase.txt`, `tests/test_base_conflict.py`
Approach: `PullRequest.mergeable`; a `BaseConflict` event; `worktree.replay_onto_base()` leaves conflict markers for the model; a rebase prompt section listing the conflicted files.
Verify:
⚬ Fake GitHub: `mergeable=false` in testing → one `rebase` attempt; `null` → nothing; a third conflict → flagged, no attempt.
⚬ A real git fixture with a conflicting base commit: the worktree holds the markers and the listed files match.
⚬ pytest + ruff gate (rule 8).
⚬ Non-vacuous: remove the cap, the third-conflict test fails.
Deviations:

#44 — Ask before guessing: clarifying questions on a vague issue [ ]
Wave: 6b · Agent: C · Added: 2026-11-23 · Source: assistant (gap review; user approved)
Ask: When an issue is too vague to act on, the worker asks me on the issue instead of spending attempts on a guess.
Why: OpenCode's `question` permission is denied (correct for an unattended run), and nothing checks the issue before work starts.
DECISIONS (assistant, 2026-11-23):
1. A check before routing, by the small local model the router uses, with its own prompt file so the router variant doesn't change.
2. At most 3 questions, in one issue comment, plus a `needs-info` label. The ticket stays in `todo`; no database change (the label and the comment are the state).
3. One round per ticket (invariant 7e). After the answer the ticket proceeds whatever the answer says.
4. Resumes when an allowed user comments on the issue, or removes `needs-info` (meaning "go ahead as written"). The worker removes the label itself on resume. Cost: +1 comment listing per waiting ticket per poll.
5. Mid-attempt questions stay denied.
6. `clarify = false` in config turns the check off.
SPEC change needed: lifecycle gains the needs-info step before routing; the `needs-info` label joins the label list.
Files:
⚬ OWNED: `src/foreman_inator/tickets/clarify.py`, `src/foreman_inator/prompts/clarify-v1.md`, `tests/test_clarify.py`
⚬ Do NOT touch: the files of slots A and B (the runner adds the poller event and the prompt block at wave close).
Approach: pure functions. `assess(ticket) -> Clear | NeedsInfo(questions)` (model call through the proxy, traced as `clarify`; malformed output → `Clear`). `answered(comments, asked_at, is_allowed) -> str | None` returns the Q&A text for the prompt.
Verify:
⚬ Fixed model outputs: clear → `Clear`; vague → at most 3 questions; malformed → `Clear`.
⚬ `answered` ignores comments from the bot and from users not on the allowlist (invariant 7b).
⚬ A ticket that was already asked once is never asked again.
⚬ pytest + ruff gate (rule 8).
⚬ Non-vacuous: accept a comment from a non-allowed user, the allowlist test fails.
Live (user): label one deliberately vague issue; answer the question from the phone; confirm the attempt uses the answer.
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

### Unscheduled (gap review, 2026-11-23)

Found in the same review as #40–#44 (user: "Yes, add them"). Each has
its evidence and a first idea; none has decisions or owned files yet, so
write the full item from the template before scheduling one.

#45 — Install dependencies in a fresh worktree before verify [ ] · Added: 2026-11-23 · Source: assistant (gap review)
Why: no install step exists anywhere in the code (no `npm ci`, `uv sync` or `swift package resolve`), so a new worktree has no `node_modules` or venv and verify can fail for reasons unrelated to the change.
Idea: detect an install command next to the verify command, save it per repo like `fman repo set-verify`, run it once per worktree. Needs network, so decide it together with #40's `sandbox_network`.

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
