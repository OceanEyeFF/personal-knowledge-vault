# Personal Knowledge Vault - M14 测试执行 Prompt

> M14 用户审核系统 —— 测试工程师执行指令
>
> **版本**: 1.0
> **创建日期**: 2026-02-24
> **适用对象**: Claude Code、CodeX 等 AI 开发工具（新独立会话使用）
> **前置条件**: M14 Phase 1 实现已完成（worktree `do-0224-aukm`）
> **目标**: 将 M14 测试覆盖率从当前水平提升到 ≥ 90%，并新增集成/E2E 测试

---

## 📍 当前状态（必读）

### Worktree 位置

```
主仓库:  /e/gitee/personal-knowledge-vault/             (分支: claude-main)
M14 开发: /e/gitee/personal-knowledge-vault/.worktrees/do-0224-aukm/  (分支: do/0224-aukm)
```

**所有测试工作在 worktree 目录下进行**：

```bash
cd /e/gitee/personal-knowledge-vault/.worktrees/do-0224-aukm
```

### M14 已实现的内容

| 文件 | 内容 | 状态 |
|------|------|------|
| `src/storage/review_manager.py` | ReviewItem dataclass + ReviewManager（CRUD + 历史 + 草稿） | ✅ 已实现 |
| `src/workflow/steps.py` | ReviewStep（交互式菜单 + AI 重新生成 + 编辑器集成）| ✅ 已实现 |
| `src/workflow/engine.py` | 注册 `"review_entry": ReviewStep` | ✅ 已实现 |
| `config/workflows/archive-url.yaml` | 插入 `review_entry` 步骤（required: true） | ✅ 已实现 |
| `config/workflows/archive-text.yaml` | 插入 `review_entry` 步骤（required: false） | ✅ 已实现 |
| `tests/unit/test_review_manager.py` | 54 个单元测试 | ✅ 全通过 |
| `tests/unit/test_review_step.py` | 17 个单元测试 | ✅ 全通过 |

### 当前测试覆盖率

```
src/storage/review_manager.py   91%   (未覆盖: 93-94, 114-117, 137-140, 144-182, 396-397, 515-516)
src/workflow/steps.py           25%   (ReviewStep 交互式菜单基本未覆盖: 553-738, 759-796, 809-832)
```

**目标**: `review_manager.py` ≥ 95%，`steps.py`（ReviewStep 部分）≥ 80%

---

## 📚 阅读顺序

在开始测试工作前，请按顺序阅读：

1. **本文档**（当前）— 执行指令
2. `src/storage/review_manager.py` — 理解 ReviewItem / ReviewManager 完整接口
3. `src/workflow/steps.py`（从第 450 行开始）— 理解 ReviewStep 的交互逻辑
4. `tests/unit/test_review_manager.py` — 了解已有测试模式（fixture 复用）
5. `tests/unit/test_review_step.py` — 了解已有 mock 模式
6. `scripts/migrations/005_add_review_system.sql` — 了解数据库 schema

---

## 🎯 测试任务清单（按优先级排列）

### Task 1：补充 ReviewManager 单元测试（优先级 🔴）

**目标文件**: `tests/unit/test_review_manager.py`（在已有 54 个测试后追加）

**缺失场景**：

```python
# 1. _update_field 的边界行为（已有间接覆盖，但需要直接验证）
class TestUpdateField:
    def test_update_field_returns_false_on_missing_record(self, tmp_path):
        """对不存在的 review_id 执行 _update_field 返回 False"""

    def test_update_field_records_history(self, tmp_path):
        """_update_field 成功后 history 表有记录"""

# 2. review_manager 的 _ensure_tables / _create_tables_inline fallback 路径
class TestEnsureTables:
    def test_ensure_tables_creates_required_tables(self, tmp_path):
        """_ensure_tables 在全新 DB 上正确建表"""

    def test_ensure_tables_idempotent(self, tmp_path):
        """重复调用 _ensure_tables 不报错（IF NOT EXISTS）"""

# 3. approve_review 后 list_drafts 不包含该条目
class TestApproveRemovesFromDrafts:
    def test_approved_not_in_drafts(self, tmp_path):
        """审核通过的条目不出现在 list_drafts 中"""

# 4. record_regeneration 多次叠加
class TestMultipleRegeneration:
    def test_multiple_regen_prompts_accumulate(self, tmp_path):
        """多次 record_regeneration 后 regeneration_prompts 是完整列表"""

    def test_regen_count_increments_correctly(self, tmp_path):
        """连续 3 次 record_regeneration 后 regeneration_count == 3"""

# 5. add_user_comment 多次追加
class TestCommentAppend:
    def test_comment_appends_newline(self, tmp_path):
        """第二次 add_user_comment 内容追加（以换行符分隔）"""
```

**运行验证**：

```bash
python -m pytest tests/unit/test_review_manager.py -v --tb=short
# 期望：60+ 个测试，全部 PASSED
```

---

### Task 2：补充 ReviewStep 单元测试（优先级 🔴）

**目标文件**: `tests/unit/test_review_step.py`（在已有 17 个测试后追加）

**缺失场景**（ReviewStep 交互式菜单 —— 行 553-738）：

```python
# 关键 mock 模式（参考已有 _run_with_approve / _run_with_reject）:
# 用 asyncio.wait_for patch + 模拟 Prompt.ask 的返回值

# 1. 修改摘要分支（菜单选项 "1"）
class TestReviewStepEditSummary:
    def test_edit_summary_via_console_input(self):
        """菜单选 1 → 控制台输入新摘要 → update_user_summary 被调用"""

    def test_edit_summary_empty_input_keeps_original(self):
        """输入空字符串时不调用 update_user_summary"""

# 2. 修改标签分支（菜单选项 "2"）
class TestReviewStepEditTags:
    def test_edit_tags_comma_separated(self):
        """输入 'AI, Python, 工程' → update_user_tags(['AI', 'Python', '工程'])"""

    def test_edit_tags_empty_input_keeps_original(self):
        """输入空字符串时不调用 update_user_tags"""

# 3. 添加评论分支（菜单选项 "3"）
class TestReviewStepAddComment:
    def test_add_comment_calls_manager(self):
        """菜单选 3 → 输入评论 → add_user_comment 被调用"""

# 4. AI 重新生成分支（菜单选项 "4"）
class TestReviewStepAiRegen:
    def test_ai_regen_calls_record_regeneration(self):
        """菜单选 4 → AI 成功 → record_regeneration 被调用"""

    def test_ai_regen_failure_falls_back_gracefully(self):
        """AI 调用失败时不崩溃，显示错误提示"""

    def test_ai_regen_exceeds_max_regenerations(self):
        """超过 max_regenerations 次后菜单不再显示选项 4"""

# 5. 查看历史分支（菜单选项 "5"）
class TestReviewStepViewHistory:
    def test_view_history_calls_get_history(self):
        """菜单选 5 → get_history 被调用"""

# 6. _open_editor 分支
class TestOpenEditor:
    def test_open_editor_success_returns_content(self, tmp_path):
        """编辑器成功返回时，返回文件内容"""

    def test_open_editor_failure_returns_none(self):
        """编辑器启动失败时返回 None"""

    def test_open_editor_empty_file_returns_none(self, tmp_path):
        """用户未修改文件（内容与初始相同）时返回 None"""
```

**运行验证**：

```bash
python -m pytest tests/unit/test_review_step.py -v --tb=short
# 期望：30+ 个测试，全部 PASSED
```

---

### Task 3：新增集成测试 —— 数据库迁移（优先级 🔴）

**新建文件**: `tests/integration/test_review_migration.py`

```python
"""
迁移 005 的实际执行验证。

注意：不依赖 MigrationManager，直接读取 SQL 文件执行，
确保 SQL 语句本身的正确性。
"""
import sqlite3
from pathlib import Path
import pytest

MIGRATION_SQL = Path("scripts/migrations/005_add_review_system.sql")


def _apply_migration(db_path: Path) -> None:
    """执行迁移 SQL（仅向上迁移部分）。"""
    sql = MIGRATION_SQL.read_text(encoding="utf-8")
    # 只取注释行「向上迁移」到「向下迁移」之间的部分
    upgrade_sql = sql.split("-- 向下迁移")[0]
    conn = sqlite3.connect(str(db_path))
    conn.executescript(upgrade_sql)
    conn.close()


class TestMigration005Schema:
    def test_review_queue_table_exists(self, tmp_path):
        db = tmp_path / "test.db"
        _apply_migration(db)
        conn = sqlite3.connect(str(db))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "review_queue" in tables

    def test_review_history_table_exists(self, tmp_path):
        db = tmp_path / "test.db"
        _apply_migration(db)
        conn = sqlite3.connect(str(db))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "review_history" in tables

    def test_review_queue_required_columns(self, tmp_path):
        db = tmp_path / "test.db"
        _apply_migration(db)
        conn = sqlite3.connect(str(db))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(review_queue)")}
        required = {
            "review_id", "review_status", "ai_generated_summary",
            "ai_generated_tags", "user_summary", "user_tags",
            "user_comments", "regeneration_count", "regeneration_prompts",
            "created_at", "approved_at", "review_version",
        }
        assert required.issubset(cols)

    def test_review_history_required_columns(self, tmp_path):
        db = tmp_path / "test.db"
        _apply_migration(db)
        conn = sqlite3.connect(str(db))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(review_history)")}
        assert {"history_id", "review_id", "action", "details", "operator", "created_at"}.issubset(cols)

    def test_indexes_created(self, tmp_path):
        db = tmp_path / "test.db"
        _apply_migration(db)
        conn = sqlite3.connect(str(db))
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        expected_indexes = {
            "idx_review_status", "idx_review_created_at",
            "idx_review_knowledge_id", "idx_review_history_review_id",
        }
        assert expected_indexes.issubset(indexes)

    def test_migration_idempotent(self, tmp_path):
        """重复执行迁移不报错（IF NOT EXISTS）。"""
        db = tmp_path / "test.db"
        _apply_migration(db)
        _apply_migration(db)  # 第二次执行不应抛出异常


class TestMigration005Constraints:
    def test_review_status_default_pending(self, tmp_path):
        db = tmp_path / "test.db"
        _apply_migration(db)
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO review_queue (ai_generated_summary, ai_generated_tags, source_type) "
            "VALUES ('s', 't', 'webpage')"
        )
        conn.commit()
        row = conn.execute("SELECT review_status FROM review_queue WHERE review_id = 1").fetchone()
        assert row[0] == "pending"

    def test_regeneration_count_default_zero(self, tmp_path):
        db = tmp_path / "test.db"
        _apply_migration(db)
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO review_queue (ai_generated_summary, ai_generated_tags, source_type) "
            "VALUES ('s', 't', 'webpage')"
        )
        conn.commit()
        row = conn.execute("SELECT regeneration_count FROM review_queue WHERE review_id = 1").fetchone()
        assert row[0] == 0

    def test_review_history_fk_cascade(self, tmp_path):
        """删除 review_queue 记录时级联删除 review_history。"""
        db = tmp_path / "test.db"
        _apply_migration(db)
        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO review_queue (ai_generated_summary, ai_generated_tags, source_type) "
            "VALUES ('s', 't', 'webpage')"
        )
        conn.execute(
            "INSERT INTO review_history (review_id, action) VALUES (1, 'init')"
        )
        conn.commit()
        conn.execute("DELETE FROM review_queue WHERE review_id = 1")
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM review_history WHERE review_id = 1").fetchone()[0]
        assert count == 0
```

**运行验证**：

```bash
python -m pytest tests/integration/test_review_migration.py -v
# 期望：12+ 个测试，全部 PASSED
```

---

### Task 4：新增集成测试 —— 工作流配置（优先级 🟡）

**新建文件**: `tests/integration/test_review_workflow.py`

```python
"""
验证 review_entry 步骤正确集成到工作流 YAML 配置中。
不运行真实工作流，只验证配置和注册表。
"""
import yaml
from pathlib import Path
import pytest


class TestWorkflowYamlConfig:
    def test_archive_url_contains_review_entry(self):
        cfg = yaml.safe_load(Path("config/workflows/archive-url.yaml").read_text())
        step_ids = [s["id"] for s in cfg["steps"]]
        assert "review_entry" in step_ids

    def test_archive_url_review_entry_before_store(self):
        cfg = yaml.safe_load(Path("config/workflows/archive-url.yaml").read_text())
        step_ids = [s["id"] for s in cfg["steps"]]
        ri = step_ids.index("review_entry")
        si = step_ids.index("store_entry")
        assert ri < si, "review_entry 必须在 store_entry 之前"

    def test_archive_url_review_required_true(self):
        cfg = yaml.safe_load(Path("config/workflows/archive-url.yaml").read_text())
        review_step = next(s for s in cfg["steps"] if s["id"] == "review_entry")
        assert review_step["config"]["required"] is True

    def test_archive_text_contains_review_entry(self):
        cfg = yaml.safe_load(Path("config/workflows/archive-text.yaml").read_text())
        step_ids = [s["id"] for s in cfg["steps"]]
        assert "review_entry" in step_ids

    def test_archive_text_review_required_false(self):
        cfg = yaml.safe_load(Path("config/workflows/archive-text.yaml").read_text())
        review_step = next(s for s in cfg["steps"] if s["id"] == "review_entry")
        assert review_step["config"]["required"] is False


class TestWorkflowEngineRegistry:
    def test_review_entry_registered_in_engine(self):
        from src.workflow.engine import WorkflowEngine
        from src.workflow.steps import ReviewStep
        engine = WorkflowEngine.__new__(WorkflowEngine)
        # 访问 _STEP_REGISTRY 类属性（或通过实例）
        registry = getattr(engine, "_STEP_REGISTRY", None) or \
                   getattr(WorkflowEngine, "_STEP_REGISTRY", None)
        assert registry is not None, "_STEP_REGISTRY 不存在"
        assert "review_entry" in registry
        assert registry["review_entry"] is ReviewStep


class TestStoreStepReviewRejectedGuard:
    """验证 StoreStep 在 review_rejected=True 时跳过。"""

    def test_store_step_skips_when_rejected(self):
        import asyncio
        from unittest.mock import MagicMock, patch
        from src.workflow.steps import StoreStep

        step = StoreStep({"targets": ["sqlite"]})

        ctx = MagicMock()
        ctx.state = {"review_rejected": True}
        ctx.entry = None

        result = asyncio.get_event_loop().run_until_complete(step.execute(ctx))
        assert result.get("skipped") is True or result.get("reason") == "review_rejected"
```

**运行验证**：

```bash
python -m pytest tests/integration/test_review_workflow.py -v
# 期望：9+ 个测试，全部 PASSED
```

---

### Task 5：新增 E2E 场景测试（优先级 🟡）

**新建文件**: `tests/integration/test_review_e2e.py`

```python
"""
ReviewManager 完整生命周期的端到端测试。
使用真实 SQLite（tmp_path），不 mock 任何方法。
"""
import pytest
from pathlib import Path
from src.storage.review_manager import ReviewManager, ReviewItem


MIGRATION_SQL = Path("scripts/migrations/005_add_review_system.sql")


def _make_manager(tmp_path: Path) -> ReviewManager:
    db = tmp_path / "e2e.db"
    sql = MIGRATION_SQL.read_text(encoding="utf-8").split("-- 向下迁移")[0]
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.executescript(sql)
    conn.close()
    return ReviewManager(db_path=db)


def _make_item(**kwargs) -> ReviewItem:
    defaults = dict(
        ai_generated_summary="AI 摘要",
        ai_generated_tags="AI,摘要,测试",
        source_type="webpage",
        source_url="https://example.com",
        original_content_preview="这是原始内容的前 500 字...",
    )
    defaults.update(kwargs)
    return ReviewItem(**defaults)


class TestFullApproveWorkflow:
    """场景：创建 → 修改摘要 → 修改标签 → 添加评论 → 审核通过"""

    def test_approved_item_has_correct_effective_values(self, tmp_path):
        mgr = _make_manager(tmp_path)
        rid = mgr.create_review(_make_item())

        mgr.update_user_summary(rid, "用户修改后的摘要")
        mgr.update_user_tags(rid, ["Python", "工程", "测试"])
        mgr.add_user_comment(rid, "这是一篇不错的文章")
        mgr.approve_review(rid)

        item = mgr.get_review(rid)
        assert item.review_status == "approved"
        assert item.get_effective_summary() == "用户修改后的摘要"
        assert "Python" in item.get_effective_tags()
        assert item.user_comments == "这是一篇不错的文章"

    def test_approved_history_has_all_actions(self, tmp_path):
        mgr = _make_manager(tmp_path)
        rid = mgr.create_review(_make_item())
        mgr.update_user_summary(rid, "新摘要")
        mgr.update_user_tags(rid, ["tag1"])
        mgr.add_user_comment(rid, "评论")
        mgr.approve_review(rid)

        history = mgr.get_history(rid)
        actions = [h["action"] for h in history]
        assert "init" in actions
        assert "modify_summary" in actions
        assert "modify_tags" in actions
        assert "add_comment" in actions
        assert "approve" in actions

    def test_version_increments_on_each_edit(self, tmp_path):
        mgr = _make_manager(tmp_path)
        rid = mgr.create_review(_make_item())

        v1 = mgr.get_review(rid).review_version
        mgr.update_user_summary(rid, "第一次修改")
        v2 = mgr.get_review(rid).review_version
        mgr.update_user_summary(rid, "第二次修改")
        v3 = mgr.get_review(rid).review_version

        assert v1 == 1
        assert v2 == 2
        assert v3 == 3


class TestFullRejectAndRestoreWorkflow:
    """场景：创建 → 拒绝 → 出现在草稿区 → 恢复 → 再次通过"""

    def test_reject_appears_in_drafts(self, tmp_path):
        mgr = _make_manager(tmp_path)
        rid = mgr.create_review(_make_item())
        mgr.reject_review(rid)

        drafts = mgr.list_drafts()
        assert any(d.review_id == rid for d in drafts)

    def test_restore_removes_from_drafts(self, tmp_path):
        mgr = _make_manager(tmp_path)
        rid = mgr.create_review(_make_item())
        mgr.reject_review(rid)
        mgr.restore_draft(rid)

        drafts = mgr.list_drafts()
        assert not any(d.review_id == rid for d in drafts)

    def test_restored_item_can_be_approved(self, tmp_path):
        mgr = _make_manager(tmp_path)
        rid = mgr.create_review(_make_item())
        mgr.reject_review(rid)
        mgr.restore_draft(rid)
        mgr.approve_review(rid)

        item = mgr.get_review(rid)
        assert item.review_status == "approved"


class TestRegenerationWorkflow:
    """场景：AI 重新生成多次，prompts 正确累积"""

    def test_three_regenerations_stored(self, tmp_path):
        mgr = _make_manager(tmp_path)
        rid = mgr.create_review(_make_item())

        mgr.record_regeneration(rid, "标签太多，请精简", "精简后的摘要", "标签1,标签2")
        mgr.record_regeneration(rid, "摘要太长", "更短的摘要", "标签1")
        mgr.record_regeneration(rid, "再精简一次", "最终摘要", "标签")

        item = mgr.get_review(rid)
        assert item.regeneration_count == 3

        import json
        prompts = json.loads(item.regeneration_prompts)
        assert len(prompts) == 3
        assert prompts[0] == "标签太多，请精简"
        assert prompts[2] == "再精简一次"

    def test_latest_regen_updates_ai_fields(self, tmp_path):
        mgr = _make_manager(tmp_path)
        rid = mgr.create_review(_make_item())
        mgr.record_regeneration(rid, "优化", "最新摘要", "新标签")

        item = mgr.get_review(rid)
        # AI 字段应该更新为最新生成的结果
        assert item.ai_generated_summary == "最新摘要"
```

**运行验证**：

```bash
python -m pytest tests/integration/test_review_e2e.py -v
# 期望：12+ 个测试，全部 PASSED
```

---

## 🚀 执行顺序

```bash
# 进入 worktree
cd /e/gitee/personal-knowledge-vault/.worktrees/do-0224-aukm

# Step 1：运行现有测试确认基线（应全部通过）
python -m pytest tests/unit/test_review_manager.py tests/unit/test_review_step.py -v
# 预期：71 passed

# Step 2：实现并运行 Task 1（ReviewManager 补充测试）
python -m pytest tests/unit/test_review_manager.py -v
# 预期：60+ passed

# Step 3：实现并运行 Task 2（ReviewStep 交互菜单测试）
python -m pytest tests/unit/test_review_step.py -v
# 预期：30+ passed

# Step 4：创建并运行集成测试（Task 3 + 4 + 5）
python -m pytest tests/integration/test_review_migration.py -v
python -m pytest tests/integration/test_review_workflow.py -v
python -m pytest tests/integration/test_review_e2e.py -v

# Step 5：全量回归（确保未破坏已有功能）
python -m pytest tests/unit/ -v --ignore=tests/unit/test_gui_*.py
python -m pytest tests/integration/ -v

# Step 6：覆盖率报告
python -m pytest tests/unit/test_review_manager.py tests/unit/test_review_step.py \
    tests/integration/test_review_migration.py \
    tests/integration/test_review_e2e.py \
    --cov=src/storage/review_manager \
    --cov=src/workflow/steps \
    --cov-report=term-missing
# 目标：review_manager.py ≥ 95%，steps.py ReviewStep 部分 ≥ 80%
```

---

## ⚠️ 关键注意事项

### 测试隔离
- **所有数据库测试必须使用 `tmp_path` fixture**，不得操作 `.data/` 或 `.data-test/`
- ReviewManager 测试已有 `_apply_migration` helper，新测试复用即可

### Mock 策略（ReviewStep 菜单测试）
- 交互式 `Prompt.ask()` 必须 mock（`patch("rich.prompt.Prompt.ask", return_value="...")`）
- `asyncio.wait_for` 必须用**模块级路径** mock：`patch("src.workflow.steps.asyncio.wait_for")`
- 编辑器测试用 `tmp_path` 创建真实临时文件

### 不需要实现的内容
- CLI `review-drafts` 命令 —— M14 Phase 3-9 尚未实现，**测试文档中的 CLI 命令测试框架预留即可**，不强制要求运行通过
- AI 重新生成 —— `_call_ai_regenerate` 全程 mock，不调用真实 DeepSeek API

### 已知边界行为
- `record_regeneration` 同时更新 `ai_generated_summary` / `ai_generated_tags` 字段（确认代码实现）
- `restore_draft` 只接受 `status='rejected'` 的记录，其他状态返回 False
- `StoreStep` 在 `context.state["review_rejected"] == True` 时早退，返回带 `skipped=True` 或 `reason` 字段的 dict

---

## 📊 验收标准

| 指标 | 目标 | 当前 |
|------|------|------|
| `review_manager.py` 覆盖率 | ≥ 95% | 91% |
| `steps.py` ReviewStep 部分覆盖率 | ≥ 80% | ~30% |
| ReviewManager 单元测试数 | ≥ 60 | 54 |
| ReviewStep 单元测试数 | ≥ 30 | 17 |
| 集成测试（迁移验证） | ≥ 12 | 0 |
| 集成测试（工作流配置） | ≥ 9 | 0 |
| E2E 场景测试 | ≥ 12 | 0 |
| **全量测试（M14 相关）** | **≥ 123** | **71** |
| 全量回归（已有测试） | 全部通过 | 218 passed |

---

## 📦 完成后交付

测试全部通过后，在 worktree 中提交：

```bash
git add tests/unit/test_review_manager.py \
        tests/unit/test_review_step.py \
        tests/integration/test_review_migration.py \
        tests/integration/test_review_workflow.py \
        tests/integration/test_review_e2e.py
git commit -m "test(M14): 补充审核系统测试 — 覆盖率 ≥ 95% + 集成/E2E 测试"
```

---

**文档版本**: v1.0
**创建日期**: 2026-02-24
**适用里程碑**: M14 - 用户审核系统测试阶段
**对应实现分支**: `do/0224-aukm`（worktree: `.worktrees/do-0224-aukm`）
