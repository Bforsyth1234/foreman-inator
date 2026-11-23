"""Tests for paths.py and config.py. Keychain is always mocked; home is tmp_path."""

import os
import stat
from pathlib import Path

import pytest

from foreman_inator import config, paths
from foreman_inator.config import Config, ConfigError, ModelEndpoint


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class FakeKeyring:
    """In-memory stand-in for the keyring module's get/set functions."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.gets: list[tuple[str, str]] = []

    def set_password(self, service: str, name: str, value: str) -> None:
        self.store[(service, name)] = value

    def get_password(self, service: str, name: str) -> str | None:
        self.gets.append((service, name))
        return self.store.get((service, name))


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    """Never touch the real Keychain: replace the functions config.py calls."""
    fake = FakeKeyring()
    monkeypatch.setattr(config.keyring, "set_password", fake.set_password)
    monkeypatch.setattr(config.keyring, "get_password", fake.get_password)
    return fake


@pytest.fixture
def loose_umask():
    """A permissive umask must not leak into the modes; restore it afterwards."""
    old = os.umask(0o022)
    yield
    os.umask(old)


def _write(foreman_home: Path, text: str) -> Path:
    path = paths.config_path()
    path.write_text(text)
    return path


# --------------------------------------------------------------------------- paths


def test_home_is_env_override_not_real_home(foreman_home: Path) -> None:
    assert not foreman_home.exists()
    assert paths.home() == foreman_home.resolve()
    assert Path.home() / ".foreman-inator" != paths.home()


@pytest.mark.usefixtures("loose_umask")
def test_created_folders_are_mode_700(foreman_home: Path) -> None:
    dirs = [paths.home(), paths.traces_dir(), paths.recordings_dir(), paths.worktrees_dir()]
    for d in dirs:
        assert d.is_dir()
        assert _mode(d) == 0o700, d
    assert paths.db_path() == foreman_home.resolve() / "foreman-inator.db"
    assert paths.db_path().parent == paths.home()


def test_existing_loose_home_is_tightened(foreman_home: Path) -> None:
    foreman_home.mkdir(mode=0o755)
    os.chmod(foreman_home, 0o755)
    paths.home()
    assert _mode(foreman_home) == 0o700


@pytest.mark.usefixtures("loose_umask")
def test_app_pem_is_mode_600(foreman_home: Path) -> None:
    key = paths.write_app_key("-----BEGIN FAKE KEY-----\nabc\n-----END FAKE KEY-----\n")
    assert key == paths.app_key_path()
    assert key.name == "app.pem"
    assert _mode(key) == 0o600
    # Rewriting an existing, loosened file restores 600.
    os.chmod(key, 0o644)
    paths.write_app_key(b"new")
    assert _mode(key) == 0o600
    assert key.read_bytes() == b"new"


# --------------------------------------------------------------------------- defaults


def test_missing_file_gives_spec_defaults() -> None:
    cfg = config.load()
    assert cfg.poll_interval_seconds == 45
    assert [m.tier for m in cfg.models_for_tier(1)] == [1]
    assert cfg.models_for_tier(1)[0].kind == "local"
    assert cfg.models_for_tier(2)[0].kind == "local"
    assert [m.name for m in cfg.models_for_tier(3)] == ["glm", "claude-opus", "claude-fable"]
    assert all(m.kind == "cloud" and m.api_key_ref for m in cfg.models_for_tier(3))
    assert cfg.allowed_users == ()
    assert cfg.spend_caps.daily_usd > 0


def test_partial_file_fills_defaults(foreman_home: Path) -> None:
    _write(foreman_home, 'allowed_users = ["Brooks"]\n[spend_caps]\ndaily_usd = 2\n')
    cfg = config.load()
    assert cfg.allowed_users == ("Brooks",)
    assert cfg.is_allowed_user("brooks")
    assert not cfg.is_allowed_user("mallory")
    assert cfg.spend_caps.daily_usd == 2.0
    assert cfg.spend_caps.monthly_usd == config.SpendCaps().monthly_usd
    assert cfg.poll_interval_seconds == 45


# --------------------------------------------------------------------------- round trip


def test_save_load_round_trip(foreman_home: Path) -> None:
    cfg = Config(
        app_id=42,
        app_slug="foreman-inator-test",
        installation_ids=(7, 8),
        allowed_users=("alice",),
        repos=("alice/project",),
        poll_interval_seconds=30,
    )
    path = config.save(cfg)
    assert path == paths.config_path()
    assert _mode(path) == 0o600
    assert config.load() == cfg


# --------------------------------------------------------------------------- strictness


def test_custom_models_load(foreman_home: Path) -> None:
    _write(foreman_home, _models_toml())
    cfg = config.load()
    assert [m.name for m in cfg.models] == ["small", "normal", "glm"]
    assert cfg.model("glm").api_key_ref == "keychain:foreman-inator/glm"
    assert cfg.reviewer_for(cfg.model("small")) == cfg.model("glm")


def test_unknown_top_level_key_is_error(foreman_home: Path) -> None:
    _write(foreman_home, "poll_interval = 45\n")
    with pytest.raises(ConfigError, match="unknown key"):
        config.load()


def test_unknown_model_key_is_error(foreman_home: Path) -> None:
    _write(
        foreman_home,
        _models_toml(extra="temperature = 0.2\n"),
    )
    with pytest.raises(ConfigError, match="unknown key.*temperature"):
        config.load()


def test_wrong_type_is_error(foreman_home: Path) -> None:
    _write(foreman_home, 'poll_interval_seconds = "45"\n')
    with pytest.raises(ConfigError, match="wrong type"):
        config.load()


def test_local_model_must_be_loopback(foreman_home: Path) -> None:
    _write(foreman_home, _models_toml(local_url="http://10.0.0.5:11434/v1"))
    with pytest.raises(ConfigError, match="localhost"):
        config.load()


def test_reviewer_independence_picks_other_family() -> None:
    cfg = Config()
    glm = cfg.model("glm")
    claude = cfg.model("claude-opus")
    fable = cfg.model("claude-fable")
    tier1, tier2 = cfg.models_for_tier(1)[0], cfg.models_for_tier(2)[0]
    assert cfg.reviewer_for(glm) == claude
    assert cfg.reviewer_for(claude) == glm
    assert cfg.reviewer_for(fable) == glm  # both Claude writers get the GLM reviewer
    assert cfg.reviewer_for(tier1) == claude
    assert cfg.reviewer_for(tier2) == claude
    assert tier1.family != tier2.family  # an escalation 1 -> 2 changes model family
    only_claude = Config(reviewer_models=("claude-opus",))
    assert only_claude.reviewer_for(claude) is None


# --------------------------------------------------------------------------- secrets


def test_plain_text_api_key_is_rejected(foreman_home: Path) -> None:
    _write(foreman_home, _models_toml(extra='api_key = "abc123"\n'))
    with pytest.raises(ConfigError, match="fman config set-key"):
        config.load()


def test_plain_text_key_in_key_ref_is_rejected(foreman_home: Path) -> None:
    _write(foreman_home, _models_toml(cloud_ref="sk-ant-0123456789"))
    with pytest.raises(ConfigError, match="fman config set-key"):
        config.load()


def test_set_key_stores_in_keychain_and_resolves(fake_keyring: FakeKeyring) -> None:
    ref = config.set_key("glm", "fake-generated-key")
    assert ref == "keychain:foreman-inator/glm"
    assert fake_keyring.store == {("foreman-inator", "glm"): "fake-generated-key"}
    assert Config().model("glm").api_key() == "fake-generated-key"


def test_keys_resolve_lazily(fake_keyring: FakeKeyring, foreman_home: Path) -> None:
    config.save(Config())
    cfg = config.load()
    assert fake_keyring.gets == []  # loading never touches the Keychain
    assert cfg.models_for_tier(1)[0].api_key() is None
    assert fake_keyring.gets == []  # nor does a local tier
    with pytest.raises(ConfigError, match="fman config set-key anthropic"):
        cfg.model("claude-opus").api_key()
    assert fake_keyring.gets == [("foreman-inator", "anthropic")]


def test_bad_key_name_rejected() -> None:
    with pytest.raises(ConfigError, match="invalid key name"):
        config.set_key("../etc", "x")


def test_model_endpoint_requires_ref_for_cloud() -> None:
    bad = ModelEndpoint("c", 3, "cloud", "m", "https://api.example.com/v1", "x")
    with pytest.raises(ConfigError, match="needs api_key_ref"):
        Config(models=(*config.default_models(), bad))


# --------------------------------------------------------------------------- helpers


def _models_toml(
    *,
    extra: str = "",
    local_url: str = "http://127.0.0.1:11434/v1",
    cloud_ref: str = "keychain:foreman-inator/glm",
) -> str:
    return f"""
reviewer_models = ["glm"]

[[models]]
name = "small"
tier = 1
kind = "local"
model = "qwen3.6:35b-a3b"
base_url = "{local_url}"
family = "qwen"

[[models]]
name = "normal"
tier = 2
kind = "local"
model = "qwen3.8:27b"
base_url = "http://localhost:11434/v1"
family = "qwen"

[[models]]
name = "glm"
tier = 3
kind = "cloud"
model = "glm-4.6"
base_url = "https://api.z.ai/api/coding/paas/v4"
family = "glm"
api_key_ref = "{cloud_ref}"
{extra}"""
