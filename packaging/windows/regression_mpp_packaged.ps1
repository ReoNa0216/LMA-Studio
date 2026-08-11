param(
    [Parameter(Mandatory = $true)]
    [string]$LifDir,
    [Parameter(Mandatory = $true)]
    [string]$MsPath,
    [Parameter(Mandatory = $true)]
    [string]$CellEventMapPath,
    [string]$CopyRoot = ""
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Exe = (Resolve-Path (Join-Path $RepoRoot "dist\LMAStudio\LMAStudio.exe")).Path
$LifRoot = (Resolve-Path -LiteralPath $LifDir).Path
$MsSource = (Resolve-Path -LiteralPath $MsPath).Path
$MapSource = (Resolve-Path -LiteralPath $CellEventMapPath).Path
$G1Source = (Resolve-Path -LiteralPath (Join-Path $LifRoot "G1.CSV")).Path
$G2Source = (Resolve-Path -LiteralPath (Join-Path $LifRoot "G2.CSV")).Path
$TempBase = [IO.Path]::GetFullPath($env:TEMP).TrimEnd("\")
$TempPrefix = $TempBase + [IO.Path]::DirectorySeparatorChar
$CopyRoot = if ($CopyRoot) {
    [IO.Path]::GetFullPath($CopyRoot)
} else {
    Join-Path $TempBase ("LMAStudio_MPP_Packaged_" + [guid]::NewGuid().ToString("N"))
}
$ProjectDir = Join-Path $CopyRoot "MPP_Project"

function Test-SafeRegressionRoot([string]$Path) {
    $FullPath = [IO.Path]::GetFullPath($Path)
    return (
        $FullPath.StartsWith($TempPrefix, [StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path $FullPath -Leaf) -like "LMAStudio_MPP_Packaged_*"
    )
}

function Remove-SafeRegressionRoot([string]$Path) {
    $Resolved = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $Path).Path)
    if (!(Test-SafeRegressionRoot $Resolved)) {
        throw "Refusing unsafe packaged MPP cleanup: $Resolved"
    }
    for ($Attempt = 0; $Attempt -lt 20; $Attempt++) {
        try {
            Remove-Item -LiteralPath $Resolved -Recurse -Force -ErrorAction Stop
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

function Get-SourceSnapshot([string[]]$Paths) {
    return @(
        $Paths | ForEach-Object {
            $Item = Get-Item -LiteralPath $_
            [pscustomobject]@{
                Path = $Item.FullName
                Length = $Item.Length
                LastWriteTimeUtcTicks = $Item.LastWriteTimeUtc.Ticks
            }
        }
    ) | ConvertTo-Json -Compress
}

function Wait-ApplicationServer([Diagnostics.Process]$Process) {
    for ($Index = 0; $Index -lt 120; $Index++) {
        Start-Sleep -Milliseconds 500
        $Process.Refresh()
        if ($Process.HasExited) {
            throw "LMAStudio.exe exited before the packaged MPP import started."
        }
        $Connection = Get-NetTCPConnection `
            -OwningProcess $Process.Id `
            -State Listen `
            -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($Connection) {
            return $Connection
        }
    }
    throw "LMAStudio.exe did not open its loopback server."
}

if (!(Test-SafeRegressionRoot $CopyRoot)) {
    throw "Packaged MPP regression must stay under the system temp directory."
}
if (Test-Path -LiteralPath $CopyRoot) {
    throw "Packaged MPP regression root already exists: $CopyRoot"
}
if (Get-Process LMAStudio -ErrorAction SilentlyContinue) {
    throw "Close LMA Studio before starting the packaged MPP regression."
}

$SourcePaths = @($G1Source, $G2Source, $MsSource, $MapSource)
$Before = Get-SourceSnapshot $SourcePaths
$Process = $null
New-Item -ItemType Directory -Path $CopyRoot | Out-Null
try {
    $Process = Start-Process `
        -FilePath $Exe `
        -WorkingDirectory (Split-Path $Exe) `
        -WindowStyle Hidden `
        -PassThru
    $Connection = Wait-ApplicationServer $Process
    $BaseUrl = "http://127.0.0.1:$($Connection.LocalPort)"
    $Meta = $null
    for ($Index = 0; $Index -lt 60; $Index++) {
        try {
            $Meta = Invoke-RestMethod -Uri "$BaseUrl/api/meta" -TimeoutSec 10
            break
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (!$Meta) {
        throw "Packaged LMA Studio metadata endpoint is unavailable."
    }

    $Payload = @{
        project_dir = $ProjectDir
        raw_input_mode = "external_reference"
        lif_inputs = @(
            @{
                key = "lif_g1"
                channel = "G1"
                detector = "green"
                identity_prior = "MPP"
                use_for_cell_annotation = $true
                path = $G1Source
            },
            @{
                key = "lif_g2"
                channel = "G2"
                detector = "green"
                identity_prior = "Lin-"
                use_for_cell_annotation = $true
                path = $G2Source
            }
        )
        ms_path = $MsSource
        cell_event_map_path = $MapSource
        calibration_protocol = @{
            protocol_version = 1
            segments = @(
                @{
                    segment_id = "lin_reference"
                    order = 1
                    start_min = 2.165
                    end_min = 8.085
                    reference_channels = @("G2")
                    reference_mode = "green_only"
                    population_label = "Lin-"
                    boundaries_confirmed = $false
                },
                @{
                    segment_id = "mpp_reference"
                    order = 2
                    start_min = 10.206
                    end_min = 17.663
                    reference_channels = @("G1")
                    reference_mode = "green_only"
                    population_label = "MPP"
                    boundaries_confirmed = $false
                }
            )
        }
        post_qc_strategy = @{ mode = "disabled" }
        annotation_start_min = 24.0
        local_delta_seed_window_min = 2.5
    } | ConvertTo-Json -Compress -Depth 10

    $Started = Get-Date
    $Result = Invoke-RestMethod `
        -Method Post `
        -Uri "$BaseUrl/api/import-project" `
        -Headers @{ "X-Annotation-Write-Token" = $Meta.write_token } `
        -ContentType "application/json; charset=utf-8" `
        -Body $Payload `
        -TimeoutSec 600
    if (!$Result.ok) {
        throw "Packaged MPP import did not return success."
    }

    $EventMap = Invoke-RestMethod -Uri "$BaseUrl/api/cell-event-map" -TimeoutSec 60
    $PointCount = @($EventMap.points).Count
    if ($PointCount -ne 906) {
        throw "Packaged MPP event map has $PointCount points; expected 906."
    }
    $RequiredOutputs = @(
        "data\lif_traces.parquet",
        "data\lif_peaks.parquet",
        "data\ms_events.parquet",
        "data\ms_scan_summary.parquet",
        "data\cell_event_map.csv",
        "annotations\annotation.sqlite",
        "lifms_project.json"
    )
    foreach ($RelativePath in $RequiredOutputs) {
        if (!(Test-Path -LiteralPath (Join-Path $ProjectDir $RelativePath))) {
            throw "Packaged MPP import is missing $RelativePath"
        }
    }
    if ($Before -ne (Get-SourceSnapshot $SourcePaths)) {
        throw "A raw MPP source changed during packaged integration."
    }

    [pscustomobject]@{
        PackagedImport = "PASS"
        ElapsedSec = [math]::Round(((Get-Date) - $Started).TotalSeconds, 2)
        EventMapRows = $PointCount
        ProjectLayout = "canonical"
        SourcesUnchanged = $true
    } | ConvertTo-Json -Compress
}
finally {
    if ($Process -and !$Process.HasExited) {
        Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
        $Process.WaitForExit(10000) | Out-Null
    }
    if (Test-Path -LiteralPath $CopyRoot) {
        Remove-SafeRegressionRoot $CopyRoot
    }
}
