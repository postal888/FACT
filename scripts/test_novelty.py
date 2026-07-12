import sys
sys.path.insert(0, "/opt/factiva")
from app import _filter_novel_drafts, _published_content_index, _filter_articles_excluding_published
from twitter_poster import load_articles
from pathlib import Path

pid = "default"
idx = _published_content_index(pid)
print("titles_in_index", len(idx["titles"]))
print("texts_in_index", len(idx["texts"]))
print("urls_in_index", len(idx["urls"]))

exports = sorted(Path("/opt/factiva/exports").glob("techcrunch_export_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
arts = load_articles(str(exports[0]))
kept, skipped = _filter_articles_excluding_published(arts, pid)
print("latest_export", exports[0].name, "total", len(arts), "kept", len(kept), "skipped", skipped)

# Simulate drafts from yesterday-style texts
fake = [
    {"title": a.get("title"), "url": a.get("url"), "draft": "Шведский стартап Lovable поднимает $300 млн при оценке $13,2 млрд"}
    for a in arts[:3]
]
novel, stale = _filter_novel_drafts(pid, fake)
print("fake_drafts novel", len(novel), "stale", stale)
