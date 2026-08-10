# -*- coding: utf-8 -*-
"""W2 Workflow 手动检查入口（只读、离线）。

旧脚本会直接请求真实 URL、读取本机 Provider 配置并写入默认数据根，已经不符合
M13 W2 的离线验收边界。真实的 Workflow oracle 现由以下自动化合同负责：

- ``tests/integration/test_workflow_yaml_contract.py``
- ``tests/integration/test_workflow_integration.py``
- ``tests/fixtures/w2/workflow/v1/states.v1.yaml``

运行它们时必须经过 ``scripts/run-test.ps1``，并指定唯一的 ``.data-test`` 根。
本文件只方便人工查看发布 YAML 的顺序和错误策略，不执行任何 step。
"""

from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.config import Config  # noqa: E402
from src.runtime.layout import RuntimeLayout  # noqa: E402
from src.workflow.config_schema import validate_workflow_config  # noqa: E402
from src.workflow.engine import WorkflowEngine  # noqa: E402


def inspect_published_workflows() -> None:
    """加载并校验两份发布 YAML，不产生网络、Provider 或存储副作用。"""

    layout = RuntimeLayout.resolve(
        resources_root=PROJECT_ROOT,
    )
    config = Config(layout=layout)
    engine = WorkflowEngine()
    for workflow_name in ("archive-url", "archive-text"):
        raw = config.get_workflow_config(workflow_name)
        steps = validate_workflow_config(
            workflow_name,
            raw,
            engine._step_registry,
        )
        print(f"\n{workflow_name} (schema_version={raw['schema_version']})")
        for index, step in enumerate(steps, start=1):
            print(
                f"  {index}. {step['id']} "
                f"[{step['type']}, on_error={step['on_error']}]"
            )

    try:
        config.get_workflow_config("search")
    except FileNotFoundError:
        print("\n[OK] search.yaml 未发布，加载请求被拒绝")
    else:
        raise AssertionError("未发布的 search 工作流不应被加载")

    print(
        "\n[OK] 只读检查完成。执行离线集成测试请使用：\n"
        ".\\scripts\\run-test.ps1 -Direct "
        "-DataRoot .data-test\\w2-workflow-manual "
        "-Command @('python','-m','pytest',"
        "'tests/integration/test_workflow_yaml_contract.py',"
        "'tests/integration/test_workflow_integration.py','-q')"
    )


if __name__ == "__main__":
    inspect_published_workflows()
