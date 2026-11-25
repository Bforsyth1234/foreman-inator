"""Seeded-bug reviewer cases (todo #35, GH #17): mutations, git apply, the seeded run.

The run tests use ``test_replay_sets.World``: a real store, real git repos, the real
proxy and the real reviewer in front of ``FakeModels``.
"""

from __future__ import annotations

import random
import subprocess
from pathlib import Path

import pytest

from foreman_inator.experiments import replay_runner as rr
from foreman_inator.experiments import replay_sets as rs
from foreman_inator.experiments import seeded as sd
from test_replay_sets import REPO, World, git, reviewer_pair


def _diff(path: str, added: list[str], *, context: tuple[list[str], list[str]] = ([], [])) -> str:
    before, after = context
    body = [f" {x}" for x in before] + [f"+{x}" for x in added] + [f" {x}" for x in after]
    old_len = len(before) + len(after)
    new_len = old_len + len(added)
    return (
        f"diff --git a/{path} b/{path}\nindex 1111111..2222222 100644\n--- a/{path}\n"
        f"+++ b/{path}\n@@ -1,{old_len} +1,{new_len} @@\n" + "\n".join(body) + "\n"
    )


def _kinds(text: str) -> list[tuple[str, int]]:
    return [(m.kind, m.new_line) for m in sd.candidates(sd.parse_diff(text))]


def _mutate(text: str, kind: str) -> str:
    files = sd.parse_diff(text)
    mut = next(m for m in sd.candidates(files) if m.kind == kind)
    return sd.render_diff(sd.apply_mutation(files, mut))


# =========================================================================== mutations


GUARD_PY = ["def clamp(n, hi):", "    if n > hi:", "        return hi", "    return n"]


def test_parse_and_render_round_trip() -> None:
    text = _diff("src/clamp.py", GUARD_PY, context=(["import os"], ["", "X = 1"]))
    assert sd.render_diff(sd.parse_diff(text)) == text


def test_python_candidates_are_found_on_added_lines_only() -> None:
    text = _diff("src/clamp.py", GUARD_PY, context=(["if os.name == 'nt':"], []))
    assert _kinds(text) == [("drop_if_guard", 3), ("flip_comparison", 3)]


def test_drop_if_guard_removes_the_whole_block_and_recounts_the_hunk() -> None:
    out = _mutate(_diff("src/clamp.py", GUARD_PY), "drop_if_guard")
    assert "@@ -1,0 +1,2 @@" in out
    assert "+def clamp(n, hi):\n+    return n\n" in out
    assert "if n > hi" not in out


def test_neutralise_and_flip() -> None:
    lines = ["def f(x):", "    assert x >= 0, 'negative'", "    raise ValueError(x)"]
    text = _diff("pkg/f.py", lines)
    assert [k for k, _ in _kinds(text)] == ["neutralise_check", "flip_comparison",
                                            "neutralise_check"]  # fmt: skip
    assert "+    pass\n+    raise ValueError(x)\n" in _mutate(text, "neutralise_check")
    assert "+    assert x < 0, 'negative'" in _mutate(text, "flip_comparison")


def test_brace_language_guard_and_throw() -> None:
    lines = ["function f(x) {", "  if (x === null) {", "    throw new Error('no');", "  }",
             "  return x;", "}"]  # fmt: skip
    text = _diff("src/f.ts", lines)
    assert [k for k, _ in _kinds(text)] == ["drop_if_guard", "flip_comparison",
                                            "neutralise_check"]  # fmt: skip
    dropped = _mutate(text, "drop_if_guard")
    assert "if (x" not in dropped and "throw" not in dropped and "+  return x;" in dropped
    assert "+  if (x !== null) {" in _mutate(text, "flip_comparison")
    assert "throw" not in _mutate(text, "neutralise_check")


@pytest.mark.parametrize(
    "path,lines,context",
    [
        # test files are never seeded
        ("tests/test_clamp.py", GUARD_PY, ([], [])),
        # a guard wrapping existing (context) lines is not wholly new
        ("src/a.py", ["if ready:"], ([], ["    go()"])),
        # an elif/else follows the guard
        ("src/a.py", ["if a:", "    x()", "else:", "    y()"], ([], [])),
        # dropping the guard would leave the def's body empty
        ("src/a.py", ["def f():", "    if a:", "        return 1"], ([], [])),
        # a brace guard with ``} else {``
        ("src/a.js", ["if (a) {", "  x();", "} else {", "  y();", "}"], ([], [])),
        # unknown language
        ("docs/a.md", ["if a > b:", "    x"], ([], [])),
    ],
)
def test_no_guard_drop_where_it_would_not_be_a_clean_seeded_bug(path, lines, context) -> None:
    assert "drop_if_guard" not in [k for k, _ in _kinds(_diff(path, lines, context=context))]


def test_comparisons_in_strings_and_comments_are_not_flipped() -> None:
    text = _diff("src/a.py", ["msg = 'a < b'  # x == y", "ok = a->b", "# n > 1"])
    assert _kinds(text) == []


def test_a_later_hunk_moves_up_by_the_dropped_lines() -> None:
    files = sd.parse_diff(
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
        "@@ -1,2 +1,5 @@\n x = 1\n+if x:\n+    y = 2\n+z = 3\n w = 4\n"
        "@@ -20,2 +23,3 @@\n a = 1\n+b = a == 1\n c = 2\n"
    )
    mut = next(m for m in sd.candidates(files) if m.kind == "drop_if_guard")
    out = sd.render_diff(sd.apply_mutation(files, mut))
    assert "@@ -1,2 +1,3 @@" in out and "@@ -20,2 +21,3 @@" in out


# =========================================================================== git apply


def _repo(tmp: Path, files: dict[str, str]) -> tuple[Path, str]:
    repo = tmp / "r"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    for p, t in files.items():
        (repo / p).parent.mkdir(parents=True, exist_ok=True)
        (repo / p).write_text(t)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "base", cwd=repo)
    return repo, git("rev-parse", "HEAD", cwd=repo)


class _Mirror:
    def __init__(self, path: Path):
        self.mirror_path = path


def test_seeded_patches_pass_git_apply_check_against_the_base(tmp_path: Path) -> None:
    filler = "".join(f"v{i} = {i}\n" for i in range(30))
    repo, base = _repo(tmp_path, {"src/a.py": "x = 1\n" + filler + "end = 0\n"})
    text = "x = 1\nif x:\n    y = 2\nz = 3\n" + filler + "end = 0\ndone = end == 0\n"
    (repo / "src/a.py").write_text(text)
    git("commit", "-qam", "change", cwd=repo)
    diff = git("diff", base, "HEAD", cwd=repo) + "\n"
    files = sd.parse_diff(diff)
    assert len(files[0].hunks) == 2  # the drop has to move the second hunk
    mirror = _Mirror(repo)
    for mut in sd.candidates(files):
        patch = sd.render_diff(sd.apply_mutation(files, mut))
        assert sd.applies(mirror, base, patch), (mut, patch)  # type: ignore[arg-type]
    # a patch whose context is not the base's is refused
    assert not sd.applies(mirror, base, diff.replace(" x = 1", " x = 9"))  # type: ignore[arg-type]


def test_choose_skips_candidates_that_do_not_apply() -> None:
    text = _diff("src/clamp.py", GUARD_PY)
    files = sd.parse_diff(text)
    seen: list[str] = []

    def check(patch: str) -> bool:
        seen.append(patch)
        return len(seen) > 1  # the first candidate "does not apply"

    picked = sd.choose(files, random.Random(1), check)
    assert picked is not None and len(seen) == 2 and picked[1] == seen[1]
    assert sd.choose(files, random.Random(1), lambda p: False) is None


def test_the_choice_is_deterministic_per_seed() -> None:
    lines = [f"    if v{i} > {i}:\n        return {i}" for i in range(8)]
    text = _diff("src/m.py", ["def f():", *"\n".join(lines).split("\n"), "    return 0"])
    files = sd.parse_diff(text)

    def pick(seed: int) -> sd.Mutation:
        got = sd.choose(files, sd._rng(seed, "b" * 40, "h" * 40), lambda p: True)
        assert got is not None
        return got[0]

    assert pick(7) == pick(7)
    assert len({(pick(s).kind, pick(s).new_line) for s in range(12)}) > 1


# =========================================================================== the seeded run


SOURCES = {
    "clamp": "def clamp(n, hi):\n    if n > hi:\n        return hi\n    return n\n",
    "div": "def div(a, b):\n    assert b != 0\n    return a // b\n",
}


def _merged_code_ticket(w: World, key: str) -> int:
    w.issue += 1
    n = w.gh.create_issue(REPO, f"Add {key}", "...", author="brooks")
    pr, base, s = 100 + n, w.head(), w.store
    t = s.create_ticket(repo=REPO, issue_number=n, title=f"Add {key}",
                        request=f"Add {key}().", base_branch="main")  # fmt: skip
    s.update_ticket(t.id, pr_number=pr)
    b = f"fman/issue-{n}/a1"
    w.branch(b, {f"src/{key}.py": SOURCES[key]}, base)
    a = s.add_attempt(t.id, model="inkling-2-mini", tier=1, trigger="first", branch=b,
                      base_commit=base)  # fmt: skip
    s.finish_attempt(a.id, None, tests_passed=True)
    s.set_accepted_attempt(t.id, a.id)
    s.set_column(t.id, "done")
    w.merge(b, pr, "merge")
    return t.id


def _intact(text: str) -> bool:
    """The fake challenger's review: approve only a diff with every merged line intact."""
    for src in SOURCES.values():
        lines = src.splitlines()
        if lines[0] in text and not all(line in text for line in lines):
            return False
    return True


@pytest.fixture
def world(tmp_path: Path):
    w = World(tmp_path)
    yield w
    w.close()


def test_seeded_run_reports_the_catch_rate_per_variant(world: World) -> None:
    w = world
    _merged_code_ticket(w, "clamp")
    _merged_code_ticket(w, "div")
    w.sync()
    before = w.live_snapshot()
    champ, chall = reviewer_pair(w)
    reviewed: list[str] = []

    def rule(model: str, text: str) -> str:
        reviewed.append(text)
        if model == "glm-5.3":  # the challenger spots any change to the merged lines
            return "approve" if _intact(text) else "request_changes"
        return "approve"  # the champion approves everything

    w.review_rule = rule
    c = rr.run_seeded(w.deps(), champion=champ, challenger=chall, seed=5,
                      settings=rr.ReplaySettings(min_cases=2))  # fmt: skip
    exp = w.store.get_experiment(c.experiment_id)
    assert (exp.mode, exp.role) == ("seeded", "reviewer")
    assert (c.merged_cases, c.seeded_cases, c.scored_cases, c.errored_cases) == (2, 2, 2, 0)
    rows = rr.results(w.store, c.experiment_id)
    assert {(r.side, r.verdict, r.score) for r in rows} == {
        ("champion", "approve", "miss"),
        ("challenger", "request_changes", "catch"),
    }
    assert (rr.catch_rate(c.champion), rr.catch_rate(c.challenger)) == (0.0, 1.0)
    assert c.verdict == "wins", c.reason
    note = exp.result_note or ""
    assert note.startswith("seeded run, seed 5, set ")
    assert "Catch rate: champion 0/2 (0%), challenger 2/2 (100%)" in note
    assert "Expected verdict: request_changes." in note
    assert sum(k.cases for k in c.by_kind) == 2
    assert w.live_snapshot() == before  # nothing written to the live tables
    assert reviewed and all("seeded" not in t for t in reviewed)  # no hint in the review

    # the same seed rebuilds the same seeded commits; the finished run is not rerun
    rset = rr._run_row(w.store, c.experiment_id)["set_id"]
    one = sd.seeded_cases(w.store, rset, lambda r: w.mirror, seed=5)
    two = sd.seeded_cases(w.store, rset, lambda r: w.mirror, seed=5)
    assert [x.case.head_commit for x in one] == [x.case.head_commit for x in two]  # type: ignore[union-attr]
    n = len(reviewed)
    again = rr.run_seeded(w.deps(), champion=champ, challenger=chall, seed=5,
                          experiment_id=c.experiment_id,
                          settings=rr.ReplaySettings(min_cases=2))  # fmt: skip
    assert len(reviewed) == n and again.challenger.catches == 2
    with pytest.raises(ValueError, match="seeded with 5"):
        rr.run_seeded(w.deps(), champion=champ, challenger=chall, seed=6,
                      experiment_id=c.experiment_id)  # fmt: skip


def test_seeded_commits_are_reviewed_as_the_change(world: World) -> None:
    w = world
    _merged_code_ticket(w, "clamp")
    w.sync()
    rset = rs.build_replay_set(w.store, "reviewer", mirror_for=lambda r: w.mirror)
    [case] = sd.seeded_cases(w.store, rset.id, lambda r: w.mirror, seed=1,
                             kinds=("drop_if_guard",))  # fmt: skip
    assert isinstance(case, sd.SeededCase)
    assert case.case.truth == "kicked_back" and case.case.head_via == "seeded:drop_if_guard"
    assert case.case.base_commit == case.source.base_commit
    shown = subprocess.run(
        ["git", "show", f"{case.case.head_commit}:src/clamp.py"],
        cwd=w.mirror.mirror_path, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    assert shown == "def clamp(n, hi):\n    return n\n"
    # nothing pushed, no branch or ref made for it
    refs = git("for-each-ref", "--format=%(objectname)", cwd=w.mirror.mirror_path)
    assert case.case.head_commit not in refs and case.case.head_commit not in w.remote_refs()


def test_kicked_back_cases_and_unseedable_changes_are_left_out(world: World) -> None:
    w = world
    w.merged_ticket("a", kickback="incomplete")  # .txt and .sh files only: nothing to seed
    w.sync()
    rset = rs.build_replay_set(w.store, "reviewer", mirror_for=lambda r: w.mirror)
    built = sd.seeded_cases(w.store, rset.id, lambda r: w.mirror)
    assert [type(b).__name__ for b in built] == ["SeededSkip"]  # the merged one only
    assert "no mutation site" in built[0].reason  # type: ignore[union-attr]
