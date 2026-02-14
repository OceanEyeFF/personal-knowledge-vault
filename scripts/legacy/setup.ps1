# Personal Knowledge Vault - 自动安装脚本 (PowerShell)
# 作者: 幽浮酱
# 用途: 创建虚拟环境、安装依赖、初始化数据库

Write-Host "=" -NoNewline -ForegroundColor Cyan
Write-Host ("=" * 58) -ForegroundColor Cyan
Write-Host "🚀 Personal Knowledge Vault - 自动安装" -ForegroundColor Cyan
Write-Host "=" -NoNewline -ForegroundColor Cyan
Write-Host ("=" * 58) -ForegroundColor Cyan
Write-Host ""

# 检查 Python 版本
Write-Host "🔍 检查 Python 版本..." -ForegroundColor Yellow
$pythonVersion = python --version 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ 错误: 未找到 Python，请先安装 Python 3.11+" -ForegroundColor Red
    exit 1
}

Write-Host "  ✓ $pythonVersion" -ForegroundColor Green

# 检查 Python 版本是否 >= 3.11
$versionMatch = $pythonVersion -match "Python (\d+)\.(\d+)"
if ($versionMatch) {
    $major = [int]$matches[1]
    $minor = [int]$matches[2]

    if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 11)) {
        Write-Host "⚠️  警告: 建议使用 Python 3.11+，当前版本: $pythonVersion" -ForegroundColor Yellow
        $continue = Read-Host "是否继续? (y/n)"
        if ($continue -ne "y") {
            exit 0
        }
    }
}

Write-Host ""

# 1. 创建虚拟环境
Write-Host "📦 创建虚拟环境..." -ForegroundColor Yellow

if (Test-Path ".venv") {
    Write-Host "  ℹ️  虚拟环境已存在，跳过创建" -ForegroundColor Cyan
} else {
    python -m venv .venv
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  ✓ 虚拟环境创建成功: .venv\" -ForegroundColor Green
    } else {
        Write-Host "  ❌ 虚拟环境创建失败" -ForegroundColor Red
        exit 1
    }
}

Write-Host ""

# 2. 激活虚拟环境
Write-Host "🔌 激活虚拟环境..." -ForegroundColor Yellow
& .\.venv\Scripts\Activate.ps1

if ($LASTEXITCODE -eq 0) {
    Write-Host "  ✓ 虚拟环境已激活" -ForegroundColor Green
} else {
    Write-Host "  ❌ 虚拟环境激活失败" -ForegroundColor Red
    exit 1
}

Write-Host ""

# 3. 升级 pip
Write-Host "⬆️  升级 pip..." -ForegroundColor Yellow
python -m pip install --upgrade pip -q
if ($LASTEXITCODE -eq 0) {
    Write-Host "  ✓ pip 已升级到最新版本" -ForegroundColor Green
} else {
    Write-Host "  ⚠️  pip 升级失败，继续安装..." -ForegroundColor Yellow
}

Write-Host ""

# 4. 安装依赖包
Write-Host "📥 安装依赖包..." -ForegroundColor Yellow
Write-Host "  (这可能需要 2-3 分钟，请耐心等待...)" -ForegroundColor Cyan

python -m pip install -r requirements.txt

if ($LASTEXITCODE -eq 0) {
    Write-Host "  ✓ 所有依赖包安装成功！" -ForegroundColor Green
} else {
    Write-Host "  ❌ 依赖包安装失败" -ForegroundColor Red
    exit 1
}

Write-Host ""

# 5. 检查关键依赖
Write-Host "🔍 验证关键依赖..." -ForegroundColor Yellow

$dependencies = @(
    "frontmatter",
    "yaml",
    "dotenv",
    "hnswlib",
    "jieba"
)

$allInstalled = $true
foreach ($dep in $dependencies) {
    python -c "import $dep" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  ✓ $dep" -ForegroundColor Green
    } else {
        Write-Host "  ❌ $dep 未安装" -ForegroundColor Red
        $allInstalled = $false
    }
}

if (-not $allInstalled) {
    Write-Host ""
    Write-Host "❌ 部分依赖包安装失败，请检查错误信息" -ForegroundColor Red
    exit 1
}

Write-Host ""

# 6. 创建 .env 文件
Write-Host "⚙️  配置环境变量..." -ForegroundColor Yellow

if (Test-Path ".env") {
    Write-Host "  ℹ️  .env 文件已存在，跳过创建" -ForegroundColor Cyan
} else {
    Copy-Item ".env.example" ".env"
    Write-Host "  ✓ 已创建 .env 文件（从 .env.example 复制）" -ForegroundColor Green
    Write-Host "  ⚠️  请编辑 .env 文件，填入你的 API Keys" -ForegroundColor Yellow
}

Write-Host ""

# 7. 创建数据目录
Write-Host "📁 创建数据目录..." -ForegroundColor Yellow

$dataDirs = @(
    ".data\db",
    ".data\vectors",
    ".data\vault",
    ".data\logs",
    ".data\tmp"
)

foreach ($dir in $dataDirs) {
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
        Write-Host "  ✓ $dir" -ForegroundColor Green
    } else {
        Write-Host "  ℹ️  $dir (已存在)" -ForegroundColor Cyan
    }
}

Write-Host ""

# 8. 完成
Write-Host "=" -NoNewline -ForegroundColor Cyan
Write-Host ("=" * 58) -ForegroundColor Cyan
Write-Host "✅ 安装完成！" -ForegroundColor Green
Write-Host "=" -NoNewline -ForegroundColor Cyan
Write-Host ("=" * 58) -ForegroundColor Cyan
Write-Host ""

Write-Host "📝 下一步操作:" -ForegroundColor Yellow
Write-Host ""
Write-Host "  1. 编辑 .env 文件，填入 API Keys:" -ForegroundColor White
Write-Host "     notepad .env" -ForegroundColor Cyan
Write-Host ""
Write-Host "  2. 运行验证脚本:" -ForegroundColor White
Write-Host "     python src\utils\verify_setup.py" -ForegroundColor Cyan
Write-Host ""
Write-Host "  3. 初始化数据库:" -ForegroundColor White
Write-Host "     python -c `"from src.storage.sqlite_store import SQLiteStore; from src.utils.config import get_config; store = SQLiteStore(get_config().db_path); store.initialize()`"" -ForegroundColor Cyan
Write-Host ""

Write-Host "💡 提示: 每次打开新终端都需要激活虚拟环境:" -ForegroundColor Yellow
Write-Host "   .\.venv\Scripts\Activate.ps1" -ForegroundColor Cyan
Write-Host ""
