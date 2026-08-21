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

# 创建唯一可编辑的本机配置。数据根初始化必须走 lifecycle plan，不能由
# 环境安装脚本隐式创建数据库、向量索引或 runtime snapshot。
Write-Host "⚙️  创建本机配置..." -ForegroundColor Yellow

$userProfileRoot = $env:USERPROFILE
if ([string]::IsNullOrWhiteSpace($userProfileRoot)) {
    $userProfileRoot = [Environment]::GetFolderPath(
        [Environment+SpecialFolder]::UserProfile
    )
}
if ([string]::IsNullOrWhiteSpace($userProfileRoot)) {
    Write-Host "  ❌ 无法解析用户配置根目录" -ForegroundColor Red
    exit 1
}

$userPkvRoot = Join-Path $userProfileRoot ".pkv"
$userConfigPath = Join-Path $userPkvRoot "config.yaml"
$defaultDataRoot = Join-Path $userPkvRoot "data"
$configTemplatePath = Join-Path $projectRoot "config\config.yaml"

if (-not (Test-Path -LiteralPath $configTemplatePath -PathType Leaf)) {
    Write-Host "  ❌ 找不到配置模板: $configTemplatePath" -ForegroundColor Red
    exit 1
}
if (Test-Path -LiteralPath $userConfigPath -PathType Leaf) {
    Write-Host "  ℹ️  $userConfigPath 已存在，跳过创建" -ForegroundColor Cyan
} elseif (Test-Path -LiteralPath $userConfigPath) {
    Write-Host "  ❌ 用户配置路径不是普通文件: $userConfigPath" -ForegroundColor Red
    exit 1
} else {
    try {
        New-Item -ItemType Directory -Path $userPkvRoot -Force -ErrorAction Stop | Out-Null
        # overwrite = $false closes the check/copy race: a concurrently created
        # user config is retained rather than silently replaced.
        [System.IO.File]::Copy($configTemplatePath, $userConfigPath, $false)
        Write-Host "  ✓ 已从 config\config.yaml 创建 $userConfigPath" -ForegroundColor Green
        Write-Host "  ⚠️  请编辑该文件，填入 API Keys；不要将密钥写入数据根。" -ForegroundColor Yellow
    } catch [System.IO.IOException] {
        if (Test-Path -LiteralPath $userConfigPath -PathType Leaf) {
            Write-Host "  ℹ️  $userConfigPath 已被其他进程创建，保留现有文件" -ForegroundColor Cyan
        } else {
            Write-Host "  ❌ 创建本机配置失败: $($_.Exception.Message)" -ForegroundColor Red
            exit 1
        }
    } catch {
        Write-Host "  ❌ 创建本机配置失败: $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }
}

Write-Host ""

# 历史 checkout 布局只能保留并提示，不得在安装时读取内容、复制、删除或迁移。
$legacyPaths = @(
    [PSCustomObject]@{
        Label = "旧 checkout 配置"
        Path = Join-Path $projectRoot "config\local.yaml"
    },
    [PSCustomObject]@{
        Label = "旧 checkout 数据根"
        Path = Join-Path $projectRoot ".data"
    }
)
$foundLegacyPaths = @($legacyPaths | Where-Object {
    Test-Path -LiteralPath $_.Path
})

if ($foundLegacyPaths.Count -gt 0) {
    Write-Host "⚠️  检测到旧 checkout 运行路径（已保留）:" -ForegroundColor Yellow
    foreach ($legacyPath in $foundLegacyPaths) {
        Write-Host "  - $($legacyPath.Label): $($legacyPath.Path)" -ForegroundColor Yellow
    }
    Write-Host "  本脚本不会读取、复制、删除或迁移这些路径。" -ForegroundColor Yellow
    Write-Host "  如需迁移，请先查看 inspect 的影响，再由用户确认单独迁移方案。" -ForegroundColor Yellow
} else {
    Write-Host "📁 数据根尚未初始化；不会创建 $defaultDataRoot。" -ForegroundColor Cyan
}

Write-Host ""

# 完成
Write-Host "=" -NoNewline -ForegroundColor Cyan
Write-Host ("=" * 58) -ForegroundColor Cyan
Write-Host "✅ 环境依赖安装完成；运行时尚未初始化。" -ForegroundColor Green
Write-Host "=" -NoNewline -ForegroundColor Cyan
Write-Host ("=" * 58) -ForegroundColor Cyan
Write-Host ""

Write-Host "📝 下一步操作:" -ForegroundColor Yellow
Write-Host ""
Write-Host "  1. 编辑唯一的本机配置，填入 API Keys:" -ForegroundColor White
Write-Host ('     notepad "{0}"' -f $userConfigPath) -ForegroundColor Cyan
Write-Host ""
Write-Host "  2. 无副作用检查当前配置与运行时状态:" -ForegroundColor White
Write-Host "     .\scripts\run-windows.ps1 python -m src.cli.commands inspect" -ForegroundColor Cyan
Write-Host ""
Write-Host "  3. 查看初始化计划（仍不会写入数据根）:" -ForegroundColor White
Write-Host "     .\scripts\run-windows.ps1 python -m src.cli.commands setup" -ForegroundColor Cyan
Write-Host ""
Write-Host "  4. 仅在审阅计划后，使用输出的 PLAN_ID 明确确认执行:" -ForegroundColor White
Write-Host "     .\scripts\run-windows.ps1 python -m src.cli.commands setup --apply --confirm <PLAN_ID> --allow-network" -ForegroundColor Cyan
Write-Host "     --allow-network 只授权计划中的最小 Provider 连通性探测，可能联网或产生费用。" -ForegroundColor Yellow
Write-Host ""
Write-Host "  5. 运行隔离验证脚本:" -ForegroundColor White
Write-Host "     .\scripts\test-conda.ps1" -ForegroundColor Cyan
Write-Host ""

Write-Host "💡 提示:" -ForegroundColor Yellow
Write-Host "  - Conda 环境名称: $envName" -ForegroundColor Cyan
Write-Host "  - Python 版本: 3.11" -ForegroundColor Cyan
Write-Host "  - 默认数据根: $defaultDataRoot（PKV_DATA_ROOT 可在进程级覆盖）" -ForegroundColor Cyan
Write-Host "  - <data-root>\config\local.yaml 是 PKV 管理的无密钥运行时快照，不是用户配置" -ForegroundColor Cyan
Write-Host "  - run-windows.ps1 会自动设置 UTF-8、PYTHONPATH 和 Conda 环境" -ForegroundColor Cyan
Write-Host ""
