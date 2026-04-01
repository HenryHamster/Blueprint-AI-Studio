"""Check all tasks with CI/PR info."""
import json, urllib.request
from pathlib import Path

config = json.loads(Path(__file__).resolve().parent.parent.joinpath(".blueprint-config.json").read_text())
base = config["api_url"].rstrip("/")
headers = {"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"}

for st in ["EVALUATING", "IN_PROGRESS", "QUEUED", "FAILED", "COMPLETED"]:
    url = base + f"/api/admin/tasks?limit=50&status={st}"
    req = urllib.request.Request(url, headers=headers)
    data = json.loads(urllib.request.urlopen(req, timeout=30).read())
    tasks = data.get("tasks", [])
    if tasks:
        print(f"\n=== {st} ({len(tasks)}) ===")
        for t in tasks:
            ctx = t.get("context") or {}
            loop = ctx.get("review_loop", {})
            ci = loop.get("ci_waiting", False)
            pr = loop.get("pr_number", "-")
            pid = (t.get("parent_task_id") or "")[:8]
            msg = (t.get("latest_status_message") or "")[:80]
            ttype = t.get("task_type", "?")
            print(f"  {t['id'][:8]}  [{ttype:8}] {t['title'][:45]:<45}  ci={ci}  pr={pr}  parent={pid or '-':<8}  {msg}")
