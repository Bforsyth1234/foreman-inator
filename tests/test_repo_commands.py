"""`fman repo add | remove | list`, the worker's oversized-list warning and the repo count
in `fman status` (#56).

Every config here is a real config.toml under the test's FOREMAN_INATOR_HOME (tmp_path).
What the App's installation can see is injected; GitHub is never called (the one test of
``installation_access`` itself uses an httpx.MockTransport).
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from typer.testing import CliRunner

import foreman_inator.setup as setup_pkg
from foreman_inator import config, paths
from foreman_inator.cli import app
from foreman_inator.db.store import Store
from foreman_inator.worker_run import repo_count_warning, run_worker
from foreman_inator.worker_status import render_status, repos_line

VISIBLE = ["acme/widgets", "acme/Gadgets", "acme/tools"]
KICKSTART = "launchctl kickstart -k gui/$(id -u)/com.foreman-inator.worker"
PLAIN = {"NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "200"}


def _base_config() -> config.Config:
    """A config with many non-default settings, all of which must survive a repo edit."""
    return config.Config(
        app_id=4242,
        app_slug="foreman-inator-test",
        installation_ids=(77,),
        allowed_users=("octocat", "friend"),
        repos=("acme/widgets",),
        poll_interval_seconds=60,
        max_parallel_per_repo=2,
        sandbox_network={"acme/widgets": "allow"},
        clarify=False,
        explore_rate=0.0,
    )


def _access(repos=VISIBLE):
    calls: list[config.Config] = []

    def access(cfg: config.Config) -> list[str]:
        calls.append(cfg)
        return list(repos)

    access.calls = calls  # type: ignore[attr-defined]
    return access


def _out():
    lines: list[str] = []
    return lines, lines.append


def test_add_remove_list_round_trip_through_config_file():
    base = _base_config()
    config.save(base)
    access = _access()

    lines, echo = _out()
    assert setup_pkg.repo_add_command(["acme/gadgets", "acme/tools"], access=access, echo=echo) == 0
    cfg = config.load()
    assert cfg.repos == ("acme/widgets", "acme/Gadgets", "acme/tools")  # GitHub's spelling
    assert replace(cfg, repos=base.repos) == base  # every other setting kept
    text = "\n".join(lines)
    assert "Added acme/Gadgets, acme/tools" in text and KICKSTART in text
    assert len(access.calls) == 1

    lines, echo = _out()
    assert (
        setup_pkg.repo_list_command(access=_access(["acme/widgets", "acme/tools"]), echo=echo) == 0
    )
    text = "\n".join(lines)
    assert "3 repo(s) in config.toml" in text
    assert re.search(r"acme/Gadgets\s+\(the installation no longer covers it", text)
    assert not re.search(r"acme/widgets\s+\(", text)

    lines, echo = _out()
    code = setup_pkg.repo_remove_command(["ACME/GADGETS"], echo=echo, open_tickets=lambda repo: 0)
    assert code == 0
    cfg = config.load()
    assert cfg.repos == ("acme/widgets", "acme/tools")
    assert replace(cfg, repos=base.repos) == base
    assert KICKSTART in "\n".join(lines)


def test_add_refuses_a_repo_the_installation_cannot_see():
    config.save(_base_config())
    raw = paths.config_path().read_bytes()
    lines, echo = _out()
    code = setup_pkg.repo_add_command(["acme/tools", "acme/secret"], access=_access(), echo=echo)
    assert code == 1
    assert paths.config_path().read_bytes() == raw  # nothing added, not even acme/tools
    text = "\n".join(lines)
    assert "cannot see acme/secret" in text and "Nothing was added" in text
    assert "https://github.com/settings/installations/77" in text
    assert "Repository access" in text and "--no-check" in text


def test_add_no_check_skips_the_access_check():
    config.save(_base_config())

    def boom(cfg):
        raise AssertionError("access checked")

    lines, echo = _out()
    assert setup_pkg.repo_add_command(["acme/secret"], check=False, access=boom, echo=echo) == 0
    assert config.load().repos == ("acme/widgets", "acme/secret")


def test_add_when_the_check_fails_writes_nothing():
    config.save(_base_config())
    raw = paths.config_path().read_bytes()

    def down(cfg):
        raise httpx.ConnectError("refused")

    lines, echo = _out()
    assert setup_pkg.repo_add_command(["acme/tools"], access=down, echo=echo) == 1
    assert paths.config_path().read_bytes() == raw
    assert "--no-check" in "\n".join(lines)


def test_add_without_app_credentials_adds_and_says_unchecked():
    lines, echo = _out()  # no config.toml at all
    assert setup_pkg.repo_add_command(["acme/tools"], access=lambda c: None, echo=echo) == 0
    assert config.load().repos == ("acme/tools",)
    assert "access was not checked" in "\n".join(lines)


def test_add_is_idempotent_and_validates_names():
    config.save(_base_config())
    raw = paths.config_path().read_bytes()
    lines, echo = _out()
    assert setup_pkg.repo_add_command(["acme/widgets"], access=_access(), echo=echo) == 0
    assert "Already in config.toml: acme/widgets" in "\n".join(lines)
    assert paths.config_path().read_bytes() == raw  # nothing to write, nothing written
    lines, echo = _out()
    assert setup_pkg.repo_add_command(["not a repo"], access=_access(), echo=echo) == 1
    assert "must look like owner/name" in "\n".join(lines)


def test_remove_keeps_tickets_and_says_so():
    config.save(_base_config())
    with Store(paths.db_path()) as store:
        store.create_ticket(
            repo="acme/widgets", issue_number=7, title="t", request="r", base_branch="main"
        )
    lines, echo = _out()
    assert setup_pkg.repo_remove_command(["acme/widgets"], echo=echo) == 0  # real store
    assert config.load().repos == ()
    text = "\n".join(lines)
    assert "acme/widgets: 1 open ticket(s)" in text
    assert "stay in the database" in text and "stops polling" in text
    with Store(paths.db_path(), read_only=True) as store:
        assert len(store.list_tickets(repo="acme/widgets")) == 1  # data kept


def test_remove_a_repo_not_configured_changes_nothing():
    config.save(_base_config())
    raw = paths.config_path().read_bytes()
    lines, echo = _out()
    assert setup_pkg.repo_remove_command(["acme/other"], echo=echo) == 1
    assert paths.config_path().read_bytes() == raw
    assert "Not in config.toml: acme/other" in "\n".join(lines)


def test_list_available_and_unchecked():
    config.save(_base_config())
    lines, echo = _out()
    setup_pkg.repo_list_command(available=True, access=_access(), echo=echo)
    text = "\n".join(lines)
    assert "2 more repo(s) the installation can see, not configured:" in text
    assert "  acme/Gadgets" in text and "  acme/tools" in text
    lines, echo = _out()
    setup_pkg.repo_list_command(check=False, access=None, echo=echo)
    assert "(Not checked against the installation: --no-check.)" in lines


def test_installation_access_lists_every_installation(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    paths.write_app_key(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
    )

    def gh(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.github.test"
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "t", "expires_at": "2099-01-01T00:00:00Z"})
        assert request.url.path == "/installation/repositories"
        return httpx.Response(
            200, json={"total_count": 1, "repositories": [{"full_name": "acme/widgets"}]}
        )

    cfg = config.Config(app_id=1, installation_ids=(5, 6))
    got = setup_pkg.installation_access(
        cfg, transport=httpx.MockTransport(gh), api_url="https://api.github.test"
    )
    assert got == ["acme/widgets", "acme/widgets"]
    assert setup_pkg.installation_access(config.Config()) is None  # no credentials


def test_cli_repo_commands(monkeypatch):
    config.save(_base_config())
    monkeypatch.setattr(setup_pkg, "installation_access", _access())
    runner = CliRunner()
    res = runner.invoke(app, ["repo", "add", "acme/tools"], env=PLAIN)
    assert res.exit_code == 0, res.output
    assert KICKSTART in res.output
    res = runner.invoke(app, ["repo", "add", "acme/secret"], env=PLAIN)
    assert res.exit_code == 1 and "cannot see acme/secret" in res.output
    res = runner.invoke(app, ["repo", "list"], env=PLAIN)
    assert res.exit_code == 0 and "2 repo(s) in config.toml" in res.output
    res = runner.invoke(app, ["repo", "remove", "acme/tools"], env=PLAIN)
    assert res.exit_code == 0 and "acme/tools: 0 open ticket(s)" in res.output
    assert config.load().repos == ("acme/widgets",)
    helps = {
        cmd: re.sub(
            r"\x1b\[[0-9;]*m", "", runner.invoke(app, ["repo", cmd, "--help"], env=PLAIN).output
        )
        for cmd in ("add", "remove", "list")
    }
    assert "--no-check" in helps["add"]
    assert "--available" in helps["list"] and "--no-check" in helps["list"]


# --- the worker's startup warning and `fman status` -----------------------------------


def _repos(n: int) -> tuple[str, ...]:
    return tuple(f"acme/r{i}" for i in range(n))


def test_startup_warning_fires_above_the_threshold_only():
    t = config.REPOS_WARN_THRESHOLD
    assert repo_count_warning(config.Config(repos=_repos(t))) is None
    msg = repo_count_warning(config.Config(repos=_repos(143)))
    assert msg is not None
    assert "lists 143 repos" in msg and "about 11,440 GitHub requests an hour" in msg
    assert "fman repo remove" in msg


@pytest.mark.parametrize(("n", "warned"), [(config.REPOS_WARN_THRESHOLD, False), (143, True)])
def test_run_worker_logs_the_warning_once(n, warned, caplog):
    config.save(config.Config(repos=_repos(n)))  # no App: the worker stops right after
    with caplog.at_level(logging.WARNING, logger="foreman_inator.worker"):
        assert run_worker(once=True) == 1
    hits = [r for r in caplog.records if "GitHub requests an hour" in r.getMessage()]
    assert len(hits) == (1 if warned else 0)


def test_status_shows_the_repo_count():
    assert (
        repos_line(config.Config(repos=_repos(1))) == "Repos: 1 in config.toml (`fman repo list`)."
    )
    big = repos_line(config.Config(repos=_repos(143)))
    assert big.startswith("Repos: 143 in config.toml") and "11,440" in big
    with Store(paths.db_path()) as store:
        text = render_status(store, config=config.Config(repos=_repos(2)))
    assert "Repos: 2 in config.toml" in text
