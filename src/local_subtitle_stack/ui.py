from __future__ import annotations

import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .app import build_service
from .config import CachePaths, ModelConfig, save_config
from .domain import SceneContextBlock
from .guards import ResourceSnapshot, capture_snapshot
from .queue import QueueError
from .utils import format_timecode, no_window_creationflags, parse_timecode

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


class SubtitleStackApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Japanese Subtitle Tool")
        self.geometry("1480x920")
        self.minsize(1100, 680)
        self.service = build_service()
        self.worker_process: subprocess.Popen[str] | None = None
        self.refresh_job: str | None = None
        self.snapshot_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.latest_snapshot: ResourceSnapshot | None = None
        self.scene_contexts: list[SceneContextBlock] = []
        self.current_job_id: str | None = None
        self.preview_ranges: dict[str, tuple[float, float]] = {}
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
        ttk.Button(
            action_bar,
            text="Start processing",
            command=self.start_worker,
            style="Primary.TButton",
        ).pack(side=tk.LEFT, padx=6)
        ttk.Button(action_bar, text="Stop safely", command=self.pause_worker).pack(side=tk.LEFT, padx=6)
        ttk.Button(action_bar, text="Retry selected job", command=self.retry_selected_job).pack(side=tk.LEFT, padx=6)

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
        ttk.Label(preview_header, textvariable=self.preview_hint_var, style="Hint.TLabel", wraplength=850).pack(
            side=tk.LEFT
        )
        ttk.Button(preview_header, text="Reload lines", command=self.reload_selected_preview).pack(side=tk.RIGHT)
        ttk.Button(
            preview_header,
            text="Use selected lines for a note",
            command=self.use_selected_lines_for_note_range,
        ).pack(side=tk.RIGHT, padx=6)

        preview_tree_frame = ttk.Frame(preview_frame, padding=(10, 0, 10, 10))
        preview_tree_frame.pack(fill=tk.BOTH, expand=True)
        preview_columns = ("time", "japanese", "literal", "adapted")
        self.preview_tree = ttk.Treeview(
            preview_tree_frame,
            columns=preview_columns,
            show="headings",
            selectmode="extended",
            height=14,
        )
        self.preview_tree.heading("time", text="Time")
        self.preview_tree.heading("japanese", text="Japanese")
        self.preview_tree.heading("literal", text="Direct English")
        self.preview_tree.heading("adapted", text="Easy English")
        self.preview_tree.column("time", width=150, anchor=tk.W)
        self.preview_tree.column("japanese", width=230, anchor=tk.W)
        self.preview_tree.column("literal", width=260, anchor=tk.W)
        self.preview_tree.column("adapted", width=300, anchor=tk.W)
        self.preview_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        preview_y = ttk.Scrollbar(preview_tree_frame, orient=tk.VERTICAL, command=self.preview_tree.yview)
        preview_y.pack(side=tk.RIGHT, fill=tk.Y)
        preview_x = ttk.Scrollbar(preview_frame, orient=tk.HORIZONTAL, command=self.preview_tree.xview)
        preview_x.pack(fill=tk.X, padx=10, pady=(0, 10))
        self.preview_tree.configure(yscrollcommand=preview_y.set, xscrollcommand=preview_x.set)

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
        ttk.Button(note_button_row, text="Add note", command=self.add_scene_block).pack(side=tk.LEFT)
        ttk.Button(note_button_row, text="Remove selected note", command=self.remove_scene_block).pack(
            side=tk.LEFT,
            padx=6,
        )
        ttk.Button(note_button_row, text="Clear all notes", command=self.clear_scene_blocks).pack(side=tk.LEFT)

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
        ttk.Button(bottom_actions, text="Save notes to this job", command=self.save_notes_selected).pack(side=tk.LEFT)
        ttk.Button(
            bottom_actions,
            text="Redo English for this job",
            command=self.redo_english_selected,
            style="Primary.TButton",
        ).pack(side=tk.LEFT, padx=6)
        ttk.Button(bottom_actions, text="Open in Subtitle Edit", command=self.open_review_selected).pack(
            side=tk.LEFT,
            padx=6,
        )
        ttk.Button(bottom_actions, text="Open subtitle folder", command=self.open_output_selected).pack(
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
        self._load_job_details(selected)

    def save_notes_selected(self) -> None:
        selected = self._selected_job_id()
        if not selected:
            messagebox.showinfo("Save notes", "Click a job on the left first.")
            return
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
        self.refresh()

    def redo_english_selected(self) -> None:
        selected = self._selected_job_id()
        if not selected:
            messagebox.showinfo("Redo English", "Click a job on the left first.")
            return
        try:
            self.service.rebuild_english(
                selected,
                batch_label=self._batch_label_value(),
                overall_context=self._context_value(),
                scene_contexts=self._scene_contexts_copy(),
            )
        except QueueError as exc:
            messagebox.showerror("Redo English", str(exc))
            return
        self.status_var.set("English subtitles were rebuilt for the selected job")
        self._load_job_details(selected)
        self.refresh()

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
        starts: list[float] = []
        ends: list[float] = []
        for item_id in selection:
            start_value, end_value = self.preview_ranges.get(str(item_id), (0.0, 0.0))
            starts.append(start_value)
            ends.append(end_value)
        self.note_start_var.set(format_timecode(min(starts)))
        self.note_end_var.set(format_timecode(max(ends)))
        self.range_notes_text.focus_set()
        self.preview_hint_var.set(
            "The selected lines filled the time boxes. Now type what that part of the scene is about."
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
        self.current_job_id = selected
        self._load_job_details(selected)

    def _load_job_details(self, job_id: str) -> None:
        try:
            _job_dir, manifest = self.service.load_job(job_id)
            rows = self.service.preview_rows(job_id)
        except QueueError as exc:
            messagebox.showerror("Load job", str(exc))
            return

        self.selected_file_var.set(manifest.source_name)
        self.selected_job_state_var.set(
            f"{STATUS_LABELS.get(manifest.status, manifest.status)} | "
            f"{STAGE_LABELS.get(manifest.current_stage, manifest.current_stage)}"
        )
        self.batch_label_var.set(manifest.series or "")
        self.context_text.delete("1.0", tk.END)
        if manifest.job_context:
            self.context_text.insert("1.0", manifest.job_context)
        self.note_start_var.set("")
        self.note_end_var.set("")
        self.range_notes_text.delete("1.0", tk.END)
        self.scene_contexts = [
            SceneContextBlock(
                start_seconds=block.start_seconds,
                end_seconds=block.end_seconds,
                notes=block.notes,
            )
            for block in manifest.scene_contexts
        ]
        self._render_scene_blocks()
        self._render_preview_rows(rows)
        if rows:
            self.preview_hint_var.set(
                "Highlight nearby lines, press Use selected lines for a note, then press Redo English."
            )
        else:
            self.preview_hint_var.set(
                "This job does not have subtitle lines yet. Start processing first, then click it again."
            )

    def _render_preview_rows(self, rows: list[dict[str, str | float | int]]) -> None:
        for item_id in self.preview_tree.get_children():
            self.preview_tree.delete(item_id)
        self.preview_ranges = {}
        for row in rows:
            time_label = f"{format_timecode(float(row['start']))} - {format_timecode(float(row['end']))}"
            item_id = f"cue-{int(row['cue_index'])}"
            self.preview_tree.insert(
                "",
                tk.END,
                iid=item_id,
                values=(
                    time_label,
                    row["japanese"],
                    row["literal_english"],
                    row["adapted_english"],
                ),
            )
            self.preview_ranges[item_id] = (float(row["start"]), float(row["end"]))
        self.preview_tree["displaycolumns"] = ("time", "japanese", "literal", "adapted")

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

    def _selected_job_id(self) -> str | None:
        selection = self.job_tree.selection()
        if not selection:
            return None
        return str(selection[0])

    def _current_profile_key(self) -> str:
        return PROFILE_KEYS_BY_LABEL.get(self.profile_var.get(), "conservative")

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

    def _worker_python(self) -> str:
        executable = Path(sys.executable)
        if executable.name.lower() == "python.exe":
            pythonw = executable.with_name("pythonw.exe")
            if pythonw.exists():
                return str(pythonw)
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
        selected = self.current_job_id
        rows = self.service.status_rows()
        for row_id in self.job_tree.get_children():
            self.job_tree.delete(row_id)
        available_ids: list[str] = []
        for row in rows:
            available_ids.append(row["job_id"])
            self.job_tree.insert(
                "",
                tk.END,
                iid=row["job_id"],
                values=(
                    row["source"],
                    STATUS_LABELS.get(row["status"], row["status"]),
                    STAGE_LABELS.get(row["stage"], row["stage"]),
                    row["updated_at"].replace("T", " "),
                ),
            )

        if selected and selected in available_ids:
            self.job_tree.selection_set(selected)
            self.job_tree.see(selected)

        with self.snapshot_lock:
            snapshot = self.latest_snapshot
        if snapshot is not None:
            self.memory_var.set(
                f"RAM free: {snapshot.free_ram_mb} MB | "
                f"VRAM free: {snapshot.gpu_free_mb or 0} MB"
            )

        if self.worker_process and self.worker_process.poll() is None:
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
        self.destroy()


def main() -> None:
    app = SubtitleStackApp()
    app.mainloop()


if __name__ == "__main__":
    main()
