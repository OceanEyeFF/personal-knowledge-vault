"""
数据库迁移管理器

负责管理数据库 Schema 的版本升级和回滚
"""

import sqlite3
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any
import logging
import subprocess
import re
from contextlib import contextmanager

from src.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


EXPECTED_TABLES_BY_VERSION = {
    "1.1.1": ("chat_sessions",),
    "1.1.2": ("review_queue", "review_history"),
    "1.2.0": ("knowledge_relations",),
}
FTS_REBUILD_VERSIONS = {"1.2.2", "1.2.3"}


class MigrationManager:
    """数据库迁移管理器"""

    def __init__(
        self,
        db_path: Path,
        migrations_dir: Path,
        *,
        read_only: bool = False,
    ):
        """
        初始化迁移管理器

        Args:
            db_path: 数据库文件路径
            migrations_dir: 迁移脚本目录路径
            read_only: 仅允许检查数据库和迁移链，不创建目录或执行写入操作
        """
        self.db_path = Path(db_path)
        self.migrations_dir = Path(migrations_dir)
        self.read_only = read_only

        # 写模式保留原有的初始化行为；只读模式不得改变文件系统。
        if not self.migrations_dir.exists():
            logger.warning(f"迁移目录不存在: {self.migrations_dir}")
            if not self.read_only:
                self.migrations_dir.mkdir(parents=True, exist_ok=True)

    def _require_writable(self, operation: str) -> None:
        """Reject mutating operations when this manager is read-only."""
        if self.read_only:
            raise RuntimeError(
                f"MigrationManager is read-only; cannot perform {operation}"
            )

    def _connect(self) -> sqlite3.Connection:
        """Open the database, optionally enforcing SQLite read-only mode."""
        if self.read_only:
            uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            conn.execute("PRAGMA query_only = ON")
            return conn
        return sqlite3.connect(self.db_path)

    @contextmanager
    def _connection(self):
        """Transaction-aware connection context that always closes the handle."""
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def get_current_version(self) -> str:
        """
        获取当前数据库版本

        Returns:
            版本号字符串（如 "1.0.0"），如果数据库未初始化返回 "0.0.0"
        """
        if self.read_only and not self.db_path.is_file():
            logger.info("数据库不存在，只读检查返回版本: 0.0.0")
            return "0.0.0"

        conn = self._connect()
        try:
            cursor = conn.cursor()

            # 检查 schema_version 表是否存在
            cursor.execute("""
                SELECT name FROM sqlite_master
                WHERE type='table' AND name='schema_version'
            """)

            if not cursor.fetchone():
                # 表不存在，说明是新数据库或旧版本
                logger.info("schema_version 表不存在，数据库版本: 0.0.0")
                return "0.0.0"

            # 获取最新版本
            cursor.execute("""
                SELECT version FROM schema_version
                ORDER BY version_id DESC LIMIT 1
            """)

            row = cursor.fetchone()
            version = row[0] if row else "0.0.0"
            logger.info(f"当前数据库版本: {version}")
            return version

        except Exception as e:
            logger.error(f"获取数据库版本失败: {e}")
            return "0.0.0"

        finally:
            conn.close()

    def get_pending_migrations(self) -> List[Tuple[str, Path]]:
        """
        获取待执行的迁移脚本

        Returns:
            (版本号, 脚本路径) 的列表，按版本号升序排列
        """
        current_version = self.get_current_version()
        migrations = []

        # 扫描迁移脚本目录
        for migration_file in sorted(self.migrations_dir.glob("*.sql")):
            # 解析版本号
            version = self._parse_version_from_file(migration_file)

            # 比较版本号
            if self._version_compare(version, current_version) > 0:
                migrations.append((version, migration_file))
                logger.debug(f"待迁移: {migration_file.name} (v{version})")

        # 按版本号排序
        migrations.sort(key=lambda x: self._version_to_tuple(x[0]))

        logger.info(f"找到 {len(migrations)} 个待执行的迁移脚本")
        return migrations

    def run_health_check(self) -> Dict[str, Any]:
        """
        执行迁移链健康检查（只读）。

        Returns:
            包含脚本链检查、数据库状态和问题列表的结果字典
        """
        migration_files = sorted(self.migrations_dir.glob("*.sql"))
        script_checks: List[Dict[str, Any]] = []
        issues: List[str] = []
        seen_versions: Dict[str, str] = {}
        previous_version: Optional[str] = None

        for migration_file in migration_files:
            metadata = self._read_migration_metadata(migration_file)
            version = metadata.get("version")
            description = metadata.get("description")
            header_ok = all(metadata.values())
            check: Dict[str, Any] = {
                "file": migration_file.name,
                "version": version,
                "description": description,
                "has_standard_headers": header_ok,
            }

            if not header_ok:
                missing = [
                    key for key, value in metadata.items() if not value
                ]
                issues.append(
                    f"{migration_file.name} 缺少标准头字段: {', '.join(missing)}"
                )

            if version:
                if not re.match(r"^\d+\.\d+\.\d+$", version):
                    issues.append(f"{migration_file.name} 的版本号格式无效: {version}")
                elif version in seen_versions:
                    issues.append(
                        f"{migration_file.name} 与 {seen_versions[version]} 使用了重复版本号 {version}"
                    )
                else:
                    seen_versions[version] = migration_file.name

                if (
                    previous_version
                    and re.match(r"^\d+\.\d+\.\d+$", previous_version)
                    and re.match(r"^\d+\.\d+\.\d+$", version)
                    and self._version_compare(version, previous_version) <= 0
                ):
                    issues.append(
                        f"{migration_file.name} 的版本号 {version} 未严格高于前一个脚本版本 {previous_version}"
                    )

                previous_version = version

            script_checks.append(check)

        db_info: Dict[str, Any] = {
            "db_path": str(self.db_path),
            "db_exists": self.db_path.exists(),
            "schema_version_exists": False,
            "current_version": "0.0.0",
            "applied_versions": [],
            "pending_migrations": [],
            "table_drift": [],
        }

        if db_info["db_exists"]:
            with self._connection() as conn:
                cursor = conn.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type='table' AND name='schema_version'
                    """
                )
                schema_version_exists = cursor.fetchone() is not None
                db_info["schema_version_exists"] = schema_version_exists

                if schema_version_exists:
                    applied_rows = conn.execute(
                        """
                        SELECT version
                        FROM schema_version
                        ORDER BY version_id ASC
                        """
                    ).fetchall()
                    applied_versions = [row[0] for row in applied_rows]
                    db_info["applied_versions"] = applied_versions
                    db_info["current_version"] = (
                        applied_versions[-1] if applied_versions else "0.0.0"
                    )

                    known_versions = set(seen_versions.keys())
                    if (
                        db_info["current_version"] != "0.0.0"
                        and db_info["current_version"] not in known_versions
                    ):
                        issues.append(
                            f"数据库当前版本 {db_info['current_version']} 不在当前迁移链定义中"
                        )

                table_names = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }

                applied_versions = set(db_info["applied_versions"])
                table_drift: List[str] = []
                for version, expected_tables in EXPECTED_TABLES_BY_VERSION.items():
                    has_tables = all(table in table_names for table in expected_tables)
                    if has_tables and version not in applied_versions:
                        table_drift.append(
                            f"数据库已存在 {', '.join(expected_tables)}，但 schema_version 中缺少 {version}"
                        )
                    if version in applied_versions and not has_tables:
                        table_drift.append(
                            f"schema_version 已记录 {version}，但数据库缺少表 {', '.join(expected_tables)}"
                        )

                db_info["table_drift"] = table_drift
                issues.extend(table_drift)

            pending = self.get_pending_migrations()
            db_info["pending_migrations"] = [
                {"version": version, "file": path.name}
                for version, path in pending
            ]

        return {
            "healthy": len(issues) == 0,
            "scripts": script_checks,
            "database": db_info,
            "issues": issues,
        }

    def apply_migration(self, migration_file: Path, auto_backup: bool = True):
        """
        执行迁移脚本

        Args:
            migration_file: 迁移脚本文件路径
            auto_backup: 是否自动备份数据库

        Raises:
            Exception: 迁移执行失败
        """
        self._require_writable("apply_migration")
        logger.info(f"开始迁移: {migration_file.name}")
        metadata = self._read_migration_metadata(migration_file)
        version = metadata.get("version")
        description = metadata.get("description") or migration_file.name

        if version == "1.2.1" and self._knowledge_items_has_timeline_columns():
            conn = self._connect()
            try:
                self._ensure_timeline_indexes(conn)
                self._record_schema_version(conn, version, description)
                conn.commit()
            finally:
                conn.close()
            logger.info("✓ 跳过迁移: %s（目标列已存在）", migration_file.name)
            return

        # 自动备份（可选）
        if auto_backup:
            try:
                self._backup_database(migration_file.name)
            except Exception as e:
                logger.warning(f"自动备份失败: {e}")
                # 继续执行迁移（备份失败不应阻止迁移）

        # 执行 SQL 脚本
        conn = self._connect()
        try:
            with open(migration_file, 'r', encoding='utf-8') as f:
                sql = f.read()

            # 执行迁移
            conn.executescript(sql)
            conn.commit()

            logger.info(f"✓ 迁移成功: {migration_file.name}")

        except Exception as e:
            conn.rollback()
            logger.error(f"✗ 迁移失败: {migration_file.name} - {e}")
            raise

        finally:
            conn.close()

    def apply_all_pending(self, auto_backup: bool = True) -> int:
        """
        执行所有待迁移脚本

        Args:
            auto_backup: 是否自动备份

        Returns:
            成功执行的迁移脚本数量

        Raises:
            Exception: 迁移执行失败
        """
        self._require_writable("apply_all_pending")
        pending = self.get_pending_migrations()

        if not pending:
            logger.info("没有待执行的迁移")
            return 0

        success_count = 0
        fts_alignment_versions = [
            version for version, _ in pending if version in FTS_REBUILD_VERSIONS
        ]

        for version, migration_file in pending:
            try:
                self.apply_migration(migration_file, auto_backup=auto_backup)
                success_count += 1
            except Exception as e:
                logger.error(f"迁移中断: {e}")
                raise

        if fts_alignment_versions:
            try:
                SQLiteStore(self.db_path).rebuild_fts5_index()
            except Exception:
                self._remove_applied_versions(fts_alignment_versions)
                logger.error("FTS 重建失败，已回滚 FTS 对齐版本标记以便下次重试", exc_info=True)
                raise

        logger.info(f"迁移完成: 成功执行 {success_count} 个脚本")
        return success_count

    def _remove_applied_versions(self, versions: List[str]) -> None:
        """移除已记录的迁移版本，用于后置校验失败后的可重试回滚。"""
        self._require_writable("_remove_applied_versions")
        if not versions:
            return

        placeholders = ", ".join("?" for _ in versions)
        with self._connection() as conn:
            conn.execute(
                f"DELETE FROM schema_version WHERE version IN ({placeholders})",
                tuple(versions),
            )

    def check_and_prompt_upgrade(self) -> bool:
        """
        检查并提示升级

        Returns:
            True: 有待升级的迁移，False: 已是最新版本
        """
        pending = self.get_pending_migrations()

        if not pending:
            logger.info("数据库 Schema 已是最新版本")
            return False

        print("")
        print("=" * 60)
        print(f" 检测到 {len(pending)} 个待升级的 Schema 变更")
        print("=" * 60)
        print("")

        for version, migration_file in pending:
            # 读取迁移描述
            description = self._get_migration_description(migration_file)
            print(f"  • {migration_file.name}")
            print(f"    版本: v{version}")
            if description:
                print(f"    说明: {description}")
            print("")

        print("建议:")
        print("  1. 备份数据: .\\scripts\\backup-data.ps1")
        print("  2. 执行升级: python scripts/migrate.py")
        print("  3. 验证数据: python -m src.main stats")
        print("")

        return True

    def _backup_database(self, migration_name: str):
        """
        备份数据库（调用已有的备份脚本）

        Args:
            migration_name: 迁移脚本名称（用于备份说明）
        """
        self._require_writable("_backup_database")
        message = f"自动备份 - Schema 迁移前 ({migration_name})"

        logger.info(f"自动备份数据库: {message}")

        # 调用备份脚本
        try:
            result = subprocess.run(
                [
                    "powershell",
                    "-File", "scripts/backup-data.ps1",
                    "-Message", message
                ],
                check=True,
                capture_output=True,
                text=True
            )
            logger.debug(result.stdout)

        except subprocess.CalledProcessError as e:
            logger.error(f"备份脚本执行失败: {e.stderr}")
            raise

    def _parse_version_from_file(self, file_path: Path) -> str:
        """
        从迁移文件解析版本号

        Args:
            file_path: 迁移脚本文件路径

        Returns:
            版本号字符串（如 "1.0.0"）
        """
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                # 匹配 "-- Version: 1.0.0" 格式
                if line.strip().startswith("-- Version:"):
                    version = line.split(":")[-1].strip()
                    return version

        # 如果没有找到版本号，使用文件名序号生成版本号
        # 例如: 001_xxx.sql -> "0.0.1"
        match = re.match(r"(\d+)_", file_path.name)
        if match:
            seq = int(match.group(1))
            return f"0.0.{seq}"

        logger.warning(f"无法解析版本号: {file_path.name}")
        return "0.0.0"

    def _get_migration_description(self, file_path: Path) -> Optional[str]:
        """
        从迁移文件读取描述信息

        Args:
            file_path: 迁移脚本文件路径

        Returns:
            描述字符串，如果未找到返回 None
        """
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip().startswith("-- Description:"):
                    return line.split(":")[-1].strip()

        return None

    def _read_migration_metadata(self, file_path: Path) -> Dict[str, Optional[str]]:
        """
        读取迁移脚本标准头部。

        Args:
            file_path: 迁移脚本路径

        Returns:
            包含 migration/version/description 的字典
        """
        metadata: Dict[str, Optional[str]] = {
            "migration": None,
            "version": None,
            "description": None,
        }

        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("-- Migration:"):
                    metadata["migration"] = stripped.split(":", 1)[-1].strip()
                elif stripped.startswith("-- Version:"):
                    metadata["version"] = stripped.split(":", 1)[-1].strip()
                elif stripped.startswith("-- Description:"):
                    metadata["description"] = stripped.split(":", 1)[-1].strip()

                if all(metadata.values()):
                    break

        return metadata

    def _knowledge_items_has_timeline_columns(self) -> bool:
        """检查 knowledge_items 是否已具备 timeline 真实时间列。"""
        if not self.db_path.exists():
            return False

        with self._connection() as conn:
            cursor = conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type='table' AND name='knowledge_items'
                """
            )
            if cursor.fetchone() is None:
                return False

            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(knowledge_items)")
            }
        return {"event_time", "published_at"}.issubset(columns)

    def _ensure_timeline_indexes(self, conn: sqlite3.Connection) -> None:
        """补齐 timeline 时间字段索引。"""
        self._require_writable("_ensure_timeline_indexes")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_knowledge_event_time ON knowledge_items(event_time)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_knowledge_published_at ON knowledge_items(published_at)"
        )

    def _record_schema_version(
        self,
        conn: sqlite3.Connection,
        version: Optional[str],
        description: str,
    ) -> None:
        """写入 schema_version 记录。"""
        self._require_writable("_record_schema_version")
        if not version:
            return
        conn.execute(
            """
            INSERT OR IGNORE INTO schema_version (version, description)
            VALUES (?, ?)
            """,
            (version, description),
        )

    def _version_compare(self, v1: str, v2: str) -> int:
        """
        比较版本号

        Args:
            v1: 版本号 1
            v2: 版本号 2

        Returns:
            1: v1 > v2, 0: v1 == v2, -1: v1 < v2
        """
        parts1 = self._version_to_tuple(v1)
        parts2 = self._version_to_tuple(v2)

        if parts1 > parts2:
            return 1
        elif parts1 < parts2:
            return -1
        else:
            return 0

    def _version_to_tuple(self, version: str) -> tuple:
        """
        将版本号字符串转换为元组（用于比较）

        Args:
            version: 版本号字符串（如 "1.0.0"）

        Returns:
            版本号元组（如 (1, 0, 0)）
        """
        try:
            return tuple(int(x) for x in version.split("."))
        except ValueError:
            logger.warning(f"无效的版本号格式: {version}")
            return (0, 0, 0)
