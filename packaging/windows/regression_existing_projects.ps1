param(
    # Retained for command-line compatibility. This regression no longer opens
    # project SQLite files through Python; it snapshots every file as bytes.
    [string]$PythonExe = "",
    [string]$ProjectsRoot = "",
    [string]$CopyRoot = ""
)

$ErrorActionPreference = "Stop"
$null = $PythonExe

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Exe = (Resolve-Path (Join-Path $RepoRoot "dist\LMAStudio\LMAStudio.exe")).Path
$ProjectsRoot = if ($ProjectsRoot) {
    (Resolve-Path $ProjectsRoot).Path
} else {
    (Resolve-Path (Join-Path $RepoRoot "..")).Path
}
$TempBase = [IO.Path]::GetFullPath($env:TEMP).TrimEnd("\")
$TempPrefix = $TempBase + [IO.Path]::DirectorySeparatorChar
$CopyRoot = if ($CopyRoot) {
    [IO.Path]::GetFullPath($CopyRoot)
} else {
    Join-Path $TempBase "LMAStudioProjectRegression_RetiredDetector_v040"
}
$CopyRoot = [IO.Path]::GetFullPath($CopyRoot)
$ProjectNames = @("Batch03Test", "CART_Exp1-3", "CART_Exp2-1", "Young_HSC3")

function Test-SafeRegressionRoot([string]$Path) {
    $FullPath = [IO.Path]::GetFullPath($Path)
    return (
        $FullPath.StartsWith($TempPrefix, [StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path $FullPath -Leaf) -like "LMAStudioProjectRegression_*"
    )
}

function Get-FileTreeSnapshot([string]$RootPath) {
    $FullRoot = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $RootPath).Path).TrimEnd("\")
    $EntryPrefix = $FullRoot + [IO.Path]::DirectorySeparatorChar
    $Entries = @(
        [pscustomobject][ordered]@{
            relative_path = "."
            kind = "directory"
            length = $null
            # Directory timestamps are not a stable copy invariant on Windows:
            # metadata propagation and indexing can update them without any
            # project file changing. Directory paths still protect structure.
            last_write_time_utc_ticks = $null
            sha256 = $null
        }
        Get-ChildItem -LiteralPath $FullRoot -Force -Recurse |
            Sort-Object FullName |
            ForEach-Object {
                $RelativePath = $_.FullName.Substring($EntryPrefix.Length).Replace("\", "/")
                $IsDirectory = $_.PSIsContainer
                $ContentHash = $null
                if (!$IsDirectory) {
                    $ContentHash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
                }
                [pscustomobject][ordered]@{
                    relative_path = $RelativePath
                    kind = if ($IsDirectory) { "directory" } else { "file" }
                    length = if ($IsDirectory) { $null } else { [long]$_.Length }
                    last_write_time_utc_ticks = if ($IsDirectory) { $null } else { [long]$_.LastWriteTimeUtc.Ticks }
                    sha256 = $ContentHash
                }
            }
    )
    $CanonicalJson = ConvertTo-Json -InputObject ([object[]]$Entries) -Compress -Depth 4
    $Hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $DigestBytes = $Hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($CanonicalJson))
    }
    finally {
        $Hasher.Dispose()
    }
    $TreeHash = ([BitConverter]::ToString($DigestBytes)).Replace("-", "").ToLowerInvariant()
    return [pscustomobject]@{
        entries = [object[]]$Entries
        canonical_json = $CanonicalJson
        tree_sha256 = $TreeHash
        entry_count = $Entries.Count
        file_count = @($Entries | Where-Object { $_.kind -eq "file" }).Count
    }
}

function Get-RetiredDetectorKind([string]$ProjectPath) {
    $ManifestPath = Join-Path $ProjectPath "lifms_project.json"
    if (!(Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        throw "Retired-detector fixture has no lifms_project.json: $ProjectPath"
    }
    $Manifest = Get-Content -Raw -Encoding UTF8 -LiteralPath $ManifestPath | ConvertFrom-Json
    $DetectorProperty = $Manifest.PSObject.Properties["lif_peak_detection"]
    if ($null -eq $DetectorProperty -or $null -eq $DetectorProperty.Value) {
        return "missing"
    }
    $Version = 0
    if (![int]::TryParse([string]$DetectorProperty.Value.detector_version, [ref]$Version)) {
        throw "Retired-detector fixture has an unreadable detector_version: $ProjectPath"
    }
    if ($Version -ne 1) {
        throw "Expected a missing/v1 retired detector fixture, found detector_version=$Version in $ProjectPath"
    }
    return "v1"
}

function Wait-ApplicationServer([Diagnostics.Process]$Process) {
    for ($Index = 0; $Index -lt 120; $Index++) {
        Start-Sleep -Milliseconds 500
        $Process.Refresh()
        if ($Process.HasExited) {
            throw "LMAStudio.exe exited before its bootstrap server became ready."
        }
        $Connection = Get-NetTCPConnection -OwningProcess $Process.Id -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($Connection) {
            return $Connection
        }
    }
    throw "LMAStudio.exe did not open a loopback listener."
}

function Get-ApplicationMeta([string]$BaseUrl) {
    for ($Index = 0; $Index -lt 120; $Index++) {
        try {
            return Invoke-RestMethod -Uri "$BaseUrl/api/meta" -TimeoutSec 10
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    }
    throw "LMAStudio.exe did not expose /api/meta."
}

function Invoke-ExpectedProjectRejection(
    [string]$BaseUrl,
    [string]$WriteToken,
    [string]$ProjectPath
) {
    $StatusCode = 0
    $Content = ""
    try {
        $Response = Invoke-WebRequest -Method Post -Uri "$BaseUrl/api/open-project" `
            -Headers @{ "X-Annotation-Write-Token" = $WriteToken } `
            -ContentType "application/json; charset=utf-8" `
            -Body (@{ project_dir = $ProjectPath } | ConvertTo-Json -Compress) `
            -TimeoutSec 60 -UseBasicParsing
        $StatusCode = [int]$Response.StatusCode
        $Content = [string]$Response.Content
    }
    catch {
        $ErrorResponse = $_.Exception.Response
        if ($null -eq $ErrorResponse) {
            throw
        }
        $StatusCode = [int]$ErrorResponse.StatusCode
        $Content = [string]$_.ErrorDetails.Message
        if ([string]::IsNullOrWhiteSpace($Content) -and
            $ErrorResponse.PSObject.Methods.Name -contains "GetResponseStream") {
            $Reader = New-Object IO.StreamReader($ErrorResponse.GetResponseStream())
            try {
                $Content = $Reader.ReadToEnd()
            }
            finally {
                $Reader.Dispose()
            }
        }
    }
    if ($StatusCode -ne 400) {
        throw "Expected HTTP 400 for retired project $ProjectPath; received $StatusCode. Body: $Content"
    }
    try {
        $Payload = $Content | ConvertFrom-Json
    }
    catch {
        throw "Retired-project rejection was not JSON: $Content"
    }
    $Message = [string]$Payload.error
    if ([string]::IsNullOrWhiteSpace($Message) -or $Message.Length -lt 20) {
        throw "Retired-project rejection did not provide a readable rebuild instruction: $Content"
    }
    # Keep this PS1 ASCII-only so Windows PowerShell 5 can parse it without a
    # UTF-8 BOM. These code points spell the expected peak-detector labels.
    $PeakDetectionLabel = -join ([char[]]@(0x5CF0, 0x8BC6, 0x522B))
    $RetiredLabel = -join ([char[]]@(0x5DF2, 0x505C, 0x7528))
    $InvalidLabel = -join ([char[]]@(0x65E0, 0x6548))
    if (!$Message.Contains($PeakDetectionLabel) -or
        (!$Message.Contains($RetiredLabel) -and !$Message.Contains($InvalidLabel))) {
        throw "HTTP 400 did not explicitly reject the retired peak detector: $Content"
    }
    return $Message
}

if (!(Test-SafeRegressionRoot $CopyRoot)) {
    throw "Regression copies must stay under the system temp directory with the LMAStudioProjectRegression_ prefix."
}
if (Test-Path -LiteralPath $CopyRoot) {
    throw "Regression root already exists: $CopyRoot"
}
if (Get-Process LMAStudio -ErrorAction SilentlyContinue) {
    throw "Close the running LMA Studio window before starting project regression."
}

$Results = @()
$TestProcesses = @()
New-Item -ItemType Directory -Path $CopyRoot | Out-Null
try {
    foreach ($Name in $ProjectNames) {
        $Source = (Resolve-Path (Join-Path $ProjectsRoot $Name)).Path
        $Copy = Join-Path $CopyRoot $Name

        # Originals are only read for copy and full-tree snapshots. The EXE is
        # pointed exclusively at the temporary copy.
        $SourceBefore = Get-FileTreeSnapshot $Source
        Copy-Item -LiteralPath $Source -Destination $Copy -Recurse
        $CopyBefore = Get-FileTreeSnapshot $Copy
        $RetiredDetector = Get-RetiredDetectorKind $Copy

        $Process = Start-Process -FilePath $Exe -WorkingDirectory (Split-Path $Exe) `
            -WindowStyle Hidden -PassThru
        $TestProcesses += $Process
        $Connection = Wait-ApplicationServer $Process
        $BaseUrl = "http://127.0.0.1:$($Connection.LocalPort)"
        $MetaBefore = Get-ApplicationMeta $BaseUrl
        if (!$MetaBefore.bootstrap) {
            throw "$Name regression did not start from the project-selection bootstrap."
        }

        $RejectionMessage = Invoke-ExpectedProjectRejection `
            $BaseUrl ([string]$MetaBefore.write_token) $Copy
        $MetaAfterRejection = Get-ApplicationMeta $BaseUrl
        if (!$MetaAfterRejection.bootstrap) {
            throw "$Name rejection replaced the bootstrap with a retired project."
        }

        # A hidden pywebview window has no reliable MainWindowHandle. Terminate
        # only the exact bootstrap process created above after the rejection.
        Stop-Process -Id $Process.Id -Force
        if (!$Process.WaitForExit(20000)) {
            throw "$Name bootstrap process did not exit after rejection."
        }
        if (Get-NetTCPConnection -LocalPort $Connection.LocalPort -State Listen -ErrorAction SilentlyContinue) {
            throw "$Name left its loopback listener running after exit."
        }

        $CopyAfter = Get-FileTreeSnapshot $Copy
        $SourceAfter = Get-FileTreeSnapshot $Source
        $CopyStable = $CopyBefore.canonical_json -ceq $CopyAfter.canonical_json
        $SourceStable = $SourceBefore.canonical_json -ceq $SourceAfter.canonical_json
        if (!$CopyStable) {
            $BeforeRows = @{}
            foreach ($Row in $CopyBefore.entries) {
                $BeforeRows[[string]$Row.relative_path] = ($Row | ConvertTo-Json -Compress)
            }
            $AfterRows = @{}
            foreach ($Row in $CopyAfter.entries) {
                $AfterRows[[string]$Row.relative_path] = ($Row | ConvertTo-Json -Compress)
            }
            $ChangedPaths = @(
                @($BeforeRows.Keys) + @($AfterRows.Keys) |
                    Sort-Object -Unique |
                    Where-Object {
                        !$BeforeRows.ContainsKey($_) -or
                        !$AfterRows.ContainsKey($_) -or
                        $BeforeRows[$_] -cne $AfterRows[$_]
                    }
            )
            Write-Host "Changed temporary-copy entries: $($ChangedPaths -join ', ')"
            throw "$Name temporary copy changed during rejection: before=$($CopyBefore.tree_sha256), after=$($CopyAfter.tree_sha256)."
        }
        if (!$SourceStable) {
            throw "$Name original project changed during rejection: before=$($SourceBefore.tree_sha256), after=$($SourceAfter.tree_sha256)."
        }

        $Results += [pscustomobject]@{
            Project = $Name
            RetiredDetector = $RetiredDetector
            HttpStatus = 400
            RejectionMessage = $RejectionMessage
            CopyEntries = $CopyAfter.entry_count
            CopyFiles = $CopyAfter.file_count
            CopyTreeHash = $CopyAfter.tree_sha256
            OriginalTreeHash = $SourceAfter.tree_sha256
            CopyStable = $CopyStable
            OriginalStable = $SourceStable
            BootstrapPreserved = [bool]$MetaAfterRejection.bootstrap
            ProcessExited = $true
        }
    }
    $Results | Format-Table -AutoSize
}
finally {
    foreach ($TestProcess in $TestProcesses) {
        try {
            $TestProcess.Refresh()
            if (!$TestProcess.HasExited) {
                Stop-Process -Id $TestProcess.Id -Force -ErrorAction SilentlyContinue
                $TestProcess.WaitForExit(10000) | Out-Null
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
