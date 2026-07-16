param(
    [string]$PythonExe = "D:\Miniconda\envs\lifms_annotation_win\python.exe",
    [string]$ProjectsRoot = "",
    [string]$CopyRoot = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Exe = (Resolve-Path (Join-Path $RepoRoot "dist\LMAStudio\LMAStudio.exe")).Path
$Python = (Resolve-Path $PythonExe).Path
$ProjectsRoot = if ($ProjectsRoot) { (Resolve-Path $ProjectsRoot).Path } else { (Resolve-Path (Join-Path $RepoRoot "..")).Path }
$TempBase = [IO.Path]::GetFullPath($env:TEMP).TrimEnd("\")
$TempPrefix = $TempBase + [IO.Path]::DirectorySeparatorChar
$CopyRoot = if ($CopyRoot) { [IO.Path]::GetFullPath($CopyRoot) } else { Join-Path $TempBase "LMAStudioProjectRegression_Codex_01" }
$CopyRoot = [IO.Path]::GetFullPath($CopyRoot)
$SnapshotScript = Join-Path $PSScriptRoot "project_snapshot.py"

function Test-SafeRegressionRoot([string]$Path) {
    $FullPath = [IO.Path]::GetFullPath($Path)
    return (
        $FullPath.StartsWith($TempPrefix, [StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path $FullPath -Leaf) -like "LMAStudioProjectRegression_*"
    )
}

if (!(Test-SafeRegressionRoot $CopyRoot)) {
    throw "Regression copies must stay under the system temp directory with the LMAStudioProjectRegression_ prefix."
}
if (Test-Path -LiteralPath $CopyRoot) {
    throw "Regression root already exists: $CopyRoot"
}

function Get-ProjectSnapshot([string]$ProjectPath) {
    $output = & $Python $SnapshotScript $ProjectPath
    if ($LASTEXITCODE -ne 0) {
        throw "Snapshot failed for $ProjectPath"
    }
    return $output | ConvertFrom-Json
}

function Wait-ApplicationServer([Diagnostics.Process]$Process) {
    for ($index = 0; $index -lt 120; $index++) {
        Start-Sleep -Milliseconds 500
        $Process.Refresh()
        if ($Process.HasExited) {
            throw "LMAStudio.exe exited before its project finished loading."
        }
        $connection = Get-NetTCPConnection -OwningProcess $Process.Id -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($connection) {
            return $connection
        }
    }
    throw "LMAStudio.exe did not open a loopback listener."
}

function Test-SameJson($Before, $After) {
    return ($Before | ConvertTo-Json -Compress) -eq ($After | ConvertTo-Json -Compress)
}

$Results = @()
$TestProcesses = @()
if (Get-Process LMAStudio -ErrorAction SilentlyContinue) {
    throw "Close the running LMA Studio window before starting project regression."
}
New-Item -ItemType Directory -Path $CopyRoot | Out-Null
try {
    foreach ($Name in @("Batch03Test", "CART_Exp1-3", "CART_Exp2-1", "Young_HSC3")) {
        $Source = (Resolve-Path (Join-Path $ProjectsRoot $Name)).Path
        $Copy = Join-Path $CopyRoot $Name
        Copy-Item -LiteralPath $Source -Destination $Copy -Recurse
        $Before = Get-ProjectSnapshot $Copy

        $Process = Start-Process -FilePath $Exe -ArgumentList @("--project-dir", "`"$Copy`"") -WorkingDirectory (Split-Path $Exe) -PassThru
        $TestProcesses += $Process
        $Connection = Wait-ApplicationServer $Process
        $BaseUrl = "http://127.0.0.1:$($Connection.LocalPort)"
        $Meta = $null
        for ($index = 0; $index -lt 120; $index++) {
            try {
                $Meta = Invoke-RestMethod -Uri "$BaseUrl/api/meta" -TimeoutSec 10
                break
            }
            catch {
                Start-Sleep -Milliseconds 500
            }
        }
        if (!$Meta -or $Meta.bootstrap) {
            throw "$Name did not load as a project."
        }

        Invoke-RestMethod -Uri "$BaseUrl/api/window?start_min=0&window_min=2.5&time_mode=aligned" -TimeoutSec 30 | Out-Null
        $AnnotationStart = [double]$Meta.project_config.annotation_start_min
        Invoke-RestMethod -Uri "$BaseUrl/api/window?start_min=$AnnotationStart&window_min=2.5&time_mode=aligned" -TimeoutSec 30 | Out-Null
        Invoke-RestMethod -Uri "$BaseUrl/api/local-delta-preview" -TimeoutSec 30 | Out-Null

        $QcPending = $null
        $QcManualDuplicates = $null
        if ($Name -eq "Young_HSC3") {
            $QcPending = 0
            $QcManualDuplicates = 0
            $QcEnd = [double]$Meta.project_config.qc_calibration_end_min
            for ($QcStart = 0.0; $QcStart -lt $QcEnd; $QcStart += 2.5) {
                $QcWindow = Invoke-RestMethod -Uri "$BaseUrl/api/window?start_min=$QcStart&window_min=2.5&time_mode=aligned" -TimeoutSec 30
                $AnnotationIds = @{}
                foreach ($Row in @($QcWindow.annotations)) {
                    $AnnotationIds[[string]$Row.annotation_id] = $true
                }
                foreach ($Group in @($QcWindow.alignment_groups)) {
                    if ([string]$Group.review_status -eq "pending") {
                        $QcPending++
                    }
                    if ([string]$Group.source -eq "manual_created" -and $AnnotationIds.ContainsKey([string]$Group.annotation_id)) {
                        $QcManualDuplicates++
                    }
                }
            }
            if ($QcPending -ne 0 -or $QcManualDuplicates -ne 0) {
                throw "Young_HSC3 QC reconciliation failed: pending=$QcPending, manual_duplicates=$QcManualDuplicates."
            }
        }

        $ExportRows = $null
        if ($Name -eq "Batch03Test") {
            $Response = Invoke-WebRequest -Method Post -Uri "$BaseUrl/api/export-accepted-csv" `
                -Headers @{ "X-Annotation-Write-Token" = $Meta.write_token } `
                -ContentType "application/json; charset=utf-8" -Body "{}" -TimeoutSec 30 -UseBasicParsing
            if ($Response.StatusCode -ne 200 -or $Response.Content.Length -lt 10) {
                throw "Packaged CSV export failed."
            }
            $ExportRows = ($Response.Content -split "`n").Count - 1
        }

        $Process.Refresh()
        $Process.CloseMainWindow() | Out-Null
        if (!$Process.WaitForExit(20000)) {
            throw "$Name did not exit after its window closed."
        }
        if (Get-NetTCPConnection -LocalPort $Connection.LocalPort -State Listen -ErrorAction SilentlyContinue) {
            throw "$Name left its HTTP listener running after exit."
        }

        $AfterMigration = Get-ProjectSnapshot $Copy
        $ImmutableChecks = [ordered]@{
            annotations_sha256 = $Before.annotations_sha256 -eq $AfterMigration.annotations_sha256
            annotation_count = $Before.annotation_count -eq $AfterMigration.annotation_count
            audit_count = $Before.audit_count -eq $AfterMigration.audit_count
            audit_sha256 = $Before.audit_sha256 -eq $AfterMigration.audit_sha256
            counts = Test-SameJson $Before.counts $AfterMigration.counts
            manifest_sha256 = $Before.manifest_sha256 -eq $AfterMigration.manifest_sha256
            parquets = Test-SameJson $Before.parquets $AfterMigration.parquets
        }
        $ChangedImmutableFields = @($ImmutableChecks.Keys | Where-Object { !$ImmutableChecks[$_] })
        if ($ChangedImmutableFields.Count -ne 0) {
            throw "$Name first-open migration changed protected data: $($ChangedImmutableFields -join ', ')."
        }

        # The first open may bind a legacy time model to the acquisition layout.
        # A second open must be fully idempotent after that documented migration.
        $Before = $AfterMigration
        $Process = Start-Process -FilePath $Exe -ArgumentList @("--project-dir", "`"$Copy`"") -WorkingDirectory (Split-Path $Exe) -PassThru
        $TestProcesses += $Process
        $Connection = Wait-ApplicationServer $Process
        $BaseUrl = "http://127.0.0.1:$($Connection.LocalPort)"
        $Meta = $null
        for ($index = 0; $index -lt 120; $index++) {
            try {
                $Meta = Invoke-RestMethod -Uri "$BaseUrl/api/meta" -TimeoutSec 10
                break
            }
            catch {
                Start-Sleep -Milliseconds 500
            }
        }
        if (!$Meta -or $Meta.bootstrap) {
            throw "$Name did not reload as a project."
        }
        $Process.Refresh()
        $Process.CloseMainWindow() | Out-Null
        if (!$Process.WaitForExit(20000)) {
            throw "$Name did not exit after its reload window closed."
        }
        if (Get-NetTCPConnection -LocalPort $Connection.LocalPort -State Listen -ErrorAction SilentlyContinue) {
            throw "$Name left its reload HTTP listener running after exit."
        }

        $After = Get-ProjectSnapshot $Copy
        $Checks = [ordered]@{
            annotations_sha256 = $Before.annotations_sha256 -eq $After.annotations_sha256
            annotation_count = $Before.annotation_count -eq $After.annotation_count
            audit_count = $Before.audit_count -eq $After.audit_count
            audit_sha256 = $Before.audit_sha256 -eq $After.audit_sha256
            counts = Test-SameJson $Before.counts $After.counts
            project_config_sha256 = $Before.project_config_sha256 -eq $After.project_config_sha256
            time_models_sha256 = $Before.time_models_sha256 -eq $After.time_models_sha256
            time_model_audit_sha256 = $Before.time_model_audit_sha256 -eq $After.time_model_audit_sha256
            input_manifest_sha256 = $Before.input_manifest_sha256 -eq $After.input_manifest_sha256
            manifest_sha256 = $Before.manifest_sha256 -eq $After.manifest_sha256
            parquets = Test-SameJson $Before.parquets $After.parquets
        }
        $ChangedFields = @($Checks.Keys | Where-Object { !$Checks[$_] })
        $Stable = $ChangedFields.Count -eq 0
        if (!$Stable) {
            throw "$Name regression changed: $($ChangedFields -join ', ')."
        }

        $Results += [pscustomobject]@{
            Project = $Name
            Channels = $Meta.lif_channels.channel -join "/"
            Annotations = $After.annotation_count
            Accepted = $After.counts.accepted
            Rejected = $After.counts.rejected
            Audit = $After.audit_count
            AnnotationHash = $After.annotations_sha256
            ExportRows = $ExportRows
            QcPending = $QcPending
            QcManualDuplicates = $QcManualDuplicates
            DataStable = $Stable
            ClosedCleanly = $true
        }
    }
    $Results | Format-Table -AutoSize
}
finally {
    foreach ($TestProcess in $TestProcesses) {
        try {
            $TestProcess.Refresh()
            if (!$TestProcess.HasExited) {
                $TestProcess.CloseMainWindow() | Out-Null
                if (!$TestProcess.WaitForExit(10000)) {
                    Stop-Process -Id $TestProcess.Id -Force -ErrorAction SilentlyContinue
                }
            }
        }
        catch {
            Stop-Process -Id $TestProcess.Id -Force -ErrorAction SilentlyContinue
        }
    }
    if (Test-Path -LiteralPath $CopyRoot) {
        $ResolvedCopyRoot = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $CopyRoot).Path)
        if (!(Test-SafeRegressionRoot $ResolvedCopyRoot)) {
            throw "Refusing unsafe regression cleanup: $ResolvedCopyRoot"
        }
        Remove-Item -LiteralPath $ResolvedCopyRoot -Recurse -Force
    }
    Write-Host "Regression copies removed: $(!(Test-Path -LiteralPath $CopyRoot))"
}
