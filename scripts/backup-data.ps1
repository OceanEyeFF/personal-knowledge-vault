# 数据备份脚本
# 备份生产数据到 .data-backup/ 目录
#
# 用法:
#   .\scripts\backup-data.ps1
#   .\scripts\backup-data.ps1 -Message "重要更新前的备份"

param(
    [string]$Message = "手动备份"
)

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backup_dir = ".data-backup/$timestamp"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " PKV 数据备份工具" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# 检查源目录是否存在
if (!(Test-Path ".data")) {
    Write-Host "[错误] .data 目录不存在，无法备份" -ForegroundColor Red
    exit 1
}

# 创建备份目录
Write-Host "[1/3] 创建备份目录..." -ForegroundColor Yellow
New-Item -ItemType Directory -Path $backup_dir -Force | Out-Null
Write-Host "  ✓ 目录: $backup_dir" -ForegroundColor Green
Write-Host ""

# 复制数据
Write-Host "[2/3] 复制数据文件..." -ForegroundColor Yellow
Copy-Item -Recurse ".data/*" $backup_dir
Write-Host "  ✓ 数据复制完成" -ForegroundColor Green
Write-Host ""

# 生成备份信息
Write-Host "[3/3] 生成备份信息..." -ForegroundColor Yellow

$file_count = (Get-ChildItem -Recurse $backup_dir | Measure-Object).Count
$total_size = (Get-ChildItem -Recurse $backup_dir | Measure-Object -Property Length -Sum).Sum
$size_mb = [math]::Round($total_size / 1MB, 2)

$info = @{
    timestamp = $timestamp
    message = $Message
    files = $file_count
    size_mb = $size_mb
    source = ".data"
    created_by = $env:USERNAME
} | ConvertTo-Json -Depth 3

$info | Out-File "$backup_dir/backup-info.json" -Encoding UTF8

Write-Host "  ✓ 备份信息已保存" -ForegroundColor Green
Write-Host ""

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " 备份成功 ✓" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  备份时间: $timestamp" -ForegroundColor Cyan
Write-Host "  备份说明: $Message" -ForegroundColor Cyan
Write-Host "  文件数量: $file_count" -ForegroundColor Cyan
Write-Host "  备份大小: $size_mb MB" -ForegroundColor Cyan
Write-Host "  备份位置: $backup_dir" -ForegroundColor Cyan
Write-Host ""

# 列出最近的备份
Write-Host "最近的 5 个备份:" -ForegroundColor Yellow
Get-ChildItem ".data-backup" -Directory |
    Sort-Object Name -Descending |
    Select-Object -First 5 |
    ForEach-Object {
        $backup_info_path = Join-Path $_.FullName "backup-info.json"
        if (Test-Path $backup_info_path) {
            $backup_info = Get-Content $backup_info_path | ConvertFrom-Json
            $name = $_.Name
            $msg = $backup_info.message
            $size = $backup_info.size_mb
            Write-Host "  • $name - $msg ($size MB)" -ForegroundColor Gray
        } else {
            Write-Host "  • $($_.Name)" -ForegroundColor Gray
        }
    }

Write-Host ""
Write-Host "提示: 使用 .\scripts\restore-data.ps1 恢复备份" -ForegroundColor Yellow
