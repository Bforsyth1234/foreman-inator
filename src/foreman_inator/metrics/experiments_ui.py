"""Start, stop, cancel and promote experiments from the metrics page (todo #67, GH #44).

``routes(store_path)`` adds to the metrics app (``web.full_app`` lists them after the
read-only ``GET /experiments``):

* ``GET /experiments/manage``: champions, a "new experiment" form (role, challenger model
  and prompt picked from lists), the page's background runs with a Stop button, and the
  running experiments with Cancel and "Promote…" (read-only; nothing here writes);
* ``POST /experiments/start``: starts ``worker_experiments.run_command(role=R,
  challenger=MODEL/PROMPT, all_=False)``, exactly what ``fman experiment run --role R
  --challenger MODEL/PROMPT`` calls, as a background job (a thread; the page never waits
  on a replay). Its own proxy, the run lock, the experiments budget and the spend caps
  apply as on the CLI, because it is the CLI's function;
* ``POST /experiments/jobs/{n}/stop``: sets that run's cancel event (as Ctrl-C does on
  the CLI); the replay stops cleanly and ``run --all`` resumes it;
* ``POST /experiments/{id}/cancel``: ``worker_experiments.cancel_experiment`` (ends it as
  rejected; refused while a run holds the run lock);
* ``GET /experiments/{id}/promote``: the comparison (``status_lines``, what ``fman
  experiment status ID`` prints) and a confirm form; ``POST /experiments/{id}/promote``
  calls ``worker_experiments.promote`` (the CLI's function) only with ``confirm=yes`` and
  the champion the confirm page showed. Without them nothing changes.

Every POST goes through ``forms.FormGuard`` (CSRF token, own loopback Origin/Host); GETs
open a read-only store. Everything from the database or a run's output is escaped.
"""

from __future__ import annotations

import atexit
import html
import logging
import sqlite3
import threading
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import BaseRoute, Route

from foreman_inator import prompts
from foreman_inator.config import Config, ConfigError
from foreman_inator.db.store import NotFoundError, Store
from foreman_inator.metrics import forms, web
from foreman_inator.worker_experiments import ExperimentError

log = logging.getLogger("foreman_inator.experiments")

MANAGE_PATH = "/experiments/manage"
MAX_JOB_LINES = 200
#: The page's nav entry (the wiring step adds it to ``web.M4_NAV``'s users).
NAV_ENTRY = (MANAGE_PATH, "Run experiments")

e = html.escape


def _wx() -> Any:
    """``worker_experiments``, looked up at call time (tests patch its functions)."""
    from foreman_inator import worker_experiments

    return worker_experiments


# =========================================================================== jobs


@dataclass
class Job:
    """One background ``run_command`` started from the page."""

    id: int
    role: str
    challenger: str
    started_at: datetime
    cancel: threading.Event = field(default_factory=threading.Event)
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_JOB_LINES))
    state: str = "running"  # running | finished | stopped | failed
    thread: threading.Thread | None = None

    def echo(self, text: str) -> None:
        self.lines.extend(str(text).splitlines() or [""])


#: ``run_command``'s keyword call, as ``fman experiment run --role R --challenger V`` makes it.
Runner = Callable[..., int]


def cli_runner(**kw: Any) -> int:
    return int(_wx().run_command(**kw))


class Jobs:
    """At most one page-started run at a time in this process (the run lock also keeps a
    CLI run and a page run apart). ``run`` is ``run_command`` (tests pass fakes into it)."""

    def __init__(self, run: Runner = cli_runner) -> None:
        self._run = run
        self._lock = threading.Lock()
        self._jobs: list[Job] = []
        atexit.register(self.shutdown)

    def list(self) -> list[Job]:
        with self._lock:
            return list(self._jobs)

    def get(self, job_id: int) -> Job:
        for j in self.list():
            if j.id == job_id:
                return j
        raise NotFoundError(f"no run #{job_id} started from this page")

    def running(self) -> Job | None:
        return next((j for j in self.list() if j.state == "running"), None)

    def start(self, role: str, challenger: str) -> Job:
        with self._lock:
            busy = next((j for j in self._jobs if j.state == "running"), None)
            if busy is not None:
                raise ExperimentError(f"run #{busy.id} is still running; stop it or wait")
            with _wx().run_lock():  # refuse at once while a CLI run holds it
                pass
            job = Job(len(self._jobs) + 1, role, challenger, datetime.now(UTC))
            self._jobs.append(job)
            job.thread = threading.Thread(
                target=self._work, args=(job,), name=f"fman-exp-run-{job.id}", daemon=True
            )
            job.thread.start()
        return job

    def _work(self, job: Job) -> None:
        try:
            self._run(
                role=job.role,
                challenger=job.challenger,
                all_=False,
                echo=job.echo,
                cancel=job.cancel,
            )
            job.state = "stopped" if job.cancel.is_set() else "finished"
        except ExperimentError as exc:
            job.echo(str(exc))
            job.state = "failed"
        except Exception as exc:
            log.exception("experiment run #%d from the metrics page failed", job.id)
            job.echo(f"failed: {type(exc).__name__}: {exc}")
            job.state = "failed"

    def stop(self, job_id: int) -> Job:
        job = self.get(job_id)
        job.cancel.set()
        return job

    def wait(self, timeout: float | None = None) -> None:
        for j in self.list():
            if j.thread is not None:
                j.thread.join(timeout)

    def shutdown(self, timeout: float = 30.0) -> None:
        """Stop every run cleanly when the process exits (a killed run is resumable)."""
        for j in self.list():
            j.cancel.set()
        self.wait(timeout)


# =========================================================================== rendering


def _choices(config: Config) -> tuple[list[str], list[str]]:
    models = [m.name for m in config.models]
    names = [p for p in prompts.list_prompts() if p.startswith(("writer-", "reviewer-"))]
    return models, names


def _select(name: str, label: str, options: Sequence[tuple[str, str]]) -> str:
    opts = "".join(f'<option value="{e(v)}">{e(t)}</option>' for v, t in options)
    return f'<label>{e(label)} <select name="{e(name)}">{opts}</select></label> '


def _start_form(guard: forms.FormGuard, config: Config) -> str:
    from foreman_inator.experiments import variants

    models, names = _choices(config)
    if not models:
        return '<p class="empty">No models in config.toml.</p>'
    model_opts = [
        (m.name, f"{m.name} (tier {m.tier}, {'cloud' if m.is_cloud else 'local'})")
        for m in config.models
    ]
    inner = (
        _select("role", "Role", [(r, r) for r in variants.ROLES])
        + _select("model", "Challenger model", model_opts)
        + _select("prompt", "Prompt", [("", "(the champion's prompt)"), *((n, n) for n in names)])
    )
    return (
        "<h2>New experiment</h2>"
        "<p>Replays the champion against this challenger on past tickets, in the background"
        " (same as <code>fman experiment run --role R --challenger MODEL/PROMPT</code>). Cloud"
        " calls count against the experiments budget and the spend caps; the run stops when"
        " either is spent. Nothing is promoted.</p>"
        + guard.form("/experiments/start", inner, button="Start")
    )


def _jobs_table(guard: forms.FormGuard, jobs: Sequence[Job]) -> str:
    if not jobs:
        return '<h2>Runs started here</h2><p class="empty">None.</p>'
    rows = []
    for j in reversed(jobs):
        stop = (
            guard.form(f"/experiments/jobs/{j.id}/stop", button="Stop")
            if j.state == "running"
            else ""
        )
        out = "\n".join(list(j.lines)[-20:])
        rows.append(
            f"<tr><td>{j.id}</td><td>{e(j.role)}</td><td>{e(j.challenger)}</td>"
            f"<td>{e(j.state)}</td><td>{j.started_at:%Y-%m-%d %H:%M}</td>"
            f"<td><pre>{e(out)}</pre></td><td>{stop}</td></tr>"
        )
    head = "<tr><th>#</th><th>Role</th><th>Challenger</th><th>State</th><th>Started</th>"
    head += "<th>Output</th><th></th></tr>"
    return (
        f'<h2>Runs started here</h2><div class="scroll"><table>{head}{"".join(rows)}</table></div>'
    )


def _running_table(guard: forms.FormGuard, store: Store, config: Config) -> str:
    wx = _wx()
    exps = [x for x in store.list_experiments() if x.status == "running"]
    if not exps:
        return '<h2>Running experiments</h2><p class="empty">None.</p>'
    rows = []
    for x in exps:
        try:
            result = wx.summary(store, x, config)
        except Exception as exc:  # a broken row must not hide the others
            result = f"not scored ({type(exc).__name__})"
        actions = guard.form(f"/experiments/{x.id}/cancel", button="Cancel")
        if "+rules@" not in x.challenger_variant and x.mode in ("replay", "shadow", "split"):
            actions += f' <a href="/experiments/{x.id}/promote">Promote…</a>'
        rows.append(
            f"<tr><td>{x.id}</td><td>{e(x.role)}</td><td>{e(x.mode)}</td>"
            f"<td>{e(x.challenger_variant)}</td><td>{e(x.champion_variant)}</td>"
            f"<td>{e(result)}</td><td>{actions}</td></tr>"
        )
    head = "<tr><th>#</th><th>Role</th><th>Mode</th><th>Challenger</th><th>Champion</th>"
    head += "<th>Result</th><th></th></tr>"
    return _table("Running experiments", head, rows)


def _table(title: str, head: str, rows: Sequence[str]) -> str:
    return f'<h2>{e(title)}</h2><div class="scroll"><table>{head}{"".join(rows)}</table></div>'


def _message(text: str | None, error: bool = False) -> str:
    if not text:
        return ""
    cls = ' class="error"' if error else ""
    return f"<p{cls}>{e(text)}</p>"


# =========================================================================== routes


def routes(
    store_path: str | Path,
    *,
    load_config: Callable[[], Config] | None = None,
    guard: forms.FormGuard | None = None,
    jobs: Jobs | None = None,
    nav: Sequence[tuple[str, str]] | None = None,
) -> list[BaseRoute]:
    """The page's routes. ``load_config`` defaults to ``config.load`` (what the CLI reads
    on each command); ``guard`` to the per-process ``forms.default_guard()``."""
    from foreman_inator import config as config_mod
    from foreman_inator.experiments import variants

    path = Path(store_path)
    guard = guard or forms.default_guard()
    jobs = jobs if jobs is not None else Jobs()
    nav = tuple(nav) if nav is not None else (*web.M4_NAV, NAV_ENTRY)
    cfg_of = load_config or config_mod.load

    def page(title: str, body: str, status: int = 200) -> HTMLResponse:
        back = f'<p><a href="{MANAGE_PATH}">Back to experiments</a></p>'
        return HTMLResponse(web.render_page(title, body + back, nav=nav), status)

    def error(text: str, status: int) -> HTMLResponse:
        return page("Experiments", f"<h1>Experiments</h1>{_message(text, True)}", status)

    def config() -> Config:
        try:
            return cfg_of()
        except ConfigError as exc:
            raise _Fail(f"config.toml: {exc}", 500) from None

    def read_store() -> Store:
        try:
            return web.open_store(path)
        except (FileNotFoundError, sqlite3.OperationalError):
            raise _Fail("No database yet: run the worker first.", 503) from None

    def write_store() -> Store:
        if not path.exists():
            raise _Fail("No database yet: run the worker first.", 503)
        return Store(path)

    def guarded(fn: Callable[..., Response]) -> Callable[..., Response]:
        def run(*a: Any) -> Response:
            try:
                return fn(*a)
            except _Fail as f:
                return error(str(f), f.status)
            except NotFoundError as exc:
                return error(str(exc), 404)

        return run

    @guarded
    def manage(request: Request) -> Response:
        cfg = config()
        store = read_store()
        try:
            champs = "".join(f"<li>{e(line)}</li>" for line in variants.status_lines(store))
            running = _running_table(guard, store, cfg)
        finally:
            store.close()
        body = (
            "<h1>Run experiments</h1>"
            '<p class="lede">A challenger becomes champion only after it wins on replay and '
            "holds up live, and only when you confirm a promotion.</p>"
            f"<h2>Champions</h2><ul>{champs}</ul>"
            + _start_form(guard, cfg)
            + _jobs_table(guard, jobs.list())
            + running
        )
        return HTMLResponse(web.render_page("Run experiments", body, nav=nav))

    @guarded
    def start(request: Request, form: dict[str, str]) -> Response:
        cfg = config()
        role, model, prompt = (form.get(k, "").strip() for k in ("role", "model", "prompt"))
        models, names = _choices(cfg)
        if role not in variants.ROLES:
            raise _Fail(f"role must be one of {', '.join(variants.ROLES)}", 400)
        if model not in models:
            raise _Fail(f"unknown model {model!r}: pick one from the list", 400)
        if prompt and (prompt not in names or not prompt.startswith(f"{role}-")):
            raise _Fail(f"prompt {prompt!r} is not a {role} prompt", 400)
        spec = f"{model}/{prompt}" if prompt else model
        store = read_store()
        try:
            chall = _wx().resolve_challenger(store, cfg, role, spec)
            champ = variants.champion(store, role)
        except ExperimentError as exc:
            raise _Fail(str(exc), 400) from None
        finally:
            store.close()
        if champ is not None and chall.variant_id == champ.id:
            raise _Fail(f"{chall.variant_id} is the {role} champion", 400)
        try:
            job = jobs.start(role, spec)
        except ExperimentError as exc:
            raise _Fail(str(exc), 409) from None
        msg = f"Started run #{job.id}: {role} challenger {chall.variant_id}, in the background."
        return page("Run started", f"<h1>Run started</h1>{_message(msg)}")

    @guarded
    def stop(request: Request, form: dict[str, str]) -> Response:
        job = jobs.stop(int(request.path_params["job"]))
        msg = f"Stopping run #{job.id}; `fman experiment run --all` (or a new start) resumes it."
        return page("Run stopping", f"<h1>Run stopping</h1>{_message(msg)}")

    @guarded
    def cancel(request: Request, form: dict[str, str]) -> Response:
        exp_id = int(request.path_params["id"])
        store = write_store()
        try:
            exp = _wx().cancel_experiment(store, exp_id)
        except ExperimentError as exc:
            raise _Fail(str(exc), 409) from None
        finally:
            store.close()
        msg = f"Experiment #{exp.id} ({exp.challenger_variant}) cancelled: ended as rejected."
        return page("Cancelled", f"<h1>Cancelled</h1>{_message(msg)}")

    def confirm_body(store: Store, cfg: Config, exp_id: int, note: str = "") -> str:
        wx = _wx()
        exp = store.get_experiment(exp_id)
        lines = wx.status_lines(store, cfg, exp_id)
        head = f"<h1>Promote experiment #{exp.id}?</h1>{note}<pre>{e(chr(10).join(lines))}</pre>"
        if exp.status != "running":
            return head + _message(f"#{exp.id} is {exp.status}; nothing to promote.", True)
        current = variants.champion(store, exp.role)
        cur = current.id if current else ""
        inner = (
            '<input type="hidden" name="confirm" value="yes">'
            f'<input type="hidden" name="champion" value="{e(cur)}">'
        )
        if exp.role == "writer":
            inner += (
                '<label><input type="checkbox" name="replay_only" value="1"> Promote on the'
                " replay alone (<code>--replay-only</code>, no live check)</label> "
            )
        return head + (
            f"<p>The live {e(exp.role)} champion is <code>{e(cur or '(none)')}</code>. Promoting"
            f" makes <code>{e(exp.challenger_variant)}</code> the {e(exp.role)} from the"
            " worker's next attempt or review, if it won on replay and holds live (the same"
            " check as <code>fman experiment promote</code>; a challenger that lost is ended"
            " as rejected).</p>"
            + guard.form(f"/experiments/{exp.id}/promote", inner, button="Confirm promote")
        )

    @guarded
    def promote_page(request: Request) -> Response:
        exp_id = int(request.path_params["id"])
        cfg = config()
        store = read_store()
        try:
            body = confirm_body(store, cfg, exp_id)
        finally:
            store.close()
        return page(f"Promote #{exp_id}?", body)

    @guarded
    def promote(request: Request, form: dict[str, str]) -> Response:
        exp_id = int(request.path_params["id"])
        cfg = config()
        store = write_store()
        try:
            exp = store.get_experiment(exp_id)
            current = variants.champion(store, exp.role)
            cur = current.id if current else ""
            stale = None
            if form.get("confirm") != "yes":
                stale = "Not promoted: confirm on this page first."
            elif form.get("champion", "") != cur:
                stale = "Not promoted: the champion changed since that page; look again."
            if stale is not None:
                body = confirm_body(store, cfg, exp_id, _message(stale, True))
                return page(f"Promote #{exp_id}?", body, 400)
            replay_only = form.get("replay_only") == "1"
            try:
                res = _wx().promote(store, cfg, str(exp_id), replay_only=replay_only)
            except ExperimentError as exc:
                raise _Fail(str(exc), 409) from None
        finally:
            store.close()
        status = 200 if res.outcome == "promoted" else 409
        title = res.outcome.capitalize()
        msg = f"{res.outcome.upper()}: {res.message}"
        return page(title, f"<h1>{e(title)}</h1>{_message(msg, res.outcome != 'promoted')}", status)

    return [
        Route(MANAGE_PATH, manage, methods=["GET"]),
        Route("/experiments/{id:int}/promote", promote_page, methods=["GET"]),
        guard.post_route("/experiments/start", start),
        guard.post_route("/experiments/jobs/{job:int}/stop", stop),
        guard.post_route("/experiments/{id:int}/cancel", cancel),
        guard.post_route("/experiments/{id:int}/promote", promote),
    ]


class _Fail(Exception):
    def __init__(self, text: str, status: int) -> None:
        super().__init__(text)
        self.status = status


__all__ = ["MANAGE_PATH", "NAV_ENTRY", "Job", "Jobs", "cli_runner", "routes"]
