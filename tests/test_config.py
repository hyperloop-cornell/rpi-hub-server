"""Test configuration loading, profiles and environment substitution."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.config import (
    APIConfig,
    HubConfig,
    Settings,
    deep_merge,
    get_settings,
    reload_settings,
)

REPO_CONFIG = Path(__file__).parent.parent / "config" / "config.yaml"


def test_default_hub_config():
    config = HubConfig()
    assert config.hub_id == "rpi-bridge-01"
    assert config.reconnect_interval == 5
    assert config.max_reconnect_attempts == 0  # never give up
    assert config.mode == "bench"


def test_pod_mode_is_rejected_until_implemented():
    with pytest.raises(ValidationError):
        HubConfig(mode="pod")


def test_default_api_binds_loopback():
    assert APIConfig().host == "127.0.0.1"


def test_repo_config_defaults(monkeypatch):
    for var in ("HUB_ID", "API_HOST", "BENCH_SOURCE", "HUB_PROFILE", "MAX_RECONNECT_ATTEMPTS"):
        monkeypatch.delenv(var, raising=False)
    settings = Settings.load_from_yaml(REPO_CONFIG, profile="")
    assert settings.hub.profile_name == ""
    assert settings.api.host == "127.0.0.1"
    assert settings.bench.source == "usb"
    assert settings.auto_connect_policy == "known_boards"
    assert settings.bench.boards_file == "config/boards.yaml"
    assert settings.hub.max_reconnect_attempts == 0


@pytest.mark.parametrize("profile,source", [("lab-hub", "usb"), ("cellular-hub", "usb"), ("dev-sim", "sim")])
def test_repo_profiles_load(profile, source, monkeypatch):
    monkeypatch.delenv("BENCH_SOURCE", raising=False)
    settings = Settings.load_from_yaml(REPO_CONFIG, profile=profile)
    assert settings.hub.profile_name == profile
    assert settings.bench.source == source
    assert settings.hub.mode == "bench"


def test_profile_overlays_base(tmp_path):
    base = tmp_path / "config.yaml"
    base.write_text("hub:\n  hub_id: base-hub\n  reconnect_interval: 5\nbuffer:\n  size_mb: 10\n", encoding="utf-8")
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "field.yaml").write_text("buffer:\n  size_mb: 25\nbench:\n  source: sim\n", encoding="utf-8")

    settings = Settings.load_from_yaml(base, profile="field")
    assert settings.hub.hub_id == "base-hub"  # untouched keys survive
    assert settings.buffer.size_mb == 25
    assert settings.bench.source == "sim"
    assert settings.hub.profile_name == "field"


def test_profile_given_as_path(tmp_path):
    base = tmp_path / "config.yaml"
    base.write_text("hub:\n  hub_id: base-hub\n", encoding="utf-8")
    custom = tmp_path / "custom.yaml"
    custom.write_text("hub:\n  hub_id: custom-hub\n", encoding="utf-8")
    assert Settings.load_from_yaml(base, profile=str(custom)).hub.hub_id == "custom-hub"


def test_unknown_profile_lists_available(tmp_path):
    base = tmp_path / "config.yaml"
    base.write_text("{}\n", encoding="utf-8")
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "lab-hub.yaml").write_text("{}\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="lab-hub"):
        Settings.load_from_yaml(base, profile="nope")


def test_env_substitution_in_base_and_profile(tmp_path, monkeypatch):
    base = tmp_path / "config.yaml"
    base.write_text('hub:\n  hub_id: ${HUB_ID:default-hub}\n  device_token: "${DEVICE_TOKEN:}"\n', encoding="utf-8")
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "p.yaml").write_text("api:\n  port: ${API_PORT:8000}\n", encoding="utf-8")

    monkeypatch.setenv("HUB_ID", "env-hub")
    monkeypatch.setenv("API_PORT", "9001")
    monkeypatch.delenv("DEVICE_TOKEN", raising=False)
    settings = Settings.load_from_yaml(base, profile="p")
    assert settings.hub.hub_id == "env-hub"
    assert settings.hub.device_token == ""
    assert settings.api.port == 9001


def test_hub_profile_env_var(tmp_path, monkeypatch):
    base = tmp_path / "config.yaml"
    base.write_text("hub:\n  hub_id: x\n", encoding="utf-8")
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "lab-hub.yaml").write_text("bench:\n  auto_connect: known_boards\n", encoding="utf-8")
    monkeypatch.setenv("HUB_PROFILE", "lab-hub")
    settings = Settings.load_from_yaml(base)
    assert settings.hub.profile_name == "lab-hub"
    assert settings.auto_connect_policy == "known_boards"


def test_serial_auto_connect_false_disables_policy():
    settings = Settings(serial={"auto_connect": False}, bench={"auto_connect": "all"})
    assert settings.auto_connect_policy == "none"


def test_settings_missing_yaml(tmp_path, monkeypatch):
    monkeypatch.delenv("HUB_PROFILE", raising=False)
    settings = Settings.load_from_yaml(tmp_path / "missing.yaml")
    assert settings.hub.hub_id == "rpi-bridge-01"


def test_deep_merge_replaces_lists():
    merged = deep_merge({"a": {"b": 1, "c": [1, 2]}, "d": 1}, {"a": {"c": [3]}, "e": 2})
    assert merged == {"a": {"b": 1, "c": [3]}, "d": 1, "e": 2}


def test_get_settings_singleton_and_reload(tmp_path, monkeypatch):
    monkeypatch.delenv("HUB_PROFILE", raising=False)
    assert get_settings() is get_settings()
    config = tmp_path / "config.yaml"
    config.write_text("hub:\n  hub_id: reloaded\n", encoding="utf-8")
    assert reload_settings(config).hub.hub_id == "reloaded"
    assert get_settings().hub.hub_id == "reloaded"
    reload_settings(REPO_CONFIG)
