"""Where foreman-inator keeps secrets and local data.

Everything lives under one home directory, ``~/.foreman-inator/`` by default
(override with the ``FOREMAN_INATOR_HOME`` environment variable). Every folder
is created with mode 700 and the GitHub App key is written with mode 600, so
code, traces and keys stay readable only by the user (SPEC: "Open source and
distribution").

Each function returns an absolute path and creates the folder it needs.
"""

import os
from pathlib import Path

ENV_HOME = "FOREMAN_INATOR_HOME"
DIR_MODE = 0o700
SECRET_FILE_MODE = 0o600

APP_KEY_NAME = "app.pem"
CONFIG_NAME = "config.toml"
DB_NAME = "foreman-inator.db"


def _ensure_dir(path: Path) -> Path:
    """Create ``path`` (and parents) and force mode 700, whatever the umask."""
    path.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    os.chmod(path, DIR_MODE)
    return path


def home() -> Path:
    """The foreman-inator home directory, created with mode 700."""
    override = os.environ.get(ENV_HOME)
    base = Path(override).expanduser() if override else Path.home() / ".foreman-inator"
    return _ensure_dir(base.resolve())


def app_key_path() -> Path:
    """Path of the GitHub App private key (the file itself may not exist yet)."""
    return home() / APP_KEY_NAME


def config_path() -> Path:
    """Path of ``config.toml`` (the file itself may not exist yet)."""
    return home() / CONFIG_NAME


def db_path() -> Path:
    """Path of the SQLite database (the file itself may not exist yet)."""
    return home() / DB_NAME


def traces_dir() -> Path:
    """Folder for traces and the full outputs saved next to them (mode 700)."""
    return _ensure_dir(home() / "traces")


def recordings_dir() -> Path:
    """Folder for run recordings (mode 700)."""
    return _ensure_dir(home() / "recordings")


def worktrees_dir() -> Path:
    """Folder holding one git worktree per attempt (mode 700)."""
    return _ensure_dir(home() / "worktrees")


def write_secret_file(path: Path, data: str | bytes) -> Path:
    """Write ``data`` to ``path`` with mode 600, never briefly world-readable."""
    raw = data.encode() if isinstance(data, str) else data
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, SECRET_FILE_MODE)
    os.fchmod(fd, SECRET_FILE_MODE)  # an existing file keeps its old mode otherwise
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
    return path


def write_app_key(pem: str | bytes) -> Path:
    """Save the GitHub App private key as ``app.pem`` with mode 600."""
    return write_secret_file(app_key_path(), pem)
