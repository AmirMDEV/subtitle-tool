from __future__ import annotations

from pathlib import Path

from local_subtitle_stack.utils import list_video_sources, subtitle_output_dir


def test_list_video_sources_filters_non_videos(tmp_path: Path) -> None:
    (tmp_path / "a.mp4").write_text("video", encoding="utf-8")
    (tmp_path / "b.txt").write_text("note", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "c.mkv").write_text("video", encoding="utf-8")

    direct = list_video_sources(tmp_path, recursive=False)
    recursive = list_video_sources(tmp_path, recursive=True)

    assert direct == [tmp_path / "a.mp4"]
    assert recursive == [tmp_path / "a.mp4", nested / "c.mkv"]


def test_subtitle_output_dir_uses_source_filename(tmp_path: Path) -> None:
    source = tmp_path / "scene.mp4"
    assert subtitle_output_dir(source) == tmp_path / "scene.mp4 subtitles"
