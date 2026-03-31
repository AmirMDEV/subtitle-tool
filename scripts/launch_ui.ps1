$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $repoRoot ".venv311\Scripts\pythonw.exe"

if (-not (Test-Path $pythonw)) {
    throw "pythonw.exe was not found at $pythonw. Set up .venv311 first."
}

Start-Process -FilePath $pythonw -ArgumentList "-m", "local_subtitle_stack.ui" -WorkingDirectory $repoRoot
