"""Cancel all non-terminal tasks (QUEUED, PROVISIONING, IN_PROGRESS, EVALUATING)."""
import json, urllib.request
from pathlib import Path

config = json.loads(Path(__file__).resolve().parent.parent.joinpath(".blueprint-config.json").read_text())
base = config["api_url"].rstrip("/")
headers = {"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"}

cancelled = 0
for status in ["QUEUED", "PROVISIONING", "IN_PROGRESS", "EVALUATING"]:
    url = base + f"/api/admin/tasks?limit=100&status={status}"
    req = urllib.request.Request(url, headers=headers)
    tasks = json.loads(urllib.request.urlopen(req, timeout=30).read()).get("tasks", [])
    for t in tasks:
        tid = t["id"]
        title = t["title"][:50]
        print(f"Cancelling [{status[:4]}] {tid[:8]} {title}...", end=" ")
        patch_url = base + f"/api/admin/tasks/{tid}"
        body = json.dumps({"status": "CANCELLED"}).encode()
        patch_req = urllib.request.Request(patch_url, data=body, method="PATCH", headers=headers)
        try:
            resp = json.loads(urllib.request.urlopen(patch_req, timeout=15).read())
            print("done")
            cancelled += 1
        except Exception as e:
            print(f"FAILED: {e}")

print(f"\nCancelled {cancelled} tasks.")
