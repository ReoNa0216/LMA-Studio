param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectDir,
    [Parameter(Mandatory = $true)]
    [string]$HscDataDir,
    [string]$PythonExe = "D:\Miniconda\envs\lifms_annotation_win\python.exe",
    [string]$CopyRoot = "",
    [switch]$CleanupStaleCopy,
    [switch]$ExerciseBoundaryAnchorWrite,
    [switch]$ExerciseSavedPairPerformance
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
    Join-Path $TempBase "LMAStudioProjectRegression_HSC1_rc5"
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

$OriginalProjectBefore = Get-ProjectSnapshot $SourceProject
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

    $BoundaryWindow = Invoke-RestMethod `
        -Uri "$BaseUrl/api/window?start_min=6&window_min=1&time_mode=aligned" `
        -TimeoutSec 60
    $BoundaryEventIds = @(
        "MS_pc34_primary_000112",
        "MS_pc34_primary_000113"
    )
    $BoundaryGroups = @(
        $BoundaryWindow.alignment_groups |
            Where-Object { $BoundaryEventIds -contains [string]$_.ms_event_id }
    )
    if ($BoundaryGroups.Count -ne 2) {
        throw "HSC1 boundary QC candidates 6.002/6.017 were not both assigned to the 6-7 min MS window."
    }
    if ($ExerciseBoundaryAnchorWrite) {
        $BoundaryRelations = @(
            @{
                lif_peak_id = "G1_merged_000195"
                ms_event_id = "MS_pc34_primary_000112"
            },
            @{
                lif_peak_id = "G1_merged_000196"
                ms_event_id = "MS_pc34_primary_000113"
            }
        )
        foreach ($Relation in $BoundaryRelations) {
            $SaveBody = @{
                lif_anchor_peak_ids = @{ G1 = $Relation.lif_peak_id }
                ms_event_id = $Relation.ms_event_id
                stage = "qc_calibration"
                calibration_segment_id = "lsk_reference"
                window_start_min = 6.0
                window_end_min = 7.0
                time_mode = "aligned"
            } | ConvertTo-Json -Compress -Depth 6
            $Saved = Invoke-RestMethod `
                -Method Post `
                -Uri "$BaseUrl/api/manual-triplet" `
                -Headers @{ "X-Annotation-Write-Token" = $Meta.write_token } `
                -ContentType "application/json; charset=utf-8" `
                -Body $SaveBody `
                -TimeoutSec 60
            if ([string]$Saved.annotation.review_status -ne "accepted") {
                throw "Packaged Save anchor did not accept boundary relation $($Relation.ms_event_id)."
            }
        }
    }

    $Window24 = Invoke-RestMethod `
        -Uri "$BaseUrl/api/window?start_min=24&window_min=2.5&time_mode=aligned" `
        -TimeoutSec 60
    $Window50 = Invoke-RestMethod `
        -Uri "$BaseUrl/api/window?start_min=50&window_min=5&time_mode=aligned" `
        -TimeoutSec 60
    $SavedPairSeconds = $null
    $SavedPairRefreshSeconds = $null
    $SavedPairTotalSeconds = $null
    $SavedPairAnnotationId = ""
    if ($ExerciseSavedPairPerformance) {
        $PerformanceWindow = Invoke-RestMethod `
            -Uri "$BaseUrl/api/window?start_min=57&window_min=1&time_mode=aligned&include_weak_lif_peaks=true" `
            -TimeoutSec 60
        $PerformanceRelation = @(
            $PerformanceWindow.annotations |
                Where-Object {
                    [string]$_.review_status -eq "accepted" -and
                    [string]$_.review_stage -eq "cell_annotation" -and
                    [string]$_.lif_peak_id -ne "" -and
                    [string]$_.ms_event_id -ne "" -and
                    [double]$_.lif_plot_time_min -ge 57.0 -and
                    [double]$_.lif_plot_time_min -le 58.0 -and
                    [double]$_.ms_plot_time_min -ge 57.0 -and
                    [double]$_.ms_plot_time_min -le 58.0
                } |
                Select-Object -First 1
        )
        if ($PerformanceRelation.Count -ne 1) {
            throw "HSC1 copy has no accepted 57-58 min Cell pair for the packaged performance probe."
        }
        $PerformanceRelation = $PerformanceRelation[0]
        $SavedPairBody = @{
            lif_channel = [string]$PerformanceRelation.lif_channel
            lif_peak_id = [string]$PerformanceRelation.lif_peak_id
            ms_event_id = [string]$PerformanceRelation.ms_event_id
            window_start_min = 57.0
            window_end_min = 58.0
            time_mode = "aligned"
        } | ConvertTo-Json -Compress
        $SavedPairWatch = [Diagnostics.Stopwatch]::StartNew()
        $SavedPair = Invoke-RestMethod `
            -Method Post `
            -Uri "$BaseUrl/api/manual-cell-pair" `
            -Headers @{ "X-Annotation-Write-Token" = $Meta.write_token } `
            -ContentType "application/json; charset=utf-8" `
            -Body $SavedPairBody `
            -TimeoutSec 60
        $SavedPairWatch.Stop()
        $SavedPairRefreshWatch = [Diagnostics.Stopwatch]::StartNew()
        $PerformanceWindowAfter = Invoke-RestMethod `
            -Uri "$BaseUrl/api/window?start_min=57&window_min=1&time_mode=aligned&include_weak_lif_peaks=true" `
            -TimeoutSec 60
        $SavedPairRefreshWatch.Stop()
        if ([string]$SavedPair.annotation.review_status -ne "accepted") {
            throw "Packaged Save pair performance probe was not accepted."
        }
        if ([string]$SavedPair.annotation.annotation_id -ne [string]$PerformanceRelation.annotation_id) {
            throw "Packaged Save pair performance probe changed the existing relation identity."
        }
        $SavedPairVisible = @(
            $PerformanceWindowAfter.annotations |
                Where-Object {
                    [string]$_.annotation_id -eq [string]$PerformanceRelation.annotation_id
                }
        )
        if ($SavedPairVisible.Count -ne 1) {
            throw "Packaged Save pair performance relation was not visible after refresh."
        }
        $SavedPairSeconds = $SavedPairWatch.Elapsed.TotalSeconds
        $SavedPairRefreshSeconds = $SavedPairRefreshWatch.Elapsed.TotalSeconds
        $SavedPairTotalSeconds = $SavedPairSeconds + $SavedPairRefreshSeconds
        $SavedPairAnnotationId = [string]$SavedPair.annotation.annotation_id
        if ($SavedPairSeconds -gt 2.0 -or $SavedPairRefreshSeconds -gt 2.0) {
            throw (
                "Packaged Save pair performance regressed: save={0:N3}s, refresh={1:N3}s." -f `
                    $SavedPairSeconds, $SavedPairRefreshSeconds
            )
        }
    }
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
    $CsvRows = @($Response.Content.TrimStart([char]0xFEFF) | ConvertFrom-Csv)
    $ExpectedCsvRows = [int]$Manifest.cell_event_map.row_count
    if ($CsvRows.Count -ne $ExpectedCsvRows) {
        throw "HSC1 CSV is not a complete event roster: expected $ExpectedCsvRows rows, found $($CsvRows.Count)."
    }
    $CsvCellNumbers = @($CsvRows | ForEach-Object { [string]$_.CellNumber })
    if (@($CsvCellNumbers | Where-Object { !$_ }).Count -ne 0) {
        throw "HSC1 CSV contains a blank CellNumber."
    }
    if (@($CsvCellNumbers | Sort-Object -Unique).Count -ne $CsvRows.Count) {
        throw "HSC1 CSV contains duplicate CellNumber values."
    }
    $UnknownRows = @($CsvRows | Where-Object { [string]$_.Type -eq "unknown" })
    $InvalidUnknownRows = @(
        $UnknownRows |
            Where-Object {
                [string]$_.annotation_kind -or
                [string]$_.review_stage -or
                [string]$_.LIF_channel -or
                [string]$_.LIF_peak_id -or
                [string]$_.residual_sec -or
                [string]$_.annotation_id
            }
    )
    if ($InvalidUnknownRows.Count -ne 0) {
        throw "HSC1 unknown CSV rows must leave annotation-specific fields blank."
    }
    $QcRows = @($CsvRows | Where-Object { [string]$_.Type -eq "QC" })
    if ($QcRows.Count -ne 0) {
        throw "HSC1 post-QC is Off, but the current CSV still contains QC rows."
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
    $CopyWriteExercised = [bool]($ExerciseBoundaryAnchorWrite -or $ExerciseSavedPairPerformance)
    if (!$CopyWriteExercised -and $Before -cne $After) {
        throw "Packaged HSC1 read-only smoke changed protected project state."
    }
    if ($CopyWriteExercised -and $Before -ceq $After) {
        throw "Packaged write exercise did not change its temporary project copy."
    }
    $OriginalProjectAfter = Get-ProjectSnapshot $SourceProject
    if ($OriginalProjectBefore -cne $OriginalProjectAfter) {
        throw "Packaged HSC1 smoke changed the original project; only the temporary copy may be used."
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
        BoundaryQcCandidates = $BoundaryGroups.Count
        BoundaryAnchorWriteExercised = [bool]$ExerciseBoundaryAnchorWrite
        SavedPairPerformanceExercised = [bool]$ExerciseSavedPairPerformance
        SavedPairSeconds = if ($null -eq $SavedPairSeconds) { "" } else { [math]::Round($SavedPairSeconds, 3) }
        SavedPairRefreshSeconds = if ($null -eq $SavedPairRefreshSeconds) { "" } else { [math]::Round($SavedPairRefreshSeconds, 3) }
        SavedPairTotalSeconds = if ($null -eq $SavedPairTotalSeconds) { "" } else { [math]::Round($SavedPairTotalSeconds, 3) }
        SavedPairAnnotationId = $SavedPairAnnotationId
        Window24CellCandidates = @($Window24.cell_candidates).Count
        Window24PostQcCandidates = @($Window24.post_qc_candidates).Count
        Window50CellCandidates = @($Window50.cell_candidates).Count
        DeltaStatus = [string]$Delta.recommendation_status
        CsvHeaderColumns = ($ActualHeader -split ",").Count
        CsvRosterRows = $CsvRows.Count
        CsvUnknownRows = $UnknownRows.Count
        CsvQcRows = $QcRows.Count
        ProjectStable = !$CopyWriteExercised
        CopyWriteIsolated = $CopyWriteExercised
        OriginalProjectStable = $true
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
