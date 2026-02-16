# Tests 测试模块

[根目录](../CLAUDE.md) > **tests**

---

## 模块职责

**全面测试覆盖**:提供单元测试、集成测试、E2E测试、黑盒测试和手动测试,确保系统质量。

### 测试层次

- **单元测试** (`unit/`): 模块级测试,Mock 外部依赖
- **集成测试** (`integration/`): 模块间协作测试
- **E2E 测试** (`e2e/`): 端到端真实环境测试
- **黑盒测试** (`blackbox/`): CLI 黑盒测试
- **手动测试** (`manual_test_*.py`): 真实环境验证

---

## 测试文件清单

### 单元测试 (unit/)

| 文件 | 测试模块 | 测试数量 |
|------|----------|---------|
| `test_processors_*.py` (7个) | 内容处理器 | 50+ |
| `test_ai_*.py` (3个) | AI 服务 | 20+ |
| `test_retrieval_*.py` (3个) | 检索引擎 | 30+ |
| `test_workflow_*.py` (3个) | 工作流引擎 | 25+ |
| `test_cli_*.py` (3个) | CLI 命令 | 17+ |

**覆盖率**: 约 85% (核心模块)

---

### 集成测试 (integration/)

| 文件 | 测试场景 |
|------|----------|
| `test_retrieval_integration.py` | 检索引擎端到端 |
| `test_workflow_integration.py` | 工作流引擎集成 |
| `test_cli_e2e.py` | CLI 端到端 |

---

### E2E 测试 (e2e/)

| 文件 | 测试场景 |
|------|----------|
| `test_real_api_workflow.py` | 真实 API 环境工作流 |

**注意**: 需要真实 API Keys (DEEPSEEK_API_KEY, OPENAI_API_KEY)

---

### 黑盒测试 (blackbox/)

| 文件 | 测试方法 |
|------|----------|
| `test_cli_basic.py` | CLI 基础黑盒测试 |
| `test_cli_blackbox.py` | CLI 完整黑盒测试 |

**测试方式**: 使用 `click.testing.CliRunner` 模拟命令行调用

---

### 手动测试脚本

| 文件 | 测试目的 |
|------|----------|
| `manual_test_ai_services.py` | AI 服务真实环境测试 |
| `manual_test_processors.py` | 内容处理器真实环境测试 |
| `manual_test_e2e_workflow.py` | E2E 工作流测试 |
| `manual_test_real_workflow.py` | 真实工作流测试 |
| `manual_test_simplified.py` | 简化工作流测试 |
| `manual_test_text_archive_safe.py` | 纯文本归档安全测试 |

**用途**:
- 需要人工判断结果
- 需要真实 API Keys
- AI 安全测试 (不影响生产数据)

---

## 运行测试

### 单元测试

```bash
# 运行所有单元测试
python -m pytest tests/unit/ -v

# 运行特定模块测试
python -m pytest tests/unit/test_processors_*.py -v
python -m pytest tests/unit/test_cli_*.py -v

# 代码覆盖率
python -m pytest tests/unit/ --cov=src --cov-report=term-missing
```

---

### 集成测试

```bash
# 运行所有集成测试(需要 API Keys)
python -m pytest tests/integration/ -v

# 在测试环境运行
$env:DB_PATH = ".data-test/db/knowledge_vault.db"
python -m pytest tests/integration/ -v
```

---

### E2E 测试

```bash
# 运行 E2E 测试(需要真实 API Keys)
python -m pytest tests/e2e/ -v --tb=short
```

---

### 黑盒测试

```bash
# 运行 CLI 黑盒测试
python -m pytest tests/blackbox/ -v
```

---

### 手动测试

```bash
# 安全的纯文本归档测试(推荐)
python tests/manual_test_text_archive_safe.py

# AI 服务测试
python tests/manual_test_ai_services.py

# 完整工作流测试
python tests/manual_test_real_workflow.py
```

---

## 测试数据 (fixtures/)

### 微信样本

- `fixtures/wechat_articles/*.html` - 微信文章 HTML 样本

### 知乎样本

- `fixtures/zhihu_content/*.html` - 知乎问答/专栏样本

### AI 聊天样本

- `fixtures/ai_chat/chatgpt_export.md` - ChatGPT 导出样本
- `fixtures/ai_chat/deepseek_export.md` - DeepSeek 导出样本

### 测试 URL

- `fixtures/test_urls.json` - 真实 URL 列表(用于集成测试)

详见: [fixtures/README.md](./fixtures/README.md)

---

## 测试环境隔离

**重要**: 所有测试应使用测试环境,不影响生产数据。

```powershell
# 方法 1: 使用测试脚本
.\scripts\run-test.ps1 <command>

# 方法 2: 手动设置环境变量
$env:DB_PATH = ".data-test/db/knowledge_vault.db"
python -m pytest tests/
```

详见: [docs/测试环境隔离指南.md](../docs/测试环境隔离指南.md)

---

## 关键配置

### pytest.ini (项目根目录)

```ini
[pytest]
testpaths = tests
python_files = test_*.py
python_classes = Test*
python_functions = test_*
addopts = -v --tb=short
```

### conftest.py

测试 fixtures 和配置(如果存在)

---

## 测试覆盖率报告

### 当前覆盖率

- **整体覆盖率**: 约 85% (核心模块)
- **单元测试**: 142+ 测试用例
- **集成测试**: 完整覆盖检索/工作流/CLI
- **E2E 测试**: 真实 API 环境验证

### 生成覆盖率报告

```bash
# 生成 HTML 报告
python -m pytest tests/unit/ --cov=src --cov-report=html

# 查看报告
open htmlcov/index.html
```

---

## 常见问题 (FAQ)

### Q1: 如何添加新测试?

1. 在对应目录创建测试文件:
```python
# tests/unit/test_my_module.py
import pytest
from src.my_module import MyClass

def test_my_function():
    result = MyClass().my_function()
    assert result == expected
```

2. 运行测试:
```bash
python -m pytest tests/unit/test_my_module.py -v
```

---

### Q2: 如何 Mock 外部依赖?

```python
from unittest.mock import patch, Mock

@patch('src.ai.deepseek_client.DeepSeekClient')
def test_with_mock(mock_client):
    mock_client.return_value.summarize.return_value = "测试摘要"
    # 测试逻辑
```

---

### Q3: 如何跳过需要 API Keys 的测试?

```python
import pytest
import os

@pytest.mark.skipif(
    not os.getenv("DEEPSEEK_API_KEY"),
    reason="需要 DEEPSEEK_API_KEY"
)
def test_with_api():
    # 测试逻辑
```

---

## 相关文件

| 文件 | 说明 |
|------|------|
| [fixtures/README.md](./fixtures/README.md) | 测试数据说明 |
| [conftest.py](./conftest.py) | pytest 配置 (如果存在) |
| [docs/测试环境隔离指南.md](../docs/测试环境隔离指南.md) | 测试环境文档 |

---

## 变更记录 (Changelog)

### 2026-02-16 18:51
- 生成 Tests 模块 CLAUDE.md 文档
- 补充测试覆盖率和测试环境隔离说明

### 2026-02-16 (v0.6.1)
- 新增 `manual_test_text_archive_safe.py` (纯文本归档安全测试)
- 新增 CLI 黑盒测试 (`tests/blackbox/`)
- 新增 E2E 真实 API 测试 (`tests/e2e/`)

### 2026-02-14 (M1-M5)
- 完成核心模块单元测试 (142+ 测试用例)
- 完成检索引擎集成测试
- 完成工作流引擎集成测试

---

**模块维护者**: AI Agent
**最后更新**: 2026-02-16 18:51:32

*本文档由 Claude Code 自动生成*
