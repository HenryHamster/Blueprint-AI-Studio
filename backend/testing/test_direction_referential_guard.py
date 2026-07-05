"""Unit tests for backend/app/direction_referential_guard.py.

Exercises the pure classifier (classify_criterion_key) and the sentinel-tuple
helper (check_criterion_key_write).  The async DB loader
(load_version_criterion_key_sets) is exercised via the integration tests in
test_admin_acceptance_criteria_router.py.

Also covers cycle-start reconciliation invariants (orphaned-gap,
contradictory-staffing/stale_active_moves, ungrounded-direction) and
criterion-to-direction mapping-link guards via compute_model_integrity_conflicts.
These tests form the regression suite for criterion c-9104511c.
"""
from app.direction_referential_guard import (
    check_criterion_key_write,
    classify_criterion_key,
)
from app.strategic_state_read_model import compute_model_integrity_conflicts

ACTIVE = frozenset(["c-active1", "c-active2"])
INACTIVE = frozenset(["c-retracted", "c-superseded"])


# ---------------------------------------------------------------------------
# classify_criterion_key
# ---------------------------------------------------------------------------


def test_classify_valid():
    assert classify_criterion_key("c-active1", ACTIVE, INACTIVE) == "valid"


def test_classify_valid_second_key():
    assert classify_criterion_key("c-active2", ACTIVE, INACTIVE) == "valid"


def test_classify_inactive_retracted():
    assert classify_criterion_key("c-retracted", ACTIVE, INACTIVE) == "inactive"


def test_classify_inactive_superseded():
    assert classify_criterion_key("c-superseded", ACTIVE, INACTIVE) == "inactive"


def test_classify_dangling():
    assert classify_criterion_key("c-nonexistent", ACTIVE, INACTIVE) == "dangling"


def test_classify_empty_key():
    assert classify_criterion_key("", ACTIVE, INACTIVE) == "dangling"


def test_classify_empty_sets():
    assert classify_criterion_key("c-x", frozenset(), frozenset()) == "dangling"


# ---------------------------------------------------------------------------
# check_criterion_key_write
# ---------------------------------------------------------------------------


def test_check_returns_none_for_valid():
    cls, active_list = check_criterion_key_write("c-active1", ACTIVE, INACTIVE)
    assert cls is None
    assert active_list == []


def test_check_returns_inactive_with_active_list():
    cls, active_list = check_criterion_key_write("c-retracted", ACTIVE, INACTIVE)
    assert cls == "inactive"
    assert set(active_list) == {"c-active1", "c-active2"}
    assert active_list == sorted(active_list)  # returned list is sorted


def test_check_returns_dangling_with_active_list():
    cls, active_list = check_criterion_key_write("c-unknown", ACTIVE, INACTIVE)
    assert cls == "dangling"
    assert set(active_list) == {"c-active1", "c-active2"}


def test_check_empty_key_returns_none():
    # Empty key is a no-op at the write surface — caller handles empty as skip.
    cls, active_list = check_criterion_key_write("", ACTIVE, INACTIVE)
    assert cls is None
    assert active_list == []


def test_check_active_list_is_sorted():
    active = frozenset(["c-z", "c-a", "c-m"])
    inactive = frozenset()
    cls, active_list = check_criterion_key_write("c-missing", active, inactive)
    assert cls == "dangling"
    assert active_list == ["c-a", "c-m", "c-z"]


# ---------------------------------------------------------------------------
# Inventory regression: enumerate all Direction.criterion_keys write sites
# so future writers cannot bypass the guard without tripping CI.
# ---------------------------------------------------------------------------


def test_criterion_keys_write_sites_are_enumerated():
    """Every non-test production file that writes Direction.criterion_keys must
    use a guard (load_version_criterion_key_sets or classify_criterion_key).

    This grep-based assertion enumerates the known write sites.  If a new
    writer is added without guard wiring, update this list AND wire the guard.
    """
    import pathlib

    backend_app = pathlib.Path(__file__).parent.parent / "app"

    # Files where Direction.criterion_keys is assigned (updated as new writers land).
    KNOWN_GUARDED_WRITERS = {
        "routers/admin_acceptance_criteria.py",  # operator link-direction endpoint
        "routers/admin_agent_ops.py",             # strategic set_acceptance_criteria op
    }

    def _assigns_criterion_keys(path: pathlib.Path) -> bool:
        """Return True if the file contains a `.criterion_keys =` assignment."""
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            return False
        return ".criterion_keys =" in source

    def _uses_guard(path: pathlib.Path) -> bool:
        """Return True if the file imports from direction_referential_guard."""
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            return False
        return "direction_referential_guard" in source

    unguarded: list[str] = []
    all_writers: list[str] = []
    for py_file in sorted(backend_app.rglob("*.py")):
        rel = py_file.relative_to(backend_app).as_posix()
        if not _assigns_criterion_keys(py_file):
            continue
        all_writers.append(rel)
        if rel not in KNOWN_GUARDED_WRITERS and not _uses_guard(py_file):
            unguarded.append(rel)

    assert not unguarded, (
        f"New Direction.criterion_keys writer(s) found without guard wiring: {unguarded}. "
        "Add the file to KNOWN_GUARDED_WRITERS and wire load_version_criterion_key_sets "
        "or classify_criterion_key from direction_referential_guard.py."
    )
    # Verify all declared writers actually exist (catches stale entries).
    for declared in KNOWN_GUARDED_WRITERS:
        assert declared in all_writers, (
            f"KNOWN_GUARDED_WRITERS entry '{declared}' does not write .criterion_keys — "
            "remove it from the list."
        )


# ---------------------------------------------------------------------------
# Helpers for reconciliation invariant tests
# ---------------------------------------------------------------------------

def _gap(id: str, name: str, status: str = "active", active_moves: list | None = None, deferral: dict | None = None) -> dict:
    g: dict = {"id": id, "name": name, "status": status}
    if active_moves is not None:
        g["active_moves"] = active_moves
    if deferral is not None:
        g["deferral"] = deferral
    return g


def _dir(id: str, text: str, gap_id: str | None = None, status: str = "active", criterion_keys: list | None = None) -> dict:
    d: dict = {"id": id, "text": text, "status": status}
    if gap_id is not None:
        d["gap_id"] = gap_id
    if criterion_keys is not None:
        d["criterion_keys"] = criterion_keys
    return d


# ---------------------------------------------------------------------------
# Reconciliation invariant: orphaned-gap (c-9104511c)
#
# Invariant: no active/blocked open gap may exist without an active direction
# staffed on it (unless it carries an explicit deferral).
# ---------------------------------------------------------------------------


def test_recon_orphaned_active_gap_with_no_direction():
    """Active gap with no direction pointing at it → orphaned_gap reported."""
    pm = {"gaps": [_gap("g-1", "Unattended gap")]}
    result = compute_model_integrity_conflicts(pm, [])
    assert result is not None
    assert len(result["orphaned_gaps"]) == 1
    assert result["orphaned_gaps"][0]["id"] == "g-1"
    assert result["counts"]["orphaned_gaps"] == 1


def test_recon_orphaned_blocked_gap_reported():
    """A blocked (not just active) open gap with no direction is also an orphan."""
    pm = {"gaps": [_gap("g-b", "Blocked gap", status="blocked")]}
    result = compute_model_integrity_conflicts(pm, [])
    assert result is not None
    orphans = result["orphaned_gaps"]
    assert len(orphans) == 1
    assert orphans[0]["status"] == "blocked"


def test_recon_closed_gap_not_an_orphan():
    """Closed gaps are not open problems — they do not appear in orphaned_gaps."""
    pm = {"gaps": [_gap("g-c", "Closed gap", status="closed")]}
    result = compute_model_integrity_conflicts(pm, [])
    assert result is None


def test_recon_deferred_gap_not_an_orphan():
    """A gap with an explicit deferral is intentionally unstaffed → not an orphan."""
    pm = {"gaps": [_gap("g-d", "Deferred gap", deferral={"reason": "parked"})]}
    result = compute_model_integrity_conflicts(pm, [])
    assert result is None


def test_recon_gap_staffed_by_active_direction_is_clean():
    """An active gap with an active direction grounded on it → no conflict."""
    pm = {"gaps": [_gap("g-1", "Staffed gap", active_moves=["d-1"])]}
    dirs = [_dir("d-1", "Active direction", gap_id="g-1")]
    assert compute_model_integrity_conflicts(pm, dirs) is None


def test_recon_orphan_count_spans_multiple_gaps():
    """Multiple unstaffed open gaps each contribute to orphaned_gaps count."""
    pm = {"gaps": [
        _gap("g-1", "Gap 1"),
        _gap("g-2", "Gap 2"),
        _gap("g-3", "Gap 3", status="closed"),  # closed — excluded
    ]}
    result = compute_model_integrity_conflicts(pm, [])
    assert result is not None
    assert result["counts"]["orphaned_gaps"] == 2
    assert {o["id"] for o in result["orphaned_gaps"]} == {"g-1", "g-2"}


# ---------------------------------------------------------------------------
# Reconciliation invariant: ungrounded-direction / no-closed-gap reference
# (c-9104511c)
#
# Invariant: no active direction may reference a closed or nonexistent gap.
# ---------------------------------------------------------------------------


def test_recon_direction_references_closed_gap_is_ungrounded():
    """Active direction whose gap_id points at a closed gap → ungrounded."""
    pm = {"gaps": [_gap("g-1", "Closed gap", status="closed")]}
    dirs = [_dir("d-1", "Drifted direction", gap_id="g-1")]
    result = compute_model_integrity_conflicts(pm, dirs)
    assert result is not None
    ud = result["ungrounded_directions"]
    assert len(ud) == 1
    assert ud[0]["id"] == "d-1"
    assert ud[0]["reason"] == "gap_not_active"
    assert ud[0]["gap_id"] == "g-1"


def test_recon_direction_references_nonexistent_gap_is_ungrounded():
    """Active direction whose gap_id does not exist in the problem model → ungrounded."""
    pm = {"gaps": []}  # gap_id "g-gone" is simply absent
    dirs = [_dir("d-1", "Drifted direction", gap_id="g-gone")]
    result = compute_model_integrity_conflicts(pm, dirs)
    assert result is not None
    ud = result["ungrounded_directions"]
    assert len(ud) == 1
    assert ud[0]["id"] == "d-1"
    assert ud[0]["reason"] == "gap_not_active"
    assert ud[0]["gap_id"] == "g-gone"


def test_recon_direction_with_no_gap_id_is_ungrounded():
    """Active direction that carries no gap_id at all → ungrounded (no_gap_id)."""
    pm = {"gaps": []}
    dirs = [_dir("d-1", "Floating direction")]
    result = compute_model_integrity_conflicts(pm, dirs)
    assert result is not None
    ud = result["ungrounded_directions"]
    assert len(ud) == 1
    assert ud[0]["reason"] == "no_gap_id"
    assert ud[0]["gap_id"] is None


def test_recon_paused_direction_excluded_from_ungrounded_check():
    """Paused directions are not active — they must not appear in ungrounded_directions."""
    pm = {"gaps": []}
    dirs = [_dir("d-p", "Paused direction", gap_id="g-gone", status="paused")]
    # No active directions means no ungrounded_directions, but gap is not in PM so no orphan either.
    result = compute_model_integrity_conflicts(pm, dirs)
    assert result is None


# ---------------------------------------------------------------------------
# Reconciliation invariant: contradictory-staffing / stale_active_moves
# (c-9104511c)
#
# Invariant: a gap's active_moves list must not reference directions that are
# no longer active (retired, completed, absent). Stale entries create
# contradictory information about who is staffed on a gap.
# ---------------------------------------------------------------------------


def test_recon_stale_active_moves_retired_direction():
    """Gap lists a retired direction as active → stale_active_moves flagged."""
    pm = {"gaps": [_gap("g-1", "Gap 1", active_moves=["d-retired", "d-1"])]}
    dirs = [_dir("d-1", "Active direction", gap_id="g-1")]
    result = compute_model_integrity_conflicts(pm, dirs)
    assert result is not None
    sam = result["stale_active_moves"]
    assert len(sam) == 1
    assert sam[0]["gap_id"] == "g-1"
    assert "d-retired" in sam[0]["stale_direction_ids"]
    assert "d-1" not in sam[0]["stale_direction_ids"]


def test_recon_stale_active_moves_absent_direction():
    """Gap lists a direction ID that is completely absent from the direction list → stale."""
    pm = {"gaps": [_gap("g-1", "Gap 1", active_moves=["d-ghost"])]}
    dirs = []
    result = compute_model_integrity_conflicts(pm, dirs)
    assert result is not None
    sam = result["stale_active_moves"]
    assert len(sam) == 1
    assert "d-ghost" in sam[0]["stale_direction_ids"]


def test_recon_stale_active_moves_on_blocked_gap():
    """A blocked gap with stale active_moves is still flagged."""
    pm = {"gaps": [_gap("g-b", "Blocked gap", status="blocked", active_moves=["d-old"])]}
    dirs = []
    result = compute_model_integrity_conflicts(pm, dirs)
    assert result is not None
    assert any(s["gap_id"] == "g-b" for s in result["stale_active_moves"])


def test_recon_no_stale_moves_when_active_moves_is_accurate():
    """Gap whose active_moves exactly matches the set of live active directions → clean."""
    pm = {"gaps": [_gap("g-1", "Gap 1", active_moves=["d-1"])]}
    dirs = [_dir("d-1", "Active direction", gap_id="g-1")]
    assert compute_model_integrity_conflicts(pm, dirs) is None


# ---------------------------------------------------------------------------
# Reconciliation invariant: all four conflict classes detected together
# (c-9104511c)
# ---------------------------------------------------------------------------


def test_recon_all_conflict_classes_in_one_model():
    """A broken model surfaces all four conflict classes simultaneously."""
    pm = {
        "gaps": [
            _gap("g-orphan", "Orphaned gap"),                           # no direction → orphaned
            _gap("g-stale", "Stale gap", active_moves=["d-retired"]),  # stale active_moves
        ]
    }
    dirs = [
        _dir("d-ungrounded", "Ungrounded dir", gap_id="g-gone"),  # nonexistent gap → ungrounded
    ]
    criterion_links = [
        {
            "id": "d-ungrounded",
            "text": "Ungrounded dir",
            "status": "active",
            "criterion_keys": ["c-dangling"],
        }
    ]
    result = compute_model_integrity_conflicts(
        pm,
        dirs,
        criterion_links=criterion_links,
        active_criterion_keys=frozenset(),
        inactive_criterion_keys=frozenset(),
    )
    assert result is not None
    assert result["counts"]["orphaned_gaps"] >= 1
    assert result["counts"]["ungrounded_directions"] >= 1
    assert result["counts"]["stale_active_moves"] >= 1
    assert result["counts"]["criterion_link_conflicts"] >= 1
    assert result["counts"]["total"] >= 4


# ---------------------------------------------------------------------------
# Criterion-to-direction mapping-link guards via compute_model_integrity_conflicts
# (c-9104511c)
# ---------------------------------------------------------------------------


def test_mapping_link_inactive_criterion_conflict():
    """Active direction referencing an inactive (retracted/superseded) criterion → conflict."""
    pm = {"gaps": [_gap("g-1", "Gap 1", active_moves=["d-1"])]}
    dirs = [_dir("d-1", "Direction 1", gap_id="g-1")]
    criterion_links = [
        {"id": "d-1", "text": "Direction 1", "status": "active", "criterion_keys": ["c-retracted"]}
    ]
    result = compute_model_integrity_conflicts(
        pm, dirs,
        criterion_links=criterion_links,
        active_criterion_keys=frozenset(),
        inactive_criterion_keys=frozenset({"c-retracted"}),
    )
    assert result is not None
    clc = result["criterion_link_conflicts"]
    assert len(clc) == 1
    assert clc[0]["criterion_key"] == "c-retracted"
    assert clc[0]["classification"] == "inactive"
    assert clc[0]["direction_id"] == "d-1"
    assert "retracted" in clc[0]["recovery_hint"].lower() or "superseded" in clc[0]["recovery_hint"].lower()


def test_mapping_link_dangling_criterion_conflict():
    """Active direction referencing a dangling (unknown/wrong-version) criterion → conflict."""
    pm = {"gaps": [_gap("g-1", "Gap 1", active_moves=["d-1"])]}
    dirs = [_dir("d-1", "Direction 1", gap_id="g-1")]
    criterion_links = [
        {"id": "d-1", "text": "Direction 1", "status": "active", "criterion_keys": ["c-unknown"]}
    ]
    result = compute_model_integrity_conflicts(
        pm, dirs,
        criterion_links=criterion_links,
        active_criterion_keys=frozenset(),
        inactive_criterion_keys=frozenset(),
    )
    assert result is not None
    clc = result["criterion_link_conflicts"]
    assert len(clc) == 1
    assert clc[0]["classification"] == "dangling"
    assert "open version" in clc[0]["recovery_hint"].lower()


def test_mapping_link_valid_criterion_no_conflict():
    """Active direction with all valid criterion keys → no criterion conflict."""
    pm = {"gaps": [_gap("g-1", "Gap 1", active_moves=["d-1"])]}
    dirs = [_dir("d-1", "Direction 1", gap_id="g-1")]
    criterion_links = [
        {"id": "d-1", "text": "Direction 1", "status": "active", "criterion_keys": ["c-valid"]}
    ]
    result = compute_model_integrity_conflicts(
        pm, dirs,
        criterion_links=criterion_links,
        active_criterion_keys=frozenset({"c-valid"}),
        inactive_criterion_keys=frozenset(),
    )
    assert result is None


def test_mapping_link_retired_direction_criterion_ignored():
    """Stale criterion keys on a retired direction are not operator-actionable → ignored."""
    pm = {"gaps": []}
    dirs: list = []
    criterion_links = [
        {"id": "d-ret", "text": "Retired dir", "status": "retired", "criterion_keys": ["c-dangling"]}
    ]
    result = compute_model_integrity_conflicts(
        pm, dirs,
        criterion_links=criterion_links,
        active_criterion_keys=frozenset(),
        inactive_criterion_keys=frozenset(),
    )
    assert result is None


def test_mapping_link_omitting_criterion_args_backward_compatible():
    """Callers that omit criterion_links/active_keys stay backward compatible — no error."""
    pm = {"gaps": [_gap("g-1", "Gap 1", active_moves=["d-1"])]}
    dirs = [_dir("d-1", "Direction 1", gap_id="g-1")]
    # No criterion_links, active_criterion_keys, or inactive_criterion_keys passed.
    assert compute_model_integrity_conflicts(pm, dirs) is None
