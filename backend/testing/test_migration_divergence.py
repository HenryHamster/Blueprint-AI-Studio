"""Tests for find_content_divergent_revisions — the cross-ref migration-identity
divergence detector that catches the back-merge renumber class (prod incident
2026-06-04) that every structural/single-checkout guard is blind to.

Pure, no DB — filename-list comparison only.
"""

from app.migration_chain import find_content_divergent_revisions


def test_no_divergence_when_refs_agree():
    base = ["0137_cascade.py", "0138_digest_cache_table.py"]
    head = ["0137_cascade.py", "0138_digest_cache_table.py"]
    assert find_content_divergent_revisions(base, head) == []


def test_normal_pr_adding_new_revisions_is_clean():
    """A normal PR extends the chain with brand-new revision ids — no shared id
    diverges, so nothing is flagged (no false positives)."""
    base = ["0137_cascade.py", "0138_digest_cache_table.py"]
    head = [*base, "0139_new_feature_table.py", "0140_another_thing.py"]
    assert find_content_divergent_revisions(base, head) == []


def test_in_place_edit_same_id_same_description_is_clean():
    """Editing a migration's body in place (same filename) is not a divergence —
    the detector is filename/identity based, so a legit re-touch never trips it."""
    base = ["0140_digest_cache_table.py"]
    head = ["0140_digest_cache_table.py"]
    assert find_content_divergent_revisions(base, head) == []


def test_same_revision_id_different_migration_is_flagged():
    """(a) A revision id reused for different content — the DB that applied the
    first silently skips the second."""
    base = ["0138_digest_cache_table.py"]
    head = ["0138_task_blocked_state_fields.py"]
    out = find_content_divergent_revisions(base, head)
    assert len(out) == 1
    assert "0138" in out[0]
    assert "digest_cache_table" in out[0] and "task_blocked_state_fields" in out[0]


def test_renumbered_migration_same_description_different_id_is_flagged():
    """(b) The same migration under two different revision ids — a renumber."""
    base = ["0138_digest_cache_table.py"]
    head = ["0140_digest_cache_table.py"]
    out = find_content_divergent_revisions(base, head)
    assert len(out) == 1
    assert "digest_cache_table" in out[0]
    assert "0138" in out[0] and "0140" in out[0]


def test_real_incident_backmerge_renumber_is_caught():
    """The actual prod incident: digest_cache_table was main's 0138; the
    main->automain back-merge renumbered it to 0140, putting automain's
    task_blocked_state_fields at 0138 and merge_gate at 0139. The on-disk chain
    is internally valid on BOTH sides (single head, filenames match revisions),
    so every structural guard passes — but revision 0138 now means two different
    things, and digest moved ids. Both must be flagged BEFORE the merge."""
    main = ["0137_cascade.py", "0138_digest_cache_table.py"]
    automain = [
        "0137_cascade.py",
        "0138_task_blocked_state_fields.py",
        "0139_project_merge_gate_runtime.py",
        "0140_digest_cache_table.py",
    ]
    out = find_content_divergent_revisions(main, automain)
    # (a) revision 0138 maps to different migrations, and (b) digest renumbered.
    assert any("0138" in d and "digest_cache_table" in d for d in out)
    assert any("digest_cache_table" in d and "0140" in d for d in out)
    assert len(out) >= 2


def test_non_migration_filenames_ignored():
    base = ["__init__.py", "env.py", "0138_digest_cache_table.py", "README.md"]
    head = ["__init__.py", "env.py", "0138_digest_cache_table.py"]
    assert find_content_divergent_revisions(base, head) == []


def test_revision_present_on_only_one_side_is_not_a_divergence():
    """A revision unique to one ref (added or removed) is not a content
    divergence — only SHARED ids/descriptions that disagree are flagged."""
    base = ["0138_digest_cache_table.py"]
    head = ["0138_digest_cache_table.py", "0139_brand_new.py"]
    assert find_content_divergent_revisions(base, head) == []
