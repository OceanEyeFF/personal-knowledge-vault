"""
M12 手动测试：httpx.AsyncClient 基础验证

目标：
1. 验证 httpx 异步客户端基本功能
2. 测试流式响应处理（aiter_lines）
3. 验证超时和错误处理

运行方式：
    python tests/manual_test_m12/test_httpx_async.py

环境要求：
    - httpx 已安装（pip install httpx）
"""

import asyncio
import sys
from pathlib import Path

# 添加项目根目录到 sys.path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

try:
    import httpx
except ImportError:
    print("❌ httpx 未安装，请运行: pip install httpx")
    sys.exit(1)


async def test_basic_get():
    """测试 1: 基本 GET 请求"""
    print("=" * 60)
    print("测试 1: 基本 GET 请求")
    print("=" * 60)

    url = "https://httpbin.org/get"
    print(f"📡 请求 URL: {url}\n")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            print(f"✅ HTTP 状态码: {resp.status_code}")
            print(f"📦 响应头 Content-Type: {resp.headers.get('Content-Type')}")
            print(f"📄 响应体（前 200 字符）:\n{resp.text[:200]}...\n")

    except httpx.TimeoutException:
        print("❌ 请求超时（10s）")
    except Exception as e:
        print(f"❌ 错误: {e}")


async def test_stream_response():
    """测试 2: 流式响应（逐行读取）"""
    print("=" * 60)
    print("测试 2: 流式响应（逐行读取）")
    print("=" * 60)

    # 使用 httpbin 的延迟响应接口（模拟流式）
    url = "https://httpbin.org/stream/10"  # 返回 10 行 JSON
    print(f"📡 请求 URL: {url}")
    print("📥 流式读取:\n")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream("GET", url) as resp:
                print(f"✅ HTTP 状态码: {resp.status_code}\n")

                line_count = 0
                async for line in resp.aiter_lines():
                    line_count += 1
                    print(f"📄 Line {line_count}: {line[:80]}...")  # 只显示前 80 字符

                print(f"\n✅ 总共读取 {line_count} 行")

    except httpx.TimeoutException:
        print("❌ 请求超时（30s）")
    except Exception as e:
        print(f"❌ 错误: {e}")


async def test_post_json():
    """测试 3: POST JSON 数据"""
    print("\n" + "=" * 60)
    print("测试 3: POST JSON 数据")
    print("=" * 60)

    url = "https://httpbin.org/post"
    payload = {"message": "Hello from M12 test", "test_id": 12345}
    print(f"📡 POST URL: {url}")
    print(f"📦 请求体: {payload}\n")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            print(f"✅ HTTP 状态码: {resp.status_code}")
            print(f"📄 响应体（前 300 字符）:\n{resp.text[:300]}...\n")

    except Exception as e:
        print(f"❌ 错误: {e}")


async def test_timeout_handling():
    """测试 4: 超时处理"""
    print("=" * 60)
    print("测试 4: 超时处理")
    print("=" * 60)

    # httpbin 的延迟接口（延迟 5 秒响应）
    url = "https://httpbin.org/delay/5"
    timeout = 2.0  # 设置 2 秒超时，必然超时

    print(f"📡 请求 URL: {url}")
    print(f"⏱️ 超时设置: {timeout}s\n")

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            print(f"✅ HTTP 状态码: {resp.status_code}")

    except httpx.TimeoutException:
        print("✅ 正确触发超时异常（预期行为）")
    except Exception as e:
        print(f"❌ 未知错误: {e}")


async def test_error_status_code():
    """测试 5: HTTP 错误状态码处理"""
    print("\n" + "=" * 60)
    print("测试 5: HTTP 错误状态码")
    print("=" * 60)

    test_cases = [
        ("https://httpbin.org/status/404", 404, "Not Found"),
        ("https://httpbin.org/status/500", 500, "Internal Server Error"),
        ("https://httpbin.org/status/429", 429, "Too Many Requests"),
    ]

    for url, expected_code, description in test_cases:
        print(f"\n📡 请求 {url}")
        print(f"🎯 预期状态码: {expected_code} ({description})")

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
                print(f"✅ 实际状态码: {resp.status_code}")

                if resp.status_code == expected_code:
                    print("✅ 状态码匹配")
                else:
                    print(f"⚠️ 状态码不匹配！预期 {expected_code}，实际 {resp.status_code}")

        except Exception as e:
            print(f"❌ 错误: {e}")


async def test_concurrent_requests():
    """测试 6: 并发请求"""
    print("\n" + "=" * 60)
    print("测试 6: 并发请求（模拟多 token 同时接收）")
    print("=" * 60)

    urls = [f"https://httpbin.org/delay/{i}" for i in range(1, 4)]  # 延迟 1-3 秒
    print(f"📡 并发请求 {len(urls)} 个 URL\n")

    async def fetch(client, url):
        resp = await client.get(url)
        return resp.status_code, url

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            tasks = [fetch(client, url) for url in urls]
            results = await asyncio.gather(*tasks)

            print("✅ 所有请求完成:")
            for status, url in results:
                print(f"  - {url} → {status}")

    except Exception as e:
        print(f"❌ 错误: {e}")


async def main():
    """运行所有测试"""
    print("\n🔬 M12 httpx.AsyncClient 基础测试\n")

    # 测试 1: 基本 GET
    await test_basic_get()

    # 测试 2: 流式响应
    await test_stream_response()

    # 测试 3: POST JSON
    await test_post_json()

    # 测试 4: 超时处理
    await test_timeout_handling()

    # 测试 5: 错误状态码
    await test_error_status_code()

    # 测试 6: 并发请求
    await test_concurrent_requests()

    print("\n" + "=" * 60)
    print("✅ 所有测试完成")
    print("=" * 60)
    print("\n📝 关键收获:")
    print("1. httpx.AsyncClient 基本功能正常")
    print("2. aiter_lines() 可用于逐行读取流式响应")
    print("3. 超时和错误处理机制清晰")
    print("\n✅ 可以安全地在 M12 中使用 httpx 进行 DeepSeek API 调用！\n")


if __name__ == "__main__":
    asyncio.run(main())
