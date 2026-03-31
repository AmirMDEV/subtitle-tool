from __future__ import annotations

import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

import psutil

from .config import AppConfig, ensure_queue_directories
from .domain import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PAUSED,
    JOB_STATUS_QUEUED,
    JOB_STATUS_WORKING,
    JobManifest,
    SceneContextBlock,
)
from .utils import atomic_write_json, now_iso, read_json, safe_slug, subtitle_output_dir


class QueueError(RuntimeError):
    pass


class QueueStore:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        ensure_queue_directories(config)
        self.root = config.queue_root_path
        self.lock_path = self.root / "worker.lock"
        self.pause_path = self.root / "pause.flag"

    @property
    def incoming_dir(self) -> Path:
        return self.root / "incoming"

    @property
    def working_dir(self) -> Path:
        return self.root / "working"

    @property
    def done_dir(self) -> Path:
        return self.root / "done"

    @property
    def failed_dir(self) -> Path:
        return self.root / "failed"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    def pause_requested(self) -> bool:
        return self.pause_path.exists()

    def set_pause(self, paused: bool) -> None:
        if paused:
            self.pause_path.write_text(now_iso(), encoding="utf-8")
        else:
            self.pause_path.unlink(missing_ok=True)

    @contextmanager
    def acquire_worker_lock(self) -> Iterator[None]:
        if self.lock_path.exists():
            data = read_json(self.lock_path, default={}) or {}
            pid = int(data.get("pid", 0) or 0)
            if pid and psutil.pid_exists(pid):
                raise QueueError(f"Worker already running with pid {pid}.")
            self.lock_path.unlink(missing_ok=True)

        atomic_write_json(self.lock_path, {"pid": os.getpid(), "created_at": now_iso()})
        try:
            yield
        finally:
            self.lock_path.unlink(missing_ok=True)

    def _manifest_path(self, job_dir: Path) -> Path:
        matches = sorted(job_dir.glob("*.job.json"))
        if matches:
            return matches[0]
        return job_dir / "job.json"

    def _job_dirs(self, parent: Path) -> list[Path]:
        return sorted(
            [path for path in parent.iterdir() if path.is_dir()],
            key=lambda item: item.name,
        )

    def enqueue(
        self,
        source_path: Path,
        profile: str,
        glossary_path: Path | None = None,
        series: str | None = None,
        job_context: str | None = None,
        scene_contexts: list[SceneContextBlock] | None = None,
    ) -> JobManifest:
        if not source_path.exists():
            raise QueueError(f"Source not found: {source_path}")

        job_id = (
            f"{now_iso().replace(':', '').replace('+00:00', 'Z').replace('-', '')}-"
            f"{safe_slug(source_path.stem)}-{uuid4().hex[:8]}"
        )
        job_dir = self.incoming_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=False)

        manifest = JobManifest(
            job_id=job_id,
            source_path=str(source_path.resolve()),
            source_name=source_path.name,
            profile=profile,
            glossary_path=str(glossary_path.resolve()) if glossary_path else None,
            series=series,
            job_context=job_context,
            scene_contexts=list(scene_contexts or []),
            export_dir=str(subtitle_output_dir(source_path.resolve())),
        )
        manifest.artifacts = {
            "job": manifest.job_filename(),
            "review": f"{source_path.stem}.review.json",
            "ja_srt": f"{source_path.stem}.ja.srt",
            "literal_srt": f"{source_path.stem}.en.literal.srt",
            "adapted_srt": f"{source_path.stem}.en.adapted.srt",
            "audio": "source.wav",
            "ja_cues": "ja.cues.json",
            "literal_cues": "literal.cues.json",
            "adapted_cues": "adapted.cues.json",
        }
        self.save_manifest(job_dir, manifest)
        return manifest

    def save_manifest(self, job_dir: Path, manifest: JobManifest) -> None:
        manifest.mark_updated()
        manifest_path = job_dir / manifest.artifacts.get("job", manifest.job_filename())
        atomic_write_json(manifest_path, manifest.to_dict())

    def load_manifest(self, job_dir: Path) -> JobManifest:
        return JobManifest.from_dict(read_json(self._manifest_path(job_dir)))

    def list_jobs(self) -> list[tuple[Path, JobManifest, str]]:
        rows: list[tuple[Path, JobManifest, str]] = []
        for name, parent in (
            ("incoming", self.incoming_dir),
            ("working", self.working_dir),
            ("done", self.done_dir),
            ("failed", self.failed_dir),
        ):
            for job_dir in self._job_dirs(parent):
                try:
                    rows.append((job_dir, self.load_manifest(job_dir), name))
                except FileNotFoundError:
                    continue
        return sorted(rows, key=lambda item: item[1].created_at)

    def find_job(self, job_id: str) -> tuple[Path, JobManifest]:
        for job_dir, manifest, _state in self.list_jobs():
            if manifest.job_id == job_id:
                return job_dir, manifest
        raise QueueError(f"Unknown job id: {job_id}")

    def claim_next_job(self) -> tuple[Path, JobManifest] | None:
        for job_dir in self._job_dirs(self.working_dir):
            manifest = self.load_manifest(job_dir)
            if manifest.status == JOB_STATUS_WORKING:
                return job_dir, manifest

        for job_dir in self._job_dirs(self.incoming_dir):
            manifest = self.load_manifest(job_dir)
            if manifest.status != JOB_STATUS_QUEUED:
                continue
            manifest.status = JOB_STATUS_WORKING
            target_dir = self.working_dir / job_dir.name
            shutil.move(str(job_dir), str(target_dir))
            self.save_manifest(target_dir, manifest)
            return target_dir, manifest
        return None

    def mark_paused(self, job_dir: Path, manifest: JobManifest) -> tuple[Path, JobManifest]:
        manifest.status = JOB_STATUS_PAUSED
        self.save_manifest(job_dir, manifest)
        target = self.incoming_dir / job_dir.name
        shutil.move(str(job_dir), str(target))
        self.save_manifest(target, manifest)
        return target, manifest

    def mark_completed(self, job_dir: Path, manifest: JobManifest) -> tuple[Path, JobManifest]:
        manifest.status = JOB_STATUS_COMPLETED
        self.save_manifest(job_dir, manifest)
        target = self.done_dir / job_dir.name
        shutil.move(str(job_dir), str(target))
        return target, manifest

    def mark_failed(self, job_dir: Path, manifest: JobManifest, error: str) -> tuple[Path, JobManifest]:
        manifest.status = JOB_STATUS_FAILED
        manifest.error = error
        self.save_manifest(job_dir, manifest)
        target = self.failed_dir / job_dir.name
        shutil.move(str(job_dir), str(target))
        return target, manifest

    def requeue_working(self, job_dir: Path, manifest: JobManifest, error: str) -> tuple[Path, JobManifest]:
        manifest.status = JOB_STATUS_QUEUED
        manifest.error = error
        self.save_manifest(job_dir, manifest)
        target = self.incoming_dir / job_dir.name
        shutil.move(str(job_dir), str(target))
        return target, manifest

    def resume_job(self, job_id: str) -> tuple[Path, JobManifest]:
        job_dir, manifest = self.find_job(job_id)
        if job_dir.parent == self.done_dir:
            raise QueueError("Completed jobs cannot be resumed. Queue the source again to rerun it.")
        manifest.status = JOB_STATUS_QUEUED
        manifest.error = None
        if job_dir.parent == self.failed_dir:
            target = self.incoming_dir / job_dir.name
            shutil.move(str(job_dir), str(target))
            job_dir = target
        self.save_manifest(job_dir, manifest)
        return job_dir, manifest
