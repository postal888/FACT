"""Seed published_stories from recent exports so automation skips already-used RSS items."""
import json
from pathlib import Path

BASE = Path("/opt/factiva")
pl_path = BASE / "pipelines.json"
pl = json.loads(pl_path.read_text(encoding="utf-8"))

exports = sorted(
    (BASE / "exports").glob("techcrunch_export_*.json"),
    key=lambda p: p.stat().st_mtime,
    reverse=True,
)[:3]

titles = []
seen = set()
for ep in exports:
    d = json.loads(ep.read_text(encoding="utf-8"))
    for a in d.get("articles") or []:
        title = (a.get("title") or "").strip()
        url = (a.get("url") or "").strip()
        key = title.lower()
        if not title or key in seen:
            continue
        seen.add(key)
        titles.append({"title": title, "url": url, "at": "backfill"})

for p in pl.get("pipelines", []):
    existing = p.get("published_stories") or []
    have = {(s.get("title") or "").strip().lower() for s in existing}
    added = 0
    for row in titles:
        if row["title"].lower() in have:
            continue
        existing.append(row)
        have.add(row["title"].lower())
        added += 1
    p["published_stories"] = existing[-500:]
    print(p.get("id"), "stories", len(p["published_stories"]), "added", added)

pl_path.write_text(json.dumps(pl, ensure_ascii=False, indent=2), encoding="utf-8")
print("ok")
