from __future__ import annotations

import os
from pathlib import Path

import pytest

from local_subtitle_stack.utils import atomic_write_text, format_timecode, parse_timecode


def test_atomic_write_text_retries_permission_error(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "sample.json"
    attempts = {"count": 0}
    original_replace = os.replace

    def flaky_replace(src: str | bytes | os.PathLike[str] | os.PathLike[bytes], dst: str | bytes | os.PathLike[str] | os.PathLike[bytes]) -> None:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise PermissionError("temporary file lock")
        original_replace(src, dst)

    monkeypatch.setattr("local_subtitle_stack.utils.os.replace", flaky_replace)
    monkeypatch.setattr("local_subtitle_stack.utils.time.sleep", lambda _seconds: None)

    atomic_write_text(target, "ok")

    assert target.read_text(encoding="utf-8") == "ok"
    assert attempts["count"] == 3


def test_parse_and_format_timecode_roundtrip() -> None:
    assert parse_timecode("12:34") == 754.0
    assert parse_timecode("01:02:03") == 3723.0
    assert format_timecode(3723.9) == "01:02:03"


def test_parse_timecode_rejects_bad_values() -> None:
    with pytest.raises(ValueError):
        parse_timecode("abc")
