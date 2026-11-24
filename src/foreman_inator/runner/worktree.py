"""Worktree manager: one clean git worktree per attempt, on an `fman/` branch.

On-disk layout, under the worktrees root (normally ``~/.foreman-inator/worktrees``)::

    <root>/<owner>/<repo>/.mirror                 bare clone; remote refs in refs/remotes/origin/*
    <root>/<owner>/<repo>/issue-<n>-a<attempt>    worktree on branch fman/issue-<n>/a<attempt>
    <root>/<owner>/<repo>/scratch-<name>          scratch worktree at any commit, never pushes

Invariant 7a lives here: the manager refuses to create, commit to or push any branch whose
name does not start with ``fman/``, and force-push is only ever sent to an ``fman/`` branch.

Every git call runs with global and system git config ignored, so a user's or CI runner's
settings (signing, hooks path, credential helpers, default branch) never change behaviour.
Commits use the identity passed in, never a global one.

The push/fetch token is handed to git through ``GIT_CONFIG_COUNT``/``GIT_CONFIG_KEY_0``/
``GIT_CONFIG_VALUE_0`` environment variables as an ``http.extraHeader``: it is never part of
the argv, never written to ``.git/config`` or the remote URL, and it is scrubbed from any
error message.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

BRANCH_PREFIX = "fman/"
MIRROR_DIRNAME = ".mirror"
BASE_FILE = "fman-base"  # stored in the worktree's private git dir, never in the tree
NETWORK_TIMEOUT = 600.0
LOCAL_TIMEOUT = 120.0

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
# Inherited variables that would point git at another repo or inject config.
_SCRUBBED_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_CONFIG",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
)


class WorktreeError(RuntimeError):
    """A git operation failed. The message never contains a token."""


class BranchRefused(WorktreeError):
    """The branch is not one foreman-inator may touch (invariant 7a)."""


class PushRefused(WorktreeError):
    """This worktree may not push (scratch worktrees, or a non-`fman/` target)."""


@dataclass(frozen=True)
class GitIdentity:
    """Author and committer for commits the manager makes (the App's bot in production)."""

    name: str = "foreman-inator[bot]"
    email: str = "foreman-inator[bot]@users.noreply.github.com"


DEFAULT_IDENTITY = GitIdentity()


@dataclass(frozen=True)
class DiffStats:
    """Size of a worktree's change against its base commit."""

    files: tuple[str, ...]
    insertions: int
    deletions: int

    @property
    def diff_files(self) -> int:
        return len(self.files)

    @property
    def diff_lines(self) -> int:
        return self.insertions + self.deletions


def branch_name(issue: int, attempt: int) -> str:
    """The branch for one attempt: ``fman/issue-<n>/a<attempt>``."""
    if issue < 1 or attempt < 1:
        raise ValueError("issue and attempt must be positive")
    return f"{BRANCH_PREFIX}issue-{issue}/a{attempt}"


def check_branch(branch: str) -> str:
    """Return ``branch`` if foreman-inator may own it, else raise :class:`BranchRefused`."""
    if not isinstance(branch, str) or not branch.startswith(BRANCH_PREFIX):
        raise BranchRefused(f"refusing branch {branch!r}: worker branches must start with 'fman/'")
    if branch == BRANCH_PREFIX:
        raise BranchRefused(f"refusing branch {branch!r}: not a valid fman/ branch name")
    ok = subprocess.run(
        ["git", "check-ref-format", f"refs/heads/{branch}"],
        env=_base_env(),
        capture_output=True,
        timeout=LOCAL_TIMEOUT,
    )
    if ok.returncode != 0:
        raise BranchRefused(f"refusing branch {branch!r}: not a valid git branch name")
    return branch


# --------------------------------------------------------------------------- git plumbing


def _base_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED_ENV}
    for k in list(env):
        if k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            del env[k]
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        LC_ALL="C",
    )
    if extra:
        env.update(extra)
    return env


def _auth_header(token: str) -> str:
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return f"Authorization: Basic {basic}"


def _secrets(token: str | None) -> list[str]:
    if not token:
        return []
    return [token, _auth_header(token).split()[-1]]


def _redact(text: str, token: str | None) -> str:
    for secret in _secrets(token):
        text = text.replace(secret, "***")
    return text


def _git(
    args: Sequence[str],
    *,
    cwd: Path,
    token: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = LOCAL_TIMEOUT,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    extra = dict(env or {})
    if token:
        extra.update(
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="http.extraHeader",
            GIT_CONFIG_VALUE_0=_auth_header(token),
        )
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=_base_env(extra),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise WorktreeError(f"git {args[0]} timed out after {timeout:.0f}s") from None
    except OSError as exc:
        raise WorktreeError(f"could not run git: {exc}") from None
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        msg = f"git {' '.join(args[:2])} failed ({proc.returncode}): {detail}"
        raise WorktreeError(_redact(msg, token))
    return proc


def _validate_clone_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme in ("http", "https") and (parts.username or parts.password):
        raise WorktreeError("clone URL must not embed credentials; pass the token separately")


# --------------------------------------------------------------------------- worktree


@dataclass
class Worktree:
    """One checked-out worktree. ``branch`` is None for a detached scratch worktree."""

    mirror: RepoMirror
    path: Path
    branch: str | None
    base_sha: str
    can_push: bool = True
    _removed: bool = field(default=False, repr=False)

    # -- inspection ---------------------------------------------------------

    def head_sha(self) -> str:
        return _git(["rev-parse", "HEAD"], cwd=self.path).stdout.strip()

    def _diff(self, args: Sequence[str]) -> str:
        """``git diff --cached <base>`` over a throwaway index holding the whole tree.

        Covers committed, uncommitted and untracked (non-ignored) changes without touching
        the worktree's real index.
        """
        git_index = Path(_git(["rev-parse", "--git-path", "index"], cwd=self.path).stdout.strip())
        if not git_index.is_absolute():
            git_index = self.path / git_index
        with tempfile.TemporaryDirectory(prefix="fman-index-") as tmp:
            index = Path(tmp) / "index"
            if git_index.exists():
                shutil.copyfile(git_index, index)
            env = {"GIT_INDEX_FILE": str(index)}
            _git(["add", "-A"], cwd=self.path, env=env)
            return _git(
                ["diff", "--cached", "--no-renames", "--no-color", *args, self.base_sha],
                cwd=self.path,
                env=env,
            ).stdout

    def changed_files(self) -> list[str]:
        """Paths changed against the base commit (renames listed as delete + add)."""
        return sorted(p for p in self._diff(["--name-only", "-z"]).split("\0") if p)

    def diff_stats(self) -> DiffStats:
        files: list[str] = []
        ins = dels = 0
        for rec in self._diff(["--numstat", "-z"]).split("\0"):
            if not rec:
                continue
            added, removed, path = rec.split("\t", 2)
            files.append(path)
            ins += int(added) if added.isdigit() else 0  # "-" for binary files
            dels += int(removed) if removed.isdigit() else 0
        return DiffStats(files=tuple(sorted(files)), insertions=ins, deletions=dels)

    def diff_text(self) -> str:
        """The full unified diff against the base commit (binary files as stubs)."""
        return self._diff([])

    # -- writes -------------------------------------------------------------

    def _current_branch(self) -> str | None:
        proc = _git(["symbolic-ref", "-q", "--short", "HEAD"], cwd=self.path, check=False)
        return proc.stdout.strip() or None

    def commit_all(self, message: str) -> str | None:
        """Stage everything and commit as the mirror's identity. Returns the SHA, or None
        when there is nothing to commit. Refuses unless HEAD is this worktree's fman/ branch."""
        current = self._current_branch()
        if current is None or current != self.branch:
            raise BranchRefused(
                f"refusing to commit: HEAD is {current or 'detached'!r}, expected {self.branch!r}"
            )
        check_branch(current)
        _git(["add", "-A"], cwd=self.path)
        if _git(["diff", "--cached", "--quiet"], cwd=self.path, check=False).returncode == 0:
            return None
        ident = self.mirror.identity
        env = {
            "GIT_AUTHOR_NAME": ident.name,
            "GIT_AUTHOR_EMAIL": ident.email,
            "GIT_COMMITTER_NAME": ident.name,
            "GIT_COMMITTER_EMAIL": ident.email,
        }
        _git(
            ["commit", "--no-verify", "--no-gpg-sign", "-q", "-m", message],
            cwd=self.path,
            env=env,
        )
        return self.head_sha()

    def push(
        self, token: str | None, *, remote_branch: str | None = None, force: bool = False
    ) -> None:
        """Push this worktree's branch to ``remote_branch`` (default: the same name).

        Both names must start with ``fman/``. ``force`` is how a later attempt replaces the
        PR's branch (#12); it is only ever applied to an ``fman/`` target.
        """
        if not self.can_push or self.branch is None:
            raise PushRefused(f"worktree {self.path.name} is a scratch worktree and never pushes")
        src = check_branch(self.branch)
        dst = check_branch(remote_branch or self.branch)
        refspec = f"{'+' if force else ''}refs/heads/{src}:refs/heads/{dst}"
        _git(
            ["push", "--porcelain", "--no-verify", "origin", refspec],
            cwd=self.path,
            token=token,
            timeout=NETWORK_TIMEOUT,
        )

    def remove(self, *, delete_branch: bool = True) -> None:
        """Remove the worktree (and its local branch). Safe to call twice or after a crash."""
        self.mirror._remove_path(self.path)
        if delete_branch and self.branch and self.branch.startswith(BRANCH_PREFIX):
            self.mirror._delete_local_branch(self.branch)
        self._removed = True


# --------------------------------------------------------------------------- mirror


class RepoMirror:
    """The bare mirror for one GitHub repo plus the worktrees beside it."""

    def __init__(
        self,
        root: Path | str,
        repo: str,
        clone_url: str,
        *,
        identity: GitIdentity = DEFAULT_IDENTITY,
    ) -> None:
        if not _REPO_RE.match(repo) or any(p in (".", "..") for p in repo.split("/")):
            raise ValueError(f"repo must look like 'owner/name', got {repo!r}")
        _validate_clone_url(clone_url)
        self.root = Path(root)
        self.repo = repo
        self.clone_url = clone_url
        self.identity = identity
        self.repo_dir = self.root / repo
        self.mirror_path = self.repo_dir / MIRROR_DIRNAME

    # -- mirror maintenance ---------------------------------------------------

    def _mirror_ok(self) -> bool:
        if not (self.mirror_path / "HEAD").exists():
            return False
        proc = _git(["rev-parse", "--is-bare-repository"], cwd=self.mirror_path, check=False)
        return proc.returncode == 0 and proc.stdout.strip() == "true"

    def ensure(self, token: str | None = None) -> Path:
        """Create the mirror if missing (or half-made by a crash), then fetch. Idempotent."""
        if not self._mirror_ok():
            if self.mirror_path.exists():
                shutil.rmtree(self.mirror_path)
            self.mirror_path.mkdir(parents=True)
            m = self.mirror_path
            _git(["init", "-q", "--bare"], cwd=m)
            _git(["config", "remote.origin.url", self.clone_url], cwd=m)
            _git(["config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"], cwd=m)
            _git(["config", "gc.auto", "0"], cwd=m)
        else:
            _git(["config", "remote.origin.url", self.clone_url], cwd=self.mirror_path)
        self.prune()
        self.fetch(token)
        return self.mirror_path

    def fetch(self, token: str | None = None) -> None:
        _git(
            ["fetch", "-q", "--prune", "--no-tags", "origin"],
            cwd=self.mirror_path,
            token=token,
            timeout=NETWORK_TIMEOUT,
        )

    def prune(self) -> None:
        """Forget worktrees whose directories are gone (e.g. after a crash)."""
        if self.mirror_path.exists():
            _git(["worktree", "prune"], cwd=self.mirror_path, check=False)

    def resolve(self, ref: str) -> str:
        """SHA of a commit: a remote branch name (``main``), ``origin/x``, or any commit-ish."""
        for cand in (f"refs/remotes/origin/{ref}", ref):
            proc = _git(
                ["rev-parse", "--verify", "-q", f"{cand}^{{commit}}"],
                cwd=self.mirror_path,
                check=False,
            )
            if proc.returncode == 0:
                return proc.stdout.strip()
        raise WorktreeError(f"unknown ref {ref!r} in {self.repo}")

    # -- worktrees --------------------------------------------------------------

    def attempt_path(self, issue: int, attempt: int) -> Path:
        return self.repo_dir / f"issue-{issue}-a{attempt}"

    def create(
        self,
        base_branch: str,
        issue: int,
        attempt: int,
        *,
        token: str | None = None,
        fetch: bool = True,
        start_point: str | None = None,
    ) -> Worktree:
        """A fresh worktree on ``fman/issue-<n>/a<attempt>`` from ``origin/<base_branch>``.

        ``start_point`` (a commit or ``origin/fman/...``) starts the branch from earlier
        work instead, while diffs stay measured against the base branch's commit. A stale
        worktree or branch for the same attempt is replaced.
        """
        branch = check_branch(branch_name(issue, attempt))
        if fetch or not self._mirror_ok():
            self.ensure(token)
        base_sha = self.resolve(base_branch)
        start_sha = self.resolve(start_point) if start_point else base_sha
        path = self.attempt_path(issue, attempt)
        self._remove_path(path)
        self._delete_local_branch(branch)
        _git(
            ["worktree", "add", "-q", "--no-track", "-b", branch, str(path), start_sha],
            cwd=self.mirror_path,
        )
        _write_base(path, base_sha)
        return Worktree(self, path, branch, base_sha, can_push=True)

    def scratch(self, commit: str, name: str, *, branch: str | None = None) -> Worktree:
        """A throwaway worktree at ``commit`` that can never push (reviewer revert check,
        replays). Detached by default; pass an ``fman/`` ``branch`` to allow local commits."""
        if not _NAME_RE.match(name):
            raise ValueError(f"bad scratch name {name!r}")
        if not self._mirror_ok():
            raise WorktreeError(f"no mirror for {self.repo}; call ensure() first")
        sha = self.resolve(commit)
        path = self.repo_dir / f"scratch-{name}"
        self._remove_path(path)
        if branch is not None:
            check_branch(branch)
            self._delete_local_branch(branch)
            args = ["worktree", "add", "-q", "--no-track", "-b", branch, str(path), sha]
        else:
            args = ["worktree", "add", "-q", "--detach", str(path), sha]
        _git(args, cwd=self.mirror_path)
        _write_base(path, sha)
        return Worktree(self, path, branch, sha, can_push=False)

    def open(self, issue: int, attempt: int) -> Worktree:
        """Reattach to an existing attempt worktree (after a worker restart)."""
        path = self.attempt_path(issue, attempt)
        base = _read_base(path)
        if base is None:
            raise WorktreeError(f"no worktree for issue {issue} attempt {attempt}")
        return Worktree(self, path, branch_name(issue, attempt), base, can_push=True)

    def worktrees(self) -> list[Path]:
        """Paths of the worktrees git knows about for this mirror (excluding the mirror)."""
        if not self._mirror_ok():
            return []
        out = _git(["worktree", "list", "--porcelain"], cwd=self.mirror_path).stdout
        paths = [Path(line[9:]) for line in out.splitlines() if line.startswith("worktree ")]
        mirror = self.mirror_path.resolve()
        return [p for p in paths if p.resolve() != mirror]

    def cleanup_issue(self, issue: int) -> None:
        """Remove every attempt worktree and local branch for an issue (after merge or drop).
        Remote branches are left alone."""
        for path in sorted(self.repo_dir.glob(f"issue-{issue}-a*")):
            self._remove_path(path)
        self.prune()
        for branch in self._local_branches(f"{BRANCH_PREFIX}issue-{issue}/"):
            self._delete_local_branch(branch)

    # -- internals --------------------------------------------------------------

    def _local_branches(self, prefix: str) -> list[str]:
        if not self._mirror_ok():
            return []
        out = _git(
            ["for-each-ref", "--format=%(refname:short)", f"refs/heads/{prefix}"],
            cwd=self.mirror_path,
        ).stdout
        return [b for b in out.splitlines() if b]

    def _remove_path(self, path: Path) -> None:
        path = Path(path)
        if path.parent.resolve() != self.repo_dir.resolve() or path.name == MIRROR_DIRNAME:
            raise WorktreeError(f"refusing to remove {path}: not a worktree of {self.repo}")
        if self._mirror_ok() and path.exists():
            _git(
                ["worktree", "remove", "--force", "--force", str(path)],
                cwd=self.mirror_path,
                check=False,
            )
        if path.exists():
            shutil.rmtree(path)
        self.prune()

    def _delete_local_branch(self, branch: str) -> None:
        if not branch.startswith(BRANCH_PREFIX) or not self._mirror_ok():
            return
        _git(["branch", "-D", "-q", branch], cwd=self.mirror_path, check=False)


def _private_git_dir(path: Path) -> Path | None:
    dotgit = path / ".git"
    if not dotgit.is_file():
        return None
    text = dotgit.read_text().strip()
    if not text.startswith("gitdir:"):
        return None
    gitdir = Path(text[len("gitdir:") :].strip())
    return gitdir if gitdir.is_absolute() else (path / gitdir).resolve()


def _write_base(path: Path, sha: str) -> None:
    gitdir = _private_git_dir(path)
    if gitdir is None:
        raise WorktreeError(f"{path} is not a linked worktree")
    (gitdir / BASE_FILE).write_text(sha + "\n")


def _read_base(path: Path) -> str | None:
    gitdir = _private_git_dir(path) if path.exists() else None
    f = gitdir / BASE_FILE if gitdir else None
    return f.read_text().strip() if f and f.exists() else None


# --------------------------------------------------------------------------- manager


class WorktreeManager:
    """Entry point for the worker: one :class:`RepoMirror` per repo under ``root``."""

    def __init__(self, root: Path | str, *, identity: GitIdentity = DEFAULT_IDENTITY) -> None:
        self.root = Path(root)
        self.identity = identity

    def mirror(self, repo: str, clone_url: str) -> RepoMirror:
        return RepoMirror(self.root, repo, clone_url, identity=self.identity)

    def create(
        self,
        repo: str,
        base_branch: str,
        ticket: int,
        attempt: int,
        *,
        clone_url: str,
        token: str | None = None,
        start_point: str | None = None,
    ) -> Worktree:
        """Fetch and create the worktree for attempt ``attempt`` of issue ``ticket``."""
        return self.mirror(repo, clone_url).create(
            base_branch, ticket, attempt, token=token, start_point=start_point
        )

    def repos(self) -> Iterator[str]:
        """``owner/name`` of every repo with a mirror on disk."""
        for m in sorted(self.root.glob(f"*/*/{MIRROR_DIRNAME}")):
            yield f"{m.parent.parent.name}/{m.parent.name}"
