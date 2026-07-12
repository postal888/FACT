import json
from pathlib import Path
from datetime import datetime, timezone

p = json.loads(Path("/opt/factiva/pipelines.json").read_text(encoding="utf-8"))
pipe = p["pipelines"][0]
st = pipe.get("auto_last_status") or {}
if st.get("step") == "error" and "уже публиковались" in (st.get("error") or ""):
    pipe["auto_last_status"] = {
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "step": "done",
        "ok": True,
        "error": "",
        "drafts": 0,
        "scheduled": 0,
        "warning": (
            "Сейчас в ленте нет новых статей (все уже публиковались). "
            "Слот 09:00 MSK сработает как обычно — если появятся новые материалы."
        ),
    }
    Path("/opt/factiva/pipelines.json").write_text(
        json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("status cleared to soft-done")
else:
    print("status unchanged", st.get("step"), st.get("error", "")[:60])
print("export_times", pipe.get("auto_export_times"))
print("auto_enabled", pipe.get("auto_enabled"))
