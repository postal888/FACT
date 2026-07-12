import json
from pathlib import Path
stack = json.loads(Path("/opt/factiva/publisher_stack.json").read_text(encoding="utf-8"))
failed = [x for x in stack if x.get("status") == "failed"]
print("failed", len(failed))
for x in failed[-12:]:
    print("---")
    print(x.get("platform"), x.get("scheduledAt"))
    print("err:", x.get("error"))
    print((x.get("text") or "")[:80])

pl = json.loads(Path("/opt/factiva/pipelines.json").read_text(encoding="utf-8"))
print("export_times", pl["pipelines"][0].get("auto_export_times"))
print("platform", pl["pipelines"][0].get("default_platform"))
