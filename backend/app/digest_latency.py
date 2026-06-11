"""In-process rolling latency aggregate for the /api/admin/digest endpoint.

Records request durations and serving-tier counts in a bounded in-memory
deque (maxlen=1000).  No per-request DB writes.  All state is per-instance;
the snapshot endpoint labels results accordingly.

Serving tiers:
  l1_precompute  — in-process worker-written precompute cache (fastest)
  l2_shared_cache — cross-instance DB shared cache (leader-written)
  l3_on_demand   — DigestCache.get_or_compute (full on-demand scan, slowest)
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any, Dict

_DEQUE_MAXLEN = 1000

# All mutable state is module-level so it persists across requests within
# one process.  The lock makes tier-count increments atomic on sub-CPython
# runtimes (CPython's GIL already makes list.append atomic, but deque.append
# is also GIL-safe; we use a lock anyway for correctness across runtimes).
_lock = threading.Lock()
_durations: deque[float] = deque(maxlen=_DEQUE_MAXLEN)
_tier_counts: Dict[str, int] = {
    "l1_precompute": 0,
    "l2_shared_cache": 0,
    "l3_on_demand": 0,
}


def record_digest_request(duration_seconds: float, tier: str) -> None:
    """Record one /api/admin/digest request duration and serving tier.

    Thread-safe; called from async request handlers (the GIL serialises
    CPython threads, but the explicit lock is future-proof and cheap).
    ``tier`` must be one of ``l1_precompute``, ``l2_shared_cache``, or
    ``l3_on_demand``; unknown tiers are recorded under the duration but do
    not increment a tier counter.
    """
    with _lock:
        _durations.append(duration_seconds)
        if tier in _tier_counts:
            _tier_counts[tier] += 1


def get_digest_latency_snapshot() -> Dict[str, Any]:
    """Return the current in-process latency aggregate.

    Returns p50/p95 of request durations (in seconds) and per-tier serve
    counts.  All values are per-instance and reset on process restart.
    """
    with _lock:
        dur_copy = list(_durations)
        tier_copy = dict(_tier_counts)

    n = len(dur_copy)
    if n == 0:
        latency_percentiles = {"p50_seconds": None, "p95_seconds": None, "sample_n": 0}
    else:
        sorted_dur = sorted(dur_copy)
        p50_idx = min(int(n * 0.50), n - 1)
        p95_idx = min(int(n * 0.95), n - 1)
        latency_percentiles = {
            "p50_seconds": round(sorted_dur[p50_idx], 4),
            "p95_seconds": round(sorted_dur[p95_idx], 4),
            "sample_n": n,
        }

    total_served = sum(tier_copy.values())
    return {
        "latency_percentiles": latency_percentiles,
        "tier_counts": tier_copy,
        "total_served": total_served,
        "instance_note": "per-instance aggregate; does not sum across replicas",
    }


def _reset_for_testing() -> None:
    """Clear all counters.  Test use only."""
    with _lock:
        _durations.clear()
        for k in _tier_counts:
            _tier_counts[k] = 0
