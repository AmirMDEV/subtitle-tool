from __future__ import annotations

import json
from pathlib import Path

import pytest

from local_subtitle_stack.config import AppConfig, CachePaths, ModelConfig, ToolPaths, default_profiles
from local_subtitle_stack.domain import ChunkPlan, Cue, SceneContextBlock
from local_subtitle_stack.guards import ResourceSnapshot
from local_subtitle_stack.integrations import SubtitleEditClient
from local_subtitle_stack.queue import QueueError, QueueStore
from local_subtitle_stack.service import WorkerService


class FakeFFmpeg:
    def create_chunk_plan(self, source_path: Path, chunks_dir: Path, chunk_seconds: int, overlap_seconds: int) -> list[ChunkPlan]:
        chunks_dir.mkdir(parents=True, exist_ok=True)
        chunk_path = chunks_dir / "chunk_0001.wav"
        chunk_path.write_text("chunk", encoding="utf-8")
        return [ChunkPlan(index=1, start=0.0, end=12.0, path=str(chunk_path))]


class FakeSubtitleEdit(SubtitleEditClient):
    def __init__(self) -> None:
        self.opened: list[Path] = []

    def open_files(self, paths: list[Path]) -> None:
        self.opened = paths


class FakeASR:
    def __init__(self, model_id: str, cache_dir: str | None = None) -> None:
        self.model_id = model_id
        self.cache_dir = cache_dir

    def transcribe_chunk(self, chunk_path: Path, batch_size: int, device: str) -> list[Cue]:
        return [
            Cue(index=1, start=0.0, end=1.2, text="motto shite"),
            Cue(index=2, start=1.6, end=3.1, text="onegai"),
        ]

    def close(self) -> None:
        return None


class SuccessfulOllama:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def generate_json(self, model: str, prompt: str, temperature: float) -> dict[str, list[str]]:
        self.calls.append((model, prompt))
        count = prompt.count('"index":')
        prefix = "adapted" if '"literal_en"' in prompt else "literal"
        return {"translations": [f"{prefix} line {index + 1}" for index in range(count)]}


class AdaptedFallbackOllama(SuccessfulOllama):
    def generate_json(self, model: str, prompt: str, temperature: float) -> dict[str, list[str]]:
        if '"literal_en"' in prompt:
            self.calls.append((model, prompt))
            return {"translations": [""]}
        return super().generate_json(model, prompt, temperature)


class BadLiteralOllama(SuccessfulOllama):
    def generate_json(self, model: str, prompt: str, temperature: float) -> dict[str, list[str]]:
        if '"literal_en"' in prompt:
            return super().generate_json(model, prompt, temperature)
        return {"translations": [""]}


class RetryableJsonOllama(SuccessfulOllama):
    def __init__(self) -> None:
        super().__init__()
        self.fail_once = True

    def generate_json(self, model: str, prompt: str, temperature: float) -> dict[str, list[str]]:
        if self.fail_once:
            self.fail_once = False
            raise json.JSONDecodeError("bad json", "{", 1)
        return super().generate_json(model, prompt, temperature)


def build_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        config_path=str(tmp_path / "config.toml"),
        queue_root=str(tmp_path / "queue"),
        tools=ToolPaths(ffmpeg="ffmpeg", ffprobe="ffprobe", ollama="ollama", subtitle_edit="subtitle", python311="py311"),
        cache_paths=CachePaths(),
        models=ModelConfig(),
        profiles=default_profiles(),
    )


def patch_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("local_subtitle_stack.service.TransformersASRClient", FakeASR)
    monkeypatch.setattr("local_subtitle_stack.service.choose_device", lambda _min: "cpu")
    monkeypatch.setattr("local_subtitle_stack.service.ensure_safe_to_start_job", lambda *args, **kwargs: None)
    monkeypatch.setattr("local_subtitle_stack.service.ensure_safe_to_start_gpu_phase", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "local_subtitle_stack.service.capture_snapshot",
        lambda: ResourceSnapshot(free_ram_mb=12_000, process_rss_mb=512, gpu_free_mb=0, gpu_total_mb=0),
    )


def build_service(tmp_path: Path, ollama: SuccessfulOllama) -> WorkerService:
    config = build_config(tmp_path)
    store = QueueStore(config)
    return WorkerService(
        config=config,
        store=store,
        ffmpeg=FakeFFmpeg(),
        subtitle_edit=FakeSubtitleEdit(),
        ollama=ollama,
    )


def test_worker_creates_local_and_exported_outputs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    patch_runtime(monkeypatch)
    service = build_service(tmp_path, SuccessfulOllama())
    source = tmp_path / "scene.mp4"
    source.write_text("video", encoding="utf-8")
    manifest = service.enqueue(source, profile="default")

    service.run_until_empty()

    job_dir, loaded = service.store.find_job(manifest.job_id)
    output_dir = Path(loaded.export_dir)
    assert job_dir.parent.name == "done"
    assert (job_dir / loaded.artifacts["ja_srt"]).exists()
    assert (job_dir / loaded.artifacts["literal_srt"]).exists()
    assert (job_dir / loaded.artifacts["adapted_srt"]).exists()
    assert (job_dir / loaded.artifacts["review"]).exists()
    assert output_dir == tmp_path / "scene.mp4 subtitles"
    assert (output_dir / loaded.artifacts["ja_srt"]).exists()
    assert (output_dir / loaded.artifacts["literal_srt"]).exists()
    assert (output_dir / loaded.artifacts["adapted_srt"]).exists()
    assert (output_dir / loaded.artifacts["review"]).exists()


def test_open_review_prefers_exported_outputs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    patch_runtime(monkeypatch)
    service = build_service(tmp_path, SuccessfulOllama())
    source = tmp_path / "review.mp4"
    source.write_text("video", encoding="utf-8")
    manifest = service.enqueue(source, profile="default")

    service.run_until_empty()

    paths = service.open_review(manifest.job_id)
    output_dir = tmp_path / "review.mp4 subtitles"
    assert paths == [
        output_dir / "review.ja.srt",
        output_dir / "review.en.literal.srt",
        output_dir / "review.en.adapted.srt",
    ]
    assert service.subtitle_edit.opened == paths


def test_adapted_translation_uses_context_and_falls_back_to_literal_and_marks_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    patch_runtime(monkeypatch)
    ollama = AdaptedFallbackOllama()
    service = build_service(tmp_path, ollama)
    source = tmp_path / "fallback.mp4"
    source.write_text("video", encoding="utf-8")
    manifest = service.enqueue(
        source,
        profile="default",
        context="The scene compares appearance and family resemblance.",
        scene_contexts=[
            SceneContextBlock(start_seconds=0.0, end_seconds=10.0, notes="Travel conversation."),
        ],
    )

    service.run_until_empty()

    _job_dir, loaded = service.store.find_job(manifest.job_id)
    output_dir = Path(loaded.export_dir)
    literal = (output_dir / loaded.artifacts["literal_srt"]).read_text(encoding="utf-8")
    adapted = (output_dir / loaded.artifacts["adapted_srt"]).read_text(encoding="utf-8")
    review = (output_dir / loaded.artifacts["review"]).read_text(encoding="utf-8")
    assert literal == adapted
    assert "translation-fallback" in review
    assert any("appearance and family resemblance" in prompt for _model, prompt in ollama.calls if '"literal_en"' in prompt)
    assert any("Travel conversation." in prompt for _model, prompt in ollama.calls if '"literal_en"' in prompt)


def test_enqueue_folder_only_queues_video_files_and_skips_duplicates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    patch_runtime(monkeypatch)
    service = build_service(tmp_path, SuccessfulOllama())
    source_dir = tmp_path / "folder"
    source_dir.mkdir()
    (source_dir / "one.mp4").write_text("video", encoding="utf-8")
    (source_dir / "two.mkv").write_text("video", encoding="utf-8")
    (source_dir / "notes.txt").write_text("ignore", encoding="utf-8")
    nested = source_dir / "nested"
    nested.mkdir()
    (nested / "three.mp4").write_text("video", encoding="utf-8")

    manifests, skipped = service.enqueue_folder(source_dir, profile="default")
    assert [manifest.source_name for manifest in manifests] == ["one.mp4", "two.mkv"]
    assert skipped == []

    recursive_manifests, recursive_skipped = service.enqueue_folder(
        source_dir,
        profile="default",
        recursive=True,
    )
    assert [manifest.source_name for manifest in recursive_manifests] == ["three.mp4"]
    assert len(recursive_skipped) == 2

    manifests, skipped = service.enqueue_folder(source_dir, profile="default")
    assert manifests == []
    assert len(skipped) == 2


def test_open_output_folder_prefers_export_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    patch_runtime(monkeypatch)
    service = build_service(tmp_path, SuccessfulOllama())
    source = tmp_path / "open-output.mp4"
    source.write_text("video", encoding="utf-8")
    manifest = service.enqueue(source, profile="default")
    opened: list[list[str]] = []
    monkeypatch.setattr(
        "local_subtitle_stack.service.subprocess.Popen",
        lambda args: opened.append(args),
    )

    service.run_until_empty()

    output_dir = service.open_output_folder(manifest.job_id)
    assert output_dir == tmp_path / "open-output.mp4 subtitles"
    assert opened == [["explorer", str(output_dir)]]


def test_literal_failure_requeues_then_fails_on_second_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    patch_runtime(monkeypatch)
    service = build_service(tmp_path, BadLiteralOllama())
    source = tmp_path / "retry-literal.mp4"
    source.write_text("video", encoding="utf-8")
    manifest = service.enqueue(source, profile="default")

    with pytest.raises(QueueError):
        service.run_until_empty()

    job_dir, loaded = service.store.find_job(manifest.job_id)
    assert job_dir.parent.name == "incoming"
    assert loaded.checkpoint("translate_literal").attempts == 1

    with pytest.raises(QueueError):
        service.run_until_empty()

    job_dir, loaded = service.store.find_job(manifest.job_id)
    assert job_dir.parent.name == "failed"
    assert loaded.checkpoint("translate_literal").attempts == 2


def test_job_start_floor_switches_after_transcribe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    patch_runtime(monkeypatch)
    service = build_service(tmp_path, SuccessfulOllama())
    source = tmp_path / "floor-switch.mp4"
    source.write_text("video", encoding="utf-8")
    manifest = service.enqueue(source, profile="conservative")
    profile = service.config.profile("conservative")

    assert service._job_start_min_free_ram(manifest, profile) == profile.min_free_ram_mb

    manifest.checkpoint("transcribe").status = "completed"

    assert (
        service._job_start_min_free_ram(manifest, profile)
        == profile.min_free_ram_translation_mb
    )


def test_invalid_json_translation_retries_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    patch_runtime(monkeypatch)
    ollama = RetryableJsonOllama()
    service = build_service(tmp_path, ollama)
    source = tmp_path / "retry-json.mp4"
    source.write_text("video", encoding="utf-8")

    service.enqueue(source, profile="default")
    service.run_until_empty()

    assert len(ollama.calls) >= 2


def test_preview_rows_and_rebuild_english_use_saved_notes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    patch_runtime(monkeypatch)
    ollama = SuccessfulOllama()
    service = build_service(tmp_path, ollama)
    source = tmp_path / "preview.mp4"
    source.write_text("video", encoding="utf-8")
    manifest = service.enqueue(source, profile="default")
    service.run_until_empty()

    preview = service.preview_rows(manifest.job_id)
    assert len(preview) == 2
    assert preview[0]["japanese"] == "motto shite"
    assert preview[0]["literal_english"] == "literal line 1"
    assert preview[0]["adapted_english"] == "adapted line 1"

    ollama.calls.clear()
    service.rebuild_english(
        manifest.job_id,
        batch_label="Batch A",
        overall_context="Whole video is about appearance comparison and tone.",
        scene_contexts=[
            SceneContextBlock(start_seconds=0.0, end_seconds=10.0, notes="Travel talk about family resemblance."),
        ],
    )

    _job_dir, loaded = service.load_job(manifest.job_id)
    assert loaded.series == "Batch A"
    assert loaded.scene_contexts[0].notes == "Travel talk about family resemblance."
    assert any("Whole video is about appearance comparison and tone." in prompt for _model, prompt in ollama.calls)
    assert any("Travel talk about family resemblance." in prompt for _model, prompt in ollama.calls)
