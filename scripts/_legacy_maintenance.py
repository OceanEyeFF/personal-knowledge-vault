"""Fail-closed boundary for retired raw maintenance entrypoints.

The helpers retained beside these entrypoints are exercised only by isolated
fixture tests.  They are not a second product lifecycle and must not become an
alternate way to mutate a configured user data root.
"""

from __future__ import annotations

import sys


LEGACY_MAINTENANCE_EXIT_CODE = 2


def reject_legacy_maintenance_entrypoint(script_name: str) -> int:
    """Refuse a retired script before it can load Config or open a data root."""

    print(
        f"{script_name} 已停用，不能作为当前 PKV 维护入口。"
        "本次调用未读取配置、未打开数据根、未执行迁移或网络请求。"
        "请使用当前 inspect → plan → confirm → execute 生命周期。",
        file=sys.stderr,
    )
    return LEGACY_MAINTENANCE_EXIT_CODE
