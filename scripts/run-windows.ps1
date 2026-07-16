<#
.SYNOPSIS
在 Windows 上以 UTF-8 和真实退出码运行 py311-private 中的命令。

.EXAMPLE
.\scripts\run-windows.ps1 python -m src.cli.commands stats

.EXAMPLE
.\scripts\run-windows.ps1 pytest tests/unit/test_text_utils.py -q
#>

$Environment = if ($env:PKV_CONDA_ENV) { $env:PKV_CONDA_ENV } else { "py311-private" }
$Command = @($args)
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

if (-not $Command -or $Command.Count -eq 0) {
    Write-Error "请提供要运行的命令，例如: python -m src.cli.commands stats"
    exit 2
}

if (-not (Get-Command conda -ErrorAction SilentlyContinue)) {
    Write-Error "未找到 conda，请先安装 Miniconda 或 Anaconda"
    exit 1
}

$utf8 = [System.Text.UTF8Encoding]::new()
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = $ProjectRoot

Push-Location $ProjectRoot
$exitCode = 1
try {
    & conda run --no-capture-output -n $Environment @Command
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}

exit $exitCode
