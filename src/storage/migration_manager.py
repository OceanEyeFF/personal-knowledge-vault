"""
数据库迁移管理器

负责管理数据库 Schema 的版本升级和回滚
"""

import sqlite3
from pathlib import Path
from typing import List, Tuple, Optional
import logging
import subprocess
import datetime
import re

logger = logging.getLogger(__name__)


class MigrationManager:
    """数据库迁移管理器"""

    def __init__(self, db_path: Path, migrations_dir: Path):
        """
        初始化迁移管理器

        Args:
            db_path: 数据库文件路径
            migrations_dir: 迁移脚本目录路径
        """
        self.db_path = Path(db_path)
        self.migrations_dir = Path(migrations_dir)

        # 确保迁移目录存在
        if not self.migrations_dir.exists():
            logger.warning(f"迁移目录不存在: {self.migrations_dir}")
            self.migrations_dir.mkdir(parents=True, exist_ok=True)

    def get_current_version(self) -> str:
        """
        获取当前数据库版本

        Returns:
            版本号字符串（如 "1.0.0"），如果数据库未初始化返回 "0.0.0"
        """
        conn = sqlite3.connect(self.db_path)
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

    def apply_migration(self, migration_file: Path, auto_backup: bool = True):
        """
        执行迁移脚本

        Args:
            migration_file: 迁移脚本文件路径
            auto_backup: 是否自动备份数据库

        Raises:
            Exception: 迁移执行失败
        """
        logger.info(f"开始迁移: {migration_file.name}")

        # 自动备份（可选）
        if auto_backup:
            try:
                self._backup_database(migration_file.name)
            except Exception as e:
                logger.warning(f"自动备份失败: {e}")
                # 继续执行迁移（备份失败不应阻止迁移）

        # 执行 SQL 脚本
        conn = sqlite3.connect(self.db_path)
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
        pending = self.get_pending_migrations()

        if not pending:
            logger.info("没有待执行的迁移")
            return 0

        success_count = 0

        for version, migration_file in pending:
            try:
                self.apply_migration(migration_file, auto_backup=auto_backup)
                success_count += 1
            except Exception as e:
                logger.error(f"迁移中断: {e}")
                raise

        logger.info(f"迁移完成: 成功执行 {success_count} 个脚本")
        return success_count

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
        timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
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
