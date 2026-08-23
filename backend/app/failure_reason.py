"""Extract a HUMAN-READABLE reason from a caught exception.

``str(exc)`` is not a reliable reason. The pinned Starlette/FastAPI
``HTTPException`` (``starlette.exceptions``) defines ``__repr__`` but NO
``__str__``, and its ``__init__`` never calls ``Exception.__init__`` — and
``BaseException.args`` is populated only by ``BaseException.__new__`` from
POSITIONAL constructor arguments. Every raise site in this codebase builds it
with keywords (``HTTPException(status_code=..., detail=...)``), so ``args`` is
empty and ``str(exc)`` is the EMPTY STRING::

    >>> str(HTTPException(status_code=500, detail="GitHub 422: Object does not exist"))
    ''

That is exactly how the 2026-08-23 force-promote incident lost its reason: the
promotion recorder wrote ``detail=str(exc)[:300]`` → ``""``, the
``if detail:`` guard then dropped the key, and ``./blueprint status`` showed
``"detail": null`` for a failure whose cause GitHub had spelled out in full.

:func:`exception_reason` reads ``.detail`` EXPLICITLY instead of relying on
``__str__``, and always names the exception type so an empty-bodied exception
still reports something an operator can act on.

Stdlib-only and import-cycle-free so any layer can use it.
"""

from __future__ import annotations

from typing import Any

# Bound on a reason before it is persisted onto an operator-visible record.
# Matches the historical ``[:300]`` slice at the promotion recorder's call
# sites — literal pin, do not widen without checking the record consumers.
DEFAULT_REASON_LIMIT = 300

_TRUNCATION_MARKER = "..."


def _detail_text(exc: BaseException) -> str:
    """``exc.detail`` rendered as text, or ``""`` when there is no usable one.

    ``HTTPException.detail`` is usually a string but is allowed to be any
    JSON-able object, so a non-string is rendered rather than dropped.
    """
    detail = getattr(exc, "detail", None)
    if detail is None:
        return ""
    if isinstance(detail, str):
        return detail.strip()
    try:
        return str(detail).strip()
    except Exception:  # noqa: BLE001 — a broken __str__ must not mask the failure
        return ""


def _repr_text(exc: BaseException) -> str:
    """``repr(exc)`` when it carries anything beyond the bare type name.

    ``repr(RuntimeError(""))`` is ``"RuntimeError('')"`` — pure noise once the
    type is already reported, so it is dropped rather than appended.
    """
    try:
        rendered = repr(exc).strip()
    except Exception:  # noqa: BLE001 — a broken __repr__ must not mask the failure
        return ""
    type_name = type(exc).__name__
    inner = rendered
    if inner.startswith(f"{type_name}(") and inner.endswith(")"):
        inner = inner[len(type_name) + 1 : -1]
    return rendered if inner.strip().strip("\"'").strip() else ""


def exception_reason(
    exc: BaseException, limit: int = DEFAULT_REASON_LIMIT
) -> str:
    """Return ``"<ExceptionType>: <reason>"`` for *exc*, bounded to *limit*.

    The reason is sourced in this order, taking the first that yields text:

    1. ``exc.detail`` — the attribute ``HTTPException`` actually carries. Read
       explicitly BECAUSE ``str()`` on a keyword-constructed ``HTTPException``
       returns ``""`` (see the module docstring).
    2. ``str(exc)`` — the normal path for ordinary exceptions.
    3. ``repr(exc)`` — last resort, so a reason is never empty when the
       exception holds anything at all.

    The type name is ALWAYS included: a bare ``TimeoutError`` with no message
    still tells an operator more than an empty string does. For an
    ``HTTPException`` the status code is included too, since "which upstream
    status did we get" is the first question asked of a promotion failure.
    """
    type_name = type(exc).__name__
    status_code: Any = getattr(exc, "status_code", None)
    if isinstance(status_code, int) and not isinstance(status_code, bool):
        prefix = f"{type_name}({status_code})"
    else:
        prefix = type_name

    reason = _detail_text(exc)
    if not reason:
        try:
            reason = str(exc).strip()
        except Exception:  # noqa: BLE001
            reason = ""
    if not reason:
        reason = _repr_text(exc)

    text = f"{prefix}: {reason}" if reason else prefix
    if limit > 0 and len(text) > limit:
        keep = max(limit - len(_TRUNCATION_MARKER), 0)
        text = text[:keep] + _TRUNCATION_MARKER
    return text


def promotion_failure_result(
    error: str, conflict: bool = False, detail: str | None = None
) -> dict[str, Any]:
    """The dict a failed ``promote()`` returns to its caller.

    ``detail`` is carried on the RESULT and not only onto the persisted
    ``last_qualification`` record: the operator lever
    (``POST /projects/{id}/promote/force`` → ``./blueprint promote-recover
    force-promote``) prints this dict straight back, and before this it printed
    ``{"status": "promotion_failed", "error": "release_branch_failed",
    "conflict": false}`` — a state with no reason attached — even when the
    cause was known.
    """
    result: dict[str, Any] = {
        "status": "promotion_failed",
        "error": error,
        "conflict": conflict,
    }
    if detail:
        result["detail"] = detail
    return result
