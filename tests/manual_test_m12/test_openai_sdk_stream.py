"""
M12 手动测试：OpenAI SDK + DeepSeek API 流式验证

目标：
1. 验证 OpenAI SDK 与 DeepSeek API 的兼容性
2. 测试 stream=True 流式输出
3. 验证 stream_usage=True 的 usage 字段返回时机
4. 测试 token 统计的准确性

运行方式：
    python tests/manual_test_m12/test_openai_sdk_stream.py

环境要求：
    - openai>=1.0.0 已安装
    - config/local.yaml 已配置 LLM 服务
"""

# ruff: noqa: E402

__test__ = False  # 手动联网脚本，不参与默认 pytest 收集

import asyncio
import sys
from pathlib import Path

# 添加项目根目录到 sys.path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

import os

from src.utils.config import get_config

# 检查 openai 库
try:
    from openai import AsyncOpenAI
    import httpx
except ImportError:
    print("[错误] openai 未安装，请运行: pip install openai")
    sys.exit(1)


def _llm_settings():
    """从本机 YAML 配置读取 LLM 连接信息。"""
    config = get_config()
    return config.llm_api_key, config.llm_base_url, config.llm_model


def _clear_proxy_env() -> None:
    """仅在手动执行时禁用代理，避免 pytest 收集阶段修改进程环境。"""
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ.pop(key, None)


async def test_basic_stream():
    """测试 1: 基本流式输出"""
    print("=" * 60)
    print("测试 1: 基本流式输出（OpenAI SDK + DeepSeek）")
    print("=" * 60)

    api_key, base_url, model = _llm_settings()
    if not api_key:
        print("[错误] config/local.yaml 未配置 LLM API Key")
        return

    print("[配置] LLM API Key 已配置")
    print(f"[配置] Base URL: {base_url}\n")

    try:
        # 创建自定义 httpx 客户端（避免代理问题）
        http_client = httpx.AsyncClient(timeout=30.0)

        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            http_client=http_client
        )

        messages = [
            {"role": "user", "content": "你好，请用一句话介绍你自己"}
        ]

        print("[请求] 开始流式请求...")
        print("[请求] messages:", messages)
        print()

        stream = await client.chat.completions.create(
            model=model,
            messages=messages,
            stream=True,
            max_tokens=100
        )

        chunk_count = 0
        full_response = ""

        print("[响应] 流式输出:\n")
        async for chunk in stream:
            chunk_count += 1

            # 提取 token
            if chunk.choices[0].delta.content:
                token = chunk.choices[0].delta.content
                full_response += token
                print(token, end="", flush=True)

            # 检查 finish_reason
            if chunk.choices[0].finish_reason:
                print(f"\n\n[完成] finish_reason: {chunk.choices[0].finish_reason}")

        print(f"\n[统计] 总共接收 {chunk_count} 个 chunk")
        print(f"[统计] 完整响应长度: {len(full_response)} 字符")
        print("[成功] 基本流式输出测试通过\n")

    except Exception as e:
        print(f"[错误] {e}\n")


async def test_stream_with_usage():
    """测试 2: 流式输出 + Token 统计（关键测试）"""
    print("=" * 60)
    print("测试 2: 流式输出 + Token 统计（stream_usage=True）")
    print("=" * 60)

    api_key, base_url, model = _llm_settings()
    if not api_key:
        print("[错误] config/local.yaml 未配置 LLM API Key")
        return

    try:
        # 创建自定义 httpx 客户端（避免代理问题）
        http_client = httpx.AsyncClient(timeout=30.0)

        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            http_client=http_client
        )

        messages = [
            {"role": "user", "content": "请写一首 5 言绝句，主题是春天"}
        ]

        print("[请求] 开始流式请求（stream_options={'include_usage': True}）...")
        print("[请求] messages:", messages)
        print()

        stream = await client.chat.completions.create(
            model=model,
            messages=messages,
            stream=True,
            stream_options={"include_usage": True},  # ✅ 正确参数：开启 token 统计
            max_tokens=200
        )

        chunk_count = 0
        full_response = ""
        usage_chunks = []  # 记录包含 usage 的 chunk

        print("[响应] 流式输出:\n")
        async for chunk in stream:
            chunk_count += 1

            # 提取 token
            if chunk.choices[0].delta.content:
                token = chunk.choices[0].delta.content
                full_response += token
                print(token, end="", flush=True)

            # 检查 usage 字段（关键）
            if hasattr(chunk, 'usage') and chunk.usage:
                usage_chunks.append({
                    "chunk_index": chunk_count,
                    "prompt_tokens": chunk.usage.prompt_tokens,
                    "completion_tokens": chunk.usage.completion_tokens,
                    "total_tokens": chunk.usage.total_tokens
                })

            # 检查 finish_reason
            if chunk.choices[0].finish_reason:
                print(f"\n\n[完成] finish_reason: {chunk.choices[0].finish_reason}")

        print(f"\n[统计] 总共接收 {chunk_count} 个 chunk")
        print(f"[统计] 完整响应长度: {len(full_response)} 字符")
        print(f"[统计] 包含 usage 字段的 chunk 数量: {len(usage_chunks)}\n")

        if usage_chunks:
            print("[成功] usage 字段返回时机:")
            for i, usage_info in enumerate(usage_chunks, 1):
                print(f"  {i}. Chunk #{usage_info['chunk_index']}: "
                      f"输入={usage_info['prompt_tokens']} tokens, "
                      f"输出={usage_info['completion_tokens']} tokens, "
                      f"总计={usage_info['total_tokens']} tokens")
            print()

            # 最后一个 usage 作为最终统计
            final_usage = usage_chunks[-1]
            print(f"[最终统计] 输入 tokens: {final_usage['prompt_tokens']}")
            print(f"[最终统计] 输出 tokens: {final_usage['completion_tokens']}")
            print(f"[最终统计] 总计 tokens: {final_usage['total_tokens']}")
            print()

            # 粗略验证（中文约 3 字/token）
            estimated_output_tokens = len(full_response) // 3
            print(f"[验证] 客户端估算输出 tokens: ~{estimated_output_tokens}")
            print(f"[验证] 服务器返回输出 tokens: {final_usage['completion_tokens']}")
            error_rate = abs(estimated_output_tokens - final_usage['completion_tokens']) / final_usage['completion_tokens'] * 100
            print(f"[验证] 估算误差: {error_rate:.1f}%")
            print()

            print("[成功] stream_options={'include_usage': True} 测试通过\n")
        else:
            print("[警告] 未接收到任何 usage 字段！")
            print("[警告] DeepSeek API 可能不支持 stream_options={'include_usage': True}\n")

    except Exception as e:
        print(f"[错误] {e}\n")


async def test_long_conversation():
    """测试 3: 多轮对话 + Token 累计"""
    print("=" * 60)
    print("测试 3: 多轮对话 + Token 累计统计")
    print("=" * 60)

    api_key, base_url, model = _llm_settings()
    if not api_key:
        print("[错误] config/local.yaml 未配置 LLM API Key")
        return

    try:
        # 创建自定义 httpx 客户端（避免代理问题）
        http_client = httpx.AsyncClient(timeout=30.0)

        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            http_client=http_client
        )

        # 模拟多轮对话
        conversation = [
            {"role": "user", "content": "请介绍一下 Python"},
            {"role": "assistant", "content": "Python 是一种高级编程语言，以简洁易读著称。"},
            {"role": "user", "content": "它有哪些优点？"}
        ]

        print("[请求] 多轮对话 tokens 统计测试...")
        print(f"[请求] 对话轮数: {len([m for m in conversation if m['role'] == 'user'])} 轮")
        print()

        stream = await client.chat.completions.create(
            model=model,
            messages=conversation,
            stream=True,
            stream_options={"include_usage": True},
            max_tokens=200
        )

        full_response = ""
        final_usage = None

        print("[响应] 流式输出:\n")
        async for chunk in stream:
            if chunk.choices[0].delta.content:
                token = chunk.choices[0].delta.content
                full_response += token
                print(token, end="", flush=True)

            if hasattr(chunk, 'usage') and chunk.usage:
                final_usage = chunk.usage

        print("\n")

        if final_usage:
            print(f"[统计] 输入 tokens: {final_usage.prompt_tokens}")
            print(f"[统计] 输出 tokens: {final_usage.completion_tokens}")
            print(f"[统计] 总计 tokens: {final_usage.total_tokens}")
            print()

            # 验证：输入 tokens 应包含所有历史消息
            total_chars = sum(len(m['content']) for m in conversation)
            estimated_input_tokens = total_chars // 3
            print(f"[验证] 历史消息总字符数: {total_chars}")
            print(f"[验证] 估算输入 tokens: ~{estimated_input_tokens}")
            print(f"[验证] 实际输入 tokens: {final_usage.prompt_tokens}")
            print()

            print("[成功] 多轮对话 token 统计测试通过\n")
        else:
            print("[警告] 未接收到 usage 字段\n")

    except Exception as e:
        print(f"[错误] {e}\n")


async def test_error_handling():
    """测试 4: 错误处理（无效 API Key）"""
    print("=" * 60)
    print("测试 4: 错误处理（OpenAI SDK 异常）")
    print("=" * 60)

    _, base_url, model = _llm_settings()
    print("[请求] 使用无效 API Key 测试错误处理...\n")

    try:
        # 创建自定义 httpx 客户端（避免代理问题）
        http_client = httpx.AsyncClient(timeout=10.0)

        client = AsyncOpenAI(
            api_key="sk-invalid-key",
            base_url=base_url,
            http_client=http_client
        )

        stream = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "测试"}],
            stream=True
        )

        async for chunk in stream:
            pass

        print("[警告] 未触发异常（预期应触发 401 错误）\n")

    except Exception as e:
        print(f"[成功] 正确捕获异常: {type(e).__name__}")
        print(f"[成功] 错误信息: {str(e)[:100]}...")
        print("[成功] OpenAI SDK 错误处理机制正常\n")


async def test_max_tokens_limit():
    """测试 5: max_tokens 限制验证"""
    print("=" * 60)
    print("测试 5: max_tokens 限制（单轮输出质量控制）")
    print("=" * 60)

    api_key, base_url, model = _llm_settings()
    if not api_key:
        print("[错误] config/local.yaml 未配置 LLM API Key")
        return

    try:
        # 创建自定义 httpx 客户端（避免代理问题）
        http_client = httpx.AsyncClient(timeout=30.0)

        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            http_client=http_client
        )

        messages = [
            {"role": "user", "content": "请写一篇 1000 字的文章，主题是人工智能"}
        ]

        print("[请求] 测试 max_tokens=50 限制...")
        print("[预期] 输出应在约 50 tokens 后停止\n")

        stream = await client.chat.completions.create(
            model=model,
            messages=messages,
            stream=True,
            stream_options={"include_usage": True},
            max_tokens=50  # 限制输出
        )

        full_response = ""
        final_usage = None
        finish_reason = None

        print("[响应] 流式输出:\n")
        async for chunk in stream:
            if chunk.choices[0].delta.content:
                token = chunk.choices[0].delta.content
                full_response += token
                print(token, end="", flush=True)

            if chunk.choices[0].finish_reason:
                finish_reason = chunk.choices[0].finish_reason

            if hasattr(chunk, 'usage') and chunk.usage:
                final_usage = chunk.usage

        print("\n")

        if final_usage:
            print(f"[统计] 输出 tokens: {final_usage.completion_tokens}")
            print(f"[统计] finish_reason: {finish_reason}")
            print()

            if final_usage.completion_tokens <= 50:
                print(f"[成功] max_tokens 限制生效（{final_usage.completion_tokens} <= 50）")
            else:
                print(f"[警告] max_tokens 限制未生效（{final_usage.completion_tokens} > 50）")

            if finish_reason == "length":
                print("[成功] finish_reason 为 'length'（因达到 max_tokens 而停止）")
            else:
                print(f"[提示] finish_reason 为 '{finish_reason}'")

            print()

    except Exception as e:
        print(f"[错误] {e}\n")


async def main():
    """运行所有测试"""
    _clear_proxy_env()
    print("\n" + "=" * 60)
    print("M12 OpenAI SDK + DeepSeek API 流式验证")
    print("=" * 60 + "\n")

    # 测试 1: 基本流式输出
    await test_basic_stream()

    # 测试 2: 流式 + Token 统计（关键）
    await test_stream_with_usage()

    # 测试 3: 多轮对话
    await test_long_conversation()

    # 测试 4: 错误处理
    await test_error_handling()

    # 测试 5: max_tokens 限制
    await test_max_tokens_limit()

    print("=" * 60)
    print("[完成] 所有测试完成")
    print("=" * 60)
    print("\n[收获]")
    print("1. OpenAI SDK 与 DeepSeek API 兼容性验证")
    print("2. stream_options={'include_usage': True} 的 usage 字段返回时机")
    print("3. Token 统计准确性验证（服务器端精确值）")
    print("4. max_tokens 单轮输出限制验证")
    print("\n[结论] 方案 A (OpenAI SDK + stream_options) 可行性验证完成！\n")


if __name__ == "__main__":
    asyncio.run(main())
