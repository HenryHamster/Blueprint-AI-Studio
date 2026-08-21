"""Behavior-lock tests for the canonical qualification-dimension registry.

The registry in ``app.qualification_dimensions`` consolidates the dimension
vocabulary that used to be declared independently in
``qualification_state_resolver`` and ``release_qualifier``. These tests pin the
active qualifier contract and assert both consumers use the registry. Legacy
smoke fields remain readable on stored rows, but retired VM smoke is not an
active dimension.
"""

import inspect
import re

from app import qualification_dimensions as qd
from app import qualification_state_resolver as qsr
from app import release_qualifier as rq


# Active literals after qualifier VM-smoke retirement.

_EXPECTED_UI_DIMENSIONS = (
    ("audit", "Audit"),
    ("test_erosion", "Test Erosion"),
    ("ci", "CI"),
    ("migration_identity", "Migration Identity"),
    ("migration_apply", "Migration Apply"),
    ("merge_grade", "Merge Grade"),
    ("challenge", "Challenge"),
    ("acceptance_window", "Acceptance Window"),
    ("criterion_coverage", "Criterion Coverage"),
    ("debt_pressure", "Debt Pressure"),
    ("goal_alignment", "Goal Alignment"),
    ("capability_proof", "Capability Proof"),
    ("env_preflight", "Env Preflight"),
)

_EXPECTED_BLOCKER_TO_DIM = {
    "ci_not_green": "ci",
    "ci_unresolved": "ci",
    "migration_identity_collision": "migration_identity",
    "migration_apply_drift": "migration_apply",
    "merge_grade_misses": "merge_grade",
    "criterion_coverage_incomplete": "criterion_coverage",
    "candidate_authorship_range_identical": "criterion_coverage",
    "candidate_operator_authorship_evidence_missing": "criterion_coverage",
    "candidate_operator_authorship_nonzero": "criterion_coverage",
    "audit_missing": "audit",
    "test_file_deleted": "test_erosion",
    "capability_proof_gate_blocked": "capability_proof",
    "boot_prereq_missing": "env_preflight",
    "boot_prereq_evidence_missing": "env_preflight",
}

_EXPECTED_LEDGER_KEYS = frozenset({
    "acceptance_window", "audit", "capability_proof", "challenge", "ci",
    "debt_pressure", "env_preflight", "merge_grade",
    "migration_apply", "migration_identity", "test_erosion",
})

# The pre-consolidation snapshot held only the three candidate-stable-vs-mutable
# gates.  env_preflight was subsequently promoted to mutable (it is
# environment-dependent: an operator repairing the deploy environment must take
# effect on a same-candidate re-drive), so the canonical registry now carries a
# fourth mutable dimension.  acceptance_window joined as the fifth: its tally
# re-derives verdicts stamped under a retired acceptance rule (design doc
# c50453ef) on every drive, so a corrected measurement must land on a
# same-candidate re-drive rather than being frozen by a reused ledger row.
# This is an intentional contract change, not drift.
_LEGACY_MUTABLE_KEYS = frozenset({
    "ci", "migration_identity", "migration_apply",
})

_EXPECTED_MUTABLE_KEYS = _LEGACY_MUTABLE_KEYS | {"env_preflight", "acceptance_window"}

_LEGACY_PROMOTION_REQUIRED = (
    "ci", "audit", "test_erosion",
    "migration_identity", "migration_apply", "merge_grade",
)


def test_ui_dimensions_order_and_labels_match_active_contract():
    # Order matters: the read model surfaces dimensions in this exact order.
    assert qd.UI_DIMENSIONS == _EXPECTED_UI_DIMENSIONS
    assert "smoketest" not in dict(qd.UI_DIMENSIONS)


def test_blocker_to_dimension_matches_active_contract():
    assert qd.BLOCKER_TO_DIMENSION == _EXPECTED_BLOCKER_TO_DIM
    assert "smoketest_startup_failed" not in qd.BLOCKER_TO_DIMENSION


def test_ledger_dimension_keys_match_active_contract():
    assert qd.LEDGER_DIMENSION_KEYS == _EXPECTED_LEDGER_KEYS
    # Exactly 11 active dual-written dimensions; legacy smoke rows remain
    # deserializable but are never composed into a new promotion decision.
    assert len(qd.LEDGER_DIMENSION_KEYS) == 11


def test_mutable_dimension_keys_match_legacy():
    # env_preflight joins the three legacy mutable gates — re-measured live each
    # drive so a repaired environment is honoured on a same-candidate re-drive.
    # acceptance_window is mutable too — re-derived under the current acceptance
    # rule so an evidence-rule improvement re-scores already-merged work.
    assert qd.MUTABLE_DIMENSION_KEYS == _EXPECTED_MUTABLE_KEYS
    assert "env_preflight" in qd.MUTABLE_DIMENSION_KEYS
    assert "acceptance_window" in qd.MUTABLE_DIMENSION_KEYS


def test_promotion_required_order_and_set_match_legacy():
    # Order matters: it drives the ``missing`` diagnostic list ordering.
    assert qd.PROMOTION_REQUIRED_DIMENSIONS == _LEGACY_PROMOTION_REQUIRED


def test_registry_has_thirteen_dimensions_with_unique_keys():
    keys = [d.key for d in qd.QUALIFICATION_DIMENSIONS]
    assert len(keys) == 13
    assert len(set(keys)) == 13
    assert "smoketest" not in keys


def test_promotion_required_is_a_subset_of_ledger_backed():
    # Every promotion-required dimension must be ledger-backed (its terminal fact
    # is read back from the ledger at composition time).
    assert set(qd.PROMOTION_REQUIRED_DIMENSIONS) <= set(qd.LEDGER_DIMENSION_KEYS)


def test_promotion_required_flag_matches_ordered_tuple():
    # The registry flag and the explicit ordered tuple describe the same set —
    # the module-load assertion guards this, re-asserted here for visibility.
    flagged = {d.key for d in qd.QUALIFICATION_DIMENSIONS if d.promotion_required}
    assert flagged == set(qd.PROMOTION_REQUIRED_DIMENSIONS)


def test_blocker_codes_have_no_cross_dimension_collisions():
    # A blocker code routes to exactly one dimension.
    seen: dict[str, str] = {}
    for d in qd.QUALIFICATION_DIMENSIONS:
        for code in d.blocker_codes:
            assert code not in seen, f"{code} routes to both {seen.get(code)} and {d.key}"
            seen[code] = d.key


def test_consumers_read_the_canonical_registry():
    # The resolver's module-level ledger/mutable sets and the qualifier's
    # promotion-required tuple must be the registry objects, not re-declarations.
    assert qsr._LEDGER_DIMENSION_KEYS is qd.LEDGER_DIMENSION_KEYS
    assert qsr._MUTABLE_DIMENSION_KEYS is qd.MUTABLE_DIMENSION_KEYS
    assert rq._PROMOTION_REQUIRED_DIMENSIONS is qd.PROMOTION_REQUIRED_DIMENSIONS


def test_dual_write_spec_covers_exactly_ledger_dimension_keys():
    """The dual-write dimension spec keys must match the canonical ledger set.

    Every ledger-backed dimension in the registry must have a corresponding spec
    entry in ``_dual_write_dimension_results``, and every spec entry must be a
    registered ledger-backed dimension.  A dimension added to the registry but
    omitted from the specs list would silently never be persisted.
    """
    source = inspect.getsource(rq._dual_write_dimension_results)
    spec_keys = set(re.findall(r'"dimension":\s*"([^"]+)"', source))
    assert spec_keys == qd.LEDGER_DIMENSION_KEYS, (
        f"spec list has extra dimensions not in LEDGER_DIMENSION_KEYS: "
        f"{spec_keys - qd.LEDGER_DIMENSION_KEYS}; "
        f"LEDGER_DIMENSION_KEYS has extra not in spec list: "
        f"{qd.LEDGER_DIMENSION_KEYS - spec_keys}"
    )


def test_dimension_column_includes_capability_proof():
    """The dimension column docstring/comment lists all active ledger keys.

    A future operator inspecting the model documentation should see every
    ledger-backed dimension, including capability_proof.
    """
    from app.release_models import QualificationDimensionResult

    source = inspect.getsource(QualificationDimensionResult)
    assert "capability_proof" in source, (
        "QualificationDimensionResult.dimension comment is missing capability_proof"
    )
