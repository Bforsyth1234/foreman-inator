"""#42 (todo 65): ``fman rules show`` previews the writer's and reviewer's rules blocks.

The blocks must be exactly what the prompt builders put in the prompt, the per-source
lines must give each file's character count and ``CLIPPED at N chars`` for a cut, and the
files are read from the commit, never the working tree. No model is called.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from foreman_inator import prompts
from foreman_inator import rules_show as RS
from foreman_inator.review import reviewer as rv
from foreman_inator.runner.worktree import _base_env
from foreman_inator.tickets import conventions as C
from foreman_inator.tickets.prompt import CONVENTIONS_BUDGET, build_writer_prompt
from test_ticket_prompt import attempt, ticket

BIG = "AGENTS-RULE-x1: " + "a" * 7_000  # more than the whole budget on its own
NESTED = "NESTED-RULE-n4: keep calc pure."
GLOBAL = "GLOBAL-RULE-g2: be brief."


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, env=_base_env(), check=True, capture_output=True, text=True
    ).stdout


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "base")
    return repo


@pytest.fixture
def setup(tmp_path):
    repo = _repo(
        tmp_path,
        {
            "AGENTS.md": BIG,
            "CLAUDE.md": BIG,  # identical to AGENTS.md: skipped
            "calc/AGENTS.md": NESTED,
            "calc/ops.py": "x = 1\n",
        },
    )
    g = tmp_path / "global.md"
    g.write_text(GLOBAL)
    g.chmod(0o600)
    return repo, g


def _build(repo, g, **kw):
    return RS.build(
        "octo/widgets", git_dir=repo, owned_files=("calc/ops.py",), global_rules=g, **kw
    )


def test_writer_block_is_exactly_what_the_writer_prompt_contains(setup):
    repo, g = setup
    show = _build(repo, g)
    w = show.writer
    assert w.prompt_name == "writer-v1"
    assert w.block and "BEGIN REPO INSTRUCTIONS" in w.block
    text = build_writer_prompt(
        ticket(), attempt(1), (), verify_command=None, conventions=w.conventions
    ).text
    assert w.block in text
    # And the same Conventions a worktree gather gives (the writer's real path).
    direct = C.gather(repo, ("calc/ops.py",), global_path=g, warn=lambda _m: None)
    assert direct.render() == w.conventions.render()


def test_reviewer_block_is_exactly_what_the_reviewer_messages_contain(setup):
    repo, g = setup
    show = _build(repo, g, reviewer_prompt="reviewer-v2")
    r = show.reviewer
    assert r.conventions is not None and r.block.startswith("The repository's own instructions")
    review_input = rv.ReviewInput.build(
        rv.IssueText(1, "t", "b"), "diff", ["calc/ops.py"], ["ok"], r.conventions
    )
    user = rv.build_messages(review_input, prompts.load("reviewer-v2"))[1]["content"]
    assert r.block in user


def test_sources_list_char_counts_and_clips(setup):
    repo, g = setup
    show = _build(repo, g, reviewer_prompt="reviewer-v2")
    lines = "\n".join(RS.format_show(show))
    conv = show.writer.conventions
    clipped = {c.path: c.shown for c in conv.clips}
    # The global file is cut first; AGENTS.md (the repo's own file) wins the budget.
    agents = next(s for s in conv.sources if s.path == "AGENTS.md")
    assert agents.chars == len(BIG)
    assert f"AGENTS.md  {len(BIG)} chars  CLIPPED at {clipped['AGENTS.md']} chars" in lines
    assert clipped["AGENTS.md"] == CONVENTIONS_BUDGET
    assert "CLIPPED at 0 chars (left out" in lines  # global and nested left out
    assert "skipped    CLAUDE.md" in lines
    assert lines.count("=== writer") == 1 and lines.count("=== reviewer") == 1


def test_unclipped_sources_have_no_clip_line(tmp_path):
    repo = _repo(tmp_path, {"AGENTS.md": "short rule\n"})
    g = tmp_path / "none.md"  # missing global file
    show = _build(repo, g)
    out = C.describe(show.writer.conventions)
    assert out == ["repo       AGENTS.md  10 chars"]


def test_reads_the_commit_not_the_working_tree(setup):
    repo, g = setup
    (repo / "calc/AGENTS.md").write_text("UNCOMMITTED-EDIT")
    show = _build(repo, g, reviewer_prompt="reviewer-v2")
    assert "UNCOMMITTED-EDIT" not in show.writer.block + show.reviewer.block


def test_reviewer_v1_has_no_conventions(setup):
    repo, g = setup
    show = _build(repo, g)
    assert show.reviewer.conventions is None and show.reviewer.block == ""
    assert "has no conventions section" in "\n".join(RS.format_show(show))


def test_no_rules_means_no_writer_block_and_reviewer_default_text(tmp_path):
    repo = _repo(tmp_path, {"x.py": ""})
    show = _build(repo, tmp_path / "missing.md", reviewer_prompt="reviewer-v2")
    assert show.writer.block == ""
    assert show.reviewer.block == rv.NO_CONVENTIONS


def test_run_finds_mirror_or_explains(tmp_path, setup):
    repo, g = setup
    out: list[str] = []
    assert RS.run("octo/none", config=None, store=None, echo=out.append,
                  worktrees_root=tmp_path / "wt") == 1  # fmt: skip
    assert "no mirror of octo/none" in out[0]
    mirror = tmp_path / "wt" / "octo" / "widgets" / ".mirror"
    mirror.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", "--bare", str(repo), str(mirror))
    out.clear()

    class Cfg:
        def rules_file_for(self, _repo):
            return None

    code = RS.run("octo/widgets", config=Cfg(), store=None, echo=out.append,
                  worktrees_root=tmp_path / "wt", global_rules=g, ref="main")  # fmt: skip
    assert code == 0 and "Rules for octo/widgets at main" in out[0]
    assert "CLIPPED at" in out[0]
    out.clear()
    assert RS.run("octo/widgets", config=Cfg(), store=None, echo=out.append,
                  worktrees_root=tmp_path / "wt", ref="nope") == 1  # fmt: skip
    assert "unknown ref" in out[0]


def test_champion_prompts_default_without_store():
    assert RS.champion_prompts(None) == ("writer-v1", "reviewer-v1")
