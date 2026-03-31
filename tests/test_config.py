from __future__ import annotations

from pathlib import Path

from local_subtitle_stack.config import AppConfig, load_config, save_config, default_profiles


def test_translation_floor_is_not_higher_than_asr_floor() -> None:
    for profile in default_profiles().values():
        assert profile.min_free_ram_translation_mb <= profile.min_free_ram_mb


def test_default_profile_is_conservative() -> None:
    config = AppConfig(config_path="config.toml", queue_root="queue")
    assert config.default_profile == "conservative"


def test_load_config_falls_back_to_legacy_path(tmp_path: Path, monkeypatch) -> None:
    default_path = tmp_path / "SubtitleTool" / "config.toml"
    legacy_path = tmp_path / "LocalSubtitleStack" / "config.toml"
    config = AppConfig(config_path=str(legacy_path), queue_root="queue")
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    save_config(config)
    monkeypatch.setattr("local_subtitle_stack.config.DEFAULT_CONFIG_PATH", default_path)
    monkeypatch.setattr("local_subtitle_stack.config.LEGACY_CONFIG_PATH", legacy_path)

    loaded = load_config()

    assert loaded.config_path == str(legacy_path)
