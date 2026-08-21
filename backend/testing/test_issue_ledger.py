"""Tests for the single authoritative review-blocker ledger."""

from __future__ import annotations

import hashlib

from app.context_bounds import REVIEW_HISTORY_MAX_ENTRIES
from app.issue_ledger import (
    _jaccard,
    _region_anchor,
    _region_key,
    build_issue_ledger,
    ensure_issue_ids,
    issue_ledger_complete_for_history,
    issue_ledger_lineage_proof_unsatisfiable,
)


def _issue(
    description: str,
    *,
    issue_id: str | None = None,
    severity: str = "MAJOR",
    file: str | None = "app.py",
    line: int | None = 41,
) -> dict:
    issue: dict = {"severity": severity, "description": description}
    if issue_id is not None:
        issue["issue_id"] = issue_id
    if file is not None:
        issue["file"] = file
    if line is not None:
        issue["line"] = line
    return issue


def _review(
    iteration: int,
    head: str,
    issues: list[dict] | None = None,
    *,
    verdict: str = "REQUEST_CHANGES",
    resolutions: list[dict] | None = None,
    base: str = "base-1",
    verified: bool = True,
) -> dict:
    entry = {
        "iteration": iteration,
        "verdict": verdict,
        "reviewed_head_sha": head,
        "reviewed_base_sha": base,
        "issues": issues or [],
    }
    if verified and head and base:
        entry["target_verified_by_backend"] = True
        entry["review_task_id"] = f"review-{iteration}"
    if resolutions is not None:
        entry["resolutions"] = resolutions
    return entry


def _backend_id(issue: dict, *, head: str, iteration: int, ordinal: int = 0) -> str:
    without_supplied_id = {k: v for k, v in issue.items() if k != "issue_id"}
    padded = [{} for _ in range(ordinal)] + [without_supplied_id]
    return ensure_issue_ids(
        padded,
        reviewed_head_sha=head,
        iteration=iteration,
    )[-1]["issue_id"]


def test_jaccard_and_region_helpers_are_conservative() -> None:
    assert _jaccard("missing null guard", "null guard is missing") == 1.0
    assert _jaccard("missing null guard", "unrelated race condition") == 0.0
    assert _region_anchor({"symbol": "handle", "line": 999}) == "sym:handle"
    assert _region_anchor({"line": 79}) == "win:1"
    assert _region_key({"file": "app.py", "line": 80}) == ("app.py", "win:2")
    assert _region_key({"description": "global invariant"}) is None


def test_issue_ids_use_stable_sha256_prefix_and_preserve_for_propagation() -> None:
    issue = _issue("null dereference in handler")
    normalized = ensure_issue_ids([issue], reviewed_head_sha="head-1", iteration=1)
    seed = "head-1|1|0|app.py|win:1|MAJOR|dereference handler null"
    expected = f"BP-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:12]}"
    assert normalized[0]["issue_id"] == expected
    assert issue.get("issue_id") is None

    explicit = ensure_issue_ids(
        [_issue("anything", issue_id="BP-existing")],
        reviewed_head_sha="head-2",
        iteration=2,
    )
    assert explicit[0]["issue_id"] == "BP-existing"


def test_empty_history_has_empty_authoritative_state() -> None:
    assert build_issue_ledger([]) == {"open": [], "resolved": []}
    assert build_issue_ledger(None) == {"open": [], "resolved": []}


def test_same_head_approve_cannot_close_prior_blocker() -> None:
    ledger = build_issue_ledger(
        [
            _review(1, "head-1", [_issue("null dereference", issue_id="BP-null")]),
            _review(2, "head-1", verdict="APPROVE"),
        ]
    )
    assert [row["description"] for row in ledger["open"]] == ["null dereference"]
    assert ledger["resolved"] == []


def test_approve_with_new_blocking_issue_keeps_it_open() -> None:
    ledger = build_issue_ledger(
        [
            _review(
                1,
                "head-1",
                [_issue("reviewer-listed blocker", issue_id="BP-new")],
                verdict="APPROVE",
            )
        ]
    )
    assert [row["description"] for row in ledger["open"]] == [
        "reviewer-listed blocker"
    ]


def test_changed_head_approve_closes_omitted_prior_but_opens_listed_blocker() -> None:
    old = _issue("prior blocker", issue_id="BP-old")
    new = _issue("new blocker", issue_id="BP-new", file="worker.py")
    old_id = _backend_id(old, head="head-1", iteration=1)
    new_id = _backend_id(new, head="head-2", iteration=2)
    ledger = build_issue_ledger(
        [
            _review(1, "head-1", [old]),
            _review(
                2,
                "head-2",
                [new],
                verdict="APPROVE",
            ),
        ]
    )
    assert [row["issue_id"] for row in ledger["open"]] == [new_id]
    assert [row["issue_id"] for row in ledger["resolved"]] == [old_id]


def test_same_head_request_changes_omission_keeps_old_and_adds_new() -> None:
    ledger = build_issue_ledger(
        [
            _review(1, "head-1", [_issue("null dereference", issue_id="BP-a")]),
            _review(
                2,
                "head-1",
                [_issue("lost update race", issue_id="BP-b", file="worker.py")],
            ),
        ]
    )
    assert [row["description"] for row in ledger["open"]] == [
        "null dereference",
        "lost update race",
    ]
    assert ledger["resolved"] == []


def test_changed_exact_head_omission_closes_fixed_blocker() -> None:
    blocker = _issue("null dereference", issue_id="BP-a")
    blocker_id = _backend_id(blocker, head="head-1", iteration=1)
    ledger = build_issue_ledger(
        [
            _review(1, "head-1", [blocker]),
            _review(2, "head-2", verdict="APPROVE"),
        ]
    )
    assert ledger["open"] == []
    assert ledger["resolved"] == [
        {
            "issue_id": blocker_id,
            "disposition": "fixed",
            "reason": "absent from review of a changed exact PR head",
            "resolved_iteration": 2,
            "resolved_head_sha": "head-2",
        }
    ]


def test_explicit_id_reraise_preserves_original_demand_fields() -> None:
    original = _issue("missing null guard before handler dereference")
    original_id = _backend_id(original, head="head-1", iteration=1)
    ledger = build_issue_ledger(
        [
            _review(
                1,
                "head-1",
                [
                        original
                ],
            ),
            _review(
                2,
                "head-2",
                [
                    _issue(
                            "handler has a missing null guard before dereference",
                            issue_id=original_id,
                    )
                ],
            ),
        ]
    )
    assert [row["issue_id"] for row in ledger["open"]] == [original_id]
    assert (
        ledger["open"][0]["description"]
        == "missing null guard before handler dereference"
    )
    assert ledger["resolved"] == []


def test_wrong_reused_id_cannot_erase_unrelated_open_blocker() -> None:
    original = _issue("missing null guard")
    original_id = _backend_id(original, head="head-1", iteration=1)
    ledger = build_issue_ledger(
        [
            _review(
                1,
                "head-1",
                [original],
            ),
            _review(
                2,
                "head-1",
                [
                    _issue(
                        "lost update race in worker queue",
                        issue_id=original_id,
                        file="worker.py",
                    )
                ],
            ),
        ]
    )
    assert len(ledger["open"]) == 2
    assert ledger["open"][0]["issue_id"] == original_id
    assert ledger["open"][0]["description"] == "missing null guard"
    assert ledger["open"][1]["issue_id"].startswith("BP-")
    assert ledger["open"][1]["issue_id"] != original_id
    assert ledger["open"][1]["description"] == "lost update race in worker queue"


def test_duplicate_id_for_distinct_current_findings_is_split_fail_closed() -> None:
    ledger = build_issue_ledger(
        [
            _review(
                1,
                "head-1",
                [
                    _issue("missing null guard", issue_id="BP-a"),
                    _issue(
                        "lost update race in worker queue",
                        issue_id="BP-a",
                        file="worker.py",
                    ),
                ],
            )
        ]
    )
    assert len(ledger["open"]) == 2
    assert len({row["issue_id"] for row in ledger["open"]}) == 2
    assert {row["description"] for row in ledger["open"]} == {
        "missing null guard",
        "lost update race in worker queue",
    }


def test_changed_head_closes_old_blocker_while_new_blocker_remains() -> None:
    ledger = build_issue_ledger(
        [
            _review(1, "head-1", [_issue("null dereference", issue_id="BP-a")]),
            _review(
                2,
                "head-2",
                [_issue("lost update race", issue_id="BP-b", file="worker.py")],
            ),
        ]
    )
    assert [row["description"] for row in ledger["open"]] == ["lost update race"]
    assert len(ledger["resolved"]) == 1


def test_same_head_explicit_refuted_or_adjudicated_resolution_closes_with_reason() -> None:
    blocker = _issue("null dereference")
    blocker_id = _backend_id(blocker, head="head-1", iteration=1)
    for disposition in ("refuted", "adjudicated"):
        ledger = build_issue_ledger(
            [
                    _review(1, "head-1", [blocker]),
                _review(
                    2,
                    "head-1",
                    verdict="APPROVE",
                    resolutions=[
                        {
                            "issue_id": blocker_id,
                            "disposition": disposition,
                            "reason": "concrete evidence",
                        }
                    ],
                ),
            ]
        )
        assert ledger["open"] == []
        assert ledger["resolved"][0]["disposition"] == disposition


def test_same_head_fixed_or_reasonless_refutation_cannot_close() -> None:
    blocker = _issue("null dereference")
    blocker_id = _backend_id(blocker, head="head-1", iteration=1)
    for resolution in (
        {"issue_id": blocker_id, "disposition": "fixed", "reason": "claimed"},
        {"issue_id": blocker_id, "disposition": "refuted", "reason": ""},
    ):
        ledger = build_issue_ledger(
            [
                _review(1, "head-1", [blocker]),
                _review(
                    2,
                    "head-1",
                    verdict="APPROVE",
                    resolutions=[resolution],
                ),
            ]
        )
        assert [row["issue_id"] for row in ledger["open"]] == [blocker_id]
        assert ledger["resolved"] == []


def test_missing_sha_provenance_never_proves_omission_fixed() -> None:
    ledger = build_issue_ledger(
        [
            _review(1, "", [_issue("null dereference", issue_id="BP-a")]),
            _review(2, "", verdict="APPROVE"),
        ]
    )
    assert [row["description"] for row in ledger["open"]] == ["null dereference"]


def test_unmarked_invented_full_target_cannot_close_verified_blocker() -> None:
    blocker = _issue("null dereference")
    ledger = build_issue_ledger(
        [
            _review(1, "head-1", [blocker]),
            _review(2, "invented-head", verdict="APPROVE", verified=False),
            _review(3, "head-1", verdict="APPROVE"),
        ]
    )
    assert [row["description"] for row in ledger["open"]] == ["null dereference"]
    assert ledger["resolved"] == []


def test_unmarked_reasoned_resolution_cannot_close_verified_blocker() -> None:
    """Root-fix regression (NeurIPS PR #1717 / plan task 3344347d).

    An unverified-target disposition must never CLOSE a blocker — that
    fail-closed invariant is unchanged. But it must not be silently dropped
    either: the ledger now records a non-authoritative ``pending_disposition``
    annotation so downstream consumers (the stuck-cycle detector) can tell a
    good-faith fix-forward disposition apart from a bare unaddressed repeat.
    """
    blocker = _issue("null dereference")
    blocker_id = _backend_id(blocker, head="head-1", iteration=1)
    ledger = build_issue_ledger(
        [
            _review(1, "head-1", [blocker]),
            _review(
                2,
                "head-1",
                verdict="APPROVE",
                verified=False,
                resolutions=[
                    {
                        "issue_id": blocker_id,
                        "disposition": "refuted",
                        "reason": "invented legacy adjudication",
                    }
                ],
            ),
        ]
    )
    assert [row["issue_id"] for row in ledger["open"]] == [blocker_id]
    assert ledger["resolved"] == []
    pending = ledger["open"][0]["pending_disposition"]
    assert pending["disposition"] == "refuted"
    assert pending["reason"] == "invented legacy adjudication"
    assert pending["iteration"] == 2
    assert pending["head_sha"] == "head-1"


def test_pending_disposition_cleared_when_issue_is_re_raised() -> None:
    """A stale pending_disposition claim must not survive a genuine re-raise:
    once a later review flags the same issue again, it is unambiguously still
    a live blocker and the earlier good-faith disposition claim is moot."""
    blocker = _issue("null dereference")
    blocker_id = _backend_id(blocker, head="head-1", iteration=1)
    ledger = build_issue_ledger(
        [
            _review(1, "head-1", [blocker]),
            _review(
                2,
                "head-1",
                verdict="APPROVE",
                verified=False,
                resolutions=[
                    {
                        "issue_id": blocker_id,
                        "disposition": "fixed",
                        "reason": "believed landed",
                    }
                ],
            ),
            _review(3, "head-2", [blocker]),
        ]
    )
    assert [row["issue_id"] for row in ledger["open"]] == [blocker_id]
    assert "pending_disposition" not in ledger["open"][0]


def test_verified_reasoned_resolution_can_reconcile_legacy_unmarked_blocker() -> None:
    blocker = _issue("legacy null dereference")
    blocker_id = _backend_id(blocker, head="legacy-head", iteration=1)
    for disposition in ("fixed", "refuted", "adjudicated"):
        ledger = build_issue_ledger(
            [
                _review(1, "legacy-head", [blocker], verified=False),
                _review(
                    2,
                    "current-head",
                    verdict="APPROVE",
                    resolutions=[
                        {
                            "issue_id": blocker_id,
                            "disposition": disposition,
                            "reason": "exact review supplied concrete reconciliation",
                        }
                    ],
                ),
            ]
        )
        assert ledger["open"] == []
        assert ledger["resolved"][0]["disposition"] == disposition


def test_fileless_global_blocker_is_authoritative() -> None:
    ledger = build_issue_ledger(
        [
            _review(
                1,
                "head-1",
                [_issue("global transaction invariant is broken", file=None, line=None)],
            )
        ]
    )
    assert len(ledger["open"]) == 1
    assert ledger["open"][0]["description"] == "global transaction invariant is broken"
    assert "file" not in ledger["open"][0]


def test_same_region_paraphrase_carries_original_identity() -> None:
    original = _issue("missing null guard before handler dereference")
    original_id = _backend_id(original, head="head-1", iteration=1)
    ledger = build_issue_ledger(
        [
            _review(
                1,
                "head-1",
                [original],
            ),
            _review(
                2,
                "head-1",
                [_issue("handler has missing null guard before dereference")],
            ),
        ]
    )
    assert len(ledger["open"]) == 1
    assert ledger["open"][0]["issue_id"] == original_id


def test_open_blockers_are_never_truncated() -> None:
    blockers = [
        _issue(
            f"unique blocker number {index}",
            issue_id=f"BP-{index:02d}",
            file=f"module_{index}.py",
        )
        for index in range(35)
    ]
    ledger = build_issue_ledger(
        [
            _review(1, "head-1", blockers),
            _review(2, "head-1", verdict="APPROVE"),
        ]
    )
    assert len(ledger["open"]) == 35
    assert len({row["issue_id"] for row in ledger["open"]}) == 35
    assert ledger["open"][30]["description"] == "unique blocker number 30"
    assert ledger["open"][-1]["description"] == "unique blocker number 34"


def test_nonblocking_and_malformed_rows_are_ignored_safely() -> None:
    ledger = build_issue_ledger(
        [
            None,
            "bad",
            {
                "verdict": "PENDING",
                "issues": [_issue("nonblocking note", severity="MINOR")],
            },
            {
                "iteration": "not-an-int",
                "verdict": "REQUEST_CHANGES",
                "reviewed_head_sha": "head-1",
                "issues": [None, "bad", _issue("minor note", severity="MINOR")],
                "resolutions": [None, "bad"],
            },
        ]
    )
    assert ledger == {"open": [], "resolved": []}


def test_structured_blockers_survive_unrecognized_or_missing_verdicts() -> None:
    ledger = build_issue_ledger(
        [
            {
                "iteration": 1,
                "verdict": "COMMENT",
                "reviewed_head_sha": "head-1",
                "issues": [_issue("comment-listed major blocker")],
            },
            {
                "iteration": 2,
                "verdict": "NOT_A_VERDICT",
                "reviewed_head_sha": "head-1",
                "issues": [
                    _issue("malformed-verdict critical blocker", severity="CRITICAL")
                ],
            },
            {
                "iteration": 3,
                "reviewed_head_sha": "head-1",
                "issues": [_issue("missing-verdict major blocker")],
            },
        ]
    )

    assert {row["description"] for row in ledger["open"]} == {
        "comment-listed major blocker",
        "malformed-verdict critical blocker",
        "missing-verdict major blocker",
    }
    assert ledger["resolved"] == []


def test_invalid_verdict_cannot_resolve_or_omit_existing_blocker() -> None:
    original = _issue("binding blocker", issue_id="BP-binding")
    ledger = build_issue_ledger(
        [
            _review(1, "head-1", [original]),
            {
                "iteration": 2,
                "verdict": "COMMENT",
                "reviewed_head_sha": "head-2",
                "issues": [],
                "resolutions": [
                    {
                        "issue_id": "BP-binding",
                        "disposition": "fixed",
                        "reason": "unsupported verdict cannot prove this",
                    }
                ],
            },
        ]
    )

    assert [row["description"] for row in ledger["open"]] == ["binding blocker"]
    assert ledger["resolved"] == []


def test_durable_seed_survives_many_sequential_history_compactions() -> None:
    history: list[dict] = []
    ledger: dict | None = None
    for iteration in range(1, REVIEW_HISTORY_MAX_ENTRIES + 26):
        history = (
            history
            + [_review(iteration, f"head-{iteration}", verdict="APPROVE")]
        )[-REVIEW_HISTORY_MAX_ENTRIES:]
        ledger = build_issue_ledger(
            history,
            **({"seed_ledger": ledger} if ledger is not None else {}),
            stamp_complete=True,
        )
        assert issue_ledger_complete_for_history(ledger, history) is True


def test_markerless_legacy_seed_ingests_new_blocker_and_regains_lineage() -> None:
    legacy_seed = {
        "open": [
            {
                "issue_id": "BP-old",
                "description": "legacy blocker",
                "severity": "MAJOR",
                "file": "legacy.py",
                "line": 10,
            }
        ],
        "resolved": [],
    }
    current = _review(
        8,
        "head-current",
        [_issue("new current blocker", issue_id="BP-new", file="current.py")],
    )

    rebuilt = build_issue_ledger(
        [current], seed_ledger=legacy_seed, stamp_complete=True
    )

    assert {row["description"] for row in rebuilt["open"]} == {
        "legacy blocker",
        "new current blocker",
    }
    assert rebuilt["complete"] is True
    assert rebuilt.get("through_entry_key")

    approval = _review(
        9,
        "head-final",
        verdict="APPROVE",
        resolutions=[
            {
                "issue_id": "BP-old",
                "disposition": "adjudicated",
                "reason": "operator verified the legacy report is obsolete",
            }
        ],
    )
    converged = build_issue_ledger(
        [current, approval], seed_ledger=rebuilt, stamp_complete=True
    )

    assert converged["open"] == []
    assert {row["issue_id"] for row in converged["resolved"]} == {
        "BP-old",
        _backend_id(
            _issue("new current blocker", file="current.py"),
            head="head-current",
            iteration=8,
        ),
    }
    assert issue_ledger_complete_for_history(converged, [current, approval]) is True


def test_discontinuous_capped_history_cannot_extend_stale_seed_proof() -> None:
    first = _review(
        5,
        "head-5",
        issues=[
            {
                "description": "future-seed blocker",
                "severity": "MAJOR",
                "file": "scanner.py",
                "line": 50,
            }
        ],
    )
    seed = build_issue_ledger([first], stamp_complete=True)
    seed_marker = seed["through_entry_key"]
    discontinuous_history = [
        _review(iteration, f"head-{iteration}", verdict="APPROVE")
        for iteration in range(1, 1 + REVIEW_HISTORY_MAX_ENTRIES)
    ]

    rebuilt = build_issue_ledger(
        discontinuous_history,
        seed_ledger=seed,
        stamp_complete=True,
    )

    assert rebuilt["complete"] is False
    assert [row["description"] for row in rebuilt["open"]] == [
        "future-seed blocker"
    ], "older retained approvals must not resolve a finding from a future seed"
    assert rebuilt["resolved"] == []
    assert rebuilt["through_entry_key"] == seed_marker, (
        "a disconnected history window must not advance the durable lineage marker"
    )
    assert issue_ledger_complete_for_history(
        rebuilt, discontinuous_history
    ) is False


def _grow_capped_history(
    ledger: dict | None,
    history: list[dict],
    iteration: int,
    *,
    drop_seed: bool = False,
) -> tuple[dict, list[dict]]:
    """Append one clean APPROVE round exactly as the review loop does."""
    history = [
        *history,
        _review(iteration, f"head-{iteration}", verdict="APPROVE"),
    ][-REVIEW_HISTORY_MAX_ENTRIES:]
    seed = None if drop_seed else ledger
    return (
        build_issue_ledger(
            history,
            **({"seed_ledger": seed} if seed is not None else {}),
            stamp_complete=True,
        ),
        history,
    )


def test_lineage_proof_lost_at_the_history_cap_never_recovers() -> None:
    """A single seed loss at the cap latches the ledger unprovable forever.

    This is the shape behind NeurIPS PR #1769: once ``complete=False`` is
    stamped alongside a lineage marker while the history sits at the cap,
    neither branch of ``build_issue_ledger``'s lineage test can ever be
    satisfied again -- the seed cannot become complete, the marker cannot be
    dropped, and history only grows.  The merge gate then re-raises
    ``BP-ledger-proof-incomplete`` on every pass, so the PR is unmergeable no
    matter how many clean reviews follow.
    """
    ledger: dict | None = None
    history: list[dict] = []
    loss_iteration = REVIEW_HISTORY_MAX_ENTRIES + 2

    for iteration in range(1, loss_iteration):
        ledger, history = _grow_capped_history(ledger, history, iteration)
        assert issue_ledger_complete_for_history(ledger, history) is True
        assert issue_ledger_lineage_proof_unsatisfiable(ledger, history) is False

    ledger, history = _grow_capped_history(
        ledger, history, loss_iteration, drop_seed=True
    )

    assert issue_ledger_complete_for_history(ledger, history) is False
    assert issue_ledger_lineage_proof_unsatisfiable(ledger, history) is True

    # Ten further perfectly clean rounds, every one seeded from the durable
    # ledger. None of them can restore the proof.
    for iteration in range(loss_iteration + 1, loss_iteration + 11):
        ledger, history = _grow_capped_history(ledger, history, iteration)
        assert issue_ledger_complete_for_history(ledger, history) is False, (
            f"iteration {iteration} recovered; the latch is no longer permanent "
            "and this test's premise needs revisiting"
        )
        assert issue_ledger_lineage_proof_unsatisfiable(ledger, history) is True


def test_lineage_proof_lost_below_the_cap_self_heals_and_is_not_terminal() -> None:
    """The same seed loss under the cap recovers, so it must not escalate."""
    ledger: dict | None = None
    history: list[dict] = []
    loss_iteration = REVIEW_HISTORY_MAX_ENTRIES // 2

    for iteration in range(1, loss_iteration):
        ledger, history = _grow_capped_history(ledger, history, iteration)

    ledger, history = _grow_capped_history(
        ledger, history, loss_iteration, drop_seed=True
    )

    assert issue_ledger_complete_for_history(ledger, history) is True
    assert issue_ledger_lineage_proof_unsatisfiable(ledger, history) is False

    for iteration in range(loss_iteration + 1, REVIEW_HISTORY_MAX_ENTRIES + 6):
        ledger, history = _grow_capped_history(ledger, history, iteration)
        assert issue_ledger_complete_for_history(ledger, history) is True
        assert issue_ledger_lineage_proof_unsatisfiable(ledger, history) is False


def test_lineage_unsatisfiable_requires_marker_incompleteness_and_the_cap() -> None:
    """Each of the three conditions is load-bearing; none alone is terminal."""
    capped = [
        _review(iteration, f"head-{iteration}", verdict="APPROVE")
        for iteration in range(1, 1 + REVIEW_HISTORY_MAX_ENTRIES)
    ]
    latched = {
        "open": [],
        "resolved": [],
        "schema_version": 1,
        "complete": False,
        "through_entry_key": "abc123",
    }

    assert issue_ledger_lineage_proof_unsatisfiable(latched, capped) is True

    # Under the cap the next build re-derives the whole retained window.
    assert (
        issue_ledger_lineage_proof_unsatisfiable(latched, capped[:-1]) is False
    )
    # A markerless seed is rebuilt conservatively instead of latching.
    assert (
        issue_ledger_lineage_proof_unsatisfiable(
            {**latched, "through_entry_key": ""}, capped
        )
        is False
    )
    # A complete ledger is not a stuck one.
    assert (
        issue_ledger_lineage_proof_unsatisfiable(
            {**latched, "complete": True}, capped
        )
        is False
    )
    # A malformed ledger is somebody else's fail-closed problem, not a latch.
    assert issue_ledger_lineage_proof_unsatisfiable("not-a-ledger", capped) is False


def test_first_ledger_build_at_the_history_cap_latches_immediately() -> None:
    """A loop that reaches the cap before it ever stamps a ledger is born stuck.

    This is the cheapest way into the latch and needs no corruption at all: a
    rollup that inherits a long review history, or any loop whose
    ``issue_ledger`` key was absent when the history crossed
    ``REVIEW_HISTORY_MAX_ENTRIES``, gets ``complete=False`` stamped together
    with a marker on its very first build -- and can never recover.
    """
    history = [
        _review(iteration, f"head-{iteration}", verdict="APPROVE")
        for iteration in range(1, 1 + REVIEW_HISTORY_MAX_ENTRIES)
    ]

    first = build_issue_ledger(history, stamp_complete=True)

    assert first["complete"] is False
    assert first["through_entry_key"]
    assert issue_ledger_lineage_proof_unsatisfiable(first, history) is True

    # One entry fewer and the same first build is trusted.
    under_cap = history[:-1]
    healthy = build_issue_ledger(under_cap, stamp_complete=True)
    assert healthy["complete"] is True
    assert issue_ledger_lineage_proof_unsatisfiable(healthy, under_cap) is False
