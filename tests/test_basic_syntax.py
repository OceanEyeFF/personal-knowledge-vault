"""
基础语法测试

不依赖外部包，验证代码的基本逻辑和语法是否正确
"""

# ruff: noqa: F401

import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def test_imports():
    """测试模块导入"""
    print("📦 测试模块导入...")

    # 测试配置模块；导入失败必须直接让 pytest 失败。
    from src.utils import config
    print("  ✓ src.utils.config")

    from src.utils import logger
    print("  ✓ src.utils.logger")

    from src.utils import text_utils
    print("  ✓ src.utils.text_utils")

    # 测试存储模块
    from src.storage import markdown_store
    print("  ✓ src.storage.markdown_store")

    from src.storage import sqlite_store
    print("  ✓ src.storage.sqlite_store")

    from src.storage import vector_store
    print("  ✓ src.storage.vector_store")

    print("✅ 所有模块导入成功！")


def test_data_class():
    """测试 Entry 数据类"""
    print("\n📝 测试 Entry 数据类...")

    # 这里我们只测试类的定义，不实际实例化（因为需要依赖包）
    from src.storage.markdown_store import Entry
    print("  ✓ Entry 类定义正确")

    # 检查必需字段
    import inspect
    sig = inspect.signature(Entry.__init__)
    params = list(sig.parameters.keys())

    required_fields = ["title", "source_type"]
    missing_fields = [field for field in required_fields if field not in params]
    assert not missing_fields, f"Entry 缺少必需字段: {missing_fields}"
    for field in required_fields:
        print(f"  ✓ 必需字段存在: {field}")

    print("✅ Entry 数据类定义正确！")


def test_file_structure():
    """测试文件结构"""
    print("\n📁 测试文件结构...")

    required_files = [
        "src/__init__.py",
        "src/utils/__init__.py",
        "src/utils/config.py",
        "src/utils/logger.py",
        "src/utils/text_utils.py",
        "src/storage/__init__.py",
        "src/storage/markdown_store.py",
        "src/storage/sqlite_store.py",
        "src/storage/vector_store.py",
        "config/config.yaml",
        "config/custom_dict.txt",
        "requirements.txt",
    ]

    missing_files = [
        file_path
        for file_path in required_files
        if not (project_root / file_path).exists()
    ]
    assert not missing_files, f"缺少必需文件: {missing_files}"
    for file_path in required_files:
        print(f"  ✓ {file_path}")
    print("✅ 所有必需文件都存在！")


def test_config_yaml():
    """测试配置文件格式"""
    print("\n⚙️  测试配置文件...")

    config_path = project_root / "config" / "config.yaml"
    assert config_path.exists(), "config.yaml 不存在"

    # 简单读取文件，检查是否为有效的文本
    content = config_path.read_text(encoding="utf-8")

    # 检查关键配置项是否存在
    required_keys = ["storage:", "ai:", "retrieval:", "logging:"]
    missing_keys = [key for key in required_keys if key not in content]
    assert not missing_keys, f"config.yaml 缺少配置项: {missing_keys}"
    for key in required_keys:
        print(f"  ✓ 配置项存在: {key}")

    print("✅ 配置文件格式正确！")


def main():
    """主函数"""
    print("=" * 60)
    print("🧪 Personal Knowledge Vault - 基础语法测试")
    print("=" * 60)
    print()

    tests = [
        test_file_structure,
        test_config_yaml,
        test_imports,
        test_data_class,
    ]

    results = []
    for test in tests:
        try:
            test()
            results.append(True)
        except Exception as e:
            print(f"❌ 测试异常: {e}")
            import traceback
            traceback.print_exc()
            results.append(False)

    print("\n" + "=" * 60)
    if all(results):
        print("✅ 所有基础测试通过！代码结构正确！")
        print("=" * 60)
        print("\n💡 下一步：安装依赖包后运行完整验证")
        print("   pip install -r requirements.txt")
        print("   python src/utils/verify_setup.py")
    else:
        print("❌ 部分测试失败，请检查上述错误")
        print("=" * 60)
        return 1

    return 0


if __name__ == "__main__":
    exit(main())
