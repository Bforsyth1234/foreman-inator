# Configuration

The worker reads `~/.foreman-inator/config.toml` (or
`$FOREMAN_INATOR_HOME/config.toml`) when it starts and again whenever the file
changes (checked at the top of each poll). A changed value applies from the
next poll, whether you edit the file by hand, use `fman repo add` / `remove`,
or save on the metrics page's `/settings` or `/tiers`. A file that does not
validate is not applied: the worker keeps its old config and logs the error.
These fields are read once at start and need a restart
(see [setup.md](setup.md#8-stopping-restarting-and-uninstalling)); the worker
logs `<field> changed; restart the worker to apply it`: `app_id`, `app_slug`, `installation_ids`, `models`, `sandbox`, `sandbox_network`, `sandbox_install`, the `embedding_*` keys, `allow_cloud_embedder`, `max_local_inference`, `relay_url` and `relay_secret_ref`
(`RESTART_FIELDS` in `src/foreman_inator/worker_config_reload.py`).
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
| `classifier_router` | `true` | Route with a trained classifier on the issue embeddings once it beats the nearest-neighbor router on the held-out newest 20% of accepted tickets (chronological split; a tie keeps nearest-neighbor). The comparison is rechecked daily. `false`: nearest-neighbor only. |
| `classifier_min_tickets` | `300` | Accepted tickets before the classifier is trained at all. At least 1. |
| `relay_url` | none | A webhook relay (smee.io or your own; https, or http to localhost). Each delivery that verifies against the App's webhook secret triggers an immediate poll; nothing in a delivery is trusted. None: polling only. See [relay.md](relay.md). |
| `relay_min_poll_gap_seconds` | 15 | The least time (0 to 3600 s) between a poll and a relay-nudged one. Nudges inside it are coalesced into one poll when it passes, so a flood of deliveries (replayed or not) polls at most once per gap. |
| `relay_secret_ref` | none | Keychain reference of the webhook secret. None reads `keychain:foreman-inator/relay-webhook-secret`, where `fman setup --relay-url` stores it. |
| `status_comment` | `true` | Keep one comment on the issue up to date while work runs: attempt and tier, verifying, in review, PR opened, handed over, dropped. Edited in place, never one per step. A repo's settings row can turn it off. |
| `status_label` | `false` | Also add the `fman:working` label while work runs (removed when it stops). It never starts work. |
| `status_show_cost` | `false` | Put the spend so far in the status comment. Issue comments on a public repo are public. |
| `chat_local_wait_seconds` | `5.0` | Chat sessions ([`fman chat`](commands.md#fman-chat) and the `/chat` page): how long a chat turn waits for the local slot before it goes to the first cloud rung at or above its tier (never for a local-only repo, nor past a cap). 0 to 600. |
| `chat_start_tier` | `1` | Chat sessions: the tier a new session starts on. 1, 2 or 3. |
| `chat_turn_budget_usd` | `attempt_budget_usd` | Chat sessions: cloud spend one chat message may use. Above 0. |
| `chat_idle_minutes` | `30` | Chat sessions: an idle session ends after this (its worktree is kept). 1 to 1440. |
| `chat_max_sessions` | `1` | Chat sessions open at once. 1 to 16. |

## `[spend_caps]`

Cloud spend caps in US dollars. Each must be 0 or more; 0 turns that kind of
cloud call off.

| Key | Default | Meaning |
| --- | --- | --- |
| `daily_usd` | `5.0` | Live cloud spend per local calendar day: writers and the AI reviewer. |
| `monthly_usd` | `50.0` | Live cloud spend per local calendar month. |
| `experiments_monthly_usd` | `10.0` | Replays and shadow reviews per month. Never draws on the live caps. |
| `chat_monthly_usd` | `20.0` | Chat sessions' share of `monthly_usd` per month (chat spend counts toward the live caps too). At most `monthly_usd`; the default is lowered to `monthly_usd` when that is smaller. |

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
| `proposer_holdout_days` | `30` | `fman experiment propose`: the proposer reads kickbacks created before now minus this; the challenger's replay uses only tickets after it. At least 1. |
| `proposer_lookback_days` | `90` | ...and no older than that cutoff minus this. At least 1. |
| `proposer_model` | `""` | The tier 3 model that drafts prompts. `""`: the first one independent of the scoring reviewer. |
| `fork_budget_usd` | `1.0` | Cloud spend one `fman replay --fork` may use, inside the experiments budget. 0 or more. |
| `fork_writer_timeout_s` | `1200` | Time limit for one fork's writer session. Above 0. |

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
| `vision` | no | `true`: this model takes images. Images that allowed users attached to the issue (body, answers, comments) are fetched by the worker from GitHub's attachment hosts and described by a vision model (a local one first; a cloud one only when the repo allows cloud, inside the spend caps); the writer gets the descriptions as text. Default `false`. |
| `wire` | no | How the writer (OpenCode) talks to the model through the proxy: `"openai"` (default, `chat/completions`) or `"anthropic"` (Anthropic's native Messages route, `/v1/messages`, which keeps prompt caching). The reviewer and router use `chat/completions` either way. |
| `plan_usd_month` | no | A flat-fee subscription for this cloud model, in USD a month (e.g. `18.0` for the GLM Coding Plan). Its calls skip the daily, monthly and experiments caps, approvals and the per-attempt USD budget (the input-token limit and local-only repos still apply). Each call is still logged at list price, and `fman status` shows the plan's calls this month and what they would have cost metered. Unset: pay per token, counted toward the caps. |
| `plan_tier` | no | The GLM Coding Plan tier, `"lite"`, `"pro"` or `"max"` (needs `plan_usd_month`). Sets the credit limits per 5 hours and per week (Lite 2,000 / 10,000, Pro 12,000 / 60,000, Max 28,000 / 140,000). `fman status` shows the credits used, estimated locally from token counts (z.ai has no usage API). When z.ai answers a call with a quota error (HTTP 429, codes 1308–1321) the model is paused until the reset time it gives: its tickets wait (not a failed attempt, no escalation to Claude) and resume on their own. |

The defaults:

| `name` | `tier` | `kind` | `model` | `base_url` | `family` | `api_key_ref` |
| --- | --- | --- | --- | --- | --- | --- |
| `inkling-2-mini` | 1 | local | `inkling-2-mini` | `http://127.0.0.1:11434/v1` | inkling | |
| `glm` | 2 | cloud | `glm-5.4` | `https://api.z.ai/api/coding/paas/v4` | glm | `keychain:foreman-inator/glm` |
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
chat_monthly_usd = 20.0
```

This file has no `[[models]]` tables, so it uses the five default models. A
file with `[[models]]` lists every model itself, one table each:

```toml
[[models]]
name = "glm"
tier = 3
kind = "cloud"
model = "glm-5.4"
base_url = "https://api.z.ai/api/coding/paas/v4"
family = "glm"
api_key_ref = "keychain:foreman-inator/glm"
```

## Per-repo settings in the database

These settings are kept in the worker's database rather than in `config.toml`:

* the verify command, set with `fman repo set-verify <owner/name> '<command>'`
  and cleared with `--clear`;
* the repo profile, set with `fman repo set-profile` (below);
* the cloud setting, set with `fman repo set-cloud <owner/name> on|off`: `off`
  keeps the repo's code on this Mac (see [`fman repo set-cloud`](commands.md#fman-repo-set-cloud));
* `chat_network`, the chat network opt-in, set with `fman repo set-chat-network
  <owner/name> on|off` or on `/settings` (default off; chat sessions only: `on` gives
  that repo's chat sandbox the outbound policy of a first-attempt dependency install,
  ports 443/80, never your credentials; tickets are unaffected. See
  [`fman repo set-chat-network`](commands.md#fman-repo-set-chat-network)).

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
