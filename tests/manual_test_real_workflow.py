# -*- coding: utf-8 -*-
"""
真实环境端到端工作流测试 (方案 A)

测试目标:
1. 使用真实 URL（从 test_urls.json 选择）
2. 使用真实 API（DeepSeek + OpenAI）
3. 完整执行 archive-url 工作流
4. 验证三重存储（Markdown + SQLite + Vector）

执行时间: 约 15 分钟（包括 API 调用）
"""

# ruff: noqa: E402, F541

import sys
import asyncio
import json
from pathlib import Path
from typing import List, Dict, Any

# 添加 src 到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.workflow.engine import WorkflowEngine
from src.utils.config import Config
from src.storage.sqlite_store import SQLiteStore


def load_test_urls() -> List[Dict[str, Any]]:
    """
    从 test_urls.json 加载测试 URL

    Returns:
        测试用例列表
    """
    fixtures_path = project_root / "tests" / "fixtures" / "test_urls.json"

    if not fixtures_path.exists():
        print(f"[WARN] 未找到测试配置文件: {fixtures_path}")
        return []

    with open(fixtures_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    test_cases = data.get("test_cases", {})

    # 选择代表性 URL（每个类型选 1 个）
    selected = []

    # 微信文章（选第 1 个）
    wechat_cases = test_cases.get("wechat", [])
    if wechat_cases:
        selected.append({
            "type": "wechat",
            "url": wechat_cases[0]["url"],
            "description": wechat_cases[0].get("description", "微信文章"),
            "expected": wechat_cases[0].get("expected", {}),
        })

    # 知乎内容（选第 1 个）
    zhihu_cases = test_cases.get("zhihu", [])
    if zhihu_cases:
        selected.append({
            "type": "zhihu",
            "url": zhihu_cases[0]["url"],
            "description": zhihu_cases[0].get("description", "知乎内容"),
            "expected": zhihu_cases[0].get("expected", {}),
        })

    # 通用网页（选 CSDN）
    generic_cases = test_cases.get("generic", [])
    for case in generic_cases:
        if "csdn" in case["url"].lower():
            selected.append({
                "type": "generic",
                "url": case["url"],
                "description": case.get("description", "通用网页"),
                "expected": case.get("expected", {}),
            })
            break

    return selected


async def test_single_url(engine: WorkflowEngine, test_case: Dict[str, Any], index: int) -> Dict[str, Any]:
    """
    测试单个 URL 的工作流

    Args:
        engine: 工作流引擎
        test_case: 测试用例
        index: 测试序号

    Returns:
        测试结果字典
    """
    url = test_case["url"]
    case_type = test_case["type"]
    description = test_case["description"]
    expected = test_case.get("expected", {})

    print("=" * 80)
    print(f"测试 {index}: {case_type.upper()} - {description}")
    print("=" * 80)
    print(f"URL: {url}")
    print()

    # 准备输入数据
    input_data = {
        "url": url,
        "source": "manual_test",
    }

    # 执行工作流
    print("[Step 1] 执行 archive-url 工作流...")
    try:
        result = await engine.execute_async("archive-url", input_data)
    except Exception as e:
        print(f"[FAIL] 工作流执行异常: {e}")
        import traceback
        traceback.print_exc()
        return {
            "success": False,
            "url": url,
            "type": case_type,
            "error": str(e),
        }

    # 检查执行结果
    print(f"\n[Step 2] 检查执行结果...")
    print(f"  成功: {result.success}")
    print(f"  错误数量: {len(result.errors)}")

    if result.errors:
        print(f"\n  错误详情:")
        for i, error in enumerate(result.errors, 1):
            print(f"    {i}. {error}")

    if not result.success:
        print(f"\n[FAIL] 工作流执行失败")
        return {
            "success": False,
            "url": url,
            "type": case_type,
            "errors": result.errors,
        }

    # 验证数据
    print(f"\n[Step 3] 验证生成的数据...")

    # 检查 State 数据
    state_data = result.data
    file_path = state_data.get("file_path")
    knowledge_id = state_data.get("knowledge_id")
    title = state_data.get("title")
    content_length = len(state_data.get("content", ""))
    summary = state_data.get("summary")
    tags = state_data.get("tags", [])

    print(f"  标题: {title or 'N/A'}")
    print(f"  内容长度: {content_length} 字符")
    print(f"  摘要: {summary[:50] if summary else 'N/A'}...")
    print(f"  标签: {tags}")
    print(f"  Markdown 路径: {file_path}")
    print(f"  Knowledge ID: {knowledge_id}")

    # 验证期望值
    validation_results = []

    if expected.get("has_title"):
        if title:
            print(f"  [OK] 标题存在")
            validation_results.append(("title", True))
        else:
            print(f"  [FAIL] 缺少标题")
            validation_results.append(("title", False))

    if expected.get("has_content"):
        min_length = expected.get("min_content_length", 100)
        if content_length >= min_length:
            print(f"  [OK] 内容长度满足要求 (>= {min_length})")
            validation_results.append(("content_length", True))
        else:
            print(f"  [FAIL] 内容长度不足 ({content_length} < {min_length})")
            validation_results.append(("content_length", False))

    # 验证 Markdown 文件
    if file_path:
        md_path = Path(file_path)
        if md_path.exists():
            print(f"  [OK] Markdown 文件已创建: {md_path.name}")
            validation_results.append(("markdown_file", True))

            # 读取文件内容验证
            with open(md_path, "r", encoding="utf-8") as f:
                md_content = f.read()
                if "---" in md_content and "title:" in md_content:
                    print(f"  [OK] YAML Front Matter 格式正确")
                    validation_results.append(("yaml_frontmatter", True))
                else:
                    print(f"  [WARN] YAML Front Matter 可能缺失")
                    validation_results.append(("yaml_frontmatter", False))
        else:
            print(f"  [FAIL] Markdown 文件未找到: {file_path}")
            validation_results.append(("markdown_file", False))

    # 验证 SQLite 记录
    if knowledge_id:
        config = Config()
        try:
            sqlite_store = SQLiteStore(config.db_path)
            sqlite_store.initialize()

            # 查询记录
            conn = sqlite_store._get_connection()
            cursor = conn.execute(
                "SELECT title, file_path FROM knowledge_items WHERE knowledge_id = ?",
                (knowledge_id,)
            )
            row = cursor.fetchone()

            if row:
                print(f"  [OK] SQLite 记录已创建 (ID: {knowledge_id})")
                print(f"       数据库标题: {row[0]}")
                validation_results.append(("sqlite_record", True))
            else:
                print(f"  [FAIL] SQLite 记录未找到 (ID: {knowledge_id})")
                validation_results.append(("sqlite_record", False))
        except Exception as e:
            print(f"  [FAIL] SQLite 验证失败: {e}")
            validation_results.append(("sqlite_record", False))

    # 总结
    all_passed = all(result for _, result in validation_results)

    print(f"\n[Step 4] 测试结果")
    if all_passed:
        print(f"  [OK] 所有验证通过")
    else:
        failed = [name for name, result in validation_results if not result]
        print(f"  [FAIL] 部分验证失败: {', '.join(failed)}")

    print()

    return {
        "success": all_passed,
        "url": url,
        "type": case_type,
        "title": title,
        "content_length": content_length,
        "file_path": file_path,
        "knowledge_id": knowledge_id,
        "validation_results": validation_results,
    }


async def main():
    """
    主测试函数
    """
    print("=" * 80)
    print("真实环境端到端工作流测试 (方案 A)")
    print("=" * 80)
    print()

    # 加载测试 URL
    print("[准备] 加载测试 URL...")
    test_cases = load_test_urls()

    if not test_cases:
        print("[FAIL] 未找到测试 URL，请检查 tests/fixtures/test_urls.json")
        return False

    print(f"[OK] 成功加载 {len(test_cases)} 个测试用例")
    for i, case in enumerate(test_cases, 1):
        print(f"  {i}. {case['type']}: {case['description']}")
    print()

    # 初始化工作流引擎
    print("[准备] 初始化工作流引擎...")
    config = Config()
    engine = WorkflowEngine(config)
    print("[OK] 工作流引擎初始化完成")
    print()

    # 检查 API Keys
    print("[准备] 检查 API Keys...")
    llm_key = config.llm_api_key
    embd_key = config.embd_api_key

    if not llm_key:
        print("[WARN] config/local.yaml 未配置 LLM API Key，AI 分析可能失败")
    else:
        print("[OK] LLM API Key 已配置")

    if not embd_key:
        print("[WARN] config/local.yaml 未配置 Embedding API Key，向量存储可能失败")
    else:
        print("[OK] Embedding API Key 已配置")
    print()

    # 执行测试
    results = []
    for i, test_case in enumerate(test_cases, 1):
        result = await test_single_url(engine, test_case, i)
        results.append(result)

    # 最终总结
    print("=" * 80)
    print("测试总结")
    print("=" * 80)

    success_count = sum(1 for r in results if r["success"])
    total_count = len(results)

    print(f"\n总计: {success_count}/{total_count} 通过\n")

    for i, result in enumerate(results, 1):
        status = "[OK]" if result["success"] else "[FAIL]"
        print(f"{i}. {status} {result['type']}: {result.get('title', 'N/A')}")
        if result.get("file_path"):
            print(f"   文件: {result['file_path']}")
        if result.get("errors"):
            for error in result["errors"]:
                print(f"   错误: {error}")

    print()
    print("=" * 80)

    if success_count == total_count:
        print("[OK] 所有真实环境测试通过!")
        print("=" * 80)
        return True
    else:
        print(f"[FAIL] 部分测试失败 ({total_count - success_count} 个)")
        print("=" * 80)
        return False


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
