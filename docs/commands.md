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
| [`fman approve-spend`](#fman-approve-spend) | Allow one more cloud attempt (and its review) of a ticket past the spend caps. |
| [`fman repo set-verify`](#fman-repo-set-verify) | Save the command that verifies a repo. |
| [`fman experiment run`](#fman-experiment-run) | Replay the champion against a challenger on past tickets. |
| [`fman experiment status`](#fman-experiment-status) | Champions, running and ended experiments with their results. |
| [`fman experiment promote`](#fman-experiment-promote) | Make a challenger champion if it won on replay and holds live. |
| [`fman replay`](#fman-replay) | Check that today's logic still decides a recorded ticket as it did. |
| [`fman export`](#fman-export) | Export one ticket's trace, recordings and rows for a bug report, redacted. |

## `fman setup`

Register your own GitHub App, check dependencies and install the launchd
agents. Safe to run again. Step by step: [setup.md](setup.md#2-run-fman-setup).

| Option | Meaning |
| --- | --- |
| `--uninstall-service` | Remove both launchd agents and exit. |
| `--org <org>` | Create the App under this organization. |
| `--app-name <name>` | App name (unique on GitHub, at most 34 characters). |
| `--allowed-user <login>` | GitHub login allowed to start work. Repeatable. |
| `--no-service` | Skip installing the launchd agents. |

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

## `fman status`

Tickets by column with their current attempt, flags, waits and PR numbers,
then the spend and sandbox lines. Read-only; works with the worker stopped.
What each line means: [setup.md](setup.md#6-reading-fman-status).

| Option | Meaning |
| --- | --- |
| `--closed <n>` | How many done and dropped tickets to list. Default 5. |

## `fman metrics`

Serve the metrics page on 127.0.0.1 (read-only; Ctrl-C stops it). Pages: `/`,
`/api/metrics`, `/traces`, `/failures`, `/experiments`.

| Option | Meaning |
| --- | --- |
| `--port <port>` | Local port. Default 8642. |

## `fman approve-spend`

```bash
fman approve-spend <ticket>
```

Allow one more cloud attempt (and its review) of the ticket past the spend
caps, up to one daily cap's worth. `<ticket>` is the ticket id from
`fman status`, not the issue number.

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

Example: `fman experiment run --role writer --challenger inkling-2-mini/writer-v2`.

## `fman experiment status`

```bash
fman experiment status [experiment]
```

Champions, running and ended experiments with their replay and live results.
With an experiment id, that experiment's details.

## `fman experiment promote`

```bash
fman experiment promote <experiment id | challenger variant>
```

Make a challenger champion if it won on replay and holds live. A challenger
that loses either is ended as rejected. Exits 0 only when promoted.

| Option | Meaning |
| --- | --- |
| `--replay-only` | Writers only: promote on the replay alone (no live writer check exists yet). |

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
