"""Packaging checks (#31): what a published wheel and sdist would contain.

Builds the real distributions with ``uv build`` into ``tmp_path`` (never into the repo),
then checks package data, entry points and dependency metadata, and runs the built
wheel's own ``fman`` / ``foreman-inator`` entry points. The wheel is unpacked with
``--no-deps`` on top of this test environment, so no network is needed for that part.
If the build itself cannot run (no ``uv``, or it needs the network and there is none),
the tests skip instead of failing.
"""

from __future__ import annotations

import email
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PKG_SRC = REPO / "src" / "foreman_inator"
ENTRY_POINTS = {"foreman-inator", "fman"}


def _online() -> bool:
    try:
        socket.create_connection(("pypi.org", 443), timeout=3).close()
    except OSError:
        return False
    return True


@pytest.fixture(scope="module")
def dists(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    out = tmp_path_factory.mktemp("dist")
    proc = subprocess.run(
        [uv, "build", "--no-sources", "--out-dir", str(out), str(REPO)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        if not _online():
            pytest.skip(f"uv build needs the network and there is none:\n{proc.stderr}")
        pytest.fail(f"uv build failed:\n{proc.stderr}")
    wheels = list(out.glob("*.whl"))
    sdists = list(out.glob("*.tar.gz"))
    assert len(wheels) == 1 and len(sdists) == 1, list(out.iterdir())
    return {"wheel": wheels[0], "sdist": sdists[0], "dir": out}


def _package_data() -> set[str]:
    files = [*PKG_SRC.glob("prompts/*.md"), *PKG_SRC.glob("db/migrations/*.sql")]
    assert files, "no package data found in the source tree"
    return {p.relative_to(PKG_SRC.parent).as_posix() for p in files}


def _wheel_metadata(wheel: Path) -> email.message.Message:
    with zipfile.ZipFile(wheel) as zf:
        name = next(n for n in zf.namelist() if n.endswith(".dist-info/METADATA"))
        return email.message_from_bytes(zf.read(name))


def test_wheel_ships_every_prompt_and_migration_and_no_tests(dists: dict[str, Path]) -> None:
    with zipfile.ZipFile(dists["wheel"]) as zf:
        names = set(zf.namelist())
    missing = _package_data() - names
    assert not missing, f"package data missing from the wheel: {sorted(missing)}"
    assert "foreman_inator/cli.py" in names
    leaked = sorted(
        n
        for n in names
        if n.startswith("tests/")
        or "/tests/" in n
        or n.endswith("conftest.py")
        or "__pycache__" in n
        or n.endswith(".pyc")
    )
    assert not leaked, f"wheel contains test or cache files: {leaked}"


def test_sdist_ships_package_data_and_license(dists: dict[str, Path]) -> None:
    with tarfile.open(dists["sdist"]) as tf:
        names = {m.name.split("/", 1)[1] for m in tf.getmembers() if "/" in m.name}
    missing = {f"src/{p}" for p in _package_data()} - names
    assert not missing, f"package data missing from the sdist: {sorted(missing)}"
    assert {"LICENSE", "README.md", "pyproject.toml", "PKG-INFO"} <= names
    assert not any(n.startswith("tests/") for n in names)


def test_wheel_entry_points(dists: dict[str, Path]) -> None:
    with zipfile.ZipFile(dists["wheel"]) as zf:
        name = next(n for n in zf.namelist() if n.endswith(".dist-info/entry_points.txt"))
        text = zf.read(name).decode()
    assert "[console_scripts]" in text
    for script in ENTRY_POINTS:
        assert f"{script} = foreman_inator.cli:app" in text


def test_wheel_metadata_is_uploadable(dists: dict[str, Path]) -> None:
    """PyPI refuses direct URL requirements; the license must be the SPDX expression."""
    meta = _wheel_metadata(dists["wheel"])
    assert meta["Name"] == "foreman-inator"
    assert meta["License-Expression"] == "MIT"
    assert meta["Requires-Python"] == ">=3.12"
    requires = meta.get_all("Requires-Dist") or []
    direct = [r for r in requires if " @ " in r or "://" in r]
    assert not direct, f"direct URL requirements would be refused by PyPI: {direct}"
    # Documents the known gap (docs/releasing.md): fde-kit is a bare name in the wheel,
    # resolvable only where fde-kit is already installed or published.
    assert "fde-kit" in requires
    urls = meta.get_all("Project-URL") or []
    assert any(
        u.startswith("Repository, https://github.com/Bforsyth1234/foreman-inator") for u in urls
    )


def test_built_wheel_entry_points_run(dists: dict[str, Path], tmp_path: Path) -> None:
    """Unpack the wheel (no deps) and run its own entry points; every command is listed."""
    uv = shutil.which("uv")
    assert uv is not None
    target = tmp_path / "site"
    subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--no-config",
            "--python",
            sys.executable,
            "--target",
            str(target),
            str(dists["wheel"]),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    # Run each console script exactly as its entry point says, with the unpacked wheel
    # first on sys.path so it shadows the editable source install.
    runner = (
        "import sys\n"
        "from importlib.metadata import distribution\n"
        "import foreman_inator\n"
        f"assert foreman_inator.__file__.startswith({str(target)!r}), foreman_inator.__file__\n"
        "name = sys.argv.pop(1)\n"
        "ep = next(e for e in distribution('foreman-inator').entry_points if e.name == name)\n"
        "sys.argv[0] = name\n"
        "ep.load()()\n"
    )
    env = {**os.environ, "PYTHONPATH": str(target), "COLUMNS": "200", "NO_COLOR": "1"}

    def run(*args: str) -> str:
        proc = subprocess.run(
            [sys.executable, "-c", runner, *args],
            capture_output=True,
            text=True,
            env=env,
            cwd=tmp_path,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    from foreman_inator import __version__

    for script in sorted(ENTRY_POINTS):
        assert run(script, "--version").strip() == f"foreman-inator {__version__}"

    import typer

    from foreman_inator.cli import app

    commands = set(typer.main.get_command(app).commands)  # type: ignore[attr-defined]
    assert {"run", "status", "setup"} <= commands
    help_text = run("fman", "--help")
    missing = sorted(c for c in commands if c not in help_text)
    assert not missing, f"`fman --help` from the wheel does not list: {missing}"
