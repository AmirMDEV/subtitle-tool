from __future__ import annotations

from pathlib import Path

import pytest

from local_subtitle_stack.config import AppConfig, CachePaths, ModelConfig, ToolPaths, default_profiles
from local_subtitle_stack.domain import SceneContextBlock
from local_subtitle_stack.queue import QueueError, QueueStore


def build_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        config_path=str(tmp_path / "config.toml"),
        queue_root=str(tmp_path / "queue"),
        tools=ToolPaths(ffmpeg="ffmpeg", ffprobe="ffprobe", ollama="ollama", subtitle_edit="subtitle", python311="py311"),
        cache_paths=CachePaths(),
        models=ModelConfig(),
        profiles=default_profiles(),
    )


def test_enqueue_uses_video_job_manifest_name(tmp_path: Path) -> None:
    config = build_config(tmp_path)
    store = QueueStore(config)
    source = tmp_path / "sample video.mp4"
    source.write_text("video", encoding="utf-8")

    manifest = store.enqueue(
        source,
        profile="default",
        job_context="Appearance comparison discussion",
        scene_contexts=[
            SceneContextBlock(start_seconds=0.0, end_seconds=600.0, notes="Travel scene."),
        ],
    )
    job_dir, loaded = store.find_job(manifest.job_id)

    manifest_files = list(job_dir.glob("*.job.json"))
    assert len(manifest_files) == 1
    assert manifest_files[0].name == "sample video.job.json"
    assert loaded.artifacts["ja_srt"] == "sample video.ja.srt"
    assert loaded.export_dir == str(tmp_path / "sample video.mp4 subtitles")
    assert loaded.job_context == "Appearance comparison discussion"
    assert loaded.scene_contexts[0].notes == "Travel scene."


def test_resume_failed_job_moves_back_to_incoming(tmp_path: Path) -> None:
    config = build_config(tmp_path)
    store = QueueStore(config)
    source = tmp_path / "retry.mp4"
    source.write_text("video", encoding="utf-8")
    manifest = store.enqueue(source, profile="default")

    job_dir, manifest = store.claim_next_job()
    assert job_dir is not None
    failed_dir, _ = store.mark_failed(job_dir, manifest, "boom")
    assert failed_dir.parent.name == "failed"

    resumed_dir, resumed_manifest = store.resume_job(manifest.job_id)
    assert resumed_dir.parent.name == "incoming"
    assert resumed_manifest.status == "queued"


def test_resume_working_job_moves_back_to_incoming(tmp_path: Path) -> None:
    config = build_config(tmp_path)
    store = QueueStore(config)
    source = tmp_path / "resume-working.mp4"
    source.write_text("video", encoding="utf-8")
    manifest = store.enqueue(source, profile="default")

    working_dir, manifest = store.claim_next_job()
    assert working_dir is not None
    assert working_dir.parent.name == "working"

    resumed_dir, resumed_manifest = store.resume_job(manifest.job_id)

    assert resumed_dir.parent.name == "incoming"
    assert resumed_manifest.status == "queued"


def test_enqueue_uses_unique_job_ids_for_same_stem(tmp_path: Path) -> None:
    config = build_config(tmp_path)
    store = QueueStore(config)
    first_dir = tmp_path / "one"
    second_dir = tmp_path / "two"
    first_dir.mkdir()
    second_dir.mkdir()
    first = first_dir / "same.mp4"
    second = second_dir / "same.mp4"
    first.write_text("video", encoding="utf-8")
    second.write_text("video", encoding="utf-8")

    first_manifest = store.enqueue(first, profile="default")
    second_manifest = store.enqueue(second, profile="default")

    assert first_manifest.job_id != second_manifest.job_id


def test_resume_completed_job_raises(tmp_path: Path) -> None:
    config = build_config(tmp_path)
    store = QueueStore(config)
    source = tmp_path / "done.mp4"
    source.write_text("video", encoding="utf-8")
    manifest = store.enqueue(source, profile="default")
    job_dir, manifest = store.claim_next_job()
    assert job_dir is not None
    store.mark_completed(job_dir, manifest)

    with pytest.raises(QueueError):
        store.resume_job(manifest.job_id)
