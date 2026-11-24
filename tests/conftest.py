"""Shared fixtures. Every test gets its own FOREMAN_INATOR_HOME under tmp_path."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def foreman_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point FOREMAN_INATOR_HOME at a fresh dir so no test touches ~/.foreman-inator."""
    home = tmp_path / "foreman-inator-home"
    monkeypatch.setenv("FOREMAN_INATOR_HOME", str(home))
    return home


def pytest_addoption(parser: pytest.Parser, pluginmanager: pytest.PytestPluginManager) -> None:
    """`addopts` in pyproject.toml passes `--numprocesses=auto` (pytest-xdist). With xdist
    disabled (`-p no:xdist`, a serial debugging run) that would be an unknown argument, so
    accept and ignore it here: the run is serial. (`-n 0` is the other serial run.)"""
    if not pluginmanager.has_plugin("xdist"):
        parser.addoption(
            "--numprocesses",
            default=None,
            help="ignored: pytest-xdist is disabled, so tests run serially",
        )
