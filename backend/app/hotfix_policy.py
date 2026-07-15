"""Hotfix-scope policy — the fail-closed allowlist that bounds the operator
hotfix-deploy channel to release-machinery-only fixes.

The operator hotfix-deploy channel (``POST /projects/{id}/promote/hotfix``)
fast-tracks a release-machinery fix ahead of the normal qualification gate —
the sanctioned replacement for the ad-hoc operator hand-merge.  Its WHOLE safety
is this allowlist: the lane may ship ONLY the release-machinery source files
listed in :data:`HOTFIX_ELIGIBLE_MODULES` (the gate is the thing being fixed),
and it fails CLOSED on anything it does not recognize.

:func:`evaluate_hotfix_scope` is a PURE, unit-testable predicate: it takes the
candidate's ACTUAL changed-file list (a GitHub diff — never the operator's
declared ``scope_modules``, which is only an audit cross-check) and returns an
eligibility verdict.  It rejects if ANY changed file is:

  * outside :data:`HOTFIX_ELIGIBLE_MODULES`;
  * under ``backend/alembic/versions/**`` — a hotfix that also changes schema is
    not a machinery-only fix; route it through the full gate (this keeps the
    hotfix lane from interacting with the migration-identity guard it is often
    trying to fix);
  * under ``.github/workflows/**``;
  * ``backend/app/main.py`` whose diff alters the ``EXPECTED_SCHEMA_REVISION``
    constant — a revision bump without the matching migration deployed wedges
    boot, so route it via the full gate (design §5).

An empty diff is also rejected: there is nothing to hotfix-deploy, and
fail-closed is the safe stance.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Release-machinery source files that are safe to fast-track through the hotfix
# lane because the gate itself is the thing being fixed (design §2.2).  This
# frozenset is the entire authorization surface of the lane — adding a path here
# widens what the operator hotfix channel may ship, so keep it minimal.
HOTFIX_ELIGIBLE_MODULES: frozenset[str] = frozenset(
    {
        "backend/app/release_scoring.py",
        "backend/app/release_qualifier.py",
        "backend/app/release_qualifier_audit.py",
        "backend/app/release_qualifier_sweep.py",
        "backend/app/release_promotion.py",
        "backend/app/migration_chain.py",
        "backend/app/rollup_migration_validation.py",
        "backend/app/migration_apply_gate.py",
        "backend/app/migration_reconciliation.py",
        "backend/app/acceptance_criteria.py",
        "backend/app/acceptance_gate.py",
        "backend/app/category_verdict_ledger.py",
        "backend/app/qualification_state_resolver.py",
        # The scope validator itself must be hotfixable so the channel can
        # repair its own authorization surface.
        "backend/app/hotfix_policy.py",
        # main.py is allowlisted but GUARDED: a diff that alters
        # EXPECTED_SCHEMA_REVISION is rejected below (a revision bump without the
        # matching migration deployed wedges boot).
        "backend/app/main.py",
    }
)

MAIN_PY_PATH = "backend/app/main.py"
_MIGRATIONS_PREFIX = "backend/alembic/versions/"
_WORKFLOWS_PREFIX = ".github/workflows/"
_EXPECTED_SCHEMA_REVISION_TOKEN = "EXPECTED_SCHEMA_REVISION"


@dataclass(frozen=True)
class HotfixScopeVerdict:
    """The result of a fail-closed hotfix-scope check.

    ``eligible`` is True only when every changed file is machinery-only and no
    guard tripped.  ``offending_files`` lists the specific paths that made the
    diff ineligible (empty when eligible), and ``reason`` is a human-readable
    justification for the audit trail / the 400 response detail.
    """

    eligible: bool
    reason: str
    offending_files: list[str] = field(default_factory=list)


def _patch_touches_schema_revision(patch: str | None) -> bool:
    """Return True iff a unified-diff ``patch`` adds/removes an
    ``EXPECTED_SCHEMA_REVISION`` line.

    Fail-closed: a ``None`` patch (the diff for main.py could not be fetched)
    is treated as touching the constant, because the guard cannot be verified.
    Only added (``+``) / removed (``-``) hunk lines are inspected — the ``+++``/
    ``---`` file headers are skipped so a filename containing the token cannot
    trip the guard.
    """
    if patch is None:
        return True
    for line in patch.splitlines():
        if not line or line[0] not in "+-":
            continue
        if line.startswith(("+++", "---")):
            continue
        if _EXPECTED_SCHEMA_REVISION_TOKEN in line:
            return True
    return False


def evaluate_hotfix_scope(
    changed_files: list[str] | None,
    *,
    main_py_patch: str | None = None,
) -> HotfixScopeVerdict:
    """Fail-closed scope check for the hotfix-deploy lane.

    ``changed_files`` is the candidate's ACTUAL changed-file list (a GitHub
    diff), NOT the operator-declared ``scope_modules``.  ``main_py_patch`` is the
    unified diff of ``backend/app/main.py`` when that file is among the changed
    files (used only to detect an ``EXPECTED_SCHEMA_REVISION`` bump); it may be
    ``None`` when main.py is unchanged.

    Returns a :class:`HotfixScopeVerdict`.  The lane is eligible ONLY when every
    changed file is in :data:`HOTFIX_ELIGIBLE_MODULES`, none is a migration or a
    workflow file, and any main.py change leaves ``EXPECTED_SCHEMA_REVISION``
    untouched.  Anything unexpected — including an empty diff — is rejected.
    """
    files = list(changed_files or [])
    if not files:
        return HotfixScopeVerdict(
            eligible=False,
            reason=(
                "no changed files between the promotion target and the candidate "
                "— nothing to hotfix-deploy (fail-closed)"
            ),
            offending_files=[],
        )

    offending: list[str] = []
    reasons: list[str] = []

    for path in files:
        if path in offending:
            continue
        if path.startswith(_MIGRATIONS_PREFIX):
            offending.append(path)
            reasons.append(
                f"{path} is a migration — a schema change is not a machinery-only "
                "hotfix; route it through the full gate"
            )
            continue
        if path.startswith(_WORKFLOWS_PREFIX):
            offending.append(path)
            reasons.append(f"{path} is a CI workflow file, not release machinery")
            continue
        if path not in HOTFIX_ELIGIBLE_MODULES:
            offending.append(path)
            reasons.append(f"{path} is outside the release-machinery allowlist")

    # main.py is allowlisted but guarded: refuse an EXPECTED_SCHEMA_REVISION bump.
    if MAIN_PY_PATH in files and _patch_touches_schema_revision(main_py_patch):
        if MAIN_PY_PATH not in offending:
            offending.append(MAIN_PY_PATH)
        reasons.append(
            f"{MAIN_PY_PATH} alters EXPECTED_SCHEMA_REVISION — a revision bump "
            "without the matching migration deployed wedges boot; route it "
            "through the full gate"
        )

    if offending:
        return HotfixScopeVerdict(
            eligible=False,
            reason="; ".join(reasons),
            offending_files=offending,
        )
    return HotfixScopeVerdict(
        eligible=True,
        reason="all changed files are allowlisted release machinery",
        offending_files=[],
    )
