# -*- coding: utf-8 -*-
"""
端到端工作流测试

模拟真实的归档场景:
1. 使用 archive-url 工作流
2. 输入真实 URL
3. 测试完整数据流: fetch -> analyze -> (sharpen) -> store
4. 验证上游数据路径
"""

import sys
from pathlib import Path
import asyncio

# 添加 src 到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.workflow.engine import WorkflowEngine
from src.utils.config import Config


async def test_archive_url_workflow():
    """
    测试 archive-url 工作流的完整执行流程

    注意: 这是一个演示测试，使用 Mock 数据
    真实场景需要:
    - 有效的 URL
    - DeepSeek API Key
    - OpenAI API Key
    """
    print("=" * 70)
    print("端到端测试: Archive URL 工作流")
    print("=" * 70)

    # 初始化配置和引擎
    config = Config()
    engine = WorkflowEngine(config)

    # 测试 URL (使用微信公众号测试 fixture)
    test_url = "https://mp.weixin.qq.com/s/test-article"

    print(f"\n[Step 1] 准备输入数据")
    print(f"  URL: {test_url}")

    input_data = {
        "url": test_url,
        "source": "manual",
    }

    # 执行工作流
    print(f"\n[Step 2] 执行 archive-url 工作流")
    print(f"  工作流配置: config/workflows/archive-url.yaml")
    print(f"  预期步骤:")
    print(f"    1. fetch_content - 抓取网页内容")
    print(f"    2. ai_analyze     - AI 分析生成摘要和标签")
    print(f"    3. idea_sharpen   - 人机交互优化（可选）")
    print(f"    4. store_entry    - 持久化存储")

    try:
        result = await engine.execute_async("archive-url", input_data)

        print(f"\n[Step 3] 工作流执行结果")
        print(f"  成功: {result.success}")
        print(f"  错误数量: {len(result.errors)}")
        print(f"  日志数量: {len(result.logs)}")

        if result.errors:
            print(f"\n  错误详情:")
            for i, error in enumerate(result.errors, 1):
                print(f"    {i}. {error}")

        if result.logs:
            print(f"\n  执行日志 (最近 10 条):")
            for log in result.logs[-10:]:
                print(f"    - {log}")

        # 检查数据流
        print(f"\n[Step 4] 验证数据流路径")
        state_keys = list(result.data.keys())
        print(f"  State 中的键: {state_keys}")

        expected_keys = [
            "url",
            "source",
            "fetch_content_result",  # 来自 FetchStep
            "ai_analyze_result",     # 来自 AnalyzeStep
            "idea_sharpen_result",   # 来自 IdeaSharpenStep
            "store_entry_result",    # 来自 StoreStep
        ]

        for key in expected_keys:
            if key in result.data:
                print(f"  [OK] {key}: {type(result.data[key]).__name__}")
            else:
                print(f"  [MISSING] {key}")

        # 验证上游数据路径
        print(f"\n[Step 5] 验证上游数据传递")

        # fetch_content -> ai_analyze
        if "fetch_content_result" in result.data and "ai_analyze_result" in result.data:
            print(f"  [OK] FetchStep -> AnalyzeStep 数据传递正常")

        # ai_analyze -> idea_sharpen
        if "ai_analyze_result" in result.data and "idea_sharpen_result" in result.data:
            print(f"  [OK] AnalyzeStep -> IdeaSharpenStep 数据传递正常")

        # idea_sharpen -> store
        if "idea_sharpen_result" in result.data and "store_entry_result" in result.data:
            print(f"  [OK] IdeaSharpenStep -> StoreStep 数据传递正常")

        print(f"\n" + "=" * 70)
        print("测试完成")
        print("=" * 70)

        if result.success:
            print("[OK] 端到端工作流测试通过")
            return True
        else:
            print("[FAIL] 工作流执行失败")
            return False

    except Exception as e:
        print(f"\n[EXCEPTION] 工作流执行异常: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_config_fallback():
    """
    测试配置加载的 fallback 机制

    验证:
    1. 优先加载 config/workflows/archive-url.yaml
    2. 如果不存在，fallback 到 config.yaml 中的 workflows.archive_url
    3. 简化语法能正确规范化
    """
    print("\n" + "=" * 70)
    print("测试: 配置加载 Fallback 机制")
    print("=" * 70)

    config = Config()

    # 测试 1: 独立 YAML 文件（应该优先）
    print(f"\n[Test 1] 优先级测试 - archive-url.yaml")
    try:
        cfg1 = config.get_workflow_config("archive-url")
        print(f"  [OK] 成功加载 archive-url.yaml")
        print(f"       步骤数: {len(cfg1.get('steps', []))}")
        print(f"       描述: {cfg1.get('description', 'N/A')[:50]}...")
    except Exception as e:
        print(f"  [FAIL] {e}")

    # 测试 2: config.yaml 中的配置（fallback）
    print(f"\n[Test 2] Fallback 测试 - config.yaml 中的 archive_url")
    try:
        cfg2 = config.get_workflow_config("archive_url")
        print(f"  [OK] 成功从 config.yaml 加载")
        print(f"       步骤数: {len(cfg2.get('steps', []))}")

        # 检查规范化
        steps = cfg2.get("steps", [])
        if steps and isinstance(steps[0], dict):
            print(f"  [OK] 简化语法已正确规范化")
            print(f"       第一个步骤: {steps[0]}")
        else:
            print(f"  [FAIL] 规范化失败，steps: {steps}")
    except Exception as e:
        print(f"  [FAIL] {e}")

    print(f"\n" + "=" * 70)


if __name__ == "__main__":
    print("开始端到端测试...")
    print()

    # 运行配置测试
    asyncio.run(test_config_fallback())

    # 运行工作流测试
    success = asyncio.run(test_archive_url_workflow())

    # 退出码
    sys.exit(0 if success else 1)
