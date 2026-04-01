"""Quick check of a task and its children."""
import json, urllib.request, sys
from pathlib import Path

config = json.loads(Path(__file__).resolve().parent.parent.joinpath(".blueprint-config.json").read_text())
url = config["api_url"].rstrip("/") + "/api/admin/tasks?limit=100"
req = urllib.request.Request(url, headers={"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"})
data = json.loads(urllib.request.urlopen(req, timeout=30).read())
tasks = data.get("tasks", data) if isinstance(data, dict) else data

parent_id = sys.argv[1] if len(sys.argv) > 1 else "791a4ed5-1866-4445-89ca-6af6ae7745ee"
parent = next((t for t in tasks if t["id"] == parent_id), None)
if parent:
    print(f"Parent: {parent['title']}")
    print(f"Status: {parent['status']}")
    print(f"Message: {(parent.get('latest_status_message') or 'none')[:200]}")
    print(f"VM: {parent.get('vm_instance_id')}")
    print(f"Heartbeat: {parent.get('last_heartbeat')}")
    print()

children = [t for t in tasks if t.get("parent_task_id") == parent_id]
print(f"Children: {len(children)}")
for c in children:
    icons = {"QUEUED": "Q", "PROVISIONING": "P", "IN_PROGRESS": "R", "COMPLETED": "D", "FAILED": "F", "CANCELLED": "X", "EVALUATING": "E"}
    s = icons.get(c["status"], "?")
    pr = c.get("pr_url", "")
    pr_num = pr.rstrip("/").split("/")[-1] if pr else "-"
    merged = (c.get("context") or {}).get("pr_merged", False)
    title = c["title"][:65]
    print(f"  [{s}] {title:<65} PR#{pr_num:<5} merged={merged}")
