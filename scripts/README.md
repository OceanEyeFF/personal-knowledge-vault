# Scripts 目录说明

这个目录包含了 Personal Knowledge Vault 的自动化脚本喵～ ฅ'ω'ฅ

## 📋 脚本列表

### Conda 自动化安装 🌟

#### `setup-conda.ps1` - Conda 自动安装脚本 ⭐⭐推荐

**用途**: 使用 Conda 创建 Python 3.11 环境并安装依赖

**运行方式**:
```powershell
# 在 PowerShell 中运行
.\scripts\setup-conda.ps1
```

**功能**:
- ✅ 检查 Conda 是否安装
- ✅ 创建 Python 3.11 Conda 环境 (pkv-py311)
- ✅ 激活环境
- ✅ 升级 pip
- ✅ 安装所有依赖包
- ✅ 验证关键依赖
- ✅ 创建 .env 配置文件
- ✅ 创建数据目录

**优势**:
- ✨ 避免 Python 3.13 兼容性问题
- ✨ 环境隔离更彻底
- ✨ 可以方便地切换 Python 版本

---

#### `test-conda.ps1` - Conda 测试脚本

**用途**: 在 Conda 环境中运行验证测试

**运行方式**:
```powershell
.\scripts\test-conda.ps1
```

**功能**:
- ✅ 检查 Conda 环境是否存在
- ✅ 激活 pkv-py311 环境
- ✅ 运行 `verify_setup.py`
- ✅ 显示测试结果

---

## 🚀 快速开始

### 第一次使用

1. **确保已安装 Conda**:
   - 如果没有，请安装 [Miniconda](https://docs.conda.io/en/latest/miniconda.html) 或 Anaconda
   - 下载地址: https://docs.conda.io/en/latest/miniconda.html

2. **运行 Conda 安装脚本**:
   ```powershell
   .\scripts\setup-conda.ps1
   ```

3. **编辑 .env 文件**:
   ```powershell
   notepad .env
   # 填入你的 DeepSeek 和 OpenAI API Keys
   ```

4. **运行验证测试**:
   ```powershell
   .\scripts\test-conda.ps1
   ```

5. **每次使用前激活环境**:
   ```powershell
   conda activate pkv-py311
   ```

### Legacy 方案 (不推荐)

如果无法使用 Conda，可以查看 [legacy/README.md](legacy/README.md) 了解传统 venv 安装方式。

⚠️ **注意**: Legacy 方案在 Python 3.13 环境下可能遇到兼容性问题。

---

## ⚠️ 常见问题

### Q1: PowerShell 提示"无法加载脚本"

**错误信息**:
```
无法加载文件 xxx.ps1，因为在此系统上禁止运行脚本
```

**解决方案**:
```powershell
# 临时允许运行脚本（仅当前会话）
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

# 或使用 CMD 运行 .bat 脚本
.\scripts\setup.bat
```

---

### Q2: 虚拟环境激活失败

**解决方案**:
```powershell
# 手动激活虚拟环境
.\.venv\Scripts\Activate.ps1   # PowerShell
.\.venv\Scripts\activate.bat    # CMD
```

---

### Q3: Python 3.13 兼容性问题（lxml、greenlet 编译失败）

**错误信息**: `fatal error C1189: #error: "this header requires Py_BUILD_CORE define"`

**原因**: Python 3.13 太新，部分包还没有提供预编译版本

**推荐解决方案**: 使用 Conda 创建 Python 3.11 环境
```powershell
.\scripts\setup-conda.ps1
```

**或手动降级 Python**:
1. 卸载 Python 3.13
2. 安装 Python 3.11
3. 重新运行 `.\scripts\setup.ps1`

---

### Q4: hnswlib 安装失败

**原因**: 需要 C++ 编译器

**Windows 解决方案**:
1. 安装 [Visual Studio Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/)
2. 或安装 [Visual Studio Community](https://visualstudio.microsoft.com/)（选择"使用 C++ 的桌面开发"工作负载）

---

## 📝 手动安装步骤

如果自动脚本失败，可以手动执行以下步骤：

```powershell
# 1. 创建虚拟环境
python -m venv .venv

# 2. 激活虚拟环境
.\.venv\Scripts\Activate.ps1

# 3. 升级 pip
python -m pip install --upgrade pip

# 4. 安装依赖
python -m pip install -r requirements.txt

# 5. 复制配置文件
copy .env.example .env

# 6. 创建数据目录
mkdir .data\db, .data\vectors, .data\vault, .data\logs, .data\tmp

# 7. 运行验证
python src\utils\verify_setup.py
```

---

## 💡 提示

- 🔧 首次安装可能需要 3-5 分钟
- 📦 Conda 环境名称: `pkv-py311` (Python 3.11)
- 🔑 记得编辑 `.env` 文件填入 API Keys
- 📝 运行测试确保一切正常
- 🌟 每次使用前需要激活环境: `conda activate pkv-py311`

---

**作者**: 幽浮酱 ฅ'ω'ฅ
**日期**: 2026-02-14
