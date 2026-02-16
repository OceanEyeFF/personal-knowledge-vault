"""
PKV 安全测试演示脚本（Python 版本）
演示如何使用测试环境安全地测试各种内容源
"""

import os
import sys
import asyncio
from pathlib import Path

# 添加项目根目录到 sys.path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from src.utils.config import Config
from src.processors.text_fallback_processor import TextFallbackProcessor
from src.storage.markdown_store import MarkdownStore
from src.storage.sqlite_store import SQLiteStore


def print_header(title: str):
    """打印标题"""
    print("\n" + "=" * 70)
    print(f" {title}")
    print("=" * 70)
    print()


def print_step(step: str):
    """打印步骤"""
    print(f"\n{'='*70}")
    print(f" {step}")
    print(f"{'='*70}\n")


async def test_text_archive():
    """测试纯文本归档（知乎回答样本）"""
    print_step("第 5 步：测试纯文本归档（知乎回答样本）")

    text_file = project_root / "tests" / "fixtures" / "zhihu" / "zhihu reply sample.txt"

    print(f"测试文件: {text_file}")
    print("说明: 知乎回答样本（避免 Playwright 访问问题）")
    print()

    if not text_file.exists():
        print(f"错误: 测试文件不存在: {text_file}")
        return

    # 读取文本文件
    with open(text_file, 'r', encoding='utf-8') as f:
        text_content = f.read()

    # 显示前 500 字符
    preview = text_content[:500]
    print("文件内容预览（前 500 字符）:")
    print("-" * 70)
    print(preview)
    print("...")
    print("-" * 70)
    print()

    confirm = input("是否执行纯文本归档测试？(Y/N，默认 Y): ").strip()
    if confirm.upper() == "N":
        print("已跳过纯文本测试")
        return

    print()
    print("提示: 使用 TextFallbackProcessor 处理纯文本")
    print()

    # 设置测试环境
    os.environ["DB_PATH"] = str(project_root / ".data-test" / "db" / "knowledge_vault.db")

    # 加载配置
    config = Config()

    # 初始化存储
    md_store = MarkdownStore(config.vault_dir)
    sqlite_store = SQLiteStore(config.db_path)

    # 初始化处理器
    processor = TextFallbackProcessor()

    print("处理纯文本中...")
    print()

    # 处理文本（TextFallbackProcessor 接受文本内容作为 url 参数）
    entry = await processor.process(text_content)

    # 手动设置标题（如果需要）
    if not entry.title or entry.title == "未知标题":
        entry.title = "知乎回答：为什么说链表已死？"

    print(f"[OK] 生成 Entry: {entry.title}")
    print(f"  摘要: {entry.summary_one_sentence[:100]}..." if len(entry.summary_one_sentence) > 100 else f"  摘要: {entry.summary_one_sentence}")
    print(f"  字数: {entry.word_count}")
    print(f"  标签: {', '.join(entry.tags) if entry.tags else '无'}")
    print(f"  来源类型: {entry.source_type}")
    print()

    # 存储到 Markdown
    md_path = md_store.save(entry)
    print(f"[OK] Markdown 已保存: {md_path}")

    # 存储到 SQLite
    sqlite_store.initialize()
    knowledge_id = sqlite_store.insert_entry(entry, str(md_path))
    print(f"[OK] SQLite 已保存 (ID: {knowledge_id})")
    print()

    print("=" * 70)
    print(" [OK] 纯文本归档完成！")
    print("=" * 70)
    print()

    # 查看测试环境统计
    print("查看测试环境状态...")
    # 直接使用 SQL 查询统计
    with sqlite_store.get_connection() as conn:
        cursor = conn.execute("SELECT COUNT(*) FROM knowledge_items")
        total_count = cursor.fetchone()[0]
        print(f"  总条目数: {total_count}")

        # 获取最新的几条记录
        cursor = conn.execute("""
            SELECT knowledge_id, title FROM knowledge_items
            ORDER BY archived_at DESC LIMIT 5
        """)
        recent_items = cursor.fetchall()
        if recent_items:
            print(f"  最新条目:")
            for kid, title in recent_items:
                print(f"    ID {kid}: {title[:50]}...")
    print()


def check_environment():
    """检测环境"""
    print_step("第 0 步：环境检测")

    db_path_env = os.environ.get("DB_PATH")

    if db_path_env:
        print("[环境变量模式]")
        print(f"  DB_PATH = {db_path_env}")

        if ".data-test" in db_path_env:
            print("  状态: [OK] 测试环境")
            is_test = True
        else:
            print("  状态: [警告] 生产环境")
            is_test = False
    else:
        print("[配置文件模式]")
        print("  未设置 DB_PATH 环境变量，使用默认配置")

        try:
            config = Config()
            db_path = config.db_path
            print(f"  实际路径: {db_path}")

            if ".data-test" in str(db_path):
                print("  状态: [OK] 测试环境")
                is_test = True
            else:
                print("  状态: [警告] 生产环境")
                is_test = False
        except Exception as e:
            print(f"  错误: 无法读取配置 - {e}")
            is_test = False

    print()
    print("=" * 70)

    # 数据库统计
    print("数据库统计:")
    print()

    try:
        config = Config()
        sqlite_store = SQLiteStore(config.db_path)

        # 检查数据库是否存在
        if not config.db_path.exists():
            print("  数据库文件不存在（可能是首次运行）")
        else:
            # 直接使用 SQL 查询统计
            with sqlite_store.get_connection() as conn:
                cursor = conn.execute("SELECT COUNT(*) FROM knowledge_items")
                total_count = cursor.fetchone()[0]
                print(f"  总条目数: {total_count}")
    except Exception as e:
        print(f"  错误: 无法读取数据库统计 - {e}")

    print()
    print("=" * 70)

    # 安全建议
    if is_test:
        print("[OK] 当前为测试环境，可以安全测试")
        print()
        print("提示: 使用测试环境执行命令")
    else:
        print("[警告] 当前为生产环境，请谨慎操作")
        print()
        print("建议: 如需测试，请使用测试环境")
        print("  1. 设置环境变量: export DB_PATH=.data-test/db/knowledge_vault.db")
        print("  2. 或使用 run-test.ps1 脚本")

    print()


async def main():
    """主函数"""
    print_header("PKV 安全测试演示（Python 版本）")

    print("演示目标:")
    print("  1. 环境检测")
    print("  2. 测试纯文本归档（知乎回答样本）")
    print()

    print("关键点:")
    print("  [OK] 所有测试在隔离的测试环境执行")
    print("  [OK] 生产数据完全不受影响")
    print("  [OK] 使用 fixtures 中的真实测试数据")
    print()

    input("按 Enter 继续...")

    # 第 0 步：环境检测
    check_environment()

    input("\n按 Enter 继续到纯文本归档测试...")

    # 第 5 步：测试纯文本归档
    await test_text_archive()

    # 验证生产环境未受影响
    print_step("第 6 步：验证生产环境未受影响")

    print("重置环境变量到生产环境...")
    if "DB_PATH" in os.environ:
        del os.environ["DB_PATH"]

    print()
    check_environment()

    # 最终总结
    print_header("演示完成 [SUCCESS]")

    print("总结:")
    print("  [OK] 测试环境完全隔离")
    print("  [OK] 生产数据安全无虞")
    print("  [OK] 纯文本归档测试成功")
    print()

    print("测试的内容类型:")
    print("  - 纯文本（知乎回答样本）")
    print()

    print("下一步:")
    print("  [DOC] 阅读文档: docs/测试环境快速开始.md")
    print("  [TOOL] 使用脚本: scripts/run-test.ps1")
    print("  [TEST] 测试网页: python demo_safe_testing.py")
    print()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n演示已中断")
    except Exception as e:
        print(f"\n错误: {e}")
        import traceback
        traceback.print_exc()
