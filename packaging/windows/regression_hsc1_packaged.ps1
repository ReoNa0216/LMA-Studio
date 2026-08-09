param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectDir,
    [Parameter(Mandatory = $true)]
    [string]$HscDataDir,
    [string]$PythonExe = "D:\Miniconda\envs\lifms_annotation_win\python.exe",
    [string]$CopyRoot = "",
    [switch]$CleanupStaleCopy
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Exe = (Resolve-Path (Join-Path $RepoRoot "dist\LMAStudio\LMAStudio.exe")).Path
$Python = (Resolve-Path $PythonExe).Path
$SourceProject = (Resolve-Path $ProjectDir).Path
$HscRoot = (Resolve-Path $HscDataDir).Path
$SnapshotScript = Join-Path $PSScriptRoot "project_snapshot.py"
$TempBase = [IO.Path]::GetFullPath($env:TEMP).TrimEnd("\")
$TempPrefix = $TempBase + [IO.Path]::DirectorySeparatorChar
$CopyRoot = if ($CopyRoot) {
    [IO.Path]::GetFullPath($CopyRoot)
} else {
    Join-Path $TempBase "LMAStudioProjectRegression_HSC1_rc1"
}
$CopyRoot = [IO.Path]::GetFullPath($CopyRoot)
$CopyProject = Join-Path $CopyRoot "HSC1"

function Test-SafeRegressionRoot([string]$Path) {
    $FullPath = [IO.Path]::GetFullPath($Path)
    return (
        $FullPath.StartsWith($TempPrefix, [StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path $FullPath -Leaf) -like "LMAStudioProjectRegression_*"
    )
}

function Remove-SafeRegressionRoot([string]$Path) {
    $ResolvedPath = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $Path).Path)
    if (!(Test-SafeRegressionRoot $ResolvedPath)) {
        throw "Refusing unsafe regression cleanup: $ResolvedPath"
    }
    for ($Attempt = 0; $Attempt -lt 20; $Attempt++) {
        try {
            Remove-Item -LiteralPath $ResolvedPath -Recurse -Force -ErrorAction Stop
            return
        }
        catch {
            if ($Attempt -eq 19) {
                throw
            }
            Start-Sleep -Milliseconds 500
        }
    }
}

function Get-ProjectSnapshot([string]$Path) {
    $Output = & $Python $SnapshotScript $Path
    if ($LASTEXITCODE -ne 0) {
        throw "Project snapshot failed for $Path"
    }
    return $Output
}

function Get-HscSourceSnapshot([string]$Path) {
    return @(
        Get-ChildItem -LiteralPath $Path -Recurse -File |
            Sort-Object FullName |
            Select-Object FullName, Length, LastWriteTimeUtc
    ) | ConvertTo-Json -Compress
}

function Wait-ApplicationServer([Diagnostics.Process]$Process) {
    for ($Index = 0; $Index -lt 120; $Index++) {
        Start-Sleep -Milliseconds 500
        $Process.Refresh()
        if ($Process.HasExited) {
            throw "LMAStudio.exe exited before HSC1 finished loading."
        }
        $Connection = Get-NetTCPConnection -OwningProcess $Process.Id -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($Connection) {
            return $Connection
        }
    }
    throw "LMAStudio.exe did not open a loopback listener for HSC1."
}

if (!(Test-SafeRegressionRoot $CopyRoot)) {
    throw "Regression copies must stay under the system temp directory with the LMAStudioProjectRegression_ prefix."
}
if ($CleanupStaleCopy) {
    if (Get-Process LMAStudio -ErrorAction SilentlyContinue) {
        throw "Close LMA Studio before cleaning a stale regression copy."
    }
    if (Test-Path -LiteralPath $CopyRoot) {
        Remove-SafeRegressionRoot $CopyRoot
    }
    Write-Host "HSC1 stale regression copy removed: $(!(Test-Path -LiteralPath $CopyRoot))"
    exit 0
}
if (Test-Path -LiteralPath $CopyRoot) {
    throw "Regression root already exists: $CopyRoot"
}
if (Get-Process LMAStudio -ErrorAction SilentlyContinue) {
    throw "Close the running LMA Studio window before starting HSC1 regression."
}

$SourceBefore = Get-HscSourceSnapshot $HscRoot
$Process = $null
New-Item -ItemType Directory -Path $CopyRoot | Out-Null
try {
    Copy-Item -LiteralPath $SourceProject -Destination $CopyProject -Recurse
    $Before = Get-ProjectSnapshot $CopyProject
    $Manifest = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $CopyProject "lifms_project.json") |
        ConvertFrom-Json

    $Process = Start-Process -FilePath $Exe `
        -ArgumentList @("--project-dir", "`"$CopyProject`"") `
        -WorkingDirectory (Split-Path $Exe) `
        -WindowStyle Hidden `
        -PassThru
    $Connection = Wait-ApplicationServer $Process
    $BaseUrl = "http://127.0.0.1:$($Connection.LocalPort)"
    $Meta = $null
    for ($Index = 0; $Index -lt 120; $Index++) {
        try {
            $Meta = Invoke-RestMethod -Uri "$BaseUrl/api/meta" -TimeoutSec 10
            break
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (!$Meta -or $Meta.bootstrap) {
        throw "Packaged EXE did not load the HSC1 project copy."
    }

    $Channels = @($Meta.acquisition_layout.lif_channels | ForEach-Object { [string]$_.channel })
    $Axes = @(
        $Meta.acquisition_layout.lif_channels |
            ForEach-Object { [string]$_.time_axis } |
            Sort-Object -Unique
    )
    if (($Channels -join "/") -ne "G1/G2") {
        throw "Unexpected HSC1 channels: $($Channels -join '/')"
    }
    if (($Axes -join "/") -ne "green_axis") {
        throw "Unexpected HSC1 physical axes: $($Axes -join '/')"
    }
    if ([double]$Meta.project_config.annotation_start_min -ne 24.0) {
        throw "HSC1 annotation_start_min changed."
    }
    if ([string]$Meta.project_config.post_qc_strategy.mode -ne "disabled") {
        throw "HSC1 post-QC strategy is not disabled."
    }

    $Window24 = Invoke-RestMethod `
        -Uri "$BaseUrl/api/window?start_min=24&window_min=2.5&time_mode=aligned" `
        -TimeoutSec 60
    $Window50 = Invoke-RestMethod `
        -Uri "$BaseUrl/api/window?start_min=50&window_min=5&time_mode=aligned" `
        -TimeoutSec 60
    $DeltaResponse = Invoke-RestMethod `
        -Method Post `
        -Uri "$BaseUrl/api/estimate-local-delta-preview" `
        -Headers @{ "X-Annotation-Write-Token" = $Meta.write_token } `
        -ContentType "application/json; charset=utf-8" `
        -Body "{}" `
        -TimeoutSec 60
    $Delta = $DeltaResponse.preview
    $Response = Invoke-WebRequest `
        -Method Post `
        -Uri "$BaseUrl/api/export-accepted-csv" `
        -Headers @{ "X-Annotation-Write-Token" = $Meta.write_token } `
        -ContentType "application/json; charset=utf-8" `
        -Body "{}" `
        -TimeoutSec 60 `
        -UseBasicParsing
    $ExpectedHeader = @(
        "CellNumber",
        "scan_Id",
        "scan_start_time",
        "TIC",
        "PC(34:1)_mz",
        "PC(34:1)_intensity",
        "UMAP1",
        "UMAP2",
        "Type",
        "annotation_kind",
        "review_stage",
        "LIF_channel",
        "LIF_peak_id",
        "MS_event_id",
        "residual_sec",
        "annotation_id"
    ) -join ","
    $ActualHeader = (($Response.Content -split "`r?`n", 2)[0]).TrimStart([char]0xFEFF)
    if ($ActualHeader -cne $ExpectedHeader) {
        throw "HSC1 compact CSV header mismatch.`nExpected: $ExpectedHeader`nActual:   $ActualHeader"
    }

    # This smoke intentionally launches with a hidden window. A hidden pywebview
    # process has no MainWindowHandle for CloseMainWindow(), so terminate only the
    # exact process created above after all read-only HTTP probes have completed.
    Stop-Process -Id $Process.Id -Force
    if (!$Process.WaitForExit(20000)) {
        throw "Packaged HSC1 smoke process did not exit after the probes."
    }
    if (Get-NetTCPConnection -LocalPort $Connection.LocalPort -State Listen -ErrorAction SilentlyContinue) {
        throw "Packaged HSC1 process left its HTTP listener running."
    }

    $After = Get-ProjectSnapshot $CopyProject
    if ($Before -cne $After) {
        throw "Packaged HSC1 read-only smoke changed protected project state."
    }
    $SourceAfter = Get-HscSourceSnapshot $HscRoot
    if ($SourceBefore -cne $SourceAfter) {
        throw "HSC1_data tree metadata changed during packaged regression."
    }

    [pscustomobject]@{
        ProjectSchema = [int]$Manifest.project_schema_version
        LayoutVersion = [int]$Manifest.acquisition_layout.layout_version
        Channels = $Channels -join "/"
        PhysicalAxes = $Axes -join "/"
        AnnotationStartMin = [double]$Meta.project_config.annotation_start_min
        PostQcMode = [string]$Meta.project_config.post_qc_strategy.mode
        Window24CellCandidates = @($Window24.cell_candidates).Count
        Window24PostQcCandidates = @($Window24.post_qc_candidates).Count
        Window50CellCandidates = @($Window50.cell_candidates).Count
        DeltaStatus = [string]$Delta.recommendation_status
        CsvHeaderColumns = ($ActualHeader -split ",").Count
        ProjectStable = $true
        HscSourceStable = $true
        SmokeProcessExited = $true
    } | Format-List
}
finally {
    if ($Process) {
        try {
            $Process.Refresh()
            if (!$Process.HasExited) {
                $Process.CloseMainWindow() | Out-Null
                if (!$Process.WaitForExit(10000)) {
                    Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
                }
            }
        }
        catch {
            Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
        }
    }
    if (Test-Path -LiteralPath $CopyRoot) {
        Remove-SafeRegressionRoot $CopyRoot
    }
    Write-Host "HSC1 packaged regression copy removed: $(!(Test-Path -LiteralPath $CopyRoot))"
}
