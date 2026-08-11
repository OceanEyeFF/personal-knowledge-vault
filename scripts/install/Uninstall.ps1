<#
.SYNOPSIS
Uninstall Personal Knowledge Vault from the current Windows user.

.DESCRIPTION
Program files are removed after manifest verification.  User data is retained
by default.  Deletion requires both -DeleteUserData and the exact confirmation
token DELETE-PKV-USER-DATA.
#>

[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter()]
    [string]$InstallRoot = "",

    [Parameter()]
    [switch]$DeleteUserData,

    [Parameter()]
    [string]$ConfirmDataDeletion = ""
)

$ErrorActionPreference = "Stop"
$ProductId = "personal-knowledge-vault"
$ExpectedManifestSchema = "pkv.payload-manifest.v1"
$ExpectedStateSchema = "pkv.install-state.v1"
$ConfirmationToken = "DELETE-PKV-USER-DATA"
$InstallMutexPrefix = "Local\PersonalKnowledgeVault-InstallRoot-"
$UserDataMutexPrefix = "Local\PersonalKnowledgeVault-UserDataRoot-"
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

function New-UserDataRootMutex {
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
        $UserDataMutexPrefix + $mutexSuffix
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
        throw "links/reparse points are forbidden under uninstall targets: $Path"
    }
    $linkType = $item.PSObject.Properties["LinkType"]
    if (-not $item.PSIsContainer -and $linkType -and $item.LinkType -eq "HardLink") {
        throw "hardlinks are forbidden under uninstall targets: $Path"
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
                throw "unsafe existing uninstall path component: $current"
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

function Assert-InstalledPayload {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][object]$Manifest
    )
    if ($Manifest.schema_version -ne $ExpectedManifestSchema) {
        throw "unsupported payload manifest schema"
    }
    Assert-NoUnsafeLinksUnderPath $Root
    $normalizedRoot = Get-NormalizedFullPath $Root
    $rootPrefix = $normalizedRoot + [System.IO.Path]::DirectorySeparatorChar
    $expected = @{
        "payload-manifest.json" = $true
        "install-state.json" = $true
    }
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
            throw "manifest path escapes install root: $relative"
        }
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            throw "installed manifest file is missing: $relative"
        }
        $actualHash = Get-Sha256 $candidate
        if (
            $actualHash -ne ([string]$entry.sha256).ToLowerInvariant() -or
            [int64](Get-Item -LiteralPath $candidate).Length -ne [int64]$entry.size
        ) {
            throw "installed payload differs from manifest: $relative"
        }
        $key = $relative.Replace('\', '/').ToLowerInvariant()
        if ($expected.ContainsKey($key)) {
            throw "duplicate installed manifest path: $relative"
        }
        $expected[$key] = $true
    }
    foreach ($file in Get-ChildItem -LiteralPath $Root -Recurse -File -Force) {
        $relative = $file.FullName.Substring($rootPrefix.Length).Replace('\', '/').ToLowerInvariant()
        if (-not $expected.ContainsKey($relative)) {
            throw "unlisted installed file blocks safe uninstall: $relative"
        }
    }
}

try {
    if (-not $env:LOCALAPPDATA) {
        throw "LOCALAPPDATA is required for per-user uninstall"
    }
    $localAppData = Get-NormalizedFullPath $env:LOCALAPPDATA
    $allowedProgramsRoot = Get-NormalizedFullPath (Join-Path $localAppData "Programs")
    if (-not $InstallRoot) {
        $InstallRoot = Join-Path $allowedProgramsRoot "PersonalKnowledgeVault"
    }
    $InstallRoot = Get-NormalizedFullPath $InstallRoot

    if ($DeleteUserData -or $ConfirmDataDeletion) {
        if (-not $DeleteUserData -or $ConfirmDataDeletion -ne $ConfirmationToken) {
            throw "user data deletion requires -DeleteUserData and exact confirmation token"
        }
    }

    $uninstallMutex = New-InstallRootMutex $InstallRoot
    $mutexAcquired = $false
    $dataMutex = $null
    $dataMutexAcquired = $false
    try {
        try {
            $mutexAcquired = $uninstallMutex.WaitOne([TimeSpan]::FromMinutes(2))
        } catch [System.Threading.AbandonedMutexException] {
            $mutexAcquired = $true
        }
        if (-not $mutexAcquired) {
            throw "timed out waiting for another installer or uninstaller instance"
        }

        # Fixed global lock order: InstallRoot first, canonical UserDataRoot
        # second.  Different custom program roots therefore cannot race while
        # detaching the one per-user data root.
        if ($DeleteUserData) {
            $dataMutexRoot = Get-NormalizedFullPath (
                Join-Path $env:LOCALAPPDATA "PersonalKnowledgeVault"
            )
            $dataMutex = New-UserDataRootMutex $dataMutexRoot
            try {
                $dataMutexAcquired = $dataMutex.WaitOne([TimeSpan]::FromMinutes(2))
            } catch [System.Threading.AbandonedMutexException] {
                $dataMutexAcquired = $true
            }
            if (-not $dataMutexAcquired) {
                throw "timed out waiting for another user data deletion"
            }
        }

        # Recompute authorities and revalidate containment/path components only
        # after acquiring the exact same InstallRoot mutex as Install.ps1.
        $localAppData = Get-NormalizedFullPath $env:LOCALAPPDATA
        $allowedProgramsRoot = Get-NormalizedFullPath (
            Join-Path $localAppData "Programs"
        )
        $InstallRoot = Assert-ContainedPath `
            -Candidate $InstallRoot `
            -Authority $allowedProgramsRoot `
            -Label "install root"
        Assert-SafeExistingPathChain `
            -Authority $localAppData `
            -Candidate $allowedProgramsRoot
        Assert-SafeExistingPathChain `
            -Authority $allowedProgramsRoot `
            -Candidate $InstallRoot
        if (-not (Test-Path -LiteralPath $InstallRoot -PathType Container)) {
            throw "install root does not exist"
        }
        Assert-NoUnsafeLinksUnderPath $InstallRoot

        $state = Read-JsonObject (Join-Path $InstallRoot "install-state.json")
        if (
            $state.schema_version -ne $ExpectedStateSchema -or
            $state.product_id -ne $ProductId
        ) {
            throw "install state is invalid"
        }
        if ((Get-NormalizedFullPath ([string]$state.install_root)) -ne $InstallRoot) {
            throw "install state root does not match requested root"
        }
        $manifestPath = Join-Path $InstallRoot "payload-manifest.json"
        if (
            (Get-Sha256 $manifestPath) -ne
            ([string]$state.payload_manifest_sha256).ToLowerInvariant()
        ) {
            throw "installed payload manifest differs from install state"
        }
        $manifest = Read-JsonObject $manifestPath
        Assert-InstalledPayload -Root $InstallRoot -Manifest $manifest

        $expectedDataRoot = Get-NormalizedFullPath (
            Join-Path $localAppData "PersonalKnowledgeVault"
        )
        $stateDataRoot = Get-NormalizedFullPath ([string]$state.user_data_root)
        if ($stateDataRoot -ne $expectedDataRoot) {
            throw "install state user data root is outside the current user contract"
        }
        if ($DeleteUserData) {
            [void](Assert-ContainedPath `
                -Candidate $stateDataRoot `
                -Authority $localAppData `
                -Label "user data root")
            Assert-SafeExistingPathChain `
                -Authority $localAppData `
                -Candidate $stateDataRoot
            if (Test-Path -LiteralPath $stateDataRoot) {
                Assert-NoUnsafeLinksUnderPath $stateDataRoot
            }
        }

        # Prepare both tombstones before mutating either target.  With data
        # deletion enabled, permanent cleanup starts only after both atomic
        # detaches have succeeded.
        $dataTombstone = $null
        $dataExists = $false
        if ($DeleteUserData) {
            $expectedDataRoot = Get-NormalizedFullPath (
                Join-Path $localAppData "PersonalKnowledgeVault"
            )
            $stateDataRoot = Get-NormalizedFullPath ([string]$state.user_data_root)
            if ($stateDataRoot -ne $expectedDataRoot) {
                throw "user data delete target changed from the exact allowed root"
            }
            [void](Assert-ContainedPath `
                -Candidate $stateDataRoot `
                -Authority $localAppData `
                -Label "user data root")
            Assert-SafeExistingPathChain `
                -Authority $localAppData `
                -Candidate $stateDataRoot
            $dataExists = Test-Path -LiteralPath $stateDataRoot
            if ($dataExists) {
                Assert-NoUnsafeLinksUnderPath $stateDataRoot
                $dataTombstone = Assert-ContainedPath `
                    -Candidate (Join-Path $localAppData (
                        ".pkv-data-delete-" + [Guid]::NewGuid().ToString("N")
                    )) `
                    -Authority $localAppData `
                    -Label "user data tombstone"
                Assert-SafeExistingPathChain `
                    -Authority $localAppData `
                    -Candidate $dataTombstone
                if (Test-Path -LiteralPath $dataTombstone) {
                    throw "user data tombstone already exists"
                }
            }
        }

        [void](Assert-ContainedPath `
            -Candidate $InstallRoot `
            -Authority $allowedProgramsRoot `
            -Label "install root")
        Assert-SafeExistingPathChain `
            -Authority $allowedProgramsRoot `
            -Candidate $InstallRoot
        Assert-NoUnsafeLinksUnderPath $InstallRoot
        Assert-InstalledPayload -Root $InstallRoot -Manifest $manifest

        Set-Location -LiteralPath ([System.IO.Path]::GetTempPath())
        $programTombstone = Assert-ContainedPath `
            -Candidate (Join-Path $allowedProgramsRoot (
                ".pkv-uninstall-" + [Guid]::NewGuid().ToString("N")
            )) `
            -Authority $allowedProgramsRoot `
            -Label "program tombstone"
        Assert-SafeExistingPathChain `
            -Authority $allowedProgramsRoot `
            -Candidate $programTombstone
        if (Test-Path -LiteralPath $programTombstone) {
            throw "program tombstone already exists"
        }

        # Final program scan immediately precedes its atomic detach.
        Assert-SafeExistingPathChain `
            -Authority $allowedProgramsRoot `
            -Candidate $InstallRoot
        Assert-NoUnsafeLinksUnderPath $InstallRoot
        Assert-InstalledPayload -Root $InstallRoot -Manifest $manifest
        [System.IO.Directory]::Move($InstallRoot, $programTombstone)
        $programDetached = $true

        $dataDetached = $false
        if ($DeleteUserData -and $dataExists) {
            try {
                # Recheck the exact root and link contract at the final point of
                # use while both the program and data mutexes are held.
                $expectedDataRoot = Get-NormalizedFullPath (
                    Join-Path $localAppData "PersonalKnowledgeVault"
                )
                $stateDataRoot = Get-NormalizedFullPath ([string]$state.user_data_root)
                if ($stateDataRoot -ne $expectedDataRoot) {
                    throw "user data delete target changed from the exact allowed root"
                }
                [void](Assert-ContainedPath `
                    -Candidate $stateDataRoot `
                    -Authority $localAppData `
                    -Label "user data root")
                Assert-SafeExistingPathChain `
                    -Authority $localAppData `
                    -Candidate $stateDataRoot
                Assert-NoUnsafeLinksUnderPath $stateDataRoot
                [System.IO.Directory]::Move($stateDataRoot, $dataTombstone)
                $dataDetached = $true
            } catch {
                $dataDetachError = $_.Exception.Message
                try {
                    if (Test-Path -LiteralPath $InstallRoot) {
                        throw "install root was recreated before rollback"
                    }
                    Assert-SafeExistingPathChain `
                        -Authority $allowedProgramsRoot `
                        -Candidate $InstallRoot
                    Assert-NoUnsafeLinksUnderPath $programTombstone
                    [System.IO.Directory]::Move($programTombstone, $InstallRoot)
                    $programDetached = $false
                } catch {
                    Write-Result ([ordered]@{
                        schema_version = "pkv.uninstall-result.v1"
                        status = "partial_detach_rollback_failed"
                        version = [string]$state.version
                        install_root = $InstallRoot
                        user_data_root = $stateDataRoot
                        program_tombstone = $programTombstone
                        data_tombstone = $null
                        error = $dataDetachError
                        rollback_error = $_.Exception.Message
                    })
                    [Console]::Error.WriteLine(
                        "PKV uninstall partially detached program files and rollback failed"
                    )
                    exit 1
                }
                throw "user data detach failed; program rollback completed: $dataDetachError"
            }
        }

        $cleanupErrors = @()
        if ($programDetached) {
            try {
                Assert-NoUnsafeLinksUnderPath $programTombstone
                Remove-Item -LiteralPath $programTombstone -Recurse -Force
            } catch {
                $cleanupErrors += "program cleanup failed: $($_.Exception.Message)"
            }
        }
        if ($dataDetached) {
            try {
                Assert-NoUnsafeLinksUnderPath $dataTombstone
                Remove-Item -LiteralPath $dataTombstone -Recurse -Force
            } catch {
                $cleanupErrors += "user data cleanup failed: $($_.Exception.Message)"
            }
        }
        if ($cleanupErrors.Count -gt 0) {
            $remainingTombstones = @()
            if (Test-Path -LiteralPath $programTombstone) {
                $remainingTombstones += $programTombstone
            }
            if ($null -ne $dataTombstone -and (Test-Path -LiteralPath $dataTombstone)) {
                $remainingTombstones += $dataTombstone
            }
            Write-Result ([ordered]@{
                schema_version = "pkv.uninstall-result.v1"
                status = "partial_cleanup_failed"
                version = [string]$state.version
                install_root = $InstallRoot
                user_data_root = $stateDataRoot
                remaining_tombstones = @($remainingTombstones)
                errors = @($cleanupErrors)
            })
            [Console]::Error.WriteLine("PKV uninstall detached targets but cleanup failed")
            exit 1
        }

        $dataStatus = if ($DeleteUserData) { "deleted" } else { "retained" }
        Write-Result ([ordered]@{
            schema_version = "pkv.uninstall-result.v1"
            status = "uninstalled"
            version = [string]$state.version
            install_root = $InstallRoot
            user_data_root = $stateDataRoot
            user_data = $dataStatus
            artifact_kind = [string]$state.artifact_kind
            artifact_status = [string]$state.artifact_status
            release_eligible = [bool]$state.release_eligible
            release_blockers = @($state.release_blockers)
            compliance_hold = (
                [string]$state.artifact_kind -ceq "test_candidate" -and
                -not [bool]$state.release_eligible
            )
        })
        exit 0
    } finally {
        if ($dataMutexAcquired) {
            [void]$dataMutex.ReleaseMutex()
        }
        if ($null -ne $dataMutex) {
            $dataMutex.Dispose()
        }
        if ($mutexAcquired) {
            [void]$uninstallMutex.ReleaseMutex()
        }
        $uninstallMutex.Dispose()
    }
} catch {
    [Console]::Error.WriteLine("PKV uninstall failed: " + $_.Exception.Message)
    exit 1
}
