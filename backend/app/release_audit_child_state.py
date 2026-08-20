"""Audit-child crash classification for the version lifecycle read model.

Extracted from ``release_version.py`` so that module stays within its declared
``_MODULE_SIZE_GOVERNANCE_CAP``. Pure/sync classification over the audit
evidence envelope — no I/O, no session, no imports from ``release_version``.
"""

from __future__ import annotations

from typing import Any

# This module is one pure classifier plus its status vocabulary. The cap keeps
# a small explicit margin over its current ~50 lines rather than generic
# headroom: anything substantially larger belongs in its own module.
_MODULE_SIZE_GOVERNANCE_CAP: int = 250

# Audit-child task statuses that are NOT a crash when no report is present.
# Mirrors the frontend ``classifyAuditNoReport``
# (VersionLifecyclePanelPrimitives.tsx) so the operator panel's top badge and
# its audit-findings section agree by construction:
#   - non-terminal (QUEUED/PROVISIONING/IN_PROGRESS/EVALUATING) → still running
#   - BLOCKED → waiting on a precondition, distinct from a crash
#   - COMPLETED with no report → finished-but-empty anomaly, not a crash
# FAILED / CANCELLED / any other (unknown) terminal status with no report is a
# genuine crash.
_AUDIT_NONCRASH_STATUSES = frozenset(
    {"QUEUED", "PROVISIONING", "IN_PROGRESS", "EVALUATING", "BLOCKED", "COMPLETED"}
)


def _audit_child_crashed(audit_child: dict[str, Any] | None) -> bool:
    """Return True when the audit child reached a terminal-failed state with no report.

    ``audit_child`` is the version-scoped audit-evidence envelope (the
    ``latest_audit`` dict resolved by ``resolve_lifecycle_audit_evidence``),
    carrying ``task_status`` and ``report``.
    ``None`` (no current audit child resolved) → not a crash, so callers that
    pass no signal preserve the prior ``qualifying`` behavior. Pure/sync — no I/O.
    """
    if not isinstance(audit_child, dict):
        return False
    if audit_child.get("report"):
        # The audit produced a report — by construction not a crashed-no-report.
        return False
    status = str(audit_child.get("task_status") or "").strip().upper()
    if not status:
        # No status signal — do not assert a crash.
        return False
    # FAILED / CANCELLED / unknown terminal status, with no report → crashed;
    # everything in the noncrash set (running / blocked / completed-empty) is not.
    return status not in _AUDIT_NONCRASH_STATUSES
