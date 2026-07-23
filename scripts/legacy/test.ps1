# Personal Knowledge Vault - 测试脚本 (PowerShell)
# 作者: 幽浮酱
# 用途: 运行所有验证测试

Write-Host "=" -NoNewline -ForegroundColor Cyan
Write-Host ("=" * 58) -ForegroundColor Cyan
Write-Host "🧪 Personal Knowledge Vault - 运行测试" -ForegroundColor Cyan
Write-Host "=" -NoNewline -ForegroundColor Cyan
Write-Host ("=" * 58) -ForegroundColor Cyan
Write-Host ""

# 检查虚拟环境
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "❌ 错误: 虚拟环境不存在" -ForegroundColor Red
    Write-Host "   请先运行: .\scripts\setup.ps1" -ForegroundColor Yellow
    exit 1
}

# 激活虚拟环境
Write-Host "🔌 激活虚拟环境..." -ForegroundColor Yellow
& .\.venv\Scripts\Activate.ps1
Write-Host "  ✓ 虚拟环境已激活" -ForegroundColor Green
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
Write-Host "  - verify_setup 使用一次性隔离目录；详细结果请查看本次终端输出" -ForegroundColor White
Write-Host "  - 阅读 QUICKSTART.md 了解更多用法" -ForegroundColor White
Write-Host ""
