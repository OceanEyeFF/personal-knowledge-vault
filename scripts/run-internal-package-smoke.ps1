<#
.SYNOPSIS
Run the non-release PKV internal package smoke checks from outside the checkout.

.DESCRIPTION
This script is intentionally limited to the P1-A internal self-test lane.  It
unpacks the ZIP created below dist\internal into a fresh directory outside the
repository, retains the caller-provided .data-test runtime root, and checks:

  * pkv --help
  * a synthetic-data BM25 search
  * MCP stdio initialize

It must be launched through scripts/run-test.ps1 -Direct so the DATA_DIR and
related runtime paths are an isolated .data-test scenario.  It never accepts a
release candidate and does not make a release decision.
#>

#requires -Version 5.1

[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$PackageRoot,

    [Parameter()]
    [string]$PackageZip,

    [Parameter()]
    [string]$WorkspaceRoot,

    [Parameter()]
    [switch]$KeepWorkspace,

    [Parameter()]
    [ValidateRange(5, 120)]
    [int]$CommandTimeoutSeconds = 30

)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
$ProjectRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $PSScriptRoot '..')
)
$InternalOutputRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $ProjectRoot 'dist\internal')
)
$CreatedWorkspace = $false
$ResolvedWorkspace = $null
. (Join-Path $PSScriptRoot 'internal-package-workspace.ps1')

function Get-CanonicalExistingPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][ValidateSet('Leaf', 'Container')][string]$Kind,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "$Label is missing: $Path"
    }
    if ($Kind -eq 'Leaf' -and -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label is not a file: $Path"
    }
    if ($Kind -eq 'Container' -and -not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "$Label is not a directory: $Path"
    }
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label must not be a symlink or junction: $Path"
    }
    return [System.IO.Path]::GetFullPath(
        (Resolve-Path -LiteralPath $Path -ErrorAction Stop).ProviderPath
    )
}

function Test-PathContainedBy {
    param(
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$Root
    )

    $trimChars = [char[]]@(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $candidatePath = ([System.IO.Path]::GetFullPath($Candidate)).TrimEnd($trimChars)
    $rootPath = ([System.IO.Path]::GetFullPath($Root)).TrimEnd($trimChars)
    if ($candidatePath.Equals($rootPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    return $candidatePath.StartsWith(
        $rootPath + [System.IO.Path]::DirectorySeparatorChar,
        [System.StringComparison]::OrdinalIgnoreCase
    )
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

function Assert-IsolatedRuntimeEnvironment {
    $required = @(
        'DATA_DIR', 'DB_PATH', 'VAULT_DIR', 'VECTOR_DIR', 'LOG_DIR', 'TMP_DIR'
    )
    foreach ($key in $required) {
        if ([string]::IsNullOrWhiteSpace(
            [Environment]::GetEnvironmentVariable($key, 'Process')
        )) {
            throw "internal smoke must run through scripts/run-test.ps1; $key is absent"
        }
    }
    if ($env:PKV_TEST_OFFLINE -ne '1' -or $env:PKV_TEST_LOAD_LOCAL -ne '0') {
        throw 'internal smoke requires the run-test offline/base-only environment'
    }

    $testRoot = Get-CanonicalExistingPath -Path (Join-Path $ProjectRoot '.data-test') `
        -Kind Container -Label '.data-test root'
    $dataRoot = Get-CanonicalExistingPath -Path $env:DATA_DIR -Kind Container `
        -Label 'DATA_DIR'
    if (-not (Test-PathContainedBy -Candidate $dataRoot -Root $testRoot) -or
        $dataRoot -eq $testRoot) {
        throw "DATA_DIR must be a dedicated .data-test child: $dataRoot"
    }
    foreach ($key in $required[1..($required.Count - 1)]) {
        $runtimePath = [System.IO.Path]::GetFullPath(
            [Environment]::GetEnvironmentVariable($key, 'Process')
        )
        if (-not (Test-PathContainedBy -Candidate $runtimePath -Root $dataRoot)) {
            throw "$key escaped DATA_DIR: $runtimePath"
        }
    }
    if (-not (Test-Path -LiteralPath $env:DB_PATH -PathType Leaf)) {
        throw "synthetic database is missing: $($env:DB_PATH). Run setup-test-db.py first."
    }
    return $dataRoot
}

function Expand-InternalPackageZipSafely {
    param(
        [Parameter(Mandatory = $true)][string]$ZipPath,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$ExpectedRootName
    )

    Add-Type -AssemblyName System.IO.Compression -ErrorAction Stop
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop
    [void][System.IO.Directory]::CreateDirectory($Destination)
    $archive = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    $seen = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    try {
        foreach ($entry in $archive.Entries) {
            $name = ([string]$entry.FullName).Replace('\', '/')
            if ([string]::IsNullOrWhiteSpace($name) -or $name.StartsWith('/') -or
                $name -match '^[A-Za-z]:' -or $name.IndexOf([char]0) -ge 0) {
                throw "internal package ZIP has an unsafe entry: $name"
            }
            $segments = @($name.Split('/') | Where-Object { $_ -ne '' })
            if ($segments.Count -lt 2 -or $segments[0] -cne $ExpectedRootName -or
                @($segments | Where-Object { $_ -eq '.' -or $_ -eq '..' }).Count -ne 0) {
                throw "internal package ZIP violates the single-root contract: $name"
            }
            $key = $name.TrimEnd('/').ToLowerInvariant()
            if (-not $seen.Add($key)) {
                throw "internal package ZIP has a duplicate path: $name"
            }
            $relative = ($segments -join '\')
            $target = [System.IO.Path]::GetFullPath((Join-Path $Destination $relative))
            if (-not (Test-PathContainedBy -Candidate $target -Root $Destination)) {
                throw "internal package ZIP escaped the workspace: $name"
            }
            $parent = [System.IO.Path]::GetDirectoryName($target)
            [void][System.IO.Directory]::CreateDirectory($parent)
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
    $root = Join-Path $Destination $ExpectedRootName
    if (-not (Test-Path -LiteralPath $root -PathType Container)) {
        throw 'internal package ZIP did not produce its expected root directory'
    }
    return $root
}

function Get-InternalSmokeProfileRoot {
    param([Parameter(Mandatory = $true)][string]$DataRoot)

    return Join-Path $DataRoot 'tmp\internal-package-home'
}

function Get-InternalSmokeInjectedProfileRoot {
    param([Parameter(Mandatory = $true)][string]$DataRoot)

    # RuntimeLayout deliberately derives this sibling profile whenever the
    # offline test seam supplies DATA_DIR.  Keep the normal USERPROFILE clean
    # for the child process, but write the synthetic product Config where that
    # explicit test seam actually resolves it.
    $canonicalDataRoot = [System.IO.Path]::GetFullPath($DataRoot)
    $parent = Split-Path -Path $canonicalDataRoot -Parent
    $leaf = Split-Path -Path $canonicalDataRoot -Leaf
    if ([string]::IsNullOrWhiteSpace($parent) -or [string]::IsNullOrWhiteSpace($leaf)) {
        throw "cannot derive the offline synthetic profile from DATA_DIR: $DataRoot"
    }
    return Join-Path $parent ('.pkv-' + $leaf)
}

function Initialize-InternalSmokeProfile {
    param([Parameter(Mandatory = $true)][string]$DataRoot)

    # The packaged process receives an otherwise clean OS profile.  Its two
    # values are fixed, non-real fixture placeholders so ordinary product
    # Config parsing can validate Provider *structure* without reading a user
    # credential or making an outbound request.  Under PKV_TEST_OFFLINE,
    # RuntimeLayout resolves the Config at the deterministic DATA_DIR sibling
    # below, rather than USERPROFILE.  It remains outside the payload, inside
    # .data-test, and is never logged.
    $profileRoot = Get-InternalSmokeInjectedProfileRoot -DataRoot $DataRoot
    $profileConfigPath = Join-Path $profileRoot 'config.yaml'
    if (Test-Path -LiteralPath $profileConfigPath) {
        throw "synthetic internal-smoke profile config must be fresh: $profileConfigPath"
    }
    [void][System.IO.Directory]::CreateDirectory($profileRoot)
    $content = @(
        'ai:',
        '  llm:',
        '    api_key: internal-smoke-placeholder',
        '  embedding:',
        '    api_key: internal-smoke-placeholder',
        ''
    ) -join "`n"
    $stream = [System.IO.FileStream]::new(
        $profileConfigPath,
        [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None
    )
    try {
        $bytes = $Utf8NoBom.GetBytes($content)
        $stream.Write($bytes, 0, $bytes.Length)
    } finally {
        $stream.Dispose()
    }
    [void](Get-CanonicalExistingPath -Path $profileConfigPath -Kind Leaf `
        -Label 'synthetic internal-smoke profile config')
    return $profileRoot
}

function New-IsolatedProcessStartInfo {
    param(
        [Parameter(Mandatory = $true)][string]$FileName,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$DataRoot
    )

    $info = [System.Diagnostics.ProcessStartInfo]::new()
    # A keyword deny-list leaks unknown provider/credential variables (for
    # example *_KEY, access-key IDs, or a PKV_DATA_ROOT override).  Start from
    # an empty environment and restore only the synthetic runtime contract and
    # the small Windows bootstrap needed to start an onedir executable.
    $info.EnvironmentVariables.Clear()
    $info.FileName = $FileName
    $info.Arguments = (($Arguments | ForEach-Object {
        ConvertTo-WindowsCommandLineArgument -Value ([string]$_)
    }) -join ' ')
    $info.WorkingDirectory = $WorkingDirectory
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardInput = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $info.StandardErrorEncoding = [System.Text.Encoding]::UTF8

    $homeRoot = Get-InternalSmokeProfileRoot -DataRoot $DataRoot
    $appData = Join-Path $homeRoot 'AppData\Roaming'
    $localAppData = Join-Path $homeRoot 'AppData\Local'
    $tempRoot = Join-Path $DataRoot 'tmp\internal-package-child'
    foreach ($path in @($homeRoot, $appData, $localAppData, $tempRoot)) {
        [void][System.IO.Directory]::CreateDirectory($path)
    }
    foreach ($key in @('DATA_DIR', 'DB_PATH', 'VAULT_DIR', 'VECTOR_DIR', 'LOG_DIR', 'TMP_DIR')) {
        $info.EnvironmentVariables[$key] = [Environment]::GetEnvironmentVariable(
            $key,
            'Process'
        )
    }
    $info.EnvironmentVariables['PKV_TEST_OFFLINE'] = '1'
    $info.EnvironmentVariables['PKV_TEST_LOAD_LOCAL'] = '0'
    $info.EnvironmentVariables['PKV_RUN_LIVE'] = '0'
    $info.EnvironmentVariables['APPDATA'] = $appData
    $info.EnvironmentVariables['LOCALAPPDATA'] = $localAppData
    $info.EnvironmentVariables['USERPROFILE'] = $homeRoot
    $info.EnvironmentVariables['TEMP'] = $tempRoot
    $info.EnvironmentVariables['TMP'] = $tempRoot
    $info.EnvironmentVariables['TMPDIR'] = $tempRoot
    # ``TMP_DIR`` is a PKV runtime-layout child copied above with DATA_DIR and
    # must remain stable across fixture construction and the external Artifact.
    # TEMP/TMP/TMPDIR are child-process scratch only and may remain separate.

    $windowsRoot = [System.Environment]::GetFolderPath(
        [System.Environment+SpecialFolder]::Windows
    )
    $systemDirectory = [System.Environment]::SystemDirectory
    if ([string]::IsNullOrWhiteSpace($windowsRoot) -or
        [string]::IsNullOrWhiteSpace($systemDirectory)) {
        throw 'cannot determine the minimal Windows runtime environment for internal smoke'
    }
    $info.EnvironmentVariables['SystemRoot'] = $windowsRoot
    $info.EnvironmentVariables['WINDIR'] = $windowsRoot
    $info.EnvironmentVariables['ComSpec'] = Join-Path $systemDirectory 'cmd.exe'
    $info.EnvironmentVariables['PATHEXT'] = '.COM;.EXE;.BAT;.CMD'
    $info.EnvironmentVariables['PATH'] = "$systemDirectory;$windowsRoot"
    return $info
}

function Get-ShortProcessOutput {
    param([AllowNull()][string]$Text)

    if ([string]::IsNullOrEmpty($Text)) {
        return ''
    }
    $normalized = $Text.Replace("`r", '').Replace("`n", ' ').Trim()
    if ($normalized.Length -gt 4000) {
        return $normalized.Substring(0, 4000) + '...'
    }
    return $normalized
}

function Stop-InternalProcess {
    param([Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process)

    # Process.HasExited itself throws when Start() failed.  Cleanup must never
    # hide the primary assertion failure, so every lifecycle operation is
    # best-effort and self-contained.
    try {
        if (-not $Process.HasExited) {
            try {
                $Process.Kill($true)
            } catch {
                $taskkill = Join-Path ([System.Environment]::SystemDirectory) 'taskkill.exe'
                & $taskkill /PID ([string]$Process.Id) /T /F *> $null
                if (-not $Process.HasExited) {
                    $Process.Kill()
                }
            }
            [void]$Process.WaitForExit(5000)
        }
    } catch {
        # The owner reports the original failure; do not let cleanup replace it.
    }
}

function Start-InternalProcess {
    param([Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process)

    # Windows PowerShell 5.1 does not expose StandardInputEncoding.  Process
    # creation snapshots Console.InputEncoding for the redirected StreamWriter;
    # explicitly supply UTF-8 without a preamble so the JSON-RPC stream never
    # starts with U+FEFF.
    $previousInputEncoding = [Console]::InputEncoding
    try {
        [Console]::InputEncoding = $Utf8NoBom
        return $Process.Start()
    } finally {
        [Console]::InputEncoding = $previousInputEncoding
    }
}

function Invoke-InternalCommand {
    param(
        [Parameter(Mandatory = $true)][string]$FileName,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$DataRoot,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = New-IsolatedProcessStartInfo -FileName $FileName -Arguments $Arguments `
        -WorkingDirectory $WorkingDirectory -DataRoot $DataRoot
    try {
        if (-not (Start-InternalProcess -Process $process)) {
            throw "failed to start internal package command: $FileName"
        }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            $process.Kill()
            [void]$process.WaitForExit(5000)
            throw "internal package command timed out: $FileName"
        }
        $process.WaitForExit()
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) {
            throw "internal package command failed ($($process.ExitCode)): $FileName; stderr=$(Get-ShortProcessOutput $stderr)"
        }
        return [pscustomobject]@{ stdout = $stdout; stderr = $stderr }
    } finally {
        Stop-InternalProcess -Process $process
        $process.Dispose()
    }
}

function Test-InternalMcpInitialize {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$DataRoot,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )

    $process = [System.Diagnostics.Process]::new()
    $stderrTask = $null
    $process.StartInfo = New-IsolatedProcessStartInfo -FileName $Executable `
        -Arguments @('--transport', 'stdio', '--log-level', 'WARNING') `
        -WorkingDirectory $WorkingDirectory -DataRoot $DataRoot
    try {
        if (-not (Start-InternalProcess -Process $process)) {
            throw "failed to start internal MCP executable: $Executable"
        }
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $initialize = [ordered]@{
            jsonrpc = '2.0'
            id = 1
            method = 'initialize'
            params = [ordered]@{
                protocolVersion = '2025-11-25'
                capabilities = [ordered]@{}
                clientInfo = [ordered]@{ name = 'pkv-internal-smoke'; version = '1.0' }
            }
        } | ConvertTo-Json -Depth 10 -Compress
        $process.StandardInput.WriteLine($initialize)
        $process.StandardInput.Flush()
        $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
        $response = $null
        while ([DateTime]::UtcNow -lt $deadline) {
            $remaining = [Math]::Max(
                1,
                [int]($deadline - [DateTime]::UtcNow).TotalMilliseconds
            )
            $responseTask = $process.StandardOutput.ReadLineAsync()
            if (-not $responseTask.Wait($remaining)) {
                throw 'internal MCP initialize timed out'
            }
            $line = $responseTask.GetAwaiter().GetResult()
            if ([string]::IsNullOrWhiteSpace($line)) {
                throw 'internal MCP stdout closed before initialize response'
            }
            try {
                $message = $line | ConvertFrom-Json -ErrorAction Stop
            } catch {
                throw "internal MCP stdout was not JSON-RPC: $line"
            }
            $fields = @($message.PSObject.Properties.Name)
            if ($fields -notcontains 'jsonrpc' -or [string]$message.jsonrpc -ne '2.0') {
                throw "internal MCP response has an invalid JSON-RPC version: $line"
            }
            if ($fields -contains 'id' -and [int]$message.id -eq 1) {
                $response = $message
                break
            }
            if ($fields -notcontains 'method' -or
                -not ([string]$message.method).StartsWith('notifications/', [System.StringComparison]::Ordinal) -or
                $fields -contains 'id' -or $fields -contains 'result' -or $fields -contains 'error') {
                throw "internal MCP stdout contained an unexpected pre-initialize message: $line"
            }
        }
        if ($null -eq $response) {
            throw 'internal MCP initialize timed out without a response'
        }
        $responseFields = @($response.PSObject.Properties.Name)
        if ($responseFields -contains 'error' -or $responseFields -notcontains 'result') {
            throw "internal MCP initialize response is invalid: $($response | ConvertTo-Json -Depth 10 -Compress)"
        }
        $process.StandardInput.WriteLine((@{
            jsonrpc = '2.0'
            method = 'notifications/initialized'
            params = @{}
        } | ConvertTo-Json -Depth 5 -Compress))
        $process.StandardInput.Close()
        if (-not $process.WaitForExit(5000)) {
            $process.Kill()
            [void]$process.WaitForExit(5000)
        }
        $stderr = $stderrTask.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) {
            throw "internal MCP exited unexpectedly ($($process.ExitCode)): $(Get-ShortProcessOutput $stderr)"
        }
    } catch {
        $originalMessage = $_.Exception.Message
        Stop-InternalProcess -Process $process
        if ($null -ne $stderrTask) {
            try {
                $stderr = $stderrTask.GetAwaiter().GetResult()
                if (-not [string]::IsNullOrWhiteSpace($stderr)) {
                    throw "$originalMessage; stderr=$(Get-ShortProcessOutput $stderr)"
                }
            } catch {
                if ($_.Exception.Message -ne $originalMessage) {
                    throw
                }
            }
        }
        throw
    } finally {
        Stop-InternalProcess -Process $process
        $process.Dispose()
    }
}

try {
    $dataRoot = Assert-IsolatedRuntimeEnvironment
    [void](Initialize-InternalSmokeProfile -DataRoot $dataRoot)
    $internalRoot = Get-CanonicalExistingPath -Path $InternalOutputRoot -Kind Container `
        -Label 'dist/internal'
    $packageRoot = Get-CanonicalExistingPath -Path $PackageRoot -Kind Container `
        -Label 'internal package root'
    if ($packageRoot -eq $internalRoot -or
        -not (Test-PathContainedBy -Candidate $packageRoot -Root $internalRoot)) {
        throw "package root must be a single package below dist/internal: $packageRoot"
    }
    $packageName = Split-Path -Leaf $packageRoot
    $zipCandidate = if ([string]::IsNullOrWhiteSpace($PackageZip)) {
        Join-Path (Split-Path -Parent $packageRoot) "$packageName.zip"
    } else {
        $PackageZip
    }
    $packageZip = Get-CanonicalExistingPath -Path $zipCandidate -Kind Leaf `
        -Label 'internal package ZIP'
    if (-not (Test-PathContainedBy -Candidate $packageZip -Root $internalRoot)) {
        throw "package ZIP must stay below dist/internal: $packageZip"
    }
    $marker = Join-Path $packageRoot 'INTERNAL-TEST-ONLY.txt'
    $metadataPath = Join-Path $packageRoot 'internal-build-info.json'
    [void](Get-CanonicalExistingPath -Path $marker -Kind Leaf -Label 'internal marker')
    [void](Get-CanonicalExistingPath -Path $metadataPath -Kind Leaf -Label 'internal metadata')
    $metadata = [System.IO.File]::ReadAllText($metadataPath, $Utf8NoBom) | ConvertFrom-Json -ErrorAction Stop
    if ([string]$metadata.classification -cne 'INTERNAL TEST ONLY' -or
        [string]$metadata.package_id -cne $packageName) {
        throw 'internal metadata classification or package ID is invalid'
    }

    if ([string]::IsNullOrWhiteSpace($WorkspaceRoot)) {
        $parent = Split-Path -Parent $ProjectRoot
        $ResolvedWorkspace = [System.IO.Path]::GetFullPath(
            (Join-Path $parent ('.pkv-internal-smoke-' + [Guid]::NewGuid().ToString('N')))
        )
    } else {
        $ResolvedWorkspace = [System.IO.Path]::GetFullPath($WorkspaceRoot)
    }
    if (Test-PathContainedBy -Candidate $ResolvedWorkspace -Root $ProjectRoot) {
        throw "external package workspace must stay outside the repository: $ResolvedWorkspace"
    }
    if (Test-Path -LiteralPath $ResolvedWorkspace) {
        throw "external package workspace must not already exist: $ResolvedWorkspace"
    }
    [void][System.IO.Directory]::CreateDirectory($ResolvedWorkspace)
    $CreatedWorkspace = $true
    $extractionRoot = Expand-InternalPackageZipSafely -ZipPath $packageZip `
        -Destination (Join-Path $ResolvedWorkspace 'extracted') -ExpectedRootName $packageName
    $appRoot = Get-CanonicalExistingPath -Path (Join-Path $extractionRoot 'pkv') `
        -Kind Container -Label 'external onedir root'
    if (Test-PathContainedBy -Candidate $appRoot -Root $ProjectRoot) {
        throw 'the onedir application did not start from outside the repository'
    }
    $cli = Get-CanonicalExistingPath -Path (Join-Path $appRoot 'pkv.exe') -Kind Leaf `
        -Label 'internal CLI executable'
    $mcp = Get-CanonicalExistingPath -Path (Join-Path $appRoot 'pkv-mcp.exe') -Kind Leaf `
        -Label 'internal MCP executable'

    $help = Invoke-InternalCommand -FileName $cli -Arguments @('--help') `
        -WorkingDirectory $appRoot -DataRoot $dataRoot -TimeoutSeconds $CommandTimeoutSeconds
    if ($help.stdout -notmatch '(?i)usage') {
        throw 'internal CLI --help did not produce a usage banner'
    }
    $search = Invoke-InternalCommand -FileName $cli `
        -Arguments @('search', 'AI', '--strategy', 'bm25', '--format', 'json') `
        -WorkingDirectory $appRoot -DataRoot $dataRoot -TimeoutSeconds $CommandTimeoutSeconds
    try {
        $searchPayload = $search.stdout | ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw "internal CLI BM25 smoke did not return JSON: $(Get-ShortProcessOutput $search.stdout)"
    }
    if ([string]$searchPayload.strategy -cne 'bm25' -or
        [string]$searchPayload.status -in @('invalid', 'error')) {
        throw 'internal CLI BM25 smoke returned an invalid/error result'
    }
    Test-InternalMcpInitialize -Executable $mcp -WorkingDirectory $appRoot `
        -DataRoot $dataRoot -TimeoutSeconds $CommandTimeoutSeconds
    $reportRoot = Join-Path $dataRoot 'reports'
    [void][System.IO.Directory]::CreateDirectory($reportRoot)
    $reportPath = Join-Path $reportRoot 'internal-package-smoke.json'
    [System.IO.File]::WriteAllText($reportPath, (([ordered]@{
        schema_version = 'pkv.internal-package-smoke.v1'
        classification = 'INTERNAL TEST ONLY'
        package_id = $packageName
        package_working_directory = $appRoot
        data_root = $dataRoot
        checks = @('cli_help', 'cli_bm25', 'mcp_stdio_initialize')
        result = 'internal self-test passed'
    } | ConvertTo-Json -Depth 10) + "`n"), $Utf8NoBom)
    Write-Output 'INTERNAL SELF-TEST PASSED'
} finally {
    if ($CreatedWorkspace -and -not $KeepWorkspace -and $null -ne $ResolvedWorkspace -and
        (Test-Path -LiteralPath $ResolvedWorkspace) -and
        -not (Test-PathContainedBy -Candidate $ResolvedWorkspace -Root $ProjectRoot) -and
        ([System.IO.Path]::GetFileName($ResolvedWorkspace)).StartsWith('.pkv-internal-smoke-', [System.StringComparison]::OrdinalIgnoreCase)) {
        Remove-InternalWorkspaceSafely -Path $ResolvedWorkspace `
            -ForbiddenRoot $ProjectRoot -RequiredLeafPrefix '.pkv-internal-smoke-'
    }
}
