import json
from pathlib import Path
p = json.loads(Path("/opt/factiva/pipelines.json").read_text(encoding="utf-8"))
p["pipelines"][0]["auto_export_times"] = ["01:32"]
Path("/opt/factiva/pipelines.json").write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
print("ok", p["pipelines"][0]["auto_export_times"])
