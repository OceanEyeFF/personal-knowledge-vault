#!/usr/bin/env python3
"""
Milestone 3 处理器集成测试脚本

用于测试真实链接的处理能力。

使用方法:
    python tests/manual_test_processors.py [--use-config]

参数:
    --use-config: 从 tests/fixtures/test_urls.json 读取测试链接
"""

import asyncio
import sys
import json
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.processors import get_processor


def load_test_urls():
    """从 JSON 配置文件加载测试链接"""
    config_path = Path(__file__).parent / "fixtures" / "test_urls.json"

    if not config_path.exists():
        print(f"[!] 配置文件不存在: {config_path}")
        return None

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        return config.get("test_cases", {})
    except Exception as e:
        print(f"[X] 加载配置文件失败: {e}")
        return None


async def test_wechat_processor(test_urls=None):
    """测试微信文章处理器"""
    print("\n" + "="*80)
    print("测试 1: 微信文章处理器")
    print("="*80)

    # 从配置文件加载或使用默认链接
    if test_urls and "wechat" in test_urls:
        urls = [case["url"] for case in test_urls["wechat"]]
    else:
        urls = ["https://mp.weixin.qq.com/s/example"]  # 默认链接

    # 检查是否为示例链接
    if all("example" in url for url in urls):
        print("\n[!] 请先在 tests/fixtures/test_urls.json 中配置真实的微信文章链接")
        print(f"当前链接: {urls}")
        print("跳过此测试...\n")
        return True  # 默认通过，避免报错

    success_count = 0

    for url in urls:
        print(f"\n正在处理: {url}")
        try:
            processor = get_processor(url)
            print(f"[OK] 识别为: {processor.__class__.__name__}")

            entry = await processor.process(url)

            # 验证结果
            assert entry.title, "[X] 标题为空"
            assert entry.content, "[X] 内容为空"
            source_url = entry.metadata.get("source_url") or entry.metadata.get("source")
            assert source_url == url, "[X] 来源 URL 不匹配"

            print(f"[OK] 处理成功:")
            print(f"  标题: {entry.title}")
            print(f"  内容长度: {len(entry.content)} 字符")
            print(f"  作者: {entry.metadata.get('author', '未知')}")
            print(f"  发布时间: {entry.metadata.get('published_time', '未知')}")
            print(f"  来源类型: {entry.metadata.get('source_type', '未知')}")

            # 显示内容预览（前 500 字符）
            print(f"\n内容预览:")
            print(entry.content[:500])
            print("...")

            success_count += 1

        except Exception as e:
            print(f"[X] 测试失败: {e}")
            import traceback
            traceback.print_exc()

    return success_count == len(urls)


async def test_zhihu_processor(test_urls=None):
    """测试知乎内容处理器"""
    print("\n" + "="*80)
    print("测试 2: 知乎内容处理器")
    print("="*80)

    # 从配置文件加载或使用默认链接
    if test_urls and "zhihu" in test_urls:
        urls = [case["url"] for case in test_urls["zhihu"]]
    else:
        urls = [
            "https://www.zhihu.com/question/12345678",
            "https://zhuanlan.zhihu.com/p/12345678",
        ]

    # 检查是否为示例链接
    if all("12345678" in url for url in urls):
        print("\n[!] 请先在 tests/fixtures/test_urls.json 中配置真实的知乎链接")
        print(f"当前链接: {urls}")
        print("跳过此测试...\n")
        return True  # 默认通过，避免报错

    success_count = 0

    for url in urls:
        print(f"\n正在处理: {url}")
        try:
            processor = get_processor(url)
            print(f"[OK] 识别为: {processor.__class__.__name__}")

            entry = await processor.process(url)

            # 验证结果
            assert entry.title, "[X] 标题为空"
            assert entry.content, "[X] 内容为空"
            source_url = entry.metadata.get("source_url") or entry.metadata.get("source")
            assert source_url == url, "[X] 来源 URL 不匹配"

            print(f"[OK] 处理成功:")
            print(f"  标题: {entry.title}")
            print(f"  内容长度: {len(entry.content)} 字符")
            print(f"  作者: {entry.metadata.get('author', '未知')}")
            print(f"  发布时间: {entry.metadata.get('published_time', '未知')}")

            # 显示内容预览（前 300 字符）
            print(f"\n内容预览:")
            print(entry.content[:300])
            print("...")

            success_count += 1

        except Exception as e:
            print(f"[X] 测试失败: {e}")
            import traceback
            traceback.print_exc()

    return success_count == len(urls)


async def test_generic_processor(test_urls=None):
    """测试通用网页处理器"""
    print("\n" + "="*80)
    print("测试 3: 通用网页处理器")
    print("="*80)

    # 从配置文件加载或使用默认链接
    if test_urls and "generic" in test_urls:
        urls = [case["url"] for case in test_urls["generic"]]
    else:
        urls = [
            "https://www.example.com/article",
            "https://docs.python.org/3/library/asyncio.html",
        ]

    # 检查是否为示例链接
    if any("example.com" in url for url in urls):
        print("\n[!] 请先在 tests/fixtures/test_urls.json 中配置真实的网页链接")
        print(f"当前链接: {urls}")
        print("跳过此测试...\n")
        return True  # 默认通过，避免报错

    success_count = 0

    for url in urls:
        print(f"\n正在处理: {url}")
        try:
            processor = get_processor(url)
            print(f"[OK] 识别为: {processor.__class__.__name__}")

            entry = await processor.process(url)

            # 验证结果
            assert entry.title, "[X] 标题为空"
            assert entry.content, "[X] 内容为空"
            source_url = entry.metadata.get("source_url") or entry.metadata.get("source")
            assert source_url == url, "[X] 来源 URL 不匹配"

            print(f"[OK] 处理成功:")
            print(f"  标题: {entry.title}")
            print(f"  内容长度: {len(entry.content)} 字符")

            # 显示内容预览（前 300 字符）
            print(f"\n内容预览:")
            print(entry.content[:300])
            print("...")

            success_count += 1

        except Exception as e:
            print(f"[X] 测试失败: {e}")
            import traceback
            traceback.print_exc()

    return success_count == len(urls)


async def test_chat_processor():
    """测试聊天记录处理器"""
    print("\n" + "="*80)
    print("测试 4: 聊天记录处理器")
    print("="*80)

    # 使用测试 Fixtures
    fixtures = [
        "tests/fixtures/chat_sample.txt",
        "tests/fixtures/chat_sample.json",
    ]

    success_count = 0

    for file_path in fixtures:
        print(f"\n正在处理: {file_path}")
        try:
            # 检查文件是否存在
            if not Path(file_path).exists():
                print(f"[!]  测试文件不存在，跳过: {file_path}")
                continue

            processor = get_processor(file_path)
            print(f"[OK] 识别为: {processor.__class__.__name__}")

            entry = await processor.process(file_path)

            # 验证结果
            assert entry.title, "[X] 标题为空"
            assert entry.content, "[X] 内容为空"
            # 聊天处理器使用 source_url 而不是 source
            # 使用 Path 对象比较，避免路径分隔符问题（Windows: \\ vs Unix: /）
            source_path = entry.metadata.get("source_url") or entry.metadata.get("source")
            assert Path(source_path) == Path(file_path) or Path(source_path).resolve() == Path(file_path).resolve(), f"[X] 来源路径不匹配: {source_path} vs {file_path}"

            print(f"[OK] 处理成功:")
            print(f"  标题: {entry.title}")
            print(f"  内容长度: {len(entry.content)} 字符")
            # Entry 使用 abstract 或 summary_100_words，不是 summary
            summary = entry.abstract or entry.summary_100_words or "无"
            print(f"  摘要: {summary[:100]}..." if len(summary) > 100 else f"  摘要: {summary}")
            print(f"  标签: {', '.join(entry.tags) if entry.tags else '无'}")

            # 显示内容预览（前 300 字符）
            print(f"\n内容预览:")
            print(entry.content[:300])
            print("...")

            success_count += 1

        except Exception as e:
            print(f"[X] 测试失败: {e}")
            import traceback
            traceback.print_exc()

    return success_count == len(fixtures)


async def main():
    """运行所有集成测试"""
    print("\n" + "="*80)
    print("Milestone 3: 内容处理器 - 集成测试")
    print("="*80)

    # 检查是否使用配置文件
    use_config = "--use-config" in sys.argv

    test_urls = None
    if use_config:
        print("\n[*] 从配置文件加载测试链接...")
        test_urls = load_test_urls()
        if not test_urls:
            print("[X] 无法加载配置文件，使用默认测试")
            use_config = False

    if not use_config:
        print("\n[!] 注意:")
        print("  1. 当前使用默认测试链接（示例链接将被跳过）")
        print("  2. 使用 --use-config 参数从 tests/fixtures/test_urls.json 读取真实链接")
        print("  3. 确保 .env 文件中已配置 DEEPSEEK_API_KEY 和 OPENAI_API_KEY")
        print("  4. 确保已安装 Playwright 浏览器: python -m playwright install chromium")
        print("\n当前仅运行聊天记录处理器测试（使用测试 Fixtures）\n")

    results = []

    # 运行各个测试
    if use_config or "--all" in sys.argv:
        results.append(("微信文章处理器", await test_wechat_processor(test_urls)))
        results.append(("知乎内容处理器", await test_zhihu_processor(test_urls)))
        results.append(("通用网页处理器", await test_generic_processor(test_urls)))

    results.append(("聊天记录处理器", await test_chat_processor()))

    # 输出测试总结
    print("\n" + "="*80)
    print("测试总结")
    print("="*80)

    passed = sum(1 for _, result in results if result)
    total = len(results)

    for test_name, result in results:
        status = "[OK] 通过" if result else "[X] 失败"
        print(f"{test_name}: {status}")

    print(f"\n总计: {passed}/{total} 通过")

    if passed == total:
        print("\n[√] 所有启用的集成测试通过!")
        return 0
    else:
        print(f"\n[!]  {total - passed} 个测试失败")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
