<#
.SYNOPSIS
Install Personal Knowledge Vault for the current Windows user.

.DESCRIPTION
The installer validates the release payload manifest before copying.  It does
not create, migrate, read or delete the user data root.  Cross-version in-place
upgrade is intentionally unsupported and returns exit code 20.
#>

[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter()]
    [string]$InstallRoot = "",

    [Parameter()]
    [switch]$AllowComplianceHoldTestCandidate,

    [Parameter()]
    [string]$ComplianceHoldConfirmation = ""
)

$ErrorActionPreference = "Stop"
$ComplianceHoldOverrideSpecified = (
    $PSBoundParameters.ContainsKey("AllowComplianceHoldTestCandidate") -or
    $PSBoundParameters.ContainsKey("ComplianceHoldConfirmation")
)
$ProductId = "personal-knowledge-vault"
$ExpectedBuildSchema = "pkv.build-info.v1"
$ExpectedManifestSchema = "pkv.payload-manifest.v1"
$ExpectedStateSchema = "pkv.install-state.v1"
$InstallMutexPrefix = "Local\PersonalKnowledgeVault-InstallRoot-"
$ComplianceHoldToken = "W4-TEST-CANDIDATE-NOT-FOR-DISTRIBUTION"
$ExpectedComplianceBlockers = @(
    "conda-native-license-materials-and-spdx",
    "html2text-gpl-compliance",
    "native-msvc-license-and-provenance",
    "qt-corresponding-source-location",
    "qt-linkage-and-replacement-not-proven",
    "qt-module-license-audit",
    "qt-notice-placeholders"
)
$PackageRoot = $PSScriptRoot
$utf8 = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

function Write-Result {
    param([Parameter(Mandatory = $true)][System.Collections.IDictionary]$Value)
    [Console]::Out.WriteLine((ConvertTo-Json -InputObject $Value -Compress))
}

function Get-NormalizedFullPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    return [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
}

function New-InstallRootMutex {
    param([Parameter(Mandatory = $true)][string]$Root)
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $mutexDigest = $sha256.ComputeHash(
            $utf8.GetBytes((Get-NormalizedFullPath $Root).ToLowerInvariant())
        )
        $mutexSuffix = ([System.BitConverter]::ToString($mutexDigest)).Replace("-", "")
    } finally {
        $sha256.Dispose()
    }
    return [System.Threading.Mutex]::new(
        $false,
        $InstallMutexPrefix + $mutexSuffix
    )
}

function Assert-ContainedPath {
    param(
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$Authority,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $candidatePath = Get-NormalizedFullPath $Candidate
    $authorityPath = Get-NormalizedFullPath $Authority
    $prefix = $authorityPath + [System.IO.Path]::DirectorySeparatorChar
    if (
        $candidatePath -eq $authorityPath -or
        -not $candidatePath.StartsWith(
            $prefix,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw "$Label must be a child of $authorityPath"
    }
    return $candidatePath
}

function Assert-NoUnsafeLinksUnderPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "links/reparse points are forbidden in install payload: $Path"
    }
    $linkType = $item.PSObject.Properties["LinkType"]
    if (-not $item.PSIsContainer -and $linkType -and $item.LinkType -eq "HardLink") {
        throw "hardlinks are forbidden in install payload: $Path"
    }
    if ($item.PSIsContainer) {
        foreach ($child in Get-ChildItem -LiteralPath $Path -Force) {
            Assert-NoUnsafeLinksUnderPath $child.FullName
        }
    }
}

function Assert-SafeExistingPathChain {
    param(
        [Parameter(Mandatory = $true)][string]$Authority,
        [Parameter(Mandatory = $true)][string]$Candidate
    )
    $authorityPath = Get-NormalizedFullPath $Authority
    $candidatePath = Get-NormalizedFullPath $Candidate
    $prefix = $authorityPath + [System.IO.Path]::DirectorySeparatorChar
    if (
        $candidatePath -ne $authorityPath -and
        -not $candidatePath.StartsWith(
            $prefix,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw "path chain escapes its authority: $candidatePath"
    }
    $current = $authorityPath
    $parts = if ($candidatePath -eq $authorityPath) {
        @()
    } else {
        $candidatePath.Substring($prefix.Length) -split '[\\/]'
    }
    foreach ($part in @(".") + $parts) {
        if ($part -ne ".") {
            $current = Join-Path $current $part
        }
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if (
                -not $item.PSIsContainer -or
                ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
            ) {
                throw "unsafe existing install path component: $current"
            }
        }
    }
}

function Read-JsonObject {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "required JSON file is missing: $Path"
    }
    return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Assert-ArtifactInstallEligibility {
    param([Parameter(Mandatory = $true)][object]$BuildInfo)
    $requiredProperties = @(
        "artifact_kind",
        "artifact_status",
        "release_eligible",
        "release_blockers"
    )
    $actualProperties = @($BuildInfo.PSObject.Properties.Name)
    foreach ($property in $requiredProperties) {
        if ($actualProperties -cnotcontains $property) {
            throw "build-info is missing release eligibility field: $property"
        }
    }
    if ($BuildInfo.release_eligible -isnot [bool]) {
        throw "build-info release_eligible must be a JSON boolean"
    }
    if (
        $BuildInfo.artifact_kind -isnot [string] -or
        $BuildInfo.artifact_status -isnot [string] -or
        $null -eq $BuildInfo.release_blockers -or
        $BuildInfo.release_blockers -isnot [System.Array]
    ) {
        throw "build-info release eligibility fields have invalid JSON types"
    }

    $artifactKind = [string]$BuildInfo.artifact_kind
    $artifactStatus = [string]$BuildInfo.artifact_status
    $releaseEligible = [bool]$BuildInfo.release_eligible
    $releaseBlockers = @($BuildInfo.release_blockers)
    $isRelease = (
        $releaseEligible -and
        $artifactKind -ceq "release" -and
        $artifactStatus -ceq "release" -and
        $releaseBlockers.Count -eq 0
    )
    if ($isRelease) {
        if ($ComplianceHoldOverrideSpecified) {
            throw "compliance-hold override arguments are forbidden for a release Artifact"
        }
        return [ordered]@{
            artifact_kind = "release"
            artifact_status = "release"
            release_eligible = $true
            release_blockers = @()
            compliance_hold = $false
        }
    }

    $expectedBlockersJson = ConvertTo-Json `
        -InputObject @($ExpectedComplianceBlockers) `
        -Compress
    $actualBlockersJson = ConvertTo-Json -InputObject @($releaseBlockers) -Compress
    $isFrozenTestCandidate = (
        -not $releaseEligible -and
        $artifactKind -ceq "test_candidate" -and
        $artifactStatus -ceq "test-candidate-on-compliance-hold" -and
        $actualBlockersJson -ceq $expectedBlockersJson
    )
    if (-not $isFrozenTestCandidate) {
        throw "build-info release eligibility tuple is invalid"
    }
    if (
        -not $AllowComplianceHoldTestCandidate -or
        $ComplianceHoldConfirmation -cne $ComplianceHoldToken
    ) {
        throw (
            "compliance-held test candidate is not installable by default; " +
            "W4 requires -AllowComplianceHoldTestCandidate and the exact confirmation token"
        )
    }
    return [ordered]@{
        artifact_kind = "test_candidate"
        artifact_status = "test-candidate-on-compliance-hold"
        release_eligible = $false
        release_blockers = @($ExpectedComplianceBlockers)
        compliance_hold = $true
    }
}

function Get-Sha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = [System.IO.File]::Open(
        $Path,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::Read
    )
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $digest = $sha256.ComputeHash($stream)
        return ([System.BitConverter]::ToString($digest)).Replace("-", "").ToLowerInvariant()
    } finally {
        $sha256.Dispose()
        $stream.Dispose()
    }
}

function Assert-Payload {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][object]$Manifest,
        [string[]]$AllowedExtra = @()
    )
    if ($Manifest.schema_version -ne $ExpectedManifestSchema) {
        throw "unsupported payload manifest schema"
    }
    Assert-NoUnsafeLinksUnderPath $Root
    $normalizedRoot = Get-NormalizedFullPath $Root
    $rootPrefix = $normalizedRoot + [System.IO.Path]::DirectorySeparatorChar
    $expected = @{}
    foreach ($entry in @($Manifest.entries)) {
        $relative = [string]$entry.path
        if (
            [System.IO.Path]::IsPathRooted($relative) -or
            $relative.Contains(":") -or
            $relative -match '(^|[\\/])\.\.([\\/]|$)'
        ) {
            throw "unsafe manifest path: $relative"
        }
        $candidate = Get-NormalizedFullPath (Join-Path $Root $relative)
        if (-not $candidate.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "manifest path escapes payload root: $relative"
        }
        $key = $relative.Replace('\', '/').ToLowerInvariant()
        if ($expected.ContainsKey($key)) {
            throw "duplicate manifest path: $relative"
        }
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            throw "manifest file is missing: $relative"
        }
        $item = Get-Item -LiteralPath $candidate -Force
        if ([int64]$entry.size -ne [int64]$item.Length) {
            throw "manifest size mismatch: $relative"
        }
        $actualHash = Get-Sha256 $candidate
        if ($actualHash -ne ([string]$entry.sha256).ToLowerInvariant()) {
            throw "manifest hash mismatch: $relative"
        }
        $expected[$key] = $true
    }
    $extras = @{}
    foreach ($relative in $AllowedExtra) {
        $extras[$relative.Replace('\', '/').ToLowerInvariant()] = $true
    }
    foreach ($file in Get-ChildItem -LiteralPath $Root -Recurse -File -Force) {
        $relative = $file.FullName.Substring($rootPrefix.Length).Replace('\', '/')
        $key = $relative.ToLowerInvariant()
        if (-not $expected.ContainsKey($key) -and -not $extras.ContainsKey($key)) {
            throw "unlisted payload file: $relative"
        }
    }
}

function Complete-ExistingInstall {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][object]$PackageBuildInfo,
        [Parameter(Mandatory = $true)][string]$PackageManifestHash,
        [Parameter(Mandatory = $true)][string]$ExpectedUserDataRoot,
        [Parameter(Mandatory = $true)][System.Collections.IDictionary]$ArtifactEligibility
    )
    Assert-NoUnsafeLinksUnderPath $Root
    $statePath = Join-Path $Root "install-state.json"
    $state = Read-JsonObject $statePath
    if (
        $state.schema_version -ne $ExpectedStateSchema -or
        $state.product_id -ne $ProductId -or
        (Get-NormalizedFullPath ([string]$state.install_root)) -ne $Root -or
        (Get-NormalizedFullPath ([string]$state.user_data_root)) -ne $ExpectedUserDataRoot -or
        [string]$state.artifact_kind -cne [string]$ArtifactEligibility.artifact_kind -or
        [string]$state.artifact_status -cne [string]$ArtifactEligibility.artifact_status -or
        $state.release_eligible -isnot [bool] -or
        [bool]$state.release_eligible -ne [bool]$ArtifactEligibility.release_eligible -or
        (ConvertTo-Json -InputObject @($state.release_blockers) -Compress) -cne
            (ConvertTo-Json -InputObject @($ArtifactEligibility.release_blockers) -Compress)
    ) {
        throw "existing install state is invalid"
    }
    if ([string]$state.version -ne [string]$PackageBuildInfo.version) {
        Write-Result ([ordered]@{
            schema_version = "pkv.install-result.v1"
            status = "upgrade_unsupported"
            installed_version = [string]$state.version
            package_version = [string]$PackageBuildInfo.version
            install_root = $Root
            artifact_kind = [string]$ArtifactEligibility.artifact_kind
            artifact_status = [string]$ArtifactEligibility.artifact_status
            release_eligible = [bool]$ArtifactEligibility.release_eligible
            release_blockers = @($ArtifactEligibility.release_blockers)
            compliance_hold = [bool]$ArtifactEligibility.compliance_hold
        })
        exit 20
    }
    $installedManifestPath = Join-Path $Root "payload-manifest.json"
    $installedManifestHash = Get-Sha256 $installedManifestPath
    if (
        $installedManifestHash -ne $PackageManifestHash -or
        ([string]$state.payload_manifest_sha256).ToLowerInvariant() -ne $installedManifestHash -or
        [string]$state.build_fingerprint -ne [string]$PackageBuildInfo.build_fingerprint
    ) {
        throw "same-version install payload differs; automatic repair is unsupported"
    }
    $installedManifest = Read-JsonObject $installedManifestPath
    Assert-Payload `
        -Root $Root `
        -Manifest $installedManifest `
        -AllowedExtra @("payload-manifest.json", "install-state.json")
    Write-Result ([ordered]@{
        schema_version = "pkv.install-result.v1"
        status = "already_installed"
        version = [string]$PackageBuildInfo.version
        install_root = $Root
        user_data_root = $ExpectedUserDataRoot
        artifact_kind = [string]$ArtifactEligibility.artifact_kind
        artifact_status = [string]$ArtifactEligibility.artifact_status
        release_eligible = [bool]$ArtifactEligibility.release_eligible
        release_blockers = @($ArtifactEligibility.release_blockers)
        compliance_hold = [bool]$ArtifactEligibility.compliance_hold
    })
    exit 0
}

try {
    if (-not $env:LOCALAPPDATA) {
        throw "LOCALAPPDATA is required for per-user installation"
    }
    $allowedProgramsRoot = Get-NormalizedFullPath (Join-Path $env:LOCALAPPDATA "Programs")
    if (-not $InstallRoot) {
        $InstallRoot = Join-Path $allowedProgramsRoot "PersonalKnowledgeVault"
    }
    # Normalize without touching the filesystem so this script and the
    # uninstaller derive the same mutex name before either mutates the root.
    $InstallRoot = Get-NormalizedFullPath $InstallRoot
    $userDataRoot = Get-NormalizedFullPath (
        Join-Path $env:LOCALAPPDATA "PersonalKnowledgeVault"
    )
    $manifestPath = Join-Path $PackageRoot "payload-manifest.json"
    $buildInfoPath = Join-Path $PackageRoot "build-info.json"
    $manifest = Read-JsonObject $manifestPath
    $buildInfo = Read-JsonObject $buildInfoPath
    if ($buildInfo.schema_version -ne $ExpectedBuildSchema) {
        throw "unsupported build-info schema"
    }
    $artifactEligibility = Assert-ArtifactInstallEligibility $buildInfo
    $installMutex = New-InstallRootMutex $InstallRoot
    $mutexAcquired = $false
    try {
        try {
            $mutexAcquired = $installMutex.WaitOne([TimeSpan]::FromMinutes(2))
        } catch [System.Threading.AbandonedMutexException] {
            $mutexAcquired = $true
        }
        if (-not $mutexAcquired) {
            throw "timed out waiting for another installer instance"
        }

        # Revalidate every destination authority and path component while the
        # root mutex is held.  The existing-install decision and every exit
        # after it remain inside this critical section.
        $allowedProgramsRoot = Get-NormalizedFullPath (
            Join-Path $env:LOCALAPPDATA "Programs"
        )
        $InstallRoot = Assert-ContainedPath `
            -Candidate $InstallRoot `
            -Authority $allowedProgramsRoot `
            -Label "install root"
        $userDataRoot = Get-NormalizedFullPath (
            Join-Path $env:LOCALAPPDATA "PersonalKnowledgeVault"
        )
        if (
            $InstallRoot.StartsWith(
                $userDataRoot + [System.IO.Path]::DirectorySeparatorChar,
                [System.StringComparison]::OrdinalIgnoreCase
            ) -or
            $userDataRoot.StartsWith(
                $InstallRoot + [System.IO.Path]::DirectorySeparatorChar,
                [System.StringComparison]::OrdinalIgnoreCase
            )
        ) {
            throw "program root and user data root must be separate"
        }
        Assert-SafeExistingPathChain `
            -Authority $env:LOCALAPPDATA `
            -Candidate $allowedProgramsRoot
        if (-not (Test-Path -LiteralPath $allowedProgramsRoot)) {
            [void][System.IO.Directory]::CreateDirectory($allowedProgramsRoot)
        }
        Assert-SafeExistingPathChain `
            -Authority $env:LOCALAPPDATA `
            -Candidate $allowedProgramsRoot
        Assert-SafeExistingPathChain `
            -Authority $allowedProgramsRoot `
            -Candidate $InstallRoot

        Assert-Payload `
            -Root $PackageRoot `
            -Manifest $manifest `
            -AllowedExtra @("payload-manifest.json")
        $packageManifestHash = Get-Sha256 $manifestPath

        if (Test-Path -LiteralPath $InstallRoot) {
            Complete-ExistingInstall `
                -Root $InstallRoot `
                -PackageBuildInfo $buildInfo `
                -PackageManifestHash $packageManifestHash `
                -ExpectedUserDataRoot $userDataRoot `
                -ArtifactEligibility $artifactEligibility
        }

        $stage = Assert-ContainedPath `
            -Candidate (Join-Path $allowedProgramsRoot (
                ".pkv-install-" + [Guid]::NewGuid().ToString("N")
            )) `
            -Authority $allowedProgramsRoot `
            -Label "install stage"
        Assert-SafeExistingPathChain `
            -Authority $allowedProgramsRoot `
            -Candidate $stage
        if (Test-Path -LiteralPath $stage) {
            throw "install stage already exists"
        }
        [void][System.IO.Directory]::CreateDirectory($stage)
        Assert-SafeExistingPathChain `
            -Authority $allowedProgramsRoot `
            -Candidate $stage
        try {
            foreach ($child in Get-ChildItem -LiteralPath $PackageRoot -Force) {
                Copy-Item -LiteralPath $child.FullName -Destination $stage -Recurse -Force
            }
            Assert-Payload `
                -Root $stage `
                -Manifest $manifest `
                -AllowedExtra @("payload-manifest.json")
            if (
                (Get-Sha256 (Join-Path $stage "payload-manifest.json")) -ne
                $packageManifestHash
            ) {
                throw "staged payload manifest differs from validated package manifest"
            }
            $state = [ordered]@{
                schema_version = $ExpectedStateSchema
                product_id = $ProductId
                version = [string]$buildInfo.version
                build_fingerprint = [string]$buildInfo.build_fingerprint
                payload_manifest_sha256 = $packageManifestHash
                install_root = $InstallRoot
                user_data_root = $userDataRoot
                artifact_kind = [string]$artifactEligibility.artifact_kind
                artifact_status = [string]$artifactEligibility.artifact_status
                release_eligible = [bool]$artifactEligibility.release_eligible
                release_blockers = @($artifactEligibility.release_blockers)
            }
            [System.IO.File]::WriteAllText(
                (Join-Path $stage "install-state.json"),
                (ConvertTo-Json -InputObject $state -Compress) + "`n",
                $utf8
            )
            try {
                Assert-SafeExistingPathChain `
                    -Authority $allowedProgramsRoot `
                    -Candidate $InstallRoot
                [System.IO.Directory]::Move($stage, $InstallRoot)
            } catch {
                if (Test-Path -LiteralPath $InstallRoot) {
                    Complete-ExistingInstall `
                        -Root $InstallRoot `
                        -PackageBuildInfo $buildInfo `
                        -PackageManifestHash $packageManifestHash `
                        -ExpectedUserDataRoot $userDataRoot `
                        -ArtifactEligibility $artifactEligibility
                }
                throw
            }
        } finally {
            if (Test-Path -LiteralPath $stage) {
                [void](Assert-ContainedPath `
                    -Candidate $stage `
                    -Authority $allowedProgramsRoot `
                    -Label "install stage")
                Assert-SafeExistingPathChain `
                    -Authority $allowedProgramsRoot `
                    -Candidate $stage
                Assert-NoUnsafeLinksUnderPath $stage
                Remove-Item -LiteralPath $stage -Recurse -Force
            }
        }
        Write-Result ([ordered]@{
            schema_version = "pkv.install-result.v1"
            status = "installed"
            version = [string]$buildInfo.version
            install_root = $InstallRoot
            user_data_root = $userDataRoot
            artifact_kind = [string]$artifactEligibility.artifact_kind
            artifact_status = [string]$artifactEligibility.artifact_status
            release_eligible = [bool]$artifactEligibility.release_eligible
            release_blockers = @($artifactEligibility.release_blockers)
            compliance_hold = [bool]$artifactEligibility.compliance_hold
        })
        exit 0
    } finally {
        if ($mutexAcquired) {
            [void]$installMutex.ReleaseMutex()
        }
        $installMutex.Dispose()
    }
} catch {
    [Console]::Error.WriteLine("PKV install failed: " + $_.Exception.Message)
    exit 1
}
