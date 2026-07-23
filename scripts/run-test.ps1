<#
.SYNOPSIS
在隔离的测试数据目录中运行 PKV CLI 或显式的直接测试命令。

.DESCRIPTION
生产数据的备份/恢复脚本不能通过此包装器运行。数据库迁移只允许直接调用 Python，
且可写迁移必须显式传入 --no-backup，避免自动备份读取生产 .data。

.EXAMPLE
.\scripts\run-test.ps1 stats

.EXAMPLE
.\scripts\run-test.ps1 -DataRoot .data-test\archive-smoke archive "https://example.com"

.EXAMPLE
.\scripts\run-test.ps1 -Direct -Command @("pytest", "tests\unit\test_text_utils.py", "-q")

.EXAMPLE
.\scripts\run-test.ps1 -Direct -Command @("python", "scripts\migrate.py", "--auto", "--no-backup")
#>

[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter(Mandatory = $true, Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$Command,

    [Parameter()]
    [string]$DataRoot = ".data-test",

    [Parameter()]
    [switch]$Direct
)

$utf8 = [System.Text.UTF8Encoding]::new()
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

function Get-NormalizedFullPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $fullPath = [System.IO.Path]::GetFullPath($Path)
    if ($fullPath.StartsWith("\\?\", [System.StringComparison]::Ordinal)) {
        $fullPath = $fullPath.Substring(4)
        if ($fullPath.StartsWith("UNC\", [System.StringComparison]::OrdinalIgnoreCase)) {
            $fullPath = "\\" + $fullPath.Substring(4)
        }
        # 扩展长度路径可能保留父目录段；剥离设备前缀后必须再次规范化。
        $fullPath = [System.IO.Path]::GetFullPath($fullPath)
    }
    return $fullPath.TrimEnd('\', '/')
}

function Assert-NoUnsafeLinksUnderPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }

    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        [Console]::Error.WriteLine(
            "错误: 测试数据路径不得包含 junction 或符号链接: $Path"
        )
        exit 2
    }
    $linkTypeProperty = $item.PSObject.Properties["LinkType"]
    if (
        -not $item.PSIsContainer -and
        $linkTypeProperty -and
        $item.LinkType -eq "HardLink"
    ) {
        [Console]::Error.WriteLine("错误: 测试数据路径不得包含硬链接文件: $Path")
        exit 2
    }

    if ($item.PSIsContainer) {
        foreach ($child in Get-ChildItem -LiteralPath $Path -Force -ErrorAction Stop) {
            Assert-NoUnsafeLinksUnderPath -Path $child.FullName
        }
    }
}

function Test-CommandMentionsText {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Text
    )

    foreach ($argument in $Arguments) {
        if ($argument.IndexOf($Text, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
            return $true
        }
    }
    return $false
}

function Test-HasExactArgument {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Value
    )

    foreach ($argument in $Arguments) {
        if ($argument.Equals($Value, [System.StringComparison]::Ordinal)) {
            return $true
        }
    }
    return $false
}

function Test-IsDirectPythonMigration {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    if ($Arguments.Count -eq 0) {
        return $false
    }

    $executable = [System.IO.Path]::GetFileName($Arguments[0])
    $pythonExecutables = @("python", "python.exe", "python3", "python3.exe", "py", "py.exe")
    if ($pythonExecutables -notcontains $executable.ToLowerInvariant()) {
        return $false
    }

    for ($index = 1; $index -lt $Arguments.Count; $index++) {
        $argument = $Arguments[$index]
        if (
            [System.IO.Path]::GetFileName($argument).Equals(
                "migrate.py",
                [System.StringComparison]::OrdinalIgnoreCase
            )
        ) {
            return $true
        }
        if (
            $argument -eq "-m" -and
            ($index + 1) -lt $Arguments.Count -and
            $Arguments[$index + 1].Equals(
                "scripts.migrate",
                [System.StringComparison]::Ordinal
            )
        ) {
            return $true
        }
    }
    return $false
}

function Test-IsSensitiveArgumentName {
    param([Parameter(Mandatory = $true)][string]$Name)

    $normalizedName = $Name.Trim().TrimStart([char[]]@('-', '/'))
    $normalizedName = [regex]::Replace(
        $normalizedName,
        '([A-Z]+)([A-Z][a-z])',
        '$1_$2'
    )
    $normalizedName = [regex]::Replace(
        $normalizedName,
        '([a-z0-9])([A-Z])',
        '$1_$2'
    )
    $normalizedName = $normalizedName.ToLowerInvariant().Replace('-', '_').Replace('.', '_')
    foreach ($marker in @(
        "access_token",
        "api_key",
        "apikey",
        "asp_net_session_id",
        "auth",
        "auth_token",
        "authorization",
        "basic_auth",
        "bearer",
        "bearer_token",
        "client_secret",
        "cookie",
        "credential",
        "credentials",
        "connect_sid",
        "id_token",
        "key",
        "jwt",
        "jwt_token",
        "pass",
        "passcode",
        "passphrase",
        "passwd",
        "password",
        "phpsessid",
        "private_key",
        "pwd",
        "oauth_token",
        "refresh_token",
        "session_id",
        "sessionid",
        "jsessionid",
        "session_token",
        "sid",
        "secret",
        "sig",
        "signature",
        "token"
    )) {
        if (
            $normalizedName -eq $marker -or
            $normalizedName.StartsWith($marker + "_", [System.StringComparison]::Ordinal) -or
            $normalizedName.EndsWith("_" + $marker, [System.StringComparison]::Ordinal) -or
            $normalizedName.Contains("_" + $marker + "_")
        ) {
            return $true
        }
    }
    return $false
}

function Protect-ArgumentForLog {
    param([Parameter(Mandatory = $true)][string]$Value)

    # 保留 URL 结构用于调试，仅隐去常见凭据查询值和 userinfo。
    $protectedValue = [regex]::Replace(
        $Value,
        '(?i)(?<scheme>\bhttps?://)[^/@\s]+@',
        '${scheme}<redacted>@'
    )
    $urlParameterPattern = '(?<prefix>[?&#;])(?<key>[^?&#;=]+)=(?<value>[^?&#;\s]*)'
    $protectedValue = [regex]::Replace(
        $protectedValue,
        $urlParameterPattern,
        [System.Text.RegularExpressions.MatchEvaluator]{
            param($match)
            try {
                $decodedKey = [Uri]::UnescapeDataString(
                    $match.Groups["key"].Value.Replace('+', ' ')
                )
            } catch {
                $decodedKey = $match.Groups["key"].Value
            }
            if (Test-IsSensitiveArgumentName $decodedKey) {
                return (
                    $match.Groups["prefix"].Value +
                    $match.Groups["key"].Value +
                    "=<redacted>"
                )
            }
            return $match.Value
        }
    )

    $assignment = [regex]::Match(
        $protectedValue,
        '^(?<name>[-/A-Za-z_][A-Za-z0-9_.-]*)(?<separator>\s*[:=]\s*)(?<value>.*)$'
    )
    if ($assignment.Success -and (Test-IsSensitiveArgumentName $assignment.Groups["name"].Value)) {
        return (
            $assignment.Groups["name"].Value +
            $assignment.Groups["separator"].Value +
            "<redacted>"
        )
    }
    if ($assignment.Success) {
        $nestedValue = $assignment.Groups["value"].Value
        $protectedNestedValue = Protect-ArgumentForLog $nestedValue
        if ($protectedNestedValue -ne $nestedValue) {
            return (
                $assignment.Groups["name"].Value +
                $assignment.Groups["separator"].Value +
                $protectedNestedValue
            )
        }
    }

    # 兼容 --data '{"api_key":"..."}' 等单参数 JSON；配置 set 的值会在上层整体隐去。
    $jsonPattern = '"(?<name>[A-Za-z_][A-Za-z0-9_.-]*)"(?<separator>\s*:\s*)"(?<value>[^"]*)"'
    $protectedValue = [regex]::Replace(
        $protectedValue,
        $jsonPattern,
        [System.Text.RegularExpressions.MatchEvaluator]{
            param($match)
            if (Test-IsSensitiveArgumentName $match.Groups["name"].Value) {
                return (
                    '"' + $match.Groups["name"].Value + '"' +
                    $match.Groups["separator"].Value + '"<redacted>"'
                )
            }
            return $match.Value
        }
    )
    return $protectedValue
}

function Get-RedactedInvocationForLog {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $redacted = [System.Collections.Generic.List[string]]::new()
    $redactNext = $false
    $configSetValueIndex = -1
    for ($index = 0; $index -lt ($Arguments.Count - 1); $index++) {
        if ($Arguments[$index] -eq "config" -and $Arguments[$index + 1] -eq "set") {
            $configSetValueIndex = $index + 3
            break
        }
    }

    for ($index = 0; $index -lt $Arguments.Count; $index++) {
        $argument = $Arguments[$index]
        if ($redactNext -or $index -eq $configSetValueIndex) {
            [void]$redacted.Add("<redacted>")
            $redactNext = $false
            continue
        }

        # 兼容非标准 KEY=VALUE 形式的 config set，值仍整体隐去。
        if ($configSetValueIndex -eq ($index + 1) -and $argument.Contains("=")) {
            $configKey = ($argument -split "=", 2)[0]
            [void]$redacted.Add($configKey + "=<redacted>")
            continue
        }

        $protectedArgument = Protect-ArgumentForLog $argument
        [void]$redacted.Add($protectedArgument)

        $isSeparateSensitiveOption = (
            -not $argument.Contains("=") -and
            (Test-IsSensitiveArgumentName $argument)
        )
        if (
            $isSeparateSensitiveOption -or
            $argument -in @("-u", "--user", "--proxy-user", "--oauth2-bearer")
        ) {
            $redactNext = $true
        }
    }
    return $redacted.ToArray()
}

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$AllowedTestRoot = Get-NormalizedFullPath (Join-Path $ProjectRoot ".data-test")
$RequestedDataRoot = if ([System.IO.Path]::IsPathRooted($DataRoot)) {
    Get-NormalizedFullPath $DataRoot
} else {
    Get-NormalizedFullPath (Join-Path $ProjectRoot $DataRoot)
}

$allowedPrefix = $AllowedTestRoot + [System.IO.Path]::DirectorySeparatorChar
if (
    $RequestedDataRoot -ne $AllowedTestRoot -and
    -not $RequestedDataRoot.StartsWith(
        $allowedPrefix,
        [System.StringComparison]::OrdinalIgnoreCase
    )
) {
    [Console]::Error.WriteLine(
        "错误: 测试数据目录必须位于仓库的 .data-test 下: $RequestedDataRoot"
    )
    exit 2
}

$normalizedCommand = @($Command | Where-Object { $_ -ne "--" })
$isConfigSet = $false
for ($index = 0; $index -lt ($normalizedCommand.Count - 1); $index++) {
    if (
        $normalizedCommand[$index] -eq "config" -and
        $normalizedCommand[$index + 1] -eq "set"
    ) {
        $isConfigSet = $true
        break
    }
}
if ($isConfigSet) {
    [Console]::Error.WriteLine(
        "测试包装器禁止 config set：该命令会修改真实的本机配置，" +
        "请直接编辑已被 Git 忽略的 config/local.yaml"
    )
    exit 2
}

$blockedProductionScripts = @("backup-data.ps1", "backup.ps1", "restore-data.ps1")
foreach ($scriptName in $blockedProductionScripts) {
    if ($Direct -and (Test-CommandMentionsText -Arguments $normalizedCommand -Text $scriptName)) {
        [Console]::Error.WriteLine(
            "测试包装器禁止运行生产数据备份/恢复脚本: $scriptName"
        )
        exit 2
    }
}

$isMigrationCommand = (
    (Test-CommandMentionsText -Arguments $normalizedCommand -Text "migrate.py") -or
    (Test-CommandMentionsText -Arguments $normalizedCommand -Text "scripts.migrate")
)
if ($Direct -and $isMigrationCommand -and -not (Test-IsDirectPythonMigration $normalizedCommand)) {
    [Console]::Error.WriteLine(
        "测试迁移仅允许直接调用 python scripts/migrate.py 或 python -m scripts.migrate"
    )
    exit 2
}

$isReadOnlyMigration = (
    (Test-HasExactArgument -Arguments $normalizedCommand -Value "--dry-run") -or
    (Test-HasExactArgument -Arguments $normalizedCommand -Value "--version") -or
    (Test-HasExactArgument -Arguments $normalizedCommand -Value "--health-check")
)
$skipsMigrationBackup = Test-HasExactArgument `
    -Arguments $normalizedCommand `
    -Value "--no-backup"
if (
    $Direct -and
    $isMigrationCommand -and
    -not $isReadOnlyMigration -and
    -not $skipsMigrationBackup
) {
    [Console]::Error.WriteLine(
        "测试迁移必须使用 --no-backup；自动备份脚本会读取生产 .data"
    )
    exit 2
}

# 拒绝现有路径中的 junction / symlink，防止 .data-test 间接指向生产数据。
$relativeDataRoot = $RequestedDataRoot.Substring($AllowedTestRoot.Length).TrimStart('\', '/')
$pathsToCheck = @($AllowedTestRoot)
$currentPath = $AllowedTestRoot
if ($relativeDataRoot) {
    foreach ($part in $relativeDataRoot -split '[\\/]') {
        $currentPath = Join-Path $currentPath $part
        $pathsToCheck += $currentPath
    }
}
foreach ($path in $pathsToCheck) {
    if (Test-Path -LiteralPath $path) {
        $item = Get-Item -LiteralPath $path -Force
        if (-not $item.PSIsContainer) {
            [Console]::Error.WriteLine(
                "错误: 测试数据路径中的现有节点必须是目录: $path"
            )
            exit 2
        }
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            [Console]::Error.WriteLine(
                "错误: 测试数据路径不得经过 junction 或符号链接: $path"
            )
            exit 2
        }
    }
}
Assert-NoUnsafeLinksUnderPath -Path $RequestedDataRoot

$runtimePaths = [ordered]@{
    DATA_DIR   = $RequestedDataRoot
    DB_PATH    = Join-Path $RequestedDataRoot "db\knowledge_vault.db"
    VAULT_DIR  = Join-Path $RequestedDataRoot "vault"
    VECTOR_DIR = Join-Path $RequestedDataRoot "vectors"
    LOG_DIR    = Join-Path $RequestedDataRoot "logs"
    TMP_DIR    = Join-Path $RequestedDataRoot "tmp"
}
$previousValues = @{}

foreach ($key in $runtimePaths.Keys) {
    $previousValues[$key] = [Environment]::GetEnvironmentVariable($key, "Process")
    [Environment]::SetEnvironmentVariable($key, $runtimePaths[$key], "Process")
}

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " 测试环境模式 (Test Environment)" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  数据根目录: $RequestedDataRoot" -ForegroundColor Gray

$exitCode = 1
try {
    foreach ($path in @(
        (Split-Path $runtimePaths.DB_PATH -Parent),
        $runtimePaths.VAULT_DIR,
        $runtimePaths.VECTOR_DIR,
        $runtimePaths.LOG_DIR,
        $runtimePaths.TMP_DIR
    )) {
        [void][System.IO.Directory]::CreateDirectory($path)
        if (-not (Test-Path -LiteralPath $path -PathType Container)) {
            throw "测试数据目录创建失败: $path"
        }
    }
    Assert-NoUnsafeLinksUnderPath -Path $RequestedDataRoot

    $invocation = if ($Direct) {
        [string[]]$Command.Clone()
    } else {
        @("python", "-m", "src.cli.commands") + $Command
    }
    $loggedInvocation = @(Get-RedactedInvocationForLog -Arguments $invocation)
    Write-Host "[执行命令] $($loggedInvocation -join ' ')" -ForegroundColor Cyan
    & (Join-Path $PSScriptRoot "run-windows.ps1") @invocation
    $exitCode = $LASTEXITCODE
} finally {
    foreach ($key in $runtimePaths.Keys) {
        [Environment]::SetEnvironmentVariable($key, $previousValues[$key], "Process")
    }
}

if ($exitCode -eq 0) {
    Write-Host "测试完成 ✓" -ForegroundColor Green
} else {
    Write-Host "测试失败 (退出码: $exitCode)" -ForegroundColor Red
}

exit $exitCode
