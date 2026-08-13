# 测试 Fixtures 和配置说明

## 📁 文件说明

### 离线处理器样本

这些文件用于离线单元、集成和黑盒测试，不需要真实网络请求：

| 路径 | 用途 | 格式 |
|------|------|------|
| `chat_sample.txt` / `chat_sample.json` | 通用聊天记录解析样本 | 纯文本 / JSON |
| `generic_sample.html` | 通用网页 HTML 样本 | HTML |
| `wechat_sample.html` / `wechat_chat_sample.txt` | 微信文章与聊天文本样本 | HTML / 纯文本 |
| `zhihu_sample.html` / `zhihu_login_wall.html` / `zhihu/zhihu reply sample.txt` | 知乎内容、登录墙和回复文本样本 | HTML / 纯文本 |
| `ai_chat/` | 合成 ChatGPT 与 DeepSeek 导出样本（HTML / Markdown） | HTML / Markdown |

### 版本化合同与评测 Fixtures

| 路径 | 用途 | 格式 |
|------|------|------|
| `w2/workflow/v1/states.v1.yaml` | Workflow 终态合同 fixture | YAML |
| `w2/retrieval/v1/contract.json` | Retrieval 响应合同 fixture | JSON |
| `w2/mcp/v1/matrix.yaml` | MCP stdio 合同矩阵 | YAML |
| `w2/chat/v1/scenarios.yaml` | GUI Chat 合同场景 | YAML |
| `w4/` | Artifact E2E 的离线文本、Chat prompt、语义 Provider 和 fixture manifest | JSON / YAML / 纯文本 |
| `w4/semantic-vector-index.v1/` | 固定向量索引及其 manifest；仅供 W4 离线场景使用 | hnswlib / JSON |
| `phase_b_5_4_min_regression.yaml` | Phase B 最小关系推理回归样例 | YAML |

### 测试辅助文件

| 路径 | 用途 | 格式 |
|------|------|------|
| `sample_data.py` | 创建确定性 SQLite 测试数据的辅助模块 | Python |
| `offline_direct_probe.py` | Direct Python 离线入口验证探针 | Python |

### 用户配置的测试 URL

| 文件 | 用途 | 格式 |
|------|------|------|
| `test_urls.json` | 集成测试真实链接配置 | JSON |

`test_urls.json` 属于 user-only live/数据出境材料，不进入默认 W2 回归或完成定义。Agent 不填写、不运行这些真实链接；默认只使用上表合成 fixture、`.data-test` 隔离根和注入的 `SafeFetcher` doubles，不读取真实 key、Provider 或 Vault。

---

## 🔧 使用 test_urls.json 配置真实链接

### 1. 打开配置文件

```bash
# 使用任何文本编辑器打开
nano tests/fixtures/test_urls.json
# 或
code tests/fixtures/test_urls.json
```

### 2. 替换示例链接

将配置文件中的示例链接替换为真实有效的链接：

```json
{
  "test_cases": {
    "wechat": [
      {
        "url": "https://mp.weixin.qq.com/s/your-real-article-url",
        "description": "你的微信文章描述",
        ...
      }
    ],
    "zhihu": [
      {
        "url": "https://www.zhihu.com/question/your-real-question-id",
        "description": "你的知乎问题描述",
        ...
      }
    ],
    "generic": [
      {
        "url": "https://your-blog.com/article",
        "description": "你的博客文章描述",
        ...
      }
    ]
  }
}
```

### 3. 推荐的测试链接来源

**微信文章**：
- 技术公众号：阮一峰的网络日志、美团技术团队
- 新闻类：人民日报、新华社
- 教程类：Python之禅、编程随想

**知乎内容**：
- 技术问题：搜索「分布式系统」「数据库原理」等
- 专栏文章：知乎官方技术专栏、个人技术博客

**通用网页**：
- 个人博客：GitHub Pages、Medium、简书
- 技术文档：Python、Node.js、React 官方文档
- 新闻网站：36kr、InfoQ

### 4. 运行集成测试

#### 默认离线模式（仅测试聊天记录处理器）

```powershell
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\processor-manual-offline -Command @(
  "python", "tests/manual_test_processors.py"
)
```

#### 使用配置文件模式（真实服务，当前阻塞）

`--use-config` 会加载本机 Provider 配置并可能抓取真实 URL，不属于 CAT-0
离线自动化。当前不得通过 wrapper 或裸 Python 执行；必须等待 U1/G8 user-only
launcher、明确授权和脱敏证据流程后，由用户在 disposable 场景中手动运行。

**输出示例**：

```
================================================================================
Milestone 3: 内容处理器 - 集成测试
================================================================================

[*] 从配置文件加载测试链接...

================================================================================
测试 1: 微信文章处理器
================================================================================

正在处理: https://mp.weixin.qq.com/s/your-real-url
[OK] 识别为: WechatProcessor
[OK] 处理成功:
  标题: 深入理解 Python 异步编程
  内容长度: 15234 字符
  作者: 技术博主
  发布时间: 2026-02-01 10:30
  来源类型: wechat

内容预览:
# 深入理解 Python 异步编程
...

================================================================================
测试总结
================================================================================
微信文章处理器: [OK] 通过
知乎内容处理器: [OK] 通过
通用网页处理器: [OK] 通过
聊天记录处理器: [OK] 通过

总计: 4/4 通过

[√] 所有启用的集成测试通过!
```

---

## 📝 配置文件格式说明

### test_urls.json 结构

```json
{
  "description": "说明信息",
  "version": "版本号",
  "last_updated": "最后更新日期",
  "test_cases": {
    "wechat": [
      {
        "url": "测试链接",
        "description": "链接描述",
        "expected": {
          "has_title": true,
          "has_author": true,
          "has_content": true,
          "min_content_length": 500
        },
        "notes": "备注信息"
      }
    ],
    "zhihu": [...],
    "generic": [...]
  },
  "usage_instructions": {...},
  "recommended_sources": {...}
}
```

### expected 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `has_title` | boolean | 是否应该有标题 |
| `has_author` | boolean | 是否应该有作者信息 |
| `has_publish_time` | boolean | 是否应该有发布时间 |
| `has_content` | boolean | 是否应该有内容 |
| `has_code_blocks` | boolean | 是否应该有代码块 |
| `min_content_length` | number | 最小内容长度（字符数） |

---

## 🎯 测试最佳实践

### 1. 选择合适的测试链接

- ✅ **推荐**：选择内容丰富、结构清晰的文章
- ✅ **推荐**：选择不需要登录即可访问的公开文章
- ✅ **推荐**：选择技术类、教程类文章（便于验证代码块解析）
- ❌ **避免**：选择需要登录或付费的文章
- ❌ **避免**：选择被墙或访问受限的网站
- ❌ **避免**：选择纯图片或视频内容

### 2. 验证测试结果

运行测试后，检查以下内容：

- ✅ 标题提取是否正确
- ✅ 作者信息是否完整
- ✅ 发布时间格式是否正确
- ✅ 内容是否完整（对比原文）
- ✅ Markdown 格式是否正确（标题、列表、代码块）
- ✅ 图片链接是否保留
- ✅ 特殊格式是否保留（如 LaTeX 公式）

### 3. 处理测试失败

如果测试失败，检查：

1. **网络连接**：确保可以访问目标网站
2. **反爬虫**：某些网站可能有严格的反爬虫策略
3. **页面结构变化**：网站可能更新了 HTML 结构
4. **链接失效**：文章可能已被删除或移动

**解决方案**：
- 更换其他测试链接
- 检查 `SafeFetcher` 返回的稳定 URL/SSRF/transport 错误码；不要改用浏览器或其他 HTTP client 绕过
- 查看详细错误日志

---

## 🔍 故障排查

### 问题 1: 配置文件无法加载

**错误信息**：
```
[X] 无法加载配置文件，使用默认测试
```

**解决方案**：
- 检查文件路径是否正确：`tests/fixtures/test_urls.json`
- 检查 JSON 格式是否正确（使用 JSON 验证工具）

### 问题 2: 所有测试被跳过

**输出**：
```
[!] 请先在 tests/fixtures/test_urls.json 中配置真实的链接
跳过此测试...
```

**解决方案**：
- 打开 `test_urls.json` 文件
- 将所有 `example` 和 `12345678` 替换为真实链接

### 问题 3: SafeFetcher 拒绝目标

**错误信息**：
```
ssrf_target_forbidden / ssrf_resolution_failed / url_invalid
```

**解决方案**：确认 URL 的 scheme、host、port、每跳 DNS 答案与 redirect 都满足公网目标策略。该拒绝是 fail-closed 安全合同；不要安装 Playwright、关闭校验或增加 requests/httpx 退路。

### 问题 4: DeepSeek API 错误

**错误信息**：
```
Warning: AI summary generation failed
```

**解决方案**：
- 检查 `config/local.yaml` 中的 LLM 配置是否正确
- 检查网络连接
- 此错误不会导致测试失败，会使用降级策略

---

## 📚 相关文档

- [MILESTONE3_TESTING_GUIDE.md](../../docs/history/milestones/MILESTONE3_TESTING_GUIDE.md) - 完整的测试指南
- [MILESTONE3_COMPLETE.md](../../docs/history/milestones/MILESTONE3_COMPLETE.md) - Milestone 3 完成报告
- [MILESTONE3_REVIEW.md](../../docs/history/milestones/MILESTONE3_REVIEW.md) - Milestone 3 审视报告

---

**创建者**: 浮浮酱 🐱
**创建日期**: 2026-02-14
**版本**: v1.0
