"""
数据库 Schema 迁移工具

用法:
    python scripts/migrate.py              # 交互式升级
    python scripts/migrate.py --auto       # 自动升级
    python scripts/migrate.py --dry-run    # 仅检查，不执行
    python scripts/migrate.py --version    # 查看当前版本
"""

import sys
import argparse
from pathlib import Path

# 添加项目根目录到 sys.path（确保能导入 src 模块）
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.storage.migration_manager import MigrationManager
from src.utils.config import Config


def main():
    parser = argparse.ArgumentParser(
        description="PKV 数据库 Schema 迁移工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/migrate.py                # 交互式升级
  python scripts/migrate.py --auto         # 自动升级
  python scripts/migrate.py --dry-run      # 仅检查
  python scripts/migrate.py --version      # 查看版本
        """
    )
    parser.add_argument("--auto", action="store_true", help="自动执行所有迁移（无需确认）")
    parser.add_argument("--dry-run", action="store_true", help="仅检查待迁移脚本，不执行")
    parser.add_argument("--version", action="store_true", help="显示当前数据库版本")
    parser.add_argument("--no-backup", action="store_true", help="跳过自动备份（不推荐）")

    args = parser.parse_args()

    # 初始化配置
    try:
        config = Config()
    except Exception as e:
        print(f"错误: 无法加载配置 - {e}")
        return 1

    db_path = config.db_path
    migrations_dir = project_root / "scripts" / "migrations"

    print("=" * 70)
    print(" PKV 数据库迁移工具")
    print("=" * 70)
    print("")
    print(f"数据库路径: {db_path}")
    print(f"迁移脚本目录: {migrations_dir}")
    print("")

    # 创建迁移管理器
    manager = MigrationManager(db_path, migrations_dir)

    # 如果仅查看版本
    if args.version:
        current_version = manager.get_current_version()
        print(f"当前数据库版本: {current_version}")
        print("")
        return 0

    # 获取当前版本
    current_version = manager.get_current_version()
    print(f"当前版本: {current_version}")
    print("")

    # 获取待执行的迁移
    pending = manager.get_pending_migrations()

    if not pending:
        print("✓ 数据库已是最新版本，无需迁移")
        print("")
        return 0

    print(f"待执行的迁移: {len(pending)}")
    print("")

    for version, migration_file in pending:
        description = manager._get_migration_description(migration_file)
        print(f"  • {migration_file.name}")
        print(f"    版本: v{version}")
        if description:
            print(f"    说明: {description}")
        print("")

    # Dry-run 模式
    if args.dry_run:
        print("Dry-run 模式，已退出（未执行迁移）")
        print("")
        return 0

    # 确认执行
    if not args.auto:
        print("=" * 70)
        print(" ⚠️  警告：数据库迁移操作")
        print("=" * 70)
        print("")
        print("  即将执行数据库 Schema 变更！")
        if not args.no_backup:
            print("  每个迁移前会自动备份到 .data-backup/")
        print("")

        confirm = input("是否继续执行迁移？(输入 YES 继续，其他任意键取消): ")

        if confirm != "YES":
            print("")
            print("已取消迁移")
            print("")
            return 0

    # 执行迁移
    print("")
    print("=" * 70)
    print(" 开始迁移")
    print("=" * 70)
    print("")

    try:
        success_count = manager.apply_all_pending(auto_backup=not args.no_backup)

        print("")
        print("=" * 70)
        print(" 迁移完成 ✓")
        print("=" * 70)
        print("")
        print(f"成功执行 {success_count} 个迁移脚本")
        print("")
        print("建议: 运行以下命令验证数据完整性")
        print("  python -m src.main stats")
        print("  python -m src.main list --limit 10")
        print("")

        return 0

    except Exception as e:
        print("")
        print("=" * 70)
        print(" 迁移失败 ✗")
        print("=" * 70)
        print("")
        print(f"错误: {e}")
        print("")

        if not args.no_backup:
            print("建议: 从备份恢复数据")
            print("  .\\scripts\\restore-data.ps1")
            print("")

        return 1


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
