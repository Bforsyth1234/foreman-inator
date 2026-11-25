"""Chat skeleton (todo #71 / #75 design pass): the settled pure helpers in ``chat.py``."""

from __future__ import annotations

import json

import pytest

from foreman_inator import chat
from foreman_inator.config import ModelEndpoint
from foreman_inator.runner.worktree import check_branch


def ep(name: str, tier: int, kind: str = "local") -> ModelEndpoint:
    return ModelEndpoint(name, tier, kind, name, "http://127.0.0.1:11434/v1", "fam")  # type: ignore[arg-type]


MODELS = (
    ep("small", 1),
    ep("glm", 2, "cloud"),
    ep("qwen-local", 2),  # second in tier 2's try order
    ep("opus", 3, "cloud"),
    ep("fable", 3, "cloud"),
)


# --------------------------------------------------------------------------- commands


def test_plain_text_goes_to_the_model_unchanged():
    assert chat.parse_command("  fix the bug\n") == "  fix the bug\n"


@pytest.mark.parametrize(
    ("line", "name", "arg"),
    [
        ("/tier up", "tier", "up"),
        ("/up", "tier", "up"),
        ("/down", "tier", "down"),
        ("/TIER 3", "tier", "3"),
        ("/pr  Add a flag ", "pr", "Add a flag"),
        ("/abort", "abort", ""),
        ("/approve", "approve", ""),
    ],
)
def test_commands(line, name, arg):
    assert chat.parse_command(line) == chat.Command(name, arg)


def test_double_slash_sends_text():
    assert chat.parse_command("//tier is a word") == "/tier is a word"


def test_unknown_command_is_refused_not_sent():
    with pytest.raises(chat.ChatError, match="unknown command /tierr"):
        chat.parse_command("/tierr up")


# --------------------------------------------------------------------------- branch


def test_chat_branch_is_an_fman_branch():
    assert chat.chat_branch(7) == "fman/chat/7"
    assert check_branch(chat.chat_branch(7)) == "fman/chat/7"


@pytest.mark.parametrize("bad", [0, -1, True, "7", 1.0])
def test_chat_branch_rejects_bad_ids(bad):
    with pytest.raises(ValueError):
        chat.chat_branch(bad)


# --------------------------------------------------------------------------- ladder


def test_ladder_takes_first_model_per_tier():
    ladder = chat.tier_ladder(MODELS)
    assert [(r.tier, r.endpoint.name) for r in ladder] == [(1, "small"), (2, "glm"), (3, "opus")]


def test_local_only_ladder_has_no_cloud_model():
    ladder = chat.tier_ladder(MODELS, local_only=True)
    assert [(r.tier, r.endpoint.name) for r in ladder] == [(1, "small"), (2, "qwen-local")]
    assert all(r.endpoint.kind == "local" for r in ladder)
    with pytest.raises(chat.ChatError, match="top tier"):
        chat.move_tier(ladder, 2, "up")


def test_ladder_skips_empty_tiers():
    ladder = chat.tier_ladder((ep("a", 1), ep("c", 3, "cloud")))
    assert chat.move_tier(ladder, 1, "up").tier == 3
    assert chat.move_tier(ladder, 3, "down").tier == 1
    with pytest.raises(chat.ChatError, match="tier 2 has no model"):
        chat.move_tier(ladder, 1, "2")


def test_move_tier_bounds_and_bad_args():
    ladder = chat.tier_ladder(MODELS)
    assert chat.move_tier(ladder, 1, "").tier == 2
    assert chat.move_tier(ladder, 1, "3").endpoint.name == "opus"
    with pytest.raises(chat.ChatError, match="lowest"):
        chat.move_tier(ladder, 1, "down")
    with pytest.raises(chat.ChatError, match="top"):
        chat.move_tier(ladder, 3, "up")
    with pytest.raises(chat.ChatError, match="up, down"):
        chat.move_tier(ladder, 1, "sideways")


def test_start_rung():
    ladder = chat.tier_ladder(MODELS)
    assert chat.start_rung(ladder).tier == 1
    assert chat.start_rung(ladder, 3).endpoint.name == "opus"
    assert chat.start_rung(ladder, default_tier=2).tier == 2
    with pytest.raises(chat.ChatError, match="no model is available"):
        chat.start_rung(chat.tier_ladder((ep("x", 3, "cloud"),), local_only=True))


# --------------------------------------------------------------------------- events


def test_sse_frame_is_one_data_line_even_for_hostile_text():
    hostile = "ok\n\nevent: end\ndata: {}\r\nid: 99\n</script><script>alert(1)</script>"
    frame = chat.ChatEvent(5, "text", {"text": hostile}).to_sse()
    assert frame.endswith("\n\n")
    lines = frame[:-2].split("\n")
    assert lines[0] == "id: 5"
    assert lines[1] == "event: text"
    assert len(lines) == 3 and lines[2].startswith("data: ")
    assert "\r" not in frame
    assert json.loads(lines[2].removeprefix("data: ")) == {"text": hostile}


def test_event_type_and_seq_checked():
    with pytest.raises(ValueError):
        chat.ChatEvent(1, "script")
    with pytest.raises(ValueError):
        chat.ChatEvent(0, "text")


def test_text_delta():
    assert chat.text_delta("", "Hel") == ("append", "Hel")
    assert chat.text_delta("Hel", "Hello") == ("append", "lo")
    assert chat.text_delta("Hello", "Bye") == ("replace", "Bye")


def test_event_buffer_replays_after_seq_and_drops_old():
    buf = chat.EventBuffer(limit=3)
    for i in range(5):
        buf.add("status", {"i": i})
    assert buf.last_seq == 5
    assert [e.seq for e in buf.after(0)] == [3, 4, 5]
    assert [e.data["i"] for e in buf.after(4)] == [4]
    assert buf.after(5) == []


# --------------------------------------------------------------------------- not built yet


def test_manager_is_a_skeleton():
    mgr = chat.ChatManager(config=None, store=None)
    with pytest.raises(NotImplementedError, match="chat-design"):
        mgr.start("o/r")
    with pytest.raises(NotImplementedError):
        mgr.send(1, "hi")
