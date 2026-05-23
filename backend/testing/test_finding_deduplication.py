"""Unit tests for app.finding_deduplication — pure dedup helpers."""

import hashlib
import uuid
from datetime import datetime, timedelta


from app.finding_deduplication import (
    CODE_ERROR_COOLDOWN,
    DedupOutcome,
    _check_evidence_overlap,
    _extract_normalized_claims,
    _extract_source_refs,
    _find_retired_approach_match,
    _find_archived_diagnosis_match,
    _jaccard_overlap,
    _normalize_source_ref,
    build_dedup_state,
    deduplicate_findings,
    extract_archived_diagnoses,
    extract_retired_failed_approaches,
    finding_hash,
    is_duplicate,
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
        created_at=created_at or datetime.utcnow(),
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
    now = datetime.utcnow()
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
    now = datetime.utcnow()
    at_boundary = _make_task(
        status=TaskStatus.FAILED,
        context={"finding_hash": "boundary"},
        created_at=now - CODE_ERROR_COOLDOWN,
        latest_status_message="CODE_ERROR: unit test failed at assertion",
    )
    hashes, _, _ = build_dedup_state([at_boundary], failed_parent_ids=set(), now=now)
    assert "boundary" not in hashes


def test_build_dedup_state_suppress_until_active():
    now = datetime.utcnow()
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
    now = datetime.utcnow()
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
    now = datetime.utcnow()
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


def test_deduplicate_findings_word_overlap_skip():
    finding = {
        "title": "alpha beta gamma delta",
        "description": "desc",
        "priority": "medium",
        "direction_id": "dir-1",
    }
    existing = [("dir-1", "alpha beta gamma delta something-else")]
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
        # No direction_id on finding — should fall back to task_context direction_id
    }
    # Existing task shares 4+ words in the SAME direction (via inheritance)
    existing = [("dir-parent", "alpha beta gamma delta different")]
    outcome = deduplicate_findings(
        [finding],
        task_context={"direction_id": "dir-parent"},
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=existing,
        open_pr_files=set(),
    )
    assert outcome.word_overlap_count == 1


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


def test_deduplicate_findings_records_word_overlap_matched_title():
    finding = {
        "title": "alpha beta gamma delta novel",
        "description": "desc",
        "priority": "medium",
        "direction_id": "dir-1",
    }
    existing_title = "alpha beta gamma delta something-else"
    outcome = deduplicate_findings(
        [finding],
        task_context=None,
        recent_finding_hashes=set(),
        failed_hashes={},
        existing_tasks_with_dirs=[("dir-1", existing_title)],
        open_pr_files=set(),
    )
    assert len(outcome.filtered) == 1
    rec = outcome.filtered[0]
    assert rec["title"] == "alpha beta gamma delta novel"
    assert rec["direction_id"] == "dir-1"
    assert rec["reason"].startswith("word overlap with: ")
    assert "alpha beta gamma delta" in rec["reason"]
    # The persisted reason must stay compact so it does not bloat the prompt.
    assert len(rec["reason"]) <= 80


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
            {"type": "file", "ref": "backend/app/db.py"},
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
        },
        {"failed_approaches": ["async migration approach"]},
    ]
    result = extract_retired_failed_approaches(retired)
    assert result == [
        "cache invalidation strategy",
        "redis cluster approach",
        "fix logging timestamps",
        "async migration approach",
    ]


def test_extract_retired_failed_approaches_empty_and_none():
    assert extract_retired_failed_approaches([]) == []
    assert extract_retired_failed_approaches(None) == []
    assert extract_retired_failed_approaches([{"unrelated_key": "x"}]) == []


def test_find_retired_approach_match_3_word_threshold():
    approaches = [
        "cache invalidation performance problem",
        "redis migration strategy gone",
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
        retired_failed_approaches=["cache invalidation performance problem"],
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
        retired_failed_approaches=["cache invalidation performance problem"],
    )
    assert len(outcome.deduplicated) == 1
    assert outcome.deduplicated[0][2] == fhash
    assert outcome.retired_approach_count == 0
    assert outcome.hash_dedup_count == 0


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
