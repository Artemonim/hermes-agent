[CmdletBinding()]
param(
    [switch]$Fast,
    [switch]$SkipLaunch,
    [Alias("NoCashe")]
    [switch]$NoCache,
    [switch]$ForceAll,
    [switch]$Clean,
    [Alias("h", "?")]
    [switch]$Help,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RemainingArguments = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# * User-facing configuration lives here so local CI behavior stays discoverable.
$script:CacheSchemaVersion = "1"
$script:CodebaseMemoryMode = "full"
$script:ReportSchemaVersion = 1

$script:RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:CacheDir = Join-Path $script:RepoRoot ".ci_cache"
$script:LogsDir = Join-Path $script:CacheDir "logs"
$script:ReportPath = Join-Path $script:CacheDir "report.json"
$script:EnforcerDir = Join-Path $script:RepoRoot ".enforcer"
$script:EnforcerLastCheckPath = Join-Path $script:EnforcerDir "Enforcer_last_check.log"
$script:EnforcerStatsPath = Join-Path $script:EnforcerDir "Enforcer_stats.log"
$script:AdapterPath = Join-Path $script:RepoRoot "scripts/ci/ae2_local.py"
$script:RunnerPath = Join-Path $script:RepoRoot "run.ps1"
$script:BuilderPath = Join-Path $script:RepoRoot "build.ps1"
$script:StageResults = New-Object System.Collections.Generic.List[object]
$script:Issues = New-Object System.Collections.Generic.List[object]
$script:Metrics = @{}
$script:RepoPaths = $null
$script:CiStartTime = Get-Date

function Show-Help {
    Write-Output "Use .\\run.ps1 -Help for the public runner help."
}

function Resolve-RepoRelativePath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $fullPath = [System.IO.Path]::GetFullPath($Path)
    if ($fullPath.StartsWith($script:RepoRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $fullPath.Substring($script:RepoRoot.Length).TrimStart("\").Replace("\", "/")
    }

    return $fullPath.Replace("\", "/")
}

function Get-PythonCommandPath {
    $candidates = @(
        (Join-Path $script:RepoRoot ".venv/Scripts/python.exe"),
        (Join-Path $script:RepoRoot "venv/Scripts/python.exe"),
        (Join-Path $script:RepoRoot ".venv/bin/python"),
        (Join-Path $script:RepoRoot "venv/bin/python")
    )

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }

    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $pythonCommand) {
        return $pythonCommand.Source
    }

    throw 'Python executable was not found. Run "uv sync --locked --extra dev" first.'
}

function Get-GitBashCommandPath {
    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($env:ProgramFiles)) {
        $candidates += Join-Path $env:ProgramFiles "Git/bin/bash.exe"
    }
    $candidates += "C:/Program Files/Git/bin/bash.exe"

    foreach ($candidate in $candidates | Select-Object -Unique) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }

    $bashCommand = Get-Command bash -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $bashCommand) {
        return $bashCommand.Source
    }

    throw "Bash was not found. Install Git for Windows so scripts/run_tests.sh can preserve CI parity."
}

function ConvertTo-Ae2StageResult {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [ValidateSet("ok", "warn", "fail", "cached", "skip")]
        [string]$Status,
        [string]$Note = "",
        [int]$DurationMs = 0,
        [hashtable]$Details = @{},
        [object[]]$Issues = @(),
        [hashtable]$Metrics = @{}
    )

    return [ordered]@{
        name = $Name
        status = $Status
        note = $Note
        duration_ms = $DurationMs
        details = $Details
        issues = $Issues
        metrics = $Metrics
    }
}

function Add-StageResult {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Result
    )

    $script:StageResults.Add($Result)
    foreach ($issue in @($Result.issues)) {
        if ($null -ne $issue) {
            $script:Issues.Add($issue)
        }
    }
    if ($Result.metrics -is [System.Collections.IDictionary]) {
        foreach ($key in $Result.metrics.Keys) {
            $script:Metrics[[string]$key] = $Result.metrics[$key]
        }
    } elseif ($null -ne $Result.metrics) {
        foreach ($property in $Result.metrics.PSObject.Properties) {
            $script:Metrics[$property.Name] = $property.Value
        }
    }
}

function Get-OverallStatus {
    if ($script:StageResults | Where-Object { $_.status -eq "fail" }) {
        return "fail"
    }
    if ($script:StageResults | Where-Object { $_.status -eq "warn" }) {
        return "warn"
    }
    return "ok"
}

function Write-StageCompletion {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Result
    )

    $durationLabel = ""
    if ($Result.duration_ms -gt 0) {
        $durationLabel = " ({0}s)" -f [math]::Round($Result.duration_ms / 1000.0, 1)
    }
    $noteLabel = if ([string]::IsNullOrWhiteSpace($Result.note)) { "" } else { " - {0}" -f $Result.note }
    Write-Information -MessageData ("[{0}] {1}{2}{3}" -f $Result.status.ToUpperInvariant(), $Result.name, $noteLabel, $durationLabel) -InformationAction Continue
}

function Write-StageLog {
    param(
        [Parameter(Mandatory = $true)]
        [string]$StageName,
        [Parameter(Mandatory = $true)]
        [string]$Content
    )

    $logPath = Join-Path $script:LogsDir ("{0}.log" -f $StageName)
    Set-Content -LiteralPath $logPath -Value $Content -Encoding utf8
    return $logPath
}

function Invoke-ExternalCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$StageName,
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [string[]]$Arguments = @()
    )

    $startedAt = Get-Date
    $output = & $FilePath @Arguments 2>&1
    $exitCode = $LASTEXITCODE
    $durationMs = [int]((Get-Date) - $startedAt).TotalMilliseconds
    $text = ($output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
    $logContent = @(
        ("command: {0} {1}" -f $FilePath, ($Arguments -join " ")),
        ("exit_code: {0}" -f $exitCode),
        "",
        $text
    ) -join [Environment]::NewLine
    $logPath = Write-StageLog -StageName $StageName -Content $logContent

    return [ordered]@{
        exit_code = $exitCode
        duration_ms = $durationMs
        output = $text
        log_path = $logPath
    }
}

function Get-RepositoryPathList {
    if ($null -ne $script:RepoPaths) {
        return $script:RepoPaths
    }

    $paths = & git -C $script:RepoRoot ls-files --cached --others --exclude-standard
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to enumerate tracked and untracked repository files with git ls-files."
    }
    $script:RepoPaths = @($paths | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    return $script:RepoPaths
}

function Get-StageInputPathList {
    param(
        [Parameter(Mandatory = $true)]
        [string]$StageName
    )

    switch ($StageName) {
        "self-check" {
            return @("run.ps1", "build.ps1", "scripts/ci/ae2_local.py", "AGENTS.md")
        }
        "lint" {
            return @(Get-RepositoryPathList | Where-Object { $_ -match "\.py$" -or $_ -in @("pyproject.toml", "uv.lock") })
        }
        "typecheck" {
            return @(Get-RepositoryPathList | Where-Object { $_ -match "\.py$" -or $_ -in @("pyproject.toml", "uv.lock") })
        }
        "compile" {
            return @(Get-RepositoryPathList | Where-Object { $_ -match "\.py$" -or $_ -in @("pyproject.toml", "uv.lock") })
        }
        "frontend" {
            return @(Get-RepositoryPathList | Where-Object {
                $_ -match "\.(ts|tsx|js|mjs|cjs|json)$" -or $_ -match "(^|/)eslint\.config\."
            })
        }
        default {
            return @()
        }
    }
}

function Get-ContentHash {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$RelativePaths,
        [Parameter(Mandatory = $true)]
        [string[]]$AdditionalValues
    )

    $hasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        foreach ($relativePath in ($RelativePaths | Sort-Object -Unique)) {
            $fullPath = Join-Path $script:RepoRoot $relativePath
            if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
                continue
            }
            $pathBytes = [System.Text.Encoding]::UTF8.GetBytes($relativePath.Replace("\", "/"))
            $contentBytes = [System.IO.File]::ReadAllBytes($fullPath)
            [void]$hasher.TransformBlock($pathBytes, 0, $pathBytes.Length, $pathBytes, 0)
            [void]$hasher.TransformBlock([byte[]](0), 0, 1, [byte[]](0), 0)
            [void]$hasher.TransformBlock($contentBytes, 0, $contentBytes.Length, $contentBytes, 0)
            [void]$hasher.TransformBlock([byte[]](0), 0, 1, [byte[]](0), 0)
        }
        foreach ($value in $AdditionalValues) {
            $valueBytes = [System.Text.Encoding]::UTF8.GetBytes($value)
            [void]$hasher.TransformBlock($valueBytes, 0, $valueBytes.Length, $valueBytes, 0)
            [void]$hasher.TransformBlock([byte[]](0), 0, 1, [byte[]](0), 0)
        }
        [void]$hasher.TransformFinalBlock([byte[]]::new(0), 0, 0)
        return ([System.BitConverter]::ToString($hasher.Hash)).Replace("-", "").ToLowerInvariant()
    } finally {
        $hasher.Dispose()
    }
}

function Get-StageCacheKey {
    param(
        [Parameter(Mandatory = $true)]
        [string]$StageName
    )

    $pythonPath = Get-PythonCommandPath
    $pythonVersion = (& $pythonPath --version 2>&1 | Out-String).Trim()
    $toolVersion = ""
    if ($StageName -eq "lint") {
        $toolVersion = (& $pythonPath -m ruff --version 2>&1 | Out-String).Trim()
    } elseif ($StageName -eq "typecheck") {
        $toolVersion = (& $pythonPath -m ty --version 2>&1 | Out-String).Trim()
    } elseif ($StageName -eq "frontend") {
        $toolVersion = (& npm --version 2>&1 | Out-String).Trim()
    }

    return Get-ContentHash -RelativePaths (Get-StageInputPathList -StageName $StageName) -AdditionalValues @(
        ("schema:{0}" -f $script:CacheSchemaVersion),
        ("stage:{0}" -f $StageName),
        ("python:{0}" -f $pythonVersion),
        ("tool:{0}" -f $toolVersion)
    )
}

function Test-StageCache {
    param(
        [Parameter(Mandatory = $true)]
        [string]$StageName,
        [Parameter(Mandatory = $true)]
        [string]$CacheKey
    )

    if ($NoCache) {
        return $false
    }
    $hashPath = Join-Path $script:CacheDir ("{0}.sha256" -f $StageName)
    $trustPath = Join-Path $script:CacheDir ("{0}.trusted" -f $StageName)
    if (-not (Test-Path -LiteralPath $hashPath -PathType Leaf) -or -not (Test-Path -LiteralPath $trustPath -PathType Leaf)) {
        return $false
    }
    return ((Get-Content -LiteralPath $hashPath -Raw).Trim() -eq $CacheKey)
}

function Write-StageCache {
    param(
        [Parameter(Mandatory = $true)]
        [string]$StageName,
        [Parameter(Mandatory = $true)]
        [string]$CacheKey
    )

    Set-Content -LiteralPath (Join-Path $script:CacheDir ("{0}.sha256" -f $StageName)) -Value $CacheKey -Encoding utf8
    New-Item -ItemType File -Path (Join-Path $script:CacheDir ("{0}.trusted" -f $StageName)) -Force | Out-Null
}

function Invoke-SelfCheckStage {
    $stageName = "self-check"
    $cacheKey = Get-StageCacheKey -StageName $stageName
    if (Test-StageCache -StageName $stageName -CacheKey $cacheKey) {
        $cached = ConvertTo-Ae2StageResult -Name $stageName -Status "cached" -Note "Cache hit."
        Add-StageResult -Result $cached
        Write-StageCompletion -Result $cached
        return
    }

    $startedAt = Get-Date
    $issues = New-Object System.Collections.Generic.List[object]
    foreach ($requiredPath in @($script:RunnerPath, $script:BuilderPath, $script:AdapterPath)) {
        if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
            $issues.Add([ordered]@{
                language = "ci"
                tool = "self-check"
                rule = "missing_file"
                count = 1
                message = ("Required CI file is missing: {0}" -f (Resolve-RepoRelativePath -Path $requiredPath))
            })
        }
    }

    foreach ($scriptPath in @($script:RunnerPath, $script:BuilderPath)) {
        if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
            continue
        }
        $tokens = $null
        $parseErrors = $null
        [void][System.Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$parseErrors)
        foreach ($parseError in $parseErrors) {
            $issues.Add([ordered]@{
                language = "powershell"
                tool = "parser"
                rule = "parse_error"
                count = 1
                message = ("{0}: {1}" -f (Resolve-RepoRelativePath -Path $scriptPath), $parseError.Message)
            })
        }
    }

    $analyzerModule = Get-Module -ListAvailable -Name PSScriptAnalyzer | Sort-Object Version -Descending | Select-Object -First 1
    if ($null -ne $analyzerModule) {
        Import-Module PSScriptAnalyzer -ErrorAction Stop
        foreach ($scriptPath in @($script:RunnerPath, $script:BuilderPath)) {
            if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
                continue
            }
            foreach ($finding in @(Invoke-ScriptAnalyzer -Path $scriptPath)) {
                $issues.Add([ordered]@{
                    language = "powershell"
                    tool = "PSScriptAnalyzer"
                    rule = $finding.RuleName
                    count = 1
                    message = ("{0}:{1}: {2}" -f (Resolve-RepoRelativePath -Path $finding.ScriptPath), $finding.Line, $finding.Message)
                })
            }
        }
    }

    $logLines = if ($issues.Count -eq 0) { @("Self-check passed.") } else { @($issues | ConvertTo-Json -Depth 6) }
    $logPath = Write-StageLog -StageName $stageName -Content ($logLines -join [Environment]::NewLine)
    $status = if ($issues.Count -eq 0) { "ok" } else { "fail" }
    $note = if ($null -eq $analyzerModule) { "PSScriptAnalyzer is not installed; parser checks passed." } elseif ($status -eq "ok") { "Runner and builder checks passed." } else { "Self-check found {0} issue(s)." -f $issues.Count }
    if ($null -eq $analyzerModule -and $status -eq "ok") {
        $status = "warn"
    }
    $result = ConvertTo-Ae2StageResult -Name $stageName -Status $status -Note $note -DurationMs ([int]((Get-Date) - $startedAt).TotalMilliseconds) -Details @{ log_path = (Resolve-RepoRelativePath -Path $logPath) } -Issues @($issues.ToArray())
    Add-StageResult -Result $result
    Write-StageCompletion -Result $result
    if ($status -eq "ok") {
        Write-StageCache -StageName $stageName -CacheKey $cacheKey
    }
    if ($status -eq "fail") {
        throw "Stage self-check failed."
    }
}

function Invoke-AdapterStage {
    param(
        [Parameter(Mandatory = $true)]
        [string]$StageName,
        [switch]$Cacheable,
        [switch]$Critical
    )

    $cacheKey = ""
    if ($Cacheable) {
        $cacheKey = Get-StageCacheKey -StageName $StageName
        if (Test-StageCache -StageName $StageName -CacheKey $cacheKey) {
            $cached = ConvertTo-Ae2StageResult -Name $StageName -Status "cached" -Note "Cache hit."
            Add-StageResult -Result $cached
            Write-StageCompletion -Result $cached
            return $cached
        }
    }

    $pythonPath = Get-PythonCommandPath
    $stageLogPath = Join-Path $script:LogsDir ("{0}.log" -f $StageName)
    $commandResult = Invoke-ExternalCommand -StageName ("{0}-adapter" -f $StageName) -FilePath $pythonPath -Arguments @(
        $script:AdapterPath,
        "--stage", $StageName,
        "--root", $script:RepoRoot,
        "--log-path", $stageLogPath
    )
    $jsonLine = $commandResult.output -split "`r?`n" | Where-Object { $_.TrimStart().StartsWith("{") } | Select-Object -Last 1
    if ($commandResult.exit_code -ne 0 -or [string]::IsNullOrWhiteSpace($jsonLine)) {
        $failed = ConvertTo-Ae2StageResult -Name $StageName -Status "fail" -Note "Stage adapter did not return a valid result." -DurationMs $commandResult.duration_ms -Details @{ log_path = (Resolve-RepoRelativePath -Path $commandResult.log_path) }
        Add-StageResult -Result $failed
        Write-StageCompletion -Result $failed
        if ($Critical) {
            throw ("Stage {0} failed." -f $StageName)
        }
        return $failed
    }

    $result = $jsonLine | ConvertFrom-Json
    $result.duration_ms = $commandResult.duration_ms
    $result.details | Add-Member -NotePropertyName "log_path" -NotePropertyValue (Resolve-RepoRelativePath -Path $stageLogPath) -Force
    $result.details | Add-Member -NotePropertyName "adapter_log_path" -NotePropertyValue (Resolve-RepoRelativePath -Path $commandResult.log_path) -Force
    Add-StageResult -Result $result
    Write-StageCompletion -Result $result
    if ($Cacheable -and $result.status -eq "ok") {
        Write-StageCache -StageName $StageName -CacheKey $cacheKey
    }
    if ($Critical -and $result.status -eq "fail") {
        throw ("Stage {0} failed: {1}" -f $StageName, $result.note)
    }
    return $result
}

function Invoke-TestStage {
    param(
        [string[]]$TestPaths = @()
    )

    $stageName = "test"
    $bashPath = Get-GitBashCommandPath
    $arguments = @("scripts/run_tests.sh") + $TestPaths
    $commandResult = Invoke-ExternalCommand -StageName $stageName -FilePath $bashPath -Arguments $arguments
    $status = if ($commandResult.exit_code -eq 0) { "ok" } else { "fail" }
    $note = if ($status -eq "ok") { "Canonical per-file Python test runner passed." } else { "Canonical Python test runner failed." }
    $result = ConvertTo-Ae2StageResult -Name $stageName -Status $status -Note $note -DurationMs $commandResult.duration_ms -Details @{ log_path = (Resolve-RepoRelativePath -Path $commandResult.log_path); selected_paths = $TestPaths }
    Add-StageResult -Result $result
    Write-StageCompletion -Result $result
    if ($status -eq "fail") {
        throw "Stage test failed."
    }
}

function Invoke-FrontendStage {
    $stageName = "frontend"
    $cacheKey = Get-StageCacheKey -StageName $stageName
    if (Test-StageCache -StageName $stageName -CacheKey $cacheKey) {
        $cached = ConvertTo-Ae2StageResult -Name $stageName -Status "cached" -Note "Cache hit."
        Add-StageResult -Result $cached
        Write-StageCompletion -Result $cached
        return
    }

    $npmCommand = Get-Command npm -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $npmCommand) {
        $failed = ConvertTo-Ae2StageResult -Name $stageName -Status "fail" -Note "npm was not found. Install the pinned Node/npm toolchain first."
        Add-StageResult -Result $failed
        Write-StageCompletion -Result $failed
        throw "Stage frontend failed."
    }

    $commandResult = Invoke-ExternalCommand -StageName $stageName -FilePath $npmCommand.Source -Arguments @("run", "check")
    $status = if ($commandResult.exit_code -eq 0) { "ok" } else { "fail" }
    $note = if ($status -eq "ok") { "Workspace checks passed." } else { "Workspace checks failed." }
    $result = ConvertTo-Ae2StageResult -Name $stageName -Status $status -Note $note -DurationMs $commandResult.duration_ms -Details @{ log_path = (Resolve-RepoRelativePath -Path $commandResult.log_path) }
    Add-StageResult -Result $result
    Write-StageCompletion -Result $result
    if ($status -eq "ok") {
        Write-StageCache -StageName $stageName -CacheKey $cacheKey
    } else {
        throw "Stage frontend failed."
    }
}

function Invoke-CodebaseMemoryStage {
    $stageName = "codebase-memory"
    $command = Get-Command codebase-memory-mcp -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $command) {
        $missing = ConvertTo-Ae2StageResult -Name $stageName -Status "warn" -Note "codebase-memory-mcp is unavailable; graph refresh was skipped."
        Add-StageResult -Result $missing
        Write-StageCompletion -Result $missing
        return
    }

    $commandResult = Invoke-ExternalCommand -StageName $stageName -FilePath $command.Source -Arguments @(
        "cli",
        "index_repository",
        "--repo-path", $script:RepoRoot,
        "--mode", $script:CodebaseMemoryMode,
        "--persistence", "false"
    )
    $status = if ($commandResult.exit_code -eq 0) { "ok" } else { "warn" }
    $note = if ($status -eq "ok") { "Code graph refreshed." } else { "Code graph refresh failed; it is an auxiliary navigation cache." }
    $result = ConvertTo-Ae2StageResult -Name $stageName -Status $status -Note $note -DurationMs $commandResult.duration_ms -Details @{ log_path = (Resolve-RepoRelativePath -Path $commandResult.log_path); mode = $script:CodebaseMemoryMode }
    Add-StageResult -Result $result
    Write-StageCompletion -Result $result
}

function Add-SkippedStage {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [string]$Note
    )

    $result = ConvertTo-Ae2StageResult -Name $Name -Status "skip" -Note $Note
    Add-StageResult -Result $result
    Write-StageCompletion -Result $result
}

function Write-CiReport {
    $report = [ordered]@{
        schema_version = $script:ReportSchemaVersion
        started_at_utc = $script:CiStartTime.ToUniversalTime().ToString("o")
        finished_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        duration_ms = [int]((Get-Date) - $script:CiStartTime).TotalMilliseconds
        status = Get-OverallStatus
        stages = $script:StageResults.ToArray()
        issues = $script:Issues.ToArray()
        metrics = $script:Metrics
        ci = @{ profile = if ($Fast) { "fast" } else { "full" }; skip_launch = [bool]$SkipLaunch }
    }
    Set-Content -LiteralPath $script:ReportPath -Value ($report | ConvertTo-Json -Depth 12) -Encoding utf8
}

function Write-EnforcerState {
    $reportJson = Get-Content -LiteralPath $script:ReportPath -Raw
    Set-Content -LiteralPath $script:EnforcerLastCheckPath -Value $reportJson -Encoding utf8
    Add-Content -LiteralPath $script:EnforcerStatsPath -Value ("--- Check finished at {0} (status={1}, profile={2}) ---" -f (Get-Date).ToUniversalTime().ToString("o"), (Get-OverallStatus), $(if ($Fast) { "fast" } else { "full" })) -Encoding utf8
}

function Write-CompactSummary {
    Write-Output ""
    Write-Output "=== Local AE2 Summary ==="
    foreach ($stage in $script:StageResults) {
        Write-Output ("[{0}] {1} - {2}" -f $stage.status.ToUpperInvariant(), $stage.name, $stage.note)
    }
    Write-Output ("Report: {0}" -f (Resolve-RepoRelativePath -Path $script:ReportPath))
}

if ($RemainingArguments.Count -gt 0) {
    Write-Output ("Error: unknown parameter(s): {0}" -f ($RemainingArguments -join ", "))
    exit 1
}

if ($Help) {
    Show-Help
    exit 0
}

if ($ForceAll) {
    $NoCache = $true
}

Set-Location $script:RepoRoot
if ($Clean -and (Test-Path -LiteralPath $script:CacheDir)) {
    Remove-Item -LiteralPath $script:CacheDir -Recurse -Force
}
New-Item -ItemType Directory -Path $script:LogsDir -Force | Out-Null
New-Item -ItemType Directory -Path $script:EnforcerDir -Force | Out-Null

$pipelineException = $null
try {
    Invoke-SelfCheckStage
    $changeResult = Invoke-AdapterStage -StageName "changed"
    $lanes = $changeResult.details.lanes
    $changedTestPaths = @($changeResult.details.test_files)

    Add-SkippedStage -Name "fmt" -Note "No repository-wide formatter contract exists; existing formatters are not broadened by this fork-local CI."
    Add-SkippedStage -Name "line-limits" -Note "Intentionally excluded: Hermes needs a separate delta/advisory policy, not global structural thresholds."

    $pythonLane = $lanes.PSObject.Properties["python"]
    $frontendLane = $lanes.PSObject.Properties["frontend"]
    $runPython = (-not $Fast) -or ($null -ne $pythonLane -and $pythonLane.Value -eq $true)
    if ($runPython) {
        Invoke-AdapterStage -StageName "lint" -Cacheable -Critical | Out-Null
        Invoke-AdapterStage -StageName "typecheck" -Cacheable | Out-Null
        Invoke-AdapterStage -StageName "compile" -Cacheable -Critical | Out-Null
    } else {
        Add-SkippedStage -Name "lint" -Note "No Python-relevant working-tree changes in the fast profile."
        Add-SkippedStage -Name "typecheck" -Note "No Python-relevant working-tree changes in the fast profile."
        Add-SkippedStage -Name "compile" -Note "No Python-relevant working-tree changes in the fast profile."
    }

    if ($Fast) {
        if ($changedTestPaths.Count -gt 0) {
            Invoke-TestStage -TestPaths $changedTestPaths
        } else {
            Add-SkippedStage -Name "test" -Note "Fast profile runs only changed Python test files; source-to-test inference is intentionally not implemented."
        }
    } else {
        Invoke-TestStage
    }

    $runFrontend = (-not $Fast) -or ($null -ne $frontendLane -and $frontendLane.Value -eq $true)
    if ($runFrontend) {
        Invoke-FrontendStage
    } else {
        Add-SkippedStage -Name "frontend" -Note "No frontend-relevant working-tree changes in the fast profile."
    }

    Add-SkippedStage -Name "coverage" -Note "Deferred: coverage must preserve Hermes per-file test isolation and combine parallel process data safely."
    Add-SkippedStage -Name "security" -Note "Deferred to existing OSV and supply-chain workflows; no new blocking scanner is enabled."
    Add-SkippedStage -Name "build" -Note "No universal Hermes artifact target exists in the minimal local CI."

    if ($Fast) {
        Add-SkippedStage -Name "codebase-memory" -Note "Skipped by fast profile."
    } else {
        Invoke-CodebaseMemoryStage
    }

    if ($SkipLaunch) {
        Add-SkippedStage -Name "launch" -Note "Skipped by -SkipLaunch."
    } else {
        Add-SkippedStage -Name "launch" -Note "Hermes launch is opt-in because the repository has multiple runtime surfaces."
    }
    Add-SkippedStage -Name "archive" -Note "No archive is produced by the minimal local CI."
} catch {
    $pipelineException = $_.Exception
    Write-Output ("Pipeline stopped: {0}" -f $_.Exception.Message)
    $failure = ConvertTo-Ae2StageResult -Name "pipeline" -Status "fail" -Note $_.Exception.Message
    Add-StageResult -Result $failure
} finally {
    try {
        Write-CiReport
    } catch {
        $pipelineException = $_.Exception
        Write-Output ("Report writing failed: {0}" -f $_.Exception.Message)
    }
    if (Test-Path -LiteralPath $script:ReportPath -PathType Leaf) {
        try {
            Write-EnforcerState
        } catch {
            $pipelineException = $_.Exception
            Write-Output ("Enforcer state writing failed: {0}" -f $_.Exception.Message)
        }
    }
    Write-CompactSummary
}

if ($null -ne $pipelineException -or (Get-OverallStatus) -eq "fail") {
    exit 1
}

exit 0
