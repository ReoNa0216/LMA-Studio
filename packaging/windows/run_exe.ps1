param(
    [string]$HostName = "127.0.0.1",
    [int]$Port = 8050,
    [string]$ProjectDir = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Exe = Join-Path $RepoRoot "dist\LMAStudio\LMAStudio.exe"

if (!(Test-Path $Exe)) {
    throw "Cannot find $Exe; run packaging\windows\build_windows.ps1 first."
}

$ArgsList = @("--host", $HostName, "--port", "$Port")
if ($ProjectDir.Trim().Length -gt 0) {
    $ProjectFull = (Resolve-Path $ProjectDir).Path
    $ArgsList += @(
        "--project-dir", $ProjectFull,
        "--raw-data-dir", (Join-Path $ProjectFull "raw_inputs"),
        "--annotation-db", (Join-Path $ProjectFull "annotation_app\annotations\annotation.sqlite")
    )
}

Write-Host "Starting LMA Studio at http://$HostName`:$Port/"
& $Exe @ArgsList
