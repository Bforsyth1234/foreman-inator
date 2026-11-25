"""``paths.write_secret_file`` is atomic and mode 600 (config.toml is hot-reloaded)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from foreman_inator import paths


def test_writes_mode_600_and_replaces_an_existing_looser_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("old")
    path.chmod(0o644)
    paths.write_secret_file(path, "new")
    assert path.read_text() == "new"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.toml"]


def test_a_write_that_fails_midway_leaves_the_old_file_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    paths.write_secret_file(path, 'repos = ["acme/widgets"]\n')
    real_fdopen = os.fdopen

    class Boom:
        def __init__(self, handle) -> None:
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            self.handle.close()

        def write(self, raw: bytes) -> int:
            self.handle.write(raw[:3])  # a partial write, then the process "dies"
            self.handle.flush()
            raise OSError("disk full")

        def __getattr__(self, name):
            return getattr(self.handle, name)

    monkeypatch.setattr(paths.os, "fdopen", lambda fd, mode: Boom(real_fdopen(fd, mode)))
    with pytest.raises(OSError, match="disk full"):
        paths.write_secret_file(path, 'repos = ["acme/widgets", "acme/gadgets"]\n')
    assert path.read_text() == 'repos = ["acme/widgets"]\n'
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.toml"]
