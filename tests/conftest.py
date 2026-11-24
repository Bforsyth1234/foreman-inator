"""Shared fixtures. Every test gets its own FOREMAN_INATOR_HOME under tmp_path."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def foreman_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point FOREMAN_INATOR_HOME at a fresh dir so no test touches ~/.foreman-inator."""
    home = tmp_path / "foreman-inator-home"
    monkeypatch.setenv("FOREMAN_INATOR_HOME", str(home))
    return home
