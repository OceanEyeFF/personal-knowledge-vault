# -*- coding: utf-8 -*-
"""
简化版真实环境测试（跳过向量存储，避免 OpenAI API 超时）
"""

import sys
import asyncio
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.workflow.engine import WorkflowEngine
from src.utils.config import Config
from src.storage.sqlite_store import SQLiteStore


async def test_without_vector():
    """测试 Markdown + SQLite 存储（跳过向量）"""
    print("=" * 80)
    print("简化测试：Markdown + SQLite（跳过向量索引）")
    print("=" * 80)
    print()

    config = Config()
    engine = WorkflowEngine(config)

    # 修改配置：只测试 markdown + sqlite
    test_url = "https://mp.weixin.qq.com/s/ZET927baoFCj3In_11fKeA"

    print(f"测试 URL: {test_url}")
    print()

    # 创建临时配置：只包含 markdown 和 sqlite
    import yaml
    temp_config_path = project_root / "config" / "workflows" / "archive-url-test.yaml"

    test_config = {
        "name": "archive-url-test",
        "steps": [
            {"id": "fetch_content", "type": "fetch_content", "config": {}},
            {"id": "ai_analyze", "type": "ai_analyze", "config": {}},
            {"id": "idea_sharpen", "type": "idea_sharpen", "config": {}},
            {
                "id": "store_entry",
                "type": "store_entry",
                "config": {
                    "targets": ["markdown", "sqlite"]  # 只测试这两个
                }
            }
        ]
    }

    with open(temp_config_path, "w", encoding="utf-8") as f:
        yaml.dump(test_config, f, allow_unicode=True)

    print("[Step 1] 执行工作流...")
    result = await engine.execute_async("archive-url-test", {"url": test_url, "source": "simplified_test"})

    print(f"\n[Step 2] 检查结果...")
    print(f"  成功: {result.success}")
    print(f"  错误: {result.errors}")
    print(f"  Knowledge ID: {result.data.get('knowledge_id')}")
    print(f"  File Path: {result.data.get('file_path')}")
    print(f"  Stored Targets: {result.data.get('stored_targets')}")

    # 验证数据库
    if result.data.get('knowledge_id'):
        print(f"\n[Step 3] 验证 SQLite 数据...")
        knowledge_id = result.data['knowledge_id']

        try:
            sqlite_store = SQLiteStore(config.db_path)
            conn = sqlite_store._get_connection()
            cursor = conn.execute(
                "SELECT title, source_url, created_at FROM knowledge_items WHERE knowledge_id = ?",
                (knowledge_id,)
            )
            row = cursor.fetchone()

            if row:
                print(f"  [OK] 数据库记录存在")
                print(f"       标题: {row[0]}")
                print(f"       URL: {row[1]}")
                print(f"       创建时间: {row[2]}")
            else:
                print(f"  [FAIL] 未找到记录")
        except Exception as e:
            print(f"  [FAIL] 数据库查询失败: {e}")

    # 验证 Markdown
    if result.data.get('file_path'):
        file_path = Path(result.data['file_path'])
        if file_path.exists():
            print(f"\n[Step 4] 验证 Markdown 文件...")
            print(f"  [OK] 文件存在: {file_path.name}")
            print(f"  [OK] 文件大小: {file_path.stat().st_size} 字节")
        else:
            print(f"  [FAIL] 文件不存在: {file_path}")

    # 清理临时配置
    temp_config_path.unlink(missing_ok=True)

    print()
    print("=" * 80)
    if result.success and result.data.get('knowledge_id'):
        print("[OK] 简化测试通过！")
        print("=" * 80)
        return True
    else:
        print("[FAIL] 测试失败")
        print("=" * 80)
        return False


if __name__ == "__main__":
    success = asyncio.run(test_without_vector())
    sys.exit(0 if success else 1)
