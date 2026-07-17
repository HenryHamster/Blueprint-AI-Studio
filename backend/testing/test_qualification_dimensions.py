"""Behavior-lock tests for the canonical qualification-dimension registry.

The registry in ``app.qualification_dimensions`` consolidates the dimension
vocabulary that used to be declared independently in
``qualification_state_resolver`` and ``release_qualifier``.  These tests pin the
derived collections to the EXACT legacy literal values so the consolidation
cannot silently change behavior, and assert both consumer modules read the
registry rather than re-declaring the vocabulary.
"""

import inspect
import re

from app import qualification_dimensions as qd
from app import qualification_state_resolver as qsr
from app import release_qualifier as rq


# The historical literals, copied verbatim from the pre-refactor source.

_LEGACY_UI_DIMENSIONS = (
    ("audit", "Audit"),
    ("test_erosion", "Test Erosion"),
    ("smoketest", "Smoketest"),
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

_LEGACY_BLOCKER_TO_DIM = {
    "ci_not_green": "ci",
    "ci_unresolved": "ci",
    "migration_identity_collision": "migration_identity",
    "migration_apply_drift": "migration_apply",
    "merge_grade_misses": "merge_grade",
    "criterion_coverage_incomplete": "criterion_coverage",
    "smoketest_startup_failed": "smoketest",
    "audit_missing": "audit",
    "test_file_deleted": "test_erosion",
    "capability_proof_gate_blocked": "capability_proof",
    "boot_prereq_missing": "env_preflight",
    "boot_prereq_evidence_missing": "env_preflight",
}

_LEGACY_LEDGER_KEYS = frozenset({
    "acceptance_window", "audit", "capability_proof", "challenge", "ci",
    "debt_pressure", "env_preflight", "merge_grade",
    "migration_apply", "migration_identity", "smoketest", "test_erosion",
})

# The pre-consolidation snapshot held only the three candidate-stable-vs-mutable
# gates.  env_preflight was subsequently promoted to mutable (it is
# environment-dependent: an operator repairing the deploy environment must take
# effect on a same-candidate re-drive), so the canonical registry now carries a
# fourth mutable dimension.  This is an intentional contract change, not drift.
_LEGACY_MUTABLE_KEYS = frozenset({
    "ci", "migration_identity", "migration_apply",
})

_EXPECTED_MUTABLE_KEYS = _LEGACY_MUTABLE_KEYS | {"env_preflight"}

_LEGACY_PROMOTION_REQUIRED = (
    "ci", "audit", "test_erosion",
    "migration_identity", "migration_apply", "merge_grade",
)


def test_ui_dimensions_order_and_labels_match_legacy():
    # Order matters: the read model surfaces dimensions in this exact order.
    assert qd.UI_DIMENSIONS == _LEGACY_UI_DIMENSIONS


def test_blocker_to_dimension_matches_legacy():
    assert qd.BLOCKER_TO_DIMENSION == _LEGACY_BLOCKER_TO_DIM


def test_ledger_dimension_keys_match_legacy():
    assert qd.LEDGER_DIMENSION_KEYS == _LEGACY_LEDGER_KEYS
    # Exactly 12 dual-written dimensions.
    assert len(qd.LEDGER_DIMENSION_KEYS) == 12


def test_mutable_dimension_keys_match_legacy():
    # env_preflight joins the three legacy mutable gates — re-measured live each
    # drive so a repaired environment is honoured on a same-candidate re-drive.
    assert qd.MUTABLE_DIMENSION_KEYS == _EXPECTED_MUTABLE_KEYS
    assert "env_preflight" in qd.MUTABLE_DIMENSION_KEYS


def test_promotion_required_order_and_set_match_legacy():
    # Order matters: it drives the ``missing`` diagnostic list ordering.
    assert qd.PROMOTION_REQUIRED_DIMENSIONS == _LEGACY_PROMOTION_REQUIRED


def test_registry_has_fourteen_dimensions_with_unique_keys():
    keys = [d.key for d in qd.QUALIFICATION_DIMENSIONS]
    assert len(keys) == 14
    assert len(set(keys)) == 14


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
    """The dimension column docstring/comment lists all 12 ledger keys.

    A future operator inspecting the model documentation should see every
    ledger-backed dimension, including capability_proof.
    """
    from app.release_models import QualificationDimensionResult

    source = inspect.getsource(QualificationDimensionResult)
    assert "capability_proof" in source, (
        "QualificationDimensionResult.dimension comment is missing capability_proof"
    )
