# 测试 Fixtures 和配置说明

## 📁 文件说明

### 测试 Fixtures（测试样本文件）

这些文件用于离线单元测试，不需要真实的网络请求：

| 文件 | 用途 | 格式 |
|------|------|------|
| `chat_sample.txt` | 纯文本格式聊天记录样本 | 纯文本 |
| `chat_sample.json` | JSON 格式聊天记录样本 | JSON |
| `generic_sample.html` | 通用网页 HTML 样本 | HTML |
| `wechat_sample.html` | 微信文章 HTML 样本 | HTML |
| `zhihu_sample.html` | 知乎内容 HTML 样本 | HTML |

### 测试配置文件

| 文件 | 用途 | 格式 |
|------|------|------|
| `test_urls.json` | 集成测试真实链接配置 | JSON |

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

#### 默认模式（仅测试聊天记录处理器）

```bash
python tests/manual_test_processors.py
```

#### 使用配置文件模式（测试所有处理器）

```bash
python tests/manual_test_processors.py --use-config
```

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
- 检查 Playwright 浏览器是否正常工作
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

### 问题 3: Playwright 浏览器未安装

**错误信息**：
```
Error: Executable doesn't exist at /path/to/chromium
```

**解决方案**：
```bash
python -m playwright install chromium
```

### 问题 4: DeepSeek API 错误

**错误信息**：
```
Warning: AI summary generation failed
```

**解决方案**：
- 检查 `.env` 文件中的 `DEEPSEEK_API_KEY` 是否正确
- 检查网络连接
- 此错误不会导致测试失败，会使用降级策略

---

## 📚 相关文档

- [MILESTONE3_TESTING_GUIDE.md](../../docs/MILESTONE3_TESTING_GUIDE.md) - 完整的测试指南
- [MILESTONE3_COMPLETE.md](../../docs/MILESTONE3_COMPLETE.md) - Milestone 3 完成报告
- [MILESTONE3_REVIEW.md](../../docs/MILESTONE3_REVIEW.md) - Milestone 3 审视报告

---

**创建者**: 浮浮酱 🐱
**创建日期**: 2026-02-14
**版本**: v1.0
