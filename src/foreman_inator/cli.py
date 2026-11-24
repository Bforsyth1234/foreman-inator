"""Command-line entry point for `foreman-inator` and its alias `fman`.

Subcommands are registered here by the wave runner, e.g.::

    @app.command()
    def setup() -> None: ...

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
