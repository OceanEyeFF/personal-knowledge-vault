# 环境检测脚本
# 仅在固定的隔离数据目录中验证配置解析和数据库访问。

$utf8 = [System.Text.UTF8Encoding]::new()
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

$runner = Join-Path $PSScriptRoot "run-test.ps1"
$testDataRoot = ".data-test/check-environment"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " PKV 环境检测" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "[隔离测试模式]" -ForegroundColor Magenta
Write-Host "  数据目录: $testDataRoot" -ForegroundColor White
Write-Host "  说明: 不读写生产 .data" -ForegroundColor Gray
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan

# run-test.ps1 会同时覆盖六个运行期路径，并校验 canonical root
# 与 reparse point，避免受当前终端残留环境变量影响。
Write-Host "配置解析:" -ForegroundColor Yellow
Write-Host ""

& $runner -DataRoot $testDataRoot -Command @("config", "show")
$configExitCode = if ($null -eq $LASTEXITCODE) { 1 } else { [int]$LASTEXITCODE }

if ($configExitCode -ne 0) {
    Write-Host ""
    Write-Host "错误: 隔离配置验证失败（退出码: $configExitCode）" -ForegroundColor Red
    exit $configExitCode
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "数据库统计:" -ForegroundColor Yellow
Write-Host ""

& $runner -DataRoot $testDataRoot -Command @("stats")
$statsExitCode = if ($null -eq $LASTEXITCODE) { 1 } else { [int]$LASTEXITCODE }

if ($statsExitCode -ne 0) {
    Write-Host ""
    Write-Host "错误: 隔离数据库统计失败（退出码: $statsExitCode）" -ForegroundColor Red
    exit $statsExitCode
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "✓ 隔离环境验证通过" -ForegroundColor Green
Write-Host "提示: 其他测试命令继续使用 .\scripts\run-test.ps1" -ForegroundColor Cyan
Write-Host ""

exit 0
