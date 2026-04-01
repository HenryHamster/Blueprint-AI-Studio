"""Find and requeue all CI-waiting EVALUATING tasks."""
import json, urllib.request
from pathlib import Path

config = json.loads(Path(__file__).resolve().parent.parent.joinpath(".blueprint-config.json").read_text())
base = config["api_url"].rstrip("/")
headers = {"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"}

# Find EVALUATING tasks
url = base + "/api/admin/tasks?limit=100&status=EVALUATING"
req = urllib.request.Request(url, headers=headers)
tasks = json.loads(urllib.request.urlopen(req, timeout=30).read()).get("tasks", [])

for t in tasks:
    ctx = t.get("context") or {}
    loop = ctx.get("review_loop", {})
    if not loop.get("ci_waiting"):
        continue
    tid = t["id"]
    pr = loop.get("pr_number", "?")
    print(f"Requeuing {tid[:8]} (Review PR #{pr})...", end=" ")
    patch_url = base + f"/api/admin/tasks/{tid}"
    body = json.dumps({"status": "QUEUED"}).encode()
    patch_req = urllib.request.Request(patch_url, data=body, method="PATCH", headers=headers)
    try:
        resp = json.loads(urllib.request.urlopen(patch_req, timeout=15).read())
        print(f"-> {resp.get('status', 'ok')}")
    except Exception as e:
        print(f"FAILED: {e}")
