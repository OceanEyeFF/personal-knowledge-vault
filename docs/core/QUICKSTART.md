# Personal Knowledge Vault - 快速开始

> 3 步快速启动指南 🚀

## 📋 前置要求

**推荐配置**:
- **Conda** (Miniconda 或 Anaconda) 🌟
  - 下载: https://docs.conda.io/en/latest/miniconda.html
- **Git**（用于版本控制）

**或传统配置**:
- **Python 3.11** (⚠️ 不推荐 3.13，存在兼容性问题)
- **Git**

**必需 API Keys**:
- DeepSeek API Key（用于摘要生成）
- OpenAI API Key（用于 Embedding）

---

## 🚀 快速安装（推荐：Conda 方式）

### Step 1: 运行 Conda 安装脚本

```powershell
# Windows PowerShell
.\scripts\setup-conda.ps1
```

这个脚本会自动：
- ✅ 创建 Python 3.11 Conda 环境 (`pkv-py311`)
- ✅ 安装所有依赖包
- ✅ 创建 .env 配置文件
- ✅ 创建数据目录

**预计时间**: 3-5 分钟

### Step 2: 配置 API Keys

```powershell
notepad .env
```

填入你的 API Keys:
```bash
DEEPSEEK_API_KEY=sk-你的-deepseek-api-key
OPENAI_API_KEY=sk-你的-openai-api-key
```

> **获取 API Keys**:
> - DeepSeek: https://platform.deepseek.com/
> - OpenAI: https://platform.openai.com/

### Step 3: 运行验证测试

```powershell
.\scripts\test-conda.ps1
```

如果看到 `✅ 所有测试通过！系统安装正确！` 就说明安装成功了！

---

## 📦 传统安装（venv 方式）

如果不使用 Conda，可以使用传统的 venv 方式：

### Windows PowerShell

```powershell
# Step 1: 运行安装脚本
.\scripts\setup.ps1

# Step 2: 配置 API Keys
notepad .env

# Step 3: 运行测试
.\scripts\test.ps1
```

### Windows CMD

```cmd
# Step 1: 运行安装脚本
.\scripts\setup.bat

# Step 2: 配置 API Keys
notepad .env

# Step 3: 激活环境并测试
.\.venv\Scripts\activate.bat
python src\utils\verify_setup.py
```

---

## 📝 下一步

安装完成后，你可以：

1. **初始化数据库**:
   ```bash
   python -m src.storage.sqlite_store
   ```

2. **测试 Markdown 存储**:
   ```bash
   python src/utils/verify_setup.py
   ```

3. **阅读完整文档**:
   - [架构设计](docs/架构设计.md)
   - [数据规范](docs/数据规范.md)
   - [Phase 1 开发计划](docs/core/PHASE1_DEV_PROMPT.md)

---

## ⚠️ 常见问题

### Q1: Python 3.13 兼容性问题

**症状**: `lxml` 或 `greenlet` 编译失败，错误信息包含 `Py_BUILD_CORE`

**解决方案**: 使用 Conda 创建 Python 3.11 环境
```powershell
.\scripts\setup-conda.ps1
```

**或手动降级**:
1. 卸载 Python 3.13
2. 安装 Python 3.11
3. 重新运行安装脚本

---

### Q2: 安装 hnswlib 失败

**Windows 用户**:
```bash
# 需要安装 Visual Studio Build Tools
# 下载地址: https://visualstudio.microsoft.com/visual-cpp-build-tools/
```

**macOS/Linux 用户**:
```bash
# 确保已安装编译器
# macOS: xcode-select --install
# Linux: sudo apt-get install build-essential
```

### Q2: jieba 分词不准确

浮浮酱已经创建了自定义词典 `config/custom_dict.txt`，你可以添加自己的专业术语喵～

### Q3: 数据存储在哪里？

所有数据都在 `.data/` 目录下（已在 `.gitignore` 中忽略）：
- `.data/vault/` - Markdown 文件
- `.data/db/` - SQLite 数据库
- `.data/vectors/` - 向量索引
- `.data/logs/` - 日志文件

---

## 🎉 成功！

如果验证通过，恭喜你完成了 Personal Knowledge Vault 的基础设施层搭建！

接下来浮浮酱会继续开发 AI 服务层、内容处理器和检索引擎喵～ φ(≧ω≦*)♪

---

**版本**: v0.1.0
**创建日期**: 2026-02-14
**作者**: 幽浮喵 ฅ'ω'ฅ
