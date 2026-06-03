"""Tests for the module_size_limits.json custom merge driver (merge_size_limits.py).

Covers: per-key max-cap wins; a key present on only one side is kept (union);
caps are never lowered or dropped; ``_doc`` and all three sections are preserved;
the written file is byte-identical to ``check_size_limits.write_limits`` output;
``main`` returns 0 on success and writes the resolution to %A; and malformed input
on either side returns non-zero so the conflict falls through to the normal path.
"""

import json
import os

from testing import check_size_limits as csl
from testing import merge_size_limits as msl


def _write_json(path, data) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=True)
        f.write("\n")


# ── merge_limits: max / union semantics ──────────────────────────────────────


def test_per_key_max_cap_wins():
    ours = {"_doc": "d", "backend": {"a.py": 100, "b.py": 500}}
    theirs = {"_doc": "d", "backend": {"a.py": 300, "b.py": 200}}
    merged = msl.merge_limits(ours, theirs)
    # Each key takes the LARGER of the two caps regardless of which side it's on.
    assert merged["backend"]["a.py"] == 300
    assert merged["backend"]["b.py"] == 500


def test_key_present_on_only_one_side_is_kept():
    ours = {"_doc": "d", "backend": {"a.py": 100, "ours_only.py": 700}}
    theirs = {"_doc": "d", "backend": {"a.py": 100, "theirs_only.py": 900}}
    merged = msl.merge_limits(ours, theirs)
    # Union of keys — neither side's unique key is dropped.
    assert merged["backend"]["ours_only.py"] == 700
    assert merged["backend"]["theirs_only.py"] == 900
    assert merged["backend"]["a.py"] == 100


def test_never_lowers_a_cap():
    # Even when theirs is uniformly lower, no resulting cap drops below ours.
    ours = {"_doc": "d", "backend": {"a.py": 1000, "b.py": 2000}}
    theirs = {"_doc": "d", "backend": {"a.py": 10, "b.py": 20}}
    merged = msl.merge_limits(ours, theirs)
    assert merged["backend"]["a.py"] == 1000
    assert merged["backend"]["b.py"] == 2000


def test_doc_and_all_three_sections_preserved():
    ours = {
        "_doc": "the canonical doc string",
        "backend": {"a.py": 1},
        "agent": {"x.ts": 2},
        "frontend": {"y.tsx": 3},
    }
    theirs = {
        "_doc": "the canonical doc string",
        "backend": {"a.py": 5},
        "agent": {"x.ts": 2},
        "frontend": {"y.tsx": 9},
    }
    merged = msl.merge_limits(ours, theirs)
    assert merged["_doc"] == "the canonical doc string"
    assert set(merged.keys()) == {"_doc", "backend", "agent", "frontend"}
    assert merged["backend"]["a.py"] == 5
    assert merged["frontend"]["y.tsx"] == 9


def test_section_present_on_only_one_side_is_kept():
    ours = {"_doc": "d", "backend": {"a.py": 1}}
    theirs = {"_doc": "d", "backend": {"a.py": 1}, "agent": {"x.ts": 2}}
    merged = msl.merge_limits(ours, theirs)
    assert merged["agent"] == {"x.ts": 2}


def test_merge_is_order_independent_on_shared_keys():
    a = {"_doc": "d", "backend": {"a.py": 100, "b.py": 500}}
    b = {"_doc": "d", "backend": {"a.py": 300, "b.py": 200}}
    # Max on shared keys is symmetric, so cap values match regardless of side.
    assert msl.merge_limits(a, b)["backend"] == msl.merge_limits(b, a)["backend"]


# ── main(): file IO, byte-stable format, exit codes ──────────────────────────


def test_main_writes_max_union_to_ours_path_and_returns_zero(tmp_path):
    ancestor = tmp_path / "O"
    ours = tmp_path / "A"
    theirs = tmp_path / "B"
    _write_json(ancestor, {"_doc": "d", "backend": {"a.py": 100}})
    _write_json(ours, {"_doc": "d", "backend": {"a.py": 100, "b.py": 500}})
    _write_json(theirs, {"_doc": "d", "backend": {"a.py": 300}})

    rc = msl.main([str(ancestor), str(ours), str(theirs)])
    assert rc == 0
    # %A (ours) is overwritten with the resolution git reads back.
    result = json.loads(ours.read_text())
    assert result["backend"]["a.py"] == 300
    assert result["backend"]["b.py"] == 500


def test_main_output_byte_identical_to_write_limits(tmp_path):
    ours = tmp_path / "A"
    theirs = tmp_path / "B"
    _write_json(ours, {"_doc": "café — dash", "backend": {"a.py": 100}})
    _write_json(theirs, {"_doc": "café — dash", "backend": {"a.py": 300}})

    msl.main([str(ours), str(ours), str(theirs)])
    actual_bytes = ours.read_bytes()

    # Build the expected bytes via the canonical writer the checker uses.
    expected_path = tmp_path / "expected"
    csl.write_limits(str(expected_path), {"_doc": "café — dash", "backend": {"a.py": 300}})
    assert actual_bytes == expected_path.read_bytes()


def test_main_malformed_ours_returns_nonzero_and_does_not_overwrite(tmp_path):
    ours = tmp_path / "A"
    theirs = tmp_path / "B"
    ours.write_text("{ this is not valid json")
    _write_json(theirs, {"_doc": "d", "backend": {"a.py": 300}})

    rc = msl.main([str(ours), str(ours), str(theirs)])
    # Non-zero → git leaves the conflict in place for the normal fix path.
    assert rc != 0
    # Ours is left untouched (still the malformed original).
    assert ours.read_text() == "{ this is not valid json"


def test_main_malformed_theirs_returns_nonzero(tmp_path):
    ours = tmp_path / "A"
    theirs = tmp_path / "B"
    _write_json(ours, {"_doc": "d", "backend": {"a.py": 100}})
    theirs.write_text("not json either")
    assert msl.main([str(ours), str(ours), str(theirs)]) != 0


def test_main_too_few_args_returns_nonzero():
    assert msl.main(["only-one-path"]) != 0


def test_main_non_object_top_level_returns_nonzero(tmp_path):
    ours = tmp_path / "A"
    theirs = tmp_path / "B"
    ours.write_text("[1, 2, 3]\n")
    _write_json(theirs, {"_doc": "d"})
    assert msl.main([str(ours), str(ours), str(theirs)]) != 0


def test_resolved_file_passes_round_trip_reload(tmp_path):
    # The resolved file must reload cleanly (no markers, valid JSON) — proving an
    # agent rebasing sees a clean auto-resolution.
    ours = tmp_path / "A"
    theirs = tmp_path / "B"
    _write_json(ours, {"_doc": "d", "backend": {"a.py": 100}, "agent": {"x.ts": 7}})
    _write_json(theirs, {"_doc": "d", "backend": {"a.py": 300}, "agent": {"x.ts": 5}})
    msl.main([str(ours), str(ours), str(theirs)])
    reloaded = csl.load_limits(str(ours))
    assert reloaded["backend"]["a.py"] == 300
    assert reloaded["agent"]["x.ts"] == 7
    assert os.path.getsize(str(ours)) > 0
