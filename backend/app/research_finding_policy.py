"""Pure research-finding normalization and routing policy helpers."""

from typing import Any, Dict, List

from app.capability_registry import off_registry_capability_refs
from app.compute_experiment_contract import resolve_compute_spend_threshold
from app.finding_deduplication import canonical_finding_body
from app.finding_deduplication import normalize_prerequisite
from app.shared_state import runtime_config
from app.task_blocked import NEED_REF_MAX_LEN


COMPUTE_EXPERIMENT_FINDING_TYPES: frozenset[str] = frozenset(
    {"compute_experiment", "measurement", "benchmark"}
)

NON_SPAWNING_FINDING_TYPES = frozenset(
    {
        "direction_proposal",
        "direction_note",
        "design_doc_observation",
        "direction_update",
    }
)


def cap_findings_by_priority(
    findings: List[Dict[str, Any]], effective_cap: int
) -> tuple[List[Dict[str, Any]], int, List[Dict[str, Any]]]:
    """Cap task-spawning findings while retaining free metadata findings."""

    priority_order = {"high": 0, "medium": 1, "low": 2}
    spawning_indices = [
        i
        for i, finding in enumerate(findings)
        if finding.get("type") not in NON_SPAWNING_FINDING_TYPES
    ]
    applied_count = max(0, len(spawning_indices) - effective_cap)
    deferred: list[dict] = []
    if len(spawning_indices) <= effective_cap:
        return findings, applied_count, deferred

    ranked = sorted(
        ((i, findings[i]) for i in spawning_indices),
        key=lambda pair: priority_order.get(pair[1].get("priority", "medium"), 1),
    )
    metadata_indices = set(range(len(findings))) - set(spawning_indices)
    kept = metadata_indices | {i for i, _ in ranked[:effective_cap]}
    # If the cap removes a prerequisite, co-defer all dependents to a fixpoint.
    # Otherwise index remapping would silently erase their structural edge.
    while True:
        co_defer = {
            i
            for i in kept - metadata_indices
            if any(
                0 <= dep < len(findings) and dep not in kept
                for dep in (findings[i].get("depends_on_indices") or [])
            )
        }
        if not co_defer:
            break
        kept -= co_defer

    kept_indices = sorted(kept)
    deferred = [finding for i, finding in enumerate(findings) if i not in kept]
    applied_count = len(deferred)
    old_to_new = {old: new for new, old in enumerate(kept_indices)}
    capped = [findings[i] for i in kept_indices]
    for finding in capped:
        dependencies = finding.get("depends_on_indices")
        if dependencies:
            finding["depends_on_indices"] = [
                old_to_new[dep] for dep in dependencies if dep in old_to_new
            ]
    return capped, applied_count, deferred


def annotate_capability_declarations(
    findings: List[Dict[str, Any]], registry_names: List[str]
) -> None:
    """Normalize capability declarations and flag off-registry references."""

    for finding in findings:
        if not isinstance(finding, dict):
            continue
        prerequisite_raw = finding.get("prerequisite") or finding.get("requires")
        prerequisite = normalize_prerequisite(prerequisite_raw)
        if prerequisite is not None:
            finding["_prerequisite"] = prerequisite
            finding.pop("_malformed_prerequisite", None)
        else:
            finding.pop("_prerequisite", None)
            if (
                isinstance(prerequisite_raw, dict)
                and isinstance(prerequisite_raw.get("kind"), str)
                and prerequisite_raw["kind"].strip().lower() == "task"
                and isinstance(prerequisite_raw.get("ref"), str)
                and prerequisite_raw["ref"].strip()
            ):
                finding["_malformed_prerequisite"] = {
                    "kind": "task",
                    "ref": prerequisite_raw["ref"].strip()[:NEED_REF_MAX_LEN],
                    "reason": (
                        "title-like task ref (not a task id or finding hash); "
                        "declare a named foundation as kind=capability"
                    ),
                }
            else:
                finding.pop("_malformed_prerequisite", None)

        capability = finding.get("provides_capability") or finding.get("capability")
        if isinstance(capability, str) and capability.strip():
            finding["_provides_capability"] = capability.strip()
        else:
            finding.pop("_provides_capability", None)

        capability_refs: List[str] = []
        if finding.get("_provides_capability"):
            capability_refs.append(finding["_provides_capability"])
        if prerequisite is not None and prerequisite.get("kind") == "capability":
            capability_refs.append(prerequisite["ref"])
        off_registry = off_registry_capability_refs(capability_refs, registry_names)
        if off_registry:
            finding["_off_registry_capabilities"] = off_registry
        else:
            finding.pop("_off_registry_capabilities", None)


def apply_structural_actionability_downgrade(findings: list) -> int:
    """Downgrade actionability grades unsupported by claims/sources/detail."""

    grade_order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    downgrades = 0
    for finding in findings:
        claims = (
            finding.get("claims") if isinstance(finding.get("claims"), list) else []
        )
        ungrounded = int(finding.get("_ungrounded_claim_count", 0) or 0)
        effective_claims = max(0, len(claims) - ungrounded)
        description = str(finding.get("description") or "").strip()
        sources = (
            finding.get("sources") if isinstance(finding.get("sources"), list) else []
        )
        valid_sources = sum(
            1
            for source in sources
            if isinstance(source, dict)
            and (
                str(source.get("type") or "").lower() in ("pr", "message")
                or source.get("valid", True) is not False
            )
        )
        structural_grade = "LOW"
        if effective_claims >= 2 and valid_sources >= 1 and len(description) >= 80:
            structural_grade = "HIGH"
        elif effective_claims >= 1 and len(description) >= 40:
            structural_grade = "MEDIUM"
        current = finding.get("actionability_grade", "MEDIUM")
        if grade_order[structural_grade] < grade_order.get(current, 1):
            finding["actionability_grade"] = structural_grade
            downgrades += 1
    return downgrades


def is_high_stakes_finding(finding: Dict[str, Any]) -> bool:
    """Return whether a finding warrants an adversarial second opinion."""

    if not isinstance(finding, dict):
        return False
    if finding.get("type") in NON_SPAWNING_FINDING_TYPES:
        return False
    if not str(finding.get("prompt") or finding.get("description") or "").strip():
        return False
    if finding.get("actionability_grade") == "LOW":
        return False
    if str(finding.get("scope") or "").lower() == "large":
        return True
    if str(finding.get("risk") or "").lower() in ("high", "critical", "very_high"):
        return True
    if finding.get("verification_basis_downgraded_from"):
        return True
    provenance = finding.get("_provenance_warnings")
    if isinstance(provenance, dict):
        if provenance.get("broken_refs") or provenance.get("ungrounded_refs"):
            return True
        if provenance.get("cited_files_changed_since_generation"):
            return True
    if finding.get("needs_revalidation") is True:
        return True

    if finding.get("type") in COMPUTE_EXPERIMENT_FINDING_TYPES:
        nested = (
            finding.get("compute_experiment") or finding.get("launch_contract") or {}
        )
        if not isinstance(nested, dict):
            nested = {}

        def pick(key: str) -> Any:
            value = nested.get(key)
            return finding.get(key) if value is None else value

        accelerator = pick("accelerator")
        if accelerator and str(accelerator).strip():
            return True
        spend = pick("spend_cap_usd")
        try:
            spend_value = float(spend)
        except (TypeError, ValueError):
            spend_value = None
        if spend_value is not None and spend_value > 0:
            threshold = resolve_compute_spend_threshold(runtime_config, None)
            if threshold == 0 or spend_value >= threshold * 0.5:
                return True
        deadline = pick("deadline_seconds")
        try:
            deadline_value = int(deadline)
        except (TypeError, ValueError):
            deadline_value = None
        if deadline_value is not None and deadline_value > 3600:
            return True
    return False


def research_findings_would_spawn(findings: List[Dict[str, Any]]) -> bool:
    """Return whether at least one finding has downstream blast radius."""

    for finding in findings:
        if not isinstance(finding, dict):
            continue
        if (finding.get("type") or "") in NON_SPAWNING_FINDING_TYPES:
            continue
        if not canonical_finding_body(finding):
            continue
        if finding.get("actionability_grade") == "LOW":
            continue
        return True
    return False


# Established private names remain available to the completion module/tests.
_cap_findings_by_priority = cap_findings_by_priority
_annotate_capability_declarations = annotate_capability_declarations
_apply_structural_actionability_downgrade = apply_structural_actionability_downgrade
_is_high_stakes_finding = is_high_stakes_finding
_research_findings_would_spawn = research_findings_would_spawn
_NON_SPAWNING_FINDING_TYPES = NON_SPAWNING_FINDING_TYPES
