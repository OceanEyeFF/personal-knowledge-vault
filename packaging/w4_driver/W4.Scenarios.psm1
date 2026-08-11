#requires -Version 5.1

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-W4ScenarioSlug {
    param([Parameter(Mandatory = $true)][string]$ScenarioId)
    return ($ScenarioId -replace '[^A-Za-z0-9._-]', '_')
}

function Get-W4CanonicalDistributionName {
    param([Parameter(Mandatory = $true)][string]$Value)
    return (($Value -replace '[-_.]+', '-').ToLowerInvariant())
}

function Assert-W4DistributionOwnerBinding {
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

    $canonicalName = Get-W4CanonicalDistributionName -Value $DistributionName
    if ([string]::IsNullOrWhiteSpace($DistributionName) -or
        $DistributionName -cne $canonicalName) {
        throw "$Label distribution name is not canonical: $DistributionName"
    }
    $components = @($ComponentIds | ForEach-Object { [string]$_ })
    $genericId = "python-distribution:$canonicalName"
    $foldedSourceRef = "/$($SourceRef.Replace([char]92, '/').ToLowerInvariant())"
    $foldedDestinations = @(
        Get-W4Utf8SortedStrings -Values @($LogicalDestinations | ForEach-Object {
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

function Assert-W4DistributionOwnerSet {
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
        Assert-W4DistributionOwnerBinding -DistributionName $name `
            -ComponentIds $components -SourceRef $SourceRef `
            -LogicalDestinations $LogicalDestinations `
            -AllowPyInstallerBootloader:$AllowPyInstallerBootloader -Label $Label
    }
    $expectedGenericIds = @(
        Get-W4Utf8SortedStrings -Values @($names | Where-Object {
            $_ -cnotin @('pyinstaller', 'pyinstaller-hooks-contrib')
        } | ForEach-Object { "python-distribution:$_" }) -Unique
    )
    $actualGenericIds = @(
        Get-W4Utf8SortedStrings -Values @($components | Where-Object {
            $_.StartsWith('python-distribution:', [System.StringComparison]::Ordinal)
        }) -Unique
    )
    if (-not $AllowAggregateComponentOwners -and
        ($actualGenericIds | ConvertTo-Json -Compress) -cne
        ($expectedGenericIds | ConvertTo-Json -Compress)) {
        throw "$Label generic distribution component owners are not exact"
    }
}

function Get-W4Utf8SortedStrings {
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

function Assert-W4Utf8SortedUniqueStrings {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [object[]]$Values,
        [Parameter(Mandatory = $true)][string]$Label,
        [switch]$RejectCaseCollisions
    )

    $strings = @($Values | ForEach-Object { [string]$_ })
    $sorted = @(Get-W4Utf8SortedStrings -Values $strings)
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

function Assert-W4ExactObjectFields {
    param(
        [Parameter(Mandatory = $true)]$Object,
        [Parameter(Mandatory = $true)][string[]]$Fields,
        [Parameter(Mandatory = $true)][string]$Label
    )

    Assert-W4JsonObjectFields -Object $Object -RequiredFields $Fields -Label $Label
    $actual = @(Get-W4Utf8SortedStrings -Values $Object.PSObject.Properties.Name)
    $expected = @(Get-W4Utf8SortedStrings -Values $Fields)
    if (($actual | ConvertTo-Json -Compress) -cne ($expected | ConvertTo-Json -Compress)) {
        throw "$Label fields are not exact. actual=$($actual -join ',') expected=$($expected -join ',')"
    }
}

function Assert-W4ExactBoolean {
    param(
        [Parameter(Mandatory = $true)][AllowNull()]$Value,
        [Parameter(Mandatory = $true)][bool]$Expected,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if ($Value -isnot [bool] -or [bool]$Value -ne $Expected) {
        throw "$Label must be the JSON boolean $($Expected.ToString().ToLowerInvariant())"
    }
}

function Assert-W4LicenseMaterialStatusBinding {
    param(
        [Parameter(Mandatory = $true)][string]$ComponentId,
        [Parameter(Mandatory = $true)]$InventoryComponent,
        [Parameter(Mandatory = $true)]$LicenseIndexComponent,
        [Parameter(Mandatory = $true)][string]$SbomStatus
    )

    $classificationIds = @(
        $InventoryComponent.classification_ids | ForEach-Object { [string]$_ }
    )
    $expectedStatus = if ($ComponentId.StartsWith(
        'conda-package:', [System.StringComparison]::Ordinal
    )) {
        'metadata-only-compliance-hold'
    } elseif ([bool]$InventoryComponent.contains_native_payload -or
        $classificationIds -ccontains 'framework:qt-pyside' -or
        $classificationIds -ccontains 'native:msvc-runtime') {
        'top-level-only-compliance-hold'
    } elseif (@($LicenseIndexComponent.license_files).Count -gt 0) {
        'bound'
    } else {
        'metadata-only-compliance-hold'
    }
    if ([string]$LicenseIndexComponent.license_material_status -cne $expectedStatus -or
        $SbomStatus -cne [string]$LicenseIndexComponent.license_material_status) {
        throw "SBOM/license-index license material status is invalid: $ComponentId"
    }
    return $expectedStatus
}

function Assert-W4BuildEnvironmentContract {
    param(
        [Parameter(Mandatory = $true)]$Contract
    )

    Assert-W4ExactObjectFields -Object $Contract -Fields @(
        'conda_hardlink_threat_model', 'hardlink_sensitive_roots', 'home_directory',
        'inherit_ambient', 'live_environment_byte_revalidation', 'path_roles',
        'python_hash_seed', 'python_no_user_site',
        'release_eligible_environment_requirement', 'source_date_epoch',
        'temporary_directory', 'timezone'
    ) -Label 'build environment contract'
    Assert-W4ExactBoolean -Value $Contract.inherit_ambient -Expected $false `
        -Label 'build environment inherit_ambient'
    Assert-W4ExactBoolean -Value $Contract.python_no_user_site -Expected $true `
        -Label 'build environment python_no_user_site'
    if ([bool]$Contract.inherit_ambient -or
        [bool]$Contract.python_no_user_site -ne $true -or
        (@($Contract.path_roles) | ConvertTo-Json -Compress) -cne
            (@(
                'python-prefix', 'python-scripts', 'python-library-bin', 'python-dlls',
                'windows-system32', 'locked-git-directory'
            ) | ConvertTo-Json -Compress) -or
        [string]$Contract.conda_hardlink_threat_model -cne
            'accepted_for_test_candidate' -or
        (@($Contract.hardlink_sensitive_roots) | ConvertTo-Json -Compress) -cne
            (@(
                'python-prefix', 'python-prefix/DLLs', 'python-prefix/Lib',
                'python-prefix/Lib/site-packages', 'python-prefix/Library/bin'
            ) | ConvertTo-Json -Compress) -or
        (@($Contract.live_environment_byte_revalidation) |
            ConvertTo-Json -Compress) -cne
            (@(
                'before-build-a', 'after-build-a', 'before-build-b',
                'after-build-b', 'before-publication'
            ) | ConvertTo-Json -Compress) -or
        [string]$Contract.release_eligible_environment_requirement -cne
            'copy-only-no-hardlinks' -or
        [string]$Contract.home_directory -cne 'per-physical-build-root' -or
        [string]$Contract.python_hash_seed -cne '0' -or
        [string]$Contract.source_date_epoch -cne 'git-commit-timestamp' -or
        [string]$Contract.temporary_directory -cne 'per-physical-build-root' -or
        [string]$Contract.timezone -cne 'UTC') {
        throw 'build-info build environment contract is not the frozen clean build contract'
    }
}

function Assert-W4ReleaseLockBinding {
    param(
        [Parameter(Mandatory = $true)]$Inputs,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256
    )
    $lockInputName = 'packaging/locks/release-environment.v2.json'
    $inputNames = @($Inputs.PSObject.Properties | ForEach-Object { [string]$_.Name })
    $lockInputProperty = if ($inputNames -ccontains $lockInputName) {
        @($Inputs.PSObject.Properties | Where-Object { $_.Name -ceq $lockInputName })[0]
    } else {
        $null
    }
    if ($ExpectedSha256 -cnotmatch '^[0-9a-f]{64}$' -or
        $null -eq $lockInputProperty -or
        [string]$lockInputProperty.Value -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$lockInputProperty.Value -cne $ExpectedSha256) {
        throw 'dependency-manifest environment lock hash is not bound by build-info.inputs'
    }
}

function Assert-W4ReleaseBlockerAuthority {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$Rows,
        [Parameter(Mandatory = $true)][string]$DeclaredSha256,
        [Parameter(Mandatory = $true)][string[]]$ExpectedIds,
        [string]$Label = 'release blocker authority'
    )

    if ($DeclaredSha256 -cnotmatch '^[0-9a-f]{64}$' -or $Rows.Count -ne $ExpectedIds.Count) {
        throw "$Label hash/count is invalid"
    }
    $actualIds = [System.Collections.Generic.List[string]]::new()
    foreach ($row in $Rows) {
        $identifier = [string]$row.id
        $expectedFields = @('condition', 'id', 'resolution')
        if ($identifier -ceq 'conda-native-license-materials-and-spdx') {
            $expectedFields += 'affected_component_selectors'
        }
        if ($identifier -ceq 'html2text-gpl-compliance') {
            $expectedFields += 'resolution_requirements'
        }
        Assert-W4ExactObjectFields -Object $row -Fields $expectedFields `
            -Label "$Label row $identifier"
        if ($identifier -cnotmatch '^[a-z0-9]+(?:-[a-z0-9]+)*$' -or
            [string]::IsNullOrWhiteSpace([string]$row.condition) -or
            [string]::IsNullOrWhiteSpace([string]$row.resolution)) {
            throw "$Label row is incomplete: $identifier"
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
        if ($identifier -ceq 'conda-native-license-materials-and-spdx') {
            $selectors = @(
                'component:*[native-payload]',
                'conda-package:*'
            )
            if ((@($row.affected_component_selectors) | ConvertTo-Json -Compress) -cne
                ($selectors | ConvertTo-Json -Compress)) {
                throw 'native closure affected component selectors are not exact'
            }
        }
        $actualIds.Add($identifier)
    }
    if ((@($actualIds) | ConvertTo-Json -Compress) -cne
        (@($ExpectedIds) | ConvertTo-Json -Compress)) {
        throw "$Label IDs/order differ from the canonical compliance authority"
    }
    $recomputed = Get-W4CanonicalJsonSha256 -Value ([object[]]$Rows)
    if ($recomputed -cne $DeclaredSha256) {
        throw "$Label SHA-256 does not match canonical rows"
    }
    return $recomputed
}

function Assert-W4CondaHardlinkThreatEvidence {
    param(
        [Parameter(Mandatory = $true)]$Evidence,
        [string]$Label = 'Conda hardlink threat evidence'
    )

    Assert-W4ExactObjectFields -Object $Evidence -Fields @(
        'schema_version', 'anchors', 'observed_hardlink_anchor_count',
        'release_eligible_environment_requirement', 'threat_model', 'validation_scope'
    ) -Label $Label
    if ([string]$Evidence.schema_version -cne 'pkv.conda-hardlink-threat-evidence.v1' -or
        [string]$Evidence.release_eligible_environment_requirement -cne
            'copy-only-no-hardlinks' -or
        [string]$Evidence.threat_model -cne 'accepted_for_test_candidate' -or
        (@($Evidence.validation_scope) | ConvertTo-Json -Compress) -cne
            (@(
                'before-build-a', 'after-build-a', 'before-build-b',
                'after-build-b', 'before-publication'
            ) | ConvertTo-Json -Compress)) {
        throw "$Label does not declare the frozen candidate threat model/revalidation scope"
    }
    $anchors = @($Evidence.anchors)
    if ($anchors.Count -ne 3) {
        throw "$Label must contain exactly three byte-identity anchors"
    }
    $labels = [System.Collections.Generic.List[string]]::new()
    $observed = 0
    foreach ($anchor in $anchors) {
        Assert-W4ExactObjectFields -Object $anchor -Fields @(
            'hardlink_count', 'label', 'path', 'sha256', 'size'
        ) -Label "$Label anchor"
        if ([string]$anchor.label -cnotmatch '^[a-z0-9]+(?:-[a-z0-9]+)*$' -or
            [string]::IsNullOrWhiteSpace([string]$anchor.path) -or
            [string]$anchor.path -match '(^|/)\.\.(/|$)' -or
            [string]$anchor.sha256 -cnotmatch '^[0-9a-f]{64}$' -or
            [int64]$anchor.size -le 0 -or [int64]$anchor.hardlink_count -lt 1) {
            throw "$Label contains an invalid anchor"
        }
        if ([int64]$anchor.hardlink_count -gt 1) {
            $observed += 1
        }
        $labels.Add([string]$anchor.label)
    }
    $expectedLabels = @('numpy-package-anchor', 'python-dll', 'python-executable')
    if ((@($labels) | ConvertTo-Json -Compress) -cne
            ($expectedLabels | ConvertTo-Json -Compress) -or
        [int]$Evidence.observed_hardlink_anchor_count -ne $observed) {
        throw "$Label anchor order/count is invalid"
    }
    return $Evidence
}

function Assert-W4ReleaseInventory {
    param(
        [Parameter(Mandatory = $true)]$Inventory,
        [Parameter(Mandatory = $true)][string]$ArtifactRoot,
        [Parameter(Mandatory = $true)]$BuildInfo,
        [Parameter(Mandatory = $true)]$Provenance,
        [Parameter(Mandatory = $true)]$DependencyManifest,
        [Parameter(Mandatory = $true)][string[]]$ExpectedExecutablePaths
    )

    Assert-W4ExactObjectFields -Object $Inventory -Fields @(
        'analysis', 'authority', 'bindings', 'components', 'coverage',
        'embedded_archives', 'included_conda_packages', 'included_distributions',
        'payload', 'schema_version'
    ) -Label 'release-inventory.json'
    if ([string]$Inventory.schema_version -cne 'pkv.release-inventory.v1') {
        throw 'release-inventory.json has an unexpected schema version'
    }
    Assert-W4ExactObjectFields -Object $Inventory.analysis -Fields @(
        'entry_count', 'portable_graph_sha256', 'source_count', 'sources',
        'virtual_entries'
    ) -Label 'release inventory analysis'
    if ([int]$Inventory.analysis.entry_count -le 0 -or
        [int]$Inventory.analysis.source_count -le 0 -or
        [string]$Inventory.analysis.portable_graph_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        @($Inventory.analysis.sources).Count -ne [int]$Inventory.analysis.source_count) {
        throw 'release inventory Analysis authority is incomplete'
    }

    $bindings = $Inventory.bindings
    Assert-W4ExactObjectFields -Object $bindings -Fields @(
        'analysis_graph_sha256', 'artifact_closure_sha256', 'artifact_path_base',
        'closure_sha256', 'conda_native_registry_sha256',
        'embedded_archives_sha256', 'payload_tree_sha256'
    ) -Label 'release inventory bindings'
    foreach ($field in @(
        'analysis_graph_sha256', 'artifact_closure_sha256', 'closure_sha256',
        'conda_native_registry_sha256', 'embedded_archives_sha256',
        'payload_tree_sha256'
    )) {
        if ([string]$bindings.$field -cnotmatch '^[0-9a-f]{64}$') {
            throw "release inventory binding is not SHA-256: $field"
        }
    }
    if ([string]$bindings.analysis_graph_sha256 -cne
            [string]$Inventory.analysis.portable_graph_sha256 -or
        [string]$bindings.artifact_path_base -cne 'app') {
        throw 'release inventory Analysis/path-base binding is invalid'
    }

    $authority = $Inventory.authority
    Assert-W4ExactObjectFields -Object $authority -Fields @(
        'artifact_kind', 'artifact_status', 'build_fingerprint',
        'conda_native_registry_path', 'conda_native_registry_sha256',
        'environment_lock_path', 'environment_lock_sha256',
        'release_blocker_authority', 'release_blocker_authority_sha256',
        'release_blockers', 'release_eligible', 'source_revision'
    ) -Label 'release inventory authority'
    Assert-W4ExactBoolean -Value $authority.release_eligible -Expected $false `
        -Label 'release inventory authority release_eligible'
    $registryInput = @($BuildInfo.inputs.PSObject.Properties | Where-Object {
        $_.Name -ceq 'packaging/locks/conda-native-registry.v1.json'
    })
    if ([string]$authority.artifact_kind -cne [string]$BuildInfo.artifact_kind -or
        [string]$authority.artifact_status -cne [string]$BuildInfo.artifact_status -or
        [string]$authority.build_fingerprint -cne [string]$BuildInfo.build_fingerprint -or
        [string]$authority.conda_native_registry_path -cne
            'packaging/locks/conda-native-registry.v1.json' -or
        $registryInput.Count -ne 1 -or
        [string]$authority.conda_native_registry_sha256 -cne
            [string]$registryInput[0].Value -or
        [string]$authority.conda_native_registry_sha256 -cne
            [string]$bindings.conda_native_registry_sha256 -or
        [string]$authority.environment_lock_path -cne
            'packaging/locks/release-environment.v2.json' -or
        [string]$authority.environment_lock_sha256 -cne
            [string]$DependencyManifest.environment_lock_sha256 -or
        [string]$authority.release_blocker_authority_sha256 -cne
            [string]$Provenance.release_blocker_authority_sha256 -or
        (@($authority.release_blocker_authority) | ConvertTo-Json -Depth 20 -Compress) -cne
            (@($Provenance.release_blocker_authority) | ConvertTo-Json -Depth 20 -Compress) -or
        (@($authority.release_blockers) | ConvertTo-Json -Compress) -cne
            (@($Provenance.release_blockers) | ConvertTo-Json -Compress) -or
        [bool]$authority.release_eligible -or
        [string]$authority.source_revision -cne [string]$BuildInfo.source_revision) {
        throw 'release inventory authority is not exactly bound to build/provenance/locks'
    }

    $archives = @($Inventory.embedded_archives)
    if ($archives.Count -ne $ExpectedExecutablePaths.Count) {
        throw "release inventory embedded archive count is invalid: $($archives.Count)"
    }
    $archiveHash = Get-W4CanonicalJsonSha256 -Value ([object[]]$archives)
    if ($archiveHash -cne [string]$bindings.embedded_archives_sha256) {
        throw 'release inventory embedded_archives_sha256 does not match canonical archives'
    }
    $expectedPaths = @(Get-W4Utf8SortedStrings -Values $ExpectedExecutablePaths)
    $actualPaths = @(Get-W4Utf8SortedStrings -Values @(
        $archives | ForEach-Object { [string]$_.executable_artifact_path }
    ))
    if (($actualPaths | ConvertTo-Json -Compress) -cne
        ($expectedPaths | ConvertTo-Json -Compress)) {
        throw 'release inventory executable archive path set is invalid'
    }

    $knownComponentIds = @{}
    foreach ($component in @($Inventory.components)) {
        $identifier = [string]$component.id
        if ([string]::IsNullOrWhiteSpace($identifier) -or
            $knownComponentIds.ContainsKey($identifier)) {
            throw 'release inventory component ID is empty or duplicated'
        }
        $knownComponentIds[$identifier] = $component
    }
    $inventoryComponentOrder = @(
        $Inventory.components | ForEach-Object { [string]$_.id }
    )
    Assert-W4Utf8SortedUniqueStrings -Values $inventoryComponentOrder `
        -Label 'release inventory components'

    $sourceByComponent = @{}
    $distributionSources = @{}
    $condaSources = @{}
    $derivedUnownedSourcePaths = [System.Collections.Generic.List[string]]::new()
    $sourcePaths = @($Inventory.analysis.sources | ForEach-Object { [string]$_.path })
    Assert-W4Utf8SortedUniqueStrings -Values $sourcePaths `
        -Label 'release inventory Analysis sources' -RejectCaseCollisions
    foreach ($source in @($Inventory.analysis.sources)) {
        Assert-W4ExactObjectFields -Object $source -Fields @(
            'component_ids', 'conda_component_ids', 'distribution_names',
            'occurrences', 'path', 'sha256', 'size'
        ) -Label 'release inventory Analysis source'
        $sourcePath = [string]$source.path
        $sourceComponentIds = @($source.component_ids | ForEach-Object { [string]$_ })
        $sourceCondaIds = @($source.conda_component_ids | ForEach-Object { [string]$_ })
        $sourceDistributionNames = @(
            $source.distribution_names | ForEach-Object { [string]$_ }
        )
        if ([string]::IsNullOrWhiteSpace($sourcePath) -or
            $sourcePath.Contains([char]92) -or
            $sourcePath -match '(^|/)\.\.(/|$)' -or
            [string]$source.sha256 -cnotmatch '^[0-9a-f]{64}$' -or
            [int64]$source.size -lt 0 -or
            $sourceComponentIds.Count -le 0 -or
            @($source.occurrences).Count -le 0) {
            throw "release inventory Analysis source identity is invalid: $sourcePath"
        }
        Assert-W4Utf8SortedUniqueStrings -Values $sourceComponentIds `
            -Label "release inventory source components $sourcePath"
        Assert-W4Utf8SortedUniqueStrings -Values $sourceCondaIds `
            -Label "release inventory source Conda components $sourcePath"
        Assert-W4Utf8SortedUniqueStrings -Values $sourceDistributionNames `
            -Label "release inventory source distributions $sourcePath"
        $occurrenceKeys = [System.Collections.Generic.List[string]]::new()
        foreach ($occurrence in @($source.occurrences)) {
            Assert-W4ExactObjectFields -Object $occurrence -Fields @(
                'destination', 'slot', 'type'
            ) -Label 'release inventory Analysis source occurrence'
            if ([string]::IsNullOrWhiteSpace([string]$occurrence.destination) -or
                [string]::IsNullOrWhiteSpace([string]$occurrence.slot) -or
                [string]::IsNullOrWhiteSpace([string]$occurrence.type) -or
                ([string]$occurrence.destination).Contains([char]92) -or
                [string]$occurrence.destination -match '(^|/)\.\.(/|$)') {
                throw "release inventory Analysis occurrence is invalid: $sourcePath"
            }
            $occurrenceKeys.Add(
                "$([string]$occurrence.slot)`0$([string]$occurrence.destination)"
            )
        }
        Assert-W4Utf8SortedUniqueStrings -Values @($occurrenceKeys) `
            -Label "release inventory Analysis source occurrences $sourcePath"
        foreach ($identifier in $sourceComponentIds) {
            if (-not $knownComponentIds.ContainsKey($identifier)) {
                throw "release inventory source references an unknown component: $identifier"
            }
            if (-not $sourceByComponent.ContainsKey($identifier)) {
                $sourceByComponent[$identifier] = [System.Collections.Generic.List[string]]::new()
            }
            $sourceByComponent[$identifier].Add($sourcePath)
        }
        foreach ($identifier in $sourceCondaIds) {
            if (-not $knownComponentIds.ContainsKey($identifier) -or
                -not $identifier.StartsWith(
                    'conda-package:', [System.StringComparison]::Ordinal
                ) -or $sourceComponentIds -cnotcontains $identifier) {
                throw "release inventory source references an invalid Conda component: $identifier"
            }
            if (-not $condaSources.ContainsKey($identifier)) {
                $condaSources[$identifier] = [System.Collections.Generic.List[string]]::new()
            }
            $condaSources[$identifier].Add($sourcePath)
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
        Assert-W4DistributionOwnerSet -DistributionNames $sourceDistributionNames `
            -ComponentIds $sourceComponentIds -SourceRef $sourcePath `
            -LogicalDestinations @(
                $source.occurrences | ForEach-Object { [string]$_.destination }
            ) -AllowPyInstallerBootloader:$sourceAllowsPyInstallerBootloader `
            -Label "release inventory Analysis source $sourcePath"
        foreach ($distributionName in $sourceDistributionNames) {
            if (-not $distributionSources.ContainsKey($distributionName)) {
                $distributionSources[$distributionName] = [System.Collections.Generic.List[string]]::new()
            }
            $distributionSources[$distributionName].Add($sourcePath)
        }
        if ($sourceCondaIds.Count -eq 0 -and $sourceDistributionNames.Count -eq 0) {
            $derivedUnownedSourcePaths.Add($sourcePath)
        }
    }

    $virtualKeys = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    foreach ($virtual in @($Inventory.analysis.virtual_entries)) {
        Assert-W4ExactObjectFields -Object $virtual -Fields @(
            'destination', 'slot', 'type'
        ) -Label 'release inventory Analysis virtual entry'
        $virtualKey = "$([string]$virtual.slot)`0$([string]$virtual.destination)"
        if ([string]::IsNullOrWhiteSpace([string]$virtual.destination) -or
            [string]::IsNullOrWhiteSpace([string]$virtual.slot) -or
            [string]$virtual.type -cne 'PYMODULE' -or
            ([string]$virtual.destination).Contains([char]92) -or
            [string]$virtual.destination -match '(^\.|\.$|\.\.|/)' -or
            -not $virtualKeys.Add($virtualKey)) {
            throw "release inventory Analysis virtual entry is invalid: $virtualKey"
        }
    }

    $includedDistributionNames = @(
        $Inventory.included_distributions | ForEach-Object { [string]$_.name }
    )
    Assert-W4Utf8SortedUniqueStrings -Values $includedDistributionNames `
        -Label 'release inventory included distributions'
    $expectedDistributionNames = @(
        Get-W4Utf8SortedStrings -Values $distributionSources.Keys
    )
    if (($includedDistributionNames | ConvertTo-Json -Compress) -cne
        ($expectedDistributionNames | ConvertTo-Json -Compress)) {
        throw 'release inventory included distributions differ from source ownership'
    }
    foreach ($included in @($Inventory.included_distributions)) {
        Assert-W4ExactObjectFields -Object $included -Fields @(
            'name', 'source_paths', 'version'
        ) -Label 'release inventory included distribution'
        $name = [string]$included.name
        $canonicalName = Get-W4CanonicalDistributionName -Value $name
        $expectedSources = @(
            Get-W4Utf8SortedStrings -Values @($distributionSources[$name]) -Unique
        )
        if ($name -cne $canonicalName -or
            [string]::IsNullOrWhiteSpace([string]$included.version) -or
            (@($included.source_paths) | ConvertTo-Json -Compress) -cne
                ($expectedSources | ConvertTo-Json -Compress)) {
            throw "release inventory included distribution binding is invalid: $name"
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
                if ($knownComponentIds.ContainsKey($_)) { $knownComponentIds[$_] }
            })
            if ($knownComponentIds.ContainsKey($genericId) -or
                $specialComponents.Count -le 0 -or
                @($specialComponents | Where-Object {
                    [string]$_.identity_status -cne 'complete' -or
                    [string]$_.version -cne [string]$included.version
                }).Count -gt 0) {
                throw "release inventory included special distribution binding is invalid: $name"
            }
        } else {
            $component = $knownComponentIds["python-distribution:$canonicalName"]
            if ($null -eq $component -or
                [string]$component.identity_status -cne 'complete' -or
                [string]$component.name -cne $canonicalName -or
                [string]$component.version -cne [string]$included.version -or
                (@($component.source_paths) | ConvertTo-Json -Compress) -cne
                    ($expectedSources | ConvertTo-Json -Compress)) {
                throw "release inventory included distribution component is invalid: $name"
            }
        }
    }

    $includedCondaIds = @(
        $Inventory.included_conda_packages | ForEach-Object { [string]$_.component_id }
    )
    Assert-W4Utf8SortedUniqueStrings -Values $includedCondaIds `
        -Label 'release inventory included Conda packages'
    $expectedCondaIds = @(Get-W4Utf8SortedStrings -Values $condaSources.Keys)
    if (($includedCondaIds | ConvertTo-Json -Compress) -cne
        ($expectedCondaIds | ConvertTo-Json -Compress)) {
        throw 'release inventory included Conda packages differ from source ownership'
    }
    foreach ($included in @($Inventory.included_conda_packages)) {
        Assert-W4ExactObjectFields -Object $included -Fields @(
            'build', 'channel', 'component_id', 'declared_license', 'name',
            'package_sha256', 'record_sha256', 'record_size', 'source_paths', 'version'
        ) -Label 'release inventory included Conda package'
        $identifier = [string]$included.component_id
        $component = $knownComponentIds[$identifier]
        $expectedSources = @(
            Get-W4Utf8SortedStrings -Values @($condaSources[$identifier]) -Unique
        )
        if ($null -eq $component -or
            [string]$component.identity_status -cne 'complete' -or
            [string]$component.build -cne [string]$included.build -or
            [string]$component.channel -cne [string]$included.channel -or
            [string]$component.declared_license -cne [string]$included.declared_license -or
            [string]$component.name -cne [string]$included.name -or
            [string]$component.package_sha256 -cne [string]$included.package_sha256 -or
            [string]$component.record_sha256 -cne [string]$included.record_sha256 -or
            [int64]$component.record_size -ne [int64]$included.record_size -or
            [string]$component.version -cne [string]$included.version -or
            (@($included.source_paths) | ConvertTo-Json -Compress) -cne
                ($expectedSources | ConvertTo-Json -Compress)) {
            throw "release inventory included Conda binding is invalid: $identifier"
        }
    }

    $embeddedByComponent = @{}
    $payloadByComponent = @{}
    $nativePayloadByComponent = @{}
    $totalEmbeddedEntries = 0
    foreach ($archive in $archives) {
        Assert-W4ExactObjectFields -Object $archive -Fields @(
            'bootloader_input', 'bootloader_prefix_sha256', 'bootloader_prefix_size',
            'component_ids', 'entries', 'entry_count', 'executable_artifact_path',
            'executable_sha256', 'executable_size', 'pkg_sha256', 'pkg_size',
            'portable_graph_sha256', 'python_library', 'python_version'
        ) -Label 'release inventory embedded archive'
        Assert-W4ExactObjectFields -Object $archive.bootloader_input -Fields @(
            'source_ref', 'source_sha256', 'source_size'
        ) -Label 'release inventory bootloader input'
        $entries = @($archive.entries)
        if ($entries.Count -ne [int]$archive.entry_count -or $entries.Count -le 0) {
            throw 'release inventory archive entry count is invalid'
        }
        $totalEmbeddedEntries += $entries.Count
        $archiveMaterial = [ordered]@{
            bootloader_input = $archive.bootloader_input
            bootloader_prefix_sha256 = [string]$archive.bootloader_prefix_sha256
            bootloader_prefix_size = [int64]$archive.bootloader_prefix_size
            component_ids = @($archive.component_ids)
            entries = [object[]]$entries
            entry_count = [int]$archive.entry_count
            executable_artifact_path = [string]$archive.executable_artifact_path
            executable_sha256 = [string]$archive.executable_sha256
            executable_size = [int64]$archive.executable_size
            pkg_sha256 = [string]$archive.pkg_sha256
            pkg_size = [int64]$archive.pkg_size
            python_library = [string]$archive.python_library
            python_version = [int]$archive.python_version
        }
        if ((Get-W4CanonicalJsonSha256 -Value $archiveMaterial) -cne
            [string]$archive.portable_graph_sha256) {
            throw "embedded archive portable graph is invalid: $($archive.executable_artifact_path)"
        }
        foreach ($hashField in @(
            [string]$archive.bootloader_input.source_sha256,
            [string]$archive.bootloader_prefix_sha256,
            [string]$archive.executable_sha256,
            [string]$archive.pkg_sha256,
            [string]$archive.portable_graph_sha256
        )) {
            if ($hashField -cnotmatch '^[0-9a-f]{64}$') {
                throw 'embedded archive contains a non-SHA-256 identity'
            }
        }
        $relativeExecutable = [string]$archive.executable_artifact_path
        if ($relativeExecutable -match '(^|/)\.\.(/|$)' -or
            -not $relativeExecutable.StartsWith('app/', [System.StringComparison]::Ordinal)) {
            throw "embedded archive executable path is unsafe: $relativeExecutable"
        }
        $executablePath = Join-Path $ArtifactRoot ($relativeExecutable.Replace('/', '\'))
        $executableSize = [int64](Get-Item -LiteralPath $executablePath -Force).Length
        $pkgSize = [int64]$archive.pkg_size
        $prefixSize = [int64]$archive.bootloader_prefix_size
        if ($executableSize -ne [int64]$archive.executable_size -or
            (Get-W4FileSha256 -Path $executablePath) -cne
                [string]$archive.executable_sha256 -or
            $pkgSize -le 0 -or $prefixSize -le 0 -or
            $prefixSize + $pkgSize -ne $executableSize -or
            (Get-W4FileSegmentSha256 -Path $executablePath -Offset 0 -Length $prefixSize) -cne
                [string]$archive.bootloader_prefix_sha256 -or
            (Get-W4FileSegmentSha256 -Path $executablePath -Offset $prefixSize -Length $pkgSize) -cne
                [string]$archive.pkg_sha256) {
            throw "embedded archive bytes/suffix/prefix do not match inventory: $relativeExecutable"
        }
        $archiveComponentIds = @($archive.component_ids | ForEach-Object { [string]$_ })
        $derivedArchiveComponentIds = [System.Collections.Generic.HashSet[string]]::new(
            [System.StringComparer]::Ordinal
        )
        [void]$derivedArchiveComponentIds.Add('build-runtime:pyinstaller-bootloader')
        foreach ($identifier in $archiveComponentIds) {
            if (-not $knownComponentIds.ContainsKey($identifier)) {
                throw "embedded archive references an unknown component: $identifier"
            }
        }
        $bootloaderPath = "$relativeExecutable!/<bootloader-prefix>"
        if (-not $embeddedByComponent.ContainsKey('build-runtime:pyinstaller-bootloader')) {
            $embeddedByComponent['build-runtime:pyinstaller-bootloader'] = [System.Collections.Generic.List[string]]::new()
        }
        $embeddedByComponent['build-runtime:pyinstaller-bootloader'].Add($bootloaderPath)
        $nativePayloadByComponent['build-runtime:pyinstaller-bootloader'] = $true
        $entryKeys = @{}
        $entryNames = @{}
        foreach ($entry in $entries) {
            $entryFields = @(
                'component_ids', 'compressed', 'content_sha256', 'kind', 'name',
                'stored_sha256', 'stored_size', 'typecode', 'uncompressed_size'
            )
            $entryComponentIds = @(
                $entry.component_ids | ForEach-Object { [string]$_ }
            )
            if ([string]$entry.kind -cne 'OPTION') {
                $entryFields += @(
                    'conda_component_ids', 'distribution_names', 'source_ref',
                    'source_sha256', 'source_size'
                )
            }
            if ([string]$entry.kind -ceq 'PYZ') {
                $entryFields += @(
                    'pyz_member_count', 'pyz_members', 'pyz_members_sha256',
                    'pyz_python_magic_sha256', 'pyz_toc_sha256', 'pyz_toc_size'
                )
            }
            Assert-W4ExactObjectFields -Object $entry -Fields $entryFields `
                -Label 'release inventory embedded entry'
            Assert-W4ExactBoolean -Value $entry.compressed `
                -Expected ([bool]$entry.compressed) -Label 'embedded entry compressed'
            $entryKind = [string]$entry.kind
            $expectedTypecode = @{
                BINARY = 'b'; DATA = 'b'; EXTENSION = 'b'; OPTION = 'o'
                PYMODULE = 'm'; PYSOURCE = 's'; PYZ = 'z'
            }[$entryKind]
            $entryKey = "$([string]$entry.typecode)`0$([string]$entry.name)"
            $foldedEntryName = ([string]$entry.name).ToLowerInvariant()
            if ($entryKeys.ContainsKey($entryKey) -or
                ($entryKind -cne 'OPTION' -and $entryNames.ContainsKey($foldedEntryName))) {
                throw "embedded archive entry is duplicated: $entryKey"
            }
            $entryKeys[$entryKey] = $true
            if ($entryKind -cne 'OPTION') {
                $entryNames[$foldedEntryName] = $true
            }
            if ($null -eq $expectedTypecode -or
                [string]::IsNullOrWhiteSpace([string]$entry.name) -or
                ([string]$entry.name).Contains([char]92) -or
                [string]$entry.name -match '(^|/)\.\.(/|$)' -or
                [string]$entry.typecode -cne $expectedTypecode -or
                [string]$entry.content_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
                [string]$entry.stored_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
                [int64]$entry.stored_size -lt 0 -or
                [int64]$entry.uncompressed_size -lt 0 -or
                $entryComponentIds.Count -le 0 -or
                (-not [bool]$entry.compressed -and (
                    [int64]$entry.stored_size -ne [int64]$entry.uncompressed_size -or
                    [string]$entry.stored_sha256 -cne [string]$entry.content_sha256
                ))) {
                throw "embedded archive entry identity is invalid: $($entry.name)"
            }
            if ($entryKind -ceq 'OPTION' -and (
                [int64]$entry.stored_size -ne 0 -or
                [int64]$entry.uncompressed_size -ne 0 -or
                [string]$entry.content_sha256 -cne
                    'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855' -or
                [string]$entry.stored_sha256 -cne
                    'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
            )) {
                throw "embedded OPTION entry is not canonical empty: $($entry.name)"
            }
            Assert-W4Utf8SortedUniqueStrings -Values $entryComponentIds `
                -Label "embedded entry components $relativeExecutable!/$($entry.name)"
            if ($entryKind -cne 'OPTION') {
                $entryCondaIds = @(
                    $entry.conda_component_ids | ForEach-Object { [string]$_ }
                )
                $entryDistributionNames = @(
                    $entry.distribution_names | ForEach-Object { [string]$_ }
                )
                Assert-W4Utf8SortedUniqueStrings -Values $entryCondaIds `
                    -Label "embedded entry Conda owners $relativeExecutable!/$($entry.name)"
                Assert-W4Utf8SortedUniqueStrings -Values $entryDistributionNames `
                    -Label "embedded entry distribution owners $relativeExecutable!/$($entry.name)"
                if ([string]::IsNullOrWhiteSpace([string]$entry.source_ref) -or
                    ([string]$entry.source_ref).Contains([char]92) -or
                    [string]$entry.source_ref -match '(^|/)\.\.(/|$)' -or
                    [string]$entry.source_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
                    [int64]$entry.source_size -lt 0) {
                    throw "embedded entry source binding is invalid: $relativeExecutable!/$($entry.name)"
                }
                foreach ($condaId in $entryCondaIds) {
                    if ($entryComponentIds -cnotcontains $condaId) {
                        throw "embedded entry Conda owner is absent from component_ids: $condaId"
                    }
                }
                Assert-W4DistributionOwnerSet -DistributionNames $entryDistributionNames `
                    -ComponentIds $entryComponentIds `
                    -SourceRef ([string]$entry.source_ref) `
                    -LogicalDestinations @([string]$entry.name) `
                    -AllowPyInstallerBootloader `
                    -AllowAggregateComponentOwners:($entryKind -ceq 'PYZ') `
                    -Label "embedded entry $relativeExecutable!/$($entry.name)"
            }
            $embeddedPath = "$relativeExecutable!/$([string]$entry.name)"
            $entryIsNative = (
                [string]$entry.kind -cin @('BINARY', 'EXECUTABLE', 'EXTENSION') -or
                [string]$entry.name -match '(?i)\.(?:dll|dylib|exe|pyd|so)$'
            )
            foreach ($identifier in $entryComponentIds) {
                if (-not $knownComponentIds.ContainsKey($identifier)) {
                    throw "embedded entry references an unknown component: $identifier"
                }
                [void]$derivedArchiveComponentIds.Add($identifier)
                if ([string]$entry.kind -cne 'PYZ') {
                    if (-not $embeddedByComponent.ContainsKey($identifier)) {
                        $embeddedByComponent[$identifier] = [System.Collections.Generic.List[string]]::new()
                    }
                    $embeddedByComponent[$identifier].Add($embeddedPath)
                    if ($entryIsNative) {
                        $nativePayloadByComponent[$identifier] = $true
                    }
                }
            }
            if ([string]$entry.kind -ceq 'PYZ') {
                $members = @($entry.pyz_members)
                if ($members.Count -le 0 -or
                    $members.Count -ne [int]$entry.pyz_member_count -or
                    (Get-W4CanonicalJsonSha256 -Value ([object[]]$members)) -cne
                        [string]$entry.pyz_members_sha256 -or
                    [string]$entry.pyz_python_magic_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
                    [string]$entry.pyz_toc_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
                    [int64]$entry.pyz_toc_size -le 0) {
                    throw "embedded PYZ member graph is invalid: $embeddedPath"
                }
                $memberKeys = @{}
                $memberNames = [System.Collections.Generic.List[string]]::new()
                $derivedPyzComponentIds = [System.Collections.Generic.HashSet[string]]::new(
                    [System.StringComparer]::Ordinal
                )
                $emptySha256 = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
                [int64]$pyzStoredSize = 0
                foreach ($member in $members) {
                    Assert-W4ExactObjectFields -Object $member -Fields @(
                        'component_ids', 'conda_component_ids', 'content_sha256',
                        'content_size', 'distribution_names', 'kind', 'name',
                        'source_kind', 'source_ref', 'source_sha256', 'source_size',
                        'stored_sha256', 'stored_size'
                    ) -Label 'release inventory PYZ member'
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
                    if ($memberKeys.ContainsKey($memberKey) -or
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
                        throw "embedded PYZ member identity is invalid: $memberKey"
                    }
                    Assert-W4Utf8SortedUniqueStrings -Values $memberComponentIds `
                        -Label "embedded PYZ member components $memberName"
                    Assert-W4Utf8SortedUniqueStrings -Values $memberCondaIds `
                        -Label "embedded PYZ member Conda owners $memberName"
                    Assert-W4Utf8SortedUniqueStrings -Values $memberDistributionNames `
                        -Label "embedded PYZ member distribution owners $memberName"
                    foreach ($condaId in $memberCondaIds) {
                        if ($memberComponentIds -cnotcontains $condaId) {
                            throw "embedded PYZ Conda owner is absent from component_ids: $condaId"
                        }
                    }
                    Assert-W4DistributionOwnerSet `
                        -DistributionNames $memberDistributionNames `
                        -ComponentIds $memberComponentIds `
                        -SourceRef ([string]$member.source_ref) `
                        -LogicalDestinations @($memberName) `
                        -Label "embedded PYZ member $memberName"
                    if ($memberKind -ceq 'namespace') {
                        $expectedVirtualRef = "virtual-namespace/$($memberName.Replace('.', '/'))"
                        if ([int64]$member.content_size -ne 0 -or
                            [int64]$member.source_size -ne 0 -or
                            [int64]$member.stored_size -ne 0 -or
                            [string]$member.content_sha256 -cne $emptySha256 -or
                            [string]$member.source_sha256 -cne $emptySha256 -or
                            [string]$member.stored_sha256 -cne $emptySha256 -or
                            [string]$member.source_ref -cne $expectedVirtualRef) {
                            throw "embedded PYZ namespace binding is invalid: $memberName"
                        }
                    } elseif (
                        [int64]$member.content_size -le 0 -or
                        [int64]$member.stored_size -le 0 -or
                        [string]::IsNullOrWhiteSpace([string]$member.source_ref) -or
                        ([int64]$member.source_size -eq 0 -and
                            [string]$member.source_sha256 -cne $emptySha256)
                    ) {
                        throw "embedded PYZ physical member binding is invalid: $memberName"
                    }
                    $memberKeys[$memberKey] = $true
                    $memberNames.Add($memberName)
                    $pyzStoredSize += [int64]$member.stored_size
                    $memberPath = "$embeddedPath#/$memberName"
                    foreach ($identifier in $memberComponentIds) {
                        if (-not $knownComponentIds.ContainsKey($identifier) -or
                            $entryComponentIds -cnotcontains $identifier) {
                            throw "embedded PYZ member component is unknown or absent from its aggregate entry: $identifier"
                        }
                        [void]$derivedPyzComponentIds.Add($identifier)
                        if (-not $embeddedByComponent.ContainsKey($identifier)) {
                            $embeddedByComponent[$identifier] = [System.Collections.Generic.List[string]]::new()
                        }
                        $embeddedByComponent[$identifier].Add($memberPath)
                    }
                }
                Assert-W4Utf8SortedUniqueStrings -Values @($memberNames) `
                    -Label "embedded PYZ member names $embeddedPath" -RejectCaseCollisions
                if (
                    (@($entryComponentIds) | ConvertTo-Json -Compress) -cne
                    (@(Get-W4Utf8SortedStrings -Values @($derivedPyzComponentIds)) |
                        ConvertTo-Json -Compress) -or
                    (17 + $pyzStoredSize + [int64]$entry.pyz_toc_size) -ne
                        [int64]$entry.uncompressed_size
                ) {
                    throw "embedded PYZ member component union/size binding is invalid: $embeddedPath"
                }
            }
        }
        if ((@($archiveComponentIds) | ConvertTo-Json -Compress) -cne
            (@(Get-W4Utf8SortedStrings -Values @($derivedArchiveComponentIds)) |
                ConvertTo-Json -Compress)) {
            throw "embedded archive component_ids are not the exact entry/prefix union: $relativeExecutable"
        }
    }

    Assert-W4ExactObjectFields -Object $Inventory.payload -Fields @(
        'file_count', 'files', 'path_base', 'tree_sha256'
    ) -Label 'release inventory payload'
    $payloadFiles = @($Inventory.payload.files)
    if ([string]$Inventory.payload.path_base -cne 'app' -or
        $payloadFiles.Count -lt $ExpectedExecutablePaths.Count -or
        [int]$Inventory.payload.file_count -ne $payloadFiles.Count) {
        throw 'release inventory payload count/path base is invalid'
    }
    $payloadPathOrder = @($payloadFiles | ForEach-Object { [string]$_.path })
    Assert-W4Utf8SortedUniqueStrings -Values $payloadPathOrder `
        -Label 'release inventory payload files' -RejectCaseCollisions
    $physicalAppRows = @(Get-W4TreeManifest -Root (Join-Path $ArtifactRoot 'app'))
    if ($physicalAppRows.Count -ne $payloadFiles.Count) {
        throw 'release inventory payload count differs from the physical app tree'
    }
    $physicalAppByPath = @{}
    foreach ($physicalRow in $physicalAppRows) {
        $physicalAppByPath[([string]$physicalRow.path).ToLowerInvariant()] = $physicalRow
    }
    $payloadTreeBuilder = [System.Text.StringBuilder]::new()
    $seenPayloadPaths = @{}
    $seenExecutableRows = [System.Collections.Generic.List[string]]::new()
    foreach ($file in $payloadFiles) {
        $artifactPath = [string]$file.artifact_path
        $archive = @($archives | Where-Object {
            [string]$_.executable_artifact_path -ceq $artifactPath
        })
        $relativePath = [string]$file.path
        if ($relativePath -match '(^|/)\.\.(/|$)' -or
            $artifactPath -cne "app/$relativePath" -or
            $seenPayloadPaths.ContainsKey($relativePath.ToLowerInvariant()) -or
            -not $physicalAppByPath.ContainsKey($relativePath.ToLowerInvariant())) {
            throw "release inventory payload path is unsafe, duplicated, or missing: $relativePath"
        }
        $seenPayloadPaths[$relativePath.ToLowerInvariant()] = $true
        $physicalRow = $physicalAppByPath[$relativePath.ToLowerInvariant()]
        if ([int64]$file.size -ne [int64]$physicalRow.size -or
            [string]$file.sha256 -cne [string]$physicalRow.sha256) {
            throw "release inventory payload bytes differ from the physical app tree: $relativePath"
        }
        if ($archive.Count -eq 1) {
            Assert-W4ExactObjectFields -Object $file -Fields @(
                'artifact_path', 'component_ids', 'embedded_archive_graph_sha256',
                'embedded_component_ids', 'embedded_entry_count', 'embedded_pkg_sha256',
                'embedded_pkg_size', 'kind', 'path', 'sha256', 'size'
            ) -Label 'release inventory executable payload row'
            if ([string]$file.kind -cne 'PYINSTALLER_BOOTLOADER_EXECUTABLE' -or
                [string]$file.sha256 -cne [string]$archive[0].executable_sha256 -or
                [int64]$file.size -ne [int64]$archive[0].executable_size -or
                [string]$file.embedded_archive_graph_sha256 -cne
                    [string]$archive[0].portable_graph_sha256 -or
                [string]$file.embedded_pkg_sha256 -cne [string]$archive[0].pkg_sha256 -or
                [int64]$file.embedded_pkg_size -ne [int64]$archive[0].pkg_size -or
                [int]$file.embedded_entry_count -ne [int]$archive[0].entry_count -or
                (@($file.component_ids) | ConvertTo-Json -Compress) -cne
                    (@($archive[0].component_ids) | ConvertTo-Json -Compress) -or
                (@($file.embedded_component_ids) | ConvertTo-Json -Compress) -cne
                    (@($archive[0].component_ids) | ConvertTo-Json -Compress)) {
                throw "release inventory executable payload/archive binding is invalid: $artifactPath"
            }
            $seenExecutableRows.Add($artifactPath)
            $identifier = 'build-runtime:pyinstaller-bootloader'
            if (-not $payloadByComponent.ContainsKey($identifier)) {
                $payloadByComponent[$identifier] = [System.Collections.Generic.List[string]]::new()
            }
            $payloadByComponent[$identifier].Add($artifactPath)
        } elseif ($archive.Count -eq 0) {
            Assert-W4ExactObjectFields -Object $file -Fields @(
                'artifact_path', 'component_ids', 'conda_component_ids',
                'distribution_names', 'kind', 'path', 'sha256', 'size',
                'source_ref', 'source_sha256', 'toc_destination'
            ) -Label 'release inventory collected payload row'
            $fileComponentIds = @($file.component_ids | ForEach-Object { [string]$_ })
            $fileCondaIds = @($file.conda_component_ids | ForEach-Object { [string]$_ })
            $fileDistributionNames = @(
                $file.distribution_names | ForEach-Object { [string]$_ }
            )
            if ([string]$file.source_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
                [string]$file.source_sha256 -cne [string]$file.sha256 -or
                [string]::IsNullOrWhiteSpace([string]$file.source_ref) -or
                [string]::IsNullOrWhiteSpace([string]$file.toc_destination) -or
                $fileComponentIds.Count -le 0) {
                throw "release inventory collected payload source binding is invalid: $artifactPath"
            }
            Assert-W4Utf8SortedUniqueStrings -Values $fileComponentIds `
                -Label "release inventory payload components $artifactPath"
            Assert-W4Utf8SortedUniqueStrings -Values $fileCondaIds `
                -Label "release inventory payload Conda components $artifactPath"
            Assert-W4Utf8SortedUniqueStrings -Values $fileDistributionNames `
                -Label "release inventory payload distributions $artifactPath"
            foreach ($condaId in $fileCondaIds) {
                if ($fileComponentIds -cnotcontains $condaId) {
                    throw "payload Conda owner is absent from component_ids: $condaId"
                }
            }
            Assert-W4DistributionOwnerSet -DistributionNames $fileDistributionNames `
                -ComponentIds $fileComponentIds `
                -SourceRef ([string]$file.source_ref) `
                -LogicalDestinations @([string]$file.toc_destination) `
                -Label "payload $artifactPath"
            foreach ($identifier in $fileComponentIds) {
                if (-not $knownComponentIds.ContainsKey($identifier)) {
                    throw "payload row references an unknown component: $identifier"
                }
                if (-not $payloadByComponent.ContainsKey($identifier)) {
                    $payloadByComponent[$identifier] = [System.Collections.Generic.List[string]]::new()
                }
                $payloadByComponent[$identifier].Add($artifactPath)
                if ([string]$file.kind -cin @('BINARY', 'EXECUTABLE', 'EXTENSION') -or
                    [string]$file.path -match '(?i)\.(?:dll|dylib|exe|pyd|so)$') {
                    $nativePayloadByComponent[$identifier] = $true
                }
            }
        } else {
            throw "release inventory payload matches multiple embedded archives: $artifactPath"
        }
        [void]$payloadTreeBuilder.Append($relativePath)
        [void]$payloadTreeBuilder.Append([char]0)
        [void]$payloadTreeBuilder.Append([string][int64]$file.size)
        [void]$payloadTreeBuilder.Append([char]0)
        [void]$payloadTreeBuilder.Append([string]$file.sha256)
        [void]$payloadTreeBuilder.Append("`n")
    }
    if ((@(Get-W4Utf8SortedStrings -Values @($seenExecutableRows)) |
            ConvertTo-Json -Compress) -cne
        ($expectedPaths | ConvertTo-Json -Compress)) {
        throw 'release inventory payload does not contain exactly the three product executables'
    }
    $payloadTreeSha = Get-W4StringSha256 -Value $payloadTreeBuilder.ToString()
    if ($payloadTreeSha -cne [string]$Inventory.payload.tree_sha256 -or
        $payloadTreeSha -cne [string]$bindings.payload_tree_sha256) {
        throw 'release inventory payload tree hash does not match executable bytes'
    }

    Assert-W4ExactObjectFields -Object $Inventory.coverage -Fields @(
        'conda_native_registry_sha256', 'embedded_archive_count',
        'embedded_entry_count', 'payload_file_count',
        'unattributed_native_file_count', 'unattributed_native_paths',
        'unowned_source_path_count', 'unowned_source_paths',
        'unresolved_component_ids'
    ) -Label 'release inventory coverage'
    $declaredUnownedSourcePaths = @(
        $Inventory.coverage.unowned_source_paths | ForEach-Object { [string]$_ }
    )
    Assert-W4Utf8SortedUniqueStrings -Values $declaredUnownedSourcePaths `
        -Label 'release inventory unowned source paths' -RejectCaseCollisions
    $expectedUnownedSourcePaths = @(
        Get-W4Utf8SortedStrings -Values @($derivedUnownedSourcePaths) -Unique
    )
    if ([string]$Inventory.coverage.conda_native_registry_sha256 -cne
            [string]$bindings.conda_native_registry_sha256 -or
        [int]$Inventory.coverage.embedded_archive_count -ne $archives.Count -or
        [int]$Inventory.coverage.embedded_entry_count -ne $totalEmbeddedEntries -or
        [int]$Inventory.coverage.payload_file_count -ne $payloadFiles.Count -or
        [int]$Inventory.coverage.unattributed_native_file_count -ne 0 -or
        @($Inventory.coverage.unattributed_native_paths).Count -ne 0 -or
        [int]$Inventory.coverage.unowned_source_path_count -ne
            $expectedUnownedSourcePaths.Count -or
        ($declaredUnownedSourcePaths | ConvertTo-Json -Compress) -cne
            ($expectedUnownedSourcePaths | ConvertTo-Json -Compress) -or
        @($Inventory.coverage.unresolved_component_ids).Count -ne 0) {
        throw 'release inventory coverage counters do not match the exact closure'
    }

    foreach ($identifier in @(Get-W4Utf8SortedStrings -Values $knownComponentIds.Keys)) {
        $component = $knownComponentIds[$identifier]
        $identityStatus = [string]$component.identity_status
        $componentPropertyNames = @($component.PSObject.Properties.Name)
        $baseFields = @(
            'classification_ids', 'embedded_paths', 'id', 'identity_status', 'name',
            'payload_paths', 'source_paths', 'type'
        )
        if ($componentPropertyNames -ccontains 'version') {
            $baseFields += 'version'
        }
        if ($identityStatus -ceq 'complete') {
            $baseFields += 'contains_native_payload'
        }
        if ($identifier.StartsWith('conda-package:', [System.StringComparison]::Ordinal)) {
            $baseFields += @(
                'build', 'build_number', 'channel', 'declared_license', 'package_sha256',
                'package_url', 'record_file', 'record_sha256', 'record_size', 'subdir'
            )
        }
        Assert-W4ExactObjectFields -Object $component -Fields $baseFields `
            -Label "release inventory component $identifier"
        if ($identityStatus -cnotin @('complete', 'classification-only') -or
            ($identityStatus -ceq 'complete' -and
                $identifier -cne 'application:project' -and
                ($componentPropertyNames -cnotcontains 'version' -or
                    [string]::IsNullOrWhiteSpace([string]$component.version))) -or
            ($identityStatus -ceq 'classification-only' -and
                $componentPropertyNames -ccontains 'contains_native_payload')) {
            throw "release inventory component identity is not release-complete: $identifier"
        }
        if ($identityStatus -ceq 'complete') {
            Assert-W4ExactBoolean -Value $component.contains_native_payload `
                -Expected $nativePayloadByComponent.ContainsKey($identifier) `
                -Label "release inventory component native-payload binding $identifier"
        }
        $payloadPaths = @($component.payload_paths | ForEach-Object { [string]$_ })
        $embeddedPaths = @($component.embedded_paths | ForEach-Object { [string]$_ })
        $sourcePaths = @($component.source_paths | ForEach-Object { [string]$_ })
        if ($payloadPaths.Count -eq 0 -and $embeddedPaths.Count -eq 0) {
            throw "release inventory component has no physical/embedded path: $identifier"
        }
        Assert-W4Utf8SortedUniqueStrings -Values $sourcePaths `
            -Label "release inventory component source paths $identifier" `
            -RejectCaseCollisions
        $expectedSourcePaths = if ($sourceByComponent.ContainsKey($identifier)) {
            @(Get-W4Utf8SortedStrings -Values @($sourceByComponent[$identifier]) -Unique)
        } else { @() }
        $expectedPayload = if ($payloadByComponent.ContainsKey($identifier)) {
            @(Get-W4Utf8SortedStrings -Values @($payloadByComponent[$identifier]) -Unique)
        } else { @() }
        $expectedEmbedded = if ($embeddedByComponent.ContainsKey($identifier)) {
            @(Get-W4Utf8SortedStrings -Values @($embeddedByComponent[$identifier]) -Unique)
        } else { @() }
        if (($payloadPaths | ConvertTo-Json -Compress) -cne
                ($expectedPayload | ConvertTo-Json -Compress) -or
            ($embeddedPaths | ConvertTo-Json -Compress) -cne
                ($expectedEmbedded | ConvertTo-Json -Compress) -or
            ($sourcePaths | ConvertTo-Json -Compress) -cne
                ($expectedSourcePaths | ConvertTo-Json -Compress)) {
            throw "release inventory component path coverage is not exact: $identifier"
        }
    }
    $classificationComponents = @(
        $Inventory.components | Where-Object {
            [string]$_.identity_status -ceq 'classification-only'
        }
    )
    foreach ($component in @($Inventory.components)) {
        $componentPaths = @(
            @($component.payload_paths) + @($component.embedded_paths) +
            @($component.source_paths) | ForEach-Object { [string]$_ }
        )
        $expectedClassifications = [System.Collections.Generic.List[string]]::new()
        foreach ($classificationComponent in $classificationComponents) {
            if ([string]$classificationComponent.id -ceq [string]$component.id) {
                continue
            }
            $classificationPaths = @(
                @($classificationComponent.payload_paths) +
                @($classificationComponent.embedded_paths) +
                @($classificationComponent.source_paths) |
                    ForEach-Object { [string]$_ }
            )
            if (@($componentPaths | Where-Object {
                $classificationPaths -ccontains [string]$_
            }).Count -gt 0) {
                $expectedClassifications.Add([string]$classificationComponent.id)
            }
        }
        $expectedClassificationIds = @(
            Get-W4Utf8SortedStrings -Values @($expectedClassifications) -Unique
        )
        if ((@($component.classification_ids) | ConvertTo-Json -Compress) -cne
            ($expectedClassificationIds | ConvertTo-Json -Compress)) {
            throw "release inventory component classification binding is invalid: $($component.id)"
        }
    }

    $portableBinding = [ordered]@{
        analysis_graph_sha256 = [string]$bindings.analysis_graph_sha256
        artifact_path_base = [string]$bindings.artifact_path_base
        conda_native_registry_sha256 = [string]$bindings.conda_native_registry_sha256
        embedded_archives_sha256 = [string]$bindings.embedded_archives_sha256
        payload_tree_sha256 = [string]$bindings.payload_tree_sha256
    }
    if ((Get-W4CanonicalJsonSha256 -Value $portableBinding) -cne
        [string]$bindings.closure_sha256) {
        throw 'release inventory portable closure hash is invalid'
    }
    $artifactClosure = [ordered]@{
        authority = $authority
        inventory_closure_sha256 = [string]$bindings.closure_sha256
    }
    if ((Get-W4CanonicalJsonSha256 -Value $artifactClosure) -cne
        [string]$bindings.artifact_closure_sha256) {
        throw 'release inventory authority/artifact closure hash is invalid'
    }
    if ([string]$bindings.closure_sha256 -cne
            [string]$BuildInfo.release_inventory_closure_sha256 -or
        [string]$bindings.closure_sha256 -cne
            [string]$Provenance.release_inventory_closure_sha256 -or
        [string]$bindings.artifact_closure_sha256 -cne
            [string]$BuildInfo.release_inventory_artifact_closure_sha256 -or
        [string]$bindings.artifact_closure_sha256 -cne
            [string]$Provenance.release_inventory_artifact_closure_sha256) {
        throw 'release inventory closure hashes are not bound by build-info/provenance'
    }
    return [pscustomobject]@{
        Archives = $archives
        Components = @($Inventory.components)
        ComponentById = $knownComponentIds
        EmbeddedEntryCount = $totalEmbeddedEntries
        PayloadTreeSha256 = $payloadTreeSha
    }
}

function New-W4ScenarioContext {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$RunContext,
        [Parameter(Mandatory = $true)]$Scenario
    )

    $slug = Get-W4ScenarioSlug -ScenarioId ([string]$Scenario.scenario_id)
    $workspace = Join-Path (Join-Path $RunContext.WorkspaceRoot 'scenarios') $slug
    $evidence = Join-Path (Join-Path $RunContext.RunRoot 'scenarios') $slug
    # Install.ps1 intentionally permits only the current synthetic user's
    # LOCALAPPDATA\Programs authority.  Keep every installed Artifact under
    # that boundary while the process cwd remains a disjoint external folder.
    $isolatedLocalAppData = Join-Path $workspace 'profile\AppData\Local'
    $installRoot = Join-Path $isolatedLocalAppData 'Programs\PersonalKnowledgeVault'
    $userDataRoot = Join-Path $workspace 'user-data'
    $cwd = Join-Path $workspace 'cwd'
    foreach ($directory in @($workspace, $evidence, $userDataRoot, $cwd)) {
        [void][System.IO.Directory]::CreateDirectory($directory)
    }
    $environment = New-W4IsolatedEnvironment -WorkspaceRoot $workspace `
        -UserDataRoot $userDataRoot -ArtifactRoot $installRoot
    return [pscustomobject]@{
        ScenarioId = [string]$Scenario.scenario_id
        Scenario = $Scenario
        Workspace = $workspace
        Evidence = $evidence
        InstallRoot = $installRoot
        UserDataRoot = $userDataRoot
        WorkingDirectory = $cwd
        Environment = $environment
        UiaContract = $RunContext.Contract.uia
        PkvExe = Join-Path $installRoot 'app\pkv.exe'
        GuiExe = Join-Path $installRoot 'app\pkv-gui.exe'
        McpExe = Join-Path $installRoot 'app\pkv-mcp.exe'
    }
}

function Remove-W4ScenarioInstallRoot {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$ScenarioContext)

    $workspace = [System.IO.Path]::GetFullPath([string]$ScenarioContext.Workspace).TrimEnd('\')
    $installRoot = [System.IO.Path]::GetFullPath([string]$ScenarioContext.InstallRoot).TrimEnd('\')
    $expectedInstallRoot = [System.IO.Path]::GetFullPath(
        (Join-Path $workspace 'profile\AppData\Local\Programs\PersonalKnowledgeVault')
    ).TrimEnd('\')
    $userDataRoot = [System.IO.Path]::GetFullPath([string]$ScenarioContext.UserDataRoot).TrimEnd('\')
    $evidenceRoot = [System.IO.Path]::GetFullPath([string]$ScenarioContext.Evidence).TrimEnd('\')
    [void][System.IO.Directory]::CreateDirectory($evidenceRoot)

    $recordPath = Join-Path $evidenceRoot 'install-cleanup.json'
    $userDataExistedBefore = Test-Path -LiteralPath $userDataRoot
    $evidenceExistedBefore = Test-Path -LiteralPath $evidenceRoot -PathType Container
    $treeManifest = @()
    try {
        if (-not $installRoot.Equals($expectedInstallRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
            -not (Test-W4PathContainedBy -Candidate $installRoot -Root $workspace)) {
            throw "Scenario InstallRoot is outside its exact synthetic authority: $installRoot"
        }
        Assert-W4DisjointPaths -First $installRoot -FirstLabel 'Scenario install root' `
            -Second $userDataRoot -SecondLabel 'Scenario user data root'
        Assert-W4DisjointPaths -First $installRoot -FirstLabel 'Scenario install root' `
            -Second $evidenceRoot -SecondLabel 'Scenario evidence root'

        $status = 'already_absent'
        if (Test-Path -LiteralPath $installRoot) {
            if (-not (Test-Path -LiteralPath $installRoot -PathType Container)) {
                throw "Scenario InstallRoot is not a directory: $installRoot"
            }
            # Hashing through the shared safe-tree walker also rejects nested
            # reparse points and multi-link files before recursive deletion.
            $treeManifest = @(Get-W4TreeManifest -Root $installRoot)
            [System.IO.Directory]::Delete($installRoot, $true)
            $status = 'removed'
        }
        if (Test-Path -LiteralPath $installRoot) {
            throw "Scenario InstallRoot remained after cleanup: $installRoot"
        }
        if ($userDataExistedBefore -and -not (Test-Path -LiteralPath $userDataRoot)) {
            throw 'InstallRoot cleanup changed the disjoint scenario user-data root'
        }
        if ($evidenceExistedBefore -and -not (Test-Path -LiteralPath $evidenceRoot -PathType Container)) {
            throw 'InstallRoot cleanup changed the disjoint scenario evidence root'
        }
        $treeJson = ConvertTo-Json -InputObject @($treeManifest) -Depth 5 -Compress
        Write-W4JsonFile -Path $recordPath -Value ([ordered]@{
            schema_version = 'pkv.m13.w4-install-cleanup.v1'
            status = $status
            install_root = $installRoot
            removed_file_count = @($treeManifest).Count
            removed_tree_sha256 = Get-W4StringSha256 -Value $treeJson
            user_data_existed_before = [bool]$userDataExistedBefore
            user_data_preserved_by_cleanup = (-not $userDataExistedBefore -or
                (Test-Path -LiteralPath $userDataRoot))
            evidence_preserved_by_cleanup = (Test-Path -LiteralPath $evidenceRoot -PathType Container)
        })
    } catch {
        $cleanupError = $_
        try {
            Write-W4JsonFile -Path $recordPath -Value ([ordered]@{
                schema_version = 'pkv.m13.w4-install-cleanup.v1'
                status = 'failed'
                install_root = $installRoot
                error = $cleanupError.Exception.Message
                user_data_existed_before = [bool]$userDataExistedBefore
                user_data_exists_after = (Test-Path -LiteralPath $userDataRoot)
                evidence_exists_after = (Test-Path -LiteralPath $evidenceRoot -PathType Container)
            })
        } catch {
            # The original cleanup boundary failure is more actionable than a
            # secondary inability to persist its diagnostic record.
        }
        throw $cleanupError
    }
}

function Invoke-W4Install {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$RunContext,
        [Parameter(Mandatory = $true)]$ScenarioContext,
        [int[]]$ExpectedExitCodes = @(0),
        [string]$EvidenceName = 'install'
    )

    $installScript = Join-Path $RunContext.ArtifactRoot 'Install.ps1'
    $result = Invoke-W4Process -FileName $RunContext.PowerShellHost `
        -Arguments @(
            '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
            '-File', $installScript, '-InstallRoot', $ScenarioContext.InstallRoot,
            '-AllowComplianceHoldTestCandidate',
            '-ComplianceHoldConfirmation', 'W4-TEST-CANDIDATE-NOT-FOR-DISTRIBUTION'
        ) -WorkingDirectory $ScenarioContext.WorkingDirectory `
        -Environment $ScenarioContext.Environment `
        -EvidenceDirectory (Join-Path $ScenarioContext.Evidence $EvidenceName) `
        -ExpectedExitCodes $ExpectedExitCodes -TimeoutSeconds 120
    if ($ExpectedExitCodes -contains 0 -and $result.ExitCode -eq 0) {
        foreach ($path in @(
            $ScenarioContext.PkvExe,
            $ScenarioContext.GuiExe,
            $ScenarioContext.McpExe,
            (Join-Path $ScenarioContext.InstallRoot 'install-state.json')
        )) {
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
                throw "Installer omitted required installed file: $path"
            }
        }
    }
    return $result
}

function Invoke-W4Uninstall {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$RunContext,
        [Parameter(Mandatory = $true)]$ScenarioContext,
        [switch]$DeleteUserData,
        [string]$EvidenceName = 'uninstall'
    )

    $uninstallScript = Join-Path $ScenarioContext.InstallRoot 'Uninstall.ps1'
    if (-not (Test-Path -LiteralPath $uninstallScript -PathType Leaf)) {
        # The source distribution copy remains available even after a partially
        # removed installation, but the normal supported entrypoint is installed.
        throw "Installed uninstaller is missing: $uninstallScript"
    }
    $arguments = @(
        '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-File', $uninstallScript,
        '-InstallRoot', $ScenarioContext.InstallRoot
    )
    if ($DeleteUserData) {
        $arguments += @(
            '-DeleteUserData',
            '-ConfirmDataDeletion',
            'DELETE-PKV-USER-DATA'
        )
    }
    return Invoke-W4Process -FileName $RunContext.PowerShellHost `
        -Arguments $arguments -WorkingDirectory $ScenarioContext.WorkingDirectory `
        -Environment $ScenarioContext.Environment `
        -EvidenceDirectory (Join-Path $ScenarioContext.Evidence $EvidenceName) `
        -ExpectedExitCodes @(0) -TimeoutSeconds 120
}

function Initialize-W4InstalledDataRoot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$ScenarioContext,
        [string]$EvidenceName = 'initialize-data-root'
    )

    return Invoke-W4Process -FileName $ScenarioContext.PkvExe `
        -Arguments @('stats') -WorkingDirectory $ScenarioContext.WorkingDirectory `
        -Environment $ScenarioContext.Environment `
        -EvidenceDirectory (Join-Path $ScenarioContext.Evidence $EvidenceName) `
        -ExpectedExitCodes @(0) -TimeoutSeconds 60
}

function Assert-W4SetEqual {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$Actual,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$Expected,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $actualRaw = @($Actual | ForEach-Object { [string]$_ })
    $expectedRaw = @($Expected | ForEach-Object { [string]$_ })
    foreach ($set in @($actualRaw, $expectedRaw)) {
        $seen = [System.Collections.Generic.HashSet[string]]::new(
            [System.StringComparer]::OrdinalIgnoreCase
        )
        foreach ($value in $set) {
            if (-not $seen.Add([string]$value)) {
                throw "$Label contains a duplicate or case-colliding value: $value"
            }
        }
    }
    $actualStrings = @(Get-W4Utf8SortedStrings -Values $actualRaw)
    $expectedStrings = @(Get-W4Utf8SortedStrings -Values $expectedRaw)
    if (($actualStrings | ConvertTo-Json -Compress) -cne
        ($expectedStrings | ConvertTo-Json -Compress)) {
        throw "$Label mismatch. actual=$($actualStrings -join ',') expected=$($expectedStrings -join ',')"
    }
}

function ConvertFrom-W4McpTextContent {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Result,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if ($Result.PSObject.Properties.Name -contains 'isError' -and [bool]$Result.isError) {
        throw "$Label returned isError=true"
    }
    if ($Result.PSObject.Properties.Name -notcontains 'content' -or @($Result.content).Count -ne 1) {
        throw "$Label must return exactly one public TextContent"
    }
    $content = @($Result.content)[0]
    if ([string]$content.type -ne 'text') {
        throw "$Label content type is not text"
    }
    return ConvertFrom-W4StrictJsonText -Text ([string]$content.text) -Label $Label
}

function Invoke-W4McpSeedText {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$RunContext,
        [Parameter(Mandatory = $true)]$ScenarioContext,
        [Parameter(Mandatory = $true)][string]$Text,
        [Parameter(Mandatory = $true)][string]$Title,
        [string]$EvidenceName = 'mcp-seed'
    )

    $evidence = Join-Path $ScenarioContext.Evidence $EvidenceName
    [void][System.IO.Directory]::CreateDirectory($evidence)
    $session = Start-W4McpSession -Executable $ScenarioContext.McpExe `
        -WorkingDirectory $ScenarioContext.WorkingDirectory `
        -Environment $ScenarioContext.Environment `
        -TranscriptPath (Join-Path $evidence 'transcript.jsonl') `
        -ProtocolVersion ([string]$RunContext.Contract.mcp.protocol_version)
    try {
        $result = Invoke-W4McpRequest -Process $session.Process -Id 2 `
            -Method 'tools/call' -Params ([ordered]@{
                name = 'archive_text'
                arguments = [ordered]@{ text = $Text; title = $Title }
            }) -TranscriptPath $session.TranscriptPath -TimeoutSeconds 60
        $payload = ConvertFrom-W4McpTextContent -Result $result -Label 'archive_text'
        if (@('success', 'degraded') -notcontains [string]$payload.status) {
            throw "archive_text seed did not reach success/degraded: $($payload.status)"
        }
        Write-W4JsonFile -Path (Join-Path $evidence 'oracle.json') -Value $payload
    } finally {
        if ($null -ne $session.Process -and -not $session.Process.HasExited) {
            [void](Stop-W4McpSession -Session $session -EvidenceDirectory $evidence)
        }
    }
}

function Get-W4DataStateDigest {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$ScenarioContext)

    return Get-W4TreeSha256 -Root $ScenarioContext.UserDataRoot `
        -ExcludedRelativePrefixes @('logs', 'tmp')
}

function Invoke-W4ReleaseAuditScenario {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$RunContext,
        [Parameter(Mandatory = $true)]$ScenarioContext
    )

    $expectedComplianceBlockers = @(
        'conda-native-license-materials-and-spdx',
        'html2text-gpl-compliance',
        'native-msvc-license-and-provenance',
        'qt-corresponding-source-location',
        'qt-linkage-and-replacement-not-proven',
        'qt-module-license-audit',
        'qt-notice-placeholders'
    )
    $blockerAuthoritySha = Assert-W4ReleaseBlockerAuthority `
        -Rows @($RunContext.Provenance.release_blocker_authority) `
        -DeclaredSha256 ([string]$RunContext.Provenance.release_blocker_authority_sha256) `
        -ExpectedIds $expectedComplianceBlockers -Label 'candidate release blocker authority'
    $requiredFiles = @($RunContext.Contract.required_artifact_files | ForEach-Object { [string]$_ })
    foreach ($relative in $requiredFiles) {
        $candidate = Join-Path $RunContext.ArtifactRoot ($relative.Replace('/', '\'))
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            throw "Release payload is missing required file: $relative"
        }
    }
    $holdPath = Join-Path $RunContext.ArtifactRoot 'COMPLIANCE-HOLD.txt'
    $holdLines = @([System.IO.File]::ReadAllLines($holdPath, [System.Text.Encoding]::UTF8))
    if ($holdLines.Count -lt 1 -or $holdLines[0] -cne
            'TEST CANDIDATE - COMPLIANCE HOLD - NOT FOR DISTRIBUTION') {
        throw 'COMPLIANCE-HOLD.txt does not carry the exact test-candidate warning'
    }
    foreach ($blocker in $expectedComplianceBlockers) {
        if (@($holdLines | Where-Object { $_ -ceq "- $blocker" }).Count -ne 1) {
            throw "COMPLIANCE-HOLD.txt does not list canonical blocker exactly once: $blocker"
        }
    }
    $forbiddenPatterns = @(
        '(^|/)(tests?|fixtures?|evals?)(/|$)',
        '(^|/)(\.env[^/]*|local\.ya?ml)$',
        '(^|/)(vault|\.data|logs?|tmp)(/|$)',
        '(^|/).*\.(db|sqlite|sqlite3|py|pyc|pyo)$',
        '(^|/)(pkv-loopback-provider|pkv-w4-driver)\.exe$',
        '(^|/)(e2e-driver|e2e-harness)(/|$)',
        '(^|/)(Invoke-W4ArtifactE2E\.ps1|W4\.(Driver|Scenarios)\.psm1|scenarios\.v1\.json|driver-manifest(\.sha256|\.json)?)$'
    )
    $actualTree = Get-W4TreeManifest -Root $RunContext.ArtifactRoot
    foreach ($item in $actualTree) {
        $path = ([string]$item.path).Replace('\', '/')
        foreach ($pattern in $forbiddenPatterns) {
            if ($path -match $pattern) {
                throw "Release payload contains forbidden path: $path"
            }
        }
    }

    $buildInfo = $RunContext.BuildInfo
    Assert-W4ExactObjectFields -Object $buildInfo `
        -Fields @(
            'schema_version', 'version', 'target', 'source_revision',
            'source_tree_clean', 'source_date_epoch', 'zip_timestamp_epoch',
            'build_fingerprint', 'artifact_kind', 'artifact_status',
            'compliance_manifest_sha256', 'conda_hardlink_threat_evidence',
            'release_blockers', 'release_eligible',
            'release_blocker_authority', 'release_blocker_authority_sha256',
            'release_inventory_artifact_closure_sha256',
            'release_inventory_closure_sha256', 'release_inventory_path',
            'release_inventory_sha256',
            'inputs', 'toolchain'
        ) `
        -Label 'build-info.json'
    Assert-W4ExactBoolean -Value $buildInfo.source_tree_clean -Expected $true `
        -Label 'build-info source_tree_clean'
    Assert-W4ExactBoolean -Value $buildInfo.release_eligible -Expected $false `
        -Label 'build-info release_eligible'
    Assert-W4ExactBoolean -Value $RunContext.Provenance.release_eligible -Expected $false `
        -Label 'Artifact provenance release_eligible'
    Assert-W4ExactBoolean -Value $RunContext.ComplianceManifest.release_eligible -Expected $false `
        -Label 'compliance manifest release_eligible'
    Assert-W4ExactBoolean -Value $RunContext.ComplianceProvenance.release_eligible -Expected $false `
        -Label 'compliance provenance release_eligible'
    if ([string]$buildInfo.schema_version -ne 'pkv.build-info.v1') {
        throw "Unexpected build-info schema: $($buildInfo.schema_version)"
    }
    if ([string]$buildInfo.version -ne [string]$RunContext.Contract.artifact_version) {
        throw "Artifact version mismatch: $($buildInfo.version)"
    }
    if ([string]$buildInfo.target -ne 'windows-x86_64' -or
        [string]$buildInfo.artifact_kind -ne 'test_candidate' -or
        [string]$buildInfo.artifact_status -ne 'test-candidate-on-compliance-hold' -or
        [bool]$buildInfo.release_eligible -or
        (@($buildInfo.release_blockers) | ConvertTo-Json -Compress) -cne
            ($expectedComplianceBlockers | ConvertTo-Json -Compress) -or
        [string]$buildInfo.release_blocker_authority_sha256 -cne $blockerAuthoritySha -or
        (@($buildInfo.release_blocker_authority) | ConvertTo-Json -Depth 20 -Compress) -cne
            (@($RunContext.Provenance.release_blocker_authority) |
                ConvertTo-Json -Depth 20 -Compress)) {
        throw 'build-info does not declare the frozen compliance-hold test candidate'
    }
    if (-not [bool]$buildInfo.source_tree_clean) {
        throw 'Release Artifact was not built from a clean source tree'
    }
    if ([string]$buildInfo.source_revision -cnotmatch '^[0-9a-f]{40}$') {
        throw 'build-info source_revision is not a full hexadecimal revision'
    }
    if ([string]$buildInfo.build_fingerprint -notmatch '^[0-9a-f]{64}$') {
        throw 'build-info build_fingerprint is not SHA-256'
    }
    $sourceEpoch = [int64]$buildInfo.source_date_epoch
    $minimumZipEpoch = [Math]::Max($sourceEpoch, [int64]315532800)
    $expectedZipEpoch = $minimumZipEpoch - ($minimumZipEpoch % 2)
    if ($sourceEpoch -le 0 -or [int64]$buildInfo.zip_timestamp_epoch -ne $expectedZipEpoch) {
        throw 'build-info source/ZIP timestamp epoch invariant is invalid'
    }
    $inputNames = @($buildInfo.inputs.PSObject.Properties.Name)
    if ($inputNames.Count -lt 1) {
        throw 'build-info inputs map is empty'
    }
    foreach ($inputProperty in @($buildInfo.inputs.PSObject.Properties)) {
        if ([string]::IsNullOrWhiteSpace([string]$inputProperty.Name) -or
            [string]$inputProperty.Value -cnotmatch '^[0-9a-f]{64}$') {
            throw "build-info input hash is invalid: $($inputProperty.Name)"
        }
    }
    Assert-W4ExactObjectFields -Object $buildInfo.toolchain -Fields @(
        'build_environment_contract', 'conda_hardlink_threat_evidence', 'git',
        'pyinstaller_bootloaders',
        'python_executable_sha256', 'python_implementation', 'python_version',
        'release_build_distributions', 'zlib_runtime_version', 'zlib_version'
    ) -Label 'build-info toolchain'
    Assert-W4BuildEnvironmentContract `
        -Contract $buildInfo.toolchain.build_environment_contract
    [void](Assert-W4CondaHardlinkThreatEvidence `
        -Evidence $buildInfo.conda_hardlink_threat_evidence `
        -Label 'build-info Conda hardlink threat evidence')
    if (($buildInfo.conda_hardlink_threat_evidence | ConvertTo-Json -Depth 20 -Compress) -cne
            ($buildInfo.toolchain.conda_hardlink_threat_evidence |
                ConvertTo-Json -Depth 20 -Compress) -or
        ($buildInfo.conda_hardlink_threat_evidence | ConvertTo-Json -Depth 20 -Compress) -cne
            ($RunContext.Provenance.conda_hardlink_threat_evidence |
                ConvertTo-Json -Depth 20 -Compress)) {
        throw 'Conda hardlink threat evidence is not exactly bound by build-info/toolchain/provenance'
    }
    Assert-W4ExactObjectFields -Object $buildInfo.toolchain.git -Fields @(
        'path', 'runtime_files', 'sha256', 'size', 'system_dll_policy', 'version'
    ) -Label 'build-info Git toolchain'
    if ([string]$buildInfo.toolchain.git.path -cne 'C:/Program Files/Git/mingw64/bin/git.exe' -or
        [string]$buildInfo.toolchain.git.version -cne 'git version 2.54.0.windows.1' -or
        [string]$buildInfo.toolchain.git.sha256 -cne
            'cab4c4eea1d869cf9f7be73868dc9a90ad2df1b1b673e5f8c8714a576c25ea96' -or
        [int64]$buildInfo.toolchain.git.size -ne 4422544 -or
        [string]$buildInfo.toolchain.git.system_dll_policy -cne
            'Windows system DLLs are target-platform inputs') {
        throw 'build-info Git toolchain identity is invalid'
    }
    $expectedGitRuntimeFiles = [ordered]@{
        'libiconv-2.dll' = [ordered]@{
            sha256 = '7a282a854e01be726c6cccfe46f548c716aa45b3014818468253aaa4efbcd067'
            size = 1143148
        }
        'libintl-8.dll' = [ordered]@{
            sha256 = '0537c3dd2378218508ebe3cc416d72a99ee2d24ae1c5525e23458f32544ef861'
            size = 298731
        }
        'libpcre2-8-0.dll' = [ordered]@{
            sha256 = 'c135a87ed0f11eae8ffc4cb469671ff0b3f5d71fab5fb024e9b1e7241ca25b52'
            size = 717955
        }
        'libwinpthread-1.dll' = [ordered]@{
            sha256 = '851f61482ad5b6aac7c6abc54bbe31d24f89e0ca683a75fcec2d47f86b2d2242'
            size = 65442
        }
        'zlib1.dll' = [ordered]@{
            sha256 = '93e9243a44c29200eeacaf9658efe2558581770e4b11ca4b500e18e424a6e3b5'
            size = 128488
        }
    }
    Assert-W4ExactObjectFields -Object $buildInfo.toolchain.git.runtime_files `
        -Fields @($expectedGitRuntimeFiles.Keys) -Label 'build-info Git runtime files'
    foreach ($runtimeName in @($expectedGitRuntimeFiles.Keys)) {
        $runtime = $buildInfo.toolchain.git.runtime_files.$runtimeName
        $expectedRuntime = $expectedGitRuntimeFiles[$runtimeName]
        Assert-W4ExactObjectFields -Object $runtime -Fields @('sha256', 'size') `
            -Label "build-info Git runtime file $runtimeName"
        if ([string]$runtime.sha256 -cne [string]$expectedRuntime.sha256 -or
            [int64]$runtime.size -ne [int64]$expectedRuntime.size) {
            throw "build-info Git runtime file identity is invalid: $runtimeName"
        }
    }
    Assert-W4ExactObjectFields -Object $buildInfo.toolchain.pyinstaller_bootloaders `
        -Fields @('run.exe', 'runw.exe') -Label 'PyInstaller bootloader hashes'
    foreach ($hashField in @(
        [string]$buildInfo.toolchain.python_executable_sha256,
        [string]$buildInfo.toolchain.pyinstaller_bootloaders.'run.exe',
        [string]$buildInfo.toolchain.pyinstaller_bootloaders.'runw.exe'
    )) {
        if ($hashField -cnotmatch '^[0-9a-f]{64}$') {
            throw 'build-info toolchain contains a non-canonical executable hash'
        }
    }
    foreach ($value in @(
        [string]$buildInfo.toolchain.python_implementation,
        [string]$buildInfo.toolchain.python_version,
        [string]$buildInfo.toolchain.zlib_runtime_version,
        [string]$buildInfo.toolchain.zlib_version
    )) {
        if ([string]::IsNullOrWhiteSpace($value)) {
            throw 'build-info toolchain contains an empty runtime identity'
        }
    }
    $fingerprintDocument = [ordered]@{
        inputs = $buildInfo.inputs
        revision = [string]$buildInfo.source_revision
        source_date_epoch = $sourceEpoch
        target = 'windows-x86_64'
        toolchain = $buildInfo.toolchain
        version = [string]$buildInfo.version
    }
    $recomputedBuildFingerprint = Get-W4CanonicalJsonSha256 -Value $fingerprintDocument
    if ($recomputedBuildFingerprint -cne [string]$buildInfo.build_fingerprint) {
        throw 'build-info build_fingerprint does not match canonical build inputs/toolchain recomputation'
    }
    $complianceInputName = 'packaging/compliance-sources.v1.json'
    $complianceInput = @($buildInfo.inputs.PSObject.Properties | Where-Object {
        $_.Name -ceq $complianceInputName
    })
    if ($complianceInput.Count -ne 1 -or
        [string]$complianceInput[0].Value -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$complianceInput[0].Value -cne [string]$buildInfo.compliance_manifest_sha256 -or
        [string]$RunContext.Provenance.compliance_manifest_sha256 -cne
            [string]$buildInfo.compliance_manifest_sha256 -or
        [string]$RunContext.Provenance.artifact_kind -cne [string]$buildInfo.artifact_kind -or
        [string]$RunContext.Provenance.artifact_status -cne [string]$buildInfo.artifact_status -or
        [bool]$RunContext.Provenance.release_eligible -or
        (@($RunContext.Provenance.release_blockers) | ConvertTo-Json -Compress) -cne
            ($expectedComplianceBlockers | ConvertTo-Json -Compress)) {
        throw 'compliance manifest hash/status/blockers are not exactly cross-bound by build-info and provenance'
    }

    $payload = $RunContext.PayloadManifest
    Assert-W4ExactObjectFields -Object $payload `
        -Fields @(
            'schema_version', 'build_fingerprint', 'self_excluded_paths',
            'entries', 'tree_sha256'
        ) `
        -Label 'payload-manifest.json'
    if ([string]$payload.schema_version -ne 'pkv.payload-manifest.v1') {
        throw "Unexpected payload manifest schema: $($payload.schema_version)"
    }
    if ([string]$payload.build_fingerprint -ne [string]$buildInfo.build_fingerprint) {
        throw 'payload-manifest build_fingerprint disagrees with build-info'
    }
    if ((@($payload.self_excluded_paths) | ConvertTo-Json -Compress) -ne
        (@('payload-manifest.json') | ConvertTo-Json -Compress)) {
        throw 'payload-manifest self_excluded_paths must be exactly payload-manifest.json'
    }
    if ([string]$payload.tree_sha256 -notmatch '^[0-9a-f]{64}$') {
        throw 'payload-manifest tree_sha256 is not SHA-256'
    }
    $manifestRows = @($payload.entries)
    foreach ($row in $manifestRows) {
        Assert-W4ExactObjectFields -Object $row -Fields @('path', 'role', 'sha256', 'size') `
            -Label 'payload-manifest file row'
    }
    $manifestByPath = @{}
    foreach ($row in $manifestRows) {
        $key = ([string]$row.path).Replace('\', '/')
        if ($manifestByPath.ContainsKey($key.ToLowerInvariant())) {
            throw "payload-manifest contains a duplicate path: $key"
        }
        $manifestByPath[$key.ToLowerInvariant()] = $row
    }
    foreach ($actual in $actualTree) {
        $key = ([string]$actual.path).ToLowerInvariant()
        # A manifest cannot contain its own final hash; the payload contract may
        # deliberately omit only payload-manifest.json itself.
        if ($key -eq 'payload-manifest.json') {
            continue
        }
        if (-not $manifestByPath.ContainsKey($key)) {
            throw "payload-manifest omitted installed payload file: $($actual.path)"
        }
        $declared = $manifestByPath[$key]
        if ([int64]$declared.size -ne [int64]$actual.size -or
            [string]$declared.sha256 -ne [string]$actual.sha256) {
            throw "payload-manifest hash/size mismatch: $($actual.path)"
        }
    }
    foreach ($key in $manifestByPath.Keys) {
        $candidate = Join-Path $RunContext.ArtifactRoot ($key.Replace('/', '\'))
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            throw "payload-manifest declares a missing file: $key"
        }
    }
    $holdManifestRow = $manifestByPath['compliance-hold.txt']
    if ($null -eq $holdManifestRow -or [string]$holdManifestRow.role -cne 'compliance_hold' -or
        [string]$holdManifestRow.sha256 -cne (Get-W4FileSha256 -Path $holdPath)) {
        throw 'payload-manifest does not hash/bind COMPLIANCE-HOLD.txt with compliance_hold role'
    }

    $treeBuilder = [System.Text.StringBuilder]::new()
    foreach ($row in $manifestRows) {
        [void]$treeBuilder.Append([string]$row.path)
        [void]$treeBuilder.Append([char]0)
        [void]$treeBuilder.Append([string][int64]$row.size)
        [void]$treeBuilder.Append([char]0)
        [void]$treeBuilder.Append([string]$row.sha256)
        [void]$treeBuilder.Append("`n")
    }
    $recomputedTreeSha = Get-W4StringSha256 -Value $treeBuilder.ToString()
    if ($recomputedTreeSha -ne [string]$payload.tree_sha256) {
        throw 'payload-manifest tree_sha256 does not match canonical path/NUL/size/NUL/hash/LF recomputation'
    }

    $dependencyPath = Join-Path $RunContext.ArtifactRoot 'dependency-manifest.json'
    $dependencyManifest = Read-W4JsonFile -Path $dependencyPath
    Assert-W4ExactObjectFields -Object $dependencyManifest -Fields @(
        'schema_version', 'artifact_status', 'release_eligible', 'release_blockers',
        'release_blocker_authority', 'release_blocker_authority_sha256',
        'environment_lock_sha256', 'license_index_path',
        'release_inventory_closure_sha256', 'release_inventory_path',
        'release_inventory_sha256', 'components'
    ) -Label 'dependency-manifest.json'
    Assert-W4ExactBoolean -Value $dependencyManifest.release_eligible -Expected $false `
        -Label 'dependency-manifest release_eligible'
    if ([string]$dependencyManifest.schema_version -ne 'pkv.dependency-manifest.v1' -or
        [string]$dependencyManifest.environment_lock_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$dependencyManifest.license_index_path -ne 'licenses/index.json' -or
        [string]$dependencyManifest.artifact_status -cne [string]$buildInfo.artifact_status -or
        [bool]$dependencyManifest.release_eligible -or
        (@($dependencyManifest.release_blockers) | ConvertTo-Json -Compress) -cne
            ($expectedComplianceBlockers | ConvertTo-Json -Compress) -or
        [string]$dependencyManifest.release_blocker_authority_sha256 -cne $blockerAuthoritySha -or
        (@($dependencyManifest.release_blocker_authority) | ConvertTo-Json -Depth 20 -Compress) -cne
            (@($RunContext.Provenance.release_blocker_authority) |
                ConvertTo-Json -Depth 20 -Compress) -or
        [string]$dependencyManifest.release_inventory_path -cne 'release-inventory.json' -or
        [string]$dependencyManifest.release_inventory_sha256 -cne
            [string]$RunContext.Provenance.release_inventory_sha256 -or
        [string]$dependencyManifest.release_inventory_closure_sha256 -cne
            [string]$RunContext.Provenance.release_inventory_closure_sha256) {
        throw 'dependency-manifest identity/hash/license index path is invalid'
    }
    Assert-W4ReleaseLockBinding -Inputs $buildInfo.inputs `
        -ExpectedSha256 ([string]$dependencyManifest.environment_lock_sha256)
    $inventoryEvidence = Assert-W4ReleaseInventory `
        -Inventory $RunContext.ReleaseInventory -ArtifactRoot $RunContext.ArtifactRoot `
        -BuildInfo $buildInfo -Provenance $RunContext.Provenance `
        -DependencyManifest $dependencyManifest `
        -ExpectedExecutablePaths @('app/pkv.exe', 'app/pkv-gui.exe', 'app/pkv-mcp.exe')
    $dependencies = @($dependencyManifest.components)
    if ($dependencies.Count -lt 1) {
        throw 'dependency-manifest contains no locked release components'
    }
    $dependencyKeys = [System.Collections.Generic.List[string]]::new()
    $dependencyByCanonicalName = @{}
    foreach ($component in $dependencies) {
        Assert-W4ExactObjectFields -Object $component -Fields @(
            'component_kind', 'installed_files_sha256', 'license', 'metadata_sha256',
            'name', 'purl', 'role', 'version'
        ) -Label 'dependency-manifest component'
        if ([string]$component.installed_files_sha256 -notmatch '^[0-9a-f]{64}$' -or
            [string]$component.metadata_sha256 -notmatch '^[0-9a-f]{64}$' -or
            [string]::IsNullOrWhiteSpace([string]$component.name) -or
            [string]::IsNullOrWhiteSpace([string]$component.version) -or
            [string]::IsNullOrWhiteSpace([string]$component.purl) -or
            [string]::IsNullOrWhiteSpace([string]$component.license)) {
            throw 'dependency-manifest component contains an invalid identity/hash/license field'
        }
        $dependencyKeys.Add("$([string]$component.name)`0$([string]$component.version)")
        $canonicalDependencyName = Get-W4CanonicalDistributionName `
            -Value ([string]$component.name)
        if ($dependencyByCanonicalName.ContainsKey($canonicalDependencyName)) {
            throw "dependency-manifest contains duplicate canonical name: $canonicalDependencyName"
        }
        $dependencyByCanonicalName[$canonicalDependencyName] = $component
    }
    Assert-W4Utf8SortedUniqueStrings -Values @($dependencyKeys) `
        -Label 'dependency-manifest canonical name/version keys'
    $expectedBuildDistributions = [ordered]@{}
    foreach ($buildDependency in @($dependencies | Where-Object { [string]$_.role -ceq 'build' })) {
        $expectedBuildDistributions[[string]$buildDependency.name] = [string]$buildDependency.version
    }
    if (($buildInfo.toolchain.release_build_distributions | ConvertTo-Json -Compress) -cne
        ($expectedBuildDistributions | ConvertTo-Json -Compress)) {
        throw 'build-info toolchain build distributions differ from dependency-manifest build components'
    }
    if ([string]$RunContext.HarnessManifest.build.toolchain_lock_sha256 -cne
        [string]$dependencyManifest.environment_lock_sha256) {
        throw 'external harness toolchain lock does not match the product dependency lock'
    }

    $licenseIndexPath = Join-Path $RunContext.ArtifactRoot 'licenses\index.json'
    $licenseIndex = Read-W4JsonFile -Path $licenseIndexPath
    Assert-W4ExactObjectFields -Object $licenseIndex `
        -Fields @('actual_runtime_inventory', 'entries', 'schema_version') `
        -Label 'licenses/index.json'
    if ([string]$licenseIndex.schema_version -ne 'pkv.license-index.v2') {
        throw 'licenses/index.json has an unexpected schema version'
    }
    Assert-W4ExactObjectFields -Object $licenseIndex.actual_runtime_inventory -Fields @(
        'components', 'release_inventory_closure_sha256', 'release_inventory_path'
    ) -Label 'licenses/index.json actual runtime inventory'
    if ([string]$licenseIndex.actual_runtime_inventory.release_inventory_path -cne
            'release-inventory.json' -or
        [string]$licenseIndex.actual_runtime_inventory.release_inventory_closure_sha256 -cne
            [string]$RunContext.ReleaseInventory.bindings.closure_sha256) {
        throw 'license index is not bound to the exact release inventory closure'
    }
    $licenseEntries = @($licenseIndex.entries)
    if ($licenseEntries.Count -ne $dependencies.Count) {
        throw 'license index does not map every dependency exactly once'
    }
    $mappedLicenseFiles = @{}
    $licenseEntryByName = @{}
    for ($componentIndex = 0; $componentIndex -lt $dependencies.Count; $componentIndex += 1) {
        $dependency = $dependencies[$componentIndex]
        $licenseEntry = $licenseEntries[$componentIndex]
        $licenseEntryFields = @(
            'name', 'version', 'purl', 'license_expression', 'license_files',
            'metadata_declared_license_files'
        )
        if ([string]$dependency.name -eq 'html2text') {
            $licenseEntryFields += 'corresponding_source'
        }
        Assert-W4ExactObjectFields -Object $licenseEntry -Fields $licenseEntryFields `
            -Label 'license index entry'
        if ([string]$licenseEntry.name -ne [string]$dependency.name -or
            [string]$licenseEntry.version -ne [string]$dependency.version -or
            [string]$licenseEntry.purl -ne [string]$dependency.purl -or
            [string]$licenseEntry.license_expression -ne [string]$dependency.license) {
            throw "license index entry does not exactly map dependency: $($dependency.name)"
        }
        $licenseFiles = @($licenseEntry.license_files)
        if ($licenseFiles.Count -lt 1) {
            throw "dependency has no packaged license material: $($dependency.name)"
        }
        $declaredHeaders = @($licenseEntry.metadata_declared_license_files | ForEach-Object { [string]$_ })
        Assert-W4Utf8SortedUniqueStrings -Values $declaredHeaders `
            -Label "metadata License-File headers $($dependency.name)" `
            -RejectCaseCollisions
        if ([string]$dependency.name -eq 'html2text') {
            Assert-W4ExactObjectFields -Object $licenseEntry.corresponding_source -Fields @(
                'distribution_path', 'sha256', 'size', 'source_url'
            ) -Label 'html2text corresponding source'
            if ([string]$licenseEntry.corresponding_source.distribution_path -ne
                    'dist/compliance-sources/html2text-2020.1.16.tar.gz' -or
                [string]$licenseEntry.corresponding_source.sha256 -notmatch '^[0-9a-f]{64}$' -or
                [int64]$licenseEntry.corresponding_source.size -le 0 -or
                -not ([string]$licenseEntry.corresponding_source.source_url).
                    StartsWith('https://', [System.StringComparison]::Ordinal)) {
                throw 'html2text corresponding-source declaration is invalid'
            }
            $externalFiles = @($RunContext.ComplianceManifest.files)
            if ($externalFiles.Count -ne 1 -or
                [string]$licenseEntry.corresponding_source.sha256 -cne
                    [string]$externalFiles[0].sha256 -or
                [int64]$licenseEntry.corresponding_source.size -ne
                    [int64]$externalFiles[0].size -or
                [string]$externalFiles[0].path -cne 'html2text-2020.1.16.tar.gz' -or
                [string]$RunContext.ComplianceProvenance.source_sha256 -cne
                    [string]$licenseEntry.corresponding_source.sha256) {
                throw 'license index corresponding source is not bound to the supplied external bundle'
            }
        }
        $componentPrefix = "licenses/$([string]$dependency.name)/"
        $metadataDeclaredRows = 0
        foreach ($licenseFile in $licenseFiles) {
            $sourceKind = [string]$licenseFile.source_kind
            if ($sourceKind -eq 'distribution') {
                Assert-W4ExactObjectFields -Object $licenseFile -Fields @(
                    'declared_by_metadata', 'path', 'sha256', 'source_kind',
                    'source_distribution_path', 'source_url'
                ) -Label 'distribution license index file row'
                Assert-W4ExactBoolean -Value $licenseFile.declared_by_metadata `
                    -Expected ([bool]$licenseFile.declared_by_metadata) `
                    -Label 'distribution license declared_by_metadata'
                if ([string]::IsNullOrWhiteSpace([string]$licenseFile.source_distribution_path) -or
                    $null -ne $licenseFile.source_url) {
                    throw 'distribution license provenance requires a distribution path and null source URL'
                }
                if ([bool]$licenseFile.declared_by_metadata) {
                    $metadataDeclaredRows += 1
                }
            } elseif ($sourceKind -eq 'compliance_asset') {
                Assert-W4ExactObjectFields -Object $licenseFile -Fields @(
                    'declared_by_metadata', 'path', 'sha256',
                    'source_distribution_path', 'source_kind', 'source_revision',
                    'source_sha256', 'source_url'
                ) -Label 'compliance asset license index file row'
                Assert-W4ExactBoolean -Value $licenseFile.declared_by_metadata -Expected $false `
                    -Label 'compliance asset declared_by_metadata'
                if ([bool]$licenseFile.declared_by_metadata -or
                    $null -ne $licenseFile.source_distribution_path -or
                    [string]$licenseFile.source_revision -notmatch '^[0-9a-f]{40}$' -or
                    [string]$licenseFile.source_sha256 -notmatch '^[0-9a-f]{64}$' -or
                    [string]$licenseFile.source_sha256 -cne [string]$licenseFile.sha256 -or
                    -not ([string]$licenseFile.source_url).
                        StartsWith('https://', [System.StringComparison]::Ordinal) -or
                    ([string]$licenseFile.source_url).IndexOf(
                        [string]$licenseFile.source_revision,
                        [System.StringComparison]::Ordinal
                    ) -lt 0) {
                    throw 'compliance license asset provenance is incomplete'
                }
            } elseif ($sourceKind -eq 'audited_override') {
                Assert-W4ExactObjectFields -Object $licenseFile -Fields @(
                    'audit_reason', 'path', 'sha256', 'source_distribution_path',
                    'source_kind', 'source_revision', 'source_sha256', 'source_url',
                    'vendored_normalization'
                ) -Label 'audited override license index file row'
                if ($null -ne $licenseFile.source_distribution_path -or
                    -not ([string]$licenseFile.source_url).StartsWith('https://', [System.StringComparison]::Ordinal) -or
                    [string]$licenseFile.source_revision -notmatch '^[0-9a-f]{40}$' -or
                    ([string]$licenseFile.source_url).IndexOf(
                        [string]$licenseFile.source_revision,
                        [System.StringComparison]::Ordinal
                    ) -lt 0 -or
                    [string]$licenseFile.source_sha256 -notmatch '^[0-9a-f]{64}$' -or
                    [string]$licenseFile.vendored_normalization -ne
                        'one trailing LF appended; all upstream content bytes otherwise unchanged' -or
                    [string]::IsNullOrWhiteSpace([string]$licenseFile.audit_reason)) {
                    throw 'audited license override provenance is incomplete'
                }
            } else {
                throw "license index file has an unknown source_kind: $sourceKind"
            }
            $relativeLicense = ([string]$licenseFile.path).Replace('\', '/')
            if (-not $relativeLicense.StartsWith($componentPrefix, [System.StringComparison]::Ordinal) -or
                $relativeLicense -match '(^|/)\.\.(/|$)' -or
                [string]$licenseFile.sha256 -notmatch '^[0-9a-f]{64}$') {
                throw "license file row violates the canonical dependency directory/hash/source contract: $relativeLicense"
            }
            $licenseKey = $relativeLicense.ToLowerInvariant()
            if ($mappedLicenseFiles.ContainsKey($licenseKey)) {
                throw "license file is mapped more than once: $relativeLicense"
            }
            $licenseFilePath = Join-Path $RunContext.ArtifactRoot ($relativeLicense.Replace('/', '\'))
            if (-not (Test-Path -LiteralPath $licenseFilePath -PathType Leaf) -or
                (Get-W4FileSha256 -Path $licenseFilePath) -ne [string]$licenseFile.sha256) {
                throw "license file is missing or differs from its index hash: $relativeLicense"
            }
            $mappedLicenseFiles[$licenseKey] = $true
        }
        if ($metadataDeclaredRows -ne $declaredHeaders.Count) {
            throw "License-File header count does not match declared license rows: $($dependency.name)"
        }
        $materialName = Get-W4CanonicalDistributionName -Value ([string]$licenseEntry.name)
        if ([string]::IsNullOrWhiteSpace($materialName) -or
            $licenseEntryByName.ContainsKey($materialName)) {
            throw "license index has a duplicate/invalid canonical component name: $materialName"
        }
        $licenseEntryByName[$materialName] = $licenseEntry
    }
    $actualLicenseFiles = @(
        Get-ChildItem -LiteralPath (Join-Path $RunContext.ArtifactRoot 'licenses') -File -Recurse |
            ForEach-Object {
                $_.FullName.Substring($RunContext.ArtifactRoot.TrimEnd('\').Length).
                    TrimStart('\').Replace('\', '/')
            } |
            Where-Object { $_ -ne 'licenses/index.json' }
    )
    if ($actualLicenseFiles.Count -ne $mappedLicenseFiles.Count -or
        @($actualLicenseFiles | Where-Object {
            -not $mappedLicenseFiles.ContainsKey(([string]$_).ToLowerInvariant())
        }).Count -ne 0) {
        throw 'licenses/index.json does not exactly cover the packaged dependency license files'
    }

    $guidePath = Join-Path $RunContext.ArtifactRoot 'USER-GUIDE.md'
    $guide = [System.IO.File]::ReadAllText($guidePath, [System.Text.Encoding]::UTF8)
    foreach ($requiredText in @(
        '0.8.1',
        'Install.ps1',
        '-InstallRoot',
        '-AllowComplianceHoldTestCandidate',
        '-ComplianceHoldConfirmation W4-TEST-CANDIDATE-NOT-FOR-DISTRIBUTION',
        '%LOCALAPPDATA%\Programs\PersonalKnowledgeVault',
        '%LOCALAPPDATA%\PersonalKnowledgeVault',
        'Uninstall.ps1',
        '-DeleteUserData',
        '-ConfirmDataDeletion DELETE-PKV-USER-DATA',
        'exit code 20',
        'pkv-mcp.exe --transport stdio',
        'find_bridges',
        'timeline_of',
        'contrast',
        'partial-v1',
        'backup',
        'restore'
    )) {
        if ($guide.IndexOf($requiredText, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) {
            throw "USER-GUIDE.md is missing required topic: $requiredText"
        }
    }
    $license = [System.IO.File]::ReadAllText((Join-Path $RunContext.ArtifactRoot 'LICENSE'))
    if ($license -notmatch 'MIT License') {
        throw 'LICENSE is not an MIT license document'
    }
    $notices = [System.IO.File]::ReadAllText((Join-Path $RunContext.ArtifactRoot 'THIRD-PARTY-NOTICES.txt'))
    $noticeFirstLine = @($notices -split '\r?\n')[0]
    if ([string]::IsNullOrWhiteSpace($notices) -or $noticeFirstLine -cne
            'TEST CANDIDATE - COMPLIANCE HOLD - NOT FOR DISTRIBUTION') {
        throw 'THIRD-PARTY-NOTICES.txt does not begin with the exact compliance-hold warning'
    }
    $sbom = Read-W4JsonFile -Path (Join-Path $RunContext.ArtifactRoot 'sbom.cdx.json')
    Assert-W4ExactObjectFields -Object $sbom `
        -Fields @('bomFormat', 'components', 'dependencies', 'metadata', 'specVersion', 'version') `
        -Label 'sbom.cdx.json'
    Assert-W4ExactObjectFields -Object $sbom.metadata `
        -Fields @('timestamp', 'component', 'properties') -Label 'SBOM metadata'
    Assert-W4ExactObjectFields -Object $sbom.metadata.component `
        -Fields @('bom-ref', 'name', 'type', 'version') -Label 'SBOM application component'
    if ([string]$sbom.bomFormat -ne 'CycloneDX' -or
        [string]$sbom.specVersion -ne '1.5' -or [int]$sbom.version -ne 1 -or
        [string]$sbom.metadata.component.'bom-ref' -ne
            "pkg:generic/personal-knowledge-vault@$([string]$RunContext.Contract.artifact_version)" -or
        [string]$sbom.metadata.component.name -ne 'Personal Knowledge Vault' -or
        [string]$sbom.metadata.component.type -ne 'application' -or
        [string]$sbom.metadata.component.version -ne [string]$RunContext.Contract.artifact_version) {
        throw 'sbom.cdx.json application identity/version is invalid'
    }
    $expectedSbomProperties = @(
        [ordered]@{ name = 'pkv:artifact-status'; value = 'test-candidate-on-compliance-hold' },
        [ordered]@{ name = 'pkv:compliance-manifest-sha256'; value = [string]$buildInfo.compliance_manifest_sha256 },
        [ordered]@{ name = 'pkv:release-eligible'; value = 'false' },
        [ordered]@{ name = 'pkv:release-blocker-authority-sha256'; value = $blockerAuthoritySha },
        [ordered]@{ name = 'pkv:release-inventory-closure-sha256'; value = [string]$RunContext.ReleaseInventory.bindings.closure_sha256 },
        [ordered]@{ name = 'pkv:release-inventory-path'; value = 'release-inventory.json' },
        [ordered]@{ name = 'pkv:release-inventory-sha256'; value = [string]$RunContext.Provenance.release_inventory_sha256 }
    ) + @($expectedComplianceBlockers | ForEach-Object {
        [ordered]@{ name = 'pkv:release-blocker'; value = [string]$_ }
    })
    $sbomProperties = @($sbom.metadata.properties)
    if ($sbomProperties.Count -ne $expectedSbomProperties.Count) {
        throw 'SBOM compliance property count is invalid'
    }
    for ($propertyIndex = 0; $propertyIndex -lt $expectedSbomProperties.Count; $propertyIndex += 1) {
        Assert-W4ExactObjectFields -Object $sbomProperties[$propertyIndex] `
            -Fields @('name', 'value') -Label 'SBOM compliance property'
        if ([string]$sbomProperties[$propertyIndex].name -cne
                [string]$expectedSbomProperties[$propertyIndex].name -or
            [string]$sbomProperties[$propertyIndex].value -cne
                [string]$expectedSbomProperties[$propertyIndex].value) {
            throw 'SBOM compliance properties do not exactly mirror the build/provenance hold'
        }
    }
    $expectedSbomInventoryIds = @(Get-W4Utf8SortedStrings -Values @(
        $inventoryEvidence.Components |
            Where-Object {
                [string]$_.identity_status -ceq 'complete' -and
                [string]$_.id -cne 'application:project'
            } |
            ForEach-Object { [string]$_.id }
    ))
    $sbomComponents = @($sbom.components)
    $runtimeLicenseComponents = @($licenseIndex.actual_runtime_inventory.components)
    if ($sbomComponents.Count -ne $expectedSbomInventoryIds.Count -or
        $runtimeLicenseComponents.Count -ne $expectedSbomInventoryIds.Count) {
        throw 'SBOM/license actual runtime identity count differs from release inventory'
    }
    $nativeAuthorityRows = @($RunContext.Provenance.release_blocker_authority | Where-Object {
        [string]$_.id -ceq 'conda-native-license-materials-and-spdx'
    })
    if ($nativeAuthorityRows.Count -ne 1) {
        throw 'native closure release blocker authority is missing or duplicated'
    }
    $affectedNativeSelectors = @(
        $nativeAuthorityRows[0].affected_component_selectors | ForEach-Object { [string]$_ }
    )
    $staticSbomLicenses = @{
        'build-runtime:pyinstaller-bootloader' = 'GPL-2.0-or-later WITH Bootloader-exception'
        'build-runtime:pyinstaller-hooks' = 'Apache-2.0'
        'build-runtime:pyinstaller-hooks-contrib' = 'Apache-2.0'
        'runtime:cpython' = 'Python-2.0'
    }
    $actualSbomIds = [System.Collections.Generic.List[string]]::new()
    for ($sbomIndex = 0; $sbomIndex -lt $expectedSbomInventoryIds.Count; $sbomIndex += 1) {
        $expectedComponentId = $expectedSbomInventoryIds[$sbomIndex]
        $inventoryComponent = $inventoryEvidence.ComponentById[$expectedComponentId]
        $component = $sbomComponents[$sbomIndex]
        $actualLicense = $runtimeLicenseComponents[$sbomIndex]
        $componentFields = @(
            'bom-ref', 'licenses', 'name', 'properties', 'purl', 'type', 'version'
        )
        Assert-W4ExactObjectFields -Object $component -Fields $componentFields `
            -Label 'SBOM runtime inventory component'
        $properties = @($component.properties)
        foreach ($property in $properties) {
            Assert-W4ExactObjectFields -Object $property -Fields @('name', 'value') `
                -Label 'SBOM runtime inventory property'
        }
        $expectedPropertyNames = @(
            'pkv:inventory-component-id', 'pkv:inventory-component-sha256',
            'pkv:inventory-identity-status', 'pkv:contains-native-payload',
            'pkv:license-material-status'
        )
        $expectedPropertyNames += @(
            @($inventoryComponent.payload_paths) | ForEach-Object { 'pkv:payload-path' }
        )
        $expectedPropertyNames += @(
            @($inventoryComponent.embedded_paths) | ForEach-Object { 'pkv:embedded-path' }
        )
        $expectedPropertyNames += @(
            @($inventoryComponent.classification_ids) |
                ForEach-Object { 'pkv:payload-classification' }
        )
        if ($expectedComponentId -ceq 'python-distribution:html2text') {
            $expectedPropertyNames += 'pkv:license-expression-status'
        }
        $condaPropertyKeys = @(
            'build', 'build_number', 'channel', 'package_sha256',
            'record_file', 'record_sha256', 'subdir'
        )
        if ($expectedComponentId.StartsWith(
            'conda-package:', [System.StringComparison]::Ordinal
        )) {
            $expectedPropertyNames += @($condaPropertyKeys | ForEach-Object {
                'pkv:conda-' + $_.Replace('_', '-')
            })
        }
        if ((@($properties | ForEach-Object { [string]$_.name }) |
                ConvertTo-Json -Compress) -cne
            ($expectedPropertyNames | ConvertTo-Json -Compress)) {
            throw "SBOM component property order/set is invalid: $expectedComponentId"
        }
        if ($properties.Count -lt 5 -or
            [string]$properties[0].name -cne 'pkv:inventory-component-id' -or
            [string]$properties[1].name -cne 'pkv:inventory-component-sha256' -or
            [string]$properties[2].name -cne 'pkv:inventory-identity-status' -or
            [string]$properties[3].name -cne 'pkv:contains-native-payload' -or
            [string]$properties[4].name -cne 'pkv:license-material-status') {
            throw "SBOM component lacks the ordered inventory identity prefix: $expectedComponentId"
        }
        $componentId = [string]$properties[0].value
        $componentSha = Get-W4CanonicalJsonSha256 -Value $inventoryComponent
        $licenseChoices = @($component.licenses)
        if ($licenseChoices.Count -ne 1) {
            throw "SBOM component must contain exactly one license choice: $expectedComponentId"
        }
        $licenseChoice = $licenseChoices[0]
        if ($expectedComponentId.StartsWith(
            'conda-package:', [System.StringComparison]::Ordinal
        )) {
            Assert-W4ExactObjectFields -Object $licenseChoice -Fields @('license') `
                -Label "SBOM Conda license choice $expectedComponentId"
            Assert-W4ExactObjectFields -Object $licenseChoice.license -Fields @('name') `
                -Label "SBOM Conda license name $expectedComponentId"
            $licenseIdentity = [string]$licenseChoice.license.name
        } else {
            Assert-W4ExactObjectFields -Object $licenseChoice -Fields @('expression') `
                -Label "SBOM license expression $expectedComponentId"
            $licenseIdentity = [string]$licenseChoice.expression
        }
        $inventoryPropertyNames = @($inventoryComponent.PSObject.Properties.Name)
        $expectedSbomName = [string]$inventoryComponent.name
        $expectedSbomVersion = [string]$inventoryComponent.version
        $expectedSbomPurl = if ($inventoryPropertyNames -ccontains 'purl') {
            [string]$inventoryComponent.purl
        } else { '' }
        $expectedSbomLicense = if ($inventoryPropertyNames -ccontains 'license') {
            [string]$inventoryComponent.license
        } else { '' }
        $lockedDependency = $null
        if ($expectedComponentId.StartsWith(
            'python-distribution:', [System.StringComparison]::Ordinal
        )) {
            $lockedName = Get-W4CanonicalDistributionName -Value (
                $expectedComponentId.Split(':', 2)[1]
            )
            if ($dependencyByCanonicalName.ContainsKey($lockedName)) {
                $lockedDependency = $dependencyByCanonicalName[$lockedName]
            }
        } elseif ($expectedComponentId -ceq 'runtime:cpython') {
            if ($dependencyByCanonicalName.ContainsKey('cpython')) {
                $lockedDependency = $dependencyByCanonicalName['cpython']
            }
        } elseif ($expectedComponentId -cin @(
            'build-runtime:pyinstaller-bootloader',
            'build-runtime:pyinstaller-hooks'
        )) {
            if ($dependencyByCanonicalName.ContainsKey('pyinstaller')) {
                $lockedDependency = $dependencyByCanonicalName['pyinstaller']
            }
        } elseif ($expectedComponentId -ceq 'build-runtime:pyinstaller-hooks-contrib') {
            if ($dependencyByCanonicalName.ContainsKey('pyinstaller-hooks-contrib')) {
                $lockedDependency = $dependencyByCanonicalName['pyinstaller-hooks-contrib']
            }
        }
        if ($null -ne $lockedDependency) {
            if ($expectedComponentId.StartsWith(
                'build-runtime:pyinstaller-', [System.StringComparison]::Ordinal
            )) {
                $expectedSbomVersion = [string]$lockedDependency.version
                $expectedSbomLicense = [string]$staticSbomLicenses[$expectedComponentId]
                $expectedSbomPurl = "pkg:generic/$($expectedComponentId.Replace(':', '-'))@$expectedSbomVersion"
            } else {
                $expectedSbomName = [string]$lockedDependency.name
                $expectedSbomVersion = [string]$lockedDependency.version
                $expectedSbomLicense = [string]$lockedDependency.license
                $expectedSbomPurl = [string]$lockedDependency.purl
            }
        } elseif ($expectedComponentId.StartsWith(
            'conda-package:', [System.StringComparison]::Ordinal
        )) {
            $expectedSbomLicense = [string]$inventoryComponent.declared_license
            $expectedSbomPurl = "pkg:conda/$expectedSbomName@$expectedSbomVersion" +
                "?build=$([string]$inventoryComponent.build)&subdir=$([string]$inventoryComponent.subdir)"
        } elseif ([string]::IsNullOrWhiteSpace($expectedSbomLicense) -and
            $staticSbomLicenses.ContainsKey($expectedComponentId)) {
            $expectedSbomLicense = [string]$staticSbomLicenses[$expectedComponentId]
        }
        if ($componentId -cne $expectedComponentId -or
            [string]$properties[1].value -cne $componentSha -or
            [string]$properties[2].value -cne 'complete' -or
            [string]$component.'bom-ref' -cne
                "urn:pkv:release-inventory-component:$componentSha" -or
            [string]$component.name -cne $expectedSbomName -or
            [string]$component.purl -cne $expectedSbomPurl -or
            [string]$component.version -cne $expectedSbomVersion -or
            $licenseIdentity -cne $expectedSbomLicense -or
            [string]$component.type -cne $(if (
                [string]$inventoryComponent.type -ceq 'framework'
            ) { 'framework' } else { 'library' }) -or
            [string]::IsNullOrWhiteSpace($licenseIdentity)) {
            throw "SBOM component is not the canonical release inventory identity: $expectedComponentId"
        }
        $classificationIds = @($inventoryComponent.classification_ids | ForEach-Object {
            [string]$_
        })
        $containsNativePayload = [bool]$inventoryComponent.contains_native_payload
        $expectedNativeProperty = if ($containsNativePayload) { 'true' } else { 'false' }
        if ([string]$properties[3].value -cne $expectedNativeProperty) {
            throw "SBOM component native-payload state differs from release inventory: $componentId"
        }
        $expectedMaterialStatus = Assert-W4LicenseMaterialStatusBinding `
            -ComponentId $componentId -InventoryComponent $inventoryComponent `
            -LicenseIndexComponent $actualLicense `
            -SbomStatus ([string]$properties[4].value)
        $payloadProperties = @($properties | Where-Object {
            [string]$_.name -ceq 'pkv:payload-path'
        } | ForEach-Object { [string]$_.value })
        $embeddedProperties = @($properties | Where-Object {
            [string]$_.name -ceq 'pkv:embedded-path'
        } | ForEach-Object { [string]$_.value })
        if (($payloadProperties | ConvertTo-Json -Compress) -cne
                (@($inventoryComponent.payload_paths) | ConvertTo-Json -Compress) -or
            ($embeddedProperties | ConvertTo-Json -Compress) -cne
                (@($inventoryComponent.embedded_paths) | ConvertTo-Json -Compress)) {
            throw "SBOM component paths differ from release inventory: $componentId"
        }
        $classificationProperties = @($properties | Where-Object {
            [string]$_.name -ceq 'pkv:payload-classification'
        } | ForEach-Object { [string]$_.value })
        if (($classificationProperties | ConvertTo-Json -Compress) -cne
            ($classificationIds | ConvertTo-Json -Compress)) {
            throw "SBOM payload classifications differ from release inventory: $componentId"
        }
        foreach ($condaKey in $condaPropertyKeys) {
            if (-not $expectedComponentId.StartsWith(
                'conda-package:', [System.StringComparison]::Ordinal
            )) {
                break
            }
            $condaPropertyName = 'pkv:conda-' + $condaKey.Replace('_', '-')
            $condaProperties = @($properties | Where-Object {
                [string]$_.name -ceq $condaPropertyName
            })
            if ($condaProperties.Count -ne 1 -or
                [string]$condaProperties[0].value -cne
                    [string]$inventoryComponent.$condaKey) {
                throw "SBOM Conda property differs from inventory: $componentId/$condaKey"
            }
        }
        foreach ($classification in $classificationProperties) {
            if (-not $inventoryEvidence.ComponentById.ContainsKey($classification) -or
                [string]$inventoryEvidence.ComponentById[$classification].identity_status -cne
                    'classification-only') {
                throw "SBOM exposes a non-classification payload annotation: $classification"
            }
        }
        if ($componentId -ceq 'python-distribution:html2text') {
            $statusProperties = @($properties | Where-Object {
                [string]$_.name -ceq 'pkv:license-expression-status'
            })
            if ($statusProperties.Count -ne 1 -or
                [string]$statusProperties[0].value -cne 'requires-legal-confirmation') {
                throw 'SBOM html2text identity lacks the legal-confirmation status'
            }
        }

        $actualLicenseFields = @(
            'classifications', 'component_id', 'component_sha256', 'embedded_paths',
            'license', 'license_files', 'license_material_status', 'name',
            'payload_paths', 'purl', 'source_paths', 'version'
        )
        if ($componentId -ceq 'python-distribution:html2text') {
            $actualLicenseFields += 'license_expression_status'
        }
        Assert-W4ExactObjectFields -Object $actualLicense -Fields $actualLicenseFields `
            -Label 'license index actual runtime component'
        $materialName = Get-W4CanonicalDistributionName -Value ([string]$component.name)
        if ($componentId -cin @(
            'build-runtime:pyinstaller-bootloader',
            'build-runtime:pyinstaller-hooks'
        )) {
            $materialName = 'pyinstaller'
        } elseif ($componentId -ceq 'build-runtime:pyinstaller-hooks-contrib') {
            $materialName = 'pyinstaller-hooks-contrib'
        }
        $materialEntry = if ($licenseEntryByName.ContainsKey($materialName)) {
            $licenseEntryByName[$materialName]
        } else { $null }
        $expectedRuntimeLicenseFiles = if ($null -ne $materialEntry) {
            @($materialEntry.license_files)
        } else { @() }
        $expectedLicenseMaterial = $expectedMaterialStatus
        if ([string]$actualLicense.component_id -cne $componentId -or
            [string]$actualLicense.component_sha256 -cne $componentSha -or
            [string]$actualLicense.name -cne [string]$component.name -or
            [string]$actualLicense.purl -cne [string]$component.purl -or
            [string]$actualLicense.version -cne [string]$component.version -or
            ($actualLicense.license | ConvertTo-Json -Depth 10 -Compress) -cne
                (@($component.licenses)[0] | ConvertTo-Json -Depth 10 -Compress) -or
            (@($actualLicense.license_files) | ConvertTo-Json -Depth 20 -Compress) -cne
                ($expectedRuntimeLicenseFiles | ConvertTo-Json -Depth 20 -Compress) -or
            [string]$actualLicense.license_material_status -cne $expectedLicenseMaterial -or
            (@($actualLicense.payload_paths) | ConvertTo-Json -Compress) -cne
                (@($inventoryComponent.payload_paths) | ConvertTo-Json -Compress) -or
            (@($actualLicense.embedded_paths) | ConvertTo-Json -Compress) -cne
                (@($inventoryComponent.embedded_paths) | ConvertTo-Json -Compress) -or
            (@($actualLicense.source_paths) | ConvertTo-Json -Compress) -cne
                (@($inventoryComponent.source_paths) | ConvertTo-Json -Compress) -or
            (@($actualLicense.classifications) | ConvertTo-Json -Compress) -cne
                ($classificationProperties | ConvertTo-Json -Compress)) {
            throw "license index actual runtime component differs from SBOM/inventory: $componentId"
        }
        if ($componentId -ceq 'python-distribution:html2text' -and
            [string]$actualLicense.license_expression_status -cne
                'requires_legal_confirmation') {
            throw 'license index html2text identity lacks legal-confirmation status'
        }
        $actualSbomIds.Add($componentId)
    }
    if ((@($actualSbomIds) | ConvertTo-Json -Compress) -cne
        ($expectedSbomInventoryIds | ConvertTo-Json -Compress)) {
        throw 'SBOM component order/identity differs from canonical release inventory'
    }
    $sbomDependencies = @($sbom.dependencies)
    if ($sbomDependencies.Count -ne 1) {
        throw 'SBOM must contain exactly one application dependency edge'
    }
    Assert-W4ExactObjectFields -Object $sbomDependencies[0] -Fields @('dependsOn', 'ref') `
        -Label 'SBOM application dependency edge'
    if ([string]$sbomDependencies[0].ref -cne
            [string]$sbom.metadata.component.'bom-ref' -or
        (@($sbomDependencies[0].dependsOn) | ConvertTo-Json -Compress) -cne
            (@($sbomComponents | ForEach-Object { [string]$_.'bom-ref' }) |
                ConvertTo-Json -Compress)) {
        throw 'SBOM dependency edge does not exactly cover runtime inventory components'
    }

    $cliVersion = Invoke-W4Process -FileName (Join-Path $RunContext.ArtifactRoot 'app\pkv.exe') `
        -Arguments @('--version') -WorkingDirectory $ScenarioContext.WorkingDirectory `
        -Environment $ScenarioContext.Environment `
        -EvidenceDirectory (Join-Path $ScenarioContext.Evidence 'cli-version') `
        -ExpectedExitCodes @(0) -TimeoutSeconds 30
    $expectedVersionOutput = "pkv, version $([string]$RunContext.Contract.artifact_version)"
    if ($cliVersion.StandardOutput.Trim() -cne $expectedVersionOutput -or
        -not [string]::IsNullOrWhiteSpace($cliVersion.StandardError)) {
        throw "Installed CLI --version output is not exact: expected '$expectedVersionOutput' with empty stderr"
    }

    $oracle = [ordered]@{
        status = 'passed'
        artifact_sha256 = $RunContext.ArtifactSha256
        payload_file_count = @($actualTree).Count
        payload_manifest_file_count = $manifestRows.Count
        application_version = [string]$buildInfo.version
        source_revision = [string]$buildInfo.source_revision
        build_fingerprint = [string]$buildInfo.build_fingerprint
        payload_tree_sha256 = $recomputedTreeSha
        dependency_component_count = $dependencies.Count
        mapped_license_file_count = $mappedLicenseFiles.Count
        zip_sha256_sidecar_verified = $true
        provenance_cross_hashes_verified = $true
        artifact_kind = 'test_candidate'
        artifact_status = 'test-candidate-on-compliance-hold'
        release_eligible = $false
        release_blockers = $expectedComplianceBlockers
        compliance_manifest_sha256 = [string]$buildInfo.compliance_manifest_sha256
        expected_run_decision = 'hold'
        user_guide_sha256 = Get-W4FileSha256 -Path $guidePath
        decision_emitted_only_by_run_summary = $true
    }
    Write-W4JsonFile -Path (Join-Path $ScenarioContext.Evidence 'oracle.json') -Value $oracle
    return $oracle
}

function Invoke-W4ApplicationLifecycleScenario {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$RunContext,
        [Parameter(Mandatory = $true)]$ScenarioContext
    )

    [void](Invoke-W4Install -RunContext $RunContext -ScenarioContext $ScenarioContext)
    $whereExe = Join-Path ([Environment]::GetEnvironmentVariable('SystemRoot')) 'System32\where.exe'
    foreach ($tool in @('python.exe', 'conda.exe')) {
        [void](Invoke-W4Process -FileName $whereExe -Arguments @($tool) `
            -WorkingDirectory $ScenarioContext.WorkingDirectory `
            -Environment $ScenarioContext.Environment `
            -EvidenceDirectory (Join-Path $ScenarioContext.Evidence ("absence-" + $tool.Replace('.exe', ''))) `
            -ExpectedExitCodes @(1) -TimeoutSeconds 10)
    }
    $installedBefore = Get-W4TreeSha256 -Root $ScenarioContext.InstallRoot
    [void](Initialize-W4InstalledDataRoot -ScenarioContext $ScenarioContext -EvidenceName 'first-run')
    $database = Join-Path $ScenarioContext.UserDataRoot 'db\knowledge_vault.db'
    if (-not (Test-Path -LiteralPath $database -PathType Leaf)) {
        throw "First run did not initialize the user database: $database"
    }
    $stateBeforeRestart = Get-W4DataStateDigest -ScenarioContext $ScenarioContext
    [void](Initialize-W4InstalledDataRoot -ScenarioContext $ScenarioContext -EvidenceName 'restart')
    $stateAfterRestart = Get-W4DataStateDigest -ScenarioContext $ScenarioContext
    $installedAfter = Get-W4TreeSha256 -Root $ScenarioContext.InstallRoot
    if ($installedBefore -ne $installedAfter) {
        throw 'Application execution mutated installed bundled resources'
    }
    if ($stateBeforeRestart -ne $stateAfterRestart) {
        throw 'Read-only restart changed durable user state outside logs/tmp'
    }
    Assert-W4DisjointPaths -First $ScenarioContext.InstallRoot -FirstLabel 'installed resources' `
        -Second $ScenarioContext.UserDataRoot -SecondLabel 'user data root'
    $oracle = [ordered]@{
        status = 'passed'
        python_absent = $true
        conda_absent = $true
        installed_tree_immutable = $true
        restart_state_preserved = $true
        installed_tree_sha256 = $installedAfter
        user_state_sha256 = $stateAfterRestart
    }
    Write-W4JsonFile -Path (Join-Path $ScenarioContext.Evidence 'oracle.json') -Value $oracle
    return $oracle
}

function Invoke-W4UrlArchiveSsrfScenario {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$RunContext,
        [Parameter(Mandatory = $true)]$ScenarioContext
    )

    [void](Invoke-W4Install -RunContext $RunContext -ScenarioContext $ScenarioContext)
    [void](Initialize-W4InstalledDataRoot -ScenarioContext $ScenarioContext)
    $before = Get-W4DataStateDigest -ScenarioContext $ScenarioContext
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    try {
        $port = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
        $target = "http://127.0.0.1:$port/w4-private-canary"
        $result = Invoke-W4Process -FileName $ScenarioContext.PkvExe `
            -Arguments @('archive', $target, '--quiet') `
            -WorkingDirectory $ScenarioContext.WorkingDirectory `
            -Environment $ScenarioContext.Environment `
            -EvidenceDirectory (Join-Path $ScenarioContext.Evidence 'archive-url') `
            -ExpectedExitCodes @(1) -TimeoutSeconds 60
        Start-Sleep -Milliseconds 200
        if ($listener.Pending()) {
            $client = $listener.AcceptTcpClient()
            $client.Dispose()
            throw 'SSRF rejection allowed an outbound connection to the loopback canary'
        }
        $combined = $result.StandardOutput + "`n" + $result.StandardError
        if ($combined -notmatch 'ssrf_target_forbidden') {
            throw 'Installed CLI did not expose stable ssrf_target_forbidden rejection'
        }
    } finally {
        $listener.Stop()
    }
    $after = Get-W4DataStateDigest -ScenarioContext $ScenarioContext
    if ($before -ne $after) {
        throw 'Rejected SSRF archive mutated durable data state'
    }
    $oracle = [ordered]@{
        status = 'passed'
        stable_code = 'ssrf_target_forbidden'
        outbound_connections = 0
        durable_state_unchanged = $true
        before_sha256 = $before
        after_sha256 = $after
    }
    Write-W4JsonFile -Path (Join-Path $ScenarioContext.Evidence 'oracle.json') -Value $oracle
    return $oracle
}

function Expand-W4FixtureVectorIndexes {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$RunContext,
        [Parameter(Mandatory = $true)]$ScenarioContext
    )

    $bundle = Join-Path $RunContext.FixtureRoot 'semantic-vector-index.v1'
    $manifestPath = Join-Path $bundle 'manifest.v1.json'
    [void](Get-W4TreeManifest -Root $bundle)
    $manifest = Read-W4JsonFile -Path $manifestPath
    Assert-W4ExactObjectFields -Object $manifest -Fields @(
        'contains_credentials', 'contains_real_vault_data', 'embedding_contract',
        'files', 'generation', 'index_contract', 'schema_version',
        'synthetic_only', 'toolchain'
    ) -Label 'Semantic vector fixture manifest'
    Assert-W4ExactBoolean -Value $manifest.synthetic_only -Expected $true `
        -Label 'Semantic vector fixture synthetic_only'
    Assert-W4ExactBoolean -Value $manifest.contains_credentials -Expected $false `
        -Label 'Semantic vector fixture contains_credentials'
    Assert-W4ExactBoolean -Value $manifest.contains_real_vault_data -Expected $false `
        -Label 'Semantic vector fixture contains_real_vault_data'
    if ([string]$manifest.schema_version -ne 'pkv.m13.w4-semantic-index.v1') {
        throw 'Semantic vector fixture has an unexpected schema version'
    }
    Assert-W4ExactObjectFields -Object $manifest.embedding_contract -Fields @(
        'base_url', 'base_url_sha256', 'dim', 'fingerprint_schema_version', 'model'
    ) -Label 'Semantic vector fixture embedding contract'
    Assert-W4ExactObjectFields -Object $manifest.index_contract -Fields @(
        'M', 'dim', 'ef_construction', 'element_count', 'generation_max_elements',
        'loaded_empty_max_elements', 'pairs', 'random_seed', 'space'
    ) -Label 'Semantic vector fixture index contract'
    Assert-W4ExactObjectFields -Object $manifest.generation -Fields @(
        'network_required', 'notes', 'script', 'source_data_required'
    ) -Label 'Semantic vector fixture generation contract'
    Assert-W4ExactObjectFields -Object $manifest.toolchain -Fields @('hnswlib') `
        -Label 'Semantic vector fixture toolchain'
    Assert-W4ExactBoolean -Value $manifest.generation.network_required -Expected $false `
        -Label 'Semantic vector fixture network_required'
    Assert-W4ExactBoolean -Value $manifest.generation.source_data_required -Expected $false `
        -Label 'Semantic vector fixture source_data_required'
    if ([string]$manifest.embedding_contract.base_url -cne 'https://api.openai.com/v1' -or
        [string]$manifest.embedding_contract.base_url_sha256 -cne
            'd9617135d6fdd0a2cde722d637a1dfcc3da37515708b3ea5d66ae607c8ac785e' -or
        [int]$manifest.embedding_contract.dim -ne 1536 -or
        [int]$manifest.embedding_contract.fingerprint_schema_version -ne 2 -or
        [string]$manifest.embedding_contract.model -cne 'text-embedding-3-small' -or
        [int]$manifest.index_contract.M -ne 16 -or
        [int]$manifest.index_contract.dim -ne 1536 -or
        [int]$manifest.index_contract.ef_construction -ne 200 -or
        [int]$manifest.index_contract.element_count -ne 0 -or
        [int]$manifest.index_contract.generation_max_elements -ne 10000 -or
        [int]$manifest.index_contract.loaded_empty_max_elements -ne 0 -or
        (@($manifest.index_contract.pairs) | ConvertTo-Json -Compress) -cne
            (@('doc_vectors', 'chunk_vectors') | ConvertTo-Json -Compress) -or
        [int]$manifest.index_contract.random_seed -ne 100 -or
        [string]$manifest.index_contract.space -cne 'cosine' -or
        [string]$manifest.generation.script -cne 'generate_fixture.py' -or
        [string]$manifest.toolchain.hnswlib -cne '0.8.0') {
        throw 'Semantic vector fixture runtime/index/toolchain contract is invalid'
    }
    $target = Join-Path $ScenarioContext.UserDataRoot 'vectors'
    if (Test-Path -LiteralPath $target) {
        if (-not (Test-Path -LiteralPath $target -PathType Container) -or
            @(Get-ChildItem -LiteralPath $target -Force).Count -ne 0) {
            throw 'Semantic vector fixture target must be an empty normal directory'
        }
    } else {
        [void][System.IO.Directory]::CreateDirectory($target)
    }
    $bundleFull = [System.IO.Path]::GetFullPath($bundle)
    $targetFull = [System.IO.Path]::GetFullPath($target)
    $expectedFiles = @(
        'chunk_vectors.idx',
        'chunk_vectors_metadata.json',
        'doc_vectors.idx',
        'doc_vectors_metadata.json'
    )
    $rows = @($manifest.files)
    if ($rows.Count -ne $expectedFiles.Count -or
        (@($rows | ForEach-Object { [string]$_.path }) | ConvertTo-Json -Compress) -cne
            ($expectedFiles | ConvertTo-Json -Compress)) {
        throw 'Semantic vector fixture file inventory is not the exact canonical set/order'
    }
    for ($index = 0; $index -lt $rows.Count; $index += 1) {
        $row = $rows[$index]
        Assert-W4ExactObjectFields -Object $row -Fields @('path', 'sha256') `
            -Label "Semantic vector fixture file row $index"
        $relative = [string]$row.path
        if ($relative -cne $expectedFiles[$index] -or
            [string]$row.sha256 -cnotmatch '^[0-9a-f]{64}$') {
            throw "Semantic vector fixture file row is invalid: $relative"
        }
        $source = [System.IO.Path]::GetFullPath((Join-Path $bundleFull $relative))
        $destination = [System.IO.Path]::GetFullPath((Join-Path $targetFull $relative))
        if (-not (Test-W4PathContainedBy -Candidate $source -Root $bundleFull) -or
            -not (Test-W4PathContainedBy -Candidate $destination -Root $targetFull) -or
            -not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "Semantic vector fixture path escaped or is missing: $relative"
        }
        if ((Get-W4FileSha256 -Path $source) -cne [string]$row.sha256) {
            throw "Semantic vector fixture hash mismatch: $relative"
        }
        [System.IO.File]::Copy($source, $destination, $false)
    }
    return Get-W4TreeSha256 -Root $target
}

function Invoke-W4SemanticProviderUnavailableScenario {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$RunContext,
        [Parameter(Mandatory = $true)]$ScenarioContext
    )

    [void](Invoke-W4Install -RunContext $RunContext -ScenarioContext $ScenarioContext)
    [void](Initialize-W4InstalledDataRoot -ScenarioContext $ScenarioContext)
    $configDirectory = Join-Path $ScenarioContext.UserDataRoot 'config'
    [void][System.IO.Directory]::CreateDirectory($configDirectory)
    [System.IO.File]::Copy(
        (Join-Path $RunContext.FixtureRoot 'semantic-provider-unavailable.local.yaml'),
        (Join-Path $configDirectory 'local.yaml'),
        $false
    )
    $vectorFixtureSha = Expand-W4FixtureVectorIndexes -RunContext $RunContext `
        -ScenarioContext $ScenarioContext
    $vectorResult = Invoke-W4Process -FileName $ScenarioContext.PkvExe `
        -Arguments @('search', 'artifact-e2e-orchid', '--strategy', 'vector', '--format', 'json') `
        -WorkingDirectory $ScenarioContext.WorkingDirectory `
        -Environment $ScenarioContext.Environment `
        -EvidenceDirectory (Join-Path $ScenarioContext.Evidence 'vector') `
        -ExpectedExitCodes @(1) -TimeoutSeconds 60
    $vectorPayload = ConvertFrom-W4StrictJsonText -Text $vectorResult.StandardOutput -Label 'vector search'
    if ([string]$vectorPayload.status -ne 'error' -or [string]$vectorPayload.strategy -ne 'vector') {
        throw 'Vector provider-unavailable response is not status=error,strategy=vector'
    }
    $vectorCodes = @($vectorPayload.issues | ForEach-Object { [string]$_.code })
    if ($vectorCodes -notcontains 'provider_config_invalid') {
        throw "Vector response did not expose provider_config_invalid: $($vectorCodes -join ',')"
    }
    if ($vectorCodes -contains 'retrieval_index_unavailable') {
        throw 'Vector scenario reached index-unavailable instead of the Provider seam'
    }

    $hybridResult = Invoke-W4Process -FileName $ScenarioContext.PkvExe `
        -Arguments @('search', 'artifact-e2e-orchid', '--strategy', 'hybrid', '--format', 'json') `
        -WorkingDirectory $ScenarioContext.WorkingDirectory `
        -Environment $ScenarioContext.Environment `
        -EvidenceDirectory (Join-Path $ScenarioContext.Evidence 'hybrid') `
        -ExpectedExitCodes @(0) -TimeoutSeconds 60
    $hybridPayload = ConvertFrom-W4StrictJsonText -Text $hybridResult.StandardOutput -Label 'hybrid search'
    if ([string]$hybridPayload.status -ne 'degraded' -or [string]$hybridPayload.strategy -ne 'hybrid') {
        throw 'Hybrid provider-unavailable response is not status=degraded,strategy=hybrid'
    }
    $hybridCodes = @($hybridPayload.issues | ForEach-Object { [string]$_.code })
    if ($hybridCodes -notcontains 'provider_config_invalid' -or
        $hybridCodes -contains 'retrieval_index_unavailable') {
        throw "Hybrid response did not reach the Provider seam: $($hybridCodes -join ',')"
    }
    $oracle = [ordered]@{
        status = 'passed'
        vector_status = [string]$vectorPayload.status
        vector_issue_codes = $vectorCodes
        hybrid_status = [string]$hybridPayload.status
        hybrid_issue_codes = $hybridCodes
        vector_fixture_sha256 = $vectorFixtureSha
    }
    Write-W4JsonFile -Path (Join-Path $ScenarioContext.Evidence 'oracle.json') -Value $oracle
    return $oracle
}

function Invoke-W4McpStdioScenario {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$RunContext,
        [Parameter(Mandatory = $true)]$ScenarioContext
    )

    [void](Invoke-W4Install -RunContext $RunContext -ScenarioContext $ScenarioContext)
    $evidence = Join-Path $ScenarioContext.Evidence 'mcp'
    [void][System.IO.Directory]::CreateDirectory($evidence)
    $session = Start-W4McpSession -Executable $ScenarioContext.McpExe `
        -WorkingDirectory $ScenarioContext.WorkingDirectory `
        -Environment $ScenarioContext.Environment `
        -TranscriptPath (Join-Path $evidence 'transcript.jsonl') `
        -ProtocolVersion ([string]$RunContext.Contract.mcp.protocol_version)
    $closed = $false
    try {
        if ([string]$session.Initialize.serverInfo.name -ne 'Personal Knowledge Vault') {
            throw "Unexpected MCP serverInfo.name: $($session.Initialize.serverInfo.name)"
        }
        $tools = Invoke-W4McpRequest -Process $session.Process -Id 2 -Method 'tools/list' `
            -Params ([ordered]@{}) -TranscriptPath $session.TranscriptPath
        $resources = Invoke-W4McpRequest -Process $session.Process -Id 3 -Method 'resources/list' `
            -Params ([ordered]@{}) -TranscriptPath $session.TranscriptPath
        $templates = Invoke-W4McpRequest -Process $session.Process -Id 4 `
            -Method 'resources/templates/list' -Params ([ordered]@{}) `
            -TranscriptPath $session.TranscriptPath
        $prompts = Invoke-W4McpRequest -Process $session.Process -Id 5 -Method 'prompts/list' `
            -Params ([ordered]@{}) -TranscriptPath $session.TranscriptPath
        Assert-W4SetEqual -Actual @($tools.tools | ForEach-Object { $_.name }) `
            -Expected @($RunContext.Contract.mcp.tools) -Label 'MCP tools/list'
        Assert-W4SetEqual -Actual @($resources.resources | ForEach-Object { $_.uri }) `
            -Expected @($RunContext.Contract.mcp.resources) -Label 'MCP resources/list'
        Assert-W4SetEqual -Actual @($templates.resourceTemplates | ForEach-Object { $_.uriTemplate }) `
            -Expected @($RunContext.Contract.mcp.resource_templates) `
            -Label 'MCP resources/templates/list'
        Assert-W4SetEqual -Actual @($prompts.prompts | ForEach-Object { $_.name }) `
            -Expected @($RunContext.Contract.mcp.prompts) -Label 'MCP prompts/list'

        $statsResult = Invoke-W4McpRequest -Process $session.Process -Id 6 `
            -Method 'tools/call' -Params ([ordered]@{
                name = 'get_stats'; arguments = [ordered]@{}
            }) -TranscriptPath $session.TranscriptPath
        $stats = ConvertFrom-W4McpTextContent -Result $statsResult -Label 'get_stats'
        if ([string]$stats.status -ne 'success' -or @($stats.issues).Count -ne 0) {
            throw 'get_stats did not return the complete success envelope'
        }

        $timelineResult = Invoke-W4McpRequest -Process $session.Process -Id 7 `
            -Method 'tools/call' -Params ([ordered]@{
                name = 'timeline_of'; arguments = [ordered]@{ topic = 'artifact-e2e-orchid' }
            }) -TranscriptPath $session.TranscriptPath
        $timeline = ConvertFrom-W4McpTextContent -Result $timelineResult -Label 'timeline_of'
        if ([string]$timeline.schema_version -ne 'phase_b.v1' -or
            [string]$timeline.implementation_level -ne 'partial') {
            throw 'timeline_of did not preserve the published partial-v1 envelope'
        }
        $processResult = Stop-W4McpSession -Session $session -EvidenceDirectory $evidence
        $closed = $true
    } finally {
        if (-not $closed -and $null -ne $session.Process -and -not $session.Process.HasExited) {
            Stop-W4ProcessTree -Process $session.Process
            $session.Process.Dispose()
        }
    }
    $oracle = [ordered]@{
        status = 'passed'
        tools = 14
        static_resources = 2
        resource_templates = 7
        total_resources = 9
        prompts = 3
        get_stats_status = [string]$stats.status
        timeline_schema_version = [string]$timeline.schema_version
        timeline_implementation_level = [string]$timeline.implementation_level
        natural_exit = (-not [bool]$processResult.forced_termination)
        exit_code = [int]$processResult.exit_code
    }
    Write-W4JsonFile -Path (Join-Path $ScenarioContext.Evidence 'oracle.json') -Value $oracle
    return $oracle
}

function Start-W4GuiApplication {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$ScenarioContext,
        [Parameter(Mandatory = $true)][string]$EvidenceName
    )

    $evidence = Join-Path $ScenarioContext.Evidence $EvidenceName
    [void][System.IO.Directory]::CreateDirectory($evidence)
    $safeEnvironment = [ordered]@{}
    foreach ($entry in $ScenarioContext.Environment.GetEnumerator()) {
        $safeEnvironment[[string]$entry.Key] = if ([string]$entry.Key -match '(KEY|TOKEN|SECRET|PASSWORD)') {
            '<redacted>'
        } else {
            [string]$entry.Value
        }
    }
    Write-W4JsonFile -Path (Join-Path $evidence 'invocation.json') -Value ([ordered]@{
        file = $ScenarioContext.GuiExe
        arguments = @()
        working_directory = $ScenarioContext.WorkingDirectory
        environment = $safeEnvironment
        automation = 'Windows UI Automation exact AutomationId/Name only'
    })
    $process = Start-W4LongRunningProcess -FileName $ScenarioContext.GuiExe `
        -WorkingDirectory $ScenarioContext.WorkingDirectory `
        -Environment $ScenarioContext.Environment -VisibleWindow
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    try {
        $window = Get-W4UiaMainWindow -ProcessId $process.Id -TimeoutSeconds 45
        Export-W4UiaTree -Root $window -Path (Join-Path $evidence 'uia-tree-start.json')
        $gui = [pscustomobject]@{
            Process = $process
            Window = $window
            StdoutTask = $stdoutTask
            StderrTask = $stderrTask
            Evidence = $evidence
        }
        Assert-W4UiaContractSegment -Gui $gui `
            -AutomationIds @('pkv_main_window', 'pkv_central', 'pkv_view_stack', 'nav_list', 'app_status') `
            -EvidenceName 'uia-contract-common.json'
        $navigation = Get-W4UiaElementById -Root $window -AutomationId 'nav_list'
        $listItemCondition = [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::ListItem
        )
        $navigationItems = $navigation.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants,
            $listItemCondition
        )
        $actualNavigationNames = [System.Collections.Generic.List[string]]::new()
        for ($navigationIndex = 0; $navigationIndex -lt $navigationItems.Count; $navigationIndex += 1) {
            $navigationItem = $navigationItems.Item($navigationIndex)
            $navigationName = [string]$navigationItem.Current.Name
            $selectionPattern = $null
            if ([int]$navigationItem.Current.ProcessId -ne [int]$process.Id -or
                [string]::IsNullOrWhiteSpace($navigationName) -or
                -not $navigationItem.TryGetCurrentPattern(
                    [System.Windows.Automation.SelectionItemPattern]::Pattern,
                    [ref]$selectionPattern
                )) {
                throw "UIA navigation item is invalid: $navigationName"
            }
            $actualNavigationNames.Add($navigationName)
        }
        $expectedNavigationNames = @(
            $ScenarioContext.UiaContract.navigation_names | ForEach-Object { [string]$_ }
        )
        if ((@($actualNavigationNames) | ConvertTo-Json -Compress) -cne
            ($expectedNavigationNames | ConvertTo-Json -Compress)) {
            throw 'UIA navigation names/order differ from scenarios.v1.json'
        }
        Write-W4JsonFile -Path (Join-Path $evidence 'uia-navigation-contract.json') `
            -Value ([ordered]@{
                schema_version = 'pkv.m13.w4-uia-navigation-evidence.v1'
                navigation_names = @($actualNavigationNames)
                exact = $true
            })
        return $gui
    } catch {
        Stop-W4ProcessTree -Process $process
        [System.IO.File]::WriteAllText(
            (Join-Path $evidence 'stdout.txt'),
            $stdoutTask.GetAwaiter().GetResult(),
            [System.Text.UTF8Encoding]::new($false)
        )
        [System.IO.File]::WriteAllText(
            (Join-Path $evidence 'stderr.txt'),
            $stderrTask.GetAwaiter().GetResult(),
            [System.Text.UTF8Encoding]::new($false)
        )
        $process.Dispose()
        throw
    }
}

function Stop-W4GuiApplication {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Gui,
        [ValidateRange(1, 60)][int]$TimeoutSeconds = 20
    )

    $process = [System.Diagnostics.Process]$Gui.Process
    $forced = $false
    try {
        if (-not $process.HasExited) {
            $pattern = $null
            if (-not $Gui.Window.TryGetCurrentPattern(
                [System.Windows.Automation.WindowPattern]::Pattern,
                [ref]$pattern
            )) {
                throw 'pkv_main_window does not support UIA WindowPattern'
            }
            ([System.Windows.Automation.WindowPattern]$pattern).Close()
            if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
                $forced = $true
                Stop-W4ProcessTree -Process $process
            } else {
                $process.WaitForExit()
            }
        }
        $stdout = $Gui.StdoutTask.GetAwaiter().GetResult()
        $stderr = $Gui.StderrTask.GetAwaiter().GetResult()
        [System.IO.File]::WriteAllText(
            (Join-Path $Gui.Evidence 'stdout.txt'),
            $stdout,
            [System.Text.UTF8Encoding]::new($false)
        )
        [System.IO.File]::WriteAllText(
            (Join-Path $Gui.Evidence 'stderr.txt'),
            $stderr,
            [System.Text.UTF8Encoding]::new($false)
        )
        $record = [ordered]@{
            exit_code = if ($process.HasExited) { [int]$process.ExitCode } else { $null }
            forced_termination = $forced
            timed_out = $forced
        }
        Write-W4JsonFile -Path (Join-Path $Gui.Evidence 'process.json') -Value $record
        if ($forced) {
            throw 'GUI did not exit normally after UIA WindowPattern.Close()'
        }
        if ($process.ExitCode -ne 0) {
            throw "GUI exited with code $($process.ExitCode)"
        }
        return $record
    } finally {
        if (-not $process.HasExited) {
            Stop-W4ProcessTree -Process $process
        }
        $process.Dispose()
    }
}

function Select-W4NavigationItem {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Gui,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $navigation = Get-W4UiaElementById -Root $Gui.Window -AutomationId 'nav_list'
    [void](Select-W4UiaItemByName -Root $navigation -Name $Name)
}

function Select-W4FirstDataItem {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][System.Windows.Automation.AutomationElement]$Root)

    $condition = [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::DataItem
    )
    $items = $Root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
    if ($items.Count -lt 1) {
        throw "UIA table contains no DataItem: $($Root.Current.AutomationId)"
    }
    $item = $items.Item(0)
    $pattern = $null
    if (-not $item.TryGetCurrentPattern(
        [System.Windows.Automation.SelectionItemPattern]::Pattern,
        [ref]$pattern
    )) {
        throw 'First UIA DataItem does not support SelectionItemPattern'
    }
    ([System.Windows.Automation.SelectionItemPattern]$pattern).Select()
    return $item
}

function Select-W4FirstListItem {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][System.Windows.Automation.AutomationElement]$Root)

    $condition = [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::ListItem
    )
    $items = $Root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
    if ($items.Count -lt 1) {
        throw "UIA list contains no ListItem: $($Root.Current.AutomationId)"
    }
    $item = $items.Item(0)
    $pattern = $null
    if (-not $item.TryGetCurrentPattern(
        [System.Windows.Automation.SelectionItemPattern]::Pattern,
        [ref]$pattern
    )) {
        throw 'First UIA ListItem does not support SelectionItemPattern'
    }
    ([System.Windows.Automation.SelectionItemPattern]$pattern).Select()
    return $item
}

function Assert-W4UiaContractSegment {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Gui,
        [Parameter(Mandatory = $true)][string[]]$AutomationIds,
        [Parameter(Mandatory = $true)][string]$EvidenceName
    )

    $valueIds = @(
        'search_input', 'archive_url_input', 'archive_text_title',
        'archive_text_content', 'chat_input'
    )
    $invokeIds = @(
        'search_submit', 'archive_url_submit', 'archive_text_submit',
        'archive_go_browser', 'chat_new_session', 'chat_send', 'chat_stop'
    )
    $selectionIds = @(
        'nav_list', 'browser_entry_table', 'search_result_table',
        'archive_tabs', 'session_list'
    )
    $expandCollapseIds = @('search_strategy')
    $records = [System.Collections.Generic.List[object]]::new()
    foreach ($automationId in $AutomationIds) {
        $element = if ($automationId -eq 'pkv_main_window') {
            if ([string]$Gui.Window.Current.AutomationId -ne 'pkv_main_window') {
                throw 'GUI root does not expose the exact pkv_main_window AutomationId'
            }
            $Gui.Window
        } else {
            Get-W4UiaElementById -Root $Gui.Window -AutomationId $automationId
        }
        if ([int]$element.Current.ProcessId -ne [int]$Gui.Process.Id) {
            throw "UIA element belongs to the wrong process: $automationId"
        }
        $requiredPatterns = [System.Collections.Generic.List[string]]::new()
        if ($automationId -eq 'pkv_main_window') {
            $requiredPatterns.Add('WindowPattern')
        }
        if ($valueIds -contains $automationId) {
            $requiredPatterns.Add('ValuePattern')
        }
        if ($invokeIds -contains $automationId) {
            $requiredPatterns.Add('InvokePattern')
        }
        if ($selectionIds -contains $automationId) {
            $requiredPatterns.Add('SelectionPattern')
        }
        if ($expandCollapseIds -contains $automationId) {
            $requiredPatterns.Add('ExpandCollapsePattern')
        }
        foreach ($patternName in $requiredPatterns) {
            $patternIdentifier = switch ($patternName) {
                'WindowPattern' { [System.Windows.Automation.WindowPattern]::Pattern }
                'ValuePattern' { [System.Windows.Automation.ValuePattern]::Pattern }
                'InvokePattern' { [System.Windows.Automation.InvokePattern]::Pattern }
                'SelectionPattern' { [System.Windows.Automation.SelectionPattern]::Pattern }
                'ExpandCollapsePattern' { [System.Windows.Automation.ExpandCollapsePattern]::Pattern }
                default { throw "Unsupported UIA pattern assertion: $patternName" }
            }
            $patternObject = $null
            if (-not $element.TryGetCurrentPattern($patternIdentifier, [ref]$patternObject)) {
                throw "UIA element $automationId does not expose required $patternName"
            }
        }
        $records.Add([ordered]@{
            automation_id = $automationId
            process_id = [int]$element.Current.ProcessId
            control_type = [string]$element.Current.ControlType.ProgrammaticName
            patterns = @($requiredPatterns)
            exact_one = $true
        })
    }
    Write-W4JsonFile -Path (Join-Path $Gui.Evidence $EvidenceName) -Value @($records)
}

function Wait-W4UiaTextContains {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][System.Windows.Automation.AutomationElement]$Element,
        [Parameter(Mandatory = $true)][string]$Text,
        [ValidateRange(1, 180)][int]$TimeoutSeconds = 30
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $actual = ''
    do {
        $actual = Get-W4UiaText -Element $Element
        if ($actual.IndexOf($Text, [System.StringComparison]::Ordinal) -ge 0) {
            return $actual
        }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "UIA text did not contain expected text. expected=$Text actual=$actual"
}

function Dismiss-W4ProcessModal {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [ValidateRange(1, 30)][int]$TimeoutSeconds = 10,
        [switch]$AllowAbsent
    )

    $desktop = [System.Windows.Automation.AutomationElement]::RootElement
    $pidCondition = [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::ProcessIdProperty,
        $ProcessId
    )
    $buttonType = [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Button
    )
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $windows = $desktop.FindAll([System.Windows.Automation.TreeScope]::Children, $pidCondition)
        for ($windowIndex = 0; $windowIndex -lt $windows.Count; $windowIndex += 1) {
            $window = $windows.Item($windowIndex)
            if ([string]$window.Current.AutomationId -eq 'pkv_main_window') {
                continue
            }
            $buttons = $window.FindAll([System.Windows.Automation.TreeScope]::Descendants, $buttonType)
            $accepted = @()
            for ($buttonIndex = 0; $buttonIndex -lt $buttons.Count; $buttonIndex += 1) {
                $button = $buttons.Item($buttonIndex)
                if (@('OK', '确定') -contains [string]$button.Current.Name) {
                    $accepted += $button
                }
            }
            if ($accepted.Count -eq 1) {
                Invoke-W4UiaElement -Element $accepted[0]
                return $true
            }
            if ($accepted.Count -gt 1) {
                throw 'Modal contains more than one exact OK/确定 button'
            }
        }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)
    if ($AllowAbsent) {
        return $false
    }
    throw "No process modal with exact OK/确定 button appeared for PID $ProcessId"
}

function Invoke-W4OfflineTextArchiveScenario {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$RunContext,
        [Parameter(Mandatory = $true)]$ScenarioContext
    )

    [void](Invoke-W4Install -RunContext $RunContext -ScenarioContext $ScenarioContext)
    $note = [System.IO.File]::ReadAllText(
        (Join-Path $RunContext.FixtureRoot 'offline-note.v1.txt'),
        [System.Text.Encoding]::UTF8
    ).Trim()
    $title = 'W4 合成离线知识条目'
    $gui = Start-W4GuiApplication -ScenarioContext $ScenarioContext -EvidenceName 'gui-archive'
    $closed = $false
    try {
        Select-W4NavigationItem -Gui $gui -Name '归档'
        $tabs = Get-W4UiaElementById -Root $gui.Window -AutomationId 'archive_tabs'
        [void](Select-W4UiaItemByName -Root $tabs -Name '文本归档')
        Set-W4UiaValue -Element (Get-W4UiaElementById -Root $gui.Window -AutomationId 'archive_text_title') -Value $title
        Set-W4UiaValue -Element (Get-W4UiaElementById -Root $gui.Window -AutomationId 'archive_text_content') -Value $note
        Invoke-W4UiaElement -Element (Get-W4UiaElementById -Root $gui.Window -AutomationId 'archive_text_submit')
        $resultTitle = Get-W4UiaElementById -Root $gui.Window -AutomationId 'archive_result_title'
        [void](Wait-W4UiaTextContains -Element $resultTitle -Text '归档成功（降级）' -TimeoutSeconds 90)
        [void](Dismiss-W4ProcessModal -ProcessId $gui.Process.Id -TimeoutSeconds 10)
        $warning = Get-W4UiaText -Element (Get-W4UiaElementById -Root $gui.Window -AutomationId 'archive_result_warning')
        if ([string]::IsNullOrWhiteSpace($warning) -or $warning -notmatch 'provider') {
            throw "Offline archive did not expose a truthful Provider degraded warning: $warning"
        }
        $idText = Get-W4UiaText -Element (Get-W4UiaElementById -Root $gui.Window -AutomationId 'archive_result_id')
        $pathText = Get-W4UiaText -Element (Get-W4UiaElementById -Root $gui.Window -AutomationId 'archive_result_path')
        if ($idText -notmatch '^ID:\s*\d+$') {
            throw "Archive result did not expose a durable knowledge ID: $idText"
        }
        if ($pathText -notmatch '^文件:\s*(.+)$') {
            throw "Archive result did not expose a durable file path: $pathText"
        }
        $savedPath = [System.IO.Path]::GetFullPath($Matches[1])
        if (-not (Test-W4PathContainedBy -Candidate $savedPath -Root $ScenarioContext.UserDataRoot) -or
            -not (Test-Path -LiteralPath $savedPath -PathType Leaf)) {
            throw "Archive result path is not a persisted file under the synthetic user root: $savedPath"
        }
        Assert-W4UiaContractSegment -Gui $gui -AutomationIds @(
            'archive_view', 'archive_tabs', 'archive_url_input', 'archive_url_submit',
            'archive_text_title', 'archive_text_content', 'archive_text_submit',
            'archive_progress_status', 'archive_result_title', 'archive_result_id',
            'archive_result_path', 'archive_result_warning', 'archive_go_browser'
        ) -EvidenceName 'uia-contract-archive.json'
        Save-W4Screenshot -Path (Join-Path $gui.Evidence 'archive-result.png') `
            -Element $gui.Window -ProcessId $gui.Process.Id
        [void](Stop-W4GuiApplication -Gui $gui)
        $closed = $true
    } finally {
        if (-not $closed -and -not $gui.Process.HasExited) {
            Stop-W4ProcessTree -Process $gui.Process
            $gui.Process.Dispose()
        }
    }

    $restart = Start-W4GuiApplication -ScenarioContext $ScenarioContext -EvidenceName 'gui-restart'
    $restartClosed = $false
    try {
        Select-W4NavigationItem -Gui $restart -Name '浏览'
        $table = Get-W4UiaElementById -Root $restart.Window -AutomationId 'browser_entry_table'
        [void](Select-W4FirstDataItem -Root $table)
        $preview = Get-W4UiaElementById -Root $restart.Window -AutomationId 'browser_preview_text'
        [void](Wait-W4UiaTextContains -Element $preview -Text 'artifact-e2e-orchid' -TimeoutSeconds 30)
        Save-W4Screenshot -Path (Join-Path $restart.Evidence 'restart-preview.png') `
            -Element $restart.Window -ProcessId $restart.Process.Id
        [void](Stop-W4GuiApplication -Gui $restart)
        $restartClosed = $true
    } finally {
        if (-not $restartClosed -and -not $restart.Process.HasExited) {
            Stop-W4ProcessTree -Process $restart.Process
            $restart.Process.Dispose()
        }
    }
    $oracle = [ordered]@{
        status = 'passed'
        workflow_terminal = 'degraded'
        knowledge_id = $idText.Substring(3).Trim()
        saved_path_sha256 = Get-W4FileSha256 -Path $savedPath
        degraded_warning = $warning
        restart_opened_saved_entry = $true
    }
    Write-W4JsonFile -Path (Join-Path $ScenarioContext.Evidence 'oracle.json') -Value $oracle
    return $oracle
}

function Invoke-W4Bm25SearchScenario {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$RunContext,
        [Parameter(Mandatory = $true)]$ScenarioContext
    )

    [void](Invoke-W4Install -RunContext $RunContext -ScenarioContext $ScenarioContext)
    $note = [System.IO.File]::ReadAllText(
        (Join-Path $RunContext.FixtureRoot 'offline-note.v1.txt'),
        [System.Text.Encoding]::UTF8
    ).Trim()
    Invoke-W4McpSeedText -RunContext $RunContext -ScenarioContext $ScenarioContext `
        -Text $note -Title 'W4 BM25 合成条目'
    $gui = Start-W4GuiApplication -ScenarioContext $ScenarioContext -EvidenceName 'gui-search'
    $closed = $false
    try {
        Select-W4NavigationItem -Gui $gui -Name '浏览'
        $browserTable = Get-W4UiaElementById -Root $gui.Window -AutomationId 'browser_entry_table'
        [void](Select-W4FirstDataItem -Root $browserTable)
        [void](Wait-W4UiaTextContains `
            -Element (Get-W4UiaElementById -Root $gui.Window -AutomationId 'browser_preview_text') `
            -Text 'artifact-e2e-orchid' -TimeoutSeconds 30)
        Assert-W4UiaContractSegment -Gui $gui -AutomationIds @(
            'browser_view', 'browser_entry_count', 'browser_entry_status',
            'browser_entry_table', 'browser_preview_title', 'browser_preview_status',
            'browser_preview_text'
        ) -EvidenceName 'uia-contract-browser.json'

        Select-W4NavigationItem -Gui $gui -Name '搜索'
        $input = Get-W4UiaElementById -Root $gui.Window -AutomationId 'search_input'
        $submit = Get-W4UiaElementById -Root $gui.Window -AutomationId 'search_submit'
        $status = Get-W4UiaElementById -Root $gui.Window -AutomationId 'search_result_status'
        Set-W4UiaValue -Element $input -Value 'artifact-e2e-orchid'
        Invoke-W4UiaElement -Element $submit
        $hitStatus = Wait-W4UiaTextContains -Element $status -Text '找到 1 条结果' -TimeoutSeconds 30
        $resultTable = Get-W4UiaElementById -Root $gui.Window -AutomationId 'search_result_table'
        [void](Select-W4FirstDataItem -Root $resultTable)
        [void](Wait-W4UiaTextContains `
            -Element (Get-W4UiaElementById -Root $gui.Window -AutomationId 'search_preview_text') `
            -Text 'artifact-e2e-orchid' -TimeoutSeconds 30)
        Assert-W4UiaContractSegment -Gui $gui -AutomationIds @(
            'search_view', 'search_input', 'search_strategy', 'search_submit',
            'search_result_status', 'search_result_table', 'search_preview_title',
            'search_preview_status', 'search_preview_text'
        ) -EvidenceName 'uia-contract-search.json'

        Set-W4UiaValue -Element $input -Value 'w4-no-hit-5f37c22a'
        Invoke-W4UiaElement -Element $submit
        $noHitStatus = Wait-W4UiaTextContains -Element $status -Text '未找到匹配结果' -TimeoutSeconds 30

        Set-W4UiaValue -Element $input -Value ''
        Invoke-W4UiaElement -Element $submit
        $invalidStatus = Wait-W4UiaTextContains -Element $status -Text '查询无效' -TimeoutSeconds 30

        $database = Join-Path $ScenarioContext.UserDataRoot 'db\knowledge_vault.db'
        $backup = $database + '.w4-backup'
        [System.IO.File]::Move($database, $backup)
        [void][System.IO.Directory]::CreateDirectory($database)
        try {
            Set-W4UiaValue -Element $input -Value 'artifact-e2e-orchid'
            Invoke-W4UiaElement -Element $submit
            $errorStatus = Wait-W4UiaTextContains -Element $status -Text '搜索失败' -TimeoutSeconds 30
            if ($errorStatus -match '未找到') {
                throw 'GUI disguised backend failure as no-hits'
            }
        } finally {
            [System.IO.Directory]::Delete($database, $false)
            [System.IO.File]::Move($backup, $database)
        }
        Save-W4Screenshot -Path (Join-Path $gui.Evidence 'search-states.png') `
            -Element $gui.Window -ProcessId $gui.Process.Id
        [void](Stop-W4GuiApplication -Gui $gui)
        $closed = $true
    } finally {
        if (-not $closed -and -not $gui.Process.HasExited) {
            Stop-W4ProcessTree -Process $gui.Process
            $gui.Process.Dispose()
        }
    }
    $oracle = [ordered]@{
        status = 'passed'
        browse_detail = 'success'
        hit = $hitStatus
        no_hits = $noHitStatus
        invalid = $invalidStatus
        backend_error = $errorStatus
    }
    Write-W4JsonFile -Path (Join-Path $ScenarioContext.Evidence 'oracle.json') -Value $oracle
    return $oracle
}

function Invoke-W4LoopbackGetJson {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [ValidateRange(1, 30)][int]$TimeoutSeconds = 10
    )

    $uri = [Uri]$Url
    if ($uri.Scheme -ne 'http' -or $uri.Host -ne '127.0.0.1') {
        throw "Harness endpoint must be numeric loopback HTTP: $Url"
    }
    Add-Type -AssemblyName System.Net.Http -ErrorAction Stop
    $handler = [System.Net.Http.HttpClientHandler]::new()
    $handler.UseProxy = $false
    $client = [System.Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromSeconds($TimeoutSeconds)
    try {
        $text = $client.GetStringAsync($uri).GetAwaiter().GetResult()
        return ConvertFrom-W4StrictJsonText -Text $text -Label "GET $Url"
    } finally {
        $client.Dispose()
        $handler.Dispose()
    }
}

function Get-W4ValidatedProcessIdentity {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][System.Diagnostics.Process]$LauncherProcess,
        [Parameter(Mandatory = $true)][int]$RuntimeProcessId
    )

    if ($RuntimeProcessId -le 0) {
        throw "Harness ready.json published an invalid runtime pid: $RuntimeProcessId"
    }
    $LauncherProcess.Refresh()
    if ($LauncherProcess.HasExited) {
        throw 'Harness launcher exited before its runtime process identity could be verified'
    }

    $runtimeProcess = if ($RuntimeProcessId -eq $LauncherProcess.Id) {
        $LauncherProcess
    } else {
        try {
            [System.Diagnostics.Process]::GetProcessById($RuntimeProcessId)
        } catch {
            throw "Harness ready.json runtime pid does not exist: $RuntimeProcessId"
        }
    }
    try {
        $runtimeProcess.Refresh()
        if ($runtimeProcess.HasExited) {
            throw "Harness ready.json runtime pid already exited: $RuntimeProcessId"
        }
        # A runtime created before the launcher cannot be its child.  The one-second
        # tolerance is only for filesystem/OS timestamp granularity; ancestry below
        # remains the authoritative relationship proof.
        if ($runtimeProcess.StartTime.ToUniversalTime() -lt
            $LauncherProcess.StartTime.ToUniversalTime().AddSeconds(-1)) {
            throw 'Harness ready.json runtime process predates the launcher'
        }

        $ancestry = [System.Collections.Generic.List[int]]::new()
        $seen = [System.Collections.Generic.HashSet[int]]::new()
        $currentPid = $RuntimeProcessId
        for ($depth = 0; $depth -lt 64; $depth += 1) {
            if ($currentPid -le 0 -or -not $seen.Add($currentPid)) {
                throw 'Harness runtime process ancestry is invalid or cyclic'
            }
            $ancestry.Add($currentPid)
            if ($currentPid -eq $LauncherProcess.Id) {
                return [pscustomobject]@{
                    LauncherPid = [int]$LauncherProcess.Id
                    RuntimePid = [int]$RuntimeProcessId
                    RuntimeIsLauncher = ($RuntimeProcessId -eq $LauncherProcess.Id)
                    AncestryPids = @($ancestry)
                    RuntimeProcess = $runtimeProcess
                }
            }
            $rows = @(Get-CimInstance -ClassName Win32_Process `
                -Filter ("ProcessId = {0}" -f $currentPid) -ErrorAction Stop)
            if ($rows.Count -ne 1) {
                throw "Harness runtime ancestry process disappeared: $currentPid"
            }
            $currentPid = [int]$rows[0].ParentProcessId
        }
        throw 'Harness runtime ancestry exceeds the fail-closed depth limit'
    } catch {
        if ($runtimeProcess.Id -ne $LauncherProcess.Id) {
            $runtimeProcess.Dispose()
        }
        throw
    }
}

function Start-W4LoopbackHarness {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$RunContext,
        [Parameter(Mandatory = $true)]$ScenarioContext
    )

    if ($null -eq $RunContext.HarnessRoot) {
        throw 'Chat scenario requires an explicit external harness root'
    }
    $exe = Join-Path $RunContext.HarnessRoot 'pkv-loopback-provider.exe'
    $manifestPath = Join-Path $RunContext.HarnessRoot 'manifest.json'
    $contractPath = Join-Path $RunContext.HarnessRoot 'contract.v1.json'
    $scriptPath = Join-Path $RunContext.HarnessRoot 'scripts\w4-chat-lifecycle.v1.json'
    foreach ($path in @($exe, $manifestPath, $contractPath, $scriptPath)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Chat harness input is missing: $path"
        }
    }
    $manifest = Read-W4JsonFile -Path $manifestPath
    if ([string]$manifest.schema_version -ne 'pkv.w3.loopback.manifest.v1' -or
        [string]$manifest.contract_id -ne 'w3.openai_compatible_loopback.v1' -or
        [string]$manifest.distribution -ne 'e2e-only' -or
        [string]$manifest.release_payload_membership -ne 'forbidden' -or
        [string]$manifest.runtime.kind -ne 'frozen') {
        throw 'Harness manifest does not declare the frozen e2e-only contract'
    }
    if ([string]$manifest.runtime.path -ne 'pkv-loopback-provider.exe') {
        throw 'Harness manifest runtime.path is not pkv-loopback-provider.exe'
    }
    Assert-W4ExactObjectFields -Object $manifest.build -Fields @(
        'source_revision', 'build_fingerprint_sha256', 'toolchain_lock_sha256'
    ) -Label 'Harness manifest build identity'
    $dependencyManifest = Read-W4JsonFile `
        -Path (Join-Path $RunContext.ArtifactRoot 'dependency-manifest.json')
    if ([string]$manifest.build.source_revision -ne [string]$RunContext.BuildInfo.source_revision -or
        [string]$manifest.build.build_fingerprint_sha256 -ne [string]$RunContext.BuildInfo.build_fingerprint -or
        [string]$manifest.build.toolchain_lock_sha256 -ne
            [string]$dependencyManifest.environment_lock_sha256) {
        throw 'Harness build identity does not match the release Artifact revision/fingerprint/toolchain lock'
    }
    $exeSha = Get-W4FileSha256 -Path $exe
    $manifestSha = Get-W4FileSha256 -Path $manifestPath
    $contractSha = Get-W4FileSha256 -Path $contractPath
    $scriptSha = Get-W4FileSha256 -Path $scriptPath
    if ($exeSha -ne [string]$manifest.runtime.sha256 -or
        $contractSha -ne [string]$manifest.contract.sha256) {
        throw 'Harness executable/contract hash disagrees with manifest.json'
    }
    $scriptRow = @($manifest.scripts | Where-Object {
        [string]$_.script_id -eq 'w4.chat.lifecycle.v1'
    })
    if ($scriptRow.Count -ne 1 -or $scriptSha -ne [string]$scriptRow[0].sha256) {
        throw 'W4 chat lifecycle script hash disagrees with manifest.json'
    }

    $state = Join-Path $ScenarioContext.Workspace 'harness-state'
    [void][System.IO.Directory]::CreateDirectory($state)
    if (@(Get-ChildItem -LiteralPath $state -Force).Count -ne 0) {
        throw 'Harness state directory must be empty before startup'
    }
    $evidence = Join-Path $ScenarioContext.Evidence 'harness'
    [void][System.IO.Directory]::CreateDirectory($evidence)
    Write-W4JsonFile -Path (Join-Path $evidence 'invocation.json') -Value ([ordered]@{
        executable = $exe
        arguments = @(
            'serve', '--manifest', $manifestPath, '--script', $scriptPath,
            '--state-dir', $state, '--port', '0', '--idle-timeout-seconds', '120'
        )
        working_directory = $ScenarioContext.WorkingDirectory
        executable_sha256 = $exeSha
        manifest_sha256 = $manifestSha
        contract_sha256 = $contractSha
        script_sha256 = $scriptSha
    })
    $process = Start-W4LongRunningProcess -FileName $exe -Arguments @(
        'serve', '--manifest', $manifestPath, '--script', $scriptPath,
        '--state-dir', $state, '--port', '0', '--idle-timeout-seconds', '120'
    ) -WorkingDirectory $ScenarioContext.WorkingDirectory `
        -Environment $ScenarioContext.Environment
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $runtimeIdentity = $null
    try {
        $readyPath = Join-Path $state 'ready.json'
        $deadline = [DateTime]::UtcNow.AddSeconds(30)
        while (-not (Test-Path -LiteralPath $readyPath -PathType Leaf) -and
            [DateTime]::UtcNow -lt $deadline -and -not $process.HasExited) {
            Start-Sleep -Milliseconds 50
        }
        if (-not (Test-Path -LiteralPath $readyPath -PathType Leaf)) {
            throw 'Harness did not atomically publish ready.json'
        }
        $ready = Read-W4JsonFile -Path $readyPath
        $expectedFields = @(
            'schema_version', 'contract_id', 'harness_version', 'pid', 'host',
            'port', 'base_url', 'health_url', 'contract_url', 'telemetry_url',
            'frozen', 'harness_sha256', 'manifest_sha256', 'contract_sha256',
            'script_id', 'script_sha256'
        )
        Assert-W4JsonObjectFields -Object $ready -RequiredFields $expectedFields -Label 'harness ready.json'
        if (@($ready.PSObject.Properties.Name).Count -ne $expectedFields.Count) {
            throw 'Harness ready.json contains undeclared fields'
        }
        if ([string]$ready.schema_version -ne 'pkv.w3.loopback.ready.v1' -or
            [string]$ready.contract_id -ne 'w3.openai_compatible_loopback.v1' -or
            [string]$ready.host -ne '127.0.0.1' -or
            -not [bool]$ready.frozen -or
            [string]$ready.script_id -ne 'w4.chat.lifecycle.v1') {
            throw 'Harness ready.json identity/loopback contract is invalid'
        }
        $runtimeIdentity = Get-W4ValidatedProcessIdentity -LauncherProcess $process `
            -RuntimeProcessId ([int]$ready.pid)
        if ([string]$ready.harness_sha256 -ne $exeSha -or
            [string]$ready.manifest_sha256 -ne $manifestSha -or
            [string]$ready.contract_sha256 -ne $contractSha -or
            [string]$ready.script_sha256 -ne $scriptSha) {
            throw 'Harness ready.json hash identity disagrees with supplied files'
        }
        foreach ($url in @($ready.base_url, $ready.health_url, $ready.contract_url, $ready.telemetry_url)) {
            $uri = [Uri][string]$url
            if ($uri.Scheme -ne 'http' -or $uri.Host -ne '127.0.0.1' -or $uri.Port -ne [int]$ready.port) {
                throw "Harness published a non-loopback or inconsistent endpoint: $url"
            }
        }
        $contractResponse = Invoke-W4LoopbackGetJson -Url ([string]$ready.contract_url)
        if ([string]$contractResponse.schema_version -ne 'pkv.w3.loopback.contract-response.v1') {
            throw 'Harness contract endpoint returned an unexpected schema'
        }
        Write-W4JsonFile -Path (Join-Path $evidence 'ready.json') -Value $ready
        Write-W4JsonFile -Path (Join-Path $evidence 'contract-response.json') -Value $contractResponse
        Write-W4JsonFile -Path (Join-Path $evidence 'process-identity.json') -Value ([ordered]@{
            schema_version = 'pkv.m13.w4-harness-process-identity.v1'
            launcher_pid = [int]$runtimeIdentity.LauncherPid
            runtime_pid = [int]$runtimeIdentity.RuntimePid
            runtime_is_launcher = [bool]$runtimeIdentity.RuntimeIsLauncher
            ancestry_pids = @($runtimeIdentity.AncestryPids)
        })
        return [pscustomobject]@{
            Process = $process
            RuntimeProcess = $runtimeIdentity.RuntimeProcess
            LauncherPid = [int]$runtimeIdentity.LauncherPid
            RuntimePid = [int]$runtimeIdentity.RuntimePid
            StdoutTask = $stdoutTask
            StderrTask = $stderrTask
            StateDirectory = $state
            Evidence = $evidence
            Ready = $ready
            ExecutableSha256 = $exeSha
            ManifestSha256 = $manifestSha
            ContractSha256 = $contractSha
            ScriptSha256 = $scriptSha
        }
    } catch {
        if ($null -ne $runtimeIdentity -and
            [int]$runtimeIdentity.RuntimePid -ne $process.Id) {
            $runtimeChild = [System.Diagnostics.Process]$runtimeIdentity.RuntimeProcess
            if (-not $runtimeChild.HasExited) {
                Stop-W4ProcessTree -Process $runtimeChild
            }
            $runtimeChild.Dispose()
        }
        Stop-W4ProcessTree -Process $process
        $process.Dispose()
        throw
    }
}

function Stop-W4LoopbackHarness {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$Harness)

    $shutdown = Join-Path $Harness.StateDirectory 'shutdown.request'
    [System.IO.File]::WriteAllText($shutdown, "shutdown`n", [System.Text.UTF8Encoding]::new($false))
    $process = [System.Diagnostics.Process]$Harness.Process
    $runtimeProcess = [System.Diagnostics.Process]$Harness.RuntimeProcess
    $forced = $false
    try {
        if (-not $process.WaitForExit(30000)) {
            $forced = $true
            Stop-W4ProcessTree -Process $process
        } else {
            $process.WaitForExit()
        }
        if ($runtimeProcess.Id -ne $process.Id -and -not $runtimeProcess.HasExited) {
            if (-not $runtimeProcess.WaitForExit(5000)) {
                $forced = $true
                Stop-W4ProcessTree -Process $runtimeProcess
            } else {
                $runtimeProcess.WaitForExit()
            }
        }
        $stdout = $Harness.StdoutTask.GetAwaiter().GetResult()
        $stderr = $Harness.StderrTask.GetAwaiter().GetResult()
        [System.IO.File]::WriteAllText((Join-Path $Harness.Evidence 'stdout.txt'), $stdout, [System.Text.UTF8Encoding]::new($false))
        [System.IO.File]::WriteAllText((Join-Path $Harness.Evidence 'stderr.txt'), $stderr, [System.Text.UTF8Encoding]::new($false))
        $processRecord = [ordered]@{
            launcher_pid = [int]$Harness.LauncherPid
            runtime_pid = [int]$Harness.RuntimePid
            exit_code = if ($process.HasExited) { [int]$process.ExitCode } else { $null }
            runtime_exit_code = if ($runtimeProcess.HasExited) { [int]$runtimeProcess.ExitCode } else { $null }
            forced_termination = $forced
            timed_out = $forced
        }
        Write-W4JsonFile -Path (Join-Path $Harness.Evidence 'process.json') -Value $processRecord
        if ($forced -or $process.ExitCode -ne 0 -or $runtimeProcess.ExitCode -ne 0) {
            throw "Harness did not exit normally after exact shutdown request: launcher=$($process.ExitCode) runtime=$($runtimeProcess.ExitCode)"
        }
        $resultPath = Join-Path $Harness.StateDirectory 'result.json'
        $result = Read-W4JsonFile -Path $resultPath
        if ([string]$result.schema_version -ne 'pkv.w3.loopback.result.v1' -or
            [string]$result.result -ne 'passed' -or
            [int]$result.completed_steps -ne 3 -or
            [int]$result.total_steps -ne 3) {
            throw 'Harness result.json did not report exactly three passed steps'
        }
        Write-W4JsonFile -Path (Join-Path $Harness.Evidence 'result.json') -Value $result
        return $result
    } finally {
        if ($runtimeProcess.Id -ne $process.Id) {
            if (-not $runtimeProcess.HasExited) {
                Stop-W4ProcessTree -Process $runtimeProcess
            }
            $runtimeProcess.Dispose()
        }
        if (-not $process.HasExited) {
            Stop-W4ProcessTree -Process $process
        }
        $process.Dispose()
    }
}

function Write-W4ChatLocalConfig {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$ScenarioContext,
        [Parameter(Mandatory = $true)][string]$BaseUrl
    )

    $directory = Join-Path $ScenarioContext.UserDataRoot 'config'
    [void][System.IO.Directory]::CreateDirectory($directory)
    $text = @"
ai:
  llm:
    provider: "openai_compatible"
    api_key: "pkv-w4-synthetic-token"
    base_url: "$BaseUrl"
    model: "pkv-loopback-chat-v1"
    max_tokens: 128
    temperature: 0.2
    timeout: 30
    max_retries: 0
"@
    [System.IO.File]::WriteAllText(
        (Join-Path $directory 'local.yaml'),
        $text.Replace("`r`n", "`n") + "`n",
        [System.Text.UTF8Encoding]::new($false)
    )
}

function Invoke-W4ChatLoopbackScenario {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$RunContext,
        [Parameter(Mandatory = $true)]$ScenarioContext
    )

    [void](Invoke-W4Install -RunContext $RunContext -ScenarioContext $ScenarioContext)
    $harness = Start-W4LoopbackHarness -RunContext $RunContext -ScenarioContext $ScenarioContext
    $harnessStopped = $false
    try {
        Write-W4ChatLocalConfig -ScenarioContext $ScenarioContext -BaseUrl ([string]$harness.Ready.base_url)
        $successPrompt = [System.IO.File]::ReadAllText((Join-Path $RunContext.FixtureRoot 'chat-success-prompt.v1.txt')).Trim()
        $stopPrompt = [System.IO.File]::ReadAllText((Join-Path $RunContext.FixtureRoot 'chat-stop-prompt.v1.txt')).Trim()
        $errorPrompt = [System.IO.File]::ReadAllText((Join-Path $RunContext.FixtureRoot 'chat-error-prompt.v1.txt')).Trim()
        $gui = Start-W4GuiApplication -ScenarioContext $ScenarioContext -EvidenceName 'gui-chat'
        $guiClosed = $false
        try {
            Select-W4NavigationItem -Gui $gui -Name '对话'
            Assert-W4UiaContractSegment -Gui $gui -AutomationIds @(
                'chat_view', 'chat_new_session', 'session_list', 'chat_messages',
                'chat_request_status', 'chat_input', 'chat_send', 'chat_stop',
                'chat_round_count'
            ) -EvidenceName 'uia-contract-chat.json'
            Invoke-W4UiaElement -Element (Get-W4UiaElementById -Root $gui.Window -AutomationId 'chat_new_session')
            $input = Get-W4UiaElementById -Root $gui.Window -AutomationId 'chat_input'
            $send = Get-W4UiaElementById -Root $gui.Window -AutomationId 'chat_send'
            $stop = Get-W4UiaElementById -Root $gui.Window -AutomationId 'chat_stop'
            $status = Get-W4UiaElementById -Root $gui.Window -AutomationId 'chat_request_status'
            $messages = Get-W4UiaElementById -Root $gui.Window -AutomationId 'chat_messages'
            $rounds = Get-W4UiaElementById -Root $gui.Window -AutomationId 'chat_round_count'

            Set-W4UiaValue -Element $input -Value $successPrompt
            Invoke-W4UiaElement -Element $send
            [void](Wait-W4UiaText -Element $status -Expected @('请求中') -TimeoutSeconds 20)
            [void](Wait-W4UiaText -Element $status -Expected @('已完成') -TimeoutSeconds 60)
            [void](Wait-W4UiaTextContains -Element $messages -Text 'PKV_W4_SUCCESS_V1' -TimeoutSeconds 20)
            [void](Wait-W4UiaText -Element $rounds -Expected @('轮数: 1 / 3') -TimeoutSeconds 10)

            Set-W4UiaValue -Element $input -Value $stopPrompt
            Invoke-W4UiaElement -Element $send
            [void](Wait-W4UiaText -Element $status -Expected @('请求中') -TimeoutSeconds 20)
            [void](Wait-W4UiaTextContains -Element $messages -Text 'PKV_W4_STOP_PARTIAL_V1' -TimeoutSeconds 30)
            Invoke-W4UiaElement -Element $stop
            [void](Wait-W4UiaText -Element $status -Expected @('已停止且未保存') -TimeoutSeconds 30)
            [void](Wait-W4UiaText -Element $rounds -Expected @('轮数: 1 / 3') -TimeoutSeconds 10)

            Set-W4UiaValue -Element $input -Value $errorPrompt
            Invoke-W4UiaElement -Element $send
            [void](Wait-W4UiaText -Element $status -Expected @('请求中') -TimeoutSeconds 20)
            [void](Wait-W4UiaText -Element $status `
                -Expected @('失败（错误代码：chat_provider_failed）') -TimeoutSeconds 60)
            [void](Wait-W4UiaText -Element $rounds -Expected @('轮数: 1 / 3') -TimeoutSeconds 10)
            Save-W4Screenshot -Path (Join-Path $gui.Evidence 'chat-terminals.png') `
                -Element $gui.Window -ProcessId $gui.Process.Id
            [void](Stop-W4GuiApplication -Gui $gui)
            $guiClosed = $true
        } finally {
            if (-not $guiClosed -and -not $gui.Process.HasExited) {
                Stop-W4ProcessTree -Process $gui.Process
                $gui.Process.Dispose()
            }
        }

        $restart = Start-W4GuiApplication -ScenarioContext $ScenarioContext -EvidenceName 'gui-chat-restart'
        $restartClosed = $false
        try {
            Select-W4NavigationItem -Gui $restart -Name '对话'
            $sessionList = Get-W4UiaElementById -Root $restart.Window -AutomationId 'session_list'
            [void](Select-W4FirstListItem -Root $sessionList)
            $restartMessages = Get-W4UiaElementById -Root $restart.Window -AutomationId 'chat_messages'
            $persisted = Wait-W4UiaTextContains -Element $restartMessages -Text 'PKV_W4_SUCCESS_V1' -TimeoutSeconds 30
            if ($persisted.Contains($stopPrompt) -or
                $persisted.Contains('PKV_W4_STOP_PARTIAL_V1') -or
                $persisted.Contains($errorPrompt)) {
                throw 'Stopped/error Chat turn leaked into durable restart state'
            }
            [void](Wait-W4UiaText `
                -Element (Get-W4UiaElementById -Root $restart.Window -AutomationId 'chat_round_count') `
                -Expected @('轮数: 1 / 3') -TimeoutSeconds 10)
            [void](Stop-W4GuiApplication -Gui $restart)
            $restartClosed = $true
        } finally {
            if (-not $restartClosed -and -not $restart.Process.HasExited) {
                Stop-W4ProcessTree -Process $restart.Process
                $restart.Process.Dispose()
            }
        }

        $telemetry = Invoke-W4LoopbackGetJson -Url ([string]$harness.Ready.telemetry_url)
        if ([string]$telemetry.schema_version -ne 'pkv.w3.loopback.telemetry.v1' -or
            [string]$telemetry.contract_id -ne 'w3.openai_compatible_loopback.v1' -or
            [string]$telemetry.status -ne 'complete' -or
            [int]$telemetry.completed_steps -ne 3 -or
            [int]$telemetry.total_steps -ne 3 -or
            @($telemetry.violations).Count -ne 0) {
            throw 'Harness telemetry did not report three complete, violation-free steps'
        }
        $success = @($telemetry.records | Where-Object { [string]$_.event -eq 'stream_completed' })
        $cancelled = @($telemetry.records | Where-Object { [string]$_.event -eq 'client_cancelled' })
        $providerError = @($telemetry.records | Where-Object { [string]$_.event -eq 'provider_error' })
        if ($success.Count -ne 1 -or -not [bool]$success[0].finish_sent -or
            -not [bool]$success[0].usage_sent -or -not [bool]$success[0].done_sent) {
            throw 'Harness success telemetry is not exactly one complete SSE terminal'
        }
        if ($cancelled.Count -ne 1 -or -not [bool]$cancelled[0].client_disconnected -or
            [bool]$cancelled[0].finish_sent -or [bool]$cancelled[0].usage_sent -or
            [bool]$cancelled[0].done_sent) {
            throw 'Harness stop telemetry did not prove client cancellation before terminal frames'
        }
        if ($providerError.Count -ne 1 -or [int]$providerError[0].response_status -ne 503) {
            throw 'Harness provider-error telemetry is not exactly one HTTP 503 terminal'
        }
        Write-W4JsonFile -Path (Join-Path $harness.Evidence 'telemetry.json') -Value $telemetry
        $harnessResult = Stop-W4LoopbackHarness -Harness $harness
        $harnessStopped = $true
    } finally {
        if (-not $harnessStopped -and -not $harness.Process.HasExited) {
            Stop-W4ProcessTree -Process $harness.Process
            $harness.Process.Dispose()
        }
    }
    $oracle = [ordered]@{
        status = 'passed'
        success_terminal = 'completed'
        stop_terminal = 'stopped'
        error_terminal = 'chat_provider_failed'
        restart_round_count = 1
        harness_steps = [int]$harnessResult.completed_steps
        harness_sha256 = $harness.ExecutableSha256
        client_disconnect_proven = $true
    }
    Write-W4JsonFile -Path (Join-Path $ScenarioContext.Evidence 'oracle.json') -Value $oracle
    return $oracle
}

function Invoke-W4UpgradeRejectionScenario {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$RunContext,
        [Parameter(Mandatory = $true)]$ScenarioContext
    )

    [void](Invoke-W4Install -RunContext $RunContext -ScenarioContext $ScenarioContext)
    [void](Initialize-W4InstalledDataRoot -ScenarioContext $ScenarioContext)
    $casesRoot = Join-Path $ScenarioContext.Workspace 'upgrade-cases'
    [void][System.IO.Directory]::CreateDirectory($casesRoot)
    $caseResults = [System.Collections.Generic.List[object]]::new()
    foreach ($case in @(
        [ordered]@{ name = 'old'; code = 'database_upgrade_required' },
        [ordered]@{ name = 'future'; code = 'database_future_version' },
        [ordered]@{ name = 'corrupt'; code = 'database_not_sqlite' }
    )) {
        $caseRoot = Join-Path $casesRoot $case.name
        [System.IO.Directory]::CreateDirectory($caseRoot) | Out-Null
        [System.IO.Directory]::Delete($caseRoot, $true)
        Copy-Item -LiteralPath $ScenarioContext.UserDataRoot -Destination $caseRoot -Recurse
        $database = Join-Path $caseRoot 'db\knowledge_vault.db'
        if ($case.name -eq 'old') {
            Invoke-W4SqliteStatement -DatabasePath $database `
                -Sql "DELETE FROM schema_version WHERE version <> '1.0.0';"
        } elseif ($case.name -eq 'future') {
            Invoke-W4SqliteStatement -DatabasePath $database `
                -Sql "UPDATE schema_version SET version='99.0.0' WHERE version_id=(SELECT MAX(version_id) FROM schema_version);"
        } else {
            [System.IO.File]::WriteAllText($database, 'not a sqlite database', [System.Text.UTF8Encoding]::new($false))
        }
        $caseEnvironment = New-W4IsolatedEnvironment `
            -WorkspaceRoot (Join-Path $ScenarioContext.Workspace ("env-" + $case.name)) `
            -UserDataRoot $caseRoot -ArtifactRoot $ScenarioContext.InstallRoot
        $before = Get-W4TreeSha256 -Root $caseRoot -ExcludedRelativePrefixes @('logs', 'tmp')
        $result = Invoke-W4Process -FileName $ScenarioContext.PkvExe -Arguments @('stats') `
            -WorkingDirectory $ScenarioContext.WorkingDirectory -Environment $caseEnvironment `
            -EvidenceDirectory (Join-Path $ScenarioContext.Evidence ("upgrade-" + $case.name)) `
            -ExpectedExitCodes @(1) -TimeoutSeconds 60
        $payload = ConvertFrom-W4StrictJsonText -Text $result.StandardOutput `
            -Label ("upgrade rejection " + $case.name)
        if ([string]$payload.status -ne 'error' -or [string]$payload.code -ne [string]$case.code -or
            [string]$payload.stage -ne 'runtime_bootstrap') {
            throw "Upgrade case $($case.name) did not expose the expected startup envelope/code"
        }
        $after = Get-W4TreeSha256 -Root $caseRoot -ExcludedRelativePrefixes @('logs', 'tmp')
        if ($before -ne $after) {
            throw "Upgrade rejection mutated synthetic user data: $($case.name)"
        }
        $caseResults.Add([ordered]@{
            case = $case.name
            code = [string]$payload.code
            exit_code = [int]$result.ExitCode
            before_sha256 = $before
            after_sha256 = $after
            non_mutating = $true
        })
    }
    $oracle = [ordered]@{
        status = 'passed'
        cases = @($caseResults)
    }
    Write-W4JsonFile -Path (Join-Path $ScenarioContext.Evidence 'oracle.json') -Value $oracle
    return $oracle
}

function Invoke-W4UninstallDataRetentionScenario {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$RunContext,
        [Parameter(Mandatory = $true)]$ScenarioContext
    )

    # The installer/uninstaller contract deliberately owns only the default
    # per-user data root.  Exercise deletion there, while a separate custom
    # PKV_DATA_ROOT sentinel proves the uninstaller never broadens its scope.
    $customDataRoot = [System.IO.Path]::GetFullPath($ScenarioContext.UserDataRoot)
    $customSentinel = Join-Path $customDataRoot 'w4-custom-root-must-survive.txt'
    [System.IO.File]::WriteAllText(
        $customSentinel,
        "synthetic-w4-custom-retention`n",
        [System.Text.UTF8Encoding]::new($false)
    )
    $customSentinelSha = Get-W4FileSha256 -Path $customSentinel
    $defaultDataRoot = Join-Path ([string]$ScenarioContext.Environment['LOCALAPPDATA']) 'PersonalKnowledgeVault'
    if ($defaultDataRoot.Equals($customDataRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Synthetic default and custom user data roots must be disjoint'
    }
    [void][System.IO.Directory]::CreateDirectory($defaultDataRoot)
    $ScenarioContext.UserDataRoot = [System.IO.Path]::GetFullPath($defaultDataRoot)
    $ScenarioContext.Environment['PKV_DATA_ROOT'] = $ScenarioContext.UserDataRoot

    [void](Invoke-W4Install -RunContext $RunContext -ScenarioContext $ScenarioContext)
    [void](Initialize-W4InstalledDataRoot -ScenarioContext $ScenarioContext)
    $sentinel = Join-Path $ScenarioContext.UserDataRoot 'w4-retention-sentinel.txt'
    [System.IO.File]::WriteAllText($sentinel, "synthetic-w4-retention`n", [System.Text.UTF8Encoding]::new($false))
    $sentinelSha = Get-W4FileSha256 -Path $sentinel
    [void](Invoke-W4Uninstall -RunContext $RunContext -ScenarioContext $ScenarioContext -EvidenceName 'uninstall-preserve')
    if (Test-Path -LiteralPath $ScenarioContext.InstallRoot) {
        throw 'Default uninstall did not remove the application root'
    }
    if (-not (Test-Path -LiteralPath $sentinel -PathType Leaf) -or
        (Get-W4FileSha256 -Path $sentinel) -ne $sentinelSha) {
        throw 'Default uninstall did not preserve synthetic user data exactly'
    }

    [void](Invoke-W4Install -RunContext $RunContext -ScenarioContext $ScenarioContext -EvidenceName 'reinstall')
    if (-not (Test-Path -LiteralPath $sentinel -PathType Leaf)) {
        throw 'Reinstall unexpectedly removed preserved user data'
    }
    [void](Invoke-W4Uninstall -RunContext $RunContext -ScenarioContext $ScenarioContext `
        -DeleteUserData -EvidenceName 'uninstall-delete-explicit')
    if (Test-Path -LiteralPath $ScenarioContext.InstallRoot) {
        throw 'Explicit uninstall did not remove the application root'
    }
    if (Test-Path -LiteralPath $ScenarioContext.UserDataRoot) {
        throw 'Explicit, confirmed uninstall did not remove the synthetic user data root'
    }
    if (-not (Test-Path -LiteralPath $customSentinel -PathType Leaf) -or
        (Get-W4FileSha256 -Path $customSentinel) -ne $customSentinelSha) {
        throw 'Explicit uninstall escaped the default data-root boundary and changed custom data'
    }
    $oracle = [ordered]@{
        status = 'passed'
        default_uninstall_removed_application = $true
        default_uninstall_preserved_user_data = $true
        explicit_delete_token = 'DELETE-PKV-USER-DATA'
        explicit_uninstall_removed_application = $true
        explicit_uninstall_removed_user_data = $true
        custom_user_data_preserved = $true
        sentinel_sha256 = $sentinelSha
        custom_sentinel_sha256 = $customSentinelSha
    }
    Write-W4JsonFile -Path (Join-Path $ScenarioContext.Evidence 'oracle.json') -Value $oracle
    return $oracle
}

function Invoke-W4Scenario {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$RunContext,
        [Parameter(Mandatory = $true)]$Scenario
    )

    $context = New-W4ScenarioContext -RunContext $RunContext -Scenario $Scenario
    $scenarioFailure = $null
    try {
        switch ([string]$Scenario.handler) {
            'release_audit' {
                $oracle = Invoke-W4ReleaseAuditScenario -RunContext $RunContext -ScenarioContext $context
            }
            'application_lifecycle' {
                $oracle = Invoke-W4ApplicationLifecycleScenario -RunContext $RunContext -ScenarioContext $context
            }
            'url_archive_ssrf_rejection' {
                $oracle = Invoke-W4UrlArchiveSsrfScenario -RunContext $RunContext -ScenarioContext $context
            }
            'semantic_provider_unavailable' {
                $oracle = Invoke-W4SemanticProviderUnavailableScenario -RunContext $RunContext -ScenarioContext $context
            }
            'mcp_stdio_call' {
                $oracle = Invoke-W4McpStdioScenario -RunContext $RunContext -ScenarioContext $context
            }
            'offline_text_archive' {
                $oracle = Invoke-W4OfflineTextArchiveScenario -RunContext $RunContext -ScenarioContext $context
            }
            'bm25_search' {
                $oracle = Invoke-W4Bm25SearchScenario -RunContext $RunContext -ScenarioContext $context
            }
            'chat_loopback' {
                $oracle = Invoke-W4ChatLoopbackScenario -RunContext $RunContext -ScenarioContext $context
            }
            'upgrade_rejection' {
                $oracle = Invoke-W4UpgradeRejectionScenario -RunContext $RunContext -ScenarioContext $context
            }
            'uninstall_data_retention' {
                $oracle = Invoke-W4UninstallDataRetentionScenario -RunContext $RunContext -ScenarioContext $context
            }
            default {
                throw "Unknown W4 scenario handler: $($Scenario.handler)"
            }
        }
    } catch {
        $scenarioFailure = $_
        throw
    } finally {
        try {
            Remove-W4ScenarioInstallRoot -ScenarioContext $context
        } catch {
            if ($null -ne $scenarioFailure) {
                throw "Scenario failed ($($scenarioFailure.Exception.Message)); InstallRoot cleanup also failed: $($_.Exception.Message)"
            }
            throw
        }
    }
    return [pscustomobject]@{
        Context = $context
        Oracle = $oracle
    }
}

Export-ModuleMember -Function @('Invoke-W4Scenario')
