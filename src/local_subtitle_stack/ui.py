from __future__ import annotations

import subprocess
import sys
import threading
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .app import build_service
from .config import CachePaths, ModelConfig, save_config
from .domain import SceneContextBlock
from .guards import ResourceSnapshot, capture_snapshot
from .queue import QueueError
from .utils import format_timecode, no_window_creationflags, parse_timecode, split_text_lines

PROFILE_LABELS = {
    "conservative": "Safe and steady (recommended)",
    "default": "Faster, uses more memory",
}
PROFILE_KEYS_BY_LABEL = {label: key for key, label in PROFILE_LABELS.items()}

STATUS_LABELS = {
    "queued": "Waiting",
    "working": "Working now",
    "paused": "Stopped safely",
    "completed": "Finished",
    "failed": "Needs attention",
}

STAGE_LABELS = {
    "extract_audio": "Getting the audio ready",
    "transcribe": "Listening to the Japanese",
    "translate_literal": "Making direct English",
    "translate_adapted": "Making easy English",
    "finalize": "Saving the subtitle files",
}


def ordered_preview_range(item_ids: list[str], start_item: str, end_item: str) -> list[str]:
    if start_item not in item_ids or end_item not in item_ids:
        return []
    start_index = item_ids.index(start_item)
    end_index = item_ids.index(end_item)
    low = min(start_index, end_index)
    high = max(start_index, end_index)
    return item_ids[low : high + 1]


def preview_item_id(cue_index: int) -> str:
    return f"cue-{cue_index}"


def cue_index_from_item_id(item_id: str) -> int | None:
    if not item_id.startswith("cue-"):
        return None
    try:
        return int(item_id.split("-", 1)[1])
    except ValueError:
        return None


def wrap_preview_text(text: str, max_chars: int, *, max_lines: int = 3) -> str:
    normalized = str(text).replace("\r\n", "\n").strip()
    if not normalized:
        return ""

    if "\n" in normalized:
        pieces: list[str] = []
        for part in normalized.splitlines():
            pieces.extend(wrap_preview_text(part, max_chars, max_lines=max_lines).splitlines())
            if len(pieces) >= max_lines:
                break
        return "\n".join(pieces[:max_lines])

    if len(normalized) <= max_chars:
        return normalized

    if any(character.isspace() for character in normalized):
        wrapped = split_text_lines(normalized, max_chars=max_chars)
        lines = wrapped.splitlines()
        if len(lines) <= max_lines:
            return wrapped
        return "\n".join(lines[:max_lines])

    hard_wrapped = [
        normalized[index : index + max_chars]
        for index in range(0, len(normalized), max_chars)
    ]
    return "\n".join(hard_wrapped[:max_lines])


@dataclass(slots=True)
class JobEditorDraft:
    batch_label: str = ""
    overall_context: str = ""
    note_start: str = ""
    note_end: str = ""
    range_notes: str = ""
    scene_contexts: list[SceneContextBlock] = field(default_factory=list)
    selected_cue_indexes: list[int] = field(default_factory=list)
    marked_start_cue_index: int | None = None
    marked_end_cue_index: int | None = None


class SubtitleStackApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Japanese Subtitle Tool")
        self.geometry("1480x920")
        self.minsize(1100, 680)
        self.service = build_service()
        self.worker_process: subprocess.Popen[str] | None = None
        self.rebuild_process: subprocess.Popen[str] | None = None
        self.rebuild_job_id: str | None = None
        self.rebuild_poll_job: str | None = None
        self.refresh_job: str | None = None
        self.snapshot_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.latest_snapshot: ResourceSnapshot | None = None
        self.scene_contexts: list[SceneContextBlock] = []
        self.current_job_id: str | None = None
        self.loaded_job_id: str | None = None
        self.editor_drafts: dict[str, JobEditorDraft] = {}
        self.preview_ranges: dict[str, tuple[float, float]] = {}
        self.preview_row_data: dict[int, dict[str, str | float | int | bool]] = {}
        self.preview_selected_cue_indexes: list[int] = []
        self.preview_mark_start_item: str | None = None
        self.preview_mark_end_item: str | None = None
        self.line_editor_cue_index: int | None = None
        self.default_model_config = ModelConfig()
        self.default_cache_paths = CachePaths()

        self.profile_var = tk.StringVar(value=PROFILE_LABELS.get("conservative", "Safe and steady (recommended)"))
        self.asr_model_var = tk.StringVar(value=self.service.config.models.asr)
        self.literal_model_var = tk.StringVar(value=self.service.config.models.literal_translation)
        self.adapted_model_var = tk.StringVar(value=self.service.config.models.adapted_translation)
        self.hf_cache_var = tk.StringVar(value=self.service.config.cache_paths.hf_hub_cache or "")
        self.batch_label_var = tk.StringVar()
        self.recursive_var = tk.BooleanVar(value=False)
        self.note_start_var = tk.StringVar()
        self.note_end_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Ready")
        self.memory_var = tk.StringVar(value="RAM free: -- MB | VRAM free: -- MB")
        self.selected_file_var = tk.StringVar(value="Pick or click a job on the left.")
        self.selected_job_state_var = tk.StringVar(value="Nothing is selected yet.")
        self.marked_range_var = tk.StringVar(value="Marked range: none")
        self.line_editor_time_var = tk.StringVar(value="")
        self.line_editor_status_var = tk.StringVar(value="Click one subtitle line to edit it here.")
        self.preview_hint_var = tk.StringVar(
            value="When you click a job, its Japanese lines and English lines show up here."
        )

        self._configure_style()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._start_snapshot_thread()
        self.refresh(reschedule=True)

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))
        style.configure("Section.TLabelframe.Label", font=("Segoe UI", 11, "bold"))
        style.configure("Hint.TLabel", foreground="#5A6470")
        style.configure("Primary.TButton", padding=(10, 6))
        style.configure("Preview.Treeview", rowheight=68)

    def _build_ui(self) -> None:
        shell = ttk.Frame(self)
        shell.pack(fill=tk.BOTH, expand=True)

        self.scroll_canvas = tk.Canvas(shell, highlightthickness=0)
        self.scroll_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.outer_scrollbar = ttk.Scrollbar(shell, orient=tk.VERTICAL, command=self.scroll_canvas.yview)
        self.outer_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.scroll_canvas.configure(yscrollcommand=self.outer_scrollbar.set)

        root = ttk.Frame(self.scroll_canvas, padding=14)
        self.scroll_window = self.scroll_canvas.create_window((0, 0), window=root, anchor="nw")
        root.bind("<Configure>", self._on_content_configure)
        self.scroll_canvas.bind("<Configure>", self._on_canvas_configure)
        self.scroll_canvas.bind("<MouseWheel>", self._on_mousewheel)
        root.bind("<MouseWheel>", self._on_mousewheel)

        ttk.Label(root, text="Make subtitles, check them, then fix the confusing parts.", style="Title.TLabel").pack(
            anchor=tk.W
        )
        ttk.Label(
            root,
            text=(
                "1. Add videos. 2. Start processing. 3. Click a job. 4. Highlight the lines that look wrong. "
                "5. Add helper notes. 6. Press Redo English."
            ),
            style="Hint.TLabel",
            wraplength=1300,
        ).pack(anchor=tk.W, pady=(4, 12))

        action_bar = ttk.Frame(root)
        action_bar.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(action_bar, text="Speed mode").pack(side=tk.LEFT)
        profile_box = ttk.Combobox(
            action_bar,
            textvariable=self.profile_var,
            values=list(PROFILE_KEYS_BY_LABEL.keys()),
            state="readonly",
            width=28,
        )
        profile_box.pack(side=tk.LEFT, padx=(8, 16))

        ttk.Button(action_bar, text="Add video files", command=self.enqueue_files, style="Primary.TButton").pack(
            side=tk.LEFT
        )
        ttk.Button(action_bar, text="Add a folder", command=self.enqueue_folder, style="Primary.TButton").pack(
            side=tk.LEFT,
            padx=6,
        )
        self.start_processing_button = ttk.Button(
            action_bar,
            text="Start processing",
            command=self.start_worker,
            style="Primary.TButton",
        )
        self.start_processing_button.pack(side=tk.LEFT, padx=6)
        self.stop_safely_button = ttk.Button(action_bar, text="Stop safely", command=self.pause_worker)
        self.stop_safely_button.pack(side=tk.LEFT, padx=6)
        self.retry_selected_button = ttk.Button(action_bar, text="Retry selected job", command=self.retry_selected_job)
        self.retry_selected_button.pack(side=tk.LEFT, padx=6)

        ttk.Label(action_bar, textvariable=self.status_var).pack(side=tk.RIGHT)
        ttk.Label(action_bar, textvariable=self.memory_var).pack(side=tk.RIGHT, padx=(0, 16))

        ttk.Label(
            root,
            text=(
                "Speed mode helps your laptop stay comfortable. "
                "Safe and steady is best when other apps are open."
            ),
            style="Hint.TLabel",
            wraplength=1300,
        ).pack(anchor=tk.W, pady=(0, 10))

        self._build_settings_panel(root)

        paned = ttk.Panedwindow(root, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(paned, padding=(0, 0, 12, 0))
        right = ttk.Frame(paned)
        paned.add(left, weight=1)
        paned.add(right, weight=2)

        self._build_left_panel(left)
        self._build_right_panel(right)

    def _build_left_panel(self, parent: ttk.Frame) -> None:
        queue_frame = ttk.LabelFrame(parent, text="Videos waiting or finished", style="Section.TLabelframe")
        queue_frame.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(queue_frame, padding=10)
        header.pack(fill=tk.X)
        ttk.Checkbutton(
            header,
            text="Look inside subfolders too",
            variable=self.recursive_var,
        ).pack(side=tk.LEFT)
        ttk.Label(
            header,
            text="Click one job to see its subtitle lines on the right.",
            style="Hint.TLabel",
        ).pack(side=tk.RIGHT)

        columns = ("source", "status", "step", "updated_at")
        tree_frame = ttk.Frame(queue_frame, padding=(10, 0, 10, 10))
        tree_frame.pack(fill=tk.BOTH, expand=True)

        self.job_tree = ttk.Treeview(
            tree_frame,
            columns=columns,
            show="headings",
            height=24,
            selectmode="browse",
        )
        self.job_tree.heading("source", text="File")
        self.job_tree.heading("status", text="Status")
        self.job_tree.heading("step", text="What it is doing")
        self.job_tree.heading("updated_at", text="Last update")
        self.job_tree.column("source", width=260, anchor=tk.W)
        self.job_tree.column("status", width=120, anchor=tk.W)
        self.job_tree.column("step", width=190, anchor=tk.W)
        self.job_tree.column("updated_at", width=170, anchor=tk.W)
        self.job_tree.bind("<<TreeviewSelect>>", self._on_job_selected)
        self.job_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        yscroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.job_tree.yview)
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.job_tree.configure(yscrollcommand=yscroll.set)

    def _build_settings_panel(self, parent: ttk.Frame) -> None:
        settings_frame = ttk.LabelFrame(parent, text="Model and cache settings", style="Section.TLabelframe")
        settings_frame.pack(fill=tk.X, pady=(0, 12))

        inner = ttk.Frame(settings_frame, padding=10)
        inner.pack(fill=tk.X)
        inner.columnconfigure(1, weight=1)

        ttk.Label(
            inner,
            text=(
                "These are app-wide defaults. The Japanese model can be a Hugging Face name or a local folder. "
                "The English models are Ollama model names."
            ),
            style="Hint.TLabel",
            wraplength=1250,
        ).grid(row=0, column=0, columnspan=4, sticky=tk.W, pady=(0, 10))

        ttk.Label(inner, text="Japanese model").grid(row=1, column=0, sticky=tk.W)
        ttk.Entry(inner, textvariable=self.asr_model_var).grid(row=1, column=1, sticky="ew", padx=(8, 8))
        ttk.Button(inner, text="Pick folder", command=self.choose_asr_model_folder).grid(row=1, column=2, sticky=tk.W)

        ttk.Label(inner, text="Direct English model").grid(row=2, column=0, sticky=tk.W, pady=(8, 0))
        ttk.Entry(inner, textvariable=self.literal_model_var).grid(
            row=2,
            column=1,
            sticky="ew",
            padx=(8, 8),
            pady=(8, 0),
        )
        ttk.Label(inner, text="Example: qwen3:4b-q8_0", style="Hint.TLabel").grid(
            row=2,
            column=2,
            sticky=tk.W,
            pady=(8, 0),
        )

        ttk.Label(inner, text="Natural English model").grid(row=3, column=0, sticky=tk.W, pady=(8, 0))
        ttk.Entry(inner, textvariable=self.adapted_model_var).grid(
            row=3,
            column=1,
            sticky="ew",
            padx=(8, 8),
            pady=(8, 0),
        )
        ttk.Label(
            inner,
            text="Used when you press Redo English or run a full job.",
            style="Hint.TLabel",
        ).grid(row=3, column=2, sticky=tk.W, pady=(8, 0))

        ttk.Label(inner, text="Japanese model cache folder").grid(row=4, column=0, sticky=tk.W, pady=(8, 0))
        ttk.Entry(inner, textvariable=self.hf_cache_var).grid(
            row=4,
            column=1,
            sticky="ew",
            padx=(8, 8),
            pady=(8, 0),
        )
        ttk.Button(inner, text="Pick folder", command=self.choose_hf_cache_folder).grid(
            row=4,
            column=2,
            sticky=tk.W,
            pady=(8, 0),
        )

        buttons = ttk.Frame(inner)
        buttons.grid(row=5, column=0, columnspan=4, sticky=tk.W, pady=(10, 0))
        ttk.Button(buttons, text="Save model settings", command=self.save_model_settings).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Use recommended defaults", command=self.reset_model_settings_defaults).pack(
            side=tk.LEFT,
            padx=6,
        )

        ttk.Label(
            inner,
            text=(
                "Leave the cache folder blank to use the normal Hugging Face cache location. "
                "Saving here updates future runs and English rebuilds."
            ),
            style="Hint.TLabel",
            wraplength=1250,
        ).grid(row=6, column=0, columnspan=4, sticky=tk.W, pady=(8, 0))

    def _build_right_panel(self, parent: ttk.Frame) -> None:
        selected_frame = ttk.LabelFrame(parent, text="Selected job", style="Section.TLabelframe")
        selected_frame.pack(fill=tk.BOTH, expand=True)

        top = ttk.Frame(selected_frame, padding=12)
        top.pack(fill=tk.X)
        ttk.Label(top, textvariable=self.selected_file_var, font=("Segoe UI", 11, "bold")).pack(anchor=tk.W)
        ttk.Label(top, textvariable=self.selected_job_state_var, style="Hint.TLabel").pack(anchor=tk.W, pady=(2, 10))

        meta = ttk.Frame(top)
        meta.pack(fill=tk.X)
        ttk.Label(meta, text="Batch label (optional)").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(meta, textvariable=self.batch_label_var, width=36).grid(row=0, column=1, sticky=tk.W, padx=(8, 16))
        ttk.Label(
            meta,
            text="Use this only if several videos belong together.",
            style="Hint.TLabel",
        ).grid(row=0, column=2, sticky=tk.W)

        preview_frame = ttk.LabelFrame(selected_frame, text="Subtitle lines", style="Section.TLabelframe")
        preview_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))

        preview_header = ttk.Frame(preview_frame, padding=10)
        preview_header.pack(fill=tk.X)
        ttk.Label(
            preview_header,
            textvariable=self.preview_hint_var,
            style="Hint.TLabel",
            wraplength=980,
            justify=tk.LEFT,
        ).pack(fill=tk.X, anchor=tk.W)

        preview_toolbar = ttk.Frame(preview_header)
        preview_toolbar.pack(fill=tk.X, pady=(8, 0))
        preview_actions = ttk.Frame(preview_toolbar)
        preview_actions.pack(side=tk.LEFT, anchor=tk.W)
        ttk.Label(preview_toolbar, textvariable=self.marked_range_var, style="Hint.TLabel").pack(
            side=tk.RIGHT,
            anchor=tk.E,
        )

        self.clear_marked_button = ttk.Button(
            preview_actions,
            text="Clear marked range",
            command=self.clear_preview_marked_range,
        )
        self.clear_marked_button.pack(side=tk.LEFT, padx=(0, 6))
        self.mark_start_button = ttk.Button(
            preview_actions,
            text="Mark start line",
            command=self.mark_preview_start_line,
        )
        self.mark_start_button.pack(side=tk.LEFT, padx=(0, 6))
        self.mark_end_button = ttk.Button(
            preview_actions,
            text="Mark end line",
            command=self.mark_preview_end_line,
        )
        self.mark_end_button.pack(side=tk.LEFT, padx=(0, 6))
        self.use_highlighted_button = ttk.Button(
            preview_actions,
            text="Use highlighted lines",
            command=self.use_selected_lines_for_note_range,
        )
        self.use_highlighted_button.pack(side=tk.LEFT, padx=(0, 6))
        self.reload_lines_button = ttk.Button(preview_actions, text="Reload lines", command=self.reload_selected_preview)
        self.reload_lines_button.pack(side=tk.LEFT)

        preview_tree_frame = ttk.Frame(preview_frame, padding=(10, 0, 10, 10))
        preview_tree_frame.pack(fill=tk.BOTH, expand=True)
        preview_columns = ("time", "japanese", "literal", "adapted")
        self.preview_tree = ttk.Treeview(
            preview_tree_frame,
            columns=preview_columns,
            show="headings",
            selectmode="extended",
            height=14,
            style="Preview.Treeview",
        )
        self.preview_tree.heading("time", text="Time")
        self.preview_tree.heading("japanese", text="Japanese")
        self.preview_tree.heading("literal", text="Direct English")
        self.preview_tree.heading("adapted", text="Easy English")
        self.preview_tree.column("time", width=150, anchor=tk.W, stretch=False)
        self.preview_tree.column("japanese", width=250, anchor=tk.W)
        self.preview_tree.column("literal", width=310, anchor=tk.W)
        self.preview_tree.column("adapted", width=360, anchor=tk.W)
        self.preview_tree.bind("<<TreeviewSelect>>", self._on_preview_lines_selected)
        self.preview_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        preview_y = ttk.Scrollbar(preview_tree_frame, orient=tk.VERTICAL, command=self.preview_tree.yview)
        preview_y.pack(side=tk.RIGHT, fill=tk.Y)
        preview_x = ttk.Scrollbar(preview_frame, orient=tk.HORIZONTAL, command=self.preview_tree.xview)
        preview_x.pack(fill=tk.X, padx=10, pady=(0, 10))
        self.preview_tree.configure(yscrollcommand=preview_y.set, xscrollcommand=preview_x.set)

        line_editor_frame = ttk.LabelFrame(selected_frame, text="Quick edit selected line", style="Section.TLabelframe")
        line_editor_frame.pack(fill=tk.BOTH, expand=False, padx=12, pady=(0, 12))

        line_editor_inner = ttk.Frame(line_editor_frame, padding=10)
        line_editor_inner.pack(fill=tk.BOTH, expand=True)
        line_editor_inner.columnconfigure(1, weight=1)

        ttk.Label(
            line_editor_inner,
            textvariable=self.line_editor_status_var,
            style="Hint.TLabel",
            wraplength=900,
            justify=tk.LEFT,
        ).grid(row=0, column=0, columnspan=4, sticky=tk.W, pady=(0, 10))

        ttk.Label(line_editor_inner, text="Time").grid(row=1, column=0, sticky=tk.W)
        ttk.Entry(
            line_editor_inner,
            textvariable=self.line_editor_time_var,
            state="readonly",
            width=22,
        ).grid(row=1, column=1, sticky=tk.W, padx=(8, 0), pady=(0, 10))

        ttk.Label(line_editor_inner, text="Japanese").grid(row=2, column=0, sticky=tk.NW)
        self.line_editor_japanese_text = tk.Text(line_editor_inner, height=3, wrap="word")
        self.line_editor_japanese_text.grid(row=2, column=1, columnspan=3, sticky="nsew", padx=(8, 0))

        ttk.Label(line_editor_inner, text="Direct English").grid(row=3, column=0, sticky=tk.NW, pady=(8, 0))
        self.line_editor_literal_text = tk.Text(line_editor_inner, height=3, wrap="word")
        self.line_editor_literal_text.grid(row=3, column=1, columnspan=3, sticky="nsew", padx=(8, 0), pady=(8, 0))

        ttk.Label(line_editor_inner, text="Easy English").grid(row=4, column=0, sticky=tk.NW, pady=(8, 0))
        self.line_editor_adapted_text = tk.Text(line_editor_inner, height=3, wrap="word")
        self.line_editor_adapted_text.grid(row=4, column=1, columnspan=3, sticky="nsew", padx=(8, 0), pady=(8, 0))

        line_editor_buttons = ttk.Frame(line_editor_inner)
        line_editor_buttons.grid(row=5, column=1, columnspan=3, sticky=tk.W, pady=(10, 0))
        self.reload_line_button = ttk.Button(
            line_editor_buttons,
            text="Reload selected line",
            command=self.reload_selected_line_editor,
        )
        self.reload_line_button.pack(side=tk.LEFT)
        self.save_line_button = ttk.Button(
            line_editor_buttons,
            text="Save line changes",
            command=self.save_selected_line_edit,
            style="Primary.TButton",
        )
        self.save_line_button.pack(side=tk.LEFT, padx=6)

        notes_frame = ttk.LabelFrame(selected_frame, text="Helper notes", style="Section.TLabelframe")
        notes_frame.pack(fill=tk.BOTH, expand=False, padx=12, pady=(0, 12))

        notes_inner = ttk.Frame(notes_frame, padding=10)
        notes_inner.pack(fill=tk.BOTH, expand=True)
        notes_inner.columnconfigure(1, weight=1)

        ttk.Label(notes_inner, text="Whole-video notes").grid(row=0, column=0, sticky=tk.NW)
        self.context_text = tk.Text(notes_inner, height=4, wrap="word")
        self.context_text.grid(row=0, column=1, columnspan=3, sticky="nsew", padx=(8, 0))
        ttk.Label(
            notes_inner,
            text=(
                "Example: scene setting, speaker relationship, place names, honorifics, and tone. "
                "Leave this blank if you do not need it."
            ),
            style="Hint.TLabel",
            wraplength=850,
        ).grid(row=1, column=1, columnspan=3, sticky=tk.W, pady=(4, 12))

        ttk.Label(notes_inner, text="From").grid(row=2, column=0, sticky=tk.W)
        ttk.Entry(notes_inner, textvariable=self.note_start_var, width=14).grid(
            row=2,
            column=1,
            sticky=tk.W,
            padx=(8, 12),
        )
        ttk.Label(notes_inner, text="To").grid(row=2, column=2, sticky=tk.W)
        ttk.Entry(notes_inner, textvariable=self.note_end_var, width=14).grid(
            row=2,
            column=3,
            sticky=tk.W,
            padx=(8, 0),
        )
        ttk.Label(
            notes_inner,
            text="Use the selected lines button to fill these in automatically.",
            style="Hint.TLabel",
        ).grid(row=3, column=1, columnspan=3, sticky=tk.W, pady=(4, 10))

        ttk.Label(notes_inner, text="Time-range note").grid(row=4, column=0, sticky=tk.NW)
        self.range_notes_text = tk.Text(notes_inner, height=3, wrap="word")
        self.range_notes_text.grid(row=4, column=1, columnspan=3, sticky="nsew", padx=(8, 0))

        note_button_row = ttk.Frame(notes_inner)
        note_button_row.grid(row=5, column=1, columnspan=3, sticky=tk.W, pady=(10, 10))
        self.add_note_button = ttk.Button(note_button_row, text="Add note", command=self.add_scene_block)
        self.add_note_button.pack(side=tk.LEFT)
        self.remove_note_button = ttk.Button(note_button_row, text="Remove selected note", command=self.remove_scene_block)
        self.remove_note_button.pack(
            side=tk.LEFT,
            padx=6,
        )
        self.clear_notes_button = ttk.Button(note_button_row, text="Clear all notes", command=self.clear_scene_blocks)
        self.clear_notes_button.pack(side=tk.LEFT)

        note_columns = ("start", "end", "notes")
        note_tree_frame = ttk.Frame(notes_inner)
        note_tree_frame.grid(row=6, column=0, columnspan=4, sticky="nsew")
        notes_inner.rowconfigure(6, weight=1)
        self.note_tree = ttk.Treeview(note_tree_frame, columns=note_columns, show="headings", height=6)
        self.note_tree.heading("start", text="From")
        self.note_tree.heading("end", text="To")
        self.note_tree.heading("notes", text="What this part is about")
        self.note_tree.column("start", width=90, anchor=tk.W)
        self.note_tree.column("end", width=90, anchor=tk.W)
        self.note_tree.column("notes", width=620, anchor=tk.W)
        self.note_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        note_scroll = ttk.Scrollbar(note_tree_frame, orient=tk.VERTICAL, command=self.note_tree.yview)
        note_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.note_tree.configure(yscrollcommand=note_scroll.set)

        bottom_actions = ttk.Frame(selected_frame, padding=(12, 0, 12, 12))
        bottom_actions.pack(fill=tk.X)
        self.save_notes_button = ttk.Button(bottom_actions, text="Save notes to this job", command=self.save_notes_selected)
        self.save_notes_button.pack(side=tk.LEFT)
        self.redo_english_button = ttk.Button(
            bottom_actions,
            text="Redo English for this job",
            command=self.redo_english_selected,
            style="Primary.TButton",
        )
        self.redo_english_button.pack(side=tk.LEFT, padx=6)
        self.open_review_button = ttk.Button(bottom_actions, text="Open in Subtitle Edit", command=self.open_review_selected)
        self.open_review_button.pack(
            side=tk.LEFT,
            padx=6,
        )
        self.open_output_button = ttk.Button(bottom_actions, text="Open subtitle folder", command=self.open_output_selected)
        self.open_output_button.pack(
            side=tk.LEFT
        )

    def enqueue_files(self) -> None:
        sources = filedialog.askopenfilenames(
            title="Select videos to queue",
            filetypes=[("Video Files", "*.mp4 *.mkv *.avi *.mov *.wmv *.m4v *.webm"), ("All Files", "*.*")],
        )
        if not sources:
            return
        try:
            manifests, skipped = self.service.enqueue_many(
                [Path(source) for source in sources],
                profile=self._current_profile_key(),
                series=self._batch_label_value(),
                context=self._context_value(),
                scene_contexts=self._scene_contexts_copy(),
            )
        except QueueError as exc:
            messagebox.showerror("Add video files", str(exc))
            return
        self.status_var.set(
            f"Queued {len(manifests)} video(s)"
            + (f" | skipped {len(skipped)} duplicate(s)" if skipped else "")
        )
        self.refresh()

    def enqueue_folder(self) -> None:
        folder = filedialog.askdirectory(title="Select a folder to queue")
        if not folder:
            return
        try:
            manifests, skipped = self.service.enqueue_folder(
                folder=Path(folder),
                profile=self._current_profile_key(),
                series=self._batch_label_value(),
                context=self._context_value(),
                scene_contexts=self._scene_contexts_copy(),
                recursive=self.recursive_var.get(),
            )
        except QueueError as exc:
            messagebox.showerror("Add a folder", str(exc))
            return
        messagebox.showinfo(
            "Folder added",
            f"Queued {len(manifests)} video(s).\nSkipped {len(skipped)} duplicate(s).",
        )
        self.status_var.set(f"Queued folder {Path(folder).name}")
        self.refresh()

    def choose_asr_model_folder(self) -> None:
        folder = filedialog.askdirectory(title="Select a local Japanese model folder")
        if folder:
            self.asr_model_var.set(folder)

    def choose_hf_cache_folder(self) -> None:
        folder = filedialog.askdirectory(title="Select a cache folder for the Japanese model")
        if folder:
            self.hf_cache_var.set(folder)

    def save_model_settings(self) -> None:
        self.service.config.models.asr = self._normalized_or_default(
            self.asr_model_var.get(),
            self.default_model_config.asr,
        )
        self.service.config.models.literal_translation = self._normalized_or_default(
            self.literal_model_var.get(),
            self.default_model_config.literal_translation,
        )
        self.service.config.models.adapted_translation = self._normalized_or_default(
            self.adapted_model_var.get(),
            self.default_model_config.adapted_translation,
        )
        self.service.config.cache_paths.hf_hub_cache = self._normalized_optional(
            self.hf_cache_var.get(),
            self.default_cache_paths.hf_hub_cache,
        )
        save_config(self.service.config)
        self._sync_model_setting_vars()
        self.status_var.set("Saved the app-wide model settings")

    def reset_model_settings_defaults(self) -> None:
        self.asr_model_var.set(self.default_model_config.asr)
        self.literal_model_var.set(self.default_model_config.literal_translation)
        self.adapted_model_var.set(self.default_model_config.adapted_translation)
        self.hf_cache_var.set(self.default_cache_paths.hf_hub_cache or "")
        self.save_model_settings()

    def start_worker(self) -> None:
        if self.rebuild_process and self.rebuild_process.poll() is None:
            self.status_var.set("Wait for Redo English to finish before starting the full queue")
            return
        self.service.store.set_pause(False)
        if self.worker_process and self.worker_process.poll() is None:
            self.status_var.set("Processing is already running")
            return
        self.worker_process = subprocess.Popen(
            [self._worker_python(), "-m", "local_subtitle_stack.cli", "worker"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            creationflags=no_window_creationflags(),
        )
        self.status_var.set(f"Processing is running (pid {self.worker_process.pid})")
        self.refresh()

    def pause_worker(self) -> None:
        self.service.store.set_pause(True)
        self.status_var.set("The app will stop after the next safe step")
        self.refresh()

    def retry_selected_job(self) -> None:
        if self.rebuild_process and self.rebuild_process.poll() is None:
            messagebox.showinfo("Retry selected job", "Wait for Redo English to finish first.")
            return
        selected = self._selected_job_id()
        if not selected:
            messagebox.showinfo("Retry selected job", "Click a job on the left first.")
            return
        try:
            self.service.resume(selected)
        except QueueError as exc:
            messagebox.showerror("Retry selected job", str(exc))
            return
        self.start_worker()

    def reload_selected_preview(self) -> None:
        selected = self._selected_job_id()
        if not selected:
            messagebox.showinfo("Reload lines", "Click a job on the left first.")
            return
        self._store_editor_draft(selected)
        self._load_job_details(selected, force_reload=True)

    def save_notes_selected(self) -> None:
        selected = self._selected_job_id()
        if not selected:
            messagebox.showinfo("Save notes", "Click a job on the left first.")
            return
        self._store_editor_draft(selected)
        try:
            self.service.save_job_notes(
                selected,
                batch_label=self._batch_label_value(),
                overall_context=self._context_value(),
                scene_contexts=self._scene_contexts_copy(),
            )
        except QueueError as exc:
            messagebox.showerror("Save notes", str(exc))
            return
        self.status_var.set("Saved the helper notes for the selected job")

    def redo_english_selected(self) -> None:
        selected = self._selected_job_id()
        if not selected:
            messagebox.showinfo("Redo English", "Click a job on the left first.")
            return
        if self.rebuild_process and self.rebuild_process.poll() is None:
            messagebox.showinfo("Redo English", "Redo English is already running for another job.")
            return
        if self.worker_process and self.worker_process.poll() is None:
            messagebox.showinfo("Redo English", "Stop the full queue first, then try Redo English again.")
            return
        self._store_editor_draft(selected)
        try:
            self.service.save_job_notes(
                selected,
                batch_label=self._batch_label_value(),
                overall_context=self._context_value(),
                scene_contexts=self._scene_contexts_copy(),
            )
        except QueueError as exc:
            messagebox.showerror("Redo English", str(exc))
            return
        command = [self._cli_python(), "-m", "local_subtitle_stack.cli", "rebuild-english", selected]
        self.rebuild_process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            creationflags=no_window_creationflags(),
        )
        self.rebuild_job_id = selected
        self._set_rebuild_controls_enabled(False)
        self.status_var.set("Redoing the English subtitles in the background")
        self._poll_rebuild_process()

    def open_review_selected(self) -> None:
        selected = self._selected_job_id()
        if not selected:
            messagebox.showinfo("Open in Subtitle Edit", "Click a job on the left first.")
            return
        try:
            self.service.open_review(selected)
        except QueueError as exc:
            messagebox.showerror("Open in Subtitle Edit", str(exc))

    def open_output_selected(self) -> None:
        selected = self._selected_job_id()
        if not selected:
            messagebox.showinfo("Open subtitle folder", "Click a job on the left first.")
            return
        try:
            self.service.open_output_folder(selected)
        except QueueError as exc:
            messagebox.showerror("Open subtitle folder", str(exc))

    def reload_selected_line_editor(self) -> None:
        selected = self._selected_job_id()
        if not selected:
            messagebox.showinfo("Reload selected line", "Click a job on the left first.")
            return
        if self.line_editor_cue_index is None:
            messagebox.showinfo("Reload selected line", "Click one subtitle line first.")
            return
        self.preview_selected_cue_indexes = [self.line_editor_cue_index]
        self._store_editor_draft(selected)
        self._load_job_details(selected, force_reload=True)

    def save_selected_line_edit(self) -> None:
        selected = self._selected_job_id()
        if not selected:
            messagebox.showinfo("Save line changes", "Click a job on the left first.")
            return
        cue_index = self.line_editor_cue_index
        if cue_index is None:
            messagebox.showinfo("Save line changes", "Click one subtitle line first.")
            return
        row = self.preview_row_data.get(cue_index)
        if row is None:
            messagebox.showerror("Save line changes", "Could not find the selected subtitle line.")
            return
        try:
            self.service.update_subtitle_line(
                selected,
                cue_index=cue_index,
                japanese_text=(
                    self.line_editor_japanese_text.get("1.0", tk.END).strip()
                    if bool(row.get("has_japanese"))
                    else None
                ),
                literal_english_text=(
                    self.line_editor_literal_text.get("1.0", tk.END).strip()
                    if bool(row.get("has_literal_english"))
                    else None
                ),
                adapted_english_text=(
                    self.line_editor_adapted_text.get("1.0", tk.END).strip()
                    if bool(row.get("has_adapted_english"))
                    else None
                ),
            )
        except QueueError as exc:
            messagebox.showerror("Save line changes", str(exc))
            return
        self.preview_selected_cue_indexes = [cue_index]
        self._store_editor_draft(selected)
        self._load_job_details(selected, force_reload=True)
        self.status_var.set(f"Saved changes for subtitle line {cue_index}")

    def add_scene_block(self) -> None:
        selected = self._selected_job_id()
        if not selected:
            messagebox.showinfo("Add note", "Click a job on the left first.")
            return
        start_text = self.note_start_var.get().strip()
        end_text = self.note_end_var.get().strip()
        notes = self.range_notes_value()
        if not start_text or not end_text or not notes:
            messagebox.showinfo("Add note", "Fill in the time range and the note first.")
            return
        try:
            start_seconds, end_seconds = self._selected_range_seconds()
        except ValueError as exc:
            messagebox.showerror("Add note", str(exc))
            return
        if any(
            block.start_seconds == start_seconds
            and block.end_seconds == end_seconds
            and block.notes == notes
            for block in self.scene_contexts
        ):
            messagebox.showinfo("Add note", "That same note is already on this job.")
            return
        self.scene_contexts.append(
            SceneContextBlock(start_seconds=start_seconds, end_seconds=end_seconds, notes=notes)
        )
        self.scene_contexts.sort(key=lambda item: (item.start_seconds, item.end_seconds, item.notes))
        self._render_scene_blocks()
        self.note_start_var.set("")
        self.note_end_var.set("")
        self.range_notes_text.delete("1.0", tk.END)
        self.clear_preview_marked_range(clear_time_boxes=False, focus_note_box=False, clear_selection=False)

    def remove_scene_block(self) -> None:
        selection = self.note_tree.selection()
        if not selection:
            messagebox.showinfo("Remove selected note", "Click a note first.")
            return
        indexes = sorted((self.note_tree.index(item_id) for item_id in selection), reverse=True)
        for index in indexes:
            del self.scene_contexts[index]
        self._render_scene_blocks()

    def clear_scene_blocks(self) -> None:
        self.scene_contexts.clear()
        self._render_scene_blocks()

    def use_selected_lines_for_note_range(self) -> None:
        selection = self.preview_tree.selection()
        if not selection:
            messagebox.showinfo("Use selected lines", "Highlight some subtitle lines first.")
            return
        ordered_selection = [item_id for item_id in self.preview_tree.get_children() if item_id in selection]
        if ordered_selection:
            self.preview_mark_start_item = str(ordered_selection[0])
            self.preview_mark_end_item = str(ordered_selection[-1])
            self.preview_selected_cue_indexes = [
                cue_index
                for item_id in ordered_selection
                if (cue_index := cue_index_from_item_id(str(item_id))) is not None
            ]
        starts: list[float] = []
        ends: list[float] = []
        for item_id in selection:
            start_value, end_value = self.preview_ranges.get(str(item_id), (0.0, 0.0))
            starts.append(start_value)
            ends.append(end_value)
        self.note_start_var.set(format_timecode(min(starts)))
        self.note_end_var.set(format_timecode(max(ends)))
        self._update_marked_range_status()
        self.range_notes_text.focus_set()
        self.preview_hint_var.set(
            "The selected lines filled the time boxes. Now type what that part of the scene is about."
        )

    def mark_preview_start_line(self) -> None:
        item_id = self._current_preview_line_id()
        if not item_id:
            messagebox.showinfo("Mark start line", "Click one subtitle line first.")
            return
        self.preview_mark_start_item = item_id
        self.preview_mark_end_item = None
        start_value, _end_value = self.preview_ranges.get(item_id, (0.0, 0.0))
        self.note_start_var.set(format_timecode(start_value))
        self.note_end_var.set("")
        self.preview_tree.selection_set((item_id,))
        cue_index = cue_index_from_item_id(item_id)
        self.preview_selected_cue_indexes = [cue_index] if cue_index is not None else []
        self.preview_tree.focus(item_id)
        self.preview_tree.see(item_id)
        self.preview_hint_var.set(
            "Start line saved. Click the last line for this note, then press Mark end line."
        )
        self._update_marked_range_status()

    def mark_preview_end_line(self) -> None:
        item_id = self._current_preview_line_id()
        if not item_id:
            messagebox.showinfo("Mark end line", "Click one subtitle line first.")
            return
        if not self.preview_mark_start_item:
            messagebox.showinfo("Mark end line", "Press Mark start line first.")
            return
        self.preview_mark_end_item = item_id
        range_items = self._current_marked_preview_range()
        if not range_items:
            messagebox.showerror("Mark end line", "Could not build a subtitle range from those lines.")
            return
        starts = [self.preview_ranges[item][0] for item in range_items]
        ends = [self.preview_ranges[item][1] for item in range_items]
        self.note_start_var.set(format_timecode(min(starts)))
        self.note_end_var.set(format_timecode(max(ends)))
        self.preview_tree.selection_set(range_items)
        self.preview_selected_cue_indexes = [
            cue_index
            for selected_item in range_items
            if (cue_index := cue_index_from_item_id(selected_item)) is not None
        ]
        self.preview_tree.focus(range_items[-1])
        self.preview_tree.see(range_items[0])
        self.preview_tree.see(range_items[-1])
        self.range_notes_text.focus_set()
        self.preview_hint_var.set(
            "The marked range filled the time boxes. Now type what that part of the scene is about."
        )
        self._update_marked_range_status()

    def clear_preview_marked_range(
        self,
        *,
        clear_time_boxes: bool = True,
        focus_note_box: bool = False,
        clear_selection: bool = True,
    ) -> None:
        self.preview_mark_start_item = None
        self.preview_mark_end_item = None
        if clear_time_boxes:
            self.note_start_var.set("")
            self.note_end_var.set("")
        if clear_selection and self.preview_tree.get_children():
            self.preview_tree.selection_remove(self.preview_tree.selection())
            self.preview_selected_cue_indexes = []
        if focus_note_box:
            self.range_notes_text.focus_set()
        self._update_marked_range_status()
        self.preview_hint_var.set(
            "Click one line, press Mark start line, click the last line, then press Mark end line."
        )

    def _selected_range_seconds(self) -> tuple[float, float]:
        start_text = self.note_start_var.get().strip()
        end_text = self.note_end_var.get().strip()
        try:
            start_seconds = self._timecode_to_seconds(start_text)
            end_seconds = self._timecode_to_seconds(end_text)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if end_seconds <= start_seconds:
            raise ValueError("The end time must be after the start time.")
        return start_seconds, end_seconds

    def _timecode_to_seconds(self, value: str) -> float:
        try:
            return parse_timecode(value)
        except ValueError as exc:
            raise ValueError("Use MM:SS or HH:MM:SS in the time boxes.") from exc

    def _on_job_selected(self, _event: object | None = None) -> None:
        selected = self._selected_job_id()
        if not selected:
            return
        if selected == self.current_job_id and selected == self.loaded_job_id:
            return
        self._store_editor_draft(self.current_job_id)
        self.current_job_id = selected
        self._load_job_details(selected)

    def _load_job_details(self, job_id: str, *, force_reload: bool = False) -> None:
        try:
            _job_dir, manifest = self.service.load_job(job_id)
            rows = self.service.preview_rows(job_id)
        except QueueError as exc:
            messagebox.showerror("Load job", str(exc))
            return

        draft = self.editor_drafts.get(job_id)
        if draft is None:
            draft = self._editor_draft_from_manifest(manifest)
            self.editor_drafts[job_id] = draft

        self.selected_file_var.set(manifest.source_name)
        self.selected_job_state_var.set(
            f"{STATUS_LABELS.get(manifest.status, manifest.status)} | "
            f"{STAGE_LABELS.get(manifest.current_stage, manifest.current_stage)}"
        )
        self.batch_label_var.set(draft.batch_label)
        self.context_text.delete("1.0", tk.END)
        if draft.overall_context:
            self.context_text.insert("1.0", draft.overall_context)
        self.note_start_var.set(draft.note_start)
        self.note_end_var.set(draft.note_end)
        self.range_notes_text.delete("1.0", tk.END)
        if draft.range_notes:
            self.range_notes_text.insert("1.0", draft.range_notes)
        self.scene_contexts = [
            SceneContextBlock(
                start_seconds=block.start_seconds,
                end_seconds=block.end_seconds,
                notes=block.notes,
            )
            for block in draft.scene_contexts
        ]
        self._render_scene_blocks()
        self._render_preview_rows(rows, draft.selected_cue_indexes)
        self.preview_mark_start_item = (
            preview_item_id(draft.marked_start_cue_index)
            if draft.marked_start_cue_index is not None and preview_item_id(draft.marked_start_cue_index) in self.preview_ranges
            else None
        )
        self.preview_mark_end_item = (
            preview_item_id(draft.marked_end_cue_index)
            if draft.marked_end_cue_index is not None and preview_item_id(draft.marked_end_cue_index) in self.preview_ranges
            else None
        )
        self.loaded_job_id = job_id
        self._update_marked_range_status()
        if len(draft.selected_cue_indexes) == 1 and draft.selected_cue_indexes[0] in self.preview_row_data:
            self._load_line_editor_for_cue(draft.selected_cue_indexes[0])
        else:
            self._clear_line_editor()
        if rows:
            if force_reload:
                self.preview_hint_var.set(
                    "Subtitle lines were reloaded. Your draft notes stayed in place."
                )
            else:
                self.preview_hint_var.set(
                    "Highlight nearby lines, or mark a start and end line, then add a helper note."
                )
        else:
            self.preview_hint_var.set(
                "This job does not have subtitle lines yet. Start processing first, then click it again."
            )

    def _render_preview_rows(
        self,
        rows: list[dict[str, str | float | int]],
        selected_cue_indexes: list[int] | None = None,
    ) -> None:
        for item_id in self.preview_tree.get_children():
            self.preview_tree.delete(item_id)
        self.preview_ranges = {}
        self.preview_row_data = {}
        selected_item_ids: list[str] = []
        for row in rows:
            time_label = f"{format_timecode(float(row['start']))} - {format_timecode(float(row['end']))}"
            cue_index = int(row["cue_index"])
            item_id = preview_item_id(cue_index)
            self.preview_tree.insert(
                "",
                tk.END,
                iid=item_id,
                values=(
                    time_label,
                    wrap_preview_text(str(row["japanese"]), 18),
                    wrap_preview_text(str(row["literal_english"]), 28),
                    wrap_preview_text(str(row["adapted_english"]), 32),
                ),
            )
            self.preview_ranges[item_id] = (float(row["start"]), float(row["end"]))
            self.preview_row_data[cue_index] = dict(row)
            if selected_cue_indexes and cue_index in selected_cue_indexes:
                selected_item_ids.append(item_id)
        self.preview_selected_cue_indexes = list(selected_cue_indexes or [])
        self.preview_tree["displaycolumns"] = ("time", "japanese", "literal", "adapted")
        if selected_item_ids:
            self.preview_tree.update_idletasks()
            self.preview_tree.selection_remove(self.preview_tree.selection())
            for item_id in selected_item_ids:
                self.preview_tree.selection_add(item_id)
            self.preview_tree.focus(selected_item_ids[-1])
            self.preview_tree.see(selected_item_ids[0])
            self.preview_tree.see(selected_item_ids[-1])

    def _render_scene_blocks(self) -> None:
        for item_id in self.note_tree.get_children():
            self.note_tree.delete(item_id)
        for block in self.scene_contexts:
            self.note_tree.insert(
                "",
                tk.END,
                values=(
                    format_timecode(block.start_seconds),
                    format_timecode(block.end_seconds),
                    block.notes,
                ),
            )

    def _editor_draft_from_manifest(self, manifest) -> JobEditorDraft:
        return JobEditorDraft(
            batch_label=manifest.series or "",
            overall_context=manifest.job_context or "",
            scene_contexts=[
                SceneContextBlock(
                    start_seconds=block.start_seconds,
                    end_seconds=block.end_seconds,
                    notes=block.notes,
                )
                for block in manifest.scene_contexts
            ],
        )

    def _selected_preview_cue_indexes(self) -> list[int]:
        indexes: list[int] = []
        for item_id in self.preview_tree.selection():
            cue_index = cue_index_from_item_id(str(item_id))
            if cue_index is not None:
                indexes.append(cue_index)
        if indexes:
            return indexes
        return list(self.preview_selected_cue_indexes)

    def _store_editor_draft(self, job_id: str | None) -> None:
        if not job_id:
            return
        self.editor_drafts[job_id] = JobEditorDraft(
            batch_label=self.batch_label_var.get().strip(),
            overall_context=self.context_text.get("1.0", tk.END).strip(),
            note_start=self.note_start_var.get().strip(),
            note_end=self.note_end_var.get().strip(),
            range_notes=self.range_notes_text.get("1.0", tk.END).strip(),
            scene_contexts=self._scene_contexts_copy(),
            selected_cue_indexes=self._selected_preview_cue_indexes(),
            marked_start_cue_index=cue_index_from_item_id(self.preview_mark_start_item or ""),
            marked_end_cue_index=cue_index_from_item_id(self.preview_mark_end_item or ""),
        )

    def _update_marked_range_status(self) -> None:
        if self.preview_mark_start_item and self.preview_mark_start_item in self.preview_ranges:
            start_seconds = self.preview_ranges[self.preview_mark_start_item][0]
            if self.preview_mark_end_item and self.preview_mark_end_item in self.preview_ranges:
                end_seconds = self.preview_ranges[self.preview_mark_end_item][1]
                start_value = min(start_seconds, self.preview_ranges[self.preview_mark_end_item][0])
                end_value = max(self.preview_ranges[self.preview_mark_start_item][1], end_seconds)
                self.marked_range_var.set(
                    f"Marked range: {format_timecode(start_value)} to {format_timecode(end_value)}"
                )
                return
            self.marked_range_var.set(f"Marked start: {format_timecode(start_seconds)}")
            return
        self.marked_range_var.set("Marked range: none")

    def _set_text_widget_value(self, widget: tk.Text, value: str, enabled: bool) -> None:
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        if value:
            widget.insert("1.0", value)
        widget.configure(state=tk.NORMAL if enabled else tk.DISABLED)

    def _clear_line_editor(self, message: str = "Click one subtitle line to edit it here.") -> None:
        self.line_editor_cue_index = None
        self.line_editor_time_var.set("")
        self.line_editor_status_var.set(message)
        for widget in (
            self.line_editor_japanese_text,
            self.line_editor_literal_text,
            self.line_editor_adapted_text,
        ):
            widget.configure(state=tk.NORMAL)
            widget.delete("1.0", tk.END)
            widget.configure(state=tk.DISABLED)
        self.save_line_button.configure(state=tk.DISABLED)
        self.reload_line_button.configure(state=tk.DISABLED)

    def _load_line_editor_for_cue(self, cue_index: int) -> None:
        row = self.preview_row_data.get(cue_index)
        if row is None:
            self._clear_line_editor()
            return
        self.line_editor_cue_index = cue_index
        self.line_editor_time_var.set(
            f"{format_timecode(float(row['start']))} - {format_timecode(float(row['end']))}"
        )
        self.line_editor_status_var.set(
            f"Editing subtitle line {cue_index}. Save changes to write them back into the subtitle files."
        )
        self._set_text_widget_value(
            self.line_editor_japanese_text,
            str(row["japanese"]),
            bool(row.get("has_japanese")),
        )
        self._set_text_widget_value(
            self.line_editor_literal_text,
            str(row["literal_english"]),
            bool(row.get("has_literal_english")),
        )
        self._set_text_widget_value(
            self.line_editor_adapted_text,
            str(row["adapted_english"]),
            bool(row.get("has_adapted_english")),
        )
        self.save_line_button.configure(state=tk.NORMAL)
        self.reload_line_button.configure(state=tk.NORMAL)

    def _selected_job_id(self) -> str | None:
        selection = self.job_tree.selection()
        if selection:
            return str(selection[0])
        return self.current_job_id

    def _current_profile_key(self) -> str:
        return PROFILE_KEYS_BY_LABEL.get(self.profile_var.get(), "conservative")

    def _current_preview_line_id(self) -> str | None:
        selection = self.preview_tree.selection()
        if selection:
            return str(selection[-1])
        focused = self.preview_tree.focus()
        if focused:
            return str(focused)
        return None

    def _current_marked_preview_range(self) -> list[str]:
        if not self.preview_mark_start_item or not self.preview_mark_end_item:
            return []
        return ordered_preview_range(
            list(self.preview_tree.get_children()),
            self.preview_mark_start_item,
            self.preview_mark_end_item,
        )

    def _batch_label_value(self) -> str | None:
        value = self.batch_label_var.get().strip()
        return value or None

    def _normalized_or_default(self, value: str, default: str) -> str:
        normalized = value.strip()
        return normalized or default

    def _normalized_optional(self, value: str, default: str) -> str:
        normalized = value.strip()
        return normalized or default

    def _sync_model_setting_vars(self) -> None:
        self.asr_model_var.set(self.service.config.models.asr)
        self.literal_model_var.set(self.service.config.models.literal_translation)
        self.adapted_model_var.set(self.service.config.models.adapted_translation)
        self.hf_cache_var.set(self.service.config.cache_paths.hf_hub_cache or "")

    def _context_value(self) -> str | None:
        value = self.context_text.get("1.0", tk.END).strip()
        return value or None

    def range_notes_value(self) -> str | None:
        value = self.range_notes_text.get("1.0", tk.END).strip()
        return value or None

    def _scene_contexts_copy(self) -> list[SceneContextBlock]:
        return [
            SceneContextBlock(
                start_seconds=block.start_seconds,
                end_seconds=block.end_seconds,
                notes=block.notes,
            )
            for block in self.scene_contexts
        ]

    def _on_preview_lines_selected(self, _event: object | None = None) -> None:
        selection = tuple(self.preview_tree.selection())
        if not selection:
            focused = self._current_preview_line_id()
            if focused:
                selection = (focused,)
        self.preview_selected_cue_indexes = [
            cue_index
            for item_id in selection
            if (cue_index := cue_index_from_item_id(str(item_id))) is not None
        ]
        if len(self.preview_selected_cue_indexes) == 1:
            self._load_line_editor_for_cue(self.preview_selected_cue_indexes[0])
        elif len(self.preview_selected_cue_indexes) >= 2:
            self._clear_line_editor(
                "Multiple subtitle lines are highlighted. Select just one line to edit its text here."
            )
        else:
            self._clear_line_editor()
        if self.preview_mark_start_item and self.preview_mark_end_item:
            self.preview_hint_var.set(
                "The marked range is ready. Type the helper note, then press Add note."
            )
        elif self.preview_mark_start_item:
            self.preview_hint_var.set(
                "Start line saved. Click the last line for this note, then press Mark end line."
            )
        elif len(selection) >= 2:
            self.preview_hint_var.set(
                "The highlighted lines are ready. Press Use highlighted lines to copy them into the note range."
            )
        elif len(selection) == 1:
            self.preview_hint_var.set(
                "If multi-select feels awkward, press Mark start line, then click another line and press Mark end line."
            )
        else:
            self.preview_hint_var.set(
                "Click one subtitle line to edit it, or highlight a few lines to build a helper note range."
            )

    def _worker_python(self) -> str:
        executable = Path(sys.executable)
        if executable.name.lower() == "python.exe":
            pythonw = executable.with_name("pythonw.exe")
            if pythonw.exists():
                return str(pythonw)
        return str(executable)

    def _cli_python(self) -> str:
        executable = Path(sys.executable)
        if executable.name.lower() == "pythonw.exe":
            python = executable.with_name("python.exe")
            if python.exists():
                return str(python)
        return str(executable)

    def _start_snapshot_thread(self) -> None:
        thread = threading.Thread(target=self._snapshot_loop, name="subtitle-stack-snapshot", daemon=True)
        thread.start()

    def _snapshot_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                snapshot = capture_snapshot()
            except Exception:
                snapshot = None
            if snapshot is not None:
                with self.snapshot_lock:
                    self.latest_snapshot = snapshot
            self.stop_event.wait(5.0)

    def refresh(self, reschedule: bool = False) -> None:
        rows = self.service.status_rows()
        self._sync_job_rows(rows)
        rows_by_id = {row["job_id"]: row for row in rows}
        selected_row = rows_by_id.get(self.current_job_id or "")
        if selected_row is not None:
            self.selected_file_var.set(selected_row["source"])
            self.selected_job_state_var.set(
                f"{STATUS_LABELS.get(selected_row['status'], selected_row['status'])} | "
                f"{STAGE_LABELS.get(selected_row['stage'], selected_row['stage'])}"
            )
        elif self.current_job_id is not None:
            self.current_job_id = None
            self.loaded_job_id = None
            self.selected_file_var.set("Pick or click a job on the left.")
            self.selected_job_state_var.set("Nothing is selected yet.")

        with self.snapshot_lock:
            snapshot = self.latest_snapshot
        if snapshot is not None:
            self.memory_var.set(
                f"RAM free: {snapshot.free_ram_mb} MB | "
                f"VRAM free: {snapshot.gpu_free_mb or 0} MB"
            )

        if self.worker_process and self.worker_process.poll() is not None:
            self.worker_process = None

        if self.rebuild_process and self.rebuild_process.poll() is None:
            self.status_var.set("Redoing the English subtitles in the background")
        elif self.worker_process and self.worker_process.poll() is None:
            self.status_var.set("Processing is running in the background")
        elif self.service.store.pause_requested():
            self.status_var.set("The queue is waiting because you asked it to stop safely")
        elif not self.status_var.get().startswith(("Queued", "Saved", "English")):
            self.status_var.set("Ready")

        if reschedule:
            self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        if self.refresh_job is not None:
            self.after_cancel(self.refresh_job)
        self.refresh_job = self.after(2000, lambda: self.refresh(reschedule=True))

    def _sync_job_rows(self, rows: list[dict[str, str]]) -> None:
        existing_ids = set(self.job_tree.get_children())
        seen_ids: set[str] = set()
        for index, row in enumerate(rows):
            item_id = row["job_id"]
            values = (
                row["source"],
                STATUS_LABELS.get(row["status"], row["status"]),
                STAGE_LABELS.get(row["stage"], row["stage"]),
                row["updated_at"].replace("T", " "),
            )
            if item_id in existing_ids:
                self.job_tree.item(item_id, values=values)
                self.job_tree.move(item_id, "", index)
            else:
                self.job_tree.insert("", index, iid=item_id, values=values)
            seen_ids.add(item_id)
        for item_id in existing_ids - seen_ids:
            self.job_tree.delete(item_id)

    def _set_rebuild_controls_enabled(self, enabled: bool) -> None:
        state = tk.NORMAL if enabled else tk.DISABLED
        for widget in (
            self.start_processing_button,
            self.retry_selected_button,
            self.save_notes_button,
            self.redo_english_button,
            self.reload_lines_button,
        ):
            widget.configure(state=state)

    def _poll_rebuild_process(self) -> None:
        process = self.rebuild_process
        if process is None:
            self.rebuild_poll_job = None
            return
        return_code = process.poll()
        if return_code is None:
            self.rebuild_poll_job = self.after(500, self._poll_rebuild_process)
            return

        stdout, stderr = process.communicate()
        finished_job_id = self.rebuild_job_id
        self.rebuild_process = None
        self.rebuild_job_id = None
        self.rebuild_poll_job = None
        self._set_rebuild_controls_enabled(True)
        message = (stdout or stderr).strip()
        if return_code == 0:
            self.status_var.set("English subtitles were rebuilt for the selected job")
            if finished_job_id:
                self._store_editor_draft(finished_job_id)
                if self.current_job_id == finished_job_id:
                    self._load_job_details(finished_job_id, force_reload=True)
            self.refresh()
            return

        self.refresh()
        messagebox.showerror(
            "Redo English",
            message or "Redo English failed. The previous English subtitle files were kept.",
        )

    def _on_content_configure(self, _event: tk.Event[tk.Misc]) -> None:
        self.scroll_canvas.configure(scrollregion=self.scroll_canvas.bbox("all"))

    def _on_canvas_configure(self, event: tk.Event[tk.Misc]) -> None:
        self.scroll_canvas.itemconfigure(self.scroll_window, width=event.width)

    def _on_mousewheel(self, event: tk.Event[tk.Misc]) -> str:
        delta = getattr(event, "delta", 0)
        if delta:
            self.scroll_canvas.yview_scroll(int(-delta / 120), "units")
        return "break"

    def _on_close(self) -> None:
        self.stop_event.set()
        if self.refresh_job is not None:
            self.after_cancel(self.refresh_job)
            self.refresh_job = None
        if self.rebuild_poll_job is not None:
            self.after_cancel(self.rebuild_poll_job)
            self.rebuild_poll_job = None
        self.destroy()


def main() -> None:
    app = SubtitleStackApp()
    app.mainloop()


if __name__ == "__main__":
    main()
