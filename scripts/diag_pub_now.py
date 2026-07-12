import json
from pathlib import Path
from collections import Counter

pl = json.loads(Path("/opt/factiva/pipelines.json").read_text(encoding="utf-8"))
p = pl["pipelines"][0]
print("platform", p.get("default_platform"))
print("auto", p.get("auto_enabled"))
print("status", json.dumps(p.get("auto_last_status"), ensure_ascii=False))
print("export_times", p.get("auto_export_times"))
print("stories", len(p.get("published_stories") or []))

stack = json.loads(Path("/opt/factiva/publisher_stack.json").read_text(encoding="utf-8"))
print("stack", len(stack), dict(Counter(x.get("status") for x in stack)))
print("platforms", dict(Counter(x.get("platform") for x in stack)))
pend = [x for x in stack if x.get("status") == "pending"]
print("pending", len(pend))
for x in pend[:8]:
    print(" ", x.get("scheduledAt"), x.get("platform"), (x.get("text") or "")[:70])
recent = sorted(stack, key=lambda x: x.get("scheduledAt") or "", reverse=True)[:8]
print("--- recent ---")
for x in recent:
    print(x.get("status"), x.get("scheduledAt"), x.get("platform"), (x.get("error") or "")[:40], (x.get("text") or "")[:50])
