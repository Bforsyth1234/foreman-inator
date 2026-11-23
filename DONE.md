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
