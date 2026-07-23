# Personal Knowledge Vault - Conda 测试脚本 (PowerShell)
# 作者: 幽浮酱
# 用途: 在 Conda 环境中运行验证测试

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $projectRoot

Write-Host "=" -NoNewline -ForegroundColor Cyan
Write-Host ("=" * 58) -ForegroundColor Cyan
Write-Host "🧪 Personal Knowledge Vault - 运行测试 (Conda)" -ForegroundColor Cyan
Write-Host "=" -NoNewline -ForegroundColor Cyan
Write-Host ("=" * 58) -ForegroundColor Cyan
Write-Host ""

# 环境名称
$envName = if ($env:PKV_CONDA_ENV) { $env:PKV_CONDA_ENV } else { "py311-private" }

if (-not (Get-Command conda -ErrorAction SilentlyContinue)) {
    Write-Host "  ❌ 错误: 未找到 Conda" -ForegroundColor Red
    exit 1
}

# 检查 Conda 环境是否存在
Write-Host "🔍 检查 Conda 环境: $envName..." -ForegroundColor Yellow
$envExists = [bool](
    conda env list | Select-String -Pattern "^$([regex]::Escape($envName))\s" -Quiet
)

if (-not $envExists) {
    Write-Host "  ❌ 错误: Conda 环境 '$envName' 不存在" -ForegroundColor Red
    Write-Host "  请先运行: .\scripts\setup-conda.ps1" -ForegroundColor Yellow
    exit 1
}

Write-Host "  ✓ 环境存在" -ForegroundColor Green
Write-Host ""

# 通过测试包装器固定六个运行路径，避免安装验证触碰生产 .data
Write-Host "🚀 运行验证脚本..." -ForegroundColor Yellow
Write-Host ""

& "$PSScriptRoot\run-test.ps1" `
    -Direct `
    -DataRoot ".data-test\verify-setup" `
    -Command @("python", "src\utils\verify_setup.py")
$verificationExitCode = $LASTEXITCODE

if ($verificationExitCode -eq 0) {
    Write-Host ""
    Write-Host "=" -NoNewline -ForegroundColor Cyan
    Write-Host ("=" * 58) -ForegroundColor Cyan
    Write-Host "✅ 所有测试通过！" -ForegroundColor Green
    Write-Host "=" -NoNewline -ForegroundColor Cyan
    Write-Host ("=" * 58) -ForegroundColor Cyan
} else {
    Write-Host ""
    Write-Host "=" -NoNewline -ForegroundColor Red
    Write-Host ("=" * 58) -ForegroundColor Red
    Write-Host "❌ 测试失败，请检查错误信息" -ForegroundColor Red
    Write-Host "=" -NoNewline -ForegroundColor Red
    Write-Host ("=" * 58) -ForegroundColor Red
    exit $verificationExitCode
}

Write-Host ""
Write-Host "📝 下一步:" -ForegroundColor Yellow
Write-Host "  - 使用 scripts\run-windows.ps1 运行 CLI、测试或 GUI" -ForegroundColor White
Write-Host "  - 阅读 docs\operations\QUICKSTART.md 了解更多用法" -ForegroundColor White
Write-Host ""
