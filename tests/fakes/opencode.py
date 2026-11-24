"""A scripted stand-in for `opencode serve` (OpenCode 1.18.7's classic HTTP API).

Stdlib only, so it also runs as a separate process. Three ways to use it:

1. HTTP level, in-process (adapter tests):

       server = FakeOpenCodeServer(Script([edit("a.py", "x = 1\\n")]), directory=tmp_path)
       url = server.start(); ...; server.stop()

2. As a fake `opencode` binary for the real driver (process, env, config, SSE, kill):

       fake = FakeOpenCode(tmp_path / "oc", Script([edit("a.py", "x = 1\\n"), say("done")]))
       driver = fake.driver(timeout=5)          # an OpenCodeDriver using fake.bin
       result = driver.run(attempt_id=1, worktree=wt, prompt=..., endpoint=..., proxy_base_url=...)
       fake.launches()   # argv, cwd, env and the opencode.json each server was started with
       fake.calls()      # every HTTP request the driver made (method, path, body)

3. As a drop-in driver in worker tests (#14): construct `fake.driver()` and hand it to the
   worker wherever it takes a `Driver`; the session edits real files in the worktree, so the
   worker's diff, verify and PR steps see real changes.

A script is a list of steps run in order after the prompt arrives, then the session
finishes (assistant message completed with `tokens_in`/`tokens_out`, `session.idle`):

    say(text)                         assistant text part
    edit(path, content)               writes the file (tool "write" if new, else "edit")
    read(path)                        tool "read" with the file's content
    bash(command)                     runs it in the directory, tool "bash" with its output
    tool(name, input, output, error)  any tool part; error=... makes it a failed call
    ask_permission(permission, patterns)   emits permission.asked, waits for a reply
    ask_question()                    emits question.asked, waits for reply/reject
    sleep(seconds)
    hang()                            blocks until the session is aborted (forever if
                                      Script(ignore_abort=True))
    fail(message)                     session.error (UnknownError), then idle
    crash(code)                       the process exits at once (out-of-process only)
    spawn_child()                     starts `sleep 600` in the server's process group
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import secrets
import socketserver
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

VERSION = "1.18.7"

# --------------------------------------------------------------------------- script


def say(text: str) -> dict[str, Any]:
    return {"op": "say", "text": text}


def edit(path: str, content: str) -> dict[str, Any]:
    return {"op": "edit", "path": path, "content": content}


def read(path: str) -> dict[str, Any]:
    return {"op": "read", "path": path}


def bash(command: str) -> dict[str, Any]:
    return {"op": "bash", "command": command}


def tool(
    name: str, input: dict[str, Any] | None = None, output: str = "", error: str | None = None
) -> dict[str, Any]:
    return {"op": "tool", "name": name, "input": input or {}, "output": output, "error": error}


def ask_permission(permission: str = "bash", patterns: list[str] | None = None) -> dict[str, Any]:
    return {"op": "ask_permission", "permission": permission, "patterns": patterns or ["*"]}


def ask_question() -> dict[str, Any]:
    return {"op": "ask_question"}


def sleep(seconds: float) -> dict[str, Any]:
    return {"op": "sleep", "seconds": seconds}


def hang() -> dict[str, Any]:
    return {"op": "hang"}


def fail(message: str = "model error") -> dict[str, Any]:
    return {"op": "fail", "message": message}


def crash(code: int = 3) -> dict[str, Any]:
    return {"op": "crash", "code": code}


def spawn_child() -> dict[str, Any]:
    return {"op": "spawn_child"}


@dataclass
class Script:
    steps: list[dict[str, Any]] = field(default_factory=list)
    tokens_in: int = 120
    tokens_out: int = 40
    cost: float = 0.0
    ignore_abort: bool = False

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> Script:
        return cls(**json.loads(text))


def _id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(10)}"


def _now_ms() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------------------- server state


class _Session:
    def __init__(self, sid: str, directory: str, title: str):
        self.id = sid
        self.info = {
            "id": sid,
            "slug": "fake",
            "projectID": "global",
            "directory": directory,
            "title": title,
            "version": VERSION,
            "time": {"created": _now_ms(), "updated": _now_ms()},
        }
        self.messages: list[dict[str, Any]] = []
        self.abort = threading.Event()
        self.busy = False


class _LoopbackHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer without the reverse-DNS lookup.

    ``HTTPServer.server_bind`` calls ``socket.getfqdn(host)``, which can block for many
    seconds on macOS (seen on GitHub's macOS runners). A loopback server has no use for it.
    """

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port


class FakeOpenCodeServer:
    """The HTTP server. `calls` lists every request as {method, path, query, body}."""

    def __init__(
        self,
        script: Script,
        *,
        directory: str | Path,
        password: str | None = None,
        username: str = "opencode",
        record_dir: str | Path | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
    ):
        self.script = script
        self.directory = str(Path(directory).resolve())
        self.password = password
        self.username = username
        self.record_dir = Path(record_dir) if record_dir else None
        self.host, self.port = host, port
        self.calls: list[dict[str, Any]] = []
        self.sessions: dict[str, _Session] = {}
        self.permission_replies: dict[str, dict[str, Any]] = {}
        self.question_replies: dict[str, str] = {}
        self._waiters: dict[str, threading.Event] = {}
        self._subs: list[queue.Queue[dict[str, Any] | None]] = []
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.url: str | None = None

    @property
    def aborted(self) -> bool:
        return any(c["path"].endswith("/abort") for c in self.calls)

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> str:
        handler = _make_handler(self)
        self._httpd = _LoopbackHTTPServer((self.host, self.port), handler)
        self._httpd.daemon_threads = True
        host, port = self._httpd.server_address[:2]
        self.url = f"http://{host}:{port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        self._stopping.set()
        with self._lock:
            for q in self._subs:
                q.put(None)
        for s in self.sessions.values():
            if not self.script.ignore_abort:
                s.abort.set()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()

    # ------------------------------------------------------------------ events

    def emit(self, type_: str, properties: dict[str, Any]) -> None:
        ev = {"id": _id("evt"), "type": type_, "properties": properties}
        with self._lock:
            for q in self._subs:
                q.put(ev)

    def subscribe(self) -> queue.Queue[dict[str, Any] | None]:
        q: queue.Queue[dict[str, Any] | None] = queue.Queue()
        with self._lock:
            self._subs.append(q)
        q.put({"id": _id("evt"), "type": "server.connected", "properties": {}})
        return q

    def unsubscribe(self, q: queue.Queue[dict[str, Any] | None]) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def _record(self, entry: dict[str, Any]) -> None:
        self.calls.append(entry)
        if self.record_dir is not None:
            with (self.record_dir / f"calls-{os.getpid()}.jsonl").open("a") as fh:
                fh.write(json.dumps(entry) + "\n")

    # ------------------------------------------------------------------ handlers

    def create_session(self, body: dict[str, Any]) -> dict[str, Any]:
        s = _Session(_id("ses"), self.directory, body.get("title") or "session")
        self.sessions[s.id] = s
        self.emit("session.created", {"sessionID": s.id, "info": s.info})
        self.emit("session.status", {"sessionID": s.id, "status": {"type": "idle"}})
        self.emit("session.idle", {"sessionID": s.id})
        return s.info

    def prompt(self, sid: str, body: dict[str, Any]) -> None:
        s = self.sessions[sid]
        text = "".join(p.get("text", "") for p in body.get("parts", []))
        threading.Thread(target=self._run, args=(s, text, body), daemon=True).start()

    def abort(self, sid: str) -> bool:
        s = self.sessions.get(sid)
        if s is None:
            return False
        if not self.script.ignore_abort:
            s.abort.set()
        return True

    def reply_permission(self, rid: str, body: dict[str, Any]) -> bool:
        self.permission_replies[rid] = body
        ev = self._waiters.get(rid)
        if ev:
            ev.set()
        return True

    def reply_question(self, rid: str, answer: str) -> bool:
        self.question_replies[rid] = answer
        ev = self._waiters.get(rid)
        if ev:
            ev.set()
        return True

    # ------------------------------------------------------------------ the scripted run

    def _message(self, s: _Session, role: str, **extra: Any) -> dict[str, Any]:
        info = {
            "id": _id("msg"),
            "sessionID": s.id,
            "role": role,
            "time": {"created": _now_ms()},
            **extra,
        }
        msg = {"info": info, "parts": []}
        s.messages.append(msg)
        self.emit("message.updated", {"sessionID": s.id, "info": info})
        return msg

    def _part(self, s: _Session, msg: dict[str, Any], part: dict[str, Any]) -> None:
        part = {"sessionID": s.id, "messageID": msg["info"]["id"], **part}
        for i, old in enumerate(msg["parts"]):
            if old["id"] == part["id"]:
                msg["parts"][i] = part
                break
        else:
            msg["parts"].append(part)
        self.emit("message.part.updated", {"sessionID": s.id, "part": part, "time": _now_ms()})

    def _tool(
        self,
        s: _Session,
        msg: dict[str, Any],
        name: str,
        input: dict[str, Any],
        output: str,
        error: str | None = None,
    ) -> None:
        pid, call = _id("prt"), _id("call")
        base = {"id": pid, "type": "tool", "callID": call, "tool": name}
        start = _now_ms()
        self._part(s, msg, {**base, "state": {"status": "pending", "input": {}, "raw": ""}})
        running = {"status": "running", "input": input, "time": {"start": start}}
        self._part(s, msg, {**base, "state": running})
        t = {"start": start, "end": _now_ms()}
        if error is None:
            state = {
                "status": "completed",
                "input": input,
                "output": output,
                "title": name,
                "metadata": {},
                "time": t,
            }
        else:
            state = {"status": "error", "input": input, "error": error, "time": t}
        self._part(s, msg, {**base, "state": state})

    def _wait(self, s: _Session, rid: str, seconds: float = 30.0) -> None:
        ev = threading.Event()
        self._waiters[rid] = ev
        end = time.monotonic() + seconds
        while time.monotonic() < end and not ev.is_set() and not s.abort.is_set():
            ev.wait(0.05)

    def _run(self, s: _Session, prompt_text: str, body: dict[str, Any]) -> None:
        s.busy = True
        user = self._message(s, "user", agent=body.get("agent"), model=body.get("model"))
        self._part(s, user, {"id": _id("prt"), "type": "text", "text": prompt_text})
        self.emit("session.status", {"sessionID": s.id, "status": {"type": "busy"}})
        model = body.get("model") or {}
        asst = self._message(
            s,
            "assistant",
            parentID=user["info"]["id"],
            modelID=model.get("modelID", ""),
            providerID=model.get("providerID", ""),
            mode="build",
            agent="build",
            path={"cwd": self.directory, "root": self.directory},
            cost=0,
            tokens={"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
        )
        error: dict[str, Any] | None = None
        try:
            for step in self.script.steps:
                if s.abort.is_set():
                    break
                error = self._step(s, asst, step)
                if error is not None:
                    break
        except Exception as exc:  # a broken script shows up as a session error
            error = {"name": "UnknownError", "data": {"message": f"fake: {exc!r}"}}
        if s.abort.is_set() and error is None:
            error = {"name": "MessageAbortedError", "data": {"message": "aborted"}}
        info = asst["info"]
        info["time"]["completed"] = _now_ms()
        info["tokens"] = {
            "input": self.script.tokens_in,
            "output": self.script.tokens_out,
            "reasoning": 0,
            "cache": {"read": 0, "write": 0},
        }
        info["cost"] = self.script.cost
        if error is not None:
            info["error"] = error
            self.emit("session.error", {"sessionID": s.id, "error": error})
        self.emit("message.updated", {"sessionID": s.id, "info": info})
        s.busy = False
        self.emit("session.status", {"sessionID": s.id, "status": {"type": "idle"}})
        self.emit("session.idle", {"sessionID": s.id})

    def _path(self, rel: str) -> Path:
        return Path(self.directory) / rel

    def _step(self, s: _Session, asst: dict[str, Any], step: dict[str, Any]) -> dict | None:
        op = step["op"]
        if op == "say":
            self._part(s, asst, {"id": _id("prt"), "type": "text", "text": step["text"]})
        elif op == "edit":
            p = self._path(step["path"])
            existed = p.exists()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(step["content"], encoding="utf-8")
            if existed:
                inp = {"filePath": str(p), "oldString": "", "newString": step["content"]}
                self._tool(s, asst, "edit", inp, "Edit applied successfully.")
            else:
                inp = {"filePath": str(p), "content": step["content"]}
                self._tool(s, asst, "write", inp, "Wrote file successfully.")
            self.emit("file.edited", {"file": str(p)})
        elif op == "read":
            p = self._path(step["path"])
            if p.exists():
                self._tool(s, asst, "read", {"filePath": str(p)}, p.read_text(encoding="utf-8"))
            else:
                self._tool(s, asst, "read", {"filePath": str(p)}, "", error=f"not found: {p}")
        elif op == "bash":
            proc = subprocess.run(
                step["command"],
                shell=True,
                cwd=self.directory,
                capture_output=True,
                text=True,
                timeout=60,
            )
            out = proc.stdout + proc.stderr
            inp = {"command": step["command"], "description": "fake"}
            self._tool(s, asst, "bash", inp, out, error=None if proc.returncode == 0 else out)
        elif op == "tool":
            self._tool(s, asst, step["name"], step["input"], step["output"], step.get("error"))
        elif op == "ask_permission":
            rid = _id("per")
            self.emit(
                "permission.asked",
                {
                    "id": rid,
                    "sessionID": s.id,
                    "permission": step["permission"],
                    "patterns": step["patterns"],
                    "metadata": {},
                    "always": [],
                },
            )
            self._wait(s, rid)
        elif op == "ask_question":
            rid = _id("que")
            self.emit(
                "question.asked",
                {"id": rid, "sessionID": s.id, "questions": [{"question": "Proceed?"}]},
            )
            self._wait(s, rid)
        elif op == "sleep":
            s.abort.wait(step["seconds"])
        elif op == "hang":
            while not s.abort.is_set() and not self._stopping.is_set():
                s.abort.wait(0.1)
            if self.script.ignore_abort:  # stopping, or never: stay silent
                while True:
                    time.sleep(3600)
        elif op == "fail":
            return {"name": "UnknownError", "data": {"message": step["message"]}}
        elif op == "crash":
            os._exit(int(step.get("code", 3)))
        elif op == "spawn_child":
            child = subprocess.Popen(["sleep", "600"])
            if self.record_dir is not None:
                (self.record_dir / f"child-{child.pid}.pid").write_text(str(child.pid))
        else:
            raise ValueError(f"unknown fake step {op!r}")
        return None


# --------------------------------------------------------------------------- HTTP


def _make_handler(srv: FakeOpenCodeServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *args: Any) -> None:  # quiet
            pass

        def _authorized(self) -> bool:
            if not srv.password:
                return True
            want = base64.b64encode(f"{srv.username}:{srv.password}".encode()).decode()
            return self.headers.get("Authorization") == f"Basic {want}"

        def _json(self, code: int, obj: Any) -> None:
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _empty(self, code: int = 204) -> None:
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _dispatch(self, method: str) -> None:
            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw) if raw else {}
            srv._record(
                {"method": method, "path": url.path, "query": parse_qs(url.query), "body": body}
            )
            if not self._authorized():
                return self._json(401, {"name": "UnauthorizedError"})
            parts = [p for p in url.path.split("/") if p]
            try:
                return self._route(method, parts, body)
            except KeyError:
                return self._json(404, {"name": "NotFoundError"})

        def _route(self, method: str, parts: list[str], body: dict[str, Any]) -> None:
            if method == "GET" and parts == ["global", "health"]:
                return self._json(200, {"healthy": True, "version": VERSION})
            if method == "GET" and parts == ["event"]:
                return self._sse()
            if method == "POST" and parts == ["session"]:
                return self._json(200, srv.create_session(body))
            if method == "GET" and parts == ["session", "status"]:
                busy = {k: {"type": "busy"} for k, v in srv.sessions.items() if v.busy}
                return self._json(200, busy)
            if parts[:1] == ["session"] and len(parts) >= 2:
                sid = parts[1]
                if method == "POST" and parts[2:] == ["prompt_async"]:
                    if sid not in srv.sessions:
                        raise KeyError(sid)
                    srv.prompt(sid, body)
                    return self._empty(204)
                if method == "POST" and parts[2:] == ["abort"]:
                    return self._json(200, srv.abort(sid))
                if method == "GET" and parts[2:] == ["message"]:
                    return self._json(200, srv.sessions[sid].messages)
                if method == "GET" and parts[2:] == []:
                    return self._json(200, srv.sessions[sid].info)
            if method == "POST" and parts[:1] == ["permission"] and parts[2:] == ["reply"]:
                return self._json(200, srv.reply_permission(parts[1], body))
            if method == "POST" and parts[:1] == ["question"] and parts[2:] == ["reject"]:
                return self._json(200, srv.reply_question(parts[1], "reject"))
            if method == "POST" and parts[:1] == ["question"] and parts[2:] == ["reply"]:
                return self._json(200, srv.reply_question(parts[1], "reply"))
            return self._json(404, {"name": "NotFoundError"})

        def _sse(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            q = srv.subscribe()
            try:
                while not srv._stopping.is_set():
                    try:
                        ev = q.get(timeout=0.25)
                    except queue.Empty:
                        continue
                    if ev is None:
                        break
                    self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                srv.unsubscribe(q)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def do_DELETE(self) -> None:  # noqa: N802
            self._dispatch("DELETE")

    return Handler


# --------------------------------------------------------------------------- fake binary


class FakeOpenCode:
    """Installs a fake `opencode` executable under `root` that serves `script`.

    `bin` is its path; `driver(**kw)` returns an `OpenCodeDriver` that uses it (state kept
    under `root/state`). `launches()` returns what each started server saw: argv, cwd, pid,
    pgid, the parsed OPENCODE_CONFIG file, the XDG/OPENCODE env vars and the names of all
    env vars it inherited. `calls()` returns every HTTP request across launches.
    """

    def __init__(self, root: str | Path, script: Script | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.record_dir = self.root / "record"
        self.record_dir.mkdir(exist_ok=True)
        self.script_path = self.root / "script.json"
        self.set_script(script or Script([say("done")]))
        self.bin = str(self.root / "opencode")
        shim = (
            "#!/bin/sh\n"
            f'exec "{sys.executable}" "{Path(__file__).resolve()}" '
            f'--fake-script "{self.script_path}" --fake-record "{self.record_dir}" "$@"\n'
        )
        Path(self.bin).write_text(shim)
        os.chmod(self.bin, 0o755)

    def set_script(self, script: Script) -> None:
        self.script_path.write_text(script.to_json())

    def driver(self, **kw: Any) -> Any:
        from foreman_inator.runner.opencode import OpenCodeDriver

        kw.setdefault("state_root", self.root / "state")
        kw.setdefault("startup_timeout", 20.0)
        return OpenCodeDriver(opencode_bin=self.bin, **kw)

    def launches(self) -> list[dict[str, Any]]:
        return [json.loads(p.read_text()) for p in sorted(self.record_dir.glob("launch-*.json"))]

    def calls(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in sorted(self.record_dir.glob("calls-*.jsonl")):
            out += [json.loads(line) for line in p.read_text().splitlines() if line]
        return out

    def children(self) -> list[int]:
        return [int(p.read_text()) for p in self.record_dir.glob("child-*.pid")]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="opencode (fake)")
    ap.add_argument("--fake-script", required=True)
    ap.add_argument("--fake-record", required=True)
    ap.add_argument("command")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--hostname", default="127.0.0.1")
    ap.add_argument("--pure", action="store_true")
    ap.add_argument("--print-logs", action="store_true")
    ap.add_argument("--log-level")
    args = ap.parse_args(argv)
    if args.command != "serve":
        print(f"fake opencode: only 'serve' is supported, got {args.command!r}", file=sys.stderr)
        return 2
    record = Path(args.fake_record)
    cfg_path = os.environ.get("OPENCODE_CONFIG")
    launch = {
        "argv": sys.argv[1:],
        "cwd": os.getcwd(),
        "pid": os.getpid(),
        "pgid": os.getpgid(0),
        "config_path": cfg_path,
        "config": json.loads(Path(cfg_path).read_text()) if cfg_path else None,
        "env": {k: v for k, v in os.environ.items() if k.startswith(("XDG_", "OPENCODE_"))},
        "env_names": sorted(os.environ),
    }
    (record / f"launch-{os.getpid()}.json").write_text(json.dumps(launch))
    server = FakeOpenCodeServer(
        Script.from_json(Path(args.fake_script).read_text()),
        directory=os.getcwd(),
        password=os.environ.get("OPENCODE_SERVER_PASSWORD"),
        username=os.environ.get("OPENCODE_SERVER_USERNAME", "opencode"),
        record_dir=record,
        host=args.hostname,
        port=args.port,
    )
    url = server.start()
    print(f"opencode server listening on {url}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
