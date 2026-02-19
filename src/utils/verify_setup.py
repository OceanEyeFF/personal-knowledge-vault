"""
验证安装脚本

测试配置、存储层等基础组件是否工作正常
"""

import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from src.utils.config import get_config
from src.utils.logger import LoggerSetup, get_logger
from src.utils.text_utils import TextProcessor
from src.storage.markdown_store import MarkdownStore, Entry
from src.storage.sqlite_store import SQLiteStore
from src.storage.vector_store import VectorStore
import numpy as np


def test_config():
    """测试配置加载"""
    print("\n🔧 测试配置加载...")
    config = get_config()

    print(f"  ✓ Vault 目录: {config.vault_dir}")
    print(f"  ✓ 数据库路径: {config.db_path}")
    print(f"  ✓ 向量索引目录: {config.vector_index_dir}")
    print(f"  ✓ 日志级别: {config.log_level}")
    print("  ✅ 配置加载成功")


def test_logger():
    """测试日志系统"""
    print("\n📝 测试日志系统...")
    config = get_config()

    LoggerSetup.setup(
        level=config.log_level,
        log_file=config.log_dir / "verify.log"
    )

    logger = get_logger(__name__)
    logger.info("这是一条测试日志")
    print("  ✅ 日志系统正常")


def test_text_processor():
    """测试文本处理"""
    print("\n🔤 测试文本处理...")
    processor = TextProcessor()

    # 测试分词
    text = "人工智能的未来发展趋势"
    tokenized = processor.tokenize_chinese(text)
    print(f"  原文: {text}")
    print(f"  分词: {tokenized}")

    # 测试文件名清理
    dirty_name = "AI驱动的知识管理?系统:设计"
    clean_name = processor.sanitize_filename(dirty_name)
    print(f"  原文件名: {dirty_name}")
    print(f"  清理后: {clean_name}")

    print("  ✅ 文本处理正常")


def test_markdown_store():
    """测试 Markdown 存储"""
    print("\n📄 测试 Markdown 存储...")
    config = get_config()
    store = MarkdownStore(vault_dir=config.vault_dir)

    # 创建测试条目
    entry = Entry(
        title="测试文章",
        source_type="wechat",
        tags=["测试", "AI"],
        keywords=["知识管理", "向量检索"],
        abstract="这是一篇测试文章",
        summary_one_sentence="测试 Markdown 存储功能",
        summary_100_words="这是一篇用于测试 Markdown 存储功能的文章，包含了完整的 YAML Front Matter 和正文内容。",
        content="# 测试标题\n\n这是测试内容。\n\n## 子标题\n\n更多内容..."
    )

    # 保存
    file_path = store.save(entry, subdir="test")
    print(f"  ✓ 保存文件: {file_path}")

    # 加载
    loaded_entry = store.load(file_path)
    print(f"  ✓ 加载文件: {loaded_entry.title}")

    # 验证
    assert loaded_entry.title == entry.title
    assert loaded_entry.content == entry.content
    print("  ✅ Markdown 存储正常")

    return str(file_path)


def test_sqlite_store():
    """测试 SQLite 存储"""
    print("\n🗄️  测试 SQLite 存储...")
    config = get_config()
    store = SQLiteStore(db_path=config.db_path)

    # 初始化数据库
    store.initialize()

    # 验证表是否存在
    assert store.table_exists("knowledge_items")
    assert store.table_exists("content_chunks")
    assert store.table_exists("tags")
    print("  ✓ 数据库 Schema 创建成功")

    # 创建测试条目
    entry = Entry(
        title="数据库测试文章",
        source_type="zhihu",
        tags=["数据库", "测试"],
        keywords=["SQLite", "FTS5"],
        abstract="测试数据库功能",
        summary_one_sentence="测试 SQLite 存储",
        summary_100_words="这是测试摘要",
        content="测试内容"
    )

    # 插入
    knowledge_id = store.insert_entry(entry, file_path="/test/db-test.md")
    print(f"  ✓ 插入条目: ID={knowledge_id}")

    # 查询
    result = store.query_by_id(knowledge_id)
    assert result is not None
    print(f"  ✓ 查询条目: {result['title']}")

    print("  ✅ SQLite 存储正常")


def test_vector_store():
    """测试向量存储"""
    print("\n🔢 测试向量存储...")
    config = get_config()
    dim = config.embedding_dim
    store = VectorStore(index_dir=config.vector_index_dir, dim=dim)

    # 添加测试向量
    test_vector = np.random.rand(dim).astype('float32')
    store.add_doc_vector(knowledge_id=1, vector=test_vector)
    print("  ✓ 添加文档向量")

    # 搜索
    results = store.search_doc(test_vector, k=1)
    print(f"  ✓ 搜索结果: {results}")

    # 验证
    assert len(results) > 0
    assert results[0][0] == 1  # 应该找到自己

    # 统计信息
    stats = store.get_index_stats()
    print(f"  ✓ 索引统计: {stats}")

    print("  ✅ 向量存储正常")


def main():
    """主函数"""
    print("=" * 60)
    print("🚀 Personal Knowledge Vault - 验证安装")
    print("=" * 60)

    try:
        test_config()
        test_logger()
        test_text_processor()
        test_markdown_store()
        test_sqlite_store()
        test_vector_store()

        print("\n" + "=" * 60)
        print("✅ 所有测试通过！系统安装正确！")
        print("=" * 60)

    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
