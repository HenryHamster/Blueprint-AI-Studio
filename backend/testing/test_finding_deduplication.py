"""Unit tests for app.finding_deduplication — pure dedup helpers."""

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch


from app.finding_deduplication import (
    CODE_ERROR_COOLDOWN,
    DedupOutcome,
    STRONG_REGION_MIN_SHARED,
    _check_evidence_overlap,
    _extract_normalized_claims,
    _extract_source_refs,
    _extract_technical_tokens,
    _find_archived_diagnosis_match,
    _find_retired_approach_match,
    _jaccard_overlap,
    _normalize_source_ref,
    _title_bigrams,
    build_dedup_state,
    deduplicate_findings,
    extract_archived_diagnoses,
    extract_recent_terminal_finding_history,
    extract_retired_failed_approaches,
    finding_hash,
    is_duplicate,
    normalize_prerequisite,
    prerequisite_matches_task,
    resolve_prerequisite_task,
    select_canonical_provider,
    task_finding_lineage,
)
from app.models import Task, TaskStatus


def _make_task(
    *,
    status=TaskStatus.COMPLETED,
    context: dict | None = None,
    created_at: datetime | None = None,
    parent_task_id: uuid.UUID | None = None,
    latest_status_message: str | None = None,
) -> Task:
    return Task(
        project_id=uuid.uuid4(),
        title="t",
        description="d",
        status=status,
        context=context or {},
        payloads=[],
        created_by="TEST",
        created_at=created_at or datetime.now(timezone.utc).replace(tzinfo=None),
        parent_task_id=parent_task_id,
        latest_status_message=latest_status_message,
        vm_instance_id=f"vm-{uuid.uuid4().hex[:8]}",
    )


def test_finding_hash_deterministic_and_truncated():
    h1 = finding_hash("Title A", "Description A")
    h2 = finding_hash("Title A", "Description A")
    h3 = finding_hash("Title A", "Description B")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 16
    expected = hashlib.sha256(b"Title ADescription A").hexdigest()[:16]
    assert h1 == expected


def test_is_duplicate_same_direction_4_word_threshold():
    # Same direction: 4 non-stopword overlap → duplicate
    existing = [("dir-1", "refactor finding deduplication helpers from module")]
    title = "refactor finding deduplication helpers into standalone module"
    assert is_duplicate(title, "dir-1", existing) is True
    # 3-word overlap is below same-direction threshold
    existing_low = [("dir-1", "totally different words here entirely")]
    assert is_duplicate(title, "dir-1", existing_low) is False


def test_is_duplicate_cross_direction_6_word_threshold():
    # 'refactor' and 'update' are stopwords; use distinctive non-stopword tokens.
    existing = [("dir-2", "alpha beta gamma delta epsilon zeta extra")]
    four_word_overlap = "alpha beta gamma delta something-else"
    assert is_duplicate(four_word_overlap, "dir-1", existing) is False
    # 6-word overlap does trigger across directions
    six_word_overlap = "alpha beta gamma delta epsilon zeta unrelated"
    assert is_duplicate(six_word_overlap, "dir-1", existing) is True


def test_is_duplicate_none_direction_treated_as_same():
    existing = [(None, "alpha beta gamma delta epsilon")]
    # 4-word overlap + unknown direction on one side → treat as same-direction
    assert is_duplicate("alpha beta gamma delta novel", "dir-1", existing) is True


def test_build_dedup_state_excludes_completed_without_pr_merged():
    # COMPLETED but not pr_merged = code never shipped, should NOT block respawn
    t = _make_task(
        status=TaskStatus.COMPLETED,
        context={"finding_hash": "abc123"},
    )
    hashes, failed, prior = build_dedup_state([t], failed_parent_ids=set())
    assert "abc123" not in hashes
    assert failed == {}
    assert prior == {}


def test_build_dedup_state_suppresses_completed_no_op_reproduce_off_ramp():
    # COMPLETED-without-pr_merged but carrying a no_op_reason = the agent took
    # the reproduce-on-HEAD off-ramp and verified the finding does not
    # reproduce. The finding hash MUST be suppressed so it is not re-dispatched
    # next cycle, and the failed_hashes label records why there's no PR.
    t = _make_task(
        status=TaskStatus.COMPLETED,
        context={
            "finding_hash": "noop1",
            "no_op_reason": "defect already fixed by intervening merge; cited code refactored away",
        },
    )
    hashes, failed, _ = build_dedup_state([t], failed_parent_ids=set())
    assert "noop1" in hashes
    assert failed["noop1"] == "no-op (not reproducing)"


def test_build_dedup_state_suppresses_completed_already_resolved_verdict():
    # COMPLETED-without-pr_merged carrying the canonical reproduce verdict
    # ``already_resolved`` (the durable signal, no legacy no_op_reason): the
    # agent verified the defect does not reproduce on HEAD. The finding hash MUST
    # be suppressed even though no_op_reason is absent.
    t = _make_task(
        status=TaskStatus.COMPLETED,
        context={
            "finding_hash": "verdict_resolved",
            "reproduce_verdict": {
                "status": "already_resolved",
                "reason": "cited symbol absent on HEAD; refactored away",
            },
        },
    )
    hashes, failed, _ = build_dedup_state([t], failed_parent_ids=set())
    assert "verdict_resolved" in hashes
    assert failed["verdict_resolved"] == "no-op (not reproducing)"


def test_build_dedup_state_indeterminate_verdict_stays_eligible():
    # COMPLETED-without-pr_merged with an ``indeterminate`` verdict: the defect
    # could NOT be ruled out, so the finding stays eligible for retry — only an
    # affirmative already_resolved (or legacy no_op_reason) suppresses it.
    t = _make_task(
        status=TaskStatus.COMPLETED,
        context={
            "finding_hash": "verdict_indet",
            "reproduce_verdict": {"status": "indeterminate"},
        },
    )
    hashes, failed, _ = build_dedup_state([t], failed_parent_ids=set())
    assert "verdict_indet" not in hashes
    assert failed == {}


def test_build_dedup_state_incomplete_no_pr_completion_stays_eligible():
    # COMPLETED-without-pr_merged and WITHOUT a no_op_reason = a dropped /
    # abandoned no-PR completion (work was left incomplete). It must remain
    # eligible for retry, so its finding hash is NOT suppressed.
    t = _make_task(
        status=TaskStatus.COMPLETED,
        context={"finding_hash": "incomplete1"},
    )
    hashes, failed, _ = build_dedup_state([t], failed_parent_ids=set())
    assert "incomplete1" not in hashes
    assert failed == {}


def test_build_dedup_state_includes_completed_pr_merged():
    t = _make_task(
        status=TaskStatus.COMPLETED,
        context={"finding_hash": "merged1", "pr_merged": True},
    )
    hashes, _, _ = build_dedup_state([t], failed_parent_ids=set())
    assert "merged1" in hashes


def test_build_dedup_state_reads_finding_hash_once():
    """Regression: get()+[] access could KeyError if context changed between reads."""

    class VolatileContext(dict):
        def get(self, key, default=None):
            if key == "finding_hash":
                return "volatile1"
            return super().get(key, default)

        def __getitem__(self, key):
            if key == "finding_hash":
                raise KeyError(key)
            return super().__getitem__(key)

    t = _make_task(status=TaskStatus.COMPLETED, context={"finding_hash": "placeholder"})
    t.context = VolatileContext({"pr_merged": True})
    hashes, failed, prior = build_dedup_state([t], failed_parent_ids=set())
    assert "volatile1" in hashes
    assert failed == {}
    assert prior == {}


def test_build_dedup_state_excludes_ghost_merges():
    parent_id = uuid.uuid4()
    t = _make_task(
        status=TaskStatus.COMPLETED,
        context={"finding_hash": "ghost1", "pr_merged": True},
        parent_task_id=parent_id,
    )
    hashes, _, _ = build_dedup_state([t], failed_parent_ids={parent_id})
    assert "ghost1" not in hashes, "ghost merge should not block respawn"


def test_build_dedup_state_non_terminal_parent_ancestry_negative_allows_respawn():
    # Non-terminal parent (e.g. EVALUATING) where is_ancestor_of returned False:
    # research_completion adds that parent's ID to failed_parent_ids.
    # build_dedup_state must treat the child as NOT shipped so it can re-spawn.
    parent_id = uuid.uuid4()
    child = _make_task(
        status=TaskStatus.COMPLETED,
        context={"finding_hash": "stranded2", "pr_merged": True},
        parent_task_id=parent_id,
    )
    hashes, _, _ = build_dedup_state([child], failed_parent_ids={parent_id})
    assert "stranded2" not in hashes, "stranded rollup child must not block re-spawn"


def test_build_dedup_state_confirmed_landed_child_stays_suppressed():
    # Parent merge commit confirmed as ancestor of integration branch (is_ancestor_of
    # returned True) → parent NOT in failed_parent_ids → child stays suppressed.
    parent_id = uuid.uuid4()
    child = _make_task(
        status=TaskStatus.COMPLETED,
        context={"finding_hash": "landed2", "pr_merged": True},
        parent_task_id=parent_id,
    )
    hashes, _, _ = build_dedup_state([child], failed_parent_ids=set())
    assert "landed2" in hashes, "genuinely landed fix must remain suppressed"


def test_build_dedup_state_ancestry_indeterminate_trusts_pr_merged_flag():
    # is_ancestor_of returned None (API error, missing merge SHA, no credentials):
    # research_completion does NOT add parent to failed_parent_ids (degrade-safe).
    # build_dedup_state must fall back to flag-trust and keep the child suppressed.
    parent_id = uuid.uuid4()
    child = _make_task(
        status=TaskStatus.COMPLETED,
        context={"finding_hash": "indet2", "pr_merged": True},
        parent_task_id=parent_id,
    )
    hashes, _, _ = build_dedup_state([child], failed_parent_ids=set())
    assert "indet2" in hashes, "indeterminate ancestry must fall back to flag-trust"


def test_extract_recent_terminal_finding_history_uses_plan_output_without_hash():
    """Final terminal history keeps accepted findings without a child hash."""
    t = _make_task(
        status=TaskStatus.COMPLETED,
        context={"research_findings_finalized": True},
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    t.payloads = [{"type": "research"}]
    t.plan_output = [
        {
            "title": "Persisted finding",
            "description": "kept in plan_output",
        }
    ]

    findings = extract_recent_terminal_finding_history([t], failed_parent_ids=set())

    assert findings == [
        {
            "title": "Persisted finding",
            "description": "kept in plan_output",
            "type": "research",
        }
    ]


def test_extract_recent_terminal_history_ignores_unfinalized_research_output():
    t = _make_task(status=TaskStatus.COMPLETED, context={})
    t.payloads = [{"type": "research"}]
    t.plan_output = [{"title": "Rejected raw finding"}]

    assert extract_recent_terminal_finding_history([t], failed_parent_ids=set()) == []


def test_build_dedup_state_excludes_infra_failures():
    # A task whose agent-reported failure_category is in INFRA_CLASSES should
    # NOT block respawn (the code never really ran).
    t = _make_task(
        status=TaskStatus.FAILED,
        context={"finding_hash": "infra1", "failure_category": "PROVISION_FAILURE"},
    )
    hashes, failed, prior = build_dedup_state([t], failed_parent_ids=set())
    assert "infra1" not in hashes
    # But it is still recorded in failed_hashes / prior_failure_by_hash
    assert failed["infra1"] == TaskStatus.FAILED
    assert prior["infra1"] is t


def test_build_dedup_state_code_error_cooldown_boundary():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    within = _make_task(
        status=TaskStatus.FAILED,
        context={"finding_hash": "cool1"},
        created_at=now - CODE_ERROR_COOLDOWN + timedelta(minutes=5),
        latest_status_message="CODE_ERROR: unit test failed at assertion",
    )
    beyond = _make_task(
        status=TaskStatus.FAILED,
        context={"finding_hash": "cool2"},
        created_at=now - CODE_ERROR_COOLDOWN - timedelta(minutes=5),
        latest_status_message="CODE_ERROR: unit test failed at assertion",
    )
    hashes, _, _ = build_dedup_state([within, beyond], failed_parent_ids=set(), now=now)
    # within-cooldown blocks; beyond-cooldown allows respawn
    assert "cool1" in hashes
    assert "cool2" not in hashes


def test_build_dedup_state_code_error_cooldown_boundary_uses_created_at_plus_window():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    at_boundary = _make_task(
        status=TaskStatus.FAILED,
        context={"finding_hash": "boundary"},
        created_at=now - CODE_ERROR_COOLDOWN,
        latest_status_message="CODE_ERROR: unit test failed at assertion",
    )
    hashes, _, _ = build_dedup_state([at_boundary], failed_parent_ids=set(), now=now)
    assert "boundary" not in hashes


def test_build_dedup_state_suppress_until_active():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    future_iso = (now + timedelta(hours=1)).isoformat()
    t = _make_task(
        status=TaskStatus.CANCELLED,
        context={"finding_hash": "sup1", "suppress_until": future_iso},
    )
    hashes, failed, _ = build_dedup_state([t], failed_parent_ids=set(), now=now)
    assert "sup1" in hashes
    # suppress_until keeps existing failed_hashes value; for cancelled tasks
    # that value is the status itself — the "suppressed" label is used only
    # when no prior failed/cancelled entry exists.
    assert failed["sup1"] == TaskStatus.CANCELLED

    past_iso = (now - timedelta(hours=1)).isoformat()
    expired = _make_task(
        status=TaskStatus.COMPLETED,
        context={"finding_hash": "sup2", "suppress_until": past_iso},
    )
    hashes2, _, _ = build_dedup_state([expired], failed_parent_ids=set(), now=now)
    assert "sup2" not in hashes2


def test_build_dedup_state_suppress_until_adds_label_for_completed_respawn():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    future_iso = (now + timedelta(hours=1)).isoformat()
    # COMPLETED-without-pr_merged task that carries an active suppress_until:
    # the hash should be added with the "suppressed" label (no prior
    # failed/cancelled entry existed).
    t = _make_task(
        status=TaskStatus.COMPLETED,
        context={"finding_hash": "sup3", "suppress_until": future_iso},
    )
    hashes, failed, _ = build_dedup_state([t], failed_parent_ids=set(), now=now)
    assert "sup3" in hashes
    assert failed["sup3"] == "suppressed"


def test_build_dedup_state_prior_failure_keeps_most_recent():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    older = _make_task(
        status=TaskStatus.FAILED,
        context={"finding_hash": "dup1"},
        created_at=now - timedelta(hours=4),
        latest_status_message="older failure",
    )
    newer = _make_task(
        status=TaskStatus.CANCELLED,
        context={"finding_hash": "dup1"},
        created_at=now - timedelta(hours=1),
        latest_status_message="newer failure",
    )
    _, _, prior = build_dedup_state([older, newer], failed_parent_ids=set(), now=now)
    assert prior["dup1"] is newer


def test_deduplicate_findings_preserves_priority_order():
    findings = [
        {"title": "low a", "description": "low a", "priority": "low"},
        {"title": "high a", "description": "high a", "priority": "high"},
        {"title": "medium a", "description": "medium a", "priority": "medium"},
        {"title": "high b", "description": "high b", "priority": "high"},
    ]
    outcome = deduplicate_findings(
        findings,
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=[],
        open_pr_files=set(),
    )
    orig_indices = [orig_i for orig_i, _, _ in outcome.deduplicated]
    # Priority high first (original indices 1, 3 in stable order), then medium (2), low (0)
    assert orig_indices == [1, 3, 2, 0]
    assert outcome.hash_dedup_count == 0
    assert outcome.word_overlap_count == 0
    assert outcome.file_overlap_count == 0


def test_deduplicate_findings_hash_dedup_counter():
    f = {"title": "seen already", "description": "seen already", "priority": "medium"}
    fhash = finding_hash(f["title"], f["description"])
    outcome = deduplicate_findings(
        [f],
        task_context=None,
        recent_finding_hashes={fhash},
        failed_hashes={fhash: "prior"},
        existing_tasks_with_dirs=[],
        open_pr_files=set(),
    )
    assert outcome.hash_dedup_count == 1
    assert outcome.deduplicated == []


def test_deduplicate_findings_intra_batch_identical_hash_yields_one():
    # Two identical findings in ONE batch: the survivor's own hash joins the
    # hash stage, so the twin cannot also survive (it would otherwise spawn a
    # second child for the same finding — the duplicate-PR shape).
    f = {"title": "same finding", "description": "same body", "priority": "medium"}
    twin = dict(f)
    outcome = deduplicate_findings(
        [f, twin],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=[],
        open_pr_files=set(),
    )
    assert len(outcome.deduplicated) == 1
    assert outcome.hash_dedup_count == 1
    assert outcome.filtered[0]["reason"] == "hash match with prior task"


def test_deduplicate_findings_region_identity_skip():
    # Shared region (2+ related_files) AND title corroboration → duplicate.
    finding = {
        "title": "alpha beta gamma delta",
        "description": "desc",
        "priority": "medium",
        "direction_id": "dir-1",
        "related_files": ["app/foo.py", "app/bar.py"],
    }
    # Active-task candidate is a 4-tuple: (dir, title, task_type, related_files).
    existing = [
        ("dir-1", "alpha beta gamma delta something-else", None,
         ["app/foo.py", "app/bar.py"])
    ]
    outcome = deduplicate_findings(
        [finding],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=existing,
        open_pr_files=set(),
    )
    assert outcome.word_overlap_count == 1
    assert outcome.deduplicated == []
    assert outcome.filtered[0]["reason"].startswith("region overlap with: ")


def test_cross_direction_actionable_finding_survives_word_overlap_only():
    # Regression for the ~28% "research spawned 0 children" over-filter: two
    # INDEPENDENT directions both touch the same hot file region and share
    # several generic title words, but represent genuinely-distinct work (no
    # shared claim, file Jaccard < STRONG threshold). The bare title-word
    # corroboration arm USED to suppress this — now cross-direction findings
    # require a strong corroborator, so the distinct finding SURVIVES.
    finding = {
        "title": "fix dispatch retry backoff in orchestrator loop handler",
        "description": "desc",
        "priority": "medium",
        "direction_id": "dir-A",
        # 2 shared files (region) + 2 unique → Jaccard 2/4 = 0.5 < 0.7 strong.
        "related_files": [
            "app/orchestrator.py",
            "app/dispatch.py",
            "app/retry_local.py",
            "app/backoff_local.py",
        ],
        "claims": ["dispatch retry has no backoff"],
    }
    existing = [
        (
            "dir-B",  # DIFFERENT direction
            "fix dispatch retry backoff in orchestrator loop logging",
            None,
            [
                "app/orchestrator.py",
                "app/dispatch.py",
                "app/logging_other.py",
                "app/metrics_other.py",
            ],
        )
    ]
    outcome = deduplicate_findings(
        [finding],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=existing,
        open_pr_files=set(),
    )
    assert outcome.word_overlap_count == 0
    assert len(outcome.deduplicated) == 1


def test_same_direction_region_duplicate_still_caught_after_cross_fix():
    # The cross-direction relaxation must NOT loosen same-direction dedup: an
    # identical-region, word-overlapping finding from the SAME direction is
    # still a genuine duplicate (same author iterating on the same intent) and
    # must STILL be filtered by title-word corroboration alone.
    finding = {
        "title": "fix dispatch retry backoff in orchestrator loop handler",
        "description": "desc",
        "priority": "medium",
        "direction_id": "dir-A",
        "related_files": [
            "app/orchestrator.py",
            "app/dispatch.py",
            "app/retry_local.py",
            "app/backoff_local.py",
        ],
    }
    existing = [
        (
            "dir-A",  # SAME direction
            "fix dispatch retry backoff in orchestrator loop logging",
            None,
            [
                "app/orchestrator.py",
                "app/dispatch.py",
                "app/logging_other.py",
                "app/metrics_other.py",
            ],
        )
    ]
    outcome = deduplicate_findings(
        [finding],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=existing,
        open_pr_files=set(),
    )
    assert outcome.word_overlap_count == 1
    assert outcome.deduplicated == []


def test_cross_direction_duplicate_with_shared_claim_still_caught():
    # Cross-direction findings are NOT exempt from dedup — a STRONG corroborator
    # (a shared claim) still proves identity even across independent directions,
    # so a true cross-direction duplicate is filtered.
    finding = {
        "title": "patch the dispatch loop",
        "description": "desc",
        "priority": "medium",
        "direction_id": "dir-A",
        "related_files": ["app/orchestrator.py", "app/dispatch.py"],
        "claims": ["dispatch loop drops tasks on lock timeout"],
    }
    existing = [
        {
            "direction_id": "dir-B",  # DIFFERENT direction
            "title": "rework dispatch entrypoint",
            "related_files": ["app/orchestrator.py", "app/dispatch.py"],
            "claims": ["dispatch loop drops tasks on lock timeout"],
        }
    ]
    outcome = deduplicate_findings(
        [finding],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=existing,
        open_pr_files=set(),
    )
    assert outcome.word_overlap_count == 1
    assert outcome.deduplicated == []


def test_deduplicate_findings_title_collision_without_region_kept():
    # Regression for the `a/b`-token false positive: titles collide on a shared
    # token but the findings target DIFFERENT files (no region). Must NOT dedup.
    finding = {
        "title": "feat: STSBOT_SEARCH_MC_ROLLOUTS seam + MC-oracle leaf A/B",
        "description": "desc",
        "priority": "medium",
        "direction_id": "dir-1",
        "related_files": ["stsbot/search_agent.py"],
    }
    existing = [
        ("dir-1", "obs: search-depth axis closed; per-fight A/B instrument", None,
         ["notes/wide_search_verdict.md"])
    ]
    outcome = deduplicate_findings(
        [finding],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=existing,
        open_pr_files=set(),
    )
    assert outcome.word_overlap_count == 0
    assert len(outcome.deduplicated) == 1


def test_deduplicate_findings_shared_region_without_corroboration_kept():
    # Two distinct findings touch some of the same files (region present, but
    # file Jaccard < strong threshold) and neither title-overlap nor a shared
    # claim corroborates → distinct work, kept.
    finding = {
        "title": "add retry backoff to dispatch",
        "description": "desc",
        "priority": "medium",
        "direction_id": "dir-1",
        "related_files": ["app/foo.py", "app/bar.py", "app/baz.py", "app/qux.py"],
        "claims": ["dispatch lacks backoff"],
    }
    existing = [
        ("dir-1", "remove dead import from helper", None,
         ["app/foo.py", "app/bar.py", "app/other.py", "app/more.py"])
    ]
    outcome = deduplicate_findings(
        [finding],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=existing,
        open_pr_files=set(),
    )
    assert outcome.word_overlap_count == 0
    assert len(outcome.deduplicated) == 1


def test_deduplicate_findings_fileless_finding_not_region_deduped():
    # A finding with no related_files cannot establish a region — it is left to
    # exact-hash dedup, never region-matched, even with an identical title.
    finding = {
        "title": "alpha beta gamma delta",
        "description": "desc",
        "priority": "medium",
        "direction_id": "dir-1",
    }
    existing = [
        ("dir-1", "alpha beta gamma delta", None, ["app/foo.py", "app/bar.py"])
    ]
    outcome = deduplicate_findings(
        [finding],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=existing,
        open_pr_files=set(),
    )
    assert outcome.word_overlap_count == 0
    assert len(outcome.deduplicated) == 1


def test_deduplicate_findings_file_overlap_subset():
    finding = {
        "title": "do thing",
        "description": "desc",
        "priority": "medium",
        "related_files": ["app/foo.py", "app/bar.py"],
    }
    outcome = deduplicate_findings(
        [finding],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=[],
        open_pr_files={"app/foo.py", "app/bar.py", "app/baz.py"},
    )
    assert outcome.file_overlap_count == 1
    assert outcome.deduplicated == []


def test_deduplicate_findings_file_overlap_not_full_subset_kept():
    finding = {
        "title": "do thing",
        "description": "desc",
        "priority": "medium",
        "related_files": ["app/foo.py", "app/new.py"],
    }
    outcome = deduplicate_findings(
        [finding],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=[],
        open_pr_files={"app/foo.py"},
    )
    assert outcome.file_overlap_count == 0
    assert len(outcome.deduplicated) == 1


def test_deduplicate_findings_parent_direction_inherited_from_context():
    finding = {
        "title": "alpha beta gamma delta novel-end",
        "description": "desc",
        "priority": "medium",
        "related_files": ["app/foo.py", "app/bar.py"],
        # No direction_id on finding — should fall back to task_context direction_id
    }
    # Existing task shares the region + 3+ words in the SAME direction (via inheritance)
    existing = [
        ("dir-parent", "alpha beta gamma delta different", None,
         ["app/foo.py", "app/bar.py"])
    ]
    outcome = deduplicate_findings(
        [finding],
        task_context={"direction_id": "dir-parent"},
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=existing,
        open_pr_files=set(),
    )
    assert outcome.word_overlap_count == 1


def test_reworded_same_direction_duplicate_of_recent_terminal_task_suppressed():
    recent_terminal_findings = [
        {
            "title": "duplicate recent terminal finding chain",
            "description": "desc",
            "direction_id": "dir-1",
            "type": "research",
            "related_files": ["backend/app/alpha.py", "backend/app/beta.py"],
            "sources": [{"type": "file", "ref": "backend/app/alpha.py"}],
            "claims": ["terminal chain already explored"],
        }
    ]
    findings = [
        {
            # Same region + title corroboration → suppressed.
            "title": "duplicate recent terminal finding chain again",
            "description": "different desc",
            "priority": "medium",
            "direction_id": "dir-1",
            "related_files": ["backend/app/alpha.py", "backend/app/beta.py"],
        },
        {
            # Different region (no shared files) → kept despite title words.
            "title": "duplicate terminal chain noticed elsewhere",
            "description": "different desc",
            "priority": "medium",
            "direction_id": "dir-2",
            "related_files": ["backend/app/unrelated.py"],
        },
    ]
    outcome = deduplicate_findings(
        findings,
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=[],
        open_pr_files=set(),
        recent_terminal_findings=recent_terminal_findings,
    )
    assert outcome.word_overlap_count == 1
    assert len(outcome.deduplicated) == 1
    assert outcome.deduplicated[0][1]["direction_id"] == "dir-2"
    assert len(outcome.filtered) == 1
    rec = outcome.filtered[0]
    assert rec["direction_id"] == "dir-1"
    assert rec["reason"].startswith("recent terminal region overlap with: ")


def test_deduplicate_findings_recent_terminal_evidence_overlap_rejects_reworded_duplicate():
    recent_terminal_findings = [
        {
            "title": "measure duplicate retry inflation drivers",
            "description": "desc",
            "direction_id": "dir-1",
            "type": "research",
            "sources": [
                {"type": "file", "ref": "backend/app/cache.py:42"},
                {"type": "file", "ref": "backend/app/models.py#L100-L200"},
            ],
            "claims": ["retry inflation is caused by duplicate spawning"],
        }
    ]
    outcome = deduplicate_findings(
        [
            {
                "title": "track duplicate retry inflation causes",
                "description": "reworded desc",
                "priority": "high",
                "direction_id": "dir-1",
                "sources": [
                    {"type": "file", "ref": "backend/app/cache.py"},
                    {"type": "file", "ref": "backend/app/models.py:200"},
                ],
                "claims": ["retry inflation is caused by duplicate spawning"],
            }
        ],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=[],
        open_pr_files=set(),
        recent_terminal_findings=recent_terminal_findings,
    )
    assert outcome.word_overlap_count == 0
    assert outcome.evidence_overlap_count == 1
    assert outcome.deduplicated == []
    assert len(outcome.filtered) == 1
    rec = outcome.filtered[0]
    assert rec["reason"].startswith("evidence overlap with: ")


def test_dedup_outcome_default_empty():
    outcome = DedupOutcome()
    assert outcome.deduplicated == []
    assert outcome.hash_dedup_count == 0
    assert outcome.word_overlap_count == 0
    assert outcome.file_overlap_count == 0
    assert outcome.evidence_overlap_count == 0
    assert outcome.filtered == []


def test_deduplicate_findings_records_hash_dedup_reason():
    finding = {
        "title": "previously attempted refactor",
        "description": "body",
        "priority": "medium",
        "direction_id": "dir-1",
    }
    fhash = finding_hash(finding["title"], finding["description"])
    outcome = deduplicate_findings(
        [finding],
        task_context={"direction_id": "dir-1"},
        recent_finding_hashes={fhash},
        failed_hashes={fhash: "prior"},
        existing_tasks_with_dirs=[],
        open_pr_files=set(),
    )
    assert len(outcome.filtered) == 1
    rec = outcome.filtered[0]
    assert rec["title"] == "previously attempted refactor"
    assert rec["direction_id"] == "dir-1"
    assert rec["reason"] == "hash match with prior task"


def test_deduplicate_findings_records_region_overlap_matched_title():
    finding = {
        "title": "alpha beta gamma delta novel",
        "description": "desc",
        "priority": "medium",
        "direction_id": "dir-1",
        "related_files": ["app/foo.py", "app/bar.py"],
    }
    existing_title = "alpha beta gamma delta something-else"
    outcome = deduplicate_findings(
        [finding],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=[
            ("dir-1", existing_title, None, ["app/foo.py", "app/bar.py"])
        ],
        open_pr_files=set(),
    )
    assert len(outcome.filtered) == 1
    rec = outcome.filtered[0]
    assert rec["title"] == "alpha beta gamma delta novel"
    assert rec["direction_id"] == "dir-1"
    assert rec["reason"].startswith("region overlap with: ")
    assert "alpha beta gamma delta" in rec["reason"]
    # The persisted reason must stay compact so it does not bloat the prompt.
    assert len(rec["reason"]) <= 80


def test_compute_finding_word_overlap_respects_active_task_type_metadata():
    finding = {
        "title": "measure live release proof acceptance evidence",
        "description": "desc",
        "priority": "medium",
        "direction_id": "dir-1",
        "type": "compute_experiment",
        "related_files": ["app/release.py", "app/proof.py"],
    }
    _files = ["app/release.py", "app/proof.py"]
    cases = [
        ("research", "research measure live release proof acceptance evidence", 1, 0),
        (
            "compute_experiment",
            "compute measure live release proof acceptance evidence",
            0,
            1,
        ),
    ]
    for task_type, active_title, expected_kept, expected_overlap in cases:
        outcome = deduplicate_findings(
            [finding],
            task_context=None,
            recent_finding_hashes=set(),
            failed_hashes={},
            existing_tasks_with_dirs=[
                ("dir-1", active_title, task_type, _files),
            ],
            open_pr_files=set(),
        )
        assert len(outcome.deduplicated) == expected_kept
        assert outcome.word_overlap_count == expected_overlap


def test_deduplicate_findings_records_file_overlap_reason():
    finding = {
        "title": "cover already-shipped files",
        "description": "desc",
        "priority": "medium",
        "direction_id": "dir-2",
        "related_files": ["app/foo.py", "app/bar.py"],
    }
    outcome = deduplicate_findings(
        [finding],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=[],
        open_pr_files={"app/foo.py", "app/bar.py"},
    )
    assert len(outcome.filtered) == 1
    rec = outcome.filtered[0]
    assert rec["title"] == "cover already-shipped files"
    assert rec["direction_id"] == "dir-2"
    assert rec["reason"] == "all files covered by open PR"


def test_deduplicate_findings_evidence_overlap_identical_sources_and_claims():
    """Two findings with identical sources + identical claim → dropped."""
    first_finding = {
        "title": "implement caching layer",
        "description": "Add cache to improve performance",
        "priority": "high",
        "direction_id": "dir-1",
        "sources": [
            {"type": "file", "ref": "backend/app/cache.py", "excerpt": "line 42"},
            {"type": "file", "ref": "backend/app/models.py:100", "excerpt": "line 100"},
        ],
        "claims": ["caching improves performance by 10x"],
    }
    second_finding = {
        "title": "optimize with caching layer",
        "description": "Add a cache layer for optimization",
        "priority": "medium",
        "direction_id": "dir-1",
        "sources": [
            {
                "type": "file",
                "ref": "backend/app/cache.py",
                "excerpt": "different part",
            },
            {
                "type": "file",
                "ref": "backend/app/models.py:200",
                "excerpt": "different part",
            },
        ],
        "claims": ["caching improves performance by 10x"],
    }
    outcome = deduplicate_findings(
        [first_finding, second_finding],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=[],
        open_pr_files=set(),
    )
    # First finding passes through all dedup stages
    assert len(outcome.deduplicated) == 1
    assert outcome.evidence_overlap_count == 1
    assert len(outcome.filtered) == 1
    rec = outcome.filtered[0]
    assert rec["reason"].startswith("evidence overlap with:")


def test_deduplicate_findings_evidence_overlap_identical_sources_different_claims():
    """Identical sources but different claims → kept."""
    first_finding = {
        "title": "implement caching",
        "description": "Add cache layer",
        "priority": "high",
        "direction_id": "dir-1",
        "sources": [
            {"type": "file", "ref": "backend/app/cache.py"},
            {"type": "file", "ref": "backend/app/models.py"},
        ],
        "claims": ["caching improves performance"],
    }
    second_finding = {
        "title": "optimize caching strategy",
        "description": "Improve cache hit rate",
        "priority": "high",
        "direction_id": "dir-1",
        "sources": [
            {"type": "file", "ref": "backend/app/cache.py"},
            {"type": "file", "ref": "backend/app/models.py"},
        ],
        "claims": ["cache hit rate needs improvement"],
    }
    outcome = deduplicate_findings(
        [first_finding, second_finding],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=[],
        open_pr_files=set(),
    )
    # Both findings pass because they have different claims
    assert len(outcome.deduplicated) == 2
    assert outcome.evidence_overlap_count == 0
    assert len(outcome.filtered) == 0


def test_deduplicate_findings_evidence_overlap_below_threshold():
    """Less than 80% source overlap → kept."""
    first_finding = {
        "title": "cache optimization",
        "description": "desc",
        "priority": "high",
        "sources": [
            {"type": "file", "ref": "backend/app/cache.py"},
            {"type": "file", "ref": "backend/app/models.py"},
            {"type": "file", "ref": "backend/app/database.py"},
            {"type": "file", "ref": "backend/app/api.py"},
            {"type": "file", "ref": "backend/app/utils.py"},
        ],
        "claims": ["caching improves performance"],
    }
    second_finding = {
        "title": "optimize cache strategy",
        "description": "desc",
        "priority": "high",
        "sources": [
            {"type": "file", "ref": "backend/app/cache.py"},
            {"type": "file", "ref": "backend/app/redis.py"},
        ],
        "claims": ["caching improves performance"],
    }
    outcome = deduplicate_findings(
        [first_finding, second_finding],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=[],
        open_pr_files=set(),
    )
    # Jaccard overlap = 1/(5+2-1) = 1/6 ≈ 0.17 < 0.8, so no evidence overlap
    assert len(outcome.deduplicated) == 2
    assert outcome.evidence_overlap_count == 0


def test_deduplicate_findings_evidence_overlap_no_sources():
    """Findings with no sources → no evidence overlap regardless of claims."""
    first_finding = {
        "title": "some finding",
        "description": "desc",
        "priority": "high",
        "claims": ["claim A"],
    }
    second_finding = {
        "title": "reworded finding",
        "description": "desc",
        "priority": "high",
        "claims": ["claim A"],
    }
    outcome = deduplicate_findings(
        [first_finding, second_finding],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=[],
        open_pr_files=set(),
    )
    # No sources means no evidence overlap check can occur
    assert len(outcome.deduplicated) == 2
    assert outcome.evidence_overlap_count == 0


def test_deduplicate_findings_evidence_overlap_no_claims():
    """Findings with sources but no claims → no evidence overlap."""
    first_finding = {
        "title": "caching changes",
        "description": "desc",
        "priority": "high",
        "sources": [
            {"type": "file", "ref": "backend/app/cache.py"},
            {"type": "file", "ref": "backend/app/models.py"},
        ],
    }
    second_finding = {
        "title": "cache optimization",
        "description": "desc",
        "priority": "high",
        "sources": [
            {"type": "file", "ref": "backend/app/cache.py"},
            {"type": "file", "ref": "backend/app/models.py"},
        ],
    }
    outcome = deduplicate_findings(
        [first_finding, second_finding],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=[],
        open_pr_files=set(),
    )
    # No claims means no evidence overlap check can occur
    assert len(outcome.deduplicated) == 2
    assert outcome.evidence_overlap_count == 0


def test_deduplicate_findings_evidence_overlap_normalizes_refs():
    """Evidence overlap check normalizes file refs (strips line numbers)."""
    first_finding = {
        "title": "fix bug",
        "description": "desc",
        "priority": "high",
        "sources": [
            {"type": "file", "ref": "backend/app/cache.py:42"},
            {"type": "file", "ref": "backend/app/models.py#L100-L200"},
        ],
        "claims": ["bug needs fixing"],
    }
    second_finding = {
        "title": "address issue",
        "description": "desc",
        "priority": "high",
        "sources": [
            {
                "type": "file",
                "ref": "backend/app/cache.py:999",
            },  # Different line number, same file
            {
                "type": "file",
                "ref": "backend/app/models.py:50",
            },  # Different line number, same file
        ],
        "claims": ["bug needs fixing"],
    }
    outcome = deduplicate_findings(
        [first_finding, second_finding],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=[],
        open_pr_files=set(),
    )
    # After normalization, both findings have identical refs + shared claims
    assert len(outcome.deduplicated) == 1
    assert outcome.evidence_overlap_count == 1


def test_deduplicate_findings_evidence_overlap_records_reason():
    """Evidence overlap rejection records proper reason."""
    first_finding = {
        "title": "implement caching layer",
        "description": "desc",
        "priority": "high",
        "sources": [{"type": "file", "ref": "backend/app/cache.py"}],
        "claims": ["improves performance"],
    }
    second_finding = {
        "title": "optimize cache",
        "description": "desc",
        "priority": "medium",
        "direction_id": "dir-2",
        "sources": [{"type": "file", "ref": "backend/app/cache.py"}],
        "claims": ["improves performance"],
    }
    outcome = deduplicate_findings(
        [first_finding, second_finding],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=[],
        open_pr_files=set(),
    )
    assert len(outcome.filtered) == 1
    rec = outcome.filtered[0]
    assert rec["reason"].startswith("evidence overlap with: implement caching")


# --- Direct unit tests for source-ref helper functions ---


def test_normalize_source_ref_formats():
    assert _normalize_source_ref("backend/app/foo.py") == "backend/app/foo.py"
    assert _normalize_source_ref("backend/app/foo.py:42") == "backend/app/foo.py"
    assert _normalize_source_ref("backend/app/foo.py#L10-L20") == "backend/app/foo.py"
    assert _normalize_source_ref("backend/app/foo.py:42-50") == "backend/app/foo.py"
    assert _normalize_source_ref("") == ""


def test_normalize_source_ref_preserves_typed_refs():
    # Typed refs like pr:123 or message:abc must not be collapsed to their prefix.
    assert _normalize_source_ref("pr:123") == "pr:123"
    assert _normalize_source_ref("message:abc") == "message:abc"
    assert _normalize_source_ref("pr:456") == "pr:456"


def test_normalize_source_ref_preserves_url_scheme():
    # URL schemes must not be stripped at the first colon.
    assert _normalize_source_ref("https://github.com/foo") == "https://github.com/foo"
    assert (
        _normalize_source_ref("https://github.com/foo/bar")
        == "https://github.com/foo/bar"
    )


def test_evidence_dedup_preserves_typed_refs_and_recent_overlap():
    """Typed source refs (pr:123) are not collapsed by normalization, and
    recent_terminal_findings seeds the primary evidence-overlap check so
    cross-cycle duplicates are caught without a separate secondary pass."""
    # Typed ref normalization: pr:123 and pr:456 stay distinct (not both "pr").
    assert _normalize_source_ref("pr:123") == "pr:123"
    assert _normalize_source_ref("message:abc") == "message:abc"

    # A terminal finding from a previous cycle.
    terminal = {
        "title": "measure retry inflation root cause",
        "description": "desc",
        "direction_id": "dir-1",
        "sources": [{"type": "file", "ref": "backend/app/cache.py:42"}],
        "claims": ["retry inflation from duplicate spawning"],
    }
    # A cross-cycle duplicate with the same evidence but a different title.
    candidate = {
        "title": "track retry inflation drivers",
        "description": "reworded desc",
        "priority": "high",
        "direction_id": "dir-1",
        "sources": [{"type": "file", "ref": "backend/app/cache.py"}],
        "claims": ["retry inflation from duplicate spawning"],
    }
    # Two findings with DIFFERENT typed PR refs should NOT evidence-overlap.
    pr_finding_a = {
        "title": "investigate pr 123 regression",
        "description": "desc a",
        "priority": "medium",
        "sources": [{"type": "pr", "ref": "pr:123"}],
        "claims": ["pr caused regression"],
    }
    pr_finding_b = {
        "title": "investigate pr 456 regression",
        "description": "desc b",
        "priority": "medium",
        "sources": [{"type": "pr", "ref": "pr:456"}],
        "claims": ["pr caused regression"],
    }
    outcome = deduplicate_findings(
        [candidate, pr_finding_a, pr_finding_b],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=[],
        open_pr_files=set(),
        recent_terminal_findings=[terminal],
    )
    # candidate is blocked by primary evidence check (seeded from terminal).
    assert outcome.evidence_overlap_count == 1
    # pr_finding_a and pr_finding_b have distinct typed refs → no evidence overlap.
    assert len(outcome.deduplicated) == 2
    rec = outcome.filtered[0]
    # Primary evidence overlap reason, not the old "recent terminal evidence overlap" prefix.
    assert rec["reason"].startswith("evidence overlap with: ")


def test_extract_source_refs_normalizes_and_filters():
    sources = [
        {"type": "file", "ref": "backend/app/foo.py:42"},
        {"type": "file", "ref": "backend/app/bar.py#L10"},
        {"type": "file"},  # no ref key
        "not-a-dict",
    ]
    refs = _extract_source_refs(sources)
    assert refs == {"backend/app/foo.py", "backend/app/bar.py"}


def test_extract_source_refs_empty_and_invalid():
    assert _extract_source_refs([]) == set()
    assert _extract_source_refs("not-a-list") == set()


def test_jaccard_overlap_boundary_values():
    assert _jaccard_overlap({"a", "b", "c"}, {"a", "b", "c"}) == 1.0
    assert _jaccard_overlap({"a", "b"}, {"c", "d"}) == 0.0
    assert _jaccard_overlap(set(), set()) == 0.0
    # |intersection|=1, |union|=3
    result = _jaccard_overlap({"a", "b"}, {"a", "c"})
    assert abs(result - 1 / 3) < 1e-9


def test_extract_normalized_claims_deduplicates_and_lowercases():
    claims = [
        "Performance Improved by 10x",
        "performance improved by 10x",
        "Another claim",
    ]
    result = _extract_normalized_claims(claims)
    assert result == {"performance improved by 10x", "another claim"}


def test_check_evidence_overlap_empty_guard():
    # No sources → no overlap regardless of claims
    assert _check_evidence_overlap(
        [], ["claim A"], [("t", [{"ref": "foo.py"}], ["claim A"])]
    ) == (False, "")
    # No claims → no overlap
    assert _check_evidence_overlap(
        [{"ref": "foo.py"}], [], [("t", [{"ref": "foo.py"}], ["claim A"])]
    ) == (False, "")


# --- Retired-direction suppression and cross-cycle filtered hash tests ---


def test_extract_retired_failed_approaches_combines_approaches_and_titles():
    retired = [
        {
            "failed_approaches": [
                "cache invalidation strategy",
                "redis cluster approach",
            ],
            "recently_completed_finding_titles": ["fix logging timestamps"],
            "category": "structural",
        },
        {"failed_approaches": ["async migration approach"]},
    ]
    result = extract_retired_failed_approaches(retired)
    assert len(result) == 4
    assert result[0] == {
        "text": "cache invalidation strategy",
        "category": "structural",
    }
    assert result[1] == {"text": "redis cluster approach", "category": "structural"}
    assert result[2] == {"text": "fix logging timestamps", "category": "structural"}
    # No category → defaults to "mixed"
    assert result[3] == {"text": "async migration approach", "category": "mixed"}


def test_extract_retired_failed_approaches_empty_and_none():
    assert extract_retired_failed_approaches([]) == []
    assert extract_retired_failed_approaches(None) == []
    assert extract_retired_failed_approaches([{"unrelated_key": "x"}]) == []


def test_find_retired_approach_match_3_word_threshold():
    approaches = [
        {"text": "cache invalidation performance problem", "category": "mixed"},
        {"text": "redis migration strategy gone", "category": "mixed"},
    ]
    # 3 non-stopword overlap → match
    assert (
        _find_retired_approach_match("cache invalidation performance redux", approaches)
        is not None
    )
    # Only 2 non-stopword overlap → no match
    assert _find_retired_approach_match("cache invalidation only", approaches) is None


def test_build_dedup_state_previously_filtered_hashes_suppresses():
    filtered_hash = "prevfiltered1234"
    hashes, failed, _ = build_dedup_state(
        [], failed_parent_ids=set(), previously_filtered_hashes={filtered_hash}
    )
    assert filtered_hash in hashes
    assert failed[filtered_hash] == "filtered-last-cycle"


def test_deduplicate_findings_retired_approach_suppresses():
    finding = {
        "title": "cache invalidation performance problem revisited",
        "description": "desc",
        "priority": "medium",
        "direction_id": "dir-1",
    }
    outcome = deduplicate_findings(
        [finding],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=[],
        open_pr_files=set(),
        retired_failed_approaches=[
            {"text": "cache invalidation performance problem", "category": "mixed"}
        ],
    )
    assert outcome.retired_approach_count == 1
    assert outcome.deduplicated == []
    assert outcome.filtered[0]["reason"].startswith("retired approach:")


def test_deduplicate_findings_distinct_finding_passes_through_both_filters():
    finding = {
        "title": "completely novel unrelated topic here",
        "description": "desc",
        "priority": "medium",
        "direction_id": "dir-1",
    }
    fhash = finding_hash(finding["title"], finding["description"])
    outcome = deduplicate_findings(
        [finding],
        task_context=None,
        recent_finding_hashes={"someotherhashabcd"},
        failed_hashes={},
        existing_tasks_with_dirs=[],
        open_pr_files=set(),
        retired_failed_approaches=[
            {"text": "cache invalidation performance problem", "category": "mixed"}
        ],
    )
    assert len(outcome.deduplicated) == 1
    assert outcome.deduplicated[0][2] == fhash
    assert outcome.retired_approach_count == 0
    assert outcome.hash_dedup_count == 0


def test_retired_approach_cross_category_does_not_suppress():
    """structural finding vs product retired approach → NOT blocked (cross-category)."""
    finding = {
        "title": "cache invalidation performance problem revisited",
        "description": "desc",
        "priority": "medium",
        "direction_id": "dir-structural",
    }
    outcome = deduplicate_findings(
        [finding],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=[],
        open_pr_files=set(),
        retired_failed_approaches=[
            {"text": "cache invalidation performance problem", "category": "product"}
        ],
        direction_categories={"dir-structural": "structural"},
    )
    assert outcome.retired_approach_count == 0
    assert len(outcome.deduplicated) == 1


def test_retired_approach_same_category_suppresses():
    """structural finding vs structural retired approach → blocked."""
    finding = {
        "title": "cache invalidation performance problem revisited",
        "description": "desc",
        "priority": "medium",
        "direction_id": "dir-structural",
    }
    outcome = deduplicate_findings(
        [finding],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=[],
        open_pr_files=set(),
        retired_failed_approaches=[
            {"text": "cache invalidation performance problem", "category": "structural"}
        ],
        direction_categories={"dir-structural": "structural"},
    )
    assert outcome.retired_approach_count == 1
    assert outcome.deduplicated == []


def test_retired_approach_mixed_category_suppresses_any():
    """mixed retired approach vs structural finding → blocked (permissive)."""
    finding = {
        "title": "cache invalidation performance problem revisited",
        "description": "desc",
        "priority": "medium",
        "direction_id": "dir-structural",
    }
    outcome = deduplicate_findings(
        [finding],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=[],
        open_pr_files=set(),
        retired_failed_approaches=[
            {"text": "cache invalidation performance problem", "category": "mixed"}
        ],
        direction_categories={"dir-structural": "structural"},
    )
    assert outcome.retired_approach_count == 1
    assert outcome.deduplicated == []


def test_retired_approach_mixed_finding_blocked_by_specific_category():
    """mixed finding vs structural retired approach → blocked (permissive)."""
    finding = {
        "title": "cache invalidation performance problem revisited",
        "description": "desc",
        "priority": "medium",
        "direction_id": "dir-mixed",
    }
    outcome = deduplicate_findings(
        [finding],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=[],
        open_pr_files=set(),
        retired_failed_approaches=[
            {"text": "cache invalidation performance problem", "category": "structural"}
        ],
        direction_categories={"dir-mixed": "mixed"},
    )
    assert outcome.retired_approach_count == 1
    assert outcome.deduplicated == []


def test_retired_approach_missing_category_treated_as_mixed():
    """Retired approach with no category key defaults to mixed → suppresses any finding."""
    finding = {
        "title": "cache invalidation performance problem revisited",
        "description": "desc",
        "priority": "medium",
        "direction_id": "dir-structural",
    }
    outcome = deduplicate_findings(
        [finding],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=[],
        open_pr_files=set(),
        # No "category" key — should default to mixed
        retired_failed_approaches=[{"text": "cache invalidation performance problem"}],
        direction_categories={"dir-structural": "structural"},
    )
    assert outcome.retired_approach_count == 1
    assert outcome.deduplicated == []


def test_build_dedup_state_passes_at_time_to_classify_failure():
    """build_dedup_state must pass at_time=last_heartbeat so rate-limit correlation
    uses the failure instant rather than sweep time, preventing RATE_LIMIT→CODE_ERROR
    flips when analytics paths run hours after the cooldown has lifted."""
    heartbeat = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=6)
    t = _make_task(
        status=TaskStatus.FAILED,
        context={"finding_hash": "rl1"},
        created_at=heartbeat,
    )
    t.last_heartbeat = heartbeat

    with patch("app.finding_deduplication.classify_failure") as mock_cf:
        mock_cf.return_value = "RATE_LIMIT"
        build_dedup_state([t], failed_parent_ids=set())
    mock_cf.assert_called_once_with(t, at_time=heartbeat)


def test_build_dedup_state_rate_limit_excluded_from_hashes():
    """A FAILED task classified as RATE_LIMIT (infra class) must not block respawn."""
    t = _make_task(
        status=TaskStatus.FAILED,
        context={"finding_hash": "rl2", "failure_category": "rate_limited"},
    )
    hashes, failed, prior = build_dedup_state([t], failed_parent_ids=set())
    assert "rl2" not in hashes
    assert failed["rl2"] == TaskStatus.FAILED
    assert prior["rl2"] is t


def test_extract_archived_diagnoses_from_design_doc():
    design_doc = {
        "retired_diagnoses_archive": [
            {
                "direction_id": "d-1",
                "diagnosis": "caching optimization attempts exhausted",
            },
            {
                "direction_id": "d-2",
                "diagnosis": "build performance improvements tested",
            },
        ]
    }
    diagnoses = extract_archived_diagnoses(design_doc)
    assert len(diagnoses) == 2
    assert "caching optimization attempts exhausted" in diagnoses
    assert "build performance improvements tested" in diagnoses


def test_find_archived_diagnosis_match_with_word_overlap():
    archived_diagnoses = [
        "caching optimization attempts exhausted performance",
        "build system complexity issues explored",
    ]
    # 3-word overlap: "caching optimization exhausted" - should match
    title = "caching optimization issues exhausted after investigation"
    match = _find_archived_diagnosis_match(title, archived_diagnoses)
    assert match is not None
    assert "caching optimization" in match


def test_deduplicate_findings_filters_archived_diagnosis_match():
    finding = {
        "title": "fix caching optimization performance memory issues",
        "description": "Improve cache hit rate",
        "priority": "medium",
        "direction_id": "dir-1",
    }
    archived_diagnoses = [
        "caching optimization performance attempts were exhausted in prior cycles"
    ]
    outcome = deduplicate_findings(
        [finding],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=[],
        open_pr_files=set(),
        archived_diagnoses=archived_diagnoses,
    )
    assert outcome.archived_diagnoses_count == 1
    assert len(outcome.deduplicated) == 0
    assert len(outcome.filtered) == 1
    assert outcome.filtered[0]["reason"].startswith("archived diagnosis:")


# ---------------------------------------------------------------------------
# normalize_prerequisite
# ---------------------------------------------------------------------------


def test_normalize_prerequisite_valid_task_kind():
    # kind=task requires an exact ref — a 16-char finding hash here.
    out = normalize_prerequisite({"kind": "task", "ref": "deadbeefcafe0000"})
    assert out == {"kind": "task", "ref": "deadbeefcafe0000"}


def test_normalize_prerequisite_valid_task_kind_uuid_ref():
    tid = str(uuid.uuid4())
    out = normalize_prerequisite({"kind": "task", "ref": tid})
    assert out == {"kind": "task", "ref": tid}


def test_normalize_prerequisite_canonicalizes_exact_task_ref():
    tid = str(uuid.uuid4()).upper()
    out = normalize_prerequisite({"kind": "task", "ref": tid})
    assert out == {"kind": "task", "ref": tid.lower()}


def test_normalize_prerequisite_rejects_title_like_task_ref():
    # A title-like task ref (free prose, or any non-UUID/non-hash token) is NOT a
    # machine-resolvable task and must be dropped at ingestion so it never becomes
    # durable stranded "missing task" demand — named foundations use capability.
    assert (
        normalize_prerequisite({"kind": "task", "ref": "Build the auth subsystem"})
        is None
    )
    assert normalize_prerequisite({"kind": "task", "ref": "abc123"}) is None
    assert normalize_prerequisite({"kind": "task", "ref": "auth-subsystem"}) is None


def test_normalize_prerequisite_valid_capability_kind():
    # Capability refs are NOT subject to the exact-task-ref policy — a named
    # foundation is exactly what capability kind is for.
    out = normalize_prerequisite({"kind": "capability", "ref": "auth-service"})
    assert out == {"kind": "capability", "ref": "auth-service"}


def test_normalize_prerequisite_strips_whitespace_and_lowercases_kind():
    out = normalize_prerequisite({"kind": "  TASK  ", "ref": "  deadbeefdeadbeef  "})
    assert out == {"kind": "task", "ref": "deadbeefdeadbeef"}


def test_normalize_prerequisite_unknown_kind_returns_none():
    assert normalize_prerequisite({"kind": "direction", "ref": "d-1"}) is None


def test_normalize_prerequisite_non_dict_returns_none():
    assert normalize_prerequisite(None) is None
    assert normalize_prerequisite("task:abc") is None
    assert normalize_prerequisite(["task", "abc"]) is None


def test_normalize_prerequisite_missing_or_blank_ref_returns_none():
    assert normalize_prerequisite({"kind": "task"}) is None
    assert normalize_prerequisite({"kind": "task", "ref": ""}) is None
    assert normalize_prerequisite({"kind": "task", "ref": "   "}) is None
    assert normalize_prerequisite({"kind": "task", "ref": 42}) is None


# ---------------------------------------------------------------------------
# resolve_prerequisite_task
# ---------------------------------------------------------------------------


def test_resolve_prerequisite_task_matches_by_id():
    foundation = _make_task(status=TaskStatus.IN_PROGRESS)
    other = _make_task(status=TaskStatus.QUEUED)
    prereq = {"kind": "task", "ref": str(foundation.id)}
    assert resolve_prerequisite_task(prereq, [other, foundation]) is foundation


def test_resolve_prerequisite_task_matches_by_finding_hash():
    foundation = _make_task(
        status=TaskStatus.IN_PROGRESS, context={"finding_hash": "deadbeefcafe0000"}
    )
    other = _make_task(status=TaskStatus.QUEUED)
    prereq = {"kind": "task", "ref": "deadbeefcafe0000"}
    assert resolve_prerequisite_task(prereq, [other, foundation]) is foundation


def test_resolve_prerequisite_task_matches_by_recorded_finding_lineage():
    foundation = _make_task(
        status=TaskStatus.IN_PROGRESS,
        context={
            "finding_hash": "1111222233334444",
            "finding_lineage": ["aaaabbbbccccdddd"],
        },
    )
    prereq = {"kind": "task", "ref": "aaaabbbbccccdddd"}

    assert task_finding_lineage(foundation) == {
        "1111222233334444",
        "aaaabbbbccccdddd",
    }
    assert resolve_prerequisite_task(prereq, [foundation]) is foundation


def test_resolve_prerequisite_task_capability_exact_marker_match():
    foundation = _make_task(
        status=TaskStatus.IN_PROGRESS, context={"provides_capability": "Auth-Service"}
    )
    # Case-insensitive exact match on the capability name.
    prereq = {"kind": "capability", "ref": "auth-service"}
    assert resolve_prerequisite_task(prereq, [foundation]) is foundation


def test_resolve_prerequisite_task_capability_list_marker_match():
    foundation = _make_task(
        status=TaskStatus.IN_PROGRESS,
        context={"capability": ["logging", "auth-service"]},
    )
    prereq = {"kind": "capability", "ref": "auth-service"}
    assert resolve_prerequisite_task(prereq, [foundation]) is foundation


def test_resolve_prerequisite_task_no_fuzzy_title_match():
    # A task whose title/context mentions auth must NOT match a capability ref
    # unless it carries an explicit capability marker.
    candidate = _make_task(status=TaskStatus.IN_PROGRESS, context={})
    candidate.title = "Build the auth-service foundation"
    prereq = {"kind": "capability", "ref": "auth-service"}
    assert resolve_prerequisite_task(prereq, [candidate]) is None


def test_resolve_prerequisite_task_no_candidate_returns_none():
    prereq = {"kind": "task", "ref": "no-such-id"}
    assert resolve_prerequisite_task(prereq, []) is None
    assert resolve_prerequisite_task(prereq, [_make_task()]) is None


def test_resolve_prerequisite_task_malformed_prereq_returns_none():
    foundation = _make_task(status=TaskStatus.IN_PROGRESS)
    assert resolve_prerequisite_task(None, [foundation]) is None
    assert (
        resolve_prerequisite_task({"kind": "bogus", "ref": "x"}, [foundation]) is None
    )


def test_resolve_prerequisite_task_selects_canonical_earliest_created_at():
    # Several candidates share the same explicit signal (duplicate foundations).
    # The CANONICAL provider — earliest by created_at — must win regardless of
    # the order the caller supplies the candidates in, so every dependent
    # deterministically attaches to the same foundation.
    base = datetime(2026, 1, 1, 12, 0, 0)
    earliest = _make_task(
        status=TaskStatus.IN_PROGRESS,
        context={"finding_hash": "aaaabbbbccccdddd"},
        created_at=base,
    )
    later = _make_task(
        status=TaskStatus.QUEUED,
        context={"finding_hash": "aaaabbbbccccdddd"},
        created_at=base + timedelta(hours=2),
    )
    prereq = {"kind": "task", "ref": "aaaabbbbccccdddd"}
    # `later` appears first in iteration order, but `earliest` is canonical.
    assert resolve_prerequisite_task(prereq, [later, earliest]) is earliest
    # Order-independent: the same canonical choice with the list reversed.
    assert resolve_prerequisite_task(prereq, [earliest, later]) is earliest


def test_resolve_prerequisite_task_canonical_capability_earliest_wins():
    # Multiple active tasks advertise the SAME explicit capability marker.
    base = datetime(2026, 2, 1, 9, 0, 0)
    earliest = _make_task(
        status=TaskStatus.IN_PROGRESS,
        context={"provides_capability": "auth-service"},
        created_at=base,
    )
    later = _make_task(
        status=TaskStatus.IN_PROGRESS,
        context={"capability": ["auth-service"]},
        created_at=base + timedelta(minutes=30),
    )
    prereq = {"kind": "capability", "ref": "auth-service"}
    assert resolve_prerequisite_task(prereq, [later, earliest]) is earliest


def test_resolve_prerequisite_task_created_at_tie_breaks_on_str_id():
    # Same created_at: the tie breaks deterministically on str(task.id), the
    # exact rule compute_foundation_convergence_actions uses.
    base = datetime(2026, 3, 1, 0, 0, 0)
    a = _make_task(
        status=TaskStatus.IN_PROGRESS,
        context={"finding_hash": "11112222ccccdddd"},
        created_at=base,
    )
    b = _make_task(
        status=TaskStatus.QUEUED,
        context={"finding_hash": "11112222ccccdddd"},
        created_at=base,
    )
    expected = min((a, b), key=lambda t: str(t.id))
    prereq = {"kind": "task", "ref": "11112222ccccdddd"}
    assert resolve_prerequisite_task(prereq, [a, b]) is expected
    assert resolve_prerequisite_task(prereq, [b, a]) is expected


def test_resolve_prerequisite_task_none_created_at_sorts_as_minimum():
    # A missing created_at sorts as datetime.min — so it only wins if it is
    # genuinely the earliest, matching the convergence path's rule. Here the
    # None-dated task wins over a real-dated one (None == minimum).
    no_date = _make_task(
        status=TaskStatus.IN_PROGRESS, context={"finding_hash": "00001111ccccdddd"}
    )
    no_date.created_at = None
    dated = _make_task(
        status=TaskStatus.QUEUED,
        context={"finding_hash": "00001111ccccdddd"},
        created_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    prereq = {"kind": "task", "ref": "00001111ccccdddd"}
    assert resolve_prerequisite_task(prereq, [dated, no_date]) is no_date


def test_resolve_prerequisite_task_single_match_unchanged():
    # A lone match is returned as-is (no behavioral change for the common case).
    foundation = _make_task(
        status=TaskStatus.IN_PROGRESS, context={"finding_hash": "5050505050505050"}
    )
    other = _make_task(status=TaskStatus.QUEUED)
    prereq = {"kind": "task", "ref": "5050505050505050"}
    assert resolve_prerequisite_task(prereq, [other, foundation]) is foundation


def test_select_canonical_provider_empty_returns_none():
    assert select_canonical_provider([]) is None


def test_select_canonical_provider_picks_earliest_then_id():
    base = datetime(2026, 4, 1, 0, 0, 0)
    later = _make_task(created_at=base + timedelta(hours=1))
    earlier = _make_task(created_at=base)
    assert select_canonical_provider([later, earlier]) is earlier
    # created_at tie → str(id) tiebreak
    a = _make_task(created_at=base)
    b = _make_task(created_at=base)
    assert select_canonical_provider([a, b]) is min((a, b), key=lambda t: str(t.id))


# ---------------------------------------------------------------------------
# prerequisite_matches_task (shared predicate — single source of truth)
# ---------------------------------------------------------------------------


def test_prerequisite_matches_task_matches_by_id():
    task = _make_task()
    assert (
        prerequisite_matches_task({"kind": "task", "ref": str(task.id)}, task) is True
    )


def test_prerequisite_matches_task_matches_by_finding_hash():
    task = _make_task(context={"finding_hash": "abc123def4560000"})
    assert (
        prerequisite_matches_task({"kind": "task", "ref": "abc123def4560000"}, task)
        is True
    )


def test_prerequisite_matches_task_matches_by_finding_lineage():
    task = _make_task(
        context={
            "finding_hash": "abc123def4560000",
            "finding_lineage": ["feedfacecafebeef"],
        }
    )
    assert (
        prerequisite_matches_task({"kind": "task", "ref": "feedfacecafebeef"}, task)
        is True
    )


def test_prerequisite_matches_task_matches_capability_marker_case_insensitive():
    task = _make_task(context={"provides_capability": "Auth-Service"})
    assert (
        prerequisite_matches_task({"kind": "capability", "ref": "auth-service"}, task)
        is True
    )


def test_prerequisite_matches_task_matches_capability_list_marker():
    task = _make_task(context={"capability": ["logging", "auth-service"]})
    assert (
        prerequisite_matches_task({"kind": "capability", "ref": "auth-service"}, task)
        is True
    )


def test_prerequisite_matches_task_negatives():
    task = _make_task(context={"finding_hash": "1111222233334444"})
    # Wrong hash, missing marker, malformed need, None task, and the resume-only
    # 'pr' kind all return False (pr is intentionally not a predicate signal).
    assert (
        prerequisite_matches_task({"kind": "task", "ref": "ffffffffffffffff"}, task)
        is False
    )
    assert (
        prerequisite_matches_task({"kind": "capability", "ref": "auth"}, task) is False
    )
    assert prerequisite_matches_task({"kind": "bogus", "ref": "x"}, task) is False
    assert prerequisite_matches_task(None, task) is False
    assert prerequisite_matches_task({"kind": "task", "ref": "x"}, None) is False
    assert prerequisite_matches_task({"kind": "pr", "ref": "42"}, task) is False


# --- Technical token and bigram Jaccard secondary dedup gates ---


def test_extract_technical_tokens_file_paths():
    tokens = _extract_technical_tokens(
        "fix performance issue in backend/app/reaper.py startup"
    )
    assert "backend/app/reaper.py" in tokens


def test_extract_technical_tokens_underscored_identifiers():
    tokens = _extract_technical_tokens(
        "finding_deduplication helpers miss reworded titles"
    )
    assert "finding_deduplication" in tokens


def test_extract_technical_tokens_mixed_case():
    tokens = _extract_technical_tokens(
        "TaskStatus enum causes TypeError in orchestrator"
    )
    assert "taskstatus" in tokens


def test_extract_technical_tokens_excludes_plain_words():
    tokens = _extract_technical_tokens("reduce spawn churn in research cycle")
    assert tokens == set()


def test_extract_technical_tokens_excludes_stopwords():
    tokens = _extract_technical_tokens("fix finding_deduplication threshold")
    # "fix" is a stopword; "finding_deduplication" is technical
    assert "finding_deduplication" in tokens
    assert "fix" not in tokens


def test_title_bigrams_basic():
    bgs = _title_bigrams("finding deduplication misses reworded titles")
    assert ("finding", "deduplication") in bgs
    assert ("deduplication", "misses") in bgs


def test_title_bigrams_strips_stopwords():
    # "the" is a stopword — bigrams bridge across it
    bgs = _title_bigrams("the finding deduplication issue")
    assert ("finding", "deduplication") in bgs
    # "the" should not appear in any bigram token
    for a, b in bgs:
        assert a != "the" and b != "the"


def test_title_bigrams_too_short():
    assert _title_bigrams("one") == set()
    assert _title_bigrams("") == set()


def test_is_duplicate_technical_token_same_direction():
    # Shared underscore identifier triggers duplicate even with < 4 word overlap
    existing = [("dir-1", "finding_deduplication helpers miss paraphrased titles")]
    title = "finding_deduplication skips reworded research findings"
    # Word overlap: {finding_deduplication} = 1 word (below 4 threshold without gate)
    assert is_duplicate(title, "dir-1", existing) is True


def test_is_duplicate_technical_token_file_path():
    existing = [("dir-1", "reaper.py startup latency exceeds threshold")]
    title = "reaper.py initialization takes too long on leader"
    assert is_duplicate(title, "dir-1", existing) is True


def test_is_duplicate_technical_token_cross_direction_single_token_no_match():
    # Cross-direction: 1 shared technical token is NOT enough (threshold is 2)
    existing = [("dir-2", "finding_deduplication helpers miss paraphrased titles")]
    title = "finding_deduplication skips reworded research findings"
    assert is_duplicate(title, "dir-1", existing) is False


def test_is_duplicate_technical_token_cross_direction_two_tokens_match():
    # Cross-direction: 2 shared technical tokens → duplicate
    existing = [
        ("dir-2", "finding_deduplication.py and reaper.py have overlapping logic")
    ]
    title = "finding_deduplication.py duplicates reaper.py retry handling"
    assert is_duplicate(title, "dir-1", existing) is True


def test_is_duplicate_bigram_jaccard_same_direction():
    # Titles with very similar structure (>0.65 bigram Jaccard) → duplicate
    # A: [word, overlap, deduplication, misses, reworded, titles, same, direction, cycles]
    # B: [word, overlap, deduplication, skips, reworded, titles, same, direction, cycles]
    # Bigrams intersect heavily: (word,overlap),(overlap,deduplication),(reworded,titles),(titles,same),(same,direction),(direction,cycles)
    title_a = (
        "word overlap deduplication misses reworded titles in same direction cycles"
    )
    title_b = (
        "word overlap deduplication skips reworded titles in same direction cycles"
    )
    existing = [("dir-1", title_b)]
    assert is_duplicate(title_a, "dir-1", existing) is True


def test_is_duplicate_bigram_jaccard_cross_direction_below_threshold():
    # Titles share 5 non-stopword words (< 6 cross-direction threshold) and
    # bigram Jaccard ~0.67 (above same-direction 0.65 but below cross-direction 0.80)
    title_a = "classify cascade cancellation events detection failures"
    title_b = "classify cascade cancellation events detection behavior"
    existing = [("dir-2", title_b)]
    # Cross-direction threshold is 0.80; this pair has Jaccard ~0.67 → no match
    assert is_duplicate(title_a, "dir-1", existing) is False


def test_is_duplicate_plain_words_no_false_positive():
    # Generic plain-word titles without technical tokens should NOT trigger secondary gates
    existing = [("dir-1", "alpha beta gamma delta work queue task")]
    title = "alpha beta gamma delta research cycle run"
    # Word overlap = 4 (alpha, beta, gamma, delta) → fires via word-overlap gate
    assert is_duplicate(title, "dir-1", existing) is True
    # Use truly different words to test no false positive
    existing3 = [("dir-1", "alpha beta gamma unrelated words here")]
    title3 = "delta epsilon zeta completely different context"
    assert is_duplicate(title3, "dir-1", existing3) is False


def test_deduplicate_findings_single_file_no_title_claim_overlap_kept():
    # After the STRONG_REGION_MIN_SHARED fix: a finding that shares only ONE file
    # with an active task (jaccard = 1.0) is NOT auto-deduped unless title words
    # OR a claim also corroborate — a 1-file overlap is not strong enough signal
    # alone. The jaccard-only arm now requires >= 3 shared files to fire.
    # (Previously this identical-1-file case was wrongly filtered by jaccard=1.0
    # alone with no title/claim agreement required.)
    findings = [
        {
            "title": "finding_deduplication.py misses paraphrased cross-cycle titles",
            "description": "The word-overlap gate does not catch reworded duplicates.",
            "priority": "medium",
            "related_files": ["backend/app/finding_deduplication.py"],
        }
    ]
    existing = [
        (
            "dir-1",
            "finding_deduplication.py skips reworded research findings",
            None,
            ["backend/app/finding_deduplication.py"],
        )
    ]
    outcome = deduplicate_findings(
        findings=findings,
        task_context={"direction_id": "dir-1"},
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=existing,
        open_pr_files=set(),
    )
    # 1 shared file, title overlap = 1 word ("finding_deduplication.py") < 3
    # threshold, no claims → NOT a duplicate under the fixed threshold.
    assert len(outcome.deduplicated) == 1
    assert outcome.word_overlap_count == 0


def test_deduplicate_findings_passes_unrelated_technical_token_finding():
    # Unrelated findings with different technical tokens should NOT be blocked
    findings = [
        {
            "title": "orchestrator.py loses context on cold restart",
            "description": "Context is dropped.",
            "priority": "medium",
        }
    ]
    existing = [
        ("dir-1", "finding_deduplication.py skips reworded research findings", None)
    ]
    outcome = deduplicate_findings(
        findings=findings,
        task_context={"direction_id": "dir-1"},
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=existing,
        open_pr_files=set(),
    )
    assert len(outcome.deduplicated) == 1
    assert outcome.word_overlap_count == 0


# ---------------------------------------------------------------------------
# Regression tests for the small-file-set jaccard auto-duplicate false positive
# (STRONG_REGION_MIN_SHARED fix + 5-tuple claims support)
# Incident: two findings sharing identical 1-2-file sets were wrongly dropped
# against "fix pr #12662" (zero title/claim agreement) because jaccard=1.0>=0.7
# fired unconditionally, producing "2 duplicates skipped -> 0 tasks" NOTE.
# ---------------------------------------------------------------------------


def test_strong_region_min_shared_constant_is_three():
    """STRONG_REGION_MIN_SHARED must equal 3 — the minimum shared-file count
    required for the jaccard-only corroborator to assert duplicate identity."""
    assert STRONG_REGION_MIN_SHARED == 3


def test_incident_two_files_disjoint_titles_and_claims_kept():
    """Regression for the live incident: two findings sharing an identical 2-file
    set (acceptance_criteria.py + routers/admin_acceptance_criteria.py) with
    completely different titles and no claims must NOT be auto-deduped.

    Before the fix: file_jaccard=1.0>=STRONG_REGION_JACCARD fired unconditionally
    and both findings were dropped against "fix pr #12662".
    After the fix: the jaccard arm requires >=3 shared files; 2-file sets need
    title-word or claim corroboration, which is absent here → KEPT.
    """
    finding_a = {
        "title": "fix: backfill late criterion mappings onto tasks",
        "description": "Criterion mappings created after task spawn are not backfilled.",
        "priority": "medium",
        "direction_id": "dir-db2679e9",
        "related_files": [
            "backend/app/acceptance_criteria.py",
            "backend/app/routers/admin_acceptance_criteria.py",
        ],
        "claims": ["late-mapped criteria never reach existing tasks"],
    }
    finding_b = {
        "title": "feat: import HEAD regression evidence for criteria",
        "description": "Criteria lack HEAD regression evidence on initial import.",
        "priority": "medium",
        "direction_id": "dir-db2679e9",
        "related_files": [
            "backend/app/acceptance_criteria.py",
            "backend/app/routers/admin_acceptance_criteria.py",
        ],
        "claims": ["criteria import has no regression evidence"],
    }
    # Active fix-review task inherits the same 2-file set; no claims on this candidate
    existing = [
        (
            "dir-db2679e9",
            "fix pr #12662",
            "fix",
            [
                "backend/app/acceptance_criteria.py",
                "backend/app/routers/admin_acceptance_criteria.py",
            ],
        )
    ]
    outcome_a = deduplicate_findings(
        [finding_a],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=existing,
        open_pr_files=set(),
    )
    outcome_b = deduplicate_findings(
        [finding_b],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=existing,
        open_pr_files=set(),
    )
    assert len(outcome_a.deduplicated) == 1, (
        "finding_a must not be dropped: 2 shared files + no title/claim agreement"
    )
    assert outcome_a.word_overlap_count == 0
    assert len(outcome_b.deduplicated) == 1, (
        "finding_b must not be dropped: 2 shared files + no title/claim agreement"
    )
    assert outcome_b.word_overlap_count == 0


def test_incident_two_findings_both_kept_in_same_batch():
    """Both findings from the incident survive as a batch: neither is a duplicate
    of the other (distinct claims) and neither is a duplicate of the active task
    (no title/claim corroboration, file set < STRONG_REGION_MIN_SHARED)."""
    finding_a = {
        "title": "fix: backfill late criterion mappings onto tasks",
        "description": "Late-mapped criteria never reach tasks already spawned.",
        "priority": "medium",
        "direction_id": "dir-db2679e9",
        "related_files": [
            "backend/app/acceptance_criteria.py",
            "backend/app/routers/admin_acceptance_criteria.py",
        ],
        "claims": ["late-mapped criteria never reach existing tasks"],
    }
    finding_b = {
        "title": "feat: import HEAD regression evidence for criteria",
        "description": "Criteria import lacks HEAD regression evidence.",
        "priority": "medium",
        "direction_id": "dir-db2679e9",
        "related_files": [
            "backend/app/acceptance_criteria.py",
            "backend/app/routers/admin_acceptance_criteria.py",
        ],
        "claims": ["criteria import has no regression evidence"],
    }
    existing = [
        (
            "dir-db2679e9",
            "fix pr #12662",
            "fix",
            [
                "backend/app/acceptance_criteria.py",
                "backend/app/routers/admin_acceptance_criteria.py",
            ],
        )
    ]
    outcome = deduplicate_findings(
        [finding_a, finding_b],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=existing,
        open_pr_files=set(),
    )
    assert len(outcome.deduplicated) == 2, (
        "Both distinct findings must survive; was 0 before STRONG_REGION_MIN_SHARED fix"
    )
    assert outcome.word_overlap_count == 0


def test_two_files_shared_claim_still_filtered():
    """When two findings share a claim AND a 2-file set, the claim corroborator
    fires correctly and the duplicate IS filtered — the fix only drops the
    jaccard-only arm for small file sets; the claim arm is unchanged."""
    finding = {
        "title": "fix: backfill late criterion mappings onto tasks",
        "description": "desc",
        "priority": "medium",
        "direction_id": "dir-1",
        "related_files": ["app/acceptance_criteria.py", "app/routers/admin_ac.py"],
        "claims": ["late-mapped criteria never reach existing tasks"],
    }
    existing = [
        (
            "dir-1",
            "fix: criterion late-backfill is missing",
            "fix",
            ["app/acceptance_criteria.py", "app/routers/admin_ac.py"],
            ["late-mapped criteria never reach existing tasks"],  # shared claim → 5-tuple
        )
    ]
    outcome = deduplicate_findings(
        [finding],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=existing,
        open_pr_files=set(),
    )
    assert outcome.word_overlap_count == 1, "shared claim should still dedup 2-file match"
    assert outcome.deduplicated == []


def test_three_or_more_shared_files_jaccard_still_filters():
    """The jaccard-only corroborator IS still trusted when >=3 files are shared:
    three distinct but related findings anchored on the same three-file module
    with jaccard=1.0 and no title/claim agreement are filtered correctly."""
    finding = {
        "title": "perf: trim agent direction fetch response to agent fields",
        "description": "Full direction payload sent to agent is 3x larger than needed.",
        "priority": "medium",
        "direction_id": "dir-cf2897a6",
        "related_files": [
            "backend/app/routers/agent_strategic_fetch.py",
            "backend/app/schemas.py",
            "backend/app/models.py",
        ],
    }
    existing = [
        (
            "dir-cf2897a6",
            "fix: mark cross-attempt evidence in qualification",
            "fix",
            [
                "backend/app/routers/agent_strategic_fetch.py",
                "backend/app/schemas.py",
                "backend/app/models.py",
            ],
        )
    ]
    outcome = deduplicate_findings(
        [finding],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=existing,
        open_pr_files=set(),
    )
    # jaccard=1.0 with 3 shared files (>= STRONG_REGION_MIN_SHARED=3) → still filtered
    assert outcome.word_overlap_count == 1
    assert outcome.deduplicated == []
    # reason embeds the matched candidate title
    assert "fix: mark cross-attempt" in outcome.filtered[0]["reason"]


def test_five_tuple_claim_corroboration_for_active_task():
    """5-tuple active-task candidates carry claims (P2): the claim arm fires
    correctly for small file sets, so legitimate same-intent duplicates are
    still caught even when the jaccard arm no longer fires for < 3 files."""
    finding = {
        "title": "reworded version of existing fix approach",
        "description": "Agent re-derives the same fix with different phrasing.",
        "priority": "medium",
        "direction_id": "dir-1",
        "related_files": ["app/acceptance_criteria.py", "app/routers/ac.py"],
        "claims": ["criterion backfill path skips tasks created before mapping"],
    }
    # 5-tuple: (dir, title, task_type, related_files, claims)
    existing_5tuple = [
        (
            "dir-1",
            "fix: backfill criterion mapping for pre-existing tasks",
            "fix",
            ["app/acceptance_criteria.py", "app/routers/ac.py"],
            ["criterion backfill path skips tasks created before mapping"],  # shared claim
        )
    ]
    outcome = deduplicate_findings(
        [finding],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=existing_5tuple,
        open_pr_files=set(),
    )
    # Shared claim corroborates → filtered, even though only 2 files shared
    assert outcome.word_overlap_count == 1
    assert outcome.deduplicated == []
    assert "reworded version" in outcome.filtered[0]["title"]


def test_five_tuple_no_claim_match_two_files_kept():
    """5-tuple active-task candidate with DIFFERENT claims and only 2 shared
    files: no claim corroboration, jaccard arm requires >=3 files → KEPT."""
    finding = {
        "title": "distinct finding about a different aspect",
        "description": "Different aspect of the same module.",
        "priority": "medium",
        "direction_id": "dir-1",
        "related_files": ["app/acceptance_criteria.py", "app/routers/ac.py"],
        "claims": ["some completely different claim about a distinct issue"],
    }
    existing_5tuple = [
        (
            "dir-1",
            "fix: backfill criterion mapping for pre-existing tasks",
            "fix",
            ["app/acceptance_criteria.py", "app/routers/ac.py"],
            ["criterion backfill path skips tasks created before mapping"],
        )
    ]
    outcome = deduplicate_findings(
        [finding],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=existing_5tuple,
        open_pr_files=set(),
    )
    # No shared claim, 2 files < STRONG_REGION_MIN_SHARED=3, low word overlap → KEPT
    assert len(outcome.deduplicated) == 1
    assert outcome.word_overlap_count == 0


def test_cross_direction_two_files_no_claim_kept():
    """Cross-direction: two findings sharing a 2-file set but with no shared
    claims must NOT be auto-deduped (previously jaccard=1.0 fired unconditionally
    for cross-direction candidates too, conflating distinct work from independent
    directions that shared a module's hot files — confirmed live in d-a94e67f5
    where test-invariants finding was dropped against a feature task)."""
    finding = {
        "title": "test: packer floor-sum and monotonic degradation invariants",
        "description": "Add invariant tests for the packer floor-sum.",
        "priority": "medium",
        "direction_id": "dir-a94e67f5",
        "related_files": [
            "backend/app/strategic_payload_compaction.py",
            "backend/testing/test_pack_under_budget.py",
        ],
    }
    existing = [
        (
            "dir-other",  # DIFFERENT direction
            "feat: sized trim log with per-section recovery hints",
            "fix",
            [
                "backend/app/strategic_payload_compaction.py",
                "backend/testing/test_pack_under_budget.py",
            ],
        )
    ]
    outcome = deduplicate_findings(
        [finding],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=existing,
        open_pr_files=set(),
    )
    assert len(outcome.deduplicated) == 1, (
        "cross-direction 2-file identical sets without claim agreement must be KEPT"
    )
    assert outcome.word_overlap_count == 0


def test_five_tuple_legacy_four_tuple_claims_fallback():
    """Legacy 4-tuple candidates still work — the claims slot defaults to frozenset()
    and the fix does not break existing callers that have not yet been updated."""
    finding = {
        "title": "investigate alpha serialization latency",
        "description": "desc",
        "priority": "medium",
        "direction_id": "dir-1",
        "related_files": ["app/foo.py", "app/bar.py"],
        "claims": ["alpha serialization causes latency spikes"],
    }
    # 4-tuple (no claims) — legacy format; title has no overlapping words with finding
    existing_4tuple = [
        ("dir-1", "migrate beta compression pipeline", "fix", ["app/foo.py", "app/bar.py"])
    ]
    outcome = deduplicate_findings(
        [finding],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=existing_4tuple,
        open_pr_files=set(),
    )
    # 4-tuple has no claims → claim arm cannot fire; 2 shared files < 3 → kept
    assert len(outcome.deduplicated) == 1
    assert outcome.word_overlap_count == 0
