param(
    [string]$VenvPath = ".venv311",
    [string]$ConfigPath = "",
    [switch]$SkipKotobaWarm,
    [switch]$SkipOllamaPull
)

$ErrorActionPreference = "Stop"
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"

$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repoRoot "$VenvPath\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "Virtual environment python was not found at $venvPython. Run scripts\bootstrap.ps1 first."
}

$env:PYTHONUTF8 = "1"

$configJson = @'
import json
from pathlib import Path
from local_subtitle_stack.config import DEFAULT_CONFIG_PATH, load_config

config_path = Path(r"__CONFIG_PATH__") if r"__CONFIG_PATH__" else DEFAULT_CONFIG_PATH
config = load_config(config_path if config_path.exists() else None)
payload = {
    "config_path": str(config.config_file_path),
    "hf_hub_cache": config.cache_paths.hf_hub_cache,
    "asr_model": config.models.asr,
    "literal_model": config.models.literal_translation,
    "adapted_model": config.models.adapted_translation,
}
print(json.dumps(payload))
'@
$configJson = $configJson.Replace("__CONFIG_PATH__", $ConfigPath)
$config = $configJson | & $venvPython - | ConvertFrom-Json

Write-Host "Using config $($config.config_path)"
Write-Host "ASR model      => $($config.asr_model)"
Write-Host "Literal model  => $($config.literal_model)"
Write-Host "Adapted model  => $($config.adapted_model)"

$hfCache = [string]$config.hf_hub_cache
if (-not $hfCache) {
    throw "cache_paths.hf_hub_cache is not configured. Set it in %LOCALAPPDATA%\SubtitleTool\config.toml before warming the Japanese model cache."
}
New-Item -ItemType Directory -Force -Path $hfCache | Out-Null
Write-Host "HF cache       => $hfCache"

$ollamaModels = [Environment]::GetEnvironmentVariable("OLLAMA_MODELS", "Process")
if (-not $ollamaModels) {
    $ollamaModels = [Environment]::GetEnvironmentVariable("OLLAMA_MODELS", "User")
}
if ($ollamaModels) {
    Write-Host "OLLAMA_MODELS  => $ollamaModels"
} else {
    Write-Host "OLLAMA_MODELS  => <default local store>"
}

if (-not $SkipKotobaWarm) {
    Write-Host ""
    Write-Host "Warming kotoba cache..."
    $warmScript = @'
from huggingface_hub import snapshot_download

snapshot_path = snapshot_download(
    repo_id=r"__ASR_MODEL__",
    cache_dir=r"__HF_CACHE__",
)
print(snapshot_path)
'@
    $warmScript = $warmScript.Replace("__ASR_MODEL__", [string]$config.asr_model)
    $warmScript = $warmScript.Replace("__HF_CACHE__", $hfCache)
    $snapshotPath = $warmScript | & $venvPython -
    Write-Host "kotoba cache ready at $snapshotPath"
}

if (-not $SkipOllamaPull) {
    Write-Host ""
    Write-Host "Checking Ollama models..."
    $ollama = Get-Command ollama -ErrorAction SilentlyContinue
    if (-not $ollama) {
        throw "Ollama is not installed or not on PATH."
    }

    $installed = & $ollama.Source list | Select-Object -Skip 1 | ForEach-Object {
        ($_ -split "\s+")[0]
    }

    $requiredModels = @([string]$config.literal_model, [string]$config.adapted_model) | Select-Object -Unique

    foreach ($model in $requiredModels) {
        if ($installed -contains $model) {
            Write-Host "[OK] $model"
            continue
        }
        Write-Host "[PULL] $model"
        & $ollama.Source pull $model
    }
}

Write-Host ""
Write-Host "Model preparation complete."
