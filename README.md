# Local Japanese to English subtitles (ASR + LLM)

Desktop app for creating Japanese-to-English subtitles locally with Kotoba ASR and Ollama, then reinterpreting the English using whole-video notes or scene-specific notes.

It is made for long videos, batch jobs, and careful subtitle cleanup.

![Subtitle Tool overview](docs/images/app-overview.png)

![Subtitle Tool context notes](docs/images/app-context-notes.png)

![Import existing subtitles](docs/images/app-import-existing.png)

## Features

- Batch queue for long videos
- Windows `.exe` release
- Safe default profile for smaller laptops
- Japanese transcription
- Direct English subtitle pass
- Natural English subtitle pass
- Whole-video helper notes
- Time-range helper notes
- Optional reference subtitle track
- Import existing `.srt` files from a video or as subtitle-only jobs
- Inline subtitle editing by double-clicking subtitle cells
- Quick edit panel for the selected line
- Resume-friendly local job state and checkpoints
- Exported subtitle folder beside the source video
- Optional Subtitle Edit handoff for final review
- Model settings inside the app
- Japanese model cache can live on another drive or a network drive

## What You Get

When a job finishes, the app saves files like these:

- `video.ja.srt`
- `video.en.literal.srt`
- `video.en.adapted.srt`
- `video.review.json`
- `video.job.json`

They are saved in a folder named like this:

- `video.mp4 subtitles`

## Fastest Way To Use It

1. Download the newest release files from the [Releases](https://github.com/AmirMDEV/local-japanese-to-english-subtitles-asr-llm/releases) page.
2. Put every `.zip` file from that release into the same folder.
3. Unzip them all into the same place, like `C:\SubtitleTool`.
4. If Windows asks whether to merge folders, say `Yes`.
5. Run `SubtitleTool.exe`.
6. Install the helper tools below one time.
7. Pick your model settings in the app.
8. Add videos or import subtitles.
9. Press `Start processing`.

## Things You Need First

You only need to install these once:

- [Ollama](https://ollama.com/download) for the English subtitle model
- [FFmpeg](https://ffmpeg.org/download.html) so the app can read video audio
- [Subtitle Edit](https://www.nikse.dk/subtitleedit) if you want one-click review in Subtitle Edit

The app itself does not ship with the big AI models inside it. You pick where those go.

## Release Files Explained

The Windows app is split into a few `.zip` files because the AI runtime is large.

That does not mean you have to install lots of different apps.

It just means:

1. Download every `.zip` file for the same release.
2. Extract them into the same folder.
3. Let Windows merge the folders together.
4. Run `SubtitleTool.exe`.

## Super Easy Setup

### 1. Install Ollama

1. Go to [ollama.com/download](https://ollama.com/download).
2. Install Ollama.
3. Open PowerShell.
4. Run this:

```powershell
ollama pull qwen3:4b-q8_0
```

That gives you a good small English model to start with.

### 2. Install FFmpeg

1. Install FFmpeg.
2. Make sure `ffmpeg` and `ffprobe` are on your `PATH`.
3. If you are not sure, open PowerShell and try:

```powershell
ffmpeg -version
ffprobe -version
```

If both commands answer back, you are good.

### 3. Open the App

Run `SubtitleTool.exe`.

At the top you will see a section called `Model and cache settings`.

### 4. Pick Where the Japanese Model Lives

In the app:

1. Find `Japanese model cache folder`.
2. Click `Pick folder`.
3. Choose where you want the Japanese model files to live.

Good examples:

- `D:\AI Models\Japanese`
- `E:\Models\kotoba`
- a network drive folder if that works well for you

This folder is where the Japanese transcription model gets downloaded the first time you use it.

### 5. Check the Model Names

The app starts with recommended defaults:

- Japanese model: `kotoba-tech/kotoba-whisper-v1.1`
- Direct English model: `qwen3:4b-q8_0`
- Natural English model: `qwen3:4b-q8_0`

If those are already in the boxes, you can keep them.

Then click:

- `Save model settings`

### 6. First Transcription Run

Now try one small video first:

1. Click `Add video files`.
2. Pick a Japanese video.
3. Click `Start processing`.

The first Japanese run may take longer because the Japanese model needs to download into the cache folder you picked.

## How To Use The App

## Normal video workflow

1. Click `Add video files` or `Add a folder`.
2. Pick `Safe and steady (recommended)` if you want the safer profile.
3. Click `Start processing`.
4. Click a finished job on the left.
5. Read the subtitle lines on the right.
6. If something looks wrong, add notes and press `Redo English for this job`.

## Import existing subtitles

Use this when you already have `.srt` files and want to edit or reprocess them.

1. Click `Import existing subtitles`.
2. Choose `From video` if you have the video too.
3. Choose `Subtitle files only` if you just have subtitles.
4. Add the subtitle files you have:
   - `Japanese`
   - `Direct English`
   - `Easy English`
   - `Reference`
5. Press `Import`.

You do not need every file. The most important source track is:

- `Japanese`, or
- `Direct English` if you do not have Japanese

## How Context Notes Work

There are two kinds of notes:

- `Whole-video notes`
- `Time-range note`

### Whole-video notes

Use this for things that stay true across the whole video.

Examples:

- who the speakers are
- where the scene is
- special names
- what kind of topic they are talking about

### Time-range notes

Use this when only part of the video needs extra help.

Example:

- `00:10:00` to `00:18:00` is a hot spring scene
- `00:42:00` to `00:50:00` is a train station scene

The app uses the matching time-range note only for subtitle lines inside that part.

## The easiest way to add a time-range note

1. Click the subtitle line where the note should start.
2. Press `Mark start line`.
3. Click the subtitle line where the note should end.
4. Press `Mark end line`.
5. Type your helper note.
6. Press `Add note`.
7. Press `Redo English for this job`.

You can also highlight several lines and press `Use highlighted lines`.

## Editing subtitle text yourself

There are two ways:

### Fast way

- Double-click a subtitle cell
- Edit it right there
- Press `Save this line`

### Bigger editor

- Click one subtitle line
- Edit it in the `Quick edit selected line` box
- Press `Save line changes`

The app writes the changes back into the saved subtitle files for that job.

## Buttons explained simply

- `Add video files`: add one or more videos
- `Add a folder`: add a whole folder of videos
- `Import existing subtitles`: open subtitle files you already have
- `Start processing`: start the queue
- `Stop safely`: stop after the next safe checkpoint
- `Retry selected job`: try the selected job again
- `Redo English for this job`: rebuild the English using your notes
- `Open in Subtitle Edit`: open the finished subtitles in Subtitle Edit
- `Open subtitle folder`: open the folder beside the source video

## Good starter settings

If you are not sure what to use, start here:

- Speed mode: `Safe and steady (recommended)`
- Japanese model: `kotoba-tech/kotoba-whisper-v1.1`
- Direct English model: `qwen3:4b-q8_0`
- Natural English model: `qwen3:4b-q8_0`

## Network drives and external drives

You can keep source videos on another drive or a network drive.

You can also keep the Japanese model cache on another drive or a network drive by picking that folder in the app.

The app still keeps its active job state locally so short storage hiccups are less likely to ruin a long run.

## Build From Source

If you want to run from source instead of the release download:

```powershell
scripts\bootstrap.ps1 -Dev
scripts\launch_ui.ps1
```

To generate the example screenshots used in this README:

```powershell
python scripts\generate_demo_screenshots.py
```

To build the Windows app bundle:

```powershell
scripts\build_windows_exe.ps1
```

That creates a Windows release zip in `dist\`.

## Notes

- This app is focused on Japanese to English subtitle work.
- It works best on Windows.
- It uses local models and local tools. You stay in control of the files and the final review.
- Subtitle Edit is optional, but very useful for the last cleanup pass.

## License

MIT
