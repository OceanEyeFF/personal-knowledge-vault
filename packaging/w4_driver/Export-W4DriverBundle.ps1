#requires -Version 5.1

[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$OutputRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-Sha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    $stream = [System.IO.FileStream]::new(
        [System.IO.Path]::GetFullPath($Path),
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::Read
    )
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString(
            $algorithm.ComputeHash($stream)
        )).Replace('-', '').ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
        $stream.Dispose()
    }
}

function Get-StringSha256 {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.UTF8Encoding]::new($false).GetBytes($Value)
        return ([BitConverter]::ToString($algorithm.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
    }
}

function Test-ContainedBy {
    param(
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$Root
    )
    $candidatePath = [IO.Path]::GetFullPath($Candidate).TrimEnd('\', '/')
    $rootPath = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    return $candidatePath.Equals($rootPath, [StringComparison]::OrdinalIgnoreCase) -or
        $candidatePath.StartsWith($rootPath + '\', [StringComparison]::OrdinalIgnoreCase)
}

function Assert-NormalPathChain {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $cursor = [IO.Path]::GetFullPath($Path)
    while ($true) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "$Label contains a reparse point: $cursor"
            }
        }
        $parent = [IO.Path]::GetDirectoryName($cursor)
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent -eq $cursor) {
            break
        }
        $cursor = $parent
    }
}

try {
    $sourceRoot = [IO.Path]::GetFullPath($PSScriptRoot)
    $repositoryRoot = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath (Join-Path $sourceRoot '..\..')).ProviderPath)
    $fixtureSource = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath (Join-Path $repositoryRoot 'tests\fixtures\w4')).ProviderPath)
    $output = [IO.Path]::GetFullPath($OutputRoot)
    if (Test-ContainedBy -Candidate $output -Root $repositoryRoot) {
        throw "W4 driver bundle output must be outside the repository: $output"
    }
    if (Test-Path -LiteralPath $output) {
        throw "W4 driver bundle output already exists: $output"
    }
    Assert-NormalPathChain -Path $output -Label 'W4 driver bundle output'
    Assert-NormalPathChain -Path $sourceRoot -Label 'W4 driver source'
    Assert-NormalPathChain -Path $fixtureSource -Label 'W4 fixture source'
    [void][IO.Directory]::CreateDirectory($output)
    [void][IO.Directory]::CreateDirectory((Join-Path $output 'fixtures'))

    $controllerFiles = @(
        'Invoke-W4ArtifactE2E.ps1',
        'W4.Driver.psm1',
        'W4.Scenarios.psm1',
        'scenarios.v2.json'
    )
    foreach ($name in $controllerFiles) {
        $source = Join-Path $sourceRoot $name
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "W4 driver source is incomplete: $name"
        }
        [IO.File]::Copy($source, (Join-Path $output $name), $false)
    }
    foreach ($source in @(Get-ChildItem -LiteralPath $fixtureSource -File -Recurse -Force | Sort-Object FullName)) {
        $relative = $source.FullName.Substring($fixtureSource.TrimEnd('\').Length).TrimStart('\')
        if ($relative -match '(^|\\)(\.data|vault|logs?|tmp|tests?)(\\|$)' -or
            $source.Name -match '^(local\.ya?ml|\.env.*)$') {
            throw "Forbidden material in W4 synthetic fixture bundle: $relative"
        }
        $target = Join-Path (Join-Path $output 'fixtures') $relative
        [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target))
        [IO.File]::Copy($source.FullName, $target, $false)
    }

    $rows = [System.Collections.Generic.List[object]]::new()
    foreach ($file in @(Get-ChildItem -LiteralPath $output -File -Recurse -Force | Sort-Object FullName)) {
        $relative = $file.FullName.Substring($output.TrimEnd('\').Length).TrimStart('\').Replace('\', '/')
        $role = if ($relative.StartsWith('fixtures/')) {
            'synthetic_fixture'
        } elseif ($relative -eq 'scenarios.v2.json') {
            'scenario_contract'
        } elseif ($relative.EndsWith('.psm1')) {
            'controller_module'
        } else {
            'controller_entrypoint'
        }
        $rows.Add([ordered]@{
            path = $relative
            role = $role
            size = [int64]$file.Length
            sha256 = Get-Sha256 -Path $file.FullName
        })
    }
    $tree = [Text.StringBuilder]::new()
    foreach ($row in $rows) {
        [void]$tree.Append([string]$row.path)
        [void]$tree.Append([char]0)
        [void]$tree.Append([string][int64]$row.size)
        [void]$tree.Append([char]0)
        [void]$tree.Append([string]$row.sha256)
        [void]$tree.Append("`n")
    }
    $manifest = [ordered]@{
        schema_version = 'pkv.m13.w4-driver-bundle.v1'
        runner_version = 'pkv.m13.artifact-runner.v2'
        distribution = 'e2e-only'
        release_payload_membership = 'forbidden'
        self_excluded_paths = @('driver-manifest.json', 'driver-manifest.sha256')
        files = @($rows)
        tree_sha256 = Get-StringSha256 -Value $tree.ToString()
    }
    $utf8 = [Text.UTF8Encoding]::new($false)
    $manifestPath = Join-Path $output 'driver-manifest.json'
    [IO.File]::WriteAllText(
        $manifestPath,
        ($manifest | ConvertTo-Json -Depth 10) + "`n",
        $utf8
    )
    $manifestSha = Get-Sha256 -Path $manifestPath
    [IO.File]::WriteAllText(
        (Join-Path $output 'driver-manifest.sha256'),
        "$manifestSha  driver-manifest.json`n",
        [Text.Encoding]::ASCII
    )
    [Console]::Out.WriteLine(([ordered]@{
        schema_version = 'pkv.m13.w4-driver-export.v1'
        output_root = $output
        manifest_sha256 = $manifestSha
        file_count = $rows.Count
        tree_sha256 = [string]$manifest.tree_sha256
    } | ConvertTo-Json -Compress))
    exit 0
} catch {
    [Console]::Error.WriteLine("W4 driver export failed: $($_.Exception.Message)")
    exit 1
}
