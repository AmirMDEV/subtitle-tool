from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .utils import now_iso, subtitle_output_dir

SCHEMA_VERSION = 3
JOB_STATUS_QUEUED = "queued"
JOB_STATUS_WORKING = "working"
JOB_STATUS_PAUSED = "paused"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"

STAGE_EXTRACT = "extract_audio"
STAGE_TRANSCRIBE = "transcribe"
STAGE_LITERAL = "translate_literal"
STAGE_ADAPTED = "translate_adapted"
STAGE_FINALIZE = "finalize"


@dataclass(slots=True)
class Cue:
    index: int
    start: float
    end: float
    text: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Cue":
        return cls(
            index=int(data["index"]),
            start=float(data["start"]),
            end=float(data["end"]),
            text=str(data["text"]),
        )


@dataclass(slots=True)
class ChunkPlan:
    index: int
    start: float
    end: float
    path: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChunkPlan":
        return cls(
            index=int(data["index"]),
            start=float(data["start"]),
            end=float(data["end"]),
            path=str(data["path"]),
        )


@dataclass(slots=True)
class ReviewFlag:
    stage: str
    group_index: int
    reason: str
    detail: str
    created_at: str = field(default_factory=now_iso)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReviewFlag":
        return cls(
            stage=str(data["stage"]),
            group_index=int(data["group_index"]),
            reason=str(data["reason"]),
            detail=str(data["detail"]),
            created_at=str(data.get("created_at", now_iso())),
        )


@dataclass(slots=True)
class StageCheckpoint:
    name: str
    status: str = "pending"
    attempts: int = 0
    updated_at: str = field(default_factory=now_iso)
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StageCheckpoint":
        return cls(
            name=str(data["name"]),
            status=str(data.get("status", "pending")),
            attempts=int(data.get("attempts", 0)),
            updated_at=str(data.get("updated_at", now_iso())),
            details=dict(data.get("details", {})),
        )


@dataclass(slots=True)
class MetricsSummary:
    peak_rss_mb: int = 0
    peak_gpu_used_mb: int = 0
    last_seen_ram_available_mb: int = 0
    last_seen_gpu_free_mb: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MetricsSummary":
        return cls(
            peak_rss_mb=int(data.get("peak_rss_mb", 0)),
            peak_gpu_used_mb=int(data.get("peak_gpu_used_mb", 0)),
            last_seen_ram_available_mb=int(data.get("last_seen_ram_available_mb", 0)),
            last_seen_gpu_free_mb=int(data.get("last_seen_gpu_free_mb", 0)),
        )


@dataclass(slots=True)
class SceneContextBlock:
    start_seconds: float
    end_seconds: float
    notes: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SceneContextBlock":
        return cls(
            start_seconds=float(data["start_seconds"]),
            end_seconds=float(data["end_seconds"]),
            notes=str(data["notes"]),
        )


@dataclass(slots=True)
class JobManifest:
    job_id: str
    source_path: str
    source_name: str
    profile: str
    schema_version: int = SCHEMA_VERSION
    status: str = JOB_STATUS_QUEUED
    current_stage: str = STAGE_EXTRACT
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    glossary_path: str | None = None
    series: str | None = None
    job_context: str | None = None
    scene_contexts: list[SceneContextBlock] = field(default_factory=list)
    export_dir: str | None = None
    error: str | None = None
    models: dict[str, str] = field(default_factory=dict)
    checkpoints: dict[str, StageCheckpoint] = field(default_factory=dict)
    chunk_plan: list[ChunkPlan] = field(default_factory=list)
    metrics: MetricsSummary = field(default_factory=MetricsSummary)
    review_flags: list[ReviewFlag] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.checkpoints:
            self.checkpoints = {
                stage: StageCheckpoint(name=stage)
                for stage in (
                    STAGE_EXTRACT,
                    STAGE_TRANSCRIBE,
                    STAGE_LITERAL,
                    STAGE_ADAPTED,
                    STAGE_FINALIZE,
                )
            }

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["checkpoints"] = {
            key: asdict(value) for key, value in self.checkpoints.items()
        }
        data["chunk_plan"] = [asdict(item) for item in self.chunk_plan]
        data["review_flags"] = [asdict(flag) for flag in self.review_flags]
        data["metrics"] = asdict(self.metrics)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobManifest":
        manifest = cls(
            job_id=str(data["job_id"]),
            source_path=str(data["source_path"]),
            source_name=str(data["source_name"]),
            profile=str(data["profile"]),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            status=str(data.get("status", JOB_STATUS_QUEUED)),
            current_stage=str(data.get("current_stage", STAGE_EXTRACT)),
            created_at=str(data.get("created_at", now_iso())),
            updated_at=str(data.get("updated_at", now_iso())),
            glossary_path=data.get("glossary_path"),
            series=data.get("series"),
            job_context=data.get("job_context"),
            scene_contexts=[
                SceneContextBlock.from_dict(item)
                for item in list(data.get("scene_contexts", []))
            ],
            export_dir=data.get("export_dir"),
            error=data.get("error"),
            models=dict(data.get("models", {})),
            artifacts=dict(data.get("artifacts", {})),
        )
        if not manifest.export_dir:
            manifest.export_dir = str(subtitle_output_dir(Path(manifest.source_path)))
        manifest.checkpoints = {
            key: StageCheckpoint.from_dict(value)
            for key, value in dict(data.get("checkpoints", {})).items()
        } or manifest.checkpoints
        manifest.chunk_plan = [
            ChunkPlan.from_dict(item) for item in list(data.get("chunk_plan", []))
        ]
        manifest.review_flags = [
            ReviewFlag.from_dict(item) for item in list(data.get("review_flags", []))
        ]
        manifest.metrics = MetricsSummary.from_dict(dict(data.get("metrics", {})))
        return manifest

    def mark_updated(self) -> None:
        self.updated_at = now_iso()

    def checkpoint(self, stage: str) -> StageCheckpoint:
        return self.checkpoints[stage]

    def job_filename(self) -> str:
        return f"{Path(self.source_name).stem}.job.json"
