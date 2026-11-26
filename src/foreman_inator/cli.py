"""Command-line entry point for `foreman-inator` and its alias `fman`.

Commands: ``setup`` (#8), ``config set-key`` (#2), ``run`` and ``status`` and
``repo set-verify`` (#14), ``metrics`` and ``approve-spend`` (#21), ``experiment run |
status | promote`` and ``replay`` (#29), ``repo add | remove | list`` (#56), ``prune``
(#56), ``repo set-profile`` (#51), ``rules show`` (#42), ``repo set-cloud`` (#49),
``experiment split start | stop | status`` (#34), ``experiment run --mode seeded``
(#35), ``sweep`` (#77), ``experiment propose | approve-draft | discard-draft`` (#49),
``replay --fork`` (#18), ``retry`` (#73), ``repo set-chat-network`` (#71). Later waves
add ``@app.command()`` functions here.

The callback below keeps `app` a multi-command group, so adding commands
never changes how `--version` works.
"""

import contextlib
from pathlib import Path
from typing import Annotated

import typer

from foreman_inator import __version__

app = typer.Typer(
    name="foreman-inator",
    help="Route labeled GitHub issues to the cheapest model likely to solve them.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"foreman-inator {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show the version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """foreman-inator: a GitHub App plus a polling worker on your Mac."""


config_app = typer.Typer(help="Manage ~/.foreman-inator/config.toml and Keychain keys.")
app.add_typer(config_app, name="config")


@config_app.command("set-key")
def config_set_key(
    name: Annotated[str, typer.Argument(help="Keychain item name, e.g. glm or anthropic.")],
) -> None:
    """Store an API key in the macOS Keychain (service foreman-inator)."""
    from foreman_inator import config as cfg

    value = typer.prompt(f"API key for {name}", hide_input=True)
    try:
        ref = cfg.set_key(name, value)
    except cfg.ConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f'Stored. In config.toml use: api_key_ref = "{ref}"')


@app.command("run")
def run_command(
    once: Annotated[
        bool, typer.Option("--once", help="One poll-and-work pass (one job per repo), then exit.")
    ] = False,
) -> None:
    """Run the worker: poll GitHub, work labeled issues, open PRs. Ctrl-C stops it cleanly."""
    from foreman_inator import worker

    raise typer.Exit(worker.run(once=once))


@app.command("status")
def status_command(
    closed: Annotated[
        int, typer.Option("--closed", help="How many done/dropped tickets to list.")
    ] = 5,
) -> None:
    """Tickets by column with their current attempt, flags and PR numbers (read-only)."""
    from foreman_inator import config as config_mod
    from foreman_inator import paths
    from foreman_inator.db.store import Store
    from foreman_inator.plugins import load_plugins
    from foreman_inator.plugins import status_lines as plugin_status_lines
    from foreman_inator.worker_status import local_ollama_host, ollama_line, render_status

    plugins = load_plugins()  # #45
    db = paths.db_path()
    if not db.exists():
        typer.echo("No tickets yet (the worker has not created its database).")
        with contextlib.suppress(config_mod.ConfigError):
            from foreman_inator.runner import sandbox
            from foreman_inator.worker_status import repos_line

            c = config_mod.load()
            typer.echo(repos_line(c))
            line = sandbox.describe(c.sandbox, dict(c.sandbox_network), dict(c.sandbox_install))  # type: ignore[arg-type]
            typer.echo(f"!!! {line} !!!" if c.sandbox == "off" else line)
            host = local_ollama_host(c)  # #35
            if host is not None:
                typer.echo(ollama_line(host))
        for pline in plugin_status_lines(plugins):
            typer.echo(pline)
        return
    try:
        cfg = config_mod.load()
    except config_mod.ConfigError as exc:
        typer.echo(f"(config.toml not readable, spend line skipped: {exc})", err=True)
        cfg = None
    host = local_ollama_host(cfg) if cfg is not None else None
    with Store(db, read_only=True) as store:
        typer.echo(
            render_status(store, closed_limit=closed, config=cfg, ollama_host=host, plugins=plugins)
        )


@app.command("metrics")
def metrics_command(
    port: Annotated[int, typer.Option("--port", help="Local port (127.0.0.1 only).")] = 8642,
) -> None:
    """Serve the metrics page on 127.0.0.1 (read-only; Ctrl-C stops it)."""
    from foreman_inator import paths
    from foreman_inator.metrics.web import metrics_url, serve

    if not 0 < port < 65536:
        raise typer.BadParameter("port must be between 1 and 65535")
    db = paths.db_path()
    if not db.exists():
        typer.echo("No tickets yet (the worker has not created its database).")
        raise typer.Exit(1)
    from foreman_inator import config as config_mod

    try:
        cfg = config_mod.load()
    except config_mod.ConfigError as exc:
        typer.echo(f"(config.toml not readable, using defaults: {exc})", err=True)
        cfg = config_mod.Config()
    typer.echo(f"Metrics on {metrics_url('127.0.0.1', port)} (Ctrl-C stops)")
    serve(
        db,
        port=port,
        config=cfg,
        recordings_dir=paths.recordings_dir(),
        traces_dir=paths.traces_dir(),
    )


@app.command("approve-spend")
def approve_spend_command(
    ticket: Annotated[int, typer.Argument(help="Ticket id (from `fman status`).")],
) -> None:
    """Allow one more cloud attempt (and its review) of TICKET past the spend caps."""
    from foreman_inator import config as config_mod
    from foreman_inator import paths
    from foreman_inator.db.store import Store
    from foreman_inator.spend import Spend, SpendError

    db = paths.db_path()
    if not db.exists():
        typer.echo("No tickets yet (the worker has not created its database).", err=True)
        raise typer.Exit(1)
    cfg = config_mod.load()
    with Store(db) as store:
        spend = Spend(store, cfg)
        try:
            approval = spend.approve(ticket)
        except SpendError as exc:
            typer.echo(f"Not approved: {exc}", err=True)
            raise typer.Exit(1) from exc
        usd = spend.approval_usd
    typer.echo(
        f"Approved (approval #{approval.id}): ticket #{ticket} may spend up to ${usd:.2f} of "
        "cloud calls past the caps on its next attempt. The worker resumes it on its next pass."
    )


repo_app = typer.Typer(
    help="Which repos the worker works on (config.toml) and per-repo settings (database)."
)
app.add_typer(repo_app, name="repo")

_REPOS_ARG = typer.Argument(help="One or more owner/name.", show_default=False)


@repo_app.command("add")
def repo_add(
    repos: Annotated[list[str], _REPOS_ARG],
    no_check: Annotated[
        bool,
        typer.Option(
            "--no-check", help="Add without checking that the App's installation can see it."
        ),
    ] = False,
) -> None:
    """Add repos to the list the worker polls (config.toml).

    Refused when the App's installation cannot see a repo (then nothing is added). The
    running worker picks the change up on its next poll (no restart needed)."""
    from foreman_inator.setup import repo_add_command

    raise typer.Exit(repo_add_command(repos, check=not no_check, echo=typer.echo))


@repo_app.command("remove")
def repo_remove(repos: Annotated[list[str], _REPOS_ARG]) -> None:
    """Take repos out of the list the worker polls (config.toml).

    Their tickets stay in the database and nothing on GitHub changes; from its next poll
    the running worker stops polling the repo and starts no work on its tickets."""
    from foreman_inator.setup import repo_remove_command

    raise typer.Exit(repo_remove_command(repos, echo=typer.echo))


@repo_app.command("list")
def repo_list(
    available: Annotated[
        bool,
        typer.Option(
            "--available", help="Also list the repos the installation can see but not configured."
        ),
    ] = False,
    no_check: Annotated[
        bool, typer.Option("--no-check", help="Do not ask GitHub what the installation covers.")
    ] = False,
) -> None:
    """The repos in config.toml, marking any the App's installation no longer covers."""
    from foreman_inator.setup import repo_list_command

    raise typer.Exit(repo_list_command(available=available, check=not no_check, echo=typer.echo))


@repo_app.command("set-verify")
def repo_set_verify(
    repo: Annotated[str, typer.Argument(help="owner/name")],
    command: Annotated[
        str | None, typer.Argument(help="Shell command that runs the checks, e.g. 'make test'.")
    ] = None,
    clear: Annotated[bool, typer.Option("--clear", help="Forget the saved command.")] = False,
) -> None:
    """Save the command that verifies REPO (used when none can be detected)."""
    import re

    from foreman_inator import paths
    from foreman_inator.db.store import Store
    from foreman_inator.worker_status import set_verify_command

    if not re.match(r"^[\w.-]+/[\w.-]+$", repo):
        raise typer.BadParameter("repo must look like owner/name")
    if not clear and not (command and command.strip()):
        raise typer.BadParameter("give a command, or --clear")
    with Store(paths.db_path()) as store:
        cleared = set_verify_command(store, repo, None if clear else command)
    if clear:
        typer.echo(f"Cleared the verify command for {repo}; it will be detected again.")
        return
    typer.echo(f"Saved. {repo} is verified with: {command}")
    if cleared:
        typer.echo(f"Unflagged ticket(s) {', '.join(f'#{i}' for i in cleared)}; work resumes.")


@repo_app.command("set-profile")
def repo_set_profile(
    repo: Annotated[str, typer.Argument(help="owner/name")],
    preset: Annotated[
        str | None,
        typer.Argument(help="prototype | personal | default | production"),
    ] = None,
    writer: Annotated[str | None, typer.Option("--writer", help="thrifty | normal | fast")] = None,
    review: Annotated[
        str | None, typer.Option("--review", help="off | light | normal | strict")
    ] = None,
) -> None:
    """Set REPO's profile: how hard the writer tries and how much review a change gets."""
    import re

    from foreman_inator import paths
    from foreman_inator.db.store import Store
    from foreman_inator.routing import profile as prof
    from foreman_inator.worker_status import set_profile

    if not re.match(r"^[\w.-]+/[\w.-]+$", repo):
        raise typer.BadParameter("repo must look like owner/name")
    if preset is None and writer is None and review is None:
        raise typer.BadParameter("give a preset, --writer or --review")
    with Store(paths.db_path()) as store:
        try:
            p = set_profile(store, repo, preset, writer=writer, review=review)
        except prof.ProfileError as exc:
            raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Saved. {repo}: {prof.describe(p)}. The worker uses it from its next decision.")


@repo_app.command("set-install")
def repo_set_install(
    repo: Annotated[str, typer.Argument(help="owner/name")],
    command: Annotated[
        str | None,
        typer.Argument(
            help="Shell command that installs the dependencies, e.g. 'uv sync --frozen'."
        ),
    ] = None,
    clear: Annotated[bool, typer.Option("--clear", help="Forget the saved command.")] = False,
) -> None:
    """Save the command that installs REPO's dependencies in a fresh worktree."""
    import re

    from foreman_inator import paths
    from foreman_inator.db.store import Store

    if not re.match(r"^[\w.-]+/[\w.-]+$", repo):
        raise typer.BadParameter("repo must look like owner/name")
    if not clear and not (command and command.strip()):
        raise typer.BadParameter("give a command, or --clear")
    with Store(paths.db_path()) as store:
        store.set_install_command(repo, None if clear else (command or "").strip())
    if clear:
        typer.echo(
            f"Cleared the install command for {repo}; it will be detected from the lockfiles again."
        )
        return
    typer.echo(f"Saved. {repo}'s worktrees are prepared with: {command}")


@repo_app.command("set-cloud")
def repo_set_cloud(
    repo: Annotated[str, typer.Argument(help="owner/name")],
    setting: Annotated[str, typer.Argument(help="on (default) | off: off is local only")],
) -> None:
    """Allow (on) or forbid (off) cloud models for REPO: off keeps its code on this Mac
    (local writers and reviewers only; experiments leave it out of cloud runs)."""
    import re

    from foreman_inator import paths
    from foreman_inator.db.store import Store

    if not re.match(r"^[\w.-]+/[\w.-]+$", repo):
        raise typer.BadParameter("repo must look like owner/name")
    value = setting.strip().lower()
    if value not in ("on", "off"):
        raise typer.BadParameter("give on or off")
    with Store(paths.db_path()) as store:
        store.set_cloud(repo, value == "on")
    if value == "on":
        typer.echo(f"Saved. {repo} may use cloud models (within the spend caps).")
    else:
        typer.echo(
            f"Saved. {repo} is local only: its code never goes to a cloud model; a ticket"
            " that runs out of local rungs is handed to you (flag local_only)."
        )


@repo_app.command("set-chat-network")
def repo_set_chat_network(
    repo: Annotated[str, typer.Argument(help="owner/name")],
    setting: Annotated[str, typer.Argument(help="on | off (default): chat sessions only")],
) -> None:
    """Open (on) or close (off, the default) outbound network for REPO's chat sessions
    (#71 decision 4): on gives the chat sandbox the dependency-install policy (ports
    443/80), never your credentials. Tickets are unaffected."""
    import re

    from foreman_inator import paths
    from foreman_inator.db.store import Store

    if not re.match(r"^[\w.-]+/[\w.-]+$", repo):
        raise typer.BadParameter("repo must look like owner/name")
    value = setting.strip().lower()
    if value not in ("on", "off"):
        raise typer.BadParameter("give on or off")
    with Store(paths.db_path()) as store:
        store.set_chat_network(repo, value == "on")
    if value == "on":
        typer.echo(
            f"Saved. {repo}'s chat sessions may reach the network on ports 443/80"
            " (tickets are unaffected)."
        )
    else:
        typer.echo(f"Saved. {repo}'s chat sessions have no network (the default).")


experiment_app = typer.Typer(
    help="Champion vs challenger: replays (overnight), shadow reviews, promotion."
)
app.add_typer(experiment_app, name="experiment")


def _fail(exc: Exception) -> None:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(1) from exc


@experiment_app.command("run")
def experiment_run(
    role: Annotated[
        str | None, typer.Option("--role", help="writer or reviewer.", show_default=False)
    ] = None,
    challenger: Annotated[
        str | None,
        typer.Option(
            "--challenger",
            help="Variant: MODEL/PROMPT@HASH8, MODEL/PROMPT (the file now) or MODEL.",
            show_default=False,
        ),
    ] = None,
    all_: Annotated[
        bool, typer.Option("--all", help="Resume every unfinished replay (the nightly job).")
    ] = False,
    rules: Annotated[
        Path | None,
        typer.Option(
            "--rules",
            help="Replay this rules file in place of the repo's rules, challenger side only"
            " (--challenger may then be omitted).",
            show_default=False,
        ),
    ] = None,
    mode: Annotated[
        str,
        typer.Option(
            "--mode",
            help="replay, or seeded: reviewers only, merged cases with a planted bug (catch rate).",
        ),
    ] = "replay",
    seed: Annotated[
        int | None,
        typer.Option("--seed", help="--mode seeded: the mutation seed.", show_default=False),
    ] = None,
    kinds: Annotated[
        list[str] | None,
        typer.Option(
            "--kinds",
            help="--mode seeded: mutation kinds (repeatable): drop_if_guard,"
            " neutralise_check, flip_comparison.",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Replay the champion against a challenger on past tickets (resumable; Ctrl-C stops).

    Runs beside the worker with its own model proxy and its own repo mirrors; costs go to
    the experiments budget only. A reviewer challenger also starts shadow-reviewing live
    PRs. Nothing is promoted: see `fman experiment promote`."""
    from foreman_inator.worker_experiments import ExperimentError, run_command

    try:
        code = run_command(
            role=role,
            challenger=challenger,
            all_=all_,
            echo=typer.echo,
            rules=rules,
            mode=mode,
            seed=seed,
            kinds=kinds or None,
        )
    except ExperimentError as exc:
        _fail(exc)
    raise typer.Exit(code)


@experiment_app.command("status")
def experiment_status(
    experiment: Annotated[
        int | None, typer.Argument(help="Experiment id for its details.", show_default=False)
    ] = None,
) -> None:
    """Champions, running and ended experiments with their replay and live results."""
    from foreman_inator import config as config_mod
    from foreman_inator import paths
    from foreman_inator.db.store import NotFoundError, Store
    from foreman_inator.worker_experiments import status_lines

    db = paths.db_path()
    if not db.exists():
        typer.echo("No experiments yet (the worker has not created its database).")
        return
    try:
        cfg = config_mod.load()
    except config_mod.ConfigError as exc:
        typer.echo(f"(config.toml not readable, using defaults: {exc})", err=True)
        cfg = config_mod.Config()
    with Store(db, read_only=True) as store:
        try:
            lines = status_lines(store, cfg, experiment)
        except NotFoundError as exc:
            _fail(exc)
    typer.echo("\n".join(lines))


@experiment_app.command("promote")
def experiment_promote(
    target: Annotated[str, typer.Argument(help="Experiment id or challenger variant id.")],
    replay_only: Annotated[
        bool,
        typer.Option(
            "--replay-only",
            help="Writers only: promote on the replay alone, without a live check (a"
            " running `fman experiment split` is the live check otherwise).",
        ),
    ] = False,
) -> None:
    """Make a challenger champion if it won on replay and holds live.

    SPEC's promotion rule. A challenger that loses either is ended as rejected; a writer
    holds live through its traffic split (`fman experiment split`), or is promoted on its
    replay alone with --replay-only. Exit 0 only when promoted."""
    from foreman_inator import config as config_mod
    from foreman_inator import paths
    from foreman_inator.db.store import NotFoundError, Store
    from foreman_inator.worker_experiments import ExperimentError, promote

    db = paths.db_path()
    if not db.exists():
        _fail(FileNotFoundError("no database yet: run the worker first"))
    with Store(db) as store:
        try:
            res = promote(store, config_mod.load(), target, replay_only=replay_only)
        except (ExperimentError, NotFoundError) as exc:
            _fail(exc)
    typer.echo(f"{res.outcome.upper()}: {res.message}")
    raise typer.Exit(0 if res.outcome == "promoted" else 1)


@experiment_app.command("propose")
def experiment_propose(
    role: Annotated[str, typer.Option("--role", help="writer or reviewer.")],
    proposer: Annotated[
        str | None,
        typer.Option(
            "--proposer",
            help="Tier 3 model that drafts it (default: experiments.proposer_model, else the"
            " first one independent of the scoring reviewer).",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Draft the next prompt of the role's champion from recent kickbacks (one LLM call).

    Writes a draft and its rationale for you to read; registers nothing. Approve it with
    `fman experiment approve-draft ID` or drop it with `fman experiment discard-draft ID`.
    One LLM-drafted challenger per role at a time; costs go to the experiments budget."""
    from foreman_inator.worker_experiments import ExperimentError, propose_command

    try:
        propose_command(role=role, proposer=proposer, echo=typer.echo)
    except ExperimentError as exc:
        _fail(exc)


@experiment_app.command("approve-draft")
def experiment_approve_draft(
    draft: Annotated[int, typer.Argument(help="Draft id (from `fman experiment propose`).")],
) -> None:
    """Approve a drafted prompt: register it as a challenger and run its replay.

    The replay uses tickets created after the draft's cutoff only, as `fman experiment
    run --role R --challenger V` would run it. Nothing is promoted."""
    from foreman_inator.worker_experiments import ExperimentError, approve_draft_command

    try:
        code = approve_draft_command(draft, echo=typer.echo)
    except ExperimentError as exc:
        _fail(exc)
    raise typer.Exit(code)


@experiment_app.command("discard-draft")
def experiment_discard_draft(
    draft: Annotated[int, typer.Argument(help="Draft id (from `fman experiment propose`).")],
) -> None:
    """Discard a drafted prompt and remove its files."""
    from foreman_inator.worker_experiments import ExperimentError, discard_draft_command

    try:
        discard_draft_command(draft, echo=typer.echo)
    except ExperimentError as exc:
        _fail(exc)


split_app = typer.Typer(
    help="Live traffic split: a share of first attempts to a writer challenger."
)
experiment_app.add_typer(split_app, name="split")


@split_app.command("start")
def experiment_split_start(
    challenger: Annotated[str, typer.Argument(help="Writer variant that won its replay.")],
    share: Annotated[
        float, typer.Option("--share", help="Share of first attempts it writes.")
    ] = 0.15,
) -> None:
    """Send a stable share of new tickets' first attempts to CHALLENGER (the champion
    writes the rest); `fman experiment promote` then judges it live."""
    from foreman_inator import config as config_mod
    from foreman_inator import paths
    from foreman_inator.db.store import NotFoundError, Store
    from foreman_inator.worker_experiments import ExperimentError, start_split

    db = paths.db_path()
    if not db.exists():
        _fail(FileNotFoundError("no database yet: run the worker first"))
    with Store(db) as store:
        try:
            exp = start_split(store, config_mod.load(), challenger, share=share)
        except (ExperimentError, NotFoundError) as exc:
            _fail(exc)
    typer.echo(
        f"Split #{exp.id} running: {exp.challenger_variant} vs {exp.champion_variant}."
        " `fman experiment split status` shows it."
    )


@split_app.command("stop")
def experiment_split_stop(
    experiment: Annotated[int, typer.Argument(help="The split's experiment id.")],
) -> None:
    """End a running split (rejected); its attempts are kept."""
    from foreman_inator import paths
    from foreman_inator.db.store import NotFoundError, Store
    from foreman_inator.worker_experiments import ExperimentError, stop_split

    db = paths.db_path()
    if not db.exists():
        _fail(FileNotFoundError("no database yet: run the worker first"))
    with Store(db) as store:
        try:
            exp = stop_split(store, experiment)
        except (ExperimentError, NotFoundError) as exc:
            _fail(exc)
    typer.echo(f"Split #{exp.id} stopped.")


@split_app.command("status")
def experiment_split_status(
    experiment: Annotated[
        int | None, typer.Argument(help="Split id (default: running ones).", show_default=False)
    ] = None,
) -> None:
    """The running split(s): share, live accept rate and cost per accepted ticket."""
    from foreman_inator import config as config_mod
    from foreman_inator import paths
    from foreman_inator.db.store import NotFoundError, Store
    from foreman_inator.worker_experiments import ExperimentError, split_lines

    db = paths.db_path()
    if not db.exists():
        typer.echo("No experiments yet (the worker has not created its database).")
        return
    try:
        cfg = config_mod.load()
    except config_mod.ConfigError as exc:
        typer.echo(f"(config.toml not readable, using defaults: {exc})", err=True)
        cfg = config_mod.Config()
    with Store(db, read_only=True) as store:
        try:
            lines = split_lines(store, cfg, experiment)
        except (ExperimentError, NotFoundError) as exc:
            _fail(exc)
    typer.echo("\n".join(lines))


rules_app = typer.Typer(help="The rules (AGENTS.md, CLAUDE.md, rules_file) the models get.")
app.add_typer(rules_app, name="rules")


@rules_app.command("show")
def rules_show_command(
    repo: Annotated[str, typer.Argument(help="owner/name")],
    ref: Annotated[
        str | None,
        typer.Option(
            "--ref", help="Commit or branch (default: the base branch).", show_default=False
        ),
    ] = None,
    files: Annotated[
        list[str] | None,
        typer.Option(
            "--file", help="An owned file, for nested rules (repeatable).", show_default=False
        ),
    ] = None,
) -> None:
    """Print the writer's and the reviewer's rules blocks for REPO exactly as built, with
    each source's size and any clipping. No model call."""
    from foreman_inator import config as config_mod
    from foreman_inator import paths, rules_show
    from foreman_inator.db.store import Store

    try:
        cfg = config_mod.load()
    except config_mod.ConfigError:
        cfg = config_mod.Config()
    db = paths.db_path()
    store = Store(db, read_only=True) if db.exists() else None
    try:
        code = rules_show.run(
            repo, config=cfg, store=store, echo=typer.echo, ref=ref, owned_files=files or ()
        )
    finally:
        if store is not None:
            store.close()
    raise typer.Exit(code)


@app.command("sweep")
def sweep_command(
    repo: Annotated[str, typer.Argument(help="owner/name")],
    label: Annotated[
        str | None, typer.Option("--label", help="Only issues with this label.", show_default=False)
    ] = None,
    exclude_label: Annotated[
        list[str] | None,
        typer.Option(
            "--exclude-label", help="Skip issues with this label (repeatable).", show_default=False
        ),
    ] = None,
    author: Annotated[
        str | None, typer.Option("--author", help="Only issues by this user.", show_default=False)
    ] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", help="At most this many issues.", show_default=False)
    ] = None,
    include_any_author: Annotated[
        bool,
        typer.Option("--include-any-author", help="Also issues by users not on allowed_users."),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show the plan; label nothing.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Label without asking.")] = False,
) -> None:
    """Label REPO's open issues for the worker in waves (as you, with the GitHub CLI):
    a preview with tiers, file overlaps and spend first, then the labels."""
    from foreman_inator import config as config_mod
    from foreman_inator import paths, sweep
    from foreman_inator.db.store import Store

    if limit is not None and limit < 1:
        raise typer.BadParameter("--limit must be at least 1")
    cfg = config_mod.load()
    filters = sweep.Filters(
        label=label,
        exclude_label=tuple(exclude_label or ()),
        author=author,
        limit=limit,
        include_any_author=include_any_author,
    )
    db = paths.db_path()
    store = Store(db, read_only=True) if db.exists() else None
    try:
        code = sweep.run(
            repo,
            config=cfg,
            store=store,
            filters=filters,
            dry_run=dry_run,
            yes=yes,
            echo=typer.echo,
            router=sweep.classifier_router(cfg, repo),
        )
    finally:
        if store is not None:
            store.close()
    raise typer.Exit(code)


@app.command("replay")
def replay_command(
    ticket: Annotated[
        str | None,
        typer.Argument(help="Ticket id (from `fman status`).", show_default=False),
    ] = None,
    recordings: Annotated[
        Path | None, typer.Option("--recordings", help="Recordings folder (default: yours).")
    ] = None,
    traces: Annotated[
        Path | None, typer.Option("--traces", help="Traces folder (default: yours).")
    ] = None,
    rows: Annotated[
        Path | None,
        typer.Option("--rows", help="Stored rows as JSON (a checked-in regression case)."),
    ] = None,
    fork: Annotated[
        int | None,
        typer.Option(
            "--fork",
            help="Attempt id: restart it at its labelled failure step with a new model or"
            " prompt (#18; model calls, experiments budget).",
            show_default=False,
        ),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option("--model", help="--fork: the model to continue with.", show_default=False),
    ] = None,
    prompt: Annotated[
        str | None,
        typer.Option(
            "--prompt", help="--fork: a writer prompt name to rebuild with.", show_default=False
        ),
    ] = None,
    step: Annotated[
        int | None,
        typer.Option(
            "--step",
            help="--fork: restart at this step instead of the labelled one.",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Step through a recorded ticket and check today's router, escalation, review policy
    and lifecycle still decide as they did (no model calls). Exit 1 on any difference.

    With --fork ATTEMPT: restart that attempt at its failure step in a scratch worktree
    (sandboxed, never pushed) and record what happens next to it. Exit 2 when it cannot."""
    from foreman_inator.worker_experiments import replay_command as run_replay

    if fork is not None:
        from foreman_inator.worker_experiments import ExperimentError, fork_command

        if ticket is not None:
            raise typer.BadParameter("give a ticket id or --fork ATTEMPT, not both")
        try:
            code = fork_command(fork, model=model, prompt=prompt, step=step, echo=typer.echo)
        except ExperimentError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(2) from exc
        raise typer.Exit(code)
    if model is not None or prompt is not None or step is not None:
        raise typer.BadParameter("--model, --prompt and --step go with --fork")
    if ticket is None:
        raise typer.BadParameter("give a ticket id (or --fork ATTEMPT)")

    raise typer.Exit(
        run_replay(
            ticket, recordings_dir=recordings, traces_dir=traces, rows_json=rows, echo=typer.echo
        )  # fmt: skip
    )


@app.command("export")
def export_command(
    ticket: Annotated[str, typer.Argument(help="Ticket id (from `fman status`).")],
    redact: Annotated[
        bool,
        typer.Option("--redact", help="Required: replace your code and text (no raw mode)."),
    ] = False,
    output: Annotated[
        Path | None,
        typer.Option(
            "-o",
            "--output",
            help="A .tar.gz/.tgz file, or an empty folder (default: ~/Downloads).",
            show_default=False,
        ),
    ] = None,
    recordings: Annotated[
        Path | None, typer.Option("--recordings", help="Recordings folder (default: yours).")
    ] = None,
    traces: Annotated[
        Path | None, typer.Option("--traces", help="Traces folder (default: yours).")
    ] = None,
    db: Annotated[
        Path | None, typer.Option("--db", help="Database (default: yours; opened read-only).")
    ] = None,
) -> None:
    """Export one ticket's trace, recordings and store rows for a bug report, with your
    code, prompts, tool output, notes, repo names and paths replaced (allow-list
    redaction). Only structure, timings, counts, models and decisions are kept."""
    from foreman_inator import config as config_mod
    from foreman_inator import export, paths
    from foreman_inator.routing import escalation

    if not redact:
        typer.echo(export.REFUSE_RAW, err=True)
        raise typer.Exit(2)
    base = None if recordings and traces and db else paths.home()
    try:
        cfg = config_mod.load()
    except config_mod.ConfigError:
        cfg = config_mod.Config()
    try:
        res = export.export_ticket(
            ticket,
            recordings_dir=recordings or base / "recordings",  # type: ignore[operator]
            traces_dir=traces or base / "traces",  # type: ignore[operator]
            db_path=db or base / paths.DB_NAME,  # type: ignore[operator]
            out=output,
            ladder=escalation.build_ladder(cfg.models),
        )
    except export.ExportError as exc:
        _fail(exc)
    tree = export.git_work_tree(res.path)
    if tree is not None:
        typer.echo(f"Warning: {res.path} is inside the git work tree {tree}; do not commit it.")
    n = res.manifest["counts"]
    typer.echo(
        f"Wrote {res.path} ({len(res.manifest['files'])} files, "
        f"{n['values_replaced']} values replaced). Read MANIFEST.json before sharing: "
        "sizes, timings, counts and file extensions are still visible."
    )


@app.command("prune")
def prune_command(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="List what would be deleted; delete nothing.")
    ] = False,
    days: Annotated[
        float | None,
        typer.Option(
            "--days", help="Retention in days (default: retention_days, 90).", show_default=False
        ),
    ] = None,
) -> None:
    """Delete unmerged tickets' recordings and traces, and ended attempts' scratch, older
    than the retention (#56). The worker runs the same prune once a day."""
    from foreman_inator import config as config_mod
    from foreman_inator import paths, prune
    from foreman_inator.db.store import Store

    if days is not None and days < 1:
        raise typer.BadParameter("--days must be at least 1")
    db = paths.db_path()
    if not db.exists():
        typer.echo("No tickets yet (the worker has not created its database).")
        return
    cfg = config_mod.load()
    with Store(db, read_only=True) as store:
        report = prune.run(
            store,
            paths.home(),
            days=days if days is not None else cfg.retention_days,
            dry_run=dry_run,
            repos=cfg.repos,
            recordings_dir=paths.recordings_dir(),
            traces_dir=paths.traces_dir(),
        )
    for line in report.lines():
        typer.echo(line)
    if report.errors:
        raise typer.Exit(1)


@app.command("retry")
def retry_command(
    ticket: Annotated[str, typer.Argument(help="owner/repo#issue, or a ticket id.")],
    tier: Annotated[
        int | None,
        typer.Option("--tier", help="Retry on this tier (1-3).", show_default=False),
    ] = None,
    up: Annotated[bool, typer.Option("--up", help="Retry one tier up.")] = False,
) -> None:
    """Retry a flagged or dropped ticket, keeping its history (attempts, branch, PR).

    Same tier by default. The worker picks it up on its next pass; the retry gets one more
    round, then the ticket comes back to you. Exit 1 when the retry is refused."""
    from datetime import UTC, datetime

    from foreman_inator import config as config_mod
    from foreman_inator import paths
    from foreman_inator.db.store import Store
    from foreman_inator.retry import retry

    if tier is not None and up:
        raise typer.BadParameter("give --tier N or --up, not both")
    db = paths.db_path()
    if not db.exists():
        _fail(FileNotFoundError("no database yet: run the worker first"))
    cfg = config_mod.load()
    api = None
    key_path = paths.app_key_path()
    if cfg.app_id is not None and cfg.installation_ids and key_path.exists():
        from foreman_inator.worker_run import build_github

        try:
            api = build_github(cfg, key_path)  # a dropped ticket's issue and PR are checked
        except Exception as exc:  # noqa: BLE001 - only a dropped ticket needs it
            typer.echo(f"(GitHub not reachable: {type(exc).__name__}; flagged tickets only)")
    choice: int | str | None = tier if tier is not None else ("up" if up else None)
    with Store(db) as store:
        res = retry(store, cfg, ticket, tier=choice, now=datetime.now(UTC), api=api)  # type: ignore[arg-type]
    typer.echo(res.reason)
    raise typer.Exit(0 if res.ok else 1)


def _register_setup() -> None:
    from foreman_inator.setup import setup_command

    app.command("setup")(setup_command)


_register_setup()


def _register_plugins() -> None:
    """#45: installed plugins' extra CLI commands, after every built-in (a plugin can
    never replace one: ``install_cli_commands`` skips taken names)."""
    from foreman_inator.plugins import install_cli_commands, load_plugins

    install_cli_commands(app, load_plugins())


_register_plugins()
