<#
.SYNOPSIS
Create a clean Windows Conda environment for the offline P0 test contract.

.DESCRIPTION
The environment is always Python 3.11. This script installs dependencies and
runs pip's dependency consistency check, but it does not install Playwright
browsers, create config/local.yaml, or create/read production .data.

.EXAMPLE
.\scripts\setup-test-conda.ps1

.EXAMPLE
.\scripts\setup-test-conda.ps1 -EnvironmentName pkv-feature-py311
#>

[CmdletBinding()]
param(
    [Parameter()]
    [ValidatePattern("^[A-Za-z0-9][A-Za-z0-9._-]*$")]
    [string]$EnvironmentName = "pkv-test-py311"
)

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$environmentFile = Join-Path $projectRoot "environment.test.yml"
$requirementsFile = Join-Path $projectRoot "requirements.txt"

$condaCommand = Get-Command conda.exe -ErrorAction SilentlyContinue
if (-not $condaCommand) {
    $condaCommand = Get-Command conda -ErrorAction SilentlyContinue
}
if (-not $condaCommand) {
    [Console]::Error.WriteLine(
        "错误: 未找到 Conda，请先安装 Miniconda 或 Anaconda。"
    )
    exit 1
}
$condaInvocation = if ($condaCommand.CommandType -eq "Application") {
    $condaCommand.Source
} else {
    $condaCommand.Name
}

function Invoke-CondaCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,

        [Parameter(Mandatory = $true)]
        [string]$FailureMessage
    )

    & $condaInvocation @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw $FailureMessage
    }
}

Push-Location $projectRoot
$exitCode = 1
try {
    $envListOutput = @(& $condaInvocation env list --json)
    if ($LASTEXITCODE -ne 0) {
        throw "无法读取 Conda 环境列表。"
    }
    $envList = ($envListOutput -join [Environment]::NewLine) | ConvertFrom-Json
    $environmentExists = @(
        $envList.envs | Where-Object {
            [System.IO.Path]::GetFileName($_) -eq $EnvironmentName
        }
    ).Count -gt 0

    if ($environmentExists) {
        throw (
            "Conda 环境 '$EnvironmentName' 已存在。请换一个新名称，" +
            "或由你明确执行 conda env remove -n $EnvironmentName 后重建。"
        )
    }

    Write-Host "创建 Windows 测试环境: $EnvironmentName (Python 3.11)" -ForegroundColor Cyan
    Invoke-CondaCommand `
        -Arguments @(
            "env",
            "create",
            "--yes",
            "--name",
            $EnvironmentName,
            "--file",
            $environmentFile
        ) `
        -FailureMessage "Conda 测试环境创建失败。"

    Write-Host "安装项目依赖..." -ForegroundColor Cyan
    Invoke-CondaCommand `
        -Arguments @(
            "run",
            "--no-capture-output",
            "-n",
            $EnvironmentName,
            "python",
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-r",
            $requirementsFile
        ) `
        -FailureMessage "项目依赖安装失败。"

    Write-Host "验证 Python 与依赖契约..." -ForegroundColor Cyan
    Invoke-CondaCommand `
        -Arguments @(
            "run",
            "--no-capture-output",
            "-n",
            $EnvironmentName,
            "python",
            "-c",
            (
                "import sys; " +
                "assert sys.version_info[:2] == (3, 11), sys.version; " +
                "print(sys.version)"
            )
        ) `
        -FailureMessage "测试环境不是受支持的 Python 3.11。"
    Invoke-CondaCommand `
        -Arguments @(
            "run",
            "--no-capture-output",
            "-n",
            $EnvironmentName,
            "python",
            "-m",
            "pip",
            "check"
        ) `
        -FailureMessage "pip check 发现依赖冲突。"

    Write-Host ""
    Write-Host "测试环境已就绪: $EnvironmentName" -ForegroundColor Green
    Write-Host (
        ".\scripts\test-conda.ps1 -EnvironmentName " +
        "$EnvironmentName -Suite P0"
    ) -ForegroundColor Cyan
    Write-Host "后续 test-conda 只使用 .data-test，不创建或读取生产 .data。" -ForegroundColor Green
    $exitCode = 0
} catch {
    [Console]::Error.WriteLine("错误: $($_.Exception.Message)")
    [Console]::Error.WriteLine(
        "失败环境会保留用于诊断；脚本不会自动删除 Conda 环境。"
    )
} finally {
    Pop-Location
}

exit $exitCode
