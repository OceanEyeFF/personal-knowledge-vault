"""
MCP Resources 单元测试

使用 mock 隔离外部依赖（SQLiteStore, MarkdownStore），
专注测试 Resource handler 的逻辑正确性。
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

import pytest

# 确保项目根目录在 Python path 中
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# Mock 数据

MOCK_ENTRY_DB = {
    "knowledge_id": 1,
    "title": "测试微信文章",
    "summary_one_sentence": "这是一句话摘要",
    "tags": "AI,NLP",
    "keywords": "人工智能,自然语言",
    "source_type": "wechat",
    "source_url": "https://mp.weixin.qq.com/s/test",
    "archived_at": "2026-02-18",
    "word_count": 500,
    "file_path": "/vault/wechat/2026/02/20260218-test.md",
}


@dataclass
class MockEntry:
    """Mock Entry 对象。"""
    content: str = "# 测试文章\n\n这是全文内容"
    title: str = "测试微信文章"


class TestGetEntryContent:
    """pkv://entries/{knowledge_id} Resource 测试。"""

    @pytest.mark.asyncio
    async def test_found_entry(self, tmp_path: Path):
        """正常条目应返回 Markdown 全文。"""
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        entry_path = vault_dir / "entry.md"
        entry_path.write_text("# 测试文章\n", encoding="utf-8")
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = {
            **MOCK_ENTRY_DB,
            "file_path": str(entry_path),
        }
        mock_md_store = MagicMock()
        mock_md_store.vault_dir = vault_dir
        mock_md_store.load.return_value = MockEntry()

        with patch("src.mcp.resources.get_sqlite_store", return_value=mock_store), \
             patch("src.mcp.resources.get_markdown_store", return_value=mock_md_store):
            from src.mcp.resources import get_entry_content
            result = await get_entry_content(knowledge_id="1")

        assert "全文内容" in result

    @pytest.mark.asyncio
    async def test_not_found(self):
        """未找到条目应返回友好提示。"""
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = None

        with patch("src.mcp.resources.get_sqlite_store", return_value=mock_store):
            from src.mcp.resources import get_entry_content
            with pytest.raises(ValueError, match="未找到条目"):
                await get_entry_content(knowledge_id="999")

    @pytest.mark.asyncio
    async def test_invalid_id(self):
        """非数字 ID 应返回错误信息。"""
        from src.mcp.resources import get_entry_content
        with pytest.raises(ValueError, match="无效"):
            await get_entry_content(knowledge_id="abc")

    @pytest.mark.asyncio
    async def test_file_not_found(self, tmp_path: Path):
        """Markdown 文件不存在应作为 Resource 错误拒绝。"""
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = {
            **MOCK_ENTRY_DB,
            "file_path": str(tmp_path / "vault" / "missing.md"),
        }
        mock_md_store = MagicMock()
        mock_md_store.vault_dir = tmp_path / "vault"
        mock_md_store.vault_dir.mkdir()
        mock_md_store.load.side_effect = FileNotFoundError()

        with patch("src.mcp.resources.get_sqlite_store", return_value=mock_store), \
             patch("src.mcp.resources.get_markdown_store", return_value=mock_md_store):
            from src.mcp.resources import get_entry_content
            with pytest.raises(ValueError, match="条目内容不可用"):
                await get_entry_content(knowledge_id="1")

    @pytest.mark.asyncio
    async def test_no_file_path(self):
        """file_path 为空应返回路径缺失提示。"""
        entry_no_path = {**MOCK_ENTRY_DB, "file_path": ""}
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = entry_no_path

        with patch("src.mcp.resources.get_sqlite_store", return_value=mock_store):
            from src.mcp.resources import get_entry_content
            with pytest.raises(ValueError, match="条目内容不可用"):
                await get_entry_content(knowledge_id="1")


class TestGetEntryMetadata:
    """pkv://entries/{knowledge_id}/metadata Resource 测试。"""

    @pytest.mark.asyncio
    async def test_found_entry(self, tmp_path: Path):
        """正常条目应返回 JSON 格式的元数据。"""
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        entry_path = vault_dir / "entry.md"
        entry_path.write_text("# 测试文章\n", encoding="utf-8")
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = {
            **MOCK_ENTRY_DB,
            "file_path": str(entry_path),
        }
        mock_md_store = MagicMock()
        mock_md_store.vault_dir = vault_dir

        with patch("src.mcp.resources.get_sqlite_store", return_value=mock_store), \
             patch("src.mcp.resources.get_markdown_store", return_value=mock_md_store):
            from src.mcp.resources import get_entry_metadata
            result = await get_entry_metadata(knowledge_id="1")

        data = json.loads(result)
        assert data["knowledge_id"] == 1
        assert data["title"] == "测试微信文章"
        assert "file_path" not in data
        # tags 应被转换为列表
        assert data["tags"] == ["AI", "NLP"]
        assert data["keywords"] == ["人工智能", "自然语言"]

    @pytest.mark.asyncio
    async def test_not_found(self):
        """未找到条目应返回 JSON 错误。"""
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = None

        with patch("src.mcp.resources.get_sqlite_store", return_value=mock_store):
            from src.mcp.resources import get_entry_metadata
            with pytest.raises(ValueError, match="未找到条目"):
                await get_entry_metadata(knowledge_id="999")

    @pytest.mark.asyncio
    async def test_rejects_entry_outside_vault(self, tmp_path: Path):
        """元数据 Resource 不应为 vault 外条目提供可引用内容。"""
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        outside_path = tmp_path / "outside.md"
        outside_path.write_text("PRIVATE_METADATA", encoding="utf-8")
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = {
            **MOCK_ENTRY_DB,
            "file_path": str(outside_path),
        }
        mock_md_store = MagicMock()
        mock_md_store.vault_dir = vault_dir

        with patch("src.mcp.resources.get_sqlite_store", return_value=mock_store), \
             patch("src.mcp.resources.get_markdown_store", return_value=mock_md_store):
            from src.mcp.resources import get_entry_metadata
            with pytest.raises(ValueError, match="条目内容不可用"):
                await get_entry_metadata(knowledge_id="1")


class TestCitationResources:
    """精确 citation Resource handler 测试。"""

    @pytest.mark.asyncio
    async def test_chunk_resource_returns_canonical_locator(self, tmp_path: Path):
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        entry_path = vault_dir / "entry.md"
        entry_path.write_text("# Entry", encoding="utf-8")
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = {
            **MOCK_ENTRY_DB,
            "file_path": str(entry_path),
        }
        mock_store.get_chunk_by_id.return_value = {
            "chunk_id": 101,
            "knowledge_id": 1,
            "chunk_index": 0,
            "chunk_text": "精确片段",
        }
        mock_md_store = MagicMock()
        mock_md_store.vault_dir = vault_dir

        with patch(
            "src.mcp.resources.get_sqlite_store",
            return_value=mock_store,
        ), patch(
            "src.mcp.resources.get_markdown_store",
            return_value=mock_md_store,
        ):
            from src.mcp.resources import get_entry_chunk
            result = await get_entry_chunk(knowledge_id="1", chunk_id="101")

        data = json.loads(result)
        assert data["chunk_text"] == "精确片段"
        assert data["citation_locator"] == "pkv://entries/1/chunks/101"

    @pytest.mark.asyncio
    async def test_chunk_resources_reject_parent_outside_vault(self, tmp_path: Path):
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        outside_path = tmp_path / "outside.md"
        outside_path.write_text("CHUNK_SECRET", encoding="utf-8")
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = {
            **MOCK_ENTRY_DB,
            "file_path": str(outside_path),
        }
        mock_store.get_chunk_by_id.return_value = {
            "chunk_id": 101,
            "knowledge_id": 1,
            "chunk_index": 0,
            "chunk_text": "CHUNK_SECRET",
        }
        mock_store.get_chunk_by_index.return_value = mock_store.get_chunk_by_id.return_value
        mock_md_store = MagicMock()
        mock_md_store.vault_dir = vault_dir

        with patch(
            "src.mcp.resources.get_sqlite_store",
            return_value=mock_store,
        ), patch(
            "src.mcp.resources.get_markdown_store",
            return_value=mock_md_store,
        ):
            from src.mcp.resources import (
                get_entry_chunk,
                get_entry_chunk_by_index,
            )

            with pytest.raises(ValueError, match="条目内容不可用"):
                await get_entry_chunk(knowledge_id="1", chunk_id="101")
            with pytest.raises(ValueError, match="条目内容不可用"):
                await get_entry_chunk_by_index(
                    knowledge_id="1",
                    chunk_index="0",
                )

    @pytest.mark.asyncio
    async def test_relation_resources_reject_endpoint_outside_vault(
        self,
        tmp_path: Path,
    ):
        """关系 Resource 不能绕过任一端点的 vault 边界。"""
        from src.relations.models import (
            RelationRecord,
            RelationSourceType,
            RelationType,
        )

        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        inside_path = vault_dir / "inside.md"
        inside_path.write_text("# Inside", encoding="utf-8")
        outside_path = tmp_path / "outside.md"
        outside_path.write_text("RELATION_SECRET", encoding="utf-8")
        record = RelationRecord(
            relation_id=7,
            source_knowledge_id=1,
            target_knowledge_id=2,
            relation_type=RelationType.REFERENCES,
            relation_source_type=RelationSourceType.MANUAL,
            evidence_payload={"source_file_path": str(outside_path)},
        )
        mock_store = MagicMock()
        mock_store.query_by_id.side_effect = lambda knowledge_id: {
            **MOCK_ENTRY_DB,
            "knowledge_id": knowledge_id,
            "file_path": str(inside_path if knowledge_id == 1 else outside_path),
        }
        relation_store = MagicMock()
        relation_store.get_relation.return_value = record
        relation_store.list_relations_between.return_value = [record]
        relation_service = MagicMock(relation_store=relation_store)
        mock_md_store = MagicMock()
        mock_md_store.vault_dir = vault_dir

        with patch(
            "src.mcp.resources.get_sqlite_store",
            return_value=mock_store,
        ), patch(
            "src.mcp.resources.get_markdown_store",
            return_value=mock_md_store,
        ), patch(
            "src.mcp.resources.get_relation_query_service",
            return_value=relation_service,
        ):
            from src.mcp.resources import (
                get_relation_by_edge_resource,
                get_relation_resource,
            )

            with pytest.raises(ValueError, match="条目内容不可用") as error:
                await get_relation_resource(relation_id="7")
            assert "outside.md" not in str(error.value)
            with pytest.raises(ValueError, match="条目内容不可用"):
                await get_relation_by_edge_resource(
                    source_knowledge_id="1",
                    target_knowledge_id="2",
                    relation_type="references",
                    relation_source_type="manual",
                )

    @pytest.mark.asyncio
    async def test_metadata_field_resource_rejects_non_timeline_field(self):
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = MOCK_ENTRY_DB

        with patch("src.mcp.resources.get_sqlite_store", return_value=mock_store):
            from src.mcp.resources import get_entry_metadata_field
            with pytest.raises(ValueError, match="不支持"):
                await get_entry_metadata_field(
                    knowledge_id="1",
                    field_name="title",
                )

    @pytest.mark.asyncio
    async def test_metadata_field_requires_persisted_legacy_value(
        self,
        tmp_path: Path,
    ):
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = {
            **MOCK_ENTRY_DB,
            "file_path": "missing-legacy.md",
        }

        mock_md_store = MagicMock()
        mock_md_store.vault_dir = tmp_path / "vault"
        mock_md_store.vault_dir.mkdir()
        with patch(
            "src.mcp.resources.get_sqlite_store",
            return_value=mock_store,
        ), patch(
            "src.mcp.resources.get_markdown_store",
            return_value=mock_md_store,
        ):
            from src.mcp.resources import get_entry_metadata_field
            with pytest.raises(ValueError, match="不存在元数据字段"):
                await get_entry_metadata_field(
                    knowledge_id="1",
                    field_name="published_time",
                )

    @pytest.mark.asyncio
    async def test_invalid_id(self):
        """非数字 ID 应返回 JSON 错误。"""
        from src.mcp.resources import get_entry_metadata
        with pytest.raises(ValueError, match="无效"):
            await get_entry_metadata(knowledge_id="abc")


class TestGetTagsResource:
    """pkv://tags Resource 测试。"""

    @pytest.mark.asyncio
    async def test_tags_resource(self):
        """应返回 JSON 格式的标签列表。"""
        mock_store = MagicMock()
        mock_store.get_all_tags_with_count.return_value = [
            {"name": "AI", "count": 10},
            {"name": "NLP", "count": 5},
        ]

        with patch("src.mcp.resources.get_sqlite_store", return_value=mock_store):
            from src.mcp.resources import get_tags_resource
            result = await get_tags_resource()

        data = json.loads(result)
        assert data["total_tags"] == 2
        assert data["tags"][0]["name"] == "AI"

    @pytest.mark.asyncio
    async def test_empty_tags(self):
        """空标签应返回空列表。"""
        mock_store = MagicMock()
        mock_store.get_all_tags_with_count.return_value = []

        with patch("src.mcp.resources.get_sqlite_store", return_value=mock_store):
            from src.mcp.resources import get_tags_resource
            result = await get_tags_resource()

        data = json.loads(result)
        assert data["total_tags"] == 0

    @pytest.mark.asyncio
    async def test_tags_resource_redacts_local_values(self):
        mock_store = MagicMock()
        mock_store.get_all_tags_with_count.return_value = [
            {"name": r"\Windows\System32\private", "count": 1},
        ]

        with patch("src.mcp.resources.get_sqlite_store", return_value=mock_store):
            from src.mcp.resources import get_tags_resource
            result = await get_tags_resource()

        assert json.loads(result)["tags"][0]["name"] == "[redacted-local-reference]"


class TestGetStatsResource:
    """pkv://stats Resource 测试。"""

    @pytest.mark.asyncio
    async def test_stats_resource(self):
        """应返回 JSON 格式的统计数据。"""
        mock_stats = {
            "total_entries": 100,
            "total_tags": 20,
            "source_types": {"wechat": 50, "zhihu": 30},
        }
        mock_store = MagicMock()
        mock_store.get_statistics.return_value = mock_stats

        with patch("src.mcp.resources.get_sqlite_store", return_value=mock_store):
            from src.mcp.resources import get_stats_resource
            result = await get_stats_resource()

        data = json.loads(result)
        assert data["total_entries"] == 100
        assert data["total_tags"] == 20

    @pytest.mark.asyncio
    async def test_stats_resource_redacts_local_values(self):
        mock_store = MagicMock()
        mock_store.get_statistics.return_value = {
            "total_entries": 1,
            "by_source_type": [(r"\??\C:\private", 1)],
        }

        with patch("src.mcp.resources.get_sqlite_store", return_value=mock_store):
            from src.mcp.resources import get_stats_resource
            result = await get_stats_resource()

        assert json.loads(result)["by_source_type"][0][0] == (
            "[redacted-local-reference]"
        )
