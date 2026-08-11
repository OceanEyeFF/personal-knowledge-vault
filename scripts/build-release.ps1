<#
.SYNOPSIS
Build the byte-reproducible unsigned Windows release Artifact.

.DESCRIPTION
This is the only supported W3 release build entrypoint.  It is deliberately
non-interactive.  The Python/Conda environment is a build-time dependency
only; the generated onedir application has no Python or Conda dependency.

.EXAMPLE
.\scripts\build-release.ps1
#>

[CmdletBinding(PositionalBinding = $false)]
param()

$ErrorActionPreference = "Stop"
$exitCode = 1
$utf8 = [System.Text.UTF8Encoding]::new($false)
$previousInputEncoding = [Console]::InputEncoding
$previousOutputEncoding = [Console]::OutputEncoding
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

try {
    [Console]::InputEncoding = $utf8
    [Console]::OutputEncoding = $utf8
    $OutputEncoding = $utf8

    $global:LASTEXITCODE = $null
    & (Join-Path $PSScriptRoot "run-windows.ps1") `
        python scripts/build_release.py --project-root $ProjectRoot
    $exitCode = if ($null -eq $LASTEXITCODE) { 1 } else { [int]$LASTEXITCODE }
} finally {
    [Console]::InputEncoding = $previousInputEncoding
    [Console]::OutputEncoding = $previousOutputEncoding
}

exit $exitCode
