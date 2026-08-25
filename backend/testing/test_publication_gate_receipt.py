"""Tests for the bounded, durable local publication-gate receipt."""

import json
import os
import sys
import uuid

import pytest

from app.models import Task, TaskStatus
from app.module_finding_chain import _receipt_is_complete_and_clean
from app.schemas import PublicationGateReceipt
from app.task_blocked import (
    GUARD_SUPPRESSIONS_MAX_BYTES,
    GUARD_SUPPRESSIONS_MAX_IDENTITIES_PER_LANE,
    GUARD_SUPPRESSIONS_MAX_LANES,
    PUBLICATION_GATE_ATTRIBUTION_IDENTITY_MAX_LENGTH,
    PUBLICATION_GATE_ATTRIBUTION_MAX_BYTES,
    PUBLICATION_GATE_ATTRIBUTION_MAX_IDENTITIES,
    PUBLICATION_GATE_ATTRIBUTION_MAX_LANE_ROWS,
    PUBLICATION_GATE_ATTRIBUTION_STRING_MAX_LENGTH,
    record_publication_gate_receipt,
    sanitize_publication_gate_receipt,
)
from testing.repo_scripts_path import resolve_scripts_dir_from_env


_BACKEND_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))

# The publication receipt's multi-lane projection is produced by the shared
# receipt writer the local gate invokes (scripts/verification_receipt.py).
# Import the real one so this suite exercises the projection that actually runs
# on the VM rather than a hand-rolled restatement of its shape. The import is
# tolerant: a missing scripts/ tree must not error the whole module and take
# the DB-backed receipt tests down with it -- but the resolver's NAMED miss
# reason is retained so the guard below reports why rather than reducing every
# cause to the same "not available here".
_SCRIPTS_DIR, _SCRIPTS_DIR_UNAVAILABLE = resolve_scripts_dir_from_env(_BACKEND_DIR)
if _SCRIPTS_DIR is not None and _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

try:
    import verification_receipt
except ImportError as exc:  # pragma: no cover - only when scripts/ is absent
    verification_receipt = None
    if not _SCRIPTS_DIR_UNAVAILABLE:
        # The directory resolved (it carries the sentinel) and the module still
        # would not import. That is a BROKEN checkout, not an absent one, and
        # it must not read as the benign "no scripts/ in this layout" case.
        _SCRIPTS_DIR_UNAVAILABLE = (
            f"{_SCRIPTS_DIR} resolved but `import verification_receipt` failed: "
            f"{exc!r} — the scripts tree is present and broken, not missing"
        )


def _require_verification_receipt():
    """Return the writer module, or skip — except in CI, where a miss FAILS.

    A silent skip in the promotion CI would mean the multi-lane projection
    invariants never enforce; only ad-hoc environments without the repo
    checkout may skip. Either way the resolver's named reason is reported, so
    the miss is diagnosable from the message alone.
    """
    if verification_receipt is not None:
        return verification_receipt
    msg = f"scripts/verification_receipt.py unusable here: {_SCRIPTS_DIR_UNAVAILABLE}"
    if os.getenv("CI"):
        pytest.fail(
            msg + " — refusing to skip in CI: mount the repo root at /repo "
            "or set BLUEPRINT_SCRIPTS_DIR so these invariants enforce"
        )
    pytest.skip(msg)


def _legacy_receipt() -> dict:
    return {"status": "passed", "command": "./check test", "timeout_seconds": 600,
            "hard_timeout_seconds": 1800, "elapsed_seconds": 42,
            "deadline_extended": False, "exit_code": 0,
            "authoritative_ci_required": False, "recorded_at": "2026-08-18T00:00:00Z"}


def _attributed_receipt() -> dict:
    return {**_legacy_receipt(), "attribution_state": "preexisting_test_failure",
            "new_failures": {"new::failure": "AssertionError: new"},
            "suppressed_failures": {"old::failure": {
                "detail": "AssertionError: inherited",
                "reason": "same route/test/failure kind failed on baseline"}},
            "disappeared_failures": {}, "skipped_failures": {}}


def _task() -> Task:
    return Task(id=uuid.uuid4(), project_id=uuid.uuid4(),
                payloads=[{"type": "create", "prompt": "test receipt"}],
                status=TaskStatus.IN_PROGRESS, context={})


def test_older_nine_key_receipt_still_validates_unchanged():
    raw = _legacy_receipt()
    sanitized = sanitize_publication_gate_receipt(raw)
    assert sanitized == raw
    assert PublicationGateReceipt.model_validate(sanitized).model_dump(exclude_none=True) == raw


def test_attribution_fields_round_trip_through_sanitize_and_record():
    task = _task()
    raw = {**_attributed_receipt(), "unknown": "drop me"}
    assert record_publication_gate_receipt(task, raw) is True
    persisted = task.context["publication_gate_receipt"]
    assert persisted["attribution_state"] == "preexisting_test_failure"
    assert persisted["suppressed_failures"] == raw["suppressed_failures"]
    assert "unknown" not in persisted
    assert sanitize_publication_gate_receipt(persisted) == persisted


def test_blocking_gate_receipt_is_never_clean_publication_evidence():
    """Persisted blocking attribution cannot satisfy a clean-gate consumer."""

    raw = {
        **_legacy_receipt(),
        "status": "failed",
        "exit_code": 1,
        "attribution_state": "coverage_regression",
        "new_failures": {"[backend-pytest] test_a::coverage": "route absent"},
        "suppressed_failures": {},
        "disappeared_failures": {},
        "skipped_failures": {},
    }

    sanitized = sanitize_publication_gate_receipt(raw)

    assert sanitized is not None
    assert sanitized["status"] == "failed"
    assert sanitized["attribution_state"] == "coverage_regression"
    assert not _receipt_is_complete_and_clean(sanitized)


def test_over_cap_identity_count_and_string_lengths_are_deterministically_truncated():
    raw = {**_legacy_receipt(),
           "attribution_state": "s" * (PUBLICATION_GATE_ATTRIBUTION_STRING_MAX_LENGTH + 10),
           "new_failures": {
               f"identity-{index:03d}": (
                   "detail-" + "d" * (PUBLICATION_GATE_ATTRIBUTION_STRING_MAX_LENGTH + 10)
                   if index == 0
                   else "short detail"
               )
               for index in range(PUBLICATION_GATE_ATTRIBUTION_MAX_IDENTITIES + 2)}}
    sanitized = sanitize_publication_gate_receipt(raw)
    assert sanitized is not None
    assert sanitized["attribution_state"] == "s" * PUBLICATION_GATE_ATTRIBUTION_STRING_MAX_LENGTH
    identities = sanitized["new_failures"]
    expected = [identity[:PUBLICATION_GATE_ATTRIBUTION_IDENTITY_MAX_LENGTH]
                for identity in sorted(raw["new_failures"])][:PUBLICATION_GATE_ATTRIBUTION_MAX_IDENTITIES]
    assert len(identities) == PUBLICATION_GATE_ATTRIBUTION_MAX_IDENTITIES
    assert list(identities) == expected
    assert all(len(identity) <= PUBLICATION_GATE_ATTRIBUTION_IDENTITY_MAX_LENGTH for identity in identities)
    assert all(len(detail) <= PUBLICATION_GATE_ATTRIBUTION_STRING_MAX_LENGTH for detail in identities.values())
    assert identities["identity-000"] == "detail-" + "d" * (PUBLICATION_GATE_ATTRIBUTION_STRING_MAX_LENGTH - 7)


def test_attribution_block_is_bounded_in_aggregate_with_truncation_counts():
    raw = {**_legacy_receipt(), "attribution_state": "state"}
    for bucket_key in ("new_failures", "suppressed_failures", "disappeared_failures", "skipped_failures"):
        raw[bucket_key] = {
            f"{bucket_key}::{index:04d}::{index * 'x'}": {
                "detail": "d" * PUBLICATION_GATE_ATTRIBUTION_STRING_MAX_LENGTH,
                "reason": "r" * PUBLICATION_GATE_ATTRIBUTION_STRING_MAX_LENGTH,
            }
            for index in range(120)
        }

    sanitized = sanitize_publication_gate_receipt(raw)

    assert sanitized is not None
    attribution = {
        key: sanitized[key]
        for key in ("attribution_state", "new_failures", "suppressed_failures", "disappeared_failures", "skipped_failures", "attribution_counts")
        if key in sanitized
    }
    assert len(json.dumps(attribution, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()) <= PUBLICATION_GATE_ATTRIBUTION_MAX_BYTES
    counts = sanitized["attribution_counts"]
    assert all(counts[key]["observed"] == 120 for key in raw if key.endswith("_failures"))
    assert all(counts[key]["kept"] == len(sanitized[key]) for key in counts)
    assert any(counts[key]["kept"] < counts[key]["observed"] for key in counts)


def test_identity_prefix_collision_is_counted_as_truncation_not_receipt_rejection():
    prefix = "x" * PUBLICATION_GATE_ATTRIBUTION_IDENTITY_MAX_LENGTH
    raw = {**_legacy_receipt(), "new_failures": {prefix + "-a": "first", prefix + "-b": "second"}}

    sanitized = sanitize_publication_gate_receipt(raw)

    assert sanitized is not None
    assert sanitized["new_failures"] == {prefix: "first"}
    assert sanitized["attribution_counts"]["new_failures"] == {"observed": 2, "kept": 1}


def test_non_string_or_non_dict_attribution_members_are_rejected():
    for value in (None, 7, ["not an identity map"], {1: "non-string detail key"}, {"reason": "missing detail"}):
        assert sanitize_publication_gate_receipt(
            {**_legacy_receipt(), "new_failures": {"test": value}}
        ) is None
    assert sanitize_publication_gate_receipt(
        {**_legacy_receipt(), "new_failures": {1: "non-string identity"}}
    ) is None


def test_unknown_keys_are_dropped_from_receipt_and_identity_details():
    raw = {**_attributed_receipt(), "unknown_receipt_key": "drop me",
           "suppressed_failures": {"test": {"detail": "kept", "reason": "kept too",
                                              "unknown_detail_key": "drop me"}}}
    sanitized = sanitize_publication_gate_receipt(raw)
    assert sanitized is not None
    assert "unknown_receipt_key" not in sanitized
    assert sanitized["suppressed_failures"] == {"test": {"detail": "kept", "reason": "kept too"}}


def test_sanitizer_bounds_cross_lane_guard_suppressions():
    """guard_suppressions round-trips, truncates deterministically over-cap,
    and rejects genuinely malformed shapes without disturbing the legacy
    nine-key receipt contract."""
    # 1. Round-trips a small, well-formed cross-lane payload.
    raw = {**_legacy_receipt(), "guard_suppressions": {
        "static": {"state": "passed", "suppressed": {}},
        "vitest": {"state": "preexisting_test_failure",
                   "suppressed": {"vitest::flaky::test": "AssertionError: inherited"}},
    }}
    sanitized = sanitize_publication_gate_receipt(raw)
    assert sanitized is not None
    assert sanitized["guard_suppressions"] == {
        "static": {"state": "passed", "suppressed": {}, "suppressed_observed_count": 0,
                   "suppressed_truncated": False},
        "vitest": {"state": "preexisting_test_failure",
                   "suppressed": {"vitest::flaky::test": "AssertionError: inherited"},
                   "suppressed_observed_count": 1, "suppressed_truncated": False},
    }
    assert sanitized["guard_suppressions_counts"] == {"observed_lanes": 2, "kept_lanes": 2}
    assert PublicationGateReceipt.model_validate(sanitized).model_dump(exclude_none=True) == sanitized

    # 2. Over-cap lane count and over-cap identities-per-lane truncate deterministically.
    over_cap_raw = {**_legacy_receipt(), "guard_suppressions": {
        f"lane-{index:03d}": {
            "state": "passed",
            "suppressed": {
                f"id-{j:03d}": "detail" for j in range(GUARD_SUPPRESSIONS_MAX_IDENTITIES_PER_LANE + 3)
            } if index == 0 else {},
        }
        for index in range(GUARD_SUPPRESSIONS_MAX_LANES + 5)
    }}
    over_cap_sanitized = sanitize_publication_gate_receipt(over_cap_raw)
    assert over_cap_sanitized is not None
    kept_lanes = over_cap_sanitized["guard_suppressions"]
    assert len(kept_lanes) == GUARD_SUPPRESSIONS_MAX_LANES
    expected_lanes = sorted(over_cap_raw["guard_suppressions"])[:GUARD_SUPPRESSIONS_MAX_LANES]
    assert sorted(kept_lanes) == expected_lanes
    assert len(kept_lanes["lane-000"]["suppressed"]) == GUARD_SUPPRESSIONS_MAX_IDENTITIES_PER_LANE
    assert kept_lanes["lane-000"]["suppressed_observed_count"] == GUARD_SUPPRESSIONS_MAX_IDENTITIES_PER_LANE + 3
    assert over_cap_sanitized["guard_suppressions_counts"]["observed_lanes"] == GUARD_SUPPRESSIONS_MAX_LANES + 5
    assert over_cap_sanitized["guard_suppressions_counts"]["kept_lanes"] == GUARD_SUPPRESSIONS_MAX_LANES

    # 3. A byte-budget breach truncates (never rejects) a well-formed payload.
    huge_raw = {**_legacy_receipt(), "guard_suppressions": {
        f"lane-{index:03d}": {
            "state": "passed",
            "suppressed": {
                f"id-{j:03d}": "d" * PUBLICATION_GATE_ATTRIBUTION_STRING_MAX_LENGTH
                for j in range(GUARD_SUPPRESSIONS_MAX_IDENTITIES_PER_LANE)
            },
        }
        for index in range(GUARD_SUPPRESSIONS_MAX_LANES)
    }}
    huge_sanitized = sanitize_publication_gate_receipt(huge_raw)
    assert huge_sanitized is not None
    huge_block = {
        "guard_suppressions": huge_sanitized["guard_suppressions"],
        "guard_suppressions_counts": huge_sanitized["guard_suppressions_counts"],
    }
    assert (
        len(json.dumps(huge_block, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
        <= GUARD_SUPPRESSIONS_MAX_BYTES
    )
    assert huge_sanitized["guard_suppressions_counts"]["kept_lanes"] < GUARD_SUPPRESSIONS_MAX_LANES

    # 4. Genuinely malformed shapes reject the WHOLE receipt (same contract as
    #    the other attribution buckets) rather than silently dropping the key.
    for malformed in (
        "not a dict",
        {"lane": "not a dict"},
        {"lane": {"state": 7, "suppressed": {}}},
        {"lane": {"state": "passed", "suppressed": "not a dict"}},
        {"lane": {"state": "passed", "suppressed": {"id": 7}}},
        {"lane": {"state": "passed", "suppressed": {1: "detail"}}},
    ):
        assert sanitize_publication_gate_receipt(
            {**_legacy_receipt(), "guard_suppressions": malformed}
        ) is None

    # 5. Absent key: legacy nine-key receipts sanitize exactly as before.
    legacy = sanitize_publication_gate_receipt(_legacy_receipt())
    assert legacy is not None
    assert "guard_suppressions" not in legacy
    assert "guard_suppressions_counts" not in legacy

    # 6. Round-trips through the durable record path too.
    task = _task()
    assert record_publication_gate_receipt(task, raw) is True
    assert task.context["publication_gate_receipt"]["guard_suppressions"] == sanitized["guard_suppressions"]



def test_producer_observed_count_survives_and_beats_len_of_the_projection():
    """The durable receipt must reveal producer-side truncation.

    The producer (scripts/guard_suppression_receipt.py) already caps each bucket
    at MAX_ENTRIES_PER_BUCKET, which equals this module's per-lane cap. Deriving
    the observed count from ``len()`` of the projection here would therefore make
    ``suppressed_observed_count`` identically equal to the kept count on every
    production payload, and a 200-suppression static lane would persist as a
    complete-looking set of 25.
    """
    raw = {**_legacy_receipt(), "guard_suppressions": {
        "static": {
            "state": "preexisting_test_failure",
            # What the producer actually forwards: 25 kept entries out of 200 seen.
            "suppressed": {f"static::route::{j:03d}": "baseline" for j in range(25)},
            "suppressed_observed_count": 200,
            "suppressed_truncated": True,
        },
    }}
    sanitized = sanitize_publication_gate_receipt(raw)
    assert sanitized is not None
    lane = sanitized["guard_suppressions"]["static"]
    assert len(lane["suppressed"]) == 25
    # The whole point: observed differs from len(entries).
    assert lane["suppressed_observed_count"] == 200
    assert lane["suppressed_observed_count"] != len(lane["suppressed"])
    assert lane["suppressed_truncated"] is True

    # A producer count that is absent or nonsensical (below the kept count, or a
    # bool) falls back to counting the projection rather than persisting a lie.
    for bogus in ({}, {"suppressed_observed_count": 1}, {"suppressed_observed_count": True}):
        fallback = sanitize_publication_gate_receipt({**_legacy_receipt(), "guard_suppressions": {
            "static": {"state": "passed",
                       "suppressed": {"static::route::a": "d", "static::route::b": "d"},
                       **bogus},
        }})
        assert fallback is not None
        assert fallback["guard_suppressions"]["static"]["suppressed_observed_count"] == 2


def test_disappeared_and_skipped_buckets_are_persisted():
    """A lane whose only content is outside `suppressed` stays auditable.

    The viewport lane's capture-skipped branch records its identity exclusively
    in the `skipped` bucket; a suppressed-only projection persisted
    ``{"state": "capture_skipped", "suppressed": {}}`` and dropped the reason.
    """
    raw = {**_legacy_receipt(), "guard_suppressions": {
        "viewport": {
            "state": "capture_skipped",
            "suppressed": {},
            "suppressed_observed_count": 0,
            "suppressed_truncated": False,
            "skipped": {"viewport::chromium_substrate_unavailable": "no chromium on this VM"},
            "skipped_observed_count": 1,
            "skipped_truncated": False,
        },
        "static": {
            "state": "baseline_failure_disappeared",
            "suppressed": {},
            "disappeared": {"static::route::gone": "baseline failure vanished"},
            "disappeared_observed_count": 30,
            "disappeared_truncated": True,
        },
    }}
    sanitized = sanitize_publication_gate_receipt(raw)
    assert sanitized is not None
    viewport = sanitized["guard_suppressions"]["viewport"]
    assert viewport["skipped"] == {
        "viewport::chromium_substrate_unavailable": "no chromium on this VM"
    }
    assert viewport["skipped_observed_count"] == 1
    # An absent optional bucket simply stays absent (older agent projections).
    assert "disappeared" not in viewport
    static = sanitized["guard_suppressions"]["static"]
    assert static["disappeared"] == {"static::route::gone": "baseline failure vanished"}
    assert static["disappeared_observed_count"] == 30
    assert static["disappeared_truncated"] is True
    assert PublicationGateReceipt.model_validate(sanitized).model_dump(exclude_none=True) == sanitized

    # Malformed optional buckets reject the whole receipt, same as `suppressed`.
    for malformed in (
        {"lane": {"state": "passed", "suppressed": {}, "skipped": "not a dict"}},
        {"lane": {"state": "passed", "suppressed": {}, "disappeared": {"id": 7}}},
    ):
        assert sanitize_publication_gate_receipt(
            {**_legacy_receipt(), "guard_suppressions": malformed}
        ) is None


def test_byte_budget_eviction_drains_low_value_buckets_first_and_flags_truncation():
    """Eviction must not silently shrink a bucket without saying so."""
    raw = {**_legacy_receipt(), "guard_suppressions": {
        f"lane-{index:03d}": {
            "state": "passed",
            "suppressed": {f"s-{j:03d}": "d" * 200 for j in range(GUARD_SUPPRESSIONS_MAX_IDENTITIES_PER_LANE)},
            "suppressed_observed_count": GUARD_SUPPRESSIONS_MAX_IDENTITIES_PER_LANE,
            "skipped": {f"k-{j:03d}": "d" * 200 for j in range(GUARD_SUPPRESSIONS_MAX_IDENTITIES_PER_LANE)},
            "skipped_observed_count": GUARD_SUPPRESSIONS_MAX_IDENTITIES_PER_LANE,
        }
        for index in range(GUARD_SUPPRESSIONS_MAX_LANES)
    }}
    sanitized = sanitize_publication_gate_receipt(raw)
    assert sanitized is not None
    block = {
        "guard_suppressions": sanitized["guard_suppressions"],
        "guard_suppressions_counts": sanitized["guard_suppressions_counts"],
    }
    assert (
        len(json.dumps(block, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
        <= GUARD_SUPPRESSIONS_MAX_BYTES
    )
    shrunk = [
        lane for lane in sanitized["guard_suppressions"].values()
        if len(lane["skipped"]) < lane["skipped_observed_count"]
        or len(lane["suppressed"]) < lane["suppressed_observed_count"]
    ]
    assert shrunk, "byte budget must have evicted something in this fixture"
    for lane in shrunk:
        assert lane["skipped_truncated"] or lane["suppressed_truncated"]


def test_attribution_only_change_archives_prior_receipt():
    task = _task()
    first = _attributed_receipt()
    second = {**first, "suppressed_failures": {}, "new_failures": {"test": "new"}}
    assert record_publication_gate_receipt(task, first) is True
    assert record_publication_gate_receipt(task, second) is True
    assert task.context["publication_gate_receipt"] == sanitize_publication_gate_receipt(second)
    assert task.context["publication_gate_receipt_history"] == [sanitize_publication_gate_receipt(first)]


def _lane_block(lane: str, state: str, suppressed: dict) -> dict:
    return {
        "lane": lane,
        "state": state,
        "blocking": False,
        "new_failures": {},
        "suppressed_failures": suppressed,
        "disappeared_failures": {},
        "skipped_failures": {},
    }


def test_receipt_names_suppressed_failures_from_every_attributed_lane():
    """A publication that leaned on several lanes must name all of them.

    Before this projection only the targeted backend lane reached the durable
    receipt: a publication forgiven by the static-corpus, Vitest or typecheck
    lanes presented as fully clean to every reviewer.
    """
    vr = _require_verification_receipt()
    sidecar = {
        "version": 1,
        "pytest": {
            "state": "preexisting_test_failure",
            "new_failures": {},
            "suppressed_failures": {"backend/testing/test_a.py::test_a::failure": {"detail": "backend inherited"}},
            "disappeared_failures": {},
            "skipped_failures": {},
        },
        "lanes": {
            "backend-pytest": _lane_block(
                "backend-pytest",
                "preexisting_test_failure",
                {"backend/testing/test_a.py::test_a::failure": {
                    "detail": "backend inherited",
                    "reason": "same route/test/failure kind failed on baseline"}},
            ),
            "static-corpus": _lane_block(
                "static-corpus",
                "preexisting_test_failure",
                {"backend/testing/test_b.py::test_b::error": {"detail": "corpus inherited"}},
            ),
            "vitest:admin-ui": _lane_block(
                "vitest:admin-ui",
                "preexisting_test_failure",
                {"src/components/Foo.test.tsx::renders::failure": {"detail": "vitest inherited"}},
            ),
            "typecheck:agent": _lane_block("typecheck:agent", "clean", {}),
        },
    }

    task = _task()
    raw = {**_legacy_receipt(), **vr.project_attribution(sidecar)}
    assert record_publication_gate_receipt(task, raw) is True
    persisted = task.context["publication_gate_receipt"]

    # Every lane's own state is named; no single lane's verdict stands in for
    # the whole gate's.
    assert persisted["attribution_state"] == (
        "backend-pytest=preexisting_test_failure; "
        "static-corpus=preexisting_test_failure; "
        "typecheck:agent=clean; "
        "vitest:admin-ui=preexisting_test_failure"
    )
    # Every lane's suppressed identity survives, attributed to its own lane.
    assert persisted["suppressed_failures"] == {
        "[backend-pytest] backend/testing/test_a.py::test_a::failure": {
            "detail": "backend inherited",
            "reason": "same route/test/failure kind failed on baseline"},
        "[static-corpus] backend/testing/test_b.py::test_b::error": {"detail": "corpus inherited"},
        "[vitest:admin-ui] src/components/Foo.test.tsx::renders::failure": {"detail": "vitest inherited"},
    }
    counts = persisted["attribution_counts"]
    for lane in ("backend-pytest", "static-corpus", "vitest:admin-ui"):
        assert counts[f"suppressed_failures[{lane}]"] == {"observed": 1, "kept": 1}
    assert counts["suppressed_failures"] == {"observed": 3, "kept": 3}
    # Identical identities in two lanes cannot overwrite each other, and the
    # sanitized receipt is still a fixed point of the sanitizer.
    assert sanitize_publication_gate_receipt(persisted) == persisted
    assert PublicationGateReceipt.model_validate(persisted).attribution_counts == counts


def test_aggregate_eviction_cannot_silently_delete_one_whole_lane():
    """Over-budget eviction must thin every lane, not erase the last one.

    Identities sort lane-first, so evicting the greatest identity would consume
    one lane entirely while the receipt still named the others as complete.
    """
    vr = _require_verification_receipt()
    lanes = ("backend-pytest", "static-corpus", "vitest:admin-ui")
    raw = {**_legacy_receipt(), "attribution_state": "; ".join(f"{lane}=preexisting_test_failure" for lane in lanes)}
    raw["suppressed_failures"] = {
        vr.namespace_identity(lane, f"test_{index:03d}.py::test::failure"): {
            "detail": "d" * 500,
            "reason": "same route/test/failure kind failed on baseline",
        }
        for lane in lanes
        for index in range(30)
    }

    sanitized = sanitize_publication_gate_receipt(raw)

    assert sanitized is not None
    counts = sanitized["attribution_counts"]
    assert counts["suppressed_failures"]["kept"] < counts["suppressed_failures"]["observed"]
    surviving = {
        vr.split_namespaced_identity(identity)[0]
        for identity in sanitized["suppressed_failures"]
    }
    assert surviving == set(lanes), "eviction erased a whole lane's suppressions"
    kept = [counts[f"suppressed_failures[{lane}]"]["kept"] for lane in lanes]
    assert all(count > 0 for count in kept)
    assert max(kept) - min(kept) <= 1, "eviction was not spread evenly across lanes"
    assert all(counts[f"suppressed_failures[{lane}]"]["observed"] == 30 for lane in lanes)
    assert sum(kept) == counts["suppressed_failures"]["kept"]


def test_attribution_block_is_bounded_with_thousands_of_lanes():
    """Per-lane counts rows must not outgrow the aggregate byte budget.

    Identity eviction can empty every failure bucket; if the per-lane truncation
    rows were still derived one-per-observed-lane from the raw map, the block
    would exceed its budget with nothing left to evict.  Rows past the cap fold
    into one remainder row that still totals every dropped lane.
    """
    lanes = 1200
    raw = {
        **_legacy_receipt(),
        "attribution_state": "s" * (PUBLICATION_GATE_ATTRIBUTION_STRING_MAX_LENGTH + 500),
        "new_failures": {
            f"[lane{index:04d}] test_{index:04d}.py::test::failure": {
                "detail": "d" * PUBLICATION_GATE_ATTRIBUTION_STRING_MAX_LENGTH,
                "reason": "r" * PUBLICATION_GATE_ATTRIBUTION_STRING_MAX_LENGTH,
            }
            for index in range(lanes)
        },
    }

    sanitized = sanitize_publication_gate_receipt(raw)

    assert sanitized is not None
    attribution = {
        key: sanitized[key]
        for key in (
            "attribution_state",
            "new_failures",
            "suppressed_failures",
            "disappeared_failures",
            "skipped_failures",
            "attribution_counts",
        )
        if key in sanitized
    }
    assert len(
        json.dumps(attribution, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ) <= PUBLICATION_GATE_ATTRIBUTION_MAX_BYTES
    counts = sanitized["attribution_counts"]
    lane_rows = [key for key in counts if key.startswith("new_failures[")]
    assert lane_rows, "lane attribution must still be reported"
    assert len(lane_rows) <= PUBLICATION_GATE_ATTRIBUTION_MAX_LANE_ROWS + 1
    # No lane's observations are silently dropped: rows (including the folded
    # remainder) still account for every observed identity.
    assert counts["new_failures"]["observed"] == lanes
    assert sum(counts[key]["observed"] for key in lane_rows) == lanes
    assert sum(counts[key]["kept"] for key in lane_rows) == counts["new_failures"]["kept"]
    remainder = counts.get("new_failures[+remainder]")
    assert remainder is not None and remainder["lanes"] > 0
    # The projection is durable: it round-trips through the persisted schema.
    assert PublicationGateReceipt.model_validate(sanitized).attribution_counts == counts


def test_legacy_single_lane_receipt_keeps_its_exact_counts_shape():
    """Un-namespaced identities must not grow per-lane count rows."""
    sanitized = sanitize_publication_gate_receipt(_attributed_receipt())
    assert sanitized is not None
    assert set(sanitized["attribution_counts"]) == {
        "new_failures", "suppressed_failures", "disappeared_failures", "skipped_failures"}
