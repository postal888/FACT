import json
from pathlib import Path

exports = sorted(Path("/opt/factiva/exports").glob("techcrunch_export_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
if exports:
    p = exports[0]
    d = json.loads(p.read_text(encoding="utf-8"))
    arts = d.get("articles", d if isinstance(d, list) else [])
    print("latest_file", p.name, "articles", len(arts))
    for i, a in enumerate(arts[:3]):
        print(f"  {i}: {a.get('title', '')[:70]}")

pl = json.loads(Path("/opt/factiva/pipelines.json").read_text(encoding="utf-8"))
for pipe in pl.get("pipelines", []):
    wf = pipe.get("workflow") or {}
    checked = wf.get("tcChecked") or {}
    n_true = sum(1 for v in checked.values() if v)
    print("pipeline", pipe.get("id"), "tcDom.max", (wf.get("tcDom") or {}).get("max"))
    print("  tcDom.datePreset", (wf.get("tcDom") or {}).get("datePreset"))
    print("  tcChecked true/total", n_true, "/", len(checked))
    print("  tcArticles saved", len(wf.get("tcArticles") or []))
    print("  lastExportArticleCount", wf.get("lastExportArticleCount"))
    print("  auto_last_status", pipe.get("auto_last_status"))
