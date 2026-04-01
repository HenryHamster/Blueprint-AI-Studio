"""Find a task by title substring and print details."""
import json, urllib.request, sys
from pathlib import Path

config = json.loads(Path(__file__).resolve().parent.parent.joinpath(".blueprint-config.json").read_text())
url = config["api_url"].rstrip("/") + "/api/admin/tasks?limit=100"
req = urllib.request.Request(url, headers={"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"})
tasks = json.loads(urllib.request.urlopen(req, timeout=30).read()).get("tasks", [])

search = sys.argv[1] if len(sys.argv) > 1 else "Review PR #81"
for t in tasks:
    if search in t.get("title", ""):
        print(f"ID: {t['id']}")
        print(f"Status: {t['status']}")
        print(f"Message: {(t.get('latest_status_message') or 'none')[:500]}")
        print(f"Context: {json.dumps(t.get('context', {}), indent=2)}")
        print(f"Heartbeat: {t.get('last_heartbeat')}")
        print("---")
