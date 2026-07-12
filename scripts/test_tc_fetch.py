import sys
sys.path.insert(0, "/opt/factiva")
from app import _fetch_tc_articles_for_automation

tc_dom = {"max": "20", "datePreset": "today"}
arts, stats, used = _fetch_tc_articles_for_automation(tc_dom, "main", 20, False)
print("used_preset", used)
print("count", len(arts))
print("requested", stats.get("date_preset_requested"))
print("used", stats.get("date_preset_used"))
