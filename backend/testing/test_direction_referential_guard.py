"""Unit tests for backend/app/direction_referential_guard.py.

Exercises the pure classifier (classify_criterion_key) and the sentinel-tuple
helper (check_criterion_key_write).  The async DB loader
(load_version_criterion_key_sets) is exercised via the integration tests in
test_admin_acceptance_criteria_router.py.
"""
from app.direction_referential_guard import (
    check_criterion_key_write,
    classify_criterion_key,
)

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
