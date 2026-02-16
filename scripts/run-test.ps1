# 测试环境运行脚本
# 使用隔离的测试数据库和配置运行 PKV 命令
#
# 用法:
#   .\scripts\run-test.ps1 archive "https://example.com"
#   .\scripts\run-test.ps1 search "AI"
#   .\scripts\run-test.ps1 stats

param(
    [Parameter(Mandatory=$true, ValueFromRemainingArguments=$true)]
    [string[]]$Command
)

# 加载测试环境变量
if (Test-Path ".env.test") {
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host " 测试环境模式 (Test Environment)" -ForegroundColor Green
    Write-Host "========================================" -ForegroundColor Cyan

    Get-Content .env.test | ForEach-Object {
        if ($_ -match '^([^=]+)=(.*)$') {
            $key = $matches[1].Trim()
            $value = $matches[2].Trim()
            [Environment]::SetEnvironmentVariable($key, $value)

            # 只显示非敏感信息
            if ($key -notmatch "API_KEY") {
                Write-Host "  ✓ $key = $value" -ForegroundColor Gray
            } else {
                Write-Host "  ✓ $key = ***" -ForegroundColor Gray
            }
        }
    }
    Write-Host ""
} else {
    Write-Host "[警告] .env.test 文件不存在，使用默认配置" -ForegroundColor Yellow
    Write-Host "提示: 创建 .env.test 文件来配置测试环境" -ForegroundColor Yellow
    Write-Host ""
}

# 确保测试数据目录存在
$test_dirs = @(
    ".data-test/db",
    ".data-test/vault",
    ".data-test/vectors",
    ".data-test/logs",
    ".data-test/tmp"
)

foreach ($dir in $test_dirs) {
    if (!(Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
}

# 执行命令
$cmd = $Command -join " "
Write-Host "[执行命令]" -ForegroundColor Cyan
Write-Host "  python -m src.main $cmd" -ForegroundColor White
Write-Host ""

# 运行命令并捕获退出码
Invoke-Expression "python -m src.main $cmd"
$exit_code = $LASTEXITCODE

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
if ($exit_code -eq 0) {
    Write-Host " 测试完成 ✓" -ForegroundColor Green
} else {
    Write-Host " 测试失败 (退出码: $exit_code)" -ForegroundColor Red
}
Write-Host "========================================" -ForegroundColor Cyan

exit $exit_code
