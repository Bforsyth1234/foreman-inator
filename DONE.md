# foreman-inator — Done

Full write-ups of finished work, moved here from todo.md (todo.md rule 1).
Newest wave last. Write-ups are kept as they were when the work closed;
later waves supersede earlier ones where they conflict.

Format for each entry:

```markdown
#NN — <title> [x]
Wave: _ · Done: YYYY-MM-DD · Commit/PR: _ · Source: user | spec | assistant
- ✅ What was built, file by file where it helps.
- ✅ Tests added, with the non-vacuity check that was run.
Deviations: what changed from the plan, and why.
Live: what the live check showed, or "none needed".
```

## Wave 1

#1 — Repo scaffold: package, tooling, CI, secret guards [x]
Wave: 1 · Done: 2026-11-23 · Commit/PR: branch `wave-1` · Source: spec
- ✅ `pyproject.toml`: `uv_build` backend, src layout, Python ≥3.12, MIT; runtime dep `typer`; dev group `pytest`, `pytest-timeout` (60 s per test), `ruff` (line 100, rules E/F/W/I/B/UP/SIM). Scripts `foreman-inator` and `fman` → `foreman_inator.cli:app`. `uv.lock` in sync.
- ✅ `src/foreman_inator/__init__.py` (`__version__` from package metadata), `cli.py` (typer group with an eager `--version` callback, so later waves add `@app.command()` only), `worker.py` (empty `run()`).
- ✅ `LICENSE` (MIT), `README.md` (personal project, no promised support, links SPEC.md).
- ✅ `.pre-commit-config.yaml`: gitleaks hook, rev v8.30.1.
- ✅ `.github/workflows/ci.yml`: pytest on ubuntu-latest + macos-latest, ruff check + format check, gitleaks-action. GitHub-hosted runners only, `permissions: contents: read`.
- ✅ `tests/conftest.py`: autouse `foreman_home` fixture sets `FOREMAN_INATOR_HOME` under `tmp_path` (path returned, not created). `tests/fakes/__init__.py`. `tests/test_smoke.py` (3 tests).
- ✅ Verify: `fman --version` and `foreman-inator --version` print `foreman-inator 0.1.0`; pytest 3 passed; ruff clean. Non-vacuity: with the fixture removed, `test_home_is_inside_tmp_path` fails; restored, passes.
- ✅ gitleaks pre-commit hook proven in a throwaway repo: a commit adding a freshly generated `-----BEGIN RSA PRIVATE KEY-----` file was refused (`RuleID: private-key`), and the commit did not land.
Deviations: `pyproject.toml` written by hand instead of `uv init --package` (same result). CI also runs `ruff format --check`. `.gitignore` kept as the SPEC version plus `*.db-wal`, `*.db-shm`, `.claude/settings.local.json` (added by the user's session before this item).
Live: none needed. The CI gitleaks job has not run yet; it proves itself on the first push of a wave branch.

## Wave 2

#2 — Paths and config: `~/.foreman-inator`, config.toml, Keychain keys [x]
Wave: 2 · Done: 2026-11-23 · Commit/PR: branch `wave-2` · Source: spec
- ✅ `src/foreman_inator/paths.py`: `home()` (env `FOREMAN_INATOR_HOME` override), `app_key_path()`, `config_path()`, `db_path()`, `traces_dir()`, `recordings_dir()`, `worktrees_dir()`; every folder forced to mode 700 with `chmod` (existing loose homes are tightened). `write_secret_file()` and `write_app_key()` write mode 600.
- ✅ `src/foreman_inator/config.py`: frozen, validated `Config` (App ID/slug, installation IDs, `allowed_users`, repos, poll interval 45 s, `SpendCaps`, `models`, `reviewer_models`), `ModelEndpoint` (name, tier, kind local|cloud, model, base_url, family, `api_key_ref`; `.api_key()` reads the Keychain lazily). `load`/`save`/`from_dict`/`to_dict`, `set_key`, `resolve_key`, `key_ref`. `Config.models_for_tier()`, `.reviewer_for(writer)` (first reviewer from a different family, else None), `.is_allowed_user()`.
- ✅ Validation: unknown keys are errors at every level; plain-text secrets rejected with a pointer to `fman config set-key <name>`; `local` models must be loopback, `cloud` models need https and a Keychain ref.
- ✅ Tests: `tests/test_config.py`, 19 tests, Keychain faked in memory. Non-vacuity: removing `chmod`/`fchmod` fails the mode tests; replacing the plain-text check with `pass` fails `test_plain_text_api_key_is_rejected`.
Deviations: values SPEC does not give were chosen: spend caps $5/day, $50/month, $10/month experiments; minimum poll interval 5 s; default model ids and URLs (tier 1 `qwen3.6:35b-a3b`, tier 2 `qwen3.8:27b` on Ollama at 127.0.0.1:11434; tier 3 `glm-4.6` then `claude-sonnet-4-5`). The tier 3 defaults are placeholders not checked against current docs (rule 12: confirm before the first cloud run). Models are a flat `[[models]]` list with a `tier` field, because tier 3 has two models in order. `reviewer_models` is an ordered preference list. `config.toml` is also written mode 600.
Live: none needed.

#3 — SQLite store: full schema and migrations [x]
Wave: 2 · Done: 2026-11-23 · Commit/PR: branch `wave-2` · Source: spec
- ✅ `src/foreman_inator/db/migrations/001_initial.sql`: every SPEC "Data model" table (tickets, attempts, kickbacks, reviews, experiments) plus `variants`, `repo_settings`. `STRICT` tables, enums as CHECK constraints with SPEC's exact values, ticket ids `AUTOINCREMENT` (never reused), times as ISO-8601 UTC text.
- ✅ Added beyond SPEC so later items need no schema change: tickets `routed_tier`, `flag_reason`, `flagged_at`, and a unique index of one open ticket per (repo, issue); attempts `started_at`, `ended_at`, `base_commit`, `verify_output_path`; kickbacks `failure_step_index`; reviews `created_at`, `shadow` (one non-shadow review per attempt); tables `champion_history` (#24), `poll_cursors` (#11), `spend_ledger` and `spend_approvals` (#18), `file_locks` (#19), `schema_migrations`.
- ✅ `src/foreman_inator/db/store.py`: `Store(path, read_only=, clock=, migrations=)`; WAL, foreign keys on, one connection under an `RLock`; `migrate()` applies numbered files each in its own transaction and refuses a database newer than the code. Typed methods per table returning frozen dataclasses (`create_ticket`, `set_column`, `add_attempt`, `finish_attempt`, `add_attempt_usage`, `record_kickback`, `record_review`, `create_experiment`, `upsert_variant`, `set_champion`, `set_verify_command`, `mark_verify_prompted`, `get_cursor`/`set_cursor`, `record_spend`/`spend_total`, `approve_spend`, `acquire_locks`/`release_locks`, `query` for raw reads). `db/__init__.py` re-exports them and the enum tuples.
- ✅ Tests: `tests/test_store.py`, 46 tests. Migrating twice is a no-op; a fresh database matches column lists copied from SPEC, the item and the listed additions; all 16 enum columns accept SPEC values and reject others; migration SQL ships in the wheel (checked in a clean venv).
- ✅ Non-vacuity: with the `"column"` CHECK removed, the `doing` test fails (2 failed, 44 passed); with `AUTOINCREMENT` dropped, `test_ticket_ids_are_never_reused` fails.
Deviations: a `db/migrations/` folder with `001_initial.sql` replaces `db/schema.sql`, so later items add `002_*.sql`. Enum checks live only in the database, not in Python. Not added, left to their items: embedding storage (#23) and per-case replay results (#25). Needs SQLite ≥ 3.37. `attempts.outcome` is NULL while an attempt runs or waits on review.
Live: none needed.

#4 — GitHub App auth, REST client and a fake GitHub [x]
Wave: 2 · Done: 2026-11-23 · Commit/PR: branch `wave-2` · Source: spec
- ✅ `src/foreman_inator/github/app_auth.py`: `AppAuth` (RS256 JWT, iat −60 s, 9 min life; `app_slug`, `list_installations`, `create_installation_token`), `InstallationAuth.token()` cached until 60 s before expiry, `send_with_retry` (rate limits always retried; 5xx and network errors retried for idempotent methods; 5 attempts), errors `GitHubError`, `GitHubNotFound`, `GitHubRateLimited`, `ForbiddenOperation`.
- ✅ `src/foreman_inator/github/client.py`: `GitHubAPI` Protocol and `GitHubClient` returning frozen dataclasses (`Issue`, `IssueEvent`, `Comment`, `Ref`, `PullRequest`, `Review`). Methods: list/get issues, issue events, comments, add/remove labels, close issue, branch create/update (only `fman/`), create/update draft PR, `find_pr_for_branch`, `mark_ready_for_review` (GraphQL), `post_review`, `list_reviews`, `token()`, `bot_login()`. `label_applied_by(events, label)` for invariant 7b. No merge or approve method exists (7a).
- ✅ `tests/fakes/github.py`: `FakeGitHub` implementing the same Protocol with the same dataclasses, plus human-side helpers (`create_repo`, `create_issue`, `apply_label(by=)`, `submit_review(by=)`, `merge_pr`, `close_pr`, `push_branch`) and controls (`writes`, `assert_no_writes()`, `forbid_writes()`, `fail_next()`).
- ✅ Tests: `tests/test_github_client.py`, 25 tests; RSA key generated at test time; one scenario run against the real client over `httpx.MockTransport` (`tests/fixtures/github/scenario.json`, 22 hand-written exchanges) and against the fake, compared step by step.
- ✅ Non-vacuity: with the expiry margin set to 0 the caching test fails; removing `require_fman_branch` fails 4 tests; sending `event: APPROVE` fails the recorded-request check.
Deviations: the bot's PR review is always a COMMENT review, because GitHub refuses APPROVE or REQUEST_CHANGES from a PR's own author (the bot opens the PRs); the verdict goes in the review body and in SQLite (affects #17, #21, and SPEC's "posts a PR review"). POST is not retried on 5xx (it may already have been applied); it is retried on rate limits. Retry helpers live in `app_auth.py`. Extra methods beyond the item for later waves.
Rule 12 (docs read 2026-11-23, docs.github.com): REST API version pinned to `2026-03-10`; "ready for review" is still GraphQL-only (`markPullRequestReadyForReview`); the issue events endpoint is current; JWT `iss` may be the Client ID or App ID; installation tokens can be variable-length `ghs_` tokens. Pages: apps JWT guide, rest/apps/apps, rest/pulls/pulls, rest/pulls/reviews, graphql/reference/pulls, rest/issues/{events,issues,labels,comments}, rest/git/refs, REST best practices, api-versions, breaking-changes, approving-a-pull-request-with-required-reviews.
Live: none needed.

#5 — Tracing core and run recorder (fde-kit) [x]
Wave: 2 · Done: 2026-11-23 · Commit/PR: branch `wave-2` · Source: spec
- ✅ `src/foreman_inator/tracing/otel.py`: `init(endpoint=, traces_dir=)` on `fde_kit.tracing.init_tracing` (safe with no backend listening: falls back to a local provider, so trace ids stay real and nothing stalls; with `traces_dir`, spans are also appended to `<traces>/<ticket>/<trace_id>.jsonl`). Span helpers with SPEC's names: `ticket_span`, `route_span`, `attempt_span`, `record_llm_call`, `record_tool_call`, `verify_span` + `set_verify_result`, `review_span` + `set_verdict`, `review_check_span`, `github_span`, `add_kickback`. Content truncated at 4,000 chars with `trace_content`.
- ✅ One trace per ticket across restarts: the root and attempt span ids are derived from the trace id, so `ticket_span(id, trace_id=<stored>)` reopens the trace and later attempts hang under the original root.
- ✅ `src/foreman_inator/tracing/recorder.py`: `Recorder(recordings_dir, ticket_id, attempt_id, role=)` with `user`, `llm`, `tool`, `close`; thread- and task-safe; `get_recorder(attempt_id)` finds the open recorder from anywhere. Files: `<recordings>/<ticket>/<attempt>.jsonl.gz`, full outputs over 4,000 chars in `<attempt>.files/`, reviews in `<attempt>.review.jsonl.gz`. `load_recording`, `ticket_recordings`, `prune(recordings_dir, merged=, traces_dir=, max_age_days=90)`.
- ✅ Tests: `tests/test_tracing.py`, 9 tests. Span tree matches SPEC (ticket → route, attempt → llm.call/tool.call/verify, review → 5 checks, github). Recordings round-trip through fde-kit's real `RunRecord.load` in full, and `ReplayProvider` serves the turns back. Other tests get spans with `capture_spans()`; nothing sets the global tracer provider.
- ✅ Non-vacuity: inverting the merged check in `prune()` fails `test_prune_keeps_merged_and_deletes_old_unmerged`; making derived span ids random fails the tree and reopen tests.
Deviations: recordings stream to plain `.jsonl` through fde-kit's writer and are gzipped on `close()` (fde-kit reads plain text only); a crash leaves a readable `.jsonl`. Kickbacks are an event on the `github` span with a link to the attempt span, because the attempt span has ended by then and OpenTelemetry cannot add events to a finished span. One root `ticket` span per ticket, emitted in the first session only. Added: local span files under `traces_dir`, side files for full outputs, `review.check` as the shared child span name.
Live: none needed.

#6 — Verify command detection and runner [x]
Wave: 2 · Done: 2026-11-23 · Commit/PR: branch `wave-2` · Source: spec (decision: detected from the repo)
- ✅ `src/foreman_inator/runner/verify.py` (stdlib only): `detect(repo_dir, override=None)` in the order override → Makefile `test`/`check` → package.json `test` script (npm, or pnpm/yarn by lockfile) → Python (`pyproject.toml`/`pytest.ini`/`tests/` with `.py` files) → `Package.swift` → None. `VerifyCommand(argv, source, display)`.
- ✅ `run(cmd, cwd, timeout=1200, output_path=None)` → `VerifyResult(passed, tests_run, tests_failed, output_path, exit_code, timed_out, seconds, command)`. Never raises; own session, SIGTERM then SIGKILL to the whole process group on timeout; full output saved to the caller's path; `.output_tail()` for retry prompts.
- ✅ `parse_counts()` for pytest, jest, vitest, XCTest, Swift Testing, unittest. `detect_subset(repo_dir, test_paths)` for the reviewer's revert check (pytest paths; jest/vitest paths; None elsewhere).
- ✅ `tests/fixtures/repos/`: `makefile`, `node`, `python`, `python_failing`, `swift`, `empty`. `tests/test_verify.py`: 43 tests, each copying its fixture into `tmp_path`; npm and swift runs skip when the tool is missing.
- ✅ Non-vacuity: with `passed=not timed_out` (exit code ignored) 3 tests fail, including the failing-fixture test; with `os.kill` instead of `os.killpg` the process-group test fails.
Deviations: Python command is `uv run pytest` with `uv.lock`, `poetry run pytest` with `poetry.lock`, else `python3 -m pytest` from PATH. `tests/` counts only as an exact-case folder holding `.py` files (a Swift `Tests/` would otherwise match on macOS). Swift runs `swift test`; SPEC's "`swift typecheck`" is not a real SwiftPM command. Extras beyond the item: `output_path`, extra result fields, `detect_subset`, more count parsers.
Live: none needed.

#7 — Worktree manager [x]
Wave: 2 · Done: 2026-11-23 · Commit/PR: branch `wave-2` · Source: spec
- ✅ `src/foreman_inator/runner/__init__.py` (docstring only) and `src/foreman_inator/runner/worktree.py` (stdlib, shells out to `git`). Layout under the worktrees root: `<owner>/<repo>/.mirror` (bare), `<owner>/<repo>/issue-<n>-a<attempt>` on branch `fman/issue-<n>/a<attempt>`, `<owner>/<repo>/scratch-<name>`.
- ✅ `WorktreeManager(root, identity)` → `.mirror(repo, clone_url)`, `.create(repo, base_branch, ticket, attempt, clone_url=, token=, start_point=)`. `RepoMirror`: `ensure`, `fetch`, `prune`, `resolve`, `create`, `scratch(commit, name, branch=None)` (never pushes), `open(issue, attempt)` (reattach after restart), `cleanup_issue(issue)`.
- ✅ `Worktree`: `base_sha`, `head_sha()`, `changed_files()`, `diff_stats()` → `DiffStats(files, insertions, deletions)`, `diff_text()`, `commit_all(message)`, `push(token, remote_branch=, force=)`, `remove()` (safe to call twice and after a crash).
- ✅ Invariant 7a: `check_branch()` refuses any branch not starting with `fman/` for create, commit and push; force applies only to an `fman/` target; scratch worktrees raise `PushRefused`.
- ✅ Token handling: passed to git only through `GIT_CONFIG_COUNT`/`http.extraHeader` environment variables; never in argv, `.git/config`, the remote URL or error messages. Every git call ignores global and system config.
- ✅ Tests: `tests/test_worktree.py`, 27 tests on local bare repos under `tmp_path` (default branch `trunk`, no global git identity needed). Non-vacuity: with the prefix check dropped 9 tests fail, including `test_push_to_main_raises`; with error redaction removed `test_token_scrubbed_from_errors` fails.
Deviations: the item's `push(branch, token)` is `Worktree.push(token, remote_branch=, force=)`; `fetch`/`ensure`/`create` also take the token for private repos. API is wider than the item (`changed_files`, `diff_text`, `scratch`, `open`, `cleanup_issue`, `start_point`) to serve #10, #12, #17, #19, #25. Needs git ≥ 2.31. Known limits: operations on one mirror are not safe concurrently (#19 must serialize per repo); a same-user process can read the token from a running git's environment.
Live: none needed.
