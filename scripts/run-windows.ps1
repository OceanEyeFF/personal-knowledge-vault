<#
.SYNOPSIS
在 Windows 上以 UTF-8 和真实退出码运行 py311-private 中的命令。

.EXAMPLE
.\scripts\run-windows.ps1 python -m src.cli.commands stats

.EXAMPLE
.\scripts\run-windows.ps1 pytest tests/unit/test_text_utils.py -q
#>

$Environment = if ($env:PKV_CONDA_ENV) { $env:PKV_CONDA_ENV } else { "py311-private" }
$flatCommand = [System.Collections.Generic.List[string]]::new()
$commandArgumentInvalid = $false
function Add-CommandArgument {
    param(
        [AllowNull()][object]$Value,
        [int]$Depth = 0
    )

    if ($Depth -ge 8 -or $Value -is [System.Collections.IDictionary]) {
        $script:commandArgumentInvalid = $true
        return
    }
    if (
        $Value -is [System.Collections.IEnumerable] -and
        $Value -isnot [string]
    ) {
        foreach ($item in $Value) {
            Add-CommandArgument -Value $item -Depth ($Depth + 1)
        }
        return
    }
    [void]$flatCommand.Add([string]$Value)
}
foreach ($argument in $args) {
    Add-CommandArgument $argument
}
$Command = [string[]]$flatCommand.ToArray()
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

function Write-Utf8ErrorAndExit {
    param(
        [Parameter(Mandatory = $true)][string]$Message,
        [Parameter(Mandatory = $true)][int]$ExitCode
    )

    $previousInputEncoding = [Console]::InputEncoding
    $previousOutputEncoding = [Console]::OutputEncoding
    try {
        $utf8Encoding = [System.Text.UTF8Encoding]::new()
        [Console]::InputEncoding = $utf8Encoding
        [Console]::OutputEncoding = $utf8Encoding
        [Console]::Error.WriteLine($Message)
    } finally {
        [Console]::InputEncoding = $previousInputEncoding
        [Console]::OutputEncoding = $previousOutputEncoding
    }
    exit $ExitCode
}

if ($commandArgumentInvalid) {
    Write-Utf8ErrorAndExit -Message "错误: 命令参数集合无效或嵌套过深" -ExitCode 2
}

if (-not $Command -or $Command.Count -eq 0) {
    Write-Utf8ErrorAndExit -Message "错误: 请提供要运行的命令，例如: python -m src.cli.commands stats" -ExitCode 2
}

function Test-ConfigKeyTouchesSensitiveValue {
    param([Parameter(Mandatory = $true)][string]$Key)

    $Key = $Key.Trim()
    if ($Key -in @(
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "PKV_LLM_API_KEY",
        "PKV_EMBD_API_KEY"
    )) {
        return $true
    }

    foreach ($sensitiveKey in @(
        "ai.llm.api_key",
        "ai.embedding.api_key",
        "processors.zhihu.cookie"
    )) {
        if (
            $Key -eq $sensitiveKey -or
            $sensitiveKey.StartsWith("$Key.", [System.StringComparison]::OrdinalIgnoreCase) -or
            $Key.StartsWith("$sensitiveKey.", [System.StringComparison]::OrdinalIgnoreCase)
        ) {
            return $true
        }
    }

    $sensitiveParts = @(
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
    )
    foreach ($rawPart in ($Key -split '\.')) {
        $part = $rawPart.Trim().Replace('-', '_')
        $part = [regex]::Replace($part, "([A-Z]+)([A-Z][a-z])", '$1_$2')
        $part = [regex]::Replace($part, "([a-z0-9])([A-Z])", '$1_$2')
        $part = [regex]::Replace($part, "[^A-Za-z0-9_]+", "_")
        $part = [regex]::Replace($part, "_+", "_").Trim('_').ToLowerInvariant()
        $compactPart = $part.Replace('_', '')
        foreach ($marker in $sensitiveParts) {
            $compactMarker = $marker.Replace('_', '')
            if (
                $part -eq $marker -or
                $part.StartsWith($marker + "_", [System.StringComparison]::Ordinal) -or
                $part.EndsWith("_" + $marker, [System.StringComparison]::Ordinal) -or
                $part.Contains("_" + $marker + "_") -or
                ($marker.Contains('_') -and $compactPart -eq $compactMarker)
            ) {
                return $true
            }
        }
    }
    return $false
}

$normalizedCommand = @($Command | Where-Object { $_ -ne "--" })
for ($index = 0; $index -lt ($normalizedCommand.Count - 2); $index++) {
    if (
        $normalizedCommand[$index] -eq "config" -and
        $normalizedCommand[$index + 1] -eq "set"
    ) {
        $configKey = ($normalizedCommand[$index + 2] -split "=", 2)[0]
        if (Test-ConfigKeyTouchesSensitiveValue $configKey) {
            $message = (
                "敏感配置不得作为命令行参数传入；" +
                "请直接编辑已被 Git 忽略的 config/local.yaml"
            )
            Write-Utf8ErrorAndExit -Message $message -ExitCode 2
        }
        break
    }
}

$CondaCommand = Get-Command conda.exe -ErrorAction SilentlyContinue
if (-not $CondaCommand) {
    $CondaCommand = Get-Command conda -ErrorAction SilentlyContinue
}
if (-not $CondaCommand) {
    Write-Utf8ErrorAndExit -Message "错误: 未找到 conda，请先安装 Miniconda 或 Anaconda" -ExitCode 1
}
$CondaInvocation = if ($CondaCommand.CommandType -eq "Application") {
    $CondaCommand.Source
} else {
    $CondaCommand.Name
}

$utf8 = [System.Text.UTF8Encoding]::new()
$previousConsoleInputEncoding = [Console]::InputEncoding
$previousConsoleOutputEncoding = [Console]::OutputEncoding
$previousRuntimeEnv = @{}
foreach ($key in @("PYTHONUTF8", "PYTHONIOENCODING", "PYTHONPATH")) {
    $previousRuntimeEnv[$key] = [Environment]::GetEnvironmentVariable($key, "Process")
}
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = $ProjectRoot
$CommandPayloadPath = Join-Path (
    [System.IO.Path]::GetTempPath()
) ("pkv-command-" + [Guid]::NewGuid().ToString("N") + ".json")
$CommandBridge = Join-Path $PSScriptRoot "run_conda_command.py"
$payloadEncoding = [System.Text.UTF8Encoding]::new($false)

Push-Location $ProjectRoot
$exitCode = 1
try {
    $previousErrorActionPreference = $ErrorActionPreference
    $hasNativeErrorPreference = Test-Path variable:PSNativeCommandUseErrorActionPreference
    if ($hasNativeErrorPreference) {
        $previousNativeErrorPreference = $PSNativeCommandUseErrorActionPreference
        $PSNativeCommandUseErrorActionPreference = $false
    }
    try {
        $ErrorActionPreference = "Stop"
        $commandJson = ConvertTo-Json -InputObject $Command -Compress
        [System.IO.File]::WriteAllText(
            $CommandPayloadPath,
            $commandJson,
            $payloadEncoding
        )
        # PowerShell 5.1 may promote a successful native command's stderr to a
        # terminating NativeCommandError when the caller redirects all streams.
        # Provider/runtime warnings are allowed to use stderr, so let conda's
        # real process exit code decide success after launch.
        $ErrorActionPreference = "Continue"
        $global:LASTEXITCODE = $null
        & $CondaInvocation run --no-capture-output -n $Environment `
            python $CommandBridge --args-file $CommandPayloadPath
        $commandSucceeded = $?
        if ($null -ne $LASTEXITCODE) {
            $exitCode = [int]$LASTEXITCODE
        } elseif ($commandSucceeded) {
            $exitCode = 0
        } else {
            $exitCode = 1
        }
    } catch {
        $exitCode = 1
        [Console]::Error.WriteLine(
            "错误: conda 命令启动失败，请检查 conda 安装与环境。"
        )
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
        if ($hasNativeErrorPreference) {
            $PSNativeCommandUseErrorActionPreference = $previousNativeErrorPreference
        }
    }
} finally {
    Pop-Location
    Remove-Item -LiteralPath $CommandPayloadPath -Force -ErrorAction SilentlyContinue
    foreach ($key in $previousRuntimeEnv.Keys) {
        [Environment]::SetEnvironmentVariable(
            $key,
            $previousRuntimeEnv[$key],
            "Process"
        )
    }
    [Console]::InputEncoding = $previousConsoleInputEncoding
    [Console]::OutputEncoding = $previousConsoleOutputEncoding
}

exit $exitCode
