# foreman-inator

Label a GitHub issue and foreman-inator opens a pull request for it, using the
cheapest model that can do the job. It learns which model that is from your
reviews.

It is a GitHub App plus a worker that runs on your Mac.

## How it works

1. You add the `agent` label to an issue.
2. The worker picks the cheapest model likely to solve it, usually a local one,
   and writes the change in its own git worktree.
3. If the checks fail, it retries once, then moves up to a stronger model.
4. A model from a different family reviews the change before you see it.
5. You get a PR. Approve and merge it, or request changes to send it back with
   your comment.
6. Your verdict is recorded per model, so the routing improves over time.

## Why not a hosted coding agent

Hosted agents run every task on one cloud model. foreman-inator starts on a
local model that costs nothing per run and pays for a cloud model only when
the cheaper ones fail. It also shows how often you accept each model's work
and what each accepted ticket cost.

## Install

You need macOS, Python 3.12 or newer, [uv](https://docs.astral.sh/uv/),
git 2.31 or newer, [OpenCode](https://opencode.ai/docs/) and
[Ollama](https://ollama.com/download). There is no PyPI package yet, so
install from a clone.

1. Clone the repo:

   ```bash
   git clone https://github.com/Bforsyth1234/foreman-inator.git
   ```

2. Install the `fman` command from inside the clone:

   ```bash
   uv tool install .
   ```

3. Run setup:

   ```bash
   fman setup
   ```

   Setup does four things:

   - Registers a private GitHub App on your account. You click "Create GitHub
     App" in the browser and pick the repos to install it on. There is no
     shared App and no shared key.
   - Checks for git, OpenCode, Ollama and the local models, and prints the
     install or `ollama pull` command for anything missing. It never runs them.
   - Writes `~/.foreman-inator/config.toml`.
   - Installs a launchd agent, so the worker starts at login and restarts if
     it stops. Pass `--no-service` to skip this.

4. Store the cloud API keys in the macOS Keychain:

   ```bash
   fman config set-key glm
   ```

   ```bash
   fman config set-key anthropic
   ```

   Without these keys the worker still runs on local models. Tier 3 and the
   AI review are unavailable, so PRs are handed to you unreviewed.

The App asks for four permissions: Issues, Pull requests and Contents (read
and write) and Metadata (read). The worker polls GitHub, so it needs no public
URL and no inbound port.

## Usage

Add the `agent` label to an issue. The worker picks it up on its next poll
(every 45 seconds by default) and opens a draft PR. The PR is marked ready for
review only after the AI reviewer approves it, or after the worker hands it to
you with a comment that says why.

Your review decides the outcome:

| You do | The worker records |
| --- | --- |
| Approve and merge | Accept. It closes the issue and posts a one-line summary: model, attempts, kickbacks, cost. |
| Request changes | Kickback. Your comment goes to the next attempt as the note. |
| Close the issue without merging | Dropped. The ticket is left out of the metrics. |

### Labels

| Label | Where | Effect |
| --- | --- | --- |
| `agent` | Issue | Start work. Only labels applied by a user in `allowed_users` count. |
| `tier:1`, `tier:2`, `tier:3` | Issue | Start on that tier instead of the router's pick. |
| `review:skip` | Issue | Skip the AI review for a trivial change. |
| `kickback:wrong`, `kickback:incomplete`, `kickback:broke`, `kickback:style` | PR | Tag your kickback. `wrong` moves the writer up a tier at once. |

### Commands

| Command | What it does |
| --- | --- |
| `fman run` | Run the worker in the foreground. `--once` does one pass and exits. |
| `fman status` | Tickets by column, with the current attempt, flags and PR numbers. |
| `fman metrics` | Serve the metrics page at `http://127.0.0.1:8642`. |
| `fman approve-spend <ticket>` | Allow one more cloud attempt for a ticket that hit the spend cap. |
| `fman repo set-verify <owner/name> '<command>'` | Save the check command for a repo when none can be detected. |
| `fman config set-key <name>` | Store an API key in the Keychain. |
| `fman setup --uninstall-service` | Remove the launchd agent. |

Config, the database, traces, recordings and worktrees all live in
`~/.foreman-inator/`. Nothing leaves your Mac except calls to GitHub and to
the cloud models you configure.

## Features

### Routing and escalation

A small local model classifies each issue as small, normal or large, and the
ticket starts on the matching tier.

| Tier | Default model | Where | Used for |
| --- | --- | --- | --- |
| 1 | Inkling 2 Mini | Local, Ollama | Small, mechanical changes |
| 2 | Qwen3.8 27B | Local, Ollama | Normal features and bug fixes |
| 3 | GLM first, then Claude Sonnet | Cloud, pay per use | Large refactors, anything that failed locally |

The models are set in `config.toml`. A ticket moves up only when it has to:

- Checks fail: one retry on the same model with the error output, then up.
- You kick it back once: one retry on the same model with your note.
- You kick it back twice, or tag it `wrong`: up one tier.
- The top model fails: the ticket is flagged for you. It never loops silently.

### AI review

After the checks pass, a model from a different family than the writer reviews
the change. It sees only the issue and the diff, not the writer's reasoning.
It runs five checks, cheapest first:

1. Run the tests and the build.
2. Measure test coverage of the changed lines.
3. Revert the source change and confirm the new tests fail.
4. Run the app when it can run on its own.
5. Read the diff against the issue.

A failure in checks 1 to 4 sends the change back without asking the model.
Two AI kickbacks move the writer up one tier. The verdict is posted as a PR
comment from the bot.

### Spend caps

Cloud spend has a daily and a monthly cap, $5 and $50 by default. At the cap,
the call is refused before it is sent and the ticket waits. Run
`fman approve-spend <ticket>` to let it continue, or wait for the cap to reset.
Local model calls are free and never count.

### Metrics

`fman metrics` serves a local page, filterable by model, repo and date range:

- First-try accept rate, kickbacks per ticket and escalation rate per model
- Cost per accepted ticket, including the failed attempts
- Median time to accept, and how often the router's first pick was right
- Reviewer miss rate: your kickbacks on PRs the AI reviewer approved
- Trend lines for cost per accepted ticket and the share solved locally

### Tracing and recording

Every ticket is one OpenTelemetry trace, so when a change is kicked back you
can see the step where the model went wrong.

- All model calls go through a local OpenAI-compatible proxy. Each one becomes
  an `llm.call` span with the prompt version, tokens, latency and cost.
- The trace also holds the routing decision, each attempt, its tool calls and
  check runs, each AI review with one span per check, and the GitHub events.
- Spans are written to `~/.foreman-inator/traces/`.
- Every attempt and review is recorded in `~/.foreman-inator/recordings/`:
  the base commit, every model response and every tool result. Recordings for
  merged tickets are kept. The rest are deleted after 90 days.

Planned: a model that reads the trace after each kickback and labels the
failure step (`misread_issue`, `missed_file`, `wrong_edit`, `ignored_failure`,
`weak_tests`, `reviewer_miss`), replay of a recorded run from the trace view,
and restarting a run from its failure step with a different prompt or model.

### Experiments (planned)

Not built yet. The design: every model or prompt change is tested before it
becomes the default. A variant is a model plus a prompt version. One variant
per role is the champion, and the others are challengers.

| Mode | Tests | Your time |
| --- | --- | --- |
| Writer replay | Rerun merged issues and score against the merged PR's tests | None |
| Reviewer replay | Replay past PRs; your merges are good, your kickbacks are bad | None |
| Seeded bugs | Plant a bug in a merged PR and see if the reviewer catches it | None |
| Shadow reviewer | A challenger reviews live PRs, but only the champion's verdict counts | None |
| Traffic split | 10 to 20% of live tickets go to the challenger | Normal |
| Head-to-head | Two PRs for one issue, and you pick one | Double |

A challenger is promoted only after it wins on replay and holds up live, with
an accept rate no lower and a cost per accepted ticket no higher.

### Safety

- The worker acts only on labels applied by users in `allowed_users`.
- It pushes only to branches it created, named `fman/...`, and never runs code
  from forks or outside PRs.
- It cannot approve or merge a PR. That is always you.
- API keys live in the macOS Keychain, not in config files.
- Tickets that touch the same files wait for each other.

The worker runs model-written code on your Mac. A sandbox for that code is in
progress. Until it lands, use foreman-inator only on repos you trust.

## Status

This is a personal project with no promised support. It is MIT licensed, and
the full design lives in [SPEC.md](SPEC.md).

| Part | State |
| --- | --- |
| Setup, worker loop, routing, escalation, AI review, spend caps, metrics, tracing, recording | Built and tested against fakes. Not yet checked on real repos. |
| Sandbox, reading GitHub CI results, inline review comments, base-branch conflicts, clarifying questions | In progress |
| Learning router, experiments, failure-step labeling, replay viewer, repo profiles, bot-inator integration | Planned |
