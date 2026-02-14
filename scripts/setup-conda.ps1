# Personal Knowledge Vault - Conda 自动安装脚本 (PowerShell)
# 作者: 幽浮酱
# 用途: 使用 Conda 创建 Python 3.11 环境、安装依赖

Write-Host "=" -NoNewline -ForegroundColor Cyan
Write-Host ("=" * 58) -ForegroundColor Cyan
Write-Host "🚀 Personal Knowledge Vault - Conda 自动安装" -ForegroundColor Cyan
Write-Host "=" -NoNewline -ForegroundColor Cyan
Write-Host ("=" * 58) -ForegroundColor Cyan
Write-Host ""

# 检查 conda
Write-Host "🔍 检查 Conda...��" -ForegroundColor Yellow
$condaVersion = conda --version 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ❌ 错误: 未找到 Conda，请先安装 Miniconda 或 Anaconda" -ForegroundColor Red
    Write-Host "  下载地址: https://docs.conda.io/en/latest/miniconda.html" -ForegroundColor Yellow
    exit 1
}

Write-Host "  ✓ $condaVersion" -ForegroundColor Green
Write-Host ""

# 设置环境名称
$envName = "pkv-py311"

# 检查环境是否存在
Write-Host "🔍 检查 Conda 环境: $envName..." -ForegroundColor Yellow
conda env list | Select-String -Pattern "^$envName\s" -Quiet
$envExists = $?

if ($envExists) {
    Write-Host "  ℹ️  环境 '$envName' 已存在" -ForegroundColor Cyan
    $removeEnv = Read-Host "  是否删除并重新创建? (y/n)"

    if ($removeEnv -eq "y") {
        Write-Host "  🗑️  删除旧环境..." -ForegroundColor Yellow
        conda env remove -n $envName -y
        Write-Host "  ✓ 旧环境已删除" -ForegroundColor Green
        $envExists = $false
    }
}

Write-Host ""

# 创建 Conda 环境
if (-not $envExists) {
    Write-Host "📦 创建 Conda 环境: $envName (Python 3.11)..." -ForegroundColor Yellow
    Write-Host "  (这可能需要 1-2 分钟...)" -ForegroundColor Cyan

    conda create -n $envName python=3.11 -y

    if ($LASTEXITCODE -eq 0) {
        Write-Host "  ✓ Conda 环境创建成功！" -ForegroundColor Green
    } else {
        Write-Host "  ❌ Conda 环境创建失败" -ForegroundColor Red
        exit 1
    }
}

Write-Host ""

# 激活环境
Write-Host "🔌 激活 Conda 环境: $envName..." -ForegroundColor Yellow
conda activate $envName

if ($LASTEXITCODE -eq 0) {
    Write-Host "  ✓ 环境已激活" -ForegroundColor Green
} else {
    Write-Host "  ⚠️  自动激活失败，请手动激活:" -ForegroundColor Yellow
    Write-Host "     conda activate $envName" -ForegroundColor Cyan
}

Write-Host ""

# 验证 Python 版本
Write-Host "🔍 验证 Python 版本..." -ForegroundColor Yellow
$pythonVersion = python --version 2>&1
Write-Host "  ✓ $pythonVersion" -ForegroundColor Green

Write-Host ""

# 升级 pip
Write-Host "⬆️  升级 pip..." -ForegroundColor Yellow
python -m pip install --upgrade pip -q

if ($LASTEXITCODE -eq 0) {
    Write-Host "  ✓ pip 已升级到最新版本" -ForegroundColor Green
} else {
    Write-Host "  ⚠️  pip 升级失败，继续安装..." -ForegroundColor Yellow
}

Write-Host ""

# 安装依赖包
Write-Host "📥 安装依赖包..." -ForegroundColor Yellow
Write-Host "  (这可能需要 3-5 分钟，请耐心等待...)��" -ForegroundColor Cyan

python -m pip install -r requirements.txt

if ($LASTEXITCODE -eq 0) {
    Write-Host "  ✓ 所有依赖包安装成功！" -ForegroundColor Green
} else {
    Write-Host "  ❌ 依赖包安装失败" -ForegroundColor Red
    exit 1
}

Write-Host ""

# 检查关键依赖
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

# 创建 .env 文件
Write-Host "⚙️  配置环境变量..." -ForegroundColor Yellow

if (Test-Path ".env") {
    Write-Host "  ℹ️  .env 文件已存在，跳过创建" -ForegroundColor Cyan
} else {
    Copy-Item ".env.example" ".env"
    Write-Host "  ✓ 已创建 .env 文件（从 .env.example 复制）" -ForegroundColor Green
    Write-Host "  ⚠️  请编辑 .env 文件，填入你的 API Keys" -ForegroundColor Yellow
}

Write-Host ""

# 创建数据目录
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

# 完成
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
Write-Host "  2. 激活 Conda 环境 (每次使用前都需要):" -ForegroundColor White
Write-Host "     conda activate $envName" -ForegroundColor Cyan
Write-Host ""
Write-Host "  3. 运行验证脚本:" -ForegroundColor White
Write-Host "     python src\utils\verify_setup.py" -ForegroundColor Cyan
Write-Host ""

Write-Host "💡 提示:" -ForegroundColor Yellow
Write-Host "  - Conda 环境名称: $envName" -ForegroundColor Cyan
Write-Host "  - Python 版本: 3.11" -ForegroundColor Cyan
Write-Host "  - 每次打开新终端都需要激活环境: conda activate $envName" -ForegroundColor Cyan
Write-Host ""
