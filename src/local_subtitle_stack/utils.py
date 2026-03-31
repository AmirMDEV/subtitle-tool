from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".m4v", ".webm"}


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding=encoding,
            dir=path.parent,
            prefix=f"{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(content)
            tmp_path = Path(handle.name)

        delay = 0.05
        for attempt in range(8):
            try:
                os.replace(tmp_path, path)
                return
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 0.5)
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def safe_slug(text: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", text.strip())
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value or "job"


def split_text_lines(text: str, max_chars: int) -> str:
    words = text.split()
    if not words or len(text) <= max_chars:
        return text.strip()

    lines: list[str] = []
    current: list[str] = []
    current_len = 0
    for word in words:
        projected = current_len + len(word) + (1 if current else 0)
        if current and projected > max_chars and len(lines) < 1:
            lines.append(" ".join(current))
            current = [word]
            current_len = len(word)
        else:
            current.append(word)
            current_len = projected
    if current:
        lines.append(" ".join(current))
    return "\n".join(lines[:2]).strip()


def is_video_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS


def list_video_sources(folder: Path, recursive: bool = False) -> list[Path]:
    iterator = folder.rglob("*") if recursive else folder.iterdir()
    return sorted(path for path in iterator if is_video_file(path))


def subtitle_output_dir(source: Path) -> Path:
    return source.parent / f"{source.name} subtitles"


def parse_timecode(value: str) -> float:
    text = value.strip()
    if not text:
        raise ValueError("Time value cannot be blank.")
    parts = text.split(":")
    if len(parts) not in {2, 3}:
        raise ValueError(f"Unsupported time format: {value}")
    try:
        if len(parts) == 2:
            minutes = int(parts[0])
            seconds = float(parts[1])
            return minutes * 60 + seconds
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = float(parts[2])
        return hours * 3600 + minutes * 60 + seconds
    except ValueError as exc:
        raise ValueError(f"Unsupported time format: {value}") from exc


def format_timecode(value: float) -> str:
    total_seconds = max(float(value), 0.0)
    hours = int(total_seconds // 3600)
    total_seconds -= hours * 3600
    minutes = int(total_seconds // 60)
    total_seconds -= minutes * 60
    seconds = int(total_seconds)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def no_window_creationflags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
