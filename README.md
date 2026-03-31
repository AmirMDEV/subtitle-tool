# Subtitle Tool

Windows-first local subtitle pipeline for long-form Japanese video transcription and English subtitle generation.

## What It Does

- Queues videos for background processing one at a time.
- Produces:
  - `*.ja.srt`
  - `*.en.literal.srt`
  - `*.en.adapted.srt`
  - `*.review.json`
  - `*.job.json`
- Exports the finished subtitle files beside each source video in a folder named `<source file name> subtitles`.
- Uses:
  - FFmpeg for audio prep
  - Transformers + CUDA for `kotoba-whisper-v1.1`
  - Ollama for literal and adapted translation
  - Subtitle Edit only for review
  - Global plus scene-ranged context notes for adapted-English disambiguation

## Setup

1. Run `scripts\bootstrap.ps1` from PowerShell.
2. Activate `.venv311`.
3. Check config at `%LOCALAPPDATA%\SubtitleTool\config.toml`.
4. Run `scripts\prepare_models.ps1` to warm the `kotoba` cache and pull the recommended Ollama models when they are missing.
   The default local translation path now uses `qwen3:4b-q8_0` for both literal and adapted passes because it behaved more reliably than `plamo-2-translate` in testing.

### NAS-Friendly Storage

- Source videos can live on a NAS path and be queued directly from that path.
- Queue state, checkpoints, partial subtitle files, and local processing chunks stay on the local machine so a NAS dropout does not lose job progress.
- `kotoba-whisper-v1.1` can be cached on the NAS by setting `cache_paths.hf_hub_cache` in `%LOCALAPPDATA%\SubtitleTool\config.toml`.
- Ollama model storage is controlled by the Ollama service itself. On Windows, Ollama documents `OLLAMA_MODELS` for changing the model store location.
- If Ollama is already using a NAS-backed model store, leave it there unless you have a specific reason to migrate it again.

Example config snippet:

```toml
[cache_paths]
hf_hub_cache = "\\\\YOUR-NAS\\AI\\hf-cache"
```

This repo now creates local processing chunks directly from the source video instead of requiring a full local extracted WAV, which keeps local SSD usage much lower for long NAS-hosted videos.

## Usage

```powershell
subtitle-stack enqueue "D:\Videos\example.mp4"
subtitle-stack worker
subtitle-stack status
subtitle-stack open-review
subtitle-stack open-output
```

For a lower-risk first run on an 8 GB VRAM laptop, start with the conservative profile:

```powershell
subtitle-stack enqueue "D:\Videos\example.mp4" --profile conservative
subtitle-stack worker
```

Queue a folder in one go:

```powershell
subtitle-stack enqueue "D:\Videos\sample-folder" --profile conservative --recursive
```

Folder scans skip videos that are already present in the queue history so you do not accidentally duplicate long-running jobs.

Add adapted-English context notes when the scene topic matters for disambiguation:

```powershell
subtitle-stack enqueue "D:\Videos\sample.mp4" `
  --context "The scene focuses on appearance comparison and family relationship wording."
```

If the worker refuses to start because free RAM is below the configured floor, close unrelated heavy apps or dev servers first and retry instead of lowering the guardrails blindly.

The stack now uses a stricter free-RAM floor for ASR than for translation, which is a better fit for 8 GB VRAM laptops running background jobs.

Run the Tkinter shell:

```powershell
python -m local_subtitle_stack.ui
```

To avoid attaching the GUI to a console window on Windows, use:

```powershell
scripts\launch_ui.ps1
```

The GUI is now built around a simpler review loop:

- `Add video files` or `Add a folder` to queue new work.
- Check `Model and cache settings` if you want to change the Japanese model, English models, or the Japanese model cache location.
- `Start processing` runs the queue in the background.
- Click a job on the left to load its subtitle lines on the right.
- Highlight the confusing lines and press `Use selected lines for a note`.
- Add a whole-video note or time-range note, then press `Redo English for this job`.
- If the time-range note list is blank, the app uses only the whole-video notes.
- If both note areas are blank, the translation runs normally with no extra context steering.
- `Open in Subtitle Edit` and `Open subtitle folder` are there for the final handoff.

The safe profile is now the default in both the GUI and CLI.
