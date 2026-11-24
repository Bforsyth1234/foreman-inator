"""Tests for `fman setup` (#8): manifest flow, dependency check, launchd agent.

Nothing here reaches github.com, opens a browser, runs launchctl or writes outside
tmp_path: the browser is a function that drives the local callback server with httpx,
GitHub and Ollama are an httpx.MockTransport, and launchctl is a recorder.
"""

from __future__ import annotations

import html
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from foreman_inator import config, paths
from foreman_inator.setup import deps, launchd, manifest, run_setup, uninstall

SNAPSHOT_MANIFEST = {
    "name": "foreman-inator-test",
    "url": "https://github.com/Bforsyth1234/foreman-inator",
    "description": manifest.APP_DESCRIPTION,
    "redirect_url": "http://127.0.0.1:5555/callback",
    "public": False,
    "default_permissions": {
        "issues": "write",
        "pull_requests": "write",
        "contents": "write",
        "metadata": "read",
        # #41: GitHub CI on the PR head and its job logs
        "checks": "read",
        "actions": "read",
    },
}


@pytest.fixture(scope="module")
def pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


class FakeGitHub:
    """Just enough of api.github.com (and Ollama's /api/tags) for setup."""

    def __init__(self, pem: str, *, owner_type: str = "User", pulled: list[str] | None = None):
        self.pem = pem
        self.owner_type = owner_type
        self.requests: list[httpx.Request] = []
        self.installation_polls = 0
        self.installed_after = 2  # the App shows up on the second poll
        self.pulled = pulled if pulled is not None else ["inkling-2-mini:latest", "qwen3.8:27b"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = request.url
        path = url.path
        if url.host == "127.0.0.1" and path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": m, "model": m} for m in self.pulled]}
            )
        assert url.host == "api.github.test", url
        m = re.fullmatch(r"/app-manifests/([^/]+)/conversions", path)
        if m and request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "id": 4242,
                    "slug": "foreman-inator-test",
                    "name": "foreman-inator-test",
                    "html_url": "https://github.com/apps/foreman-inator-test",
                    "owner": {"login": "octocat", "type": self.owner_type},
                    "client_id": "Iv1.abc",
                    "client_secret": "not-kept",
                    "webhook_secret": None,
                    "pem": self.pem,
                    "permissions": dict(manifest.PERMISSIONS),
                },
            )
        if path == "/app":
            return httpx.Response(200, json={"slug": "foreman-inator-test"})
        if path == "/app/installations":
            self.installation_polls += 1
            if self.installation_polls < self.installed_after:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[{"id": 77, "account": {"login": "octocat"}}])
        if path == "/app/installations/77/access_tokens" and request.method == "POST":
            return httpx.Response(
                201, json={"token": "ghs_fake", "expires_at": "2099-01-01T00:00:00Z"}
            )
        if path == "/installation/repositories":
            assert request.headers["Authorization"] == "Bearer ghs_fake"
            page = int(url.params.get("page", "1"))
            repos = [["octocat/one", "octocat/two"], ["octocat/three"]][page - 1]
            return httpx.Response(
                200,
                json={"total_count": 3, "repositories": [{"full_name": r} for r in repos]},
            )
        return httpx.Response(404, json={"message": f"no fake for {request.method} {path}"})


class Browser:
    """Plays the user: follows the start page to GitHub and comes back with a code."""

    def __init__(self, code: str = "the-code") -> None:
        self.code = code
        self.opened: list[str] = []
        self.manifest: dict | None = None
        self.action: str | None = None

    def __call__(self, url: str) -> bool:
        self.opened.append(url)
        if not url.startswith("http://127.0.0.1"):
            return True  # the install page on github.com: just record it
        page = httpx.get(url).text
        self.action = html.unescape(re.search(r"action='([^']*)'", page).group(1))
        value = html.unescape(re.search(r"name='manifest' value='([^']*)'", page).group(1))
        self.manifest = json.loads(value)
        state = parse_qs(urlparse(self.action).query)["state"][0]
        callback = self.manifest["redirect_url"]
        resp = httpx.get(callback, params={"code": self.code, "state": state})
        assert resp.status_code == 200
        return True


class Launchctl:
    def __init__(self, bootstrap_rc: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.bootstrap_rc = bootstrap_rc

    def __call__(self, argv):
        self.calls.append(list(argv))
        rc = self.bootstrap_rc if argv[0] == "bootstrap" else 3
        return subprocess.CompletedProcess(["launchctl", *argv], rc, "", "boom" if rc else "")


def fake_which(present: dict[str, str]):
    return lambda name: present.get(name)


def fake_run(outputs: dict[str, str]):
    def run(argv):
        return outputs[argv[0]]

    return run


TOOLS = {"git": "/usr/bin/git", "opencode": "/opt/bin/opencode", "ollama": "/opt/bin/ollama"}
TOOL_OUT = {"git": "git version 2.50.1 (Apple Git-155)\n", "opencode": "1.2.3\n", "ollama": "0.9\n"}


def _fman(tmp_path: Path) -> Path:
    exe = tmp_path / "bin" / "fman"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return exe


def _setup(tmp_path: Path, gh: FakeGitHub, browser: Browser, lc: Launchctl, **kw):
    return run_setup(
        opener=browser,
        echo=lambda s: None,
        transport=httpx.MockTransport(gh),
        api_url="https://api.github.test",
        launchctl=lc,
        agents_dir=tmp_path / "LaunchAgents",
        fman_path=_fman(tmp_path) if not (tmp_path / "bin").exists() else tmp_path / "bin/fman",
        uid=501,
        which=fake_which(TOOLS),
        run=fake_run(TOOL_OUT),
        callback_timeout=10,
        sleep=lambda s: None,
        **kw,
    )


# --- manifest -------------------------------------------------------------------------


def test_manifest_matches_spec_permissions_snapshot():
    built = manifest.build_manifest("http://127.0.0.1:5555/callback", "foreman-inator-test")
    assert built == SNAPSHOT_MANIFEST
    # SPEC: Issues RW, Pull requests RW, Contents RW, Metadata R, plus (#41) Checks R and
    # Actions R. Nothing else.
    assert SNAPSHOT_MANIFEST["default_permissions"] == manifest.PERMISSIONS


def test_manifest_has_no_webhook_and_is_private():
    built = manifest.build_manifest("http://127.0.0.1:1/callback")
    assert "hook_attributes" not in built
    assert "default_events" not in built
    assert built["public"] is False
    assert built["name"].startswith("foreman-inator-")
    with pytest.raises(ValueError):
        manifest.build_manifest("http://x/callback", "x" * 35)


def test_new_app_and_install_urls():
    assert manifest.new_app_url("s t") == "https://github.com/settings/apps/new?state=s%20t"
    assert (
        manifest.new_app_url("abc", org="acme")
        == "https://github.com/organizations/acme/settings/apps/new?state=abc"
    )
    assert manifest.install_url("my-app") == "https://github.com/apps/my-app/installations/new"


def test_start_page_escapes_manifest():
    m = {"name": "a'b\"<c>", "default_permissions": {"issues": "write"}}
    page = manifest.start_page(m, "https://github.com/settings/apps/new?state=x&y")
    value = re.search(r"name='manifest' value='([^']*)'", page).group(1)
    assert json.loads(html.unescape(value)) == m
    assert "method='post'" in page


def test_callback_server_checks_state():
    with manifest.CallbackServer(app_name="foreman-inator-test") as server:
        assert server.manifest["redirect_url"] == server.base_url + "/callback"
        assert server.base_url.startswith("http://127.0.0.1:")
        bad = httpx.get(server.base_url + "/callback", params={"code": "c", "state": "wrong"})
        assert bad.status_code == 400
        with pytest.raises(manifest.ManifestFlowError):
            server.wait_for_code(0.05)
        good = httpx.get(
            server.base_url + "/callback", params={"code": "c1", "state": server.state}
        )
        assert good.status_code == 200
        assert server.wait_for_code(1) == "c1"


def test_convert_code_against_fake_endpoint(pem):
    gh = FakeGitHub(pem)
    creds = manifest.convert_code(
        "abc123", transport=httpx.MockTransport(gh), base_url="https://api.github.test"
    )
    req = gh.requests[0]
    assert req.method == "POST"
    assert req.url.path == "/app-manifests/abc123/conversions"
    assert "Authorization" not in req.headers
    assert creds.app_id == 4242
    assert creds.slug == "foreman-inator-test"
    assert creds.owner_login == "octocat"
    assert creds.pem == pem
    assert pem not in repr(creds)


def test_convert_code_error_is_raised(pem):
    def refuse(request):
        return httpx.Response(422, json={"message": "Validation Failed"})

    from foreman_inator.github.app_auth import GitHubError

    with pytest.raises(GitHubError):
        manifest.convert_code("x", transport=httpx.MockTransport(refuse))


# --- the whole flow -------------------------------------------------------------------


def test_full_setup_flow(tmp_path, pem):
    gh, browser, lc = FakeGitHub(pem), Browser(), Launchctl()
    result = _setup(tmp_path, gh, browser, lc)

    # The browser POSTed exactly the snapshot permissions to the personal-account page.
    assert browser.manifest["default_permissions"] == SNAPSHOT_MANIFEST["default_permissions"]
    assert browser.action.startswith("https://github.com/settings/apps/new?state=")
    assert browser.opened[1] == "https://github.com/apps/foreman-inator-test/installations/new"

    # Key saved with mode 600 under the (test) foreman-inator home.
    key = paths.app_key_path()
    assert key.read_text() == pem
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.home().stat().st_mode) == 0o700

    cfg = config.load()
    assert result.created_app
    assert cfg.app_id == 4242
    assert cfg.app_slug == "foreman-inator-test"
    assert cfg.installation_ids == (77,)
    assert cfg.allowed_users == ("octocat",)
    assert cfg.repos == ("octocat/one", "octocat/two", "octocat/three")
    assert "not-kept" not in paths.config_path().read_text()
    assert gh.installation_polls == 2

    assert all(c.ok for c in result.checks), result.checks

    plist = tmp_path / "LaunchAgents" / launchd.PLIST_NAME
    nightly = tmp_path / "LaunchAgents" / launchd.NIGHTLY_PLIST_NAME  # #29, next to it
    assert lc.calls == [
        ["bootout", f"gui/501/{launchd.LABEL}"],
        ["bootstrap", "gui/501", str(plist)],
        ["bootout", f"gui/501/{launchd.NIGHTLY_LABEL}"],
        ["bootstrap", "gui/501", str(nightly)],
    ]
    assert result.service.loaded and result.nightly.loaded


def test_rerun_reuses_app_and_keeps_entries(tmp_path, pem):
    gh, lc = FakeGitHub(pem), Launchctl()
    _setup(tmp_path, gh, Browser(), lc)
    browser2 = Browser()
    gh2 = FakeGitHub(pem)
    gh2.installed_after = 1
    result = _setup(tmp_path, gh2, browser2, lc, allowed_users=["friend"])
    assert not result.created_app
    assert browser2.opened == []  # no new App, already installed
    assert not any("app-manifests" in r.url.path for r in gh2.requests)
    cfg = config.load()
    assert cfg.allowed_users == ("octocat", "friend")
    assert cfg.installation_ids == (77,)
    assert cfg.repos == ("octocat/one", "octocat/two", "octocat/three")


def test_org_owner_is_not_an_allowed_user(tmp_path, pem):
    gh = FakeGitHub(pem, owner_type="Organization")
    _setup(tmp_path, gh, Browser(), Launchctl(), org="acme", allowed_users=["me"])
    assert config.load().allowed_users == ("me",)


def test_no_installation_yet_still_saves_app(tmp_path, pem):
    gh = FakeGitHub(pem)
    gh.installed_after = 10**6
    times = iter(range(0, 10**6, 100))
    result = run_setup(
        opener=Browser(),
        echo=lambda s: None,
        transport=httpx.MockTransport(gh),
        api_url="https://api.github.test",
        install_service=False,
        which=fake_which(TOOLS),
        run=fake_run(TOOL_OUT),
        install_timeout=300,
        clock=lambda: next(times),
        sleep=lambda s: None,
    )
    cfg = config.load()
    assert cfg.app_id == 4242 and cfg.installation_ids == () and cfg.repos == ()
    assert result.service is None


# --- dependencies ---------------------------------------------------------------------


def test_git_version_parsing_and_minimum():
    assert deps.parse_git_version("git version 2.31.0") == (2, 31, 0)
    assert deps.parse_git_version("git version 2.50.1 (Apple Git-155)") == (2, 50, 1)
    ok = deps.check_git(which=fake_which(TOOLS), run=lambda a: "git version 2.31.0\n")
    assert ok.ok
    old = deps.check_git(which=fake_which(TOOLS), run=lambda a: "git version 2.30.9\n")
    assert not old.ok and "older than 2.31" in old.detail and old.fix
    missing = deps.check_git(which=fake_which({}))
    assert not missing.ok and missing.fix == deps.INSTALL_HINTS["git"]


def test_missing_tools_print_install_commands_and_never_run_them(tmp_path):
    ran: list[list[str]] = []

    def run(argv):
        ran.append(list(argv))
        return "git version 2.40.0\n"

    gh = FakeGitHub("unused", pulled=["inkling-2-mini:latest"])  # Ollama's name when untagged
    checks = deps.check_all(
        config.Config(),
        which=fake_which({"git": "/usr/bin/git"}),
        run=run,
        transport=httpx.MockTransport(gh),
    )
    by = {c.name: c for c in checks}
    assert by["git"].ok
    assert not by["opencode"].ok and "opencode.ai" in by["opencode"].fix
    assert not by["ollama"].ok and "brew install ollama" in by["ollama"].fix
    assert by["model tier 1: inkling-2-mini"].ok
    assert by["model tier 2: qwen3.8:27b"].fix == "ollama pull qwen3.8:27b"
    assert ran == [["git", "--version"]]  # only version probes, no installs or pulls
    report = deps.format_report(checks)
    assert "run: ollama pull qwen3.8:27b" in report


def test_ollama_unreachable_is_reported():
    def down(request):
        raise httpx.ConnectError("refused", request=request)

    checks = deps.check_models(config.Config(), transport=httpx.MockTransport(down))
    assert len(checks) == 2
    assert all(not c.ok and "not reachable" in c.detail for c in checks)
    assert checks[0].fix.endswith("ollama pull inkling-2-mini")


def test_ollama_host_and_latest_tag():
    assert deps.ollama_host("http://127.0.0.1:11434/v1") == "http://127.0.0.1:11434"
    assert deps._matches("llama3", {"llama3:latest"})
    assert not deps._matches("qwen3.8:27b", {"qwen3.8:14b"})


# --- launchd --------------------------------------------------------------------------


def test_plist_runs_fman_run_at_login_and_keeps_alive(tmp_path):
    fman = _fman(tmp_path)
    data = launchd.plist_dict(fman, home=paths.home(), path_env="/usr/bin:/bin")
    assert data["Label"] == "com.foreman-inator.worker"
    assert data["ProgramArguments"] == [str(fman), "run"]
    assert Path(data["ProgramArguments"][0]).is_absolute()
    assert data["RunAtLoad"] is True and data["KeepAlive"] is True
    logs = paths.home() / "logs"
    assert data["StandardOutPath"] == str(logs / "worker.out.log")
    assert data["StandardErrorPath"] == str(logs / "worker.err.log")
    assert stat.S_IMODE(logs.stat().st_mode) == 0o700
    # The test home is set through FOREMAN_INATOR_HOME, so the agent must carry it.
    assert data["EnvironmentVariables"][paths.ENV_HOME] == str(paths.home())


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("plutil") is None, reason="plutil is macOS-only"
)
def test_generated_plist_passes_plutil_lint(tmp_path):
    lc = Launchctl()
    res = launchd.install_service(
        fman=_fman(tmp_path), agents_dir=tmp_path / "LaunchAgents", launchctl=lc, uid=501
    )
    out = subprocess.run(["plutil", "-lint", str(res.plist_path)], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert plistlib.loads(res.plist_path.read_bytes())["ProgramArguments"][1] == "run"


def test_bootstrap_failure_is_reported(tmp_path):
    res = launchd.install_service(
        fman=_fman(tmp_path), agents_dir=tmp_path / "LA", launchctl=Launchctl(5), uid=501
    )
    assert not res.loaded and "bootstrap failed (5)" in res.message and "boom" in res.message


def test_uninstall_boots_out_and_removes_plist(tmp_path):
    agents = tmp_path / "LaunchAgents"
    lc = Launchctl()
    launchd.install_service(fman=_fman(tmp_path), agents_dir=agents, launchctl=lc, uid=501)
    assert (agents / launchd.PLIST_NAME).exists()
    lc.calls.clear()
    res = uninstall(echo=lambda s: None, launchctl=lc, agents_dir=agents, uid=501)
    assert lc.calls == [  # the nightly experiments agent (#29) goes with it
        ["bootout", f"gui/501/{launchd.LABEL}"],
        ["bootout", f"gui/501/{launchd.NIGHTLY_LABEL}"],
    ]
    assert not (agents / launchd.PLIST_NAME).exists()
    assert "removed" in res.message
    again = uninstall(echo=lambda s: None, launchctl=lc, agents_dir=agents, uid=501)
    assert "was not installed" in again.message


def test_resolve_fman(tmp_path):
    exe = _fman(tmp_path)
    assert launchd.resolve_fman(which=lambda n: str(exe)) == exe
    assert os.path.isabs(launchd.resolve_fman(which=lambda n: str(exe)))


def test_setup_command_is_typer_registrable(tmp_path, monkeypatch):
    import typer
    from typer.testing import CliRunner

    import foreman_inator.setup as setup_pkg

    calls = []
    monkeypatch.setattr(setup_pkg, "uninstall", lambda **kw: calls.append(kw))
    app = typer.Typer()
    app.command("setup")(setup_pkg.setup_command)

    @app.command("other")
    def other() -> None: ...

    result = CliRunner().invoke(app, ["setup", "--uninstall-service"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    # CI terminals make rich colour and wrap the help, which splits the flag names.
    plain_env = {"NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "200"}
    raw_help = CliRunner().invoke(app, ["setup", "--help"], env=plain_env).output
    help_text = re.sub(r"\x1b\[[0-9;]*m", "", raw_help)
    for flag in ("--uninstall-service", "--org", "--app-name", "--allowed-user", "--no-service"):
        assert flag in help_text


def test_setup_prints_the_permissions_upgrade_for_an_old_app(tmp_path, pem):
    """#41, wired at wave 6b's close: an App created before `checks: read` and
    `actions: read` were added gets the upgrade text from `fman setup`."""
    old = {k: v for k, v in manifest.PERMISSIONS.items() if k not in ("checks", "actions")}

    class OldApp(FakeGitHub):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/app/installations/77" and request.method == "GET":
                self.requests.append(request)
                return httpx.Response(200, json={"id": 77, "permissions": old})
            return super().__call__(request)

    out: list[str] = []
    gh = OldApp(pem)
    run_setup(
        opener=Browser(),
        echo=out.append,
        transport=httpx.MockTransport(gh),
        api_url="https://api.github.test",
        install_service=False,
        which=fake_which(TOOLS),
        run=fake_run(TOOL_OUT),
        callback_timeout=10,
        sleep=lambda s: None,
    )
    text = "\n".join(out)
    assert "missing permissions" in text and "Checks: read" in text and "Actions: read" in text
    assert "/settings/installations/77" in text
