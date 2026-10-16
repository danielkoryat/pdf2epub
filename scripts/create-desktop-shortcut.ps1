# Creates a "PDF to EPUB" shortcut on the desktop (Windows).
# Double-clicking it starts the pdf2epub web UI in a minimized window and opens it in the browser;
# if it is already running, it just opens the page. Close the window to stop the server.
#
#   powershell -ExecutionPolicy Bypass -File scripts\create-desktop-shortcut.ps1

$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $PSScriptRoot
$venvExe = Join-Path $project ".venv\Scripts\pdf2epub-web.exe"

if (Test-Path $venvExe) {
    $target = $venvExe
    $arguments = ""
} else {
    # No environment yet: let uv create it on first start.
    $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
    if (-not $uv) { throw "Neither $venvExe nor uv was found. Run 'uv sync' in $project first." }
    $target = $uv
    $arguments = "run pdf2epub-web"
}

$desktop = [Environment]::GetFolderPath("Desktop")
$path = Join-Path $desktop "PDF to EPUB.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($path)
$shortcut.TargetPath = $target
$shortcut.Arguments = $arguments
$shortcut.WorkingDirectory = $project
$shortcut.IconLocation = (Join-Path $project "assets\pdf2epub.ico") + ",0"
$shortcut.Description = "Convert PDFs to EPUB and enrich EPUB books (pdf2epub)"
$shortcut.WindowStyle = 7  # minimized
$shortcut.Save()
Write-Output "Created $path"
