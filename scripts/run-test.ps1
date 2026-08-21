<#
.SYNOPSIS
在隔离的测试数据目录中运行 PKV CLI 或显式的直接测试命令。

.DESCRIPTION
生产数据的备份/恢复、迁移与已停用的原始维护写脚本不能通过此包装器运行。
它们不是当前的 inspect → plan → confirm → execute lifecycle；仅隔离 DATA_DIR
也不能把它们变成受支持的写入入口。

.EXAMPLE
.\scripts\run-test.ps1 stats

.EXAMPLE
.\scripts\run-test.ps1 -DataRoot .data-test\archive-smoke archive "https://example.com"

.EXAMPLE
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\unit-text -Command @("python", "-m", "pytest", "tests\unit\test_text_utils.py", "-q")
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

function Get-InvocationCommandName {
    param([Parameter(Mandatory = $true)][string]$Value)

    $name = [System.IO.Path]::GetFileName($Value).ToLowerInvariant()
    foreach ($extension in @(".exe", ".cmd", ".bat", ".com")) {
        if ($name.EndsWith($extension, [System.StringComparison]::Ordinal)) {
            return $name.Substring(0, $name.Length - $extension.Length)
        }
    }
    return $name
}

function Test-IsPytestInvocation {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    if ($Arguments.Count -eq 0) {
        return $false
    }
    $commandName = Get-InvocationCommandName $Arguments[0]
    $isPythonLauncher = $commandName -match '^(?:pyw?|python[0-9.w]*|pypy[0-9.w]*)$'
    return (
        $commandName -in @("pytest", "py.test") -or
        (
            $isPythonLauncher -and
            $Arguments.Count -ge 3 -and
            $Arguments[1] -eq "-m" -and
            $Arguments[2] -eq "pytest"
        )
    )
}

function Test-IsPythonInvocation {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    if ($Arguments.Count -eq 0) {
        return $false
    }
    $commandName = Get-InvocationCommandName $Arguments[0]
    return $commandName -match '^(?:pyw?|python[0-9.w]*|pypy[0-9.w]*)$'
}

function Test-HasPytestConfigBypass {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    foreach ($argument in $Arguments) {
        $lower = $argument.ToLowerInvariant()
        if ($lower -in @(
            "--noconftest",
            "--confcutdir",
            "--rootdir",
            "-c",
            "--config-file",
            "-o",
            "--override-ini",
            "--pyargs",
            "--doctest-modules",
            "--doctest-glob",
            "--collect-in-virtualenv",
            "-p",
            "--plugins",
            "--basetemp",
            "--junitxml",
            "--junit-xml",
            "--log-file",
            "--result-log",
            "--html",
            "--json-report-file",
            "--ignore",
            "--ignore-glob",
            "--deselect",
            "--cov-config"
        )) {
            return $true
        }
        if ($lower -match '^-c(?:=|.+)$' -or $lower -match '^-o(?:=|.+)$') {
            return $true
        }
        foreach ($prefix in @(
            "--confcutdir=",
            "--rootdir=",
            "--config-file=",
            "--override-ini=",
            "--pyargs=",
            "--doctest-glob=",
            "--plugins=",
            "--basetemp=",
            "--junitxml=",
            "--junit-xml=",
            "--log-file=",
            "--result-log=",
            "--html=",
            "--json-report-file=",
            "--ignore=",
            "--ignore-glob=",
            "--deselect=",
            "--cov-config="
        )) {
            if ($lower.StartsWith($prefix, [System.StringComparison]::Ordinal)) {
                return $true
            }
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

# Direct Python is a deliberately narrow offline-test seam, not a general
# repository script runner.  Keep the supported targets here as an auditable
# allowlist so a newly added maintenance/build helper cannot obtain FT7 merely
# by living under the checkout.  Script paths are compared lexically before
# any test-runtime directory is created; do not resolve or probe caller input.
$directPythonModuleAllowlist = @(
    "src.cli.commands",
    "src.mcp.server",
    "src.utils.verify_setup",
    # The fixed MCP quality evaluation is an offline test lane.  Its own
    # parser separately constrains task/proposal inputs and output to runtime
    # isolated roots.
    "evals.mcp_quality"
)
$directPythonScriptAllowlist = @(
    @(
        "scripts/setup-test-db.py",
        "scripts/rebuild-dev-vault.py",
        "scripts/check_chunk_index_consistency.py",
        "src/utils/verify_setup.py",
        # Test-only probe used by this wrapper's behavioral contract tests.
        # This is intentionally not a wildcard for arbitrary tests/ scripts.
        "tests/fixtures/offline_direct_probe.py"
    ) | ForEach-Object {
        Get-NormalizedFullPath (Join-Path $ProjectRoot $_)
    }
)

function Test-DirectPythonTargetIsAllowlisted {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    # The caller has already established the invocation is Python and that a
    # target follows -m when applicable.  Module spelling is an exact Python
    # import contract; scripts use the lexical project-root form above so
    # equivalent relative/absolute spellings remain supported without probing
    # or following the target path.
    if ($Arguments[1] -eq "-m") {
        foreach ($allowedModule in $directPythonModuleAllowlist) {
            if ($Arguments[2] -ceq $allowedModule) {
                return $true
            }
        }
        return $false
    }

    try {
        $candidate = if ([System.IO.Path]::IsPathRooted($Arguments[1])) {
            Get-NormalizedFullPath $Arguments[1]
        } else {
            Get-NormalizedFullPath (Join-Path $ProjectRoot $Arguments[1])
        }
    } catch {
        return $false
    }
    foreach ($allowedScript in $directPythonScriptAllowlist) {
        if ($candidate.Equals($allowedScript, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    return $false
}

function Test-PathWithinRequestedTestRoot {
    param(
        [Parameter(Mandatory = $true)][string]$Value,
        [switch]$AllowStdout
    )

    if ($AllowStdout -and $Value -eq "-") {
        return $true
    }
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $false
    }
    try {
        $candidate = if ([System.IO.Path]::IsPathRooted($Value)) {
            Get-NormalizedFullPath $Value
        } else {
            Get-NormalizedFullPath (Join-Path $ProjectRoot $Value)
        }
    } catch {
        return $false
    }
    $prefix = $RequestedDataRoot + [System.IO.Path]::DirectorySeparatorChar
    return (
        $candidate -eq $RequestedDataRoot -or
        $candidate.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
    )
}

function Test-ChunkConsistencyInvocationIsContained {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $pathOptions = @("--db-path", "--vector-dir", "--report-json")
    # argparse accepts unambiguous long-option abbreviations by default.  The
    # wrapper must not accept one that it cannot prove is contained: otherwise
    # ``--db-p C:\\real\\vault.db`` reaches the script as ``--db-path`` after
    # this preflight and can read outside the selected test root.
    $abbreviationPrefixes = [ordered]@{
        "--db"     = "--db-path"
        "--vector" = "--vector-dir"
        "--report" = "--report-json"
    }
    for ($index = 0; $index -lt $Arguments.Count; $index++) {
        $argument = $Arguments[$index]
        foreach ($prefix in $abbreviationPrefixes.Keys) {
            $option = $abbreviationPrefixes[$prefix]
            if (
                $argument.StartsWith($prefix, [System.StringComparison]::Ordinal) -and
                $argument -ne $option -and
                -not $argument.StartsWith($option + "=", [System.StringComparison]::Ordinal)
            ) {
                return $false
            }
        }
        foreach ($option in $pathOptions) {
            $value = $null
            if ($argument -eq $option) {
                if ($index + 1 -ge $Arguments.Count) {
                    return $false
                }
                $value = $Arguments[$index + 1]
            } elseif ($argument.StartsWith($option + "=", [System.StringComparison]::Ordinal)) {
                $value = $argument.Substring($option.Length + 1)
            }
            if ($null -ne $value -and -not (Test-PathWithinRequestedTestRoot -Value $value -AllowStdout:($option -eq "--report-json"))) {
                return $false
            }
        }
    }
    return $true
}

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
        "请由用户直接编辑 %USERPROFILE%\\.pkv\\config.yaml"
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

# Historical raw maintenance entrypoints are not test helpers.  Their module
# level fixture seams may still be imported by pytest, but a direct script
# invocation must fail before this wrapper creates an isolated runtime layout.
$blockedLegacyMaintenanceEntrypoints = @(
    "backfill_chunks.py",
    "scripts.backfill_chunks",
    "backfill_relations.py",
    "scripts.backfill_relations",
    "init_db.py",
    "scripts.init_db",
    # These historical audit/check scripts accept caller-selected Vault/SQLite
    # paths.  Even under the offline entrypoint they could read outside this
    # run's data root before any test assertion executes.
    "audit_rearchive_urls.py",
    "scripts.audit_rearchive_urls"
)
foreach ($entrypoint in $blockedLegacyMaintenanceEntrypoints) {
    if ($Direct -and (Test-CommandMentionsText -Arguments $normalizedCommand -Text $entrypoint)) {
        [Console]::Error.WriteLine(
            "测试包装器禁止已停用的 legacy maintenance 入口: $entrypoint"
        )
        exit 2
    }
}

$isChunkConsistencyCommand = (
    Test-CommandMentionsText -Arguments $normalizedCommand -Text "check_chunk_index_consistency.py"
) -or (
    Test-CommandMentionsText -Arguments $normalizedCommand -Text "scripts.check_chunk_index_consistency"
)
if (
    $Direct -and
    $isChunkConsistencyCommand -and
    -not (Test-ChunkConsistencyInvocationIsContained -Arguments $normalizedCommand)
) {
    [Console]::Error.WriteLine(
        "测试包装器拒绝 consistency checker 的外部路径；仅允许当前 DataRoot 内的输入/报告或 stdout"
    )
    exit 2
}

$isMigrationCommand = (
    (Test-CommandMentionsText -Arguments $normalizedCommand -Text "migrate.py") -or
    (Test-CommandMentionsText -Arguments $normalizedCommand -Text "scripts.migrate")
)
if ($Direct -and $isMigrationCommand) {
    [Console]::Error.WriteLine(
        "测试包装器禁止 migration：脚本尚未接入 base-only 配置入口"
    )
    exit 2
}

$isDirectPytest = $Direct -and (
    Test-IsPytestInvocation -Arguments $normalizedCommand
)
$isDirectPython = $Direct -and (
    Test-IsPythonInvocation -Arguments $normalizedCommand
)
if ($isDirectPython -and -not $isDirectPytest -and $normalizedCommand.Count -lt 2) {
    [Console]::Error.WriteLine(
        "错误: Direct Python 必须提供仓库内的 -m module 或 script.py 目标"
    )
    exit 2
}
if ($isDirectPython -and -not $isDirectPytest) {
    $directPythonMode = $normalizedCommand[1]
    if (
        $directPythonMode -eq "-c" -or
        $directPythonMode -eq "-" -or
        ($directPythonMode.StartsWith("-") -and $directPythonMode -ne "-m")
    ) {
        [Console]::Error.WriteLine(
            "错误: Direct Python 仅允许仓库内的 -m module 或 script.py 目标"
        )
        exit 2
    }
    if ($directPythonMode -eq "-m" -and $normalizedCommand.Count -lt 3) {
        [Console]::Error.WriteLine("错误: Direct Python -m 必须提供模块名")
        exit 2
    }
    if (-not (Test-DirectPythonTargetIsAllowlisted -Arguments $normalizedCommand)) {
        [Console]::Error.WriteLine(
            "错误: Direct Python 目标不在显式离线测试白名单中"
        )
        exit 2
    }
}
if ($isDirectPytest -and (Test-HasPytestConfigBypass -Arguments $normalizedCommand)) {
    [Console]::Error.WriteLine(
        "测试包装器禁止改变 pytest 的 root conftest/config 边界"
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
$profileRoot = Join-Path $RequestedDataRoot "profile"
$profilePaths = [ordered]@{
    HOME           = $profileRoot
    USERPROFILE    = $profileRoot
    APPDATA        = Join-Path $profileRoot "AppData\Roaming"
    LOCALAPPDATA   = Join-Path $profileRoot "AppData\Local"
    XDG_CONFIG_HOME = Join-Path $profileRoot ".config"
    XDG_CACHE_HOME  = Join-Path $profileRoot ".cache"
    XDG_DATA_HOME   = Join-Path $profileRoot ".local\share"
}
$managedEnvironment = [ordered]@{}
foreach ($key in $runtimePaths.Keys) {
    $managedEnvironment[$key] = $runtimePaths[$key]
}
$managedEnvironment["COVERAGE_FILE"] = Join-Path $RequestedDataRoot "reports\.coverage"
$managedEnvironment["TEMP"] = $runtimePaths.TMP_DIR
$managedEnvironment["TMP"] = $runtimePaths.TMP_DIR
$managedEnvironment["TMPDIR"] = $runtimePaths.TMP_DIR
foreach ($key in $profilePaths.Keys) {
    $managedEnvironment[$key] = $profilePaths[$key]
}
$managedEnvironment["PYTHONDONTWRITEBYTECODE"] = "1"
$managedEnvironment["PYTHONNOUSERSITE"] = "1"
$managedEnvironment["PYTEST_ADDOPTS"] = "--strict-markers"
$managedEnvironment["PYTEST_PLUGINS"] = ""
$managedEnvironment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = ""
$managedEnvironment["PKV_RUN_LIVE"] = "0"
$managedEnvironment["PKV_TEST_OFFLINE"] = "1"
$managedEnvironment["PKV_TEST_LOAD_LOCAL"] = "0"
$managedEnvironment["PKV_TEST_PROJECT_ROOT"] = $ProjectRoot
# ``PKV_DATA_ROOT`` is a formal product override, so merely setting legacy
# ``DATA_DIR`` is insufficient: an inherited user value would otherwise win
# while Config is constructed before the child can assert its isolated paths.
# Bind it to this exact accepted .data-test root before starting any Python.
$managedEnvironment["PKV_DATA_ROOT"] = $RequestedDataRoot

# Python can execute .pth/sitecustom and coverage startup hooks before the
# offline entrypoint gets control.  Remove every inherited path/config knob
# that can redirect those hooks or their output before conda starts Python.
foreach ($key in @(
    "COVERAGE_PROCESS_START",
    "COVERAGE_PROCESS_CONFIG",
    "COVERAGE_RCFILE",
    "COVERAGE_FORCE_CONFIG",
    "COVERAGE_DEBUG",
    "COVERAGE_DEBUG_FILE",
    "COV_CORE_SOURCE",
    "COV_CORE_CONFIG",
    "COV_CORE_DATAFILE",
    "COV_CORE_BRANCH",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONINSPECT",
    "PYTHONWARNINGS",
    "PYTHONUSERBASE"
)) {
    $managedEnvironment[$key] = $null
}

$previousValues = @{}
foreach ($key in $managedEnvironment.Keys) {
    $previousValues[$key] = [Environment]::GetEnvironmentVariable($key, "Process")
    [Environment]::SetEnvironmentVariable($key, $managedEnvironment[$key], "Process")
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
        $runtimePaths.TMP_DIR,
        $profilePaths.HOME,
        $profilePaths.APPDATA,
        $profilePaths.LOCALAPPDATA,
        $profilePaths.XDG_CONFIG_HOME,
        $profilePaths.XDG_CACHE_HOME,
        $profilePaths.XDG_DATA_HOME,
        (Join-Path $RequestedDataRoot "reports")
    )) {
        [void][System.IO.Directory]::CreateDirectory($path)
        if (-not (Test-Path -LiteralPath $path -PathType Container)) {
            throw "测试数据目录创建失败: $path"
        }
    }
    Assert-NoUnsafeLinksUnderPath -Path $RequestedDataRoot

    $invocation = if ($isDirectPytest) {
        $commandName = Get-InvocationCommandName $Command[0]
        $argumentStart = if ($commandName -in @("pytest", "py.test")) { 1 } else { 3 }
        $pytestArguments = if ($Command.Count -gt $argumentStart) {
            @($Command[$argumentStart..($Command.Count - 1)])
        } else {
            @()
        }
        @("python", "-m", "pytest") + $pytestArguments
    } elseif ($Direct -and $isDirectPython) {
        @("python", "tests/offline_entrypoint.py", "python") + @(
            $Command[1..($Command.Count - 1)]
        )
    } elseif ($Direct) {
        [string[]]$Command.Clone()
    } else {
        @("python", "tests/offline_entrypoint.py", "cli") + $Command
    }

    if ($Direct -and (Test-IsPytestInvocation -Arguments $invocation)) {
        $pytestBaseTemp = Join-Path $RequestedDataRoot "tmp\pytest"
        $pytestCache = Join-Path $RequestedDataRoot "tmp\pytest-cache"
        # Insert trusted values before pytest's option terminator so config,
        # inherited addopts, or earlier arguments cannot redirect writes.
        $trustedPytestArguments = @(
            "--basetemp=$pytestBaseTemp",
            "-o",
            "cache_dir=$pytestCache"
        )
        $terminatorIndex = [Array]::IndexOf($invocation, "--")
        if ($terminatorIndex -ge 0) {
            $beforeTerminator = @($invocation[0..($terminatorIndex - 1)])
            $fromTerminator = @(
                $invocation[$terminatorIndex..($invocation.Count - 1)]
            )
            $invocation = @(
                $beforeTerminator + $trustedPytestArguments + $fromTerminator
            )
        } else {
            $invocation += $trustedPytestArguments
        }
    }

    if ($isDirectPytest) {
        $pytestArguments = @($invocation[3..($invocation.Count - 1)])
        $invocation = @(
            "python",
            "tests/offline_entrypoint.py",
            "pytest"
        ) + $pytestArguments
    }

    $loggedInvocation = @(Get-RedactedInvocationForLog -Arguments $invocation)
    Write-Host "[执行命令] $($loggedInvocation -join ' ')" -ForegroundColor Cyan
    & (Join-Path $PSScriptRoot "run-windows.ps1") @invocation
    $exitCode = $LASTEXITCODE
} finally {
    foreach ($key in $managedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($key, $previousValues[$key], "Process")
    }
}

if ($exitCode -eq 0) {
    Write-Host "测试完成 ✓" -ForegroundColor Green
} else {
    Write-Host "测试失败 (退出码: $exitCode)" -ForegroundColor Red
}

exit $exitCode
