# 环境检测脚本
# 检测当前运行环境（生产 vs 测试）

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " PKV 环境检测" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# 检查 DB_PATH 环境变量
if ($env:DB_PATH) {
    Write-Host "[环境变量模式]" -ForegroundColor Magenta
    Write-Host "  DB_PATH = $env:DB_PATH" -ForegroundColor White

    if ($env:DB_PATH -match "\.data-test") {
        Write-Host "  状态: ✓ 测试环境" -ForegroundColor Green
        $is_test = $true
    } else {
        Write-Host "  状态: ⚠️  生产环境" -ForegroundColor Yellow
        $is_test = $false
    }
} else {
    Write-Host "[配置文件模式]" -ForegroundColor Magenta
    Write-Host "  未设置 DB_PATH 环境变量，使用默认配置" -ForegroundColor Gray

    # 通过 Python 读取实际配置
    $db_path = python -c "from src.utils.config import Config; print(Config().db_path)" 2>$null

    if ($LASTEXITCODE -eq 0) {
        Write-Host "  实际路径: $db_path" -ForegroundColor White

        if ($db_path -match "\.data-test") {
            Write-Host "  状态: ✓ 测试环境" -ForegroundColor Green
            $is_test = $true
        } else {
            Write-Host "  状态: ⚠️  生产环境" -ForegroundColor Yellow
            $is_test = $false
        }
    } else {
        Write-Host "  错误: 无法读取配置（Python 环境未就绪？）" -ForegroundColor Red
        exit 1
    }
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan

# 查询数据库统计
Write-Host "数据库统计:" -ForegroundColor Yellow
Write-Host ""

$stats = python -m src.main stats 2>&1

if ($LASTEXITCODE -eq 0) {
    $stats | Select-String "总条目数|数据库大小|最近更新" | ForEach-Object {
        Write-Host "  $_" -ForegroundColor White
    }
} else {
    Write-Host "  错误: 无法读取数据库统计" -ForegroundColor Red
    Write-Host "  可能原因: 数据库未初始化或环境配置错误" -ForegroundColor Gray
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan

# 安全建议
if ($is_test) {
    Write-Host "✓ 当前为测试环境，可以安全测试" -ForegroundColor Green
    Write-Host ""
    Write-Host "提示: 使用 .\scripts\run-test.ps1 运行命令" -ForegroundColor Cyan
} else {
    Write-Host "⚠️  当前为生产环境，请谨慎操作" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "建议: 如需测试，请使用测试环境" -ForegroundColor Cyan
    Write-Host "  1. 创建测试配置: copy .env.test.example .env.test" -ForegroundColor Gray
    Write-Host "  2. 运行测试命令: .\scripts\run-test.ps1 <command>" -ForegroundColor Gray
    Write-Host "  3. 重要变更前备份: .\scripts\backup-data.ps1" -ForegroundColor Gray
}

Write-Host ""
