"""``fman run``: build the worker's real collaborators from config and paths (#14, #21).

GitHub clients (one per App installation; several are routed by repo), the store, the
local traces, the spend caps (#18) and the model proxy on 127.0.0.1 (a free port) with
the caps as its spend hook (same store, same ``Spend`` instance the worker asks), the
OpenCode driver and the worktree manager. Before the first tick, one ``opencode serve``
is started and stopped (``warm_up_opencode``) so parallel attempts never race on
OpenCode's first plugin install in the shared state directory. SIGINT and SIGTERM stop
the worker cleanly (exit 0). Logs go to stdout, which the launchd agent writes to
``~/.foreman-inator/logs/``; one line per state change, never tokens, keys or file
contents.
"""

from __future__ import annotations

import logging
import signal
import sys
from pathlib import Path
from typing import Any

from foreman_inator import tracing
from foreman_inator.config import Config
from foreman_inator.db.store import Store
from foreman_inator.runner.worktree import WorktreeManager
from foreman_inator.spend import Spend
from foreman_inator.worker import Worker

log = logging.getLogger("foreman_inator.worker")


class _RoutedGitHub:
    """Several App installations: send each call to the client of the repo's installation."""

    def __init__(self, clients: dict[str, Any], default: Any) -> None:
        self._clients = clients
        self._default = default

    def token_for(self, repo: str) -> str:
        return self._clients.get(repo, self._default).token()

    def __getattr__(self, name: str) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            repo = args[0] if args else kwargs.get("repo")
            return getattr(self._clients.get(repo, self._default), name)(*args, **kwargs)

        return call


def build_github(cfg: Config, key_path: Path) -> Any:
    from foreman_inator.github.client import GitHubClient
    from foreman_inator.setup import manifest

    bot = f"{cfg.app_slug}[bot]" if cfg.app_slug else None
    clients = [
        GitHubClient.for_installation(cfg.app_id, key_path, i, bot_login=bot)  # type: ignore[arg-type]
        for i in cfg.installation_ids
    ]
    if len(clients) == 1:
        return clients[0]
    by_repo: dict[str, Any] = {}
    for inst, client in zip(cfg.installation_ids, clients, strict=True):
        for repo in manifest.installation_repos(client.auth.app, inst):
            by_repo[repo] = client
    return _RoutedGitHub(by_repo, clients[0])


def _setup_logging() -> None:
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=logging.INFO,
            stream=sys.stdout,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
    for noisy in ("httpx", "httpcore", "uvicorn", "uvicorn.error"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def install_signal_handlers(worker: Worker) -> dict[int, Any]:
    """SIGINT and SIGTERM stop the worker cleanly. Returns the previous handlers."""

    def handler(signum: int, _frame: Any) -> None:
        worker.request_stop(f"signal {signal.Signals(signum).name}")

    old = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        old[sig] = signal.signal(sig, handler)
    return old


def warm_up_opencode(driver: Any, cfg: Config, proxy: Any) -> bool:
    """Start one ``opencode serve`` in a throwaway directory, open one session, stop it.
    OpenCode installs its plugins into the shared state directory on its first start;
    two first starts at once can race. Never raises: a failure is only logged (the
    first real attempt then reports the problem).

    The session matters: OpenCode writes into the shared config dir
    (``config/opencode/.gitignore``) on its first *session*, not on start. A sandboxed
    attempt (#40) has that dir read-only, so without this every attempt on a fresh
    install failed with ``POST /session: 500`` (seen live 2026-11-23, OpenCode 1.18.7)."""
    import secrets
    import shutil
    import tempfile

    from foreman_inator.runner.opencode import OpenCodeServer, OpenCodeSession, build_config

    state = driver.state_root / "attempts" / f"warm-up-{secrets.token_hex(3)}"
    try:
        with tempfile.TemporaryDirectory(prefix="fman-warmup-") as tmp:
            work = Path(tmp) / "work"
            work.mkdir()
            server = OpenCodeServer(
                work,
                config=build_config(cfg.models[0], proxy.base_url(0)),
                attempt_dir=state,
                shared_dir=driver.state_root / "shared",
                opencode_bin=driver.opencode_bin,
                startup_timeout=driver.startup_timeout,
                base_env=driver.base_env,
            )
            try:
                url = server.start()
                api = OpenCodeSession(url, directory=work, auth=server.auth)
                try:
                    api.create("foreman-inator warm-up")
                finally:
                    api.close()
            finally:
                server.stop()
        log.info("opencode warm-up done")
        return True
    except Exception as exc:
        log.warning("opencode warm-up failed: %s: %s", type(exc).__name__, exc)
        return False
    finally:
        shutil.rmtree(state, ignore_errors=True)


def repo_count_warning(cfg: Config) -> str | None:
    """#56: one warning when ``repos`` is larger than ``REPOS_WARN_THRESHOLD`` (setup once
    wrote every repo of an "All repositories" installation), with the request estimate."""
    from foreman_inator import config as config_mod

    n = len(cfg.repos)
    if n <= config_mod.REPOS_WARN_THRESHOLD:
        return None
    per_hour = config_mod.listing_requests_per_hour(n, cfg.poll_interval_seconds)
    return (
        f"config.toml lists {n} repos (more than {config_mod.REPOS_WARN_THRESHOLD}): about"
        f" {per_hour:,} GitHub requests an hour for the issue listings alone (one per repo"
        f" every {cfg.poll_interval_seconds}s; an installation's limit starts at 5,000 an"
        " hour). Check the list with `fman repo list`, take repos out with `fman repo"
        " remove OWNER/NAME`, then restart the worker"
    )


def run_worker(*, once: bool = False) -> int:
    """``fman run``: build the real collaborators and work until stopped. Exit code."""
    from foreman_inator import config as config_mod
    from foreman_inator import paths
    from foreman_inator.proxy.server import ModelProxy
    from foreman_inator.runner.opencode import OpenCodeDriver
    from foreman_inator.runner.worktree import GitIdentity

    _setup_logging()
    cfg = config_mod.load()
    too_many = repo_count_warning(cfg)
    if too_many:
        log.warning(too_many)
    key_path = paths.app_key_path()
    if cfg.app_id is None or not cfg.installation_ids or not key_path.exists():
        log.error("no GitHub App configured or installed: run `fman setup` first")
        return 1
    if not cfg.repos:
        log.warning("config.toml lists no repos; nothing to poll")
    if not cfg.allowed_users:
        log.warning("allowed_users is empty: no label will start work")
    store = Store(paths.db_path())
    tracing.init(traces_dir=paths.traces_dir())
    api = build_github(cfg, key_path)
    bot = f"{cfg.app_slug}[bot]" if cfg.app_slug else "foreman-inator[bot]"
    spend = Spend(
        store,
        cfg,
        attempt_budget_usd=cfg.attempt_budget_usd,
        attempt_max_input_tokens_cloud=cfg.attempt_max_input_tokens_cloud,
        attempt_max_input_tokens_local=cfg.attempt_max_input_tokens_local,
    )
    # The embedding model (#23) is served next to the writers but is never a rung.
    proxy = ModelProxy(cfg.proxy_models(), store=store, spend_hook=spend.hook).start()
    driver = OpenCodeDriver()
    if not once:
        warm_up_opencode(driver, cfg, proxy)
    worker = Worker(
        api=api,
        store=store,
        config=cfg,
        worktrees=WorktreeManager(
            paths.worktrees_dir(),
            identity=GitIdentity(bot, f"{bot}@users.noreply.github.com"),
        ),
        driver=driver,
        proxy=proxy,
        spend=spend,
        recordings_dir=paths.recordings_dir(),
        traces_dir=paths.traces_dir(),
    )
    old = install_signal_handlers(worker)
    try:
        worker.recover()
        if once:
            worker.run_once()
            worker.drain_background(timeout=120.0)  # labels and shadows of this pass
        else:
            worker.serve_forever()
    finally:
        worker.shutdown()
        proxy.stop()
        store.close()
        for sig, h in old.items():
            signal.signal(sig, h)
        log.info("worker stopped")
    return 0


__all__ = [
    "build_github",
    "install_signal_handlers",
    "repo_count_warning",
    "run_worker",
    "warm_up_opencode",
]
