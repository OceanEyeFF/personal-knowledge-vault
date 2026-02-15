# Personal Knowledge Vault - Conda 测试脚本 (PowerShell)
# 作者: 幽浮酱
# 用途: 在 Conda 环境中运行验证测试

Write-Host "=" -NoNewline -ForegroundColor Cyan
Write-Host ("=" * 58) -ForegroundColor Cyan
Write-Host "🧪 Personal Knowledge Vault - 运行测试 (Conda)" -ForegroundColor Cyan
Write-Host "=" -NoNewline -ForegroundColor Cyan
Write-Host ("=" * 58) -ForegroundColor Cyan
Write-Host ""

# 环境名称
$envName = "pkv-py311"

# 检查 Conda 环境是否存在
Write-Host "🔍 检查 Conda 环境: $envName..." -ForegroundColor Yellow
conda env list | Select-String -Pattern "^$envName\s" -Quiet

if (-not $?) {
    Write-Host "  ❌ 错误: Conda 环境 '$envName' 不存在" -ForegroundColor Red
    Write-Host "  请先运行: .\scripts\setup-conda.ps1" -ForegroundColor Yellow
    exit 1
}

Write-Host "  ✓ 环境存在" -ForegroundColor Green
Write-Host ""

# 激活环境
Write-Host "🔌 激活 Conda 环境: $envName..." -ForegroundColor Yellow
conda activate $envName

if ($LASTEXITCODE -eq 0) {
    Write-Host "  ✓ 环境已激活" -ForegroundColor Green
} else {
    Write-Host "  ⚠️  自动激活失败" -ForegroundColor Yellow
    Write-Host "  请手动激活后再运行测试:" -ForegroundColor Yellow
    Write-Host "     conda activate $envName" -ForegroundColor Cyan
    Write-Host "     python src\utils\verify_setup.py" -ForegroundColor Cyan
    exit 1
}

Write-Host ""

# 运行验证脚本
Write-Host "🚀 运行验证脚本..." -ForegroundColor Yellow
Write-Host ""

python src\utils\verify_setup.py

if ($LASTEXITCODE -eq 0) {
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
    exit 1
}

Write-Host ""
Write-Host "📝 下一步:" -ForegroundColor Yellow
Write-Host "  - 查看 .data\logs\verify.log 获取详细日志" -ForegroundColor White
Write-Host "  - 阅读 QUICKSTART.md 了解更多用法" -ForegroundColor White
Write-Host ""
