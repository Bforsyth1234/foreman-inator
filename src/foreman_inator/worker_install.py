"""The dependency-install step of an attempt (todo #45), as a ``Worker`` mixin.

``InstallSteps.prepare_worktree(ticket, attempt, worktree)`` overrides the attempt seam
(slot A's ``worker_setup``, wave 11): once per worktree, before the writer starts, it
installs the repo's dependencies so the writer's own test runs and the worker's verify
find them (``runner.install``: detection, policy, cache). It raises
``worker_setup.SetupFailed(reason, detail)`` when the install fails or times out, with the
end of the install log in the detail (reason ``dependency_install``), so the ticket is
flagged instead of attempted (#53); the full log is
``traces/<ticket>/attempt-<id>-install.log``, beside the verify log.

Skipped (returns, nothing run): the repo has ``sandbox_install = "off"``; nothing to
install was detected and none was saved (``fman repo set-install``); the worktree was
installed already (its marker).

Policy (``runner.install`` has the full statement): the attempt's sandbox with writes to
the worktree and (base-branch files only) the repo's package cache, no home or
foreman-inator reads, and outbound TCP to any host on ports 443/80 only while the worktree
holds exactly the base branch's files; after a model edit (an attempt that builds on an
earlier attempt's branch) it runs offline from a snapshot of the cache. A repo with
``sandbox_network = "allow"`` gets full network. ``sandbox = "off"``: unsandboxed.

Cache miss (#57): an offline install that fails because the repo's cache lacks a package
(an empty or deleted cache) is retried once after filling the cache from a scratch
worktree at the base commit, when the change leaves the lockfiles alone; when it edits
them the flag says new dependencies need the user.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from foreman_inator.db.store import Ticket
from foreman_inator.runner import install, sandbox

log = logging.getLogger("foreman_inator.worker")

#: Untracked install output the worker's own commit must never pick up, whatever the
#: repo's .gitignore says (uv and virtualenv also write ``*`` into their venv's
#: .gitignore; ``node_modules/`` is in ``worker_attempt.EXCLUDES`` already).
INSTALL_EXCLUDES: tuple[str, ...] = (".venv/", ".build/")

#: ``SetupFailed.reason`` for a failed install (the flag: ``setup_failed:
#: dependency_install: <detail>``), and how much of the log's end goes in the detail
#: (``worker_setup`` clips the detail to one short line for the issue comment).
SETUP_REASON = "dependency_install"
DETAIL_TAIL = 300

#: #57: refill an empty cache from the base commit when an offline install misses it.
#: Default for ``config.install_refill_cache`` until that key is wired. Off for now: the
#: existing ``test_install.py::test_after_a_model_edit_the_install_is_offline`` asserts
#: the old behaviour (cold cache + model edit -> flagged, the index never reached) and
#: must be updated in the same step that turns this on.
REFILL_ON_CACHE_MISS = False


def _setup_failed() -> type[Exception]:
    """``worker_setup.SetupFailed`` (slot A, wave 11), imported when needed."""
    try:
        from foreman_inator.worker_setup import SetupFailed  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - only before the wave's modules are joined
        return _FallbackSetupFailed
    return SetupFailed  # type: ignore[no-any-return]


class _FallbackSetupFailed(RuntimeError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class InstallSteps:
    # Provided by Worker.
    store: Any
    config: Any
    traces_dir: Path

    def prepare_worktree(self, ticket: Ticket, attempt: Any, worktree: Any) -> None:
        """The attempt seam: earlier setup (``super()``, when one defines it), then the
        dependency install. Raises ``SetupFailed`` on an install failure."""
        parent = getattr(super(), "prepare_worktree", None)
        if parent is not None:
            parent(ticket, attempt, worktree)
        self.install_dependencies(ticket, attempt, worktree)

    def install_line(self) -> str:
        """The dependency-install sentence for ``fman status`` and the PR description."""
        return sandbox.describe_install(self.config.sandbox, dict(self.config.sandbox_install))

    def install_dependencies(
        self, ticket: Ticket, attempt: Any, worktree: Any
    ) -> install.InstallResult | None:
        """Install ``worktree``'s dependencies (see the module docstring). None when the
        step was skipped."""
        repo = ticket.repo
        if self.config.sandbox_install_for(repo) == "off":
            log.info("ticket %d: dependency install off for %s", ticket.id, repo)
            return None
        wt_path = Path(getattr(worktree, "path", worktree))
        settings = self.store.get_repo_settings(repo)
        command = install.detect(wt_path, settings.install_command if settings else None)
        if command is None or install.installed(wt_path, command):
            return None
        _exclude_install_output(worktree, wt_path)
        att_id = getattr(attempt, "id", attempt)
        log_path = self.traces_dir / str(ticket.id) / f"attempt-{att_id}-install.log"
        policy = self._sandbox_policy(  # type: ignore[attr-defined]
            ticket, wt_path, f"a{att_id}-install", writer=False
        )
        base_sha = getattr(worktree, "base_sha", None)
        network = "off"
        try:
            if policy.mode == "off":
                network, cache = "off", install.cache_root(repo)
            else:
                sandbox.ensure_available(policy)  # SandboxUnavailable: flagged by the caller
                base = install.is_base(wt_path, base_sha)
                network = install.network_for(policy, base=base)
                cache = install.cache_root(repo) if base else None
            result = install.run(
                command,
                wt_path,
                policy=policy,
                network=network,
                cache=cache,
                log_path=log_path,
                timeout=self.config.install_timeout_seconds,
            )
        except install.InstallFailed as exc:
            if network != "none" or not install.is_cache_miss(install.failure_text(exc)):
                raise self._install_failed(ticket, exc, log_name=log_path.name) from exc
            result = self._recover_cache_miss(ticket, att_id, worktree, wt_path, command, exc)
        finally:
            self._drop_scratch(policy)  # type: ignore[attr-defined]
        log.info(
            "ticket %d: dependencies installed (%s, network %s, %.1fs)",
            ticket.id, command.display, result.network, result.seconds,
        )  # fmt: skip
        return result

    def _install_failed(
        self, ticket: Ticket, exc: install.InstallFailed, note: str = "", log_name: str = ""
    ) -> Exception:
        """The ``SetupFailed`` for a failed install: reason, ``note``, log, its last lines."""
        log.warning("ticket %d: %s (log: %s)", ticket.id, exc.reason, exc.log_path)
        name = exc.log_path.name if exc.log_path else log_name
        where = f"; log traces/{ticket.id}/{name}" if name else ""
        detail = f"{exc.reason}{note}{where}; last lines: {exc.detail[-DETAIL_TAIL:]}"
        return _setup_failed()(SETUP_REASON, detail)

    def _recover_cache_miss(
        self,
        ticket: Ticket,
        att_id: Any,
        worktree: Any,
        wt_path: Path,
        command: install.InstallCommand,
        first: install.InstallFailed,
    ) -> install.InstallResult:
        """#57: an offline install missed the cache. Dependency files unchanged from the
        base commit: fill the cache from a scratch worktree detached at the base commit
        (network, as a first attempt), then retry offline in ``wt_path`` once. Changed:
        flagged, new dependencies need the user. ``wt_path`` itself never gets network."""
        base_sha = getattr(worktree, "base_sha", None)
        if not base_sha:
            raise self._install_failed(ticket, first) from first
        files = install.dependency_files(command)
        changed = install.dependency_files_changed(wt_path, base_sha, files)
        if changed is None or changed:  # the model asked for other dependencies
            what = ", ".join(changed) if changed else "the dependency files"
            note = (f"; new dependencies need the user: the change edits {what}, and an "
                    "install after a model edit has no network")  # fmt: skip
            raise self._install_failed(ticket, first, note) from first
        mirror = getattr(worktree, "mirror", None)
        enabled = getattr(self.config, "install_refill_cache", REFILL_ON_CACHE_MISS)
        if not enabled or mirror is None or not hasattr(mirror, "scratch"):
            raise self._install_failed(ticket, first) from first
        log.info("ticket %d: offline install missed the cache; filling it from %s",
                 ticket.id, str(base_sha)[:12])  # fmt: skip
        trace = self.traces_dir / str(ticket.id)
        repo = ticket.repo
        scratch = mirror.scratch(base_sha, f"a{att_id}-cachefill")
        fill_policy = self._sandbox_policy(  # type: ignore[attr-defined]
            ticket, scratch.path, f"a{att_id}-cachefill", writer=False
        )
        try:
            install.refill_cache(
                command, scratch.path, base_sha, policy=fill_policy,
                cache=install.cache_root(repo),
                log_path=trace / f"attempt-{att_id}-install-cachefill.log",
                timeout=self.config.install_timeout_seconds,
            )  # fmt: skip
        except install.InstallFailed as exc:
            note = "; filling the empty cache from the base commit failed too"
            raise self._install_failed(ticket, exc, note) from exc
        finally:
            self._drop_scratch(fill_policy)  # type: ignore[attr-defined]
            scratch.remove()
        # A fresh policy: its scratch holds a new snapshot of the now-filled cache.
        retry_policy = self._sandbox_policy(  # type: ignore[attr-defined]
            ticket, wt_path, f"a{att_id}-install-retry", writer=False
        )
        try:
            return install.run(
                command, wt_path, policy=retry_policy,
                network=install.network_for(retry_policy, base=False), cache=None,
                log_path=trace / f"attempt-{att_id}-install-retry.log",
                timeout=self.config.install_timeout_seconds,
            )  # fmt: skip
        except install.InstallFailed as exc:
            note = "; still failing after filling the cache from the base commit"
            raise self._install_failed(ticket, exc, note) from exc
        finally:
            self._drop_scratch(retry_policy)  # type: ignore[attr-defined]


def _exclude_install_output(worktree: Any, wt_path: Path) -> None:
    """Add ``INSTALL_EXCLUDES`` to the mirror's ``info/exclude`` (shared by its worktrees)."""
    mirror = getattr(getattr(worktree, "mirror", None), "mirror_path", None)
    if mirror is None:
        try:
            out = subprocess.run(
                ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                cwd=wt_path, capture_output=True, text=True, timeout=30,
            )  # fmt: skip
        except (OSError, subprocess.TimeoutExpired):
            return
        if out.returncode != 0:
            return
        mirror = Path(out.stdout.strip())
    info = Path(mirror) / "info"
    try:
        info.mkdir(exist_ok=True)
        f = info / "exclude"
        have = f.read_text().splitlines() if f.exists() else []
        missing = [e for e in INSTALL_EXCLUDES if e not in have]
        if missing:
            f.write_text("\n".join([*have, "# foreman-inator (#45)", *missing]) + "\n")
    except OSError as exc:
        log.debug("could not update %s: %s", info / "exclude", exc)


__all__ = ["INSTALL_EXCLUDES", "REFILL_ON_CACHE_MISS", "SETUP_REASON", "InstallSteps"]
