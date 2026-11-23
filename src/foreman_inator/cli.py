"""Command-line entry point for `foreman-inator` and its alias `fman`.

Commands: ``setup`` (#8), ``config set-key`` (#2), ``run`` and ``status`` and
``repo set-verify`` (#14). Later waves add ``@app.command()`` functions here.

The callback below keeps `app` a multi-command group, so adding commands
never changes how `--version` works.
"""

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
    from foreman_inator import paths
    from foreman_inator.db.store import Store
    from foreman_inator.worker_status import render_status

    db = paths.db_path()
    if not db.exists():
        typer.echo("No tickets yet (the worker has not created its database).")
        return
    with Store(db, read_only=True) as store:
        typer.echo(render_status(store, closed_limit=closed))


repo_app = typer.Typer(help="Per-repo settings kept in the worker's database.")
app.add_typer(repo_app, name="repo")


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


def _register_setup() -> None:
    from foreman_inator.setup import setup_command

    app.command("setup")(setup_command)


_register_setup()
