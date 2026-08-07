"""Size-budget and non-destructive-compaction regression tests for the
strategic payload's ``problem_model`` section.

The strategic prompt is ~137 KB and re-sent across ~17 turns per cycle;
``problem_model`` was its largest section (~37-41 KB, ~27.6-30% of the prompt).
``_agent_view_problem_model`` compacts the low-value provenance/detail fields
out of every agent-owned entry to cut that section to ~24 KB on the realistic
fixture shared with :mod:`test_strategic_payload_size` — a ~33% reduction.

This module asserts the SHIPPED contract of that compaction:

1. **Size budget** — the assembled ``problem_model`` section stays under the
   new byte budget, so the reduction cannot silently regress.
2. **Explicit, not silent** — the trim is surfaced via the
   ``_agent_view_compacted`` marker naming exactly what was stripped, and the
   durable model is never mutated.
3. **Recoverable** — every stripped field remains in the durable model (the
   ``./fetch problem-model`` source).
4. **Non-destructive write-back** — an agent echoing the compacted view cannot
   wipe the stripped fields by omission; ``apply_agent_update`` restores them.
5. **Field-list sync** — the view builder and the merge must agree on the
   compacted field set, or the restore would silently miss a field.
"""

import copy
import json
import os

from app.payload_schemas import _serialized_size, pack_under_budget
from app.problem_model import (
    _COMPACT_CAPABILITY_FIELDS,
    _COMPACT_FRICTION_FIELDS,
    _COMPACT_GAP_FIELDS,
    apply_agent_update,
)
from app.strategic_problem_view import (
    _COMPACT_CAPABILITY_FIELDS as VIEW_COMPACT_CAPABILITY_FIELDS,
    _COMPACT_FRICTION_FIELDS as VIEW_COMPACT_FRICTION_FIELDS,
    _COMPACT_GAP_FIELDS as VIEW_COMPACT_GAP_FIELDS,
    _agent_view_problem_model,
)

# New byte budget for the assembled agent-view problem_model section, measured
# from the shared fixture's compacted output (23,496 B) with headroom. A
# regression that re-ships the compacted provenance fields (→ 35,097 B) or the
# revision log (→ 28,451 B) both trip it.
_PROBLEM_MODEL_SECTION_MAX_BYTES = 28_000


def _build_fixture() -> dict:
    """The deterministic mature-project problem_model fixture shared with
    :mod:`test_strategic_payload_size`."""
    import importlib.util

    sibling = os.path.join(os.path.dirname(__file__), "test_strategic_payload_size.py")
    spec = importlib.util.spec_from_file_location("test_strategic_payload_size", sibling)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._build_problem_model_fixture()


def _serialized_bytes(value) -> int:
    """Serialized JSON byte length using the project-standard ``default=str``."""
    return len(json.dumps(value, default=str).encode())


def test_problem_model_section_under_new_byte_budget():
    """The assembled agent-view problem_model section stays under the new byte
    budget — the size-budget regression guard for the v29 cost target."""
    pm = _build_fixture()
    view = _agent_view_problem_model(pm)
    total = _serialized_bytes(view)
    assert total < _PROBLEM_MODEL_SECTION_MAX_BYTES, (
        f"agent-view problem_model shipped {total} bytes — over the "
        f"{_PROBLEM_MODEL_SECTION_MAX_BYTES} budget"
    )


def test_problem_model_reduction_is_material():
    """The compaction is a material cut, not a rounding change: re-adding the
    stripped provenance fields restores a section meaningfully larger than the
    shipped one (the before reading the v29 cost reduction is scored on)."""
    pm = _build_fixture()
    view = _agent_view_problem_model(pm)
    shipped = _serialized_bytes(view)

    # Reconstruct the legacy pre-compaction view from the durable model.
    legacy = copy.deepcopy(view)
    legacy.pop("_agent_view_compacted", None)
    for section, fields in (
        ("capabilities", VIEW_COMPACT_CAPABILITY_FIELDS),
        ("gaps", VIEW_COMPACT_GAP_FIELDS),
        ("friction", VIEW_COMPACT_FRICTION_FIELDS),
    ):
        for item, original in zip(legacy.get(section, []), pm.get(section, [])):
            if isinstance(item, dict) and isinstance(original, dict):
                for field in fields:
                    if field in original:
                        item[field] = copy.deepcopy(original[field])
    legacy_size = _serialized_bytes(legacy)

    assert shipped < legacy_size, (
        f"compaction must shrink the section: shipped {shipped}, legacy {legacy_size}"
    )
    # Material: at least a 20% reduction on the fixture.
    assert shipped < legacy_size * 0.8, (
        f"compaction must be material: shipped {shipped} vs legacy {legacy_size}"
    )


def test_compaction_marker_is_explicit_and_durable_untouched():
    """The trim is loud: the view carries a ``_agent_view_compacted`` marker
    naming exactly which fields were stripped, and the durable model is never
    mutated (all stripped detail remains recoverable via ``./fetch``)."""
    pm = _build_fixture()
    view = _agent_view_problem_model(pm)

    marker = view.get("_agent_view_compacted")
    assert marker is not None, "compacted view must carry the _agent_view_compacted marker"
    assert marker["capabilities"] == ["source", "evidence_refs"]
    assert marker["gaps"] == ["source", "evidence_refs"]
    assert marker["friction"] == ["source", "mitigations_tried"]

    # The shipped entries are actually missing the compacted fields…
    for cap in view["capabilities"]:
        assert "source" not in cap and "evidence_refs" not in cap
    for gap in view["gaps"]:
        assert "source" not in gap and "evidence_refs" not in gap
    for fr in view["friction"]:
        assert "source" not in fr and "mitigations_tried" not in fr

    # …while the durable model keeps every one of them.
    for cap in pm["capabilities"]:
        assert "source" in cap and "evidence_refs" in cap
    for gap in pm["gaps"]:
        assert "source" in gap and "evidence_refs" in gap
    for fr in pm["friction"]:
        assert "source" in fr and "mitigations_tried" in fr


def _assert_source_preserved(merged_source, original_source):
    """Compare two ``source`` dicts allowing the pydantic round-trip to add
    ``None`` defaults for keys the durable model omitted."""
    assert isinstance(merged_source, dict) and isinstance(original_source, dict)
    for key, value in original_source.items():
        assert merged_source.get(key) == value, (
            f"source.{key} not preserved: merged={merged_source} original={original_source}"
        )


def _build_valid_durable() -> dict:
    """A small, schema-valid durable problem_model carrying every compacted
    field, so ``apply_agent_update``'s strict validation accepts it."""
    return {
        "revision": 5,
        "intent": {"purpose": "Blueprint keeps itself ahead of relevant advances"},
        "capabilities": [
            {
                "name": "CapA",
                "status": "working",
                "notes": "works well",
                "source": {"type": "shipped_change", "pr_number": 42},
                "evidence_refs": [
                    {"kind": "named_test", "ref": "testing/test_cap_a.py", "proven": True}
                ],
                "backing_paths": ["backend/app/cap_a.py"],
            }
        ],
        "gaps": [
            {
                "id": "g-1",
                "name": "gap one",
                "description": "the gap",
                "status": "active",
                "priority": "medium",
                "source": {
                    "type": "user_message",
                    "message_id": "msg-1",
                    "excerpt": "from user",
                },
                "evidence_refs": [
                    {"kind": "named_test", "ref": "testing/test_gap_one.py", "proven": True}
                ],
            }
        ],
        "friction": [
            {
                "id": "f-1",
                "name": "flaky ci",
                "description": "flaky ci",
                "status": "active",
                "source": {"type": "self_reflection", "message_id": "msg-2"},
                "mitigations_tried": ["added retry", "increased timeout"],
            }
        ],
    }


def test_write_back_restores_compacted_fields():
    """An agent acting on the compacted view cannot wipe the stripped fields:
    ``apply_agent_update`` restores them from the durable model when the
    proposal omits them (blank means 'never received', not 'clear')."""
    pm = _build_valid_durable()
    view = _agent_view_problem_model(pm)
    # The agent edits the compacted view it saw — e.g. bumping one gap's
    # priority — and submits it back without reconstructing provenance fields.
    proposed = copy.deepcopy(view)
    proposed["gaps"][0]["priority"] = "high"

    new_raw, _diff = apply_agent_update(pm, proposed, "2026-08-07T00:00:00")

    merged_caps = {c["name"]: c for c in new_raw["capabilities"]}
    for cap in pm["capabilities"]:
        merged = merged_caps[cap["name"]]
        _assert_source_preserved(merged["source"], cap["source"])
        assert merged["evidence_refs"] == cap["evidence_refs"]
    merged_gaps = {g["id"]: g for g in new_raw["gaps"]}
    for gap in pm["gaps"]:
        merged = merged_gaps[gap["id"]]
        _assert_source_preserved(merged["source"], gap["source"])
        assert merged["evidence_refs"] == gap["evidence_refs"]
    merged_friction = {f["id"]: f for f in new_raw["friction"]}
    for fr in pm["friction"]:
        merged = merged_friction[fr["id"]]
        _assert_source_preserved(merged["source"], fr["source"])
        assert merged["mitigations_tried"] == fr["mitigations_tried"]

    # The agent's real edit still lands.
    assert new_raw["gaps"][0]["priority"] == "high"
    # Packer markers never persist into the durable model.
    assert "_agent_view_compacted" not in new_raw
    assert "_gap_memory_trimmed" not in new_raw


def test_compacted_field_lists_are_in_sync():
    """The view builder and the write-back merge must agree on the compacted
    field set. If one module strips a field the other does not restore (or
    restores a field the builder never strips), the compaction would silently
    lose durable detail or the marker would lie."""
    assert set(_COMPACT_CAPABILITY_FIELDS) == set(VIEW_COMPACT_CAPABILITY_FIELDS)
    assert set(_COMPACT_GAP_FIELDS) == set(VIEW_COMPACT_GAP_FIELDS)
    assert set(_COMPACT_FRICTION_FIELDS) == set(VIEW_COMPACT_FRICTION_FIELDS)


def test_strategic_payload_total_packs_under_budget_with_compacted_model():
    """A representative strategic payload assembled from the compacted
    problem_model (plus the other bounded sections) packs under the strategic
    byte budget with the model shipped intact."""
    from app.strategic_spawning import _STRATEGIC_REVIEW_SCHEMA

    pm = _build_fixture()
    problem_model = _agent_view_problem_model(pm)
    payload = {
        "type": "strategic_review",
        "research_mode": "strategic",
        "problem_model": problem_model,
        "design_doc": {
            "sections": [
                {"key": "notes/strategic-context", "summary": "Prior-cycle context index entry"},
                {"key": "systems/release-pipeline", "summary": "Architecture section summary"},
            ]
        },
        "effective_purpose": "Blueprint keeps itself ahead of relevant advances with minimal attention",
        "direction_counts": {"active": 4, "max_active": 6},
        "max_active_directions": 6,
        "project_name": "Blueprint",
        "open_message_count": 0,
        "open_messages": [],
        "conversation_thread": [],
    }

    packed = pack_under_budget(payload, _STRATEGIC_REVIEW_SCHEMA)
    packed_bytes = _serialized_size(packed)

    assert packed_bytes < 120 * 1024
    assert "problem_model" in packed
    assert packed["problem_model"] == problem_model
    assert "_agent_view_compacted" in packed["problem_model"]
