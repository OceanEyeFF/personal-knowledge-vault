<#
.SYNOPSIS
Fail-closed preflight for the explicit M13 artifact-only lane.

.DESCRIPTION
This script validates an already installed artifact without importing or
executing the repository source tree.  It may optionally launch the supplied
artifact entrypoint as a contract probe from an isolated directory outside the
repository.  It deliberately does not make a release-verification decision.
#>

[CmdletBinding(DefaultParameterSetName = 'Preflight', PositionalBinding = $false)]
param(
    [Parameter(Mandatory = $true, ParameterSetName = 'Preflight')]
    [ValidateNotNullOrEmpty()]
    [string]$ArtifactRoot,

    [Parameter(Mandatory = $true, ParameterSetName = 'Preflight')]
    [ValidateNotNullOrEmpty()]
    [string]$EntryPoint,

    [Parameter(Mandatory = $true, ParameterSetName = 'Preflight')]
    [ValidateNotNullOrEmpty()]
    [string]$ManifestPath,

    [Parameter(Mandatory = $true, ParameterSetName = 'Preflight')]
    [ValidateNotNullOrEmpty()]
    [string]$FixturePath,

    [Parameter(Mandatory = $true, ParameterSetName = 'Preflight')]
    [Parameter(Mandatory = $true, ParameterSetName = 'FullMatrix')]
    [ValidateNotNullOrEmpty()]
    [string]$EvidenceRoot,

    [Parameter(ParameterSetName = 'Preflight')]
    [Parameter(Mandatory = $true, ParameterSetName = 'FullMatrix')]
    [string]$HarnessPath,

    [Parameter(ParameterSetName = 'Preflight')]
    [switch]$RequireHarness,

    [Parameter(ParameterSetName = 'Preflight')]
    [switch]$RunContractProbe,

    [Parameter(ParameterSetName = 'Preflight')]
    [string[]]$ProbeArguments = @(),

    [Parameter(ParameterSetName = 'Preflight')]
    [ValidateRange(1, 300)]
    [int]$ProbeTimeoutSeconds = 30,

    [Parameter(Mandatory = $true, ParameterSetName = 'FullMatrix')]
    [switch]$RunFullMatrix,

    [Parameter(Mandatory = $true, ParameterSetName = 'FullMatrix')]
    [ValidateNotNullOrEmpty()]
    [string]$DistributionZip,

    [Parameter(Mandatory = $true, ParameterSetName = 'FullMatrix')]
    [ValidateNotNullOrEmpty()]
    [string]$CandidateRoot,

    [Parameter(Mandatory = $true, ParameterSetName = 'FullMatrix')]
    [ValidateNotNullOrEmpty()]
    [string]$DistributionSha256Path,

    [Parameter(Mandatory = $true, ParameterSetName = 'FullMatrix')]
    [ValidateNotNullOrEmpty()]
    [string]$ProvenancePath,

    [Parameter(Mandatory = $true, ParameterSetName = 'FullMatrix')]
    [ValidateNotNullOrEmpty()]
    [string]$ComplianceSourcesRoot,

    [Parameter(Mandatory = $true, ParameterSetName = 'FullMatrix')]
    [ValidateNotNullOrEmpty()]
    [string]$ComplianceManifestPath,

    [Parameter(Mandatory = $true, ParameterSetName = 'FullMatrix')]
    [ValidateNotNullOrEmpty()]
    [string]$ComplianceProvenancePath,

    [Parameter(Mandatory = $true, ParameterSetName = 'FullMatrix')]
    [ValidateNotNullOrEmpty()]
    [string]$DriverRoot,

    [Parameter(Mandatory = $true, ParameterSetName = 'FullMatrix')]
    [ValidateNotNullOrEmpty()]
    [string]$WorkspaceRoot,

    [Parameter(Mandatory = $true, ParameterSetName = 'FullMatrix')]
    [ValidateNotNullOrEmpty()]
    [string]$HarnessWorkspaceRoot,

    [Parameter(ParameterSetName = 'FullMatrix')]
    [ValidateRange(60, 7200)]
    [int]$FullMatrixTimeoutSeconds = 3600,

    [Parameter()]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')]
    [string]$RunId = ([Guid]::NewGuid().ToString('N'))
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-CanonicalExistingPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][ValidateSet('Any', 'Container', 'Leaf')][string]$Kind
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Required path does not exist: $Path"
    }
    if ($Kind -eq 'Container' -and -not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "Required directory is not a directory: $Path"
    }
    if ($Kind -eq 'Leaf' -and -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required file is not a file: $Path"
    }

    $resolved = Resolve-Path -LiteralPath $Path -ErrorAction Stop
    return [System.IO.Path]::GetFullPath($resolved.ProviderPath)
}

function Get-CanonicalProspectivePath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $absolute = [System.IO.Path]::GetFullPath($Path)
    if (Test-Path -LiteralPath $absolute) {
        return Get-CanonicalExistingPath -Path $absolute -Kind Container
    }

    $missingSegments = [System.Collections.Generic.List[string]]::new()
    $cursor = $absolute
    while (-not (Test-Path -LiteralPath $cursor)) {
        $leaf = [System.IO.Path]::GetFileName($cursor)
        if ([string]::IsNullOrWhiteSpace($leaf)) {
            throw "Evidence root has no resolvable parent: $Path"
        }
        $missingSegments.Insert(0, $leaf)
        $parent = [System.IO.Path]::GetDirectoryName($cursor)
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent -eq $cursor) {
            throw "Evidence root has no resolvable parent: $Path"
        }
        $cursor = $parent
    }

    $resolved = Get-CanonicalExistingPath -Path $cursor -Kind Container
    foreach ($segment in $missingSegments) {
        $resolved = Join-Path $resolved $segment
    }
    return [System.IO.Path]::GetFullPath($resolved)
}

function Test-PathContainedBy {
    param(
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$Root
    )

    $trimChars = [char[]]@([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
    $candidatePath = ([System.IO.Path]::GetFullPath($Candidate)).TrimEnd($trimChars)
    $rootPath = ([System.IO.Path]::GetFullPath($Root)).TrimEnd($trimChars)
    if ($candidatePath.Equals($rootPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    $prefix = $rootPath + [System.IO.Path]::DirectorySeparatorChar
    return $candidatePath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
}

function Assert-OutsideRepository {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$RepositoryRoot
    )

    if (Test-PathContainedBy -Candidate $Path -Root $RepositoryRoot) {
        throw "$Label must resolve outside the repository: $Path"
    }
}

function Assert-DisjointPaths {
    param(
        [Parameter(Mandatory = $true)][string]$First,
        [Parameter(Mandatory = $true)][string]$FirstLabel,
        [Parameter(Mandatory = $true)][string]$Second,
        [Parameter(Mandatory = $true)][string]$SecondLabel
    )

    if (
        (Test-PathContainedBy -Candidate $First -Root $Second) -or
        (Test-PathContainedBy -Candidate $Second -Root $First)
    ) {
        throw "$FirstLabel and $SecondLabel must be disjoint"
    }
}

function Test-TextContainsPath {
    param(
        [AllowNull()][string]$Text,
        [Parameter(Mandatory = $true)][string]$Path
    )

    if ([string]::IsNullOrEmpty($Text)) {
        return $false
    }
    $normalizedText = $Text.Replace('/', '\')
    $normalizedPath = $Path.Replace('/', '\').TrimEnd('\')
    return $normalizedText.IndexOf($normalizedPath, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
}

function ConvertTo-WindowsCommandLineArgument {
    param([AllowEmptyString()][string]$Value)

    if ($Value -cnotmatch '[\s"]') {
        return $Value
    }

    $builder = [System.Text.StringBuilder]::new()
    [void]$builder.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $backslashes += 1
            continue
        }
        if ($character -eq '"') {
            [void]$builder.Append((('\' * (($backslashes * 2) + 1)) -join ''))
            [void]$builder.Append('"')
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            [void]$builder.Append((('\' * $backslashes) -join ''))
            $backslashes = 0
        }
        [void]$builder.Append($character)
    }
    if ($backslashes -gt 0) {
        [void]$builder.Append((('\' * ($backslashes * 2)) -join ''))
    }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Assert-SingleFileLink {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if ([Environment]::OSVersion.Platform -cne [System.PlatformID]::Win32NT) {
        throw "Cannot determine hard-link state for $Label on this platform"
    }
    $systemRoot = [Environment]::GetEnvironmentVariable('SystemRoot')
    if ([string]::IsNullOrWhiteSpace($systemRoot)) {
        throw "Cannot determine hard-link state for $Label because SystemRoot is unavailable"
    }
    $fsutil = Join-Path $systemRoot 'System32\fsutil.exe'
    if (-not (Test-Path -LiteralPath $fsutil -PathType Leaf)) {
        throw "Cannot determine hard-link state for $Label because fsutil.exe is unavailable"
    }

    $processInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $processInfo.FileName = $fsutil
    $processInfo.Arguments = 'hardlink list ' + (ConvertTo-WindowsCommandLineArgument -Value $Path)
    $processInfo.UseShellExecute = $false
    $processInfo.CreateNoWindow = $true
    $processInfo.RedirectStandardOutput = $true
    $processInfo.RedirectStandardError = $true

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $processInfo
    try {
        if (-not $process.Start()) {
            throw 'fsutil.exe did not start'
        }
        $standardOutputTask = $process.StandardOutput.ReadToEndAsync()
        $standardErrorTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(10000)) {
            try {
                $process.Kill($true)
            } catch {
                $process.Kill()
            }
            [void]$process.WaitForExit(5000)
            throw 'fsutil.exe timed out'
        }
        $process.WaitForExit()
        $standardOutput = $standardOutputTask.GetAwaiter().GetResult()
        $standardError = $standardErrorTask.GetAwaiter().GetResult()
        if ($process.ExitCode -cne 0) {
            throw "fsutil.exe exited $($process.ExitCode): $standardError"
        }
    } catch {
        throw "Cannot determine hard-link state for $Label at $($Path): $($_.Exception.Message)"
    } finally {
        $process.Dispose()
    }

    $links = @(
        $standardOutput -split '\r?\n' |
            ForEach-Object { $_.Trim() } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
    if ($links.Count -cne 1 -or -not $links[0].StartsWith('\')) {
        if ($links.Count -gt 1) {
            throw "Unsafe HardLink rejected for $Label at $Path"
        }
        throw "Cannot determine hard-link state for $Label at $Path"
    }
}

function Assert-SafePathChain {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $cursor = [System.IO.Path]::GetFullPath($Path)
    $isLeaf = $true
    while ($true) {
        $item = $null
        try {
            $item = Get-Item -LiteralPath $cursor -Force -ErrorAction Stop
        } catch [System.Management.Automation.ItemNotFoundException] {
            $item = $null
        } catch {
            throw "Cannot inspect path chain for $Label at $($cursor): $($_.Exception.Message)"
        }

        if ($null -cne $item) {
            if (
                ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne
                0
            ) {
                throw "Unsafe ReparsePoint rejected for $Label at $cursor"
            }
            if ($isLeaf -and -not $item.PSIsContainer) {
                Assert-SingleFileLink -Path $cursor -Label $Label
            }
        }

        $parent = [System.IO.Path]::GetDirectoryName($cursor)
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent -eq $cursor) {
            break
        }
        $cursor = $parent
        $isLeaf = $false
    }
}

function Assert-SafeTree {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Label
    )

    Assert-SafePathChain -Path $Root -Label $Label
    $resolvedRoot = Get-CanonicalExistingPath -Path $Root -Kind Container
    $pending = [System.Collections.Generic.Stack[string]]::new()
    $pending.Push($resolvedRoot)
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        foreach ($item in @(Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop)) {
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -cne 0) {
                throw "Unsafe ReparsePoint rejected inside $Label at $($item.FullName)"
            }
            if ($item.PSIsContainer) {
                $pending.Push($item.FullName)
            } else {
                Assert-SingleFileLink -Path $item.FullName -Label "$Label file"
            }
        }
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

function Get-LocalFileHash {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath,
        [Parameter(Mandatory = $true)][ValidateSet('SHA256')][string]$Algorithm
    )

    $fullPath = [System.IO.Path]::GetFullPath($LiteralPath)
    if (-not [System.IO.File]::Exists($fullPath)) {
        throw "Cannot hash missing file: $fullPath"
    }
    $stream = [System.IO.FileStream]::new(
        $fullPath,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::Read
    )
    $hasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        $digest = ([System.BitConverter]::ToString(
            $hasher.ComputeHash($stream)
        )).Replace('-', '').ToLowerInvariant()
    } finally {
        $hasher.Dispose()
        $stream.Dispose()
    }
    return [pscustomobject]@{
        Algorithm = 'SHA256'
        Hash = $digest
        Path = $fullPath
    }
}

function Get-CanonicalJsonSha256 {
    param(
        [Parameter(Mandatory = $true)]$Value
    )

    $json = ConvertTo-Json -InputObject $Value -Depth 100 -Compress
    # Windows PowerShell 5.1 HTML-escapes printable JSON characters that the W3
    # Python canonical encoder (ensure_ascii=False) writes literally.
    $json = [System.Text.RegularExpressions.Regex]::Replace(
        $json,
        '(?<!\x5c)(?<pairs>(?:\x5c\x5c)*)\x5cu(?<code>0026|0027|003[cCeE]|0085|2028|2029)',
        {
            param($match)
            return $match.Groups['pairs'].Value +
                [char][Convert]::ToInt32($match.Groups['code'].Value, 16)
        }
    )
    return Get-StringSha256 -Value ($json + "`n")
}

function Get-FileSegmentSha256 {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][ValidateRange(0, [int64]::MaxValue)][int64]$Offset,
        [Parameter(Mandatory = $true)][ValidateRange(0, [int64]::MaxValue)][int64]$Length,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $file = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($file.PSIsContainer -or $Offset -gt [int64]$file.Length -or
        $Length -gt ([int64]$file.Length - $Offset)) {
        throw "$Label byte range is outside the file"
    }
    $stream = [System.IO.File]::Open(
        $file.FullName,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::Read
    )
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        [void]$stream.Seek($Offset, [System.IO.SeekOrigin]::Begin)
        [byte[]]$buffer = New-Object byte[] (1024 * 1024)
        [int64]$remaining = $Length
        while ($remaining -gt 0) {
            $requested = [int][Math]::Min([int64]$buffer.Length, $remaining)
            $read = $stream.Read($buffer, 0, $requested)
            if ($read -le 0) {
                throw "$Label ended before the declared byte range"
            }
            [void]$algorithm.TransformBlock($buffer, 0, $read, $buffer, 0)
            $remaining -= $read
        }
        [void]$algorithm.TransformFinalBlock([byte[]]@(), 0, 0)
        return ([BitConverter]::ToString($algorithm.Hash)).Replace('-', '').ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
        $stream.Dispose()
    }
}

function Get-Utf8SortedStrings {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [object[]]$Values,
        [switch]$Unique
    )

    [string[]]$strings = @($Values | ForEach-Object { [string]$_ })
    $utf8 = [System.Text.UTF8Encoding]::new($false)
    $comparison = [System.Comparison[string]]{
        param([string]$left, [string]$right)
        [byte[]]$leftBytes = $utf8.GetBytes($left)
        [byte[]]$rightBytes = $utf8.GetBytes($right)
        $length = [Math]::Min($leftBytes.Length, $rightBytes.Length)
        for ($index = 0; $index -lt $length; $index += 1) {
            if ($leftBytes[$index] -lt $rightBytes[$index]) { return -1 }
            if ($leftBytes[$index] -gt $rightBytes[$index]) { return 1 }
        }
        return $leftBytes.Length.CompareTo($rightBytes.Length)
    }
    $comparer = [System.Collections.Generic.Comparer[string]]::Create($comparison)
    [Array]::Sort($strings, $comparer)
    if (-not $Unique) {
        return $strings
    }
    $result = [System.Collections.Generic.List[string]]::new()
    $previous = $null
    foreach ($value in $strings) {
        if ($null -eq $previous -or -not [string]::Equals(
            $previous, $value, [System.StringComparison]::Ordinal
        )) {
            $result.Add($value)
            $previous = $value
        }
    }
    return $result.ToArray()
}

function Get-CanonicalDistributionName {
    param([Parameter(Mandatory = $true)][string]$Value)
    return (($Value -replace '[-_.]+', '-').ToLowerInvariant())
}

function Assert-DistributionOwnerBinding {
    param(
        [Parameter(Mandatory = $true)][string]$DistributionName,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$ComponentIds,
        [Parameter(Mandatory = $true)][string]$SourceRef,
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [object[]]$LogicalDestinations,
        [switch]$AllowPyInstallerBootloader,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $canonicalName = Get-CanonicalDistributionName -Value $DistributionName
    if ([string]::IsNullOrWhiteSpace($DistributionName) -or
        $DistributionName -cne $canonicalName) {
        throw "$Label distribution name is not canonical: $DistributionName"
    }
    $components = @($ComponentIds | ForEach-Object { [string]$_ })
    $genericId = "python-distribution:$canonicalName"
    $foldedSourceRef = "/$($SourceRef.Replace([char]92, '/').ToLowerInvariant())"
    $foldedDestinations = @(
        Get-Utf8SortedStrings -Values @($LogicalDestinations | ForEach-Object {
            ([string]$_).Replace([char]92, '/').ToLowerInvariant()
        }) -Unique
    )
    if ($canonicalName -ceq 'pyinstaller') {
        $isHooks = (
            $foldedSourceRef.Contains('/pyinstaller/hooks/rthooks/') -or
            $foldedSourceRef.Contains('/pyinstaller/fake-modules/_pyi_rth_utils/')
        )
        $isBootloader = (
            $AllowPyInstallerBootloader -and
            $foldedDestinations.Count -eq 1 -and
            $foldedDestinations[0] -ceq 'pyiboot01_bootstrap' -and
            $foldedSourceRef -ceq
                '/python-prefix/lib/site-packages/pyinstaller/loader/pyiboot01_bootstrap.py'
        )
        if ($isHooks -eq $isBootloader) {
            throw "$Label PyInstaller source does not select exactly one dedicated runtime owner"
        }
        $expectedId = if ($isHooks) {
            'build-runtime:pyinstaller-hooks'
        } else {
            'build-runtime:pyinstaller-bootloader'
        }
        $otherId = if ($isHooks) {
            'build-runtime:pyinstaller-bootloader'
        } else {
            'build-runtime:pyinstaller-hooks'
        }
        if ($components -ccontains $genericId -or
            $components -cnotcontains $expectedId -or
            $components -ccontains $otherId -or
            $components -ccontains 'build-runtime:pyinstaller-hooks-contrib') {
            throw "$Label PyInstaller owner is not the exact dedicated runtime component"
        }
        return
    }
    if ($canonicalName -ceq 'pyinstaller-hooks-contrib') {
        if (-not $foldedSourceRef.Contains('/_pyinstaller_hooks_contrib/rthooks/') -or
            $components -ccontains $genericId -or
            $components -cnotcontains 'build-runtime:pyinstaller-hooks-contrib' -or
            $components -ccontains 'build-runtime:pyinstaller-bootloader' -or
            $components -ccontains 'build-runtime:pyinstaller-hooks') {
            throw "$Label PyInstaller hooks-contrib owner must be its dedicated runtime component"
        }
        return
    }
    if ($components -cnotcontains $genericId) {
        throw "$Label distribution owner is absent from component_ids: $canonicalName"
    }
}

function Assert-DistributionOwnerSet {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [object[]]$DistributionNames,
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [object[]]$ComponentIds,
        [Parameter(Mandatory = $true)][string]$SourceRef,
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [object[]]$LogicalDestinations,
        [switch]$AllowPyInstallerBootloader,
        [switch]$AllowAggregateComponentOwners,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $names = @($DistributionNames | ForEach-Object { [string]$_ })
    $components = @($ComponentIds | ForEach-Object { [string]$_ })
    foreach ($name in $names) {
        Assert-DistributionOwnerBinding -DistributionName $name `
            -ComponentIds $components -SourceRef $SourceRef `
            -LogicalDestinations $LogicalDestinations `
            -AllowPyInstallerBootloader:$AllowPyInstallerBootloader -Label $Label
    }
    $expectedGenericIds = @(
        Get-Utf8SortedStrings -Values @($names | Where-Object {
            $_ -cnotin @('pyinstaller', 'pyinstaller-hooks-contrib')
        } | ForEach-Object { "python-distribution:$_" }) -Unique
    )
    $actualGenericIds = @(
        Get-Utf8SortedStrings -Values @($components | Where-Object {
            $_.StartsWith('python-distribution:', [System.StringComparison]::Ordinal)
        }) -Unique
    )
    if (-not $AllowAggregateComponentOwners -and
        ($actualGenericIds | ConvertTo-Json -Compress) -cne
        ($expectedGenericIds | ConvertTo-Json -Compress)) {
        throw "$Label generic distribution component owners are not exact"
    }
}

function Assert-Utf8SortedUniqueStrings {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [object[]]$Values,
        [Parameter(Mandatory = $true)][string]$Label,
        [switch]$RejectCaseCollisions
    )

    $strings = @($Values | ForEach-Object { [string]$_ })
    $sorted = @(Get-Utf8SortedStrings -Values $strings)
    if (($strings | ConvertTo-Json -Compress) -cne
        ($sorted | ConvertTo-Json -Compress)) {
        throw "$Label is not sorted with ordinal/UTF-8 semantics"
    }
    $comparer = if ($RejectCaseCollisions) {
        [System.StringComparer]::OrdinalIgnoreCase
    } else {
        [System.StringComparer]::Ordinal
    }
    $seen = [System.Collections.Generic.HashSet[string]]::new($comparer)
    foreach ($value in $strings) {
        if (-not $seen.Add($value)) {
            throw "$Label contains a duplicate or case-colliding value: $value"
        }
    }
}

function Get-TreeManifestRows {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [string[]]$ExcludedRelativePaths = @()
    )

    $resolvedRoot = Get-CanonicalExistingPath -Path $Root -Kind Container
    $excluded = @{}
    foreach ($relative in $ExcludedRelativePaths) {
        $excluded[([string]$relative).Replace('\', '/').ToLowerInvariant()] = $true
    }
    $rowsByPath = [System.Collections.Generic.Dictionary[string, object]]::new(
        [System.StringComparer]::Ordinal
    )
    $caseFoldedPaths = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    foreach ($file in @(Get-ChildItem -LiteralPath $resolvedRoot -File -Recurse -Force)) {
        $relative = $file.FullName.Substring($resolvedRoot.TrimEnd('\').Length).
            TrimStart('\').Replace('\', '/')
        if ($excluded.ContainsKey($relative.ToLowerInvariant())) {
            continue
        }
        if (-not $caseFoldedPaths.Add($relative)) {
            throw "tree contains a duplicate or case-colliding path: $relative"
        }
        $rowsByPath.Add($relative, [ordered]@{
            path = $relative
            size = [int64]$file.Length
            sha256 = (Get-LocalFileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        })
    }
    $rows = [System.Collections.Generic.List[object]]::new()
    # Keep the outer runner's tree serialization byte-for-byte aligned with
    # W4.Driver:Get-W4SafeTreeFiles, which orders its FullName values through
    # Sort-Object before the controller hashes the manifest.
    foreach ($relative in @($rowsByPath.Keys | Sort-Object)) {
        $rows.Add($rowsByPath[$relative])
    }
    return @($rows)
}

function Get-TreeManifestSha256 {
    param([Parameter(Mandatory = $true)][string]$Root)

    $rows = @(Get-TreeManifestRows -Root $Root)
    return Get-StringSha256 -Value ($rows | ConvertTo-Json -Depth 5 -Compress)
}

function Read-StrictJsonFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label is missing: $Path"
    }
    try {
        $value = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8) |
            ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw "$Label is not valid JSON: $Path"
    }
    if ($null -eq $value) {
        throw "$Label contains null: $Path"
    }
    return $value
}

function Assert-ExactJsonFields {
    param(
        [Parameter(Mandatory = $true)]$Object,
        [Parameter(Mandatory = $true)][string[]]$Fields,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $actual = @(Get-Utf8SortedStrings -Values $Object.PSObject.Properties.Name)
    $expected = @(Get-Utf8SortedStrings -Values $Fields)
    if (($actual | ConvertTo-Json -Compress) -cne ($expected | ConvertTo-Json -Compress)) {
        throw "$Label fields are not exact"
    }
}

function Assert-ExactJsonBoolean {
    param(
        [Parameter(Mandatory = $true)][AllowNull()]$Value,
        [Parameter(Mandatory = $true)][bool]$Expected,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if ($Value -isnot [bool] -or [bool]$Value -cne $Expected) {
        throw "$Label must be the JSON boolean $($Expected.ToString().ToLowerInvariant())"
    }
}

function Get-ExpectedLicenseMaterialStatus {
    param(
        [Parameter(Mandatory = $true)][string]$ComponentId,
        [Parameter(Mandatory = $true)]$InventoryComponent,
        [Parameter(Mandatory = $true)]$LicenseIndexComponent
    )

    if ($ComponentId.StartsWith(
        'conda-package:', [System.StringComparison]::Ordinal
    )) {
        return 'metadata-only-compliance-hold'
    }
    if ([bool]$InventoryComponent.contains_native_payload -or
        @($InventoryComponent.classification_ids) -ccontains 'framework:qt-pyside' -or
        @($InventoryComponent.classification_ids) -ccontains 'native:msvc-runtime') {
        return 'top-level-only-compliance-hold'
    }
    if (@($LicenseIndexComponent.license_files).Count -gt 0) {
        return 'bound'
    }
    return 'metadata-only-compliance-hold'
}

function Assert-ReleaseBlockerAuthority {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$Rows,
        [Parameter(Mandatory = $true)][string]$DeclaredSha256,
        [Parameter(Mandatory = $true)][string[]]$ExpectedIds,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if ($DeclaredSha256 -cnotmatch '^[0-9a-f]{64}$' -or $Rows.Count -cne $ExpectedIds.Count) {
        throw "$Label hash/count is invalid"
    }
    $actualIds = [System.Collections.Generic.List[string]]::new()
    foreach ($row in $Rows) {
        $identifier = [string]$row.id
        $fields = @('condition', 'id', 'resolution')
        if ($identifier -ceq 'conda-native-license-materials-and-spdx') {
            $fields += 'affected_component_selectors'
        }
        if ($identifier -ceq 'html2text-gpl-compliance') {
            $fields += 'resolution_requirements'
        }
        Assert-ExactJsonFields -Object $row -Fields $fields -Label "$Label row $identifier"
        if ([string]::IsNullOrWhiteSpace([string]$row.condition) -or
            [string]::IsNullOrWhiteSpace([string]$row.resolution)) {
            throw "$Label row is incomplete: $identifier"
        }
        if ($identifier -ceq 'conda-native-license-materials-and-spdx') {
            $selectors = @(
                'component:*[native-payload]',
                'conda-package:*'
            )
            if ((@($row.affected_component_selectors) | ConvertTo-Json -Compress) -cne
                ($selectors | ConvertTo-Json -Compress)) {
                throw 'Conda/native blocker affected component selectors are not exact'
            }
        }
        if ($identifier -ceq 'html2text-gpl-compliance') {
            $requirements = @(
                'combined-work-licensing-decision',
                'corresponding-source-scope-and-persistent-location',
                'spdx-license-expression',
                'whole-work-license-and-notices'
            )
            if ((@($row.resolution_requirements) | ConvertTo-Json -Compress) -cne
                ($requirements | ConvertTo-Json -Compress)) {
                throw 'html2text GPL compliance resolution requirements are not exact'
            }
        }
        $actualIds.Add($identifier)
    }
    if ((@($actualIds) | ConvertTo-Json -Compress) -cne
        (@($ExpectedIds) | ConvertTo-Json -Compress)) {
        throw "$Label IDs/order differ from the canonical compliance authority"
    }
    $actualSha = Get-CanonicalJsonSha256 -Value ([object[]]$Rows)
    if ($actualSha -cne $DeclaredSha256) {
        throw "$Label SHA-256 does not match canonical rows"
    }
    return $actualSha
}

function Assert-CondaHardlinkThreatEvidence {
    param(
        [Parameter(Mandatory = $true)]$Evidence,
        [Parameter(Mandatory = $true)][string]$Label
    )

    Assert-ExactJsonFields -Object $Evidence -Fields @(
        'schema_version', 'anchors', 'observed_hardlink_anchor_count',
        'release_eligible_environment_requirement', 'threat_model', 'validation_scope'
    ) -Label $Label
    $expectedLabels = @(
        'numpy-package-anchor', 'python-dll', 'python-executable'
    )
    $anchors = @($Evidence.anchors)
    if ([string]$Evidence.schema_version -cne 'pkv.conda-hardlink-threat-evidence.v1' -or
        [string]$Evidence.release_eligible_environment_requirement -cne
            'copy-only-no-hardlinks' -or
        [string]$Evidence.threat_model -cne 'accepted_for_test_candidate' -or
        (@($Evidence.validation_scope) | ConvertTo-Json -Compress) -cne
            (@(
                'before-build-a', 'after-build-a', 'before-build-b',
                'after-build-b', 'before-publication'
            ) | ConvertTo-Json -Compress) -or
        $anchors.Count -cne $expectedLabels.Count) {
        throw "$Label contract is invalid"
    }
    $observedHardlinks = 0
    for ($anchorIndex = 0; $anchorIndex -lt $expectedLabels.Count; $anchorIndex += 1) {
        $anchor = $anchors[$anchorIndex]
        Assert-ExactJsonFields -Object $anchor -Fields @(
            'hardlink_count', 'label', 'path', 'sha256', 'size'
        ) -Label "$Label anchor"
        if ([string]$anchor.label -cne $expectedLabels[$anchorIndex] -or
            [string]::IsNullOrWhiteSpace([string]$anchor.path) -or
            ([string]$anchor.path).Contains([char]92) -or
            ([string]$anchor.path) -match '(^|/)\.\.(/|$)' -or
            [string]$anchor.sha256 -cnotmatch '^[0-9a-f]{64}$' -or
            [int64]$anchor.size -le 0 -or [int64]$anchor.hardlink_count -le 0) {
            throw "$Label anchor is invalid: $($expectedLabels[$anchorIndex])"
        }
        if ([int64]$anchor.hardlink_count -gt 1) {
            $observedHardlinks += 1
        }
    }
    if ([int64]$Evidence.observed_hardlink_anchor_count -cne $observedHardlinks) {
        throw "$Label observed hardlink count is inconsistent"
    }
}

function Assert-InventorySourceGraph {
    param(
        [Parameter(Mandatory = $true)]$Inventory,
        [Parameter(Mandatory = $true)]$ComponentById,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $sourceByComponent = @{}
    $distributionSources = @{}
    $condaSources = @{}
    $unownedSourcePaths = [System.Collections.Generic.List[string]]::new()
    $paths = @($Inventory.analysis.sources | ForEach-Object { [string]$_.path })
    Assert-Utf8SortedUniqueStrings -Values $paths -Label "$Label sources" `
        -RejectCaseCollisions
    foreach ($source in @($Inventory.analysis.sources)) {
        Assert-ExactJsonFields -Object $source -Fields @(
            'component_ids', 'conda_component_ids', 'distribution_names',
            'occurrences', 'path', 'sha256', 'size'
        ) -Label "$Label source"
        $path = [string]$source.path
        $componentIds = @($source.component_ids | ForEach-Object { [string]$_ })
        $condaIds = @($source.conda_component_ids | ForEach-Object { [string]$_ })
        $distributionNames = @(
            $source.distribution_names | ForEach-Object { [string]$_ }
        )
        if ([string]::IsNullOrWhiteSpace($path) -or $path.Contains([char]92) -or
            $path -match '(^|/)\.\.(/|$)' -or
            [string]$source.sha256 -cnotmatch '^[0-9a-f]{64}$' -or
            [int64]$source.size -lt 0 -or $componentIds.Count -le 0 -or
            @($source.occurrences).Count -le 0) {
            throw "$Label source identity is invalid: $path"
        }
        Assert-Utf8SortedUniqueStrings -Values $componentIds `
            -Label "$Label source components $path"
        Assert-Utf8SortedUniqueStrings -Values $condaIds `
            -Label "$Label source Conda owners $path"
        Assert-Utf8SortedUniqueStrings -Values $distributionNames `
            -Label "$Label source distribution owners $path"
        $occurrenceKeys = [System.Collections.Generic.List[string]]::new()
        foreach ($occurrence in @($source.occurrences)) {
            Assert-ExactJsonFields -Object $occurrence -Fields @(
                'destination', 'slot', 'type'
            ) -Label "$Label source occurrence"
            if ([string]::IsNullOrWhiteSpace([string]$occurrence.destination) -or
                [string]::IsNullOrWhiteSpace([string]$occurrence.slot) -or
                [string]::IsNullOrWhiteSpace([string]$occurrence.type) -or
                ([string]$occurrence.destination).Contains([char]92) -or
                [string]$occurrence.destination -match '(^|/)\.\.(/|$)') {
                throw "$Label source occurrence is invalid: $path"
            }
            $occurrenceKeys.Add(
                "$([string]$occurrence.slot)`0$([string]$occurrence.destination)"
            )
        }
        Assert-Utf8SortedUniqueStrings -Values @($occurrenceKeys) `
            -Label "$Label source occurrences $path"
        foreach ($componentId in $componentIds) {
            if (-not $ComponentById.ContainsKey($componentId)) {
                throw "$Label source references unknown component: $componentId"
            }
            if (-not $sourceByComponent.ContainsKey($componentId)) {
                $sourceByComponent[$componentId] = @()
            }
            $sourceByComponent[$componentId] += $path
        }
        foreach ($condaId in $condaIds) {
            if (-not $ComponentById.ContainsKey($condaId) -or
                -not $condaId.StartsWith('conda-package:', [System.StringComparison]::Ordinal) -or
                $componentIds -cnotcontains $condaId) {
                throw "$Label source has invalid Conda owner: $condaId"
            }
            if (-not $condaSources.ContainsKey($condaId)) { $condaSources[$condaId] = @() }
            $condaSources[$condaId] += $path
        }
        $sourceOccurrences = @($source.occurrences)
        $sourceAllowsPyInstallerBootloader = (
            $sourceOccurrences.Count -gt 0 -and
            @($sourceOccurrences | Where-Object {
                -not ([string]$_.slot).StartsWith(
                    'embedded:', [System.StringComparison]::Ordinal
                ) -or [string]$_.destination -cne 'pyiboot01_bootstrap'
            }).Count -eq 0
        )
        Assert-DistributionOwnerSet -DistributionNames $distributionNames `
            -ComponentIds $componentIds -SourceRef $path `
            -LogicalDestinations @(
                $source.occurrences | ForEach-Object { [string]$_.destination }
            ) -AllowPyInstallerBootloader:$sourceAllowsPyInstallerBootloader `
            -Label "$Label source $path"
        foreach ($distributionName in $distributionNames) {
            if (-not $distributionSources.ContainsKey($distributionName)) {
                $distributionSources[$distributionName] = @()
            }
            $distributionSources[$distributionName] += $path
        }
        if ($condaIds.Count -eq 0 -and $distributionNames.Count -eq 0) {
            $unownedSourcePaths.Add($path)
        }
    }

    $virtualKeys = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    foreach ($virtual in @($Inventory.analysis.virtual_entries)) {
        Assert-ExactJsonFields -Object $virtual -Fields @(
            'destination', 'slot', 'type'
        ) -Label "$Label virtual entry"
        $key = "$([string]$virtual.slot)`0$([string]$virtual.destination)"
        if ([string]::IsNullOrWhiteSpace([string]$virtual.destination) -or
            [string]::IsNullOrWhiteSpace([string]$virtual.slot) -or
            [string]$virtual.type -cne 'PYMODULE' -or
            ([string]$virtual.destination).Contains([char]92) -or
            [string]$virtual.destination -match '(^\.|\.$|\.\.|/)' -or
            -not $virtualKeys.Add($key)) {
            throw "$Label virtual entry is invalid: $key"
        }
    }

    $includedDistributionNames = @(
        $Inventory.included_distributions | ForEach-Object { [string]$_.name }
    )
    Assert-Utf8SortedUniqueStrings -Values $includedDistributionNames `
        -Label "$Label included distributions"
    if (($includedDistributionNames | ConvertTo-Json -Compress) -cne
        (@(Get-Utf8SortedStrings -Values $distributionSources.Keys) |
            ConvertTo-Json -Compress)) {
        throw "$Label included distributions differ from source ownership"
    }
    foreach ($included in @($Inventory.included_distributions)) {
        Assert-ExactJsonFields -Object $included -Fields @(
            'name', 'source_paths', 'version'
        ) -Label "$Label included distribution"
        $name = [string]$included.name
        $canonicalName = Get-CanonicalDistributionName -Value $name
        $expectedSources = @(
            Get-Utf8SortedStrings -Values @($distributionSources[$name]) -Unique
        )
        if ($name -cne $canonicalName -or
            [string]::IsNullOrWhiteSpace([string]$included.version) -or
            (@($included.source_paths) | ConvertTo-Json -Compress) -cne
                ($expectedSources | ConvertTo-Json -Compress)) {
            throw "$Label included distribution binding is invalid: $name"
        }
        if ($canonicalName -cin @('pyinstaller', 'pyinstaller-hooks-contrib')) {
            $genericId = "python-distribution:$canonicalName"
            $specialIds = if ($canonicalName -ceq 'pyinstaller') {
                @(
                    'build-runtime:pyinstaller-bootloader',
                    'build-runtime:pyinstaller-hooks'
                )
            } else {
                @('build-runtime:pyinstaller-hooks-contrib')
            }
            $specialComponents = @($specialIds | ForEach-Object {
                if ($ComponentById.ContainsKey($_)) { $ComponentById[$_] }
            })
            if ($ComponentById.ContainsKey($genericId) -or
                $specialComponents.Count -le 0 -or
                @($specialComponents | Where-Object {
                    [string]$_.identity_status -cne 'complete' -or
                    [string]$_.version -cne [string]$included.version
                }).Count -gt 0) {
                throw "$Label included special distribution binding is invalid: $name"
            }
        } else {
            $component = $ComponentById["python-distribution:$canonicalName"]
            if ($null -eq $component -or
                [string]$component.identity_status -cne 'complete' -or
                [string]$component.name -cne $canonicalName -or
                [string]$component.version -cne [string]$included.version -or
                (@($component.source_paths) | ConvertTo-Json -Compress) -cne
                    ($expectedSources | ConvertTo-Json -Compress)) {
                throw "$Label included distribution component is invalid: $name"
            }
        }
    }

    $includedCondaIds = @(
        $Inventory.included_conda_packages | ForEach-Object { [string]$_.component_id }
    )
    Assert-Utf8SortedUniqueStrings -Values $includedCondaIds `
        -Label "$Label included Conda packages"
    if (($includedCondaIds | ConvertTo-Json -Compress) -cne
        (@(Get-Utf8SortedStrings -Values $condaSources.Keys) | ConvertTo-Json -Compress)) {
        throw "$Label included Conda packages differ from source ownership"
    }
    foreach ($included in @($Inventory.included_conda_packages)) {
        Assert-ExactJsonFields -Object $included -Fields @(
            'build', 'channel', 'component_id', 'declared_license', 'name',
            'package_sha256', 'record_sha256', 'record_size', 'source_paths', 'version'
        ) -Label "$Label included Conda package"
        $componentId = [string]$included.component_id
        $component = $ComponentById[$componentId]
        $expectedSources = @(
            Get-Utf8SortedStrings -Values @($condaSources[$componentId]) -Unique
        )
        if ($null -eq $component -or
            [string]$component.identity_status -cne 'complete' -or
            [string]$component.build -cne [string]$included.build -or
            [string]$component.channel -cne [string]$included.channel -or
            [string]$component.declared_license -cne [string]$included.declared_license -or
            [string]$component.name -cne [string]$included.name -or
            [string]$component.package_sha256 -cne [string]$included.package_sha256 -or
            [string]$component.record_sha256 -cne [string]$included.record_sha256 -or
            [int64]$component.record_size -cne [int64]$included.record_size -or
            [string]$component.version -cne [string]$included.version -or
            (@($included.source_paths) | ConvertTo-Json -Compress) -cne
                ($expectedSources | ConvertTo-Json -Compress)) {
            throw "$Label included Conda binding is invalid: $componentId"
        }
    }

    return [pscustomobject]@{
        SourceByComponent = $sourceByComponent
        UnownedSourcePaths = @(
            Get-Utf8SortedStrings -Values @($unownedSourcePaths) -Unique
        )
    }
}

function Assert-HarnessReleaseInventory {
    param(
        [Parameter(Mandatory = $true)]$Inventory,
        [Parameter(Mandatory = $true)][string]$InventoryPath,
        [Parameter(Mandatory = $true)][string]$RuntimePath,
        [Parameter(Mandatory = $true)]$Provenance,
        [Parameter(Mandatory = $true)][object[]]$ExpectedAuthorityRows,
        [Parameter(Mandatory = $true)][string]$ExpectedAuthoritySha256,
        [Parameter(Mandatory = $true)][string[]]$ExpectedBlockers
    )

    Assert-ExactJsonFields -Object $Inventory -Fields @(
        'analysis', 'authority', 'bindings', 'components', 'coverage',
        'embedded_archives', 'included_conda_packages', 'included_distributions',
        'payload', 'schema_version'
    ) -Label 'W4 harness release inventory'
    if ([string]$Inventory.schema_version -cne 'pkv.release-inventory.v1') {
        throw 'W4 harness release inventory schema is invalid'
    }

    $authority = $Inventory.authority
    Assert-ExactJsonFields -Object $authority -Fields @(
        'artifact_kind', 'artifact_status', 'build_fingerprint',
        'conda_native_registry_path', 'conda_native_registry_sha256',
        'environment_lock_path', 'environment_lock_sha256',
        'release_blocker_authority', 'release_blocker_authority_sha256',
        'release_blockers', 'release_eligible', 'source_revision'
    ) -Label 'W4 harness release inventory authority'
    Assert-ExactJsonBoolean -Value $authority.release_eligible -Expected $false `
        -Label 'W4 harness inventory authority release_eligible'
    $inventoryAuthoritySha = Assert-ReleaseBlockerAuthority `
        -Rows @($authority.release_blocker_authority) `
        -DeclaredSha256 ([string]$authority.release_blocker_authority_sha256) `
        -ExpectedIds $ExpectedBlockers -Label 'W4 harness inventory blocker authority'
    if ([string]$authority.artifact_kind -cne 'e2e_test_harness' -or
        [string]$authority.artifact_status -cne
            'internal-verification-only-on-native-compliance-hold' -or
        [string]$authority.build_fingerprint -cne [string]$Provenance.build_fingerprint -or
        [string]$authority.conda_native_registry_path -cne
            'packaging/locks/conda-native-registry.v1.json' -or
        [string]$authority.conda_native_registry_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$authority.environment_lock_path -cne
            'packaging/locks/release-environment.v2.json' -or
        [string]$authority.environment_lock_sha256 -cne
            [string]$Provenance.toolchain_lock_sha256 -or
        $inventoryAuthoritySha -cne $ExpectedAuthoritySha256 -or
        (@($authority.release_blocker_authority) | ConvertTo-Json -Depth 20 -Compress) -cne
            (@($ExpectedAuthorityRows) | ConvertTo-Json -Depth 20 -Compress) -or
        (@($authority.release_blockers) | ConvertTo-Json -Compress) -cne
            ($ExpectedBlockers | ConvertTo-Json -Compress) -or
        [bool]$authority.release_eligible -or
        [string]$authority.source_revision -cne [string]$Provenance.source_revision) {
        throw 'W4 harness release inventory authority is inconsistent'
    }

    Assert-ExactJsonFields -Object $Inventory.analysis -Fields @(
        'entry_count', 'portable_graph_sha256', 'source_count', 'sources',
        'virtual_entries'
    ) -Label 'W4 harness inventory Analysis graph'
    if ([int64]$Inventory.analysis.entry_count -le 0 -or
        [int64]$Inventory.analysis.source_count -cne @($Inventory.analysis.sources).Count -or
        [string]$Inventory.analysis.portable_graph_sha256 -cnotmatch '^[0-9a-f]{64}$') {
        throw 'W4 harness inventory Analysis graph is invalid'
    }

    $bindings = $Inventory.bindings
    Assert-ExactJsonFields -Object $bindings -Fields @(
        'analysis_graph_sha256', 'artifact_closure_sha256', 'artifact_path_base',
        'closure_sha256', 'conda_native_registry_sha256',
        'embedded_archives_sha256', 'payload_tree_sha256'
    ) -Label 'W4 harness release inventory bindings'
    foreach ($bindingName in @(
        'analysis_graph_sha256', 'artifact_closure_sha256', 'closure_sha256',
        'conda_native_registry_sha256', 'embedded_archives_sha256',
        'payload_tree_sha256'
    )) {
        if ([string]$bindings.$bindingName -cnotmatch '^[0-9a-f]{64}$') {
            throw "W4 harness inventory binding is invalid: $bindingName"
        }
    }

    $coverage = $Inventory.coverage
    Assert-ExactJsonFields -Object $coverage -Fields @(
        'conda_native_registry_sha256', 'embedded_archive_count',
        'embedded_entry_count', 'payload_file_count',
        'unattributed_native_file_count', 'unattributed_native_paths',
        'unowned_source_path_count', 'unowned_source_paths',
        'unresolved_component_ids'
    ) -Label 'W4 harness release inventory coverage'
    if ([int64]$coverage.embedded_archive_count -cne 1 -or
        [int64]$coverage.embedded_entry_count -cne 66 -or
        [int64]$coverage.payload_file_count -cne 1 -or
        [int64]$coverage.unattributed_native_file_count -cne 0 -or
        @($coverage.unattributed_native_paths).Count -cne 0 -or
        @($coverage.unresolved_component_ids).Count -cne 0 -or
        [int64]$coverage.unowned_source_path_count -ne
            @($coverage.unowned_source_paths).Count -or
        [string]$coverage.conda_native_registry_sha256 -cne
            [string]$bindings.conda_native_registry_sha256 -or
        [string]$bindings.conda_native_registry_sha256 -cne
            [string]$authority.conda_native_registry_sha256) {
        throw 'W4 harness release inventory coverage is incomplete'
    }

    $archives = @($Inventory.embedded_archives)
    if ($archives.Count -cne 1) {
        throw 'W4 harness release inventory must contain exactly one embedded archive'
    }
    $archive = $archives[0]
    Assert-ExactJsonFields -Object $archive -Fields @(
        'bootloader_input', 'bootloader_prefix_sha256', 'bootloader_prefix_size',
        'component_ids', 'entries', 'entry_count', 'executable_artifact_path',
        'executable_sha256', 'executable_size', 'pkg_sha256', 'pkg_size',
        'portable_graph_sha256', 'python_library', 'python_version'
    ) -Label 'W4 harness embedded archive'
    Assert-ExactJsonFields -Object $archive.bootloader_input -Fields @(
        'source_ref', 'source_sha256', 'source_size'
    ) -Label 'W4 harness bootloader input'
    $runtime = Get-Item -LiteralPath $RuntimePath -Force -ErrorAction Stop
    $runtimeSha = (Get-LocalFileHash -LiteralPath $runtime.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    if ([string]$archive.executable_artifact_path -cne 'pkv-loopback-provider.exe' -or
        [string]$archive.executable_sha256 -cne $runtimeSha -or
        [int64]$archive.executable_size -cne [int64]$runtime.Length -or
        [int64]$archive.entry_count -cne 66 -or
        @($archive.entries).Count -cne 66 -or
        [string]$archive.pkg_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        [int64]$archive.pkg_size -le 0 -or
        [int64]$archive.pkg_size -ge [int64]$runtime.Length -or
        [string]$archive.bootloader_prefix_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        [int64]$archive.bootloader_prefix_size -ne
            ([int64]$runtime.Length - [int64]$archive.pkg_size) -or
        [string]$archive.bootloader_input.source_ref -cnotmatch '^python-prefix/' -or
        [string]$archive.bootloader_input.source_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        [int64]$archive.bootloader_input.source_size -le 0 -or
        [string]$archive.python_library -cne 'python311.dll' -or
        [int64]$archive.python_version -cne 311) {
        throw 'W4 harness embedded archive identity/hash/size contract is invalid'
    }
    if ((Get-FileSegmentSha256 -Path $RuntimePath `
            -Offset ([int64]$archive.bootloader_prefix_size) `
            -Length ([int64]$archive.pkg_size) -Label 'W4 harness embedded PKG') -cne
            [string]$archive.pkg_sha256 -or
        (Get-FileSegmentSha256 -Path $RuntimePath -Offset 0 `
            -Length ([int64]$archive.bootloader_prefix_size) `
            -Label 'W4 harness bootloader prefix') -cne
            [string]$archive.bootloader_prefix_sha256) {
        throw 'W4 harness executable is not the exact bootloader-prefix + PKG byte sequence'
    }

    $kindCounts = @{}
    $entryNames = @{}
    $archiveComponentIds = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::Ordinal
    )
    foreach ($entry in @($archive.entries)) {
        $kind = [string]$entry.kind
        $fields = @(
            'component_ids', 'compressed', 'content_sha256', 'kind', 'name',
            'stored_sha256', 'stored_size', 'typecode', 'uncompressed_size'
        )
        if ($kind -cne 'OPTION') {
            $fields += @(
                'conda_component_ids', 'distribution_names', 'source_ref',
                'source_sha256', 'source_size'
            )
        }
        if ($kind -eq 'PYZ') {
            $fields += @(
                'pyz_member_count', 'pyz_members', 'pyz_members_sha256',
                'pyz_python_magic_sha256', 'pyz_toc_sha256', 'pyz_toc_size'
            )
        }
        Assert-ExactJsonFields -Object $entry -Fields $fields `
            -Label "W4 harness embedded archive entry $kind"
        $expectedTypecode = @{
            BINARY = 'b'; DATA = 'b'; EXTENSION = 'b'; OPTION = 'o'
            PYMODULE = 'm'; PYSOURCE = 's'; PYZ = 'z'
        }[$kind]
        if ($null -eq $expectedTypecode -or
            [string]::IsNullOrWhiteSpace([string]$entry.name) -or
            ([string]$entry.name).Contains([char]92) -or
            ([string]$entry.name) -match '(^|/)\.\.(/|$)' -or
            [string]$entry.typecode -cne $expectedTypecode -or
            $entry.compressed -isnot [bool] -or
            [string]$entry.content_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
            [string]$entry.stored_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
            [int64]$entry.stored_size -lt 0 -or [int64]$entry.uncompressed_size -lt 0 -or
            @($entry.component_ids).Count -eq 0) {
            throw "W4 harness embedded archive entry is invalid: $kind/$($entry.name)"
        }
        $entryComponentIds = @($entry.component_ids | ForEach-Object { [string]$_ })
        Assert-Utf8SortedUniqueStrings -Values $entryComponentIds `
            -Label "W4 harness embedded entry components $($entry.name)"
        $foldedEntryName = ([string]$entry.name).ToLowerInvariant()
        if ($kind -cne 'OPTION' -and $entryNames.ContainsKey($foldedEntryName)) {
            throw "W4 harness embedded archive has a duplicate/case-colliding entry: $($entry.name)"
        }
        if ($kind -cne 'OPTION') {
            $entryNames[$foldedEntryName] = $true
        }
        if (-not $kindCounts.ContainsKey($kind)) {
            $kindCounts[$kind] = 0
        }
        $kindCounts[$kind] = [int]$kindCounts[$kind] + 1
        foreach ($componentId in @($entry.component_ids)) {
            if ([string]::IsNullOrWhiteSpace([string]$componentId)) {
                throw 'W4 harness embedded archive has an empty component ID'
            }
            [void]$archiveComponentIds.Add([string]$componentId)
        }
        if ($kind -eq 'OPTION') {
            if ([int64]$entry.stored_size -cne 0 -or
                [int64]$entry.uncompressed_size -cne 0 -or
                [string]$entry.content_sha256 -cne
                    'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855' -or
                [string]$entry.stored_sha256 -cne
                    'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855') {
                throw 'W4 harness OPTION entry is not the canonical empty entry'
            }
        } else {
            $entryCondaIds = @(
                $entry.conda_component_ids | ForEach-Object { [string]$_ }
            )
            $entryDistributionNames = @(
                $entry.distribution_names | ForEach-Object { [string]$_ }
            )
            Assert-Utf8SortedUniqueStrings -Values $entryCondaIds `
                -Label "W4 harness embedded entry Conda owners $($entry.name)"
            Assert-Utf8SortedUniqueStrings -Values $entryDistributionNames `
                -Label "W4 harness embedded entry distribution owners $($entry.name)"
            if ([string]$entry.source_ref -cnotmatch '^[a-z][a-z0-9-]{0,63}/' -or
                ([string]$entry.source_ref).Contains([char]92) -or
                ([string]$entry.source_ref) -match '(^|/)\.\.(/|$)' -or
                [string]$entry.source_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
                [int64]$entry.source_size -le 0) {
                throw "W4 harness embedded source binding is invalid: $($entry.name)"
            }
            foreach ($condaId in $entryCondaIds) {
                if ($entryComponentIds -cnotcontains $condaId) {
                    throw "W4 harness embedded Conda owner is absent from component_ids: $condaId"
                }
            }
            Assert-DistributionOwnerSet -DistributionNames $entryDistributionNames `
                -ComponentIds $entryComponentIds `
                -SourceRef ([string]$entry.source_ref) `
                -LogicalDestinations @([string]$entry.name) `
                -AllowPyInstallerBootloader `
                -AllowAggregateComponentOwners:($kind -ceq 'PYZ') `
                -Label "W4 harness embedded entry $($entry.name)"
        }
        if ($kind -eq 'PYZ') {
            $pyzMembers = @($entry.pyz_members)
            if ([int64]$entry.pyz_member_count -cne $pyzMembers.Count -or
                $pyzMembers.Count -le 0 -or
                [string]$entry.pyz_members_sha256 -cne
                    (Get-CanonicalJsonSha256 -Value ([object[]]$pyzMembers)) -or
                [string]$entry.pyz_python_magic_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
                [string]$entry.pyz_toc_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
                [int64]$entry.pyz_toc_size -le 0) {
                throw 'W4 harness PYZ member graph binding is invalid'
            }
            $pyzMemberKeys = @{}
            $pyzMemberNames = [System.Collections.Generic.List[string]]::new()
            $pyzComponentIds = [System.Collections.Generic.HashSet[string]]::new(
                [System.StringComparer]::Ordinal
            )
            $emptySha256 = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
            [int64]$pyzStoredSize = 0
            foreach ($member in $pyzMembers) {
                Assert-ExactJsonFields -Object $member -Fields @(
                    'component_ids', 'conda_component_ids', 'content_sha256',
                    'content_size', 'distribution_names', 'kind', 'name',
                    'source_kind', 'source_ref', 'source_sha256', 'source_size',
                    'stored_sha256', 'stored_size'
                ) -Label 'W4 harness PYZ member'
                $memberName = [string]$member.name
                $memberKind = [string]$member.kind
                $memberComponentIds = @(
                    $member.component_ids | ForEach-Object { [string]$_ }
                )
                $memberCondaIds = @(
                    $member.conda_component_ids | ForEach-Object { [string]$_ }
                )
                $memberDistributionNames = @(
                    $member.distribution_names | ForEach-Object { [string]$_ }
                )
                $memberKey = "$memberName`0$([string]$member.source_ref)"
                if ($pyzMemberKeys.ContainsKey($memberKey) -or
                    [string]::IsNullOrWhiteSpace($memberName) -or
                    $memberName -match '(^\.|\.$|\.\.|[/\\\x00])' -or
                    $memberKind -cnotin @('module', 'package', 'namespace') -or
                    [string]$member.source_kind -cnotin @(
                        'PYMODULE', 'PYMODULE-1', 'PYMODULE-2'
                    ) -or
                    [string]$member.content_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
                    [string]$member.source_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
                    [string]$member.stored_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
                    [int64]$member.content_size -lt 0 -or
                    [int64]$member.source_size -lt 0 -or
                    [int64]$member.stored_size -lt 0 -or
                    $memberComponentIds.Count -le 0) {
                    throw "W4 harness PYZ member identity is invalid: $memberName"
                }
                Assert-Utf8SortedUniqueStrings -Values $memberComponentIds `
                    -Label "W4 harness PYZ member components $memberName"
                Assert-Utf8SortedUniqueStrings -Values $memberCondaIds `
                    -Label "W4 harness PYZ member Conda owners $memberName"
                Assert-Utf8SortedUniqueStrings -Values $memberDistributionNames `
                    -Label "W4 harness PYZ member distribution owners $memberName"
                foreach ($condaId in $memberCondaIds) {
                    if ($memberComponentIds -cnotcontains $condaId) {
                        throw "W4 harness PYZ Conda owner is absent from component_ids: $condaId"
                    }
                }
                Assert-DistributionOwnerSet -DistributionNames $memberDistributionNames `
                    -ComponentIds $memberComponentIds `
                    -SourceRef ([string]$member.source_ref) `
                    -LogicalDestinations @($memberName) `
                    -Label "W4 harness PYZ member $memberName"
                if ($memberKind -ceq 'namespace') {
                    $expectedVirtualRef = "virtual-namespace/$($memberName.Replace('.', '/'))"
                    if ([int64]$member.content_size -cne 0 -or
                        [int64]$member.source_size -cne 0 -or
                        [int64]$member.stored_size -cne 0 -or
                        [string]$member.content_sha256 -cne $emptySha256 -or
                        [string]$member.source_sha256 -cne $emptySha256 -or
                        [string]$member.stored_sha256 -cne $emptySha256 -or
                        [string]$member.source_ref -cne $expectedVirtualRef) {
                        throw "W4 harness PYZ namespace binding is invalid: $memberName"
                    }
                } elseif (
                    [int64]$member.content_size -le 0 -or
                    [int64]$member.stored_size -le 0 -or
                    [string]::IsNullOrWhiteSpace([string]$member.source_ref) -or
                    ([int64]$member.source_size -eq 0 -and
                        [string]$member.source_sha256 -cne $emptySha256)
                ) {
                    throw "W4 harness PYZ physical member binding is invalid: $memberName"
                }
                $pyzMemberKeys[$memberKey] = $true
                $pyzMemberNames.Add($memberName)
                $pyzStoredSize += [int64]$member.stored_size
                foreach ($componentId in $memberComponentIds) {
                    if (@($entry.component_ids) -cnotcontains [string]$componentId) {
                        throw "W4 harness PYZ member component is absent from its aggregate entry: $componentId"
                    }
                    [void]$pyzComponentIds.Add([string]$componentId)
                }
            }
            Assert-Utf8SortedUniqueStrings -Values @($pyzMemberNames) `
                -Label 'W4 harness PYZ member names' -RejectCaseCollisions
            if ((@($entry.component_ids) | ConvertTo-Json -Compress) -cne
                    (@(Get-Utf8SortedStrings -Values @($pyzComponentIds)) |
                        ConvertTo-Json -Compress) -or
                (17 + $pyzStoredSize + [int64]$entry.pyz_toc_size) -ne
                    [int64]$entry.uncompressed_size) {
                throw 'W4 harness PYZ member component union/size binding is invalid'
            }
        }
    }
    $expectedKindCounts = [ordered]@{
        BINARY = 47; DATA = 1; EXTENSION = 8; OPTION = 1
        PYMODULE = 5; PYSOURCE = 3; PYZ = 1
    }
    foreach ($kindName in $expectedKindCounts.Keys) {
        if ([int]$kindCounts[$kindName] -cne [int]$expectedKindCounts[$kindName]) {
            throw "W4 harness embedded archive $kindName count is invalid"
        }
    }
    if ($kindCounts.Count -cne $expectedKindCounts.Count) {
        throw 'W4 harness embedded archive contains an unexpected entry kind'
    }
    [void]$archiveComponentIds.Add('build-runtime:pyinstaller-bootloader')
    $expectedArchiveComponents = @(
        Get-Utf8SortedStrings -Values @($archiveComponentIds)
    )
    if ((@($archive.component_ids) | ConvertTo-Json -Compress) -cne
        ($expectedArchiveComponents | ConvertTo-Json -Compress)) {
        throw 'W4 harness embedded archive component IDs are inconsistent'
    }

    $archiveMaterial = [ordered]@{
        bootloader_input = $archive.bootloader_input
        bootloader_prefix_sha256 = [string]$archive.bootloader_prefix_sha256
        bootloader_prefix_size = [int64]$archive.bootloader_prefix_size
        component_ids = @($archive.component_ids)
        entries = @($archive.entries)
        entry_count = [int64]$archive.entry_count
        executable_artifact_path = [string]$archive.executable_artifact_path
        executable_sha256 = [string]$archive.executable_sha256
        executable_size = [int64]$archive.executable_size
        pkg_sha256 = [string]$archive.pkg_sha256
        pkg_size = [int64]$archive.pkg_size
        python_library = [string]$archive.python_library
        python_version = [int64]$archive.python_version
    }
    if ([string]$archive.portable_graph_sha256 -cne
        (Get-CanonicalJsonSha256 -Value $archiveMaterial) -or
        [string]$bindings.embedded_archives_sha256 -cne
            (Get-CanonicalJsonSha256 -Value ([object[]]$archives))) {
        throw 'W4 harness embedded archive canonical graph hashes are invalid'
    }

    Assert-ExactJsonFields -Object $Inventory.payload -Fields @(
        'file_count', 'files', 'path_base', 'tree_sha256'
    ) -Label 'W4 harness release inventory payload'
    $payloadFiles = @($Inventory.payload.files)
    if ([int64]$Inventory.payload.file_count -cne 1 -or $payloadFiles.Count -cne 1 -or
        [string]$Inventory.payload.path_base -cne '.' -or
        [string]$Inventory.payload.tree_sha256 -cne [string]$bindings.payload_tree_sha256) {
        throw 'W4 harness inventory payload contract is invalid'
    }
    $payloadExecutable = $payloadFiles[0]
    Assert-ExactJsonFields -Object $payloadExecutable -Fields @(
        'artifact_path', 'component_ids', 'embedded_archive_graph_sha256',
        'embedded_component_ids', 'embedded_entry_count', 'embedded_pkg_sha256',
        'embedded_pkg_size', 'kind', 'path', 'sha256', 'size'
    ) -Label 'W4 harness inventory executable payload row'
    if ([string]$payloadExecutable.artifact_path -cne 'pkv-loopback-provider.exe' -or
        [string]$payloadExecutable.path -cne 'pkv-loopback-provider.exe' -or
        [string]$payloadExecutable.kind -cne 'PYINSTALLER_BOOTLOADER_EXECUTABLE' -or
        [string]$payloadExecutable.sha256 -cne $runtimeSha -or
        [int64]$payloadExecutable.size -cne [int64]$runtime.Length -or
        [string]$payloadExecutable.embedded_archive_graph_sha256 -cne
            [string]$archive.portable_graph_sha256 -or
        [int64]$payloadExecutable.embedded_entry_count -cne 66 -or
        [string]$payloadExecutable.embedded_pkg_sha256 -cne [string]$archive.pkg_sha256 -or
        [int64]$payloadExecutable.embedded_pkg_size -cne [int64]$archive.pkg_size -or
        (@($payloadExecutable.component_ids) | ConvertTo-Json -Compress) -cne
            (@($archive.component_ids) | ConvertTo-Json -Compress) -or
        (@($payloadExecutable.embedded_component_ids) | ConvertTo-Json -Compress) -cne
            (@($archive.component_ids) | ConvertTo-Json -Compress)) {
        throw 'W4 harness executable payload/archive binding is invalid'
    }

    $nativePayloadComponentIds = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::Ordinal
    )
    foreach ($entry in @($archive.entries)) {
        $entryKind = [string]$entry.kind
        $entryName = [string]$entry.name
        if ($entryKind -in @('BINARY', 'EXECUTABLE', 'EXTENSION') -or
            $entryName -match '(?i)\.(dll|dylib|exe|pyd|so)$') {
            foreach ($componentId in @($entry.component_ids)) {
                [void]$nativePayloadComponentIds.Add([string]$componentId)
            }
        }
    }
    [void]$nativePayloadComponentIds.Add('build-runtime:pyinstaller-bootloader')

    $expectedPayloadByComponent = @{
        'build-runtime:pyinstaller-bootloader' = @('pkv-loopback-provider.exe')
    }
    $expectedEmbeddedByComponent = @{}
    $expectedEmbeddedByComponent['build-runtime:pyinstaller-bootloader'] = @(
        'pkv-loopback-provider.exe!/<bootloader-prefix>'
    )
    foreach ($entry in @($archive.entries)) {
        $entryBase = "pkv-loopback-provider.exe!/$($entry.name)"
        if ([string]$entry.kind -eq 'PYZ') {
            foreach ($member in @($entry.pyz_members)) {
                $memberPath = "$entryBase#/$($member.name)"
                foreach ($componentId in @($member.component_ids)) {
                    $key = [string]$componentId
                    if (-not $expectedEmbeddedByComponent.ContainsKey($key)) {
                        $expectedEmbeddedByComponent[$key] = @()
                    }
                    $expectedEmbeddedByComponent[$key] += $memberPath
                }
            }
        } else {
            foreach ($componentId in @($entry.component_ids)) {
                $key = [string]$componentId
                if (-not $expectedEmbeddedByComponent.ContainsKey($key)) {
                    $expectedEmbeddedByComponent[$key] = @()
                }
                $expectedEmbeddedByComponent[$key] += $entryBase
            }
        }
    }

    $componentById = @{}
    $componentOrder = @($Inventory.components | ForEach-Object { [string]$_.id })
    Assert-Utf8SortedUniqueStrings -Values $componentOrder `
        -Label 'W4 harness inventory components'
    foreach ($component in @($Inventory.components)) {
        $componentId = [string]$component.id
        $componentIdentityStatus = [string]$component.identity_status
        $componentPropertyNames = @($component.PSObject.Properties.Name)
        $componentFields = @(
            'classification_ids', 'embedded_paths', 'id', 'identity_status', 'name',
            'payload_paths', 'source_paths', 'type'
        )
        if ($componentIdentityStatus -ceq 'complete') {
            $componentFields += 'contains_native_payload'
        }
        if ($componentPropertyNames -ccontains 'version') {
            $componentFields += 'version'
        }
        if ($componentId.StartsWith('conda-package:', [System.StringComparison]::Ordinal)) {
            $componentFields += @(
                'build', 'build_number', 'channel', 'declared_license', 'package_sha256',
                'package_url', 'record_file', 'record_sha256', 'record_size', 'subdir'
            )
        }
        Assert-ExactJsonFields -Object $component -Fields $componentFields `
            -Label "W4 harness inventory component $componentId"
        $expectedContainsNative = $nativePayloadComponentIds.Contains($componentId)
        if ([string]::IsNullOrWhiteSpace($componentId) -or
            $componentById.ContainsKey($componentId) -or
            $componentIdentityStatus -notin @('classification-only', 'complete') -or
            ($componentIdentityStatus -ceq 'complete' -and
                $component.contains_native_payload -isnot [bool]) -or
            ($componentIdentityStatus -ceq 'complete' -and
                [bool]$component.contains_native_payload -cne $expectedContainsNative) -or
            ($componentIdentityStatus -ceq 'classification-only' -and
                $componentPropertyNames -ccontains 'contains_native_payload') -or
            ($componentIdentityStatus -ceq 'complete' -and
                $componentId -cne 'application:project' -and
                ($componentPropertyNames -cnotcontains 'version' -or
                    [string]::IsNullOrWhiteSpace([string]$component.version))) -or
            (@($component.payload_paths).Count -eq 0 -and
                @($component.embedded_paths).Count -eq 0)) {
            throw "W4 harness inventory component is invalid: $componentId"
        }
        Assert-Utf8SortedUniqueStrings -Values @($component.classification_ids) `
            -Label "W4 harness inventory component classifications $componentId"
        Assert-Utf8SortedUniqueStrings -Values @($component.payload_paths) `
            -Label "W4 harness inventory component payload paths $componentId" `
            -RejectCaseCollisions
        Assert-Utf8SortedUniqueStrings -Values @($component.embedded_paths) `
            -Label "W4 harness inventory component embedded paths $componentId" `
            -RejectCaseCollisions
        Assert-Utf8SortedUniqueStrings -Values @($component.source_paths) `
            -Label "W4 harness inventory component source paths $componentId" `
            -RejectCaseCollisions
        $expectedPayloadPaths = if ($expectedPayloadByComponent.ContainsKey($componentId)) {
            @(Get-Utf8SortedStrings -Values @($expectedPayloadByComponent[$componentId]) -Unique)
        } else { @() }
        $expectedEmbeddedPaths = if ($expectedEmbeddedByComponent.ContainsKey($componentId)) {
            @(Get-Utf8SortedStrings -Values @($expectedEmbeddedByComponent[$componentId]) -Unique)
        } else { @() }
        if ((@($component.payload_paths) | ConvertTo-Json -Compress) -cne
                ($expectedPayloadPaths | ConvertTo-Json -Compress) -or
            (@($component.embedded_paths) | ConvertTo-Json -Compress) -cne
                ($expectedEmbeddedPaths | ConvertTo-Json -Compress)) {
            throw "W4 harness inventory component path coverage is not exact: $componentId"
        }
        $componentById[$componentId] = $component
    }
    $sourceSummary = Assert-InventorySourceGraph -Inventory $Inventory `
        -ComponentById $componentById -Label 'W4 harness inventory Analysis'
    foreach ($componentId in $componentById.Keys) {
        $expectedSourcePaths = if ($sourceSummary.SourceByComponent.ContainsKey($componentId)) {
            @(
                Get-Utf8SortedStrings `
                    -Values @($sourceSummary.SourceByComponent[$componentId]) -Unique
            )
        } else { @() }
        if ((@($componentById[$componentId].source_paths) | ConvertTo-Json -Compress) -cne
            ($expectedSourcePaths | ConvertTo-Json -Compress)) {
            throw "W4 harness inventory component source coverage is not exact: $componentId"
        }
    }
    $declaredUnownedSourcePaths = @(
        $coverage.unowned_source_paths | ForEach-Object { [string]$_ }
    )
    Assert-Utf8SortedUniqueStrings -Values $declaredUnownedSourcePaths `
        -Label 'W4 harness inventory unowned source paths' -RejectCaseCollisions
    if ([int64]$coverage.unowned_source_path_count -ne
            $sourceSummary.UnownedSourcePaths.Count -or
        ($declaredUnownedSourcePaths | ConvertTo-Json -Compress) -cne
            (@($sourceSummary.UnownedSourcePaths) | ConvertTo-Json -Compress)) {
        throw 'W4 harness inventory unowned source coverage is not exact'
    }
    foreach ($entry in @($archive.entries)) {
        $entryBase = "pkv-loopback-provider.exe!/$($entry.name)"
        if ([string]$entry.kind -eq 'PYZ') {
            foreach ($member in @($entry.pyz_members)) {
                $memberPath = "$entryBase#/$($member.name)"
                foreach ($componentId in @($member.component_ids)) {
                    if (-not $componentById.ContainsKey([string]$componentId) -or
                        @($componentById[[string]$componentId].embedded_paths) -cnotcontains
                            $memberPath) {
                        throw "W4 harness PYZ component coverage is missing: $memberPath"
                    }
                }
            }
        } else {
            foreach ($componentId in @($entry.component_ids)) {
                if (-not $componentById.ContainsKey([string]$componentId) -or
                    @($componentById[[string]$componentId].embedded_paths) -cnotcontains
                        $entryBase) {
                    throw "W4 harness embedded component coverage is missing: $entryBase"
                }
            }
        }
    }
    if (-not $componentById.ContainsKey('build-runtime:pyinstaller-bootloader') -or
        @($componentById['build-runtime:pyinstaller-bootloader'].embedded_paths) -cnotcontains
            'pkv-loopback-provider.exe!/<bootloader-prefix>') {
        throw 'W4 harness bootloader-prefix component coverage is missing'
    }

    $portableBindingMaterial = [ordered]@{
        analysis_graph_sha256 = [string]$bindings.analysis_graph_sha256
        artifact_path_base = [string]$bindings.artifact_path_base
        conda_native_registry_sha256 = [string]$bindings.conda_native_registry_sha256
        embedded_archives_sha256 = [string]$bindings.embedded_archives_sha256
        payload_tree_sha256 = [string]$bindings.payload_tree_sha256
    }
    $artifactBindingMaterial = [ordered]@{
        authority = $authority
        inventory_closure_sha256 = [string]$bindings.closure_sha256
    }
    if ([string]$bindings.analysis_graph_sha256 -cne
            [string]$Inventory.analysis.portable_graph_sha256 -or
        [string]$bindings.closure_sha256 -cne
            (Get-CanonicalJsonSha256 -Value $portableBindingMaterial) -or
        [string]$bindings.artifact_closure_sha256 -cne
            (Get-CanonicalJsonSha256 -Value $artifactBindingMaterial) -or
        [string]$bindings.closure_sha256 -cne
            [string]$Provenance.release_inventory_closure_sha256 -or
        (Get-LocalFileHash -LiteralPath $InventoryPath -Algorithm SHA256).Hash.ToLowerInvariant() -cne
            [string]$Provenance.release_inventory_sha256) {
        throw 'W4 harness release inventory canonical closure binding is invalid'
    }
    return [pscustomobject]@{
        Archive = $archive
        Bindings = $bindings
        ComponentById = $componentById
        Components = @($Inventory.components)
    }
}

function Expand-SafeSingleRootZip {
    param(
        [Parameter(Mandatory = $true)][string]$ZipPath,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$ExpectedRootName
    )

    Add-Type -AssemblyName System.IO.Compression -ErrorAction Stop
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop
    [void][System.IO.Directory]::CreateDirectory($Destination)
    if (@(Get-ChildItem -LiteralPath $Destination -Force).Count -cne 0) {
        throw "Harness extraction destination must be empty: $Destination"
    }
    $archive = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    $seen = @{}
    try {
        foreach ($entry in $archive.Entries) {
            $name = ([string]$entry.FullName).Replace('\', '/')
            if ([string]::IsNullOrWhiteSpace($name) -or $name.StartsWith('/') -or
                $name -match '^[A-Za-z]:' -or $name.IndexOf([char]0) -ge 0) {
                throw "Harness ZIP contains an invalid absolute/empty entry: $name"
            }
            $segments = @($name.Split('/') | Where-Object { $_ -cne '' })
            if ($segments.Count -eq 0 -or $segments[0] -cne $ExpectedRootName -or
                @($segments | Where-Object { $_ -eq '.' -or $_ -eq '..' }).Count -cne 0) {
                throw "Harness ZIP violates the single-root/no-traversal contract: $name"
            }
            $key = $name.TrimEnd('/').ToLowerInvariant()
            if ($seen.ContainsKey($key)) {
                throw "Harness ZIP contains a duplicate case-insensitive entry: $name"
            }
            $seen[$key] = $true
            $target = [System.IO.Path]::GetFullPath(
                (Join-Path $Destination ($name.Replace('/', '\')))
            )
            if (-not (Test-PathContainedBy -Candidate $target -Root $Destination)) {
                throw "Harness ZIP entry escaped extraction root: $name"
            }
            if ($name.EndsWith('/')) {
                [void][System.IO.Directory]::CreateDirectory($target)
                continue
            }
            [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($target))
            $input = $entry.Open()
            $output = [System.IO.FileStream]::new(
                $target,
                [System.IO.FileMode]::CreateNew,
                [System.IO.FileAccess]::Write,
                [System.IO.FileShare]::None
            )
            try {
                $input.CopyTo($output)
            } finally {
                $output.Dispose()
                $input.Dispose()
            }
        }
    } finally {
        $archive.Dispose()
    }
    $children = @(Get-ChildItem -LiteralPath $Destination -Force)
    if ($children.Count -cne 1 -or -not $children[0].PSIsContainer -or
        $children[0].Name -cne $ExpectedRootName) {
        throw 'Harness ZIP did not extract to exactly one canonical root directory'
    }
    return $children[0].FullName
}

function Get-ProbeCommand {
    param(
        [Parameter(Mandatory = $true)][string]$ResolvedEntryPoint,
        [string[]]$Arguments = @()
    )

    $extension = [System.IO.Path]::GetExtension($ResolvedEntryPoint).ToLowerInvariant()
    if ($extension -eq '.ps1') {
        $powerShellHost = (Get-Process -Id $PID -ErrorAction Stop).Path
        $hostArguments = @(
            '-NoLogo',
            '-NoProfile',
            '-NonInteractive',
            '-ExecutionPolicy',
            'Bypass',
            '-File',
            $ResolvedEntryPoint
        ) + $Arguments
        return [pscustomobject]@{
            FileName = $powerShellHost
            Arguments = [string[]]$hostArguments
        }
    }
    if ($extension -notin @('.exe', '.com')) {
        throw "Contract probe entrypoint must be .exe, .com, or synthetic .ps1: $ResolvedEntryPoint"
    }
    return [pscustomobject]@{
        FileName = $ResolvedEntryPoint
        Arguments = [string[]]$Arguments
    }
}

function Add-ChildEnvironmentValue {
    param(
        [Parameter(Mandatory = $true)][System.Diagnostics.ProcessStartInfo]$ProcessInfo,
        [Parameter(Mandatory = $true)][string]$Name,
        [AllowEmptyString()][string]$Value,
        [Parameter(Mandatory = $true)][string]$RepositoryRoot
    )

    if (Test-TextContainsPath -Text $Value -Path $RepositoryRoot) {
        throw "Child environment value for $Name contains the repository path"
    }
    $ProcessInfo.EnvironmentVariables[$Name] = $Value
}

function Stop-ProbeProcessTree {
    param([Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process)

    if ($Process.HasExited) {
        return
    }
    try {
        # .NET 5+ overload: Kill(entireProcessTree: true).
        $Process.Kill($true)
        return
    } catch {
        # Windows PowerShell 5.1 runs on .NET Framework, which has no Kill(bool)
        # overload.  taskkill /T is the equivalent fail-closed fallback.
        $systemRoot = [Environment]::GetEnvironmentVariable('SystemRoot')
        $taskKill = Join-Path $systemRoot 'System32\taskkill.exe'
        & $taskKill /PID $Process.Id /T /F *> $null
        if (-not $Process.WaitForExit(5000)) {
            $Process.Kill()
        }
    }
}

function Assert-FullMatrixPostcondition {
    param(
        [Parameter(Mandatory = $true)][string]$EvidenceRoot,
        [Parameter(Mandatory = $true)][string]$ExecutionId,
        [Parameter(Mandatory = $true)][string]$ArtifactId,
        [Parameter(Mandatory = $true)][string]$ArtifactSha256,
        [Parameter(Mandatory = $true)][string]$ControllerSha256,
        [Parameter(Mandatory = $true)][string]$FixtureSha256,
        [Parameter(Mandatory = $true)][string]$HarnessRuntimeSha256,
        [Parameter(Mandatory = $true)][string]$HarnessTreeSha256,
        [Parameter(Mandatory = $true)][string]$CandidateTreeSha256,
        [Parameter(Mandatory = $true)][string]$ComplianceTreeSha256,
        [Parameter(Mandatory = $true)]$ArtifactProvenance,
        [Parameter(Mandatory = $true)][string]$ControllerRoot,
        [Parameter(Mandatory = $true)][string]$FixtureRoot,
        [Parameter(Mandatory = $true)][string]$HarnessRoot,
        [Parameter(Mandatory = $true)][string]$CandidateRoot,
        [Parameter(Mandatory = $true)][string]$ComplianceRoot,
        [Parameter(Mandatory = $true)][string]$WorkspaceRoot,
        [Parameter(Mandatory = $true)][string[]]$ExpectedScenarioIds,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$ControllerStdout,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$ControllerStderr
    )

    $runRoot = Join-Path (Join-Path $EvidenceRoot 'runs') $ExecutionId
    if (-not (Test-Path -LiteralPath $runRoot -PathType Container)) {
        throw "W4 controller returned success without a run evidence root: $runRoot"
    }
    Assert-SafeTree -Root $runRoot -Label 'W4 run evidence'
    $summaryPath = Join-Path $runRoot 'w4-run-summary.json'
    $registryPath = Join-Path $runRoot 'w4-evidence-registry.json'
    $manifestPath = Join-Path $runRoot 'run-evidence-manifest.json'
    $summary = Read-StrictJsonFile -Path $summaryPath -Label 'W4 run summary'
    $registry = Read-StrictJsonFile -Path $registryPath -Label 'W4 evidence registry'
    $runManifest = Read-StrictJsonFile -Path $manifestPath -Label 'W4 run evidence manifest'

    Assert-ExactJsonFields -Object $summary -Fields @(
        'schema_version', 'runner_version', 'execution_id', 'artifact_id',
        'artifact_sha256', 'source_revision', 'build_fingerprint',
        'artifact_kind', 'artifact_status', 'compliance_manifest_sha256',
        'release_eligible', 'release_blockers',
        'release_inventory_artifact_closure_sha256',
        'release_inventory_closure_sha256', 'release_inventory_sha256',
        'controller_sha256', 'fixture_sha256', 'scenarios_total',
        'matrix_rows_total', 'artifact_verified', 'artifact_failed',
        'artifact_pending', 'functional_verified', 'scenarios', 'decision'
    ) -Label 'W4 run summary'
    Assert-ExactJsonBoolean -Value $summary.release_eligible `
        -Expected ([bool]$ArtifactProvenance.release_eligible) `
        -Label 'W4 run summary release_eligible'
    Assert-ExactJsonBoolean -Value $summary.functional_verified -Expected $true `
        -Label 'W4 run summary functional_verified'
    $expectedDecision = if ([bool]$ArtifactProvenance.release_eligible -and
        @($ArtifactProvenance.release_blockers).Count -eq 0) { 'release' } else { 'hold' }
    if ([string]$summary.schema_version -cne 'pkv.m13.w4-run-summary.v1' -or
        [string]$summary.runner_version -cne 'pkv.m13.artifact-runner.v2' -or
        [string]$summary.execution_id -cne $ExecutionId -or
        [string]$summary.artifact_id -cne $ArtifactId -or
        [string]$summary.artifact_sha256 -cne $ArtifactSha256 -or
        [string]$summary.controller_sha256 -cne $ControllerSha256 -or
        [string]$summary.fixture_sha256 -cne $FixtureSha256 -or
        [string]$summary.source_revision -cne [string]$ArtifactProvenance.source_revision -or
        [string]$summary.build_fingerprint -cne [string]$ArtifactProvenance.build_fingerprint -or
        [string]$summary.artifact_kind -cne [string]$ArtifactProvenance.artifact_kind -or
        [string]$summary.artifact_status -cne [string]$ArtifactProvenance.artifact_status -or
        [string]$summary.compliance_manifest_sha256 -cne
            [string]$ArtifactProvenance.compliance_manifest_sha256 -or
        [string]$summary.release_inventory_artifact_closure_sha256 -cne
            [string]$ArtifactProvenance.release_inventory_artifact_closure_sha256 -or
        [string]$summary.release_inventory_closure_sha256 -cne
            [string]$ArtifactProvenance.release_inventory_closure_sha256 -or
        [string]$summary.release_inventory_sha256 -cne
            [string]$ArtifactProvenance.release_inventory_sha256 -or
        [bool]$summary.release_eligible -cne [bool]$ArtifactProvenance.release_eligible -or
        (@($summary.release_blockers) | ConvertTo-Json -Compress) -cne
            (@($ArtifactProvenance.release_blockers) | ConvertTo-Json -Compress) -or
        [int]$summary.scenarios_total -cne 9 -or
        [int]$summary.matrix_rows_total -cne 10 -or
        [int]$summary.artifact_verified -cne 9 -or
        [int]$summary.artifact_failed -cne 0 -or
        [int]$summary.artifact_pending -cne 0 -or
        -not [bool]$summary.functional_verified -or
        [string]$summary.decision -cne $expectedDecision) {
        throw 'W4 success summary did not satisfy the exact functional/eligibility/decision/9-0-0/hash postcondition'
    }
    $scenarioRows = @($summary.scenarios)
    if ($scenarioRows.Count -cne 9) {
        throw 'W4 success summary does not contain exactly 9 scenario rows'
    }
    $summaryIds = [System.Collections.Generic.List[string]]::new()
    foreach ($row in $scenarioRows) {
        Assert-ExactJsonFields -Object $row -Fields @(
            'scenario_id', 'state', 'oracle_result', 'duration_ms', 'error'
        ) -Label 'W4 summary scenario row'
        if ([string]$row.state -cne 'artifact_verified' -or
            [string]$row.oracle_result -cne 'passed' -or
            $null -cne $row.error -or [int64]$row.duration_ms -lt 0) {
            throw "W4 summary contains a non-passing scenario: $($row.scenario_id)"
        }
        $summaryIds.Add([string]$row.scenario_id)
    }
    if ((@($summaryIds) | ConvertTo-Json -Compress) -cne
        (@($ExpectedScenarioIds) | ConvertTo-Json -Compress)) {
        throw 'W4 summary scenario IDs/order differ from the frozen driver contract'
    }

    $stdoutSummary = $null
    if (-not [string]::IsNullOrWhiteSpace($ControllerStderr)) {
        throw 'W4 controller success must have empty stderr'
    }
    try {
        $stdoutSummary = $ControllerStdout.Trim() | ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw 'W4 controller success stdout must be exactly one JSON summary'
    }
    if (($stdoutSummary | ConvertTo-Json -Depth 30 -Compress) -cne
        ($summary | ConvertTo-Json -Depth 30 -Compress)) {
        throw 'W4 controller stdout summary differs from the persisted release summary'
    }

    Assert-ExactJsonFields -Object $registry `
        -Fields @('schema_version', 'execution_id', 'records') -Label 'W4 evidence registry'
    $records = @($registry.records)
    if ([string]$registry.schema_version -cne 'pkv.m13.w4-run-evidence.v1' -or
        [string]$registry.execution_id -cne $ExecutionId -or $records.Count -cne 9) {
        throw 'W4 evidence registry identity/count is invalid'
    }
    $recordIds = [System.Collections.Generic.List[string]]::new()
    $isolationPath = Join-Path $runRoot 'source-isolation.json'
    if (-not (Test-Path -LiteralPath $isolationPath -PathType Leaf)) {
        throw 'W4 source-isolation proof is missing'
    }
    $isolationSha = (Get-LocalFileHash -LiteralPath $isolationPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($isolationSha -cnotmatch '^[0-9a-f]{64}$') {
        throw 'W4 source-isolation proof hash is invalid'
    }
    $isolation = Read-StrictJsonFile -Path $isolationPath -Label 'W4 source-isolation proof'
    Assert-ExactJsonFields -Object $isolation -Fields @(
        'schema_version', 'execution_id', 'controller_root', 'controller_sha256',
        'controller_files', 'controller_language', 'imports_product_source',
        'working_root', 'evidence_root', 'artifact_root', 'fixture_root',
        'harness_root', 'harness_tree_sha256', 'candidate_root',
        'candidate_tree_sha256', 'compliance_sources_root',
        'compliance_sources_tree_sha256', 'python_path_injected',
        'python_runtime_required', 'conda_runtime_required'
    ) -Label 'W4 source-isolation proof'
    foreach ($booleanField in @(
        'imports_product_source', 'python_path_injected',
        'python_runtime_required', 'conda_runtime_required'
    )) {
        Assert-ExactJsonBoolean -Value $isolation.$booleanField -Expected $false `
            -Label "W4 source-isolation $booleanField"
    }
    $expectedControllerRows = @(Get-TreeManifestRows -Root $ControllerRoot)
    $expectedWorkingRoot = Join-Path (Join-Path $WorkspaceRoot 'runs') $ExecutionId
    $expectedArtifactRoot = Join-Path (Join-Path $expectedWorkingRoot 'extracted') $ArtifactId
    if ([string]$isolation.schema_version -cne 'pkv.m13.w4-source-isolation.v1' -or
        [string]$isolation.execution_id -cne $ExecutionId -or
        [string]$isolation.controller_root -cne $ControllerRoot -or
        [string]$isolation.controller_sha256 -cne $ControllerSha256 -or
        (@($isolation.controller_files) | ConvertTo-Json -Depth 5 -Compress) -cne
            ($expectedControllerRows | ConvertTo-Json -Depth 5 -Compress) -or
        [string]$isolation.working_root -cne $expectedWorkingRoot -or
        [string]$isolation.evidence_root -cne $runRoot -or
        [string]$isolation.artifact_root -cne $expectedArtifactRoot -or
        [string]$isolation.fixture_root -cne $FixtureRoot -or
        [string]$isolation.harness_root -cne $HarnessRoot -or
        [string]$isolation.candidate_root -cne $CandidateRoot -or
        [string]$isolation.compliance_sources_root -cne $ComplianceRoot -or
        [string]$isolation.harness_tree_sha256 -cne $HarnessTreeSha256 -or
        [string]$isolation.candidate_tree_sha256 -cne $CandidateTreeSha256 -or
        [string]$isolation.compliance_sources_tree_sha256 -cne $ComplianceTreeSha256 -or
        [string]$isolation.controller_language -cne
            'powershell-dotnet-system-assemblies-only' -or
        [bool]$isolation.imports_product_source -or
        [bool]$isolation.python_path_injected -or
        [bool]$isolation.python_runtime_required -or
        [bool]$isolation.conda_runtime_required) {
        throw 'W4 source-isolation proof does not exactly bind immutable input trees/runtime isolation'
    }
    foreach ($record in $records) {
        Assert-ExactJsonFields -Object $record -Fields @(
            'scenario_id', 'state', 'producer_lane', 'artifact_id', 'artifact_sha256',
            'normalized_manifest_sha256', 'build_fingerprint', 'source_revision',
            'runner_version', 'execution_id', 'executed_at', 'environment_fingerprint',
            'fixture_sha256', 'harness_sha256', 'evidence_manifest_sha256',
            'source_isolation_proof_sha256', 'oracle_result', 'evidence_paths'
        ) -Label 'W4 evidence record'
        $executedAt = [string]$record.executed_at
        $parsedExecutedAt = [DateTime]::MinValue
        $executedAtIsUtc = (
            $record.executed_at -is [string] -and
            $executedAt -cmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{7}Z$' -and
            [DateTime]::TryParseExact(
                $executedAt,
                'o',
                [System.Globalization.CultureInfo]::InvariantCulture,
                [System.Globalization.DateTimeStyles]::RoundtripKind,
                [ref]$parsedExecutedAt
            ) -and
            $parsedExecutedAt.Kind -eq [DateTimeKind]::Utc
        )
        if ([string]$record.state -cne 'artifact_verified' -or
            [string]$record.oracle_result -cne 'passed' -or
            [string]$record.producer_lane -cne 'artifact-only' -or
            [string]$record.artifact_id -cne $ArtifactId -or
            [string]$record.artifact_sha256 -cne $ArtifactSha256 -or
            [string]$record.normalized_manifest_sha256 -cne
                [string]$ArtifactProvenance.payload_manifest_sha256 -or
            [string]$record.build_fingerprint -cne [string]$ArtifactProvenance.build_fingerprint -or
            [string]$record.source_revision -cne [string]$ArtifactProvenance.source_revision -or
            [string]$record.runner_version -cne 'pkv.m13.artifact-runner.v2' -or
            [string]$record.execution_id -cne $ExecutionId -or
            -not $executedAtIsUtc -or
            [string]$record.fixture_sha256 -cne $FixtureSha256 -or
            [string]$record.source_isolation_proof_sha256 -cne $isolationSha -or
            [string]$record.environment_fingerprint -cne
                (Get-LocalFileHash -LiteralPath (Join-Path $runRoot 'environment.json') `
                    -Algorithm SHA256).Hash.ToLowerInvariant() -or
            [string]$record.evidence_manifest_sha256 -cnotmatch '^[0-9a-f]{64}$') {
            throw "W4 evidence record is not a verified Artifact-only record: $($record.scenario_id)"
        }
        if ($null -ne $record.harness_sha256) {
            throw "W4 evidence harness binding is invalid: $($record.scenario_id)"
        }
        $recordIds.Add([string]$record.scenario_id)
        $paths = @($record.evidence_paths | ForEach-Object { [string]$_ })
        if ($paths.Count -lt 1 -or @($paths | Sort-Object -Unique).Count -cne $paths.Count) {
            throw "W4 evidence record has empty/duplicate evidence paths: $($record.scenario_id)"
        }
        $evidenceManifestMatches = [System.Collections.Generic.List[string]]::new()
        foreach ($relative in $paths) {
            $normalized = $relative.Replace('\', '/')
            if ($normalized.StartsWith('/') -or $normalized -match '^[A-Za-z]:' -or
                $normalized -match '(^|/)\.\.(/|$)') {
                throw "W4 evidence path is unsafe: $relative"
            }
            $evidenceFile = [System.IO.Path]::GetFullPath(
                (Join-Path $runRoot ($normalized.Replace('/', '\')))
            )
            if (-not (Test-PathContainedBy -Candidate $evidenceFile -Root $runRoot) -or
                -not (Test-Path -LiteralPath $evidenceFile -PathType Leaf)) {
                throw "W4 evidence path is missing or escaped the run root: $relative"
            }
            if ($normalized.EndsWith('/evidence-manifest.json', [System.StringComparison]::Ordinal)) {
                $evidenceManifestMatches.Add($evidenceFile)
            }
        }
        if ($evidenceManifestMatches.Count -cne 1 -or
            (Get-LocalFileHash -LiteralPath $evidenceManifestMatches[0] -Algorithm SHA256).Hash.ToLowerInvariant() -ne
                [string]$record.evidence_manifest_sha256) {
            throw "W4 evidence record does not bind exactly one evidence manifest: $($record.scenario_id)"
        }
        $scenarioEvidenceManifest = Read-StrictJsonFile `
            -Path $evidenceManifestMatches[0] -Label 'W4 scenario evidence manifest'
        Assert-ExactJsonFields -Object $scenarioEvidenceManifest -Fields @(
            'schema_version', 'scenario_id', 'entries', 'tree_sha256'
        ) -Label 'W4 scenario evidence manifest'
        $scenarioEvidenceRoot = [System.IO.Path]::GetDirectoryName($evidenceManifestMatches[0])
        $actualScenarioRows = @(Get-TreeManifestRows -Root $scenarioEvidenceRoot `
            -ExcludedRelativePaths @('evidence-manifest.json', 'evidence-record.json'))
        if ([string]$scenarioEvidenceManifest.schema_version -ne
                'pkv.m13.w4-scenario-evidence-manifest.v1' -or
            [string]$scenarioEvidenceManifest.scenario_id -cne [string]$record.scenario_id -or
            (@($scenarioEvidenceManifest.entries) | ConvertTo-Json -Depth 5 -Compress) -cne
                ($actualScenarioRows | ConvertTo-Json -Depth 5 -Compress) -or
            [string]$scenarioEvidenceManifest.tree_sha256 -ne
                (Get-StringSha256 -Value ($actualScenarioRows | ConvertTo-Json -Depth 5 -Compress))) {
            throw "W4 scenario evidence manifest does not bind its evidence tree: $($record.scenario_id)"
        }
    }
    if ((@($recordIds) | ConvertTo-Json -Compress) -cne
        (@($ExpectedScenarioIds) | ConvertTo-Json -Compress)) {
        throw 'W4 evidence registry scenario IDs/order differ from the frozen contract'
    }

    Assert-ExactJsonFields -Object $runManifest `
        -Fields @('schema_version', 'execution_id', 'entries', 'tree_sha256') `
        -Label 'W4 run evidence manifest'
    $actualRunRows = @(Get-TreeManifestRows -Root $runRoot `
        -ExcludedRelativePaths @('run-evidence-manifest.json'))
    $declaredRunRows = @($runManifest.entries)
    if ([string]$runManifest.schema_version -cne 'pkv.m13.w4-run-evidence-manifest.v1' -or
        [string]$runManifest.execution_id -cne $ExecutionId -or
        ($declaredRunRows | ConvertTo-Json -Depth 5 -Compress) -cne
            ($actualRunRows | ConvertTo-Json -Depth 5 -Compress) -or
        [string]$runManifest.tree_sha256 -ne
            (Get-StringSha256 -Value ($actualRunRows | ConvertTo-Json -Depth 5 -Compress))) {
        throw 'W4 run evidence manifest does not exactly bind the persisted run evidence tree'
    }
    return $summary
}

function Invoke-FullArtifactMatrix {
    param([Parameter(Mandatory = $true)][string]$RepositoryRoot)

    foreach ($item in @(
        @($CandidateRoot, 'Candidate root'),
        @($DistributionZip, 'Distribution ZIP'),
        @($DistributionSha256Path, 'Distribution SHA-256 sidecar'),
        @($ProvenancePath, 'Distribution provenance'),
        @($ComplianceSourcesRoot, 'Compliance sources root'),
        @($ComplianceManifestPath, 'Compliance source manifest'),
        @($ComplianceProvenancePath, 'Compliance source provenance'),
        @($DriverRoot, 'W4 driver root'),
        @($HarnessPath, 'W4 harness package root'),
        @($EvidenceRoot, 'W4 evidence root'),
        @($WorkspaceRoot, 'W4 workspace root'),
        @($HarnessWorkspaceRoot, 'W4 harness extraction workspace root')
    )) {
        Assert-SafePathChain -Path $item[0] -Label $item[1]
    }
    $resolvedCandidateRoot = Get-CanonicalExistingPath -Path $CandidateRoot -Kind Container
    $resolvedZip = Get-CanonicalExistingPath -Path $DistributionZip -Kind Leaf
    $resolvedSha = Get-CanonicalExistingPath -Path $DistributionSha256Path -Kind Leaf
    $resolvedProvenance = Get-CanonicalExistingPath -Path $ProvenancePath -Kind Leaf
    $resolvedComplianceRoot = Get-CanonicalExistingPath `
        -Path $ComplianceSourcesRoot -Kind Container
    $resolvedComplianceManifest = Get-CanonicalExistingPath `
        -Path $ComplianceManifestPath -Kind Leaf
    $resolvedComplianceProvenance = Get-CanonicalExistingPath `
        -Path $ComplianceProvenancePath -Kind Leaf
    $resolvedDriverRoot = Get-CanonicalExistingPath -Path $DriverRoot -Kind Container
    $resolvedController = Get-CanonicalExistingPath `
        -Path (Join-Path $resolvedDriverRoot 'Invoke-W4ArtifactE2E.ps1') -Kind Leaf
    $resolvedScenarioContract = Get-CanonicalExistingPath `
        -Path (Join-Path $resolvedDriverRoot 'scenarios.v2.json') -Kind Leaf
    $resolvedFixture = Get-CanonicalExistingPath `
        -Path (Join-Path $resolvedDriverRoot 'fixtures') -Kind Container
    $resolvedHarnessPackage = Get-CanonicalExistingPath -Path $HarnessPath -Kind Container
    $prospectiveEvidence = Get-CanonicalProspectivePath -Path $EvidenceRoot
    $prospectiveWorkspace = Get-CanonicalProspectivePath -Path $WorkspaceRoot
    $prospectiveHarnessWorkspace = Get-CanonicalProspectivePath -Path $HarnessWorkspaceRoot

    foreach ($item in @(
        @($resolvedCandidateRoot, 'Candidate root'),
        @($resolvedZip, 'Distribution ZIP'),
        @($resolvedSha, 'Distribution SHA-256 sidecar'),
        @($resolvedProvenance, 'Distribution provenance'),
        @($resolvedComplianceRoot, 'Compliance sources root'),
        @($resolvedComplianceManifest, 'Compliance source manifest'),
        @($resolvedComplianceProvenance, 'Compliance source provenance'),
        @($resolvedDriverRoot, 'W4 driver root'),
        @($resolvedHarnessPackage, 'W4 harness package root'),
        @($prospectiveEvidence, 'W4 evidence root'),
        @($prospectiveWorkspace, 'W4 workspace root'),
        @($prospectiveHarnessWorkspace, 'W4 harness extraction workspace root')
    )) {
        Assert-OutsideRepository -Path $item[0] -Label $item[1] -RepositoryRoot $RepositoryRoot
    }
    Assert-SafeTree -Root $resolvedDriverRoot -Label 'W4 driver bundle'
    Assert-SafeTree -Root $resolvedFixture -Label 'W4 fixture bundle'
    Assert-SafeTree -Root $resolvedHarnessPackage -Label 'W4 harness package'
    Assert-SafeTree -Root $resolvedCandidateRoot -Label 'W4 candidate package'
    Assert-SafeTree -Root $resolvedComplianceRoot -Label 'W4 compliance source bundle'
    foreach ($candidateInput in @($resolvedZip, $resolvedSha, $resolvedProvenance)) {
        if (-not (Test-PathContainedBy -Candidate $candidateInput -Root $resolvedCandidateRoot)) {
            throw 'Candidate ZIP/SHA-256/provenance inputs must be contained by CandidateRoot'
        }
    }
    foreach ($complianceInput in @($resolvedComplianceManifest, $resolvedComplianceProvenance)) {
        if (-not (Test-PathContainedBy -Candidate $complianceInput -Root $resolvedComplianceRoot)) {
            throw 'Compliance manifest/provenance inputs must be contained by ComplianceSourcesRoot'
        }
    }
    $controllerRoot = $resolvedDriverRoot
    foreach ($moduleName in @('W4.Driver.psm1', 'W4.Scenarios.psm1')) {
        $modulePath = Join-Path $controllerRoot $moduleName
        if (-not (Test-Path -LiteralPath $modulePath -PathType Leaf)) {
            throw "W4 controller bundle is incomplete: $moduleName"
        }
        Assert-SafePathChain -Path $modulePath -Label "W4 controller module $moduleName"
    }
    foreach ($pair in @(
        @($prospectiveEvidence, 'W4 evidence root', $prospectiveWorkspace, 'W4 workspace root'),
        @($prospectiveHarnessWorkspace, 'W4 harness extraction workspace root', $prospectiveEvidence, 'W4 evidence root'),
        @($prospectiveHarnessWorkspace, 'W4 harness extraction workspace root', $prospectiveWorkspace, 'W4 workspace root'),
        @($prospectiveHarnessWorkspace, 'W4 harness extraction workspace root', $resolvedDriverRoot, 'W4 driver root'),
        @($prospectiveHarnessWorkspace, 'W4 harness extraction workspace root', $resolvedFixture, 'W4 fixture root'),
        @($prospectiveHarnessWorkspace, 'W4 harness extraction workspace root', $resolvedHarnessPackage, 'W4 harness package root'),
        @($prospectiveHarnessWorkspace, 'W4 harness extraction workspace root', $resolvedZip, 'Distribution ZIP'),
        @($prospectiveHarnessWorkspace, 'W4 harness extraction workspace root', $resolvedSha, 'Distribution SHA-256 sidecar'),
        @($prospectiveHarnessWorkspace, 'W4 harness extraction workspace root', $resolvedProvenance, 'Distribution provenance'),
        @($prospectiveHarnessWorkspace, 'W4 harness extraction workspace root', $resolvedCandidateRoot, 'Candidate root'),
        @($prospectiveHarnessWorkspace, 'W4 harness extraction workspace root', $resolvedComplianceRoot, 'Compliance sources root'),
        @($resolvedFixture, 'W4 fixture root', $prospectiveEvidence, 'W4 evidence root'),
        @($resolvedFixture, 'W4 fixture root', $prospectiveWorkspace, 'W4 workspace root'),
        @($resolvedHarnessPackage, 'W4 harness package root', $prospectiveEvidence, 'W4 evidence root'),
        @($resolvedHarnessPackage, 'W4 harness package root', $prospectiveWorkspace, 'W4 workspace root'),
        @($controllerRoot, 'W4 controller root', $prospectiveEvidence, 'W4 evidence root'),
        @($controllerRoot, 'W4 controller root', $prospectiveWorkspace, 'W4 workspace root'),
        @($resolvedCandidateRoot, 'Candidate root', $prospectiveEvidence, 'W4 evidence root'),
        @($resolvedCandidateRoot, 'Candidate root', $prospectiveWorkspace, 'W4 workspace root'),
        @($resolvedCandidateRoot, 'Candidate root', $resolvedDriverRoot, 'W4 driver root'),
        @($resolvedCandidateRoot, 'Candidate root', $resolvedFixture, 'W4 fixture root'),
        @($resolvedCandidateRoot, 'Candidate root', $resolvedHarnessPackage, 'W4 harness package root'),
        @($resolvedCandidateRoot, 'Candidate root', $resolvedComplianceRoot, 'Compliance sources root'),
        @($resolvedComplianceRoot, 'Compliance sources root', $prospectiveEvidence, 'W4 evidence root'),
        @($resolvedComplianceRoot, 'Compliance sources root', $prospectiveWorkspace, 'W4 workspace root'),
        @($resolvedComplianceRoot, 'Compliance sources root', $resolvedDriverRoot, 'W4 driver root'),
        @($resolvedComplianceRoot, 'Compliance sources root', $resolvedFixture, 'W4 fixture root'),
        @($resolvedComplianceRoot, 'Compliance sources root', $resolvedHarnessPackage, 'W4 harness package root')
    )) {
        Assert-DisjointPaths -First $pair[0] -FirstLabel $pair[1] -Second $pair[2] -SecondLabel $pair[3]
    }
    foreach ($argument in @(
        $resolvedCandidateRoot, $resolvedZip, $resolvedSha, $resolvedProvenance,
        $resolvedComplianceRoot, $resolvedComplianceManifest, $resolvedComplianceProvenance,
        $resolvedController,
        $resolvedDriverRoot, $resolvedScenarioContract, $resolvedFixture, $resolvedHarnessPackage,
        $prospectiveEvidence, $prospectiveWorkspace, $prospectiveHarnessWorkspace, $RunId
    )) {
        if (Test-TextContainsPath -Text $argument -Path $RepositoryRoot) {
            throw 'Full-matrix controller arguments must not contain the repository path'
        }
    }

    $driverManifestPath = Join-Path $resolvedDriverRoot 'driver-manifest.json'
    $driverSidecarPath = Join-Path $resolvedDriverRoot 'driver-manifest.sha256'
    foreach ($path in @($driverManifestPath, $driverSidecarPath)) {
        Assert-SafePathChain -Path $path -Label 'W4 driver manifest'
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "W4 driver bundle manifest is missing: $path"
        }
    }
    try {
        $driverManifest = [System.IO.File]::ReadAllText(
            $driverManifestPath,
            [System.Text.Encoding]::UTF8
        ) | ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw 'W4 driver manifest is not valid JSON'
    }
    $driverFields = @(
        'schema_version', 'runner_version', 'distribution',
        'release_payload_membership', 'self_excluded_paths', 'files', 'tree_sha256'
    )
    $actualDriverFields = @($driverManifest.PSObject.Properties.Name | Sort-Object)
    if (($actualDriverFields | ConvertTo-Json -Compress) -ne
        (@($driverFields | Sort-Object) | ConvertTo-Json -Compress) -or
        [string]$driverManifest.schema_version -cne 'pkv.m13.w4-driver-bundle.v1' -or
        [string]$driverManifest.runner_version -cne 'pkv.m13.artifact-runner.v2' -or
        [string]$driverManifest.distribution -cne 'e2e-only' -or
        [string]$driverManifest.release_payload_membership -cne 'forbidden') {
        throw 'W4 driver manifest identity/fields are invalid'
    }
    if ((@($driverManifest.self_excluded_paths) | ConvertTo-Json -Compress) -ne
        (@('driver-manifest.json', 'driver-manifest.sha256') | ConvertTo-Json -Compress)) {
        throw 'W4 driver manifest self_excluded_paths are not exact'
    }
    $driverManifestSha = (Get-LocalFileHash -LiteralPath $driverManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $driverSidecar = [System.IO.File]::ReadAllText(
        $driverSidecarPath,
        [System.Text.Encoding]::ASCII
    ).Trim()
    if ($driverSidecar -cne "$driverManifestSha  driver-manifest.json") {
        throw 'W4 driver manifest SHA-256 sidecar is invalid'
    }
    $declaredDriverFiles = @($driverManifest.files)
    $actualDriverFiles = @(
        Get-ChildItem -LiteralPath $resolvedDriverRoot -File -Recurse -Force |
            Where-Object {
                $_.FullName -cne $driverManifestPath -and $_.FullName -cne $driverSidecarPath
            } |
            Sort-Object FullName
    )
    if ($declaredDriverFiles.Count -cne $actualDriverFiles.Count) {
        throw 'W4 driver manifest file count does not match bundle contents'
    }
    $treeText = [System.Text.StringBuilder]::new()
    for ($index = 0; $index -lt $declaredDriverFiles.Count; $index += 1) {
        $row = $declaredDriverFiles[$index]
        $relative = $actualDriverFiles[$index].FullName.Substring(
            $resolvedDriverRoot.TrimEnd('\').Length
        ).TrimStart('\').Replace('\', '/')
        $rowFields = @($row.PSObject.Properties.Name | Sort-Object)
        if (($rowFields | ConvertTo-Json -Compress) -ne
            (@('path', 'role', 'sha256', 'size') | Sort-Object | ConvertTo-Json -Compress) -or
            [string]$row.path -cne $relative -or
            [int64]$row.size -cne [int64]$actualDriverFiles[$index].Length -or
            [string]$row.sha256 -cne (Get-LocalFileHash -LiteralPath $actualDriverFiles[$index].FullName -Algorithm SHA256).Hash.ToLowerInvariant()) {
            throw "W4 driver bundle hash/path row mismatch: $relative"
        }
        [void]$treeText.Append([string]$row.path)
        [void]$treeText.Append([char]0)
        [void]$treeText.Append([string][int64]$row.size)
        [void]$treeText.Append([char]0)
        [void]$treeText.Append([string]$row.sha256)
        [void]$treeText.Append("`n")
    }
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        $treeBytes = [System.Text.UTF8Encoding]::new($false).GetBytes($treeText.ToString())
        $treeSha = ([BitConverter]::ToString($algorithm.ComputeHash($treeBytes))).Replace('-', '').ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
    }
    if ($treeSha -cne [string]$driverManifest.tree_sha256) {
        throw 'W4 driver bundle tree_sha256 recomputation failed'
    }

    $artifactSha = (Get-LocalFileHash -LiteralPath $resolvedZip -Algorithm SHA256).Hash.ToLowerInvariant()
    $artifactId = [System.IO.Path]::GetFileNameWithoutExtension($resolvedZip)
    $artifactZipName = "$artifactId.zip"
    $expectedCandidateNames = @(
        $artifactZipName,
        "$artifactZipName.sha256",
        "$artifactId.provenance.json"
    ) | Sort-Object
    $candidateItems = @(Get-ChildItem -LiteralPath $resolvedCandidateRoot -Force)
    $actualCandidateNames = @($candidateItems | ForEach-Object { $_.Name } | Sort-Object)
    if ($artifactId -cne 'PersonalKnowledgeVault-0.8.1-windows-x86_64' -or
        @($candidateItems | Where-Object { $_.PSIsContainer }).Count -cne 0 -or
        ($actualCandidateNames | ConvertTo-Json -Compress) -cne
            ($expectedCandidateNames | ConvertTo-Json -Compress) -or
        $resolvedZip -cne (Join-Path $resolvedCandidateRoot $artifactZipName) -or
        $resolvedSha -cne (Join-Path $resolvedCandidateRoot "$artifactZipName.sha256") -or
        $resolvedProvenance -cne
            (Join-Path $resolvedCandidateRoot "$artifactId.provenance.json")) {
        throw 'CandidateRoot must contain exactly the canonical 0.8.1 candidate files'
    }
    $candidateSidecarText = [System.IO.File]::ReadAllText($resolvedSha, [System.Text.Encoding]::ASCII)
    if ($candidateSidecarText -cne "$artifactSha  $artifactZipName`n") {
        throw 'Candidate ZIP SHA-256 sidecar is not exact or does not match the ZIP'
    }

    $harnessId = 'PKV-W4-LoopbackHarness-1.0.0-windows-x86_64'
    $harnessZipName = "$harnessId.zip"
    $harnessZip = Join-Path $resolvedHarnessPackage $harnessZipName
    $harnessSidecar = Join-Path $resolvedHarnessPackage "$harnessZipName.sha256"
    $harnessProvenancePath = Join-Path $resolvedHarnessPackage "$harnessId.provenance.json"
    $expectedHarnessPackageNames = @(
        $harnessZipName,
        "$harnessZipName.sha256",
        "$harnessId.provenance.json"
    ) | Sort-Object
    $actualHarnessPackageNames = @(
        Get-ChildItem -LiteralPath $resolvedHarnessPackage -Force |
            ForEach-Object { $_.Name } |
            Sort-Object
    )
    if (($actualHarnessPackageNames | ConvertTo-Json -Compress) -ne
        ($expectedHarnessPackageNames | ConvertTo-Json -Compress)) {
        throw 'W4 harness package root must contain exactly ZIP/SHA-256/provenance sidecars'
    }
    foreach ($path in @($harnessZip, $harnessSidecar, $harnessProvenancePath)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "W4 harness package file is missing: $path"
        }
        Assert-SafePathChain -Path $path -Label 'W4 harness package file'
    }
    $harnessZipSha = (Get-LocalFileHash -LiteralPath $harnessZip -Algorithm SHA256).Hash.ToLowerInvariant()
    $harnessSidecarText = [System.IO.File]::ReadAllText(
        $harnessSidecar,
        [System.Text.Encoding]::ASCII
    )
    if ($harnessSidecarText -cne "$harnessZipSha  $harnessZipName`n") {
        throw 'W4 harness ZIP SHA-256 sidecar is not exact or does not match the ZIP'
    }
    $harnessProvenance = Read-StrictJsonFile `
        -Path $harnessProvenancePath -Label 'W4 harness provenance'
    Assert-ExactJsonFields -Object $harnessProvenance -Fields @(
        'schema_version', 'artifact_file', 'artifact_sha256', 'artifact_size',
        'artifact_status', 'build_fingerprint', 'contract_sha256', 'harness_version',
        'artifact_kind',
        'legal_manifest_path', 'legal_manifest_sha256', 'manifest_path',
        'manifest_sha256', 'release_blocker_authority',
        'release_blocker_authority_sha256', 'release_blockers', 'release_eligible',
        'release_inventory_closure_sha256', 'release_inventory_path',
        'release_inventory_sha256', 'release_payload_membership', 'runtime_path',
        'runtime_sha256', 'sbom_path', 'sbom_sha256', 'source_revision',
        'toolchain_lock_sha256'
    ) -Label 'W4 harness provenance'
    Assert-ExactJsonBoolean -Value $harnessProvenance.release_eligible `
        -Expected $false -Label 'W4 harness provenance release_eligible'
    $expectedHarnessBlockers = @('harness-native-license-and-provenance')
    $harnessAuthoritySha = Assert-ReleaseBlockerAuthority `
        -Rows @($harnessProvenance.release_blocker_authority) `
        -DeclaredSha256 ([string]$harnessProvenance.release_blocker_authority_sha256) `
        -ExpectedIds $expectedHarnessBlockers -Label 'W4 harness blocker authority'
    if ([string]$harnessProvenance.schema_version -cne 'pkv.w3-harness-provenance.v1' -or
        [string]$harnessProvenance.artifact_file -cne $harnessZipName -or
        [string]$harnessProvenance.artifact_sha256 -cne $harnessZipSha -or
        [int64]$harnessProvenance.artifact_size -cne [int64](Get-Item -LiteralPath $harnessZip).Length -or
        [string]$harnessProvenance.artifact_kind -cne 'e2e_test_harness' -or
        [string]$harnessProvenance.artifact_status -cne
            'internal-verification-only-on-native-compliance-hold' -or
        [string]$harnessProvenance.harness_version -cne '1.0.0' -or
        [string]$harnessProvenance.legal_manifest_path -cne "$harnessId/legal-manifest.json" -or
        [string]$harnessProvenance.manifest_path -cne "$harnessId/manifest.json" -or
        [string]$harnessProvenance.release_inventory_path -cne
            "$harnessId/release-inventory.json" -or
        [string]$harnessProvenance.release_payload_membership -cne 'forbidden' -or
        (@($harnessProvenance.release_blockers) | ConvertTo-Json -Compress) -cne
            ($expectedHarnessBlockers | ConvertTo-Json -Compress) -or
        [bool]$harnessProvenance.release_eligible -or
        [string]$harnessProvenance.runtime_path -cne "$harnessId/pkv-loopback-provider.exe" -or
        [string]$harnessProvenance.sbom_path -cne "$harnessId/sbom.cdx.json" -or
        [string]$harnessProvenance.legal_manifest_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$harnessProvenance.release_inventory_closure_sha256 -cnotmatch
            '^[0-9a-f]{64}$' -or
        [string]$harnessProvenance.release_inventory_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$harnessProvenance.sbom_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$harnessProvenance.manifest_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$harnessProvenance.runtime_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$harnessProvenance.contract_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$harnessProvenance.toolchain_lock_sha256 -cnotmatch '^[0-9a-f]{64}$') {
        throw 'W4 harness provenance identity/path/hash/size contract is invalid'
    }
    $releaseProvenance = Read-StrictJsonFile `
        -Path $resolvedProvenance -Label 'candidate Artifact provenance'
    Assert-ExactJsonFields -Object $releaseProvenance -Fields @(
        'schema_version', 'artifact_file', 'artifact_kind', 'artifact_status',
        'artifact_sha256', 'artifact_size', 'build_info_path', 'build_info_sha256',
        'build_fingerprint', 'compliance_manifest_sha256', 'compliance_sources',
        'conda_hardlink_threat_evidence',
        'payload_manifest_path', 'payload_manifest_sha256', 'sbom_path',
        'sbom_sha256', 'source_revision', 'release_blockers',
        'release_blocker_authority', 'release_blocker_authority_sha256',
        'release_eligible',
        'release_inventory_artifact_closure_sha256',
        'release_inventory_closure_sha256', 'release_inventory_path',
        'release_inventory_sha256', 'version'
    ) -Label 'candidate Artifact provenance'
    Assert-ExactJsonBoolean -Value $releaseProvenance.release_eligible `
        -Expected $false -Label 'candidate Artifact provenance release_eligible'
    $expectedBlockers = @(
        'conda-native-license-materials-and-spdx',
        'html2text-gpl-compliance',
        'native-msvc-license-and-provenance'
    )
    Assert-CondaHardlinkThreatEvidence `
        -Evidence $releaseProvenance.conda_hardlink_threat_evidence `
        -Label 'candidate Artifact provenance conda hardlink evidence'
    $releaseAuthoritySha = Assert-ReleaseBlockerAuthority `
        -Rows @($releaseProvenance.release_blocker_authority) `
        -DeclaredSha256 ([string]$releaseProvenance.release_blocker_authority_sha256) `
        -ExpectedIds $expectedBlockers -Label 'candidate Artifact blocker authority'
    if ([string]$releaseProvenance.schema_version -cne 'pkv.artifact-provenance.v1' -or
        [string]$releaseProvenance.artifact_file -cne $artifactZipName -or
        [string]$releaseProvenance.artifact_kind -cne 'test_candidate' -or
        [string]$releaseProvenance.artifact_status -cne
            'test-candidate-on-compliance-hold' -or
        [string]$releaseProvenance.artifact_sha256 -cne $artifactSha -or
        [int64]$releaseProvenance.artifact_size -cne [int64](Get-Item -LiteralPath $resolvedZip).Length -or
        [string]$releaseProvenance.version -cne '0.8.1' -or
        [bool]$releaseProvenance.release_eligible -or
        (@($releaseProvenance.release_blockers) | ConvertTo-Json -Compress) -cne
            ($expectedBlockers | ConvertTo-Json -Compress) -or
        [string]$releaseProvenance.source_revision -cnotmatch '^[0-9a-f]{40}$' -or
        [string]$releaseProvenance.build_fingerprint -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$releaseProvenance.compliance_manifest_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$releaseProvenance.payload_manifest_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$releaseProvenance.release_inventory_artifact_closure_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$releaseProvenance.release_inventory_closure_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$releaseProvenance.release_inventory_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$releaseProvenance.release_inventory_path -cne
            "$artifactId/release-inventory.json") {
        throw 'Candidate Artifact provenance identity/status/hash/hold contract is invalid'
    }
    Assert-ExactJsonFields -Object $releaseProvenance.compliance_sources -Fields @(
        'manifest_path', 'manifest_sha256', 'provenance_path', 'provenance_sha256',
        'root', 'source_file', 'source_sha256', 'source_size'
    ) -Label 'candidate provenance compliance_sources'
    $expectedComplianceNames = @(
        'html2text-2020.1.16.tar.gz',
        'html2text-2020.1.16.tar.gz.sha256',
        'manifest.json',
        'provenance.json'
    ) | Sort-Object
    $complianceItems = @(Get-ChildItem -LiteralPath $resolvedComplianceRoot -Force)
    if (@($complianceItems | Where-Object { $_.PSIsContainer }).Count -cne 0 -or
        (@($complianceItems | ForEach-Object { $_.Name } | Sort-Object) |
            ConvertTo-Json -Compress) -cne ($expectedComplianceNames | ConvertTo-Json -Compress) -or
        $resolvedComplianceManifest -cne (Join-Path $resolvedComplianceRoot 'manifest.json') -or
        $resolvedComplianceProvenance -cne
            (Join-Path $resolvedComplianceRoot 'provenance.json')) {
        throw 'ComplianceSourcesRoot must contain exactly the canonical 4-file bundle'
    }
    $sourceName = 'html2text-2020.1.16.tar.gz'
    $sourcePath = Join-Path $resolvedComplianceRoot $sourceName
    $sourceSidecarPath = Join-Path $resolvedComplianceRoot "$sourceName.sha256"
    $sourceSha = (Get-LocalFileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $sourceSize = [int64](Get-Item -LiteralPath $sourcePath).Length
    if ($sourceSha -cne 'e296318e16b059ddb97f7a8a1d6a5c1d7af4544049a01e261731d2d5cc277bbb' -or
        $sourceSize -cne 49464 -or
        [System.IO.File]::ReadAllText($sourceSidecarPath, [System.Text.Encoding]::ASCII) -cne
            "$sourceSha  $sourceName`n") {
        throw 'Compliance source archive/SHA-256 sidecar is invalid'
    }
    $complianceManifest = Read-StrictJsonFile `
        -Path $resolvedComplianceManifest -Label 'compliance source manifest'
    $complianceProvenance = Read-StrictJsonFile `
        -Path $resolvedComplianceProvenance -Label 'compliance source provenance'
    Assert-ExactJsonFields -Object $complianceManifest -Fields @(
        'schema_version', 'artifact_kind', 'build_fingerprint',
        'compliance_manifest_sha256', 'files', 'release_blockers',
        'release_blocker_authority', 'release_blocker_authority_sha256',
        'release_eligible', 'source_revision'
    ) -Label 'compliance source manifest'
    Assert-ExactJsonBoolean -Value $complianceManifest.release_eligible `
        -Expected $false -Label 'compliance source manifest release_eligible'
    Assert-ExactJsonFields -Object $complianceProvenance -Fields @(
        'schema_version', 'artifact_kind', 'build_fingerprint',
        'compliance_manifest_sha256', 'manifest_sha256', 'release_blockers',
        'release_blocker_authority', 'release_blocker_authority_sha256',
        'release_eligible', 'source_file', 'source_sha256', 'source_revision'
    ) -Label 'compliance source provenance'
    Assert-ExactJsonBoolean -Value $complianceProvenance.release_eligible `
        -Expected $false -Label 'compliance source provenance release_eligible'
    $complianceManifestSha = (Get-LocalFileHash -LiteralPath $resolvedComplianceManifest -Algorithm SHA256).Hash.ToLowerInvariant()
    $complianceProvenanceSha = (Get-LocalFileHash -LiteralPath $resolvedComplianceProvenance -Algorithm SHA256).Hash.ToLowerInvariant()
    $complianceManifestAuthoritySha = Assert-ReleaseBlockerAuthority `
        -Rows @($complianceManifest.release_blocker_authority) `
        -DeclaredSha256 ([string]$complianceManifest.release_blocker_authority_sha256) `
        -ExpectedIds $expectedBlockers -Label 'compliance source manifest blocker authority'
    $complianceProvenanceAuthoritySha = Assert-ReleaseBlockerAuthority `
        -Rows @($complianceProvenance.release_blocker_authority) `
        -DeclaredSha256 ([string]$complianceProvenance.release_blocker_authority_sha256) `
        -ExpectedIds $expectedBlockers -Label 'compliance source provenance blocker authority'
    $complianceRows = @($complianceManifest.files)
    if ($complianceRows.Count -cne 1) {
        throw 'Compliance source manifest must contain exactly one source row'
    }
    Assert-ExactJsonFields -Object $complianceRows[0] -Fields @(
        'component', 'license_expression_assessment', 'license_expression_status',
        'path', 'sha256', 'size', 'version'
    ) -Label 'compliance source row'
    if ([string]$complianceManifest.schema_version -cne 'pkv.compliance-source-bundle.v1' -or
        [string]$complianceManifest.artifact_kind -cne 'corresponding_source_bundle' -or
        [string]$complianceManifest.build_fingerprint -cne [string]$releaseProvenance.build_fingerprint -or
        [string]$complianceManifest.compliance_manifest_sha256 -cne
            [string]$releaseProvenance.compliance_manifest_sha256 -or
        [bool]$complianceManifest.release_eligible -or
        (@($complianceManifest.release_blockers) | ConvertTo-Json -Compress) -cne
            ($expectedBlockers | ConvertTo-Json -Compress) -or
        $complianceManifestAuthoritySha -cne $releaseAuthoritySha -or
        (@($complianceManifest.release_blocker_authority) | ConvertTo-Json -Depth 20 -Compress) -cne
            (@($releaseProvenance.release_blocker_authority) | ConvertTo-Json -Depth 20 -Compress) -or
        [string]$complianceManifest.source_revision -cne [string]$releaseProvenance.source_revision -or
        [string]$complianceRows[0].component -cne 'html2text' -or
        [string]$complianceRows[0].license_expression_assessment -cne 'GPL-3.0-only' -or
        [string]$complianceRows[0].license_expression_status -cne
            'requires_legal_confirmation' -or
        [string]$complianceRows[0].path -cne $sourceName -or
        [string]$complianceRows[0].sha256 -cne $sourceSha -or
        [int64]$complianceRows[0].size -cne $sourceSize -or
        [string]$complianceRows[0].version -cne '2020.1.16' -or
        [string]$complianceProvenance.schema_version -cne
            'pkv.compliance-source-provenance.v1' -or
        [string]$complianceProvenance.artifact_kind -cne 'corresponding_source_bundle' -or
        [string]$complianceProvenance.build_fingerprint -cne
            [string]$releaseProvenance.build_fingerprint -or
        [string]$complianceProvenance.compliance_manifest_sha256 -cne
            [string]$releaseProvenance.compliance_manifest_sha256 -or
        [string]$complianceProvenance.manifest_sha256 -cne $complianceManifestSha -or
        [bool]$complianceProvenance.release_eligible -or
        (@($complianceProvenance.release_blockers) | ConvertTo-Json -Compress) -cne
            ($expectedBlockers | ConvertTo-Json -Compress) -or
        $complianceProvenanceAuthoritySha -cne $releaseAuthoritySha -or
        (@($complianceProvenance.release_blocker_authority) | ConvertTo-Json -Depth 20 -Compress) -cne
            (@($releaseProvenance.release_blocker_authority) | ConvertTo-Json -Depth 20 -Compress) -or
        [string]$complianceProvenance.source_file -cne $sourceName -or
        [string]$complianceProvenance.source_sha256 -cne $sourceSha -or
        [string]$complianceProvenance.source_revision -cne
            [string]$releaseProvenance.source_revision -or
        [string]$releaseProvenance.compliance_sources.root -cne '../compliance-sources' -or
        [string]$releaseProvenance.compliance_sources.manifest_path -cne
            '../compliance-sources/manifest.json' -or
        [string]$releaseProvenance.compliance_sources.manifest_sha256 -cne
            $complianceManifestSha -or
        [string]$releaseProvenance.compliance_sources.provenance_path -cne
            '../compliance-sources/provenance.json' -or
        [string]$releaseProvenance.compliance_sources.provenance_sha256 -cne
            $complianceProvenanceSha -or
        [string]$releaseProvenance.compliance_sources.source_file -cne $sourceName -or
        [string]$releaseProvenance.compliance_sources.source_sha256 -cne $sourceSha -or
        [int64]$releaseProvenance.compliance_sources.source_size -cne $sourceSize) {
        throw 'Candidate and compliance-source manifest/provenance/source bindings are invalid'
    }
    if ([string]$harnessProvenance.build_fingerprint -cne [string]$releaseProvenance.build_fingerprint -or
        [string]$harnessProvenance.source_revision -cne [string]$releaseProvenance.source_revision) {
        throw 'W4 release Artifact and harness provenance do not share the same frozen build/revision'
    }

    foreach ($directory in @($prospectiveEvidence, $prospectiveWorkspace, $prospectiveHarnessWorkspace)) {
        [void][System.IO.Directory]::CreateDirectory($directory)
        Assert-SafePathChain -Path $directory -Label 'W4 mutable root'
        Assert-SafeTree -Root $directory -Label 'W4 mutable root'
    }
    $resolvedEvidence = Get-CanonicalExistingPath -Path $prospectiveEvidence -Kind Container
    $resolvedWorkspace = Get-CanonicalExistingPath -Path $prospectiveWorkspace -Kind Container
    $resolvedHarnessWorkspace = Get-CanonicalExistingPath `
        -Path $prospectiveHarnessWorkspace -Kind Container
    $harnessExtractRoot = Join-Path $resolvedHarnessWorkspace ("harness-input-" + $RunId)
    if (-not (Test-PathContainedBy -Candidate $harnessExtractRoot -Root $resolvedHarnessWorkspace)) {
        throw 'W4 harness extraction root escaped its mutable authority'
    }
    Assert-SafePathChain -Path $harnessExtractRoot -Label 'W4 harness extraction root'
    if (Test-Path -LiteralPath $harnessExtractRoot) {
        throw "W4 harness extraction root already exists: $harnessExtractRoot"
    }
    $resolvedHarness = Expand-SafeSingleRootZip -ZipPath $harnessZip `
        -Destination $harnessExtractRoot -ExpectedRootName $harnessId
    Assert-SafeTree -Root $resolvedHarness -Label 'extracted W4 harness'
    $expectedHarnessFiles = @(
        'COMPLIANCE-HOLD.txt',
        'LICENSE',
        'THIRD-PARTY-NOTICES.txt',
        'contract.v1.json',
        'legal-manifest.json',
        'licenses/cpython-3.11.15-LICENSE.txt',
        'licenses/index.json',
        'licenses/pyinstaller-6.21.0-COPYING.txt',
        'manifest.json',
        'pkv-loopback-provider.exe',
        'release-inventory.json',
        'sbom.cdx.json',
        'scripts/provider-error.v1.json',
        'scripts/stop.v1.json',
        'scripts/success.v1.json',
        'scripts/w4-chat-lifecycle.v1.json'
    ) | Sort-Object
    $actualHarnessFiles = @(
        Get-ChildItem -LiteralPath $resolvedHarness -File -Recurse -Force |
            ForEach-Object {
                $_.FullName.Substring($resolvedHarness.TrimEnd('\').Length).
                    TrimStart('\').Replace('\', '/')
            } |
            Sort-Object
    )
    if (($actualHarnessFiles | ConvertTo-Json -Compress) -cne
        ($expectedHarnessFiles | ConvertTo-Json -Compress)) {
        throw 'Extracted W4 harness file set differs from the frozen package contract'
    }
    $harnessManifestPath = Join-Path $resolvedHarness 'manifest.json'
    $harnessRuntimePath = Join-Path $resolvedHarness 'pkv-loopback-provider.exe'
    $harnessContractPath = Join-Path $resolvedHarness 'contract.v1.json'
    $harnessLegalManifestPath = Join-Path $resolvedHarness 'legal-manifest.json'
    $harnessInventoryPath = Join-Path $resolvedHarness 'release-inventory.json'
    $harnessSbomPath = Join-Path $resolvedHarness 'sbom.cdx.json'
    if ((Get-LocalFileHash -LiteralPath $harnessLegalManifestPath -Algorithm SHA256).Hash.ToLowerInvariant() -cne
            [string]$harnessProvenance.legal_manifest_sha256 -or
        (Get-LocalFileHash -LiteralPath $harnessSbomPath -Algorithm SHA256).Hash.ToLowerInvariant() -cne
            [string]$harnessProvenance.sbom_sha256 -or
        (Get-LocalFileHash -LiteralPath $harnessInventoryPath -Algorithm SHA256).Hash.ToLowerInvariant() -cne
            [string]$harnessProvenance.release_inventory_sha256) {
        throw 'W4 harness provenance does not bind legal-manifest/SBOM/release inventory'
    }
    $harnessLegalManifest = Read-StrictJsonFile `
        -Path $harnessLegalManifestPath -Label 'W4 harness legal manifest'
    Assert-ExactJsonFields -Object $harnessLegalManifest -Fields @(
        'schema_version', 'artifact_kind', 'artifact_status', 'build_fingerprint',
        'release_blocker_authority', 'release_blocker_authority_sha256',
        'release_blockers', 'release_eligible', 'release_inventory_closure_sha256',
        'release_inventory_sha256', 'entries'
    ) -Label 'W4 harness legal manifest'
    Assert-ExactJsonBoolean -Value $harnessLegalManifest.release_eligible `
        -Expected $false -Label 'W4 harness legal manifest release_eligible'
    $harnessLegalAuthoritySha = Assert-ReleaseBlockerAuthority `
        -Rows @($harnessLegalManifest.release_blocker_authority) `
        -DeclaredSha256 ([string]$harnessLegalManifest.release_blocker_authority_sha256) `
        -ExpectedIds $expectedHarnessBlockers -Label 'W4 harness legal blocker authority'
    $expectedLegalPaths = @(
        'COMPLIANCE-HOLD.txt',
        'LICENSE',
        'THIRD-PARTY-NOTICES.txt',
        'licenses/cpython-3.11.15-LICENSE.txt',
        'licenses/index.json',
        'licenses/pyinstaller-6.21.0-COPYING.txt',
        'release-inventory.json',
        'sbom.cdx.json'
    )
    $legalRows = @($harnessLegalManifest.entries)
    if ([string]$harnessLegalManifest.schema_version -cne
            'pkv.harness-legal-manifest.v1' -or
        [string]$harnessLegalManifest.artifact_kind -cne 'e2e_test_harness' -or
        [string]$harnessLegalManifest.artifact_status -cne
            'internal-verification-only-on-native-compliance-hold' -or
        [string]$harnessLegalManifest.build_fingerprint -cne
            [string]$harnessProvenance.build_fingerprint -or
        $harnessLegalAuthoritySha -cne $harnessAuthoritySha -or
        (@($harnessLegalManifest.release_blocker_authority) | ConvertTo-Json -Depth 20 -Compress) -cne
            (@($harnessProvenance.release_blocker_authority) | ConvertTo-Json -Depth 20 -Compress) -or
        (@($harnessLegalManifest.release_blockers) | ConvertTo-Json -Compress) -cne
            ($expectedHarnessBlockers | ConvertTo-Json -Compress) -or
        [bool]$harnessLegalManifest.release_eligible -or
        [string]$harnessLegalManifest.release_inventory_closure_sha256 -cne
            [string]$harnessProvenance.release_inventory_closure_sha256 -or
        [string]$harnessLegalManifest.release_inventory_sha256 -cne
            [string]$harnessProvenance.release_inventory_sha256 -or
        $legalRows.Count -cne $expectedLegalPaths.Count) {
        throw 'W4 harness legal manifest identity/count/build binding is invalid'
    }
    for ($legalIndex = 0; $legalIndex -lt $expectedLegalPaths.Count; $legalIndex += 1) {
        $legalRow = $legalRows[$legalIndex]
        Assert-ExactJsonFields -Object $legalRow -Fields @('path', 'sha256', 'size') `
            -Label 'W4 harness legal manifest row'
        $legalPath = Join-Path $resolvedHarness ($expectedLegalPaths[$legalIndex].Replace('/', '\'))
        if ([string]$legalRow.path -cne $expectedLegalPaths[$legalIndex] -or
            [string]$legalRow.sha256 -cne
                (Get-LocalFileHash -LiteralPath $legalPath -Algorithm SHA256).Hash.ToLowerInvariant() -or
            [int64]$legalRow.size -cne [int64](Get-Item -LiteralPath $legalPath).Length) {
            throw "W4 harness legal manifest row is invalid: $($expectedLegalPaths[$legalIndex])"
        }
    }
    $harnessInventory = Read-StrictJsonFile `
        -Path $harnessInventoryPath -Label 'W4 harness release inventory'
    $harnessInventorySummary = Assert-HarnessReleaseInventory `
        -Inventory $harnessInventory -InventoryPath $harnessInventoryPath `
        -RuntimePath $harnessRuntimePath -Provenance $harnessProvenance `
        -ExpectedAuthorityRows @($harnessProvenance.release_blocker_authority) `
        -ExpectedAuthoritySha256 $harnessAuthoritySha `
        -ExpectedBlockers $expectedHarnessBlockers
    $harnessLicenseIndex = Read-StrictJsonFile `
        -Path (Join-Path $resolvedHarness 'licenses\index.json') `
        -Label 'W4 harness license index'
    Assert-ExactJsonFields -Object $harnessLicenseIndex `
        -Fields @('schema_version', 'actual_runtime_inventory', 'entries') `
        -Label 'W4 harness license index'
    Assert-ExactJsonFields -Object $harnessLicenseIndex.actual_runtime_inventory `
        -Fields @('components', 'release_inventory_closure_sha256',
            'release_inventory_path') -Label 'W4 harness actual runtime license inventory'
    $expectedHarnessLicenses = @(
        [ordered]@{
            name = 'cpython'; version = '3.11.15'; expression = 'Python-2.0'
            purl = 'pkg:generic/cpython@3.11.15'
            path = 'licenses/cpython-3.11.15-LICENSE.txt'
            sha256 = '3b2f81fe21d181c499c59a256c8e1968455d6689d269aa85373bfb6af41da3bf'
        },
        [ordered]@{
            name = 'pyinstaller'; version = '6.21.0'
            expression = 'GPL-2.0-or-later WITH Bootloader-exception'
            purl = 'pkg:generic/pyinstaller@6.21.0'
            path = 'licenses/pyinstaller-6.21.0-COPYING.txt'
            sha256 = 'dcf75fdb959db1e3b41c0f8505069d2ece781b5ec6b3d0a4d30975cfc6580245'
        }
    )
    $harnessLicenseRows = @($harnessLicenseIndex.entries)
    if ([string]$harnessLicenseIndex.schema_version -cne 'pkv.license-index.v2' -or
        [string]$harnessLicenseIndex.actual_runtime_inventory.release_inventory_path -cne
            'release-inventory.json' -or
        [string]$harnessLicenseIndex.actual_runtime_inventory.release_inventory_closure_sha256 -cne
            [string]$harnessInventory.bindings.closure_sha256 -or
        $harnessLicenseRows.Count -cne 2) {
        throw 'W4 harness license index identity/count is invalid'
    }
    for ($licenseIndex = 0; $licenseIndex -lt 2; $licenseIndex += 1) {
        $licenseRow = $harnessLicenseRows[$licenseIndex]
        $expectedLicense = $expectedHarnessLicenses[$licenseIndex]
        Assert-ExactJsonFields -Object $licenseRow -Fields @(
            'license_expression', 'license_files', 'name', 'purl', 'version'
        ) -Label 'W4 harness license index entry'
        $licenseFiles = @($licenseRow.license_files)
        if ($licenseFiles.Count -cne 1) {
            throw 'W4 harness license entry must bind exactly one license file'
        }
        Assert-ExactJsonFields -Object $licenseFiles[0] `
            -Fields @('path', 'sha256', 'source_kind') `
            -Label 'W4 harness license file row'
        if ([string]$licenseRow.name -cne [string]$expectedLicense.name -or
            [string]$licenseRow.version -cne [string]$expectedLicense.version -or
            [string]$licenseRow.license_expression -cne [string]$expectedLicense.expression -or
            [string]$licenseRow.purl -cne [string]$expectedLicense.purl -or
            [string]$licenseFiles[0].path -cne [string]$expectedLicense.path -or
            [string]$licenseFiles[0].sha256 -cne [string]$expectedLicense.sha256 -or
            [string]$licenseFiles[0].source_kind -cne 'compliance_asset') {
            throw "W4 harness license row differs from frozen legal material: $($expectedLicense.name)"
        }
    }
    $actualRuntimeLicenseRows = @(
        $harnessLicenseIndex.actual_runtime_inventory.components
    )
    $expectedRuntimeComponentIds = @(Get-Utf8SortedStrings -Values @(
        @($harnessInventory.components) |
            Where-Object {
                [string]$_.identity_status -ceq 'complete' -and
                [string]$_.id -cne 'application:project'
            } |
            ForEach-Object { [string]$_.id }
    ))
    $actualRuntimeComponentIds = @(
        $actualRuntimeLicenseRows |
            ForEach-Object { [string]$_.component_id }
    )
    if (($actualRuntimeComponentIds | ConvertTo-Json -Compress) -cne
        ($expectedRuntimeComponentIds | ConvertTo-Json -Compress)) {
        throw 'W4 harness actual runtime license component IDs/order differ from inventory'
    }
    $runtimeLicenseById = @{}
    foreach ($actualRuntimeRow in $actualRuntimeLicenseRows) {
        $actualRuntimePropertyNames = @($actualRuntimeRow.PSObject.Properties.Name)
        $actualRuntimeFields = @(
            'classifications', 'component_id', 'component_sha256', 'embedded_paths',
            'license', 'license_files', 'license_material_status', 'name',
            'payload_paths', 'source_paths'
        )
        if ($actualRuntimePropertyNames -ccontains 'purl') {
            $actualRuntimeFields += 'purl'
        }
        if ($actualRuntimePropertyNames -ccontains 'version') {
            $actualRuntimeFields += 'version'
        }
        if ($actualRuntimePropertyNames -ccontains 'license_expression_status') {
            $actualRuntimeFields += 'license_expression_status'
        }
        Assert-ExactJsonFields -Object $actualRuntimeRow -Fields $actualRuntimeFields `
            -Label 'W4 harness actual runtime license component'
        $componentId = [string]$actualRuntimeRow.component_id
        $inventoryComponent = $harnessInventorySummary.ComponentById[$componentId]
        if ($null -eq $inventoryComponent) {
            throw "W4 harness actual runtime component is absent from inventory: $componentId"
        }
        $expectedLicenseMaterialStatus = Get-ExpectedLicenseMaterialStatus `
            -ComponentId $componentId -InventoryComponent $inventoryComponent `
            -LicenseIndexComponent $actualRuntimeRow
        if ($runtimeLicenseById.ContainsKey($componentId) -or
            [string]$actualRuntimeRow.component_sha256 -cne
                (Get-CanonicalJsonSha256 -Value $inventoryComponent) -or
            (@($actualRuntimeRow.classifications) | ConvertTo-Json -Compress) -cne
                (@($inventoryComponent.classification_ids) | ConvertTo-Json -Compress) -or
            (@($actualRuntimeRow.embedded_paths) | ConvertTo-Json -Compress) -cne
                (@($inventoryComponent.embedded_paths) | ConvertTo-Json -Compress) -or
            (@($actualRuntimeRow.payload_paths) | ConvertTo-Json -Compress) -cne
                (@($inventoryComponent.payload_paths) | ConvertTo-Json -Compress) -or
            (@($actualRuntimeRow.source_paths) | ConvertTo-Json -Compress) -cne
                (@($inventoryComponent.source_paths) | ConvertTo-Json -Compress) -or
            [string]$actualRuntimeRow.license_material_status -cne
                $expectedLicenseMaterialStatus) {
            throw "W4 harness actual runtime license row is inconsistent: $componentId"
        }
        $licenseFields = @($actualRuntimeRow.license.PSObject.Properties.Name)
        if (($licenseFields | ConvertTo-Json -Compress) -cne
                (@('expression') | ConvertTo-Json -Compress) -and
            ($licenseFields | ConvertTo-Json -Compress) -cne
                (@('license') | ConvertTo-Json -Compress)) {
            throw "W4 harness actual runtime license choice is invalid: $componentId"
        }
        $runtimeLicenseById[$componentId] = $actualRuntimeRow
    }
    $harnessSbom = Read-StrictJsonFile -Path $harnessSbomPath -Label 'W4 harness SBOM'
    Assert-ExactJsonFields -Object $harnessSbom `
        -Fields @('bomFormat', 'specVersion', 'version', 'metadata', 'components',
            'dependencies') `
        -Label 'W4 harness SBOM'
    Assert-ExactJsonFields -Object $harnessSbom.metadata `
        -Fields @('timestamp', 'component', 'properties') -Label 'W4 harness SBOM metadata'
    Assert-ExactJsonFields -Object $harnessSbom.metadata.component `
        -Fields @('bom-ref', 'name', 'type', 'version') -Label 'W4 harness SBOM component'
    $harnessSbomProperties = @($harnessSbom.metadata.properties)
    $harnessSbomComponents = @($harnessSbom.components)
    if ([string]$harnessSbom.bomFormat -cne 'CycloneDX' -or
        [string]$harnessSbom.specVersion -cne '1.5' -or [int]$harnessSbom.version -cne 1 -or
        [string]$harnessSbom.metadata.component.'bom-ref' -cne
            'pkg:generic/pkv-w4-loopback-harness@1.0.0' -or
        [string]$harnessSbom.metadata.component.name -cne 'PKV W4 Loopback Harness' -or
        [string]$harnessSbom.metadata.component.type -cne 'application' -or
        [string]$harnessSbom.metadata.component.version -cne '1.0.0' -or
        $harnessSbomProperties.Count -cne 9 -or
        $harnessSbomComponents.Count -cne $actualRuntimeLicenseRows.Count) {
        throw 'W4 harness SBOM identity/count contract is invalid'
    }
    $expectedHarnessSbomProperties = @(
        [ordered]@{ name = 'pkv:artifact-kind'; value = 'e2e_test_harness' },
        [ordered]@{
            name = 'pkv:artifact-status'
            value = 'internal-verification-only-on-native-compliance-hold'
        },
        [ordered]@{
            name = 'pkv:release-blocker'; value = 'harness-native-license-and-provenance'
        },
        [ordered]@{
            name = 'pkv:release-blocker-authority-sha256'; value = $harnessAuthoritySha
        },
        [ordered]@{ name = 'pkv:release-eligible'; value = 'false' },
        [ordered]@{
            name = 'pkv:release-inventory-closure-sha256'
            value = [string]$harnessInventory.bindings.closure_sha256
        },
        [ordered]@{
            name = 'pkv:release-inventory-path'; value = 'release-inventory.json'
        },
        [ordered]@{
            name = 'pkv:release-inventory-sha256'
            value = [string]$harnessProvenance.release_inventory_sha256
        },
        [ordered]@{ name = 'pkv:release-payload-membership'; value = 'forbidden' }
    )
    for ($propertyIndex = 0; $propertyIndex -lt $expectedHarnessSbomProperties.Count; $propertyIndex += 1) {
        Assert-ExactJsonFields -Object $harnessSbomProperties[$propertyIndex] `
            -Fields @('name', 'value') -Label 'W4 harness SBOM property'
        if (($harnessSbomProperties[$propertyIndex] | ConvertTo-Json -Compress) -cne
            ($expectedHarnessSbomProperties[$propertyIndex] | ConvertTo-Json -Compress)) {
            throw 'W4 harness SBOM metadata properties are invalid'
        }
    }
    $sbomRefs = [System.Collections.Generic.List[string]]::new()
    for ($componentIndex = 0; $componentIndex -lt $harnessSbomComponents.Count; $componentIndex += 1) {
        $sbomComponent = $harnessSbomComponents[$componentIndex]
        $runtimeLicenseRow = $actualRuntimeLicenseRows[$componentIndex]
        $runtimeLicensePropertyNames = @($runtimeLicenseRow.PSObject.Properties.Name)
        $componentId = [string]$runtimeLicenseRow.component_id
        $inventoryComponent = $harnessInventorySummary.ComponentById[$componentId]
        $sbomFields = @('bom-ref', 'licenses', 'name', 'properties', 'type')
        if ($runtimeLicensePropertyNames -ccontains 'purl') {
            $sbomFields += 'purl'
        }
        if ($runtimeLicensePropertyNames -ccontains 'version') {
            $sbomFields += 'version'
        }
        Assert-ExactJsonFields -Object $sbomComponent -Fields $sbomFields `
            -Label 'W4 harness SBOM runtime component'
        $licenseChoices = @($sbomComponent.licenses)
        if ($licenseChoices.Count -cne 1) {
            throw 'W4 harness SBOM component must have exactly one license choice'
        }
        $componentProperties = @($sbomComponent.properties)
        $expectedComponentPropertyNames = @(
            'pkv:inventory-component-id',
            'pkv:inventory-component-sha256',
            'pkv:inventory-identity-status',
            'pkv:contains-native-payload',
            'pkv:license-material-status'
        )
        $expectedComponentPropertyNames += @(
            @($runtimeLicenseRow.payload_paths) | ForEach-Object { 'pkv:payload-path' }
        )
        $expectedComponentPropertyNames += @(
            @($runtimeLicenseRow.embedded_paths) | ForEach-Object { 'pkv:embedded-path' }
        )
        $expectedComponentPropertyNames += @(
            @($runtimeLicenseRow.classifications) |
                ForEach-Object { 'pkv:payload-classification' }
        )
        if ($runtimeLicensePropertyNames -ccontains 'license_expression_status') {
            $expectedComponentPropertyNames += 'pkv:license-expression-status'
        }
        foreach ($condaKey in @(
            'build', 'build_number', 'channel', 'package_sha256',
            'record_file', 'record_sha256', 'subdir'
        )) {
            if (@($inventoryComponent.PSObject.Properties.Name) -ccontains $condaKey) {
                $expectedComponentPropertyNames +=
                    ('pkv:conda-' + $condaKey.Replace('_', '-'))
            }
        }
        $actualComponentPropertyNames = @(
            $componentProperties | ForEach-Object { [string]$_.name }
        )
        if (($actualComponentPropertyNames | ConvertTo-Json -Compress) -cne
            ($expectedComponentPropertyNames | ConvertTo-Json -Compress)) {
            throw "W4 harness SBOM component property order/set is invalid: $componentId"
        }
        foreach ($property in $componentProperties) {
            Assert-ExactJsonFields -Object $property -Fields @('name', 'value') `
                -Label 'W4 harness SBOM component property'
            if ([string]$property.name -notmatch
                '^pkv:(inventory-component-id|inventory-component-sha256|inventory-identity-status|contains-native-payload|license-material-status|payload-path|embedded-path|payload-classification|license-expression-status|conda-[a-z0-9-]+)$') {
                throw "W4 harness SBOM contains an unexpected component property: $($property.name)"
            }
        }
        $propertyValues = @{}
        foreach ($property in $componentProperties) {
            $propertyName = [string]$property.name
            if (-not $propertyValues.ContainsKey($propertyName)) {
                $propertyValues[$propertyName] = [System.Collections.Generic.List[string]]::new()
            }
            $propertyValues[$propertyName].Add([string]$property.value)
        }
        foreach ($condaKey in @(
            'build', 'build_number', 'channel', 'package_sha256',
            'record_file', 'record_sha256', 'subdir'
        )) {
            if (@($inventoryComponent.PSObject.Properties.Name) -cnotcontains $condaKey) {
                continue
            }
            $condaPropertyName = 'pkv:conda-' + $condaKey.Replace('_', '-')
            if (@($propertyValues[$condaPropertyName]).Count -cne 1 -or
                [string]$propertyValues[$condaPropertyName][0] -cne
                    [string]$inventoryComponent.$condaKey) {
                throw "W4 harness SBOM Conda property binding is invalid: $componentId/$condaKey"
            }
        }
        if ($runtimeLicensePropertyNames -ccontains 'license_expression_status' -and
            (@($propertyValues['pkv:license-expression-status']).Count -cne 1 -or
                [string]$propertyValues['pkv:license-expression-status'][0] -cne
                    [string]$runtimeLicenseRow.license_expression_status)) {
            throw "W4 harness SBOM license-expression status is invalid: $componentId"
        }
        $expectedComponentHash = [string]$runtimeLicenseRow.component_sha256
        $expectedType = if ([string]$inventoryComponent.type -ceq 'framework') {
            'framework'
        } else {
            'library'
        }
        if ([string]$sbomComponent.'bom-ref' -cne
                "urn:pkv:release-inventory-component:$expectedComponentHash" -or
            [string]$sbomComponent.name -cne [string]$runtimeLicenseRow.name -or
            [string]$sbomComponent.purl -cne [string]$runtimeLicenseRow.purl -or
            [string]$sbomComponent.version -cne [string]$runtimeLicenseRow.version -or
            [string]$sbomComponent.type -cne $expectedType -or
            ($licenseChoices | ConvertTo-Json -Depth 10 -Compress) -cne
                (@($runtimeLicenseRow.license) | ConvertTo-Json -Depth 10 -Compress) -or
            @($propertyValues['pkv:inventory-component-id']).Count -cne 1 -or
            [string]$propertyValues['pkv:inventory-component-id'][0] -cne $componentId -or
            @($propertyValues['pkv:inventory-component-sha256']).Count -cne 1 -or
            [string]$propertyValues['pkv:inventory-component-sha256'][0] -cne
                $expectedComponentHash -or
            @($propertyValues['pkv:inventory-identity-status']).Count -cne 1 -or
            [string]$propertyValues['pkv:inventory-identity-status'][0] -cne 'complete' -or
            @($propertyValues['pkv:contains-native-payload']).Count -cne 1 -or
            [string]$propertyValues['pkv:contains-native-payload'][0] -cne
                $(if ([bool]$inventoryComponent.contains_native_payload) { 'true' } else { 'false' }) -or
            @($propertyValues['pkv:license-material-status']).Count -cne 1 -or
            [string]$propertyValues['pkv:license-material-status'][0] -cne
                [string]$runtimeLicenseRow.license_material_status -or
            (@($propertyValues['pkv:payload-path']) | ConvertTo-Json -Compress) -cne
                (@($runtimeLicenseRow.payload_paths) | ConvertTo-Json -Compress) -or
            (@($propertyValues['pkv:embedded-path']) | ConvertTo-Json -Compress) -cne
                (@($runtimeLicenseRow.embedded_paths) | ConvertTo-Json -Compress) -or
            (@($propertyValues['pkv:payload-classification']) | ConvertTo-Json -Compress) -cne
                (@($runtimeLicenseRow.classifications) | ConvertTo-Json -Compress)) {
            throw "W4 harness SBOM/inventory component binding is invalid: $componentId"
        }
        $sbomRefs.Add([string]$sbomComponent.'bom-ref')
    }
    $harnessDependencies = @($harnessSbom.dependencies)
    if ($harnessDependencies.Count -cne 1) {
        throw 'W4 harness SBOM must contain exactly one application dependency row'
    }
    Assert-ExactJsonFields -Object $harnessDependencies[0] -Fields @('dependsOn', 'ref') `
        -Label 'W4 harness SBOM dependency row'
    if ([string]$harnessDependencies[0].ref -cne
            'pkg:generic/pkv-w4-loopback-harness@1.0.0' -or
        (@($harnessDependencies[0].dependsOn) | ConvertTo-Json -Compress) -cne
            (@($sbomRefs) | ConvertTo-Json -Compress)) {
        throw 'W4 harness SBOM dependency graph is inconsistent with inventory components'
    }
    $harnessManifest = Read-StrictJsonFile -Path $harnessManifestPath -Label 'W4 harness manifest'
    Assert-ExactJsonFields -Object $harnessManifest -Fields @(
        'schema_version', 'contract_id', 'harness_version', 'distribution',
        'release_payload_membership', 'runtime', 'contract', 'scripts', 'build'
    ) -Label 'W4 harness manifest'
    if ([string]$harnessManifest.schema_version -cne 'pkv.w3.loopback.manifest.v1' -or
        [string]$harnessManifest.contract_id -cne 'w3.openai_compatible_loopback.v1' -or
        [string]$harnessManifest.harness_version -cne '1.0.0' -or
        [string]$harnessManifest.distribution -cne 'e2e-only' -or
        [string]$harnessManifest.release_payload_membership -cne 'forbidden') {
        throw 'W4 harness manifest identity/distribution contract is invalid'
    }
    Assert-ExactJsonFields -Object $harnessManifest.runtime `
        -Fields @('kind', 'path', 'size', 'sha256') -Label 'W4 harness runtime manifest row'
    Assert-ExactJsonFields -Object $harnessManifest.contract `
        -Fields @('path', 'sha256') -Label 'W4 harness contract manifest row'
    Assert-ExactJsonFields -Object $harnessManifest.build `
        -Fields @('source_revision', 'build_fingerprint_sha256', 'toolchain_lock_sha256') `
        -Label 'W4 harness build manifest row'
    if ([string]$harnessManifest.runtime.kind -cne 'frozen' -or
        [string]$harnessManifest.runtime.path -cne 'pkv-loopback-provider.exe' -or
        [int64]$harnessManifest.runtime.size -cne [int64](Get-Item -LiteralPath $harnessRuntimePath).Length -or
        [string]$harnessManifest.runtime.sha256 -cne (Get-LocalFileHash -LiteralPath $harnessRuntimePath -Algorithm SHA256).Hash.ToLowerInvariant() -or
        [string]$harnessManifest.contract.path -cne 'contract.v1.json' -or
        [string]$harnessManifest.contract.sha256 -cne (Get-LocalFileHash -LiteralPath $harnessContractPath -Algorithm SHA256).Hash.ToLowerInvariant() -or
        [string]$harnessManifest.build.source_revision -cne [string]$harnessProvenance.source_revision -or
        [string]$harnessManifest.build.build_fingerprint_sha256 -cne [string]$harnessProvenance.build_fingerprint -or
        [string]$harnessManifest.build.toolchain_lock_sha256 -cne [string]$harnessProvenance.toolchain_lock_sha256 -or
        (Get-LocalFileHash -LiteralPath $harnessManifestPath -Algorithm SHA256).Hash.ToLowerInvariant() -cne [string]$harnessProvenance.manifest_sha256 -or
        (Get-LocalFileHash -LiteralPath $harnessRuntimePath -Algorithm SHA256).Hash.ToLowerInvariant() -cne [string]$harnessProvenance.runtime_sha256 -or
        (Get-LocalFileHash -LiteralPath $harnessContractPath -Algorithm SHA256).Hash.ToLowerInvariant() -cne [string]$harnessProvenance.contract_sha256) {
        throw 'W4 harness manifest/provenance/runtime/contract cross-hash validation failed'
    }
    $expectedScriptIds = @(
        'w3.chat.provider-error.v1', 'w3.chat.stop.v1',
        'w3.chat.success.v1', 'w4.chat.lifecycle.v1'
    )
    $actualScriptIds = @($harnessManifest.scripts | ForEach-Object { [string]$_.script_id })
    if (($actualScriptIds | ConvertTo-Json -Compress) -ne
        ($expectedScriptIds | ConvertTo-Json -Compress)) {
        throw 'W4 harness manifest script IDs/order are not canonical'
    }
    foreach ($scriptRow in @($harnessManifest.scripts)) {
        Assert-ExactJsonFields -Object $scriptRow `
            -Fields @('script_id', 'path', 'sha256') -Label 'W4 harness script row'
        $scriptRelative = ([string]$scriptRow.path).Replace('\', '/')
        if (-not $scriptRelative.StartsWith('scripts/', [System.StringComparison]::Ordinal) -or
            $scriptRelative -match '(^|/)\.\.(/|$)') {
            throw "W4 harness script path is invalid: $scriptRelative"
        }
        $scriptFile = Join-Path $resolvedHarness ($scriptRelative.Replace('/', '\'))
        if (-not (Test-Path -LiteralPath $scriptFile -PathType Leaf) -or
            (Get-LocalFileHash -LiteralPath $scriptFile -Algorithm SHA256).Hash.ToLowerInvariant() -cne [string]$scriptRow.sha256) {
            throw "W4 harness script hash does not match manifest: $scriptRelative"
        }
    }
    $scenarioContract = Read-StrictJsonFile `
        -Path $resolvedScenarioContract -Label 'W4 scenario contract'
    Assert-ExactJsonFields -Object $scenarioContract -Fields @(
        'schema_version', 'runner_version', 'artifact_version', 'ordered_scenarios',
        'required_matrix_rows', 'required_artifact_files', 'mcp'
    ) -Label 'W4 scenario contract'
    $expectedScenarioIds = @(
        $scenarioContract.ordered_scenarios | ForEach-Object { [string]$_.scenario_id }
    )
    $expectedMatrixRows = @(
        $scenarioContract.ordered_scenarios | ForEach-Object { @($_.matrix_rows) }
    )
    if ([string]$scenarioContract.schema_version -cne 'pkv.m13.w4-driver-scenarios.v2' -or
        [string]$scenarioContract.runner_version -cne 'pkv.m13.artifact-runner.v2' -or
        $expectedScenarioIds.Count -cne 9 -or
        @($expectedScenarioIds | Sort-Object -Unique).Count -cne 9 -or
        $expectedMatrixRows.Count -cne 10 -or
        @($expectedMatrixRows | Sort-Object -Unique).Count -cne 10) {
        throw 'W4 scenario contract does not freeze exactly 9 scenarios/10 rows'
    }
    $artifactSha = (Get-LocalFileHash -LiteralPath $resolvedZip -Algorithm SHA256).Hash.ToLowerInvariant()
    $artifactId = [System.IO.Path]::GetFileNameWithoutExtension($resolvedZip)
    $controllerTreeShaBefore = Get-TreeManifestSha256 -Root $resolvedDriverRoot
    $fixtureTreeShaBefore = Get-TreeManifestSha256 -Root $resolvedFixture
    $harnessTreeShaBefore = Get-TreeManifestSha256 -Root $resolvedHarness
    $candidateTreeShaBefore = Get-TreeManifestSha256 -Root $resolvedCandidateRoot
    $complianceTreeShaBefore = Get-TreeManifestSha256 -Root $resolvedComplianceRoot
    $launcherEvidence = Join-Path (Join-Path $resolvedEvidence 'launcher') $RunId
    Assert-SafeTree -Root $resolvedEvidence -Label 'W4 evidence root before launcher output'
    if (-not (Test-PathContainedBy -Candidate $launcherEvidence -Root $resolvedEvidence)) {
        throw 'W4 launcher evidence root escaped its mutable authority'
    }
    Assert-SafePathChain -Path $launcherEvidence -Label 'W4 launcher evidence root'
    if (Test-Path -LiteralPath $launcherEvidence) {
        throw "W4 launcher evidence already exists: $launcherEvidence"
    }
    [void][System.IO.Directory]::CreateDirectory($launcherEvidence)
    Assert-SafePathChain -Path $launcherEvidence -Label 'W4 launcher evidence root'
    Assert-SafeTree -Root $launcherEvidence -Label 'W4 launcher evidence root'

    $processInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $processInfo.FileName = (Get-Process -Id $PID -ErrorAction Stop).Path
    $controllerArguments = @(
        '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-File', $resolvedController,
        '-CandidateRoot', $resolvedCandidateRoot,
        '-DistributionZip', $resolvedZip,
        '-DistributionSha256', $resolvedSha,
        '-ProvenancePath', $resolvedProvenance,
        '-ComplianceSourcesRoot', $resolvedComplianceRoot,
        '-ComplianceManifestPath', $resolvedComplianceManifest,
        '-ComplianceProvenancePath', $resolvedComplianceProvenance,
        '-FixtureRoot', $resolvedFixture,
        '-EvidenceRoot', $resolvedEvidence,
        '-WorkspaceRoot', $resolvedWorkspace,
        '-ScenarioContract', $resolvedScenarioContract,
        '-HarnessRoot', $resolvedHarness,
        '-ExecutionId', $RunId
    )
    $processInfo.Arguments = (($controllerArguments | ForEach-Object {
        ConvertTo-WindowsCommandLineArgument -Value $_
    }) -join ' ')
    $processInfo.WorkingDirectory = $resolvedWorkspace
    $processInfo.UseShellExecute = $false
    $processInfo.CreateNoWindow = $true
    $processInfo.RedirectStandardOutput = $true
    $processInfo.RedirectStandardError = $true
    $processInfo.EnvironmentVariables.Clear()
    $systemRoot = [Environment]::GetEnvironmentVariable('SystemRoot')
    if ([string]::IsNullOrWhiteSpace($systemRoot)) {
        throw 'SystemRoot is unavailable for the W4 controller environment'
    }
    $systemPath = @(
        (Join-Path $systemRoot 'System32'),
        (Join-Path $systemRoot 'System32\Wbem'),
        (Join-Path $systemRoot 'System32\WindowsPowerShell\v1.0')
    ) -join [System.IO.Path]::PathSeparator
    $controllerEnvironment = [ordered]@{
        SystemRoot = $systemRoot
        WINDIR = $systemRoot
        COMSPEC = (Join-Path $systemRoot 'System32\cmd.exe')
        PATH = $systemPath
        PATHEXT = '.COM;.EXE;.BAT;.CMD'
        TEMP = (Join-Path $resolvedWorkspace 'launcher-temp')
        TMP = (Join-Path $resolvedWorkspace 'launcher-temp')
        TMPDIR = (Join-Path $resolvedWorkspace 'launcher-temp')
        USERPROFILE = (Join-Path $resolvedWorkspace 'launcher-profile')
        LOCALAPPDATA = (Join-Path $resolvedWorkspace 'launcher-profile\AppData\Local')
        APPDATA = (Join-Path $resolvedWorkspace 'launcher-profile\AppData\Roaming')
        PKV_W4_ARTIFACT_ONLY = '1'
    }
    foreach ($directory in @(
        $controllerEnvironment.TEMP,
        $controllerEnvironment.USERPROFILE,
        $controllerEnvironment.LOCALAPPDATA,
        $controllerEnvironment.APPDATA
    )) {
        if (-not (Test-PathContainedBy -Candidate $directory -Root $resolvedWorkspace)) {
            throw "W4 controller environment directory escaped its workspace: $directory"
        }
        Assert-SafePathChain -Path $directory -Label 'W4 controller environment directory'
        [void][System.IO.Directory]::CreateDirectory($directory)
        Assert-SafePathChain -Path $directory -Label 'W4 controller environment directory'
        Assert-SafeTree -Root $directory -Label 'W4 controller environment directory'
    }
    foreach ($entry in $controllerEnvironment.GetEnumerator()) {
        Add-ChildEnvironmentValue -ProcessInfo $processInfo -Name $entry.Key `
            -Value ([string]$entry.Value) -RepositoryRoot $RepositoryRoot
    }

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $processInfo
    $timedOut = $false
    try {
        if (-not $process.Start()) {
            throw 'W4 controller process did not start'
        }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($FullMatrixTimeoutSeconds * 1000)) {
            $timedOut = $true
            Stop-ProbeProcessTree -Process $process
        } else {
            $process.WaitForExit()
        }
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        [System.IO.File]::WriteAllText(
            (Join-Path $launcherEvidence 'controller-stdout.txt'),
            $stdout,
            [System.Text.UTF8Encoding]::new($false)
        )
        [System.IO.File]::WriteAllText(
            (Join-Path $launcherEvidence 'controller-stderr.txt'),
            $stderr,
            [System.Text.UTF8Encoding]::new($false)
        )
        if ($timedOut) {
            throw "W4 controller timed out after $FullMatrixTimeoutSeconds seconds"
        }
        $exitCode = [int]$process.ExitCode
        Assert-SafeTree -Root $resolvedDriverRoot -Label 'W4 driver bundle after execution'
        Assert-SafeTree -Root $resolvedFixture -Label 'W4 fixture bundle after execution'
        Assert-SafeTree -Root $resolvedHarness -Label 'W4 harness bundle after execution'
        Assert-SafeTree -Root $resolvedCandidateRoot -Label 'W4 candidate package after execution'
        Assert-SafeTree -Root $resolvedComplianceRoot -Label 'W4 compliance bundle after execution'
        Assert-SafeTree -Root $resolvedEvidence -Label 'W4 evidence root after execution'
        Assert-SafeTree -Root $resolvedWorkspace -Label 'W4 workspace root after execution'
        if ((Get-TreeManifestSha256 -Root $resolvedDriverRoot) -cne $controllerTreeShaBefore -or
            (Get-TreeManifestSha256 -Root $resolvedFixture) -cne $fixtureTreeShaBefore -or
            (Get-TreeManifestSha256 -Root $resolvedHarness) -cne $harnessTreeShaBefore -or
            (Get-TreeManifestSha256 -Root $resolvedCandidateRoot) -cne $candidateTreeShaBefore -or
            (Get-TreeManifestSha256 -Root $resolvedComplianceRoot) -cne $complianceTreeShaBefore) {
            throw 'W4 controller mutated an immutable driver/fixture/harness/candidate/compliance input tree'
        }
        $postconditionVerified = $false
        if ($exitCode -eq 0) {
            [void](Assert-FullMatrixPostcondition `
                -EvidenceRoot $resolvedEvidence -ExecutionId $RunId `
                -ArtifactId $artifactId -ArtifactSha256 $artifactSha `
                -ControllerSha256 $controllerTreeShaBefore `
                -FixtureSha256 $fixtureTreeShaBefore `
                -HarnessRuntimeSha256 ([string]$harnessProvenance.runtime_sha256) `
                -HarnessTreeSha256 $harnessTreeShaBefore `
                -CandidateTreeSha256 $candidateTreeShaBefore `
                -ComplianceTreeSha256 $complianceTreeShaBefore `
                -ArtifactProvenance $releaseProvenance `
                -ControllerRoot $resolvedDriverRoot -FixtureRoot $resolvedFixture `
                -HarnessRoot $resolvedHarness -CandidateRoot $resolvedCandidateRoot `
                -ComplianceRoot $resolvedComplianceRoot -WorkspaceRoot $resolvedWorkspace `
                -ExpectedScenarioIds $expectedScenarioIds `
                -ControllerStdout $stdout -ControllerStderr $stderr)
            $postconditionVerified = $true
        }
        $launcherResult = [ordered]@{
            schema_version = 'pkv.m13.w4-launcher-result.v1'
            runner_version = 'pkv.m13.artifact-runner.v2'
            execution_id = $RunId
            controller_exit_code = $exitCode
            timed_out = $false
            forced_termination = $false
            postcondition_verified = $postconditionVerified
            stdout_sha256 = (Get-LocalFileHash -LiteralPath (Join-Path $launcherEvidence 'controller-stdout.txt') -Algorithm SHA256).Hash.ToLowerInvariant()
            stderr_sha256 = (Get-LocalFileHash -LiteralPath (Join-Path $launcherEvidence 'controller-stderr.txt') -Algorithm SHA256).Hash.ToLowerInvariant()
        }
        $launcherJson = $launcherResult | ConvertTo-Json -Depth 6 -Compress
        [System.IO.File]::WriteAllText(
            (Join-Path $launcherEvidence 'launcher-result.json'),
            $launcherJson,
            [System.Text.UTF8Encoding]::new($false)
        )
        if (-not [string]::IsNullOrWhiteSpace($stdout)) {
            [Console]::Out.Write($stdout)
        }
        if (-not [string]::IsNullOrWhiteSpace($stderr)) {
            [Console]::Error.Write($stderr)
        }
        return $exitCode
    } finally {
        if (-not $process.HasExited) {
            Stop-ProbeProcessTree -Process $process
        }
        $process.Dispose()
    }
}

try {
    $repositoryRoot = Get-CanonicalExistingPath -Path (Join-Path $PSScriptRoot '..') -Kind Container
    if ($PSCmdlet.ParameterSetName -eq 'FullMatrix') {
        $fullMatrixExit = Invoke-FullArtifactMatrix -RepositoryRoot $repositoryRoot
        exit $fullMatrixExit
    }
    Assert-SafePathChain -Path $ArtifactRoot -Label 'Artifact root'
    Assert-SafePathChain -Path $EntryPoint -Label 'Artifact entrypoint'
    Assert-SafePathChain -Path $ManifestPath -Label 'Artifact manifest'
    Assert-SafePathChain -Path $FixturePath -Label 'Fixture'
    Assert-SafePathChain -Path $EvidenceRoot -Label 'Evidence root'
    $resolvedArtifactRoot = Get-CanonicalExistingPath -Path $ArtifactRoot -Kind Container
    $resolvedEntryPoint = Get-CanonicalExistingPath -Path $EntryPoint -Kind Leaf
    $resolvedManifest = Get-CanonicalExistingPath -Path $ManifestPath -Kind Leaf
    $resolvedFixture = Get-CanonicalExistingPath -Path $FixturePath -Kind Any
    $prospectiveEvidenceRoot = Get-CanonicalProspectivePath -Path $EvidenceRoot

    Assert-OutsideRepository -Path $resolvedArtifactRoot -Label 'Artifact root' -RepositoryRoot $repositoryRoot
    Assert-OutsideRepository -Path $resolvedEntryPoint -Label 'Artifact entrypoint' -RepositoryRoot $repositoryRoot
    Assert-OutsideRepository -Path $resolvedManifest -Label 'Artifact manifest' -RepositoryRoot $repositoryRoot
    Assert-OutsideRepository -Path $resolvedFixture -Label 'Fixture' -RepositoryRoot $repositoryRoot
    Assert-OutsideRepository -Path $prospectiveEvidenceRoot -Label 'Evidence root' -RepositoryRoot $repositoryRoot

    if (-not (Test-PathContainedBy -Candidate $resolvedEntryPoint -Root $resolvedArtifactRoot)) {
        throw 'Artifact entrypoint must be contained by the installed artifact root'
    }
    if (-not (Test-PathContainedBy -Candidate $resolvedManifest -Root $resolvedArtifactRoot)) {
        throw 'Artifact manifest must be contained by the installed artifact root'
    }
    Assert-DisjointPaths -First $resolvedArtifactRoot -FirstLabel 'Artifact root' -Second $prospectiveEvidenceRoot -SecondLabel 'Evidence root'
    Assert-DisjointPaths -First $resolvedFixture -FirstLabel 'Fixture' -Second $resolvedArtifactRoot -SecondLabel 'Artifact root'
    Assert-DisjointPaths -First $resolvedFixture -FirstLabel 'Fixture' -Second $prospectiveEvidenceRoot -SecondLabel 'Evidence root'

    try {
        $manifestText = [System.IO.File]::ReadAllText($resolvedManifest, [System.Text.Encoding]::UTF8)
        $parsedManifest = $manifestText | ConvertFrom-Json -ErrorAction Stop
        if ($null -eq $parsedManifest) {
            throw 'Manifest JSON must contain a value'
        }
    } catch {
        throw "Artifact manifest is not valid JSON: $resolvedManifest"
    }

    $resolvedHarness = $null
    if ($RequireHarness -and [string]::IsNullOrWhiteSpace($HarnessPath)) {
        throw 'This scenario requires an explicit external harness path'
    }
    if (-not [string]::IsNullOrWhiteSpace($HarnessPath)) {
        Assert-SafePathChain -Path $HarnessPath -Label 'Harness'
        $resolvedHarness = Get-CanonicalExistingPath -Path $HarnessPath -Kind Any
        Assert-OutsideRepository -Path $resolvedHarness -Label 'Harness' -RepositoryRoot $repositoryRoot
        Assert-DisjointPaths -First $resolvedHarness -FirstLabel 'Harness' -Second $resolvedArtifactRoot -SecondLabel 'Artifact root'
        Assert-DisjointPaths -First $resolvedHarness -FirstLabel 'Harness' -Second $prospectiveEvidenceRoot -SecondLabel 'Evidence root'
    }

    foreach ($argument in $ProbeArguments) {
        if (Test-TextContainsPath -Text $argument -Path $repositoryRoot) {
            throw 'Probe arguments must not contain the repository path'
        }
    }

    [void][System.IO.Directory]::CreateDirectory($prospectiveEvidenceRoot)
    Assert-SafePathChain -Path $prospectiveEvidenceRoot -Label 'Evidence root'
    $resolvedEvidenceRoot = Get-CanonicalExistingPath -Path $prospectiveEvidenceRoot -Kind Container
    Assert-SafeTree -Root $resolvedEvidenceRoot -Label 'Evidence root'
    Assert-OutsideRepository -Path $resolvedEvidenceRoot -Label 'Evidence root' -RepositoryRoot $repositoryRoot
    Assert-DisjointPaths -First $resolvedArtifactRoot -FirstLabel 'Artifact root' -Second $resolvedEvidenceRoot -SecondLabel 'Evidence root'

    $runRoot = Join-Path (Join-Path $resolvedEvidenceRoot 'runs') $RunId
    if (-not (Test-PathContainedBy -Candidate $runRoot -Root $resolvedEvidenceRoot)) {
        throw 'Run evidence directory escaped the evidence root'
    }
    Assert-SafePathChain -Path $runRoot -Label 'Run evidence directory'
    if (Test-Path -LiteralPath $runRoot) {
        throw "Run evidence directory already exists: $runRoot"
    }
    $workRoot = Join-Path $runRoot 'work'
    $tempRoot = Join-Path $workRoot 'tmp'
    $profileRoot = Join-Path $workRoot 'profile'
    $localAppDataRoot = Join-Path $profileRoot 'AppData\Local'
    $roamingAppDataRoot = Join-Path $profileRoot 'AppData\Roaming'
    foreach ($directory in @($workRoot, $tempRoot, $profileRoot, $localAppDataRoot, $roamingAppDataRoot)) {
        if (-not (Test-PathContainedBy -Candidate $directory -Root $runRoot)) {
            throw "Probe derived directory escaped the run evidence root: $directory"
        }
        Assert-SafePathChain -Path $directory -Label 'Probe derived directory'
        [void][System.IO.Directory]::CreateDirectory($directory)
        Assert-SafePathChain -Path $directory -Label 'Probe derived directory'
    }
    Assert-SafeTree -Root $runRoot -Label 'Run evidence directory'
    Assert-SafePathChain -Path $workRoot -Label 'Probe working directory'
    $resolvedWorkRoot = Get-CanonicalExistingPath -Path $workRoot -Kind Container
    Assert-OutsideRepository -Path $resolvedWorkRoot -Label 'Probe working directory' -RepositoryRoot $repositoryRoot
    if (-not (Test-PathContainedBy -Candidate $resolvedWorkRoot -Root $resolvedEvidenceRoot)) {
        throw 'Probe working directory must be contained by the evidence root'
    }
    if (Test-PathContainedBy -Candidate $resolvedWorkRoot -Root $resolvedArtifactRoot) {
        throw 'Probe working directory must not be contained by the artifact root'
    }

    $probeStatus = 'not_requested'
    $probeExitCode = $null
    if ($RunContractProbe) {
        $command = Get-ProbeCommand -ResolvedEntryPoint $resolvedEntryPoint -Arguments $ProbeArguments
        $commandPath = [string]$command.FileName
        [string[]]$commandArguments = @($command.Arguments)
        if (Test-TextContainsPath -Text $commandPath -Path $repositoryRoot) {
            throw 'Probe executable must not resolve inside the repository'
        }
        foreach ($argument in $commandArguments) {
            if (Test-TextContainsPath -Text $argument -Path $repositoryRoot) {
                throw 'Probe argv must not contain the repository path'
            }
        }

        $processInfo = [System.Diagnostics.ProcessStartInfo]::new()
        $processInfo.FileName = $commandPath
        $processInfo.Arguments = (($commandArguments | ForEach-Object { ConvertTo-WindowsCommandLineArgument -Value $_ }) -join ' ')
        $processInfo.WorkingDirectory = $resolvedWorkRoot
        $processInfo.UseShellExecute = $false
        $processInfo.CreateNoWindow = $true
        $processInfo.RedirectStandardOutput = $true
        $processInfo.RedirectStandardError = $true
        $processInfo.EnvironmentVariables.Clear()

        $systemRoot = [Environment]::GetEnvironmentVariable('SystemRoot')
        if ([string]::IsNullOrWhiteSpace($systemRoot)) {
            throw 'SystemRoot is unavailable for the isolated child environment'
        }
        $systemPath = @(
            (Join-Path $systemRoot 'System32'),
            (Join-Path $systemRoot 'System32\Wbem'),
            (Join-Path $systemRoot 'System32\WindowsPowerShell\v1.0')
        ) -join [System.IO.Path]::PathSeparator

        $probeOutput = Join-Path $runRoot 'contract-probe.json'
        $childEnvironment = [ordered]@{
            SystemRoot = $systemRoot
            WINDIR = $systemRoot
            COMSPEC = (Join-Path $systemRoot 'System32\cmd.exe')
            PATH = $systemPath
            PATHEXT = '.COM;.EXE;.BAT;.CMD'
            TEMP = $tempRoot
            TMP = $tempRoot
            TMPDIR = $tempRoot
            USERPROFILE = $profileRoot
            LOCALAPPDATA = $localAppDataRoot
            APPDATA = $roamingAppDataRoot
            PKV_ARTIFACT_ROOT = $resolvedArtifactRoot
            PKV_ARTIFACT_ENTRYPOINT = $resolvedEntryPoint
            PKV_ARTIFACT_MANIFEST = $resolvedManifest
            PKV_ARTIFACT_FIXTURE = $resolvedFixture
            PKV_ARTIFACT_EVIDENCE_ROOT = $resolvedEvidenceRoot
            PKV_ARTIFACT_WORK_ROOT = $resolvedWorkRoot
            PKV_ARTIFACT_PROBE_OUTPUT = $probeOutput
        }
        if ($null -cne $resolvedHarness) {
            $childEnvironment['PKV_ARTIFACT_HARNESS'] = $resolvedHarness
        }
        foreach ($item in $childEnvironment.GetEnumerator()) {
            Add-ChildEnvironmentValue -ProcessInfo $processInfo -Name $item.Key -Value ([string]$item.Value) -RepositoryRoot $repositoryRoot
        }

        $process = [System.Diagnostics.Process]::new()
        $process.StartInfo = $processInfo
        if (-not $process.Start()) {
            throw 'Contract probe process did not start'
        }
        $standardOutputTask = $process.StandardOutput.ReadToEndAsync()
        $standardErrorTask = $process.StandardError.ReadToEndAsync()
        $completed = $process.WaitForExit($ProbeTimeoutSeconds * 1000)
        if (-not $completed) {
            Stop-ProbeProcessTree -Process $process
            [void]$process.WaitForExit(5000)
        } else {
            # Flush asynchronous output events after the timed wait.
            $process.WaitForExit()
        }
        $standardOutput = $standardOutputTask.GetAwaiter().GetResult()
        $standardError = $standardErrorTask.GetAwaiter().GetResult()
        if (-not $completed) {
            [System.IO.File]::WriteAllText((Join-Path $runRoot 'probe-stdout.txt'), $standardOutput, [System.Text.UTF8Encoding]::new($false))
            [System.IO.File]::WriteAllText((Join-Path $runRoot 'probe-stderr.txt'), $standardError, [System.Text.UTF8Encoding]::new($false))
            throw "Contract probe timed out after $ProbeTimeoutSeconds seconds"
        }
        $probeExitCode = $process.ExitCode
        [System.IO.File]::WriteAllText((Join-Path $runRoot 'probe-stdout.txt'), $standardOutput, [System.Text.UTF8Encoding]::new($false))
        [System.IO.File]::WriteAllText((Join-Path $runRoot 'probe-stderr.txt'), $standardError, [System.Text.UTF8Encoding]::new($false))
        if ($probeExitCode -cne 0) {
            throw "Contract probe failed with exit code $probeExitCode"
        }
        $probeStatus = 'passed'
    }

    $status = if ($RunContractProbe) { 'contract_probe_passed' } else { 'preflight_passed' }
    $result = [ordered]@{
        schema_version = 'pkv.m13.artifact-preflight.v1'
        status = $status
        run_id = $RunId
        artifact_root = $resolvedArtifactRoot
        entrypoint = $resolvedEntryPoint
        manifest = $resolvedManifest
        fixture = $resolvedFixture
        harness = $resolvedHarness
        evidence_root = $resolvedEvidenceRoot
        working_directory = $resolvedWorkRoot
        contract_probe = [ordered]@{
            status = $probeStatus
            exit_code = $probeExitCode
        }
    }
    $resultJson = $result | ConvertTo-Json -Depth 6 -Compress
    $resultPath = Join-Path $runRoot 'preflight-result.json'
    [System.IO.File]::WriteAllText($resultPath, $resultJson, [System.Text.UTF8Encoding]::new($false))
    [Console]::Out.WriteLine($resultJson)
    exit 0
} catch {
    [Console]::Error.WriteLine("Artifact preflight failed: $($_.Exception.Message)")
    exit 1
}
