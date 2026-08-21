"""
已停用的关系层回填脚本。

原始脚本入口不能绕过当前 lifecycle / writer lease；旧的实现仅保留为
历史代码与隔离 fixture 参考，不能再操作配置的数据根。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from scripts._legacy_maintenance import reject_legacy_maintenance_entrypoint


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="已停用的 PKV 关系回填入口（所有执行均会被拒绝）"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="历史参数；入口已停用，不会执行写入",
    )
    parser.add_argument(
        "--knowledge-id",
        action="append",
        type=int,
        dest="knowledge_ids",
        help="历史参数；入口已停用",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="历史参数；入口已停用",
    )
    parser.add_argument(
        "--vault-dir",
        type=str,
        default=None,
        help="历史参数；入口已停用",
    )
    parser.add_argument(
        "--report-json",
        type=str,
        default=None,
        help="历史参数；入口已停用",
    )
    parser.add_argument(
        "--report-yaml",
        type=str,
        default=None,
        help="历史参数；入口已停用",
    )
    parser.add_argument(
        "--report-md",
        type=str,
        default=None,
        help="历史参数；入口已停用",
    )
    parser.add_argument(
        "--min-coverage",
        type=float,
        default=None,
        help="历史参数；入口已停用",
    )
    parser.add_argument(
        "--max-noise",
        type=float,
        default=None,
        help="历史参数；入口已停用",
    )
    parser.add_argument(
        "--max-conflict",
        type=float,
        default=None,
        help="历史参数；入口已停用",
    )
    parser.add_argument(
        "--fail-on-gate",
        action="store_true",
        help="历史参数；入口已停用",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    # Keep argparse help available, but do not import a historical service or
    # instantiate Config on a configured data root.
    _parse_args(argv)
    return reject_legacy_maintenance_entrypoint("scripts/backfill_relations.py")


if __name__ == "__main__":
    raise SystemExit(main())
