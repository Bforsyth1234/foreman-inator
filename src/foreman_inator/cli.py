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


def _register_setup() -> None:
    from foreman_inator.setup import setup_command

    app.command("setup")(setup_command)


_register_setup()
