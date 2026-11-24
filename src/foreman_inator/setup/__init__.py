"""``fman setup``: register the user's own GitHub App, check dependencies, install launchd.

``run_setup`` holds the logic with every outside effect injectable (browser, HTTP,
launchctl, LaunchAgents folder, PATH lookups), so tests run it end to end without a
terminal. ``setup_command`` is the thin typer entry the CLI registers.

Re-running is safe: an App already in config.toml (with ``app.pem`` present) is reused,
and new installations and repos are appended.
"""

from __future__ import annotations

import time
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Annotated

import httpx
import typer

from foreman_inator import config, paths
from foreman_inator.github.app_auth import API_URL, AppAuth, GitHubError, Installation
from foreman_inator.setup import deps, launchd, manifest

Echo = Callable[[str], None]
_opt = typer.Option


@dataclass
class SetupResult:
    config: config.Config
    created_app: bool = False
    checks: list[deps.Check] = field(default_factory=list)
    service: launchd.ServiceResult | None = None
    nightly: launchd.ServiceResult | None = None  # #29: `fman experiment run --all`


def register_app(
    *,
    opener: manifest.Opener,
    echo: Echo,
    org: str | None = None,
    app_name: str | None = None,
    transport: httpx.BaseTransport | None = None,
    api_url: str = API_URL,
    timeout: float = manifest.CALLBACK_TIMEOUT_SECONDS,
) -> manifest.AppCredentials:
    """Run the manifest flow in the browser and return the new App's credentials."""
    with manifest.CallbackServer(org=org, app_name=app_name) as server:
        echo(f"Opening your browser to create the App: {server.start_url}")
        opener(server.start_url)
        code = server.wait_for_code(timeout)
    creds = manifest.convert_code(code, transport=transport, base_url=api_url)
    unexpected = {k: v for k, v in creds.permissions.items() if manifest.PERMISSIONS.get(k) != v}
    if creds.permissions and unexpected:
        echo(f"Warning: GitHub reports permissions beyond the manifest: {unexpected}")
    return creds


def run_setup(
    *,
    opener: manifest.Opener = webbrowser.open,
    echo: Echo = print,
    org: str | None = None,
    app_name: str | None = None,
    allowed_users: Sequence[str] = (),
    transport: httpx.BaseTransport | None = None,
    api_url: str = API_URL,
    install_service: bool = True,
    launchctl: launchd.Launchctl | None = None,
    agents_dir: Path | None = None,
    fman_path: Path | None = None,
    uid: int | None = None,
    which: deps.Which | None = None,
    run: deps.Runner | None = None,
    callback_timeout: float = manifest.CALLBACK_TIMEOUT_SECONDS,
    install_timeout: float = manifest.INSTALL_TIMEOUT_SECONDS,
    poll_interval: float = manifest.INSTALL_POLL_SECONDS,
    clock: manifest.Clock = time.monotonic,
    sleep: manifest.Sleep = time.sleep,
) -> SetupResult:
    cfg = config.load()
    key_path = paths.app_key_path()
    result = SetupResult(cfg)

    # 1. The App: reuse the one in config.toml, or register a new one.
    if cfg.app_id is not None and key_path.exists():
        echo(f"Using the App already configured (id {cfg.app_id}).")
        if allowed_users:
            cfg = manifest.add_allowed_users(cfg, allowed_users)
    else:
        creds = register_app(
            opener=opener,
            echo=echo,
            org=org,
            app_name=app_name,
            transport=transport,
            api_url=api_url,
            timeout=callback_timeout,
        )
        paths.write_app_key(creds.pem)
        cfg = manifest.apply_app(cfg, creds, allowed_users)
        config.save(cfg)  # keep the App even if a later step fails
        result.created_app = True
        echo(f"Created App {creds.slug} (id {creds.app_id}); key saved to {key_path}.")
    if not cfg.allowed_users:
        echo(
            "Warning: allowed_users is empty, so no label will start work. "
            "Re-run with --allowed-user <login>."
        )

    auth = AppAuth(cfg.app_id, key_path, transport=transport, base_url=api_url, sleep=sleep)
    if not cfg.app_slug:
        cfg = replace(cfg, app_slug=auth.app_slug())

    # 2. Installations and repos.
    installations = auth.list_installations()
    if not installations:
        url = manifest.install_url(cfg.app_slug)
        echo(f"Install the App on the repos it may work on: {url}")
        opener(url)
        installations = manifest.wait_for_installations(
            auth, timeout=install_timeout, interval=poll_interval, clock=clock, sleep=sleep
        )
    repos: list[str] = []
    for inst in installations:
        repos.extend(manifest.installation_repos(auth, inst.id))
    cfg = manifest.apply_installations(cfg, installations, repos)
    config.save(cfg)
    if installations:
        echo(f"Installed on: {', '.join(repos) or '(no repos selected)'}")
        _check_permissions(auth, cfg, installations, org=org, echo=echo)
    else:
        echo("No installation yet. Install the App, then run `fman setup` again.")

    # 3. Dependencies (report only; nothing is installed).
    dep_kw = {k: v for k, v in (("which", which), ("run", run)) if v is not None}
    result.checks = deps.check_all(cfg, transport=transport, **dep_kw)
    echo("Dependencies:\n" + deps.format_report(result.checks))

    # 4. launchd agents: the worker, and the nightly experiments run (#29).
    if install_service:
        svc_kw = {k: v for k, v in (("launchctl", launchctl), ("uid", uid)) if v is not None}
        result.service = launchd.install_service(fman=fman_path, agents_dir=agents_dir, **svc_kw)
        echo(result.service.message)
        ex = cfg.experiments
        result.nightly = launchd.install_nightly(
            fman=fman_path,
            agents_dir=agents_dir,
            hour=ex.nightly_hour,
            minute=ex.nightly_minute,
            **svc_kw,
        )
        echo(result.nightly.message)

    result.config = cfg
    echo(f"Config written to {paths.config_path()}.")
    return result


def _check_permissions(
    auth: AppAuth,
    cfg: config.Config,
    installations: Sequence[Installation],
    *,
    org: str | None,
    echo: Echo,
) -> dict[str, str]:
    """#41: an App created before ``checks: read`` / ``actions: read`` were added lacks
    them; print how to add them (the worker meanwhile skips CI with one warning).
    Returns the missing permissions (union over installations)."""
    missing: dict[str, str] = {}
    lacking: list[int] = []
    for inst in installations:
        try:
            granted = manifest.installation_permissions(auth, inst.id)
        except GitHubError as exc:
            echo(f"Could not read the permissions of installation {inst.id}: {exc}")
            continue
        gap = manifest.missing_permissions(granted)
        if gap:
            missing.update(gap)
            lacking.append(inst.id)
    if missing:
        echo(
            manifest.permissions_upgrade_text(
                cfg.app_slug, missing, org=org, installation_ids=lacking
            )
        )
    return missing


def uninstall(
    *,
    echo: Echo = print,
    launchctl: launchd.Launchctl | None = None,
    agents_dir: Path | None = None,
    uid: int | None = None,
) -> launchd.ServiceResult:
    kw = {k: v for k, v in (("launchctl", launchctl), ("uid", uid)) if v is not None}
    res = launchd.uninstall_service(agents_dir=agents_dir, **kw)
    echo(res.message)
    echo(launchd.uninstall_nightly(agents_dir=agents_dir, **kw).message)
    return res


def setup_command(
    uninstall_service: Annotated[
        bool, _opt("--uninstall-service", help="Remove both launchd agents and exit.")
    ] = False,
    org: Annotated[
        str | None, _opt("--org", help="Create the App under this organization.")
    ] = None,
    app_name: Annotated[
        str | None, _opt("--app-name", help="App name (unique on GitHub, max 34 chars).")
    ] = None,
    allowed_user: Annotated[
        list[str] | None,
        _opt("--allowed-user", help="GitHub login allowed to start work (repeatable)."),
    ] = None,
    no_service: Annotated[
        bool, _opt("--no-service", help="Skip installing the launchd agents.")
    ] = False,
) -> None:
    """Register your own GitHub App, check dependencies and install the launchd agent."""
    if uninstall_service:
        uninstall(echo=typer.echo)
        return
    users = list(allowed_user or [])
    if org and not users:
        users = [typer.prompt("GitHub login allowed to start work (yours)")]
    try:
        run_setup(
            echo=typer.echo,
            org=org,
            app_name=app_name,
            allowed_users=users,
            install_service=not no_service,
        )
    except (manifest.ManifestFlowError, launchd.ServiceError, config.ConfigError) as exc:
        typer.echo(f"Setup stopped: {exc}", err=True)
        raise typer.Exit(1) from exc


__all__ = ["SetupResult", "register_app", "run_setup", "setup_command", "uninstall"]
