<#
.SYNOPSIS
Fail-closed preflight for the explicit M13 artifact-only lane.

.DESCRIPTION
This script validates an already installed artifact without importing or
executing the repository source tree.  It may optionally launch the supplied
artifact entrypoint as a contract probe from an isolated directory outside the
repository.  It deliberately does not make a release-verification decision.
#>

[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ArtifactRoot,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$EntryPoint,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ManifestPath,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$FixturePath,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$EvidenceRoot,

    [Parameter()]
    [string]$HarnessPath,

    [Parameter()]
    [switch]$RequireHarness,

    [Parameter()]
    [switch]$RunContractProbe,

    [Parameter()]
    [string[]]$ProbeArguments = @(),

    [Parameter()]
    [ValidateRange(1, 300)]
    [int]$ProbeTimeoutSeconds = 30,

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

    if ($Value -notmatch '[\s"]') {
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

    if ([Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
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
        if ($process.ExitCode -ne 0) {
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
    if ($links.Count -ne 1 -or -not $links[0].StartsWith('\')) {
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

        if ($null -ne $item) {
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

try {
    $repositoryRoot = Get-CanonicalExistingPath -Path (Join-Path $PSScriptRoot '..') -Kind Container
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
    Assert-OutsideRepository -Path $resolvedEvidenceRoot -Label 'Evidence root' -RepositoryRoot $repositoryRoot
    Assert-DisjointPaths -First $resolvedArtifactRoot -FirstLabel 'Artifact root' -Second $resolvedEvidenceRoot -SecondLabel 'Evidence root'

    $runRoot = Join-Path (Join-Path $resolvedEvidenceRoot 'runs') $RunId
    if (Test-Path -LiteralPath $runRoot) {
        throw "Run evidence directory already exists: $runRoot"
    }
    $workRoot = Join-Path $runRoot 'work'
    $tempRoot = Join-Path $workRoot 'tmp'
    $profileRoot = Join-Path $workRoot 'profile'
    $localAppDataRoot = Join-Path $profileRoot 'AppData\Local'
    $roamingAppDataRoot = Join-Path $profileRoot 'AppData\Roaming'
    foreach ($directory in @($workRoot, $tempRoot, $profileRoot, $localAppDataRoot, $roamingAppDataRoot)) {
        [void][System.IO.Directory]::CreateDirectory($directory)
    }
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
        if ($null -ne $resolvedHarness) {
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
        if ($probeExitCode -ne 0) {
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
