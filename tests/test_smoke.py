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


def test_worker_run_is_a_noop() -> None:
    assert run() is None
