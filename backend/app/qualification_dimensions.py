"""Canonical registry of qualification dimensions — the single source of truth.

Historically the dimension vocabulary and its per-dimension policy were declared
independently in several places that drifted apart:

  * ``qualification_state_resolver`` — the ordered UI dimension list + labels
    (``_DIMENSIONS``), the blocker-code → dimension routing (``_BLOCKER_TO_DIM``),
    the ledger-backed key set (``_LEDGER_DIMENSION_KEYS``), and the re-measured
    "mutable" key set (``_MUTABLE_DIMENSION_KEYS``).
  * ``release_qualifier`` — the promotion-required key tuple
    (``_PROMOTION_REQUIRED_DIMENSIONS``) and the dual-write ledger spec list.

Each of those encodes one facet (label, ledger-backed?, promotion-required?,
re-measured each drive?, which blocker codes route here) of the SAME set of
dimensions.  Keeping them apart let the facets fall out of sync — env preflight
and capability proof, in particular, are represented inconsistently across the
writer, the promotion-required list, the blocker map, and the read model.

This module declares every active dimension ONCE, with all of its facets, and
derives the concrete collections the two modules consume.  The registry reflects
the current smoke-retired qualification contract: qualifier VM smoke is archival
metadata, not an active dimension.  ``test_qualification_dimensions`` locks the
derived collections and routing to that contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class QualificationDimension:
    """One qualification dimension and every facet of its gate contract.

    Attributes:
        key: The stable dimension identifier written to the ledger / read model.
        label: Human-facing label surfaced by the read model.
        ledger_backed: Whether the dimension is dual-written as its own
            ``QualificationDimensionResult`` ledger row.  The two blob-only
            dimensions (``criterion_coverage``, ``goal_alignment``) are derived
            live from ``last_qualification``.
        promotion_required: Whether a terminal same-``candidate_sha`` fact for
            this dimension is required before a promotion may compose from the
            ledger.
        mutable: Whether the dimension's live result is re-measured on every
            re-drive.  A reused ledger row for a mutable dimension must not
            overwrite the freshly-measured blob-derived state.
        blocker_codes: Hard-blocker codes that route to this dimension.
    """

    key: str
    label: str
    ledger_backed: bool = False
    promotion_required: bool = False
    mutable: bool = False
    blocker_codes: tuple[str, ...] = field(default_factory=tuple)


# The canonical registry, in the read-model / UI display order.  Every derived
# collection below is computed from this single list.
QUALIFICATION_DIMENSIONS: tuple[QualificationDimension, ...] = (
    QualificationDimension(
        "audit", "Audit",
        ledger_backed=True, promotion_required=True,
        blocker_codes=("audit_missing",),
    ),
    QualificationDimension(
        "test_erosion", "Test Erosion",
        ledger_backed=True, promotion_required=True,
        blocker_codes=("test_file_deleted",),
    ),
    QualificationDimension(
        "ci", "CI",
        ledger_backed=True, promotion_required=True, mutable=True,
        blocker_codes=("ci_not_green", "ci_unresolved"),
    ),
    QualificationDimension(
        "migration_identity", "Migration Identity",
        ledger_backed=True, promotion_required=True, mutable=True,
        blocker_codes=("migration_identity_collision",),
    ),
    QualificationDimension(
        "migration_apply", "Migration Apply",
        ledger_backed=True, promotion_required=True, mutable=True,
        blocker_codes=("migration_apply_drift",),
    ),
    QualificationDimension(
        "merge_grade", "Merge Grade",
        ledger_backed=True, promotion_required=True,
        blocker_codes=("merge_grade_misses",),
    ),
    QualificationDimension(
        "challenge", "Challenge",
        ledger_backed=True,
    ),
    # acceptance_window is MUTABLE: the tally re-derives verdicts stamped under a
    # retired acceptance rule (design doc c50453ef) on every drive, so the live
    # result can change between same-candidate re-drives after an evidence-rule
    # improvement lands. A reused ledger row must never overwrite — or be
    # overlaid over — the freshly measured blob-derived state.
    QualificationDimension(
        "acceptance_window", "Acceptance Window",
        ledger_backed=True, mutable=True,
    ),
    QualificationDimension(
        "criterion_coverage", "Criterion Coverage",
        blocker_codes=("criterion_coverage_incomplete",),
    ),
    QualificationDimension(
        "debt_pressure", "Debt Pressure",
        ledger_backed=True,
    ),
    QualificationDimension(
        "goal_alignment", "Goal Alignment",
    ),
    QualificationDimension(
        "capability_proof", "Capability Proof",
        ledger_backed=True,
        blocker_codes=("capability_proof_gate_blocked",),
    ),
    # env_preflight is environment-dependent, not candidate-stable: an operator
    # repairing the deploy environment (or arming the supported one-shot
    # boot-prereq acknowledgement) must take effect on a same-candidate re-drive.
    # It is therefore ``mutable`` — re-measured live each drive, its reused ledger
    # row never composed or overlaid — so a prior boot_prereq failure cannot be
    # reused/overlaid after the environment is fixed.
    QualificationDimension(
        "env_preflight", "Env Preflight",
        ledger_backed=True, mutable=True,
        blocker_codes=("boot_prereq_missing", "boot_prereq_evidence_missing"),
    ),
)


# ── Derived collections (the shapes the two consumer modules use) ──────────────

# Ordered ``(key, label)`` pairs for the read-model dimension list.
UI_DIMENSIONS: tuple[tuple[str, str], ...] = tuple(
    (d.key, d.label) for d in QUALIFICATION_DIMENSIONS
)

# Blocker-code → dimension-key routing.
BLOCKER_TO_DIMENSION: dict[str, str] = {
    code: d.key for d in QUALIFICATION_DIMENSIONS for code in d.blocker_codes
}

# Dimensions dual-written as their own ledger row.
LEDGER_DIMENSION_KEYS: frozenset[str] = frozenset(
    d.key for d in QUALIFICATION_DIMENSIONS if d.ledger_backed
)

# Dimensions re-measured live on every re-drive.
MUTABLE_DIMENSION_KEYS: frozenset[str] = frozenset(
    d.key for d in QUALIFICATION_DIMENSIONS if d.mutable
)

# Gates that must each have a terminal same-``candidate_sha`` fact before a
# promotion may compose from the ledger. Order preserves the established
# diagnostic ordering.
PROMOTION_REQUIRED_DIMENSIONS: tuple[str, ...] = (
    "ci",
    "audit",
    "test_erosion",
    "migration_identity",
    "migration_apply",
    "merge_grade",
)


# Fail-fast if the explicit promotion-required order drifts from the registry
# flags — the ordered tuple and the ``promotion_required`` facet must describe
# the same set.
assert set(PROMOTION_REQUIRED_DIMENSIONS) == {
    d.key for d in QUALIFICATION_DIMENSIONS if d.promotion_required
}, "PROMOTION_REQUIRED_DIMENSIONS drifted from the registry promotion_required flags"
