# 🚀 运行我！- 快速开始指南

> **主人，浮浮酱已经为你准备好了一切！** ฅ'ω'ฅ
>
> 推荐使用 **Conda 方式**安装，可以避免 Python 3.13 兼容性问题喵～
>
> **当前边界**：本仓库的 Windows-first Developer Preview 支持 CLI 与 MCP stdio；桌面 GUI 已迁至独立的 `pkv-GUI` 仓库。MCP HTTP/Bearer 和 `search.yaml` 不受支持。默认验证离线，只使用 `.data-test` 合成数据，不需要也不得读取真实 API key、真实 Provider 或真实 Vault。

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
- ✅ 创建 Python 3.11 Conda 环境 (`py311-private`)
- ✅ 安装所有依赖包
- ✅ 创建本机配置文件 (`config/local.yaml`)
- ✅ 创建数据目录 (`.data/`)

**预计时间**: 3-5 分钟

#### Step 2: 可选配置 Provider

```powershell
# 编辑本机私有配置
notepad config\local.yaml
```

BM25、浏览、MCP stdio 能力发现和安装验证不需要 API Key。只有用户主动使用摘要、Chat、向量或混合检索时，才在 `ai.llm.*` / `ai.embedding.*` 中填写对应 Provider 配置；不要把密钥写入仓库、日志或自动化输出。

> **如果你还没有 API Keys**:
> - LLM: 使用 OpenAI-compatible Chat Completions 服务
> - Embedding: 使用 OpenAI-compatible Embeddings 服务

#### Step 3: 运行验证测试

```powershell
# 运行 Conda 测试脚本
.\scripts\test-conda.ps1

# 或手动运行
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\verify-setup -Command @("python", "src\utils\verify_setup.py")
```

如果看到 `✅ 所有测试通过！系统安装正确！`，就说明成功了！🎉

---

### 方式 B: Legacy venv 安装（不推荐，可能遇到兼容性问题）

⚠️ **警告**: 此方案在 Python 3.13 环境下可能遇到编译错误。

如果确实无法使用 Conda，请查看:
- 📖 [scripts/legacy/README.md](scripts/legacy/README.md) - Legacy 脚本说明

---

## ❓ 遇到问题了？

### 问题 1: PowerShell 提示"无法加载脚本"

**解决方案**:
```powershell
# 临时允许运行脚本
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

# 然后重新运行
.\scripts\legacy\setup.ps1
```

**或者使用 CMD 脚本**:
```cmd
.\scripts\legacy\setup.bat
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
3. 重新运行 `.\scripts\legacy\setup.ps1`

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
- 📖 [docs/operations/QUICKSTART.md](docs/operations/QUICKSTART.md) - 完整安装指南
- 📖 [docs/operations/CHANGELOG.md](docs/operations/CHANGELOG.md) - 当前变更与验证记录

---

## ✅ 安装成功后

恭喜！你已经完成了当前 Developer Preview 的本地安装。

### 可用入口

```powershell
# CLI 命令与帮助
python -m src.main --help

# MCP stdio server
python -m src.mcp.server
```

当前默认发布面是 Windows-first、离线、fresh-install 的 Developer Preview。CLI 与 MCP stdio 可用；GUI 由独立仓库发布。MCP HTTP/Bearer、真实数据验收和原地升级不在本阶段承诺范围内。

### 下一步

- 查看 [当前战略与路线收敛](docs/overview/当前战略与路线收敛-2026-03.md)
- 查看 [阶段开发路线与依赖](docs/overview/阶段开发路线与依赖-2026-03.md)
- 按 [使用手册](docs/operations/使用手册.md) 选择 CLI 或 MCP stdio 工作流

---

## 📚 推荐阅读

- 📖 [项目立项文档](docs/overview/项目立项文档.md) - 了解项目愿景
- 📖 [架构设计](docs/overview/架构设计.md) - 深入理解系统架构
- 📖 [当前事实基线](docs/overview/当前事实基线-2026-03.md) - 当前能力与边界
- 📖 [Phase 1 开发计划（已归档）](docs/history/prompts/PHASE1_DEV_PROMPT.md) - 追溯历史路线
- 📖 [Milestone 1 完成报告（历史）](docs/history/milestones/MILESTONE1_COMPLETE.md) - 历史成果记录

---

**准备好了吗？让我们开始吧！** 🚀

---

*本文档反映当前 Windows-first Developer Preview 的快速开始边界。*
