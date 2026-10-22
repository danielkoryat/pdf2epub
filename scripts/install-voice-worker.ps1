# Installs the audiobook voice worker for the local app on this computer (Windows, NVIDIA GPU optional).
#
#   powershell -ExecutionPolicy Bypass -File scripts\install-voice-worker.ps1
#
# It installs the worker's own Python environment (voice-worker\, with Chatterbox unless -NoChatterbox). Nothing
# runs in the background: the local app (the desktop shortcut) starts the worker whenever it runs and stops it
# when its window is closed, so the audiobooks you make locally use this computer's processor and GPU. The
# public site makes its audiobooks on its own server.
#
# With an NVIDIA GPU (and Chatterbox, whose PyTorch brings the CUDA libraries) it also installs the GPU build of
# ONNX Runtime in voice-worker\.venv\onnxruntime-gpu, so BlueTTS runs on the GPU, about 3.5 times faster.
#
# It also removes what earlier versions set up to serve the public site from this computer: the sign-in task
# "pdf2epub voice worker" and the settings file with the site's token.

param(
    [switch]$NoChatterbox
)

$ErrorActionPreference = "Stop"
$project = Join-Path (Split-Path -Parent $PSScriptRoot) "voice-worker"
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { throw "uv was not found: install it from https://docs.astral.sh/uv/ first." }
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { throw "ffmpeg was not found on PATH: install it first." }

$task = Get-ScheduledTask -TaskName "pdf2epub voice worker" -ErrorAction SilentlyContinue
if ($task) {
    Stop-ScheduledTask -TaskName $task.TaskName
    Unregister-ScheduledTask -TaskName $task.TaskName -Confirm:$false
    Write-Output "Removed the sign-in task that made voices for the public site"
}
$config = Join-Path $env:APPDATA "pdf2epub-voice\config.json"
if (Test-Path $config) {
    Remove-Item $config
    Write-Output "Removed $config"
}

Write-Output "Installing the voice worker's environment (the first time this downloads a few GB)..."
$extra = @()
if (-not $NoChatterbox) { $extra = @("--extra", "chatterbox") }
& $uv sync --project $project @extra
if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }

# The GPU build installs the same Python package as the CPU one, so it goes in a folder of its own that the worker
# puts first (voice-worker\src\pdf2epub_voice\gpu.py). 1.26 is the last build for CUDA 12, which PyTorch has.
$gpuRuntime = Join-Path $project ".venv\onnxruntime-gpu"
if (Test-Path $gpuRuntime) { Remove-Item -Recurse -Force $gpuRuntime }
if (-not $NoChatterbox -and (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
    Write-Output "Installing ONNX Runtime for the GPU, for BlueTTS..."
    & $uv pip install --python (Join-Path $project ".venv\Scripts\python.exe") --target $gpuRuntime --no-deps "onnxruntime-gpu==1.26.0"
    if ($LASTEXITCODE -ne 0) { throw "Installing onnxruntime-gpu failed" }
}

& (Join-Path $project ".venv\Scripts\pdf2epub-voice.exe") check
Write-Output "Done. The local app (the desktop shortcut) now makes audiobook voices on this computer while it runs."
