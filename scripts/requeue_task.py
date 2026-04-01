"""Requeue a task by ID."""
import json, urllib.request, sys
from pathlib import Path

config = json.loads(Path(__file__).resolve().parent.parent.joinpath(".blueprint-config.json").read_text())

for tid in sys.argv[1:]:
    url = config["api_url"].rstrip("/") + f"/api/admin/tasks/{tid}"
    body = json.dumps({"status": "QUEUED"}).encode()
    req = urllib.request.Request(url, data=body, method="PATCH",
        headers={"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"})
    resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
    print(f"{tid[:8]}: {resp}")
