from __future__ import annotations

import pytest

from local_subtitle_stack.pipeline import validate_translation_payload


def test_refusal_boilerplate_is_rejected() -> None:
    with pytest.raises(ValueError, match="refusal or sanitization boilerplate"):
        validate_translation_payload({"translations": ["I cannot comply with that."]}, expected_count=1)


def test_normal_dialogue_with_cant_still_passes() -> None:
    assert validate_translation_payload(
        {"translations": ["I can't help it if we have the same body type."]},
        expected_count=1,
    ) == ["I can't help it if we have the same body type."]
