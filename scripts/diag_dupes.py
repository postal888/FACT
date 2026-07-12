import json
from pathlib import Path
from collections import Counter

stack = json.loads(Path("/opt/factiva/publisher_stack.json").read_text(encoding="utf-8"))
print("stack_total", len(stack))
by_status = Counter(x.get("status") for x in stack)
print("by_status", dict(by_status))

# recent published
pub = [x for x in stack if x.get("status") == "published"]
pub.sort(key=lambda x: x.get("scheduledAt") or x.get("publishedAt") or "", reverse=True)
print("\n--- last 8 published ---")
for x in pub[:8]:
    t = (x.get("text") or "")[:90].replace("\n", " ")
    print(x.get("scheduledAt"), x.get("platform"), t)

# duplicate texts among published
keys = []
for x in pub:
    t = " ".join((x.get("text") or "").lower().split())[:120]
    keys.append(t)
c = Counter(keys)
dups = [(k, n) for k, n in c.items() if n > 1]
print("\nduplicate_published_texts", len(dups))
for k, n in dups[:5]:
    print(n, k[:80])

pl = json.loads(Path("/opt/factiva/pipelines.json").read_text(encoding="utf-8"))
for p in pl.get("pipelines", []):
    st = p.get("auto_last_status") or {}
    print("\npipeline", p.get("id"), "auto", p.get("auto_enabled"))
    print("  status", st.get("step"), "drafts", st.get("drafts"), "scheduled", st.get("scheduled"), "articles", st.get("articles_in_export"), "at", st.get("at"))
    print("  warning", st.get("warning"))
    print("  skipped_dupes", st.get("skipped_duplicates"))
    print("  last_auto_runs", p.get("last_auto_runs"))
