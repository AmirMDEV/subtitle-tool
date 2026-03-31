param(
    [string]$VenvPath = ".venv311",
    [string]$TorchIndexUrl = "https://download.pytorch.org/whl/cu124",
    [switch]$SkipTorch,
    [switch]$Dev
)

$ErrorActionPreference = "Stop"

Write-Host "Checking Python 3.11..."
$python311 = & py -3.11 -c "import sys; print(sys.executable)"
if (-not $python311) {
    throw "Python 3.11 was not found. Install Python 3.11 before bootstrapping this repo."
}

$pythonVersion = & py -3.11 -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
Write-Host "Using Python $pythonVersion at $python311"

if (Test-Path $VenvPath) {
    Write-Host "Virtual environment already exists at $VenvPath"
} else {
    & py -3.11 -m venv $VenvPath
}

$venvPython = Join-Path $VenvPath "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "Expected virtual environment python at $venvPython"
}

Write-Host "Upgrading pip..."
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install wheel

if (-not $SkipTorch) {
    Write-Host "Installing CUDA-enabled PyTorch from $TorchIndexUrl ..."
    & $venvPython -m pip install torch torchvision torchaudio --index-url $TorchIndexUrl
}

Write-Host "Installing project dependencies..."
if ($Dev) {
    & $venvPython -m pip install -e ".[dev]" --no-build-isolation
} else {
    & $venvPython -m pip install -e . --no-build-isolation
}

Write-Host "Checking required tools..."
$tools = @{
    ffmpeg = (Get-Command ffmpeg -ErrorAction SilentlyContinue)
    ffprobe = (Get-Command ffprobe -ErrorAction SilentlyContinue)
    ollama = (Get-Command ollama -ErrorAction SilentlyContinue)
}

foreach ($name in $tools.Keys) {
    if (-not $tools[$name]) {
        throw "Missing required tool: $name"
    }
    Write-Host "$name => $($tools[$name].Source)"
}

$subtitleEditCandidates = @(
    "C:\Program Files\Subtitle Edit\SubtitleEdit.exe",
    "C:\Program Files (x86)\Subtitle Edit\SubtitleEdit.exe",
    "$env:LOCALAPPDATA\Programs\Subtitle Edit\SubtitleEdit.exe"
)
$subtitleEdit = $subtitleEditCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $subtitleEdit) {
    throw "Subtitle Edit was not found."
}
Write-Host "Subtitle Edit => $subtitleEdit"

Write-Host "Bootstrap complete."
Write-Host "Activate the environment with:"
Write-Host "  $VenvPath\Scripts\Activate.ps1"
