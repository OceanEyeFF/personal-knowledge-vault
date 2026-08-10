"""
MCP Resources 单元测试

使用 mock 隔离外部依赖（SQLiteStore, MarkdownStore），
专注测试 Resource handler 的逻辑正确性。
"""

import json
import sys
from pathlib import Path
from urllib.parse import quote
from unittest.mock import MagicMock, patch

import pytest

# 确保项目根目录在 Python path 中
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.relations.models import (  # noqa: E402
    RelationRecord,
    RelationSourceType,
    RelationType,
)
from src.storage.markdown_store import Entry  # noqa: E402


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


def _make_relation_record(**overrides):
    values = {
        "relation_id": 7,
        "source_knowledge_id": 1,
        "target_knowledge_id": 2,
        "relation_type": RelationType.REFERENCES,
        "relation_source_type": RelationSourceType.MANUAL,
        "evidence_payload": {"note": "safe"},
    }
    values.update(overrides)
    return RelationRecord(**values)


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
        mock_md_store.load.return_value = Entry(
            title="测试微信文章",
            source_type="wechat",
            content="# 测试文章\n\n这是全文内容",
        )

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

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "case",
        [
            "duck",
            "magic_mock",
            "entry_subclass",
            "property_probe",
            "none",
            "empty_content",
            "non_string_content",
        ],
    )
    async def test_loaded_entry_contract_is_exact_direct_and_fastmcp(
        self,
        case,
        tmp_path: Path,
        caplog,
    ):
        """Only an exact Entry with readable text may cross the Resource boundary."""
        from src.mcp import resources
        from src.mcp.server import mcp

        secret = "api_key_ENTRY_CONTENT_CONTRACT_CANARY"

        class DuckEntry:
            content = secret

        class EntrySubclass(Entry):
            pass

        class ContentProbe:
            calls = 0

            @property
            def content(self):
                type(self).calls += 1
                raise RuntimeError(secret)

        class SecretObject:
            def __str__(self):
                return secret

        malformed = {
            "duck": DuckEntry(),
            "magic_mock": MagicMock(content=secret),
            "entry_subclass": EntrySubclass(
                title="subclass",
                source_type="text",
                content=secret,
            ),
            "property_probe": ContentProbe(),
            "none": None,
            "empty_content": Entry(
                title="empty",
                source_type="text",
                content="",
            ),
            "non_string_content": Entry(
                title="bad-content",
                source_type="text",
            ),
        }[case]
        if case == "non_string_content":
            malformed.content = SecretObject()

        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        entry_path = vault_dir / "entry.md"
        entry_path.write_text("# placeholder", encoding="utf-8")
        store = MagicMock()
        store.query_by_id.return_value = {
            **MOCK_ENTRY_DB,
            "file_path": str(entry_path),
        }
        markdown_store = MagicMock()
        markdown_store.vault_dir = vault_dir
        markdown_store.load.return_value = malformed

        with patch.object(resources, "get_sqlite_store", return_value=store), patch.object(
            resources,
            "get_markdown_store",
            return_value=markdown_store,
        ):
            with pytest.raises(ValueError) as direct_error:
                await resources.get_entry_content(knowledge_id="1")
            with pytest.raises(Exception) as fastmcp_error:
                await mcp.read_resource("pkv://entries/1")

        assert str(direct_error.value) == (
            "resource_not_readable: 请求的资源暂时不可用"
        )
        assert "resource_not_readable" in str(fastmcp_error.value)
        assert secret not in str(direct_error.value)
        assert secret not in str(fastmcp_error.value)
        assert secret not in caplog.text
        assert ContentProbe.calls == 0


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
    async def test_chunk_index_resource_returns_canonical_locator(
        self,
        tmp_path: Path,
    ):
        """chunk_index resolves one exact parent-bound chunk and locator."""
        from src.mcp import resources

        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        entry_path = vault_dir / "entry.md"
        entry_path.write_text("# Entry", encoding="utf-8")
        store = MagicMock()
        store.query_by_id.return_value = {
            **MOCK_ENTRY_DB,
            "file_path": str(entry_path),
        }
        store.get_chunk_by_index.return_value = {
            "chunk_id": 101,
            "knowledge_id": 1,
            "chunk_index": 0,
            "chunk_text": "按索引定位的片段",
            "context_before": "前文",
            "context_after": "后文",
        }
        markdown_store = MagicMock(vault_dir=vault_dir)

        with patch.object(
            resources,
            "get_sqlite_store",
            return_value=store,
        ), patch.object(
            resources,
            "get_markdown_store",
            return_value=markdown_store,
        ):
            result = await resources.get_entry_chunk_by_index(
                knowledge_id="1",
                chunk_index="0",
            )

        data = json.loads(result)
        assert data == {
            "chunk_id": 101,
            "knowledge_id": 1,
            "chunk_index": 0,
            "chunk_text": "按索引定位的片段",
            "context_before": "前文",
            "context_after": "后文",
            "citation_locator": "pkv://entries/1/chunk-index/0",
        }
        store.get_chunk_by_index.assert_called_once_with(1, 0)

    @pytest.mark.asyncio
    async def test_chunk_index_rejects_sqlite_overflow_before_backend(self):
        """Canonical decimal syntax alone cannot exceed SQLite's integer range."""
        from src.mcp import resources

        with patch.object(resources, "get_sqlite_store") as store_getter:
            with pytest.raises(ValueError, match="无效的 chunk_index"):
                await resources.get_entry_chunk_by_index(
                    knowledge_id="1",
                    chunk_index="9223372036854775808",
                )

        store_getter.assert_not_called()

    def test_json_resource_rejects_cyclic_backend_tree(self):
        """Cyclic backend values fail closed instead of escaping JSON serialization."""
        from src.mcp import resources

        cyclic = []
        cyclic.append(cyclic)

        with pytest.raises(ValueError) as error:
            resources._json_resource({"payload": cyclic})

        assert str(error.value) == (
            "resource_not_readable: 请求的资源暂时不可用"
        )

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
    @pytest.mark.parametrize(
        ("field_name", "invalid_value"),
        [
            ("relation_type", RelationType.REFERENCES),
            ("relation_type", True),
            ("relation_type", ""),
            ("relation_type", " references "),
            ("relation_type", "pkv-resource-enum-canary"),
            ("relation_source_type", RelationSourceType.MANUAL),
            ("relation_source_type", None),
            ("relation_source_type", ""),
            ("relation_source_type", " manual "),
            ("relation_source_type", "pkv-resource-enum-canary"),
        ],
    )
    async def test_relation_by_edge_rejects_invalid_enums_before_service(
        self,
        field_name,
        invalid_value,
    ):
        from src.mcp.resources import get_relation_by_edge_resource
        from src.mcp.server import mcp

        kwargs = {
            "source_knowledge_id": "1",
            "target_knowledge_id": "2",
            "relation_type": "references",
            "relation_source_type": "manual",
        }
        kwargs[field_name] = invalid_value
        with patch("src.mcp.resources.get_relation_query_service") as getter:
            with pytest.raises(ValueError) as error:
                await get_relation_by_edge_resource(**kwargs)
            if type(invalid_value) is str:
                relation_type = quote(str(kwargs["relation_type"]), safe="")
                relation_source_type = quote(
                    str(kwargs["relation_source_type"]),
                    safe="",
                )
                uri = (
                    "pkv://relations/by-edge/1/2/"
                    f"{relation_type}/{relation_source_type}"
                )
                with pytest.raises(Exception) as fastmcp_error:
                    await mcp.read_resource(uri)

        assert str(error.value) == f"无效的 {field_name}"
        assert "pkv-resource-enum-canary" not in str(error.value)
        if type(invalid_value) is str:
            assert "pkv-resource-enum-canary" not in str(fastmcp_error.value)
        getter.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("handler_name", "kwargs", "uri"),
        [
            ("get_entry_content", {"knowledge_id": "01"}, "pkv://entries/01"),
            (
                "get_entry_metadata",
                {"knowledge_id": "+1"},
                f"pkv://entries/{quote('+1', safe='')}/metadata",
            ),
            (
                "get_entry_chunk",
                {"knowledge_id": " 1 ", "chunk_id": "101"},
                f"pkv://entries/{quote(' 1 ', safe='')}/chunks/101",
            ),
            (
                "get_entry_chunk_by_index",
                {"knowledge_id": "١", "chunk_index": "0"},
                f"pkv://entries/{quote('١', safe='')}/chunk-index/0",
            ),
            (
                "get_entry_chunk",
                {"knowledge_id": "1", "chunk_id": "0101"},
                "pkv://entries/1/chunks/0101",
            ),
            (
                "get_entry_chunk_by_index",
                {"knowledge_id": "1", "chunk_index": "00"},
                "pkv://entries/1/chunk-index/00",
            ),
            (
                "get_relation_resource",
                {"relation_id": "07"},
                "pkv://relations/07",
            ),
            (
                "get_relation_by_edge_resource",
                {
                    "source_knowledge_id": "01",
                    "target_knowledge_id": "2",
                    "relation_type": "references",
                    "relation_source_type": "manual",
                },
                "pkv://relations/by-edge/01/2/references/manual",
            ),
            (
                "get_relation_by_edge_resource",
                {
                    "source_knowledge_id": "1",
                    "target_knowledge_id": "02",
                    "relation_type": "references",
                    "relation_source_type": "manual",
                },
                "pkv://relations/by-edge/1/02/references/manual",
            ),
            (
                "get_entry_content",
                {"knowledge_id": "9223372036854775808"},
                "pkv://entries/9223372036854775808",
            ),
        ],
    )
    async def test_noncanonical_resource_integers_are_rejected_before_backends(
        self,
        handler_name,
        kwargs,
        uri,
        caplog,
    ):
        from src.mcp import resources
        from src.mcp.server import mcp

        with (
            patch.object(resources, "get_sqlite_store") as sqlite_getter,
            patch.object(
                resources,
                "get_markdown_store",
            ) as markdown_getter,
            patch.object(
                resources,
                "get_relation_query_service",
            ) as relation_getter,
        ):
            with pytest.raises(ValueError) as direct_error:
                await getattr(resources, handler_name)(**kwargs)
            with pytest.raises(Exception) as fastmcp_error:
                await mcp.read_resource(uri)

        assert str(direct_error.value).startswith("无效的 ")
        assert "RESOURCE_INTEGER_CANARY" not in str(fastmcp_error.value)
        assert "RESOURCE_INTEGER_CANARY" not in caplog.text
        sqlite_getter.assert_not_called()
        markdown_getter.assert_not_called()
        relation_getter.assert_not_called()

    @pytest.mark.asyncio
    async def test_resource_integer_rejects_string_subclasses_before_backends(self):
        from src.mcp import resources

        class StringSubclass(str):
            pass

        with patch.object(resources, "get_sqlite_store") as sqlite_getter:
            with pytest.raises(ValueError, match="无效的 knowledge_id"):
                await resources.get_entry_content(
                    knowledge_id=StringSubclass("1")
                )

        sqlite_getter.assert_not_called()

    @pytest.mark.asyncio
    async def test_metadata_field_name_is_exact_direct_and_fastmcp(self):
        from src.mcp import resources
        from src.mcp.server import mcp

        field_name = " event_time "
        uri = f"pkv://entries/1/metadata/{quote(field_name, safe='')}"
        with patch.object(resources, "get_sqlite_store") as sqlite_getter:
            with pytest.raises(ValueError) as direct_error:
                await resources.get_entry_metadata_field(
                    knowledge_id="1",
                    field_name=field_name,
                )
            with pytest.raises(Exception) as fastmcp_error:
                await mcp.read_resource(uri)

        assert str(direct_error.value) == "不支持的 timeline 元数据字段"
        assert "不支持的 timeline 元数据字段" in str(fastmcp_error.value)
        sqlite_getter.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "case",
        [
            "duck",
            "magic_mock",
            "record_subclass",
            "empty_dict",
            "relation_id_none",
            "relation_id_mismatch",
            "relation_id_bool",
            "source_id_bool",
            "target_id_bool",
            "relation_type_string",
            "relation_source_type_string",
            "direction_string",
            "weight_bool",
            "weight_zero",
            "weight_nan",
            "weight_infinite",
            "weight_huge_integer",
            "evidence_list",
            "evidence_dict_subclass",
            "evidence_unknown_object",
            "created_at_integer",
            "updated_at_integer",
            "missing_attribute",
        ],
    )
    async def test_relation_resource_rejects_malformed_record_direct_and_fastmcp(
        self,
        case,
        caplog,
    ):
        from src.mcp import resources
        from src.mcp.server import mcp

        secret = "api_key_RELATION_RECORD_CONTRACT_CANARY"

        class RelationProbe:
            calls = 0

            @property
            def relation_id(self):
                type(self).calls += 1
                raise RuntimeError(secret)

        class RelationRecordSubclass(RelationRecord):
            pass

        class DictSubclass(dict):
            pass

        class SecretObject:
            calls = 0

            def __str__(self):
                type(self).calls += 1
                return secret

        if case == "duck":
            record = RelationProbe()
        elif case == "magic_mock":
            record = MagicMock(relation_id=7, evidence_payload={"note": secret})
        elif case == "record_subclass":
            record = RelationRecordSubclass(
                relation_id=7,
                source_knowledge_id=1,
                target_knowledge_id=2,
                relation_type=RelationType.REFERENCES,
                relation_source_type=RelationSourceType.MANUAL,
                evidence_payload={"note": secret},
            )
        elif case == "empty_dict":
            record = {}
        else:
            record = _make_relation_record(evidence_payload={"note": secret})
            mutations = {
                "relation_id_none": ("relation_id", None),
                "relation_id_mismatch": ("relation_id", 8),
                "relation_id_bool": ("relation_id", True),
                "source_id_bool": ("source_knowledge_id", True),
                "target_id_bool": ("target_knowledge_id", True),
                "relation_type_string": ("relation_type", "references"),
                "relation_source_type_string": (
                    "relation_source_type",
                    "manual",
                ),
                "direction_string": ("direction", "directed"),
                "weight_bool": ("weight", True),
                "weight_zero": ("weight", 0.0),
                "weight_nan": ("weight", float("nan")),
                "weight_infinite": ("weight", float("inf")),
                "weight_huge_integer": ("weight", 10 ** 10000),
                "evidence_list": ("evidence_payload", [secret]),
                "evidence_dict_subclass": (
                    "evidence_payload",
                    DictSubclass(note=secret),
                ),
                "evidence_unknown_object": (
                    "evidence_payload",
                    {"note": SecretObject()},
                ),
                "created_at_integer": ("created_at", 123),
                "updated_at_integer": ("updated_at", 123),
            }
            if case == "missing_attribute":
                del record.evidence_payload
            else:
                attribute, value = mutations[case]
                setattr(record, attribute, value)

        relation_store = MagicMock()
        relation_store.get_relation.return_value = record
        relation_service = MagicMock()
        relation_service.relation_store = relation_store

        with patch.object(
            resources,
            "get_relation_query_service",
            return_value=relation_service,
        ), patch.object(resources, "get_sqlite_store") as sqlite_getter:
            with pytest.raises(ValueError) as direct_error:
                await resources.get_relation_resource(relation_id="7")
            with pytest.raises(Exception) as fastmcp_error:
                await mcp.read_resource("pkv://relations/7")

        assert str(direct_error.value) == (
            "resource_not_readable: 请求的资源暂时不可用"
        )
        assert "resource_not_readable" in str(fastmcp_error.value)
        assert secret not in str(direct_error.value)
        assert secret not in str(fastmcp_error.value)
        assert secret not in caplog.text
        assert RelationProbe.calls == 0
        assert SecretObject.calls == 0
        sqlite_getter.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "case",
        [
            "records_scalar",
            "records_list_subclass",
            "malformed_sibling",
            "record_subclass_sibling",
            "source_identity_mismatch",
            "target_identity_mismatch",
            "relation_id_bool",
            "relation_type_string",
            "weight_infinite",
            "weight_huge_integer",
            "evidence_unknown_object",
        ],
    )
    async def test_relation_by_edge_rejects_malformed_collection_direct_and_fastmcp(
        self,
        case,
        caplog,
    ):
        from src.mcp import resources
        from src.mcp.server import mcp

        secret = "api_key_RELATION_EDGE_CONTRACT_CANARY"

        class ListSubclass(list):
            pass

        class RelationRecordSubclass(RelationRecord):
            pass

        class SecretObject:
            calls = 0

            def __str__(self):
                type(self).calls += 1
                return secret

        matching = _make_relation_record(evidence_payload={"note": secret})
        if case == "records_scalar":
            records = {"record": secret}
        elif case == "records_list_subclass":
            records = ListSubclass([matching])
        elif case == "malformed_sibling":
            records = [matching, {"note": secret}]
        elif case == "record_subclass_sibling":
            records = [
                matching,
                RelationRecordSubclass(
                    relation_id=8,
                    source_knowledge_id=1,
                    target_knowledge_id=2,
                    relation_type=RelationType.PARENT_OF,
                    relation_source_type=RelationSourceType.MANUAL,
                    evidence_payload={"note": secret},
                ),
            ]
        elif case == "source_identity_mismatch":
            records = [
                _make_relation_record(
                    source_knowledge_id=3,
                    evidence_payload={"note": secret},
                )
            ]
        elif case == "target_identity_mismatch":
            records = [
                _make_relation_record(
                    target_knowledge_id=3,
                    evidence_payload={"note": secret},
                )
            ]
        else:
            bad_sibling = _make_relation_record(
                relation_id=8,
                relation_type=RelationType.PARENT_OF,
                evidence_payload={"note": secret},
            )
            mutations = {
                "relation_id_bool": ("relation_id", True),
                "relation_type_string": ("relation_type", "parent_of"),
                "weight_infinite": ("weight", float("inf")),
                "weight_huge_integer": ("weight", 10 ** 10000),
                "evidence_unknown_object": (
                    "evidence_payload",
                    {"note": SecretObject()},
                ),
            }
            attribute, value = mutations[case]
            setattr(bad_sibling, attribute, value)
            records = [matching, bad_sibling]

        relation_store = MagicMock()
        relation_store.list_relations_between.return_value = records
        relation_service = MagicMock()
        relation_service.relation_store = relation_store

        with patch.object(
            resources,
            "get_relation_query_service",
            return_value=relation_service,
        ), patch.object(resources, "get_sqlite_store") as sqlite_getter:
            with pytest.raises(ValueError) as direct_error:
                await resources.get_relation_by_edge_resource(
                    source_knowledge_id="1",
                    target_knowledge_id="2",
                    relation_type="references",
                    relation_source_type="manual",
                )
            with pytest.raises(Exception) as fastmcp_error:
                await mcp.read_resource(
                    "pkv://relations/by-edge/1/2/references/manual"
                )

        assert str(direct_error.value) == (
            "resource_not_readable: 请求的资源暂时不可用"
        )
        assert "resource_not_readable" in str(fastmcp_error.value)
        assert secret not in str(direct_error.value)
        assert secret not in str(fastmcp_error.value)
        assert secret not in caplog.text
        assert SecretObject.calls == 0
        sqlite_getter.assert_not_called()

    @pytest.mark.parametrize("kind", ["duck", "record_subclass"])
    def test_relation_payload_requires_exact_relation_record(self, kind, caplog):
        from src.mcp import resources

        secret = "api_key_RELATION_PAYLOAD_CONTRACT_CANARY"

        class RelationRecordSubclass(RelationRecord):
            pass

        record = (
            {"evidence_payload": secret}
            if kind == "duck"
            else RelationRecordSubclass(
                relation_id=7,
                source_knowledge_id=1,
                target_knowledge_id=2,
                relation_type=RelationType.REFERENCES,
                relation_source_type=RelationSourceType.MANUAL,
                evidence_payload={"note": secret},
            )
        )

        with pytest.raises(ValueError) as error:
            resources._relation_payload(record)

        assert str(error.value) == (
            "resource_not_readable: 请求的资源暂时不可用"
        )
        assert secret not in str(error.value)
        assert secret not in caplog.text

    @pytest.mark.parametrize(
        ("handler_name", "kwargs", "backend_getter"),
        [
            ("get_entry_content", {"knowledge_id": "1"}, "get_sqlite_store"),
            ("get_entry_metadata", {"knowledge_id": "1"}, "get_sqlite_store"),
            (
                "get_entry_chunk",
                {"knowledge_id": "1", "chunk_id": "101"},
                "get_sqlite_store",
            ),
            (
                "get_entry_chunk_by_index",
                {"knowledge_id": "1", "chunk_index": "0"},
                "get_sqlite_store",
            ),
            (
                "get_entry_metadata_field",
                {"knowledge_id": "1", "field_name": "event_time"},
                "get_sqlite_store",
            ),
            ("get_tags_resource", {}, "get_sqlite_store"),
            ("get_stats_resource", {}, "get_sqlite_store"),
            (
                "get_relation_resource",
                {"relation_id": "7"},
                "get_relation_query_service",
            ),
            (
                "get_relation_by_edge_resource",
                {
                    "source_knowledge_id": "1",
                    "target_knowledge_id": "2",
                    "relation_type": "references",
                    "relation_source_type": "manual",
                },
                "get_relation_query_service",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_resource_backend_failures_are_stable_and_redacted(
        self,
        handler_name,
        kwargs,
        backend_getter,
        caplog,
    ):
        from src.mcp import resources

        canary = r"api_key=RESOURCE-CANARY C:\private\vault.db"
        with patch.object(
            resources,
            backend_getter,
            side_effect=RuntimeError(canary),
        ):
            with pytest.raises(ValueError) as error:
                await getattr(resources, handler_name)(**kwargs)

        assert str(error.value) == (
            "resource_not_readable: 请求的资源暂时不可用"
        )
        assert "RESOURCE-CANARY" not in str(error.value)
        assert "RESOURCE-CANARY" not in caplog.text
        assert r"C:\private\vault.db" not in caplog.text

    @pytest.mark.parametrize(
        ("uri", "backend_getter"),
        [
            ("pkv://entries/1", "get_sqlite_store"),
            ("pkv://entries/1/metadata", "get_sqlite_store"),
            ("pkv://entries/1/chunks/101", "get_sqlite_store"),
            ("pkv://entries/1/chunk-index/0", "get_sqlite_store"),
            ("pkv://entries/1/metadata/event_time", "get_sqlite_store"),
            ("pkv://tags", "get_sqlite_store"),
            ("pkv://stats", "get_sqlite_store"),
            ("pkv://relations/7", "get_relation_query_service"),
            (
                "pkv://relations/by-edge/1/2/references/manual",
                "get_relation_query_service",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_fastmcp_resource_error_never_exposes_backend_diagnostics(
        self,
        uri,
        backend_getter,
        caplog,
    ):
        from src.mcp import resources
        from src.mcp.server import mcp

        canary = r"api_key=RESOURCE-PROTOCOL-CANARY C:\private\vault.db"
        with patch.object(
            resources,
            backend_getter,
            side_effect=RuntimeError(canary),
        ):
            with pytest.raises(Exception) as error:
                await mcp.read_resource(uri)

        rendered = str(error.value)
        assert "resource_not_readable" in rendered
        assert "RESOURCE-PROTOCOL-CANARY" not in rendered
        assert r"C:\private\vault.db" not in rendered
        assert "RESOURCE-PROTOCOL-CANARY" not in caplog.text
        assert r"C:\private\vault.db" not in caplog.text

    @pytest.mark.asyncio
    async def test_metadata_field_resource_rejects_non_timeline_field(self):
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = MOCK_ENTRY_DB

        with patch("src.mcp.resources.get_sqlite_store", return_value=mock_store):
            from src.mcp.resources import get_entry_metadata_field
            with pytest.raises(ValueError) as error:
                await get_entry_metadata_field(
                    knowledge_id="1",
                    field_name="title",
                )

        assert str(error.value) == "不支持的 timeline 元数据字段"
        mock_store.query_by_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_metadata_field_rejection_is_fixed_and_redacted_direct_and_fastmcp(
        self,
        caplog,
    ):
        from src.mcp import resources
        from src.mcp.server import mcp

        secret = "METADATA_FIELD_SECRET_CANARY"
        malicious_field = f"title\r\napi_key={secret} C:\\private\\vault.db"
        encoded_field = quote(malicious_field, safe="")

        with patch.object(resources, "get_sqlite_store") as getter:
            with pytest.raises(ValueError) as direct_error:
                await resources.get_entry_metadata_field(
                    knowledge_id="1",
                    field_name=malicious_field,
                )
            with pytest.raises(Exception) as fastmcp_error:
                await mcp.read_resource(
                    f"pkv://entries/1/metadata/{encoded_field}"
                )

        assert str(direct_error.value) == "不支持的 timeline 元数据字段"
        assert "不支持的 timeline 元数据字段" in str(fastmcp_error.value)
        assert secret not in str(direct_error.value)
        assert secret not in str(fastmcp_error.value)
        assert secret not in caplog.text
        assert r"C:\private\vault.db" not in str(direct_error.value)
        assert r"C:\private\vault.db" not in str(fastmcp_error.value)
        getter.assert_not_called()

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

    @pytest.mark.parametrize(
        "backend_result",
        [
            pytest.param([False, None, ""], id="list"),
            pytest.param((True, "api_key=FIELD-CANARY"), id="short-tuple"),
            pytest.param(
                (1, "api_key=FIELD-CANARY", "knowledge_items"),
                id="non-bool-found",
            ),
            pytest.param(
                (True, "api_key=FIELD-CANARY", "unexpected_store"),
                id="invalid-storage-field",
            ),
            pytest.param(
                (True, 7, "knowledge_items"),
                id="non-string-value",
            ),
            pytest.param(
                (True, "", "knowledge_items"),
                id="empty-value",
            ),
            pytest.param(
                (False, "api_key=FIELD-CANARY", ""),
                id="false-with-value",
            ),
            pytest.param(
                (False, None, "knowledge_items"),
                id="false-with-storage",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_metadata_field_backend_tuple_is_strict_direct_and_fastmcp(
        self,
        backend_result,
        tmp_path: Path,
        caplog,
    ):
        from src.mcp import resources
        from src.mcp.server import mcp

        store = MagicMock()
        store.query_by_id.return_value = MOCK_ENTRY_DB
        markdown_store = MagicMock()
        markdown_store.vault_dir = tmp_path / "vault"

        with patch.object(
            resources,
            "get_sqlite_store",
            return_value=store,
        ), patch.object(
            resources,
            "get_markdown_store",
            return_value=markdown_store,
        ), patch.object(
            resources,
            "read_persisted_metadata_field",
            return_value=backend_result,
        ):
            with pytest.raises(ValueError) as direct_error:
                await resources.get_entry_metadata_field(
                    knowledge_id="1",
                    field_name="event_time",
                )
            with pytest.raises(Exception) as fastmcp_error:
                await mcp.read_resource(
                    "pkv://entries/1/metadata/event_time"
                )

        expected = "resource_not_readable: 请求的资源暂时不可用"
        assert str(direct_error.value) == expected
        assert "resource_not_readable" in str(fastmcp_error.value)
        assert "FIELD-CANARY" not in str(direct_error.value)
        assert "FIELD-CANARY" not in str(fastmcp_error.value)
        assert "FIELD-CANARY" not in caplog.text

    @pytest.mark.asyncio
    async def test_metadata_field_exact_missing_tuple_direct_and_fastmcp(
        self,
        tmp_path: Path,
    ):
        from src.mcp import resources
        from src.mcp.server import mcp

        store = MagicMock()
        store.query_by_id.return_value = MOCK_ENTRY_DB
        markdown_store = MagicMock()
        markdown_store.vault_dir = tmp_path / "vault"

        with patch.object(
            resources,
            "get_sqlite_store",
            return_value=store,
        ), patch.object(
            resources,
            "get_markdown_store",
            return_value=markdown_store,
        ), patch.object(
            resources,
            "read_persisted_metadata_field",
            return_value=(False, None, ""),
        ):
            with pytest.raises(ValueError) as direct_error:
                await resources.get_entry_metadata_field(
                    knowledge_id="1",
                    field_name="event_time",
                )
            with pytest.raises(Exception) as fastmcp_error:
                await mcp.read_resource(
                    "pkv://entries/1/metadata/event_time"
                )

        assert "不存在元数据字段" in str(direct_error.value)
        assert "不存在元数据字段" in str(fastmcp_error.value)

    @pytest.mark.asyncio
    async def test_metadata_field_exact_success_tuple_direct_and_fastmcp(
        self,
        tmp_path: Path,
    ):
        from src.mcp import resources
        from src.mcp.server import mcp

        store = MagicMock()
        store.query_by_id.return_value = MOCK_ENTRY_DB
        markdown_store = MagicMock()
        markdown_store.vault_dir = tmp_path / "vault"
        backend_result = (
            True,
            "2026-08-07T10:00:00+08:00",
            "knowledge_items",
        )

        with patch.object(
            resources,
            "get_sqlite_store",
            return_value=store,
        ), patch.object(
            resources,
            "get_markdown_store",
            return_value=markdown_store,
        ), patch.object(
            resources,
            "read_persisted_metadata_field",
            return_value=backend_result,
        ):
            direct = await resources.get_entry_metadata_field(
                knowledge_id="1",
                field_name="event_time",
            )
            contents = await mcp.read_resource(
                "pkv://entries/1/metadata/event_time"
            )

        direct_payload = json.loads(direct)
        protocol_payload = json.loads(list(contents)[0].content)
        assert protocol_payload == direct_payload
        assert direct_payload == {
            "knowledge_id": 1,
            "field": "event_time",
            "physical_source_field": "event_time",
            "storage_field": "knowledge_items",
            "value": "2026-08-07T10:00:00+08:00",
            "citation_locator": "pkv://entries/1/metadata/event_time",
        }

    @pytest.mark.asyncio
    async def test_invalid_id(self):
        """非数字 ID 应返回 JSON 错误。"""
        from src.mcp.resources import get_entry_metadata
        with pytest.raises(ValueError, match="无效"):
            await get_entry_metadata(knowledge_id="abc")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "entry_empty",
        "entry_scalar",
        "entry_dict_subclass",
        "tags_empty_string",
        "tags_list_subclass",
        "tags_boolean_count",
        "tags_duplicate_name",
        "tags_out_of_order",
        "stats_empty",
        "stats_scalar",
        "stats_dict_subclass",
        "stats_boolean_count",
        "stats_duplicate_source",
        "stats_zero_source_count",
        "stats_source_not_conserved",
        "stats_duplicate_tag",
        "stats_tag_exceeds_total",
        "stats_tags_out_of_order",
        "stats_too_many_top_tags",
    ],
)
async def test_resource_malformed_backend_is_stable_direct_and_fastmcp(case, caplog):
    from src.mcp import resources
    from src.mcp.server import mcp

    secret = "api_key_RESOURCE_SHAPE_CANARY"

    class DictSubclass(dict):
        pass

    class ListSubclass(list):
        pass

    store = MagicMock()
    if case == "entry_empty":
        handler, kwargs, uri = (
            resources.get_entry_content,
            {"knowledge_id": "1"},
            "pkv://entries/1",
        )
        store.query_by_id.return_value = {}
    elif case == "entry_scalar":
        handler, kwargs, uri = (
            resources.get_entry_content,
            {"knowledge_id": "1"},
            "pkv://entries/1",
        )
        store.query_by_id.return_value = secret
    elif case == "entry_dict_subclass":
        handler, kwargs, uri = (
            resources.get_entry_content,
            {"knowledge_id": "1"},
            "pkv://entries/1",
        )
        store.query_by_id.return_value = DictSubclass(
            knowledge_id=1,
            title=secret,
            source_type="text",
            file_path="entry.md",
        )
    elif case == "tags_empty_string":
        handler, kwargs, uri = resources.get_tags_resource, {}, "pkv://tags"
        store.get_all_tags_with_count.return_value = ""
    elif case == "tags_list_subclass":
        handler, kwargs, uri = resources.get_tags_resource, {}, "pkv://tags"
        store.get_all_tags_with_count.return_value = ListSubclass(
            [{"name": secret, "count": 1}]
        )
    elif case == "tags_boolean_count":
        handler, kwargs, uri = resources.get_tags_resource, {}, "pkv://tags"
        store.get_all_tags_with_count.return_value = [
            {"name": secret, "count": True}
        ]
    elif case == "tags_duplicate_name":
        handler, kwargs, uri = resources.get_tags_resource, {}, "pkv://tags"
        store.get_all_tags_with_count.return_value = [
            {"name": secret, "count": 2},
            {"name": secret, "count": 1},
        ]
    elif case == "tags_out_of_order":
        handler, kwargs, uri = resources.get_tags_resource, {}, "pkv://tags"
        store.get_all_tags_with_count.return_value = [
            {"name": "safe", "count": 1},
            {"name": secret, "count": 2},
        ]
    elif case == "stats_empty":
        handler, kwargs, uri = resources.get_stats_resource, {}, "pkv://stats"
        store.get_statistics.return_value = {}
    elif case == "stats_scalar":
        handler, kwargs, uri = resources.get_stats_resource, {}, "pkv://stats"
        store.get_statistics.return_value = secret
    elif case == "stats_dict_subclass":
        handler, kwargs, uri = resources.get_stats_resource, {}, "pkv://stats"
        store.get_statistics.return_value = DictSubclass(
            total_entries=0,
            by_source_type=[],
            top_tags=[{"name": secret, "count": 0}],
        )
    elif case == "stats_duplicate_source":
        handler, kwargs, uri = resources.get_stats_resource, {}, "pkv://stats"
        store.get_statistics.return_value = {
            "total_entries": 2,
            "by_source_type": [(secret, 1), (secret, 1)],
            "top_tags": [],
        }
    elif case == "stats_zero_source_count":
        handler, kwargs, uri = resources.get_stats_resource, {}, "pkv://stats"
        store.get_statistics.return_value = {
            "total_entries": 0,
            "by_source_type": [(secret, 0)],
            "top_tags": [],
        }
    elif case == "stats_source_not_conserved":
        handler, kwargs, uri = resources.get_stats_resource, {}, "pkv://stats"
        store.get_statistics.return_value = {
            "total_entries": 1,
            "by_source_type": [(secret, 2)],
            "top_tags": [],
        }
    elif case == "stats_duplicate_tag":
        handler, kwargs, uri = resources.get_stats_resource, {}, "pkv://stats"
        store.get_statistics.return_value = {
            "total_entries": 2,
            "by_source_type": [("webpage", 2)],
            "top_tags": [
                {"name": secret, "count": 1},
                {"name": secret, "count": 1},
            ],
        }
    elif case == "stats_tag_exceeds_total":
        handler, kwargs, uri = resources.get_stats_resource, {}, "pkv://stats"
        store.get_statistics.return_value = {
            "total_entries": 1,
            "by_source_type": [("webpage", 1)],
            "top_tags": [{"name": secret, "count": 2}],
        }
    elif case == "stats_tags_out_of_order":
        handler, kwargs, uri = resources.get_stats_resource, {}, "pkv://stats"
        store.get_statistics.return_value = {
            "total_entries": 2,
            "by_source_type": [("webpage", 2)],
            "top_tags": [
                {"name": "safe", "count": 1},
                {"name": secret, "count": 2},
            ],
        }
    elif case == "stats_too_many_top_tags":
        handler, kwargs, uri = resources.get_stats_resource, {}, "pkv://stats"
        store.get_statistics.return_value = {
            "total_entries": 21,
            "by_source_type": [("webpage", 21)],
            "top_tags": [
                {
                    "name": f"tag-{index}-{secret if index == 0 else 'safe'}",
                    "count": 1,
                }
                for index in range(21)
            ],
        }
    else:
        handler, kwargs, uri = resources.get_stats_resource, {}, "pkv://stats"
        store.get_statistics.return_value = {
            "total_entries": True,
            "by_source_type": [(secret, 1)],
            "top_tags": [],
        }

    with patch.object(resources, "get_sqlite_store", return_value=store):
        with pytest.raises(ValueError) as direct_error:
            await handler(**kwargs)
        with pytest.raises(Exception) as fastmcp_error:
            await mcp.read_resource(uri)

    assert str(direct_error.value) == (
        "resource_not_readable: 请求的资源暂时不可用"
    )
    assert "resource_not_readable" in str(fastmcp_error.value)
    assert secret not in str(direct_error.value)
    assert secret not in str(fastmcp_error.value)
    assert secret not in caplog.text


def test_json_resource_never_calls_string_coercion_for_unknown_objects(caplog):
    from src.mcp import resources

    secret = "api_key_RESOURCE_STR_CANARY"

    class SecretObject:
        calls = 0

        def __str__(self):
            type(self).calls += 1
            return secret

    with pytest.raises(ValueError) as error:
        resources._json_resource({"value": SecretObject()})

    assert str(error.value) == "resource_not_readable: 请求的资源暂时不可用"
    assert SecretObject.calls == 0
    assert secret not in str(error.value)
    assert secret not in caplog.text


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
            "by_source_type": [
                ("wechat", 50),
                ("zhihu", 30),
                ("webpage", 20),
            ],
            "top_tags": [{"name": "AI", "count": 20}],
        }
        mock_store = MagicMock()
        mock_store.get_statistics.return_value = mock_stats

        with patch("src.mcp.resources.get_sqlite_store", return_value=mock_store):
            from src.mcp.resources import get_stats_resource
            result = await get_stats_resource()

        data = json.loads(result)
        assert data["total_entries"] == 100
        assert data["top_tags"][0]["count"] == 20

    @pytest.mark.asyncio
    async def test_stats_resource_redacts_local_values(self):
        mock_store = MagicMock()
        mock_store.get_statistics.return_value = {
            "total_entries": 1,
            "by_source_type": [(r"\??\C:\private", 1)],
            "top_tags": [],
        }

        with patch("src.mcp.resources.get_sqlite_store", return_value=mock_store):
            from src.mcp.resources import get_stats_resource
            result = await get_stats_resource()

        assert json.loads(result)["by_source_type"][0][0] == (
            "[redacted-local-reference]"
        )
