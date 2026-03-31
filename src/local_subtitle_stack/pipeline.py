from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from .domain import Cue, SceneContextBlock
from .utils import atomic_write_json, atomic_write_text, format_timecode, split_text_lines

REFUSAL_MARKERS = (
    "unable to comply",
    "content policy",
    "that request",
    "with that request",
    "cannot provide that",
    "can't provide that",
    "cannot comply with that",
    "can't comply with that",
)


def normalize_japanese_cues(cues: list[Cue]) -> list[Cue]:
    normalized: list[Cue] = []
    for cue in cues:
        text = cue.text.strip()
        if not text:
            continue
        start = max(cue.start, 0.0)
        end = max(cue.end, start + 0.5)
        normalized.append(
            Cue(
                index=len(normalized) + 1,
                start=start,
                end=end,
                text=text.replace("  ", " "),
            )
        )
    return normalized


def combine_chunk_cues(chunks: list[tuple[float, list[Cue]]]) -> list[Cue]:
    combined: list[Cue] = []
    for offset, cues in chunks:
        for cue in cues:
            start = cue.start + offset
            end = cue.end + offset
            if combined:
                previous = combined[-1]
                if cues_likely_duplicate(previous, start, end, cue.text):
                    previous.end = max(previous.end, end)
                    continue
                if start < previous.end:
                    start = previous.end
                    if end <= start + 0.05:
                        continue
            combined.append(
                Cue(
                    index=len(combined) + 1,
                    start=start,
                    end=end,
                    text=cue.text,
                )
            )
    return combined


def normalize_compare_text(text: str) -> str:
    lowered = text.lower().strip()
    return re.sub(r"[\W_]+", "", lowered)


def cues_likely_duplicate(previous: Cue, start: float, end: float, text: str) -> bool:
    candidate_text = normalize_compare_text(text)
    previous_text = normalize_compare_text(previous.text)
    if not candidate_text or not previous_text:
        return False
    same_text = candidate_text == previous_text
    contained_text = candidate_text in previous_text or previous_text in candidate_text
    close_in_time = start <= previous.end + 1.0 and end >= previous.start - 1.0
    return close_in_time and (same_text or contained_text)


def cue_groups(cues: list[Cue], size: int) -> list[list[Cue]]:
    return [cues[index : index + size] for index in range(0, len(cues), size)]


def load_glossary(glossary_path: str | None) -> list[dict[str, str]]:
    if not glossary_path:
        return []
    path = Path(glossary_path)
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if isinstance(data, list):
        return [dict(item) for item in data]
    return []


def build_context_notes(
    group: list[Cue],
    global_context: str | None,
    scene_contexts: list[SceneContextBlock],
) -> str | None:
    sections: list[str] = []
    global_note = (global_context or "").strip()
    if global_note:
        sections.append(f"Overall video context:\n{global_note}")

    if group:
        group_start = min(cue.start for cue in group)
        group_end = max(cue.end for cue in group)
        matches = [
            block
            for block in scene_contexts
            if block.notes.strip()
            and block.start_seconds <= group_end
            and block.end_seconds >= group_start
        ]
        if matches:
            scene_lines = [
                f"- {format_timecode(block.start_seconds)} to {format_timecode(block.end_seconds)}: {block.notes.strip()}"
                for block in matches
            ]
            sections.append("Scene-specific context:\n" + "\n".join(scene_lines))

    if not sections:
        return None
    return "\n\n".join(sections)


def build_literal_prompt(group: list[Cue], glossary: list[dict[str, str]], metadata: str) -> str:
    return build_literal_prompt_with_context(
        group=group,
        glossary=glossary,
        metadata=metadata,
        context_notes=None,
    )


def build_literal_prompt_with_context(
    group: list[Cue],
    glossary: list[dict[str, str]],
    metadata: str,
    context_notes: str | None,
) -> str:
    glossary_text = "\n".join(
        f"- {item.get('jp', '')} => {item.get('preferred_en') or item.get('literal_en', '')} ({item.get('notes', '')})"
        for item in glossary
    )
    cues_json = json.dumps(
        [{"index": cue.index, "text": cue.text} for cue in group],
        ensure_ascii=False,
        indent=2,
    )
    return (
        "You are translating Japanese dialogue into literal English subtitles.\n"
        "Rules:\n"
        "- Preserve the source meaning, including direct, explicit, or sensitive wording when present.\n"
        "- Do not euphemise, censor, moralise, or sanitize the source meaning.\n"
        "- Keep each translation aligned 1:1 with the input cue count.\n"
        "- Use context notes only to resolve ambiguity. Do not invent new content.\n"
        "- Return JSON only in the shape {\"translations\": [\"...\"]}.\n"
        "- Do not include notes.\n"
        f"Metadata: {metadata or 'none'}\n"
        f"Context notes: {context_notes or 'none'}\n"
        f"Glossary:\n{glossary_text or '- none'}\n"
        f"Cues:\n{cues_json}\n"
    )


def build_adapted_prompt(
    group: list[Cue],
    literal_group: list[Cue],
    prev_context: list[Cue],
    next_context: list[Cue],
    glossary: list[dict[str, str]],
    metadata: str,
    context_notes: str | None = None,
) -> str:
    glossary_text = "\n".join(
        f"- {item.get('jp', '')} => {item.get('preferred_en') or item.get('literal_en', '')} ({item.get('notes', '')})"
        for item in glossary
    )
    payload = {
        "previous_context": [cue.text for cue in prev_context],
        "target_group": [
            {"index": cue.index, "jp": cue.text, "literal_en": literal_group[pos].text}
            for pos, cue in enumerate(group)
        ],
        "next_context": [cue.text for cue in next_context],
        "context_notes": context_notes or "",
    }
    return (
        "You are adapting literal English subtitle lines into natural subtitle English for Japanese dialogue.\n"
        "Rules:\n"
        "- Keep the same cue count and the same order.\n"
        "- Preserve the intended meaning, tone, and speaker relationship.\n"
        "- Do not censor, sanitize, or soften the source meaning.\n"
        "- If the source includes direct, explicit, or sensitive wording, keep that meaning accurate.\n"
        "- Make the English read naturally as subtitles.\n"
        "- If a line is ambiguous, prefer interpretations that fit the context notes and neighboring cues.\n"
        "- Return JSON only in the shape {\"translations\": [\"...\"]}.\n"
        f"Metadata: {metadata or 'none'}\n"
        f"Context notes: {context_notes or 'none'}\n"
        f"Glossary:\n{glossary_text or '- none'}\n"
        f"Payload:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
    )


def validate_translation_payload(payload: dict[str, Any], expected_count: int) -> list[str]:
    if not isinstance(payload, dict):
        raise ValueError("Model output is not a JSON object.")
    translations = payload.get("translations")
    if not isinstance(translations, list):
        raise ValueError("Model output did not contain a translations list.")
    if len(translations) != expected_count:
        raise ValueError(
            f"Model returned {len(translations)} translations, expected {expected_count}."
        )
    cleaned: list[str] = []
    for item in translations:
        text = str(item).strip()
        if not text:
            raise ValueError("Model returned an empty translation.")
        if text.startswith("```") or text.endswith("```"):
            raise ValueError("Model returned markdown fences instead of raw JSON values.")
        if looks_like_refusal_boilerplate(text):
            raise ValueError("Model returned refusal or sanitization boilerplate.")
        cleaned.append(text)
    return cleaned


def apply_translations(template_cues: list[Cue], texts: list[str], max_chars: int = 42) -> list[Cue]:
    translated: list[Cue] = []
    for cue, text in zip(template_cues, texts, strict=True):
        translated.append(
            Cue(
                index=cue.index,
                start=cue.start,
                end=cue.end,
                text=split_text_lines(text.strip(), max_chars=max_chars),
            )
        )
    return translated


def format_srt_timestamp(value: float) -> str:
    total_ms = max(int(round(value * 1000)), 0)
    hours = total_ms // 3_600_000
    total_ms %= 3_600_000
    minutes = total_ms // 60_000
    total_ms %= 60_000
    seconds = total_ms // 1000
    millis = total_ms % 1000
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def write_srt(path: Path, cues: list[Cue]) -> None:
    lines: list[str] = []
    for index, cue in enumerate(cues, start=1):
        end = max(cue.end, cue.start + 0.5)
        lines.extend(
            [
                str(index),
                f"{format_srt_timestamp(cue.start)} --> {format_srt_timestamp(end)}",
                cue.text.strip(),
                "",
            ]
        )
    atomic_write_text(path, "\n".join(lines).strip() + "\n")


def write_review_flags(path: Path, review_flags: list[dict[str, Any]]) -> None:
    atomic_write_json(path, review_flags)


def strict_retry_prompt(prompt: str) -> str:
    return prompt + (
        "\nReturn only valid JSON. Do not add commentary, markdown, explanations, or policy language."
    )


def metadata_from_manifest(source_name: str, series: str | None) -> str:
    stem = Path(source_name).stem
    if series:
        return f"series={series}; filename={stem}"
    return f"filename={stem}"


def likely_malformed_json_text(text: str) -> bool:
    lowered = text.lower().strip()
    return bool(re.search(r"```|^note:|^here", lowered))


def looks_like_refusal_boilerplate(text: str) -> bool:
    lowered = text.lower().strip()
    if "policy" in lowered:
        return True
    if re.search(r"\b(can(?:not|'t))\s+help it\b", lowered):
        return False
    if any(marker in lowered for marker in REFUSAL_MARKERS):
        return True
    refusal_patterns = (
        r"\b(?:cannot|can't|unable to)\s+(?:assist|comply|provide)\b",
        r"\b(?:cannot|can't)\s+help with that\b",
        r"\b(?:cannot|can't)\s+do that\b",
    )
    return any(re.search(pattern, lowered) for pattern in refusal_patterns)
