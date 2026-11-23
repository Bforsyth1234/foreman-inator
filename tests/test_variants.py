"""#24: variants (model + prompt version), champions, and the prompts package."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fde_kit.prompts import PromptRef, prompt_hash

from foreman_inator import prompts
from foreman_inator.config import Config
from foreman_inator.db.store import ROLES, NotFoundError, Store, StoreError
from foreman_inator.experiments import variants as v

PACKAGED = ("clarify-v1", "reviewer-v1", "router-v1", "writer-v1")


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "db.sqlite")
    yield s
    s.close()


@pytest.fixture
def prompts_copy(tmp_path: Path) -> Path:
    """A temp copy of the packaged prompts dir; tests edit this, never the packaged files."""
    dest = tmp_path / "prompts-copy"
    dest.mkdir()
    for name in prompts.list_prompts():
        with prompts.prompt_path(name) as src:
            shutil.copy(src, dest / src.name)
    return dest


# --------------------------------------------------------------------------- prompts package


def test_packaged_prompts_are_listed():
    assert set(PACKAGED) <= set(prompts.list_prompts())


def test_load_hash_is_fde_kit_prompt_hash_of_the_stripped_file():
    ref = prompts.load("writer-v1")
    with prompts.prompt_path("writer-v1") as path:
        raw = path.read_text(encoding="utf-8")
    assert ref.name == "writer-v1"
    assert ref.sha256 == prompt_hash(raw.strip())
    assert prompts.version("writer-v1") == f"writer-v1@{ref.sha256[:8]}"


def test_load_matches_every_existing_loader():
    from foreman_inator.review.reviewer import load_reviewer_template
    from foreman_inator.routing.rules import load_router_template
    from foreman_inator.tickets.clarify import load_clarify_template
    from foreman_inator.tickets.prompt import load_writer_template

    for name, loader in (
        ("writer-v1", load_writer_template),
        ("router-v1", load_router_template),
        ("reviewer-v1", load_reviewer_template),
        ("clarify-v1", load_clarify_template),
    ):
        theirs, ours = loader(), prompts.load(name)
        assert (ours.name, ours.sha256, ours.text) == (theirs.name, theirs.sha256, theirs.text)


def test_a_prompts_folder_in_cwd_cannot_shadow_a_packaged_prompt(tmp_path, monkeypatch):
    packaged = prompts.load("writer-v1").sha256
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "writer-v1.md").write_text("shadow prompt\n", encoding="utf-8")
    (tmp_path / "prompts" / "only-in-cwd.md").write_text("x\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert prompts.load("writer-v1").sha256 == packaged
    assert "only-in-cwd" not in prompts.list_prompts()
    with pytest.raises(FileNotFoundError):
        prompts.load("only-in-cwd")


@pytest.mark.parametrize("bad", ["../writer-v1", "writer-v1.md", "a/b", "", ".hidden"])
def test_bad_prompt_names_are_refused(bad):
    with pytest.raises(ValueError):
        prompts.load(bad)


def test_prompts_dir_replaces_the_package(prompts_copy):
    (prompts_copy / "writer-v2.md").write_text("new writer\n", encoding="utf-8")
    assert "writer-v2" in prompts.list_prompts(prompts_copy)
    assert "writer-v2" not in prompts.list_prompts()
    assert prompts.load("writer-v1", prompts_copy) == prompts.load("writer-v1")


# --------------------------------------------------------------------------- ids


def test_variant_id_format():
    ref = PromptRef(name="writer-v1", sha256="0123456789abcdef" * 4, text="t")
    assert v.variant_id("inkling-2-mini", ref) == "inkling-2-mini/writer-v1@01234567"


def test_variant_id_equals_the_writer_prompts_own():
    from foreman_inator.tickets.prompt import WriterPrompt, load_writer_template

    t = load_writer_template()
    wp = WriterPrompt(text="", prompt_name=t.name, prompt_hash=t.sha256, situation="first")
    assert v.variant_id("qwen3.8-27b", prompts.load("writer-v1")) == wp.variant_id("qwen3.8-27b")


def test_variant_id_equals_the_reviewers_own(monkeypatch):
    """Run the real review_change up to the point it builds its variant id."""
    from foreman_inator.review import reviewer as rv

    class Captured(Exception):
        pass

    def capture(**kw):
        raise Captured(kw["variant_id"])

    monkeypatch.setattr(rv, "_Run", capture)
    cfg = Config()
    writer, reviewer = cfg.model("inkling-2-mini"), cfg.model("claude-opus")
    with pytest.raises(Captured) as got:
        rv.review_change(
            issue=None, change=None, writer=writer, reviewer=reviewer, proxy=None, attempt_id=1
        )
    assert got.value.args[0] == v.variant_id("claude-opus", prompts.load("reviewer-v1"))


def test_parse_round_trip_and_model_with_slash():
    ref = prompts.load("router-v1")
    for model in ("glm", "org/model-x"):
        p = v.parse_variant_id(v.variant_id(model, ref))
        assert (p.model, p.prompt_name, p.hash8) == (model, "router-v1", ref.sha256[:8])


@pytest.mark.parametrize(
    "bad",
    ["glm", "glm@0123abcd", "/writer-v1@0123abcd", "glm/@0123abcd", "glm/w@0123", "g/w@ZZZZZZZZ"],
)
def test_parse_refuses_malformed(bad):
    with pytest.raises(v.VariantError):
        v.parse_variant_id(bad)


# --------------------------------------------------------------------------- store


def test_register_is_idempotent_and_roles_are_the_stores(store):
    assert ROLES == ("writer", "reviewer")
    ref = prompts.load("writer-v1")
    a = v.register(store, "writer", "glm", ref)
    b = v.register(store, "writer", "glm", ref)
    assert a == b and len(store.list_variants()) == 1
    with pytest.raises(v.VariantError):
        v.register(store, "router", "glm", prompts.load("router-v1"))


def test_set_champion_writes_history_and_checks_role(store):
    w1 = v.register(store, "writer", "glm", prompts.load("writer-v1"))
    w2 = v.register(store, "writer", "qwen3.8-27b", prompts.load("writer-v1"))
    r1 = v.register(store, "reviewer", "claude-opus", prompts.load("reviewer-v1"))
    assert v.champion(store, "writer") is None
    v.set_champion(store, "writer", w1.id, note="first")
    v.set_champion(store, "writer", w2.id, note="promoted")
    assert v.champion(store, "writer") == w2
    assert [(c.variant_id, c.note) for c in v.champion_history(store, "writer")] == [
        (w1.id, "first"),
        (w2.id, "promoted"),
    ]
    with pytest.raises(StoreError):
        v.set_champion(store, "writer", r1.id)
    with pytest.raises(NotFoundError):
        v.set_champion(store, "reviewer", "claude-opus/reviewer-v9@00000000")
    assert v.lookup(store, "nope/x@00000000") is None


def test_ensure_defaults_fresh_install_has_champions_and_is_idempotent(store):
    cfg = Config()
    first = v.ensure_defaults(store, cfg)
    writer_ref, reviewer_ref = prompts.load("writer-v1"), prompts.load("reviewer-v1")
    assert v.champion(store, "writer").id == v.variant_id("inkling-2-mini", writer_ref)
    assert v.champion(store, "reviewer").id == v.variant_id("claude-opus", reviewer_ref)
    assert {x.model for x in store.list_variants(role="writer")} == {m.name for m in cfg.models}
    assert {x.model for x in store.list_variants(role="reviewer")} == set(cfg.reviewer_models)
    assert len(first.champions_set) == 2
    again = v.ensure_defaults(store, cfg)
    assert again.champions_set == ()
    assert len(v.champion_history(store, "writer")) == 1
    assert all(s.matches for s in (v.role_status(store, r) for r in ROLES))


def test_editing_a_prompt_file_creates_a_new_variant_and_the_old_stays_queryable(
    store, prompts_copy
):
    """The item's Verify bullet."""
    cfg = Config()
    v.ensure_defaults(store, cfg, prompts_dir=prompts_copy)
    old = v.champion(store, "writer")
    assert old is not None

    path = prompts_copy / "writer-v1.md"
    path.write_text(path.read_text(encoding="utf-8") + "\nOne more rule.\n", encoding="utf-8")
    edited = prompts.load("writer-v1", prompts_copy)
    new = v.register(store, "writer", old.model, edited)

    assert new.id != old.id
    assert v.parse_variant_id(new.id).prompt_name == "writer-v1"
    kept = store.get_variant(old.id)
    assert (kept.role, kept.model, kept.prompt_name, kept.prompt_hash) == (
        "writer",
        "inkling-2-mini",
        "writer-v1",
        prompts.load("writer-v1").sha256,
    )
    assert kept.prompt_hash != edited.sha256

    # The edited prompt is not promoted: champion unchanged, ensure_defaults keeps it, and
    # the status surfaces the mismatch with the variant that actually runs.
    v.ensure_defaults(store, cfg, prompts_dir=prompts_copy)
    assert v.champion(store, "writer") == old
    status = v.role_status(store, "writer", prompts_dir=prompts_copy)
    assert not status.matches and status.running_variant_id == new.id
    assert "PROMPT CHANGED" in status.line() and new.id in status.line()

    v.set_champion(store, "writer", new.id, note="promote edited writer")
    assert v.role_status(store, "writer", prompts_dir=prompts_copy).matches
    assert store.get_variant(old.id) == kept


def test_status_lines_cover_roles_and_roleless_prompts(store, prompts_copy):
    assert v.status_lines(store)[0].startswith("writer: no champion")
    v.ensure_defaults(store, Config())
    lines = v.status_lines(store)
    assert lines[0].startswith("writer: champion inkling-2-mini/writer-v1@")
    assert lines[1].startswith("reviewer: champion claude-opus/reviewer-v1@")
    assert prompts.version("router-v1") in lines[2]
    assert prompts.version("clarify-v1") in lines[2]
    (prompts_copy / "writer-v1.md").unlink()
    assert "MISSING" in v.status_lines(store, prompts_dir=prompts_copy)[0]
