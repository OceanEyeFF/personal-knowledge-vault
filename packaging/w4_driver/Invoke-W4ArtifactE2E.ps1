#requires -Version 5.1

[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$DistributionZip,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$CandidateRoot,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$DistributionSha256,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ProvenancePath,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ComplianceSourcesRoot,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ComplianceManifestPath,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ComplianceProvenancePath,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$FixtureRoot,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$EvidenceRoot,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$WorkspaceRoot,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ScenarioContract,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$HarnessRoot,

    [Parameter()]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')]
    [string]$ExecutionId = ([Guid]::NewGuid().ToString('N'))
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'W4.Driver.psm1') -Force -ErrorAction Stop
Import-Module (Join-Path $PSScriptRoot 'W4.Scenarios.psm1') -Force -ErrorAction Stop

function Get-CanonicalW4Path {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][ValidateSet('Leaf', 'Container')][string]$Kind
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Required path is missing: $Path"
    }
    if ($Kind -eq 'Leaf' -and -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required path is not a file: $Path"
    }
    if ($Kind -eq 'Container' -and -not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "Required path is not a directory: $Path"
    }
    return [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $Path -ErrorAction Stop).ProviderPath)
}

function Assert-W4ExactFields {
    param(
        [Parameter(Mandatory = $true)]$Object,
        [Parameter(Mandatory = $true)][string[]]$Fields,
        [Parameter(Mandatory = $true)][string]$Label
    )
    Assert-W4JsonObjectFields -Object $Object -RequiredFields $Fields -Label $Label
    $actual = @($Object.PSObject.Properties.Name | Sort-Object)
    $expected = @($Fields | Sort-Object)
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

function Invoke-W4DiskPreflight {
    param(
        [Parameter(Mandatory = $true)][string]$ZipPath,
        [Parameter(Mandatory = $true)][string]$WorkspacePath,
        [Parameter(Mandatory = $true)][string]$EvidencePath
    )

    Add-Type -AssemblyName System.IO.Compression -ErrorAction Stop
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop
    $archive = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    $expandedBytes = [int64]0
    try {
        foreach ($entry in $archive.Entries) {
            if ([string]$entry.FullName -and -not ([string]$entry.FullName).EndsWith('/')) {
                $entryLength = [int64]$entry.Length
                if ($entryLength -lt 0 -or $expandedBytes -gt ([int64]::MaxValue - $entryLength)) {
                    throw 'Artifact ZIP expanded-size metadata overflows the supported disk preflight range'
                }
                $expandedBytes += $entryLength
            }
        }
    } finally {
        $archive.Dispose()
    }
    if ($expandedBytes -le 0) {
        throw 'Artifact ZIP contains no file payload for disk preflight'
    }
    if ($expandedBytes -gt [int64]::MaxValue / 3) {
        throw 'Artifact ZIP expanded size exceeds the supported disk preflight range'
    }
    # Extraction plus one installed copy are live concurrently.  A third copy's
    # worth is retained as fail-closed headroom for installer staging/evidence.
    $requiredBytes = [Math]::Max([int64](512MB), [int64]($expandedBytes * 3))
    $workspaceFull = [System.IO.Path]::GetFullPath($WorkspacePath)
    $driveRoot = [System.IO.Path]::GetPathRoot($workspaceFull)
    if ([string]::IsNullOrWhiteSpace($driveRoot)) {
        throw "Cannot resolve workspace drive for disk preflight: $workspaceFull"
    }
    $drive = [System.IO.DriveInfo]::new($driveRoot)
    if (-not $drive.IsReady) {
        throw "Workspace drive is not ready for W4 execution: $driveRoot"
    }
    $availableBytes = [int64]$drive.AvailableFreeSpace
    $sufficient = $availableBytes -ge $requiredBytes
    Write-W4JsonFile -Path $EvidencePath -Value ([ordered]@{
        schema_version = 'pkv.m13.w4-disk-preflight.v1'
        drive_root = $drive.Name
        available_free_bytes = $availableBytes
        artifact_expanded_bytes = $expandedBytes
        required_free_bytes = $requiredBytes
        sequential_install_cleanup = $true
        sufficient = [bool]$sufficient
    })
    if (-not $sufficient) {
        throw "Insufficient free space for W4 Artifact E2E: available=$availableBytes required=$requiredBytes drive=$($drive.Name)"
    }
}

function Expand-W4ZipSafely {
    param(
        [Parameter(Mandatory = $true)][string]$ZipPath,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$ExpectedRootName
    )

    Add-Type -AssemblyName System.IO.Compression -ErrorAction Stop
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop
    [void][System.IO.Directory]::CreateDirectory($Destination)
    if (@(Get-ChildItem -LiteralPath $Destination -Force).Count -ne 0) {
        throw "Extraction destination must be empty: $Destination"
    }
    $archive = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    $seen = @{}
    try {
        foreach ($entry in $archive.Entries) {
            $name = ([string]$entry.FullName).Replace('\', '/')
            if ([string]::IsNullOrWhiteSpace($name) -or $name.StartsWith('/') -or
                $name -match '^[A-Za-z]:' -or $name.IndexOf([char]0) -ge 0) {
                throw "ZIP contains an invalid absolute/empty entry: $name"
            }
            $segments = @($name.Split('/') | Where-Object { $_ -ne '' })
            if ($segments.Count -eq 0 -or $segments[0] -ne $ExpectedRootName -or
                @($segments | Where-Object { $_ -eq '.' -or $_ -eq '..' }).Count -ne 0) {
                throw "ZIP entry violates the single-root/no-traversal contract: $name"
            }
            $key = $name.TrimEnd('/').ToLowerInvariant()
            if ($seen.ContainsKey($key)) {
                throw "ZIP contains duplicate case-insensitive entry: $name"
            }
            $seen[$key] = $true
            $target = Join-Path $Destination ($name.Replace('/', '\'))
            $fullTarget = [System.IO.Path]::GetFullPath($target)
            if (-not (Test-W4PathContainedBy -Candidate $fullTarget -Root $Destination)) {
                throw "ZIP entry escaped extraction root: $name"
            }
            if ($name.EndsWith('/')) {
                [void][System.IO.Directory]::CreateDirectory($fullTarget)
                continue
            }
            $parent = [System.IO.Path]::GetDirectoryName($fullTarget)
            [void][System.IO.Directory]::CreateDirectory($parent)
            $input = $entry.Open()
            $output = [System.IO.FileStream]::new(
                $fullTarget,
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
    if ($children.Count -ne 1 -or -not $children[0].PSIsContainer -or
        $children[0].Name -ne $ExpectedRootName) {
        throw 'ZIP did not extract to exactly one canonical root directory'
    }
    return $children[0].FullName
}

function Test-W4ScenarioContract {
    param([Parameter(Mandatory = $true)]$Contract)

    Assert-W4ExactFields -Object $Contract -Fields @(
        'schema_version', 'runner_version', 'artifact_version', 'ordered_scenarios',
        'required_matrix_rows', 'required_artifact_files', 'mcp'
    ) -Label 'W4 scenario contract'
    if ([string]$Contract.schema_version -cne 'pkv.m13.w4-driver-scenarios.v2' -or
        [string]$Contract.runner_version -cne 'pkv.m13.artifact-runner.v2' -or
        [string]$Contract.artifact_version -cne '0.8.1') {
        throw 'W4 scenario contract schema/runner/Artifact version is not frozen v2/v2/0.8.1'
    }
    $scenarios = @($Contract.ordered_scenarios)
    if ($scenarios.Count -ne 9) {
        throw "W4 contract must declare exactly 9 scenarios; got $($scenarios.Count)"
    }
    $ids = @($scenarios | ForEach-Object { [string]$_.scenario_id })
    if (@($ids | Sort-Object -Unique).Count -ne 9) {
        throw 'W4 scenario IDs are not unique'
    }
    foreach ($scenario in $scenarios) {
        $scenarioFields = @(
            'scenario_id', 'matrix_rows', 'handler', 'timeout_seconds',
            'requires_harness'
        )
        Assert-W4ExactFields -Object $scenario -Fields $scenarioFields `
            -Label "W4 scenario row $([string]$scenario.scenario_id)"
        if ($scenario.timeout_seconds -isnot [int] -or
            [int]$scenario.timeout_seconds -le 0 -or
            $scenario.requires_harness -isnot [bool]) {
            throw "W4 scenario row types are invalid: $([string]$scenario.scenario_id)"
        }
    }
    $rows = @($scenarios | ForEach-Object { @($_.matrix_rows) } | ForEach-Object { [string]$_ })
    if ($rows.Count -ne 10 -or @($rows | Sort-Object -Unique).Count -ne 10) {
        throw 'W4 scenarios must uniquely own exactly 10 lifecycle rows'
    }
    $requiredRows = @($Contract.required_matrix_rows | ForEach-Object { [string]$_ } | Sort-Object)
    if (($rows | Sort-Object | ConvertTo-Json -Compress) -ne
        ($requiredRows | ConvertTo-Json -Compress)) {
        throw 'W4 scenario matrix rows drifted from required_matrix_rows'
    }
    $harnessScenarios = @($scenarios | Where-Object { [bool]$_.requires_harness })
    if ($harnessScenarios.Count -ne 0) {
        throw 'Headless W4 scenarios must not require the external provider harness'
    }
}

function Get-W4EvidenceManifest {
    param(
        [Parameter(Mandatory = $true)][string]$ScenarioEvidenceRoot,
        [Parameter(Mandatory = $true)][string]$ScenarioId
    )
    $entries = @(Get-W4TreeManifest -Root $ScenarioEvidenceRoot `
        -ExcludedRelativePrefixes @('evidence-manifest.json', 'evidence-record.json'))
    if ($entries.Count -eq 0) {
        throw "Scenario produced no evidence files: $ScenarioId"
    }
    return [ordered]@{
        schema_version = 'pkv.m13.w4-scenario-evidence-manifest.v1'
        scenario_id = $ScenarioId
        entries = $entries
        tree_sha256 = Get-W4StringSha256 -Value ($entries | ConvertTo-Json -Depth 5 -Compress)
    }
}

function Test-W4EvidenceRecord {
    param(
        [Parameter(Mandatory = $true)]$Record,
        [Parameter(Mandatory = $true)][string]$RunRoot,
        [Parameter(Mandatory = $true)][bool]$HarnessRequired
    )
    $fields = @(
        'scenario_id', 'state', 'producer_lane', 'artifact_id', 'artifact_sha256',
        'normalized_manifest_sha256', 'build_fingerprint', 'source_revision',
        'runner_version', 'execution_id', 'executed_at', 'environment_fingerprint',
        'fixture_sha256', 'harness_sha256', 'evidence_manifest_sha256',
        'source_isolation_proof_sha256', 'oracle_result', 'evidence_paths'
    )
    Assert-W4ExactFields -Object $Record -Fields $fields -Label 'W4 evidence record'
    if (@('artifact_verified', 'artifact_failed') -notcontains [string]$Record.state -or
        [string]$Record.producer_lane -ne 'artifact-only' -or
        [string]$Record.runner_version -ne 'pkv.m13.artifact-runner.v2') {
        throw 'W4 evidence record has an invalid state/producer/runner identity'
    }
    $expectedOracle = if ([string]$Record.state -eq 'artifact_verified') { 'passed' } else { 'failed' }
    if ([string]$Record.oracle_result -ne $expectedOracle) {
        throw 'W4 evidence state and oracle_result disagree'
    }
    foreach ($field in @(
        'artifact_sha256', 'normalized_manifest_sha256', 'build_fingerprint',
        'environment_fingerprint', 'fixture_sha256', 'evidence_manifest_sha256',
        'source_isolation_proof_sha256'
    )) {
        if ([string]$Record.$field -notmatch '^[0-9a-f]{64}$') {
            throw "W4 evidence field is not SHA-256: $field"
        }
    }
    if ([string]$Record.source_revision -notmatch '^[0-9a-f]{40}$') {
        throw 'W4 evidence source_revision is invalid'
    }
    if ($HarnessRequired) {
        if ([string]$Record.harness_sha256 -notmatch '^[0-9a-f]{64}$') {
            throw 'Chat evidence record lacks harness_sha256'
        }
    } elseif ($null -ne $Record.harness_sha256) {
        throw 'Non-Chat evidence record must not declare harness_sha256'
    }
    if (@($Record.evidence_paths).Count -eq 0) {
        throw 'W4 evidence record has no evidence paths'
    }
    foreach ($relative in @($Record.evidence_paths)) {
        $value = ([string]$relative).Replace('\', '/')
        if ($value.StartsWith('/') -or $value -match '^[A-Za-z]:' -or
            @($value.Split('/') | Where-Object { $_ -eq '..' }).Count -ne 0) {
            throw "Evidence path is not safe relative path: $value"
        }
        $absolute = Join-Path $RunRoot ($value.Replace('/', '\'))
        if (-not (Test-Path -LiteralPath $absolute -PathType Leaf) -or
            -not (Test-W4PathContainedBy -Candidate $absolute -Root $RunRoot)) {
            throw "Evidence path is missing or escaped run root: $value"
        }
    }
}

try {
    $candidateRoot = Get-CanonicalW4Path -Path $CandidateRoot -Kind Container
    $distributionZip = Get-CanonicalW4Path -Path $DistributionZip -Kind Leaf
    $distributionShaPath = Get-CanonicalW4Path -Path $DistributionSha256 -Kind Leaf
    $provenancePath = Get-CanonicalW4Path -Path $ProvenancePath -Kind Leaf
    $complianceSourcesRoot = Get-CanonicalW4Path -Path $ComplianceSourcesRoot -Kind Container
    $complianceManifestPath = Get-CanonicalW4Path -Path $ComplianceManifestPath -Kind Leaf
    $complianceProvenancePath = Get-CanonicalW4Path -Path $ComplianceProvenancePath -Kind Leaf
    $fixtureRoot = Get-CanonicalW4Path -Path $FixtureRoot -Kind Container
    $evidenceRoot = Get-CanonicalW4Path -Path $EvidenceRoot -Kind Container
    $workspaceRoot = Get-CanonicalW4Path -Path $WorkspaceRoot -Kind Container
    $scenarioContractPath = Get-CanonicalW4Path -Path $ScenarioContract -Kind Leaf
    $harnessRoot = Get-CanonicalW4Path -Path $HarnessRoot -Kind Container
    $controllerRoot = Get-CanonicalW4Path -Path $PSScriptRoot -Kind Container
    foreach ($candidateInput in @($distributionZip, $distributionShaPath, $provenancePath)) {
        if (-not (Test-W4PathContainedBy -Candidate $candidateInput -Root $candidateRoot)) {
            throw 'Candidate ZIP/SHA-256/provenance inputs must be contained by CandidateRoot'
        }
    }
    foreach ($complianceInput in @($complianceManifestPath, $complianceProvenancePath)) {
        if (-not (Test-W4PathContainedBy -Candidate $complianceInput -Root $complianceSourcesRoot)) {
            throw 'Compliance manifest/provenance inputs must be contained by ComplianceSourcesRoot'
        }
    }
    foreach ($pair in @(
        @($evidenceRoot, 'Evidence root', $workspaceRoot, 'Workspace root'),
        @($fixtureRoot, 'Fixture root', $evidenceRoot, 'Evidence root'),
        @($fixtureRoot, 'Fixture root', $workspaceRoot, 'Workspace root'),
        @($harnessRoot, 'Harness root', $evidenceRoot, 'Evidence root'),
        @($harnessRoot, 'Harness root', $workspaceRoot, 'Workspace root'),
        @($candidateRoot, 'Candidate root', $evidenceRoot, 'Evidence root'),
        @($candidateRoot, 'Candidate root', $workspaceRoot, 'Workspace root'),
        @($candidateRoot, 'Candidate root', $fixtureRoot, 'Fixture root'),
        @($candidateRoot, 'Candidate root', $harnessRoot, 'Harness root'),
        @($candidateRoot, 'Candidate root', $controllerRoot, 'Controller root'),
        @($candidateRoot, 'Candidate root', $complianceSourcesRoot, 'Compliance sources root'),
        @($complianceSourcesRoot, 'Compliance sources root', $evidenceRoot, 'Evidence root'),
        @($complianceSourcesRoot, 'Compliance sources root', $workspaceRoot, 'Workspace root'),
        @($complianceSourcesRoot, 'Compliance sources root', $fixtureRoot, 'Fixture root'),
        @($complianceSourcesRoot, 'Compliance sources root', $harnessRoot, 'Harness root'),
        @($complianceSourcesRoot, 'Compliance sources root', $controllerRoot, 'Controller root'),
        @($controllerRoot, 'Controller root', $evidenceRoot, 'Evidence root'),
        @($controllerRoot, 'Controller root', $workspaceRoot, 'Workspace root')
    )) {
        Assert-W4DisjointPaths -First $pair[0] -FirstLabel $pair[1] -Second $pair[2] -SecondLabel $pair[3]
    }
    # Walk all externally supplied trees before reading/importing any child.
    # The module walker rejects nested reparse points and multi-link files.
    foreach ($mutable in @(
        @($evidenceRoot, 'Evidence root'),
        @($workspaceRoot, 'Workspace root')
    )) {
        Assert-W4SafePathChain -Path $mutable[0] -Label $mutable[1]
        [void](Get-W4TreeManifest -Root $mutable[0])
    }
    $controllerManifest = @(Get-W4TreeManifest -Root $controllerRoot)
    $fixtureInputManifest = @(Get-W4TreeManifest -Root $fixtureRoot)
    $harnessInputManifest = @(Get-W4TreeManifest -Root $harnessRoot)
    $candidateInputManifest = @(Get-W4TreeManifest -Root $candidateRoot)
    $complianceInputManifest = @(Get-W4TreeManifest -Root $complianceSourcesRoot)

    $runRoot = Join-Path (Join-Path $evidenceRoot 'runs') $ExecutionId
    $runWorkspace = Join-Path (Join-Path $workspaceRoot 'runs') $ExecutionId
    foreach ($candidate in @(
        @($runRoot, $evidenceRoot, 'Execution evidence root'),
        @($runWorkspace, $workspaceRoot, 'Execution workspace root')
    )) {
        if (-not (Test-W4PathContainedBy -Candidate $candidate[0] -Root $candidate[1])) {
            throw "$($candidate[2]) escaped its mutable authority"
        }
        if (Test-Path -LiteralPath $candidate[0]) {
            throw "Execution path already exists; evidence is immutable: $($candidate[0])"
        }
        [void][System.IO.Directory]::CreateDirectory($candidate[0])
        Assert-W4SafePathChain -Path $candidate[0] -Label $candidate[2]
        [void](Get-W4TreeManifest -Root $candidate[0])
    }

    $contract = Read-W4JsonFile -Path $scenarioContractPath
    Test-W4ScenarioContract -Contract $contract
    $fixtureManifest = Read-W4JsonFile -Path (Join-Path $fixtureRoot 'fixture-manifest.v1.json')
    Assert-W4ExactBoolean -Value $fixtureManifest.synthetic_only -Expected $true `
        -Label 'fixture manifest synthetic_only'
    Assert-W4ExactBoolean -Value $fixtureManifest.contains_credentials -Expected $false `
        -Label 'fixture manifest contains_credentials'
    Assert-W4ExactBoolean -Value $fixtureManifest.contains_real_vault_data -Expected $false `
        -Label 'fixture manifest contains_real_vault_data'
    if ([string]$fixtureManifest.schema_version -ne 'pkv.m13.w4-fixtures.v1' -or
        -not [bool]$fixtureManifest.synthetic_only -or
        [bool]$fixtureManifest.contains_credentials -or
        [bool]$fixtureManifest.contains_real_vault_data) {
        throw 'W4 fixture root is not the frozen synthetic-only fixture bundle'
    }
    $harnessManifest = Read-W4JsonFile -Path (Join-Path $harnessRoot 'manifest.json')
    Assert-W4ExactFields -Object $harnessManifest -Fields @(
        'schema_version', 'contract_id', 'harness_version', 'distribution',
        'release_payload_membership', 'runtime', 'contract', 'scripts', 'build'
    ) -Label 'W4 harness manifest'
    Assert-W4ExactFields -Object $harnessManifest.build -Fields @(
        'source_revision', 'build_fingerprint_sha256', 'toolchain_lock_sha256'
    ) -Label 'W4 harness build identity'
    if ([string]$harnessManifest.schema_version -cne 'pkv.w3.loopback.manifest.v1' -or
        [string]$harnessManifest.contract_id -cne 'w3.openai_compatible_loopback.v1' -or
        [string]$harnessManifest.harness_version -cne '1.0.0' -or
        [string]$harnessManifest.distribution -cne 'e2e-only' -or
        [string]$harnessManifest.release_payload_membership -cne 'forbidden') {
        throw 'W4 harness manifest identity/distribution contract is invalid'
    }

    $artifactFileName = [System.IO.Path]::GetFileName($distributionZip)
    $artifactId = [System.IO.Path]::GetFileNameWithoutExtension($distributionZip)
    $expectedArtifactId = "PersonalKnowledgeVault-$($contract.artifact_version)-windows-x86_64"
    if ($artifactId -ne $expectedArtifactId) {
        throw "Artifact filename/root identity is unexpected: $artifactId"
    }
    $expectedCandidateNames = @(
        "$expectedArtifactId.zip",
        "$expectedArtifactId.zip.sha256",
        "$expectedArtifactId.provenance.json"
    ) | Sort-Object
    $actualCandidateItems = @(Get-ChildItem -LiteralPath $candidateRoot -Force)
    $actualCandidateNames = @($actualCandidateItems | ForEach-Object { $_.Name } | Sort-Object)
    if (@($actualCandidateItems | Where-Object { $_.PSIsContainer }).Count -ne 0 -or
        ($actualCandidateNames | ConvertTo-Json -Compress) -cne
            ($expectedCandidateNames | ConvertTo-Json -Compress) -or
        $distributionZip -cne (Join-Path $candidateRoot "$expectedArtifactId.zip") -or
        $distributionShaPath -cne (Join-Path $candidateRoot "$expectedArtifactId.zip.sha256") -or
        $provenancePath -cne (Join-Path $candidateRoot "$expectedArtifactId.provenance.json")) {
        throw 'CandidateRoot must contain exactly the canonical ZIP/SHA-256/provenance files'
    }
    $artifactSha = Get-W4FileSha256 -Path $distributionZip
    $shaText = [System.IO.File]::ReadAllText($distributionShaPath, [System.Text.Encoding]::ASCII).Trim()
    if ($shaText -notmatch '^([0-9a-f]{64})  ([^\\/]+\.zip)$' -or
        $Matches[1] -ne $artifactSha -or $Matches[2] -ne $artifactFileName) {
        throw 'ZIP .sha256 sidecar does not exactly bind the supplied Artifact filename/hash'
    }
    $provenance = Read-W4JsonFile -Path $provenancePath
    $provenanceFields = @(
        'schema_version', 'artifact_file', 'artifact_kind', 'artifact_status',
        'artifact_sha256', 'artifact_size',
        'build_info_path', 'build_info_sha256', 'build_fingerprint',
        'compliance_manifest_sha256', 'compliance_sources',
        'conda_hardlink_threat_evidence',
        'payload_manifest_path', 'payload_manifest_sha256', 'sbom_path',
        'sbom_sha256', 'source_revision', 'release_blockers',
        'release_eligible', 'release_blocker_authority',
        'release_blocker_authority_sha256',
        'release_inventory_artifact_closure_sha256',
        'release_inventory_closure_sha256', 'release_inventory_path',
        'release_inventory_sha256', 'version'
    )
    Assert-W4ExactFields -Object $provenance -Fields $provenanceFields -Label 'Artifact provenance'
    Assert-W4ExactBoolean -Value $provenance.release_eligible -Expected $false `
        -Label 'Artifact provenance release_eligible'
    if ([string]$provenance.schema_version -ne 'pkv.artifact-provenance.v1' -or
        [string]$provenance.artifact_file -ne $artifactFileName -or
        [string]$provenance.artifact_kind -ne 'test_candidate' -or
        [string]$provenance.artifact_status -ne 'test-candidate-on-compliance-hold' -or
        [string]$provenance.artifact_sha256 -ne $artifactSha -or
        [int64]$provenance.artifact_size -ne (Get-Item -LiteralPath $distributionZip).Length -or
        [string]$provenance.version -ne [string]$contract.artifact_version -or
        [bool]$provenance.release_eligible -or
        (@($provenance.release_blockers) | ConvertTo-Json -Compress) -cne
            (@(
                'conda-native-license-materials-and-spdx',
                'html2text-gpl-compliance',
                'native-msvc-license-and-provenance'
            ) | ConvertTo-Json -Compress)) {
        throw 'Artifact provenance does not bind the supplied ZIP identity/version/size'
    }
    if ([string]$provenance.source_revision -notmatch '^[0-9a-f]{40}$' -or
        [string]$provenance.build_fingerprint -notmatch '^[0-9a-f]{64}$' -or
        [string]$provenance.compliance_manifest_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$provenance.release_blocker_authority_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$provenance.payload_manifest_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$provenance.release_inventory_artifact_closure_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$provenance.release_inventory_closure_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$provenance.release_inventory_sha256 -notmatch '^[0-9a-f]{64}$') {
        throw 'Artifact provenance contains invalid revision/fingerprint/manifest identity'
    }

    $expectedComplianceNames = @(
        'html2text-2020.1.16.tar.gz',
        'html2text-2020.1.16.tar.gz.sha256',
        'manifest.json',
        'provenance.json'
    ) | Sort-Object
    $actualComplianceItems = @(Get-ChildItem -LiteralPath $complianceSourcesRoot -Force)
    $actualComplianceNames = @($actualComplianceItems | ForEach-Object { $_.Name } | Sort-Object)
    if (@($actualComplianceItems | Where-Object { $_.PSIsContainer }).Count -ne 0 -or
        ($actualComplianceNames | ConvertTo-Json -Compress) -cne
            ($expectedComplianceNames | ConvertTo-Json -Compress) -or
        $complianceManifestPath -cne (Join-Path $complianceSourcesRoot 'manifest.json') -or
        $complianceProvenancePath -cne (Join-Path $complianceSourcesRoot 'provenance.json')) {
        throw 'ComplianceSourcesRoot must contain exactly the canonical source/SHA-256/manifest/provenance files'
    }
    $complianceSourceName = 'html2text-2020.1.16.tar.gz'
    $complianceSourcePath = Join-Path $complianceSourcesRoot $complianceSourceName
    $complianceSourceSidecarPath = Join-Path $complianceSourcesRoot "$complianceSourceName.sha256"
    $complianceSourceSha = Get-W4FileSha256 -Path $complianceSourcePath
    $complianceSourceSize = [int64](Get-Item -LiteralPath $complianceSourcePath).Length
    $complianceSidecarText = [System.IO.File]::ReadAllText(
        $complianceSourceSidecarPath,
        [System.Text.Encoding]::ASCII
    )
    if ($complianceSourceSha -cne
            'e296318e16b059ddb97f7a8a1d6a5c1d7af4544049a01e261731d2d5cc277bbb' -or
        $complianceSourceSize -ne 49464 -or
        $complianceSidecarText -cne "$complianceSourceSha  $complianceSourceName`n") {
        throw 'Compliance corresponding-source file/SHA-256 sidecar is invalid'
    }
    Assert-W4ExactFields -Object $provenance.compliance_sources -Fields @(
        'manifest_path', 'manifest_sha256', 'provenance_path', 'provenance_sha256',
        'root', 'source_file', 'source_sha256', 'source_size'
    ) -Label 'Artifact provenance compliance_sources'
    $complianceManifestSha = Get-W4FileSha256 -Path $complianceManifestPath
    $complianceProvenanceSha = Get-W4FileSha256 -Path $complianceProvenancePath
    if ([string]$provenance.compliance_sources.root -cne '../compliance-sources' -or
        [string]$provenance.compliance_sources.manifest_path -cne
            '../compliance-sources/manifest.json' -or
        [string]$provenance.compliance_sources.manifest_sha256 -cne $complianceManifestSha -or
        [string]$provenance.compliance_sources.provenance_path -cne
            '../compliance-sources/provenance.json' -or
        [string]$provenance.compliance_sources.provenance_sha256 -cne $complianceProvenanceSha -or
        [string]$provenance.compliance_sources.source_file -cne $complianceSourceName -or
        [string]$provenance.compliance_sources.source_sha256 -cne $complianceSourceSha -or
        [int64]$provenance.compliance_sources.source_size -ne $complianceSourceSize) {
        throw 'Artifact provenance does not bind the supplied compliance-source bundle'
    }
    $complianceManifest = Read-W4JsonFile -Path $complianceManifestPath
    Assert-W4ExactFields -Object $complianceManifest -Fields @(
        'schema_version', 'artifact_kind', 'build_fingerprint',
        'compliance_manifest_sha256', 'files', 'release_blockers',
        'release_eligible', 'release_blocker_authority',
        'release_blocker_authority_sha256', 'source_revision'
    ) -Label 'Compliance source manifest'
    Assert-W4ExactBoolean -Value $complianceManifest.release_eligible -Expected $false `
        -Label 'Compliance source manifest release_eligible'
    $complianceFiles = @($complianceManifest.files)
    if ($complianceFiles.Count -ne 1) {
        throw 'Compliance source manifest must bind exactly one source file'
    }
    Assert-W4ExactFields -Object $complianceFiles[0] -Fields @(
        'component', 'license_expression_assessment', 'license_expression_status',
        'path', 'sha256', 'size', 'version'
    ) -Label 'Compliance source manifest file row'
    if ([string]$complianceManifest.schema_version -cne 'pkv.compliance-source-bundle.v1' -or
        [string]$complianceManifest.artifact_kind -cne 'corresponding_source_bundle' -or
        [string]$complianceManifest.build_fingerprint -cne [string]$provenance.build_fingerprint -or
        [string]$complianceManifest.compliance_manifest_sha256 -cne
            [string]$provenance.compliance_manifest_sha256 -or
        [bool]$complianceManifest.release_eligible -or
        (@($complianceManifest.release_blockers) | ConvertTo-Json -Compress) -cne
            (@($provenance.release_blockers) | ConvertTo-Json -Compress) -or
        [string]$complianceManifest.release_blocker_authority_sha256 -cne
            [string]$provenance.release_blocker_authority_sha256 -or
        (@($complianceManifest.release_blocker_authority) | ConvertTo-Json -Depth 20 -Compress) -cne
            (@($provenance.release_blocker_authority) | ConvertTo-Json -Depth 20 -Compress) -or
        [string]$complianceManifest.source_revision -cne [string]$provenance.source_revision -or
        [string]$complianceFiles[0].component -cne 'html2text' -or
        [string]$complianceFiles[0].license_expression_assessment -cne 'GPL-3.0-only' -or
        [string]$complianceFiles[0].license_expression_status -cne
            'requires_legal_confirmation' -or
        [string]$complianceFiles[0].path -cne $complianceSourceName -or
        [string]$complianceFiles[0].sha256 -cne $complianceSourceSha -or
        [int64]$complianceFiles[0].size -ne $complianceSourceSize -or
        [string]$complianceFiles[0].version -cne '2020.1.16') {
        throw 'Compliance source manifest identity/status/file binding is invalid'
    }
    $complianceProvenance = Read-W4JsonFile -Path $complianceProvenancePath
    Assert-W4ExactFields -Object $complianceProvenance -Fields @(
        'schema_version', 'artifact_kind', 'build_fingerprint',
        'compliance_manifest_sha256', 'manifest_sha256', 'release_blockers',
        'release_eligible', 'release_blocker_authority',
        'release_blocker_authority_sha256', 'source_file', 'source_sha256',
        'source_revision'
    ) -Label 'Compliance source provenance'
    Assert-W4ExactBoolean -Value $complianceProvenance.release_eligible -Expected $false `
        -Label 'Compliance source provenance release_eligible'
    if ([string]$complianceProvenance.schema_version -cne
            'pkv.compliance-source-provenance.v1' -or
        [string]$complianceProvenance.artifact_kind -cne 'corresponding_source_bundle' -or
        [string]$complianceProvenance.build_fingerprint -cne [string]$provenance.build_fingerprint -or
        [string]$complianceProvenance.compliance_manifest_sha256 -cne
            [string]$provenance.compliance_manifest_sha256 -or
        [string]$complianceProvenance.manifest_sha256 -cne $complianceManifestSha -or
        [bool]$complianceProvenance.release_eligible -or
        (@($complianceProvenance.release_blockers) | ConvertTo-Json -Compress) -cne
            (@($provenance.release_blockers) | ConvertTo-Json -Compress) -or
        [string]$complianceProvenance.release_blocker_authority_sha256 -cne
            [string]$provenance.release_blocker_authority_sha256 -or
        (@($complianceProvenance.release_blocker_authority) |
            ConvertTo-Json -Depth 20 -Compress) -cne
            (@($provenance.release_blocker_authority) | ConvertTo-Json -Depth 20 -Compress) -or
        [string]$complianceProvenance.source_file -cne $complianceSourceName -or
        [string]$complianceProvenance.source_sha256 -cne $complianceSourceSha -or
        [string]$complianceProvenance.source_revision -cne [string]$provenance.source_revision) {
        throw 'Compliance source provenance does not exactly bind the candidate build/source bundle'
    }

    Invoke-W4DiskPreflight -ZipPath $distributionZip -WorkspacePath $runWorkspace `
        -EvidencePath (Join-Path $runRoot 'disk-preflight.json')
    $extractRoot = Join-Path $runWorkspace 'extracted'
    $artifactRoot = Expand-W4ZipSafely -ZipPath $distributionZip `
        -Destination $extractRoot -ExpectedRootName $artifactId
    $buildInfoPath = Join-Path $artifactRoot 'build-info.json'
    $payloadManifestPath = Join-Path $artifactRoot 'payload-manifest.json'
    $releaseInventoryPath = Join-Path $artifactRoot 'release-inventory.json'
    $sbomPath = Join-Path $artifactRoot 'sbom.cdx.json'
    foreach ($path in @($buildInfoPath, $payloadManifestPath, $releaseInventoryPath, $sbomPath)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Extracted Artifact is missing provenance-bound file: $path"
        }
    }
    if ((Get-W4FileSha256 -Path $buildInfoPath) -ne [string]$provenance.build_info_sha256 -or
        (Get-W4FileSha256 -Path $payloadManifestPath) -ne [string]$provenance.payload_manifest_sha256 -or
        (Get-W4FileSha256 -Path $releaseInventoryPath) -ne [string]$provenance.release_inventory_sha256 -or
        (Get-W4FileSha256 -Path $sbomPath) -ne [string]$provenance.sbom_sha256 -or
        [string]$provenance.build_info_path -ne "$artifactId/build-info.json" -or
        [string]$provenance.payload_manifest_path -ne "$artifactId/payload-manifest.json" -or
        [string]$provenance.release_inventory_path -ne "$artifactId/release-inventory.json" -or
        [string]$provenance.sbom_path -ne "$artifactId/sbom.cdx.json") {
        throw 'Artifact provenance path/hash cross-check failed after extraction'
    }
    $buildInfo = Read-W4JsonFile -Path $buildInfoPath
    $payloadManifest = Read-W4JsonFile -Path $payloadManifestPath
    $releaseInventory = Read-W4JsonFile -Path $releaseInventoryPath
    Assert-W4ExactBoolean -Value $buildInfo.source_tree_clean -Expected $true `
        -Label 'build-info source_tree_clean'
    Assert-W4ExactBoolean -Value $buildInfo.release_eligible -Expected $false `
        -Label 'build-info release_eligible'
    if ([string]$buildInfo.source_revision -ne [string]$provenance.source_revision -or
        [string]$buildInfo.build_fingerprint -ne [string]$provenance.build_fingerprint -or
        [string]$buildInfo.artifact_kind -ne [string]$provenance.artifact_kind -or
        [string]$buildInfo.artifact_status -ne [string]$provenance.artifact_status -or
        [string]$buildInfo.compliance_manifest_sha256 -ne
            [string]$provenance.compliance_manifest_sha256 -or
        [bool]$buildInfo.release_eligible -ne [bool]$provenance.release_eligible -or
        (@($buildInfo.release_blockers) | ConvertTo-Json -Compress) -cne
            (@($provenance.release_blockers) | ConvertTo-Json -Compress) -or
        [string]$buildInfo.release_inventory_artifact_closure_sha256 -cne
            [string]$provenance.release_inventory_artifact_closure_sha256 -or
        [string]$buildInfo.release_inventory_closure_sha256 -cne
            [string]$provenance.release_inventory_closure_sha256 -or
        [string]$buildInfo.release_inventory_path -cne 'release-inventory.json' -or
        [string]$buildInfo.release_inventory_sha256 -cne
            [string]$provenance.release_inventory_sha256 -or
        [string]$payloadManifest.build_fingerprint -ne [string]$provenance.build_fingerprint) {
        throw 'Extracted build/payload identity disagrees with provenance'
    }
    if ([string]$harnessManifest.build.source_revision -cne [string]$provenance.source_revision -or
        [string]$harnessManifest.build.build_fingerprint_sha256 -cne
            [string]$provenance.build_fingerprint -or
        [string]$harnessManifest.build.toolchain_lock_sha256 -cnotmatch '^[0-9a-f]{64}$') {
        throw 'W4 harness manifest does not share the candidate build/revision identity'
    }

    $powerShellHost = (Get-Process -Id $PID -ErrorAction Stop).Path
    $environmentRecord = [ordered]@{
        schema_version = 'pkv.m13.w4-environment.v1'
        os_version = [Environment]::OSVersion.VersionString
        os_architecture = [Environment]::GetEnvironmentVariable('PROCESSOR_ARCHITECTURE')
        powershell_version = $PSVersionTable.PSVersion.ToString()
        powershell_edition = if ($PSVersionTable.PSObject.Properties.Name -contains 'PSEdition') {
            [string]$PSVersionTable.PSEdition
        } else {
            'Desktop'
        }
        python_required = $false
        conda_required = $false
        desktop_uia_required = $false
    }
    $environmentPath = Join-Path $runRoot 'environment.json'
    Write-W4JsonFile -Path $environmentPath -Value $environmentRecord
    $environmentFingerprint = Get-W4FileSha256 -Path $environmentPath
    $fixtureSha = Get-W4StringSha256 -Value ($fixtureInputManifest | ConvertTo-Json -Depth 5 -Compress)
    $controllerSha = Get-W4StringSha256 -Value ($controllerManifest | ConvertTo-Json -Depth 5 -Compress)
    $isolation = [ordered]@{
        schema_version = 'pkv.m13.w4-source-isolation.v1'
        execution_id = $ExecutionId
        controller_root = $controllerRoot
        controller_sha256 = $controllerSha
        controller_files = $controllerManifest
        controller_language = 'powershell-dotnet-system-assemblies-only'
        imports_product_source = $false
        working_root = $runWorkspace
        evidence_root = $runRoot
        artifact_root = $artifactRoot
        fixture_root = $fixtureRoot
        harness_root = $harnessRoot
        harness_tree_sha256 = (Get-W4StringSha256 `
            -Value ($harnessInputManifest | ConvertTo-Json -Depth 5 -Compress))
        candidate_root = $candidateRoot
        candidate_tree_sha256 = (Get-W4StringSha256 `
            -Value ($candidateInputManifest | ConvertTo-Json -Depth 5 -Compress))
        compliance_sources_root = $complianceSourcesRoot
        compliance_sources_tree_sha256 = (Get-W4StringSha256 `
            -Value ($complianceInputManifest | ConvertTo-Json -Depth 5 -Compress))
        python_path_injected = $false
        python_runtime_required = $false
        conda_runtime_required = $false
    }
    $isolationPath = Join-Path $runRoot 'source-isolation.json'
    Write-W4JsonFile -Path $isolationPath -Value $isolation
    $isolationSha = Get-W4FileSha256 -Path $isolationPath

    $runContext = [pscustomobject]@{
        Contract = $contract
        ArtifactRoot = $artifactRoot
        ArtifactId = $artifactId
        ArtifactSha256 = $artifactSha
        DistributionZip = $distributionZip
        Provenance = $provenance
        ComplianceSourcesRoot = $complianceSourcesRoot
        ComplianceManifest = $complianceManifest
        ComplianceProvenance = $complianceProvenance
        BuildInfo = $buildInfo
        PayloadManifest = $payloadManifest
        ReleaseInventory = $releaseInventory
        HarnessManifest = $harnessManifest
        FixtureRoot = $fixtureRoot
        FixtureSha256 = $fixtureSha
        HarnessRoot = $harnessRoot
        EvidenceRoot = $evidenceRoot
        RunRoot = $runRoot
        WorkspaceRoot = $runWorkspace
        PowerShellHost = $powerShellHost
        ExecutionId = $ExecutionId
        EnvironmentFingerprint = $environmentFingerprint
        SourceIsolationSha256 = $isolationSha
        ControllerSha256 = $controllerSha
    }

    $records = [System.Collections.Generic.List[object]]::new()
    $scenarioSummaries = [System.Collections.Generic.List[object]]::new()
    $stopForUnsafeArtifact = $false
    foreach ($scenario in @($contract.ordered_scenarios)) {
        if ($stopForUnsafeArtifact) {
            $scenarioSummaries.Add([ordered]@{
                scenario_id = [string]$scenario.scenario_id
                state = 'artifact_pending'
                oracle_result = $null
                reason = 'not executed because release_audit rejected the Artifact'
            })
            continue
        }
        $scenarioResult = $null
        $errorText = $null
        $startedAt = [DateTime]::UtcNow
        try {
            $scenarioResult = Invoke-W4Scenario -RunContext $runContext -Scenario $scenario
            $state = 'artifact_verified'
            $oracleResult = 'passed'
        } catch {
            $state = 'artifact_failed'
            $oracleResult = 'failed'
            $errorText = $_.Exception.Message
            $slug = ([string]$scenario.scenario_id -replace '[^A-Za-z0-9._-]', '_')
            $scenarioEvidence = Join-Path (Join-Path $runRoot 'scenarios') $slug
            [void][System.IO.Directory]::CreateDirectory($scenarioEvidence)
            Write-W4JsonFile -Path (Join-Path $scenarioEvidence 'failure.json') -Value ([ordered]@{
                error_type = $_.Exception.GetType().FullName
                message = $_.Exception.Message
                script_stack_trace = [string]$_.ScriptStackTrace
            })
            $scenarioResult = [pscustomobject]@{
                Context = [pscustomobject]@{ Evidence = $scenarioEvidence }
                Oracle = $null
            }
            if ([string]$scenario.scenario_id -eq 'w4.release_audit.v1') {
                $stopForUnsafeArtifact = $true
            }
        }

        $evidenceManifest = Get-W4EvidenceManifest `
            -ScenarioEvidenceRoot $scenarioResult.Context.Evidence `
            -ScenarioId ([string]$scenario.scenario_id)
        $evidenceManifestPath = Join-Path $scenarioResult.Context.Evidence 'evidence-manifest.json'
        Write-W4JsonFile -Path $evidenceManifestPath -Value $evidenceManifest
        $evidenceManifestSha = Get-W4FileSha256 -Path $evidenceManifestPath
        $evidencePaths = @(
            Get-ChildItem -LiteralPath $scenarioResult.Context.Evidence -File -Recurse -Force |
                Sort-Object FullName |
                ForEach-Object {
                    $_.FullName.Substring($runRoot.TrimEnd('\').Length).TrimStart('\').Replace('\', '/')
                }
        )
        $harnessSha = if ([bool]$scenario.requires_harness) {
            Get-W4FileSha256 -Path (Join-Path $harnessRoot 'pkv-loopback-provider.exe')
        } else {
            $null
        }
        $record = [ordered]@{
            scenario_id = [string]$scenario.scenario_id
            state = $state
            producer_lane = 'artifact-only'
            artifact_id = $artifactId
            artifact_sha256 = $artifactSha
            normalized_manifest_sha256 = [string]$provenance.payload_manifest_sha256
            build_fingerprint = [string]$provenance.build_fingerprint
            source_revision = [string]$provenance.source_revision
            runner_version = [string]$contract.runner_version
            execution_id = $ExecutionId
            executed_at = [DateTime]::UtcNow.ToString('o')
            environment_fingerprint = $environmentFingerprint
            fixture_sha256 = $fixtureSha
            harness_sha256 = $harnessSha
            evidence_manifest_sha256 = $evidenceManifestSha
            source_isolation_proof_sha256 = $isolationSha
            oracle_result = $oracleResult
            evidence_paths = $evidencePaths
        }
        Test-W4EvidenceRecord -Record ([pscustomobject]$record) -RunRoot $runRoot `
            -HarnessRequired ([bool]$scenario.requires_harness)
        Write-W4JsonFile -Path (Join-Path $scenarioResult.Context.Evidence 'evidence-record.json') -Value $record
        $records.Add($record)
        $scenarioSummaries.Add([ordered]@{
            scenario_id = [string]$scenario.scenario_id
            state = $state
            oracle_result = $oracleResult
            duration_ms = [int64]([DateTime]::UtcNow - $startedAt).TotalMilliseconds
            error = $errorText
        })
    }

    # UI Automation evidence is owned by the separately versioned pkv-GUI repository.

    $controllerAfter = @(Get-W4TreeManifest -Root $controllerRoot)
    $fixtureAfter = @(Get-W4TreeManifest -Root $fixtureRoot)
    $harnessAfter = @(Get-W4TreeManifest -Root $harnessRoot)
    $candidateAfter = @(Get-W4TreeManifest -Root $candidateRoot)
    $complianceAfter = @(Get-W4TreeManifest -Root $complianceSourcesRoot)
    if ((Get-W4StringSha256 -Value ($controllerAfter | ConvertTo-Json -Depth 5 -Compress)) -ne $controllerSha -or
        (Get-W4StringSha256 -Value ($fixtureAfter | ConvertTo-Json -Depth 5 -Compress)) -ne $fixtureSha -or
        (Get-W4StringSha256 -Value ($harnessAfter | ConvertTo-Json -Depth 5 -Compress)) -ne
            [string]$isolation.harness_tree_sha256 -or
        (Get-W4StringSha256 -Value ($candidateAfter | ConvertTo-Json -Depth 5 -Compress)) -ne
            [string]$isolation.candidate_tree_sha256 -or
        (Get-W4StringSha256 -Value ($complianceAfter | ConvertTo-Json -Depth 5 -Compress)) -ne
            [string]$isolation.compliance_sources_tree_sha256) {
        throw 'Controller, fixture, harness, candidate, or compliance input tree changed during W4 execution'
    }

    $expectedScenarioCount = @($contract.ordered_scenarios).Count
    $verified = @($records | Where-Object { [string]$_.state -eq 'artifact_verified' }).Count
    $failed = @($records | Where-Object { [string]$_.state -eq 'artifact_failed' }).Count
    $pending = $expectedScenarioCount - $verified - $failed
    $functionalVerified = (
        $verified -eq $expectedScenarioCount -and $failed -eq 0 -and $pending -eq 0
    )
    $decision = if ($functionalVerified -and [bool]$provenance.release_eligible -and
        @($provenance.release_blockers).Count -eq 0) {
        'release'
    } else {
        'hold'
    }
    $summary = [ordered]@{
        schema_version = 'pkv.m13.w4-run-summary.v1'
        runner_version = [string]$contract.runner_version
        execution_id = $ExecutionId
        artifact_id = $artifactId
        artifact_sha256 = $artifactSha
        source_revision = [string]$provenance.source_revision
        build_fingerprint = [string]$provenance.build_fingerprint
        artifact_kind = [string]$provenance.artifact_kind
        artifact_status = [string]$provenance.artifact_status
        compliance_manifest_sha256 = [string]$provenance.compliance_manifest_sha256
        release_inventory_artifact_closure_sha256 = [string]$provenance.release_inventory_artifact_closure_sha256
        release_inventory_closure_sha256 = [string]$provenance.release_inventory_closure_sha256
        release_inventory_sha256 = [string]$provenance.release_inventory_sha256
        release_eligible = [bool]$provenance.release_eligible
        release_blockers = @($provenance.release_blockers)
        controller_sha256 = $controllerSha
        fixture_sha256 = $fixtureSha
        scenarios_total = $expectedScenarioCount
        matrix_rows_total = @($contract.required_matrix_rows).Count
        artifact_verified = $verified
        artifact_failed = $failed
        artifact_pending = $pending
        functional_verified = $functionalVerified
        scenarios = @($scenarioSummaries)
        decision = $decision
    }
    Write-W4JsonFile -Path (Join-Path $runRoot 'w4-evidence-registry.json') -Value ([ordered]@{
        schema_version = 'pkv.m13.w4-run-evidence.v1'
        execution_id = $ExecutionId
        records = @($records)
    })
    Write-W4JsonFile -Path (Join-Path $runRoot 'w4-run-summary.json') -Value $summary
    $runEntries = @(Get-W4TreeManifest -Root $runRoot `
        -ExcludedRelativePrefixes @('run-evidence-manifest.json'))
    $runManifest = [ordered]@{
        schema_version = 'pkv.m13.w4-run-evidence-manifest.v1'
        execution_id = $ExecutionId
        entries = $runEntries
        tree_sha256 = Get-W4StringSha256 -Value ($runEntries | ConvertTo-Json -Depth 5 -Compress)
    }
    Write-W4JsonFile -Path (Join-Path $runRoot 'run-evidence-manifest.json') -Value $runManifest
    [Console]::Out.WriteLine(($summary | ConvertTo-Json -Depth 30 -Compress))
    if (-not $functionalVerified) {
        exit 1
    }
    exit 0
} catch {
    [Console]::Error.WriteLine("W4 Artifact E2E failed closed: $($_.Exception.Message)")
    exit 1
}
