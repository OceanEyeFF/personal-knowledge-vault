# 🚀 运行我！- 快速开始指南

> **主人，浮浮酱已经为你准备好了一切！** ฅ'ω'ฅ
>
> 推荐使用 **Conda 方式**安装，可以避免 Python 3.13 兼容性问题喵～

---

## 📋 开始前的准备

**推荐配置**:
- ✅ **Conda** (Miniconda 或 Anaconda) 🌟
  - 下载: https://docs.conda.io/en/latest/miniconda.html
- ✅ Git

**或传统配置**:
- ✅ Python 3.11 (⚠️ 不推荐 3.13，有兼容性问题)
- ✅ Git

---

## 🎯 安装方式

### 🌟 方式 A: Conda 安装（强烈推荐）

**为什么推荐？**
- ✨ 自动创建 Python 3.11 环境
- ✨ 避免 Python 3.13 兼容性问题
- ✨ 环境隔离更彻底

#### Step 1: 运行 Conda 安装脚本

```powershell
# 在项目根目录下打开 PowerShell，然后运行：
.\scripts\setup-conda.ps1
```

**这个脚本会自动完成**:
- ✅ 创建 Python 3.11 Conda 环境 (`pkv-py311`)
- ✅ 安装所有依赖包
- ✅ 创建配置文件 (`.env`)
- ✅ 创建数据目录 (`.data/`)

**预计时间**: 3-5 分钟

#### Step 2: 配置 API Keys

```powershell
# 编辑 .env 文件
notepad .env
```

填入你的 API Keys:
```bash
DEEPSEEK_API_KEY=sk-你的-deepseek-api-key
OPENAI_API_KEY=sk-你的-openai-api-key
```

> **如果你还没有 API Keys**:
> - DeepSeek: https://platform.deepseek.com/
> - OpenAI: https://platform.openai.com/

#### Step 3: 运行验证测试

```powershell
# 运行 Conda 测试脚本
.\scripts\test-conda.ps1

# 或手动运行
conda activate pkv-py311
python src\utils\verify_setup.py
```

如果看到 `✅ 所有测试通过！系统安装正确！`，就说明成功了！🎉

---

### 方式 B: Legacy venv 安装（不推荐，可能遇到兼容性问题）

⚠️ **警告**: 此方案在 Python 3.13 环境下可能遇到编译错误。

如果确实无法使用 Conda，请查看:
- 📖 [scripts/legacy/README.md](scripts/legacy/README.md) - Legacy 脚本说明

---

### Step 2: 配置 API Keys

```powershell
# 编辑 .env 文件
notepad .env
```

填入你的 API Keys:
```bash
DEEPSEEK_API_KEY=sk-你的-deepseek-api-key
OPENAI_API_KEY=sk-你的-openai-api-key
```

> **如果你还没有 API Keys**:
> - DeepSeek: https://platform.deepseek.com/
> - OpenAI: https://platform.openai.com/

---

### Step 3: 运行验证测试

```powershell
# 运行测试脚本
.\scripts\test.ps1

# 或手动运行
.\.venv\Scripts\Activate.ps1
python src\utils\verify_setup.py
```

如果看到 `✅ 所有测试通过！系统安装正确！`，就说明成功了！🎉

---

## ❓ 遇到问题了？

### 问题 1: PowerShell 提示"无法加载脚本"

**解决方案**:
```powershell
# 临时允许运行脚本
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

# 然后重新运行
.\scripts\setup.ps1
```

**或者使用 CMD 脚本**:
```cmd
.\scripts\setup.bat
```

---

### 问题 2: Python 3.13 兼容性问题

**错误**: `lxml` 或 `greenlet` 编译失败

**症状**:
```
fatal error C1189: #error: "this header requires Py_BUILD_CORE define"
```

**推荐解决方案**: 使用 Conda 安装（会自动使用 Python 3.11）
```powershell
.\scripts\setup-conda.ps1
```

**或手动降级 Python**:
1. 卸载 Python 3.13
2. 安装 Python 3.11
3. 重新运行 `.\scripts\setup.ps1`

---

### 问题 3: hnswlib 安装失败

**原因**: 需要 C++ 编译器

**解决方案**:
1. 安装 [Visual Studio Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/)
2. 选择"使用 C++ 的桌面开发"工作负载
3. 重新运行安装脚本

---

### 问题 4: 其他问题

请查看:
- 📖 [scripts/README.md](scripts/README.md) - 脚本详细说明
- 📖 [docs/QUICKSTART.md](docs/QUICKSTART.md) - 完整安装指南
- 📖 [docs/VALIDATION_REPORT.md](docs/VALIDATION_REPORT.md) - 验证报告

---

## ✅ 安装成功后

恭喜！你已经完成了 **Milestone 1: 基础设施层** 的搭建！

### 已经实现的功能：

1. ✅ **配置系统** - YAML + 环境变量
2. ✅ **Markdown 存储** - YAML Front Matter 支持
3. ✅ **SQLite 存储** - 完整 Schema + FTS5 全文搜索
4. ✅ **向量存储** - hnswlib HNSW 算法
5. ✅ **文本处理** - jieba 中文分词

### 下一步开发计划：

- 🚧 Milestone 2: AI 服务封装 (DeepSeek、OpenAI)
- 🚧 Milestone 3: 内容处理器 (微信、知乎、通用网页)
- 🚧 Milestone 4: 检索引擎 (BM25、向量、混合检索)
- 🚧 Milestone 5: 工作流引擎
- 🚧 Milestone 6: CLI 入口
- 🚧 Milestone 7: 文档和交付

---

## 📚 推荐阅读

- 📖 [项目立项文档](docs/项目立项文档.md) - 了解项目愿景
- 📖 [架构设计](docs/架构设计.md) - 深入理解系统架构
- 📖 [开发计划](docs/STARTER_PROMPT.md) - 完整开发计划
- 📖 [完成报告](docs/MILESTONE1_COMPLETE.md) - Milestone 1 成果

---

**准备好了吗？让我们开始吧！** 🚀

---

*作者: 幽浮酱 ฅ'ω'ฅ*
*日期: 2026-02-14*
*版本: v0.1.0*
