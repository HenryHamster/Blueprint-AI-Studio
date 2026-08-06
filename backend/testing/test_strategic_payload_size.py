"""Byte-size measurements for the strategic decision-loop payload.

The v28 stabilisation work measured the strategic decision-loop payload and
found its largest section is ``problem_model`` at 27.6% of the mean prompt
bytes (37,196 bytes of a 134,842-byte prompt). This test builds the
agent-view ``problem_model`` section from a deterministic, realistic fixture
and asserts the STRUCTURAL byte reductions that keep the section bounded —
chiefly that ``revision_log`` (the durable model-write history, which the
schema marks "Not auto-injected into prompts — accessible via tool call") is
no longer shipped inside the problem_model section.

It asserts measured byte sizes, not just structure: total section bytes, the
per-section bytes of the sections the builder ships, and the exact bytes
saved by dropping the revision log. The byte ceilings are sized to the
fixture with headroom; a regression that re-ships the revision log (the
single largest structurally-redundant slice, ~18% of the section) trips the
total-bytes ceiling.
"""

import json

from app.payload_schemas import _serialized_size, pack_under_budget
from app.strategic_problem_view import _agent_view_problem_model

# Byte ceilings for the fixture below, measured from the fixture's serialized
# agent-view output with headroom. Total is deliberately tight: re-shipping
# the ~4.9 KB revision_log pushes the total over it.
_PROBLEM_MODEL_VIEW_MAX_BYTES = 39_000
_PER_SECTION_MAX_BYTES = {
    "gaps": 18_000,
    "capabilities": 15_000,
    "friction": 5_000,
    "intent": 2_000,
}


def _serialized_bytes(value) -> int:
    """Serialized JSON byte length using the project-standard ``default=str``
    (matches ``_serialized_size`` / the packer's measurement)."""
    return len(json.dumps(value, default=str).encode())


def _build_problem_model_fixture() -> dict:
    """A realistic mature-project problem_model fixture.

    Deterministic (no wall-clock / randomness): a populated intent, context,
    constraints, ~22 capabilities, ~9 active gaps with append-only history /
    past_moves / evidence, ~4 friction entries, pending confirmations, and a
    full revision_log — sized to the production problem_model scale (~37 KB
    agent-view) so the byte assertions are meaningful.
    """
    intent = {
        "purpose": "Blueprint exists to keep itself genuinely ahead of relevant AI and "
        "software-engineering advances and to move Henry toward the most effective "
        "frontier of research and building, turning goals into important verified "
        "user-visible outcomes with minimal attention, elapsed time, money, and "
        "maintenance burden; autonomous activity is not an outcome.",
        "current_focus": "v28 STABILIZE - reduce the strategist decision-loop cost per "
        "unit of good decision, recover prompt-cache read share, and keep the "
        "cheapened loop from missing course-changing findings.",
        "user_purpose": "Keep Blueprint genuinely ahead of relevant AI and software "
        "advances with minimal operator attention and money.",
        "source": {
            "type": "user_message",
            "message_id": "msg-op-001",
            "excerpt": "I want Blueprint to stay ahead of relevant advances with minimal attention",
        },
    }
    context = {
        "project_type": "self-improving software-engineering product",
        "users": "Henry first; designed to generalize to other repos",
        "team": "solo human operator plus autonomous AI agents",
        "deploy_cadence": "continuous when qualified",
        "notes": "Blueprint has two coupled loops: the outer frontier loop and the inner RSI loop.",
    }
    constraints = [
        "Do NOT modify files in .github/workflows/",
        "Do NOT modify existing migration files in backend/alembic/versions/",
        "Optimize for important verified progress per Henry attention",
        "Use bounded parallelism with logical-work deduplication",
        "Keep routine research on the cheapest model tier that performs them well",
        "Do not leave new behavior in shadow mode",
    ]

    cap_names = [
        "conversational-workday-path",
        "tool-to-conversation-handoff",
        "strategic-cycle-effort-classifier",
        "prompt-cache-read-recovery",
        "strategic-payload-byte-budget-packer",
        "model-tier-selection",
        "release-qualifier",
        "ci-compliance-guardrail",
        "migration-chain-apply-gate",
        "strategic-cycle-cost-projection",
        "fleet-sizing-instruments",
        "measured-effort-tier-split",
        "direction-outcome-scoring",
        "gap-reconciliation",
        "design-doc-index",
        "operator-context-refs",
        "self-reflection-log",
        "retirement-monitor",
        "sweep-ownership-contract",
        "effort-tier-recording",
        "cache-boundary-stable-prefix",
        "resident-coherence",
    ]
    capabilities = []
    for i, name in enumerate(cap_names):
        capabilities.append(
            {
                "name": name,
                "status": "working" if i % 3 else "partial",
                "notes": f"Backs the {name.replace('-', ' ')} flow end-to-end; verified by targeted tests.",
                "source": {
                    "type": "shipped_change",
                    "pr_number": 16800 + i,
                    "excerpt": f"PR #{16800 + i} shipped the {name} flow",
                },
                "backing_paths": [
                    f"backend/app/{name}.py",
                    f"backend/testing/test_{name}.py",
                ],
                "evidence_refs": [
                    {
                        "kind": "named_test",
                        "ref": f"backend/testing/test_{name}.py",
                        "proven": True,
                    },
                    {"kind": "pr", "ref": str(16800 + i), "proven": True},
                ],
            }
        )

    gap_topics = [
        ("strategist-loop-cost", "strategic decision loop cost is $4.51/cycle, above the $4.41 baseline", "high"),
        ("cache-read-share", "prompt cache read share needs to reach 0.80 on the highest-volume task type", "high"),
        ("problem-model-bloat", "problem_model is 27.6% of the strategic prompt; per-gap history repeats prior cycles", "high"),
        ("tier-selection-does-not-move-cost", "six cycles of tier-selection work did not move decision-loop cost", "medium"),
        ("consequential-effort-max", "consequential strategic_review cycles may inherit a research-effort fallback below max", "medium"),
        ("routine-verbosity", "routine strategic cycles may get terse verbosity on prose deliverables", "medium"),
        ("pip-cache-plumbing", "shared pip/uv package store plumbing never reaches pip/uv in the provider CLI env", "medium"),
        ("operator-reply-recovery", "project-message reply recovery needs to survive poll churn", "medium"),
        ("archived-gap-retention", "archived gaps retained for UI reference but not shipped to the agent", "low"),
    ]
    gaps = []
    for gi, (gid, desc, prio) in enumerate(gap_topics):
        history = [
            {
                "ts": f"2026-06-{10 + hi % 20}T00:{hi:02d}:00Z",
                "event": ["opened", "priority_raised", "move_attached", "refined", "compacted"][hi % 5],
                "note": f"Cycle {hi}: observed {desc[:40]}; took action; outcome recorded in direction stats",
            }
            for hi in range(20)
        ]
        gaps.append(
            {
                "id": f"g-{gid}",
                "name": f"Close the {gid} gap",
                "description": desc,
                "evidence": (
                    f"Measured over {20 + gi} cycles; the gap persists because tier "
                    "selection does not address prompt bytes; see the efficiency snapshot."
                ),
                "evidence_refs": [
                    {"kind": "pr", "ref": str(16800 + gi), "proven": True},
                    {"kind": "named_test", "ref": "backend/testing/test_strategic_payload_size.py", "proven": True},
                ],
                "size": "medium",
                "priority": prio,
                "status": "active",
                "opened": f"2026-05-{10 + gi}T00:00:00Z",
                "tag": "self_improvement" if gi < 5 else None,
                "source": {
                    "type": "agent_observation",
                    "message_id": f"msg-gap-{gi}",
                    "excerpt": desc[:100],
                },
                "active_moves": [f"d-active-{gi}"],
                "past_moves": [f"d-prev-{gi}-{i}" for i in range(12)],
                "history": history,
            }
        )

    friction = []
    for fi, (fid, fname) in enumerate(
        [
            ("f-cache-churn", "Payload byte layout shifts break the prompt cache prefix"),
            ("f-context-window", "Strategic prompt size approaches context limits"),
            ("f-slow-cycle", "Full strategic cycles are slow and expensive"),
            ("f-tier-hysteresis", "Model tier selection does not reflect measured cost"),
        ]
    ):
        friction.append(
            {
                "id": fid,
                "name": fname,
                "description": f"Recurring blocker: {fname.lower()} observed across cycles; degrades decision-loop economics.",
                "seen_in": [f"d-active-{fi}", f"d-prev-{fi}-0", f"d-prev-{fi}-1"],
                "status": "active" if fi < 3 else "mitigated",
                "mitigations_tried": [f"Attempt {mi}: {fname.lower()} mitigation" for mi in range(6)],
                "source": {
                    "type": "self_reflection",
                    "message_id": f"msg-friction-{fi}",
                    "excerpt": f"Noticed {fname} again",
                },
            }
        )

    pending = [
        {
            "id": "pc-1",
            "type": "intent_change",
            "proposal": "Shift focus to payload-byte reduction",
            "rationale": "Tier selection is not the binding constraint",
            "created_at": "2026-07-20T00:00:00Z",
        },
        {
            "id": "pc-2",
            "type": "major_decision",
            "proposal": "Settle the cache-boundary architecture",
            "rationale": "Needed before implementing cache prefix changes",
            "created_at": "2026-07-21T00:00:00Z",
        },
    ]

    revision_log = [
        {
            "revision": r + 1,
            "ts": f"2026-06-{5 + r}T12:00:00Z",
            "summary": (
                f"Cycle {r + 1}: opened {2 + r % 3} gaps, closed {r % 2} gaps, "
                f"updated {5 + r} capabilities, refined strategic intent"
            ),
            "changes": {
                "gaps_opened": [t[0] for t in gap_topics[: 2 + r % 3]],
                "gaps_closed": [],
                "capabilities_updated": cap_names[: 5 + r % 4],
            },
        }
        for r in range(10)
    ]

    return {
        "revision": 28,
        "last_updated": "2026-08-05T00:00:00Z",
        "last_updated_by": "agent",
        "intent": intent,
        "context": context,
        "constraints": constraints,
        "capabilities": capabilities,
        "gaps": gaps,
        "friction": friction,
        "pending_user_confirmation": pending,
        "revision_log": revision_log,
        "archived_gaps": [
            {
                "id": f"g-arch-{i}",
                "name": f"Archived gap {i}",
                "description": "closed long ago",
                "status": "closed",
            }
            for i in range(25)
        ],
    }


def test_problem_model_view_drops_revision_log():
    """The revision log — durable model-write history marked "not auto-injected
    into prompts" — is structurally dropped from the agent-view problem_model,
    and the durable fixture is never mutated."""
    pm = _build_problem_model_fixture()
    assert "revision_log" in pm  # the durable model really carries one

    view = _agent_view_problem_model(pm)

    assert "revision_log" not in view, (
        "revision_log is not shipped in the agent-view problem_model: its schema "
        "marks it 'Not auto-injected into prompts', no strategic-agent code reads "
        "it, and the write-back merge preserves it unconditionally."
    )
    # The durable input is never mutated.
    assert "revision_log" in pm


def test_problem_model_view_byte_reduction_measured():
    """The structural cut is measurable: shipping the revision log would make
    the section materially larger, and the shipped section is strictly smaller
    than the legacy view."""
    pm = _build_problem_model_fixture()
    view = _agent_view_problem_model(pm)

    legacy_view = dict(view)
    legacy_view["revision_log"] = pm["revision_log"]

    shipped = _serialized_bytes(view)
    legacy = _serialized_bytes(legacy_view)

    assert shipped < legacy, (
        "the agent-view problem_model must be strictly smaller than the legacy "
        "view that shipped revision_log"
    )
    # The saved bytes are material — the revision log was the single largest
    # structurally-redundant slice, >12% of the section on this fixture (the
    # measured production share). A sub-threshold cut would not be a real
    # reduction.
    saved = legacy - shipped
    assert saved > shipped // 8


def test_problem_model_section_bytes_under_ceiling():
    """Total and per-section bytes of the shipped problem_model section stay
    under measured ceilings. Re-shipping the revision log would push the total
    over its ceiling (the log is ~7.8 KB on this fixture, the total ceiling is
    only ~3.7 KB above the shipped total)."""
    pm = _build_problem_model_fixture()
    view = _agent_view_problem_model(pm)

    total = _serialized_bytes(view)
    assert total < _PROBLEM_MODEL_VIEW_MAX_BYTES, (
        f"agent-view problem_model shipped {total} bytes — over the "
        f"{_PROBLEM_MODEL_VIEW_MAX_BYTES} ceiling"
    )

    for section, ceiling in _PER_SECTION_MAX_BYTES.items():
        section_bytes = _serialized_bytes(view.get(section, {}))
        assert section_bytes < ceiling, (
            f"problem_model.{section} shipped {section_bytes} bytes — over the "
            f"{ceiling} ceiling"
        )

    # A revision-log regression is caught by the total ceiling.
    legacy_total = total + _serialized_bytes(pm["revision_log"])
    assert legacy_total >= _PROBLEM_MODEL_VIEW_MAX_BYTES, (
        "test invariant: the fixture's revision_log must be large enough that "
        "re-shipping it trips the total-bytes ceiling"
    )


def test_strategic_payload_total_bytes_under_budget():
    """A representative strategic payload assembled from fixtures (the
    agent-view problem_model section plus the other bounded sections) packs
    under the strategic byte budget with the problem_model shipped intact."""
    from app.strategic_spawning import _STRATEGIC_REVIEW_SCHEMA

    pm = _build_problem_model_fixture()
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
        "vision": {"_strategic_cycle_history": []},
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

    # The whole payload stays under the strategic packer budget.
    assert packed_bytes < 120 * 1024
    # The problem_model section ships intact (not summarized/dropped by the packer).
    assert "problem_model" in packed
    assert packed["problem_model"] == problem_model
    assert "revision_log" not in packed["problem_model"]
