"""Replay a rules change before it goes live (#43, todo #66).

``fman experiment run --rules FILE``: FILE replaces the gathered conventions on the
challenger side only; model and prompt are the same on both sides; the text's hash is in
the challenger's variant id and the experiment's note, and the text is kept by hash so a
later edit of FILE never changes an old run.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fakes.opencode import Script
from foreman_inator import worker_experiments as wx
from foreman_inator.config import Config
from foreman_inator.db.store import Store
from foreman_inator.experiments import replay_runner as rr
from foreman_inator.tickets.prompt import CONVENTIONS_BUDGET, load_writer_template
from test_replay_sets import REPO, World, git, key_of, reviewer_pair
from test_replay_sets import script as replay_script

AGENTS = "AGENTS-MARKER: the repo's current rules"
OVERRIDE = "RULES-OVERRIDE-MARKER: the proposed rules"


@pytest.fixture
def world(tmp_path: Path):
    w = World(tmp_path)
    yield w
    w.close()


def _commit_agents(w: World) -> None:
    (w.seed / "AGENTS.md").write_text(AGENTS + "\n")
    git("add", "-A", cwd=w.seed)
    git("commit", "-q", "-m", "rules", cwd=w.seed)
    git("push", "-q", "origin", "main", cwd=w.seed)


def _same_model_pair(w: World, rules: rr.RulesOverride):
    t = load_writer_template()
    champ = rr.WriterVariant(
        f"inkling-2-mini/{t.name}@{t.sha256[:8]}", w.config.model("inkling-2-mini"), t
    )
    return champ, rr.with_rules(champ, rules)


def test_the_override_reaches_the_challenger_only_and_is_recorded(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    """Champion unchanged: it still sees the repo's AGENTS.md and never the override.
    Non-vacuity: with ``_drive`` giving the override to both sides (``rules =
    challenger.rules``), the champion's prompt carries the override marker and this fails."""
    w = world
    _commit_agents(w)
    w.merged_ticket("a")
    w.merged_ticket("b")
    w.sync()
    rules = rr.RulesOverride(OVERRIDE + "\n", "AGENTS.md")
    champ, chall = _same_model_pair(w, rules)
    assert chall.variant_id == f"{champ.variant_id}+rules@{rules.hash8}"
    assert (chall.endpoint, chall.template) == (champ.endpoint, champ.template)

    def choose(prompt: str, endpoint: Any) -> Script:
        return replay_script("ok" if "RULES-OVERRIDE-MARKER" in prompt else "wrong",
                             key_of(prompt))  # fmt: skip

    w.driver.choose = choose
    caplog.set_level(logging.INFO, logger=rr.log.name)
    c = rr.run_replay(w.deps(), role="writer", champion=champ, challenger=chall,
                      settings=rr.ReplaySettings(min_cases=2))  # fmt: skip

    assert len(w.driver.runs) == 4
    by_side: dict[str, list[dict[str, Any]]] = {"champion": [], "challenger": []}
    for r in w.driver.runs:
        side = "challenger" if "RULES-OVERRIDE-MARKER" in r["prompt"] else "champion"
        by_side[side].append(r)
    # champion unchanged: the production rules, and never the override
    assert len(by_side["champion"]) == 2 and len(by_side["challenger"]) == 2
    for r in by_side["champion"]:
        assert "AGENTS-MARKER" in r["prompt"]
    # challenger: the override replaces the gathered rules; model and prompt the same
    for r in by_side["challenger"]:
        assert "AGENTS-MARKER" not in r["prompt"]
    assert {r["endpoint"].name for r in w.driver.runs} == {"inkling-2-mini"}

    hashes = re.findall(r"case \d+ (champion|challenger) \w+ \(conventions sha256 (\w{64})\)",
                        caplog.text)  # fmt: skip
    chall_hashes = {h for side, h in hashes if side == "challenger"}
    champ_hashes = {h for side, h in hashes if side == "champion"}
    assert chall_hashes == {rules.conventions().sha256}
    assert champ_hashes and champ_hashes.isdisjoint(chall_hashes)

    # writer side: pass rate, cost and tokens per side
    assert (c.champion.passed, c.challenger.passed, c.verdict) == (0, 2, "wins")
    assert c.challenger.tokens_in > 0 and c.champion.tokens_in > 0
    exp = w.store.get_experiment(c.experiment_id)
    assert exp.challenger_variant == chall.variant_id
    note = exp.result_note
    assert f"Rules override AGENTS.md sha256 {rules.sha256}" in note
    assert "Tokens in/out: champion" in note and "Passed the merged tests" in note


def test_a_rules_override_goes_on_the_challenger_only(world: World) -> None:
    w = world
    rules = rr.RulesOverride(OVERRIDE)
    champ, chall = _same_model_pair(w, rules)
    with pytest.raises(ValueError, match="challenger only"):
        rr.run_replay(w.deps(), role="writer", champion=chall, challenger=champ)
    forged = replace(chall, variant_id=champ.variant_id + "+rules@00000000")
    with pytest.raises(ValueError, match="rules hash"):
        rr.run_replay(w.deps(), role="writer", champion=champ, challenger=forged)


def test_reviewer_rules_refused_until_the_reviewer_reads_rules(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rr, "REVIEW_TAKES_CONVENTIONS", False)
    champ, _ = reviewer_pair(world)
    chall = rr.with_rules(replace(champ, variant_id="glm/reviewer-v1@x"), rr.RulesOverride("x"))
    with pytest.raises(ValueError, match="does not read repo rules"):
        rr.run_replay(world.deps(), role="reviewer", champion=champ, challenger=chall)


def test_reviewer_rules_override_and_style_tag_rate(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once ``review_change`` takes ``conventions`` (#39), the challenger's review gets
    the override and the champion's none; ``style``-tagged kickbacks are counted."""
    w = world
    w.merged_ticket("a")
    w.merged_ticket("b")
    w.sync()
    champ, _ = reviewer_pair(w)
    rules = rr.RulesOverride("Prefer short names.", "AGENTS.md")
    chall = rr.with_rules(replace(champ, variant_id="claude-opus/reviewer-v1@x"), rules)
    seen: list[Any] = []

    def fake_review(**kw: Any) -> Any:
        conv = kw.get("conventions")
        seen.append(conv)
        if conv is None:
            return SimpleNamespace(verdict="approve", tag=None, note="ok",
                                   tokens_in=3, tokens_out=1, cost_usd=0.0)  # fmt: skip
        return SimpleNamespace(verdict="request_changes", tag="style", note="names too long",
                               tokens_in=3, tokens_out=1, cost_usd=0.0)  # fmt: skip

    monkeypatch.setattr(rr, "REVIEW_TAKES_CONVENTIONS", True)
    monkeypatch.setattr(rr.rv, "review_change", fake_review)
    c = rr.run_replay(w.deps(), role="reviewer", champion=champ, challenger=chall,
                      settings=rr.ReplaySettings(min_cases=1))  # fmt: skip
    assert len(seen) == 2 * c.total_cases and c.total_cases >= 2
    assert seen[0::2] == [None] * c.total_cases  # champion sides
    assert all(s is not None and "Prefer short names." in s.render() for s in seen[1::2])
    assert (c.champion.style_tags, c.challenger.style_tags) == (0, c.total_cases)
    assert c.challenger.false_kickbacks == c.total_cases and c.verdict == "does_not_win"
    note = w.store.get_experiment(c.experiment_id).result_note
    assert f"Style-tagged kickbacks: champion 0/{c.total_cases}, challenger" in note
    assert rules.sha256 in note


def test_override_conventions_and_ids() -> None:
    r = rr.RulesOverride("a\r\nb\n", "team.md")
    conv = r.conventions()
    assert [(s.kind, s.path, s.text) for s in conv.sources] == [("repo", "team.md", "a\nb")]
    assert conv.clips == ()
    big = rr.RulesOverride("x" * (CONVENTIONS_BUDGET + 500)).conventions()
    assert big.clips and big.sources[0].clipped
    assert len(big.render()) < CONVENTIONS_BUDGET + 200
    assert rr.RulesOverride("  \n").conventions().empty
    assert rr.split_rules_id("m/writer-v1@0123abcd+rules@deadbeef") == (
        "m/writer-v1@0123abcd", "deadbeef",
    )  # fmt: skip
    assert rr.split_rules_id("m/writer-v1@0123abcd") == ("m/writer-v1@0123abcd", None)
    assert rr.RulesOverride("a").hash8 != rr.RulesOverride("b").hash8


# =========================================================================== the command


def _env(tmp_path: Path) -> wx.RunEnv:
    store = Store(tmp_path / "fman.db")
    cfg = Config(app_id=1, repos=(REPO,))
    return wx.RunEnv(store, cfg, deps=None, home=tmp_path / "home")  # type: ignore[arg-type]


def test_run_with_rules_keeps_the_text_so_an_edit_never_changes_the_old_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(wx.rr, "run_replay", lambda deps, **kw: calls.append(kw) or kw)
    rules_file = tmp_path / "AGENTS.md"
    rules_file.write_text("Version one of the rules.\n")
    try:
        wx.run_experiment(env, "writer", None, rules=rules_file, home=env.home)
        (kw,) = calls
        champ, chall = kw["champion"], kw["challenger"]
        assert champ.rules is None and chall.rules.text == "Version one of the rules.\n"
        v1 = chall.variant_id
        assert v1.startswith(champ.variant_id + "+rules@")
        assert (chall.endpoint, chall.template) == (champ.endpoint, champ.template)

        rules_file.write_text("Version two.\n")  # edited after the run started
        resumed = wx._variant_from_id(env.config, "writer", v1, env.home)
        assert resumed.variant_id == v1 and resumed.rules.text == "Version one of the rules.\n"
        wx.run_experiment(env, "writer", None, rules=rules_file, home=env.home)
        assert calls[-1]["challenger"].variant_id != v1  # the edit is a new variant

        with pytest.raises(wx.ExperimentError, match="no kept rules"):
            wx.load_rules("ffffffff", env.home)
    finally:
        env.store.close()


def test_run_command_argument_checks(tmp_path: Path) -> None:
    with pytest.raises(wx.ExperimentError, match="or --all"):
        wx.run_command(role=None, challenger=None, all_=True, rules=tmp_path / "x.md")
    with pytest.raises(wx.ExperimentError, match="--rules"):
        wx.run_command(role="writer", challenger=None, all_=False, rules=tmp_path / "nope.md")
    empty = tmp_path / "empty.md"
    empty.write_text("  \n")
    with pytest.raises(wx.ExperimentError, match="empty"):
        wx.run_command(role="writer", challenger=None, all_=False, rules=empty)
    with pytest.raises(wx.ExperimentError, match="--challenger"):
        wx.run_command(role="writer", challenger=None, all_=False)
