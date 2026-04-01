"""Cancel duplicate fix tasks, check what's missing."""
import json, urllib.request
from pathlib import Path

config = json.loads(Path(__file__).resolve().parent.parent.joinpath(".blueprint-config.json").read_text())
base = config["api_url"].rstrip("/")
headers = {"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"}

# Get all non-terminal tasks
seen_prs = {}
to_cancel = []
for st in ["QUEUED", "IN_PROGRESS", "EVALUATING"]:
    url = base + f"/api/admin/tasks?limit=50&status={st}"
    req = urllib.request.Request(url, headers=headers)
    tasks = json.loads(urllib.request.urlopen(req, timeout=30).read()).get("tasks", [])
    for t in tasks:
        p = (t.get("payloads") or [{}])[0]
        pr = p.get("pr_number")
        ttype = p.get("type", t.get("task_type", "?"))
        key = f"{ttype}-{pr}"
        if pr and key in seen_prs:
            to_cancel.append(t["id"])
            print(f"DUPLICATE: {t['id'][:8]} [{ttype}] PR #{pr} (keeping {seen_prs[key][:8]})")
        else:
            if pr:
                seen_prs[key] = t["id"]
            print(f"KEEP:      {t['id'][:8]} [{ttype:8}] {st:11} PR #{pr or '-':<4}  {t['title'][:50]}")

# Cancel duplicates
for tid in to_cancel:
    patch_url = base + f"/api/admin/tasks/{tid}"
    body = json.dumps({"status": "CANCELLED"}).encode()
    req = urllib.request.Request(patch_url, data=body, method="PATCH", headers=headers)
    try:
        json.loads(urllib.request.urlopen(req, timeout=15).read())
        print(f"  Cancelled {tid[:8]}")
    except Exception as e:
        print(f"  Failed to cancel {tid[:8]}: {e}")

# Check which PRs have NO active task watching them
print("\n=== PRs with no active task ===")
open_prs = [84, 85, 86, 87, 89, 90, 91]
active_prs = set()
for key in seen_prs:
    parts = key.split("-")
    if len(parts) == 2 and parts[1].isdigit():
        active_prs.add(int(parts[1]))

for pr in open_prs:
    if pr not in active_prs:
        print(f"  PR #{pr} - NO active task")
    else:
        print(f"  PR #{pr} - has active task")
