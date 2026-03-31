from __future__ import annotations

from .config import load_config
from .integrations import FFmpegClient, OllamaClient, SubtitleEditClient
from .queue import QueueStore
from .service import WorkerService


def build_service() -> WorkerService:
    config = load_config()
    store = QueueStore(config)
    ffmpeg = FFmpegClient(config.tools.ffmpeg, config.tools.ffprobe)
    subtitle_edit = SubtitleEditClient(config.tools.subtitle_edit)
    ollama = OllamaClient()
    return WorkerService(
        config=config,
        store=store,
        ffmpeg=ffmpeg,
        subtitle_edit=subtitle_edit,
        ollama=ollama,
    )
