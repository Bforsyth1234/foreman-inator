"""``fman setup``: register the user's own GitHub App, check dependencies, install launchd.

``run_setup`` holds the logic with every outside effect injectable (browser, HTTP,
launchctl, LaunchAgents folder, PATH lookups), so tests run it end to end without a
terminal. ``setup_command`` is the thin typer entry the CLI registers.

Re-running is safe: an App already in config.toml (with ``app.pem`` present) is reused,
and new installations are appended.

Which repos the worker works on is the user's choice (#56), never "everything the
installation can see" (GitHub's install page defaults to "All repositories"):
``choose_repos`` decides, ``parse_choice`` reads a typed answer, and the ``fman repo
add | remove | list`` helpers (``repo_add_command`` and friends) edit the list later.
On a re-run the ``repos`` list is left exactly as it is.
"""

from __future__ import annotations

import re
import sys
import time
import webbrowser
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Annotated

import httpx
import typer

from foreman_inator import config, paths
from foreman_inator.github.app_auth import API_URL, AppAuth, GitHubError, Installation
from foreman_inator.setup import deps, launchd, manifest

Echo = Callable[[str], None]
#: Asks the user one question and returns the typed answer (``typer.prompt`` in the CLI).
Ask = Callable[[str], str]
#: The repos the App's installations can see (GitHub spelling), or None when there are no
#: App credentials to ask with. Injectable so tests never call GitHub.
AccessCheck = Callable[[config.Config], "list[str] | None"]
_opt = typer.Option

#: #56: on a first setup, an installation covering at most this many repos that the user
#: picked one by one on GitHub ("Only select repositories") is taken as the choice. More
#: than that, or "All repositories" at any size, and setup asks instead. Five is about
#: what one person picks by hand for a first try, and 5 repos cost 400 issue listings an
#: hour at the default poll interval, far inside the installation's rate limit.
FEW_REPOS = 5
_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
_MAX_ASKS = 3
_SHOWN = 10  # repo names printed inline before "... and N more"
RESTART_TEXT = (
    "The worker reads config.toml only when it starts. Restart it to pick this up:\n"
    "  launchctl kickstart -k gui/$(id -u)/com.foreman-inator.worker\n"
    "(If you run `fman run` by hand instead, stop it with Ctrl-C and start it again.)"
)


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
    relay_url: str | None = None,
) -> manifest.AppCredentials:
    """Run the manifest flow in the browser and return the new App's credentials.
    ``relay_url`` (#15): the App is registered with its webhook pointed there."""
    with manifest.CallbackServer(org=org, app_name=app_name, relay_url=relay_url) as server:
        echo(f"Opening your browser to create the App: {server.start_url}")
        opener(server.start_url)
        code = server.wait_for_code(timeout)
    creds = manifest.convert_code(code, transport=transport, base_url=api_url)
    unexpected = {k: v for k, v in creds.permissions.items() if manifest.PERMISSIONS.get(k) != v}
    if creds.permissions and unexpected:
        echo(f"Warning: GitHub reports permissions beyond the manifest: {unexpected}")
    return creds


def _with_relay(cfg: config.Config, relay_url: str, secret: str, echo: Echo) -> config.Config:
    """#15: save ``relay_url`` and the secret's Keychain reference; store ``secret`` (the
    one GitHub returned when it created the App) under ``relay-webhook-secret``."""
    from foreman_inator.github.relay import DEFAULT_SECRET_NAME

    if secret:
        ref = config.set_key(DEFAULT_SECRET_NAME, secret)
        echo(f"Webhook relay: {relay_url}; the App's webhook secret is in the Keychain.")
    else:
        ref = config.key_ref(DEFAULT_SECRET_NAME)
        echo(
            "Webhook relay saved. The App already exists, so set its webhook URL to the relay "
            "and its secret in the App's settings on GitHub, then store the same secret with "
            f"`fman config set-key {DEFAULT_SECRET_NAME}` (see docs/relay.md)."
        )
    return replace(cfg, relay_url=relay_url, relay_secret_ref=ref)


def run_setup(
    *,
    opener: manifest.Opener = webbrowser.open,
    echo: Echo = print,
    org: str | None = None,
    app_name: str | None = None,
    allowed_users: Sequence[str] = (),
    repos: Sequence[str] | None = None,
    ask: Ask | None = None,
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
    relay_url: str | None = None,
) -> SetupResult:
    """``repos``: the repos to work on (``--repo``), replacing the list; None leaves the
    choice to ``choose_repos`` (ask through ``ask`` on a first run, or keep the list).
    ``ask`` None means no terminal: nothing is asked and the list is not widened.
    ``relay_url`` (#15, ``--relay-url``): a new App gets its webhook pointed at the relay,
    and the webhook secret GitHub returns is stored in the Keychain
    (``relay-webhook-secret``); ``relay_url`` and ``relay_secret_ref`` are saved."""
    cfg = config.load()
    on_disk = cfg
    key_path = paths.app_key_path()
    result = SetupResult(cfg)

    # 1. The App: reuse the one in config.toml, or register a new one.
    if cfg.app_id is not None and key_path.exists():
        echo(f"Using the App already configured (id {cfg.app_id}).")
        if allowed_users:
            cfg = manifest.add_allowed_users(cfg, allowed_users)
        if relay_url and relay_url != cfg.relay_url:
            cfg = _with_relay(cfg, relay_url, "", echo)
    else:
        creds = register_app(
            opener=opener,
            echo=echo,
            org=org,
            app_name=app_name,
            transport=transport,
            api_url=api_url,
            timeout=callback_timeout,
            relay_url=relay_url,
        )
        paths.write_app_key(creds.pem)
        cfg = manifest.apply_app(cfg, creds, allowed_users)
        if relay_url:
            cfg = _with_relay(cfg, relay_url, creds.webhook_secret, echo)
        config.save(cfg)  # keep the App even if a later step fails
        on_disk = cfg
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
    first_run = not cfg.installation_ids  # no installation recorded yet: nothing chosen
    accessible: list[str] = []
    for inst in installations:
        accessible.extend(manifest.installation_repos(auth, inst.id))
    details = _installation_details(auth, installations, echo=echo)
    cfg = manifest.apply_installations(cfg, installations)  # ids only (#56)
    if installations:
        choice = choose_repos(
            cfg.repos,
            accessible,
            first_run=first_run,
            selection=_selection(details, installations),
            given=repos,
            ask=ask,
            echo=echo,
        )
        cfg = replace(cfg, repos=choice.repos)
        access_text = manifest.repository_access_text(
            [details.get(i.id) or i.id for i in installations]
        )
        for line in choice_lines(choice, access_text=access_text):
            echo(line)
    elif repos:
        echo("No installation yet, so --repo was not applied; run `fman setup` again.")
    if cfg != on_disk:  # a re-run that changes nothing leaves the file byte for byte
        config.save(cfg)
        on_disk = cfg
    if installations:
        _check_permissions(cfg, installations, details, org=org, echo=echo)
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


def _installation_details(
    auth: AppAuth, installations: Sequence[Installation], *, echo: Echo
) -> dict[int, manifest.InstallationDetails]:
    out: dict[int, manifest.InstallationDetails] = {}
    for inst in installations:
        try:
            out[inst.id] = manifest.installation_details(auth, inst.id)
        except GitHubError as exc:
            echo(f"Could not read the details of installation {inst.id}: {exc}")
    return out


def _selection(
    details: dict[int, manifest.InstallationDetails], installations: Sequence[Installation]
) -> str:
    """The selection: "selected" only when every installation says so; "all" when any
    covers all repos; "" when GitHub did not say (treated as "ask")."""
    kinds = [details[i.id].repository_selection if i.id in details else "" for i in installations]
    if "all" in kinds:
        return "all"
    return "selected" if kinds and all(k == "selected" for k in kinds) else ""


def _check_permissions(
    cfg: config.Config,
    installations: Sequence[Installation],
    details: dict[int, manifest.InstallationDetails],
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
        if inst.id not in details:
            continue  # already reported by _installation_details
        gap = manifest.missing_permissions(details[inst.id].permissions)
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


# --- which repos (#56) ------------------------------------------------------------


@dataclass(frozen=True)
class RepoChoice:
    """What ``choose_repos`` decided. ``how``: ``given`` (``--repo``), ``kept`` (a re-run:
    the list is left as it is), ``picked_on_github`` (a first run on a few repos chosen
    one by one), ``prompted``, ``none_yet`` (a first run with nobody to ask), or
    ``nothing_accessible``."""

    repos: tuple[str, ...]
    how: str
    accessible: int = 0
    added: tuple[str, ...] = ()
    available: tuple[str, ...] = ()  # accessible but not in ``repos``: listed, never added
    refused: tuple[str, ...] = ()  # asked for, but the installation cannot see them


def _dedupe(names: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return tuple(out)


def match_repos(names: Iterable[str], accessible: Sequence[str]) -> tuple[list[str], list[str]]:
    """Split ``names`` into those the installation can see (in GitHub's spelling; names are
    case-insensitive on GitHub) and the rest."""
    by_lower = {r.lower(): r for r in accessible}
    found: list[str] = []
    unknown: list[str] = []
    for n in names:
        hit = by_lower.get(n.lower())
        (found if hit else unknown).append(hit or n)
    return list(_dedupe(found)), list(_dedupe(unknown))


def parse_choice(text: str, accessible: Sequence[str]) -> tuple[list[str], list[str]]:
    """A typed answer to the setup question: ``owner/name`` entries or 1-based numbers
    from the printed list, separated by commas or spaces; ``all`` takes every listed
    repo; an empty answer is no repos. Returns (chosen, entries not understood)."""
    tokens = [t for t in re.split(r"[\s,]+", text.strip()) if t]
    if [t.lower() for t in tokens] == ["all"]:
        return list(_dedupe(accessible)), []
    names: list[str] = []
    bad: list[str] = []
    for t in tokens:
        if t.isdigit():
            i = int(t)
            (names.append(accessible[i - 1]) if 1 <= i <= len(accessible) else bad.append(t))
        else:
            names.append(t)
    found, unknown = match_repos(names, accessible)
    return found, bad + unknown


def _not_in(names: Iterable[str], chosen: Iterable[str]) -> tuple[str, ...]:
    have = {c.lower() for c in chosen}
    return _dedupe(n for n in names if n.lower() not in have)


def choose_repos(
    current: Sequence[str],
    accessible: Sequence[str],
    *,
    first_run: bool,
    selection: str = "",
    given: Sequence[str] | None = None,
    ask: Ask | None = None,
    echo: Echo = print,
    few: int = FEW_REPOS,
) -> RepoChoice:
    """Decide the ``repos`` list. Never widens it on its own:

    - ``given`` (``--repo``): exactly those the installation can see; the rest refused.
    - a re-run (``first_run`` False): ``current`` unchanged; new repos are ``available``.
    - a first run: at most ``few`` repos picked one by one on GitHub (``selection ==
      "selected"``) are added; otherwise ``ask`` is asked (default: none), and with no
      ``ask`` (no terminal) nothing is added."""
    current = tuple(current)
    n = len(accessible)

    def done(repos: Sequence[str], how: str, **kw: tuple[str, ...]) -> RepoChoice:
        return RepoChoice(tuple(repos), how, n, available=_not_in(accessible, repos), **kw)

    if given is not None:
        found, refused = match_repos(given, accessible)
        return done(found, "given", added=_not_in(found, current), refused=tuple(refused))
    if not first_run:
        return done(current, "kept")
    if not accessible:
        return done(current, "nothing_accessible")
    if n <= few and selection == "selected":
        repos = _dedupe([*current, *accessible])
        return done(repos, "picked_on_github", added=_not_in(repos, current))
    if ask is None:
        return done(current, "none_yet")
    echo(f"The App's installation can see {n} repo(s):")
    for i, r in enumerate(accessible, 1):
        echo(f"  {i:>4}. {r}")
    chosen: list[str] = []
    for _ in range(_MAX_ASKS):
        answer = ask(
            "Which repos should the worker work on? Names or numbers, separated by commas"
            " or spaces ('all' for every one; press Enter for none yet)"
        )
        chosen, bad = parse_choice(answer, accessible)
        if not bad:
            break
        echo(f"Not in the list: {', '.join(bad)}. Try again.")
        chosen = []
    repos = _dedupe([*current, *chosen])
    return done(repos, "prompted", added=_not_in(repos, current))


def _names(repos: Sequence[str], limit: int = _SHOWN) -> str:
    shown = ", ".join(repos[:limit])
    return shown + (f", ... and {len(repos) - limit} more" if len(repos) > limit else "")


def _add_hint() -> str:
    return (
        "Add the ones to work on with `fman repo add OWNER/NAME ...`"
        " (or `fman setup --repo OWNER/NAME`)."
    )


def choice_lines(choice: RepoChoice, *, access_text: str = "") -> list[str]:
    """What setup prints about the repos it chose (or did not)."""
    out: list[str] = []
    many = choice.accessible > FEW_REPOS
    if choice.how == "given":
        out.append(f"Working on {len(choice.repos)} repo(s): {_names(choice.repos) or '(none)'}.")
        if choice.refused:
            out.append(
                f"Not added, the App's installation cannot see: {', '.join(choice.refused)}."
            )
            if access_text:
                out.append(access_text)
    elif choice.how == "kept":
        out.append(
            f"Repos in config.toml left as they are ({len(choice.repos)}): "
            f"{_names(choice.repos) or '(none)'}."
        )
        if choice.available:
            out.append(
                f"{len(choice.available)} more repo(s) the installation can see are not in"
                " config.toml and were not added (`fman repo list --available` lists them;"
                " `fman repo add OWNER/NAME` adds one)."
            )
    elif choice.how == "picked_on_github":
        out.append(
            f"The installation covers {choice.accessible} repo(s) you selected on GitHub;"
            f" working on: {_names(choice.repos)}."
        )
        out.append("`fman repo remove OWNER/NAME` takes one out.")
    elif choice.how == "prompted":
        if choice.repos:
            out.append(f"Working on {len(choice.repos)} repo(s): {_names(choice.repos)}.")
        else:
            out.append("No repos chosen yet, so the worker has nothing to poll. " + _add_hint())
        if many and access_text:
            out.append(access_text)
    elif choice.how == "none_yet":
        out.append(
            f"The App's installation can see {choice.accessible} repo(s); none were added"
            " (no terminal to ask in). " + _add_hint()
        )
        if access_text:
            out.append(access_text)
    elif choice.how == "nothing_accessible":
        out.append("The installation covers no repos yet.")
        if access_text:
            out.append(access_text)
    if len(choice.repos) > config.REPOS_WARN_THRESHOLD:
        out.append(_oversized_text(len(choice.repos)))
    return out


def _oversized_text(n: int, interval: int = config.Config.poll_interval_seconds) -> str:
    per_hour = config.listing_requests_per_hour(n, interval)
    return (
        f"Warning: {n} repos is a lot to watch: about {per_hour:,} GitHub requests an hour"
        " just to list issues. `fman repo remove OWNER/NAME` takes repos out."
    )


# --- `fman repo add | remove | list` (#56) --------------------------------------------


class RepoCommandError(ValueError):
    """A bad repo name, or a repo the installation cannot see."""


def check_repo_name(name: str) -> str:
    name = name.strip()
    if not _REPO_RE.match(name):
        raise RepoCommandError(f"{name!r} must look like owner/name")
    return name


def installation_access(
    cfg: config.Config,
    *,
    transport: httpx.BaseTransport | None = None,
    api_url: str = API_URL,
) -> list[str] | None:
    """Every repo the App's installations can see (one ``/installation/repositories``
    listing per installation), or None without App credentials. Calls GitHub."""
    key = paths.app_key_path()
    if cfg.app_id is None or not cfg.installation_ids or not key.exists():
        return None
    auth = AppAuth(cfg.app_id, key, transport=transport, base_url=api_url)
    out: list[str] = []
    for inst in cfg.installation_ids:
        out.extend(manifest.installation_repos(auth, inst))
    return out


def _grant_text(cfg: config.Config) -> str:
    return manifest.repository_access_text(list(cfg.installation_ids))


def add_repos(
    cfg: config.Config, names: Sequence[str], accessible: Sequence[str] | None
) -> tuple[config.Config, list[str]]:
    """``cfg`` with ``names`` appended (GitHub's spelling when checked), and the lines to
    print. ``accessible`` None: not checked. Raises ``RepoCommandError`` when any name is
    malformed or not visible to the installation; then nothing is added."""
    names = [check_repo_name(n) for n in names]
    lines: list[str] = []
    if accessible is not None:
        found, unknown = match_repos(names, accessible)
        if unknown:
            raise RepoCommandError(
                f"The App's installation cannot see {', '.join(unknown)}, so the worker"
                " would only get errors polling it. Nothing was added.\n"
                + _grant_text(cfg)
                + "\nThen run this again (or pass --no-check to add it anyway)."
            )
        names = found
    new = _not_in(names, cfg.repos)
    already = [n for n in names if n not in new]
    if already:
        lines.append(f"Already in config.toml: {', '.join(already)}.")
    if new:
        cfg = replace(cfg, repos=(*cfg.repos, *new))
        lines.append(f"Added {', '.join(new)}. config.toml now lists {len(cfg.repos)} repo(s).")
    return cfg, lines


def remove_repos(
    cfg: config.Config, names: Sequence[str], open_tickets: Callable[[str], int] | None = None
) -> tuple[config.Config, list[str]]:
    """``cfg`` without ``names`` (case-insensitive), and the lines to print. Tickets of a
    removed repo are not touched: they stay in the database, and the worker (which polls
    and dispatches only configured repos) leaves them until the repo is added back."""
    names = [check_repo_name(n) for n in names]
    lower = {n.lower() for n in names}
    gone = [r for r in cfg.repos if r.lower() in lower]
    missing = [n for n in names if n.lower() not in {g.lower() for g in gone}]
    lines: list[str] = []
    if missing:
        lines.append(f"Not in config.toml: {', '.join(missing)}.")
    if gone:
        cfg = replace(cfg, repos=tuple(r for r in cfg.repos if r.lower() not in lower))
        lines.append(f"Removed {', '.join(gone)}. config.toml now lists {len(cfg.repos)} repo(s).")
        for r in gone:
            n = open_tickets(r) if open_tickets else 0
            lines.append(
                f"{r}: {n} open ticket(s). Tickets stay in the database (nothing is deleted)"
                " and nothing on GitHub is changed. Once restarted, the worker stops polling"
                " the repo and starts no work on its tickets; add it back to resume them."
            )
    return cfg, lines


def list_repo_lines(
    cfg: config.Config,
    accessible: Sequence[str] | None,
    *,
    available: bool = False,
    unchecked: str = "no App credentials",
) -> list[str]:
    """``fman repo list``: the configured repos, each marked when the installation no
    longer covers it; with ``available``, the covered repos not configured."""
    if not cfg.repos:
        lines = ["config.toml lists no repos: the worker has nothing to poll."]
    else:
        covered = {r.lower() for r in accessible} if accessible is not None else None
        lines = [f"{len(cfg.repos)} repo(s) in config.toml:"]
        for r in cfg.repos:
            mark = ""
            if covered is not None and r.lower() not in covered:
                mark = "  (the installation no longer covers it: polling it fails)"
            lines.append(f"  {r}{mark}")
    if len(cfg.repos) > config.REPOS_WARN_THRESHOLD:
        lines.append(_oversized_text(len(cfg.repos), cfg.poll_interval_seconds))
    if accessible is None:
        lines.append(f"(Not checked against the installation: {unchecked}.)")
        return lines
    rest = _not_in(accessible, cfg.repos)
    if available:
        lines.append(f"{len(rest)} more repo(s) the installation can see, not configured:")
        lines += [f"  {r}" for r in rest]
    elif rest:
        lines.append(
            f"{len(rest)} more repo(s) the installation can see are not configured"
            " (--available lists them)."
        )
    return lines


def _open_tickets(repo: str) -> int:
    """Open tickets of ``repo`` in the worker's database (0 when there is none)."""
    db = paths.db_path()
    if not db.exists():
        return 0
    try:
        from foreman_inator.db.store import Store

        with Store(db, read_only=True) as store:
            return len(store.list_tickets(column=("todo", "in_progress", "testing"), repo=repo))
    except Exception:
        return 0


def _access_or_error(
    cfg: config.Config, access: AccessCheck | None
) -> tuple[list[str] | None, str | None]:
    """``access`` None: ``installation_access`` (looked up at call time, so tests can
    replace it on the module)."""
    try:
        return (access or installation_access)(cfg), None
    except (GitHubError, httpx.HTTPError, OSError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def repo_add_command(
    names: Sequence[str],
    *,
    check: bool = True,
    access: AccessCheck | None = None,
    echo: Echo = print,
    path: Path | None = None,
) -> int:
    """``fman repo add``. Exit code."""
    cfg = config.load(path)
    accessible: list[str] | None = None
    if check:
        accessible, err = _access_or_error(cfg, access)
        if err:
            echo(
                f"Could not check what the App's installation can see ({err}). Nothing was"
                " added; pass --no-check to add without checking."
            )
            return 1
        if accessible is None:
            echo("No App credentials yet (`fman setup`), so access was not checked.")
    try:
        new, lines = add_repos(cfg, names, accessible)
    except RepoCommandError as exc:
        echo(str(exc))
        return 1
    for line in lines:
        echo(line)
    if new != cfg:
        config.save(new, path)
        if len(new.repos) > config.REPOS_WARN_THRESHOLD:
            echo(_oversized_text(len(new.repos), new.poll_interval_seconds))
        echo(RESTART_TEXT)
    return 0


def repo_remove_command(
    names: Sequence[str],
    *,
    echo: Echo = print,
    path: Path | None = None,
    open_tickets: Callable[[str], int] = _open_tickets,
) -> int:
    """``fman repo remove``. Exit code (1 when no name given was configured)."""
    cfg = config.load(path)
    try:
        new, lines = remove_repos(cfg, names, open_tickets)
    except RepoCommandError as exc:
        echo(str(exc))
        return 1
    for line in lines:
        echo(line)
    if new == cfg:
        return 1
    config.save(new, path)
    echo(RESTART_TEXT)
    return 0


def repo_list_command(
    *,
    available: bool = False,
    check: bool = True,
    access: AccessCheck | None = None,
    echo: Echo = print,
    path: Path | None = None,
) -> int:
    """``fman repo list``. Read-only. Exit code."""
    cfg = config.load(path)
    accessible: list[str] | None = None
    err = None
    if check:
        accessible, err = _access_or_error(cfg, access)
    unchecked = err or ("--no-check" if not check else "no App credentials")
    lines = list_repo_lines(cfg, accessible, available=available, unchecked=unchecked)
    for line in lines:
        echo(line)
    return 0


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


def _prompt(question: str) -> str:
    return str(typer.prompt(question, default="", show_default=False))


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
    repo: Annotated[
        list[str] | None,
        _opt(
            "--repo",
            help="OWNER/NAME to work on (repeatable); replaces the repos list, no prompt.",
            show_default=False,
        ),
    ] = None,
    relay_url: Annotated[
        str | None,
        _opt(
            "--relay-url",
            help="Webhook relay (https) whose deliveries trigger an early poll (docs/relay.md).",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Register your own GitHub App, check dependencies and install the launchd agent.

    Repos are never added on their own: on a first run setup asks which ones to work on
    (or takes --repo); a re-run leaves the repos list as it is."""
    if uninstall_service:
        uninstall(echo=typer.echo)
        return
    users = list(allowed_user or [])
    if org and not users:
        users = [typer.prompt("GitHub login allowed to start work (yours)")]
    given = None
    if repo:
        try:
            given = [check_repo_name(r) for r in repo]
        except RepoCommandError as exc:
            raise typer.BadParameter(str(exc), param_hint="--repo") from exc
    if relay_url is not None:
        from foreman_inator.github.relay import RelayConfigError, check_relay_url

        try:
            relay_url = check_relay_url(relay_url)
        except RelayConfigError as exc:
            raise typer.BadParameter(str(exc), param_hint="--relay-url") from exc
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    try:
        run_setup(
            echo=typer.echo,
            org=org,
            app_name=app_name,
            allowed_users=users,
            repos=given,
            ask=_prompt if interactive else None,
            install_service=not no_service,
            relay_url=relay_url,
        )
    except (manifest.ManifestFlowError, launchd.ServiceError, config.ConfigError) as exc:
        typer.echo(f"Setup stopped: {exc}", err=True)
        raise typer.Exit(1) from exc


__all__ = [
    "FEW_REPOS",
    "RESTART_TEXT",
    "RepoChoice",
    "RepoCommandError",
    "SetupResult",
    "add_repos",
    "check_repo_name",
    "choice_lines",
    "choose_repos",
    "installation_access",
    "list_repo_lines",
    "match_repos",
    "parse_choice",
    "register_app",
    "remove_repos",
    "repo_add_command",
    "repo_list_command",
    "repo_remove_command",
    "run_setup",
    "setup_command",
    "uninstall",
]
