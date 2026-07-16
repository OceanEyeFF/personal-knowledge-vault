# Personal Knowledge Vault - Conda 自动安装脚本 (PowerShell)
# 作者: 幽浮酱
# 用途: 使用 Conda 创建 Python 3.11 环境、安装依赖

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $projectRoot

Write-Host "=" -NoNewline -ForegroundColor Cyan
Write-Host ("=" * 58) -ForegroundColor Cyan
Write-Host "🚀 Personal Knowledge Vault - Conda 自动安装" -ForegroundColor Cyan
Write-Host "=" -NoNewline -ForegroundColor Cyan
Write-Host ("=" * 58) -ForegroundColor Cyan
Write-Host ""

# 检查 conda
Write-Host "🔍 检查 Conda..." -ForegroundColor Yellow
if (-not (Get-Command conda -ErrorAction SilentlyContinue)) {
    Write-Host "  ❌ 错误: 未找到 Conda，请先安装 Miniconda 或 Anaconda" -ForegroundColor Red
    Write-Host "  下载地址: https://docs.conda.io/en/latest/miniconda.html" -ForegroundColor Yellow
    exit 1
}
$condaVersion = conda --version 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ❌ 错误: 未找到 Conda，请先安装 Miniconda 或 Anaconda" -ForegroundColor Red
    Write-Host "  下载地址: https://docs.conda.io/en/latest/miniconda.html" -ForegroundColor Yellow
    exit 1
}

Write-Host "  ✓ $condaVersion" -ForegroundColor Green
Write-Host ""

# 设置环境名称
$envName = if ($env:PKV_CONDA_ENV) { $env:PKV_CONDA_ENV } else { "py311-private" }

# 检查环境是否存在
Write-Host "🔍 检查 Conda 环境: $envName..." -ForegroundColor Yellow
$envExists = [bool](
    conda env list | Select-String -Pattern "^$([regex]::Escape($envName))\s" -Quiet
)

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

# 验证 Python 版本
Write-Host "🔍 验证 Python 版本..." -ForegroundColor Yellow
$pythonVersion = conda run -n $envName python --version 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ❌ 无法在 Conda 环境中运行 Python" -ForegroundColor Red
    exit 1
}
Write-Host "  ✓ $pythonVersion" -ForegroundColor Green

Write-Host ""

# 安装 Windows 预编译 hnswlib，避免本地编译失败
Write-Host "📦 安装 hnswlib 0.8.0 (conda-forge)..." -ForegroundColor Yellow
conda install -y -n $envName -c conda-forge hnswlib=0.8.0
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ❌ hnswlib 安装失败" -ForegroundColor Red
    exit 1
}

Write-Host ""

# 升级 pip
Write-Host "⬆️  升级 pip..." -ForegroundColor Yellow
conda run -n $envName python -m pip install --upgrade pip -q

if ($LASTEXITCODE -eq 0) {
    Write-Host "  ✓ pip 已升级到最新版本" -ForegroundColor Green
} else {
    Write-Host "  ⚠️  pip 升级失败，继续安装..." -ForegroundColor Yellow
}

Write-Host ""

# 安装依赖包
Write-Host "📥 安装依赖包..." -ForegroundColor Yellow
Write-Host "  (这可能需要 3-5 分钟，请耐心等待...)" -ForegroundColor Cyan

conda run -n $envName python -m pip install -r requirements.txt

if ($LASTEXITCODE -eq 0) {
    Write-Host "  ✓ 所有依赖包安装成功！" -ForegroundColor Green
} else {
    Write-Host "  ❌ 依赖包安装失败" -ForegroundColor Red
    exit 1
}

Write-Host ""

# 安装 Playwright Chromium
Write-Host "🌐 安装 Playwright Chromium..." -ForegroundColor Yellow
conda run -n $envName python -m playwright install chromium
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ❌ Playwright Chromium 安装失败" -ForegroundColor Red
    exit 1
}

Write-Host ""

# 检查关键依赖
Write-Host "🔍 验证关键依赖..." -ForegroundColor Yellow

$dependencies = @(
    "frontmatter",
    "yaml",
    "hnswlib",
    "jieba",
    "playwright"
)

$allInstalled = $true
foreach ($dep in $dependencies) {
    conda run -n $envName python -c "import $dep" 2>$null
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

# 创建本机私有 YAML 配置
Write-Host "⚙️  创建本机配置..." -ForegroundColor Yellow

if (Test-Path "config\local.yaml") {
    Write-Host "  ℹ️  config\local.yaml 已存在，跳过创建" -ForegroundColor Cyan
} else {
    Copy-Item "config\config.yaml" "config\local.yaml"
    Write-Host "  ✓ 已从 config\config.yaml 创建 config\local.yaml" -ForegroundColor Green
    Write-Host "  ⚠️  请编辑 config\local.yaml，填入 API Keys" -ForegroundColor Yellow
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
Write-Host "  1. 编辑本机配置，填入 API Keys:" -ForegroundColor White
Write-Host "     notepad config\local.yaml" -ForegroundColor Cyan
Write-Host ""
Write-Host "  2. 使用统一 Windows 运行器启动命令:" -ForegroundColor White
Write-Host "     .\scripts\run-windows.ps1 python -m src.cli.commands --help" -ForegroundColor Cyan
Write-Host ""
Write-Host "  3. 运行验证脚本:" -ForegroundColor White
Write-Host "     .\scripts\test-conda.ps1" -ForegroundColor Cyan
Write-Host ""

Write-Host "💡 提示:" -ForegroundColor Yellow
Write-Host "  - Conda 环境名称: $envName" -ForegroundColor Cyan
Write-Host "  - Python 版本: 3.11" -ForegroundColor Cyan
Write-Host "  - run-windows.ps1 会自动设置 UTF-8、PYTHONPATH 和 Conda 环境" -ForegroundColor Cyan
Write-Host ""
