"""
AI 服务手动测试脚本

使用真实的 API Key 测试 AI 服务功能
"""

import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.ai.deepseek_client import DeepSeekClient
from src.ai.openai_client import OpenAIClient
from src.ai.embedder import Embedder
from src.utils.logger import LoggerSetup


# 设置日志
LoggerSetup.setup(level="INFO")

print("=" * 70)
print("AI 服务手动测试")
print("=" * 70)
print()

# 测试文本
test_text = """
人工智能（Artificial Intelligence，简称 AI）是计算机科学的一个分支，
致力于创建能够模拟人类智能行为的系统。AI 技术包括机器学习、深度学习、
自然语言处理、计算机视觉等多个领域。

近年来，随着计算能力的提升和大数据的发展，AI 技术取得了突破性进展。
大型语言模型（LLM）如 GPT、Claude 等展现出了惊人的语言理解和生成能力，
在文本生成、翻译、摘要、问答等任务上表现优异。

AI 技术正在深刻改变着我们的生活和工作方式，从智能助手到自动驾驶，
从医疗诊断到金融分析，AI 的应用场景越来越广泛。
"""

# =============================
# 测试 1: DeepSeek 摘要生成
# =============================
print("测试 1: DeepSeek 摘要生成")
print("-" * 70)

try:
    deepseek_client = DeepSeekClient()

    print("正在生成摘要...")
    summary = deepseek_client.summarize(test_text, max_words=100)

    print("\n原文长度:", len(test_text), "字符")
    print("摘要长度:", len(summary), "字符")
    print("\n生成的摘要:")
    print(summary)
    print("\n✅ DeepSeek 摘要生成测试通过！")

except Exception as e:
    print(f"\n❌ DeepSeek 摘要生成测试失败: {e}")
    import traceback
    traceback.print_exc()

print()

# =============================
# 测试 2: DeepSeek 标签提取
# =============================
print("测试 2: DeepSeek 标签提取")
print("-" * 70)

try:
    print("正在提取标签...")
    tags = deepseek_client.extract_tags(test_text)

    print("\n提取的标签数量:", len(tags))
    print("标签列表:", tags)

    # 验证标签数量
    assert 3 <= len(tags) <= 5, f"标签数量应该在 3-5 之间，实际: {len(tags)}"

    print("\n✅ DeepSeek 标签提取测试通过！")

except Exception as e:
    print(f"\n❌ DeepSeek 标签提取测试失败: {e}")
    import traceback
    traceback.print_exc()

print()

# =============================
# 测试 3: OpenAI Embedding
# =============================
print("测试 3: OpenAI Embedding 向量化")
print("-" * 70)

try:
    openai_client = OpenAIClient()

    print("正在生成向量...")
    embedding = openai_client.embed("人工智能是计算机科学的一个分支")

    print("\n向量维度:", len(embedding))
    print("向量前 5 个值:", embedding[:5])

    # 验证向量维度
    assert len(embedding) == 1536, f"向量维度应该是 1536，实际: {len(embedding)}"

    print("\n✅ OpenAI Embedding 测试通过！")

except Exception as e:
    print(f"\n❌ OpenAI Embedding 测试失败: {e}")
    import traceback
    traceback.print_exc()

print()

# =============================
# 测试 4: Embedder 统一接口
# =============================
print("测试 4: Embedder 统一接口")
print("-" * 70)

try:
    embedder = Embedder()

    # 测试文档级向量化
    print("正在生成文档级向量...")
    doc_vector = embedder.embed_document(test_text[:200])  # 使用部分文本

    print("文档向量维度:", doc_vector.shape)
    assert doc_vector.shape == (1536,), f"文档向量维度应该是 (1536,)，实际: {doc_vector.shape}"

    # 测试分块级向量化
    print("\n正在生成分块级向量...")
    chunk_vectors, chunks = embedder.embed_chunks(test_text, return_chunks=True)

    print("分块数量:", len(chunks))
    print("分块向量矩阵形状:", chunk_vectors.shape)
    print("第一个分块:", chunks[0][:50] + "...")

    # 测试余弦相似度
    print("\n正在计算余弦相似度...")
    query_vector = embedder.embed_document("人工智能")
    similarities = embedder.batch_cosine_similarity(query_vector, chunk_vectors)

    print("相似度分数:", similarities)
    print("最相似的分块索引:", similarities.argmax())

    print("\n✅ Embedder 统一接口测试通过！")

except Exception as e:
    print(f"\n❌ Embedder 统一接口测试失败: {e}")
    import traceback
    traceback.print_exc()

print()

# =============================
# 总结
# =============================
print("=" * 70)
print("测试完成！")
print("=" * 70)
print()
print("如果所有测试都通过，说明 AI 服务工作正常！")
print("您可以：")
print("  1. 调整 Prompt 模板（src/ai/prompts/）优化效果")
print("  2. 继续开发 Milestone 3")
print("  3. 将代码合并到主分支")
print()
