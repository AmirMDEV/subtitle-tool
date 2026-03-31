from __future__ import annotations

from pathlib import Path

import pytest

from local_subtitle_stack.domain import Cue, SceneContextBlock
from local_subtitle_stack.pipeline import (
    apply_translations,
    build_context_notes,
    build_adapted_prompt,
    build_literal_prompt,
    combine_chunk_cues,
    validate_translation_payload,
    write_srt,
)


def test_validate_translation_payload_rejects_refusal() -> None:
    with pytest.raises(ValueError):
        validate_translation_payload(
            {"translations": ["I can't assist with that request."]},
            expected_count=1,
        )


def test_validate_translation_payload_allows_plain_dialogue_with_cant_help() -> None:
    translations = validate_translation_payload(
        {"translations": ["I can't help it, it feels too good."]},
        expected_count=1,
    )
    assert translations == ["I can't help it, it feels too good."]


def test_combine_chunk_cues_keeps_non_duplicate_overlap_boundary_lines() -> None:
    combined = combine_chunk_cues(
        [
            (
                0.0,
                [
                    Cue(index=1, start=0.0, end=1.1, text="first line"),
                    Cue(index=2, start=1.1, end=2.2, text="second line"),
                ],
            ),
            (
                1.8,
                [
                    Cue(index=1, start=0.0, end=0.6, text="second line"),
                    Cue(index=2, start=0.4, end=1.3, text="third line"),
                ],
            ),
        ]
    )
    assert [cue.text for cue in combined] == ["first line", "second line", "third line"]
    assert combined[-1].start >= combined[-2].end


def test_apply_translations_and_write_srt_preserve_timings(tmp_path: Path) -> None:
    cues = [
        Cue(index=1, start=1.0, end=2.5, text="a"),
        Cue(index=2, start=3.0, end=4.2, text="i"),
    ]
    translated = apply_translations(cues, ["first line", "second line"])
    assert translated[0].start == 1.0
    assert translated[1].end == 4.2

    output = tmp_path / "sample.srt"
    write_srt(output, translated)
    content = output.read_text(encoding="utf-8")
    assert "00:00:01,000 --> 00:00:02,500" in content
    assert "first line" in content


def test_prompts_include_meaning_rules_and_context() -> None:
    group = [Cue(index=1, start=0.0, end=1.0, text="motto shite")]
    glossary = [{"jp": "motto", "preferred_en": "more", "notes": "Keep direct"}]
    literal = build_literal_prompt(group, glossary, metadata="filename=test")
    adapted = build_adapted_prompt(
        group=group,
        literal_group=[Cue(index=1, start=0.0, end=1.0, text="Do more.")],
        prev_context=[Cue(index=0, start=0.0, end=0.5, text="a")],
        next_context=[Cue(index=2, start=1.0, end=2.0, text="u")],
        glossary=glossary,
        metadata="filename=test",
        context_notes="The speakers are comparing appearance and family resemblance.",
    )
    assert "Do not euphemise, censor, moralise, or sanitize the source meaning" in literal
    assert "direct, explicit, or sensitive wording" in literal
    assert "Context notes:" in literal
    assert "Do not censor, sanitize, or soften the source meaning" in adapted
    assert "previous_context" in adapted
    assert "Context notes:" in adapted
    assert "appearance and family resemblance" in adapted


def test_build_context_notes_combines_global_and_matching_scene_ranges() -> None:
    group = [Cue(index=3, start=620.0, end=632.0, text="line")]
    notes = build_context_notes(
        group=group,
        global_context="Whole video context about appearance comparison and tone.",
        scene_contexts=[
            SceneContextBlock(start_seconds=0.0, end_seconds=300.0, notes="Travel scene."),
            SceneContextBlock(start_seconds=600.0, end_seconds=900.0, notes="At home scene about family resemblance."),
        ],
    )
    assert notes is not None
    assert "Whole video context" in notes
    assert "At home scene about family resemblance." in notes
    assert "Travel scene." not in notes
