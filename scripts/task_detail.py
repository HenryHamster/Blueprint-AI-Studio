"""Show full task detail by ID."""
import json, urllib.request, sys
from pathlib import Path

config = json.loads(Path(__file__).resolve().parent.parent.joinpath(".blueprint-config.json").read_text())
url = config["api_url"].rstrip("/") + "/api/admin/tasks?limit=100"
req = urllib.request.Request(url, headers={"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"})
tasks = json.loads(urllib.request.urlopen(req, timeout=30).read()).get("tasks", [])

tid = sys.argv[1] if len(sys.argv) > 1 else ""
for t in tasks:
    if t["id"] == tid or tid in t.get("title", ""):
        print(json.dumps({"id": t["id"], "title": t["title"], "status": t["status"],
            "payloads": t.get("payloads"), "context": t.get("context"),
            "error_logs": (t.get("error_logs") or "")[:500],
            "latest_status_message": (t.get("latest_status_message") or "")[:500],
            "attempt_count": t.get("attempt_count")}, indent=2))
        break
