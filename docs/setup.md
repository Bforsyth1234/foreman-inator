# Setting up foreman-inator

This guide takes you from nothing to a first pull request. It describes what
the code does. Most of it has only been run against fakes in the test suite;
the live checks still open are listed in [todo.md](../todo.md#live-checks), and
the steps most likely to differ on a real Mac are marked **Not yet checked live**.

Terms used below:

- **Ticket:** the worker's record of one labeled issue.
- **Attempt:** one run of one model on a ticket: write the change, then run the checks.
- **Tier:** one of three cost levels: 1 and 2 are local models, 3 is cloud models.
- **Kickback:** a change sent back, by you (request changes) or by the AI
  reviewer, with a note and optionally a tag.
- **Hand-over:** the worker stops working on a ticket and gives it to you,
  with a comment on the issue that says why.

## Prerequisites

| What | Why | How to get it |
| --- | --- | --- |
| macOS | The sandbox uses `sandbox-exec`; the service uses launchd; keys use the Keychain | |
| git 2.31 or newer | One git worktree per attempt | `xcode-select --install` or `brew install git` |
| [uv](https://docs.astral.sh/uv/) | Installs the `fman` command (and Python 3.12+ if needed) | see the uv docs |
| [OpenCode](https://opencode.ai/docs/) | The coding agent each attempt runs | `curl -fsSL https://opencode.ai/v2/install \| bash` or `brew install anomalyco/tap/opencode-v2` |
| [Ollama](https://ollama.com/download) | Serves the local models | `brew install ollama` or the app |
| The local models | Tiers 1 and 2, the router and the clarify check | `ollama pull inkling-2-mini` and `ollama pull qwen3.8:27b` |
| `embeddinggemma` (optional at first) | The nearest-neighbor router, which starts after 50 accepted tickets | `ollama pull embeddinggemma` |
| Cloud API keys (optional) | Tier 3 writers and the AI reviewer | a Z.ai GLM Coding Plan key and an Anthropic API key |

The install commands for git, OpenCode and Ollama are the ones `fman setup`
prints when a tool is missing. The OpenCode driver was built and smoke-tested
against OpenCode 1.18.7. **Not yet checked live:** whether the version the
`opencode-v2` install command gives you still accepts the configuration the
driver writes (todo #52 notes that OpenCode's docs now show a newer schema).

SPEC.md notes that Inkling 2 Mini needs Ollama 0.40.17 or newer and is about
20 GB. Start Ollama before running setup, so setup can see which models are
pulled.

Without cloud keys the worker still runs on the local models. A ticket that
reaches tier 3 fails there and ends with a hand-over, and the AI review cannot
make its model call, so PRs reach you without an AI verdict (the reviewer's
code checks still run).

## 1. Install the `fman` command

The package is not on PyPI. Once the code is merged into the repo's `main`
branch, install it straight from GitHub:

```bash
uv tool install git+https://github.com/Bforsyth1234/foreman-inator
```

At the time of writing `main` holds only the spec and the plan; the code is on
the stacked `wave-*` branches waiting to be merged, and the command above fails
with "does not appear to be a Python project". Until the merge, install from a
checkout of the latest wave branch:

```bash
git clone https://github.com/Bforsyth1234/foreman-inator.git
cd foreman-inator
git checkout <latest wave branch>
uv tool install .
```

Both forms take the fde-kit dependency from its git repo
(`https://github.com/Bforsyth1234/fde-kit`), as `pyproject.toml` says.

Check it worked:

```bash
fman --version
```

`foreman-inator` is the same command under its long name. If your shell cannot
find `fman`, run `uv tool update-shell` and open a new terminal.

Run the installed command, not `uv run fman` from a checkout: the worker's own
virtual environment then leaks into the sandboxed attempts (todo #54).

## 2. Run `fman setup`

```bash
fman setup
```

Options (see [commands.md](commands.md#fman-setup)):

- `--allowed-user <login>`: a GitHub login allowed to start work. Repeatable.
  When the App is created on your personal account, your login is added
  automatically.
- `--org <org>`: create the App under an organization instead. Setup then asks
  for an allowed login, because the organization itself cannot apply labels.
- `--app-name <name>`: the App's name (at most 34 characters; App names are
  unique across GitHub). The default is `foreman-inator-` plus six random hex
  characters, so the bot account is `<that name>[bot]`.
- `--no-service`: skip the launchd agents.

What happens, in order:

1. **Create the App.** The terminal prints
   `Opening your browser to create the App: http://127.0.0.1:<port>/start`.
   That local page forwards to GitHub's "Create GitHub App" page with the
   settings filled in. Click **Create GitHub App**. GitHub sends the browser
   back to the local page, which says "App created. You can close this tab and
   return to the terminal." You have 30 minutes. The App is private, has no
   webhook (the worker polls), and asks for these permissions:

   | Permission | Access | Used for |
   | --- | --- | --- |
   | Issues | Read and write | Read labeled issues, post questions, hand-overs and summaries |
   | Pull requests | Read and write | Open draft PRs, mark them ready, post the AI review, read your reviews |
   | Contents | Read and write | Push the `fman/...` branches |
   | Metadata | Read | Required by GitHub for every App |
   | Checks | Read | Read GitHub CI results for the PR head |
   | Actions | Read | Read the log of a failing Actions job |

   **Not yet checked live:** whether GitHub accepts a manifest with no
   `hook_attributes` (todo #8).

2. **Install it.** If the App has no installation yet, setup opens
   `https://github.com/apps/<app-name>/installations/new` and waits up to 15
   minutes. Choose **Only select repositories** and pick the
   repos the worker may work on. Do not choose "All repositories": setup adds
   every repo the installation covers to `config.toml`, and the worker polls
   each one (todo #56; there is no command yet to remove repos, so edit the
   `repos` list in `config.toml` by hand). Setup then prints `Installed on: ...`.
   If the App was created before the Checks and Actions permissions were added,
   setup prints how to add them.

3. **Check dependencies.** Setup prints one line per check, `[ok ]` or
   `[MISSING]`, for git, `opencode`, `ollama` and each tier 1 and 2 model, with
   the command to run for anything missing. It never installs anything itself.
   It does not check `embeddinggemma`.

4. **Install the launchd agents.** Unless you passed `--no-service`, two agents
   go into `~/Library/LaunchAgents/` and are loaded at once:

   | Agent | Runs | When |
   | --- | --- | --- |
   | `com.foreman-inator.worker` | `<path to fman> run` | At login, and restarted whenever it stops. It starts now. |
   | `com.foreman-inator.experiments` | `<path to fman> experiment run --all` | Nightly at 02:00 local time, at low priority. With no experiment started it has nothing to do. |

Setup ends with `Config written to ~/.foreman-inator/config.toml.`

Running `fman setup` again is safe: it reuses the App in `config.toml` (when
`app.pem` is still there), adds new installations and repos, and reinstalls
the agents. Re-run it after changing `[experiments] nightly_hour`, which is
read only when the agent is installed.

### What lands in `~/.foreman-inator/`

Every folder is mode 700, and `app.pem` and `config.toml` are mode 600.
Set `FOREMAN_INATOR_HOME` to use another folder.

| Path | Contents |
| --- | --- |
| `app.pem` | Your App's private key |
| `config.toml` | App id, installation ids, allowed users, repos, models; see [configuration.md](configuration.md) |
| `foreman-inator.db` | Tickets, attempts, reviews, spend, experiments (SQLite; created by the worker) |
| `traces/` | One trace per ticket, plus full check output and CI logs |
| `recordings/` | Every attempt's and review's model responses and tool results (they contain your code) |
| `worktrees/` | A bare mirror per repo and one worktree per attempt |
| `logs/` | `worker.out.log`, `worker.err.log`, `experiments.out.log`, `experiments.err.log` |
| `sandbox/`, `opencode/`, `replays/` | Per-attempt scratch space, per-attempt OpenCode config, experiment mirrors |

## 3. Store the cloud keys

```bash
fman config set-key glm
fman config set-key anthropic
```

Each command prompts for the key without echoing it and stores it in the macOS
Keychain under the service `foreman-inator`. The default config already refers
to these two names (`api_key_ref = "keychain:foreman-inator/glm"` and
`"keychain:foreman-inator/anthropic"`); the `anthropic` key serves both Claude
models. The worker reads a key at the moment it calls the model, so a running
worker picks up a new key without a restart. `config.toml` refuses anything
that looks like a key typed into it.

The default GLM endpoint is the Z.ai Coding Plan URL
(`https://api.z.ai/api/coding/paas/v4`). To use other cloud models, edit the
`[[models]]` list: see [configuration.md](configuration.md#models).

**Not yet checked live:** whether the Keychain asks for permission when the
launchd worker first reads a key.

## 4. Your first issue

### Create the labels

The worker reads these labels. Create them on the repo's Labels page
(`https://github.com/<owner>/<repo>/labels`), or with the GitHub CLI, for example
`gh label create agent`. The worker adds and removes `needs-info` itself.

| Label | Put it on | Effect |
| --- | --- | --- |
| `agent` | Issue | Start work. Removing it drops the ticket. |
| `tier:1`, `tier:2`, `tier:3` | Issue | Start on that tier instead of the router's pick. |
| `review:skip` | Issue | Skip the AI review; the PR is handed to you once the checks pass. |
| `kickback:wrong`, `kickback:incomplete`, `kickback:broke`, `kickback:style` | PR | Tag your request for changes. `wrong` moves the writer up a tier at once. |
| `needs-info` | Issue | Added by the worker when it asked questions. Remove it to have the worker go ahead as written. |

Only labels applied by a user in `allowed_users` count, and the same goes for
reviews and answers to questions.

### Label an issue

Pick a small issue (a typo, a one-function fix) in a repo the App is installed
on, and add `agent`. The worker polls every 45 seconds. Then:

1. A small local model checks whether the issue can be worked on as written.
   If not, the bot posts up to three numbered questions and adds `needs-info`.
   Answer in a comment, or remove `needs-info`. It asks only once per ticket.
2. The router picks the start tier and the first attempt starts.
3. When the change passes the repo's checks, the bot opens a draft PR from
   `fman/issue-<n>/a1` that says "Closes #<n>" and lists the model, tier and cost.
4. The worker waits for GitHub CI on the PR head, then runs the AI review and
   posts it as a comment review on the PR.
5. The PR is marked ready for review (GitHub notifies you) after an approve
   from the AI reviewer, or after a hand-over.

Watch it with:

```bash
fman status
```

and in the worker log, `~/.foreman-inator/logs/worker.err.log`.

The worker finds the repo's check command itself: a Makefile `test` or `check`
target, a `test` script in `package.json` (npm, pnpm or yarn by lockfile),
pytest for a Python project (`uv run pytest`, `poetry run pytest` or
`python3 -m pytest`), or `swift test` for a Swift package. If it finds none, it
flags the ticket on the issue once. Save a command for the repo and the ticket
continues on the next pass:

```bash
fman repo set-verify <owner/name> 'make test'
```

The checks run inside the sandbox with no network. A repo whose dependencies
are not already installed then fails its checks for reasons unrelated to the
change (todo #45, seen in the first live run). Until that is fixed, the way
out is to allow network for that repo's checks in `config.toml`, which also
lets the check code send anything it can read:

```toml
[sandbox_network]
"owner/name" = "allow"
```

## 5. Review the PR

Your review decides the outcome:

| You do | What happens |
| --- | --- |
| Approve and merge | Accept. The merge is what counts. The issue is closed and the bot posts a one-line summary: model, attempts, kickbacks, cost. |
| Request changes | Kickback. Your review comment, plus any line comments on that review, goes to the next attempt as the note. Add a `kickback:<tag>` label to the PR before you submit the review to tag it. |
| Close the issue without merging | Dropped. The ticket is left out of the metrics. The draft PR stays open; close it yourself. |

How a kickback is handled:

- The first request for changes on a model: one more attempt on the same model
  with your note.
- The second, or one tagged `kickback:wrong`: the next attempt moves up one
  rung (the next model in the escalation ladder; see
  [how-it-works.md](how-it-works.md#escalation)).
- Requesting changes on a handed-over PR starts new work on it.

Reviews on an older commit of the PR are ignored, as are reviews and labels
from users not in `allowed_users`. If the PR's base branch moves and the PR no
longer merges cleanly, the worker makes a rebase attempt (at most two per
ticket) and reviews the result again.

To start a ticket over, remove the `agent` label (the ticket is dropped), close
its draft PR, and add `agent` again (a new ticket).

## 6. Reading `fman status`

`fman status` reads the database only, so it works whether or not the worker
is running. It lists tickets under TODO, IN PROGRESS, TESTING (waiting for
your review), DONE and DROPPED (`--closed N` sets how many closed ones to
show), then a summary line, the champion lines, the cloud spend line and the
sandbox line.

| Line | Meaning | What to do |
| --- | --- | --- |
| `WAITING: spend approval (spend_cap: ... cap $5.00 reached); run fman approve-spend <id>` | A cloud call would pass the daily or monthly cap, so it was refused before it was sent. | Run `fman approve-spend <id>` to allow one more attempt (and its review), or wait for the cap to reset; the ticket then resumes on its own. |
| `WAITING: on files held by ticket(s) #N` | Another open ticket holds a file this one needs. | Nothing; it starts when the other ticket is merged or dropped. |
| `WAITING: GitHub CI on the PR head` | Local checks passed; the worker waits for the PR's CI. | Nothing. With no CI after 2 minutes it continues; still pending after 30 minutes, it continues with a note. A red job sends the change back with the job's log. |
| `WAITING: needs-info: answer the question on the issue, or remove the label` | The bot asked questions on a vague issue. | Answer in a comment, or remove `needs-info`. |
| `FLAGGED: <reason>` | The ticket was handed to you. The reason starts with a code, for example `checks_failed`, `kickback_cap`, `ai_kickback_cap`, `kickback_wrong`, `reviewer_handover`, `no_reviewer`, `review_skipped`, `rebase_cap`, `no_verify_command`, `sandbox_unavailable`, `interrupted`, `publish_failed`. The same explanation is on the issue. | Review the PR if there is one. For `no_verify_command`, run `fman repo set-verify`. Otherwise fix the cause and start the ticket over. |
| `Sandbox: on (sandbox-exec).` | Model-written code runs sandboxed. | Nothing. |
| `!!! Sandbox: OFF (sandbox = "off" in config): model-written code runs unsandboxed. !!!` | `sandbox = "off"` is set. | Set it back to `"on"` unless you accept the risk. |
| `cloud spend: $X of $5.00 today, $Y of $50.00 this month; experiments ...` | Spend against the caps. | Change the caps in `[spend_caps]`. |

`FLAGGED: sandbox_unavailable` means the sandbox could not start, so nothing
ran for that ticket: the sandbox fails closed. `sandbox = "off"` in
`config.toml` is the only way past it, and every PR description and
`fman status` then say so.

## 7. Metrics

```bash
fman metrics
```

serves a read-only page at `http://127.0.0.1:8642` (`--port` to change it):
accept rates, kickbacks, escalation, cost per accepted ticket and trend lines,
per model, filterable by model, repo and dates. It also has `/traces` (step
through a recorded ticket), `/failures` and `/experiments`. See
[how-it-works.md](how-it-works.md#metrics-and-the-trace-viewer).

## 8. Stopping, restarting and uninstalling

The launchd agent restarts the worker whenever it stops, so stop it through
launchctl:

```bash
launchctl bootout gui/$(id -u)/com.foreman-inator.worker
```

This lasts until your next login. To restart it after editing `config.toml`
(the worker reads the file only at start):

```bash
launchctl kickstart -k gui/$(id -u)/com.foreman-inator.worker
```

Do not run `fman run` by hand while the agent is loaded. Without the agent,
`fman run` runs the worker in the foreground (Ctrl-C stops it cleanly) and
`fman run --once` does one pass and exits.

To remove foreman-inator:

1. Remove both launchd agents:

   ```bash
   fman setup --uninstall-service
   ```

2. Remove the command:

   ```bash
   uv tool uninstall foreman-inator
   ```

3. Delete the App on GitHub: your account's Settings, Developer settings,
   GitHub Apps, the App, then Advanced and Delete. This also removes its
   installations.
4. Delete the API keys from the Keychain (Keychain Access, or
   `security delete-generic-password -s foreman-inator -a glm` and the same
   with `-a anthropic`).
5. Delete the data folder, which holds the App key and recordings of your code:

   ```bash
   rm -rf ~/.foreman-inator
   ```

Draft PRs and `fman/...` branches the bot created stay on GitHub until you
close or delete them.
