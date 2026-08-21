"""
已停用的数据库 Schema 迁移入口。

真实旧库/生产迁移仍受 user-only gate 阻塞。这个原始脚本不能作为当前
产品 lifecycle 的旁路：除 ``--help`` 外，它在读取 Config 或数据库前
都将 fail-closed。
"""

import sys
import argparse
from pathlib import Path
from typing import Optional, Sequence

# Add the project root only to import the standard-library-only rejection
# helper when this file is invoked as ``python scripts/migrate.py``.
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from scripts._legacy_maintenance import reject_legacy_maintenance_entrypoint


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="已停用的 PKV 数据库 Schema 迁移入口（所有执行均会被拒绝）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="所有非 --help 调用都会在读取 Config 或数据库前返回 exit 2。",
    )
    parser.add_argument("--auto", action="store_true", help="历史参数；入口已停用")
    parser.add_argument("--dry-run", action="store_true", help="历史参数；入口已停用")
    parser.add_argument("--version", action="store_true", help="历史参数；入口已停用")
    parser.add_argument("--health-check", action="store_true", help="历史参数；入口已停用")
    parser.add_argument("--no-backup", action="store_true", help="历史参数；入口已停用")

    parser.parse_args(argv)

    # ``run-test.ps1`` already blocks this script.  Keep the same protection
    # for a bare invocation, before Config() could select a real data root.
    return reject_legacy_maintenance_entrypoint("scripts/migrate.py")

if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
