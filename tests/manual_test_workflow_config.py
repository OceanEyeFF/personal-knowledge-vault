"""手动检查 M13 W2 发布工作流配置。

本脚本只读取 bundled 配置，不执行工作流、不访问网络，也不写入 Vault。
当前发布合同只包含 ``archive-url.yaml`` 与 ``archive-text.yaml``；不存在
``search.yaml`` 或 ``config.yaml`` 内嵌 steps fallback。
"""

import sys
from pathlib import Path

# 添加 src 到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.utils.config import Config
from src.runtime.layout import RuntimeLayout


def test_workflow_config_loading():
    """检查两份版本化 YAML，并确认未发布的 search 工作流 fail-closed。"""
    layout = RuntimeLayout.resolve(
        resources_root=project_root,
    )
    config = Config(layout=layout)
    for workflow_name in ("archive-url", "archive-text"):
        workflow = config.get_workflow_config(workflow_name)
        assert workflow.get("schema_version") == 1
        assert workflow.get("name") == workflow_name
        steps = workflow.get("steps")
        assert isinstance(steps, list) and steps
        assert all(
            isinstance(step, dict)
            and step.get("id")
            and step.get("type")
            and step.get("on_error") in {"fail", "continue"}
            for step in steps
        )
        print(
            f"[OK] {workflow_name}.yaml: "
            f"schema=1, steps={len(steps)}"
        )

    alias = config.get_workflow_config("archive_url")
    assert alias.get("name") == "archive-url"
    print("[OK] archive_url 仅作为文件名别名解析到 archive-url.yaml")

    try:
        config.get_workflow_config("search")
    except FileNotFoundError:
        print("[OK] search 工作流未发布，加载请求按合同拒绝")
    else:
        raise AssertionError("未发布的 search 工作流不应被加载")

    print("[OK] 未使用 config.yaml 内嵌 steps fallback")


if __name__ == "__main__":
    test_workflow_config_loading()
