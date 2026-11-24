"""The sandbox at the worker's three launch points (todo #40, wired in wave 6b).

``SandboxSteps`` is mixed into ``Worker``. Every process that runs model-written code
starts through one of three seams, and each gets a ``runner.sandbox.Policy`` from
``_sandbox_policy``:

=====================================  ============================  ======================
Launch point                           Policy                        Network
=====================================  ============================  ======================
``AttemptSteps._launch_writer``        the attempt's worktree,       the proxy port only
(``opencode serve`` + its bash tool)   scratch ``a<id>-writer``      (the driver sets it)
``AttemptSteps._launch_verify``        the worktree, scratch         the repo's
(the repo's verify command)            ``a<id>-verify``              ``sandbox_network``
``ReviewSteps._launch_review``         ``sandbox.active``; each      the repo's
(the reviewer's checks)                check re-binds it to its own  ``sandbox_network``
                                       scratch worktree
=====================================  ============================  ======================

Scratch dirs live under ``~/.foreman-inator/sandbox/<key>`` (mode 700) and are removed
after the launch. ``warm_up_opencode`` stays unsandboxed (it runs no model code).

Fail closed: ``sandbox_ready`` is asked before an attempt row is written or a review
starts. With ``sandbox = "on"`` and no usable backend (not macOS, no ``sandbox-exec``) it
returns the backend's message; the caller hands the ticket to the user with
``FLAG_SANDBOX`` (flagged, no attempt counted, nothing retried: a flagged ticket gets no
job). ``sandbox = "off"`` is the only way out, and ``fman status`` and every PR
description say so (``sandbox.describe``).
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from foreman_inator import paths
from foreman_inator.db.store import Ticket
from foreman_inator.runner import sandbox

log = logging.getLogger("foreman_inator.worker")

#: Flag reason prefix: the sandbox is on but cannot run here (fail closed).
FLAG_SANDBOX = "sandbox_unavailable"


class SandboxSteps:
    # Provided by Worker.
    config: Any

    def sandbox_line(self) -> str:
        """The line ``fman status`` and the PR description show (decision 5)."""
        return sandbox.describe(self.config.sandbox, dict(self.config.sandbox_network))

    def sandbox_ready(self, worktree: Path | str) -> str | None:
        """None when launches may go ahead; else why not (``SandboxUnavailable``)."""
        probe = sandbox.Policy(worktree=Path(worktree), scratch=Path(worktree))
        if self.config.sandbox != "on":
            return None
        try:
            sandbox.ensure_available(probe)
        except sandbox.SandboxUnavailable as exc:
            return str(exc)
        return None

    def _sandbox_policy(
        self, t: Ticket, wt_path: Path | str, key: str, *, writer: bool
    ) -> sandbox.Policy:
        """The policy for one launch: ``wt_path`` read/write, its own scratch dir, and the
        network the launch point gets (writer: none but the proxy port, which the
        OpenCode driver fills in from the proxy URL)."""
        if self.config.sandbox == "off":  # no discovery work: every launch runs as is
            return sandbox.Policy(
                worktree=Path(wt_path), scratch=paths.home() / "sandbox" / key, mode="off"
            )
        scratch = self._sandbox_scratch(key)
        network = "none" if writer else self.config.sandbox_network_for(t.repo)
        return sandbox.Policy.create(
            wt_path,
            scratch,
            mode=self.config.sandbox,
            network=network,  # type: ignore[arg-type]
        )

    def _sandbox_scratch(self, key: str) -> Path:
        scratch = paths.home() / "sandbox" / key
        shutil.rmtree(scratch, ignore_errors=True)  # a killed launch's leftovers
        scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
        return scratch

    def _drop_scratch(self, policy: sandbox.Policy | None) -> None:
        if policy is not None:
            shutil.rmtree(policy.scratch, ignore_errors=True)


__all__ = ["FLAG_SANDBOX", "SandboxSteps"]
