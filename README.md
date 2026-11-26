# foreman-inator

Label a GitHub issue and foreman-inator opens a pull request for it, using the
cheapest model that can do the job. It learns which model that is from your
reviews.

It is a GitHub App plus a worker that runs on your Mac, for a developer who
wants local models to do most of their issues and cloud models only when the
local ones fail.

**Status:** built and tested against fakes (over 1,100 automated tests), but not
yet run end to end (label to merged PR) against real GitHub and real models.
A first partial run on a private test repo found problems that are still open,
and the live checks are listed in [todo.md](todo.md#live-checks).

## What happens when you label an issue

1. You add the `agent` label. If the issue is too vague to work on, the worker
   first asks up to three questions on it and waits for your answer.
2. The worker picks the cheapest model likely to solve it, usually a local
   one, and writes the change in its own git worktree, inside a sandbox.
3. It runs the repo's tests. If they fail, it retries once, then moves up to a
   stronger model. When they pass, it opens a draft PR and waits for GitHub CI.
4. A model from a different family reviews the change. The PR is marked ready
   for your review only after that review approves it, or when the worker hands
   it to you with a comment that says why.
5. You approve and merge it, or request changes to send it back with your
   comment. The outcome is recorded per model, so the routing improves over time.

| Tier | Default model | Where | Used for |
| --- | --- | --- | --- |
| 1 | Inkling 2 Mini | Local, Ollama | Small, mechanical changes |
| 2 | GLM (open weights) | Cloud, pay per use | Normal features and bug fixes |
| 3 | Claude Opus, then Claude Fable | Cloud, pay per use | Large refactors, anything that failed on tier 2 |

Tier 2 used to be Qwen3.8 27B on Ollama; it is still supported as an opt-in
local tier 2 (see [docs/setup.md](docs/setup.md#keeping-tier-2-local)). A
cloud tier 2 attempt costs a few cents, which is cheaper in time than a slow
local attempt that escalates anyway.

## Chat sessions

For work you want to steer yourself, `fman chat <owner/name>` (or the `/chat`
page of `fman metrics`) opens an interactive coding session in its own
worktree, through the same tiers, spend caps and sandbox as a ticket. It starts
on the local tier 1, `/tier up` moves it to a stronger model, its spend shows
next to ticket spend in `fman status`, and `/pr` opens a draft PR (nothing
merges). See [docs/commands.md](docs/commands.md#fman-chat).

## Install and first run

You need macOS, git 2.31 or newer, [uv](https://docs.astral.sh/uv/),
[OpenCode](https://opencode.ai/docs/) and [Ollama](https://ollama.com/download).
The package is not on PyPI; install it from GitHub:

```bash
uv tool install git+https://github.com/Bforsyth1234/foreman-inator
ollama pull inkling-2-mini
fman setup
```

This installs from the repo's `main` branch. At the time of writing `main`
holds only the spec, and the code is on the `wave-*` branches waiting to be
merged; until they are, install from a checkout as described in
[docs/setup.md](docs/setup.md#1-install-the-fman-command).

`fman setup` opens your browser to register your own private GitHub App, asks
you to install it on the repos it may work on, checks the dependencies, and
installs a launchd agent that starts the worker at login.

For tiers 2 and 3 and the AI review, store the cloud API keys in the Keychain:

```bash
fman config set-key glm
fman config set-key anthropic
```

Then add the `agent` label to an issue in one of those repos and watch it with
`fman status`. The full guide, including what each step shows and how to
review, is [docs/setup.md](docs/setup.md).

## Safety

- **Nothing merges without you.** The worker cannot approve or merge a PR, and
  it pushes only to branches it created (`fman/...`).
- **Only allowed users start work.** A label counts only if a user in
  `allowed_users` applied it. Code from forks and other people's PRs is never run.
- **Local by default, with spend caps.** Code, traces and recordings stay on
  your Mac. Cloud models are called only for tiers configured as cloud, and
  only inside a daily and monthly cap ($5 and $50 by default). With the
  default ladder tiers 2 and 3 are cloud, so tier 2 work spends money too.
- **Secrets stay outside the repo.** The App key and config live in
  `~/.foreman-inator/` (mode 700); API keys live in the macOS Keychain.
- **Every loop is capped.** Retries, escalations and rebases have fixed limits
  and end with the issue handed to you.
- **The reviewer is independent.** It is from a different model family than
  the writer and sees only the issue and the diff, not the writer's reasoning.
- **Model-written code runs sandboxed.** The coding agent, the test command and
  the reviewer's checks run under macOS `sandbox-exec`, confined to the attempt's
  worktree. If the sandbox cannot start, nothing runs.

## Documentation

- [docs/setup.md](docs/setup.md): install, `fman setup`, the first issue, reviewing, `fman status`, stopping and uninstalling
- [docs/how-it-works.md](docs/how-it-works.md): the lifecycle, architecture, escalation, spend caps, experiments and what leaves your Mac
- [docs/configuration.md](docs/configuration.md): every `config.toml` key
- [docs/commands.md](docs/commands.md): every `fman` command and option
- [docs/publishing.md](docs/publishing.md): letting other people use it (their own App, or `fman setup --public`)
- [docs/releasing.md](docs/releasing.md): how a release is cut; [docs/homebrew.md](docs/homebrew.md): the Homebrew tap (`brew install` once the tap is live)
- [docs/chat-design.md](docs/chat-design.md): the chat feature's design and decisions (built; live checks pending)
- [SPEC.md](SPEC.md): the full design; [DONE.md](DONE.md): what was built and where it differs

This is a personal project with no promised support. It is MIT licensed.
