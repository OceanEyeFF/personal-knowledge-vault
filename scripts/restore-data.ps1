# 数据恢复脚本
# 从 .data-backup/ 目录恢复数据
#
# 用法:
#   .\scripts\restore-data.ps1  # 交互式选择备份
#   .\scripts\restore-data.ps1 -BackupTimestamp "20260216-143000"

param(
    [string]$BackupTimestamp = ""
)

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " PKV 数据恢复工具" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# 列出可用备份
if (!(Test-Path ".data-backup")) {
    Write-Host "[错误] 没有找到备份目录 (.data-backup)" -ForegroundColor Red
    exit 1
}

$backups = Get-ChildItem ".data-backup" -Directory | Sort-Object Name -Descending

if ($backups.Count -eq 0) {
    Write-Host "[错误] 没有可用的备份" -ForegroundColor Red
    exit 1
}

# 显示可用备份
Write-Host "可用备份列表:" -ForegroundColor Yellow
Write-Host ""

$backup_list = @()
for ($i = 0; $i -lt $backups.Count; $i++) {
    $backup = $backups[$i]
    $backup_info_path = Join-Path $backup.FullName "backup-info.json"

    if (Test-Path $backup_info_path) {
        $info = Get-Content $backup_info_path | ConvertFrom-Json
        Write-Host "  [$($i+1)] $($backup.Name)" -ForegroundColor Cyan
        Write-Host "      说明: $($info.message)" -ForegroundColor Gray
        Write-Host "      大小: $($info.size_mb) MB" -ForegroundColor Gray
        Write-Host "      文件数: $($info.files)" -ForegroundColor Gray
        $backup_list += $backup
    } else {
        Write-Host "  [$($i+1)] $($backup.Name)" -ForegroundColor Cyan
        Write-Host "      (无详细信息)" -ForegroundColor Gray
        $backup_list += $backup
    }
    Write-Host ""
}

# 选择备份
$selected_backup = $null

if ($BackupTimestamp -ne "") {
    # 使用指定的备份
    $selected_backup = $backups | Where-Object { $_.Name -eq $BackupTimestamp }
    if ($null -eq $selected_backup) {
        Write-Host "[错误] 找不到备份: $BackupTimestamp" -ForegroundColor Red
        exit 1
    }
    Write-Host "[自动选择] $BackupTimestamp" -ForegroundColor Green
} else {
    # 交互式选择
    $choice = Read-Host "请选择要恢复的备份 [1-$($backups.Count)] 或 Q 退出"

    if ($choice -eq "Q" -or $choice -eq "q") {
        Write-Host "已取消恢复" -ForegroundColor Yellow
        exit 0
    }

    $index = [int]$choice - 1
    if ($index -lt 0 -or $index -ge $backups.Count) {
        Write-Host "[错误] 无效的选择" -ForegroundColor Red
        exit 1
    }

    $selected_backup = $backup_list[$index]
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Red
Write-Host " ⚠️  警告：数据恢复操作" -ForegroundColor Yellow
Write-Host "========================================" -ForegroundColor Red
Write-Host ""
Write-Host "  将恢复备份: $($selected_backup.Name)" -ForegroundColor Yellow
Write-Host "  当前 .data 目录将被完全替换！" -ForegroundColor Red
Write-Host ""

$confirm = Read-Host "确认恢复？(输入 YES 继续，其他任意键取消)"

if ($confirm -ne "YES") {
    Write-Host "已取消恢复" -ForegroundColor Yellow
    exit 0
}

Write-Host ""
Write-Host "[1/3] 删除当前数据..." -ForegroundColor Yellow

if (Test-Path ".data") {
    Remove-Item -Recurse -Force ".data"
    Write-Host "  ✓ 已删除当前 .data 目录" -ForegroundColor Green
} else {
    Write-Host "  • .data 目录不存在，跳过删除" -ForegroundColor Gray
}

Write-Host ""
Write-Host "[2/3] 恢复备份数据..." -ForegroundColor Yellow
New-Item -ItemType Directory -Path ".data" -Force | Out-Null
Copy-Item -Recurse "$($selected_backup.FullName)/*" ".data/"
Write-Host "  ✓ 数据恢复完成" -ForegroundColor Green

Write-Host ""
Write-Host "[3/3] 验证数据..." -ForegroundColor Yellow

$db_path = ".data/db/knowledge_vault.db"
if (Test-Path $db_path) {
    Write-Host "  ✓ 数据库文件存在: $db_path" -ForegroundColor Green
} else {
    Write-Host "  ⚠️  警告: 未找到数据库文件" -ForegroundColor Yellow
}

$vault_path = ".data/vault"
if (Test-Path $vault_path) {
    $file_count = (Get-ChildItem -Recurse $vault_path -File | Measure-Object).Count
    Write-Host "  ✓ Vault 目录存在: $file_count 个文件" -ForegroundColor Green
} else {
    Write-Host "  ⚠️  警告: 未找到 Vault 目录" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " 恢复成功 ✓" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "提示: 运行以下命令验证数据:" -ForegroundColor Yellow
Write-Host "  python -m src.main stats" -ForegroundColor Cyan
Write-Host "  python -m src.main list" -ForegroundColor Cyan
