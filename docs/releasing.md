# Releasing foreman-inator

For the maintainer. The release workflow is `.github/workflows/release.yml`. It needs no
stored token: PyPI and TestPyPI trust it through trusted publishing (OIDC).

**Status on 2026-11-23: nothing can be published to PyPI yet.** fde-kit, a dependency, is
not on PyPI, so a wheel published there would not install. Until that is fixed, the
workflow builds, tests and makes a GitHub release on each tag, and skips the upload with a
message. See [The fde-kit problem](#the-fde-kit-problem).

## How people install it today

```sh
uv tool install git+https://github.com/Bforsyth1234/foreman-inator
```

This works once the package is on `main` (on 2026-11-23 `main` has no `pyproject.toml`
yet, so until the wave branches merge, add `@<branch-or-tag>`, for example `@v0.1.0`).
uv reads `[tool.uv.sources]` when it builds from a git checkout, so it fetches fde-kit
from GitHub as well.

pipx works too when it uses its uv backend, which recent pipx (1.19) picks when uv is
installed. With pipx's pip backend, fde-kit has to be installed first:

```sh
pipx install --preinstall "fde-kit @ git+https://github.com/Bforsyth1234/fde-kit" \
  git+https://github.com/Bforsyth1234/foreman-inator
```

`uv tool install foreman-inator` and `pipx install foreman-inator` (from PyPI) do not work
yet. The package is not published, and it could not be installed from PyPI if it were.

## The fde-kit problem

`pyproject.toml` lists `fde-kit` as a plain dependency and points uv at GitHub for it in
`[tool.uv.sources]`. That table is uv-only and is not part of the built package. The wheel
built on 2026-11-23 (`uv build`, uv 0.9.29) says:

```
Requires-Dist: fde-kit
Requires-Dist: httpx>=0.27
...
```

So:

- PyPI would **accept** the upload: there is no direct URL in the metadata (PyPI refuses
  those, so `fde-kit @ git+https://...` in `dependencies` is not an option either).
- A stranger's `uv tool install foreman-inator` would then **fail**: fde-kit was not found
  in the registry (checked: `https://pypi.org/pypi/fde-kit/json` returns 404 on
  2026-11-23). pip and pipx fail the same way ("No matching distribution found for
  fde-kit").
- Worse, anyone could register `fde-kit` on PyPI later, and every install of foreman-inator
  would then pull their code. A published foreman-inator with a bare `fde-kit` requirement
  is a standing invitation for that (dependency confusion).

foreman-inator uses a small part of fde-kit: `fde_kit.tracing` (`init_tracing`,
`trace_content`), `fde_kit.prompts` (`PromptRef`, `load_prompt`; prompt hashes come
through `PromptRef`), `fde_kit.record` (`RunRecorder`, `RunRecord`, `utc_now`) and `fde_kit.config.Settings`
(read by `tracing/otel.py`). fde-kit itself brings in `anthropic`, `pydantic`,
`pydantic-settings`, OpenTelemetry and `rich`.

### Options

| Option | What it takes | Cost | Risk |
| --- | --- | --- | --- |
| **A. Publish fde-kit to PyPI first** (recommended) | In the fde-kit repo: confirm the name is free, add the same trusted-publishing release workflow, tag v0.1.0. Then in foreman-inator: change `"fde-kit"` to `"fde-kit>=0.1,<0.2"`, drop or keep the `[tool.uv.sources]` entry (keep it only for local development), relock, flip `PUBLISH_ENABLED`. | An hour or two, once. fde-kit then has to make releases when foreman-inator needs a change in it. | Low. Claims the fde-kit name too, which closes the dependency-confusion hole. |
| **B. Stay on GitHub installs** | Nothing more. README says `uv tool install git+https://github.com/Bforsyth1234/foreman-inator`. Tags give GitHub releases with the wheel attached. | Every user needs uv (or pipx with the uv backend, or `--preinstall`). No `uv tool install foreman-inator`, no Homebrew formula from PyPI (#38). | Low, but the Ask of #31 (`uv tool install foreman-inator` for anyone) stays unmet, and the `foreman-inator` name on PyPI stays unclaimed (a name is only held by uploading). |
| **C. Vendor the pieces used** | Copy the tracing, prompts and record code (and the `Settings` it reads) into `src/foreman_inator/_vendor/fde_kit/` with fde-kit's MIT notice, change the imports, drop the dependency, add OpenTelemetry and anything else they import directly. | Half a day. Two copies of the same code to keep in step; fixes in fde-kit no longer reach foreman-inator by themselves. | Medium: drift between the copies; the run-record row format is shared with bot-inator through fde-kit. Removes `anthropic` and other unused dependencies from the install. |
| D. Rename the dependency to a name you control and publish under it | Like A, under a different name (`fde-kit-bf`, say). | Same as A plus a rename. | Only useful if `fde-kit` turns out to be taken. It is free today. |

**Recommendation: A.** It is the one change that makes `uv tool install foreman-inator`
work for anyone, keeps one copy of the shared code, and takes the fde-kit name off the
table for squatters. Until it is done, use B (which works today) in the README.

After A, check before flipping the switch:

1. `uv lock` resolves fde-kit from PyPI (the lock's `source` for fde-kit is
   `registry`, not `git`), if you dropped the source entry.
2. In a fresh venv: `uv pip install --no-sources --default-index https://pypi.org/simple
   dist/*.whl` succeeds. The workflow's publish gate runs this same check.
3. Set `PUBLISH_ENABLED: "true"` in `release.yml` in a reviewed commit.

## Name check (2026-11-23)

Read-only checks; nothing was registered or claimed.

| Name | Where | URL checked | Result |
| --- | --- | --- | --- |
| `foreman-inator` | PyPI | https://pypi.org/project/foreman-inator/ (JSON API `https://pypi.org/pypi/foreman-inator/json`) | 404: free |
| `foreman-inator` | TestPyPI | https://test.pypi.org/pypi/foreman-inator/json | 404: free |
| `foreman-inator` | GitHub App slug | https://github.com/apps/foreman-inator | 404: no public App with that slug |
| `fde-kit` | PyPI | https://pypi.org/pypi/fde-kit/json (also `fde_kit`) | 404: free |
| `fde-kit` | TestPyPI | https://test.pypi.org/pypi/fde-kit/json | 404: free |
| `fman` | PyPI | https://pypi.org/pypi/fman/json | **Taken** (an unrelated `fman` 0.0.1). Harmless: `fman` is only a command name inside the `foreman-inator` package, not a package name. A user with that other package installed would have two `fman` commands; the PyPI name is not affected. |

Notes:

- A free name is not reserved. A PyPI "pending publisher" does not reserve it either:
  someone else who publishes first takes it and the pending publisher is dropped. Publish
  soon after setting it up.
- GitHub App names are per user: each user's `fman setup` creates their own App, and the
  manifest flow asks for a name that is free on GitHub at that moment. A 404 on
  `github.com/apps/foreman-inator` means no public App holds that slug today; a private App
  could still hold it, and `fman setup` would then need another name.

## One-time setup

### GitHub environments

Repository Settings, Environments, create two:

| Environment | Protection |
| --- | --- |
| `pypi` | Required reviewers: yourself. Deployment branches and tags: selected, tag pattern `v*`. |
| `testpypi` | Optional reviewers. No branch rule needed (manual runs come from any branch). |

### Trusted publishers

The package does not exist on either index yet, so add a **pending** publisher (PyPI:
Your account, Publishing, "Add a new pending publisher"; TestPyPI: same page on
test.pypi.org, with a separate TestPyPI account).

| Field | PyPI | TestPyPI |
| --- | --- | --- |
| PyPI Project Name | `foreman-inator` | `foreman-inator` |
| Owner | `Bforsyth1234` | `Bforsyth1234` |
| Repository name | `foreman-inator` | `foreman-inator` |
| Workflow name | `release.yml` | `release.yml` |
| Environment name | `pypi` | `testpypi` |

After the first upload a pending publisher becomes a normal one. No API token is created
or stored anywhere.

## Release checklist

1. Make sure `main` is green in CI and the fde-kit decision above is done (or accept a
   GitHub-only release).
2. Bump the version: `uv version --bump patch` (or `minor`, or an exact version such as
   `uv version 0.2.0rc1`). This edits `pyproject.toml` and `uv.lock`. Commit.
3. Changelog: the GitHub release notes are generated from merged PRs, and the release
   page is the changelog (`Changelog` URL in `pyproject.toml`). Edit the release text
   afterwards if something needs saying.
4. Optional dry run on TestPyPI: Actions, Release, "Run workflow" on the release commit.
   It builds, tests and publishes to TestPyPI only (no GitHub release). Then:
   `uv tool install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ foreman-inator`.
   TestPyPI never accepts the same version twice; use a pre-release (`0.2.0rc1`) for
   repeat tries.
5. Tag and push: `git tag v0.2.0 && git push origin v0.2.0`. The tag must equal `v` plus
   the package version, or the workflow stops.
6. Approve the `pypi` deployment when GitHub asks.
7. Check: `uv tool install foreman-inator==0.2.0` in a clean shell, then `fman --version`.

### What the workflow does

| Job | When | What |
| --- | --- | --- |
| `build` | always | `uv build --no-sources`; checks the tag equals `v<version>`; checks the wheel has every `prompts/*.md` and `db/migrations/*.sql` and no tests, and no direct URL requirements; runs the publish gate; uploads `dist/` as an artifact. |
| `smoke` | always, ubuntu and macOS | installs the locked dependencies, then the built wheel (not the source tree); `foreman-inator --version`, `fman --version`, `fman --help`; the full test suite against the installed wheel. |
| `publish-skipped` | gate closed | explains why nothing was uploaded (warning on the run page). |
| `publish-testpypi` | gate open, and a manual run or a pre-release tag (`a`, `b`, `rc`, `.dev`) | uploads to TestPyPI from environment `testpypi`. |
| `publish-pypi` | gate open, and a full version tag | uploads to PyPI from environment `pypi`. |
| `github-release` | any `v*` tag, after the tests, unless an upload failed | `gh release create` with generated notes, the wheel and sdist attached; marked pre-release for pre-release versions. Says how to install from GitHub when the package was not uploaded. |

The publish gate is open only when `PUBLISH_ENABLED` is `"true"` **and** the built wheel
resolves from PyPI alone (`uv pip install --dry-run --no-sources --no-build` against
`https://pypi.org/simple`). The first condition is a switch you flip in a reviewed commit;
the second is the stranger's install, checked on every run.

Permissions: the workflow default is none; `build` and `smoke` read contents; only the two
publish jobs get `id-token: write`; only `github-release` gets `contents: write`. Runners
are GitHub-hosted. Checkouts do not keep credentials. No dependency cache is used in this
workflow.

### Updating action pins

Third-party actions are pinned to full commit SHAs, as GitHub's hardening guide asks, with
the tag in a comment:

| Action | Tag | Commit | Tag date |
| --- | --- | --- | --- |
| `actions/checkout` | v7.0.1 | `3d3c42e5aac5ba805825da76410c181273ba90b1` | 2026-07-20 |
| `astral-sh/setup-uv` | v10.3.3 | `68559e99cc807c1eb7e9238fd43a15bba460eec2` | 2026-10-28 |
| `actions/upload-artifact` | v7.0.2 | `cf430e030ddbb5b0abf93d22962f4752f3646cd9` | 2026-10-07 |
| `actions/download-artifact` | v8.0.2 | `9000827ccba6bdab643e8b6fd33ac0654aef8333` | 2026-10-07 |
| `pypa/gh-action-pypi-publish` | v1.14.3 | `7245906fb61fa33a13554f9d5fb856fe61758086` | 2026-10-20 |

uv itself is pinned to 0.9.29 (`UV_VERSION`), the version the project builds with
(`uv_build>=0.9.0,<0.10.0`).

To update one: read the action's release notes, then

```sh
git ls-remote --tags https://github.com/OWNER/REPO 'refs/tags/vX.Y.Z*'
```

and use the commit on the `^{}` line if there is one (an annotated tag), otherwise the
only line. Replace the SHA and the comment. Or add Dependabot for the `github-actions`
ecosystem, which updates SHA pins and their comments. `ci.yml` uses tag pins
(`@v4`, `@v6`); it does not publish, so that is a smaller risk, but the same update
applies if you want it.

## Docs read for this (rule 12, 2026-11-23)

- Python Packaging User Guide, publishing with GitHub Actions:
  https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/
- PyPI trusted publishers, adding a publisher:
  https://docs.pypi.org/trusted-publishers/adding-a-publisher/
- PyPI, creating a project through OIDC (pending publishers):
  https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/
- `pypa/gh-action-pypi-publish` README: https://github.com/pypa/gh-action-pypi-publish
  (trusted publishing is not supported from a reusable workflow, so this workflow is not
  one; attestations are on by default)
- uv, building and publishing: https://docs.astral.sh/uv/guides/package/ (recommends
  `uv build --no-sources` for publishing)
- uv, dependency sources: https://docs.astral.sh/uv/concepts/projects/dependencies/
  ("Sources are only respected by uv")
- GitHub, secure use of Actions: https://docs.github.com/en/actions/reference/security/secure-use
  (full-SHA pins, least-privilege `GITHUB_TOKEN`, untrusted values through `env:`, no
  self-hosted runners on public repos)
