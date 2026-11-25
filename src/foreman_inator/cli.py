"""Command-line entry point for `foreman-inator` and its alias `fman`.

Commands: ``setup`` (#8), ``config set-key`` (#2), ``run`` and ``status`` and
``repo set-verify`` (#14), ``metrics`` and ``approve-spend`` (#21), ``experiment run |
status | promote`` and ``replay`` (#29), ``repo add | remove | list`` (#56), ``prune``
(#56). Later waves
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
    from foreman_inator.worker_status import render_status

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
        return
    try:
        cfg = config_mod.load()
    except config_mod.ConfigError as exc:
        typer.echo(f"(config.toml not readable, spend line skipped: {exc})", err=True)
        cfg = None
    with Store(db, read_only=True) as store:
        typer.echo(render_status(store, closed_limit=closed, config=cfg))


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
    running worker picks the change up when restarted."""
    from foreman_inator.setup import repo_add_command

    raise typer.Exit(repo_add_command(repos, check=not no_check, echo=typer.echo))


@repo_app.command("remove")
def repo_remove(repos: Annotated[list[str], _REPOS_ARG]) -> None:
    """Take repos out of the list the worker polls (config.toml).

    Their tickets stay in the database and nothing on GitHub changes; once restarted, the
    worker stops polling the repo and starts no work on its tickets."""
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
) -> None:
    """Replay the champion against a challenger on past tickets (resumable; Ctrl-C stops).

    Runs beside the worker with its own model proxy and its own repo mirrors; costs go to
    the experiments budget only. A reviewer challenger also starts shadow-reviewing live
    PRs. Nothing is promoted: see `fman experiment promote`."""
    from foreman_inator.worker_experiments import ExperimentError, run_command

    try:
        code = run_command(role=role, challenger=challenger, all_=all_, echo=typer.echo)
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
            help="Writers only: promote on the replay alone (no live writer check exists yet).",
        ),
    ] = False,
) -> None:
    """Make a challenger champion if it won on replay and holds live.

    SPEC's promotion rule. A challenger that loses either is ended as rejected; a writer
    can only be promoted on its replay with --replay-only. Exit 0 only when promoted."""
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


@app.command("replay")
def replay_command(
    ticket: Annotated[str, typer.Argument(help="Ticket id (from `fman status`).")],
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
) -> None:
    """Step through a recorded ticket and check today's router, escalation, review policy
    and lifecycle still decide as they did (no model calls). Exit 1 on any difference."""
    from foreman_inator.worker_experiments import replay_command as run_replay

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


def _register_setup() -> None:
    from foreman_inator.setup import setup_command

    app.command("setup")(setup_command)


_register_setup()
