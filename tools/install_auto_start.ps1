param([switch]$Remove)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$shortcutPath = Join-Path ([Environment]::GetFolderPath("Startup")) "STW Intelligence Auto Runner.lnk"
$stopFile = Join-Path $repoRoot "data\stw_auto_runner.stop"

if ($Remove) {
    if (Test-Path -LiteralPath $shortcutPath) {
        Remove-Item -LiteralPath $shortcutPath
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $stopFile) | Out-Null
    New-Item -ItemType File -Force -Path $stopFile | Out-Null
    Write-Host "STW Intelligence automatic launch disabled."
    exit 0
}

$launcher = Get-Command pyw -ErrorAction SilentlyContinue
if ($launcher) {
    $pythonw = $launcher.Source
    $pythonArguments = '-3 "' + (Join-Path $PSScriptRoot "stw_auto_runner.py") + '"'
} else {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) {
        throw "Python 3 was not found."
    }
    $pythonw = Join-Path (Split-Path -Parent $python.Source) "pythonw.exe"
    if (-not (Test-Path -LiteralPath $pythonw)) {
        throw "pythonw.exe was not found beside Python."
    }
    $pythonArguments = '"' + (Join-Path $PSScriptRoot "stw_auto_runner.py") + '"'
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = $pythonArguments
$shortcut.WorkingDirectory = $repoRoot
$shortcut.Description = "Start STW Intelligence while Fortnite is running"
$shortcut.Save()
if (Test-Path -LiteralPath $stopFile) {
    Remove-Item -LiteralPath $stopFile
}
Start-Process -FilePath $pythonw -ArgumentList $pythonArguments -WorkingDirectory $repoRoot -WindowStyle Hidden
Write-Host "STW Intelligence will now monitor for Fortnite after Windows sign-in."
