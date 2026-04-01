"""Find all EVALUATING tasks and show ci_waiting status."""
import json, urllib.request
from pathlib import Path

config = json.loads(Path(__file__).resolve().parent.parent.joinpath(".blueprint-config.json").read_text())
url = config["api_url"].rstrip("/") + "/api/admin/tasks?limit=100&status=EVALUATING"
req = urllib.request.Request(url, headers={"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"})
data = json.loads(urllib.request.urlopen(req, timeout=30).read())
tasks = data.get("tasks", data) if isinstance(data, dict) else data

for t in tasks:
    ctx = t.get("context") or {}
    loop = ctx.get("review_loop", {})
    ci = loop.get("ci_waiting", False)
    print(f"{t['id'][:8]}  {t['title'][:55]:<55}  ci_waiting={ci}  pr={loop.get('pr_number', '-')}")
