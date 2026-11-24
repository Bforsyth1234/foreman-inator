"""Sandbox for model-written code (todo #40).

Profile-text, policy, env and config tests run everywhere. Enforcement tests run a probe
script inside a real ``sandbox-exec`` on macOS only, with a fake home and a fake
foreman-inator home under ``tmp_path``; they never read the user's real files (a denial
is checked by errno, and nothing is printed from outside the worktree).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from fakes.opencode import FakeOpenCode, Script, bash, say
from foreman_inator import config as config_mod
from foreman_inator.config import ConfigError, ModelEndpoint
from foreman_inator.review import checks
from foreman_inator.runner import sandbox as sb
from foreman_inator.runner import verify
from foreman_inator.runner.sandbox import Policy, SandboxUnavailable, SeatbeltBackend
from foreman_inator.runner.worktree import GitIdentity, WorktreeManager

FIXTURES = Path(__file__).parent / "fixtures" / "repos"
FAKES = Path(__file__).parent / "fakes"
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Seed",
    "GIT_AUTHOR_EMAIL": "seed@example.com",
    "GIT_COMMITTER_NAME": "Seed",
    "GIT_COMMITTER_EMAIL": "seed@example.com",
}

macos = pytest.mark.skipif(
    not SeatbeltBackend().available(), reason="sandbox-exec enforcement tests run on macOS only"
)


# --------------------------------------------------------------------------- helpers


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, env={**os.environ, **GIT_ENV}
    )


class Attempt:
    """A fake foreman-inator home with secrets, a mirror, this attempt's worktree and a
    second attempt's worktree, and a fake user home with a key."""

    def __init__(self, tmp: Path, fixture: str = "python"):
        self.tmp = tmp
        self.home = tmp / "home"
        (self.home / ".ssh").mkdir(parents=True)
        (self.home / ".ssh" / "id_ed25519").write_text("FAKE-SSH-KEY\n")
        self.fhome = tmp / "fhome"
        self.fhome.mkdir()
        for name in ("app.pem", "config.toml", "foreman-inator.db"):
            (self.fhome / name).write_text(f"FAKE {name}\n")
        for d in ("traces", "recordings"):
            (self.fhome / d).mkdir()
            (self.fhome / d / "x.jsonl").write_text("FAKE\n")
        src = tmp / "src"
        shutil.copytree(FIXTURES / fixture, src)
        _git("init", "-q", "-b", "trunk", cwd=src)
        _git("add", "-A", cwd=src)
        _git("commit", "-qm", "seed", cwd=src)
        origin = tmp / "origin.git"
        _git("clone", "-q", "--bare", str(src), str(origin), cwd=tmp)
        mgr = WorktreeManager(self.fhome / "worktrees", identity=GitIdentity())
        self.wt = mgr.create("o/r", "trunk", 1, 1, clone_url=str(origin))
        self.other = mgr.create("o/r", "trunk", 2, 1, clone_url=str(origin))
        (self.other.path / "other-secret.txt").write_text("OTHER ATTEMPT\n")
        self.mirror = self.fhome / "worktrees" / "o" / "r" / ".mirror"
        self.scratch = tmp / "scratch"
        self.scratch.mkdir()

    def policy(self, **kw) -> Policy:
        kw.setdefault("home", self.home)
        kw.setdefault("foreman_home", self.fhome)
        return Policy.create(self.wt.path, self.scratch, **kw)


PROBE = r"""
import errno, json, os, socket, sys
results = {}
def attempt(name, fn):
    try:
        fn()
        results[name] = "ok"
    except OSError as e:
        results[name] = errno.errorcode.get(e.errno, str(e.errno))
def read(p):
    with open(p, "rb") as f:
        f.read(1)
def write(p):
    with open(p, "w") as f:
        f.write("x")
def connect(host, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3)
    try:
        s.connect((host, port))
    finally:
        s.close()
spec = json.loads(sys.argv[1])
for name, (op, *args) in spec.items():
    fn = {"read": read, "write": write, "connect": connect, "listdir": os.listdir}[op]
    attempt(name, lambda fn=fn, args=args: fn(*args))
print(json.dumps(results))
"""


def run_probe(att: Attempt, policy: Policy, spec: dict) -> dict:
    probe = att.wt.path / "probe.py"
    probe.write_text(PROBE)
    pol = policy.for_command(sys.executable)
    argv = sb.wrap([sys.executable, str(probe), json.dumps(spec)], pol)
    out = subprocess.run(
        argv, cwd=att.wt.path, env=sb.apply_env(os.environ, pol), capture_output=True, text=True,
        timeout=60,
    )  # fmt: skip
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


class Listener:
    """A loopback TCP server accepting and closing connections (the 'proxy')."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.accepted = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            c.close()

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def att(tmp_path: Path) -> Attempt:
    return Attempt(tmp_path)


# ------------------------------------------------------------- profile text (everywhere)


def _plain_policy(tmp: Path, **kw) -> Policy:
    return Policy(
        worktree=tmp / "wt",
        scratch=tmp / "scratch",
        home=tmp / "home",
        foreman_home=tmp / "home" / ".foreman-inator",
        **kw,
    )


def test_profile_denies_by_default_and_writes_only_own_paths(tmp_path):
    text = sb.render_profile(_plain_policy(tmp_path))
    lines = text.splitlines()
    assert lines[0] == "(version 1)"
    assert "(deny default)" in lines
    writes = [line for line in lines if line.startswith("(allow file-write*")]
    assert writes[0] == (
        f'(allow file-write* (subpath "{tmp_path / "wt"}") (subpath "{tmp_path / "scratch"}"))'
    )
    assert not any(line in ("(allow file-write*)", "(allow default)") for line in lines)


def test_profile_rule_order_broad_to_narrow(tmp_path):
    home = tmp_path / "home"
    pol = _plain_policy(tmp_path, toolchains=(home / ".nvm" / "versions",))
    text = sb.render_profile(pol)
    deny_roots = text.index('(deny file-read* (subpath "/Users")')
    tools = text.index(f'(allow file-read* (subpath "{home / ".nvm" / "versions"}")')
    sensitive = text.index(f'(subpath "{home / ".ssh"}")')
    own = text.index(f'(allow file-read* (subpath "{tmp_path / "wt"}")')
    assert text.index("(allow file-read*)") < deny_roots < tools < sensitive < own


def test_profile_never_emits_an_unfiltered_allow(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "MACH_SERVICES", ())
    text = sb.render_profile(_plain_policy(tmp_path))
    assert "(allow mach-lookup" not in text  # an empty filter list would allow all of them
    for line in text.splitlines():
        if line.startswith("(allow") and line != "(allow file-read*)":
            assert line not in {
                f"(allow {op})" for op in ("network*", "mach-lookup", "file-write*")
            }


def test_network_none_has_no_network_rule(tmp_path):
    assert "network" not in sb.render_profile(_plain_policy(tmp_path)).split(";; network")[1]


def test_writer_network_is_the_proxy_port_only(tmp_path):
    text = sb.render_profile(_plain_policy(tmp_path, proxy_port=48123, listen_loopback=True))
    net = text.split(";; network")[1]
    assert '(allow network-outbound (remote ip "localhost:48123"))' in net
    assert '(allow network-bind (local ip "localhost:*"))' in net
    assert "(allow network*)" not in net
    assert net.count("network-outbound") == 1


def test_network_allow_opens_everything(tmp_path):
    net = sb.render_profile(_plain_policy(tmp_path, network="allow")).split(";; network")[1]
    assert "(allow network*)" in net


def test_git_steering_files_are_never_writable(tmp_path):
    git = sb.GitAccess(protected=(tmp_path / "wt" / ".git", tmp_path / "m" / "commondir"))
    text = sb.render_profile(_plain_policy(tmp_path, git=git))
    deny = [line for line in text.splitlines() if line.startswith("(deny file-write*")]
    assert deny and f'(literal "{tmp_path / "wt" / ".git"}")' in deny[-1]
    assert text.index(deny[-1]) > text.index("(allow file-write*")


def test_paths_are_quoted(tmp_path):
    odd = tmp_path / 'we"ird\\dir'
    text = sb.render_profile(_plain_policy(tmp_path, reads=(odd,)))
    assert '(subpath "' + str(odd).replace("\\", "\\\\").replace('"', '\\"') + '")' in text


def test_policy_validates(tmp_path):
    with pytest.raises(sb.SandboxError):
        _plain_policy(tmp_path, mode="maybe")
    with pytest.raises(sb.SandboxError):
        _plain_policy(tmp_path, network="some")
    with pytest.raises(sb.SandboxError):
        _plain_policy(tmp_path, proxy_port=70000)


# -------------------------------------------------------- wrap, fail closed (everywhere)


def test_off_and_none_return_argv_unchanged(tmp_path):
    assert sb.wrap(["make", "test"], None) == ["make", "test"]
    assert sb.wrap(["make", "test"], _plain_policy(tmp_path, mode="off")) == ["make", "test"]
    assert sb.child_env(_plain_policy(tmp_path, mode="off")) == {}


def test_on_without_sandbox_exec_fails_closed(tmp_path):
    old = sb.set_backend(SeatbeltBackend(platform="linux"))
    try:
        with pytest.raises(SandboxUnavailable, match='sandbox = "off"'):
            sb.wrap(["make", "test"], _plain_policy(tmp_path))
        with pytest.raises(SandboxUnavailable):
            sb.ensure_available(_plain_policy(tmp_path))
        sb.ensure_available(_plain_policy(tmp_path, mode="off"))  # off: nothing to check
        sb.ensure_available(None)
    finally:
        sb.set_backend(old)


def test_missing_sandbox_exec_binary_fails_closed(tmp_path):
    backend = SeatbeltBackend(sandbox_exec=str(tmp_path / "nope"), platform="darwin")
    with pytest.raises(SandboxUnavailable):
        backend.wrap(["true"], _plain_policy(tmp_path))


def test_verify_run_fails_closed_before_running_anything(tmp_path):
    marker = tmp_path / "ran"
    cmd = verify.VerifyCommand.of(["/bin/sh", "-c", f"touch {marker}"], "override")
    (tmp_path / "wt").mkdir()
    old = sb.set_backend(SeatbeltBackend(platform="linux"))
    try:
        with pytest.raises(SandboxUnavailable):
            verify.run(cmd, tmp_path / "wt", timeout=10, output_path=tmp_path / "o.log",
                       sandbox=_plain_policy(tmp_path))  # fmt: skip
    finally:
        sb.set_backend(old)
    assert not marker.exists()


def test_wrap_shape(tmp_path):
    backend = SeatbeltBackend(sandbox_exec="/bin/echo", platform="darwin")
    argv = backend.wrap(["npm", "test"], _plain_policy(tmp_path))
    assert argv[0] == "/bin/echo" and argv[1] == "-p" and argv[3:] == ["--", "npm", "test"]
    assert argv[2] == sb.render_profile(_plain_policy(tmp_path))


def test_child_env_points_home_and_caches_into_scratch(tmp_path):
    pol = _plain_policy(tmp_path, shims=(("git", Path("/usr/bin/true")),))
    env = sb.child_env(pol, "/usr/bin:/bin")
    for key in ("HOME", "TMPDIR", "XDG_CACHE_HOME", "npm_config_cache", "UV_CACHE_DIR"):
        assert Path(env[key]).is_relative_to(tmp_path / "scratch"), key
        assert Path(env[key]).is_dir()
    assert env["PATH"] == f"{tmp_path / 'scratch' / 'bin'}:/usr/bin:/bin"
    assert (tmp_path / "scratch" / "bin" / "git").resolve() == Path("/usr/bin/true").resolve()
    zprofile = (tmp_path / "scratch" / "home" / ".zprofile").read_text()
    assert str(tmp_path / "scratch" / "bin") in zprofile  # login shells keep the links first
    assert env["UV_OFFLINE"] == "1"
    assert "UV_OFFLINE" not in sb.child_env(_plain_policy(tmp_path, network="allow"), "")


def test_swift_wrapper_disables_the_nested_sandbox(tmp_path):
    pol = _plain_policy(tmp_path, shims=(("swift", Path("/opt/x/swift")),))
    sb.child_env(pol, "")
    text = (tmp_path / "scratch" / "bin" / "swift").read_text()
    assert "--disable-sandbox" in text and "build|test|run|package" in text
    assert os.access(tmp_path / "scratch" / "bin" / "swift", os.X_OK)


def test_active_policy_is_scoped(tmp_path):
    pol = _plain_policy(tmp_path)
    assert sb.current() is None
    with sb.active(pol):
        assert sb.current() is pol
    assert sb.current() is None


def test_describe_for_status_and_pr():
    assert "OFF" in sb.describe("off")
    assert sb.describe("on", {"a/b": "allow", "c/d": "none"}).endswith("network open for a/b.")


# --------------------------------------------------------------------------- discovery (everywhere)


def test_git_access_lists_exactly_this_worktrees_paths(att):
    g = sb.git_access(att.wt.path)
    m = att.mirror.resolve()
    gitdir = (m / "worktrees" / att.wt.path.name).resolve()
    assert g.reads == (m,)
    assert g.writes == (gitdir, m / "objects")
    assert m / "refs/heads/fman/issue-1/a1" in g.write_files
    assert m / "logs/refs/heads/fman/issue-1/a1" in g.write_files
    assert not any("issue-2" in str(p) for p in g.write_files)  # not the other attempt's ref
    assert att.wt.path.resolve() / ".git" in g.protected
    assert gitdir / "commondir" in g.protected and gitdir / "fman-base" in g.protected


def test_git_access_plain_dir_needs_nothing(tmp_path):
    assert sb.git_access(tmp_path) == sb.GitAccess()


def test_toolchain_reads_never_open_broad_or_sensitive_dirs(tmp_path):
    home = tmp_path / "home"
    nvm_bin = home / ".nvm/versions/node/v24/bin"
    local_bin = home / ".local/bin"
    venv = home / "proj/.venv"
    tool_venv = home / ".local/share/uv/tools/ruff"
    for d in (nvm_bin, local_bin, venv / "bin", tool_venv / "bin", home / ".config/x/bin"):
        d.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr\n")
    (tool_venv / "pyvenv.cfg").write_text("home = /usr\n")
    (tool_venv / "bin/ruff").write_text("")
    (local_bin / "ruff").symlink_to(tool_venv / "bin/ruff")
    (venv / "bin/python").symlink_to("/bin/sh")
    path = os.pathsep.join(str(p) for p in (nvm_bin, local_bin, home / ".config/x/bin", "/usr/bin"))
    found = {p.relative_to(home.resolve()).as_posix() for p in
             sb.toolchain_reads(path, home, argv0=str(venv / "bin/python"))
             if p.is_relative_to(home.resolve())}  # fmt: skip
    assert ".nvm/versions/node/v24" in found  # known toolchain layout: the prefix
    assert ".local/bin" in found and ".local" not in found  # bin dir only
    assert ".local/share/uv/tools/ruff" in found  # a venv behind a ~/.local/bin link
    assert "proj/.venv" in found  # argv0's venv
    assert not any(p.startswith(".config") for p in found)  # sensitive, even on PATH


# --------------------------------------------------------------------------- config (everywhere)


def test_config_sandbox_defaults_on_and_round_trips():
    cfg = config_mod.Config()
    assert cfg.sandbox == "on"
    assert cfg.sandbox_network_for("you/project") == "none"
    cfg = config_mod.from_dict({"sandbox": "off", "sandbox_network": {"You/Project": "allow"}})
    assert cfg.sandbox == "off" and cfg.sandbox_network_for("you/project") == "allow"
    again = config_mod.from_dict(config_mod.to_dict(cfg))
    assert again == cfg and hash(again) == hash(cfg)


@pytest.mark.parametrize(
    "data",
    [{"sandbox": "maybe"}, {"sandbox": True}, {"sandbox_network": {"x/y": "open"}},
     {"sandbox_network": {"notarepo": "allow"}}, {"sandbox_network": "allow"}],
)  # fmt: skip
def test_config_sandbox_rejects_bad_values(data):
    with pytest.raises(ConfigError):
        config_mod.from_dict(data)


# --------------------------------------------------------------------------- enforcement (macOS)


@macos
def test_reads_outside_the_worktree_fail(att):
    res = run_probe(
        att,
        att.policy(),
        {
            "ssh_key": ["read", str(att.home / ".ssh" / "id_ed25519")],
            "app_pem": ["read", str(att.fhome / "app.pem")],
            "config": ["read", str(att.fhome / "config.toml")],
            "db": ["read", str(att.fhome / "foreman-inator.db")],
            "traces": ["read", str(att.fhome / "traces" / "x.jsonl")],
            "recordings": ["listdir", str(att.fhome / "recordings")],
            "other_worktree": ["read", str(att.other.path / "other-secret.txt")],
            "real_home": ["listdir", str(Path.home())],
            "own_file": ["read", str(att.wt.path / "pyproject.toml")],
            "system": ["read", "/etc/hosts"],
        },
    )
    denied = ("ssh_key", "app_pem", "config", "db", "traces", "recordings", "other_worktree",
              "real_home")  # fmt: skip
    assert {k: res[k] for k in denied} == dict.fromkeys(denied, "EPERM")
    assert res["own_file"] == "ok" and res["system"] == "ok"


@macos
def test_writes_outside_the_worktree_fail(att):
    outside = att.tmp / "outside.txt"
    res = run_probe(
        att,
        att.policy(),
        {
            "outside": ["write", str(outside)],
            "fhome": ["write", str(att.fhome / "new.txt")],
            "home": ["write", str(att.home / "new.txt")],
            "other_worktree": ["write", str(att.other.path / "x.txt")],
            "mirror_config": ["write", str(att.mirror / "config")],
            "other_ref": ["write", str(att.mirror / "refs" / "heads" / "fman" / "issue-2" / "a1")],
            "dot_git": ["write", str(att.wt.path / ".git")],
            "inside": ["write", str(att.wt.path / "new.txt")],
            "scratch": ["write", str(att.scratch / "tmp.txt")],
        },
    )
    blocked = ("outside", "fhome", "home", "other_worktree", "mirror_config", "other_ref",
               "dot_git")  # fmt: skip
    assert {k: res[k] for k in blocked} == dict.fromkeys(blocked, "EPERM")
    assert not outside.exists()
    assert res["inside"] == "ok" and (att.wt.path / "new.txt").read_text() == "x"
    assert res["scratch"] == "ok"


@macos
def test_network_reaches_the_proxy_port_only(att):
    proxy, other = Listener(), Listener()
    try:
        res = run_probe(
            att,
            att.policy(proxy_port=proxy.port),
            {
                "proxy": ["connect", "127.0.0.1", proxy.port],
                "other_loopback": ["connect", "127.0.0.1", other.port],
                # TEST-NET-1: never routed, so only the sandbox can make this EPERM
                "outside": ["connect", "192.0.2.1", 9],
            },
        )
        assert res == {"proxy": "ok", "other_loopback": "EPERM", "outside": "EPERM"}
        assert other.accepted == 0
        none = run_probe(att, att.policy(), {"proxy": ["connect", "127.0.0.1", proxy.port]})
        assert none == {"proxy": "EPERM"}  # verify and checks: no network at all
    finally:
        proxy.close()
        other.close()


@macos
def test_network_allow_opens_outbound(att):
    other = Listener()
    try:
        res = run_probe(
            att, att.policy(network="allow"), {"loopback": ["connect", "127.0.0.1", other.port]}
        )
        assert res == {"loopback": "ok"}
    finally:
        other.close()


@macos
def test_git_status_and_commit_work_inside(att):
    pol = att.policy()
    env = sb.apply_env(os.environ, pol)
    (att.wt.path / "feature.txt").write_text("new\n")

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(sb.wrap(["git", *args], pol), cwd=att.wt.path, env=env,
                              capture_output=True, text=True, timeout=60)  # fmt: skip

    st = git("status", "--short")
    assert st.returncode == 0 and "feature.txt" in st.stdout, st.stderr
    assert git("add", "-A").returncode == 0
    c = git("-c", "user.name=A", "-c", "user.email=a@example.com", "commit", "-qm", "inside")
    assert c.returncode == 0, c.stderr
    assert "couldn't create cache file" not in c.stderr  # the xcrun shim is bypassed
    head = subprocess.run(
        ["git", "log", "-1", "--format=%s", "fman/issue-1/a1"],
        cwd=att.mirror, capture_output=True, text=True, env={**os.environ, **GIT_ENV},
    )  # fmt: skip
    assert head.stdout.strip() == "inside"


@macos
def test_a_sandboxed_process_cannot_sandbox_itself_again(att):
    pol = att.policy()
    out = subprocess.run(
        sb.wrap([sb.SANDBOX_EXEC, "-p", "(version 1)(allow default)", "/bin/echo", "x"], pol),
        cwd=att.wt.path, env=sb.apply_env(os.environ, pol), capture_output=True, text=True,
    )  # fmt: skip
    assert out.returncode == 71 and "Operation not permitted" in out.stderr


@macos
@pytest.mark.timeout(600)  # a cold `swift build` on a small, busy CI runner can pass 60 s
@pytest.mark.parametrize("fixture", ["python", "node", "swift", "makefile"])
def test_fixture_repos_pass_verify_in_the_sandbox(tmp_path, fixture):
    att = Attempt(tmp_path, fixture)
    cmd = verify.detect(att.wt.path, python=sys.executable)
    assert cmd is not None
    if shutil.which(cmd.argv[0]) is None:
        pytest.skip(f"{cmd.argv[0]} not on PATH")
    res = verify.run(cmd, att.wt.path, timeout=600, output_path=tmp_path / "verify.log",
                     sandbox=att.policy())  # fmt: skip
    log = res.output_path.read_text()
    assert res.passed, log[-3000:]
    assert "Operation not permitted" not in log


@macos
def test_failing_verify_still_fails_in_the_sandbox(tmp_path):
    att = Attempt(tmp_path, "python_failing")
    cmd = verify.detect(att.wt.path, python=sys.executable)
    res = verify.run(cmd, att.wt.path, timeout=120, output_path=tmp_path / "v.log",
                     sandbox=att.policy())  # fmt: skip
    assert not res.passed and res.tests_failed == 1


@macos
def test_verify_missing_executable_is_127_in_the_sandbox(att):
    cmd = verify.VerifyCommand.of(["fman-no-such-tool"], "override")
    res = verify.run(cmd, att.wt.path, timeout=10, output_path=att.tmp / "m.log",
                     sandbox=att.policy())  # fmt: skip
    assert res.exit_code == 127 and not res.passed


@macos
def test_verify_command_cannot_read_secrets(att):
    cmd = verify.VerifyCommand.from_override(
        f"cat {att.fhome / 'app.pem'}; cat {att.home / '.ssh' / 'id_ed25519'}; echo done"
    )
    res = verify.run(cmd, att.wt.path, timeout=30, output_path=att.tmp / "s.log",
                     sandbox=att.policy())  # fmt: skip
    log = res.output_path.read_text()
    assert "FAKE" not in log and log.count("Operation not permitted") == 2


@macos
def test_reviewer_checks_run_under_the_active_policy(att):
    """The worker wraps review_change in sandbox.active(policy); the checks bind it to
    each scratch worktree (here: the attempt's own) and its work dir."""
    leak = att.wt.path / "tests" / "test_leak.py"
    leak.write_text(
        "def test_leak():\n"
        f"    open({str(att.fhome / 'app.pem')!r}).read()\n"
    )  # fmt: skip
    command = verify.detect(att.wt.path, python=sys.executable)
    with sb.active(att.policy()):
        res = checks.check_tests(att.wt.path, command, timeout=120, output_path=att.tmp / "c.log")
    assert res.status == "failed" and "PermissionError" in (att.tmp / "c.log").read_text()
    leak.unlink()
    with sb.active(att.policy()):
        ok = checks.check_tests(att.wt.path, command, timeout=120, output_path=att.tmp / "d.log")
    assert ok.status == "passed"
    unsandboxed = checks.check_tests(
        att.wt.path, command, timeout=120, output_path=att.tmp / "e.log"
    )
    assert unsandboxed.status == "passed"  # no policy anywhere: unchanged behaviour


@macos
def test_opencode_server_and_its_bash_tool_run_inside(att):
    """The driver's fake `opencode` binary under the sandbox: it still starts, listens
    on loopback and answers the driver; its bash tool (a child) is confined."""
    proxy = Listener()
    fake = FakeOpenCode(att.tmp / "oc", Script([
        bash(f"cat {att.fhome / 'app.pem'}; echo hi > {att.tmp / 'oc-outside.txt'}; "
             "echo inside > from_bash.txt"),
        say("done"),
    ]))  # fmt: skip
    endpoint = ModelEndpoint("m", 1, "local", "m:1", "http://127.0.0.1:11434/v1", "qwen")
    policy = att.policy(reads=[FAKES], writes=[fake.root], argv0=sys.executable)
    try:
        result = fake.driver(timeout=30).run(
            attempt_id=1,
            worktree=att.wt,
            prompt="x",
            endpoint=endpoint,
            proxy_base_url=f"http://127.0.0.1:{proxy.port}/a/1/v1",
            sandbox=policy,
        )
    finally:
        proxy.close()
    assert result.finished, result.error
    assert (att.wt.path / "from_bash.txt").read_text() == "inside\n"
    assert not (att.tmp / "oc-outside.txt").exists()
    launch = fake.launches()[-1]
    assert "HOME" in launch["env_names"] and "TMPDIR" in launch["env_names"]
    assert launch["env"]["XDG_DATA_HOME"].startswith(str((fake.root / "state").resolve()))


@macos
def test_opencode_with_unavailable_sandbox_raises_and_starts_nothing(att):
    fake = FakeOpenCode(att.tmp / "oc", Script([say("done")]))
    endpoint = ModelEndpoint("m", 1, "local", "m:1", "http://127.0.0.1:11434/v1", "qwen")
    old = sb.set_backend(SeatbeltBackend(platform="linux"))
    try:
        with pytest.raises(SandboxUnavailable):
            fake.driver(timeout=10).run(
                attempt_id=1, worktree=att.wt, prompt="x", endpoint=endpoint,
                proxy_base_url="http://127.0.0.1:1/a/1/v1", sandbox=att.policy(),
            )  # fmt: skip
    finally:
        sb.set_backend(old)
    assert fake.launches() == []
