"""Shared runtime state used by both main.py and admin.py.

Extracted to its own module to avoid circular imports when admin
endpoints need to read or mutate autonomous-loop / runtime-config state.
"""

import os
from typing import Dict

# --- Runtime Config (adjustable via admin API) ---
runtime_config: Dict = {
    "max_concurrent_vms": int(os.getenv("MAX_CONCURRENT_VMS", "5")),
    "max_retries": int(os.getenv("MAX_RETRIES", "3")),
    "max_autonomous_tasks_per_hour": 5,
}

# --- Autonomous Loop State ---
autonomous_failure_counts: Dict[str, int] = {}  # project_id -> consecutive failures
