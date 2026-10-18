# Sets up the audiobook voice worker on this computer (Windows, NVIDIA GPU optional) and starts it at every sign-in.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install-voice-worker.ps1 -Url https://pdf2epub.danielkoryat.com
#
# It asks for the site's voice token (PDF2EPUB_VOICE_TOKEN in the server's secrets.env), installs the worker's
# own Python environment (voice-worker\, with Chatterbox unless -NoChatterbox), writes the settings to
# %APPDATA%\pdf2epub-voice\config.json, and registers the task "pdf2epub voice worker" that runs it in the
# background at sign-in. The log is %LOCALAPPDATA%\pdf2epub-voice\worker.log. To remove it:
#   Unregister-ScheduledTask -TaskName "pdf2epub voice worker"

param(
    [string]$Url = "http://127.0.0.1:5050",
    [string]$Token = "",
    [string]$Name = $env:COMPUTERNAME,
    [switch]$NoChatterbox
)

$ErrorActionPreference = "Stop"
$project = Join-Path (Split-Path -Parent $PSScriptRoot) "voice-worker"
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { throw "uv was not found: install it from https://docs.astral.sh/uv/ first." }
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { throw "ffmpeg was not found on PATH: install it first." }

if (-not $Token -and -not $Url.StartsWith("http://127.0.0.1")) {
    $Token = Read-Host "Voice worker token for $Url"
}

Write-Output "Installing the voice worker's environment (the first time this downloads a few GB)..."
$extra = @()
if (-not $NoChatterbox) { $extra = @("--extra", "chatterbox") }
& $uv sync --project $project @extra
if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }

$configDir = Join-Path $env:APPDATA "pdf2epub-voice"
New-Item -ItemType Directory -Force $configDir | Out-Null
$config = @{ url = $Url; token = $Token; name = $Name } | ConvertTo-Json
[IO.File]::WriteAllText((Join-Path $configDir "config.json"), $config)  # UTF-8 without a byte order mark

# pythonw: no console window. The worker keeps Windows awake while it reads a book.
$pythonw = Join-Path $project ".venv\Scripts\pythonw.exe"
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-X utf8 -m pdf2epub_voice run" -WorkingDirectory $project
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 5) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "pdf2epub voice worker" -Action $action -Trigger $trigger -Settings $settings `
    -Description "Makes the voices of pdf2epub audiobooks" -Force | Out-Null
Start-ScheduledTask -TaskName "pdf2epub voice worker"

& (Join-Path $project ".venv\Scripts\pdf2epub-voice.exe") check
Write-Output "The voice worker is running and will start at every sign-in. Log: $(Join-Path $env:LOCALAPPDATA 'pdf2epub-voice\worker.log')"
