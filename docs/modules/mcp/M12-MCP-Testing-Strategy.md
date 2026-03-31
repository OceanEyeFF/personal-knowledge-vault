# M12 MCP 真实场景测试方案

> 三层递进式测试框架：Inspector 快速验证 → Python 脚本全流程 → pytest E2E 套件长期维护

**核心理念**：充分利用已有的测试环境隔离方案（`.data-test/` + `.env.test`），循序渐进地验证 MCP 层的可用性。

当前状态补注（2026-03-11）：

- 当前代码基线共 `14` 个 Tool，其中 `query_subgraph`、`explain_relation`、`collect_evidence` 已可调用
- `find_bridges`、`timeline_of`、`contrast` 也已接入 MCP，但仍属于 partial implementation
- 当前自动化验证已经覆盖 unit / integration / blackbox；完整 E2E 仍需继续补齐

---

## 一、测试层次设计

### 层 1：MCP Inspector 快速验证（5-10 分钟）

**目标**：快速确认 MCP Server 启动、Tool/Resource/Prompt 注册成功

**工具**：`@modelcontextprotocol/inspector`（MCP 官方 CLI）

**步骤**：

```bash
# 1. 启动 MCP Server (stdio 模式)
cd E:/gitee/personal-knowledge-vault
python -m src.mcp.server

# 2. 另开终端，启动 Inspector (需要 Node.js)
npx @modelcontextprotocol/inspector python -m src.mcp.server

# 3. 浏览器打开 http://localhost:5173，交互式测试：
#    - 列出所有 Tool/Resource/Prompt
#    - 逐一调用 Tool，查看返回值
#    - 验证参数验证 (Schema)
#    - 测试错误处理路径
```

**检查清单**：

- [ ] 14 个 Tool 全部列出 (12 只读 + 2 写入)
- [ ] 4 个 Resource 全部列出
- [ ] 3 个 Prompt 全部列出
- [ ] `search_knowledge "AI"` → 返回搜索结果
- [ ] `get_entry {valid_id}` → 返回知识条目
- [ ] `query_subgraph {knowledge_id}` → 返回 nodes / edges / truncated
- [ ] `explain_relation {source_id, target_id}` → 返回 summary / path
- [ ] `collect_evidence {question}` → 返回 seed / evidence[]
- [ ] `find_bridges {seed_id}` → 返回 `implementation_level=partial`，且 `evidence_sources` 中包含 `graph_bridge_signal`
- [ ] `timeline_of {topic}` → 当存在真实时间字段时返回 `inferred_time_field=event_time` 或 `published_at`，并包含 `structured_time_fields`
- [ ] `contrast {topic_a, topic_b}` → 返回 `shared_tags / only_a_tags / only_b_tags`，且 `comparison_dimensions` 中包含 `relation_graph_signal`
- [ ] `archive_url "https://example.com"` → 成功/失败响应
- [ ] 无效参数测试 → 返回合理的错误信息
- [ ] 大数据量返回 → 不超时不崩溃

**优势**：直观、快速、官方工具、无需写代码

---

### 层 2：Python 脚本全流程测试（15-30 分钟）

**目标**：模拟真实的 Claude Code 客户端行为，验证完整工作流

**环境**：使用隔离的测试数据库 (`.data-test/db/knowledge_vault.db`)

**脚本位置**：`tests/blackbox/test_mcp_client_simulation.py`

**测试场景**（6 个核心场景）：

#### 场景 1：知识库搜索流程

```python
# 模拟：Claude Code 用户问 "什么是 AI 工作流"
# 1. search_knowledge("AI 工作流")
# 2. 获得 3 条结果
# 3. get_entry(第一条结果的 ID)
# 4. 获得全文内容
# 验证：搜索准确率、内容完整性
```

#### 场景 2：快速归档 URL + 即刻搜索

```python
# 模拟：Claude Code 用户边对话边归档
# 1. archive_url("https://mp.weixin.qq.com/...")
# 2. 等待 2s (模拟用户思考时间)
# 3. search_knowledge("刚才那个文章的关键词")
# 4. 验证新归档的内容已出现在搜索结果中
```

#### 场景 3：知识库内容与引用卡片

```python
# 模拟：Claude Code 用户获取引用卡片
# 1. get_related(知识条目 ID)  # 获取相关条目
# 2. 批量调用 get_entry() 获取完整信息
# 3. format_reference_card_html() 生成引用卡片
# 验证：卡片格式、链接有效性、元信息准确
```

#### 场景 4：搜索建议 Prompt

```python
# 模拟：Claude Code 用户在 Prompt 中实现搜索建议
# 1. 调用 Prompt: search_and_summarize("关键词")
# 2. 验证 Prompt 模板包含 {query}, {results} 变量
# 3. 实际代入数据验证格式
```

#### 场景 5：知识库 QA 对话

```python
# 模拟：Claude Code 在知识库上进行 QA
# 1. 调用 Prompt: knowledge_qa("问题")
# 2. 获得包含检索结果的系统提示词
# 3. 验证上下文格式和数据有效性
```

#### 场景 6：思想磨砺工作流

```python
# 模拟：Claude Code 用户的创意碰撞会话
# 1. 调用 Prompt: idea_sharpen("想法")
# 2. 可选调用 search_knowledge() 搜索相关灵感
# 3. 验证 Prompt 是否正确引入背景知识
```

**脚本框架**：

```python
# tests/blackbox/test_mcp_client_simulation.py
import asyncio
import json
from pathlib import Path
from src.mcp.server import MCPServer  # 启动 MCP Server 进程内

class MCPClientSimulator:
    """模拟 Claude Code MCP 客户端的行为"""

    def __init__(self, mcp_server):
        self.server = mcp_server

    async def search_and_display(self, query: str):
        """场景 1: 搜索"""
        result = await self.server.call_tool("search_knowledge", {"query": query})
        return json.loads(result)

    async def archive_and_verify(self, url: str):
        """场景 2: 归档后验证"""
        archive_result = await self.server.call_tool("archive_url", {"url": url})
        # 等待异步任务完成
        await asyncio.sleep(2)
        # 验证已出现在搜索结果
        ...

    # 其他场景方法...

async def test_scenario_1_search():
    """测试场景 1"""
    simulator = MCPClientSimulator(...)
    results = await simulator.search_and_display("AI 工作流")
    assert len(results) > 0
    assert "标题" in results[0]
    ...

# 使用 pytest 运行
# pytest tests/blackbox/test_mcp_client_simulation.py -v
```

**执行方式**：

```bash
# 自动化模式（不需要启动服务）
pytest tests/blackbox/test_mcp_client_simulation.py -v

# 或手动模式（debug 用）
python -m src.mcp.server &
python tests/blackbox/test_mcp_client_simulation.py
```

**输出示例**：

```
✓ 场景 1: 知识库搜索 - 3 条结果
✓ 场景 2: 归档 URL + 即刻搜索 - 内容已同步
✓ 场景 3: 引用卡片生成 - 格式正确
✓ 场景 4: 搜索建议 Prompt - 模板有效
✓ 场景 5: 知识库 QA Prompt - 上下文完整
✓ 场景 6: 思想磨砺 Prompt - 灵感补充成功
```

---

### 层 3：pytest E2E 套件长期维护（30-60 分钟初建，后续自动化）

**目标**：完整的回归测试套件，可持续运行，检测 MCP 功能退化

**文件结构**：

```
tests/e2e/
├── conftest.py                        # 共享 fixtures (MCP Server, 测试数据库)
├── test_mcp_e2e_search.py            # 搜索相关 E2E
├── test_mcp_e2e_archive.py           # 归档相关 E2E
├── test_mcp_e2e_knowledge_qa.py      # QA Prompt E2E
└── test_mcp_e2e_full_workflow.py     # 完整工作流 E2E
```

**核心 Fixture** (`conftest.py`)：

```python
import pytest
import os
from src.mcp.server import MCPServer
from src.storage.sqlite_store import SQLiteStore
from pathlib import Path

@pytest.fixture(scope="session")
def test_env():
    """设置测试环境 (使用 .data-test/)"""
    os.environ["DB_PATH"] = ".data-test/db/knowledge_vault.db"
    os.environ["DATA_DIR"] = ".data-test/vault"
    os.environ["VECTOR_DIR"] = ".data-test/vectors"
    yield
    # 清理 (可选)

@pytest.fixture
def mcp_server(test_env):
    """为每个测试启动 MCP Server"""
    server = MCPServer()
    # 内存模式或使用 .data-test 数据库
    yield server
    # 关闭

@pytest.fixture
def sample_knowledge_db(mcp_server):
    """插入测试数据"""
    store = SQLiteStore()
    # 插入 10 条知识条目（混合来源：微信、知乎、文本）
    test_entries = [
        {"title": "AI 工作流初探", "source_type": "wechat", "content": "..."},
        # ... 更多条目
    ]
    for entry in test_entries:
        store.add_entry(entry)
    yield store
    # 测试后清理
```

**E2E 测试示例**：

```python
# tests/e2e/test_mcp_e2e_search.py
import pytest

class TestMCPSearchE2E:
    """MCP 搜索功能完整 E2E"""

    @pytest.mark.asyncio
    async def test_search_by_keyword(self, mcp_server, sample_knowledge_db):
        """E2E: 按关键词搜索"""
        result = await mcp_server.call_tool(
            "search_knowledge",
            {"query": "AI 工作流", "strategy": "auto"}
        )
        entries = json.loads(result)

        assert len(entries) >= 3
        assert entries[0]["title"] == "AI 工作流初探"
        assert "content" in entries[0]

    @pytest.mark.asyncio
    async def test_search_with_tag_filter(self, mcp_server, sample_knowledge_db):
        """E2E: 带标签过滤的搜索"""
        # 先为某条条目添加标签
        sample_knowledge_db.update_tags("entry_id", ["AI", "工作流"])

        result = await mcp_server.call_tool(
            "search_knowledge",
            {"query": "工作流", "tag_filter": "AI"}
        )

        entries = json.loads(result)
        assert all("AI" in e["tags"] for e in entries)

    @pytest.mark.asyncio
    async def test_search_empty_result(self, mcp_server):
        """E2E: 无搜索结果处理"""
        result = await mcp_server.call_tool(
            "search_knowledge",
            {"query": "完全不存在的关键词 xyz123"}
        )

        entries = json.loads(result)
        assert len(entries) == 0
```

**执行与报告**：

```bash
# 运行所有 E2E 测试
pytest tests/e2e/test_mcp_e2e_*.py -v --tb=short

# 仅运行搜索相关
pytest tests/e2e/test_mcp_e2e_search.py -v

# 生成覆盖率报告
pytest tests/e2e/ --cov=src.mcp --cov-report=html
```

---

## 二、测试数据库准备

### 使用已有的测试 Fixtures

浮浮酱发现项目里已有现成的测试数据喵～ 复用方案：

```python
# tests/fixtures/ 中的样本数据
- test_urls.json              # 微信/知乎/通用网页 URL 样本
- wechat_sample.html         # 微信文章样本
- zhihu_sample.html          # 知乎文章样本
- ai_chat_sample.json        # AI 聊天记录样本

# 使用方式：
from tests.fixtures.sample_data import WECHAT_SAMPLES, ZHIHU_SAMPLES

@pytest.fixture
def populate_test_db():
    """使用样本数据填充测试数据库"""
    store = SQLiteStore(db_path=".data-test/db/knowledge_vault.db")

    # 导入微信样本 (3 篇)
    for url, html in WECHAT_SAMPLES:
        entry = process_wechat(url, html)
        store.add_entry(entry)

    # 导入知乎样本 (3 篇)
    for url, html in ZHIHU_SAMPLES:
        entry = process_zhihu(url, html)
        store.add_entry(entry)

    # 插入纯文本条目 (5 篇)
    for text in TEXT_SAMPLES:
        entry = create_text_entry(text)
        store.add_entry(entry)

    yield store
    # 清理
```

### 一键生成测试数据库

新建辅助脚本 `scripts/setup-test-db.py`：

```bash
# 生成包含 20 条样本条目的测试数据库
python scripts/setup-test-db.py --seed 42 --count 20 --output .data-test/db/knowledge_vault.db
```

---

## 三、持续集成建议

### CI/CD 流水线

```yaml
# .github/workflows/mcp-test.yml (GitHub Actions 示例)
name: MCP E2E Tests

on: [push, pull_request]

jobs:
  mcp-e2e:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.11'

      # Layer 1: 快速单元测试 (已有)
      - name: Unit Tests
        run: pytest tests/unit/test_mcp_*.py -v

      # Layer 2: Python 脚本模拟测试
      - name: Client Simulation
        run: pytest tests/blackbox/test_mcp_client_simulation.py -v

      # Layer 3: E2E 测试
      - name: E2E Tests
        run: pytest tests/e2e/test_mcp_e2e_*.py -v --cov=src.mcp

      - name: Upload Coverage
        uses: codecov/codecov-action@v3
```

---

## 四、快速开始指南

### 第一天：Inspector 快速验证（10 分钟）

```bash
# 安装 Node.js 依赖
npm install -g @modelcontextprotocol/inspector

# 启动 MCP Server
cd E:/gitee/personal-knowledge-vault
python -m src.mcp.server

# 另开终端
npx @modelcontextprotocol/inspector python -m src.mcp.server

# 浏览器打开 http://localhost:5173
# 交互式测试每个 Tool
```

**预期结果**：8 Tools + 4 Resources + 3 Prompts 全部可用 ✅

---

### 第二天：Python 脚本全流程（30 分钟）

```bash
# 1. 创建测试数据库
python scripts/setup-test-db.py --count 20

# 2. 运行模拟客户端测试
pytest tests/blackbox/test_mcp_client_simulation.py -v -s

# 3. 查看完整日志
```

**预期结果**：6 个场景全部通过 ✅

---

### 第三天：建立 E2E 套件（60 分钟）

```bash
# 1. 编写 E2E 测试用例（参考上述代码）
# 2. 运行 E2E 测试
pytest tests/e2e/test_mcp_e2e_*.py -v

# 3. 查看覆盖率报告
pytest tests/e2e/ --cov=src.mcp --cov-report=html
open htmlcov/index.html
```

**预期结果**：MCP 层覆盖率 >90% ✅

---

## 五、避坑指南

### 常见问题

| 问题 | 原因 | 解决 |
|------|------|------|
| `ModuleNotFoundError: No module named 'mcp'` | MCP SDK 未安装 | `pip install mcp>=0.1.0` |
| `SSRF 防护拒绝本地 URL` | 安全验证生效 | 改用实际网址或禁用防护（仅测试环境） |
| Inspector 连接超时 | MCP Server 未启动 | 检查 stdio 通信 |
| 搜索结果为空 | 测试数据库未填充 | 运行 `setup-test-db.py` |
| Token 限制告警 | 测试数据过大 | 减少 `--count` 参数 |

---

## 六、验收标准

MCP 真实场景测试完成的标志：

- [ ] Layer 1: Inspector 中所有 Tool/Resource/Prompt 均可调用，无异常
- [ ] Layer 2: Python 脚本 6 个场景 100% 通过
- [ ] Layer 3: E2E 测试套件 >80% 覆盖率，无失败用例
- [ ] 双主题支持：light/dark 主题下 MCP 返回结果一致
- [ ] 并发测试：5 并发请求无竞态条件
- [ ] 长时间运行：1 小时连续运行无内存泄漏

---

## 七、后续扩展

- **压力测试**：Apache JMeter / Locust 模拟 100+ 并发
- **集成测试**：与真实 Claude Code MCP 配置集成
- **性能基准**：建立 Tool 响应时间基准
- **自动化 CI**：GitHub Actions / GitLab CI 每次 PR 自动运行
