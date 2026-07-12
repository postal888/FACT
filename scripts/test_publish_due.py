import sys
sys.path.insert(0, "/opt/factiva")
from datetime import datetime, timezone
from app import (
    _load_publisher_stack,
    _process_due_publisher_items,
    _utc_now,
    _parse_iso_utc,
)

stack = _load_publisher_stack()
pend = [x for x in stack if x.get("status") == "pending"]
print("now", _utc_now().isoformat())
for x in pend[:3]:
    sa = x.get("scheduledAt")
    try:
        due = _parse_iso_utc(sa)
        print("item", x.get("id"), "sched", sa, "due", due, "past", _utc_now() >= due)
    except Exception as e:
        print("parse err", e)

print("processing...")
try:
    changed = _process_due_publisher_items(stack)
    print("changed", changed)
    from collections import Counter
    print(dict(Counter(x.get("status") for x in stack if x.get("pipeline") == "default")))
    for x in stack:
        if x.get("status") in ("failed", "cancelled") and x.get("scheduledAt", "").startswith("2026-07-10T22:"):
            print(x.get("status"), x.get("error"), (x.get("text") or "")[:50])
except Exception as e:
    import traceback
    traceback.print_exc()
