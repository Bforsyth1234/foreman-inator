"""The per-attempt OpenCode config: the provider package per wire (#52) and OpenCode's own
instruction-file loading switched off (#64).

Rule 12 (read 2026-11-25):
* https://opencode.ai/docs/rules: OpenCode loads ``AGENTS.md`` (else ``CLAUDE.md``) up
  from the project, ``~/.config/opencode/AGENTS.md`` and ``~/.claude/CLAUDE.md``; only
  env vars turn it off (``OPENCODE_DISABLE_PROJECT_CONFIG``, ``OPENCODE_DISABLE_CLAUDE_CODE``
  / ``_PROMPT``); ``instructions`` only adds files.
* The installed OpenCode 1.18.7 binary: its ``Instruction`` service checks
  ``OPENCODE_DISABLE_PROJECT_CONFIG`` before every project ``findUp`` (and the
  read-time nearby-``AGENTS.md`` lookup); ``disableClaudeCodePrompt`` is
  ``OPENCODE_DISABLE_CLAUDE_CODE || OPENCODE_DISABLE_CLAUDE_CODE_PROMPT``. Its
  ``@ai-sdk/anthropic`` model posts to ``{baseURL}/messages``.
* https://opencode.ai/docs/providers documents a newer ``providers``/``package`` schema;
  1.18.7 still takes the ``provider``/``npm``/``options`` one pinned below.
"""

from __future__ import annotations

from pathlib import Path

from foreman_inator.config import OLLAMA_URL, ModelEndpoint, default_models, key_ref
from foreman_inator.runner.opencode import PERMISSIONS, build_config, server_env

PROXY = "http://127.0.0.1:4100/a/9/v1"


def snapshot(npm: str, model: str, name: str) -> dict:
    ref = f"fman/{model}"
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "fman": {
                "npm": npm,
                "name": "foreman-inator proxy",
                "options": {"baseURL": PROXY, "apiKey": "fman-proxy"},
                "models": {model: {"name": name, "tool_call": True}},
            }
        },
        "model": ref,
        "small_model": ref,
        "enabled_providers": ["fman"],
        "share": "disabled",
        "autoupdate": False,
        "snapshot": False,
        "lsp": False,
        "formatter": False,
        "permission": PERMISSIONS,
    }


def test_local_and_glm_configs_are_unchanged():
    local = ModelEndpoint("qwen", 2, "local", "qwen3.8:27b", OLLAMA_URL, "qwen")
    glm = ModelEndpoint(
        "glm", 3, "cloud", "glm-5.3", "https://api.z.ai/api/coding/paas/v4", "glm", key_ref("glm")
    )
    assert build_config(local, PROXY) == snapshot(
        "@ai-sdk/openai-compatible", "qwen3.8:27b", "qwen"
    )
    assert build_config(glm, PROXY) == snapshot("@ai-sdk/openai-compatible", "glm-5.3", "glm")


def test_anthropic_wire_uses_the_anthropic_provider_at_the_proxy():
    opus = next(m for m in default_models() if m.name == "claude-opus")
    assert opus.wire == "anthropic"
    cfg = build_config(opus, PROXY + "/")
    assert cfg == snapshot("@ai-sdk/anthropic", "claude-opus-5-5", "claude-opus")
    # @ai-sdk/anthropic posts to {baseURL}/messages: the proxy's Messages route
    assert cfg["provider"]["fman"]["options"]["baseURL"] + "/messages" == PROXY + "/messages"


def test_opencode_never_loads_instruction_files_itself(tmp_path: Path):
    """#64: our prompt is the only source of AGENTS.md / CLAUDE.md for the writer."""
    for ep in default_models():
        assert "instructions" not in build_config(ep, PROXY)
    env = server_env(
        {"HOME": str(tmp_path), "PATH": "/usr/bin", "OPENCODE_DISABLE_PROJECT_CONFIG": "0"},
        config_file=tmp_path / "opencode.json",
        config_home=tmp_path / "config",
        data_home=tmp_path / "data",
        state_home=tmp_path / "state",
        cache_home=tmp_path / "cache",
        password="pw",
    )
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"  # project + nearby AGENTS.md
    assert env["OPENCODE_DISABLE_CLAUDE_CODE"] == "1"  # every CLAUDE.md
    # the global <config>/opencode/AGENTS.md lives in the driver's own config dir
    assert env["XDG_CONFIG_HOME"] == str(tmp_path / "config")
