#!/usr/bin/env python3
import json
from pathlib import Path

stack = json.loads(Path("/opt/factiva/publisher_stack.json").read_text(encoding="utf-8"))
failed = [x for x in stack if x.get("status") == "failed" and x.get("platform") == "twitter"]
print("failed_twitter", len(failed))
for x in failed[:5]:
    text = x.get("text") or ""
    print("---")
    print("error:", (x.get("error") or "")[:300])
    print("len:", len(text))
    print("text:", text[:120], "...")
