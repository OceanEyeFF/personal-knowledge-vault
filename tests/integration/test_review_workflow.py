"""
验证 review_entry 步骤正确集成到工作流 YAML 配置和引擎注册表中。
不运行真实工作流，只验证配置和注册表。
"""
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml


class TestWorkflowYamlConfig:
    def test_archive_url_contains_review_entry(self):
        """archive-url.yaml 应包含 review_entry 步骤。"""
        cfg = yaml.safe_load(Path("config/workflows/archive-url.yaml").read_text(encoding="utf-8"))
        step_ids = [s["id"] for s in cfg["steps"]]
        assert "review_entry" in step_ids

    def test_archive_url_review_entry_before_store(self):
        """archive-url.yaml 中 review_entry 应在 store_entry 之前。"""
        cfg = yaml.safe_load(Path("config/workflows/archive-url.yaml").read_text(encoding="utf-8"))
        step_ids = [s["id"] for s in cfg["steps"]]
        ri = step_ids.index("review_entry")
        si = step_ids.index("store_entry")
        assert ri < si, "review_entry 必须在 store_entry 之前"

    def test_archive_url_review_required_true(self):
        """archive-url.yaml 中 review_entry 的 required 应为 True。"""
        cfg = yaml.safe_load(Path("config/workflows/archive-url.yaml").read_text(encoding="utf-8"))
        review_step = next(s for s in cfg["steps"] if s["id"] == "review_entry")
        assert review_step["config"]["required"] is True

    def test_archive_text_contains_review_entry(self):
        """archive-text.yaml 应包含 review_entry 步骤。"""
        cfg = yaml.safe_load(Path("config/workflows/archive-text.yaml").read_text(encoding="utf-8"))
        step_ids = [s["id"] for s in cfg["steps"]]
        assert "review_entry" in step_ids

    def test_archive_text_review_required_false(self):
        """archive-text.yaml 中 review_entry 的 required 应为 False。"""
        cfg = yaml.safe_load(Path("config/workflows/archive-text.yaml").read_text(encoding="utf-8"))
        review_step = next(s for s in cfg["steps"] if s["id"] == "review_entry")
        assert review_step["config"]["required"] is False

    def test_archive_url_review_entry_has_config(self):
        """archive-url.yaml 中 review_entry 步骤必须有 config 字段。"""
        cfg = yaml.safe_load(Path("config/workflows/archive-url.yaml").read_text(encoding="utf-8"))
        review_step = next(s for s in cfg["steps"] if s["id"] == "review_entry")
        assert "config" in review_step

    def test_archive_text_review_entry_has_config(self):
        """archive-text.yaml 中 review_entry 步骤必须有 config 字段。"""
        cfg = yaml.safe_load(Path("config/workflows/archive-text.yaml").read_text(encoding="utf-8"))
        review_step = next(s for s in cfg["steps"] if s["id"] == "review_entry")
        assert "config" in review_step


class TestWorkflowEngineRegistry:
    def test_review_entry_registered_in_engine(self):
        """WorkflowEngine 模块级 _STEP_REGISTRY 应包含 review_entry。"""
        import src.workflow.engine as engine_module
        from src.workflow.steps import ReviewStep
        registry = engine_module._STEP_REGISTRY
        assert registry is not None, "_STEP_REGISTRY 不存在"
        assert "review_entry" in registry
        assert registry["review_entry"] is ReviewStep

    def test_registry_contains_standard_steps(self):
        """_STEP_REGISTRY 应同时包含基础步骤类型。"""
        import src.workflow.engine as engine_module
        registry = engine_module._STEP_REGISTRY
        # 基础步骤应该存在（以实际注册名称为准）
        for step_type in ("fetch_content", "ai_analyze", "store_entry"):
            assert step_type in registry, f"缺少标准步骤: {step_type}"

    def test_register_step_adds_to_registry(self):
        """register_step 方法应能动态添加步骤到注册表。"""
        import src.workflow.engine as engine_module
        from src.workflow.steps import BaseStep

        class DummyStep(BaseStep):
            async def execute(self, context):
                return {}

        original_registry = dict(engine_module._STEP_REGISTRY)
        try:
            engine = engine_module.WorkflowEngine.__new__(engine_module.WorkflowEngine)
            engine.register_step("dummy_test_step", DummyStep)
            assert "dummy_test_step" in engine_module._STEP_REGISTRY
            assert engine_module._STEP_REGISTRY["dummy_test_step"] is DummyStep
        finally:
            # 清理：还原注册表
            engine_module._STEP_REGISTRY.clear()
            engine_module._STEP_REGISTRY.update(original_registry)


class TestStoreStepReviewRejectedGuard:
    """验证 StoreStep 在 review_rejected=True 时跳过存储。"""

    def test_store_step_skips_when_rejected(self):
        """review_rejected=True 时 StoreStep 应跳过并返回标识。"""
        from src.workflow.models import WorkflowContext
        from src.workflow.steps import StoreStep

        step = StoreStep(
            step_id="store_entry",
            config={"targets": ["sqlite"]},
        )
        ctx = WorkflowContext(initial_state={"review_rejected": True})

        result = asyncio.run(step.execute(ctx))
        # 应跳过，结果中有 review_rejected=True 或 skipped=True
        assert result.get("review_rejected") is True or result.get("skipped") is True

    def test_store_step_proceeds_when_not_rejected(self):
        """review_rejected 未设置时 StoreStep 不应因此跳过（会因 entry=None 报错）。"""
        from src.workflow.models import WorkflowContext
        from src.workflow.steps import StoreStep

        step = StoreStep(
            step_id="store_entry",
            config={"targets": ["sqlite"]},
        )
        ctx = WorkflowContext(initial_state={})  # 无 review_rejected

        result = asyncio.run(step.execute(ctx))
        # 无 entry 会走 error 路径，但不是因为 review_rejected
        assert result.get("review_rejected") is not True
