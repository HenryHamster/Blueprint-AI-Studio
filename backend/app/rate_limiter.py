"""Shared slowapi rate limiter (split out of shared_state).

Re-exported from :mod:`app.shared_state` so existing importers
(``from app.shared_state import limiter``) are unaffected. Behaviour-preserving.
"""

import os

from slowapi import Limiter
from slowapi.util import get_remote_address

# Rate limiter — shared between main.py and admin.py
# Only enforced in Cloud Run (K_REVISION set), disabled locally for dev/test
# Uses Redis for cross-instance coordination when REDIS_URL is set,
# falls back to per-instance in-memory storage otherwise.
_redis_url = os.getenv("REDIS_URL")
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=_redis_url if _redis_url else "memory://",
    enabled=bool(os.getenv("K_REVISION")),
)
