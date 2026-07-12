import json
import re
from pathlib import Path
from collections import defaultdict
from datetime import datetime

stack = json.loads(Path("/opt/factiva/publisher_stack.json").read_text(encoding="utf-8"))
pub = [x for x in stack if x.get("status") == "published"]

def day_key(item):
    s = item.get("scheduledAt") or item.get("publishedAt") or ""
    return s[:10]

by_day = defaultdict(list)
for x in pub:
    by_day[day_key(x)].append(x)

for day in sorted(by_day.keys())[-3:]:
    print(f"\n=== {day} ({len(by_day[day])} posts) ===")
    for x in sorted(by_day[day], key=lambda i: i.get("scheduledAt") or ""):
        t = " ".join((x.get("text") or "").split())[:100]
        print("-", t)

# Compare Jul 9 vs Jul 10 by token overlap
def tokens(text):
    t = re.sub(r"[^\w\s]", " ", (text or "").lower(), flags=re.UNICODE)
    return set(w for w in t.split() if len(w) > 3)

d9 = by_day.get("2026-07-09", [])
d10 = by_day.get("2026-07-10", [])
print(f"\n=== similarity Jul9({len(d9)}) vs Jul10({len(d10)}) ===")
for a in d10:
    ta = tokens(a.get("text"))
    best = None
    best_score = 0
    for b in d9:
        tb = tokens(b.get("text"))
        if not ta or not tb:
            continue
        score = len(ta & tb) / max(1, len(ta | tb))
        if score > best_score:
            best_score = score
            best = b
    if best_score >= 0.25:
        print(f"overlap={best_score:.2f}")
        print("  TODAY:", " ".join((a.get("text") or "").split())[:90])
        print("  YDAY :", " ".join((best.get("text") or "").split())[:90])

# Check export files article titles
exports = sorted(Path("/opt/factiva/exports").glob("techcrunch_export_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:4]
print("\n=== recent exports ===")
for p in exports:
    d = json.loads(p.read_text(encoding="utf-8"))
    arts = d.get("articles") or []
    print(p.name, "n=", len(arts), "titles:")
    for a in arts[:5]:
        print(" ", (a.get("title") or "")[:80])
