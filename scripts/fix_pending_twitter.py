import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Move stuck pending posts to Twitter and re-slot from now
stack_path = Path("/opt/factiva/publisher_stack.json")
stack = json.loads(stack_path.read_text(encoding="utf-8"))
now = datetime.now(timezone.utc).replace(tzinfo=None)
pending = [x for x in stack if x.get("status") == "pending" and x.get("pipeline") == "default"]
pending.sort(key=lambda x: x.get("scheduledAt") or "")
print("pending_before", len(pending))
for i, x in enumerate(pending):
    x["platform"] = "twitter"
    slot = now + timedelta(minutes=1 + i)
    x["scheduledAt"] = slot.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    print(" ", x["id"], x["scheduledAt"], (x.get("text") or "")[:50])
stack_path.write_text(json.dumps(stack, ensure_ascii=False, indent=2), encoding="utf-8")

pl_path = Path("/opt/factiva/pipelines.json")
pl = json.loads(pl_path.read_text(encoding="utf-8"))
for p in pl.get("pipelines", []):
    if p.get("id") == "default":
        p["default_platform"] = "twitter"
        print("default_platform -> twitter")
pl_path.write_text(json.dumps(pl, ensure_ascii=False, indent=2), encoding="utf-8")
print("ok")
