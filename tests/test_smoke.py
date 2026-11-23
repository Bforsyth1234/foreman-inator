import os
from pathlib import Path

from typer.testing import CliRunner

from foreman_inator import __version__
from foreman_inator.cli import app
from foreman_inator.worker import run


def test_home_is_inside_tmp_path(tmp_path: Path) -> None:
    home = os.environ.get("FOREMAN_INATOR_HOME")
    assert home, "FOREMAN_INATOR_HOME must be set for every test"
    assert Path(home).resolve().is_relative_to(tmp_path.resolve())
    assert not Path(home).resolve().is_relative_to(Path.home() / ".foreman-inator")


def test_version_flag() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output
    assert __version__ != "0.0.0"


def test_worker_run_without_setup_exits_1() -> None:
    # #14 replaced the skeleton no-op: with no App configured, `fman run` stops at once.
    assert run() == 1


def test_config_set_key_stores_in_keychain(monkeypatch):
    from typer.testing import CliRunner

    from foreman_inator import config as cfg
    from foreman_inator.cli import app

    stored = {}
    monkeypatch.setattr(
        cfg.keyring, "set_password", lambda service, name, value: stored.update({name: value})
    )
    result = CliRunner().invoke(app, ["config", "set-key", "glm"], input="fake-value\n")
    assert result.exit_code == 0, result.output
    assert stored == {"glm": "fake-value"}
    assert "keychain:foreman-inator/glm" in result.output
