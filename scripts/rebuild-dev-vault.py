#!/usr/bin/env python3
"""开发专用轻量重建入口（P1）。

在安全隔离根上执行 可受控清理 -> 数据库迁移 -> 确定性最小种子 -> 健康检查
的完整重建流程，并支持幂等重复执行。

安全契约（禁止生产数据）:
- 默认根目录为仓库内 ``.data-test/rebuild-dev``，绝不隐式指向 ``.data``。
- 重建根严格只能是仓库 ``.data-test`` 的专用子目录；``.data``、仓库其他
  目录、仓库外路径、文件系统根、用户主目录等危险目标一律拒绝，无任何旁路开关。
- 危险目标拒绝为纯字符串判断，不解析、不 stat 被拒绝的路径。
- 清理前检查路径上的 junction / 符号链接 / 硬链接，拒绝绕过边界。
- 迁移始终以 ``auto_backup=False`` 执行（自动备份脚本会读取生产 ``.data``）。
- 通过版本化 ``rebuild-manifest.json`` 识别本脚本完整生成的 root；非空但
  缺少/损坏 manifest、数据库缺失、结构不完整、pending migrations 或版本
  未到最新的 root 一律 fail-closed 拒绝（exit 1），不写入、不清理，
  必须显式 ``--force`` 才能重建。
- 对已通过边界校验的已存在 root，在任何内容读取（iterdir / manifest /
  DB）之前执行只读递归内部链接扫描；发现 symlink / junction / hardlink
  或扫描无法完整遍历（权限/IO 错误）立即拒绝（exit 2），扫描本身不跟随
  子项链接。

用法:
  python scripts/rebuild-dev-vault.py                  # 重建或检查默认根
  python scripts/rebuild-dev-vault.py --root .data-test/rebuild-dev
  python scripts/rebuild-dev-vault.py --force          # 受控清理后完整重建
  python scripts/rebuild-dev-vault.py --check-only     # 仅健康检查，绝不写入
  python scripts/rebuild-dev-vault.py --json           # 机器可读结果契约

退出码:
  0  成功（重建 / 本脚本完整生成且结构校验通过的 root 已是最新 / 健康检查通过）
  1  流程/参数值失败（如 count 无效、迁移/种子/健康检查失败，或结构校验拒绝）
  2  CLI 语法错误或根目录/链接安全拒绝（危险目标等）
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import shutil
import sqlite3
import stat
import sys
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import frontmatter

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.storage.markdown_store import Entry, MarkdownStore  # noqa: E402
from src.storage.migration_manager import MigrationManager  # noqa: E402
from src.storage.sqlite_store import SQLiteStore  # noqa: E402
from src.utils.text_utils import TextProcessor  # noqa: E402

DEFAULT_ROOT_REL = ".data-test/rebuild-dev"
DEFAULT_SEED = 20260731
DEFAULT_COUNT = 3
MIGRATIONS_DIR = PROJECT_ROOT / "scripts" / "migrations"

# 生产数据目录名（大小写不敏感比较），无论位于何处都拒绝作为重建根。
_PROHIBITED_ROOT_NAMES = {".data", ".data_backup", ".data-backup", "backups"}
# 测试根本身也不得作为重建根：必须使用其下的专用子目录。
_TEST_ROOT_NAMES = {".data-test", ".data_test"}

# 确定性最小种子内容（不依赖外部 fixture / 网络 / AI 服务）。
_SEED_TOPICS = ["开发环境", "测试契约", "知识工作流", "离线验证"]
_SEED_BODIES = [
    "这是开发重建用的确定性种子条目，仅用于验证本地开发环境。",
    "内容不包含任何用户真实数据，可由固定种子完全复现。",
    "用于验证迁移链、统计与检索契约在隔离根上正常工作。",
]
_SEED_TAGS = ["重建", "种子", "开发", "测试"]

MANIFEST_NAME = "rebuild-manifest.json"
MANIFEST_VERSION = 1
TOOL_NAME = "rebuild-dev-vault"
_MANIFEST_KEYS = (
    "manifest_version",
    "tool",
    "schema_version",
    "seeded",
    "seed_count",
    "seed",
    "created_at",
)
_STANDARD_DIRS = ("db", "vault", "vectors", "logs", "tmp")
_REQUIRED_TABLES = {
    "schema_version",
    "knowledge_items",
    "content_chunks",
    "tags",
    "knowledge_tags",
    "knowledge_items_fts",
}
_REQUIRED_TRIGGERS = {
    "knowledge_items_ai",
    "knowledge_items_au",
    "knowledge_items_ad",
}


class RebuildError(Exception):
    """重建流程失败（退出码 1）。"""


class RootRejectedError(RebuildError):
    """根目录校验拒绝（退出码 2）。"""


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_unsafe_link(path: Path, *, include_mount: bool = False) -> bool:
    """检测符号链接 / junction / 硬链接及受控根内部挂载点。"""
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError:
        return False
    file_attributes = getattr(path_stat, "st_file_attributes", 0)
    is_reparse_point = bool(file_attributes & 0x400)
    is_hard_linked_file = stat.S_ISREG(path_stat.st_mode) and path_stat.st_nlink > 1
    try:
        is_mount = include_mount and path.is_mount()
    except NotImplementedError:
        # Python 3.11 on Windows does not implement Path.is_mount(); junctions
        # and volume mount points remain covered by FILE_ATTRIBUTE_REPARSE_POINT.
        is_mount = False
    except OSError as exc:
        raise RootRejectedError(f"无法判断路径是否为挂载点: {path} - {exc}") from exc
    return (
        stat.S_ISLNK(path_stat.st_mode)
        or is_reparse_point
        or is_hard_linked_file
        or is_mount
    )


def _has_unsafe_link_on_path(root: Path) -> Path | None:
    """检查现有路径链上的 junction / 符号链接 / 硬链接。"""
    parts = list(root.parts)
    for index in range(len(parts) - 1, -1, -1):
        probe = Path(*parts[: index + 1])
        if probe.exists() or probe.is_symlink():
            if _is_unsafe_link(probe):
                return probe
    return None


def _assert_no_unsafe_links_under(root: Path) -> None:
    """清理前递归检查目标目录内不存在链接，避免误删链接指向的真实数据。"""
    unsafe = _find_unsafe_link_under(root)
    if unsafe is not None:
        raise RootRejectedError(f"拒绝清理包含链接或挂载点的目录: {unsafe}")


def _find_unsafe_link_under(root: Path) -> Path | None:
    """只读递归扫描 root 内部是否存在 symlink / junction / hardlink。

    不跟随子项链接：每个子项先 lstat 判定，命中立即返回；目录仅在确认
    非链接后才递归。扫描必须完整遍历既有 root 内部：任何权限/IO 错误都
    视为无法证明安全，fail-closed 立即以 RootRejectedError 拒绝，绝不
    吞掉错误继续。仅对已通过 resolve_rebuild_root 字符串/边界校验的
    候选根调用，任何内容读取之前执行。
    """
    if not root.exists():
        return None
    if _is_unsafe_link(root, include_mount=True):
        return root
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                child = Path(entry.path)
                if _is_unsafe_link(child, include_mount=True):
                    return child
                if entry.is_dir(follow_symlinks=False):
                    found = _find_unsafe_link_under(child)
                    if found is not None:
                        return found
    except OSError as exc:
        raise RootRejectedError(
            f"无法完整扫描重建根内部（扫描失败即拒绝）: {root} - {exc}"
        ) from exc
    return None


def _capture_root_identity(root: Path) -> tuple[int, int, int, int]:
    """记录根目录身份，用于在各阶段之间检测并发替换。"""
    try:
        root_stat = os.lstat(root)
    except OSError as exc:
        raise RootRejectedError(f"无法读取重建根身份: {root} - {exc}") from exc
    if _is_unsafe_link(root, include_mount=True):
        raise RootRejectedError(f"重建根是链接或挂载点，拒绝访问: {root}")
    return (
        root_stat.st_dev,
        root_stat.st_ino,
        stat.S_IFMT(root_stat.st_mode),
        getattr(root_stat, "st_file_attributes", 0),
    )


def _assert_root_identity(root: Path, expected: tuple[int, int, int, int]) -> None:
    """在读取/清理/写入阶段前复核根目录没有被替换。"""
    actual = _capture_root_identity(root)
    if actual != expected:
        raise RootRejectedError(f"重建根在执行期间被替换，拒绝继续: {root}")


def _has_meaningful_content(root: Path) -> bool:
    """空的标准目录 scaffold 不视为已有 Vault 内容。"""
    if not root.exists():
        return False
    try:
        for child in root.iterdir():
            if child.name not in _STANDARD_DIRS or not child.is_dir():
                return True
            if any(child.iterdir()):
                return True
    except OSError as exc:
        raise RootRejectedError(f"无法检查重建根内容: {root} - {exc}") from exc
    return False


def resolve_rebuild_root(
    root_arg: str,
    *,
    project_root: Path = PROJECT_ROOT,
) -> Path:
    """解析并校验重建根目录（危险拒绝为纯字符串判断，不 resolve/stat）。

    返回规范化后的根目录路径。重建根必须严格是仓库 ``.data-test`` 下的
    专用子目录；仓库外路径、生产数据目录名与链接绕过一律拒绝，且无任何
    旁路开关。
    """
    lexical = Path(root_arg).expanduser()
    if not lexical.is_absolute():
        lexical = project_root / lexical
    lexical = Path(os.path.abspath(lexical))

    test_root = project_root / ".data-test"

    # 1. 纯字符串危险目标拒绝（不调用 resolve/stat/exists/lstat）。
    if lexical == lexical.parent:
        raise RootRejectedError(f"拒绝文件系统根目录作为重建根: {lexical}")
    if lexical == project_root:
        raise RootRejectedError("重建根不能是项目根目录")
    try:
        if lexical == Path.home():
            raise RootRejectedError("重建根不能是用户主目录")
    except RuntimeError:
        pass
    for part in lexical.parts[1:]:
        if Path(part).name.casefold() in _PROHIBITED_ROOT_NAMES:
            raise RootRejectedError(f"重建根路径不得包含生产数据目录名: {part}")

    # 2. 仓库外路径一律拒绝（无任何旁路开关）。
    if not _is_relative_to(lexical, project_root):
        raise RootRejectedError(
            f"重建根必须位于仓库 .data-test 下，仓库外路径拒绝: {lexical}"
        )

    # 3. 仓库内路径：只允许严格位于 .data-test 下的专用子目录。
    if not _is_relative_to(lexical, test_root):
        raise RootRejectedError(
            f"仓库内重建根只能位于 .data-test 下，禁止其他项目路径: {lexical}"
        )
    if lexical == test_root:
        raise RootRejectedError(
            "重建根必须是 .data-test 下的专用子目录，"
            f"不能是 .data-test 本身: {lexical}"
        )
    rel_parts = lexical.relative_to(test_root).parts
    for part in rel_parts:
        if Path(part).name.casefold() in _TEST_ROOT_NAMES:
            raise RootRejectedError(f"重建根路径不得嵌套测试根目录名: {part}")

    # 4. 仅对候选根执行链接检查与解析后边界复核。
    link = _has_unsafe_link_on_path(test_root)
    if link is not None:
        raise RootRejectedError(f"测试数据路径不得经过 junction 或符号链接: {link}")
    link = _has_unsafe_link_on_path(lexical)
    if link is not None:
        raise RootRejectedError(f"测试数据路径不得经过 junction 或符号链接: {link}")
    resolved = lexical.resolve(strict=False)
    resolved_test_root = test_root.resolve(strict=False)
    if not _is_relative_to(resolved, resolved_test_root):
        raise RootRejectedError("测试数据路径解析后越过仓库 .data-test 边界")
    return resolved


def _cleanup_root(
    root: Path,
    *,
    expected_identity: tuple[int, int, int, int],
) -> None:
    """受控清理：删除根目录内容并重建，不删除根目录本身。"""
    if root.exists():
        _assert_root_identity(root, expected_identity)
        _assert_no_unsafe_links_under(root)
        for child in root.iterdir():
            try:
                _assert_root_identity(root, expected_identity)
                if _is_unsafe_link(child, include_mount=True):
                    raise RootRejectedError(f"清理目标被替换为链接或挂载点: {child}")
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            except OSError as exc:
                raise RebuildError(f"受控清理失败: {child} - {exc}") from exc
        _assert_root_identity(root, expected_identity)
    root.mkdir(parents=True, exist_ok=True)


def _run_migrations(db_path: Path) -> int:
    """执行数据库迁移链（auto_backup=False，禁止备份脚本读取生产 .data）。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    manager = MigrationManager(db_path, MIGRATIONS_DIR)
    applied = manager.apply_all_pending(auto_backup=False)
    return applied


def _ensure_standard_dirs(root: Path) -> None:
    """补齐重建根的标准子目录，保证结构完整可验证。"""
    for name in _STANDARD_DIRS:
        (root / name).mkdir(parents=True, exist_ok=True)


def _load_manifest(root: Path) -> dict | None:
    """读取重建 manifest；缺失或格式错误返回 None。"""
    manifest_path = root / MANIFEST_NAME
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_manifest(
    root: Path,
    *,
    schema_version: str,
    seeded: bool,
    seed_count: int,
    seed: int,
) -> Path:
    """原子写入版本化重建 manifest（最后一步，标记完整成功生成）。"""
    payload = {
        "manifest_version": MANIFEST_VERSION,
        "tool": TOOL_NAME,
        "schema_version": schema_version,
        "seeded": bool(seeded),
        "seed_count": seed_count,
        "seed": seed,
        "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }
    target = root / MANIFEST_NAME
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, target)
    return target


def _parse_version_from_migration(migration_file: Path) -> str | None:
    """从迁移脚本读取 '-- Version: x.y.z'。"""
    try:
        for line in migration_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("-- Version:"):
                return stripped.split(":", 1)[-1].strip() or None
    except OSError:
        return None
    return None


def _version_key(version: str) -> tuple:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return (0, 0, 0)


def _latest_migration_version(migrations_dir: Path) -> str:
    """迁移链中最大的脚本版本号。"""
    latest = "0.0.0"
    for migration_file in sorted(migrations_dir.glob("*.sql")):
        version = _parse_version_from_migration(migration_file)
        if version and _version_key(version) > _version_key(latest):
            latest = version
    return latest


def _safe_entry_count(db_path: Path) -> int | None:
    """只读统计条目数；数据库缺失/未初始化时返回 None（不创建任何文件）。"""
    try:
        with closing(_connect_read_only(db_path)) as conn:
            if not _table_exists(conn, "knowledge_items"):
                return None
            row = conn.execute("SELECT COUNT(*) FROM knowledge_items").fetchone()
            return int(row[0]) if row else 0
    except (OSError, sqlite3.Error):
        return None


def _connect_read_only(db_path: Path) -> sqlite3.Connection:
    """以 SQLite URI mode=ro 打开数据库，避免只读检查创建/修改文件。"""
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _read_schema_objects(db_path: Path) -> tuple[set[str], set[str]] | None:
    try:
        with closing(_connect_read_only(db_path)) as conn:
            rows = conn.execute(
                "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'trigger')"
            ).fetchall()
    except (OSError, sqlite3.Error):
        return None
    tables = {row["name"] for row in rows if row["type"] == "table"}
    triggers = {row["name"] for row in rows if row["type"] == "trigger"}
    return tables, triggers


def _read_entry_records(db_path: Path) -> list[dict] | None:
    try:
        with closing(_connect_read_only(db_path)) as conn:
            if not _table_exists(conn, "knowledge_items"):
                return None
            rows = conn.execute(
                """
                SELECT knowledge_id, file_path, title, content, source_type, source_url,
                       event_time, published_at, archived_at, keywords, tags,
                       summary_one_sentence, summary_100_words, search_strategy, word_count
                FROM knowledge_items
                ORDER BY knowledge_id
                """
            ).fetchall()
    except (OSError, sqlite3.Error):
        return None
    return [dict(row) for row in rows]


def _validate_fts_integrity(db_path: Path) -> list[str]:
    """Compare the FTS index with its source table without mutating either."""
    columns = ("title", "summary_100_words", "keywords", "tags")
    try:
        with closing(_connect_read_only(db_path)) as conn:
            if not all(
                _table_exists(conn, table)
                for table in ("knowledge_items", "knowledge_items_fts")
            ):
                return []
            source_rows = conn.execute(
                """
                SELECT knowledge_id AS rowid, title, summary_100_words, keywords, tags
                FROM knowledge_items
                ORDER BY knowledge_id
                """
            ).fetchall()
            fts_rows = conn.execute(
                """
                SELECT rowid, title, summary_100_words, keywords, tags
                FROM knowledge_items_fts
                ORDER BY rowid
                """
            ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        return [f"FTS 索引完整性不可读取: {exc}"]

    def indexed_values(row: sqlite3.Row) -> tuple[str, ...]:
        return tuple("" if row[column] is None else str(row[column]) for column in columns)

    source: dict[int, tuple[str, ...]] = {}
    for row in source_rows:
        expected = TextProcessor.prepare_fts5_data(
            row["title"] or "",
            row["summary_100_words"] or "",
            row["keywords"] or "",
            row["tags"] or "",
        )
        source[int(row["rowid"])] = tuple(str(expected[column]) for column in columns)
    indexed = {int(row["rowid"]): indexed_values(row) for row in fts_rows}
    issues: list[str] = []
    missing = source.keys() - indexed.keys()
    orphaned = indexed.keys() - source.keys()
    mismatched = {
        rowid for rowid in source.keys() & indexed.keys() if source[rowid] != indexed[rowid]
    }
    if missing:
        issues.append(f"FTS 索引缺失 {len(missing)} 个 knowledge_items 条目")
    if orphaned:
        issues.append(f"FTS 索引存在 {len(orphaned)} 个孤儿条目")
    if mismatched:
        issues.append(f"FTS 索引有 {len(mismatched)} 个条目的检索字段不一致")
    return issues


def _validate_sqlite_integrity(db_path: Path) -> list[str]:
    """Run read-only structural, foreign-key, and tag-count integrity checks."""
    try:
        with closing(_connect_read_only(db_path)) as conn:
            quick_rows = conn.execute("PRAGMA quick_check").fetchall()
            foreign_key_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
            tag_count_drift = []
            if all(
                _table_exists(conn, table)
                for table in ("tags", "knowledge_tags")
            ):
                tag_count_drift = conn.execute(
                    """
                    SELECT t.tag_id
                    FROM tags AS t
                    LEFT JOIN knowledge_tags AS kt ON kt.tag_id = t.tag_id
                    GROUP BY t.tag_id, t.count
                    HAVING t.count != COUNT(kt.knowledge_id)
                    """
                ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        return [f"SQLite 完整性不可读取: {exc}"]

    issues: list[str] = []
    quick_results = [str(row[0]) for row in quick_rows]
    if quick_results != ["ok"]:
        issues.append(f"SQLite quick_check 失败（{len(quick_results)} 项）")
    if foreign_key_rows:
        issues.append(f"SQLite 外键完整性失败（{len(foreign_key_rows)} 项）")
    if tag_count_drift:
        issues.append(f"标签计数与关联不一致（{len(tag_count_drift)} 项）")
    return issues


def _read_only_statistics(db_path: Path) -> dict:
    """返回与 SQLiteStore.get_statistics 兼容的只读统计结果。"""
    with closing(_connect_read_only(db_path)) as conn:
        if not _table_exists(conn, "knowledge_items"):
            return {}
        total_entries = int(conn.execute("SELECT COUNT(*) FROM knowledge_items").fetchone()[0])
        by_source_type = [
            (row["source_type"], int(row["cnt"]))
            for row in conn.execute(
                "SELECT source_type, COUNT(*) AS cnt "
                "FROM knowledge_items GROUP BY source_type"
            ).fetchall()
        ]
        top_tags = [
            {"name": row["name"], "count": int(row["count"])}
            for row in conn.execute(
                "SELECT name, count FROM tags ORDER BY count DESC LIMIT 20"
            ).fetchall()
        ]
    return {
        "total_entries": total_entries,
        "by_source_type": by_source_type,
        "top_tags": top_tags,
    }


def _validate_markdown_storage(
    root: Path,
    entry_records: list[dict],
) -> list[str]:
    """核对 SQLite 条目与 Markdown 路径、正文和核心元数据。"""
    issues: list[str] = []
    vault_dir = root / "vault"
    if not vault_dir.is_dir():
        return ["vault 目录缺失"]

    expected_paths: set[str] = set()
    for record in entry_records:
        knowledge_id = int(record["knowledge_id"])
        raw_path = record["file_path"]
        if not isinstance(raw_path, str) or not raw_path.strip():
            issues.append(f"条目 {knowledge_id} 缺少 Markdown file_path")
            continue
        candidate = Path(os.path.abspath(raw_path))
        if not _is_relative_to(candidate, vault_dir):
            issues.append(f"条目 {knowledge_id} 的 Markdown 路径越过 vault 边界")
            continue
        normalized = os.path.normcase(str(candidate))
        expected_paths.add(normalized)
        if not candidate.is_file():
            issues.append(f"条目 {knowledge_id} 的 Markdown 文件缺失")
        elif candidate.suffix.casefold() != ".md":
            issues.append(f"条目 {knowledge_id} 的主存储不是 Markdown 文件")
        else:
            try:
                post = frontmatter.load(candidate)
            except Exception as exc:
                issues.append(f"条目 {knowledge_id} 的 Markdown 无法解析: {exc}")
                continue

            expected_content = record["content"] or ""
            if not post.content.strip():
                issues.append(f"条目 {knowledge_id} 的 Markdown 正文为空")
            elif post.content != expected_content:
                issues.append(f"条目 {knowledge_id} 的 Markdown 正文与数据库不一致")

            def terms(value: object) -> list[str]:
                if value is None or value == "":
                    return []
                if isinstance(value, (list, tuple)):
                    return [str(item) for item in value]
                return [item for item in str(value).split(",") if item]

            expected_metadata = {
                "title": record["title"],
                "source_type": record["source_type"],
                "source_url": record["source_url"],
                "event_time": record["event_time"],
                "published_at": record["published_at"],
                "archived_at": record["archived_at"],
                "keywords": terms(record["keywords"]),
                "tags": terms(record["tags"]),
                "summary_one_sentence": record["summary_one_sentence"] or "",
                "summary_100_words": record["summary_100_words"] or "",
                "search_strategy": record["search_strategy"],
                "word_count": record["word_count"],
            }
            drifted: list[str] = []
            for key, expected in expected_metadata.items():
                actual = post.metadata.get(key)
                if key in {"event_time", "published_at", "archived_at"}:
                    actual = None if actual is None else str(actual)
                    expected = None if expected is None else str(expected)
                elif key in {"keywords", "tags"}:
                    actual = terms(actual)
                if actual != expected:
                    drifted.append(key)
            if drifted:
                issues.append(
                    f"条目 {knowledge_id} 的 Markdown 元数据与数据库不一致: "
                    + ", ".join(drifted)
                )

    try:
        actual_paths = {
            os.path.normcase(str(path.absolute()))
            for path in vault_dir.rglob("*.md")
            if path.is_file()
        }
    except OSError as exc:
        issues.append(f"无法完整枚举 Markdown 主存储: {exc}")
        return issues

    missing = expected_paths - actual_paths
    orphaned = actual_paths - expected_paths
    if missing:
        issues.append(f"Markdown 主存储缺失 {len(missing)} 个数据库引用文件")
    if orphaned:
        issues.append(f"Markdown 主存储存在 {len(orphaned)} 个未索引文件")
    return issues


def _validate_rebuilt_root(root: Path, db_path: Path) -> dict:
    """fail-closed 结构校验：仅本脚本完整生成的 root 才算有效。

    校验: manifest 严格字段、数据库核心对象、schema/pending、条目数、
    Markdown 主存储一一对应，以及所有标准目录。
    """
    issues: list[str] = []
    manifest = _load_manifest(root)
    manifest_issues: list[str] = []
    if manifest is None:
        manifest_issues.append(f"缺少或损坏的 {MANIFEST_NAME}")
    else:
        missing = [key for key in _MANIFEST_KEYS if key not in manifest]
        if missing:
            manifest_issues.append(f"manifest 字段不完整: {', '.join(missing)}")
        if (
            type(manifest.get("manifest_version")) is not int
            or manifest.get("manifest_version") != MANIFEST_VERSION
        ):
            manifest_issues.append(
                f"manifest 版本不支持: {manifest.get('manifest_version')!r}"
            )
        if not isinstance(manifest.get("tool"), str) or manifest.get("tool") != TOOL_NAME:
            manifest_issues.append(f"manifest 工具标识不匹配: {manifest.get('tool')!r}")
        if not isinstance(manifest.get("schema_version"), str) or not manifest.get(
            "schema_version"
        ):
            manifest_issues.append("manifest schema_version 字段必须是非空字符串")
        if not isinstance(manifest.get("seeded"), bool):
            manifest_issues.append("manifest seeded 字段必须是布尔值")
        if type(manifest.get("seed_count")) is not int or manifest.get("seed_count", -1) < 0:
            manifest_issues.append("manifest seed_count 字段必须是非负整数")
        if type(manifest.get("seed")) is not int:
            manifest_issues.append("manifest seed 字段必须是整数")
        if not isinstance(manifest.get("created_at"), str) or not manifest.get("created_at"):
            manifest_issues.append("manifest created_at 字段必须是非空字符串")
        if not manifest_issues:
            if manifest["seeded"] and manifest["seed_count"] <= 0:
                manifest_issues.append("seeded root 的 seed_count 必须大于 0")
            if not manifest["seeded"] and manifest["seed_count"] != 0:
                manifest_issues.append("no-seed root 的 seed_count 必须为 0")
    issues.extend(manifest_issues)

    schema_version = "0.0.0"
    if not db_path.exists():
        issues.append("数据库缺失: db/knowledge_vault.db")
    else:
        manager = MigrationManager(db_path, MIGRATIONS_DIR, read_only=True)
        try:
            schema_version = manager.get_current_version()
            pending = manager.get_pending_migrations()
        except Exception as exc:
            issues.append(f"数据库读取失败: {exc}")
            pending = []
        latest = _latest_migration_version(MIGRATIONS_DIR)
        if schema_version != latest:
            issues.append(f"数据库版本 {schema_version} 不是最新 {latest}")
        if pending:
            issues.append(f"存在 {len(pending)} 个待执行迁移，不能视为 up_to_date")

        schema_objects = _read_schema_objects(db_path)
        if schema_objects is None:
            issues.append("数据库 schema 对象不可读取")
        else:
            tables, triggers = schema_objects
            missing_tables = sorted(_REQUIRED_TABLES - tables)
            missing_triggers = sorted(_REQUIRED_TRIGGERS - triggers)
            if missing_tables:
                issues.append(f"数据库缺少核心表: {', '.join(missing_tables)}")
            if missing_triggers:
                issues.append(f"数据库缺少 FTS 触发器: {', '.join(missing_triggers)}")
        issues.extend(_validate_sqlite_integrity(db_path))
        issues.extend(_validate_fts_integrity(db_path))

        entry_records = _read_entry_records(db_path)
        if entry_records is None:
            issues.append("数据库缺少 knowledge_items 表或不可统计")
        elif manifest is not None and not manifest_issues:
            entry_count = len(entry_records)
            manifest_schema = manifest.get("schema_version")
            if manifest_schema != schema_version:
                issues.append(
                    f"manifest 记录版本 {manifest_schema} 与数据库 {schema_version} 不一致"
                )
            expected = manifest["seed_count"]
            if manifest.get("seeded"):
                if entry_count != expected:
                    issues.append(
                        f"条目数 {entry_count} 与 manifest 期望 {expected} 不一致"
                    )
            elif entry_count != 0:
                issues.append(f"no-seed root 不应有条目（当前 {entry_count}）")
            issues.extend(_validate_markdown_storage(root, entry_records))

    for dirname in _STANDARD_DIRS:
        if not (root / dirname).is_dir():
            issues.append(f"标准目录缺失: {dirname}")

    return {
        "ok": not issues,
        "issues": issues,
        "manifest": manifest,
        "schema_version": schema_version,
    }


def _build_seed_entry(
    index: int,
    *,
    rng: random.Random,
    base_time: datetime,
    used_titles: set,
) -> Entry:
    topic = _SEED_TOPICS[index % len(_SEED_TOPICS)]
    title = f"重建种子 {index + 1:03d} {topic}"
    if title in used_titles:
        title = f"{title}-{rng.randint(100, 999)}"
    used_titles.add(title)

    body = "\n\n".join(_SEED_BODIES)
    archived_at = (base_time + timedelta(minutes=index)).strftime("%Y-%m-%d %H:%M:%S")
    tags = list(dict.fromkeys([_SEED_TAGS[index % len(_SEED_TAGS)], "重建"]))
    keywords = ["rebuild", "seed", f"seed-{index + 1}"]

    return Entry(
        title=title,
        source_type="text",
        source_url=None,
        archived_at=archived_at,
        tags=tags,
        keywords=keywords,
        abstract=f"{topic} 确定性种子条目",
        summary_one_sentence=f"{topic} - 确定性最小种子条目",
        summary_100_words=body,
        search_strategy="keyword",
        word_count=len(body),
        content=f"# {title}\n\n{body}",
    )


def _seed_entries(
    root: Path,
    db_path: Path,
    *,
    seed: int,
    count: int,
) -> int:
    """生成确定性最小种子：Markdown 主存储 + SQLite 索引 + FTS5。"""
    if count <= 0:
        raise RebuildError("count 必须大于 0")
    store = SQLiteStore(db_path)
    md_store = MarkdownStore(vault_dir=root / "vault")
    rng = random.Random(seed)
    used_titles: set = set()
    base_time = datetime(2026, 2, 1, 9, 0, 0)

    file_paths: list[Path] = []
    entry_ids: list[int] = []
    for index in range(count):
        entry = _build_seed_entry(
            index, rng=rng, base_time=base_time, used_titles=used_titles
        )
        file_path = md_store.save(entry)
        knowledge_id = store.insert_entry(entry, str(file_path))
        file_paths.append(file_path)
        entry_ids.append(knowledge_id)

    if len(entry_ids) > 1:
        for index, file_path in enumerate(file_paths):
            candidates = [kid for kid in entry_ids if kid != entry_ids[index]]
            related_count = rng.randint(1, min(2, len(candidates)))
            related_ids = rng.sample(candidates, k=related_count)
            _update_related_docs(file_path, related_ids)

    return len(entry_ids)


def _update_related_docs(file_path: Path, related_ids: list[int]) -> None:
    post = frontmatter.load(file_path)
    post.metadata["related_docs"] = related_ids
    file_path.write_text(frontmatter.dumps(post), encoding="utf-8")


def _current_version_if_exists(db_path: Path) -> str:
    """读取数据库版本；文件不存在时避免创建连接（只读路径）。"""
    if not db_path.exists():
        return "0.0.0"
    return MigrationManager(db_path, MIGRATIONS_DIR, read_only=True).get_current_version()


def _health_report(root: Path, db_path: Path) -> dict:
    """只读健康检查：迁移链健康 + 数据库统计。"""
    manager = MigrationManager(db_path, MIGRATIONS_DIR, read_only=True)
    health = manager.run_health_check()
    stats: dict = {}
    if db_path.exists():
        stats = _read_only_statistics(db_path)
    return {
        "root": str(root),
        "db_path": str(db_path),
        "schema_version": _current_version_if_exists(db_path),
        "health": health,
        "stats": stats,
    }


def _check_only(root: Path, db_path: Path) -> dict:
    """仅健康检查（绝不写入）；对不存在或不完整的 root/db 必须失败。"""
    if not db_path.exists():
        return {
            "ok": False,
            "root": str(root),
            "db_path": str(db_path),
            "schema_version": "0.0.0",
            "error": "数据库不存在：--check-only 不创建任何目录或数据库",
            "issues": ["数据库缺失: db/knowledge_vault.db"],
        }
    validation = _validate_rebuilt_root(root, db_path)
    if not validation["ok"]:
        return {
            "ok": False,
            "root": str(root),
            "db_path": str(db_path),
            "schema_version": validation.get("schema_version", "0.0.0"),
            "error": "root 结构不完整或非本脚本生成，不能视为健康",
            "issues": validation["issues"],
        }
    manager = MigrationManager(db_path, MIGRATIONS_DIR, read_only=True)
    health = manager.run_health_check()
    stats = _read_only_statistics(db_path)
    return {
        "ok": bool(health.get("healthy")),
        "root": str(root),
        "db_path": str(db_path),
        "schema_version": manager.get_current_version(),
        "health": health,
        "stats": stats,
    }


def _print_summary(report: dict, *, quiet: bool) -> None:
    if quiet:
        return
    phase = report.get("phase", "checked")
    print("=" * 60)
    print(" PKV 开发重建结果")
    print("=" * 60)
    print(f"  根目录: {report.get('root', '-')}")
    print(f"  阶段: {phase}")
    if phase == "invalid":
        print(f"  ✗ {report.get('error', 'root 结构校验失败')}")
        for issue in report.get("issues", []):
            print(f"    - {issue}")
        print("  提示: 非空 root 未通过结构校验时不会写入或清理；")
        print("        如需重建请显式使用 --force")
        print("=" * 60)
        return
    health = report["health"]
    issues = health.get("issues", [])
    stats = report.get("stats", {})
    print(f"  数据库: {report.get('db_path', '-')}")
    print(f"  迁移版本: {report['schema_version']}")
    if report.get("migrations_applied") is not None:
        print(f"  本次应用迁移: {report['migrations_applied']}")
    if stats:
        print(f"  条目总数: {stats.get('total_entries', 0)}")
    if phase == "up_to_date":
        print("  ✓ 根目录已是最新且健康，未做任何写入")
    if health.get("healthy"):
        print("  ✓ 迁移链健康检查通过")
    else:
        print("  ✗ 迁移链健康检查发现问题:")
        for issue in issues:
            print(f"    - {issue}")
    print("=" * 60)


def _emit_json(payload: dict, exit_code: int) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(exit_code)


def _prepare_json_mode() -> None:
    """--json 模式下保持 stdout/stderr 契约干净（仅输出 JSON）。"""
    logging.getLogger("jieba").disabled = True


def _validate_arguments(args: argparse.Namespace) -> None:
    """在任何文件系统访问之前拒绝无效参数。"""
    if not args.no_seed and args.count <= 0:
        raise RebuildError("count 必须大于 0（使用 --no-seed 可跳过种子）")


def _requested_configuration_issues(
    args: argparse.Namespace,
    manifest: dict | None,
) -> list[str]:
    """避免把与调用参数不一致的既有 root 静默报告为 up_to_date。"""
    if manifest is None:
        return []
    issues: list[str] = []
    requested_seeded = not args.no_seed
    if manifest.get("seeded") != requested_seeded:
        issues.append("现有 root 的 seeded 配置与本次调用不一致；请使用匹配参数或 --force")
    if requested_seeded:
        if manifest.get("seed_count") != args.count:
            issues.append("现有 root 的 seed_count 与 --count 不一致；如需变更请使用 --force")
        if manifest.get("seed") != args.seed:
            issues.append("现有 root 的 seed 与 --seed 不一致；如需变更请使用 --force")
    return issues


def _assert_root_stable_and_safe(
    root: Path,
    expected_identity: tuple[int, int, int, int],
) -> None:
    """在每个实际使用点前复核根身份并重新扫描内部链接/挂载点。"""
    _assert_root_identity(root, expected_identity)
    unsafe = _find_unsafe_link_under(root)
    if unsafe is not None:
        raise RootRejectedError(f"重建根内部出现链接或挂载点，拒绝继续: {unsafe}")
    _assert_root_identity(root, expected_identity)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="PKV 开发专用轻量重建（安全隔离根，禁止生产 .data）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--root",
        default=DEFAULT_ROOT_REL,
        help=f"重建根目录（默认: {DEFAULT_ROOT_REL}，必须位于 .data-test 下）",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"种子随机种子（默认: {DEFAULT_SEED}）",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=DEFAULT_COUNT,
        help=f"种子条目数量（默认: {DEFAULT_COUNT}）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="受控清理现有根目录后完整重建（清理前校验链接）",
    )
    parser.add_argument(
        "--no-seed",
        action="store_true",
        help="跳过种子生成（仅迁移 + 健康检查）",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="仅健康检查，绝不写入",
    )
    parser.add_argument(
        "--json", action="store_true", help="输出机器可读 JSON 结果契约"
    )
    parser.add_argument("--quiet", action="store_true", help="抑制人类可读输出")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.json:
        _prepare_json_mode()

    try:
        _validate_arguments(args)
        root = resolve_rebuild_root(args.root)
        db_path = root / "db" / "knowledge_vault.db"
        root_identity: tuple[int, int, int, int] | None = None

        # 链接安全门：已存在 root 在任何内容读取（iterdir/manifest/DB）之前
        # 做只读递归内部链接扫描，发现 symlink/junction/hardlink 立即拒绝。
        if root.exists():
            unsafe = _find_unsafe_link_under(root)
            if unsafe is not None:
                raise RootRejectedError(
                    "重建根内部存在链接或挂载点"
                    f"（symlink/junction/hardlink/mount），拒绝访问: {unsafe}"
                )
            root_identity = _capture_root_identity(root)

        if args.check_only:
            if root_identity is not None:
                _assert_root_stable_and_safe(root, root_identity)
            report = _check_only(root, db_path)
            report["phase"] = "checked" if report["ok"] else "invalid"
            if args.json:
                _emit_json(report, 0 if report["ok"] else 1)
            _print_summary(report, quiet=args.quiet)
            return 0 if report["ok"] else 1

        root_has_content = _has_meaningful_content(root)
        if root_identity is not None:
            _assert_root_identity(root, root_identity)

        if root_has_content and not args.force:
            # 幂等路径：只读校验本脚本完整生成的 root；不完整/未知则 fail-closed。
            assert root_identity is not None
            _assert_root_stable_and_safe(root, root_identity)
            validation = _validate_rebuilt_root(root, db_path)
            validation["issues"].extend(
                _requested_configuration_issues(args, validation.get("manifest"))
            )
            validation["ok"] = not validation["issues"]
            if not validation["ok"]:
                report = {
                    "ok": False,
                    "phase": "invalid",
                    "root": str(root),
                    "db_path": str(db_path),
                    "schema_version": validation.get("schema_version", "0.0.0"),
                    "error": "root 非空但未通过结构校验（无写入、无清理）",
                    "issues": validation["issues"],
                }
                if args.json:
                    _emit_json(report, 1)
                _print_summary(report, quiet=args.quiet)
                return 1
            report = _health_report(root, db_path)
            if report["health"].get("healthy"):
                report["phase"] = "up_to_date"
                report["ok"] = True
            else:
                report["phase"] = "invalid"
                report["ok"] = False
                report["error"] = "迁移链健康检查不通过"
                report["issues"] = report["health"].get("issues", [])
            report["stats"] = report.get("stats") or {}
            if args.json:
                _emit_json(report, 0 if report["ok"] else 1)
            _print_summary(report, quiet=args.quiet)
            return 0 if report["ok"] else 1

        if args.force and root_has_content:
            assert root_identity is not None
            _cleanup_root(root, expected_identity=root_identity)
        else:
            root.mkdir(parents=True, exist_ok=True)

        root_identity = _capture_root_identity(root)
        _assert_root_stable_and_safe(root, root_identity)
        _ensure_standard_dirs(root)
        _assert_root_stable_and_safe(root, root_identity)
        applied = _run_migrations(db_path)
        _assert_root_stable_and_safe(root, root_identity)
        seeded = 0
        if not args.no_seed:
            seeded = _seed_entries(root, db_path, seed=args.seed, count=args.count)

        _assert_root_stable_and_safe(root, root_identity)
        report = _health_report(root, db_path)
        report["phase"] = "rebuilt"
        report["migrations_applied"] = applied
        report["seeded"] = seeded
        report["ok"] = bool(report["health"].get("healthy"))
        if report["ok"]:
            # manifest 最后写入：标记完整成功生成；健康失败则不写（fail-closed）。
            _assert_root_stable_and_safe(root, root_identity)
            _write_manifest(
                root,
                schema_version=report["schema_version"],
                seeded=not args.no_seed,
                seed_count=seeded,
                seed=args.seed,
            )
            _assert_root_stable_and_safe(root, root_identity)
            validation = _validate_rebuilt_root(root, db_path)
            if not validation["ok"]:
                (root / MANIFEST_NAME).unlink(missing_ok=True)
                report["ok"] = False
                report["phase"] = "invalid"
                report["error"] = "重建后完整性校验失败，未保留完成标记"
                report["issues"] = validation["issues"]
        if args.json:
            _emit_json(report, 0 if report["ok"] else 1)
        _print_summary(report, quiet=args.quiet)
        return 0 if report["ok"] else 1

    except RootRejectedError as exc:
        if args.json:
            _emit_json({"ok": False, "error": str(exc)}, 2)
        print(f"[error] {exc}", file=sys.stderr)
        return 2
    except RebuildError as exc:
        if args.json:
            _emit_json({"ok": False, "error": str(exc)}, 1)
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - 防御性兜底
        if args.json:
            _emit_json({"ok": False, "error": f"未预期错误: {exc}"}, 1)
        print(f"[error] 未预期错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
