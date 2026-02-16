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

### 测试环境管理 🧪

#### `run-test.ps1` - 测试环境运行脚本

**用途**: 使用隔离的测试数据库运行 PKV 命令（不影响生产数据）

**运行方式**:
```powershell
# 在测试环境归档网页
.\scripts\run-test.ps1 archive "https://example.com"

# 在测试环境搜索
.\scripts\run-test.ps1 search "AI"

# 查看测试环境统计
.\scripts\run-test.ps1 stats
```

**功能**:
- ✅ 自动加载 `.env.test` 测试配置
- ✅ 隔离测试数据到 `.data-test/` 目录
- ✅ 自动创建测试目录结构
- ✅ 显示测试环境状态

**使用场景**:
- 测试新功能而不影响生产数据
- 验证数据库变更
- 开发调试

---

#### `backup-data.ps1` - 数据备份脚本

**用途**: 备份生产数据到 `.data-backup/` 目录

**运行方式**:
```powershell
# 手动备份
.\scripts\backup-data.ps1

# 带说明的备份
.\scripts\backup-data.ps1 -Message "重要更新前的备份"
```

**功能**:
- ✅ 完整备份 `.data/` 目录
- ✅ 生成备份信息文件（时间戳、大小、说明）
- ✅ 显示最近的 5 个备份
- ✅ 自动计算备份大小和文件数

**最佳实践**:
- 重要变更前先备份
- 定期清理旧备份（手动）

---

#### `restore-data.ps1` - 数据恢复脚本

**用途**: 从备份恢复数据

**运行方式**:
```powershell
# 交互式选择备份恢复
.\scripts\restore-data.ps1

# 恢复指定时间戳的备份
.\scripts\restore-data.ps1 -BackupTimestamp "20260216-143000"
```

**功能**:
- ✅ 列出所有可用备份（含详细信息）
- ✅ 交互式选择备份版本
- ✅ 安全确认机制（需输入 YES）
- ✅ 自动验证恢复结果

**警告**:
- ⚠️ 恢复操作会**完全替换**当前 `.data/` 目录
- ⚠️ 建议先备份当前数据再恢复

---

#### `.env.test.example` - 测试环境配置模板

**用途**: 测试环境配置示例文件

**使用方式**:
```powershell
# 复制模板文件
copy .env.test.example .env.test

# 编辑配置
notepad .env.test
```

**配置项**:
```env
# 数据库路径（使用测试专用目录）
DB_PATH=.data-test/db/knowledge_vault.db

# 测试用 API Keys（可选）
DEEPSEEK_API_KEY=sk-test-your-key
OPENAI_API_KEY=sk-test-your-key

# 日志级别（DEBUG 获取详细日志）
LOG_LEVEL=DEBUG
```

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
