from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import tomllib
import tomli_w

from .utils import atomic_write_text


LOCAL_APPDATA_ROOT = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
DEFAULT_QUEUE_ROOT = Path.home() / "Videos" / "Subtitle Queue"
DEFAULT_CONFIG_DIR = LOCAL_APPDATA_ROOT / "SubtitleTool"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.toml"
LEGACY_CONFIG_DIR = LOCAL_APPDATA_ROOT / "LocalSubtitleStack"
LEGACY_CONFIG_PATH = LEGACY_CONFIG_DIR / "config.toml"


@dataclass(slots=True)
class ToolPaths:
    ffmpeg: str = ""
    ffprobe: str = ""
    ollama: str = ""
    subtitle_edit: str = ""
    python311: str = ""


@dataclass(slots=True)
class CachePaths:
    hf_hub_cache: str = ""


@dataclass(slots=True)
class ModelConfig:
    asr: str = "kotoba-tech/kotoba-whisper-v1.1"
    literal_translation: str = "qwen3:4b-q8_0"
    adapted_translation: str = "qwen3:4b-q8_0"


@dataclass(slots=True)
class ProfileConfig:
    name: str
    chunk_seconds: int = 480
    chunk_overlap_seconds: int = 1
    asr_batch_size: int = 2
    translation_group_size: int = 8
    adapted_group_size: int = 8
    max_rss_mb: int = 16_384
    min_free_ram_mb: int = 8_192
    min_free_ram_translation_mb: int = 6_144
    min_free_vram_mb: int = 2_048
    max_gpu_use_mb: int = 6_656


@dataclass(slots=True)
class AppConfig:
    config_path: str
    queue_root: str
    default_profile: str = "conservative"
    tools: ToolPaths = field(default_factory=ToolPaths)
    cache_paths: CachePaths = field(default_factory=CachePaths)
    models: ModelConfig = field(default_factory=ModelConfig)
    profiles: dict[str, ProfileConfig] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.profiles:
            self.profiles = default_profiles()

    @property
    def queue_root_path(self) -> Path:
        return Path(self.queue_root)

    @property
    def config_file_path(self) -> Path:
        return Path(self.config_path)

    def profile(self, name: str | None = None) -> ProfileConfig:
        selected = name or self.default_profile
        return self.profiles[selected]

    def to_dict(self) -> dict[str, Any]:
        return {
            "queue_root": self.queue_root,
            "default_profile": self.default_profile,
            "tools": asdict(self.tools),
            "cache_paths": asdict(self.cache_paths),
            "models": asdict(self.models),
            "profiles": {key: asdict(value) for key, value in self.profiles.items()},
        }


def default_profiles() -> dict[str, ProfileConfig]:
    return {
        "default": ProfileConfig(name="default"),
        "conservative": ProfileConfig(
            name="conservative",
            asr_batch_size=1,
            translation_group_size=6,
            adapted_group_size=6,
            max_rss_mb=12_288,
            min_free_ram_mb=8_192,
            min_free_ram_translation_mb=6_144,
            min_free_vram_mb=2_560,
            max_gpu_use_mb=6_144,
        ),
    }


def detect_subtitle_edit() -> str:
    candidates = [
        Path(r"C:\Program Files\Subtitle Edit\SubtitleEdit.exe"),
        Path(r"C:\Program Files (x86)\Subtitle Edit\SubtitleEdit.exe"),
        Path.home() / "AppData" / "Local" / "Programs" / "Subtitle Edit" / "SubtitleEdit.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return ""


def detect_python311() -> str:
    try:
        completed = subprocess.run(
            ["py", "-3.11", "-c", "import sys; print(sys.executable)"],
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return ""


def detect_tool(name: str) -> str:
    return shutil.which(name) or ""


def default_config() -> AppConfig:
    return AppConfig(
        config_path=str(DEFAULT_CONFIG_PATH),
        queue_root=str(DEFAULT_QUEUE_ROOT),
        tools=ToolPaths(
            ffmpeg=detect_tool("ffmpeg"),
            ffprobe=detect_tool("ffprobe"),
            ollama=detect_tool("ollama"),
            subtitle_edit=detect_subtitle_edit(),
            python311=detect_python311(),
        ),
        cache_paths=CachePaths(),
        models=ModelConfig(),
        profiles=default_profiles(),
    )


def save_config(config: AppConfig) -> None:
    config.config_file_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(config.config_file_path, tomli_w.dumps(config.to_dict()))


def load_config(path: Path | None = None) -> AppConfig:
    if path is not None:
        config_path = path
    elif DEFAULT_CONFIG_PATH.exists():
        config_path = DEFAULT_CONFIG_PATH
    elif LEGACY_CONFIG_PATH.exists():
        config_path = LEGACY_CONFIG_PATH
    else:
        config_path = DEFAULT_CONFIG_PATH
    if not config_path.exists():
        config = default_config()
        config.config_path = str(config_path)
        save_config(config)
        return config

    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    profiles = {
        key: ProfileConfig(**value) for key, value in dict(data.get("profiles", {})).items()
    } or default_profiles()
    default_profile = str(data.get("default_profile", "conservative"))
    if default_profile == "default":
        default_profile = "conservative"
    if default_profile not in profiles:
        default_profile = "conservative"
    config = AppConfig(
        config_path=str(config_path),
        queue_root=str(data.get("queue_root", DEFAULT_QUEUE_ROOT)),
        default_profile=default_profile,
        tools=ToolPaths(**dict(data.get("tools", {}))),
        cache_paths=CachePaths(**dict(data.get("cache_paths", {}))),
        models=ModelConfig(**dict(data.get("models", {}))),
        profiles=profiles,
    )
    return config


def ensure_queue_directories(config: AppConfig) -> None:
    for name in ("incoming", "working", "done", "failed", "logs", "glossaries"):
        (config.queue_root_path / name).mkdir(parents=True, exist_ok=True)
