"""Authority invariants for activation approval.

``ReleaseVersion.activation_requested_at`` is the field the deploy agent polls
before it takes a promoted version live, so it is the last authority boundary
in the pipeline.  Two principals may now stamp it -- a human admin and the
Resident -- and these tests pin the properties that keep that safe:

1. There is exactly ONE write site for the field in the whole backend.
2. Human and Resident provenance cannot be crossed by any caller.
3. Every approval is attributable: a human approval needs an operator id, a
   Resident approval needs a real task UUID.

These are standalone (no DB): they test the authority algebra and scan source,
which is precisely the part that must not be able to drift.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

from app.release_activation_approval import (
    APPROVAL_MODE_HUMAN,
    APPROVAL_MODE_RESIDENT,
    RESIDENT_ACTOR_PREFIX,
    ActivationApprover,
)
from testing.fixgate_attribution import format_offender_marker

_APP_ROOT = Path(__file__).resolve().parents[1] / "app"


def test_approval_mode_literals_are_pinned():
    """Durable audit values. Changing these silently rewrites deploy history.

    Pinned as LITERALS on purpose: asserting the constant against itself would
    certify any change to it, which is exactly the self-certifying diff this
    repo refuses.
    """
    assert APPROVAL_MODE_HUMAN == "in_product_approval"
    assert APPROVAL_MODE_RESIDENT == "resident_approval"
    assert RESIDENT_ACTOR_PREFIX == "strategic_task:"
    assert APPROVAL_MODE_HUMAN != APPROVAL_MODE_RESIDENT


def test_resident_can_never_produce_a_human_approval():
    """The structural boundary: the Resident cannot widen its own authority.

    ``ActivationApprover.resident`` hard-codes its mode, so no argument -- an
    email, the human mode string itself -- can turn a Resident approval into an
    in-product human one.
    """
    for candidate in (
        uuid.uuid4(),
        str(uuid.uuid4()),
    ):
        approver = ActivationApprover.resident(candidate)
        assert approver.mode == APPROVAL_MODE_RESIDENT
        assert approver.mode != APPROVAL_MODE_HUMAN
        assert approver.is_resident is True
        assert approver.actor.startswith(RESIDENT_ACTOR_PREFIX)

    # An operator-looking argument does not buy human provenance; it is not
    # even accepted, because it is not a task UUID.
    with pytest.raises(ValueError):
        ActivationApprover.resident("lihuans@berkeley.edu")
    with pytest.raises(ValueError):
        ActivationApprover.resident(APPROVAL_MODE_HUMAN)


def test_human_approval_requires_an_operator_id():
    """An unattributable human approval is refused rather than stamped."""
    approver = ActivationApprover.human("operator@example.com")
    assert approver.mode == APPROVAL_MODE_HUMAN
    assert approver.actor == "operator@example.com"
    assert approver.is_resident is False

    for empty in ("", "   ", None):
        with pytest.raises(ValueError):
            ActivationApprover.human(empty)


def test_resident_approval_requires_a_real_task_uuid():
    """Every Resident approval resolves back to the task that decided it."""
    task_id = uuid.uuid4()
    approver = ActivationApprover.resident(task_id)
    assert approver.actor == f"{RESIDENT_ACTOR_PREFIX}{task_id}"

    for bad in ("", None, "not-a-uuid", "strategic_task:abc"):
        with pytest.raises(ValueError):
            ActivationApprover.resident(bad)


def test_approver_is_immutable():
    """Provenance decided at construction cannot be edited afterwards."""
    approver = ActivationApprover.resident(uuid.uuid4())
    with pytest.raises(Exception):
        approver.mode = APPROVAL_MODE_HUMAN  # type: ignore[misc]


def test_activation_requested_at_has_exactly_one_write_site():
    """The deploy trigger stays a single, auditable choke point.

    Callers must go through ``approve_latest_promoted_activation`` so they
    inherit its gates. A second assignment anywhere in ``backend/app`` would be
    a path that arms a deploy without them -- which is how this field ended up
    needing a shared module in the first place.
    """
    assignment = re.compile(r"\.activation_requested_at\s*=(?!=)")
    sites: list[str] = []
    for path in sorted(_APP_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if assignment.search(line):
                sites.append(f"{path.relative_to(_APP_ROOT).as_posix()}:{lineno}")

    files = sorted({site.rsplit(":", 1)[0] for site in sites})
    assert files == ["release_activation_approval.py"], (
        "activation_requested_at must be written in exactly one place "
        "(app/release_activation_approval.py) so every approval passes the "
        f"same gates; found write sites: {sites}"
        + "\n"
        + format_offender_marker(sites)
    )
    assert len(sites) == 1, (
        "expected a single assignment inside the approval helper; "
        f"found: {sites}"
        + "\n"
        + format_offender_marker(sites)
    )
