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

function Show-Help {
    Write-Output "Hermes fork-local AE2 runner"
    Write-Output ""
    Write-Output "Usage: .\\run.ps1 [-Fast] [-SkipLaunch] [-NoCache] [-ForceAll] [-Clean]"
    Write-Output ""
    Write-Output "Profiles:"
    Write-Output "  -Fast        Checks only affected Python/frontend lanes; runs changed Python test files."
    Write-Output "  (default)   Runs the full canonical Python suite and all workspace checks."
    Write-Output ""
    Write-Output "Execution:"
    Write-Output "  -SkipLaunch Accepted for AgentEnforcer compatibility; Hermes launch stays opt-in."
    Write-Output ""
    Write-Output "Cache control:"
    Write-Output "  -NoCache    Ignores passed stage stamps for this run."
    Write-Output "  -ForceAll   Re-runs every cacheable stage."
    Write-Output "  -Clean      Removes only this repository's .ci_cache directory before the run."
}

if ($RemainingArguments.Count -gt 0) {
    Write-Output ("Error: unknown parameter(s): {0}" -f ($RemainingArguments -join ", "))
    exit 1
}

if ($Help) {
    Show-Help
    exit 0
}

$buildScript = Join-Path $PSScriptRoot "build.ps1"
if (-not (Test-Path -LiteralPath $buildScript -PathType Leaf)) {
    Write-Output ("Error: build.ps1 was not found at {0}" -f $buildScript)
    exit 1
}

$forwardedParameters = @{}
foreach ($entry in $PSBoundParameters.GetEnumerator()) {
    if ($entry.Key -ne "Help") {
        $forwardedParameters[$entry.Key] = $entry.Value
    }
}

if ($ForceAll) {
    $forwardedParameters["NoCache"] = $true
}

& $buildScript @forwardedParameters
if ($null -eq $LASTEXITCODE) {
    exit 0
}

exit ([int]$LASTEXITCODE)
