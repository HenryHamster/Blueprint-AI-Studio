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


def test_discontinuous_capped_history_cannot_extend_stale_seed_proof() -> None:
    first = _review(1, "head-1", verdict="APPROVE")
    seed = build_issue_ledger([first], stamp_complete=True)
    discontinuous_history = [
        _review(iteration, f"head-{iteration}", verdict="APPROVE")
        for iteration in range(100, 100 + REVIEW_HISTORY_MAX_ENTRIES)
    ]

    rebuilt = build_issue_ledger(
        discontinuous_history,
        seed_ledger=seed,
        stamp_complete=True,
    )

    assert rebuilt["complete"] is False
    assert issue_ledger_complete_for_history(
        rebuilt, discontinuous_history
    ) is False
