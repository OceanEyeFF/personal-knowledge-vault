# Backup .data to a timestamped zip and keep recent archives.
[Console]::Error.WriteLine(
    "backup.ps1 已停用：历史 .data 备份脚本不属于当前 PKV 运行时，未读取配置、未打开数据根。"
)
exit 2

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path $ScriptDir -Parent

$DataDir = Join-Path $RepoRoot ".data"
$BackupRoot = Join-Path $RepoRoot "backups"
$RetentionDays = 7

try {
    if (-not (Test-Path $DataDir -PathType Container)) {
        throw "Data directory not found: $DataDir"
    }

    if (-not (Test-Path $BackupRoot -PathType Container)) {
        New-Item -Path $BackupRoot -ItemType Directory | Out-Null
    }

    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $zipName = "backup-$timestamp.zip"
    $zipPath = Join-Path $BackupRoot $zipName

    Compress-Archive -Path $DataDir -DestinationPath $zipPath -Force

    $cutoff = (Get-Date).AddDays(-$RetentionDays)
    Get-ChildItem -Path $BackupRoot -Filter "backup-*.zip" -File |
        Where-Object { $_.LastWriteTime -lt $cutoff } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }

    Write-Host "Backup completed."
    Write-Host "Archive: $zipPath"
    Write-Host "Retention: $RetentionDays days"
    exit 0
} catch {
    Write-Host "Backup failed: $($_.Exception.Message)"
    exit 1
}
