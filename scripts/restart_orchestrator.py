"""Restart the orchestrator background task via the admin API.

Usage:
    python scripts/restart_orchestrator.py
"""
from blueprint_api import api

try:
    resp = api("POST", "/api/admin/restart-orchestrator", {})
    print(f"Orchestrator: {resp.get('status', 'unknown')}")
except Exception as e:
    print(f"Failed: {e}")
