"""
M12 手动测试：DeepSeek API 流式调用验证

目标：
1. 验证 DeepSeek API 流式接口可用性
2. 理解 SSE 响应格式
3. 测试错误处理（401/429/500）

运行方式：
    python tests/manual_test_m12/test_deepseek_stream.py

环境要求：
    - DEEPSEEK_API_KEY 环境变量已配置
    - httpx 已安装（pip install httpx）
"""

import asyncio
import os
import sys
from pathlib import Path

# 添加项目根目录到 sys.path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

# 加载 .env 文件
try:
    from dotenv import load_dotenv
    env_file = project_root / ".env"
    if env_file.exists():
        load_dotenv(env_file)
        print(f"[配置] 已加载环境变量from {env_file}")
    else:
        print(f"[警告] .env 文件不存在: {env_file}")
except ImportError:
    print("[警告] python-dotenv 未安装，将直接读取系统环境变量")

try:
    import httpx
except ImportError:
    print("[错误] httpx 未安装，请运行: pip install httpx")
    sys.exit(1)


async def test_basic_stream():
    """测试 1: 最基本的流式请求"""
    print("=" * 60)
    print("测试 1: 基本流式请求")
    print("=" * 60)

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        print("[错误] 环境变量 DEEPSEEK_API_KEY 未设置")
        return

    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": "你好"}],
        "stream": True,
    }

    print(f"[请求] URL: {url}")
    print(f"[请求] 请求体: {payload}")
    print("\n[响应] 流式响应:\n")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                print(f"[成功] HTTP 状态码: {resp.status_code}\n")

                if resp.status_code != 200:
                    error_body = await resp.aread()
                    print(f"[错误] 错误响应: {error_body.decode('utf-8')}")
                    return

                # 解析 SSE 流
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]  # 去掉 "data: " 前缀

                        if data == "[DONE]":
                            print("\n[成功] 流式传输结束 [DONE]")
                            break

                        # 打印原始 JSON（调试用）
                        print(f"[数据] {data}")

                        # TODO: 解析 JSON，提取 delta.content
                        # import json
                        # chunk = json.loads(data)
                        # token = chunk["choices"][0]["delta"].get("content", "")
                        # if token:
                        #     print(token, end="", flush=True)

    except httpx.TimeoutException:
        print("[错误] 请求超时（30s）")
    except Exception as e:
        print(f"[错误] 未知错误: {e}")


async def test_error_handling():
    """测试 2: 错误处理（无效 API Key）"""
    print("\n" + "=" * 60)
    print("测试 2: 错误处理（无效 API Key）")
    print("=" * 60)

    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {
        "Authorization": "Bearer sk-invalid-key",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": "Hello"}],
        "stream": True,
    }

    print("[测试] 使用无效 API Key 测试...\n")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            print(f"HTTP 状态码: {resp.status_code}")
            print(f"响应体: {resp.text}")

            if resp.status_code == 401:
                print("[成功] 正确返回 401 Unauthorized")
            else:
                print(f"[警告] 预期 401，实际 {resp.status_code}")

    except Exception as e:
        print(f"[错误] {e}")


async def test_long_response():
    """测试 3: 长回复（测试流式稳定性）"""
    print("\n" + "=" * 60)
    print("测试 3: 长回复流式传输")
    print("=" * 60)

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        print("[错误] DEEPSEEK_API_KEY 未设置，跳过此测试")
        return

    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "user", "content": "请用 200 字介绍 Python asyncio 的核心概念"}
        ],
        "stream": True,
        "max_tokens": 500,
    }

    print("[测试] 请求较长回复（约 200 字）...\n")

    token_count = 0

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                if resp.status_code != 200:
                    print(f"[错误] HTTP {resp.status_code}")
                    return

                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        if data == "[DONE]":
                            break

                        token_count += 1
                        # 每 10 个 chunk 显示一次进度
                        if token_count % 10 == 0:
                            print(f"[进度] 已接收 {token_count} 个 chunk")

                print(f"\n[成功] 总共接收 {token_count} 个 chunk")

    except Exception as e:
        print(f"[错误] {e}")


async def main():
    """运行所有测试"""
    print("\n" + "=" * 60)
    print("M12 DeepSeek API 流式调用测试")
    print("=" * 60 + "\n")

    # 测试 1: 基本流式请求
    await test_basic_stream()

    # 测试 2: 错误处理
    await test_error_handling()

    # 测试 3: 长回复
    await test_long_response()

    print("\n" + "=" * 60)
    print("[完成] 所有测试完成")
    print("=" * 60)
    print("\n[下一步]")
    print("1. 将测试结果记录到 docs/milestones/M12_RESEARCH/02_DEEPSEEK_API_RESEARCH.md")
    print("2. 补充 SSE 格式的详细解析逻辑")
    print("3. 测试限流场景（429 错误）")


if __name__ == "__main__":
    asyncio.run(main())
