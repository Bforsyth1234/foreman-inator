# Commands

Every command is available as `fman` and as `foreman-inator`. This list is
taken from the commands' own `--help`; run `fman <command> --help` for the
current text. `fman --version` prints the version.

| Command | What it does |
| --- | --- |
| [`fman setup`](#fman-setup) | Register your own GitHub App, check dependencies and install the launchd agents. |
| [`fman config set-key`](#fman-config-set-key) | Store an API key in the macOS Keychain. |
| [`fman run`](#fman-run) | Run the worker: poll GitHub, work labeled issues, open PRs. |
| [`fman status`](#fman-status) | Tickets by column with their current attempt, flags and PR numbers. |
| [`fman metrics`](#fman-metrics) | Serve the metrics page on 127.0.0.1. |
| [`fman chat`](#fman-chat) | Interactive coding sessions through the same tiers, caps and sandbox as tickets. |
| [`fman approve-spend`](#fman-approve-spend) | Allow one more cloud attempt (and its review) of a ticket past the spend caps. |
| [`fman repo add`](#fman-repo-add) | Add repos to the list the worker polls. |
| [`fman repo remove`](#fman-repo-remove) | Take repos out of the list the worker polls. |
| [`fman repo list`](#fman-repo-list) | The repos in `config.toml`, marking any the installation no longer covers. |
| [`fman repo set-verify`](#fman-repo-set-verify) | Save the command that verifies a repo. |
| [`fman repo set-profile`](#fman-repo-set-profile) | Set a repo's profile: how hard the writer tries and how much review a change gets. |
| [`fman repo set-cloud`](#fman-repo-set-cloud) | Allow or forbid cloud models for a repo (off: local only). |
| [`fman repo set-chat-network`](#fman-repo-set-chat-network) | Open or close network access for a repo's chat sessions. |
| [`fman rules show`](#fman-rules-show) | Print the writer's and reviewer's rules blocks for a repo, as built. |
| [`fman sweep`](#fman-sweep) | Label a repo's open issues for the worker in waves, after a preview. |
| [`fman experiment run`](#fman-experiment-run) | Replay the champion against a challenger on past tickets. |
| [`fman experiment status`](#fman-experiment-status) | Champions, running and ended experiments with their results. |
| [`fman experiment promote`](#fman-experiment-promote) | Make a challenger champion if it won on replay and holds live. |
| [`fman experiment split`](#fman-experiment-split) | Send a share of first attempts to a writer challenger (the live check). |
| [`fman experiment propose`](#fman-experiment-propose) | Draft the next prompt of a role's champion from recent kickbacks (you approve or discard it). |
| [`fman experiment approve-draft`](#fman-experiment-approve-draft) | Approve a drafted prompt: register it as a challenger and run its replay. |
| [`fman experiment discard-draft`](#fman-experiment-discard-draft) | Discard a drafted prompt and remove its files. |
| [`fman replay`](#fman-replay) | Check that today's logic still decides a recorded ticket as it did; `--fork` restarts an attempt at its failure step. |
| [`fman retry`](#fman-retry) | Retry a flagged or dropped ticket, keeping its history. |
| [`fman export`](#fman-export) | Export one ticket's trace, recordings and rows for a bug report, redacted. |
| [`fman prune`](#fman-prune) | Delete old recordings, traces and scratch of unmerged tickets. |

## `fman setup`

Register your own GitHub App, check dependencies and install the launchd
agents. Safe to run again. Repos are never added on their own: on a first run
setup asks which ones to work on (or takes `--repo`), and a re-run leaves the
`repos` list as it is. Step by step: [setup.md](setup.md#2-run-fman-setup).

| Option | Meaning |
| --- | --- |
| `--uninstall-service` | Remove both launchd agents and exit. |
| `--org <org>` | Create the App under this organization. |
| `--app-name <name>` | App name (unique on GitHub, at most 34 characters). |
| `--allowed-user <login>` | GitHub login allowed to start work. Repeatable. |
| `--no-service` | Skip installing the launchd agents. |
| `--repo <owner/name>` | A repo to work on. Repeatable. Replaces the `repos` list, with no question. |
| `--relay-url <url>` | A webhook relay (https; http only for localhost) whose deliveries trigger an early poll. A new App gets its webhook pointed there and its webhook secret is stored in the Keychain (`relay-webhook-secret`); for an existing App, see [relay.md](relay.md). |
| `--public` | Let other GitHub accounts install your App; the key stays on this Mac. Off by default (the App is private). Applies only when setup creates the App: it then prints the install link to share. With an App that already exists, it says to change the visibility on GitHub instead. Read [publishing.md](publishing.md) first. |

## `fman config set-key`

```bash
fman config set-key <name>
```

Prompts for the key (not echoed) and stores it in the macOS Keychain under the
service `foreman-inator` as `<name>`, for example `glm` or `anthropic`. Prints
the `api_key_ref` to use in `config.toml`.

## `fman run`

Run the worker in the foreground: poll GitHub, work labeled issues, open PRs.
Ctrl-C stops it cleanly. The launchd agent installed by `fman setup` runs this
for you; do not run a second one beside it.

| Option | Meaning |
| --- | --- |
| `--once` | One poll-and-work pass (one job per repo), then exit. |

**The `ab:pair` label (head-to-head).** Add `ab:pair` together with `agent`
(or later, until the ticket is done) and the worker opens two PRs for the
issue, written by two different models: side A is the normal ticket, side B
runs the opponent (a running writer experiment's challenger on the same tier,
else the next rung up the ladder, else the rung below). Merge the one you
prefer; the worker closes the other PR and deletes its `fman/` branch, and the
result counts in `fman metrics` and experiment results (it never gates
promotion). Closing one PR alone loses that side; the other carries on.
Removing `ab:pair` drops side B and closes its PR. Only a label applied by an
allowed user counts; `pair_enabled = false` ignores it with one comment.

## `fman status`

Tickets by column with their current attempt, flags, waits and PR numbers,
then the repo count, spend and sandbox lines, the Ollama line (`Ollama: N loaded
(X GB)` or `Ollama: unreachable (...)`, when a model is local) and one line per
installed plugin (or `Plugins: none`). Read-only; works with the worker stopped.
What each line means: [setup.md](setup.md#6-reading-fman-status).

The spend line ends with the chat figure: `chat $x today, $y of $z this month
(tickets $a today, $b this month)`, with `[chat sub-cap reached]` once chat has
used `chat_monthly_usd`. A ticket whose spend approval was cut short (a restart
or cancel mid-attempt) shows `SPEND: approval #N: $left of $limit left (carried
to the next attempt)` under its line: the worker's startup recovery carries an
approval with room left to the ticket's next attempt instead of asking again.

| Option | Meaning |
| --- | --- |
| `--closed <n>` | How many done and dropped tickets to list. Default 5. |

## `fman metrics`

Serve the metrics page on 127.0.0.1 (Ctrl-C stops it). Pages: `/`,
`/api/metrics`, `/traces`, `/failures`, `/experiments`, `/resources` (the
models Ollama holds, memory and local slots, from the snapshot the worker
writes once per poll) and `/experiments/manage` (start, stop, cancel, propose
and promote experiments), `/activity` (what the agents are working on now:
running attempts with model, step, elapsed time and cost, the `fman status`
queue, and a **Retry** button on each flagged ticket that does what
`fman retry <ticket> [--up]` does), `/settings` (edit spend caps, timeouts,
`allowed_users`, repos and per-repo settings; a save is validated like
`config.load` and the running worker picks it up on its next poll, except the
fields listed under [configuration.md](configuration.md)) and `/tiers` (models
and providers per tier, reviewer order, a Test button per endpoint; API keys go
to the Keychain and are never shown) and `/chat` (start, follow and finish chat
sessions; see [`fman chat`](#fman-chat)). The main page has a "Spend: tickets ·
chat · experiments" block for the range, with chat's share of this month against
`chat_monthly_usd`. Every page lists all the others in its nav. Buttons are the
only writes, each behind a form token and a loopback check.

`/chat` is the one page with a script: a small inline one (per-response nonce,
strict `Content-Security-Policy`) that streams the reply over server-sent events
(`/chat/<id>/events`) and inserts model text as plain text. Every button is a
plain form too, so the page works with JavaScript off. The page's chat session
lives in the `fman metrics` process: stopping the page ends it (locks released,
worktree kept; `fman chat resume <id>` or the page continues it).

| Option | Meaning |
| --- | --- |
| `--port <port>` | Local port. Default 8642. |

## `fman chat`

```bash
fman chat <owner/name> [--tier N] [--base BRANCH]
fman chat list
fman chat resume <id>
fman chat pr <id> [--title TITLE]
fman chat discard <id> [--yes]
```

An interactive coding session in its own worktree on branch `fman/chat/<id>`,
cut from the watched repo's base branch (never a ticket's `fman/` branch). It
runs OpenCode through the same model ladder, proxy, spend caps and sandbox as a
ticket, and starts on tier 1 (local) unless `--tier` or `chat_start_tier` says
otherwise. Each line you type is one message (one turn); lines starting with `/`
are commands:

| Command | Meaning |
| --- | --- |
| `/tier up`, `/tier down`, `/tier N` | Move the session's tier; the next message uses it. |
| `/model`, `/status`, `/cost` | The current model, the session's state, and `session $x · chat this month $y of $z · month $m of $M`. |
| `/approve` | After a cap refusal: allow the refused message once, up to one approval's worth. |
| `/verify` | Run the repo's verify command in the verify sandbox. |
| `/pr [title]` | Verify (a failure leads the PR body, it does not block), push the branch and open a **draft** PR (no AI review; nothing merges). |
| `/discard` | Delete the worktree and branch; nothing is pushed (asks to confirm). |
| `/abort` | Stop the running turn (Ctrl-C in the terminal does the same). |
| `/help`, `/quit` | Help; end the session here (worktree kept). |

Spend: chat counts toward `monthly_usd` and at most `chat_monthly_usd` of it may
be chat; one message may spend at most `chat_turn_budget_usd`. A chat turn goes
ahead of queued ticket calls for the local GPU; if it would wait more than
`chat_local_wait_seconds`, that turn uses the first cloud model at or above the
session's tier (never for a local-only repo) and says so. Chat sessions take
file locks on the files they edit, so tickets wait instead of conflicting;
`/pr`, `/discard` and the end of the session release them. One open session at
a time (`chat_max_sessions`). Chats never feed the router or replays.

`fman chat list` shows every session with its state, tier, model and cost;
`resume` reopens an ended one in its kept worktree with its history; `pr` and
`discard` do what `/pr` and `/discard` do. The `/chat` page of
[`fman metrics`](#fman-metrics) is the same engine in the browser. Whatever you
paste goes to the model and the session's recording on this Mac.

## `fman approve-spend`

```bash
fman approve-spend <ticket>
```

Allow one more cloud attempt (and its review) of the ticket past the spend
caps, up to one daily cap's worth. `<ticket>` is the ticket id from
`fman status`, not the issue number.

## `fman repo add`

```bash
fman repo add <owner/name> [<owner/name> ...]
```

Add repos to the list the worker polls (`repos` in `config.toml`). Each one is
checked against what the App's installation can see. If any is not visible,
nothing is added, and the command prints the installation's settings URL to
grant access there. Without App credentials (before `fman setup`) the check is
skipped, and the command says so. The running worker picks the change up on
its next poll; no restart is needed.

| Option | Meaning |
| --- | --- |
| `--no-check` | Add without checking that the App's installation can see it. |

## `fman repo remove`

```bash
fman repo remove <owner/name> [<owner/name> ...]
```

Take repos out of the list the worker polls. Their tickets stay in the
database and nothing on GitHub changes. From its next poll the running worker
stops polling the repo and starts no work on its tickets. The command prints
how many open tickets each removed repo has. It exits 1 when
none of the names was in the list.

## `fman repo list`

The repos in `config.toml`. When the App credentials are present, it marks
any repo the installation no longer covers (polling it fails) and counts the
repos the installation can see that are not listed. Read-only.

| Option | Meaning |
| --- | --- |
| `--available` | Also list the repos the installation can see but not configured. |
| `--no-check` | Do not ask GitHub what the installation covers. |

## `fman repo set-verify`

```bash
fman repo set-verify <owner/name> '<command>'
fman repo set-verify <owner/name> --clear
```

Save the shell command that runs a repo's checks, used when none can be
detected. Tickets flagged for a missing command continue on the worker's next
pass.

| Option | Meaning |
| --- | --- |
| `--clear` | Forget the saved command. |

## `fman repo set-profile`

```bash
fman repo set-profile <owner/name> <preset>
fman repo set-profile <owner/name> [<preset>] [--writer W] [--review R]
```

Set a repo's profile. `<preset>` is `prototype`, `personal`, `default` or
`production`; without one, the repo's current profile is the start. The
options then change one mode. An unknown name is refused with the valid ones,
and nothing is saved. The worker uses the new profile from its next decision.
See [Repo profiles](configuration.md#repo-profiles).

| Option | Meaning |
| --- | --- |
| `--writer` | `thrifty`, `normal` or `fast`. |
| `--review` | `off`, `light`, `normal` or `strict`. |

## `fman repo set-cloud`

```bash
fman repo set-cloud <owner/name> on|off
```

`off` makes the repo local only: its code and issue text never go to a cloud
model. Escalation stops at the last local rung and hands the ticket to you
(flag `local_only`); the AI reviewer is a local model of another family, or the
review is skipped with a note on the PR; the spend gate refuses cloud calls for
it; kNN routing and the embedding backfill do not use a cloud embedder; shadow
reviews, replays with a cloud model and the failure labeler leave it out.
`fman status` lists local-only repos. `on` (the default) undoes it.

## `fman repo set-chat-network`

```bash
fman repo set-chat-network <owner/name> on|off
```

Chat sessions ([`fman chat`](#fman-chat)) run sandboxed with no network by
default. `on` gives that repo's chat sandbox the same outbound policy
as a first-attempt dependency install (ports 443/80), never your credentials or
Keychain. Tickets are unaffected. `off` (the default) closes it again. The
`/settings` page has the same switch per repo.

## `fman rules show`

```bash
fman rules show <owner/name> [--ref REF] [--file PATH]...
```

Print the rules blocks the writer and the reviewer get for the repo, exactly as
built: each source (global `AGENTS.md`, the repo's `AGENTS.md` / `CLAUDE.md`,
nested files on the `--file` paths, the `rules_file`) with its size, and
`CLIPPED at N chars` where the shared budget cut it. `--ref` reads another
commit or branch. No model call. Exit 1 when the repo has no local mirror yet.

## `fman sweep`

```bash
fman sweep <owner/name> [--label L] [--exclude-label L]... [--author A]
           [--limit N] [--include-any-author] [--dry-run] [--yes]
```

Label a repo's open issues for the worker, as you, through the GitHub CLI
(`gh auth login` first; the App's token is never used, since the worker ignores
labels the bot applies). It prints a preview first: each issue's tier from the
local router (a `tier:N` label wins), file overlaps grouped into waves, and the
spend estimate. Issues already worked, with a `fman/` PR, or by authors not on
`allowed_users` (unless `--include-any-author`) are skipped. `--dry-run` stops
after the preview; `--yes` labels without asking. New PRs then wait at
`max_open_prs_per_repo`.

## `fman experiment run`

Replay the champion against a challenger on past tickets. Resumable; Ctrl-C
stops it. It runs beside the worker with its own model proxy and repo mirrors,
and its costs go to the experiments budget only. A reviewer challenger also
starts shadow-reviewing live PRs. Nothing is promoted.

| Option | Meaning |
| --- | --- |
| `--role <role>` | `writer` or `reviewer`. |
| `--challenger <variant>` | `MODEL/PROMPT@HASH8`, `MODEL/PROMPT` (the prompt file as it is now) or `MODEL`. |
| `--all` | Resume every unfinished replay (what the nightly agent runs). |
| `--mode <mode>` | `replay` (default) or `seeded`: reviewers only; merged cases with a planted bug (a dropped `if` guard, a neutralised check, a flipped comparison) that each reviewer should kick back. Reports each side's catch rate. |
| `--seed <n>` | `--mode seeded`: the mutation seed (a resumed run keeps its own). |
| `--kinds <kind>` | `--mode seeded`: only these mutation kinds (repeatable). |
| `--rules <file>` | Replay this rules file in place of the repo's rules, on the challenger side only (same model and prompt on both sides, so the run compares the rules). `--challenger` may then be omitted. The file's hash is part of the challenger's id (`...+rules@HASH8`). A reviewer replay shows rules only with a `reviewer-v2` prompt. |

Example: `fman experiment run --role writer --challenger inkling-2-mini/writer-v2`.
Rules: `fman experiment run --role writer --rules ~/notes/new-AGENTS.md`.

## `fman experiment status`

```bash
fman experiment status [experiment]
```

Champions, running and ended experiments with their replay and live results.
With an experiment id, that experiment's details, including its origin (you,
or an LLM-drafted prompt you approved) and its cutoff.

## `fman experiment promote`

```bash
fman experiment promote <experiment id | challenger variant>
```

Make a challenger champion if it won on replay and holds live. A challenger
that loses either is ended as rejected. Exits 0 only when promoted.

| Option | Meaning |
| --- | --- |
| `--replay-only` | Writers only: promote on the replay alone, without the live check. Otherwise a writer holds live through its running `fman experiment split`. |

## `fman experiment split`

```bash
fman experiment split start <variant> [--share 0.15]
fman experiment split stop <experiment id>
fman experiment split status [experiment id]
```

`start`: a stable share (default 15%) of new tickets' first attempts go to the
writer challenger, which must have won its replay; the champion writes the
rest. `status`: the split's share and each side's live accept rate and cost per
accepted ticket. `stop`: end it as rejected (its attempts are kept).
`fman experiment promote` judges a running split as the "holds live" check.

## `fman experiment propose`

```bash
fman experiment propose --role writer|reviewer [--proposer <model>]
```

A tier 3 model drafts the next prompt of the role's champion from the
patterns in recent kickbacks: those created before the cutoff (now minus
`experiments.proposer_holdout_days`, default 30) and no older than
`proposer_lookback_days` before it. Prints the draft's path, its rationale and
the estimated cost. Nothing is registered. The proposer must not be the
reviewer that scores the challenger (nor of its family); `--proposer` (or
`experiments.proposer_model`) picks it. One LLM-drafted challenger per role at
a time. The call is costed to the experiments budget. The metrics page's
"Propose an experiment" button does the same.

## `fman experiment approve-draft`

```bash
fman experiment approve-draft <draft id>
```

Install the draft (as you left the file), register it as a challenger with the
champion's model, and run its replay exactly as `fman experiment run --role R
--challenger V` would, on tickets created after the draft's cutoff only.
Nothing is promoted: `fman experiment promote` is still the only way.

## `fman experiment discard-draft`

```bash
fman experiment discard-draft <draft id>
```

Mark the draft discarded and remove its files.

## `fman replay`

```bash
fman replay <ticket>
```

Step through a recorded ticket and check that today's router, escalation,
review policy and lifecycle still decide as they did. No model calls. Exits 1
on any difference.

| Option | Meaning |
| --- | --- |
| `--recordings <path>` | Recordings folder (default: yours). |
| `--traces <path>` | Traces folder (default: yours). |
| `--rows <path>` | Stored rows as JSON (a checked-in regression case). |
| `--fork <attempt>` | Instead: restart that attempt at its labelled failure step with `--model` and/or `--prompt`, in a scratch worktree under the sandbox (never pushed). Its recorded file edits before that step are re-applied; the result is recorded next to the attempt (`<attempt>.fork_<n>.jsonl.gz`) and shown in the trace view. Model calls go to the experiments budget, at most `experiments.fork_budget_usd` per fork. Exits 2 when it cannot start. |
| `--model <name>` | With `--fork`: the model to continue with. |
| `--prompt <name>` | With `--fork`: a writer prompt to rebuild the prompt with. |
| `--step <n>` | With `--fork`: restart at this step instead of the labelled one. |

## `fman retry`

```bash
fman retry <owner/repo#issue | owner/repo#issue:a|b | ticket id> [--tier N | --up]
```

For an `ab:pair` issue, `owner/repo#issue` is ambiguous: name the side as
`owner/repo#issue:a` or `owner/repo#issue:b`, or by its ticket id (`fman
status` lists both). A dropped side of a pair is not put back.

Retry a flagged or dropped ticket, keeping its history: the same ticket, its
attempts, traces, reviews and PR branch. Same tier by default; `--tier N` or
`--up` moves it. The worker picks it up on its next pass; the retry gets one
more round (at most two attempts), then the ticket comes back to you, and a
ticket can be retried at most a few times. Refused (exit 1, with the reason)
for a merged ticket, a PR waiting on your review, a flag a retry cannot fix
(for example a missing verify command: `fman repo set-verify`), a spend cap
still reached, or a cloud tier on a local-only repo. Writes nothing to GitHub.

## `fman export`

```bash
fman export <ticket> --redact
```

Export one ticket's trace, recordings and store rows for a bug report. Your
code, prompts, tool output, notes, repo names and paths are replaced
(allow-list redaction); only structure, timings, counts, models and decisions
are kept. `--redact` is required: there is no raw mode.

| Option | Meaning |
| --- | --- |
| `--redact` | Required: replace your code and text. |
| `--output`, `-o <path>` | A `.tar.gz` or `.tgz` file, or an empty folder. Default: `~/Downloads`. |
| `--recordings <path>` | Recordings folder (default: yours). |
| `--traces <path>` | Traces folder (default: yours). |
| `--db <path>` | Database (default: yours; opened read-only). |

Read the export before you attach it anywhere.

## `fman prune`

```bash
fman prune [--dry-run] [--days <n>]
```

Delete unmerged tickets' recordings and traces, and ended attempts' scratch
(sandbox, OpenCode and replay work folders), older than the retention, plus the
package caches of repos no longer in `config.toml`. Merged tickets' recordings
are kept. The worker runs the same prune once a day.

| Option | Meaning |
| --- | --- |
| `--dry-run` | List what would be deleted; delete nothing. |
| `--days <n>` | Retention in days. At least 1. Default: `retention_days` (90). |
