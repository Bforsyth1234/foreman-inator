"""``fman run``: build the worker's real collaborators from config and paths (todo #14).

GitHub clients (one per App installation; several are routed by repo), the store, the
local traces, the model proxy on 127.0.0.1 (a free port), the OpenCode driver and the
worktree manager. SIGINT and SIGTERM stop the worker cleanly (exit 0). Logs go to stdout,
which the launchd agent writes to ``~/.foreman-inator/logs/``; one line per state change,
never tokens, keys or file contents.
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


def run_worker(*, once: bool = False) -> int:
    """``fman run``: build the real collaborators and work until stopped. Exit code."""
    from foreman_inator import config as config_mod
    from foreman_inator import paths
    from foreman_inator.proxy.server import ModelProxy
    from foreman_inator.runner.opencode import OpenCodeDriver
    from foreman_inator.runner.worktree import GitIdentity

    _setup_logging()
    cfg = config_mod.load()
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
    proxy = ModelProxy(cfg, store=store).start()
    worker = Worker(
        api=api,
        store=store,
        config=cfg,
        worktrees=WorktreeManager(
            paths.worktrees_dir(),
            identity=GitIdentity(bot, f"{bot}@users.noreply.github.com"),
        ),
        driver=OpenCodeDriver(),
        proxy=proxy,
        recordings_dir=paths.recordings_dir(),
        traces_dir=paths.traces_dir(),
    )
    old = install_signal_handlers(worker)
    try:
        worker.recover()
        if once:
            worker.run_once()
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


__all__ = ["build_github", "install_signal_handlers", "run_worker"]
