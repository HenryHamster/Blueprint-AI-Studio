"""Shared viewport screenshot evidence helpers.

The component-key derivation for viewport screenshot GCS paths lives here so
the upload endpoint (agent.py) and download endpoint (main.py) always produce
the same object name for a given (project_id, component, width) triple.  The
visual-evidence validation path in payload_schemas.py also calls this function
so all three callers stay byte-for-byte consistent.

The duplication bug (upload key != download key) was the primary defect that
required two fix cycles on PR #9522 — centralising here prevents recurrence.
"""

from __future__ import annotations

import re

_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._-]")
_MAX_COMPONENT_LEN = 100

# Width bounds enforced by the upload endpoint (Field(ge=1, le=4096) on the
# agent request model).  The visual-evidence validation re-enforces these so
# a declaration cannot reference a width that would be rejected at upload time.
VIEWPORT_WIDTH_MIN = 1
VIEWPORT_WIDTH_MAX = 4096


def viewport_object_name(project_id: object, component: str, width: int) -> str:
    """Return the canonical GCS object name for a viewport screenshot.

    Single source of truth — previously duplicated inline in agent.py (upload)
    and main.py (download).  Both have been refactored to call this function.
    """
    safe_component = _SAFE_COMPONENT_RE.sub("_", component)[:_MAX_COMPONENT_LEN]
    return f"viewport-failures/{project_id}/{safe_component}-{width}.png"


def resolve_visual_evidence(
    project_id: object,
    component: str,
    width: int,
    task_context: dict | None,
) -> dict:
    """Resolve a declared visual_evidence entry against uploaded screenshot evidence.

    The agent stamps ``task.context['viewport_failure_screenshot_uri']`` on
    upload.  This function checks whether the URI in context matches the
    canonical object name for the declared (component, width) pair.  Absent or
    never-uploaded evidence resolves as UNVERIFIED — it never silently counts
    as verified.  No live GCS existence probe is performed; the function is
    fail-soft by design.

    Returns a dict with:
      resolved    — True only when a matching URI was found in context
      object_name — the canonical GCS object name (from viewport_object_name)
      uri         — the uploaded GCS URI if found, else None
      status      — "verified" | "unverified"
    """
    object_name = viewport_object_name(project_id, component, width)
    ctx = task_context or {}
    uploaded_uri = ctx.get("viewport_failure_screenshot_uri")
    if (
        uploaded_uri
        and isinstance(uploaded_uri, str)
        and uploaded_uri.endswith(object_name)
    ):
        return {
            "resolved": True,
            "object_name": object_name,
            "uri": uploaded_uri,
            "status": "verified",
        }
    return {
        "resolved": False,
        "object_name": object_name,
        "uri": None,
        "status": "unverified",
    }
