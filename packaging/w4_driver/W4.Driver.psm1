#requires -Version 5.1

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
$script:ProcessInputEncodingLock = [object]::new()

function ConvertTo-W4CommandLineArgument {
    [CmdletBinding()]
    param([AllowEmptyString()][string]$Value)

    if ($Value -notmatch '[\s"]') {
        return $Value
    }
    $builder = [System.Text.StringBuilder]::new()
    [void]$builder.Append('"')
    $slashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $slashes += 1
            continue
        }
        if ($character -eq '"') {
            [void]$builder.Append(('\' * (($slashes * 2) + 1)))
            [void]$builder.Append('"')
            $slashes = 0
            continue
        }
        if ($slashes -gt 0) {
            [void]$builder.Append(('\' * $slashes))
            $slashes = 0
        }
        [void]$builder.Append($character)
    }
    if ($slashes -gt 0) {
        [void]$builder.Append(('\' * ($slashes * 2)))
    }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Write-W4JsonFile {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowNull()]$Value,
        [ValidateRange(2, 100)][int]$Depth = 30,
        [switch]$Compress
    )

    $parent = [System.IO.Path]::GetDirectoryName([System.IO.Path]::GetFullPath($Path))
    if (-not [string]::IsNullOrWhiteSpace($parent)) {
        [void][System.IO.Directory]::CreateDirectory($parent)
    }
    $json = if ($Compress) {
        $Value | ConvertTo-Json -Depth $Depth -Compress
    } else {
        $Value | ConvertTo-Json -Depth $Depth
    }
    [System.IO.File]::WriteAllText($Path, $json + "`n", $script:Utf8NoBom)
}

function Read-W4JsonFile {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required JSON file is missing: $Path"
    }
    try {
        $value = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8) |
            ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw "Invalid JSON file at $($Path): $($_.Exception.Message)"
    }
    if ($null -eq $value) {
        throw "JSON file contains null: $Path"
    }
    return $value
}

function Get-W4FileSha256 {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Cannot hash missing file: $Path"
    }
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

function Get-W4StringSha256 {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)

    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = $script:Utf8NoBom.GetBytes($Value)
        return ([System.BitConverter]::ToString($algorithm.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
    }
}

function Get-W4CanonicalJsonSha256 {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][AllowNull()]$Value,
        [ValidateRange(2, 100)][int]$Depth = 50
    )

    # W3 writes canonical JSON with sorted object keys, compact separators, UTF-8,
    # and exactly one trailing LF.  Objects read from those files retain that key
    # order in Windows PowerShell 5.1; newly constructed documents passed here
    # must therefore use [ordered] maps in canonical key order.
    $json = ConvertTo-Json -InputObject $Value -Depth $Depth -Compress
    # Windows PowerShell 5.1's JSON serializer HTML-escapes these printable
    # characters, while Python's canonical encoder intentionally leaves them
    # literal (ensure_ascii=False).  Normalize that serializer difference before
    # hashing W3 canonical documents such as the <bootloader-prefix> path.
    $json = [System.Text.RegularExpressions.Regex]::Replace(
        $json,
        '(?<!\x5c)(?<pairs>(?:\x5c\x5c)*)\x5cu(?<code>0026|0027|003[cCeE]|0085|2028|2029)',
        {
            param($match)
            return $match.Groups['pairs'].Value +
                [char][Convert]::ToInt32($match.Groups['code'].Value, 16)
        }
    )
    return Get-W4StringSha256 -Value ($json + "`n")
}

function Get-W4FileSegmentSha256 {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][ValidateRange(0, [long]::MaxValue)][long]$Offset,
        [Parameter(Mandatory = $true)][ValidateRange(0, [long]::MaxValue)][long]$Length
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Cannot hash a segment of a missing file: $Path"
    }
    $fileLength = [int64](Get-Item -LiteralPath $Path -Force -ErrorAction Stop).Length
    if ($Offset -gt $fileLength -or $Length -gt ($fileLength - $Offset)) {
        throw "File segment exceeds file bounds: path=$Path offset=$Offset length=$Length size=$fileLength"
    }
    $stream = [System.IO.FileStream]::new(
        $Path,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::Read
    )
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        [void]$stream.Seek($Offset, [System.IO.SeekOrigin]::Begin)
        $remaining = [int64]$Length
        $buffer = [byte[]]::new(1024 * 1024)
        while ($remaining -gt 0) {
            $requested = [int][Math]::Min([int64]$buffer.Length, $remaining)
            $read = $stream.Read($buffer, 0, $requested)
            if ($read -le 0) {
                throw "Unexpected EOF while hashing file segment: $Path"
            }
            [void]$algorithm.TransformBlock($buffer, 0, $read, $buffer, 0)
            $remaining -= $read
        }
        $empty = [byte[]]::new(0)
        [void]$algorithm.TransformFinalBlock($empty, 0, 0)
        return ([System.BitConverter]::ToString($algorithm.Hash)).Replace('-', '').ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
        $stream.Dispose()
    }
}

function Initialize-W4FileIdentityInspector {
    if ('PkvW4.FileIdentity' -as [type]) {
        return
    }
    Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace PkvW4 {
    public static class FileIdentity {
        [StructLayout(LayoutKind.Sequential)]
        private struct BY_HANDLE_FILE_INFORMATION {
            public uint FileAttributes;
            public System.Runtime.InteropServices.ComTypes.FILETIME CreationTime;
            public System.Runtime.InteropServices.ComTypes.FILETIME LastAccessTime;
            public System.Runtime.InteropServices.ComTypes.FILETIME LastWriteTime;
            public uint VolumeSerialNumber;
            public uint FileSizeHigh;
            public uint FileSizeLow;
            public uint NumberOfLinks;
            public uint FileIndexHigh;
            public uint FileIndexLow;
        }

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetFileInformationByHandle(
            SafeFileHandle handle,
            out BY_HANDLE_FILE_INFORMATION information
        );

        public static uint GetLinkCount(string path) {
            using (FileStream stream = new FileStream(
                path,
                FileMode.Open,
                FileAccess.Read,
                FileShare.ReadWrite | FileShare.Delete
            )) {
                BY_HANDLE_FILE_INFORMATION information;
                if (!GetFileInformationByHandle(stream.SafeFileHandle, out information)) {
                    throw new System.ComponentModel.Win32Exception(
                        Marshal.GetLastWin32Error(),
                        "GetFileInformationByHandle failed"
                    );
                }
                return information.NumberOfLinks;
            }
        }
    }
}
'@ -ErrorAction Stop
}

function Get-W4SafeTreeFiles {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Root)

    if ([Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
        throw 'W4 Artifact tree identity checks require Windows'
    }
    Initialize-W4FileIdentityInspector
    $resolvedRoot = [System.IO.Path]::GetFullPath(
        (Resolve-Path -LiteralPath $Root -ErrorAction Stop).ProviderPath
    )
    $rootItem = Get-Item -LiteralPath $resolvedRoot -Force -ErrorAction Stop
    if (-not $rootItem.PSIsContainer -or
        ($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Tree root must be a normal non-link directory: $resolvedRoot"
    }
    $pending = [System.Collections.Generic.Stack[string]]::new()
    $pending.Push($resolvedRoot)
    $files = [System.Collections.Generic.List[object]]::new()
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        foreach ($item in @(Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop)) {
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Artifact tree contains a reparse point: $($item.FullName)"
            }
            if ($item.PSIsContainer) {
                $pending.Push($item.FullName)
                continue
            }
            $linkCount = [PkvW4.FileIdentity]::GetLinkCount($item.FullName)
            if ($linkCount -ne 1) {
                throw "Artifact tree contains a multi-link file: $($item.FullName) count=$linkCount"
            }
            $files.Add($item)
        }
    }
    return @($files | Sort-Object FullName)
}

function Assert-W4SafePathChain {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $cursor = [System.IO.Path]::GetFullPath($Path)
    while ($true) {
        $item = $null
        try {
            $item = Get-Item -LiteralPath $cursor -Force -ErrorAction Stop
        } catch [System.Management.Automation.ItemNotFoundException] {
            $item = $null
        } catch {
            throw "Cannot inspect path chain for $Label at $($cursor): $($_.Exception.Message)"
        }
        if ($null -ne $item -and
            ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Unsafe ReparsePoint rejected for $Label at $cursor"
        }
        $parent = [System.IO.Path]::GetDirectoryName($cursor)
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent -eq $cursor) {
            break
        }
        $cursor = $parent
    }
}

function Get-W4TreeManifest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [string[]]$ExcludedRelativePrefixes = @()
    )

    $resolvedRoot = [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $Root -ErrorAction Stop).ProviderPath)
    $prefixes = @($ExcludedRelativePrefixes | ForEach-Object {
        ([string]$_).Replace('\', '/').Trim('/')
    })
    $rows = [System.Collections.Generic.List[object]]::new()
    foreach ($file in @(Get-W4SafeTreeFiles -Root $resolvedRoot)) {
        $relative = $file.FullName.Substring($resolvedRoot.TrimEnd('\').Length).TrimStart('\').Replace('\', '/')
        $excluded = $false
        foreach ($prefix in $prefixes) {
            if ($relative -eq $prefix -or $relative.StartsWith($prefix + '/', [System.StringComparison]::OrdinalIgnoreCase)) {
                $excluded = $true
                break
            }
        }
        if ($excluded) {
            continue
        }
        $rows.Add([ordered]@{
            path = $relative
            size = [int64]$file.Length
            sha256 = Get-W4FileSha256 -Path $file.FullName
        })
    }
    return @($rows)
}

function Get-W4TreeSha256 {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [string[]]$ExcludedRelativePrefixes = @()
    )

    $rows = Get-W4TreeManifest -Root $Root -ExcludedRelativePrefixes $ExcludedRelativePrefixes
    $normalized = ($rows | ConvertTo-Json -Depth 5 -Compress)
    return Get-W4StringSha256 -Value $normalized
}

function Test-W4PathContainedBy {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$Root
    )

    $candidatePath = [System.IO.Path]::GetFullPath($Candidate).TrimEnd('\', '/')
    $rootPath = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    if ($candidatePath.Equals($rootPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    return $candidatePath.StartsWith($rootPath + '\', [System.StringComparison]::OrdinalIgnoreCase)
}

function Assert-W4DisjointPaths {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$First,
        [Parameter(Mandatory = $true)][string]$FirstLabel,
        [Parameter(Mandatory = $true)][string]$Second,
        [Parameter(Mandatory = $true)][string]$SecondLabel
    )

    if ((Test-W4PathContainedBy -Candidate $First -Root $Second) -or
        (Test-W4PathContainedBy -Candidate $Second -Root $First)) {
        throw "$FirstLabel and $SecondLabel must be disjoint"
    }
}

function New-W4IsolatedEnvironment {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$WorkspaceRoot,
        [Parameter(Mandatory = $true)][string]$UserDataRoot,
        [Parameter(Mandatory = $true)][string]$ArtifactRoot,
        [hashtable]$Additional = @{}
    )

    $systemRoot = [Environment]::GetEnvironmentVariable('SystemRoot')
    if ([string]::IsNullOrWhiteSpace($systemRoot)) {
        throw 'SystemRoot is required for the isolated Artifact environment'
    }
    $workspace = [System.IO.Path]::GetFullPath($WorkspaceRoot)
    $profile = Join-Path $workspace 'profile'
    $temp = Join-Path $workspace 'tmp'
    $localAppData = Join-Path $profile 'AppData\Local'
    $appData = Join-Path $profile 'AppData\Roaming'
    foreach ($directory in @($workspace, $profile, $temp, $localAppData, $appData, $UserDataRoot)) {
        [void][System.IO.Directory]::CreateDirectory($directory)
    }
    $systemPath = @(
        (Join-Path $systemRoot 'System32'),
        (Join-Path $systemRoot 'System32\Wbem'),
        (Join-Path $systemRoot 'System32\WindowsPowerShell\v1.0')
    ) -join [System.IO.Path]::PathSeparator
    $environment = [ordered]@{
        SystemRoot = $systemRoot
        WINDIR = $systemRoot
        COMSPEC = (Join-Path $systemRoot 'System32\cmd.exe')
        PATH = $systemPath
        PATHEXT = '.COM;.EXE;.BAT;.CMD'
        TEMP = $temp
        TMP = $temp
        TMPDIR = $temp
        USERPROFILE = $profile
        LOCALAPPDATA = $localAppData
        APPDATA = $appData
        PKV_DATA_ROOT = [System.IO.Path]::GetFullPath($UserDataRoot)
        PKV_ARTIFACT_ROOT = [System.IO.Path]::GetFullPath($ArtifactRoot)
        PYTHONIOENCODING = 'utf-8'
        NO_COLOR = '1'
    }
    foreach ($key in @($Additional.Keys | Sort-Object)) {
        $name = [string]$key
        if ($name -match '^(PYTHONPATH|PYTHONHOME|PYTHONSTARTUP|PYTHONUSERBASE|CONDA_.+|VIRTUAL_ENV|HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|OPENAI_API_KEY|DEEPSEEK_API_KEY)$') {
            throw "Forbidden isolated environment override: $name"
        }
        $environment[$name] = [string]$Additional[$key]
    }
    return $environment
}

function New-W4ProcessStartInfo {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$FileName,
        [string[]]$Arguments = @(),
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)]$Environment,
        [switch]$RedirectInput,
        [switch]$VisibleWindow
    )

    if (-not (Test-Path -LiteralPath $WorkingDirectory -PathType Container)) {
        throw "Process working directory does not exist: $WorkingDirectory"
    }
    $processInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $processInfo.FileName = $FileName
    $processInfo.Arguments = (($Arguments | ForEach-Object {
        ConvertTo-W4CommandLineArgument -Value ([string]$_)
    }) -join ' ')
    $processInfo.WorkingDirectory = [System.IO.Path]::GetFullPath($WorkingDirectory)
    $processInfo.UseShellExecute = $false
    $processInfo.CreateNoWindow = -not $VisibleWindow
    $processInfo.WindowStyle = if ($VisibleWindow) {
        [System.Diagnostics.ProcessWindowStyle]::Normal
    } else {
        [System.Diagnostics.ProcessWindowStyle]::Hidden
    }
    $processInfo.RedirectStandardOutput = $true
    $processInfo.RedirectStandardError = $true
    $processInfo.RedirectStandardInput = [bool]$RedirectInput
    $processInfo.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $processInfo.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    $processInfo.EnvironmentVariables.Clear()
    foreach ($entry in $Environment.GetEnumerator()) {
        $processInfo.EnvironmentVariables[[string]$entry.Key] = [string]$entry.Value
    }
    return $processInfo
}

function Start-W4RedirectedProcess {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process,
        [switch]$RedirectInput
    )

    if (-not $RedirectInput) {
        return $Process.Start()
    }

    # Windows PowerShell 5.1 does not expose ProcessStartInfo.StandardInputEncoding.
    # Process.Start snapshots Console.InputEncoding when it creates the redirected
    # StreamWriter, so make that snapshot explicitly UTF-8 without a preamble.  The
    # console setting is process-global: serialize the small critical section and
    # restore the ambient value even when Process.Start throws.
    [System.Threading.Monitor]::Enter($script:ProcessInputEncodingLock)
    $previousInputEncoding = $null
    try {
        $previousInputEncoding = [Console]::InputEncoding
        [Console]::InputEncoding = $script:Utf8NoBom
        return $Process.Start()
    } finally {
        if ($null -ne $previousInputEncoding) {
            [Console]::InputEncoding = $previousInputEncoding
        }
        [System.Threading.Monitor]::Exit($script:ProcessInputEncodingLock)
    }
}

function Stop-W4ProcessTree {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process)

    if ($Process.HasExited) {
        return
    }
    try {
        $Process.Kill($true)
    } catch {
        $taskKill = Join-Path ([Environment]::GetEnvironmentVariable('SystemRoot')) 'System32\taskkill.exe'
        & $taskKill /PID $Process.Id /T /F *> $null
    }
    [void]$Process.WaitForExit(5000)
}

function Invoke-W4Process {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$FileName,
        [string[]]$Arguments = @(),
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)]$Environment,
        [Parameter(Mandatory = $true)][string]$EvidenceDirectory,
        [int[]]$ExpectedExitCodes = @(0),
        [ValidateRange(1, 1800)][int]$TimeoutSeconds = 60,
        [AllowNull()][string]$StandardInput = $null,
        [switch]$VisibleWindow
    )

    [void][System.IO.Directory]::CreateDirectory($EvidenceDirectory)
    $redactedEnvironment = [ordered]@{}
    foreach ($entry in $Environment.GetEnumerator()) {
        $redactedEnvironment[[string]$entry.Key] = if ([string]$entry.Key -match '(KEY|TOKEN|SECRET|PASSWORD)') {
            '<redacted>'
        } else {
            [string]$entry.Value
        }
    }
    $invocation = [ordered]@{
        file = [System.IO.Path]::GetFullPath($FileName)
        arguments = @($Arguments)
        working_directory = [System.IO.Path]::GetFullPath($WorkingDirectory)
        environment = $redactedEnvironment
        expected_exit_codes = @($ExpectedExitCodes)
        timeout_seconds = $TimeoutSeconds
    }
    Write-W4JsonFile -Path (Join-Path $EvidenceDirectory 'invocation.json') -Value $invocation

    $redirectInput = $null -ne $StandardInput
    $processInfo = New-W4ProcessStartInfo -FileName $FileName -Arguments $Arguments `
        -WorkingDirectory $WorkingDirectory -Environment $Environment `
        -RedirectInput:$redirectInput -VisibleWindow:$VisibleWindow
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $processInfo
    $startedAt = [DateTime]::UtcNow
    $timedOut = $false
    $forcedTermination = $false
    try {
        if (-not (Start-W4RedirectedProcess -Process $process -RedirectInput:$redirectInput)) {
            throw "Process did not start: $FileName"
        }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if ($redirectInput) {
            $process.StandardInput.Write($StandardInput)
            $process.StandardInput.Close()
        }
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            $timedOut = $true
            $forcedTermination = $true
            Stop-W4ProcessTree -Process $process
        } else {
            $process.WaitForExit()
        }
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        [System.IO.File]::WriteAllText((Join-Path $EvidenceDirectory 'stdout.txt'), $stdout, $script:Utf8NoBom)
        [System.IO.File]::WriteAllText((Join-Path $EvidenceDirectory 'stderr.txt'), $stderr, $script:Utf8NoBom)
        $exitCode = if ($process.HasExited) { [int]$process.ExitCode } else { $null }
        $processEvidence = [ordered]@{
            started_at = $startedAt.ToString('o')
            ended_at = [DateTime]::UtcNow.ToString('o')
            process_id = [int]$process.Id
            exit_code = $exitCode
            timed_out = $timedOut
            forced_termination = $forcedTermination
            stdout_sha256 = Get-W4FileSha256 -Path (Join-Path $EvidenceDirectory 'stdout.txt')
            stderr_sha256 = Get-W4FileSha256 -Path (Join-Path $EvidenceDirectory 'stderr.txt')
        }
        Write-W4JsonFile -Path (Join-Path $EvidenceDirectory 'process.json') -Value $processEvidence
        if ($timedOut) {
            throw "Process timed out after $TimeoutSeconds seconds: $FileName"
        }
        if ($ExpectedExitCodes -notcontains $exitCode) {
            throw "Unexpected process exit code $exitCode; expected $($ExpectedExitCodes -join ', '): $FileName"
        }
        return [pscustomobject]@{
            ExitCode = $exitCode
            StandardOutput = $stdout
            StandardError = $stderr
            ProcessEvidence = $processEvidence
        }
    } finally {
        if (-not $process.HasExited) {
            Stop-W4ProcessTree -Process $process
        }
        $process.Dispose()
    }
}

function Start-W4LongRunningProcess {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$FileName,
        [string[]]$Arguments = @(),
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)]$Environment,
        [switch]$RedirectInput,
        [switch]$VisibleWindow
    )

    $processInfo = New-W4ProcessStartInfo -FileName $FileName -Arguments $Arguments `
        -WorkingDirectory $WorkingDirectory -Environment $Environment `
        -RedirectInput:$RedirectInput -VisibleWindow:$VisibleWindow
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $processInfo
    if (-not (Start-W4RedirectedProcess -Process $process -RedirectInput:$RedirectInput)) {
        $process.Dispose()
        throw "Process did not start: $FileName"
    }
    return $process
}

function Assert-W4JsonObjectFields {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Object,
        [Parameter(Mandatory = $true)][string[]]$RequiredFields,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $names = @($Object.PSObject.Properties.Name)
    foreach ($field in $RequiredFields) {
        if ($names -cnotcontains $field) {
            throw "$Label is missing required field: $field"
        }
    }
}

function ConvertFrom-W4StrictJsonText {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $trimmed = $Text.Trim()
    if ([string]::IsNullOrWhiteSpace($trimmed)) {
        throw "$Label returned empty JSON output"
    }
    try {
        return $trimmed | ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw "$Label returned non-JSON output; stdout must contain exactly one JSON value"
    }
}

function Send-W4McpMessage {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process,
        [Parameter(Mandatory = $true)]$Message,
        [Parameter(Mandatory = $true)][string]$TranscriptPath
    )

    $json = $Message | ConvertTo-Json -Depth 30 -Compress
    [System.IO.File]::AppendAllText(
        $TranscriptPath,
        (([ordered]@{ direction = 'client_to_server'; message = $Message } | ConvertTo-Json -Depth 30 -Compress) + "`n"),
        $script:Utf8NoBom
    )
    $Process.StandardInput.WriteLine($json)
    $Process.StandardInput.Flush()
}

function Receive-W4McpResponse {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process,
        [Parameter(Mandatory = $true)][int]$Id,
        [Parameter(Mandatory = $true)][string]$TranscriptPath,
        [ValidateRange(1, 120)][int]$TimeoutSeconds = 20
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        $remaining = [Math]::Max(1, [int]($deadline - [DateTime]::UtcNow).TotalMilliseconds)
        $readTask = $Process.StandardOutput.ReadLineAsync()
        if (-not $readTask.Wait($remaining)) {
            throw "MCP response id=$Id timed out after $TimeoutSeconds seconds"
        }
        $line = $readTask.GetAwaiter().GetResult()
        if ($null -eq $line) {
            throw "MCP stdout closed before response id=$Id"
        }
        if ([string]::IsNullOrWhiteSpace($line)) {
            throw 'MCP stdout contained a blank non-protocol line'
        }
        try {
            $message = $line | ConvertFrom-Json -ErrorAction Stop
        } catch {
            throw "MCP stdout contained non-JSON protocol data: $line"
        }
        [System.IO.File]::AppendAllText(
            $TranscriptPath,
            (([ordered]@{ direction = 'server_to_client'; message = $message } | ConvertTo-Json -Depth 30 -Compress) + "`n"),
            $script:Utf8NoBom
        )
        if ($message.PSObject.Properties.Name -contains 'id' -and [int]$message.id -eq $Id) {
            if ($message.PSObject.Properties.Name -contains 'error') {
                throw "MCP response id=$Id returned JSON-RPC error: $($message.error | ConvertTo-Json -Depth 10 -Compress)"
            }
            if ($message.PSObject.Properties.Name -notcontains 'result') {
                throw "MCP response id=$Id has neither result nor error"
            }
            return $message.result
        }
    }
    throw "MCP response id=$Id timed out"
}

function Invoke-W4McpRequest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process,
        [Parameter(Mandatory = $true)][int]$Id,
        [Parameter(Mandatory = $true)][string]$Method,
        [Parameter(Mandatory = $true)][AllowNull()]$Params,
        [Parameter(Mandatory = $true)][string]$TranscriptPath,
        [ValidateRange(1, 120)][int]$TimeoutSeconds = 20
    )

    $message = [ordered]@{
        jsonrpc = '2.0'
        id = $Id
        method = $Method
        params = $Params
    }
    Send-W4McpMessage -Process $Process -Message $message -TranscriptPath $TranscriptPath
    return Receive-W4McpResponse -Process $Process -Id $Id -TranscriptPath $TranscriptPath -TimeoutSeconds $TimeoutSeconds
}

function Start-W4McpSession {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)]$Environment,
        [Parameter(Mandatory = $true)][string]$TranscriptPath,
        [Parameter(Mandatory = $true)][string]$ProtocolVersion
    )

    [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($TranscriptPath))
    [System.IO.File]::WriteAllText($TranscriptPath, '', $script:Utf8NoBom)
    $process = Start-W4LongRunningProcess -FileName $Executable `
        -Arguments @('--transport', 'stdio', '--log-level', 'WARNING') `
        -WorkingDirectory $WorkingDirectory -Environment $Environment -RedirectInput
    # Drain stderr from process start.  A noisy server must not be able to fill
    # the redirected pipe and deadlock while stdout is consumed as JSON-RPC.
    $stderrTask = $process.StandardError.ReadToEndAsync()
    try {
        $initialize = Invoke-W4McpRequest -Process $process -Id 1 -Method 'initialize' `
            -Params ([ordered]@{
                protocolVersion = $ProtocolVersion
                capabilities = [ordered]@{}
                clientInfo = [ordered]@{ name = 'pkv-w4-driver'; version = '1.0' }
            }) -TranscriptPath $TranscriptPath -TimeoutSeconds 30
        Send-W4McpMessage -Process $process -Message ([ordered]@{
            jsonrpc = '2.0'
            method = 'notifications/initialized'
            params = [ordered]@{}
        }) -TranscriptPath $TranscriptPath
        return [pscustomobject]@{
            Process = $process
            Initialize = $initialize
            TranscriptPath = $TranscriptPath
            NextId = 2
            StderrTask = $stderrTask
        }
    } catch {
        Stop-W4ProcessTree -Process $process
        $startupStderr = $stderrTask.GetAwaiter().GetResult()
        [System.IO.File]::WriteAllText(
            (Join-Path ([System.IO.Path]::GetDirectoryName($TranscriptPath)) 'mcp-stderr.txt'),
            $startupStderr,
            $script:Utf8NoBom
        )
        $process.Dispose()
        throw
    }
}

function Stop-W4McpSession {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Session,
        [Parameter(Mandatory = $true)][string]$EvidenceDirectory,
        [ValidateRange(1, 60)][int]$TimeoutSeconds = 20
    )

    $process = [System.Diagnostics.Process]$Session.Process
    $forced = $false
    try {
        # No request reader is outstanding here.  Start draining the remaining
        # stdout before closing stdin so shutdown notifications cannot fill the
        # pipe and deadlock the child process.
        $stdoutTailTask = $process.StandardOutput.ReadToEndAsync()
        $process.StandardInput.Close()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            $forced = $true
            Stop-W4ProcessTree -Process $process
        } else {
            $process.WaitForExit()
        }
        $stdoutTail = $stdoutTailTask.GetAwaiter().GetResult()
        $stderr = $Session.StderrTask.GetAwaiter().GetResult()
        $tailPath = Join-Path $EvidenceDirectory 'mcp-stdout-tail.txt'
        [System.IO.File]::WriteAllText($tailPath, $stdoutTail, $script:Utf8NoBom)
        [System.IO.File]::WriteAllText((Join-Path $EvidenceDirectory 'mcp-stderr.txt'), $stderr, $script:Utf8NoBom)
        $tailMessages = [System.Collections.Generic.List[object]]::new()
        $normalizedTail = $stdoutTail.Replace("`r`n", "`n").Replace("`r", "`n")
        $tailLines = @($normalizedTail.Split("`n"))
        for ($tailIndex = 0; $tailIndex -lt $tailLines.Count; $tailIndex += 1) {
            $tailLine = [string]$tailLines[$tailIndex]
            if ($tailLine.Length -eq 0 -and $tailIndex -eq ($tailLines.Count - 1)) {
                continue
            }
            if ([string]::IsNullOrWhiteSpace($tailLine)) {
                throw 'MCP stdout tail contained a blank non-protocol line'
            }
            try {
                $tailMessage = $tailLine | ConvertFrom-Json -ErrorAction Stop
            } catch {
                throw "MCP stdout tail contained non-JSON protocol data: $tailLine"
            }
            $tailFields = @($tailMessage.PSObject.Properties.Name)
            if ([string]$tailMessage.jsonrpc -ne '2.0' -or
                $tailFields -notcontains 'method' -or
                -not ([string]$tailMessage.method).StartsWith('notifications/', [System.StringComparison]::Ordinal) -or
                $tailFields -contains 'id' -or
                $tailFields -contains 'result' -or
                $tailFields -contains 'error') {
                throw "MCP stdout tail contained an unexpected non-notification message: $tailLine"
            }
            $tailMessages.Add($tailMessage)
            [System.IO.File]::AppendAllText(
                [string]$Session.TranscriptPath,
                (([ordered]@{ direction = 'server_to_client_after_eof'; message = $tailMessage } |
                    ConvertTo-Json -Depth 30 -Compress) + "`n"),
                $script:Utf8NoBom
            )
        }
        $result = [ordered]@{
            stdin_closed = $true
            exit_code = if ($process.HasExited) { [int]$process.ExitCode } else { $null }
            forced_termination = $forced
            timed_out = $forced
            trailing_notification_count = $tailMessages.Count
            stdout_tail_sha256 = Get-W4FileSha256 -Path $tailPath
        }
        Write-W4JsonFile -Path (Join-Path $EvidenceDirectory 'mcp-process.json') -Value $result
        if ($forced) {
            throw 'MCP server did not exit naturally after stdin EOF'
        }
        if ($process.ExitCode -ne 0) {
            throw "MCP server exited $($process.ExitCode) after stdin EOF"
        }
        return $result
    } finally {
        if (-not $process.HasExited) {
            Stop-W4ProcessTree -Process $process
        }
        $process.Dispose()
    }
}

function Initialize-W4Uia {
    [CmdletBinding()]
    param()

    if ([Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
        throw 'W4 GUI Artifact scenarios require a real Windows desktop with UI Automation'
    }
    try {
        Add-Type -AssemblyName UIAutomationClient -ErrorAction Stop
        Add-Type -AssemblyName UIAutomationTypes -ErrorAction Stop
    } catch {
        throw "Windows UI Automation assemblies are unavailable: $($_.Exception.Message)"
    }
    if ($null -eq [System.Windows.Automation.AutomationElement]::RootElement) {
        throw 'Windows UI Automation desktop root is unavailable'
    }
}

function Get-W4UiaElementById {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][System.Windows.Automation.AutomationElement]$Root,
        [Parameter(Mandatory = $true)][string]$AutomationId,
        [ValidateRange(1, 120)][int]$TimeoutSeconds = 20,
        [switch]$AllowZero
    )

    $condition = [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::AutomationIdProperty,
        $AutomationId
    )
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $matches = $Root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
        if ($matches.Count -eq 1) {
            return $matches.Item(0)
        }
        if ($matches.Count -gt 1) {
            throw "AutomationId must be unique but matched $($matches.Count) elements: $AutomationId"
        }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)
    if ($AllowZero) {
        return $null
    }
    throw "AutomationId was not found within $TimeoutSeconds seconds: $AutomationId"
}

function Get-W4UiaMainWindow {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [ValidateRange(1, 120)][int]$TimeoutSeconds = 30
    )

    Initialize-W4Uia
    $idCondition = [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::AutomationIdProperty,
        'pkv_main_window'
    )
    $pidCondition = [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::ProcessIdProperty,
        $ProcessId
    )
    $condition = [System.Windows.Automation.AndCondition]::new($idCondition, $pidCondition)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $matches = [System.Windows.Automation.AutomationElement]::RootElement.FindAll(
            [System.Windows.Automation.TreeScope]::Children,
            $condition
        )
        if ($matches.Count -eq 1) {
            return $matches.Item(0)
        }
        if ($matches.Count -gt 1) {
            throw "Process $ProcessId exposed multiple pkv_main_window elements"
        }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Process $ProcessId did not expose pkv_main_window through UI Automation"
}

function Get-W4UiaText {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][System.Windows.Automation.AutomationElement]$Element)

    $pattern = $null
    if ($Element.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$pattern)) {
        return ([System.Windows.Automation.ValuePattern]$pattern).Current.Value
    }
    $pattern = $null
    if ($Element.TryGetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern, [ref]$pattern)) {
        return ([System.Windows.Automation.TextPattern]$pattern).DocumentRange.GetText(-1)
    }
    return [string]$Element.Current.Name
}

function Set-W4UiaValue {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][System.Windows.Automation.AutomationElement]$Element,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value
    )

    $pattern = $null
    if (-not $Element.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$pattern)) {
        throw "Element does not support UIA ValuePattern: $($Element.Current.AutomationId)"
    }
    $valuePattern = [System.Windows.Automation.ValuePattern]$pattern
    if ($valuePattern.Current.IsReadOnly) {
        throw "Element is read-only: $($Element.Current.AutomationId)"
    }
    $valuePattern.SetValue($Value)
}

function Invoke-W4UiaElement {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][System.Windows.Automation.AutomationElement]$Element)

    $pattern = $null
    if (-not $Element.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
        throw "Element does not support UIA InvokePattern: $($Element.Current.AutomationId)"
    }
    ([System.Windows.Automation.InvokePattern]$pattern).Invoke()
}

function Select-W4UiaItemByName {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][System.Windows.Automation.AutomationElement]$Root,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $condition = [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::NameProperty,
        $Name
    )
    $matches = $Root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
    if ($matches.Count -ne 1) {
        throw "UIA Name must match exactly one element; name=$Name count=$($matches.Count)"
    }
    $element = $matches.Item(0)
    $pattern = $null
    if (-not $element.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$pattern)) {
        throw "Element does not support UIA SelectionItemPattern: $Name"
    }
    ([System.Windows.Automation.SelectionItemPattern]$pattern).Select()
    return $element
}

function Wait-W4UiaText {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][System.Windows.Automation.AutomationElement]$Element,
        [Parameter(Mandatory = $true)][string[]]$Expected,
        [ValidateRange(1, 180)][int]$TimeoutSeconds = 30,
        [switch]$Prefix
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $last = ''
    do {
        $last = Get-W4UiaText -Element $Element
        foreach ($candidate in $Expected) {
            if (($Prefix -and $last.StartsWith($candidate, [System.StringComparison]::Ordinal)) -or
                (-not $Prefix -and $last -eq $candidate)) {
                return $last
            }
        }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "UIA text did not reach expected value. actual=$last expected=$($Expected -join ' | ')"
}

function Export-W4UiaTree {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][System.Windows.Automation.AutomationElement]$Root,
        [Parameter(Mandatory = $true)][string]$Path
    )

    $items = [System.Collections.Generic.List[object]]::new()
    $all = $Root.FindAll(
        [System.Windows.Automation.TreeScope]::Subtree,
        [System.Windows.Automation.Condition]::TrueCondition
    )
    for ($index = 0; $index -lt $all.Count; $index += 1) {
        $element = $all.Item($index)
        try {
            $items.Add([ordered]@{
                automation_id = [string]$element.Current.AutomationId
                name = [string]$element.Current.Name
                control_type = [string]$element.Current.ControlType.ProgrammaticName
                enabled = [bool]$element.Current.IsEnabled
                offscreen = [bool]$element.Current.IsOffscreen
            })
        } catch [System.Windows.Automation.ElementNotAvailableException] {
            $items.Add([ordered]@{ unavailable = $true })
        }
    }
    Write-W4JsonFile -Path $Path -Value @($items)
}

function Save-W4Screenshot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][System.Windows.Automation.AutomationElement]$Element,
        [Parameter(Mandatory = $true)][int]$ProcessId
    )

    try {
        Add-Type -AssemblyName System.Drawing -ErrorAction Stop
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
        if ([string]$Element.Current.AutomationId -ne 'pkv_main_window') {
            throw "screenshot target is not pkv_main_window: $($Element.Current.AutomationId)"
        }
        if ([int]$Element.Current.ProcessId -ne $ProcessId) {
            throw "screenshot target PID mismatch: expected=$ProcessId actual=$($Element.Current.ProcessId)"
        }
        if ([bool]$Element.Current.IsOffscreen) {
            throw 'screenshot target pkv_main_window is offscreen'
        }
        $uiaBounds = $Element.Current.BoundingRectangle
        if ([double]::IsNaN($uiaBounds.X) -or [double]::IsNaN($uiaBounds.Y) -or
            [double]::IsInfinity($uiaBounds.X) -or [double]::IsInfinity($uiaBounds.Y)) {
            throw 'screenshot target returned invalid UIA bounds'
        }
        $left = [int][Math]::Floor($uiaBounds.Left)
        $top = [int][Math]::Floor($uiaBounds.Top)
        $right = [int][Math]::Ceiling($uiaBounds.Right)
        $bottom = [int][Math]::Ceiling($uiaBounds.Bottom)
        $width = $right - $left
        $height = $bottom - $top
        if ($width -le 1 -or $height -le 1) {
            throw "screenshot target has no drawable area: ${width}x${height}"
        }
        $virtualScreen = [System.Windows.Forms.SystemInformation]::VirtualScreen
        if ($left -lt $virtualScreen.Left -or $top -lt $virtualScreen.Top -or
            $right -gt $virtualScreen.Right -or $bottom -gt $virtualScreen.Bottom) {
            throw 'screenshot target bounds extend outside the Windows virtual screen'
        }
        [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($Path))
        $bitmap = [System.Drawing.Bitmap]::new(
            $width,
            $height,
            [System.Drawing.Imaging.PixelFormat]::Format32bppArgb
        )
        try {
            $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
            try {
                $graphics.CopyFromScreen($left, $top, 0, 0, $bitmap.Size)
            } finally {
                $graphics.Dispose()
            }
            $pixelRectangle = [System.Drawing.Rectangle]::new(0, 0, $width, $height)
            $bitmapData = $bitmap.LockBits(
                $pixelRectangle,
                [System.Drawing.Imaging.ImageLockMode]::ReadOnly,
                [System.Drawing.Imaging.PixelFormat]::Format32bppArgb
            )
            try {
                $byteCount = [Math]::Abs($bitmapData.Stride) * $height
                $pixels = [byte[]]::new($byteCount)
                [System.Runtime.InteropServices.Marshal]::Copy($bitmapData.Scan0, $pixels, 0, $byteCount)
                $varied = $false
                for ($pixel = 4; $pixel -lt $pixels.Length; $pixel += 4) {
                    if ($pixels[$pixel] -ne $pixels[0] -or
                        $pixels[$pixel + 1] -ne $pixels[1] -or
                        $pixels[$pixel + 2] -ne $pixels[2]) {
                        $varied = $true
                        break
                    }
                }
                if (-not $varied) {
                    throw 'screenshot target produced an empty single-color image'
                }
            } finally {
                $bitmap.UnlockBits($bitmapData)
            }
            $bitmap.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
        } finally {
            $bitmap.Dispose()
        }
    } catch {
        throw "Application-window screenshot capture failed: $($_.Exception.Message)"
    }
}

function Invoke-W4SqliteStatement {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$DatabasePath,
        [Parameter(Mandatory = $true)][string]$Sql
    )

    if (-not ('PkvW4.NativeSqlite' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

namespace PkvW4 {
    public static class NativeSqlite {
        [DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl, CharSet = CharSet.Ansi)]
        private static extern int sqlite3_open_v2(string filename, out IntPtr db, int flags, string zvfs);
        [DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl, CharSet = CharSet.Ansi)]
        private static extern int sqlite3_exec(IntPtr db, string sql, IntPtr callback, IntPtr arg, out IntPtr error);
        [DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
        private static extern int sqlite3_close(IntPtr db);
        [DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
        private static extern void sqlite3_free(IntPtr value);

        public static void Execute(string path, string sql) {
            IntPtr db;
            const int READWRITE = 0x00000002;
            int rc = sqlite3_open_v2(path, out db, READWRITE, null);
            if (rc != 0) throw new InvalidOperationException("sqlite3_open_v2 rc=" + rc);
            try {
                IntPtr error;
                rc = sqlite3_exec(db, sql, IntPtr.Zero, IntPtr.Zero, out error);
                if (rc != 0) {
                    string message = error == IntPtr.Zero ? "" : Marshal.PtrToStringAnsi(error);
                    if (error != IntPtr.Zero) sqlite3_free(error);
                    throw new InvalidOperationException("sqlite3_exec rc=" + rc + " message=" + message);
                }
            } finally {
                sqlite3_close(db);
            }
        }
    }
}
'@ -ErrorAction Stop
    }
    [PkvW4.NativeSqlite]::Execute([System.IO.Path]::GetFullPath($DatabasePath), $Sql)
}

Export-ModuleMember -Function @(
    'Assert-W4DisjointPaths',
    'Assert-W4JsonObjectFields',
    'Assert-W4SafePathChain',
    'ConvertFrom-W4StrictJsonText',
    'Export-W4UiaTree',
    'Get-W4CanonicalJsonSha256',
    'Get-W4FileSha256',
    'Get-W4FileSegmentSha256',
    'Get-W4StringSha256',
    'Get-W4TreeManifest',
    'Get-W4TreeSha256',
    'Get-W4UiaElementById',
    'Get-W4UiaMainWindow',
    'Get-W4UiaText',
    'Initialize-W4Uia',
    'Invoke-W4McpRequest',
    'Invoke-W4Process',
    'Invoke-W4SqliteStatement',
    'Invoke-W4UiaElement',
    'New-W4IsolatedEnvironment',
    'Read-W4JsonFile',
    'Save-W4Screenshot',
    'Select-W4UiaItemByName',
    'Set-W4UiaValue',
    'Start-W4LongRunningProcess',
    'Start-W4McpSession',
    'Stop-W4McpSession',
    'Stop-W4ProcessTree',
    'Test-W4PathContainedBy',
    'Wait-W4UiaText',
    'Write-W4JsonFile'
)
