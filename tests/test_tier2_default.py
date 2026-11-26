"""#70: tier 2 defaults to open-weights cloud (GLM); local Qwen 27B is the opt-in."""

from __future__ import annotations

from pathlib import Path

import pytest

from foreman_inator import config
from foreman_inator.config import Config
from foreman_inator.review.reviewer import NoIndependentReviewer, pick_reviewer
from foreman_inator.routing.escalation import build_ladder

# A config.toml written before #70: qwen local at tier 2, GLM first at tier 3.
OLD_TOML = """
allowed_users = ["brooks"]
reviewer_models = ["claude-opus", "glm"]

[[models]]
name = "inkling-2-mini"
tier = 1
kind = "local"
model = "inkling-2-mini"
base_url = "http://127.0.0.1:11434/v1"
family = "inkling"

[[models]]
name = "qwen3.8-27b"
tier = 2
kind = "local"
model = "qwen3.8:27b"
base_url = "http://127.0.0.1:11434/v1"
family = "qwen"

[[models]]
name = "glm"
tier = 3
kind = "cloud"
model = "glm-5.4"
base_url = "https://api.z.ai/api/coding/paas/v4"
family = "glm"
api_key_ref = "keychain:foreman-inator/glm"

[[models]]
name = "claude-opus"
tier = 3
kind = "cloud"
model = "claude-opus-5-5"
base_url = "https://api.anthropic.com/v1"
family = "claude"
api_key_ref = "keychain:foreman-inator/anthropic"
wire = "anthropic"

[[models]]
name = "claude-fable"
tier = 3
kind = "cloud"
model = "claude-fable-5-2"
base_url = "https://api.anthropic.com/v1"
family = "claude"
api_key_ref = "keychain:foreman-inator/anthropic"
wire = "anthropic"
"""


def test_default_tiers_are_local_cloud_cloud() -> None:
    cfg = Config()
    assert [cfg.models_for_tier(t)[0].kind for t in (1, 2, 3)] == ["local", "cloud", "cloud"]
    assert [m.name for m in cfg.models_for_tier(2)] == ["glm"]
    assert "qwen3.8-27b" not in {m.name for m in cfg.models}


def test_escalation_from_tier_1_goes_to_glm() -> None:
    ladder = build_ladder(Config().models)
    assert [(r.tier, r.model) for r in ladder] == [
        (1, "inkling-2-mini"),
        (2, "glm"),
        (3, "claude-opus"),
        (3, "claude-fable"),
    ]


def test_every_default_writer_gets_a_reviewer_of_another_family() -> None:
    """Invariant 7f under the new defaults: GLM at tier 2 is reviewed by Claude Opus."""
    cfg = Config()
    picks = {m.name: pick_reviewer(cfg, m).name for m in cfg.models}
    assert picks == {
        "inkling-2-mini": "claude-opus",
        "glm": "claude-opus",
        "claude-opus": "glm",
        "claude-fable": "glm",
    }
    for m in cfg.models:
        assert pick_reviewer(cfg, m).family != m.family


def test_glm_only_reviewer_cannot_review_a_glm_writer() -> None:
    cfg = Config(reviewer_models=("glm",))
    with pytest.raises(NoIndependentReviewer):
        pick_reviewer(cfg, cfg.model("glm"))


def test_a_pre_70_config_with_qwen_at_tier_2_loads_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(OLD_TOML)
    cfg = config.load(path)
    assert cfg.models == config.local_tier2_models()
    assert [m.name for m in cfg.models_for_tier(2)] == ["qwen3.8-27b"]
    assert cfg.models_for_tier(2)[0].kind == "local"
    assert [m.name for m in cfg.models_for_tier(3)] == ["glm", "claude-opus", "claude-fable"]
    assert pick_reviewer(cfg, cfg.model("qwen3.8-27b")).name == "claude-opus"
    assert config.load(config.save(cfg, tmp_path / "again.toml")) == cfg


def test_qwen_opt_in_endpoint() -> None:
    q = config.qwen_27b_local()
    assert (q.tier, q.kind, q.model, q.family) == (2, "local", "qwen3.8:27b", "qwen")
