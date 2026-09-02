<#
.SYNOPSIS
Run smoke, collection, offline, MCP coverage, or P0 preflight tests in a Windows Conda env.

.DESCRIPTION
All pytest commands use scripts/run-test.ps1, so project runtime paths and
pytest temporary paths stay below .data-test. Manual and network tests are
never selected by this script. MCP coverage is an explicit Windows suite, not
an inferred result of the P0 source-compatibility preflight.

.EXAMPLE
.\scripts\test-conda.ps1 -EnvironmentName py311-private -Suite P0
#>

[CmdletBinding()]
param(
    [Parameter()]
    [ValidatePattern("^[A-Za-z0-9][A-Za-z0-9._-]*$")]
    [string]$EnvironmentName,

    [Parameter()]
    [ValidateSet("Smoke", "Contract", "Offline", "MCP", "P0")]
    [string]$Suite = "Smoke"
)

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$envName = if ($EnvironmentName) {
    $EnvironmentName
} elseif ($env:PKV_CONDA_ENV) {
    $env:PKV_CONDA_ENV
} else {
    "py311-private"
}

$condaCommand = Get-Command conda.exe -ErrorAction SilentlyContinue
if (-not $condaCommand) {
    $condaCommand = Get-Command conda -ErrorAction SilentlyContinue
}
if (-not $condaCommand) {
    [Console]::Error.WriteLine("错误: 未找到 Conda。")
    exit 1
}
$condaInvocation = if ($condaCommand.CommandType -eq "Application") {
    $condaCommand.Source
} else {
    $condaCommand.Name
}

$versionOutput = @(
    & $condaInvocation run -n $envName python -c (
        "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    )
)
if ($LASTEXITCODE -ne 0) {
    [Console]::Error.WriteLine(
        "错误: Conda 环境 '$envName' 不存在或无法运行 Python。"
    )
    exit 1
}
$pythonMinorCandidate = (
    $versionOutput |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
        Select-Object -Last 1
)
if ([string]::IsNullOrWhiteSpace([string]$pythonMinorCandidate)) {
    [Console]::Error.WriteLine(
        "错误: Conda 环境 '$envName' 未返回 Python 版本 (empty version output)。"
    )
    exit 1
}
$pythonMinor = ([string]$pythonMinorCandidate).Trim()
if ($pythonMinor -ne "3.11") {
    [Console]::Error.WriteLine(
        "错误: P0 只支持 Python 3.11，环境 '$envName' 当前为 $pythonMinor。"
    )
    exit 1
}

function Invoke-IsolatedTestStep {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,

        [Parameter(Mandatory = $true)]
        [string]$DataRoot,

        [Parameter(Mandatory = $true)]
        [string[]]$Command,

        [Parameter()]
        [switch]$CaptureOutput
    )

    Write-Host ""
    Write-Host "[$Name]" -ForegroundColor Cyan
    $script:lastStepOutput = @()
    if ($CaptureOutput) {
        $stepOutput = @(
            & "$PSScriptRoot\run-test.ps1" `
                -Direct `
                -DataRoot $DataRoot `
                -Command $Command 2>&1
        )
        $script:lastStepExitCode = $LASTEXITCODE
        $script:lastStepOutput = @(
            $stepOutput | ForEach-Object { "$_" }
        )
        $stepOutput | ForEach-Object { Write-Output $_ }
    } else {
        & "$PSScriptRoot\run-test.ps1" `
            -Direct `
            -DataRoot $DataRoot `
            -Command $Command
        $script:lastStepExitCode = $LASTEXITCODE
    }
}

function Assert-NoUnsafeCleanupLinks {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter()]
        [switch]$Recurse
    )

    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "测试运行目录不得包含 junction 或符号链接: $Path"
    }
    $linkTypeProperty = $item.PSObject.Properties["LinkType"]
    if (
        -not $item.PSIsContainer -and
        $linkTypeProperty -and
        $item.LinkType -eq "HardLink"
    ) {
        throw "测试运行目录不得包含硬链接文件: $Path"
    }

    if ($Recurse -and $item.PSIsContainer) {
        foreach ($child in Get-ChildItem -LiteralPath $Path -Force -ErrorAction Stop) {
            Assert-NoUnsafeCleanupLinks -Path $child.FullName -Recurse
        }
    }
}

function Assert-SafeTestRunCleanup {
    param(
        [Parameter(Mandatory = $true)]
        [string]$DataRoot,

        [Parameter(Mandatory = $true)]
        [string]$RunPath,

        [Parameter(Mandatory = $true)]
        [string]$ExpectedLeaf
    )

    $normalizedDataRoot = [IO.Path]::GetFullPath($DataRoot).TrimEnd('\', '/')
    $normalizedRunPath = [IO.Path]::GetFullPath($RunPath).TrimEnd('\', '/')
    $dataRootPrefix = $normalizedDataRoot + [IO.Path]::DirectorySeparatorChar
    if (-not $normalizedRunPath.StartsWith(
        $dataRootPrefix,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "测试运行目录不在预期的 .data-test 下: $normalizedRunPath"
    }
    if ([IO.Path]::GetFileName($normalizedRunPath) -ne $ExpectedLeaf) {
        throw "测试运行目录不是预期场景: $normalizedRunPath"
    }

    Assert-NoUnsafeCleanupLinks -Path $normalizedDataRoot
    Assert-NoUnsafeCleanupLinks -Path $normalizedRunPath -Recurse
}

$exitCode = 1
$runId = [Guid]::NewGuid().ToString("N").Substring(0, 8)
$testRunRoot = ".data-test\conda-$runId"
$testDataRoot = [IO.Path]::GetFullPath(
    (Join-Path $projectRoot ".data-test")
)
$testRunPath = [IO.Path]::GetFullPath(
    (Join-Path $projectRoot $testRunRoot)
)
$testDataPrefix = $testDataRoot + [IO.Path]::DirectorySeparatorChar
if (-not $testRunPath.StartsWith(
    $testDataPrefix,
    [StringComparison]::OrdinalIgnoreCase
)) {
    [Console]::Error.WriteLine(
        "错误: 测试运行目录不在预期的 .data-test 下: $testRunPath"
    )
    exit 1
}

$previousEnvironmentName = $env:PKV_CONDA_ENV
$previousRunLive = $env:PKV_RUN_LIVE
$previousArchiveUrl = $env:PKV_E2E_ARCHIVE_URL
$previousBytecodeSetting = $env:PYTHONDONTWRITEBYTECODE
$locationPushed = $false
try {
    $env:PKV_CONDA_ENV = $envName
    $env:PKV_RUN_LIVE = "0"
    $env:PKV_E2E_ARCHIVE_URL = ""
    $env:PYTHONDONTWRITEBYTECODE = "1"
    Push-Location $projectRoot
    $locationPushed = $true

    Write-Host "Conda 环境: $envName" -ForegroundColor Green
    Write-Host "Python 契约: 3.11" -ForegroundColor Green
    Write-Host "测试范围: $Suite" -ForegroundColor Green
    Write-Host "数据根目录: $testRunRoot" -ForegroundColor Green
    Write-Host "manual/network: 禁止" -ForegroundColor Green

    & $condaInvocation run --no-capture-output -n $envName `
        python -m pip check
    if ($LASTEXITCODE -ne 0) {
        throw "pip check 发现依赖冲突。"
    }

    if ($Suite -eq "Smoke") {
        Invoke-IsolatedTestStep `
            -Name "环境 smoke" `
            -DataRoot "$testRunRoot\smoke" `
            -Command @(
                "python",
                "-m",
                "pytest",
                "tests\test_basic_syntax.py",
                "-m",
                "not manual and not network and not artifact and not windows_release_env",
                "-q"
            )
        if ($lastStepExitCode -ne 0) {
            $exitCode = $lastStepExitCode
            throw "环境 smoke 失败。"
        }
    }

    if ($Suite -in @("Contract", "P0")) {
        Invoke-IsolatedTestStep `
            -Name "默认收集契约" `
            -DataRoot "$testRunRoot\collect" `
            -Command @(
                "python",
                "-m",
                "pytest",
                "--collect-only",
                "-q"
            ) `
            -CaptureOutput
        if ($lastStepExitCode -ne 0) {
            $exitCode = $lastStepExitCode
            throw "默认收集契约失败。"
        }
        $manualNodes = @(
            $lastStepOutput |
                Select-String -SimpleMatch "manual_test"
        )
        if ($manualNodes.Count -gt 0) {
            $exitCode = 1
            throw "默认收集包含 manual_test 节点。"
        }
    }

    if ($Suite -eq "MCP") {
        Invoke-IsolatedTestStep `
            -Name "MCP 覆盖率门禁" `
            -DataRoot "$testRunRoot\mcp-coverage" `
            -Command @(
                "python",
                "-m",
                "pytest",
                "tests\unit\test_mcp_citation_url_security.py",
                "tests\unit\test_mcp_coverage_contract.py",
                "tests\unit\test_mcp_prompts.py",
                "tests\unit\test_mcp_quality_scorer.py",
                "tests\unit\test_mcp_resources.py",
                "tests\unit\test_mcp_security.py",
                "tests\unit\test_mcp_server_w2.py",
                "tests\unit\test_mcp_tools.py",
                "tests\integration\test_mcp_client_simulation.py",
                "tests\integration\test_mcp_functional.py",
                "tests\integration\test_mcp_integration.py",
                "tests\integration\test_mcp_quality_eval.py",
                "tests\integration\test_mcp_ssrf_zero_write.py",
                "tests\blackbox\test_mcp_blackbox.py",
                "tests\blackbox\test_r4_mcp_fullflow.py",
                "tests\e2e\test_mcp_e2e_archive.py",
                "tests\e2e\test_mcp_e2e_knowledge_qa.py",
                "tests\e2e\test_mcp_e2e_search.py",
                "-m",
                "not manual and not network and not artifact and not windows_release_env",
                "--cov=src.mcp",
                "--cov-report=term-missing",
                "--cov-fail-under=95",
                "-q"
            )
        if ($lastStepExitCode -ne 0) {
            $exitCode = $lastStepExitCode
            throw "MCP 覆盖率门禁失败。"
        }
    }

    if ($Suite -in @("Offline", "P0")) {
        Invoke-IsolatedTestStep `
            -Name "完整离线套件" `
            -DataRoot "$testRunRoot\offline" `
            -Command @(
                "python",
                "-m",
                "pytest",
                "-m",
                "not manual and not network and not artifact and not windows_release_env",
                "-q"
            )
        if ($lastStepExitCode -ne 0) {
            $exitCode = $lastStepExitCode
            throw "完整离线套件失败。"
        }
    }

    $exitCode = 0
    Write-Host ""
    Write-Host "Conda 测试通过。" -ForegroundColor Green
} catch {
    [Console]::Error.WriteLine("错误: $($_.Exception.Message)")
} finally {
    if ($locationPushed) {
        Pop-Location
    }
    if ($null -eq $previousEnvironmentName) {
        Remove-Item Env:PKV_CONDA_ENV -ErrorAction SilentlyContinue
    } else {
        $env:PKV_CONDA_ENV = $previousEnvironmentName
    }
    if ($null -eq $previousRunLive) {
        Remove-Item Env:PKV_RUN_LIVE -ErrorAction SilentlyContinue
    } else {
        $env:PKV_RUN_LIVE = $previousRunLive
    }
    if ($null -eq $previousArchiveUrl) {
        Remove-Item Env:PKV_E2E_ARCHIVE_URL -ErrorAction SilentlyContinue
    } else {
        $env:PKV_E2E_ARCHIVE_URL = $previousArchiveUrl
    }
    if ($null -eq $previousBytecodeSetting) {
        Remove-Item Env:PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue
    } else {
        $env:PYTHONDONTWRITEBYTECODE = $previousBytecodeSetting
    }
    if ($exitCode -eq 0 -and (Test-Path -LiteralPath $testRunPath)) {
        try {
            Assert-SafeTestRunCleanup `
                -DataRoot $testDataRoot `
                -RunPath $testRunPath `
                -ExpectedLeaf "conda-$runId"
            Remove-Item `
                -LiteralPath $testRunPath `
                -Recurse `
                -Force `
                -ErrorAction Stop
            Write-Host "已清理测试运行目录: $testRunPath" `
                -ForegroundColor Green
        } catch {
            $exitCode = 1
            [Console]::Error.WriteLine(
                "错误: 无法清理测试运行目录: $($_.Exception.Message)"
            )
            Write-Host "保留失败诊断目录: $testRunPath" `
                -ForegroundColor Yellow
        }
    } elseif ($exitCode -ne 0) {
        Write-Host "保留失败诊断目录: $testRunPath" `
            -ForegroundColor Yellow
    }
}

exit $exitCode
