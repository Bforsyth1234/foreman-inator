"""OpenCode driver: run one coding attempt with `opencode serve` in a worktree (todo #10).

One attempt = one OpenCode server process, cwd = the worktree, driven over HTTP:

    OpenCodeServer      starts/stops the `opencode serve` process (own process group,
                        per-attempt config, isolated XDG dirs, random basic-auth password)
    OpenCodeSession     the HTTP adapter: every endpoint path and event shape OpenCode
                        defines lives here and nowhere else (rule 12)
    OpenCodeDriver.run  the attempt loop: create session, send the prompt, map tool events
                        to `tool.call` spans + recorder `tool` rows, stop on idle / error /
                        timeout, always clean up the process

`llm.call` spans are not made here: OpenCode's provider base URL points at the model proxy
(#9, `http://127.0.0.1:<port>/a/<attempt_id>/v1`), which traces every model call. The
driver does not compute the diff either; the worker reads it from the worktree.

OpenCode API this relies on (OpenCode 1.18.7, read 2026-11-23 from the server's own OpenAPI
document at `GET /doc`, plus https://opencode.ai/docs/server/, /docs/permissions/,
/docs/providers/). These are the classic (SDK) routes; the server also exposes a newer
`/api/...` (v2) set that the docs now lead with. Only `OpenCodeSession` and
`build_config` need to change if we move.

    GET  /global/health                          {"healthy": true, "version": "1.18.7"}
    POST /session?directory=                     body {title, permission: [rule]} -> Session
    POST /session/{id}/prompt_async?directory=   body {parts:[{type:"text",text}], model:
                                                 {providerID, modelID}, agent} -> 204
    GET  /event?directory=                       SSE, `data: {"id","type","properties"}`
    POST /session/{id}/abort?directory=          -> true
    POST /permission/{requestID}/reply           body {reply: once|always|reject, message}
    POST /question/{requestID}/reject
    GET  /session/{id}/message?directory=        [{info: Message, parts: [Part]}]

Event types used: `session.status` ({type: busy|idle|retry}), `session.idle`,
`session.error` (error.name, e.g. MessageAbortedError), `message.updated` (info.role,
tokens, cost), `message.part.updated` (part.type "text" | "tool"; a tool part carries
callID, tool, state{status pending|running|completed|error, input, output|error,
time{start,end} in epoch ms}), `permission.asked`, `question.asked`.

Headless setup. The per-attempt config (`opencode.json`, outside the worktree, passed by
`OPENCODE_CONFIG`) defines one provider `fman` (`@ai-sdk/openai-compatible`, baseURL = the
proxy's per-attempt URL, a dummy apiKey: the proxy holds real keys) and its one model,
`enabled_providers: ["fman"]`, sharing/autoupdate/LSP/formatters off. `XDG_CONFIG_HOME`,
`XDG_DATA_HOME`, `XDG_STATE_HOME` point at foreman-inator's own dirs, so the user's global
OpenCode config, auth.json and sessions are never read or written. Permissions never ask:
read/edit/glob/grep/list/bash/task/todowrite/skill/lsp allow; `.env` reads, webfetch,
websearch, question, doom_loop and external_directory deny. Anything that still asks is
rejected by the driver (counted in `permission_rejections`).

Residual risk without a sandbox: OpenCode's `bash` tool runs with the user's own authority.
external_directory is a best-effort check on paths it can see in the command; a shell
command can still read or write outside the worktree and reach the network. The driver
strips API keys, tokens, `SSH_AUTH_SOCK` and git askpass helpers from the server's
environment, so the agent cannot push or use the user's credentials.

Sandbox (#40): `run(..., sandbox=policy)` / `OpenCodeServer(..., sandbox=policy)` start
`opencode serve` under `runner.sandbox` (macOS `sandbox-exec`), and its bash tool and every
command it runs inherit it: writes only to the worktree, the attempt's state dir and the
policy's scratch dir; the shared plugin/cache dir is read-only (the unsandboxed warm-up
installs plugins); outbound network only to the proxy's loopback port; it may listen on
loopback for the driver. No `sandbox` (the default) runs it exactly as before.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os
import queue
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx

from foreman_inator.config import ModelEndpoint
from foreman_inator.runner import sandbox as sandbox_mod
from foreman_inator.runner.sandbox import Policy
from foreman_inator.tracing import get_recorder, record_tool_call

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 20 * 60.0  # per attempt (todo #10)
STARTUP_TIMEOUT = 60.0
ABORT_GRACE = 30.0  # after an abort, how long to wait for the session to go idle
STOP_GRACE = 5.0  # SIGTERM -> SIGKILL
PROVIDER_ID = "fman"
AGENT = "build"
OUTCOME_FAILED = "failed_checks"
SERVER_USER = "opencode"

Status = Literal["finished", "timed_out", "errored"]


# --------------------------------------------------------------------------- results


@dataclass(frozen=True)
class AttemptResult:
    """What one OpenCode run did. The worker computes the diff from the worktree.

    `outcome` is "failed_checks" when the run timed out or errored (nothing to verify);
    None when it finished, because the outcome then depends on the verify step.
    """

    status: Status
    outcome: str | None
    final_message: str
    error: str | None = None
    session_id: str | None = None
    tool_calls: int = 0
    tool_errors: int = 0
    edits: int = 0
    assistant_messages: int = 0
    permission_rejections: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0  # as OpenCode reports it; the proxy's numbers are authoritative
    seconds: float = 0.0
    files_edited: tuple[str, ...] = ()
    cancelled: bool = False  # the caller's `cancel` event stopped the run (#14)

    @property
    def finished(self) -> bool:
        return self.status == "finished"


class Driver(Protocol):
    """What the worker (#14), replays (#25) and fork-from-failure (#36) call."""

    def run(
        self,
        *,
        attempt_id: int,
        worktree: Any,
        prompt: str,
        endpoint: ModelEndpoint,
        proxy_base_url: str,
        timeout: float | None = None,
        cancel: threading.Event | None = None,
    ) -> AttemptResult: ...


class OpenCodeError(RuntimeError):
    pass


# --------------------------------------------------------------------------- config + env

PERMISSIONS: dict[str, Any] = {
    "read": {"*": "allow", "*.env": "deny", "*.env.*": "deny", "*.env.example": "allow"},
    "edit": "allow",
    "glob": "allow",
    "grep": "allow",
    "list": "allow",
    "bash": "allow",
    "task": "allow",
    "todowrite": "allow",
    "skill": "allow",
    "lsp": "allow",
    "external_directory": "deny",
    "question": "deny",
    "webfetch": "deny",
    "websearch": "deny",
    "doom_loop": "deny",
}


#: #52: the AI SDK provider package OpenCode loads for each ``ModelEndpoint.wire``.
#: OpenCode 1.18.7 (the installed binary, inspected 2026-11-25) builds an
#: ``@ai-sdk/anthropic`` model from ``options.baseURL`` and posts to
#: ``{baseURL}/messages`` (its ``buildRequestUrl`` default) with ``x-api-key`` and
#: ``anthropic-version: 2023-06-01``; with that package it also places the
#: ``cacheControl: {type: "ephemeral"}`` markers itself. So ``baseURL`` is the proxy's
#: ``.../v1`` and the request lands on the proxy's Messages route. 1.18.7 still reads the
#: ``provider`` / ``npm`` / ``options`` schema written here; https://opencode.ai/docs/providers
#: (read 2026-11-25) documents only the newer ``providers`` / ``package`` / ``settings``
#: schema (``@opencode/ai/providers/anthropic-compatible``), so re-check on an upgrade.
PROVIDER_PACKAGES = {"openai": "@ai-sdk/openai-compatible", "anthropic": "@ai-sdk/anthropic"}


def build_config(endpoint: ModelEndpoint, proxy_base_url: str) -> dict[str, Any]:
    """The per-attempt `opencode.json`: one provider pointing at the proxy, one model.

    No ``instructions`` key (#64): our writer prompt is the only place the repo's
    ``AGENTS.md`` / ``CLAUDE.md`` reach the model. OpenCode's own loading of those files
    is turned off by ``server_env`` (see the note there)."""
    model_ref = f"{PROVIDER_ID}/{endpoint.model}"
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            PROVIDER_ID: {
                "npm": PROVIDER_PACKAGES.get(endpoint.wire, PROVIDER_PACKAGES["openai"]),
                "name": "foreman-inator proxy",
                # The proxy holds the real keys (Keychain); OpenCode never sees one.
                "options": {"baseURL": proxy_base_url.rstrip("/"), "apiKey": "fman-proxy"},
                "models": {endpoint.model: {"name": endpoint.name, "tool_call": True}},
            }
        },
        "model": model_ref,
        "small_model": model_ref,
        "enabled_providers": [PROVIDER_ID],
        "share": "disabled",
        "autoupdate": False,
        "snapshot": False,
        "lsp": False,
        "formatter": False,
        "permission": PERMISSIONS,
    }


#: Chat (#71): one provider per wire, so a ladder can mix OpenAI-compatible and Anthropic
#: models in one session. The OpenAI-compatible one keeps the attempts' id.
CHAT_PROVIDERS = {"openai": PROVIDER_ID, "anthropic": f"{PROVIDER_ID}-anthropic"}


def _chat_endpoint(item: Any) -> ModelEndpoint:
    """A ladder item: a ``chat.Rung`` (its ``endpoint``) or a ``ModelEndpoint``."""
    ep = getattr(item, "endpoint", item)
    if not isinstance(ep, ModelEndpoint):
        raise TypeError(f"not a ladder rung or ModelEndpoint: {item!r}")
    return ep


def build_chat_config(
    ladder: Iterable[Any], small_model: ModelEndpoint | Any, proxy_url: str
) -> dict[str, Any]:
    """The per-session ``opencode.json`` of a chat (chat design section 4): every model
    of the session's ladder (``chat.Rung``s or ``ModelEndpoint``s, lowest tier first)
    under a ``fman`` provider pointing at the chat's proxy URL, so a tier move only
    changes the ``modelID`` the next ``prompt_async`` names: no restart, history kept.

    ``small_model`` (OpenCode's title/summary model; the session's lowest-tier model) is
    added if the ladder lacks it. ``model`` (the default when a prompt names none) is the
    first rung. One provider per wire (``CHAT_PROVIDERS``), each listed only when a model
    uses it. Everything else (permissions, no share/LSP/formatter/snapshot, no
    ``instructions``) is exactly ``build_config``'s, so a chat is as headless and as
    closed as an attempt."""
    endpoints = [_chat_endpoint(r) for r in ladder]
    if not endpoints:
        raise ValueError("a chat needs at least one model in its ladder")
    small = _chat_endpoint(small_model)
    if all((e.wire, e.model) != (small.wire, small.model) for e in endpoints):
        endpoints.append(small)
    base = proxy_url.rstrip("/")
    providers: dict[str, dict[str, Any]] = {}
    owner: dict[str, str] = {}  # model id -> provider id
    for ep in endpoints:
        wire = ep.wire if ep.wire in CHAT_PROVIDERS else "openai"
        pid = CHAT_PROVIDERS[wire]
        if owner.get(ep.model, pid) != pid:
            raise ValueError(f"model id {ep.model!r} is listed under two wires")
        owner[ep.model] = pid
        prov = providers.setdefault(
            pid,
            {
                "npm": PROVIDER_PACKAGES[wire],
                "name": "foreman-inator proxy",
                # The proxy holds the real keys (Keychain); OpenCode never sees one.
                "options": {"baseURL": base, "apiKey": "fman-proxy"},
                "models": {},
            },
        )
        prov["models"].setdefault(ep.model, {"name": ep.name, "tool_call": True})
    first = endpoints[0]
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": providers,
        "model": f"{owner[first.model]}/{first.model}",
        "small_model": f"{owner[small.model]}/{small.model}",
        "enabled_providers": list(providers),
        "share": "disabled",
        "autoupdate": False,
        "snapshot": False,
        "lsp": False,
        "formatter": False,
        "permission": PERMISSIONS,
    }


def config_models(config: Mapping[str, Any]) -> dict[str, str]:
    """``{modelID: providerID}`` for every model an ``opencode.json`` lists."""
    out: dict[str, str] = {}
    for pid, prov in (config.get("provider") or {}).items():
        for mid in (prov or {}).get("models") or {}:
            out.setdefault(str(mid), str(pid))
    return out


def config_proxy_url(config: Mapping[str, Any]) -> str | None:
    """The proxy base URL the config's providers point at (they all share one)."""
    for prov in (config.get("provider") or {}).values():
        url = ((prov or {}).get("options") or {}).get("baseURL")
        if url:
            return str(url)
    return None


_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH)", re.IGNORECASE)
_DROP_PREFIXES = ("OPENCODE_", "XDG_", "GIT_", "GH_", "GITHUB_", "FOREMAN_INATOR_")
_DROP_NAMES = {"SSH_AUTH_SOCK", "SSH_ASKPASS", "SUDO_ASKPASS"}


def server_env(
    base: Mapping[str, str],
    *,
    config_file: Path,
    config_home: Path,
    data_home: Path,
    state_home: Path,
    cache_home: Path,
    password: str,
) -> dict[str, str]:
    """The `opencode serve` environment: the user's env minus credentials and minus the
    worker's own Python environment (#54: its venv on PATH, VIRTUAL_ENV and the other
    activation variables, sandboxed or not), plus isolation.

    #64, instruction files: OpenCode loads ``AGENTS.md`` (else ``CLAUDE.md``, then
    ``CONTEXT.md``) found up from the project dir, the ``AGENTS.md`` next to a file the
    model reads, ``<config>/opencode/AGENTS.md`` and ``~/.claude/CLAUDE.md``
    (https://opencode.ai/docs/rules, read 2026-11-25; confirmed in the 1.18.7 binary's
    ``Instruction`` service). There is no config key that turns this off, only env vars:
    ``OPENCODE_DISABLE_PROJECT_CONFIG=1`` skips every project and nearby file (the 1.18.7
    code checks it before each ``findUp``), and ``OPENCODE_DISABLE_CLAUDE_CODE=1`` skips
    every ``CLAUDE.md``. That leaves only ``<XDG_CONFIG_HOME>/opencode/AGENTS.md``, inside
    the driver's own shared config dir, where nothing writes one. So the writer sees the
    repo's rules once, from our prompt, and replays do not depend on OpenCode for them."""
    env = {
        k: v
        for k, v in sandbox_mod.scrub_worker_env(base).items()
        if not _SECRET_NAME.search(k) and not k.startswith(_DROP_PREFIXES) and k not in _DROP_NAMES
    }
    env.update(
        {
            "OPENCODE_CONFIG": str(config_file),
            "XDG_CONFIG_HOME": str(config_home),
            "XDG_DATA_HOME": str(data_home),
            "XDG_STATE_HOME": str(state_home),
            "XDG_CACHE_HOME": str(cache_home),
            "OPENCODE_SERVER_USERNAME": SERVER_USER,
            "OPENCODE_SERVER_PASSWORD": password,
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",  # no repo opencode.json, no AGENTS.md (#64)
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "OPENCODE_DISABLE_SHARE": "1",
            "OPENCODE_DISABLE_MODELS_FETCH": "1",
            "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
            "OPENCODE_DISABLE_CLAUDE_CODE": "1",  # no ~/.claude prompts or skills
            "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


# --------------------------------------------------------------------------- the process

_LISTENING = re.compile(r"listening on (http://[0-9.:\[\]a-zA-Z-]+)")


class OpenCodeServer:
    """One `opencode serve` process for one attempt. Use as a context manager; `stop()`
    kills the whole process group and is safe to call more than once."""

    def __init__(
        self,
        worktree_path: Path,
        *,
        config: dict[str, Any],
        attempt_dir: Path,
        shared_dir: Path,
        opencode_bin: str = "opencode",
        startup_timeout: float = STARTUP_TIMEOUT,
        base_env: Mapping[str, str] | None = None,
        sandbox: Policy | None = None,
    ):
        self.worktree_path = Path(worktree_path).resolve()
        self.sandbox = sandbox
        self.attempt_dir = Path(attempt_dir)
        self.shared_dir = Path(shared_dir)
        self.config = config
        self.opencode_bin = opencode_bin
        self.startup_timeout = startup_timeout
        self.base_env = dict(os.environ if base_env is None else base_env)
        self.password = secrets.token_urlsafe(24)
        self.log_path = self.attempt_dir / "serve.log"
        self.url: str | None = None
        self.proc: subprocess.Popen[bytes] | None = None

    @property
    def auth(self) -> tuple[str, str]:
        return (SERVER_USER, self.password)

    def start(self) -> str:
        if _is_within(self.attempt_dir.resolve(), self.worktree_path):
            raise OpenCodeError("the OpenCode state dir must be outside the worktree")
        for d in ("data", "state"):
            (self.attempt_dir / d).mkdir(parents=True, exist_ok=True)
        for d in ("config", "cache"):  # shared across attempts: plugin installs, npm cache
            (self.shared_dir / d).mkdir(parents=True, exist_ok=True)
        config_file = self.attempt_dir / "opencode.json"
        config_file.write_text(json.dumps(self.config, indent=2), encoding="utf-8")
        env = server_env(
            self.base_env,
            config_file=config_file,
            config_home=self.shared_dir / "config",
            data_home=self.attempt_dir / "data",
            state_home=self.attempt_dir / "state",
            cache_home=self.shared_dir / "cache",
            password=self.password,
        )
        argv = [self.opencode_bin, "serve", "--pure", "--hostname", "127.0.0.1", "--port", "0"]
        policy = self.sandbox_policy()
        if policy is not None:
            env = sandboxed_env(env, policy)
            argv = sandbox_mod.wrap(argv, policy)  # raises SandboxUnavailable: fail closed
        with self.log_path.open("wb") as logf:
            self.proc = subprocess.Popen(
                argv,
                cwd=self.worktree_path,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=logf,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # its own process group, so stop() gets bash children
            )
        try:
            self.url = self._wait_listening()
        except BaseException:
            self.stop()
            raise
        return self.url

    def sandbox_policy(self) -> Policy | None:
        """The sandbox for this server: the given policy bound to this worktree, with the
        attempt's state dir writable, the shared dir read-only, loopback listening on and
        the opencode binary's install dir readable. None when no sandbox was given."""
        if self.sandbox is None:
            return None
        if self.sandbox.mode == "off":
            return self.sandbox
        exe = shutil.which(self.opencode_bin, path=self.base_env.get("PATH")) or self.opencode_bin
        policy = self.sandbox.bind(
            self.worktree_path,
            reads=[self.shared_dir],
            writes=[self.attempt_dir],
            argv0=exe,
        )
        return dataclasses.replace(policy, listen_loopback=True)

    def _wait_listening(self) -> str:
        assert self.proc is not None
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            m = _LISTENING.search(self._log_text())
            if m:
                return m.group(1)
            if self.proc.poll() is not None:
                raise OpenCodeError(
                    f"opencode serve exited with {self.proc.returncode}: {self.log_tail()}"
                )
            time.sleep(0.05)
        raise OpenCodeError(
            f"opencode serve did not start in {self.startup_timeout:.0f}s: {self.log_tail(500)}"
        )

    def _log_text(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def log_tail(self, chars: int = 2000) -> str:
        return self._log_text()[-chars:]

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        proc = self.proc
        if proc is None:
            return
        pgid = proc.pid  # start_new_session: pgid == pid
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=STOP_GRACE)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)  # stragglers in the group, even if the leader quit
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=STOP_GRACE)

    def __enter__(self) -> OpenCodeServer:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


_KEEP_FROM_SERVER_ENV = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")


def sandboxed_env(env: Mapping[str, str], policy: Policy) -> dict[str, str]:
    """``server_env`` plus the sandbox's HOME/TMPDIR/cache overrides. OpenCode keeps its
    own XDG dirs (the per-attempt and shared state dirs), so its isolation is unchanged."""
    out = dict(env)
    for k, v in sandbox_mod.child_env(policy, env.get("PATH", "")).items():
        if k not in _KEEP_FROM_SERVER_ENV:
            out[k] = v
    return out


def proxy_port(proxy_base_url: str) -> int | None:
    """The loopback port of the proxy URL the attempt's provider points at."""
    try:
        return urlsplit(proxy_base_url).port
    except ValueError:
        return None


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------- the HTTP adapter


@dataclass(frozen=True)
class ToolEvent:
    call_id: str
    name: str
    input: dict[str, Any]
    output: str
    is_error: bool
    session_id: str
    start_ms: int | None = None
    end_ms: int | None = None


@dataclass(frozen=True)
class TextEvent:
    session_id: str
    message_id: str
    part_id: str
    text: str


@dataclass(frozen=True)
class MessageEvent:
    session_id: str
    message_id: str
    role: str
    completed: bool
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    error: str | None = None


@dataclass(frozen=True)
class StatusEvent:
    session_id: str
    status: str  # busy | idle | retry


@dataclass(frozen=True)
class IdleEvent:
    session_id: str


@dataclass(frozen=True)
class ErrorEvent:
    session_id: str | None
    name: str
    message: str


@dataclass(frozen=True)
class PermissionEvent:
    request_id: str
    session_id: str
    permission: str
    patterns: tuple[str, ...]


@dataclass(frozen=True)
class QuestionEvent:
    request_id: str
    session_id: str


@dataclass(frozen=True)
class FileEditedEvent:
    file: str


@dataclass(frozen=True)
class Connected:
    """The event stream is live (OpenCode sends `server.connected` first)."""


@dataclass(frozen=True)
class StreamClosed:
    error: str | None = None


DriverEvent = (
    ToolEvent
    | TextEvent
    | MessageEvent
    | StatusEvent
    | IdleEvent
    | ErrorEvent
    | PermissionEvent
    | QuestionEvent
    | FileEditedEvent
    | Connected
    | StreamClosed
)


def parse_event(raw: dict[str, Any]) -> DriverEvent | None:
    """One OpenCode bus event -> a driver event, or None for the ones the driver ignores.
    Tool parts become a ToolEvent only once finished (state completed or error)."""
    kind = raw.get("type")
    p = raw.get("properties") or {}
    if kind == "message.part.updated":
        part = p.get("part") or {}
        sid = part.get("sessionID") or p.get("sessionID") or ""
        if part.get("type") == "tool":
            state = part.get("state") or {}
            status = state.get("status")
            if status not in ("completed", "error"):
                return None
            t = state.get("time") or {}
            out = state.get("output") if status == "completed" else state.get("error")
            return ToolEvent(
                call_id=str(part.get("callID") or part.get("id") or ""),
                name=str(part.get("tool") or "unknown"),
                input=dict(state.get("input") or {}),
                output="" if out is None else str(out),
                is_error=status == "error",
                session_id=sid,
                start_ms=t.get("start"),
                end_ms=t.get("end"),
            )
        if part.get("type") == "text" and not part.get("synthetic"):
            mid, pid = str(part.get("messageID", "")), str(part.get("id", ""))
            return TextEvent(sid, mid, pid, str(part.get("text", "")))
        return None
    if kind == "message.updated":
        info = p.get("info") or {}
        tokens = info.get("tokens") or {}
        err = info.get("error")
        return MessageEvent(
            session_id=str(info.get("sessionID") or p.get("sessionID") or ""),
            message_id=str(info.get("id", "")),
            role=str(info.get("role", "")),
            completed=bool((info.get("time") or {}).get("completed")),
            tokens_in=int(tokens.get("input") or 0),
            tokens_out=int(tokens.get("output") or 0),
            cost_usd=float(info.get("cost") or 0.0),
            error=_error_text(err) if err else None,
        )
    if kind == "session.status":
        return StatusEvent(str(p.get("sessionID", "")), str((p.get("status") or {}).get("type")))
    if kind == "session.idle":
        return IdleEvent(str(p.get("sessionID", "")))
    if kind == "session.error":
        err = p.get("error") or {}
        name = str(err.get("name", "UnknownError"))
        return ErrorEvent(p.get("sessionID"), name, _error_text(err))
    if kind == "permission.asked":
        return PermissionEvent(
            str(p.get("id", "")),
            str(p.get("sessionID", "")),
            str(p.get("permission", "")),
            tuple(p.get("patterns") or ()),
        )
    if kind == "question.asked":
        return QuestionEvent(str(p.get("id", "")), str(p.get("sessionID", "")))
    if kind == "file.edited":
        return FileEditedEvent(str(p.get("file", "")))
    if kind == "server.connected":
        return Connected()
    return None


def _error_text(err: Any) -> str:
    if not isinstance(err, dict):
        return str(err)
    data = err.get("data") or {}
    msg = data.get("message") if isinstance(data, dict) else None
    return f"{err.get('name', 'Error')}: {msg}" if msg else str(err.get("name", "Error"))


def iter_sse(lines: Iterator[str]) -> Iterator[dict[str, Any]]:
    """Parse a text/event-stream into the JSON payloads of its `data:` fields."""
    buf: list[str] = []
    for line in lines:
        if line == "":
            if buf:
                payload = "\n".join(buf)
                buf = []
                with contextlib.suppress(json.JSONDecodeError):
                    obj = json.loads(payload)
                    if isinstance(obj, dict):
                        yield obj
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        if name == "data":
            buf.append(value[1:] if value.startswith(" ") else value)
    if buf:
        with contextlib.suppress(json.JSONDecodeError):
            obj = json.loads("\n".join(buf))
            if isinstance(obj, dict):
                yield obj


class OpenCodeSession:
    """The OpenCode HTTP API, for one server and one working directory. The only place
    that knows OpenCode's routes and payloads (with `parse_event` and `build_config`)."""

    def __init__(
        self, base_url: str, *, directory: Path, auth: tuple[str, str] | None, timeout=30.0
    ):
        self.base_url = base_url.rstrip("/")
        self.directory = str(directory)
        self.auth = auth
        self.session_id: str | None = None
        self._client = httpx.Client(
            base_url=self.base_url,
            auth=auth,
            timeout=timeout,
            params={"directory": self.directory},
        )

    def close(self) -> None:
        self._client.close()

    def _ok(self, r: httpx.Response) -> httpx.Response:
        if r.status_code >= 400:
            req = r.request
            raise OpenCodeError(f"{req.method} {req.url.path}: {r.status_code} {r.text[:300]}")
        return r

    def health(self) -> dict[str, Any]:
        return self._ok(self._client.get("/global/health")).json()

    def create(self, title: str) -> str:
        r = self._ok(self._client.post("/session", json={"title": title}))
        self.session_id = str(r.json()["id"])
        return self.session_id

    def prompt(self, text: str, *, model_id: str, provider_id: str = PROVIDER_ID) -> None:
        body = {
            "parts": [{"type": "text", "text": text}],
            "model": {"providerID": provider_id, "modelID": model_id},
            "agent": AGENT,
        }
        self._ok(self._client.post(f"/session/{self.session_id}/prompt_async", json=body))

    def get(self, session_id: str) -> dict[str, Any] | None:
        """``GET /session/{id}``: the stored session, or None when the server has no such
        session (404 ``NotFoundError``). A new server started on the same
        ``XDG_DATA_HOME`` serves the sessions an earlier one created (checked against
        1.18.7, see ``ChatServer``)."""
        r = self._client.get(f"/session/{session_id}")
        if r.status_code == 404:
            return None
        return self._ok(r).json()

    def use(self, session_id: str) -> str:
        """Point this adapter at an existing session (a resumed chat)."""
        self.session_id = session_id
        return session_id

    def status(self) -> dict[str, Any]:
        """``GET /session/status``: ``{sessionID: {type: idle|busy|retry, ...}}`` (1.18.7's
        ``SessionStatus``; a session it does not list is idle)."""
        out = self._ok(self._client.get("/session/status")).json()
        return out if isinstance(out, dict) else {}

    def abort(self, timeout: float = 10.0) -> bool:
        if self.session_id is None:
            return False
        r = self._client.post(f"/session/{self.session_id}/abort", timeout=timeout)
        return r.status_code < 400

    def reject_permission(self, request_id: str, message: str) -> None:
        self._client.post(
            f"/permission/{request_id}/reply", json={"reply": "reject", "message": message}
        )

    def reject_question(self, request_id: str) -> None:
        self._client.post(f"/question/{request_id}/reject")

    def messages(self) -> list[dict[str, Any]]:
        return self._ok(self._client.get(f"/session/{self.session_id}/message")).json()

    def final_text(self) -> str | None:
        """The text of the last assistant message, from the stored session."""
        for msg in reversed(self.messages()):
            if (msg.get("info") or {}).get("role") != "assistant":
                continue
            texts = [
                str(p.get("text", ""))
                for p in msg.get("parts") or []
                if p.get("type") == "text" and not p.get("synthetic")
            ]
            if any(texts):
                return "".join(texts).strip()
        return None

    def events(self, stop: threading.Event) -> Iterator[DriverEvent]:
        """Driver events from `GET /event` until the stream closes or `stop` is set."""
        timeout = httpx.Timeout(10.0, read=None)
        try:
            with httpx.stream(
                "GET",
                f"{self.base_url}/event",
                params={"directory": self.directory},
                auth=self.auth,
                timeout=timeout,
            ) as r:
                if r.status_code >= 400:
                    yield StreamClosed(f"event stream: HTTP {r.status_code}")
                    return
                for raw in iter_sse(r.iter_lines()):
                    if stop.is_set():
                        return
                    ev = parse_event(raw)
                    if ev is not None:
                        yield ev
        except httpx.HTTPError as exc:
            if not stop.is_set():
                yield StreamClosed(f"event stream: {type(exc).__name__}: {exc}")
            return
        yield StreamClosed(None)


# --------------------------------------------------------------------------- tool mapping

_KINDS = {
    "read": "read",
    "glob": "search",
    "grep": "search",
    "list": "search",
    "codesearch": "search",
    "edit": "edit",
    "write": "edit",
    "patch": "edit",
    "multiedit": "edit",
    "apply_patch": "edit",
    "bash": "shell",
    "webfetch": "web",
    "websearch": "web",
    "todowrite": "todo",
    "todoread": "todo",
    "task": "subagent",
}


def tool_kind(name: str) -> str:
    return _KINDS.get(name, "other")


def record_tool(attempt_id: int, ev: ToolEvent) -> None:
    """One finished tool call -> a `tool.call` span and (if a recorder is open) a `tool` row."""
    kind = tool_kind(ev.name)
    rec = get_recorder(attempt_id)
    row = None
    if rec is not None:
        row = rec.tool(
            name=ev.name,
            input=ev.input,
            content=ev.output,
            is_error=ev.is_error,
            tool_use_id=ev.call_id,
            tool_kind=kind,
        )
    record_tool_call(
        attempt_id=attempt_id,
        name=ev.name,
        input=ev.input,
        output=ev.output,
        kind=kind,
        is_error=ev.is_error,
        output_file=(row or {}).get("output_file"),
        start_time_ns=ev.start_ms * 1_000_000 if ev.start_ms else None,
        end_time_ns=ev.end_ms * 1_000_000 if ev.end_ms else None,
    )


def _edited_path(ev: ToolEvent) -> str | None:
    for key in ("filePath", "file_path", "path"):
        v = ev.input.get(key)
        if isinstance(v, str) and v:
            return v
    return None


def handle_event(
    ev: DriverEvent,
    run: _Run,
    api: OpenCodeSession,
    record: Callable[[ToolEvent], None],
    *,
    reject_message: str = "unattended run: not permitted",
) -> bool:
    """Apply one event to ``run`` (the attempt's or one chat turn's tally); True when it
    is over. Permission and question asks are rejected (``PERMISSIONS`` never asks; an
    allow here would be a way around 7g); each finished tool call goes to ``record``
    once. Shared by ``OpenCodeDriver`` and ``ChatServer`` (chat design section 2)."""
    if isinstance(ev, ToolEvent):
        key = f"{ev.session_id}:{ev.call_id}"
        if key in run.tools:
            return False
        run.tools.add(key)
        run.tool_errors += ev.is_error
        if tool_kind(ev.name) == "edit":
            run.edits += 1
            path = _edited_path(ev)
            if path:
                run.files.add(path)
        record(ev)
    elif isinstance(ev, TextEvent):
        run.texts[(ev.message_id, ev.part_id)] = ev.text
    elif isinstance(ev, MessageEvent):
        if ev.session_id == run.session_id:
            if ev.role == "assistant":
                run.assistant[ev.message_id] = ev
                if ev.error and "MessageAbortedError" not in ev.error:
                    run.error = ev.error
            elif ev.role == "user":
                run.user_messages.add(ev.message_id)
    elif isinstance(ev, StatusEvent):
        if ev.session_id == run.session_id and ev.status == "busy":
            run.seen_busy = True
    elif isinstance(ev, IdleEvent):
        if ev.session_id == run.session_id and (run.seen_busy or run.assistant):
            return True
    elif isinstance(ev, ErrorEvent):
        if ev.session_id in (None, run.session_id):
            if ev.name == "MessageAbortedError":
                run.aborted_error = True
            else:
                run.error = ev.message
    elif isinstance(ev, PermissionEvent):
        run.rejections += 1
        log.warning("opencode asked for %s %s; rejecting", ev.permission, ev.patterns)
        with contextlib.suppress(httpx.HTTPError):
            api.reject_permission(ev.request_id, reject_message)
    elif isinstance(ev, QuestionEvent):
        run.rejections += 1
        with contextlib.suppress(httpx.HTTPError):
            api.reject_question(ev.request_id)
    elif isinstance(ev, FileEditedEvent):
        if ev.file:
            run.files.add(ev.file)
    elif isinstance(ev, StreamClosed):
        run.error = run.error or ev.error or "event stream closed"
        return True
    return False


class EventPump:
    """Reads ``GET /event`` on a thread into ``queue`` while the server lives. A dropped
    stream is reconnected up to ``retries`` times in a row (``reset_on_connect``: a
    stream that reached ``server.connected`` again starts the count over, for a chat
    server that lives for many turns); then a ``StreamClosed`` is queued."""

    def __init__(
        self,
        api: OpenCodeSession,
        alive: Callable[[], bool],
        *,
        name: str,
        retries: int = 3,
        reset_on_connect: bool = False,
    ):
        self.api = api
        self.alive = alive
        self.name = name
        self.retries = retries
        self.reset_on_connect = reset_on_connect
        self.stop_event = threading.Event()
        self.queue: queue.Queue[DriverEvent] = queue.Queue()
        self.closed = threading.Event()  # set when the pump gave up or was stopped
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def _run(self) -> None:
        try:
            tries = 0
            while tries < self.retries:  # reconnect a dropped stream while the server lives
                tries += 1
                for ev in self.api.events(self.stop_event):
                    if self.reset_on_connect and isinstance(ev, Connected):
                        tries = 0
                    self.queue.put(ev)
                if self.stop_event.is_set() or not self.alive():
                    return
                time.sleep(0.2)
            self.queue.put(StreamClosed(f"event stream closed {self.retries} times"))
        finally:
            self.closed.set()


# --------------------------------------------------------------------------- the driver


@dataclass
class _Run:
    session_id: str
    seen_busy: bool = False
    tools: set[str] = field(default_factory=set)
    tool_errors: int = 0
    edits: int = 0
    files: set[str] = field(default_factory=set)
    texts: dict[tuple[str, str], str] = field(default_factory=dict)
    assistant: dict[str, MessageEvent] = field(default_factory=dict)
    user_messages: set[str] = field(default_factory=set)
    rejections: int = 0
    error: str | None = None
    aborted_error: bool = False
    cancelled: bool = False
    queued_s: float = 0.0  # #34: time queued for a local slot, added to the deadline


class OpenCodeDriver:
    """Runs attempts with a real (or fake) `opencode` binary. Thread-compatible: one `run`
    per call, each with its own server process."""

    def __init__(
        self,
        *,
        opencode_bin: str = "opencode",
        timeout: float = DEFAULT_TIMEOUT,
        startup_timeout: float = STARTUP_TIMEOUT,
        abort_grace: float = ABORT_GRACE,
        state_root: Path | str | None = None,
        keep_state: bool = False,
        base_env: Mapping[str, str] | None = None,
        on_event: Callable[[DriverEvent], None] | None = None,
        extra_time: Callable[[int], float] | None = None,
    ):
        self.opencode_bin = opencode_bin
        self.timeout = timeout
        self.startup_timeout = startup_timeout
        self.abort_grace = abort_grace
        self._state_root = Path(state_root) if state_root is not None else None
        self.keep_state = keep_state
        self.base_env = base_env
        self.on_event = on_event
        # #34: seconds to add to an attempt's timeout, asked each loop turn with the
        # attempt id; the worker passes `ModelProxy.queue_wait` so time spent queued for
        # a local-model slot never counts against the writer timeout.
        self.extra_time = extra_time

    @property
    def state_root(self) -> Path:
        if self._state_root is None:
            from foreman_inator import paths

            self._state_root = paths.home() / "opencode"
        return self._state_root

    def run(
        self,
        *,
        attempt_id: int,
        worktree: Any,
        prompt: str,
        endpoint: ModelEndpoint,
        proxy_base_url: str,
        timeout: float | None = None,
        cancel: threading.Event | None = None,
        sandbox: Policy | None = None,
    ) -> AttemptResult:
        """Run one attempt to the end. `worktree` is a `Worktree` or a path. Never leaves
        the server running: the process group is killed on return, error or Ctrl-C.

        `cancel` (worker shutdown, ticket dropped): once set, the session is aborted as on
        a timeout, and the result has `cancelled=True` and status "errored".

        `sandbox` (#40): run the server under this policy; its `proxy_port` defaults to
        the port of `proxy_base_url`. `sandbox.SandboxUnavailable` is raised (not turned
        into an errored result) so the worker flags the ticket instead of retrying."""
        wt_path = Path(getattr(worktree, "path", worktree)).resolve()
        limit = self.timeout if timeout is None else timeout
        t0 = time.monotonic()
        attempt_dir = self.state_root / "attempts" / f"a{attempt_id}-{secrets.token_hex(3)}"
        server = OpenCodeServer(
            wt_path,
            config=build_config(endpoint, proxy_base_url),
            attempt_dir=attempt_dir,
            shared_dir=self.state_root / "shared",
            opencode_bin=self.opencode_bin,
            startup_timeout=self.startup_timeout,
            base_env=self.base_env,
            sandbox=_with_proxy_port(sandbox, proxy_base_url),
        )
        rec = get_recorder(attempt_id)
        if rec is not None:
            rec.user(prompt, by="worker")
        api: OpenCodeSession | None = None
        try:
            try:
                url = server.start()
                api = OpenCodeSession(url, directory=wt_path, auth=server.auth)
                return self._drive(
                    api, server, attempt_id, prompt, endpoint, limit, t0, cancel=cancel
                )
            except (OpenCodeError, httpx.HTTPError) as exc:
                # httpx.HTTPError: the server died mid-request (a crash races the reply), so
                # the transport error reaches us before the adapter can wrap it.
                return AttemptResult(
                    status="errored",
                    outcome=OUTCOME_FAILED,
                    final_message="",
                    error=str(exc) or type(exc).__name__,
                    session_id=api.session_id if api else None,
                    seconds=time.monotonic() - t0,
                )
        finally:
            if api is not None:
                api.close()
            server.stop()
            if not self.keep_state:
                shutil.rmtree(attempt_dir, ignore_errors=True)

    # ------------------------------------------------------------------ the loop

    def _drive(
        self,
        api: OpenCodeSession,
        server: OpenCodeServer,
        attempt_id: int,
        prompt: str,
        endpoint: ModelEndpoint,
        limit: float,
        t0: float,
        *,
        cancel: threading.Event | None = None,
    ) -> AttemptResult:
        sid = api.create(f"foreman-inator attempt {attempt_id}")
        pump = EventPump(api, server.alive, name=f"opencode-events-{attempt_id}")
        stop, q = pump.stop_event, pump.queue
        pump.start()
        run = _Run(session_id=sid)
        aborted = False
        try:
            early = self._await_connected(q, run, api, attempt_id)
            api.prompt(prompt, model_id=endpoint.model)
            if early:
                return self._result(api, run, sid, False, limit, t0)
            deadline = t0 + limit
            while True:
                now = time.monotonic()
                if not aborted and not run.cancelled:
                    run.queued_s = self._extra(attempt_id)
                    deadline = t0 + limit + run.queued_s
                if not aborted and cancel is not None and cancel.is_set():
                    aborted = run.cancelled = True
                    log.info("opencode attempt %s cancelled", attempt_id)
                    with contextlib.suppress(httpx.HTTPError):
                        api.abort()
                    deadline = now + min(self.abort_grace, 5.0)
                    continue
                if not aborted and now >= deadline:
                    aborted = True
                    log.warning("opencode attempt %s timed out after %.0fs", attempt_id, limit)
                    with contextlib.suppress(httpx.HTTPError):
                        api.abort()
                    deadline = now + self.abort_grace
                    continue
                if aborted and now >= deadline:
                    break  # OpenCode ignored the abort: stop() kills it
                try:
                    ev = q.get(timeout=min(1.0, max(0.01, deadline - now)))
                except queue.Empty:
                    if not server.alive():
                        run.error = run.error or f"opencode exited: {server.log_tail(500)}"
                        break
                    continue
                if self.on_event is not None:
                    self.on_event(ev)
                if self._handle(ev, run, api, attempt_id):
                    break
        finally:
            stop.set()
        return self._result(api, run, sid, aborted, limit, t0)

    def _extra(self, attempt_id: int) -> float:
        """The injected extra time (#34), never negative; a failing callable adds none."""
        if self.extra_time is None:
            return 0.0
        try:
            return max(0.0, float(self.extra_time(attempt_id)))
        except Exception as exc:
            log.warning("opencode extra_time for attempt %s failed: %s", attempt_id, exc)
            return 0.0

    def _await_connected(
        self, q: queue.Queue[DriverEvent], run: _Run, api: OpenCodeSession, attempt_id: int
    ) -> bool:
        """Wait (up to 10 s) for the event stream to be live before prompting, so no event
        of this run is missed. True if the stream already failed."""
        end = time.monotonic() + 10.0
        while time.monotonic() < end:
            try:
                ev = q.get(timeout=0.1)
            except queue.Empty:
                continue
            if isinstance(ev, Connected):
                return False
            if self._handle(ev, run, api, attempt_id):
                return True
        log.warning("opencode event stream sent no server.connected; prompting anyway")
        return False

    def _result(
        self, api: OpenCodeSession, run: _Run, sid: str, aborted: bool, limit: float, t0: float
    ) -> AttemptResult:
        final = run_final_text(api, run)
        tokens_in = sum(m.tokens_in for m in run.assistant.values())
        tokens_out = sum(m.tokens_out for m in run.assistant.values())
        cost = sum(m.cost_usd for m in run.assistant.values())
        if run.cancelled:
            status: Status = "errored"
            error = "cancelled by the worker"
        elif aborted:
            status = "timed_out"
            error = f"timed out after {limit:.0f}s"
            if run.queued_s >= 1:
                error += f" (plus {run.queued_s:.0f}s queued for a local-model slot)"
        elif run.error:
            status, error = "errored", run.error
        else:
            status, error = "finished", None
        return AttemptResult(
            status=status,
            outcome=None if status == "finished" else OUTCOME_FAILED,
            final_message=final,
            error=error,
            session_id=sid,
            tool_calls=len(run.tools),
            tool_errors=run.tool_errors,
            edits=run.edits,
            assistant_messages=len(run.assistant),
            permission_rejections=run.rejections,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
            seconds=time.monotonic() - t0,
            files_edited=tuple(sorted(run.files)),
            cancelled=run.cancelled,
        )

    def _handle(self, ev: DriverEvent, run: _Run, api: OpenCodeSession, attempt_id: int) -> bool:
        """Apply one event; True when the run is over."""
        return handle_event(ev, run, api, lambda tool_ev: record_tool(attempt_id, tool_ev))


def _with_proxy_port(policy: Policy | None, proxy_base_url: str) -> Policy | None:
    if policy is None or policy.proxy_port is not None:
        return policy
    return dataclasses.replace(policy, proxy_port=proxy_port(proxy_base_url))


def run_final_text(api: OpenCodeSession, run: _Run) -> str:
    """The last assistant message's text: from the stored session if the server answers,
    else from the text parts seen on the event stream."""
    with contextlib.suppress(httpx.HTTPError, OpenCodeError, ValueError, KeyError):
        text = api.final_text()
        if text is not None:
            return text
    order = list(run.assistant)
    for mid in reversed(order):
        parts = [t for (m, _), t in run.texts.items() if m == mid]
        if any(parts):
            return "".join(parts).strip()
    return ""


# --------------------------------------------------------------------------- chat (#71)

ChatTurnStatus = Literal["finished", "aborted", "timed_out", "errored"]


class ChatBusy(OpenCodeError):
    """A message was sent while the session's previous turn is still running."""


@dataclass(frozen=True)
class TurnResult:
    """What one chat turn (one user message) did. Spend is the proxy's to count; the
    tokens and cost here are OpenCode's own numbers, for display only."""

    status: ChatTurnStatus
    model_id: str
    final_message: str
    error: str | None = None
    tool_calls: int = 0
    tool_errors: int = 0
    edits: int = 0
    assistant_messages: int = 0
    permission_rejections: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    files_edited: tuple[str, ...] = ()
    queued_s: float = 0.0

    @property
    def finished(self) -> bool:
        return self.status == "finished"


def chat_sandbox(policy: Policy | None, *, network: bool = False) -> Policy | None:
    """The chat server's sandbox: the writer's ``policy`` unchanged, or with ``network``
    (decision 4, the repo's ``chat_network`` opt-in) the first-attempt dependency
    install's outbound rule as well: TCP to any host on ``sandbox.INSTALL_PORTS``
    (443/80) plus DNS and certificate trust (``Policy.outbound_ports``, the field
    ``runner.install.install_policy`` sets for base-branch installs). The proxy port and
    loopback listening stay; writes, reads, the Keychain (securityd stays denied) and the
    scrubbed environment are unchanged. No policy, or ``sandbox = "off"``: as given."""
    if policy is None or policy.mode == "off" or not network or policy.network == "allow":
        return policy
    return dataclasses.replace(policy, outbound_ports=tuple(sandbox_mod.INSTALL_PORTS))


def record_chat_tool(chat_id: int, ev: ToolEvent) -> None:
    """The default tool recorder of a chat: a ``tool`` row in the session's recording
    (``get_recorder(chat_id, "chat")``), if one is open. Spans for chat tool calls are
    the caller's (``ChatServer(on_tool=...)``), as their parent is the chat turn span."""
    rec = get_recorder(chat_id, "chat")
    if rec is not None:
        rec.tool(
            name=ev.name,
            input=ev.input,
            content=ev.output,
            is_error=ev.is_error,
            tool_use_id=ev.call_id,
            tool_kind=tool_kind(ev.name),
        )


class ChatServer:
    """One ``opencode serve`` and one OpenCode session for a whole chat (#71, chat design
    section 2): many turns, one process, the session's history kept between them.

        server = ChatServer(wt, chat_id=3, config=build_chat_config(...), state_dir=...,
                            shared_dir=..., sandbox=writer_policy, network=False)
        sid = server.start()                    # or start(resume=sid) after a restart
        turn = server.send("fix the test", "qwen3:8b")
        for ev in turn: ...                     # DriverEvents until idle/error/timeout
        turn.result                             # TurnResult
        server.abort()                          # from any thread: ends the running turn
        server.stop()                           # kills the process group; state kept

    Reuses the attempt pieces unchanged: ``OpenCodeServer`` (process, env, sandbox
    binding), ``OpenCodeSession`` (HTTP), ``parse_event``, ``handle_event`` (permission
    and question asks rejected, tool calls counted once) and ``EventPump``. A turn ends
    on the session going idle, a session error, ``turn_timeout`` (plus ``extra_time()``,
    the local-slot queue wait) or ``abort()``; an abort OpenCode ignores for
    ``abort_grace`` stops the server (7e: nothing keeps running unseen).

    ``state_dir`` (``<home>/opencode/chats/<id>``) holds ``opencode.json`` and the
    session's ``XDG_DATA_HOME``/``XDG_STATE_HOME``; ``stop()`` keeps it, so a later
    ``start(resume=...)`` reopens the session.

    Rule 12, OpenCode 1.18.7 (``opencode --version``; its own OpenAPI document at
    ``GET /doc`` and a live check against the installed binary, 2026-11-25; also
    https://opencode.ai/docs/server/):

    * Multi-message sessions: ``POST /session/{id}/prompt_async`` ("Create and send a new
      message to a session asynchronously, starting the session if needed") appends a
      turn to an existing session; two prompts in a row left four messages (user,
      assistant, user, assistant) in ``GET /session/{id}/message``. Each prompt names its
      own ``model`` ({providerID, modelID}), so the tier can change per turn.
    * Reload / resume: sessions are stored in ``<XDG_DATA_HOME>/opencode/opencode.db``
      (SQLite). A second server started on the same ``XDG_DATA_HOME`` answered
      ``GET /session/{id}`` with 200 and the full message list, and listed it in
      ``GET /session``; an unknown id is 404 ``NotFoundError``. So resume = start a new
      server on the kept state dir and keep prompting the old session id; no summary
      prompt is needed. (``/session/{id}/fork``, ``/summarize``, ``/revert`` exist too;
      unused.)
    * ``GET /session/status`` maps session ids to ``SessionStatus`` (idle | busy |
      retry); used after an event-stream reconnect to see whether a turn ended while
      no events arrived.
    * Without ``enabled_providers`` 1.18.7 falls back to its own hosted ``opencode``
      provider and answers from the network; ``build_chat_config`` always sets it.
    """

    def __init__(
        self,
        worktree_path: Path | str,
        *,
        chat_id: int,
        config: dict[str, Any],
        state_dir: Path | str,
        shared_dir: Path | str,
        opencode_bin: str = "opencode",
        startup_timeout: float = STARTUP_TIMEOUT,
        turn_timeout: float = DEFAULT_TIMEOUT,
        abort_grace: float = ABORT_GRACE,
        base_env: Mapping[str, str] | None = None,
        sandbox: Policy | None = None,
        network: bool = False,
        on_tool: Callable[[ToolEvent], None] | None = None,
        extra_time: Callable[[], float] | None = None,
    ):
        self.worktree_path = Path(worktree_path).resolve()
        self.chat_id = chat_id
        self.config = config
        self.models = config_models(config)
        if not self.models:
            raise ValueError("the chat config lists no models")
        self.state_dir = Path(state_dir)
        self.turn_timeout = turn_timeout
        self.abort_grace = abort_grace
        self.on_tool = on_tool or (lambda ev: record_chat_tool(chat_id, ev))
        self.extra_time = extra_time
        proxy_url = config_proxy_url(config) or ""
        self.server = OpenCodeServer(
            self.worktree_path,
            config=config,
            attempt_dir=self.state_dir,
            shared_dir=Path(shared_dir),
            opencode_bin=opencode_bin,
            startup_timeout=startup_timeout,
            base_env=base_env,
            sandbox=chat_sandbox(_with_proxy_port(sandbox, proxy_url), network=network),
        )
        self.api: OpenCodeSession | None = None
        self.pump: EventPump | None = None
        self.session_id: str | None = None
        self.resumed = False
        self._lock = threading.Lock()
        self._turn: ChatTurnStream | None = None
        self._old_messages: set[str] = set()  # message ids of earlier turns

    # ------------------------------------------------------------------ lifecycle

    def start(self, *, resume: str | None = None, title: str | None = None) -> str:
        """Start the server, wait for its event stream, then reopen ``resume`` (an
        OpenCode session id from an earlier server on this state dir) or create a new
        session. Returns the session id (``resumed`` says which). Raises
        ``OpenCodeError`` (and ``sandbox.SandboxUnavailable``: fail closed) with nothing
        left running."""
        try:
            url = self.server.start()
            self.api = OpenCodeSession(url, directory=self.worktree_path, auth=self.server.auth)
            self.pump = EventPump(
                self.api, self.server.alive, name=f"opencode-chat-{self.chat_id}",
                reset_on_connect=True,
            )  # fmt: skip
            self.pump.start()
            self._await_connected()
            if resume and self.api.get(resume) is not None:
                self.session_id = self.api.use(resume)
                self.resumed = True
                with contextlib.suppress(httpx.HTTPError, OpenCodeError, ValueError):
                    self._old_messages = {
                        str((m.get("info") or {}).get("id", "")) for m in self.api.messages()
                    }
            else:
                if resume:
                    log.warning("opencode chat %s: session %s not found; new session",
                                self.chat_id, resume)  # fmt: skip
                self.session_id = self.api.create(title or f"foreman-inator chat {self.chat_id}")
                self.resumed = False
            return self.session_id
        except BaseException:
            self.stop()
            raise

    def _await_connected(self) -> None:
        assert self.pump is not None
        end = time.monotonic() + 10.0
        while time.monotonic() < end:
            try:
                ev = self.pump.queue.get(timeout=0.1)
            except queue.Empty:
                if not self.server.alive():
                    raise OpenCodeError(f"opencode exited: {self.server.log_tail(500)}") from None
                continue
            if isinstance(ev, Connected):
                return
            if isinstance(ev, StreamClosed):
                raise OpenCodeError(ev.error or "event stream closed")
        log.warning("opencode chat %s: no server.connected; going on", self.chat_id)

    def alive(self) -> bool:
        return self.server.alive() and self.pump is not None and not self.pump.closed.is_set()

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._turn is not None and not self._turn.done

    def abort(self) -> bool:
        """End the running turn (status ``aborted``). Safe from any thread; False when no
        turn is running."""
        with self._lock:
            turn = self._turn
        if turn is None or turn.done:
            return False
        turn.abort_requested.set()
        return True

    def stop(self) -> None:
        """Stop the event pump and kill the server's process group. Safe to call twice.
        The state dir is kept (resume)."""
        with self._lock:
            turn = self._turn
        if turn is not None and not turn.done:
            turn.abort_requested.set()
        if self.pump is not None:
            self.pump.stop()
        if self.api is not None:
            self.api.close()
        self.server.stop()

    def __enter__(self) -> ChatServer:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ------------------------------------------------------------------ turns

    def send(self, text: str, model_id: str, *, timeout: float | None = None) -> ChatTurnStream:
        """Send one user message on ``model_id`` (one of the config's models; its provider
        comes from the config). Returns the turn: iterate it for its ``DriverEvent``s,
        then read ``.result``. ``ChatBusy`` while the previous turn still runs."""
        if self.api is None or self.session_id is None:
            raise OpenCodeError("the chat server is not started")
        provider = self.models.get(model_id)
        if provider is None:
            raise OpenCodeError(f"model {model_id!r} is not in this chat's config")
        with self._lock:
            if self._turn is not None and not self._turn.done:
                raise ChatBusy("still working on the previous message")
            turn = ChatTurnStream(
                self, text, model_id, provider,
                self.turn_timeout if timeout is None else timeout,
            )  # fmt: skip
            self._turn = turn
        turn._begin()
        return turn

    def _extra(self) -> float:
        if self.extra_time is None:
            return 0.0
        try:
            return max(0.0, float(self.extra_time()))
        except Exception as exc:
            log.warning("opencode chat %s extra_time failed: %s", self.chat_id, exc)
            return 0.0


class ChatTurnStream:
    """One running chat turn: an iterator of ``DriverEvent``s (``Connected`` left out),
    with ``result`` set once it is exhausted. Breaking out early aborts the turn."""

    def __init__(
        self, server: ChatServer, text: str, model_id: str, provider_id: str, limit: float
    ):
        self.server = server
        self.text = text
        self.model_id = model_id
        self.provider_id = provider_id
        self.limit = limit
        self.abort_requested = threading.Event()
        self.result: TurnResult | None = None
        assert server.session_id is not None
        self._run = _Run(session_id=server.session_id)
        self._t0 = time.monotonic()
        self._gen = self._events()

    @property
    def done(self) -> bool:
        return self.result is not None

    def __iter__(self) -> Iterator[DriverEvent]:
        return self._gen

    def __next__(self) -> DriverEvent:
        return next(self._gen)

    def wait(self) -> TurnResult:
        """Drain the turn's events and return its result."""
        for _ in self._gen:
            pass
        assert self.result is not None
        return self.result

    def _begin(self) -> None:
        """Drop stale events and post the prompt (``send`` calls this at once, so the
        turn runs even before anyone iterates). A failure ends the turn ``errored``."""
        srv = self.server
        assert srv.api is not None and srv.pump is not None
        self._drain(srv.pump, self._run)
        if not self._run.error:
            try:
                srv.api.prompt(self.text, model_id=self.model_id, provider_id=self.provider_id)
                return
            except (OpenCodeError, httpx.HTTPError) as exc:
                self._run.error = str(exc) or type(exc).__name__
        self._finish(srv.api, self._run, "errored")

    def _events(self) -> Iterator[DriverEvent]:
        if self.done:
            return
        srv = self.server
        api, pump, run = srv.api, srv.pump, self._run
        assert api is not None and pump is not None
        status: ChatTurnStatus | None = None
        ended = False
        aborted = False
        try:
            deadline = self._t0 + self.limit
            while True:
                now = time.monotonic()
                if not aborted:
                    run.queued_s = srv._extra()
                    deadline = self._t0 + self.limit + run.queued_s
                if not aborted and self.abort_requested.is_set():
                    aborted, status = True, "aborted"
                    with contextlib.suppress(httpx.HTTPError):
                        api.abort()
                    deadline = now + srv.abort_grace
                    continue
                if not aborted and now >= deadline:
                    aborted, status = True, "timed_out"
                    log.warning("opencode chat %s turn timed out after %.0fs",
                                srv.chat_id, self.limit)  # fmt: skip
                    with contextlib.suppress(httpx.HTTPError):
                        api.abort()
                    deadline = now + srv.abort_grace
                    continue
                if aborted and now >= deadline:
                    run.error = run.error or "OpenCode ignored the abort; server stopped"
                    srv.server.stop()  # 7e: never leave a turn running unseen
                    break
                try:
                    ev = pump.queue.get(timeout=min(1.0, max(0.01, deadline - now)))
                except queue.Empty:
                    if not srv.server.alive():
                        run.error = run.error or f"opencode exited: {srv.server.log_tail(500)}"
                        break
                    continue
                if isinstance(ev, Connected):  # the stream reconnected mid-turn
                    if run.seen_busy and not self._still_busy(api, run.session_id):
                        break  # it went idle while no events arrived
                    continue
                if self._stale(ev):
                    continue
                over = handle_event(ev, run, api, srv.on_tool, reject_message=CHAT_REJECT)
                yield ev
                if over:
                    break
            ended = True
        finally:
            if not ended and status is None:
                # the caller stopped iterating mid-turn: stop the model too (7e)
                status, run.error = "aborted", run.error or "turn left before it ended"
                with contextlib.suppress(httpx.HTTPError):
                    api.abort()
            self._finish(api, run, status)

    def _drain(self, pump: EventPump, run: _Run) -> None:
        """Drop events left over from before this turn; a dead stream fails the turn."""
        while True:
            try:
                ev = pump.queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(ev, StreamClosed):
                run.error = ev.error or "event stream closed"
        if pump.closed.is_set() and not run.error:
            run.error = "event stream closed"

    def _stale(self, ev: DriverEvent) -> bool:
        """An event of an earlier turn's message (a late update)."""
        old = self.server._old_messages
        if isinstance(ev, (MessageEvent, TextEvent)):
            return ev.message_id in old
        return False

    @staticmethod
    def _still_busy(api: OpenCodeSession, sid: str) -> bool:
        try:
            st = api.status().get(sid) or {}
        except (httpx.HTTPError, OpenCodeError, ValueError):
            return True
        return st.get("type") in ("busy", "retry")

    def _finish(self, api: OpenCodeSession, run: _Run, status: ChatTurnStatus | None) -> None:
        if self.result is not None:
            return
        srv = self.server
        if status is None:
            status = "errored" if run.error else "finished"
        error = run.error
        if status == "timed_out":
            error = f"timed out after {self.limit:.0f}s"
            if run.queued_s >= 1:
                error += f" (plus {run.queued_s:.0f}s queued for a local-model slot)"
        elif status == "aborted":
            error = error or "aborted"
        srv._old_messages |= set(run.assistant) | run.user_messages
        self.result = TurnResult(
            status=status,
            model_id=self.model_id,
            final_message=_turn_final_text(api, run),
            error=error,
            tool_calls=len(run.tools),
            tool_errors=run.tool_errors,
            edits=run.edits,
            assistant_messages=len(run.assistant),
            permission_rejections=run.rejections,
            tokens_in=sum(m.tokens_in for m in run.assistant.values()),
            tokens_out=sum(m.tokens_out for m in run.assistant.values()),
            cost_usd=sum(m.cost_usd for m in run.assistant.values()),
            seconds=time.monotonic() - self._t0,
            files_edited=tuple(sorted(run.files)),
            queued_s=run.queued_s,
        )


#: What a rejected permission ask tells the chat's model: ask the user in plain text.
CHAT_REJECT = "not permitted in a chat; ask the user in plain text instead"


def _turn_final_text(api: OpenCodeSession, run: _Run) -> str:
    """The last assistant message of this turn (never an earlier turn's)."""
    with contextlib.suppress(httpx.HTTPError, OpenCodeError, ValueError, KeyError):
        for msg in reversed(api.messages()):
            info = msg.get("info") or {}
            if info.get("role") != "assistant" or str(info.get("id")) not in run.assistant:
                continue
            texts = [
                str(p.get("text", ""))
                for p in msg.get("parts") or []
                if p.get("type") == "text" and not p.get("synthetic")
            ]
            if any(texts):
                return "".join(texts).strip()
    for mid in reversed(list(run.assistant)):
        parts = [t for (m, _), t in run.texts.items() if m == mid]
        if any(parts):
            return "".join(parts).strip()
    return ""
