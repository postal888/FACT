"""TechCrunch RSS feed → article list for the post builder.

Pattern adapted from CrackTheDeck deals_feed.py (RSS + cache),
extended with article body for draft generation.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.request
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

logger = logging.getLogger(__name__)

FEED_USER_AGENT = (
    "Mozilla/5.0 (compatible; FactivaExporter/1.0; +https://github.com/)"
)
TECHCRUNCH_FEEDS = {
    "main": "https://techcrunch.com/feed/",
    "funding": "https://techcrunch.com/tag/funding/feed/",
    "startups": "https://techcrunch.com/category/startups/feed/",
    "ai": "https://techcrunch.com/category/artificial-intelligence/feed/",
}
FEED_LABELS = {
    "main": "Главная",
    "funding": "Funding",
    "startups": "Startups",
    "ai": "AI",
}

TC_TOPIC_CATEGORIES = {
    "ai": "AI",
    "startups": "Стартапы",
    "funding": "Фандинг",
    "events": "Ивенты",
    "security": "Безопасность",
    "apps": "Приложения",
    "gadgets": "Гаджеты",
    "media": "Медиа",
    "enterprise": "Enterprise",
    "other": "Разное",
}

_EVENT_TITLE_RE = re.compile(
    r"\b(event|battlefield|summit|conference|keynote|applications now close)\b",
    re.I,
)

def _entry_tags(entry: dict) -> list[str]:
    tags = []
    for t in entry.get("tags") or []:
        term = ""
        if isinstance(t, dict):
            term = (t.get("term") or t.get("label") or "").strip()
        else:
            term = (getattr(t, "term", None) or getattr(t, "label", None) or "").strip()
        if term:
            tags.append(term)
    return tags


def classify_tc_category(title: str, body: str, tags: list[str] | None = None) -> str:
    """Map TechCrunch RSS tags + title heuristics to a topic bucket."""
    tags = tags or []
    title_l = (title or "").lower()
    tag_low = [t.lower() for t in tags if t]

    if _EVENT_TITLE_RE.search(title or "") or any("battlefield" in t for t in tag_low):
        return "events"
    if any(t in tag_low for t in ("security", "cybersecurity", "cyberattack", "data breach")):
        return "security"
    if "ai" in tag_low:
        return "ai"
    if any(t in tag_low for t in ("venture", "funding")):
        return "funding"
    if any("startup" in t for t in tag_low):
        return "startups"
    if any(t in tag_low for t in ("gadgets", "hardware")):
        return "gadgets"
    if "apps" in tag_low:
        return "apps"
    if any("media" in t for t in tag_low):
        return "media"
    if "enterprise" in tag_low:
        return "enterprise"
    return "other"


def _tags_from_article(article: dict) -> list[str]:
    raw = article.get("tags") or []
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    return []
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(html: str) -> str:
    if not html:
        return ""
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = _TAG_RE.sub(" ", text)
    text = unescape(text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def _entry_datetime(published) -> datetime | None:
    if not published:
        return None
    try:
        if hasattr(published, "tm_mon"):
            return datetime(published.tm_year, published.tm_mon, published.tm_mday)
        if isinstance(published, str):
            dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    except Exception:
        pass
    return None


def _format_date(published) -> str:
    dt = _entry_datetime(published)
    if dt:
        return dt.strftime("%b ") + str(dt.day) + dt.strftime(", %Y")
    if published:
        return str(published)[:32]
    return ""


def _parse_bound_date(value: str | None) -> datetime | None:
    if not value or not str(value).strip():
        return None
    try:
        return datetime.strptime(str(value).strip()[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _in_date_range(dt: datetime | None, date_from: datetime | None, date_to: datetime | None) -> bool:
    if date_from is None and date_to is None:
        return True
    if dt is None:
        return False
    day = dt.date()
    if date_from and day < date_from.date():
        return False
    if date_to and day > date_to.date():
        return False
    return True


def _entry_body(entry: dict) -> str:
    """Prefer full content:encoded, then summary."""
    content = entry.get("content") or []
    if content:
        val = (content[0].get("value") or "").strip()
        text = _strip_html(val)
        if len(text) > 200:
            return text
    summary = (entry.get("summary") or entry.get("description") or "").strip()
    return _strip_html(summary)


def _fetch_page_body(url: str, timeout: int = 8) -> str:
    """Best-effort full article text from the TC page."""
    if not url:
        return ""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read(400_000).decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning("TC page fetch failed %s: %s", url, e)
        return ""

    paragraphs = [_strip_html(p) for p in re.findall(r"(?is)<p\b[^>]*>(.*?)</p>", html)]
    paragraphs = [p for p in paragraphs if len(p) > 50 and "subscribe" not in p.lower()]
    if paragraphs:
        joined = " ".join(paragraphs)
        if len(joined) > 300:
            return joined[:8000]

    candidates = []
    for pat in (
        r'(?is)<div[^>]+class="[^"]*article-content[^"]*"[^>]*>(.{200,12000}?)</div>',
        r'(?is)<div[^>]+class="[^"]*entry-content[^"]*"[^>]*>(.{200,12000}?)</div>',
        r'(?is)<article\b[^>]*>(.{200,15000}?)</article>',
    ):
        for m in re.finditer(pat, html):
            text = _strip_html(m.group(1))
            if len(text) > 300:
                candidates.append(text)

    if not candidates:
        return ""
    return max(candidates, key=len)[:8000]


def _download_feed(url: str, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": FEED_USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _parse_feed_bytes(raw: bytes) -> list[dict]:
    import feedparser

    feed = feedparser.parse(raw)
    return list(getattr(feed, "entries", []) or [])


def _fetch_feed_entries(feed_key: str = "main") -> list[dict]:
    url = TECHCRUNCH_FEEDS.get(feed_key) or TECHCRUNCH_FEEDS["main"]
    try:
        entries = _parse_feed_bytes(_download_feed(url))
        if entries:
            return entries
        if feed_key != "main":
            return _parse_feed_bytes(_download_feed(TECHCRUNCH_FEEDS["main"]))
    except Exception as e:
        logger.warning("TechCrunch feed fetch failed: %s", e)
    return []


def _enrich_page_bodies(items: list[dict], deadline: float | None = None) -> None:
    """Fetch full article pages sequentially (reliable, no thread-pool hang)."""
    for item in items:
        if len(item.get("body") or "") >= 500:
            continue
        if deadline is not None and time.time() >= deadline:
            break
        page_body = fetch_page_body_for_url(item.get("url") or "")
        if len(page_body) > len(item.get("body") or ""):
            item["body"] = page_body


def fetch_page_body_for_url(url: str, timeout: int = 6) -> str:
    return _fetch_page_body(url, timeout=timeout)


def fetch_techcrunch_articles(
    feed_key: str = "main",
    max_articles: int = 20,
    fetch_full_body: bool = True,
    date_from: str | None = None,
    date_to: str | None = None,
) -> tuple[list[dict], dict]:
    """Return articles shaped like Factiva parser output + url."""
    entries = _fetch_feed_entries(feed_key)
    limit = max(1, min(int(max_articles), 50))
    from_dt = _parse_bound_date(date_from)
    to_dt = _parse_bound_date(date_to)
    if from_dt and to_dt and from_dt > to_dt:
        raise ValueError("Дата «с» не может быть позже даты «по»")

    articles = []
    skipped_date = 0
    for entry in entries:
        title = (entry.get("title") or "").strip()
        url = (entry.get("link") or "").strip()
        if not title or not url:
            continue
        pub = entry.get("published_parsed") or entry.get("published")
        pub_dt = _entry_datetime(pub)
        if not _in_date_range(pub_dt, from_dt, to_dt):
            skipped_date += 1
            continue
        if len(articles) >= limit:
            break
        date_str = _format_date(pub)
        body = _entry_body(entry)
        rss_tags = _entry_tags(entry)
        category = classify_tc_category(title, body, rss_tags)
        if len(body) > 8000:
            body = body[:8000] + "..."
        if not body:
            body = title
        articles.append({
            "title": title,
            "source": "TechCrunch",
            "date": date_str,
            "body": body,
            "url": url,
            "category": category,
            "category_label": TC_TOPIC_CATEGORIES.get(category, category),
            "tags": rss_tags[:8],
        })
    if fetch_full_body and articles:
        deadline = time.time() + min(90, 10 + len(articles) * 8)
        _enrich_page_bodies(articles, deadline=deadline)
        for item in articles:
            body = item.get("body") or item["title"]
            if len(body) > 8000:
                item["body"] = body[:8000] + "..."
    try:
        from twitter_poster import dedupe_articles
        articles, removed_dupes = dedupe_articles(articles)
    except Exception:
        removed_dupes = 0
    stats = {
        "feed_entries": len(entries),
        "date_skipped": skipped_date,
        "removed_duplicates": removed_dupes,
        "date_from": date_from,
        "date_to": date_to,
    }
    return articles, stats


def save_articles_json(
    articles: list[dict],
    exports_dir: Path,
    feed_key: str = "main",
    out_name: str | None = None,
) -> Path:
    import re

    exports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    if out_name and str(out_name).strip():
        base = re.sub(r"\.json$", "", str(out_name).strip(), flags=re.IGNORECASE)
        base = re.sub(r"[^\w\-]+", "_", base, flags=re.UNICODE)
        base = re.sub(r"_+", "_", base).strip("_") or "techcrunch_export"
        name = f"{base}_{stamp}.json"
    else:
        name = f"techcrunch_{feed_key}_{stamp}.json"
    path = exports_dir / name
    payload = {
        "source": "TechCrunch",
        "feed": feed_key,
        "feed_label": FEED_LABELS.get(feed_key, feed_key),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "articles": articles,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
