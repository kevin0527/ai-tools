#!/usr/bin/env python3
"""
Multi-source News Digest Generator

Supported sources: hn, bbc, reddit, weibo, github
New sources can be added by subclassing BaseSource.

Usage:
    python3 news_digest.py <source> [source ...] [-d DAYS] [--max N] [-o FILE]

Examples:
    python3 news_digest.py hn                      # HN only, last 1 day
    python3 news_digest.py bbc -d 3               # BBC, last 3 days
    python3 news_digest.py github                  # GitHub Trending daily
    python3 news_digest.py github:python -d 7     # Python repos, weekly
    python3 news_digest.py hn bbc reddit weibo github -d 1 --max 15

Source options (append with colon):
    bbc:top / bbc:world / bbc:tech / bbc:sci / bbc:biz
    reddit:worldnews / reddit:technology / reddit:science / ...
    github:<language>  e.g. github:python / github:typescript / github:rust
"""

import sys, os, re, json, time, datetime, argparse, webbrowser
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import requests
from lxml import html as lhtml
from readability import Document


# ─── Constants ───────────────────────────────────────────────────────────────

FETCH_TIMEOUT   = 10
MAX_ARTICLE_CHARS = 4000
SUMMARY_WORKERS = 8


# ─── Story schema (normalized across all sources) ────────────────────────────

@dataclass
class Story:
    title:        str
    url:          str
    source:       str           # source id: "hn" / "bbc" / "reddit" / "weibo"
    created_at:   int  = 0      # unix timestamp
    points:       int  = 0      # upvotes / heat score
    comments:     int  = 0
    comments_url: str  = ""
    author:       str  = ""
    description:  str  = ""     # brief blurb from feed
    article_text: str  = ""     # extracted full text
    summary:      str  = ""     # Claude Chinese summary


# ─── Source base class ────────────────────────────────────────────────────────

class BaseSource(ABC):
    id:    str   # short identifier used on CLI
    name:  str   # display name
    color: str   # accent hex color
    icon:  str   # emoji

    @abstractmethod
    def fetch(self, days: int, max_stories: int) -> list[Story]:
        """Return stories from the last `days` days, up to `max_stories`."""

    def summary_hint(self) -> str:
        """Extra context injected into the Claude prompt."""
        return ""

    def fetch_text(self, story: "Story") -> str:
        """Return article text for a story. Override for custom fetching logic."""
        return fetch_article_text(story.url)


# ─── Source implementations ───────────────────────────────────────────────────

class HNSource(BaseSource):
    id    = "hn"
    name  = "Hacker News"
    color = "#ff6600"
    icon  = "🔥"

    def fetch(self, days: int, max_stories: int) -> list[Story]:
        since = int(time.time()) - days * 24 * 3600
        results, page = [], 0
        while len(results) < max_stories:
            params = {"tags": "front_page", "hitsPerPage": 50, "page": page,
                      "numericFilters": f"created_at_i>{since}"}
            resp = requests.get("https://hn.algolia.com/api/v1/search",
                                params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            hits = data.get("hits", [])
            if not hits:
                break
            results.extend(hits)
            page += 1
            if page >= data.get("nbPages", 1):
                break
        results.sort(key=lambda x: x.get("points", 0), reverse=True)
        return [Story(
            title    = h.get("title", ""),
            url      = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
            source   = self.id,
            created_at = h.get("created_at_i", 0),
            points   = h.get("points", 0),
            comments = h.get("num_comments", 0),
            comments_url = f"https://news.ycombinator.com/item?id={h.get('objectID')}",
            author   = h.get("author", ""),
        ) for h in results[:max_stories]]

    def summary_hint(self) -> str:
        return "这是 Hacker News 上的一篇热门技术/科技/创业文章。"


class BBCSource(BaseSource):
    id    = "bbc"
    name  = "BBC News"
    color = "#bb1919"
    icon  = "📰"

    # Available feeds; default uses top stories
    FEEDS = {
        "top":    "https://feeds.bbci.co.uk/news/rss.xml",
        "world":  "https://feeds.bbci.co.uk/news/world/rss.xml",
        "tech":   "https://feeds.bbci.co.uk/news/technology/rss.xml",
        "sci":    "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
        "biz":    "https://feeds.bbci.co.uk/news/business/rss.xml",
    }

    def __init__(self, feed: str = "top"):
        self._feed_url = self.FEEDS.get(feed, self.FEEDS["top"])

    def fetch(self, days: int, max_stories: int) -> list[Story]:
        since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
        resp = requests.get(self._feed_url,
                            headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        ns = {"media": "http://search.yahoo.com/mrss/"}
        stories = []
        for item in root.findall(".//item"):
            title   = _xml_text(item, "title")
            link    = _xml_text(item, "link")
            pub_raw = _xml_text(item, "pubDate")
            desc    = re.sub(r"<[^>]+>", "", _xml_text(item, "description"))
            try:
                pub_dt = parsedate_to_datetime(pub_raw).astimezone(datetime.timezone.utc)
            except Exception:
                pub_dt = since  # include if unparseable
            if pub_dt < since:
                continue
            stories.append(Story(
                title       = title,
                url         = link,
                source      = self.id,
                created_at  = int(pub_dt.timestamp()),
                description = desc,
            ))
        stories.sort(key=lambda s: s.created_at, reverse=True)
        return stories[:max_stories]

    def summary_hint(self) -> str:
        return "这是 BBC News 的一篇国际新闻报道。"


class RedditSource(BaseSource):
    id    = "reddit"
    name  = "Reddit"
    color = "#ff4500"
    icon  = "👾"

    def __init__(self, subreddit: str = "worldnews"):
        self._sub = subreddit

    def fetch(self, days: int, max_stories: int) -> list[Story]:
        since = time.time() - days * 24 * 3600
        url = f"https://www.reddit.com/r/{self._sub}/hot.json"
        headers = {"User-Agent": "NewsDigest/1.0 (personal digest script)"}
        resp = requests.get(url, headers=headers, params={"limit": 100}, timeout=15)
        resp.raise_for_status()
        posts = resp.json()["data"]["children"]
        stories = []
        for p in posts:
            d = p["data"]
            if d.get("created_utc", 0) < since:
                continue
            if d.get("is_self") and not d.get("selftext"):
                # empty self-post, skip
                continue
            # prefer external URL; fall back to reddit thread
            article_url = d.get("url", "")
            if article_url.startswith("https://www.reddit.com"):
                article_url = f"https://www.reddit.com{d.get('permalink', '')}"
            stories.append(Story(
                title        = d.get("title", ""),
                url          = article_url,
                source       = self.id,
                created_at   = int(d.get("created_utc", 0)),
                points       = d.get("score", 0),
                comments     = d.get("num_comments", 0),
                comments_url = f"https://www.reddit.com{d.get('permalink', '')}",
                author       = d.get("author", ""),
                description  = d.get("selftext", "")[:300],
            ))
        stories.sort(key=lambda s: s.points, reverse=True)
        return stories[:max_stories]

    def summary_hint(self) -> str:
        return f"这是 Reddit r/{self._sub} 上的一篇热门帖子。"


class WeiboSource(BaseSource):
    id    = "weibo"
    name  = "微博热搜"
    color = "#e6162d"
    icon  = "🔴"

    def fetch(self, days: int, max_stories: int) -> list[Story]:
        # Hot search is always "current" — days param is informational only
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://weibo.com/",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        resp = requests.get("https://weibo.com/ajax/side/hotSearch",
                            headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        d = data.get("data", {})
        # API returns either "realtime" or "band_list" depending on version
        band_list = d.get("realtime") or d.get("band_list") or []
        stories = []
        now = int(time.time())
        for item in band_list[:max_stories]:
            word  = item.get("word", "")
            num   = item.get("num", 0)
            rank  = item.get("rank", 0) + 1
            label = item.get("label_name", "")
            # Some items link to an external article
            ext_url = item.get("word_scheme", "") or item.get("url", "")
            if ext_url and not ext_url.startswith("http"):
                ext_url = ""
            search_url = f"https://s.weibo.com/weibo?q={quote_plus(word)}&Refer=index"
            stories.append(Story(
                title       = f"{'🔥 ' if label in ('热','爆') else ''}{word}",
                url         = ext_url or search_url,
                source      = self.id,
                created_at  = now,
                points      = num,
                description = f"热搜榜第 {rank} 位 · 热度 {num:,}",
            ))
        return stories

    def summary_hint(self) -> str:
        return (
            "这是微博热搜榜上的一个热点话题关键词。"
            "请根据你的知识，用中文简要解释这个话题的背景、起因或当前热议原因（2-3句话）。"
            "若无法确认具体事件，请说明该词的一般含义或可能的背景。"
        )


class GitHubSource(BaseSource):
    id    = "github"
    name  = "GitHub Trending"
    color = "#24292f"
    icon  = "⭐"

    # days → GitHub's "since" parameter
    _SINCE = {1: "daily", 7: "weekly", 30: "monthly"}

    def __init__(self, language: str = ""):
        self._language = language  # e.g. "python", "typescript", ""

    @property
    def name(self) -> str:
        suffix = f" · {self._language}" if self._language else ""
        return f"GitHub Trending{suffix}"

    def _since(self, days: int) -> str:
        if days <= 1:  return "daily"
        if days <= 7:  return "weekly"
        return "monthly"

    def fetch(self, days: int, max_stories: int) -> list[Story]:
        since    = self._since(days)
        lang_seg = f"/{self._language}" if self._language else ""
        url      = f"https://github.com/trending{lang_seg}?since={since}"
        headers  = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()

        tree     = lhtml.fromstring(resp.text)
        articles = tree.cssselect("article.Box-row")
        stories  = []
        for article in articles[:max_stories]:
            # ── repo link & name ──
            links = article.cssselect("h2 a, h3 a")
            if not links:
                continue
            href      = links[0].get("href", "").strip()          # /owner/repo
            repo_name = re.sub(r"\s+", "", links[0].text_content())  # "owner/repo"

            # ── description ──
            desc_els = article.cssselect("p")
            desc = desc_els[0].text_content().strip() if desc_els else ""

            # ── language ──
            lang_els = article.cssselect("[itemprop='programmingLanguage']")
            language = lang_els[0].text_content().strip() if lang_els else ""

            # ── total stars ──
            star_link = article.cssselect("a[href$='/stargazers']")
            stars_raw = star_link[0].text_content().strip() if star_link else "0"
            total_stars = int(re.sub(r"[^\d]", "", stars_raw) or 0)

            # ── stars this period ──
            period_span = article.cssselect(".float-sm-right")
            period_text = period_span[0].text_content().strip() if period_span else ""
            m = re.search(r"([\d,]+)\s+star", period_text)
            period_stars = int(m.group(1).replace(",", "")) if m else 0

            meta = " · ".join(filter(None, [
                language,
                f"⭐ {total_stars:,}" if total_stars else "",
                f"+{period_stars:,} {since}" if period_stars else "",
            ]))

            stories.append(Story(
                title       = repo_name,
                url         = f"https://github.com{href}",
                source      = self.id,
                created_at  = int(time.time()),
                points      = period_stars or total_stars,
                description = desc,
                author      = meta,
            ))
        return stories

    def fetch_text(self, story: "Story") -> str:
        """Fetch README.md instead of the repo HTML page."""
        owner_repo = story.url.replace("https://github.com/", "").strip("/")
        headers    = {"User-Agent": "Mozilla/5.0"}
        for branch in ("main", "master"):
            try:
                raw_url = (f"https://raw.githubusercontent.com/"
                           f"{owner_repo}/{branch}/README.md")
                resp = requests.get(raw_url, headers=headers,
                                    timeout=FETCH_TIMEOUT)
                if resp.status_code == 200:
                    text = resp.text
                    # strip badge lines and code blocks for cleaner Claude input
                    text = re.sub(r"!\[.*?\]\(.*?\)", "", text)         # images
                    text = re.sub(r"```[\s\S]*?```", "", text)           # code blocks
                    text = re.sub(r"`[^`]+`", "", text)                  # inline code
                    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)  # links
                    text = re.sub(r"[ \t]*\|.*\|[ \t]*\n", "", text)    # tables
                    text = re.sub(r"\s{3,}", "\n\n", text).strip()
                    return text[:MAX_ARTICLE_CHARS]
            except Exception:
                continue
        return ""

    def summary_hint(self) -> str:
        lang = f"（{self._language}）" if self._language else ""
        return (
            f"这是 GitHub Trending 上的一个热门开源项目{lang}。"
            "请用中文总结该项目的核心功能、技术亮点和适用场景（2-4句话）。"
        )


# ─── Registry ─────────────────────────────────────────────────────────────────

def _make_source(sid: str) -> BaseSource:
    """Parse optional config from source id like 'reddit:technology' or 'bbc:world'."""
    parts = sid.split(":", 1)
    name  = parts[0]
    arg   = parts[1] if len(parts) > 1 else None
    if name == "hn":
        return HNSource()
    if name == "bbc":
        return BBCSource(arg or "top")
    if name == "reddit":
        return RedditSource(arg or "worldnews")
    if name == "weibo":
        return WeiboSource()
    if name == "github":
        return GitHubSource(arg or "")
    raise ValueError(f"Unknown source: '{name}'. Available: hn, bbc, reddit, weibo, github")


# ─── API key helper ───────────────────────────────────────────────────────────

def _get_api_key() -> str:
    key = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    if key:
        return key
    for env_path in [
        os.path.join(os.getcwd(), ".env"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    ]:
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("ANTHROPIC_AUTH_TOKEN="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


# ─── Article fetcher ──────────────────────────────────────────────────────────

_SKIP_DOMAINS = {"news.ycombinator.com", "reddit.com", "www.reddit.com",
                 "s.weibo.com", "weibo.com"}

def fetch_article_text(url: str) -> str:
    if not url:
        return ""
    domain = re.match(r"https?://(?:www\.)?([^/]+)", url)
    if domain and domain.group(1) in _SKIP_DOMAINS:
        return ""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; NewsDigest/1.0)"}
        resp = requests.get(url, headers=headers, timeout=FETCH_TIMEOUT, allow_redirects=True)
        if "text/html" not in resp.headers.get("Content-Type", ""):
            return ""
        doc  = Document(resp.text)
        text = re.sub(r"<[^>]+>", " ", doc.summary())
        text = re.sub(r"\s+", " ", text).strip()
        return text[:MAX_ARTICLE_CHARS]
    except Exception:
        return ""


# ─── Claude summarizer ────────────────────────────────────────────────────────

def summarize_stories(stories: list[Story], source: BaseSource) -> None:
    """Fill story.summary in-place via Claude API."""
    api_key = _get_api_key()
    if not api_key:
        print(f"  [warn] ANTHROPIC_AUTH_TOKEN 未设置，跳过 AI 总结", file=sys.stderr)
        return

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    hint   = source.summary_hint()

    def summarize_one(story: Story) -> None:
        if story.article_text:
            prompt = (
                f"{hint}\n\n"
                f"标题：{story.title}\n"
                f"来源：{story.url}\n\n"
                f"文章内容（节选）：\n{story.article_text}\n\n"
                "请用中文，用2-4句话简洁总结这篇文章的核心内容和价值。"
                "只输出总结，不要任何前缀或说明。"
            )
        elif story.description:
            prompt = (
                f"{hint}\n\n"
                f"标题：{story.title}\n"
                f"简介：{story.description}\n\n"
                "请用中文，用1-3句话总结或解释这个话题。"
                "只输出总结，不要任何前缀或说明。"
            )
        else:
            prompt = (
                f"{hint}\n\n"
                f"标题/关键词：{story.title}\n\n"
                "请用中文，用1-2句话解释这个话题的背景或意义。"
                "只输出总结，不要任何前缀或说明。"
            )
        try:
            msg = client.messages.create(
                model      = "claude-haiku-4-5-20251001",
                max_tokens = 250,
                messages   = [{"role": "user", "content": prompt}],
            )
            story.summary = msg.content[0].text.strip()
        except Exception as e:
            print(f"\n  [warn] Claude error for '{story.title[:40]}': {e}", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=SUMMARY_WORKERS) as pool:
        futures = list(pool.map(summarize_one, stories))
    _ = futures  # map is eager; results already applied in-place


# ─── HTML rendering ───────────────────────────────────────────────────────────

def _xml_text(el, tag: str) -> str:
    child = el.find(tag)
    return (child.text or "").strip() if child is not None else ""

def format_time(ts: int) -> str:
    if not ts:
        return ""
    dt   = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    diff = datetime.datetime.now(datetime.timezone.utc) - dt
    h    = int(diff.total_seconds() / 3600)
    if h < 1:
        return f"{int(diff.total_seconds()/60)} 分钟前"
    if h < 24:
        return f"{h} 小时前"
    return f"{diff.days} 天前"

def extract_domain(url: str) -> str:
    m = re.match(r"https?://(?:www\.)?([^/]+)", url or "")
    return m.group(1) if m else ""

def _render_story(rank: int, s: Story, accent: str) -> str:
    domain      = extract_domain(s.url)
    domain_html = f'<div class="story-domain">{domain}</div>' if domain else ""
    summary_html = (
        f'<div class="story-summary" style="border-left-color:{accent}">'
        f'{s.summary}</div>'
    ) if s.summary else ""

    meta_parts = []
    if s.points:
        meta_parts.append(f'<span class="badge">▲ {s.points:,}</span>')
    if s.comments and s.comments_url:
        meta_parts.append(
            f'<span class="badge">💬 <a href="{s.comments_url}" target="_blank">'
            f'{s.comments:,} 条评论</a></span>'
        )
    elif s.description and not s.summary:
        meta_parts.append(f'<span class="badge muted">{s.description[:60]}</span>')
    if s.created_at:
        meta_parts.append(f'<span class="badge">🕐 {format_time(s.created_at)}</span>')
    if s.author:
        meta_parts.append(f'<span class="badge">👤 {s.author}</span>')

    return f"""
  <div class="story">
    <div class="story-header">
      <span class="story-rank" style="color:{accent};background:{accent}18">{rank}</span>
      <a class="story-title" href="{s.url}" target="_blank" rel="noopener">{s.title}</a>
    </div>
    {domain_html}
    {summary_html}
    <div class="story-meta">{''.join(meta_parts)}</div>
  </div>"""

def render_html(sources_stories: list[tuple[BaseSource, list[Story]]], days: int) -> str:
    now   = datetime.datetime.now()
    since = now - datetime.timedelta(days=days)
    date_range = f"{since.strftime('%Y-%m-%d')} ~ {now.strftime('%Y-%m-%d')}"
    total = sum(len(stories) for _, stories in sources_stories)

    # ── Tab buttons ──
    tab_btns = []
    for src, stories in sources_stories:
        tab_btns.append(
            f'<button class="tab-btn" data-tab="{src.id}" '
            f'style="--c:{src.color}" onclick="showTab(\'{src.id}\')">'
            f'{src.icon} {src.name} <span class="cnt">{len(stories)}</span></button>'
        )
    tab_btns[0] = tab_btns[0].replace('class="tab-btn"', 'class="tab-btn active"')

    # ── Tab panels ──
    panels = []
    for src, stories in sources_stories:
        cards = "".join(_render_story(i + 1, s, src.color) for i, s in enumerate(stories))
        hidden = "" if src == sources_stories[0][0] else ' style="display:none"'
        panels.append(f'<div id="tab-{src.id}" class="tab-panel"{hidden}>{cards}</div>')

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>News Digest · {now.strftime('%Y-%m-%d')}</title>
<style>
  :root {{ --bg:#f5f5f0; --card:#fff; --text:#1a1a1a; --muted:#666; --border:#e0e0e0; }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
          background:var(--bg); color:var(--text); line-height:1.6; padding:24px 16px; }}
  .container {{ max-width:880px; margin:0 auto; }}

  header {{ background:linear-gradient(135deg,#1a1a2e,#16213e);
            color:#fff; padding:22px 28px; border-radius:14px; margin-bottom:20px; }}
  header h1 {{ font-size:1.4rem; font-weight:700; letter-spacing:-.3px; }}
  header .meta {{ font-size:.85rem; opacity:.75; margin-top:5px; }}
  .stats {{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom:20px; }}
  .stat {{ background:var(--card); border:1px solid var(--border); border-radius:8px;
           padding:8px 16px; font-size:.85rem; color:var(--muted); }}
  .stat strong {{ color:var(--text); }}

  .tabs {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:20px; }}
  .tab-btn {{ border:none; cursor:pointer; border-radius:8px; padding:8px 16px;
              font-size:.88rem; font-weight:600; background:var(--card);
              border:2px solid var(--border); color:var(--muted);
              transition:all .15s; display:flex; align-items:center; gap:6px; }}
  .tab-btn:hover {{ border-color:var(--c); color:var(--c); }}
  .tab-btn.active {{ background:var(--c); border-color:var(--c); color:#fff; }}
  .tab-btn .cnt {{ background:rgba(255,255,255,.25); border-radius:10px;
                   padding:1px 7px; font-size:.78rem; }}
  .tab-btn:not(.active) .cnt {{ background:#f0f0f0; color:#888; }}

  .story {{ background:var(--card); border:1px solid var(--border); border-radius:12px;
            padding:18px 22px; margin-bottom:14px; transition:box-shadow .15s; }}
  .story:hover {{ box-shadow:0 4px 18px rgba(0,0,0,.08); }}
  .story-header {{ display:flex; align-items:flex-start; gap:10px; }}
  .story-rank {{ flex-shrink:0; width:26px; height:26px; border-radius:6px;
                 text-align:center; line-height:26px; font-size:.78rem; font-weight:700; }}
  .story-title {{ font-size:1rem; font-weight:600; color:var(--text);
                  text-decoration:none; flex:1; }}
  .story-title:hover {{ opacity:.75; }}
  .story-domain {{ font-size:.78rem; color:var(--muted); margin:4px 0 0 36px; }}
  .story-summary {{ margin:10px 0 10px 36px; font-size:.9rem; color:#333;
                    background:#f8f8f6; border-left:3px solid #ccc;
                    padding:8px 12px; border-radius:0 6px 6px 0; }}
  .story-meta {{ display:flex; gap:12px; margin:8px 0 0 36px;
                 font-size:.8rem; color:var(--muted); flex-wrap:wrap; }}
  .story-meta a {{ color:var(--muted); text-decoration:none; }}
  .story-meta a:hover {{ opacity:.7; }}
  .badge {{ display:inline-flex; align-items:center; gap:3px; }}

  footer {{ text-align:center; color:var(--muted); font-size:.8rem;
            margin-top:32px; padding-top:20px; border-top:1px solid var(--border); }}
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>📡 News Digest</h1>
    <div class="meta">{date_range} &nbsp;·&nbsp; {', '.join(s.name for s, _ in sources_stories)}</div>
  </header>
  <div class="stats">
    <div class="stat"><strong>{total}</strong> 条新闻</div>
    <div class="stat">覆盖 <strong>{days}</strong> 天</div>
    <div class="stat">生成于 <strong>{now.strftime('%Y-%m-%d %H:%M')}</strong></div>
  </div>
  <div class="tabs">{''.join(tab_btns)}</div>
  {''.join(panels)}
  <footer>Powered by News APIs &amp; Claude · news_digest.py</footer>
</div>
<script>
function showTab(id) {{
  document.querySelectorAll('.tab-panel').forEach(p => p.style.display='none');
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-'+id).style.display='';
  document.querySelector('[data-tab="'+id+'"]').classList.add('active');
}}
</script>
</body>
</html>"""


# ─── Pipeline ─────────────────────────────────────────────────────────────────

def run_source(src: BaseSource, days: int, max_stories: int,
               no_fetch: bool, no_summary: bool) -> tuple[BaseSource, list[Story]]:
    print(f"\n[{src.icon} {src.name}] Fetching...", file=sys.stderr)
    try:
        stories = src.fetch(days, max_stories)
    except Exception as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        return src, []
    print(f"  Got {len(stories)} stories", file=sys.stderr)

    if not no_fetch and stories:
        print(f"  Fetching article text...", file=sys.stderr)
        def fetch_one(s: Story) -> None:
            s.article_text = src.fetch_text(s)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(fetch_one, stories))
        fetched = sum(1 for s in stories if s.article_text)
        print(f"  Article text: {fetched}/{len(stories)}", file=sys.stderr)

    if not no_summary and stories:
        print(f"  Summarizing with Claude...", file=sys.stderr)
        summarize_stories(stories, src)
        done = sum(1 for s in stories if s.summary)
        print(f"  Summaries: {done}/{len(stories)}", file=sys.stderr)

    return src, stories


# ─── Main ─────────────────────────────────────────────────────────────────────

AVAILABLE = ["hn", "bbc", "reddit", "weibo"]


def main():
    p = argparse.ArgumentParser(
        description="Multi-source News Digest",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Available sources:\n"
            "  hn               Hacker News front page\n"
            "  bbc[:feed]       BBC News  (feed: top*/world/tech/sci/biz)\n"
            "  reddit[:sub]     Reddit    (sub: worldnews*/technology/science/…)\n"
            "  weibo            微博热搜\n"
            "  github[:lang]    GitHub Trending  (lang: python/typescript/rust/…)\n"
        ),
    )
    p.add_argument("sources", nargs="+", metavar="SOURCE",
                   help="One or more sources to fetch (e.g. hn bbc reddit weibo)")
    p.add_argument("-d", "--days", type=int, default=1,
                   help="Days to look back (default: 1)")
    p.add_argument("--max", type=int, default=20,
                   help="Max stories per source (default: 20)")
    p.add_argument("-o", "--output", default="",
                   help="Output HTML file (default: news_digest_YYYY-MM-DD.html)")
    p.add_argument("--no-fetch",   action="store_true", help="Skip article fetching")
    p.add_argument("--no-summary", action="store_true", help="Skip AI summarization")
    args = p.parse_args()

    days        = max(1, args.days)
    source_ids  = args.sources
    output_file = args.output or f"/tmp/news_digest_{datetime.date.today()}.html"

    sources = []
    for sid in source_ids:
        try:
            sources.append(_make_source(sid))
        except ValueError as e:
            print(f"[error] {e}", file=sys.stderr)
            sys.exit(1)

    results: list[tuple[BaseSource, list[Story]]] = []
    for src in sources:
        result = run_source(src, days, args.max, args.no_fetch, args.no_summary)
        results.append(result)

    print("\nRendering HTML...", file=sys.stderr)
    html = render_html(results, days)
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Saved → {output_file}", file=sys.stderr)
    print(output_file)
    webbrowser.open(f"file://{os.path.abspath(output_file)}")


if __name__ == "__main__":
    main()
