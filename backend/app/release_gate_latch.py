"""Deployed-gate latch detection for a blocked release version.

Extracted from ``release_stall_alerts`` so the stall-alert module stays inside
its size-governance cap. This module owns ONE question: when an open version is
blocked by a release gate, does the blocked candidate itself carry the repair to
the DEPLOYED implementation of that gate? A candidate in that shape can never
clear the gate from inside the qualifier — the validator runs in the deployed
backend — so re-cutting the candidate loops forever and only a deploy or the
hotfix lane can break it.

It also owns where the terminal facts for that question come from:
:class:`QualificationVerdict` (append-only, one row per finalized qualification)
rather than ``ReleaseVersion.last_qualification`` (overwritten on every finalize
AND again by the auto-retrigger's pending stub seconds later).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import github, release_version
from app.models import Alert, AlertSeverity, Project
from app.notification_dispatch import post_event_message
from app.qualification_state_resolver import (
    resolve_newest_finalized_qualification_verdict,
)
from app.release_promotion import _resolve_github_with_session
from app.release_qualifier_audit_surface import (
    _changed_audited_surface_paths,
    audited_surface_path_map,
)

_MODULE_SIZE_GOVERNANCE_CAP: int = 600

logger = logging.getLogger(__name__)


def _release_qualifier_module():
    from app import release_qualifier as rq

    return rq


_BLOCKER_LITERAL = re.compile(
    r"(?:\bblocker\s*=\s*|['\"]blocker['\"]\s*:\s*)['\"]([a-z0-9_]+)['\"]"
)
_GATE_SOURCES = (
    # Keep this inventory aligned with every module that can persist a release
    # qualification blocker.  The qualifier was split into lifecycle, audit,
    # and reconciliation modules; scanning only the original two modules makes
    # a self-repair in an extracted gate indistinguishable from no repair.
    ("backend/app/release_scoring.py", Path(__file__).with_name("release_scoring.py")),
    ("backend/app/release_qualifier.py", Path(__file__).with_name("release_qualifier.py")),
    (
        "backend/app/release_qualifier_sweep.py",
        Path(__file__).with_name("release_qualifier_sweep.py"),
    ),
    (
        "backend/app/release_qualifier_audit.py",
        Path(__file__).with_name("release_qualifier_audit.py"),
    ),
    (
        "backend/app/release_reconciliation.py",
        Path(__file__).with_name("release_reconciliation.py"),
    ),
    (
        "backend/app/release_stall_alerts.py",
        Path(__file__).with_name("release_stall_alerts.py"),
    ),
)


# The literal scan above only finds where a blocker STRING is emitted, which
# is a proxy for "implements the gate" — it is blind whenever the measurement
# predicate that actually decides the gate lives in a module that never
# spells out the blocker literal itself (e.g. release_commit_authorship.py
# computes the authorship count/evidence; release_scoring.py only turns that
# result into the "candidate_operator_authorship_nonzero" string). A genuine
# repair landing only in the predicate module then fails to intersect
# gate_implementing_paths, detect_self_modifying_gate returns None, and the
# latch alert never fires (see commit 2a5a98336). This map is the explicit,
# documented correction: blocker code -> deployed module(s) that carry the
# gate's actual decision (measurement predicate, evidence producer, ledger
# composition), unioned with the literal scan rather than replacing it.
#
# Membership is restricted to blockers classified ``EXECUTION_SITE_DEPLOYED``
# in ``release_scoring.BLOCKER_EXECUTION_SITE``: the latch only makes a claim
# about gates whose validator runs in the DEPLOYED backend, so naming an extra
# module for a candidate-side gate (e.g. ``audit_missing``, which is decided by
# ``audit_score is None`` from the audit child's logs) would arm the latch
# branch on an unrelated file touch, emit a wrong operator instruction, and
# suppress the ordinary calcification alert. ``test_release_stall_alerts.py``
# pins that consistency.
_SUPPLEMENTAL_GATE_IMPLEMENTING_PATHS: dict[str, tuple[str, ...]] = {
    "candidate_operator_authorship_nonzero": (
        "backend/app/release_commit_authorship.py",
    ),
    "candidate_operator_authorship_evidence_missing": (
        "backend/app/release_commit_authorship.py",
    ),
    "criterion_coverage_incomplete": (
        "backend/app/acceptance_criteria.py",
        "backend/app/category_verdict_ledger.py",
    ),
}


def gate_implementing_paths(blocker: str) -> list[str]:
    """Return deployed modules that implement the gate for *blocker*.

    Unions the literal-string scan (which modules emit the blocker code) with
    the explicit :data:`_SUPPLEMENTAL_GATE_IMPLEMENTING_PATHS` correction for
    gates whose decision is made in a module that never spells out the
    literal — see that map's docstring for why the scan alone is unreliable.
    """
    if not isinstance(blocker, str) or not blocker:
        return []
    scanned = {
        name for name, source_path in _GATE_SOURCES
        if blocker in _BLOCKER_LITERAL.findall(source_path.read_text())
    }
    supplemental = set(_SUPPLEMENTAL_GATE_IMPLEMENTING_PATHS.get(blocker, ()))
    return sorted(scanned | supplemental)


def detect_self_modifying_gate(
    blocker: str,
    deployed_surface: dict[str, str] | None,
    candidate_surface: dict[str, str] | None,
) -> dict[str, object] | None:
    """Measure whether a candidate edits the deployed implementation of *blocker*."""
    if not isinstance(deployed_surface, dict) or not isinstance(candidate_surface, dict):
        return None
    changed = _changed_audited_surface_paths(deployed_surface, candidate_surface)
    implicated = sorted(set(changed) & set(gate_implementing_paths(blocker)))
    if not implicated:
        return None
    return {
        "blocker": blocker,
        "implicated_paths": implicated,
        "deployed_blobs": {path: deployed_surface.get(path) for path in implicated},
        "candidate_blobs": {path: candidate_surface.get(path) for path in implicated},
    }


async def _audited_surface_at_ref(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    ref: str,
) -> dict[str, str] | None:
    """Fetch the complete audited tree map at an arbitrary *ref*.

    Used for BOTH sides of the latch comparison: the deployed promotion target
    and the candidate SHA the durable verdict names. Deriving the candidate side
    from the repository tree is what makes the detector independent of
    ``last_qualification["audited_surface_paths"]``, which the auto-retrigger
    stub erases seconds after finalize.

    Failure semantics are unchanged: any GitHub error is logged and degrades to
    ``None`` so the caller falls back to the ordinary calcification alert; it
    never raises into the sweep.
    """
    try:
        project = await session.get(Project, version.project_id)
        if project is None:
            return None
        resolved = await _resolve_github_with_session(session, project)
        if resolved is None:
            return None
        token, owner, repo = resolved
        entries = await github.list_repo_tree_paths_at_ref(
            token, owner, repo, ref, project_id=str(project.id)
        )
    except Exception:  # noqa: BLE001 — diagnosis degrades to the old alert
        logger.warning(
            "[RELEASE_QUALIFIER] audited-surface lookup at %s failed for v%d",
            ref,
            version.version_number,
            exc_info=True,
        )
        return None
    return None if entries is None else audited_surface_path_map(entries)


async def _deployed_audited_surface(
    session: AsyncSession, version: release_version.ReleaseVersion
) -> tuple[str, dict[str, str]] | None:
    """Fetch the complete audited tree at the live promotion target."""
    try:
        project = await session.get(Project, version.project_id)
        if project is None:
            return None
        resolved = await _resolve_github_with_session(session, project)
        if resolved is None:
            return None
        token, owner, repo = resolved
        _, target = _release_qualifier_module()._lts_branch_names(project)
        target_sha = await github.get_branch_sha(
            token, owner, repo, target, project_id=str(project.id)
        )
    except Exception:  # noqa: BLE001 — diagnosis degrades to the old alert
        logger.warning(
            "[RELEASE_QUALIFIER] deployed audited-surface lookup failed for v%d",
            version.version_number,
            exc_info=True,
        )
        return None
    surface = await _audited_surface_at_ref(session, version, target_sha)
    return None if surface is None else (target_sha, surface)


@dataclass(frozen=True)
class TerminalQualificationFacts:
    """The blocked-state facts a stall detector needs, and where they came from.

    ``source`` is ``"verdict"`` when the facts came from the append-only
    :class:`~app.models.QualificationVerdict` row and ``"last_qualification"``
    when they were read from the mutable blob (legacy versions with no verdict
    row).
    """

    status: str | None
    blocker: str | None
    candidate_sha: str | None
    sub_scores: dict | None
    source: str


async def _resolve_terminal_qualification_facts(
    session: AsyncSession, version: release_version.ReleaseVersion
) -> TerminalQualificationFacts:
    """Resolve the version's blocked state from the DURABLE verdict record.

    ``version.last_qualification`` is overwritten on every finalize AND again by
    the auto-retrigger, which stamps a pending stub seconds later; the reaper
    sweep that runs these checks cannot reliably observe that window, so the
    mutable blob is not a usable source of terminal facts. The append-only
    verdict row is. ``last_qualification`` is used only when no finalized verdict
    row exists at all (legacy versions predating the verdict table).
    """
    qual = (
        version.last_qualification
        if isinstance(version.last_qualification, dict)
        else {}
    )
    verdict = None
    try:
        verdict = await resolve_newest_finalized_qualification_verdict(
            session, version
        )
    except Exception:  # noqa: BLE001 — degrade to the blob, never break the sweep
        logger.warning(
            "[RELEASE_QUALIFIER] finalized-verdict lookup failed for v%d",
            version.version_number,
            exc_info=True,
        )
        verdict = None
    if verdict is not None:
        return TerminalQualificationFacts(
            status=verdict.terminal_status or None,
            blocker=verdict.blocker,
            candidate_sha=verdict.candidate_sha,
            sub_scores=(
                verdict.sub_scores if isinstance(verdict.sub_scores, dict) else None
            ),
            source="verdict",
        )
    sub_scores = qual.get("sub_scores")
    return TerminalQualificationFacts(
        status=qual.get("status"),
        blocker=qual.get("blocker"),
        candidate_sha=qual.get("candidate_sha"),
        sub_scores=sub_scores if isinstance(sub_scores, dict) else None,
        source="last_qualification",
    )


def _deployed_gate_latch_details(
    version: release_version.ReleaseVersion,
    *,
    blocker: str | None = None,
    candidate_sha: str | None = None,
    base_tree_entries: list[dict] | None = None,
    candidate_tree_entries: list[dict] | None = None,
    deployed_surface: dict[str, str] | None = None,
    candidate_surface: dict[str, str] | None = None,
    compare_base_sha: str | None = None,
) -> tuple[str, list[str], str] | None:
    """Return ``(gate, implicated_paths, compare_range)`` for a self-repair.

    A missing identity is indeterminate, not an empty range: without both SHAs
    the operator-facing alert would claim evidence it cannot name.

    *blocker* and *candidate_sha* are the terminal facts resolved from the
    durable verdict (see :func:`_resolve_terminal_qualification_facts`). When
    neither is supplied the function falls back to ``last_qualification`` for a
    legacy version that has no verdict row.
    """
    qual = version.last_qualification or {}
    if version.status != "open":
        return None
    if blocker is None and candidate_sha is None:
        if qual.get("status") != "not_promoted":
            return None
        blocker = qual.get("blocker")
        candidate_sha = qual.get("candidate_sha")
    if not isinstance(blocker, str) or not blocker:
        return None
    if not gate_implementing_paths(blocker):
        return None
    base_sha = compare_base_sha or version.base_sha
    if not isinstance(base_sha, str) or not base_sha:
        return None
    if not isinstance(candidate_sha, str) or not candidate_sha:
        return None
    if deployed_surface is None and base_tree_entries is not None:
        deployed_surface = audited_surface_path_map(base_tree_entries)
    if candidate_surface is None and candidate_tree_entries is not None:
        candidate_surface = audited_surface_path_map(candidate_tree_entries)
    detected = detect_self_modifying_gate(blocker, deployed_surface, candidate_surface)
    if detected is None:
        return None
    implicated = cast(list[str], detected["implicated_paths"])
    if not implicated:
        return None
    return str(blocker), implicated, f"{base_sha}...{candidate_sha}"


def _deployed_gate_latch_dedupe_key(
    version: release_version.ReleaseVersion, gate: str, compare_range: str
) -> str:
    return f"release-gate-latch:{version.id}:{gate}:{compare_range}"[:255]


_DEDUPE_KEY_PREFIX = "release-gate-latch:"


async def resolve_active_gate_latch_alert(
    session: AsyncSession, version_id
) -> dict[str, object] | None:
    """Return the active (undismissed) deployed-gate-latch Alert for *version_id*.

    Read-only counterpart to :func:`_check_deployed_gate_latch`: rather than
    recomputing the GitHub-surface diff live on every lifecycle poll (too
    expensive for a 30s-cadence read endpoint), this reuses the Alert row the
    sweep already persisted. Returns ``None`` when no such alert exists (the
    common case) or the version has never latched a gate.
    """
    dedupe_prefix = f"{_DEDUPE_KEY_PREFIX}{version_id}:"
    alert = await session.scalar(
        select(Alert)
        .where(
            Alert.dedupe_key.is_not(None),
            Alert.dedupe_key.startswith(dedupe_prefix),
            Alert.dismissed_at.is_(None),
        )
        .order_by(Alert.created_at.desc())
        .limit(1)
    )
    if alert is None:
        return None
    gate = None
    compare_range = None
    if isinstance(alert.dedupe_key, str):
        remainder = alert.dedupe_key[len(dedupe_prefix):]
        parts = remainder.split(":", 1)
        if len(parts) == 2:
            gate, compare_range = parts
    return {
        "gate": gate,
        "compare_range": compare_range,
        "message": alert.message,
        "detected_at": alert.created_at.isoformat() if alert.created_at else None,
    }


async def _check_deployed_gate_latch(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    *,
    blocker: str | None = None,
    candidate_sha: str | None = None,
    base_tree_entries: list[dict] | None = None,
    candidate_tree_entries: list[dict] | None = None,
    deployed_surface: dict[str, str] | None = None,
    candidate_surface: dict[str, str] | None = None,
    compare_base_sha: str | None = None,
) -> bool:
    """Alert when a blocked candidate carries the deployed gate's repair."""
    details = _deployed_gate_latch_details(
        version,
        blocker=blocker,
        candidate_sha=candidate_sha,
        base_tree_entries=base_tree_entries,
        candidate_tree_entries=candidate_tree_entries,
        deployed_surface=deployed_surface,
        candidate_surface=candidate_surface,
        compare_base_sha=compare_base_sha,
    )
    if details is None:
        return False
    gate, implicated_paths, compare_range = details
    dedupe_key = _deployed_gate_latch_dedupe_key(version, gate, compare_range)
    existing = await session.scalar(
        select(Alert).where(
            Alert.project_id == version.project_id,
            Alert.dedupe_key == dedupe_key,
            Alert.dismissed_at.is_(None),
        ).limit(1)
    )
    if existing is not None:
        return False

    paths_text = ", ".join(implicated_paths)
    summary = (
        f"v{version.version_number} is blocked by gate {gate}, but the candidate "
        f"modifies its deployed implementation: {paths_text}. Compare range "
        f"{compare_range}. This repair cannot clear the gate from inside the "
        "qualifier; use the approved release-machinery hotfix lane."
    )
    session.add(
        Alert(
            project_id=version.project_id,
            event_type="release_blocked",
            severity=AlertSeverity.ERROR,
            domain="release_lifecycle",
            source="release_stall_alerts",
            dedupe_key=dedupe_key,
            title=f"Release v{version.version_number} gate latched: {gate}",
            message=summary,
        )
    )
    try:
        await post_event_message(
            session,
            version.project_id,
            "release_blocked",
            summary,
            context={
                "version_number": version.version_number,
                "version_id": str(version.id),
                "blocker": gate,
                "implicated_paths": implicated_paths,
                "compare_range": compare_range,
                "calcification": "deployed_gate_latch",
            },
            idempotency_key=f"{dedupe_key}:message",
        )
    except Exception as exc:  # noqa: BLE001 — alert must not break the sweep
        logger.warning(
            "[RELEASE_QUALIFIER] failed to post deployed-gate latch message "
            "for v%d (non-fatal): %s",
            version.version_number,
            exc,
        )
    version.calcification_alerted_blocker = gate
    session.add(version)
    logger.error(
        "[RELEASE_QUALIFIER] deployed gate latch for v%d: gate=%s paths=%s range=%s",
        version.version_number,
        gate,
        paths_text,
        compare_range,
    )
    return True
