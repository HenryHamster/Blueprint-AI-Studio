"""Restart the orchestrator background task via the admin API.

Usage:
    python scripts/restart_orchestrator.py
"""
import json, urllib.request
from pathlib import Path

config = json.loads(Path(__file__).resolve().parent.parent.joinpath(".blueprint-config.json").read_text())
base = config["api_url"].rstrip("/")
headers = {"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"}

url = base + "/api/admin/restart-orchestrator"
req = urllib.request.Request(url, data=b"{}", method="POST", headers=headers)
try:
    resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
    print(f"Orchestrator: {resp.get('status', 'unknown')}")
except Exception as e:
    print(f"Failed: {e}")
