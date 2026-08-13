# Personal Knowledge Vault - 快速开始

> 当前版本快速启动指南
> 适用于想先把系统跑起来，再决定从 CLI、MCP 还是 GUI 进入的用户

**最后更新**: 2026-08-07

---

## 1. 你会得到什么

完成本指南后，你将得到一个可运行的 PKV 本地环境，包含：

- CLI 入口
- MCP Server stdio 入口
- GUI 桌面入口
- 本地数据目录（Markdown / SQLite / 向量索引 / 日志）

---

## 2. 前置要求

### 推荐配置

- Conda（Miniconda 或 Anaconda）
- Python 3.11+
- Git

### 可选 Provider 配置

- `ai.llm.*`：仅在用户手动使用摘要、标签或 Chat 等 Provider-backed 能力时需要
- `ai.embedding.*`：仅在用户手动使用向量/混合检索或生成 Embedding 时需要

BM25、浏览、MCP stdio 能力发现及默认自动化验证均不需要真实 API Key。项目默认验证必须离线，只使用 `.data-test` 隔离根和合成数据，不连接真实 Provider，也不读取真实 Vault。

---

## 3. 推荐安装方式（Conda）

### Step 1：运行安装脚本

```powershell
.\scripts\setup-conda.ps1
```

该脚本会完成：

- 创建 Python 3.11 Conda 环境
- 安装依赖
- 初始化基础目录

### Step 2：配置本机 YAML

```powershell
notepad config/local.yaml
```

`config/local.yaml` 已被 Git 忽略。只有需要 Provider-backed 能力时才填写以下内容：

```yaml
ai:
  llm:
    api_key: "your-llm-key"
  embedding:
    api_key: "your-embedding-key"
```

### Step 3：运行验证

```powershell
.\scripts\test-conda.ps1
```

如果验证通过，说明基础环境已就绪。

---

## 4. 传统安装方式（venv）

如果你不使用 Conda，可以手动创建虚拟环境：

```bash
python -m venv .venv
```

激活后安装依赖：

```bash
pip install -r requirements.txt
```

然后复制并编辑 `config/local.yaml`，再运行：

```powershell
Copy-Item config/config.yaml config/local.yaml
python src/utils/verify_setup.py
```

---

## 5. 当前推荐入口

PKV 当前有三个主要入口。

### 5.1 CLI

查看帮助：

```powershell
.\scripts\run-windows.ps1 python -m src.main --help
```

常用命令（源码 checkout）：

```bash
python -m src.main archive https://example.com/article
python -m src.main archive-text "一条本地笔记" --title "示例笔记"
python -m src.main search "关键词"
python -m src.main show 1
python -m src.main list --limit 20
python -m src.main tags --format json
python -m src.main related 1 --format json
python -m src.main stats
```

受控安装的 Windows held test candidate 才提供 pkv.exe；它仍处于 compliance
hold，不应被表述为正式发布。安装与验收步骤见
[用户指南](release/USER-GUIDE.md)。

### 5.2 MCP Server

stdio 模式：

```bash
python -m src.mcp
```

M13 Developer Preview 只支持 stdio；`streamable-http` 与 Bearer Token 认证未进入发布面，不能作为启动或部署方式。

适用场景：

- Claude Code
- Codex
- Cursor
- 其他 MCP Client

已发现的 `find_bridges`、`timeline_of`、`contrast` 仍是 `partial-v1`：响应会继续声明 `implementation_level=partial` 并给出 `limitation_notes`，不能按 full 语义理解。

### 5.3 GUI

启动桌面应用：

```bash
python -m src.gui
```

适用场景：

- 浏览知识条目
- BM25 搜索结果检查
- 归档状态查看
- 配置和调试

---

## 6. 运行后你应该看到什么

### 数据目录

系统运行后，数据一般位于 `.data/`：

- `.data/vault/`：Markdown 主存储
- `.data/db/`：SQLite 数据库
- `.data/vectors/`：向量索引
- `.data/logs/`：日志文件

### 核心代码入口

- `src/main.py`：CLI
- `src/mcp/`：MCP Server
- `src/gui/`：GUI 应用

---

## 7. 推荐阅读顺序

如果你刚接手项目，建议按下面顺序看文档：

1. [当前战略与路线收敛-2026-03.md](../overview/当前战略与路线收敛-2026-03.md)
2. [personal-knowledge-vault-prd.md](../overview/personal-knowledge-vault-prd.md)
3. [架构设计.md](../overview/架构设计.md)
4. [技术选型.md](../overview/技术选型.md)
5. [项目结构说明.md](../overview/项目结构说明.md)

如果你需要查看历史执行上下文，请到：

- `docs/history/prompts/`

而不是继续把历史 Prompt 当作当前核心路线文档。

---

## 8. 常见问题

### Q1：Python 3.13 兼容性问题

如果出现 `lxml`、`greenlet` 或构建相关报错，优先使用 Python 3.11。

### Q2：hnswlib 安装失败

Windows 通常需要 C++ Build Tools。  
macOS / Linux 需要系统编译工具链。

### Q3：数据存储在哪里

默认在 `.data/` 目录，而不是 `docs/`。

### Q4：应该优先用哪个入口

建议：

- 调试和批量操作优先 `CLI`
- AI Agent 集成优先 `MCP`
- 浏览和检查优先 `GUI`

---

## 9. 一句话建议

如果你只是想确认系统可用，最短路径是：

1. 跑安装脚本
2. 配 `config/local.yaml`
3. 启动 CLI 或 MCP
4. 归档一条内容再搜索一次
