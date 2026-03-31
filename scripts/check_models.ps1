param(
    [string[]]$RequiredModels = @(
        "qwen3:4b-q8_0"
    )
)

$ErrorActionPreference = "Stop"

$ollama = Get-Command ollama -ErrorAction SilentlyContinue
if (-not $ollama) {
    throw "Ollama is not installed or not on PATH."
}

$ollamaModels = [Environment]::GetEnvironmentVariable("OLLAMA_MODELS", "Process")
if (-not $ollamaModels) {
    $ollamaModels = [Environment]::GetEnvironmentVariable("OLLAMA_MODELS", "User")
}

function Test-ModelInstalled {
    param(
        [string]$RequiredModel,
        [string[]]$InstalledModels
    )

    if ($InstalledModels -contains $RequiredModel) {
        return $true
    }
    if ($RequiredModel -notmatch ":") {
        return [bool]($InstalledModels | Where-Object { $_ -like "${RequiredModel}:*" } | Select-Object -First 1)
    }
    return $false
}

$models = & ollama list | Select-Object -Skip 1 | ForEach-Object {
    ($_ -split "\s+")[0]
}

if ($ollamaModels) {
    Write-Host "OLLAMA_MODELS => $ollamaModels"
    Write-Host ""
}

Write-Host "Installed Ollama models:"
$models | ForEach-Object { Write-Host " - $_" }

Write-Host ""
Write-Host "Required models:"
foreach ($model in ($RequiredModels | Select-Object -Unique)) {
    if (Test-ModelInstalled -RequiredModel $model -InstalledModels $models) {
        Write-Host " [OK]  $model"
    } else {
        Write-Host " [MISS] $model"
        Write-Host "        Run: ollama pull $model"
    }
}
