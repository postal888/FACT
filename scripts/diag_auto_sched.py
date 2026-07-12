import json
from pathlib import Path
p = json.loads(Path("/opt/factiva/pipelines.json").read_text(encoding="utf-8"))
pipe = p["pipelines"][0]
print("auto_enabled", pipe.get("auto_enabled"))
print("export_times", pipe.get("auto_export_times"))
print("platform", pipe.get("default_platform"))
print("status", json.dumps(pipe.get("auto_last_status"), ensure_ascii=False))
print("stories", len(pipe.get("published_stories") or []))
print("last_auto_runs", pipe.get("last_auto_runs"))
