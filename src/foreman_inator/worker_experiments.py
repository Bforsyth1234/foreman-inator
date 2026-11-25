"""``fman experiment run | status | promote`` and ``fman replay`` (todo #29).

Experiments are a separate process from the worker (``fman experiment run``, by hand or
from the nightly launchd agent). It never touches GitHub and never writes the live tables
(``tickets``, ``attempts``, ``reviews``, ``kickbacks``): replays (#25) write only the
``experiments`` row and the 005 replay tables; the shadow reviewer (#26) runs inside the
worker.

Running beside the worker
-------------------------
* **Its own model proxy** (``make_proxy``): a second ``ModelProxy`` on another loopback
  port, with the real ``Spend`` hook, so every replay call is a traced ``llm.call`` in the
  ``experiments`` bucket (7c). The worker's proxy is never used.
* **Its own repo mirrors** (``sync_replay_mirror``): replays never run git in the worker's
  mirror. Each repo gets a bare mirror under ``~/.foreman-inator/replays/mirrors/``, filled
  from the worker's mirror over git's local transport: the base-branch refs, the attempt
  branches, and, fetched by sha, every attempt's base and recorded head commit (the worker
  deletes an attempt's branch once its ticket closes). Reading the worker's mirror is a
  read; the replay's scratch worktrees and branches live only in the replay mirror, so
  the worker's sweep of ``scratch-*`` worktrees can never remove a running replay's.
* **One run at a time**: ``run.lock`` under ``~/.foreman-inator/replays/`` is held with
  ``flock`` for the whole run; a second ``fman experiment run`` exits at once.
* **Budgets**: the monthly experiments budget (``spend_caps.experiments_monthly_usd``, the
  ledger shared with the worker's shadow reviews) and ``[experiments] run_budget_usd`` per
  replay run; either stops the run cleanly (``replay_runner``), nobody is asked.
* **Stops**: SIGINT and SIGTERM set the cancel event; the running case side is thrown away
  and its processes killed; the run is resumed by the next ``run`` or ``run --all``.
  Killed outright (SIGKILL), a side is stored only once scored, so a resume reruns only
  unscored sides; nothing live was touched.

Experiments and promotion (SPEC "Promotion rule": wins on replay AND holds live)
-------------------------------------------------------------------------------
``run --role R --challenger V`` registers V, starts (or resumes) the replay of the
current champion against V, and for a reviewer also starts V's shadow experiment at once
(SPEC: the shadow is "always on once a challenger exists"; only the oldest running one
gets traffic). ``run --all`` resumes every unfinished replay; it never promotes.

``promote <experiment id | variant>`` is the only way a champion changes:

1. the newest running replay of V must be against the *current* champion, finished, and
   ``wins``; ``does_not_win`` ends V's experiments as ``rejected``; unfinished or
   inconclusive changes nothing;
2. holds live:
   * reviewer: ``shadow.judge_live`` over at least ``live_min_sample`` paired PRs the user
     decided: ``holds`` → promote; ``fails`` → ``rejected``; ``insufficient`` → nothing;
   * writer: the traffic split (#34, ``experiments.split``): the newest running split of
     V against the current champion, ``split.judge`` over at least ``live_min_sample``
     decided first attempts per side: ``holds`` → promote; ``fails`` → ``rejected``;
     ``insufficient`` (or no split) → nothing, unless ``--replay-only``, which promotes
     on the replay alone and says so in the champion history note.
3. promoting writes a ``champion_history`` row and ends V's running experiments as
   ``promoted``; the worker uses the new champion from its next attempt or review.

Traffic split (#34, GH #16)
---------------------------
``split start V [--share 0.15]`` (``start_split``) starts a writer split of the current
champion against V, refused unless V's newest replay against that champion finished and
``wins``, V's model is configured on the champion model's tier and is not cloud when the
champion's model is local, and no other split against the champion is running.
``split stop <id>`` (``stop_split``) ends it as ``rejected``. The worker reads the
running split on each first attempt (``worker_attempt``). ``ab:pair`` is not built.

Rules replays (#43, todo #66)
-----------------------------
``run --role R --rules FILE [--challenger V]`` replays FILE in place of the gathered
rules on the challenger side only (``replay_runner.RulesOverride``); V defaults to the
champion's own model and prompt, so the comparison isolates the rules. The challenger's
id is V's id plus ``+rules@<hash8>`` of the file's text, and the text is kept under
``replays/rules/<sha256>.md`` when the run starts: ``run --all`` resumes with that copy,
so editing FILE afterwards never changes the old run (a re-run with the edited file is a
new variant and a new experiment). A rules variant is not promoted: the rules go live by
editing the rules file itself.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import signal
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from foreman_inator import paths, prompts
from foreman_inator.config import Config, ConfigError
from foreman_inator.db.store import Experiment, Store
from foreman_inator.experiments import replay_runner as rr
from foreman_inator.experiments import replay_sets as rs
from foreman_inator.experiments import shadow, variants
from foreman_inator.experiments import split as split_mod
from foreman_inator.runner.worktree import MIRROR_DIRNAME, RepoMirror, WorktreeError, _git

log = logging.getLogger("foreman_inator.experiments")

REPLAYS_DIRNAME = "replays"
RULES_DIRNAME = "rules"  # replays/rules/<sha256>.md: rules override texts, as run (#43)
LOCK_NAME = "run.lock"
KEEP_REF = "refs/fman-keep/"


class ExperimentError(RuntimeError):
    """A request the experiment commands refuse (the message says why and what to do)."""


def replays_root(home: Path | None = None) -> Path:
    root = (home or paths.home()) / REPLAYS_DIRNAME
    root.mkdir(mode=paths.DIR_MODE, parents=True, exist_ok=True)
    return root


def settings_from(config: Config) -> rr.ReplaySettings:
    ex = config.experiments
    return rr.ReplaySettings(
        set_size=ex.set_size,
        run_budget_usd=ex.run_budget_usd,
        writer_timeout_s=ex.writer_timeout_s,
        verify_timeout_s=ex.verify_timeout_s,
        review_timeout_s=ex.review_timeout_s,
        min_cases=ex.min_cases,
    )


# =========================================================================== mirrors


def _wanted_commits(store: Store, repo: str, recordings_dir: Path | None) -> list[str]:
    """Every attempt's base commit and recorded head commit for ``repo``."""
    out: list[str] = []
    for t in store.list_tickets(repo=repo):
        for a in store.list_attempts(t.id):
            if a.base_commit:
                out.append(a.base_commit)
            head = rs._recorded_head(recordings_dir, t.id, a.id)
            if head:
                out.append(head)
    return sorted(set(out))


def _has(mirror: Path, sha: str) -> bool:
    proc = _git(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=mirror, check=False)
    return proc.returncode == 0


def sync_replay_mirror(
    store: Store,
    repo: str,
    *,
    worker_root: Path,
    replay_root: Path,
    recordings_dir: Path | None = None,
) -> RepoMirror | None:
    """The replay mirror of ``repo``, brought up to date from the worker's mirror (see the
    module docstring). None when the worker has no mirror of the repo."""
    src = Path(worker_root) / repo / MIRROR_DIRNAME
    if not (src / "HEAD").exists():
        return None
    mirror = RepoMirror(replay_root, repo, str(src))
    m = mirror.mirror_path
    if not (m / "HEAD").exists():
        m.mkdir(parents=True, exist_ok=True)
        _git(["init", "-q", "--bare"], cwd=m)
        _git(["config", "gc.auto", "0"], cwd=m)
    _git(["config", "remote.origin.url", str(src)], cwd=m)
    # Base branches and attempt branches only: the replay's own fman/replay/* branches
    # are outside both patterns, so --prune never touches them.
    _git(
        ["-c", "uploadpack.allowAnySHA1InWant=true", "fetch", "-q", "--prune", "--no-tags",
         "origin", "+refs/remotes/origin/*:refs/remotes/origin/*",
         "+refs/heads/fman/issue-*:refs/heads/fman/issue-*"],
        cwd=m,
    )  # fmt: skip
    missing = [s for s in _wanted_commits(store, repo, recordings_dir) if not _has(m, s)]
    for sha in missing:  # unreachable in the worker's mirror once a branch is deleted
        if not _has(src, sha):
            continue
        _git(
            ["-c", "uploadpack.allowAnySHA1InWant=true", "fetch", "-q", "--no-tags", str(src),
             f"{sha}:{KEEP_REF}{sha}"],
            cwd=m,
            check=False,
        )  # fmt: skip
    mirror.prune()
    return mirror


def mirror_for(
    store: Store, *, worker_root: Path, replay_root: Path, recordings_dir: Path | None
) -> Callable[[str], RepoMirror | None]:
    cache: dict[str, RepoMirror | None] = {}

    def get(repo: str) -> RepoMirror | None:
        if repo not in cache:
            try:
                cache[repo] = sync_replay_mirror(
                    store,
                    repo,
                    worker_root=worker_root,
                    replay_root=replay_root,
                    recordings_dir=recordings_dir,
                )
            except WorktreeError as exc:
                log.warning("replay mirror of %s: %s", repo, exc)
                cache[repo] = None
        return cache[repo]

    return get


# =========================================================================== variants


def _role(role: str) -> rr.Role:
    if role not in variants.ROLES:
        raise ExperimentError(f"role must be one of {', '.join(variants.ROLES)}")
    return role  # type: ignore[return-value]


def _endpoint(config: Config, model: str) -> Any:
    try:
        return config.model(model)
    except ConfigError as exc:
        raise ExperimentError(f"{exc}; add it to config.toml first") from None


def _variant(
    config: Config, role: rr.Role, model: str, prompt: str, hash8: str | None
) -> rr.Variant:
    try:
        ref = prompts.load(prompt)
    except (FileNotFoundError, ValueError) as exc:
        raise ExperimentError(f"prompt {prompt!r}: {exc}") from None
    vid = variants.variant_id(model, ref)
    if hash8 is not None and ref.sha256[:8] != hash8:
        raise ExperimentError(
            f"{model}/{prompt}@{hash8} is not the prompt file on disk (now {vid}); an edited "
            "prompt is a new variant: name it with its new hash, or a new prompt file"
        )
    ep = _endpoint(config, model)
    if role == "writer":
        return rr.WriterVariant(vid, ep, ref)
    return rr.ReviewerVariant(vid, ep, prompt)


def champion_variant(store: Store, config: Config, role: rr.Role) -> rr.Variant:
    """The current champion as a runnable variant (its prompt file must be unchanged)."""
    champ = variants.champion(store, role)
    if champ is None:
        variants.ensure_defaults(store, config)
        champ = variants.champion(store, role)
    if champ is None:
        raise ExperimentError(f"no {role} champion (no {role} model is configured)")
    st = variants.role_status(store, role)
    if not st.matches:
        raise ExperimentError(
            f"the {role} champion {champ.id}'s prompt file {champ.prompt_name}.md changed "
            "since it was promoted, so the champion cannot be replayed; revert the file, or "
            "put the change in a new prompt file and run it as the challenger"
        )
    return _variant(config, role, champ.model, champ.prompt_name, champ.prompt_hash[:8])


def resolve_challenger(store: Store, config: Config, role: rr.Role, spec: str) -> rr.Variant:
    """``<model>/<prompt>@<hash8>``, ``<model>/<prompt>`` (the file on disk) or ``<model>``
    (the champion's prompt)."""
    spec = spec.strip()
    if "@" in spec:
        try:
            p = variants.parse_variant_id(spec)
        except variants.VariantError as exc:
            raise ExperimentError(str(exc)) from None
        return _variant(config, role, p.model, p.prompt_name, p.hash8)
    if "/" in spec:
        model, _, prompt = spec.rpartition("/")
        return _variant(config, role, model, prompt, None)
    champ = variants.champion(store, role)
    prompt = champ.prompt_name if champ else variants.default_prompt_name(role)
    return _variant(config, role, spec, prompt, None)


def _register(store: Store, role: rr.Role, v: rr.Variant) -> None:
    ref = v.template if isinstance(v, rr.WriterVariant) else prompts.load(v.prompt_name)
    variants.register(store, role, v.endpoint.name, ref)


def _variant_from_id(
    config: Config, role: rr.Role, vid: str, home: Path | None = None
) -> rr.Variant:
    base, rules_hash = rr.split_rules_id(vid)
    p = variants.parse_variant_id(base)
    v = _variant(config, role, p.model, p.prompt_name, p.hash8)
    return v if rules_hash is None else rr.with_rules(v, load_rules(rules_hash, home))


# =========================================================================== rules (#43)


def _rules_dir(home: Path | None = None) -> Path:
    d = replays_root(home) / RULES_DIRNAME
    d.mkdir(mode=paths.DIR_MODE, parents=True, exist_ok=True)
    return d


def read_rules(path: Path | str) -> rr.RulesOverride:
    """The override from ``--rules FILE`` (refused when missing or empty)."""
    p = Path(path).expanduser()
    try:
        rules = rr.RulesOverride.from_path(p)
    except (OSError, UnicodeDecodeError) as exc:
        raise ExperimentError(f"--rules {p}: {exc}") from None
    if not rules.text.strip():
        raise ExperimentError(f"--rules {p} is empty")
    return rules


def keep_rules(rules: rr.RulesOverride, home: Path | None = None) -> Path:
    """Store the override's text by its hash (what a resumed run replays)."""
    dest = _rules_dir(home) / f"{rules.sha256}.md"
    if not dest.exists():
        tmp = dest.with_suffix(".tmp")
        tmp.write_text(f"{rules.name}\n{rules.text}", encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(dest)
    return dest


def load_rules(hash8: str, home: Path | None = None) -> rr.RulesOverride:
    """The kept override whose sha256 starts with ``hash8`` (its hash re-checked)."""
    found = sorted(_rules_dir(home).glob(f"{hash8}*.md")) if hash8.isalnum() else []
    for path in found:
        name, _, text = path.read_text(encoding="utf-8").partition("\n")
        rules = rr.RulesOverride(text, name or "AGENTS.md")
        if rules.sha256 == path.stem:
            return rules
    raise ExperimentError(f"no kept rules text for +rules@{hash8}; run it again with --rules")


def ensure_shadow(store: Store, champion: str, challenger: str) -> Experiment:
    """The running reviewer shadow experiment for this challenger (started if none)."""
    for e in shadow.running_shadow_experiments(store):
        if e.challenger_variant == challenger:
            return e
    exp = store.create_experiment(
        role="reviewer", champion_variant=champion, challenger_variant=challenger, mode="shadow"
    )
    log.info("experiment %d: shadow reviews by %s started", exp.id, challenger)
    return exp


# =========================================================================== split (#34)


def start_split(
    store: Store, config: Config, challenger: str, *, share: float = split_mod.DEFAULT_SHARE
) -> Experiment:
    """``fman experiment split start V``: see the module docstring ("Traffic split").
    Returns the running split (an already running one of V against the champion as is)."""
    try:
        share = split_mod.check_share(share)
    except ValueError as exc:
        raise ExperimentError(str(exc)) from None
    champ = champion_variant(store, config, "writer")
    chall = resolve_challenger(store, config, "writer", challenger)
    vid = chall.variant_id
    if vid == champ.variant_id:
        raise ExperimentError(f"{vid} is the writer champion")
    for e in split_mod.running_splits(store):
        if e.champion_variant != champ.variant_id:
            continue
        if e.challenger_variant == vid:
            return e
        raise ExperimentError(
            f"split #{e.id} ({e.challenger_variant}) is already running against the champion;"
            f" stop it first (`fman experiment split stop {e.id}`)"
        )
    min_cases = config.experiments.min_cases
    if not split_mod.replay_won(store, vid, champ.variant_id, min_cases=min_cases):
        raise ExperimentError(
            f"{vid} has not won a finished replay against the champion {champ.variant_id}; "
            f"run `fman experiment run --role writer --challenger {vid}` first"
        )
    ch_ep, co_ep = chall.endpoint, champ.endpoint
    if ch_ep.tier != co_ep.tier:
        raise ExperimentError(
            f"{ch_ep.name} is a tier {ch_ep.tier} model and the champion's {co_ep.name} is "
            f"tier {co_ep.tier}; a split only swaps a model on the same tier"
        )
    if ch_ep.is_cloud and not co_ep.is_cloud:
        raise ExperimentError(
            f"{ch_ep.name} is a cloud model and the champion's {co_ep.name} is local; a split "
            "never sends local tickets to the cloud"
        )
    exp = store.create_experiment(
        role="writer",
        champion_variant=champ.variant_id,
        challenger_variant=vid,
        mode="split",
        traffic_share=share,
    )
    log.info("experiment %d: writer split %.0f%% to %s started", exp.id, share * 100, vid)
    return exp


def stop_split(store: Store, experiment_id: int) -> Experiment:
    """``fman experiment split stop <id>``: end a running split (``rejected``, with its
    live comparison so far in the note). Its attempts and their cursors are kept."""
    exp = store.get_experiment(experiment_id)
    if exp.mode != "split" or exp.status != "running":
        raise ExperimentError(f"#{exp.id} is not a running split ({exp.mode}, {exp.status})")
    c = split_mod.score(store, exp.id)
    j = split_mod.judge(c)
    note = f"stopped by `fman experiment split stop`\n{split_mod.comparison_note(c, j)}"
    return store.end_experiment(exp.id, "rejected", note)


def split_lines(store: Store, config: Config, experiment_id: int | None = None) -> list[str]:
    """``fman experiment split status [id]`` (reads only)."""
    exps = (
        [store.get_experiment(experiment_id)]
        if experiment_id is not None
        else split_mod.running_splits(store)
    )
    if not exps:
        return ["No running writer split. Start one with `fman experiment split start V`."]
    min_sample = config.experiments.live_min_sample
    lines: list[str] = []
    active = split_mod.active_split(store, min_cases=config.experiments.min_cases)
    for e in exps:
        if e.mode != "split":
            raise ExperimentError(f"#{e.id} is a {e.mode} experiment, not a split")
        share = split_mod.DEFAULT_SHARE if e.traffic_share is None else e.traffic_share
        live = "gets traffic" if active is not None and active.id == e.id else "no traffic"
        c = split_mod.score(store, e.id)
        j = split_mod.judge(c, min_sample=min_sample)
        lines += [
            f"Split #{e.id}: {e.status}, {share:.0%} of first attempts, {live}",
            *split_mod.comparison_note(c, j).splitlines(),
        ]
    return lines


# =========================================================================== running


@dataclass
class RunEnv:
    """What ``run_experiment`` and ``run_all`` need (``open_run_env`` builds the real one)."""

    store: Store
    config: Config
    deps: rr.ReplayDeps
    cancel: threading.Event = field(default_factory=threading.Event)
    home: Path | None = None  # replays root parent (kept rules texts); None = paths.home()


def make_proxy(config: Config, store: Store, spend: Any, local_slots: Any = None) -> Any:
    """The experiment process's own model proxy (a free loopback port). #34: its local
    calls are capped at ``max_local_inference`` through the cross-process slot files the
    worker also uses, so a replay and the worker never run two local inferences past the
    cap; a run inside the worker's process may pass the worker proxy's ``slots``."""
    from foreman_inator.proxy.server import ModelProxy, shared_local_slots

    if local_slots is None:
        local_slots = shared_local_slots(int(config.max_local_inference))
    return ModelProxy(
        config.proxy_models(),
        store=store,
        spend_hook=spend.hook,
        local_slots=local_slots,
    ).start()


def build_deps(
    store: Store,
    config: Config,
    *,
    proxy: Any,
    spend: Any,
    driver: Any = None,
    home: Path | None = None,
    worker_root: Path | None = None,
    recordings_dir: Path | None = None,
) -> rr.ReplayDeps:
    root = replays_root(home)
    rec = recordings_dir if recordings_dir is not None else paths.recordings_dir()
    work = root / "work"
    work.mkdir(parents=True, exist_ok=True)
    return rr.ReplayDeps(
        store=store,
        proxy=proxy,
        mirror_for=mirror_for(
            store,
            worker_root=worker_root if worker_root is not None else paths.worktrees_dir(),
            replay_root=root / "mirrors",
            recordings_dir=rec,
        ),
        sandbox=rr.config_sandbox(config),
        work_dir=work,
        driver=driver,
        endpoint_for=config.model,
        spend=spend,
        recordings_dir=rec,
        config=config,
    )


def run_experiment(
    env: RunEnv,
    role: str,
    challenger: str | None,
    *,
    rules: Path | str | None = None,
    home: Path | None = None,
) -> rr.Comparison:
    """Start or resume the replay of the champion against ``challenger`` (and, for a
    reviewer, its shadow experiment). ``rules`` (#43): a rules file replayed on the
    challenger side only, in place of the gathered rules; ``challenger`` then defaults
    to the champion's model and prompt."""
    r = _role(role)
    champ = champion_variant(env.store, env.config, r)
    if challenger is None and rules is None:
        raise ExperimentError("give --challenger (or --rules)")
    spec = challenger if challenger is not None else champ.variant_id
    chall = resolve_challenger(env.store, env.config, r, rr.split_rules_id(spec)[0])
    if rules is not None:
        override = read_rules(rules)
        if r == "reviewer":
            try:
                rr.check_reviewer_rules(chall.prompt_name)  # type: ignore[union-attr]
            except ValueError as exc:
                raise ExperimentError(str(exc)) from None
        chall = rr.with_rules(chall, override)
        keep_rules(override, home)
    if chall.variant_id == champ.variant_id:
        raise ExperimentError(f"{chall.variant_id} is the {r} champion")
    _register(env.store, r, chall)
    if r == "reviewer" and chall.rules is None:
        # a rules variant has no live shadow: the worker's reviewer reads the real rules
        ensure_shadow(env.store, champ.variant_id, chall.variant_id)
    done = _newest(env.store, r, chall.variant_id, "replay")
    if done is not None and done.champion_variant == champ.variant_id:
        c = rr.compare(env.store, done.id, min_cases=env.config.experiments.min_cases)
        if c.state == "finished":  # already run against this champion: nothing to rerun
            return c
    log.info("replay: %s %s against the champion %s", r, chall.variant_id, champ.variant_id)
    return rr.run_replay(
        env.deps,
        role=r,
        champion=champ,
        challenger=chall,
        settings=settings_from(env.config),
        cancel=env.cancel,
    )


def unfinished_replays(store: Store) -> list[Experiment]:
    rows = store.query(
        "SELECT e.id FROM experiments e JOIN replay_runs r ON r.experiment_id = e.id"
        " WHERE e.mode = 'replay' AND e.status = 'running' AND r.state != 'finished'"
        " ORDER BY e.id"
    )
    return [store.get_experiment(int(r["id"])) for r in rows]


def run_all(env: RunEnv, echo: Callable[[str], None] = print) -> list[rr.Comparison]:
    """``run --all`` (the nightly job): resume every unfinished replay against the current
    champion. Never promotes."""
    out: list[rr.Comparison] = []
    todo = unfinished_replays(env.store)
    if not todo:
        echo("No unfinished replays. Start one with `fman experiment run --role R --challenger V`.")
    for exp in todo:
        if env.cancel.is_set():
            break
        role = _role(exp.role)
        current = variants.champion(env.store, role)
        if current is None or current.id != exp.champion_variant:
            echo(f"#{exp.id}: compares against {exp.champion_variant}, no longer the champion;"
                 " skipped")  # fmt: skip
            continue
        try:
            champ = champion_variant(env.store, env.config, role)
            chall = _variant_from_id(env.config, role, exp.challenger_variant, env.home)
        except (ExperimentError, variants.VariantError) as exc:
            echo(f"#{exp.id}: skipped: {exc}")
            continue
        if role == "reviewer" and chall.rules is None:
            ensure_shadow(env.store, champ.variant_id, chall.variant_id)
        c = rr.run_replay(
            env.deps,
            role=role,
            champion=champ,
            challenger=chall,
            settings=settings_from(env.config),
            experiment_id=exp.id,
            cancel=env.cancel,
        )
        echo(f"#{exp.id}: {c.state}{f' ({c.stop_reason})' if c.stop_reason else ''}: {c.verdict}")
        out.append(c)
        if c.stop_reason in (rr.STOP_EXPERIMENTS_BUDGET, rr.STOP_SANDBOX):
            break  # every other run would stop the same way
    return out


@contextlib.contextmanager
def run_lock(home: Path | None = None) -> Iterator[None]:
    """One ``fman experiment run`` at a time (``flock``; released when the process dies)."""
    path = replays_root(home) / LOCK_NAME
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ExperimentError("another `fman experiment run` is running") from None
        yield
    finally:
        os.close(fd)


def install_cancel_handlers(cancel: threading.Event) -> dict[int, Any]:
    """SIGINT and SIGTERM set ``cancel`` (the run stops cleanly). Returns the old ones."""

    def handler(signum: int, _frame: Any) -> None:
        log.info("experiment run stopping: signal %s", signal.Signals(signum).name)
        cancel.set()

    return {s: signal.signal(s, handler) for s in (signal.SIGINT, signal.SIGTERM)}


def run_command(
    *,
    role: str | None,
    challenger: str | None,
    all_: bool,
    echo: Callable[[str], None] = print,
    proxy_factory: Callable[[Config, Store, Any], Any] | None = None,
    driver_factory: Callable[[], Any] | None = None,
    config: Config | None = None,
    local_slots: Any = None,
    rules: Path | str | None = None,
    mode: str = "replay",
    seed: int | None = None,
    kinds: list[str] | tuple[str, ...] | None = None,
) -> int:
    """``fman experiment run``: its own proxy, its own mirrors, the run lock, clean stops.
    ``local_slots``: the worker proxy's ``slots`` when run inside the worker (#34).
    ``rules`` (#43, ``--rules FILE``): replay FILE in place of the gathered rules on the
    challenger side only; ``challenger`` may then be omitted (the champion's model and
    prompt). ``mode="seeded"`` (#35, ``--mode seeded``): a reviewer challenger against
    the champion on merged cases with a planted bug (``replay_runner.run_seeded``);
    ``seed`` and ``kinds`` choose the mutations."""
    from foreman_inator import config as config_mod
    from foreman_inator.spend import Spend

    if mode not in ("replay", "seeded"):
        raise ExperimentError(f"unknown --mode {mode!r}; valid: replay, seeded")
    if mode == "seeded" and (all_ or rules is not None or role != "reviewer" or not challenger):
        raise ExperimentError("--mode seeded needs --role reviewer and --challenger (no --rules)")
    if mode == "replay" and (seed is not None or kinds):
        raise ExperimentError("--seed and --kinds go with --mode seeded")

    if all_ == bool(role or challenger or rules):
        raise ExperimentError("give --role and --challenger (or --rules), or --all")
    if not all_ and not (role and (challenger or rules)):
        raise ExperimentError("give --role and --challenger (or --rules)")
    if rules is not None:
        read_rules(rules)  # refuse a missing or empty file before taking the lock
    cfg = config or config_mod.load()
    if not paths.db_path().exists():
        raise ExperimentError("no database yet: run the worker first")
    with run_lock(), Store(paths.db_path()) as store:
        spend = Spend(store, cfg)
        if proxy_factory is not None:
            proxy = proxy_factory(cfg, store, spend)
        elif local_slots is not None:
            proxy = make_proxy(cfg, store, spend, local_slots=local_slots)
        else:
            proxy = make_proxy(cfg, store, spend)
        if driver_factory is None:
            from foreman_inator.runner.opencode import OpenCodeDriver

            # #34: time queued for a local slot never counts against a replay's writer.
            def driver_factory() -> Any:
                return OpenCodeDriver(extra_time=getattr(proxy, "queue_wait", None))

        env = RunEnv(store, cfg, build_deps(store, cfg, proxy=proxy, spend=spend,
                                            driver=driver_factory()))  # fmt: skip
        old = install_cancel_handlers(env.cancel) if _main_thread() else {}
        try:
            if all_:
                run_all(env, echo)
            elif mode == "seeded":
                run_seeded_experiment(env, challenger or "", seed=seed, kinds=kinds, echo=echo)
            else:
                c = run_experiment(env, role or "", challenger, rules=rules)
                echo(rr.comparison_note(c))
                echo(f"Experiment #{c.experiment_id}: `fman experiment status {c.experiment_id}`")
        finally:
            for sig, h in old.items():
                signal.signal(sig, h)
            with contextlib.suppress(Exception):
                proxy.stop()
    if env.cancel.is_set():
        echo("Stopped; `fman experiment run --all` resumes it.")
    return 0


def run_seeded_experiment(
    env: RunEnv,
    challenger: str,
    *,
    seed: int | None = None,
    kinds: list[str] | tuple[str, ...] | None = None,
    echo: Callable[[str], None] = print,
) -> rr.SeededComparison:
    """#35: start or resume the seeded-bug comparison of the reviewer champion against
    ``challenger`` and print its result."""
    champ = champion_variant(env.store, env.config, "reviewer")
    chall = resolve_challenger(env.store, env.config, "reviewer", challenger)
    if chall.variant_id == champ.variant_id:
        raise ExperimentError(f"{chall.variant_id} is the reviewer champion")
    _register(env.store, "reviewer", chall)
    try:
        c = rr.run_seeded(
            env.deps,
            champion=champ,  # type: ignore[arg-type]
            challenger=chall,  # type: ignore[arg-type]
            seed=seed,
            kinds=kinds or None,
            settings=settings_from(env.config),
            cancel=env.cancel,
        )
    except ValueError as exc:
        raise ExperimentError(str(exc)) from None
    echo(rr.seeded_note(c))
    echo(f"Experiment #{c.experiment_id}: `fman experiment status {c.experiment_id}`")
    return c


def _main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


def _newest(store: Store, role: str, challenger: str, mode: str) -> Experiment | None:
    found = [
        e
        for e in store.list_experiments(role=role, status="running")  # type: ignore[arg-type]
        if e.mode == mode and e.challenger_variant == challenger
    ]
    return found[-1] if found else None


# The status, promotion and ``fman replay`` code lives in ``worker_promote`` and
# ``worker_replay`` (kept under ~500 lines each); re-exported here.
from foreman_inator.worker_promote import (  # noqa: E402
    PromoteResult,
    promote,
    status_lines,
    summary,
)
from foreman_inator.worker_replay import replay_command, replay_report  # noqa: E402

__all__ = [
    "ExperimentError",
    "PromoteResult",
    "RunEnv",
    "build_deps",
    "champion_variant",
    "ensure_shadow",
    "install_cancel_handlers",
    "keep_rules",
    "load_rules",
    "make_proxy",
    "mirror_for",
    "promote",
    "read_rules",
    "replay_command",
    "replay_report",
    "resolve_challenger",
    "run_all",
    "run_command",
    "run_experiment",
    "run_lock",
    "run_seeded_experiment",
    "settings_from",
    "status_lines",
    "summary",
    "split_lines",
    "start_split",
    "stop_split",
    "sync_replay_mirror",
    "unfinished_replays",
]
