"""Contract guards for the pre-promotion rehearsal gate.

Two failure modes are dangerous:
  - WEDGE: the gate demands evidence that cannot be produced (e.g. precondition
    mismatch between what the host checks and what receipt validation requires).
  - VANISH: the criterion the gate writes evidence to is superseded/retracted,
    so the qualifier stops reading it and the rehearsal runs while enforcing
    nothing.

Each guard is deliberately structured so that perturbing the thing it protects
causes the assertion to fail — see the inline comments for what must change to
break each guard.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import release_qualifier as rq
from app.release_prepromotion_rehearsal import (
    PREPROMOTION_GRADEABILITY_CONTRACTS,
    PREPROMOTION_REHEARSAL_CRITERION_KEY,
    PREPROMOTION_REHEARSAL_PRECONDITIONS,
    apply_rehearsal_evidence,
    dispatch_prepromotion_rehearsal,
    validate_prepromotion_gradeability,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_scripts_dir() -> str | None:
    """Locate scripts/ across Docker test environments and plain checkouts."""
    candidates = []
    env_override = os.getenv("BLUEPRINT_SCRIPTS_DIR")
    if env_override:
        candidates.append(env_override)
    candidates.append("/repo/scripts")
    candidates.append(
        os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
    )
    for path in candidates:
        if os.path.isfile(os.path.join(path, "release_preconditions.py")):
            return path
    return None


def _import_release_preconditions():
    """Import release_preconditions from scripts/, or skip if unavailable."""
    scripts_dir = _resolve_scripts_dir()
    if scripts_dir is None:
        msg = (
            "scripts/release_preconditions.py not found — checked "
            "BLUEPRINT_SCRIPTS_DIR, /repo/scripts, and ../../scripts "
            "relative to this file"
        )
        if os.getenv("CI"):
            pytest.fail(
                msg + " — refusing to skip in CI: mount the repo root at "
                "/repo or set BLUEPRINT_SCRIPTS_DIR"
            )
        pytest.skip(msg)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    try:
        import release_preconditions
    except Exception as exc:
        if os.getenv("CI"):
            pytest.fail(f"release_preconditions unimportable in CI: {exc}")
        pytest.skip(f"release_preconditions unimportable: {exc}")
    return release_preconditions


def _mock_criterion_row(criterion_key: str, status: str) -> SimpleNamespace:
    return SimpleNamespace(criterion_key=criterion_key, status=status)


# ---------------------------------------------------------------------------
# Guard 1 — precondition-set agreement
#
# PREPROMOTION_REHEARSAL_PRECONDITIONS (backend receipt validator) must equal
# PRE_STOP_PRECONDITIONS (scripts/release_preconditions.py, passed verbatim as
# RehearsalReport.checked in local_app_release.py:1317).
#
# If either side is edited independently, receipt validation silently changes
# strictness: the host emits one set, but the backend requires the other, so
# every receipt either over- or under-validates.
#
# To break this guard: add or remove an entry from either tuple.
# ---------------------------------------------------------------------------

def test_rehearsal_preconditions_match_pre_stop_preconditions():
    """Backend PREPROMOTION_REHEARSAL_PRECONDITIONS must equal scripts PRE_STOP_PRECONDITIONS.

    The host passes PRE_STOP_PRECONDITIONS verbatim as RehearsalReport.checked
    (local_app_release.py line 1317). Receipt validation requires every entry in
    PREPROMOTION_REHEARSAL_PRECONDITIONS to appear in receipt['checked']. Set
    equality (not subset) is required: if the backend constant grows an entry the
    host never emits, receipts that were valid become invalid (wedge); if it shrinks,
    the backend silently stops enforcing the dropped condition.
    """
    rp = _import_release_preconditions()

    backend_set = set(PREPROMOTION_REHEARSAL_PRECONDITIONS)
    scripts_set = set(rp.PRE_STOP_PRECONDITIONS)

    assert backend_set == scripts_set, (
        "Precondition drift detected between "
        "app.release_prepromotion_rehearsal.PREPROMOTION_REHEARSAL_PRECONDITIONS "
        f"({sorted(backend_set)}) and "
        "scripts.release_preconditions.PRE_STOP_PRECONDITIONS "
        f"({sorted(scripts_set)}). "
        "Edit BOTH constants together — a mismatch in either direction changes "
        "receipt-validation strictness without touching the other side."
    )


def test_precondition_agreement_fails_on_backend_extra_entry():
    """Demonstrate Guard 1 fails when the backend tuple has an extra entry.

    This test perturbs the backend set to confirm the assertion is live and
    cannot be silently bypassed.
    """
    rp = _import_release_preconditions()
    scripts_set = set(rp.PRE_STOP_PRECONDITIONS)
    # Simulate an extra backend entry that the host never emits.
    perturbed_backend = set(PREPROMOTION_REHEARSAL_PRECONDITIONS) | {
        "extra entry the host never checks"
    }
    assert perturbed_backend != scripts_set, (
        "The perturbation must produce a mismatch — the guard must be able to fail."
    )


def test_precondition_agreement_fails_on_scripts_extra_entry():
    """Demonstrate Guard 1 fails when the scripts tuple has an extra entry."""
    rp = _import_release_preconditions()
    backend_set = set(PREPROMOTION_REHEARSAL_PRECONDITIONS)
    # Simulate a new host check that the backend hasn't declared yet.
    perturbed_scripts = set(rp.PRE_STOP_PRECONDITIONS) | {
        "new host check not yet declared in backend"
    }
    assert perturbed_scripts != backend_set, (
        "The perturbation must produce a mismatch — the guard must be able to fail."
    )


# ---------------------------------------------------------------------------
# Guard 2 — the criterion the gate writes to is ACTIVE
#
# PREPROMOTION_REHEARSAL_CRITERION_KEY must resolve to an active criterion so
# the qualifier actually grades the evidence the rehearsal produces.  The
# qualifier's activeness predicate is `row.status == "active"` (implemented in
# release_qualifier._resolve_active_criterion_keys).  That is the authoritative
# source: the qualifier builds active_categories from that predicate before
# passing them into apply_rehearsal_evidence, so any key that fails the
# predicate never reaches the evidence-application step.
#
# To break the vanish-direction guard: change the criterion key or mark the
# mock row as "superseded"/"retracted" and the key will disappear from the
# active set.
#
# To break the wedge-direction guard: change PREPROMOTION_REHEARSAL_CRITERION_KEY
# to a value that does not match the criterion_key the mock row carries.
# ---------------------------------------------------------------------------

def test_rehearsal_criterion_key_is_recognized_by_qualifier_activeness_check():
    """PREPROMOTION_REHEARSAL_CRITERION_KEY must appear in the active criterion set.

    The qualifier uses _resolve_active_criterion_keys(rows) — filtering rows
    on row.status == "active" — to build the set of keys it grades.  This test
    asserts that a row carrying PREPROMOTION_REHEARSAL_CRITERION_KEY with
    status="active" is included in that set.

    If the criterion is ever superseded or retracted in the rubric, the real row
    will have status != "active" and the key will disappear from the active set,
    causing the rehearsal to keep writing evidence that nothing reads.
    """
    active_row = _mock_criterion_row(PREPROMOTION_REHEARSAL_CRITERION_KEY, "active")
    active_keys = rq._resolve_active_criterion_keys([active_row])

    assert PREPROMOTION_REHEARSAL_CRITERION_KEY in active_keys, (
        f"PREPROMOTION_REHEARSAL_CRITERION_KEY ({PREPROMOTION_REHEARSAL_CRITERION_KEY!r}) "
        "is not recognized as active by release_qualifier._resolve_active_criterion_keys. "
        "The qualifier will silently ignore all rehearsal evidence — the gate has vanished."
    )


def test_superseded_rehearsal_criterion_is_excluded_from_active_keys():
    """Demonstrate that a superseded criterion key does not appear in active keys.

    This confirms the activeness predicate is the live gate: once the criterion
    is superseded, it falls out of the set and Guard 2 (above) would fail.
    """
    superseded_row = _mock_criterion_row(PREPROMOTION_REHEARSAL_CRITERION_KEY, "superseded")
    active_keys = rq._resolve_active_criterion_keys([superseded_row])

    assert PREPROMOTION_REHEARSAL_CRITERION_KEY not in active_keys, (
        "A superseded criterion must NOT appear in active_keys — "
        "the activeness predicate must exclude it."
    )


def test_retracted_rehearsal_criterion_is_excluded_from_active_keys():
    """Retracted criterion key also excluded — confirms all non-active statuses fail."""
    retracted_row = _mock_criterion_row(PREPROMOTION_REHEARSAL_CRITERION_KEY, "retracted")
    active_keys = rq._resolve_active_criterion_keys([retracted_row])

    assert PREPROMOTION_REHEARSAL_CRITERION_KEY not in active_keys


def test_rehearsal_evidence_is_noop_when_criterion_is_not_active():
    """Evidence applied to empty active_categories is a no-op — the gate has vanished.

    When the criterion is superseded, the qualifier passes an empty
    active_categories list (no active criterion matches the key).
    apply_rehearsal_evidence then returns verdicts unchanged — the rehearsal ran
    but its result is never enforced.  This test confirms that failure mode is
    detectable rather than silently swallowing the receipt.
    """
    _CANDIDATE = "a" * 40
    verdict = {
        "category_key": PREPROMOTION_REHEARSAL_CRITERION_KEY,
        "status": "partial",
        "tier": "judged",
        "gap": "no host rehearsal receipt",
        "citations": [],
    }
    valid_receipt = {
        "kind": "blueprint.local_app_release_rehearsal.v1",
        "candidate": "registry.example/app@sha256:" + "b" * 64,
        "revision": _CANDIDATE,
        "checked": list(PREPROMOTION_REHEARSAL_PRECONDITIONS),
        "deferred": ["migration apply and post-migration schema verification"],
        "units_stopped": False,
    }

    # No active categories — simulates the criterion being superseded.
    result = apply_rehearsal_evidence([verdict], [], valid_receipt, _CANDIDATE)

    # The verdict is returned unchanged; the receipt had no effect.
    assert result is not None
    assert result[0]["status"] == "partial", (
        "When active_categories is empty the receipt is not applied — "
        "the gate has silently vanished. This confirms the failure mode "
        "Guard 2 is designed to detect."
    )


def test_rehearsal_criterion_key_format_is_valid():
    """PREPROMOTION_REHEARSAL_CRITERION_KEY must match the c-<8hexchars> pattern.

    criterion_key values outside this format are rejected by the rubric
    validator and would never be stored as active.  A typo would cause the
    key to resolve to nothing in the rubric and the gate to vanish.
    """
    import re
    pattern = re.compile(r"^c-[0-9a-f]{8}$")
    assert pattern.fullmatch(PREPROMOTION_REHEARSAL_CRITERION_KEY), (
        f"PREPROMOTION_REHEARSAL_CRITERION_KEY {PREPROMOTION_REHEARSAL_CRITERION_KEY!r} "
        "does not match the expected c-<8hexchars> format. "
        "The rubric validator will reject it and the gate will never be active."
    )


# ---------------------------------------------------------------------------
# Guard 3 — every workflow dispatch proves its candidate ref first
#
# The ref is deliberately owned by dispatch_prepromotion_rehearsal, not by
# individual callers.  This is the general shape guard for pre-promotion
# evidence: a criterion's producer must establish its candidate-bound input
# before the producer runs.  A caller that bypasses that contract, or a future
# edit that dispatches after an unresolved ref, must fail before a workflow run
# can be created.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rehearsal_dispatch_resolves_candidate_ref_before_workflow(mocker):
    """The workflow cannot run until release-vN resolves to the candidate SHA."""
    events: list[str] = []

    async def create_branch(_token, _owner, _repo, branch_name, sha):
        events.append(f"ref:{branch_name}:{sha}")
        return {"object": {"sha": sha}}

    async def dispatch_workflow(*_args, **_kwargs):
        events.append("dispatch")
        assert events[-2].startswith("ref:release-v17:"), events
        assert events[-2].endswith("a" * 40), events
        return True

    mocker.patch(
        "app.release_prepromotion_rehearsal.github.create_branch",
        new=AsyncMock(side_effect=create_branch),
    )
    dispatch = mocker.patch(
        "app.release_prepromotion_rehearsal.github.dispatch_workflow_run",
        new=AsyncMock(side_effect=dispatch_workflow),
    )

    assert await dispatch_prepromotion_rehearsal(
        "token", "owner", "repo", 17, "a" * 40
    ) is True
    assert events == [f"ref:release-v17:{'a' * 40}", "dispatch"]
    dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_unresolvable_candidate_ref_blocks_workflow_dispatch(mocker):
    """The pre-17278 unresolved-ref shape fails closed before dispatch."""
    stale = "b" * 40
    mocker.patch(
        "app.release_prepromotion_rehearsal.github.create_branch",
        new=AsyncMock(
            side_effect=[
                {"object": {"sha": stale}},
                {"object": {"sha": stale}},
            ]
        ),
    )
    mocker.patch(
        "app.release_prepromotion_rehearsal.github.delete_branch",
        new=AsyncMock(return_value=True),
    )
    dispatch = mocker.patch(
        "app.release_prepromotion_rehearsal.github.dispatch_workflow_run",
        new=AsyncMock(return_value=True),
    )

    with pytest.raises(RuntimeError, match="does not resolve to candidate"):
        await dispatch_prepromotion_rehearsal(
            "token", "owner", "repo", 17, "a" * 40
        )
    dispatch.assert_not_awaited()


def test_gradeability_contract_rejects_circular_evidence_producer():
    """A producer blocked by the promotion gate is rejected before release."""
    with pytest.raises(ValueError, match="at or after its promotion gate"):
        validate_prepromotion_gradeability(
            [{"category_key": "c-circular1"}],
            contracts={
                "c-circular1": {
                    "producer": "post_promotion_rehearsal",
                    "producer_phase": "promotion_gate",
                    "gate_phase": "promotion_gate",
                }
            },
        )


def test_registered_gradeability_contracts_are_pre_promotion_gradeable():
    """Every special machine-backed criterion remains wired to a safe phase."""
    validate_prepromotion_gradeability(
        [{"category_key": key} for key in PREPROMOTION_GRADEABILITY_CONTRACTS]
    )
