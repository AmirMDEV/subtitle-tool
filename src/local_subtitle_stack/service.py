from __future__ import annotations

import subprocess
from pathlib import Path

from .config import AppConfig
from .domain import (
    JOB_STATUS_COMPLETED,
    STAGE_ADAPTED,
    STAGE_EXTRACT,
    STAGE_FINALIZE,
    STAGE_LITERAL,
    STAGE_TRANSCRIBE,
    Cue,
    JobManifest,
    ReviewFlag,
    SceneContextBlock,
)
from .guards import (
    capture_snapshot,
    choose_device,
    ensure_safe_to_start_gpu_phase,
    ensure_safe_to_start_job,
)
from .integrations import (
    FFmpegClient,
    OllamaClient,
    SubtitleEditClient,
    TransformersASRClient,
    load_cues,
    save_cues,
)
from .pipeline import (
    apply_translations,
    build_context_notes,
    build_adapted_prompt,
    build_literal_prompt,
    build_literal_prompt_with_context,
    combine_chunk_cues,
    cue_groups,
    load_glossary,
    metadata_from_manifest,
    normalize_japanese_cues,
    strict_retry_prompt,
    validate_translation_payload,
    write_review_flags,
    write_srt,
)
from .queue import QueueError, QueueStore
from .utils import atomic_write_json, atomic_write_text, list_video_sources, read_json, subtitle_output_dir


class PauseRequested(RuntimeError):
    pass


class WorkerService:
    def __init__(
        self,
        config: AppConfig,
        store: QueueStore,
        ffmpeg: FFmpegClient,
        subtitle_edit: SubtitleEditClient,
        ollama: OllamaClient,
    ) -> None:
        self.config = config
        self.store = store
        self.ffmpeg = ffmpeg
        self.subtitle_edit = subtitle_edit
        self.ollama = ollama

    def enqueue(
        self,
        source: Path,
        profile: str,
        glossary: Path | None = None,
        series: str | None = None,
        context: str | None = None,
        scene_contexts: list[SceneContextBlock] | None = None,
    ) -> JobManifest:
        return self.store.enqueue(
            source_path=source,
            profile=profile,
            glossary_path=glossary,
            series=series,
            job_context=context,
            scene_contexts=scene_contexts,
        )

    def enqueue_many(
        self,
        sources: list[Path],
        profile: str,
        glossary: Path | None = None,
        series: str | None = None,
        context: str | None = None,
        scene_contexts: list[SceneContextBlock] | None = None,
    ) -> tuple[list[JobManifest], list[Path]]:
        manifests: list[JobManifest] = []
        skipped: list[Path] = []
        existing = {
            self._source_key(Path(manifest.source_path))
            for _job_dir, manifest, _state in self.store.list_jobs()
        }
        seen: set[str] = set()
        for source in sources:
            resolved = source.resolve()
            key = self._source_key(resolved)
            if key in seen or key in existing:
                skipped.append(resolved)
                continue
            seen.add(key)
            existing.add(key)
            manifests.append(
                self.enqueue(
                    source=resolved,
                    profile=profile,
                    glossary=glossary,
                    series=series,
                    context=context,
                    scene_contexts=scene_contexts,
                )
            )
        return manifests, skipped

    def enqueue_folder(
        self,
        folder: Path,
        profile: str,
        glossary: Path | None = None,
        series: str | None = None,
        context: str | None = None,
        scene_contexts: list[SceneContextBlock] | None = None,
        recursive: bool = False,
    ) -> tuple[list[JobManifest], list[Path]]:
        if not folder.exists():
            raise QueueError(f"Folder not found: {folder}")
        if not folder.is_dir():
            raise QueueError(f"Folder path is not a directory: {folder}")
        return self.enqueue_many(
            list_video_sources(folder, recursive=recursive),
            profile=profile,
            glossary=glossary,
            series=series,
            context=context,
            scene_contexts=scene_contexts,
        )

    def status_rows(self) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for _job_dir, manifest, state in self.store.list_jobs():
            rows.append(
                {
                    "job_id": manifest.job_id,
                    "state_dir": state,
                    "status": manifest.status,
                    "stage": manifest.current_stage,
                    "source": manifest.source_name,
                    "updated_at": manifest.updated_at,
                }
            )
        return rows

    def load_job(self, job_id: str) -> tuple[Path, JobManifest]:
        return self.store.find_job(job_id)

    def preview_rows(self, job_id: str) -> list[dict[str, str | float | int]]:
        job_dir, manifest = self.store.find_job(job_id)
        ja_cues = self._load_optional_cues(job_dir, manifest, "ja_cues")
        literal_cues = self._load_optional_cues(job_dir, manifest, "literal_cues")
        adapted_cues = self._load_optional_cues(job_dir, manifest, "adapted_cues")

        ja_by_index = {cue.index: cue for cue in ja_cues}
        literal_by_index = {cue.index: cue for cue in literal_cues}
        adapted_by_index = {cue.index: cue for cue in adapted_cues}
        indexes = sorted(set(ja_by_index) | set(literal_by_index) | set(adapted_by_index))

        rows: list[dict[str, str | float | int]] = []
        for cue_index in indexes:
            anchor = ja_by_index.get(cue_index) or literal_by_index.get(cue_index) or adapted_by_index[cue_index]
            rows.append(
                {
                    "cue_index": cue_index,
                    "start": anchor.start,
                    "end": anchor.end,
                    "japanese": ja_by_index.get(cue_index).text if cue_index in ja_by_index else "",
                    "literal_english": literal_by_index.get(cue_index).text if cue_index in literal_by_index else "",
                    "adapted_english": adapted_by_index.get(cue_index).text if cue_index in adapted_by_index else "",
                }
            )
        return rows

    def save_job_notes(
        self,
        job_id: str,
        *,
        batch_label: str | None,
        overall_context: str | None,
        scene_contexts: list[SceneContextBlock],
    ) -> JobManifest:
        job_dir, manifest = self.store.find_job(job_id)
        manifest.series = batch_label or None
        manifest.job_context = overall_context or None
        manifest.scene_contexts = list(scene_contexts)
        self._save_manifest(job_dir, manifest)
        return manifest

    def rebuild_english(
        self,
        job_id: str,
        *,
        batch_label: str | None,
        overall_context: str | None,
        scene_contexts: list[SceneContextBlock],
    ) -> JobManifest:
        job_dir, manifest = self.store.find_job(job_id)
        if not (job_dir / manifest.artifacts["ja_cues"]).exists():
            raise QueueError("Japanese subtitle lines are not ready yet. Start processing first.")

        manifest.series = batch_label or None
        manifest.job_context = overall_context or None
        manifest.scene_contexts = list(scene_contexts)
        manifest.review_flags = [
            flag for flag in manifest.review_flags if flag.stage not in {STAGE_LITERAL, STAGE_ADAPTED}
        ]
        for stage_name in (STAGE_LITERAL, STAGE_ADAPTED, STAGE_FINALIZE):
            checkpoint = manifest.checkpoint(stage_name)
            checkpoint.status = "pending"
            checkpoint.attempts = 0
            checkpoint.details = {}

        self._clear_translation_outputs(job_dir, manifest)
        self._save_manifest(job_dir, manifest)

        profile = self.config.profile(manifest.profile)
        ensure_safe_to_start_job(profile.min_free_ram_translation_mb, profile.max_rss_mb)
        self._stage_translate_literal(job_dir, manifest)
        self._stage_translate_adapted(job_dir, manifest)
        self._stage_finalize(job_dir, manifest)
        self._save_manifest(job_dir, manifest)
        return manifest

    def run_until_empty(self) -> None:
        with self.store.acquire_worker_lock():
            while True:
                claimed = self.store.claim_next_job()
                if not claimed:
                    return
                job_dir, manifest = claimed
                self._run_job(job_dir, manifest)

    def resume(self, job_id: str) -> JobManifest:
        _job_dir, manifest = self.store.resume_job(job_id)
        return manifest

    def open_review(self, job_id: str | None = None) -> list[Path]:
        job_dir, manifest = self._resolve_target_job(job_id)
        outputs = self._review_output_paths(job_dir, manifest)
        if not all(path.exists() for path in outputs):
            raise QueueError("Selected job does not have subtitle outputs yet.")
        self.subtitle_edit.open_files(outputs)
        return outputs

    def open_output_folder(self, job_id: str | None = None) -> Path:
        _job_dir, manifest = self._resolve_target_job(job_id)
        output_dir = self._output_dir_for_manifest(manifest)
        target = output_dir if output_dir.exists() else Path(manifest.source_path).parent
        subprocess.Popen(["explorer", str(target)])
        return target

    def _run_job(self, job_dir: Path, manifest: JobManifest) -> None:
        profile = self.config.profile(manifest.profile)
        try:
            ensure_safe_to_start_job(
                self._job_start_min_free_ram(manifest, profile),
                profile.max_rss_mb,
            )
            self._stage_extract(job_dir, manifest)
            self._stage_transcribe(job_dir, manifest)
            self._stage_translate_literal(job_dir, manifest)
            self._stage_translate_adapted(job_dir, manifest)
            self._stage_finalize(job_dir, manifest)
            self.store.mark_completed(job_dir, manifest)
        except PauseRequested:
            return
        except QueueError:
            raise
        except Exception as exc:
            self._handle_stage_failure(job_dir, manifest, exc)

    def _job_start_min_free_ram(self, manifest: JobManifest, profile) -> int:
        if manifest.checkpoint(STAGE_TRANSCRIBE).status == "completed":
            return profile.min_free_ram_translation_mb
        return profile.min_free_ram_mb

    def _should_pause(self, job_dir: Path, manifest: JobManifest) -> None:
        if self.store.pause_requested():
            self.store.mark_paused(job_dir, manifest)
            raise PauseRequested()

    def _update_metrics(self, manifest: JobManifest) -> None:
        snapshot = capture_snapshot()
        manifest.metrics.peak_rss_mb = max(manifest.metrics.peak_rss_mb, snapshot.process_rss_mb)
        manifest.metrics.peak_gpu_used_mb = max(manifest.metrics.peak_gpu_used_mb, snapshot.gpu_used_mb)
        manifest.metrics.last_seen_ram_available_mb = snapshot.free_ram_mb
        manifest.metrics.last_seen_gpu_free_mb = snapshot.gpu_free_mb

    def _save_manifest(self, job_dir: Path, manifest: JobManifest) -> None:
        self._update_metrics(manifest)
        self.store.save_manifest(job_dir, manifest)

    def _stage_extract(self, job_dir: Path, manifest: JobManifest) -> None:
        checkpoint = manifest.checkpoint(STAGE_EXTRACT)
        if checkpoint.status == "completed":
            return
        manifest.current_stage = STAGE_EXTRACT
        checkpoint.attempts += 1
        chunks_dir = job_dir / "chunks"
        profile = self.config.profile(manifest.profile)
        manifest.chunk_plan = self.ffmpeg.create_chunk_plan(
            source_path=Path(manifest.source_path),
            chunks_dir=chunks_dir,
            chunk_seconds=profile.chunk_seconds,
            overlap_seconds=profile.chunk_overlap_seconds,
        )
        checkpoint.status = "completed"
        checkpoint.details = {"chunk_count": len(manifest.chunk_plan)}
        self._save_manifest(job_dir, manifest)

    def _stage_transcribe(self, job_dir: Path, manifest: JobManifest) -> None:
        checkpoint = manifest.checkpoint(STAGE_TRANSCRIBE)
        if checkpoint.status == "completed":
            return
        self._should_pause(job_dir, manifest)

        manifest.current_stage = STAGE_TRANSCRIBE
        checkpoint.attempts += 1
        profile = self.config.profile(manifest.profile)
        device = choose_device(profile.min_free_vram_mb)
        if device == "cuda":
            ensure_safe_to_start_gpu_phase(profile.min_free_ram_mb, profile.min_free_vram_mb, profile.max_rss_mb)
        else:
            ensure_safe_to_start_job(profile.min_free_ram_mb, profile.max_rss_mb)

        transcript_dir = job_dir / "chunk-transcripts"
        transcript_dir.mkdir(parents=True, exist_ok=True)
        asr = TransformersASRClient(
            self.config.models.asr,
            cache_dir=self.config.cache_paths.hf_hub_cache or None,
        )
        all_chunk_cues: list[tuple[float, list[Cue]]] = []
        batch_size = profile.asr_batch_size
        try:
            for chunk in manifest.chunk_plan:
                self._should_pause(job_dir, manifest)
                transcript_path = transcript_dir / f"chunk_{chunk.index:04d}.json"
                if transcript_path.exists():
                    local_cues = load_cues(transcript_path)
                else:
                    try:
                        local_cues = asr.transcribe_chunk(Path(chunk.path), batch_size=batch_size, device=device)
                    except RuntimeError as exc:
                        if "out of memory" in str(exc).lower() and batch_size > 1:
                            batch_size = 1
                            local_cues = asr.transcribe_chunk(Path(chunk.path), batch_size=batch_size, device=device)
                        else:
                            raise
                    save_cues(transcript_path, local_cues)
                all_chunk_cues.append((chunk.start, local_cues))
                checkpoint.details["completed_chunks"] = chunk.index
                self._save_manifest(job_dir, manifest)
        finally:
            asr.close()

        merged = combine_chunk_cues(all_chunk_cues)
        normalized = normalize_japanese_cues(merged)
        ja_cues_path = job_dir / manifest.artifacts["ja_cues"]
        save_cues(ja_cues_path, normalized)
        write_srt(job_dir / manifest.artifacts["ja_srt"], normalized)
        checkpoint.status = "completed"
        self._save_manifest(job_dir, manifest)

    def _run_translation_prompt(self, model_name: str, prompt: str, expected_count: int, adapted: bool) -> list[str]:
        try:
            payload = self.ollama.generate_json(
                model=model_name,
                prompt=prompt,
                temperature=0.1 if not adapted else 0.3,
            )
            return validate_translation_payload(payload, expected_count=expected_count)
        except ValueError:
            retry_payload = self.ollama.generate_json(
                model=model_name,
                prompt=strict_retry_prompt(prompt),
                temperature=0.0 if not adapted else 0.2,
            )
            return validate_translation_payload(retry_payload, expected_count=expected_count)

    def _translate_stage(
        self,
        *,
        job_dir: Path,
        manifest: JobManifest,
        stage_name: str,
        model_name: str,
        output_artifact: str,
        output_srt_artifact: str,
        group_size: int,
        adapted: bool,
    ) -> None:
        checkpoint = manifest.checkpoint(stage_name)
        if checkpoint.status == "completed":
            return
        self._should_pause(job_dir, manifest)

        manifest.current_stage = stage_name
        checkpoint.attempts += 1
        profile = self.config.profile(manifest.profile)
        ensure_safe_to_start_job(profile.min_free_ram_translation_mb, profile.max_rss_mb)

        ja_cues = load_cues(job_dir / manifest.artifacts["ja_cues"])
        literal_cues = load_cues(job_dir / manifest.artifacts["literal_cues"]) if adapted else []
        glossary = load_glossary(manifest.glossary_path)
        metadata = metadata_from_manifest(manifest.source_name, manifest.series)
        groups = cue_groups(ja_cues, group_size)
        partial_path = job_dir / f"{output_artifact}.partial.json"
        partial_rows = read_json(partial_path, default=[]) or []
        translated_cues = [Cue.from_dict(item) for item in partial_rows]
        start_group = int(checkpoint.details.get("completed_groups", 0))

        for group_index in range(start_group, len(groups)):
            self._should_pause(job_dir, manifest)
            group = groups[group_index]
            if adapted:
                literal_group = literal_cues[group_index * group_size : group_index * group_size + len(group)]
                prev_context = ja_cues[max(0, group_index * group_size - 2) : group_index * group_size]
                next_context = ja_cues[
                    group_index * group_size + len(group) : group_index * group_size + len(group) + 2
                ]
                prompt = build_adapted_prompt(
                    group=group,
                    literal_group=literal_group,
                    prev_context=prev_context,
                    next_context=next_context,
                    glossary=glossary,
                    metadata=metadata,
                    context_notes=build_context_notes(
                        group=group,
                        global_context=manifest.job_context,
                        scene_contexts=manifest.scene_contexts,
                    ),
                )
            else:
                prompt = build_literal_prompt_with_context(
                    group=group,
                    glossary=glossary,
                    metadata=metadata,
                    context_notes=build_context_notes(
                        group=group,
                        global_context=manifest.job_context,
                        scene_contexts=manifest.scene_contexts,
                    ),
                )

            try:
                translations = self._run_translation_prompt(model_name, prompt, len(group), adapted=adapted)
            except Exception as exc:
                if adapted:
                    fallback = literal_cues[
                        group_index * group_size : group_index * group_size + len(group)
                    ]
                    translated_cues.extend(fallback)
                    manifest.review_flags.append(
                        ReviewFlag(
                            stage=stage_name,
                            group_index=group_index,
                            reason="translation-fallback",
                            detail=str(exc),
                        )
                    )
                else:
                    raise
            else:
                translated_cues.extend(apply_translations(group, translations))

            checkpoint.details["completed_groups"] = group_index + 1
            atomic_write_json(
                partial_path,
                [
                    {"index": cue.index, "start": cue.start, "end": cue.end, "text": cue.text}
                    for cue in translated_cues
                ],
            )
            self._save_manifest(job_dir, manifest)

        final_path = job_dir / manifest.artifacts[output_artifact]
        save_cues(final_path, translated_cues)
        write_srt(job_dir / manifest.artifacts[output_srt_artifact], translated_cues)
        checkpoint.status = "completed"
        self._save_manifest(job_dir, manifest)
        partial_path.unlink(missing_ok=True)

    def _stage_translate_literal(self, job_dir: Path, manifest: JobManifest) -> None:
        self._translate_stage(
            job_dir=job_dir,
            manifest=manifest,
            stage_name=STAGE_LITERAL,
            model_name=self.config.models.literal_translation,
            output_artifact="literal_cues",
            output_srt_artifact="literal_srt",
            group_size=self.config.profile(manifest.profile).translation_group_size,
            adapted=False,
        )

    def _stage_translate_adapted(self, job_dir: Path, manifest: JobManifest) -> None:
        self._translate_stage(
            job_dir=job_dir,
            manifest=manifest,
            stage_name=STAGE_ADAPTED,
            model_name=self.config.models.adapted_translation,
            output_artifact="adapted_cues",
            output_srt_artifact="adapted_srt",
            group_size=self.config.profile(manifest.profile).adapted_group_size,
            adapted=True,
        )

    def _stage_finalize(self, job_dir: Path, manifest: JobManifest) -> None:
        checkpoint = manifest.checkpoint(STAGE_FINALIZE)
        if checkpoint.status == "completed":
            return
        manifest.current_stage = STAGE_FINALIZE
        checkpoint.attempts += 1
        review_path = job_dir / manifest.artifacts["review"]
        write_review_flags(
            review_path,
            [
                {
                    "stage": flag.stage,
                    "group_index": flag.group_index,
                    "reason": flag.reason,
                    "detail": flag.detail,
                    "created_at": flag.created_at,
                }
                for flag in manifest.review_flags
            ],
        )
        output_dir = self._export_final_outputs(job_dir, manifest)
        checkpoint.status = "completed"
        checkpoint.details = {"export_dir": str(output_dir)}
        self._save_manifest(job_dir, manifest)

    def _handle_stage_failure(self, job_dir: Path, manifest: JobManifest, exc: Exception) -> None:
        checkpoint = manifest.checkpoint(manifest.current_stage)
        detail = f"{type(exc).__name__}: {exc}"
        if checkpoint.attempts >= 2:
            self.store.mark_failed(job_dir, manifest, detail)
            raise QueueError(detail)
        self.store.requeue_working(job_dir, manifest, detail)
        raise QueueError(detail)

    def _source_key(self, path: Path) -> str:
        return str(path.resolve()).casefold()

    def _resolve_target_job(self, job_id: str | None) -> tuple[Path, JobManifest]:
        if job_id:
            return self.store.find_job(job_id)
        completed = [
            (job_dir, manifest)
            for job_dir, manifest, state in self.store.list_jobs()
            if state == "done" and manifest.status == JOB_STATUS_COMPLETED
        ]
        if not completed:
            raise QueueError("No completed job found.")
        return completed[-1]

    def _output_dir_for_manifest(self, manifest: JobManifest) -> Path:
        if manifest.export_dir:
            return Path(manifest.export_dir)
        return subtitle_output_dir(Path(manifest.source_path))

    def _review_output_paths(self, job_dir: Path, manifest: JobManifest) -> list[Path]:
        export_dir = self._output_dir_for_manifest(manifest)
        exported = [
            export_dir / manifest.artifacts["ja_srt"],
            export_dir / manifest.artifacts["literal_srt"],
            export_dir / manifest.artifacts["adapted_srt"],
        ]
        if all(path.exists() for path in exported):
            return exported
        return [
            job_dir / manifest.artifacts["ja_srt"],
            job_dir / manifest.artifacts["literal_srt"],
            job_dir / manifest.artifacts["adapted_srt"],
        ]

    def _export_text_artifact(self, source_path: Path, target_path: Path) -> None:
        atomic_write_text(target_path, source_path.read_text(encoding="utf-8"))

    def _export_final_outputs(self, job_dir: Path, manifest: JobManifest) -> Path:
        output_dir = self._output_dir_for_manifest(manifest)
        for artifact in ("ja_srt", "literal_srt", "adapted_srt", "review"):
            source_path = job_dir / manifest.artifacts[artifact]
            target_path = output_dir / manifest.artifacts[artifact]
            self._export_text_artifact(source_path, target_path)
        return output_dir

    def _load_optional_cues(self, job_dir: Path, manifest: JobManifest, artifact_key: str) -> list[Cue]:
        path = job_dir / manifest.artifacts[artifact_key]
        if not path.exists():
            return []
        return load_cues(path)

    def _clear_translation_outputs(self, job_dir: Path, manifest: JobManifest) -> None:
        for filename in (
            "literal_cues.partial.json",
            "adapted_cues.partial.json",
        ):
            (job_dir / filename).unlink(missing_ok=True)

        for artifact in ("literal_cues", "adapted_cues", "literal_srt", "adapted_srt", "review"):
            local_path = job_dir / manifest.artifacts[artifact]
            local_path.unlink(missing_ok=True)
            export_path = self._output_dir_for_manifest(manifest) / manifest.artifacts[artifact]
            export_path.unlink(missing_ok=True)
