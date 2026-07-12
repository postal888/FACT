import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

stack_path = Path("/opt/factiva/publisher_stack.json")
stack = json.loads(stack_path.read_text(encoding="utf-8"))
now = datetime.now(timezone.utc).replace(tzinfo=None)

# Retry failed+pending twitter posts via Telegram (API has no write permission)
retry = [
    x for x in stack
    if x.get("pipeline") == "default"
    and x.get("platform") == "twitter"
    and x.get("status") in ("failed", "pending")
]
retry.sort(key=lambda x: x.get("scheduledAt") or "")
print("retry", len(retry))
for i, x in enumerate(retry):
    x["platform"] = "telegram"
    x["status"] = "pending"
    x["error"] = ""
    slot = now + timedelta(minutes=1 + i)
    x["scheduledAt"] = slot.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    print(" ", x["id"], x["scheduledAt"], (x.get("text") or "")[:50])

stack_path.write_text(json.dumps(stack, ensure_ascii=False, indent=2), encoding="utf-8")

pl_path = Path("/opt/factiva/pipelines.json")
pl = json.loads(pl_path.read_text(encoding="utf-8"))
for p in pl.get("pipelines", []):
    if p.get("id") == "default":
        p["default_platform"] = "telegram"
        # restore sane export time if corrupted
        times = p.get("auto_export_times") or []
        fixed = []
        for t in times:
            s = str(t).strip()
            if len(s) >= 4 and ":" in s:
                parts = s.split(":")
                try:
                    h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 and parts[1] != "" else -1
                    if 0 <= h <= 23 and 0 <= m <= 59:
                        fixed.append(f"{h:02d}:{m:02d}")
                except ValueError:
                    pass
        p["auto_export_times"] = fixed or ["08:00"]
        print("export_times", p["auto_export_times"], "platform", p["default_platform"])
pl_path.write_text(json.dumps(pl, ensure_ascii=False, indent=2), encoding="utf-8")
print("ok")
