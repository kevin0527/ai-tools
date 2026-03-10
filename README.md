# AI Tools

个人 AI 辅助工具集合。

---

## news_digest.py

多源新闻摘要生成器。从多个新闻源抓取内容，调用 Claude AI 生成中文摘要，输出为美观的 HTML 页面并自动在浏览器中打开。

### 功能

- 支持 6 个新闻源：Hacker News、BBC News、Reddit、微博热搜、GitHub Trending、财经新闻
- 自动抓取原文正文（readability 提取）
- 使用 Claude Haiku 生成中文摘要
- 多线程并发抓取与总结
- 输出带标签页切换的响应式 HTML 报告

### 安装依赖

```bash
pip install requests lxml readability-lxml anthropic
```

### 配置

设置 Anthropic API Key（用于 AI 摘要，不配置则跳过摘要步骤）：

```bash
export ANTHROPIC_AUTH_TOKEN=sk-ant-...
```

或在脚本同级目录创建 `.env` 文件：

```
ANTHROPIC_AUTH_TOKEN=sk-ant-...
```

### 用法

```
python3 news_digest.py <source> [source ...] [-d DAYS] [--max N] [-o FILE]
```

**参数说明**

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `source` | 新闻源（可多个，见下方列表） | 必填 |
| `-d`, `--days` | 抓取最近几天的内容 | `1` |
| `--max` | 每个来源最多条数 | `20` |
| `-o`, `--output` | 输出 HTML 文件路径 | `/tmp/news_digest_YYYY-MM-DD.html` |
| `--no-fetch` | 跳过原文抓取 | — |
| `--no-summary` | 跳过 AI 摘要 | — |

**支持的新闻源**

| 源 | 说明 | 子选项示例 |
|----|------|-----------|
| `hn` | Hacker News 热门 | — |
| `bbc[:feed]` | BBC News | `bbc:top`（默认）/ `bbc:world` / `bbc:tech` / `bbc:sci` / `bbc:biz` |
| `reddit[:sub]` | Reddit 热帖 | `reddit:worldnews`（默认）/ `reddit:technology` / `reddit:science` |
| `weibo` | 微博热搜实时榜 | — |
| `github[:lang]` | GitHub Trending | `github:python` / `github:typescript` / `github:rust` |
| `finance[:cat]` | 财经新闻 | `finance:all`（默认）/ `finance:crypto` / `finance:gold` / `finance:stock` |

### 示例

```bash
# Hacker News，最近 1 天
python3 news_digest.py hn

# BBC 科技频道，最近 3 天
python3 news_digest.py bbc:tech -d 3

# GitHub Python 项目周榜
python3 news_digest.py github:python -d 7

# 多源汇总，每源最多 15 条
python3 news_digest.py hn bbc reddit weibo github -d 1 --max 15

# 只看标题，跳过抓取和摘要（速度最快）
python3 news_digest.py hn weibo --no-fetch --no-summary

# 财经三合一：加密货币 + 黄金 + 美股
python3 news_digest.py finance:crypto finance:gold finance:stock -d 1 --max 15
```

### 输出示例

生成的 HTML 页面包含：
- 顶部统计栏（新闻总数、时间范围、生成时间）
- 每个来源对应一个标签页
- 每条新闻显示标题、来源域名、AI 中文摘要、热度/评论数等元信息

### 扩展新来源

继承 `BaseSource` 并实现 `fetch()` 方法，然后在 `_make_source()` 中注册即可：

```python
class MySource(BaseSource):
    id    = "my"
    name  = "My Source"
    color = "#0066cc"
    icon  = "📌"

    def fetch(self, days: int, max_stories: int) -> list[Story]:
        # 返回 Story 对象列表
        ...
```
