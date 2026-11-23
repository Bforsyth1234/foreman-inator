"""foreman-inator: a GitHub App plus a local worker that routes issues to models."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("foreman-inator")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0"

__all__ = ["__version__"]
