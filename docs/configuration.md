# Configuration

The worker reads `~/.foreman-inator/config.toml` (or
`$FOREMAN_INATOR_HOME/config.toml`) once, when it starts; restart it after an
edit (see [setup.md](setup.md#8-stopping-restarting-and-uninstalling)).
`fman setup` writes the file with every key filled in, so you edit values
rather than add keys. Every key is optional; a missing key takes the default
below. The source of truth is `src/foreman_inator/config.py`.

Validation is strict: an unknown key at any level is an error, and so is any
key named like a secret (`api_key`, `key`, `token`, `secret`, `password`, ...)
or any value shaped like an API key (`sk-...`, `ghp_...`, ...). API keys live in
the macOS Keychain: store one with `fman config set-key <name>` and refer to it
as `"keychain:foreman-inator/<name>"`.

## Top-level keys

| Key | Default | Meaning |
| --- | --- | --- |
| `app_id` | none | Your GitHub App's id. Written by `fman setup`. |
| `app_slug` | none | Your App's slug; the bot account is `<slug>[bot]`. Written by `fman setup`. |
| `installation_ids` | `[]` | The App's installations. Written by `fman setup`. |
| `allowed_users` | `[]` | GitHub logins whose labels, reviews and answers count (case-insensitive). Empty means no label starts work. |
| `repos` | `[]` | Repos to poll, as `"owner/name"`. Setup adds only the repos you choose; change the list with `fman repo add` and `fman repo remove`. |
| `poll_interval_seconds` | `45` | Seconds between polls. At least 5. |
| `reviewer_models` | `["claude-opus", "glm"]` | AI reviewer preference order, by model `name`. The first whose `family` differs from the writer's is used. Each name must be in `[[models]]`. |
| `max_parallel_per_repo` | `1` | Tickets of one repo worked at the same time, as far as file locks permit. 1 to 16. 1 suits one local GPU. |
| `max_open_prs_per_repo` | `5` | Open `fman/` PRs one repo may have before the worker waits to open another (attempts still run; only the draft PR waits, and `fman status` says `WAITING: open-PR cap`). 0 = no cap. |
| `reviewer_order` | `"configured"` | How the AI reviewer is picked. `"configured"`: the first of `reviewer_models` of another family. `"cheapest"`: the cheapest cloud model (or one named in `reviewer_models`) of another family; a low-confidence verdict gets one more review by the `"configured"` pick, which decides. A local-only repo never gets that second review. |
| `split_large` | `true` | For an issue the router calls large (tier 3), one tier 3 call proposes sub-issues as a comment and the ticket is flagged `split_proposed` instead of worked. You create and label the sub-issues you want; a `tier:N` label on the issue runs it as one ticket instead. Never for a local-only repo with a cloud tier 3. |
| `split_timeout_seconds` | `180` | Time limit for that proposal call. Above 0, at most 1800. |
| `router_timeout_seconds` | `30` | Time limit for the router's classification call. Above 0, at most 600. |
| `sandbox` | `"on"` | `"on"`: OpenCode, the verify command and the reviewer's checks run under `sandbox-exec`, and nothing runs if the sandbox cannot start. `"off"`: they run unsandboxed, and `fman status` and every PR description say so. |
| `clarify` | `true` | Ask questions on a vague issue before working on it. |
| `embedding_model` | `"embeddinggemma"` | Model the nearest-neighbor router embeds issues with. Must differ from every writer model `name`. |
| `embedding_base_url` | `"http://127.0.0.1:11434/v1"` | Its OpenAI-compatible endpoint (Ollama by default). A non-localhost URL needs `allow_cloud_embedder = true`, https and `embedding_api_key_ref`. |
| `allow_cloud_embedder` | `false` | Allow sending issue text to a cloud embedding service. |
| `embedding_api_key_ref` | none | Keychain reference for a cloud embedding service's key. |
| `knn_threshold` | `50` | Accepted tickets before the nearest-neighbor router replaces the rules router. At least 1. |
| `knn_k` | `10` | Neighbors that vote. At least 1. |
| `knn_timeout_seconds` | `5` | Time limit for one routing embedding call. Above 0, at most 600. |
| `explore_rate` | `0.10` | Share of tickets started one tier below the router's pick. 0 to 1; 0 turns it off. |
| `failure_labels` | `true` | After each kickback, label the failure step with a local model, in the background. |
| `failure_label_timeout_seconds` | `30` | Time limit for one labeling call. Above 0, at most 600. |
| `allow_cloud_labeler` | `false` | Allow a cloud model as the failure labeler (by default it is the first local model). |
| `install_refill_cache` | `true` | When an install after a model edit (offline) misses the empty package cache and the dependency files are unchanged from the base commit, fill the cache once from a scratch checkout of the base commit (with network) and retry offline. `false`: flag the ticket instead. |
| `attempt_budget_usd` | `1.50` | Cloud spend one attempt may use. Over it, the attempt ends and the ticket is handed to you (`attempt_budget`), with no retry on any tier. Above 0. |
| `attempt_max_input_tokens_cloud` | `1500000` | Input tokens one attempt may send to cloud models, with the same effect. At least 1. |
| `attempt_max_input_tokens_local` | `4000000` | Input tokens one attempt may send to local models, with the same effect. At least 1. |
| `cold_load_timeout_seconds` | `120` | Extra seconds the clarity check and the router wait when Ollama says their model is not loaded yet (a cold load); a timeout while the model loads is retried once. The worker asks tier 1's local Ollama (`/api/ps`). 0 to 3600. |
| `max_local_inference` | `1` | Local-model calls in flight at once, across the worker and its experiment runs; the rest queue. Time spent queued never counts against an attempt's writer timeout. Cloud calls and embeddings are not limited. 1 to 16. |
| `retention_days` | `90` | Days `fman prune` and the worker's daily prune keep an unmerged ticket's recordings and traces and ended attempts' scratch. At least 1. |

## `[spend_caps]`

Cloud spend caps in US dollars. Each must be 0 or more; 0 turns that kind of
cloud call off.

| Key | Default | Meaning |
| --- | --- | --- |
| `daily_usd` | `5.0` | Live cloud spend per local calendar day: writers and the AI reviewer. |
| `monthly_usd` | `50.0` | Live cloud spend per local calendar month. |
| `experiments_monthly_usd` | `10.0` | Replays and shadow reviews per month. Never draws on the live caps. |

## `[sandbox_network]`

Per repo, whether the sandbox lets that repo's verify command and the
reviewer's checks reach the network. The writer (OpenCode) can only ever reach
the model proxy. Repos not listed get `"none"`.

```toml
[sandbox_network]
"owner/name" = "allow"   # or "none"
```

`"allow"` is needed today for a repo whose checks install dependencies (todo
#45). It also lets check code send anything it can read.

## `[rules_file]`

Per repo, one more rules file put into the writer's prompt (a path; `~` is
expanded; use an absolute path). Repos not listed have none.

```toml
[rules_file]
"owner/name" = "~/notes/owner-name-rules.md"
```

## Rules the writer is given

Every attempt's prompt carries the rules from these sources, in this order:

1. `~/.foreman-inator/AGENTS.md`: your rules for every repo. It is kept at
   mode 600 (a looser mode is tightened when read).
2. The repo's `[rules_file]`, if set.
3. The repo's root `AGENTS.md` and `CLAUDE.md`, both of them. A file that is a
   symlink of, or identical to, an earlier one is skipped.
4. `AGENTS.md` / `CLAUDE.md` in the folders on the path of the files the issue
   names, shallow folders first.

The prompt tells the model that a repository file wins where it disagrees with
your global or per-repo rules. All sources share one budget of 6,000
characters. When they do not fit, the global file is cut first, then the
`[rules_file]`, then nested files (deepest first), then `CLAUDE.md`, and the
root `AGENTS.md` last; each cut is logged as a warning. Repo files are read only
from inside the worktree. The attempt's trace span records the hash of the
block the model saw (`conventions.sha256`).

A `reviewer-v2` review gets the same block, built from the same sources as they
are at the change's base commit (so a writer's edit to `AGENTS.md` never reaches
its own reviewer); `reviewer-v1` shows no rules.

## `[experiments]`

Replays, the shadow reviewer and the nightly run.

| Key | Default | Meaning |
| --- | --- | --- |
| `set_size` | `100` | Latest merges a replay set is built from. At least 1. |
| `run_budget_usd` | `2.0` | Cloud spend one replay run may use, inside the experiments budget. 0 or more. |
| `writer_timeout_s` | `1200` | Time limit for one writer replay side. |
| `verify_timeout_s` | `600` | Time limit for scoring one writer side with the merged tests. |
| `review_timeout_s` | `1800` | Time limit for one replayed review. |
| `min_cases` | `10` | Cases scored on both sides before a replay gives a verdict. At least 1. |
| `live_min_sample` | `30` | Paired PRs you decided before a shadowed reviewer is judged live. At least 1. |
| `shadow` | `true` | Run a running shadow experiment's challenger on live PRs. |
| `shadow_timeout_s` | `900` | Time limit for one shadow review. |
| `nightly_hour` | `2` | Local hour (0–23) of the nightly `fman experiment run --all`. Takes effect when `fman setup` is run again. |
| `nightly_minute` | `0` | Its minute (0–59). |

Timeouts are in seconds and must be above 0.

## Models

`[[models]]` is the list of writer and reviewer models, one table each. It
replaces the default list as a whole, and every tier (1, 2 and 3) needs at
least one model. Within a tier, the order in the file is the try order, and
every model is one rung of the escalation ladder.

| Key | Required | Meaning |
| --- | --- | --- |
| `name` | yes | Unique handle used in config, attempts and metrics, for example `"inkling-2-mini"`. |
| `tier` | yes | 1, 2 or 3. |
| `kind` | yes | `"local"` (on this Mac; must be a localhost URL; free) or `"cloud"` (https, needs `api_key_ref`, counts toward the spend caps). |
| `model` | yes | The model id sent to the endpoint. |
| `base_url` | yes | OpenAI-compatible base URL. |
| `family` | yes | For example `"qwen"` or `"claude"`. The AI reviewer must be from a different family than the writer. |
| `api_key_ref` | for cloud | `"keychain:foreman-inator/<name>"`. |
| `wire` | no | How the writer (OpenCode) talks to the model through the proxy: `"openai"` (default, `chat/completions`) or `"anthropic"` (Anthropic's native Messages route, `/v1/messages`, which keeps prompt caching). The reviewer and router use `chat/completions` either way. |

The defaults:

| `name` | `tier` | `kind` | `model` | `base_url` | `family` | `api_key_ref` |
| --- | --- | --- | --- | --- | --- | --- |
| `inkling-2-mini` | 1 | local | `inkling-2-mini` | `http://127.0.0.1:11434/v1` | inkling | |
| `glm` | 2 | cloud | `glm-5.3` | `https://api.z.ai/api/coding/paas/v4` | glm | `keychain:foreman-inator/glm` |
| `claude-opus` | 3 | cloud | `claude-opus-5-5` | `https://api.anthropic.com/v1` | claude | `keychain:foreman-inator/anthropic` |
| `claude-fable` | 3 | cloud | `claude-fable-5-2` | `https://api.anthropic.com/v1` | claude | `keychain:foreman-inator/anthropic` |

Tier 2 is GLM, open-weights cloud, since todo #70 (it was `qwen3.8-27b`, local,
before). A config file that lists its own `[[models]]` keeps its ladder as
written, so a file with Qwen at tier 2 loads unchanged. To keep tier 2 local,
list Qwen as tier 2 (and, if you like, GLM back at tier 3 ahead of Claude):

```toml
[[models]]
name = "qwen3.8-27b"
tier = 2
kind = "local"
model = "qwen3.8:27b"
base_url = "http://127.0.0.1:11434/v1"
family = "qwen"
```

Because `[[models]]` replaces the whole list, add the other tiers' tables too.
With the defaults, the AI reviewer of a GLM writer is Claude Opus and of a
Claude writer is GLM (`reviewer_models`), so the reviewer is always of another
family.

The default Claude models have `wire = "anthropic"`: the writer reaches them on
the proxy's Anthropic Messages route, so prompt caching works and cached input
tokens are priced at Anthropic's cache-read and cache-write rates (traced on the
`llm.call` span and in the recording's usage). Other models use `"openai"`.

## Example

```toml
app_id = 123456
app_slug = "foreman-inator-a1b2c3"
installation_ids = [7890]
allowed_users = ["you"]
repos = ["you/project"]
poll_interval_seconds = 45
reviewer_models = ["claude-opus", "glm"]
sandbox = "on"

[sandbox_network]
"you/project" = "none"

[spend_caps]
daily_usd = 5.0
monthly_usd = 50.0
experiments_monthly_usd = 10.0
```

This file has no `[[models]]` tables, so it uses the five default models. A
file with `[[models]]` lists every model itself, one table each:

```toml
[[models]]
name = "glm"
tier = 3
kind = "cloud"
model = "glm-5.3"
base_url = "https://api.z.ai/api/coding/paas/v4"
family = "glm"
api_key_ref = "keychain:foreman-inator/glm"
```

## Per-repo settings in the database

Three settings are kept in the worker's database rather than in `config.toml`:

* the verify command, set with `fman repo set-verify <owner/name> '<command>'`
  and cleared with `--clear`;
* the repo profile, set with `fman repo set-profile` (below);
* the cloud setting, set with `fman repo set-cloud <owner/name> on|off`: `off`
  keeps the repo's code on this Mac (see [`fman repo set-cloud`](commands.md#fman-repo-set-cloud)).

### Repo profiles

A profile is two modes. `writer` sets how hard the writer tries:

| `writer` | Effect |
| --- | --- |
| `thrifty` | 3 failed checks on a tier before moving up; a cloud writer attempt waits for `fman approve-spend`; 25% of tickets start one tier lower. |
| `normal` | Today's rules: 2 failed checks per tier; `explore_rate` applies. |
| `fast` | Starts one configured tier above the router's pick (capped at the top tier); the first failed check moves up; never starts lower. |

`review` sets how much review a change gets before the PR is marked ready:

| `review` | Effect |
| --- | --- |
| `off` | No review: checks pass, the PR is marked ready and handed to you, as with `review:skip`. |
| `light` | The reviewer's tests, coverage and revert checks only; no model reads the diff. |
| `normal` | Today's AI review; `review:skip` counts. |
| `strict` | The full AI review; `review:skip` is ignored; one AI kickback moves up a tier. |

Presets: `prototype` = fast + off, `personal` = thrifty + light, `default` =
normal + normal, `production` = fast + strict. A `tier:N` label is never
shifted, and user kickbacks work the same in every mode. A repo with no profile
is `default`. `fman status` lists every repo whose profile is not `default`,
and that repo's PR descriptions carry a `Repo profile:` line. A change takes
effect on the worker's next decision.
