param([string]$ProjectDir = "")

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Exe = Join-Path $RepoRoot "dist\LMAStudio\LMAStudio.exe"

if (!(Test-Path $Exe)) {
    throw "Cannot find $Exe; run packaging\windows\build_windows.ps1 first."
}

$ArgsList = @()
if ($ProjectDir.Trim().Length -gt 0) {
    $ProjectFull = (Resolve-Path $ProjectDir).Path
    $ArgsList += @("--project-dir", $ProjectFull)
}

Write-Host "Starting LMA Studio desktop application"
& $Exe @ArgsList
