<#
.SYNOPSIS
Build a fast PKV onedir package for INTERNAL TEST ONLY.

.DESCRIPTION
This is deliberately separate from scripts/build-release.ps1.  It produces an
uninstalled onedir tree and ZIP only below dist\internal, labels both as
INTERNAL TEST ONLY, and never writes dist\release.  -Smoke additionally uses a
synthetic .data-test root to exercise the externally unpacked package.  It is
not a release build or a release verification command.
#>

[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter()]
    [string]$OutputRoot,

    [Parameter()]
    [switch]$Smoke,

    [Parameter()]
    [switch]$KeepSmokeWorkspace,

    [Parameter()]
    [ValidateRange(0, [int]::MaxValue)]
    [int]$Seed = 20260813
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$exitCode = 1
$utf8 = [System.Text.UTF8Encoding]::new($false)
$previousInputEncoding = [Console]::InputEncoding
$previousOutputEncoding = [Console]::OutputEncoding
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

try {
    [Console]::InputEncoding = $utf8
    [Console]::OutputEncoding = $utf8
    $OutputEncoding = $utf8

    $arguments = @(
        'scripts/build_internal_package.py',
        '--project-root',
        $ProjectRoot
    )
    if (-not [string]::IsNullOrWhiteSpace($OutputRoot)) {
        $arguments += @('--output-root', $OutputRoot)
    }
    $global:LASTEXITCODE = $null
    $buildOutput = @(
        & (Join-Path $PSScriptRoot 'run-windows.ps1') python @arguments 2>&1
    )
    $exitCode = if ($null -eq $LASTEXITCODE) { 1 } else { [int]$LASTEXITCODE }
    foreach ($line in $buildOutput) {
        Write-Output $line
    }
    if ($exitCode -ne 0) {
        exit $exitCode
    }

    $resultLine = @(
        $buildOutput |
            ForEach-Object { [string]$_ } |
            Where-Object { $_ -match '"classification"\s*:\s*"INTERNAL TEST ONLY"' }
    ) | Select-Object -Last 1
    if ([string]::IsNullOrWhiteSpace([string]$resultLine)) {
        throw 'internal package builder did not emit its result JSON'
    }
    $result = $resultLine | ConvertFrom-Json -ErrorAction Stop
    if ([string]$result.classification -cne 'INTERNAL TEST ONLY' -or
        -not [bool]$result.payload_verified -or
        [string]::IsNullOrWhiteSpace([string]$result.package_root) -or
        [string]::IsNullOrWhiteSpace([string]$result.zip_path)) {
        throw 'internal package builder returned an invalid result contract'
    }

    if ($Smoke) {
        $dataRoot = ".data-test\internal-package-$([string]$result.package_id)"
        $runTest = Join-Path $PSScriptRoot 'run-test.ps1'
        & $runTest -Direct -DataRoot $dataRoot -Command @(
            'python', 'scripts/setup-test-db.py', '--seed', [string]$Seed, '--count', '6',
            '--runtime-ready'
        )
        if ($LASTEXITCODE -ne 0) {
            exit [int]$LASTEXITCODE
        }
        $smokeCommand = @(
            'powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
            (Join-Path $PSScriptRoot 'run-internal-package-smoke.ps1'),
            '-PackageRoot', [string]$result.package_root,
            '-PackageZip', [string]$result.zip_path
        )
        if ($KeepSmokeWorkspace) {
            $smokeCommand += '-KeepWorkspace'
        }
        & $runTest -Direct -DataRoot $dataRoot -Command $smokeCommand
        $exitCode = [int]$LASTEXITCODE
        if ($exitCode -ne 0) {
            exit $exitCode
        }
    }
} finally {
    [Console]::InputEncoding = $previousInputEncoding
    [Console]::OutputEncoding = $previousOutputEncoding
}

exit $exitCode
