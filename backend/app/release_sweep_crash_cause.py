"""Bounded crash-cause capture for the release qualifier sweep.

Extracted from ``release_qualifier_sweep`` — which sits against its design-pinned
size cap — following the same pattern as ``release_qualifier_hotfix_audit``.
These are pure functions over a plain dict with no session, no GitHub, and no
clock, so they move without changing any monkeypatch target: the sweep module
re-exports every name below, and the existing tests keep reaching them at
``app.release_qualifier_sweep.<name>``.
"""

import traceback
from typing import Any

# Bounded crash-cause formatting only: this module must not grow into a second
# sweep. 150 leaves room for one more capture shape, not a new subsystem.
_MODULE_SIZE_GOVERNANCE_CAP: int = 150

# Consecutive sweep-finalize crashes before force-finalizing the pending
# qualification and raising an operator alert.  Mirrors _MIN_CALCIFICATION_ATTEMPTS:
# three consecutive crashes strongly suggest a persistent bug in the finalize
# path rather than a transient error, and the loop must self-recover.
_MAX_SWEEP_CRASH_STREAK = 3

# A crash-loop record is operator-facing durable state, not a replacement for
# logs. Keep the captured cause bounded so a pathological exception message or
# traceback cannot inflate the JSONB qualification record on every sweep.
_SWEEP_CRASH_EXCEPTION_CLASS_MAX = 120
_SWEEP_CRASH_MESSAGE_MAX = 300
_SWEEP_CRASH_FRAME_MAX = 220
_SWEEP_CRASH_FRAME_COUNT = 4


def _bound_sweep_finalize_crash_cause(cause: dict[str, Any]) -> dict[str, Any]:
    """Keep a persisted finalize cause within its fixed JSONB budget."""
    raw_frames = cause.get("traceback_digest")
    frames = (
        [str(frame)[:_SWEEP_CRASH_FRAME_MAX] for frame in raw_frames]
        if isinstance(raw_frames, list)
        else []
    )[:_SWEEP_CRASH_FRAME_COUNT]
    return {
        "exception_class": str(cause.get("exception_class") or "unknown")[
            :_SWEEP_CRASH_EXCEPTION_CLASS_MAX
        ],
        "message": str(cause.get("message") or "<no message>")[:
            _SWEEP_CRASH_MESSAGE_MAX
        ],
        "traceback_digest": frames,
    }


def _capture_sweep_finalize_crash_cause(exc: Exception) -> dict[str, Any]:
    """Return a bounded, operator-readable cause for a swallowed finalize error."""
    return _bound_sweep_finalize_crash_cause(
        {
            "exception_class": type(exc).__name__,
            "message": str(exc),
            "traceback_digest": [
                f"{frame.filename}:{frame.lineno}:{frame.name}"
                for frame in traceback.extract_tb(exc.__traceback__)[-
                    _SWEEP_CRASH_FRAME_COUNT:
                ]
            ],
        }
    )


def _format_sweep_finalize_crash_cause(cause: dict[str, Any]) -> str:
    """Render the bounded crash cause for alerts and operator notifications."""
    exception_class = str(cause.get("exception_class") or "unknown")
    message = str(cause.get("message") or "<no message>")
    frames = cause.get("traceback_digest")
    frame_text = (
        " <- ".join(str(frame) for frame in frames)
        if isinstance(frames, list)
        else "<unavailable>"
    )
    return f"{exception_class}: {message}; traceback={frame_text}"
