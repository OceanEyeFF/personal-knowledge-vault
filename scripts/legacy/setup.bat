@echo off
REM Personal Knowledge Vault - 自动安装脚本 (CMD/Batch)
REM 作者: 幽浮酱
REM 用途: 创建虚拟环境、安装依赖、初始化数据库

echo ============================================================
echo 🚀 Personal Knowledge Vault - 自动安装
echo ============================================================
echo.

REM 检查 Python
echo 🔍 检查 Python 版本...
python --version >nul 2>&1
if errorlevel 1 (
    echo ❌ 错误: 未找到 Python，请先安装 Python 3.11+
    exit /b 1
)

for /f "tokens=*" %%i in ('python --version') do set PYTHON_VERSION=%%i
echo   ✓ %PYTHON_VERSION%
echo.

REM 创建虚拟环境
echo 📦 创建虚拟环境...
if exist .venv (
    echo   ℹ️  虚拟环境已存在，跳过创建
) else (
    python -m venv .venv
    if errorlevel 1 (
        echo   ❌ 虚拟环境创建失败
        exit /b 1
    )
    echo   ✓ 虚拟环境创建成功: .venv\
)
echo.

REM 激活虚拟环境
echo 🔌 激活虚拟环境...
call .venv\Scripts\activate.bat
echo   ✓ 虚拟环境已激活
echo.

REM 升级 pip
echo ⬆️  升级 pip...
python -m pip install --upgrade pip -q
echo   ✓ pip 已升级到最新版本
echo.

REM 安装依赖
echo 📥 安装依赖包...
echo   (这可能需要 2-3 分钟，请耐心等待...)
python -m pip install -r requirements.txt

if errorlevel 1 (
    echo   ❌ 依赖包安装失败
    exit /b 1
)
echo   ✓ 所有依赖包安装成功！
echo.

REM 验证依赖
echo 🔍 验证关键依赖...
python -c "import frontmatter" 2>nul && echo   ✓ frontmatter || echo   ❌ frontmatter
python -c "import yaml" 2>nul && echo   ✓ yaml || echo   ❌ yaml
python -c "import dotenv" 2>nul && echo   ✓ dotenv || echo   ❌ dotenv
python -c "import hnswlib" 2>nul && echo   ✓ hnswlib || echo   ❌ hnswlib
python -c "import jieba" 2>nul && echo   ✓ jieba || echo   ❌ jieba
echo.

REM 创建 .env
echo ⚙️  配置环境变量...
if exist .env (
    echo   ℹ️  .env 文件已存在，跳过创建
) else (
    copy .env.example .env >nul
    echo   ✓ 已创建 .env 文件（从 .env.example 复制）
    echo   ⚠️  请编辑 .env 文件，填入你的 API Keys
)
echo.

REM 创建数据目录
echo 📁 创建数据目录...
if not exist .data\db mkdir .data\db && echo   ✓ .data\db
if not exist .data\vectors mkdir .data\vectors && echo   ✓ .data\vectors
if not exist .data\vault mkdir .data\vault && echo   ✓ .data\vault
if not exist .data\logs mkdir .data\logs && echo   ✓ .data\logs
if not exist .data\tmp mkdir .data\tmp && echo   ✓ .data\tmp
echo.

REM 完成
echo ============================================================
echo ✅ 安装完成！
echo ============================================================
echo.
echo 📝 下一步操作:
echo.
echo   1. 编辑 .env 文件，填入 API Keys:
echo      notepad .env
echo.
echo   2. 运行验证脚本:
echo      python src\utils\verify_setup.py
echo.
echo 💡 提示: 每次打开新终端都需要激活虚拟环境:
echo    .venv\Scripts\activate.bat
echo.

pause
