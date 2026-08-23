"""``str(exc)`` is not a reason — the empirical root cause of a lost failure.

INCIDENT 2026-08-23. ``./blueprint promote-recover force-promote`` returned
``{"status": "promotion_failed", "error": "release_branch_failed",
"conflict": false}`` and ``./blueprint status`` reported
``"detail": null`` — for a failure whose cause GitHub had spelled out in full
("Object does not exist", because the operator passed a 9-character sha).

The reason was not missing. It was DELETED by ``detail=str(exc)[:300]``:

* the pinned Starlette ``HTTPException`` defines ``__repr__`` but no
  ``__str__``, and its ``__init__`` never calls ``Exception.__init__``;
* ``BaseException.args`` is filled only by ``BaseException.__new__`` from
  POSITIONAL constructor arguments;
* every raise site constructs it with keywords, so ``args == ()`` and
  ``str(exc) == ""``;
* ``_record_promotion_failure``'s ``if detail:`` guard then dropped the key
  entirely, so ``summarize_qualification_state`` read ``None``.

These tests pin that behaviour of the dependency (so an upgrade that changes it
is noticed) and prove the fix reads ``.detail`` explicitly instead.
"""

import pytest
from fastapi import HTTPException

from app.failure_reason import (
    DEFAULT_REASON_LIMIT,
    exception_reason,
    promotion_failure_result,
)


def test_keyword_constructed_http_exception_stringifies_to_empty():
    """THE ROOT CAUSE, pinned against the dependency.

    If a future Starlette gives ``HTTPException`` a ``__str__``, this test fails
    and tells the next reader that the workaround's premise changed. It does
    NOT mean the workaround should be removed: reading ``.detail`` explicitly is
    correct either way.
    """
    exc = HTTPException(status_code=500, detail="GitHub 422: Object does not exist")

    assert exc.args == (), "keyword-only construction leaves BaseException.args empty"
    assert str(exc) == "", (
        "str() on a keyword-constructed HTTPException is the empty string — "
        "this is why detail=str(exc)[:300] recorded nothing"
    )
    # Positional construction behaves differently, which is why the defect was
    # invisible to anyone who checked it at a REPL the other way round.
    assert str(HTTPException(500, detail="real reason")) == "500"


def test_exception_reason_recovers_the_http_exception_detail():
    """The fix: read ``.detail`` explicitly rather than trusting ``__str__``."""
    exc = HTTPException(status_code=500, detail="GitHub 422: Object does not exist")

    reason = exception_reason(exc)

    assert "Object does not exist" in reason
    # The type and the upstream status survive too — the first two questions
    # asked of any promotion failure.
    assert "HTTPException" in reason
    assert "500" in reason


def test_exception_reason_uses_str_for_ordinary_exceptions():
    """Ordinary exceptions keep their message; the type is always named."""
    assert exception_reason(ValueError("boom")) == "ValueError: boom"
    assert exception_reason(RuntimeError("")) == "RuntimeError", (
        "a message-less exception still reports WHICH exception it was"
    )


def test_exception_reason_falls_back_to_repr_when_str_is_empty():
    """An exception with neither ``detail`` nor a ``str`` still yields text."""

    class _Silent(Exception):
        def __str__(self):
            return ""

        def __repr__(self):
            return "_Silent(<opaque upstream state>)"

    reason = exception_reason(_Silent())

    assert "opaque upstream state" in reason


def test_exception_reason_renders_a_non_string_detail():
    """``HTTPException.detail`` may be any JSON-able object, not just a str."""
    exc = HTTPException(status_code=422, detail={"message": "Object does not exist"})

    assert "Object does not exist" in exception_reason(exc)


def test_exception_reason_is_bounded_and_marks_truncation():
    """Bounded before it lands on an operator-visible record."""
    # Literal pin: the historic [:300] slice, not "some bound".
    assert DEFAULT_REASON_LIMIT == 300

    reason = exception_reason(ValueError("x" * 5000))

    assert len(reason) == 300
    assert reason.endswith("..."), "a truncated reason must LOOK truncated"

    short = exception_reason(ValueError("x" * 5000), limit=40)
    assert len(short) == 40


@pytest.mark.parametrize(
    "exc",
    [
        HTTPException(status_code=502, detail="GitHub 502"),
        HTTPException(status_code=500, detail=""),
        RuntimeError("release branch still points at the wrong sha"),
        TimeoutError(),
    ],
)
def test_exception_reason_never_returns_empty(exc):
    """The whole point: a recorded failure ALWAYS carries something readable.

    An empty reason is what ``if detail:`` silently swallowed.
    """
    assert exception_reason(exc).strip()


def test_promotion_failure_result_carries_the_detail_to_the_operator():
    """The force-promote HTTP response prints this dict straight back."""
    result = promotion_failure_result(
        "release_branch_failed",
        False,
        "HTTPException(500): GitHub 422: Object does not exist",
    )

    assert result["status"] == "promotion_failed"
    assert result["error"] == "release_branch_failed"
    assert result["conflict"] is False
    assert "Object does not exist" in result["detail"], (
        "the operator lever must not print a state with no reason attached"
    )


def test_promotion_failure_result_omits_detail_when_there_is_none():
    """No fabricated empty key on the paths that genuinely have no reason."""
    result = promotion_failure_result("no_credentials")

    assert result == {
        "status": "promotion_failed",
        "error": "no_credentials",
        "conflict": False,
    }
