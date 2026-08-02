"""Tests for digest self-review / SR analytics (app.sr_*)."""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlmodel import select

from app.models import (
    TaskStatus,
)
from testing.factories import make_project, make_task, make_membership, _utcnow

from testing._digest_test_helpers import _curr_ts, _prev_ts  # noqa: F401


def _sr_skipped_envelope_unknown_verdict() -> dict:
    return {
        "verdict": "unknown",
        "fixes_applied": False,
        "check_results": {},
        "blind_spots": [],
    }


def _sr_skipped_envelope_blind_spot_marker(marker: str) -> dict:
    return {
        "verdict": "unknown",
        "fixes_applied": False,
        "check_results": {},
        "blind_spots": [marker],
    }


def _sr_valid_outcome() -> dict:
    return {
        "verdict": "pass",
        "fixes_applied": False,
        "check_results": {"correctness": {"passed": True}},
        "blind_spots": [],
    }


@pytest.mark.asyncio
async def test_digest_self_review_skip_rate_detects_skipped_envelope(
    client: AsyncClient, db_session
):
    """An unknown-verdict outcome with empty check_results counts as a skip."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 3 skipped (unknown verdict, empty check_results) + 2 valid → 5 code-change tasks
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_self_review_outcome": _sr_skipped_envelope_unknown_verdict()
                },
            )
        )
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={"_self_review_outcome": _sr_valid_outcome()},
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    skip_rate = response.json()["self_review_skip_rate"]
    assert skip_rate is not None
    assert skip_rate["skipped_count"] == 3
    assert skip_rate["sample_size"] == 5
    assert skip_rate["overall_rate"] == 0.6


@pytest.mark.asyncio
async def test_digest_self_review_skip_rate_detects_blind_spot_marker(
    client: AsyncClient, db_session
):
    """blind_spots entries containing the fallback markers count as skips."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 2 parse-failure + 2 missing-file + 1 valid → 5 code-change tasks, 4 skipped
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "refactor"}],
                context={
                    "_self_review_outcome": _sr_skipped_envelope_blind_spot_marker(
                        "self-review JSON parsing failed"
                    )
                },
            )
        )
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "migrate"}],
                context={
                    "_self_review_outcome": _sr_skipped_envelope_blind_spot_marker(
                        "self-review skipped — investigate"
                    )
                },
            )
        )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={"_self_review_outcome": _sr_valid_outcome()},
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    skip_rate = response.json()["self_review_skip_rate"]
    assert skip_rate is not None
    assert skip_rate["skipped_count"] == 4
    assert skip_rate["sample_size"] == 5
    assert skip_rate["overall_rate"] == 0.8


@pytest.mark.asyncio
async def test_digest_self_review_skip_rate_excludes_non_code_change_types(
    client: AsyncClient,
    db_session,
):
    """Review/research tasks are excluded from the denominator."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 5 code-change tasks: 1 skipped, 4 valid
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={"_self_review_outcome": _sr_skipped_envelope_unknown_verdict()},
        )
    )
    for _ in range(4):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={"_self_review_outcome": _sr_valid_outcome()},
            )
        )
    # Non-code-change tasks with skipped envelopes — must NOT affect the metric
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "review"}],
                context={
                    "_self_review_outcome": _sr_skipped_envelope_unknown_verdict()
                },
            )
        )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"_self_review_outcome": _sr_skipped_envelope_unknown_verdict()},
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    skip_rate = response.json()["self_review_skip_rate"]
    assert skip_rate is not None
    assert skip_rate["sample_size"] == 5
    assert skip_rate["skipped_count"] == 1
    assert skip_rate["overall_rate"] == 0.2


@pytest.mark.asyncio
async def test_digest_self_review_skip_rate_returns_none_below_min_sample(
    client: AsyncClient,
    db_session,
):
    """Returns None when fewer than 5 code-change tasks are available."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 4 code-change tasks total → below threshold
    for _ in range(4):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_self_review_outcome": _sr_skipped_envelope_unknown_verdict()
                },
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    assert response.json()["self_review_skip_rate"] is None


@pytest.mark.asyncio
async def test_digest_self_review_skip_rate_by_task_type_breakdown(
    client: AsyncClient,
    db_session,
):
    """by_task_type reports per-type rates only when that type has >=5 samples."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 5 create tasks: 2 skipped + 3 valid → eligible for by_task_type
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_self_review_outcome": _sr_skipped_envelope_unknown_verdict()
                },
            )
        )
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={"_self_review_outcome": _sr_valid_outcome()},
            )
        )
    # 2 fix tasks → below per-type threshold; included in overall but not in by_task_type
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={
                    "_self_review_outcome": _sr_skipped_envelope_unknown_verdict()
                },
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    skip_rate = response.json()["self_review_skip_rate"]
    assert skip_rate is not None
    assert skip_rate["sample_size"] == 7
    assert skip_rate["skipped_count"] == 4
    assert "create" in skip_rate["by_task_type"]
    assert skip_rate["by_task_type"]["create"]["sample_size"] == 5
    assert skip_rate["by_task_type"]["create"]["skipped_count"] == 2
    assert skip_rate["by_task_type"]["create"]["rate"] == 0.4
    assert "fix" not in skip_rate["by_task_type"]


@pytest.mark.asyncio
async def test_digest_sr_disallowed_blind_spot_rate_below_min_sample(
    client: AsyncClient, db_session
):
    """Fewer than 5 code-change tasks → sr_disallowed_blind_spot_rate is None."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for _ in range(4):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_self_review_outcome": {
                        **_sr_valid_outcome(),
                        "_filtered_blind_spots_count": 1,
                    }
                },
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    assert response.json()["sr_disallowed_blind_spot_rate"] is None


@pytest.mark.asyncio
async def test_digest_sr_disallowed_blind_spot_rate_basic(
    client: AsyncClient, db_session
):
    """Tasks with _filtered_blind_spots_count > 0 produce a correct violation rate."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 2 violators + 3 clean = 5 code-change tasks → rate = 0.4
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_self_review_outcome": {
                        **_sr_valid_outcome(),
                        "_filtered_blind_spots_count": 2,
                    }
                },
            )
        )
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={
                    "_self_review_outcome": {
                        **_sr_valid_outcome(),
                        "_filtered_blind_spots_count": 0,
                    }
                },
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    rate = response.json()["sr_disallowed_blind_spot_rate"]
    assert rate is not None
    assert rate["violation_count"] == 2
    assert rate["sample_size"] == 5
    assert rate["overall_rate"] == 0.4


@pytest.mark.asyncio
async def test_sr_skip_rate_excludes_pre_self_review_failures(
    client: AsyncClient, db_session
):
    """Tasks that failed before reaching self-review (no _self_review_outcome key)
    are excluded from both the numerator and denominator of self_review_skip_rate."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 2 tasks that reached SR and skipped it (counted as skips)
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={"_self_review_outcome": _sr_skipped_envelope_unknown_verdict()},
            )
        )
    # 3 tasks that reached SR with a valid outcome (counted as non-skips)
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={"_self_review_outcome": _sr_valid_outcome()},
            )
        )
    # 4 code-change tasks that FAILED before reaching SR — no _self_review_outcome key.
    # These must be excluded from the denominator entirely.
    for _ in range(4):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.FAILED,
                payloads=[{"type": "create"}],
                context={"some_other_key": "value"},
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    skip_rate = response.json()["self_review_skip_rate"]
    assert skip_rate is not None
    # Only the 5 tasks that reached SR count; the 4 pre-SR failures are excluded.
    assert skip_rate["sample_size"] == 5
    assert skip_rate["skipped_count"] == 2
    assert skip_rate["overall_rate"] == 0.4


@pytest.mark.asyncio
async def test_sr_disallowed_blind_spot_rate_excludes_pre_self_review_failures(
    client: AsyncClient, db_session
):
    """Tasks that failed before reaching self-review (no _self_review_outcome key)
    are excluded from sr_disallowed_blind_spot_rate's denominator."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 2 tasks with violations
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_self_review_outcome": {
                        **_sr_valid_outcome(),
                        "_filtered_blind_spots_count": 1,
                    }
                },
            )
        )
    # 3 tasks without violations
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={
                    "_self_review_outcome": {
                        **_sr_valid_outcome(),
                        "_filtered_blind_spots_count": 0,
                    }
                },
            )
        )
    # 4 code-change tasks that FAILED before reaching SR — no _self_review_outcome key.
    # These must NOT inflate the denominator.
    for _ in range(4):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.FAILED,
                payloads=[{"type": "create"}],
                context={"some_other_key": "value"},
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    rate = response.json()["sr_disallowed_blind_spot_rate"]
    assert rate is not None
    # Only the 5 tasks that reached SR count; the 4 pre-SR failures are excluded.
    assert rate["sample_size"] == 5
    assert rate["violation_count"] == 2
    assert rate["overall_rate"] == 0.4


@pytest.mark.asyncio
async def test_sr_blind_spots_aggregates_themes(client: AsyncClient, db_session):
    """Top themes are counted and skipped/fallback envelopes are excluded."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 3 tasks with the same first-6-token theme (should merge into count=3)
    for i in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {"correctness": {"passed": True}},
                        "blind_spots": [
                            f"did not verify file path number {i} thoroughly"
                        ],
                    }
                },
            )
        )
    # 1 task with a distinct blind-spot theme
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "fix"}],
            context={
                "_self_review_outcome": {
                    "verdict": "pass",
                    "fixes_applied": False,
                    "check_results": {},
                    "blind_spots": ["async error propagation not checked"],
                }
            },
        )
    )
    # 1 skipped envelope — must NOT contribute to themes
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={"_self_review_outcome": _sr_skipped_envelope_unknown_verdict()},
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-blind-spots?days=30"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["window_days"] == 30
    themes_by_name = {t["theme"]: t for t in body["themes"]}
    # The 3 identical long strings normalise to the same 6-token prefix
    assert themes_by_name["did not verify file path number"]["count"] == 3
    assert themes_by_name["async error propagation not checked"]["count"] == 1
    # Skipped envelope contributes no entries
    assert body["total_distinct_themes"] == 2
    # Each theme entry must carry an examples list
    for entry in body["themes"]:
        assert "examples" in entry
        for ex in entry["examples"]:
            assert "text" in ex and "task_id" in ex and "task_type" in ex
    # The repeated-theme has examples capped and preserves verbatim text
    repeated = themes_by_name["did not verify file path number"]
    assert len(repeated["examples"]) == 3
    for ex in repeated["examples"]:
        assert ex["text"].startswith("did not verify file path number")
        assert ex["task_type"] == "create"


@pytest.mark.asyncio
async def test_sr_blind_spots_examples_capped_at_limit(client: AsyncClient, db_session):
    """Examples are capped at _BLIND_SPOT_EXAMPLES_PER_THEME even when count exceeds it."""
    from app.sr_blind_spots import _BLIND_SPOT_EXAMPLES_PER_THEME

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create more tasks than the example cap for a single theme. The blind-spot
    # string must NOT match DISALLOWED_BLIND_SPOT_PATTERNS — intended-skip
    # phrasings ("did not run the full ... suite") are filtered before themeing.
    for i in range(_BLIND_SPOT_EXAMPLES_PER_THEME + 2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {"correctness": {"passed": True}},
                        "blind_spots": [f"did not verify file path number {i}"],
                    }
                },
            )
        )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-blind-spots?days=30"
    )
    assert response.status_code == 200
    body = response.json()
    theme = body["themes"][0]
    assert theme["count"] == _BLIND_SPOT_EXAMPLES_PER_THEME + 2
    assert len(theme["examples"]) == _BLIND_SPOT_EXAMPLES_PER_THEME


@pytest.mark.asyncio
async def test_sr_blind_spots_missing_task_type_produces_empty_string(
    client: AsyncClient, db_session
):
    """task_type is '' when first_payload has no 'type' key."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{}],  # no 'type' key
            context={
                "_self_review_outcome": {
                    "verdict": "pass",
                    "fixes_applied": False,
                    "check_results": {},
                    "blind_spots": ["async propagation not verified at all"],
                }
            },
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-blind-spots?days=30"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["themes"]
    ex = body["themes"][0]["examples"][0]
    assert ex["task_type"] == ""


@pytest.mark.asyncio
async def test_sr_blind_spots_skipped_envelopes_contribute_zero_examples(
    client: AsyncClient, db_session
):
    """Skipped/fallback SR envelopes produce no theme entries and no examples."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={"_self_review_outcome": _sr_skipped_envelope_unknown_verdict()},
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_self_review_outcome": _sr_skipped_envelope_blind_spot_marker(
                    "self-review skipped"
                )
            },
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-blind-spots?days=30"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["themes"] == []
    assert body["total_distinct_themes"] == 0


@pytest.mark.asyncio
async def test_sr_blind_spots_example_task_id_matches_source_task(
    client: AsyncClient, db_session
):
    """Each example carries the task_id of the task that emitted that blind_spot."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "refactor"}],
        context={
            "_self_review_outcome": {
                "verdict": "pass",
                "fixes_applied": False,
                "check_results": {},
                "blind_spots": ["did not check caller compatibility thoroughly here"],
            }
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-blind-spots?days=30"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["themes"]
    ex = body["themes"][0]["examples"][0]
    assert ex["task_id"] == str(task.id)
    assert ex["task_type"] == "refactor"


@pytest.mark.asyncio
async def test_prompt_fingerprint_analytics_groups_by_fingerprint(
    client: AsyncClient, db_session
):
    """Code-change tasks bucket by prompt_fingerprint with correct success/skip rates."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))

    # Fingerprint A: 3 tasks, 2 first-attempt successes, 0 SR skips
    for succ in (True, True, False):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                prompt_fingerprint="aaaaaaaaaaaa",
                context={
                    "_first_attempt_succeeded": succ,
                    "_self_review_outcome": _sr_valid_outcome(),
                    **(
                        {}
                        if succ
                        else {"_first_attempt_rejection_categories": ["correctness"]}
                    ),
                },
            )
        )
    # Fingerprint B: 3 tasks, 0 first-attempt successes, 1 SR skip
    for i in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                prompt_fingerprint="bbbbbbbbbbbb",
                context={
                    "_first_attempt_succeeded": False,
                    "_first_attempt_rejection_categories": [
                        "correctness",
                        "performance",
                    ],
                    "_self_review_outcome": (
                        _sr_skipped_envelope_unknown_verdict()
                        if i == 0
                        else _sr_valid_outcome()
                    ),
                },
            )
        )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/prompt-fingerprint-analytics?days=30"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["window_days"] == 30
    assert body["sample_size"] == 6

    by_fp = {fp["fingerprint"]: fp for fp in body["fingerprints"]}
    assert set(by_fp) == {"aaaaaaaaaaaa", "bbbbbbbbbbbb"}

    a = by_fp["aaaaaaaaaaaa"]
    assert a["sample_size"] == 3
    assert a["sr_sample_size"] == 3
    assert a["first_attempt_measured"] == 3
    assert a["first_attempt_success_rate"] == round(2 / 3, 3)
    assert a["sr_skip_rate"] == 0.0
    assert a["sr_catch_rate"] == 0.0
    assert a["rejection_category_counts"] == {"correctness": 1}

    b = by_fp["bbbbbbbbbbbb"]
    assert b["sample_size"] == 3
    assert b["sr_sample_size"] == 2
    assert b["first_attempt_success_rate"] == 0.0
    assert b["sr_skip_rate"] == round(1 / 3, 3)
    assert b["sr_catch_rate"] is None
    assert b["rejection_category_counts"] == {"correctness": 3, "performance": 3}

    # Project mean = 2/6 successes = 0.333; A above, B below.
    assert body["project_first_attempt_success_rate"] == round(2 / 6, 3)
    assert a["delta_vs_project_mean"] is not None and a["delta_vs_project_mean"] > 0
    assert b["delta_vs_project_mean"] is not None and b["delta_vs_project_mean"] < 0


@pytest.mark.asyncio
async def test_prompt_fingerprint_analytics_unknown_bucket_for_legacy_rows(
    client: AsyncClient, db_session
):
    """Tasks without a prompt_fingerprint fall into an `unknown` bucket."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))

    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                prompt_fingerprint=None,
                context={
                    "_first_attempt_succeeded": True,
                    "_self_review_outcome": _sr_valid_outcome(),
                },
            )
        )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/prompt-fingerprint-analytics"
    )
    assert response.status_code == 200
    fps = response.json()["fingerprints"]
    assert len(fps) == 1
    assert fps[0]["fingerprint"] == "unknown"
    assert fps[0]["sample_size"] == 3


@pytest.mark.asyncio
async def test_prompt_fingerprint_analytics_falls_back_to_context_mirror(
    client: AsyncClient, db_session
):
    """Legacy rows use context.prompt_fingerprint when the column is null."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))

    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                prompt_fingerprint=None,
                context={
                    "prompt_fingerprint": "legacyfp12345",
                    "_first_attempt_succeeded": True,
                    "_self_review_outcome": _sr_valid_outcome(),
                },
            )
        )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/prompt-fingerprint-analytics"
    )
    assert response.status_code == 200
    fps = response.json()["fingerprints"]
    assert len(fps) == 1
    assert fps[0]["fingerprint"] == "legacyfp12345"
    assert fps[0]["sample_size"] == 3


@pytest.mark.asyncio
async def test_prompt_fingerprint_analytics_excludes_non_code_change(
    client: AsyncClient, db_session
):
    """Research/plan task types are not counted toward fingerprint analytics."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))

    # 3 research tasks (excluded) sharing a fingerprint
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "research"}],
                prompt_fingerprint="cccccccccccc",
                context={
                    "_first_attempt_succeeded": True,
                    "_self_review_outcome": _sr_valid_outcome(),
                },
            )
        )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/prompt-fingerprint-analytics"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["sample_size"] == 0
    assert body["fingerprints"] == []


@pytest.mark.asyncio
async def test_prompt_fingerprint_analytics_filters_below_min_sample_size(
    client: AsyncClient, db_session
):
    """Fingerprint buckets with sample_size < min_sample_size are excluded."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))

    # Fingerprint A has 3 tasks; B has 1 task — only A should survive a min of 2.
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                prompt_fingerprint="aaaaaaaaaaaa",
                context={
                    "_first_attempt_succeeded": True,
                    "_self_review_outcome": _sr_valid_outcome(),
                },
            )
        )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            prompt_fingerprint="bbbbbbbbbbbb",
            context={
                "_first_attempt_succeeded": False,
                "_self_review_outcome": _sr_valid_outcome(),
            },
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/prompt-fingerprint-analytics?min_sample_size=2"
    )
    assert response.status_code == 200
    fps = response.json()["fingerprints"]
    assert [f["fingerprint"] for f in fps] == ["aaaaaaaaaaaa"]
    # Project mean still includes the singleton — sample_size counts all 4.
    assert response.json()["sample_size"] == 4


@pytest.mark.asyncio
async def test_prompt_fingerprint_analytics_sr_quality_fields_gate_on_sr_sample_size(
    client: AsyncClient, db_session
):
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))

    db_session.add_all(
        [
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                prompt_fingerprint="aaaaaaaaaaaa",
                context={
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {
                            "correctness": {"passed": False, "issue": "x"}
                        },
                        "blind_spots": [],
                    },
                    "_first_attempt_rejection_categories": ["correctness"],
                },
            ),
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                prompt_fingerprint="aaaaaaaaaaaa",
                context={
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {},
                        "blind_spots": [],
                    },
                    "_first_attempt_rejection_categories": ["security"],
                },
            ),
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                prompt_fingerprint="aaaaaaaaaaaa",
                context={
                    "_self_review_outcome": _sr_skipped_envelope_unknown_verdict()
                },
            ),
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                prompt_fingerprint="bbbbbbbbbbbb",
                context={
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {
                            "correctness": {"passed": False, "issue": "x"}
                        },
                        "blind_spots": [],
                    },
                    "_first_attempt_rejection_categories": ["correctness"],
                },
            ),
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                prompt_fingerprint="bbbbbbbbbbbb",
                context={
                    "_self_review_outcome": _sr_skipped_envelope_unknown_verdict()
                },
            ),
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                prompt_fingerprint="bbbbbbbbbbbb",
                context={
                    "_self_review_outcome": _sr_skipped_envelope_unknown_verdict()
                },
            ),
        ]
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/prompt-fingerprint-analytics?min_sample_size=2"
    )
    assert response.status_code == 200
    by_fp = {fp["fingerprint"]: fp for fp in response.json()["fingerprints"]}

    a = by_fp["aaaaaaaaaaaa"]
    assert a["sample_size"] == 3
    assert a["sr_sample_size"] == 2
    assert a["sr_catch_sample_size"] == 2
    assert a["sr_catch_rate"] == 0.5
    assert a["sr_false_positive_rate"] == 0.0
    b = by_fp["bbbbbbbbbbbb"]
    assert b["sample_size"] == 3
    assert b["sr_sample_size"] == 1
    assert b["sr_catch_rate"] is None
    assert b["sr_false_positive_rate"] is None
    assert b["sr_false_positive_rate_by_category"] == []


@pytest.mark.asyncio
async def test_prompt_fingerprint_analytics_requires_project_membership(
    client: AsyncClient, db_session
):
    """Non-member callers get 404 from _get_user_project_with_role."""
    project = make_project(user_id="some-other-owner")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    # Intentionally skip make_membership for the test user.

    response = await client.get(
        f"/api/admin/projects/{project.id}/prompt-fingerprint-analytics"
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_digest_sr_trends_fp_fields_passthrough(client: AsyncClient, db_session):
    """Digest passes fp_count and false_positive_rate from sr_catch_rate_trends in design_doc."""
    project = make_project()
    project.design_doc = {
        "sr_catch_rate_trends": {
            "computed_at": "2026-05-01T00:00:00",
            "signal_last_computed_at": "2026-05-01T00:00:00",
            "categories": {
                "correctness": {
                    "current_hit_rate": 0.7,
                    "miss_rate": 0.3,
                    "trend": "stable",
                    "fp_count": 2,
                    "tp_count": 3,
                    "false_positive_rate": 0.4,
                }
            },
            "by_task_type": {},
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    body = response.json()
    trends = body.get("self_review_prediction_accuracy_trends")
    assert trends is not None
    correctness = trends["categories"]["correctness"]
    assert correctness["fp_count"] == 2
    assert correctness["tp_count"] == 3
    assert correctness["false_positive_rate"] == 0.4
    assert trends["signal_last_computed_at"] == "2026-05-01T00:00:00"


@pytest.mark.asyncio
async def test_digest_sr_trends_full_fidelity_passthrough(
    client: AsyncClient, db_session
):
    """Digest passes all canonical SR trend fields unchanged (full-fidelity contract)."""
    project = make_project()
    project.design_doc = {
        "sr_catch_rate_trends": {
            "computed_at": "2026-05-01T00:00:00",
            "bucket_mode": "cycle",
            "current_cycle_id": "cycle-1",
            "previous_cycle_id": "cycle-0",
            "categories": {
                "correctness": {
                    "current_hit_rate": 0.8,
                    "miss_rate": 0.2,
                    "trend": "improving",
                    "fp_count": 1,
                    "tp_count": 4,
                    "false_positive_rate": 0.2,
                }
            },
            "by_task_type": {"create": {"correctness": {"hit_count": 3}}},
            "by_scope": {
                "large": {
                    "correctness": {
                        "hit_rate": 0.4,
                        "miss_rate": 0.6,
                        "sample_count": 5,
                    }
                }
            },
            "by_severity": {
                "high": {
                    "current_miss_rate": 0.1,
                    "current_hit_rate": 0.9,
                    "trend": "stable",
                }
            },
            "sr_skip_rate": 0.05,
            "sr_skip_reason_distribution": {"legacy_unknown": 2},
            "by_direction": {"dir-abc": {"correctness": {"hit_count": 2}}},
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    trends = response.json().get("self_review_prediction_accuracy_trends")
    assert trends is not None

    assert trends["bucket_mode"] == "cycle"
    assert trends["current_cycle_id"] == "cycle-1"
    assert trends["previous_cycle_id"] == "cycle-0"
    assert trends["sr_skip_rate"] == 0.05
    assert trends["sr_skip_reason_distribution"] == {"legacy_unknown": 2}
    assert "dir-abc" in trends["by_direction"]
    assert "execution_evidence_miss_rates" not in trends
    assert "edges_considered_stats" not in trends
    assert trends["by_severity"]["high"]["trend"] == "stable"
    assert trends["computed_at"] == "2026-05-01T00:00:00"
    assert trends["categories"]["correctness"]["fp_count"] == 1
    assert trends["by_scope"]["large"]["correctness"]["miss_rate"] == 0.6
    assert trends["by_scope"]["large"]["correctness"]["sample_count"] == 5


@pytest.mark.asyncio
async def test_digest_sr_trends_overall_verdict_passthrough(
    client: AsyncClient, db_session
):
    """Digest explicitly promotes overall_trend_verdict/significant/data_sufficient."""
    project = make_project()
    project.design_doc = {
        "sr_catch_rate_trends": {
            "computed_at": "2026-05-01T00:00:00",
            "categories": {},
            "by_task_type": {},
            "overall_hit_rate": 0.72,
            "previous_overall_hit_rate": 0.65,
            "overall_trend_verdict": "improving",
            "overall_trend_significant": True,
            "overall_trend_data_sufficient": True,
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    trends = response.json().get("self_review_prediction_accuracy_trends")
    assert trends is not None
    assert trends["overall_trend_verdict"] == "improving"
    assert trends["overall_trend_significant"] is True
    assert trends["overall_trend_data_sufficient"] is True

    # Stable/not-significant case
    project2 = make_project()
    project2.design_doc = {
        "sr_catch_rate_trends": {
            "computed_at": "2026-05-01T00:00:00",
            "categories": {},
            "by_task_type": {},
            "overall_hit_rate": 0.70,
            "previous_overall_hit_rate": 0.71,
            "overall_trend_verdict": "stable",
            "overall_trend_significant": False,
            "overall_trend_data_sufficient": True,
        }
    }
    db_session.add(project2)
    await db_session.commit()
    await db_session.refresh(project2)

    response2 = await client.get(f"/api/admin/digest?project_id={project2.id}")
    assert response2.status_code == 200
    trends2 = response2.json().get("self_review_prediction_accuracy_trends")
    assert trends2 is not None
    assert trends2["overall_trend_verdict"] == "stable"
    assert trends2["overall_trend_significant"] is False

    # Absent (None) case — fields present but null
    project3 = make_project()
    project3.design_doc = {
        "sr_catch_rate_trends": {
            "computed_at": "2026-05-01T00:00:00",
            "categories": {},
            "by_task_type": {},
        }
    }
    db_session.add(project3)
    await db_session.commit()
    await db_session.refresh(project3)

    response3 = await client.get(f"/api/admin/digest?project_id={project3.id}")
    assert response3.status_code == 200
    trends3 = response3.json().get("self_review_prediction_accuracy_trends")
    assert trends3 is not None
    assert trends3["overall_trend_verdict"] is None
    assert trends3["overall_trend_significant"] is None
    assert trends3["overall_trend_data_sufficient"] is None


@pytest.mark.asyncio
async def test_digest_sr_trends_co_failure_pairs_passthrough(
    client: AsyncClient, db_session
):
    """Digest passes co_failure_pairs from sr_catch_rate_trends in design_doc."""
    project = make_project()
    project.design_doc = {
        "sr_catch_rate_trends": {
            "computed_at": "2026-05-01T00:00:00",
            "signal_last_computed_at": "2026-05-01T00:00:00",
            "categories": {},
            "by_task_type": {},
            "co_failure_pairs": [
                {
                    "cat_a": "correctness",
                    "cat_b": "test-coverage",
                    "co_fail_count": 5,
                    "p_b_given_a": 0.8333,
                    "base_rate_b": 0.4167,
                    "lift": 2.0,
                },
                {
                    "cat_a": "test-coverage",
                    "cat_b": "correctness",
                    "co_fail_count": 5,
                    "p_b_given_a": 1.0,
                    "base_rate_b": 0.5,
                    "lift": 2.0,
                },
            ],
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    trends = response.json().get("self_review_prediction_accuracy_trends")
    assert trends is not None
    pairs = trends.get("co_failure_pairs")
    assert isinstance(pairs, list)
    assert len(pairs) == 2
    first = pairs[0]
    assert first["cat_a"] == "correctness"
    assert first["cat_b"] == "test-coverage"
    assert first["co_fail_count"] == 5
    assert first["lift"] == 2.0


@pytest.mark.asyncio
async def test_digest_sr_trends_empty_optional_fields_present(
    client: AsyncClient, db_session
):
    """Digest returns sr trend keys with empty defaults when optional fields are absent."""
    project = make_project()
    project.design_doc = {
        "sr_catch_rate_trends": {
            "computed_at": "2026-05-01T00:00:00",
            "bucket_mode": "calendar",
            "current_cycle_id": None,
            "previous_cycle_id": None,
            "categories": {},
            "by_task_type": {},
            "by_severity": {},
            "sr_skip_rate": None,
            "sr_skip_reason_distribution": {},
            "by_direction": {},
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    trends = response.json().get("self_review_prediction_accuracy_trends")
    assert trends is not None

    for key in (
        "bucket_mode",
        "current_cycle_id",
        "previous_cycle_id",
        "sr_skip_rate",
        "sr_skip_reason_distribution",
        "by_direction",
    ):
        assert key in trends, f"missing key: {key}"
    assert trends["sr_skip_rate"] is None
    assert trends["by_direction"] == {}
    assert "execution_evidence_miss_rates" not in trends
    assert "edges_considered_stats" not in trends


@pytest.mark.asyncio
async def test_serialize_sr_catch_rate_trends_is_stale(client: AsyncClient, db_session):
    """is_stale=True when computed_at is >7 days old; False when fresh; True when absent."""
    # Case 1: stale (8 days old)
    project = make_project()
    stale_ts = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=8)).isoformat()
    project.design_doc = {
        "sr_catch_rate_trends": {
            "computed_at": stale_ts,
            "categories": {"correctness": {"miss_rate": 0.3, "trend": "stable"}},
            "by_task_type": {},
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    trends = response.json().get("self_review_prediction_accuracy_trends")
    assert trends is not None
    assert trends["is_stale"] is True

    # Case 2: fresh (1 day old)
    project2 = make_project()
    fresh_ts = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)).isoformat()
    project2.design_doc = {
        "sr_catch_rate_trends": {
            "computed_at": fresh_ts,
            "categories": {"correctness": {"miss_rate": 0.2, "trend": "stable"}},
            "by_task_type": {},
        }
    }
    db_session.add(project2)
    await db_session.commit()
    await db_session.refresh(project2)

    response2 = await client.get(f"/api/admin/digest?project_id={project2.id}")
    assert response2.status_code == 200
    trends2 = response2.json().get("self_review_prediction_accuracy_trends")
    assert trends2 is not None
    assert trends2["is_stale"] is False

    # Case 3: computed_at absent → stale
    project3 = make_project()
    project3.design_doc = {
        "sr_catch_rate_trends": {
            "categories": {"correctness": {"miss_rate": 0.1, "trend": "stable"}},
            "by_task_type": {},
        }
    }
    db_session.add(project3)
    await db_session.commit()
    await db_session.refresh(project3)

    response3 = await client.get(f"/api/admin/digest?project_id={project3.id}")
    assert response3.status_code == 200
    trends3 = response3.json().get("self_review_prediction_accuracy_trends")
    assert trends3 is not None
    assert trends3["is_stale"] is True


@pytest.mark.asyncio
async def test_serialize_sr_catch_rate_trends_handles_tz_aware_computed_at(
    client: AsyncClient, db_session
):
    """A tz-aware computed_at is normalized before the stale check runs."""
    project = make_project()
    computed_at = datetime.now(timezone.utc) - timedelta(days=1)
    project.design_doc = {
        "sr_catch_rate_trends": {
            "computed_at": computed_at.isoformat(),
            "categories": {"correctness": {"miss_rate": 0.2, "trend": "stable"}},
            "by_task_type": {},
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    trends = response.json().get("self_review_prediction_accuracy_trends")
    assert trends is not None
    assert trends["is_stale"] is False


@pytest.mark.asyncio
async def test_direction_sr_breakdown_empty(client: AsyncClient, db_session):
    """No tasks → empty directions list with min_sample key."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(
        f"/api/admin/projects/{project.id}/direction-sr-breakdown"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["directions"] == []
    assert body["min_sample"] == 3


@pytest.mark.asyncio
async def test_direction_sr_breakdown_basic(client: AsyncClient, db_session):
    """Directions with >=3 qualifying tasks appear with correct catch-rate stats."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    ctx_hit = {
        "direction_id": "d-aaa",
        "_first_attempt_rejection_categories": ["correctness"],
        "_self_review_outcome": {"check_results": {"correctness": {"passed": False}}},
    }
    ctx_miss = {
        "direction_id": "d-aaa",
        "_first_attempt_rejection_categories": ["correctness"],
        "_self_review_outcome": {"check_results": {"correctness": {"passed": True}}},
    }
    for ctx in [ctx_hit, ctx_hit, ctx_miss]:
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context=ctx,
            )
        )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/direction-sr-breakdown"
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["directions"]) == 1
    entry = body["directions"][0]
    assert entry["direction_id"] == "d-aaa"
    assert entry["sample_size"] == 3
    assert entry["worst_category"] == "correctness"
    assert entry["overall_catch_rate"] == round(2 / 3, 3)
    cat = entry["categories"]["correctness"]
    assert cat["sample_count"] == 3
    assert cat["hit_rate"] == round(2 / 3, 3)
    assert cat["miss_rate"] == round(1 / 3, 3)


@pytest.mark.asyncio
async def test_direction_sr_breakdown_excludes_insufficient_sample(
    client: AsyncClient, db_session
):
    """Directions with fewer than 3 qualifying tasks are omitted."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "direction_id": "d-bbb",
                    "_first_attempt_rejection_categories": ["correctness"],
                    "_self_review_outcome": {
                        "check_results": {"correctness": {"passed": False}}
                    },
                },
            )
        )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/direction-sr-breakdown"
    )
    assert response.status_code == 200
    assert response.json()["directions"] == []


@pytest.mark.asyncio
async def test_direction_sr_breakdown_examples_finalized(
    client: AsyncClient, db_session
):
    """Miss examples flow through ``_finalize_examples`` (severity-sorted,
    deduped, capped). Old inline aggregation appended raw issues until cap=3
    and would have included the duplicate; the canonical path dedupes."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 3 qualifying tasks, all misses on "correctness". Each task has a
    # review_history with the same MAJOR description plus one CRITICAL
    # description. Old code would have produced a list with duplicates;
    # finalized output dedupes by cleaned text and surfaces CRITICAL first.
    rev_hist = [
        {
            "iteration": 0,
            "issues": [
                {"description": "off-by-one in the loop", "severity": "MAJOR"},
                {
                    "description": "race condition under concurrent calls",
                    "severity": "CRITICAL",
                },
            ],
        }
    ]
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "direction_id": "d-fin",
                    "_first_attempt_rejection_categories": ["correctness"],
                    "_self_review_outcome": {
                        "check_results": {"correctness": {"passed": True}}
                    },
                    "review_loop": {"review_history": rev_hist},
                },
            )
        )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/direction-sr-breakdown"
    )
    assert response.status_code == 200
    cat = response.json()["directions"][0]["categories"]["correctness"]
    examples = cat.get("examples") or []
    # Deduped: each unique description appears once even though 3 tasks
    # contributed it.
    descs = [ex["desc"] for ex in examples]
    assert len(descs) == len(set(descs))
    # CRITICAL is sorted before MAJOR.
    assert examples[0]["severity"] == "CRITICAL"


@pytest.mark.asyncio
async def test_direction_sr_breakdown_categories_sorted_worst_first(
    client: AsyncClient, db_session
):
    """Category dict is ordered worst-first by miss_rate, and worst_category
    matches the first key — proving the endpoint pulls ordering from the
    canonical sort rather than inferring from raw iteration order."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 3 tasks: correctness hit rate 1.0 (0 miss), error-handling hit rate 0.0
    # (3 miss). error-handling should come first.
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "direction_id": "d-sort",
                    "_first_attempt_rejection_categories": [
                        "correctness",
                        "error-handling",
                    ],
                    "_self_review_outcome": {
                        "check_results": {
                            "correctness": {"passed": False},
                            "error-handling": {"passed": True},
                        }
                    },
                },
            )
        )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/direction-sr-breakdown"
    )
    assert response.status_code == 200
    entry = response.json()["directions"][0]
    cats = entry["categories"]
    first_key = next(iter(cats))
    assert first_key == "error-handling"
    assert entry["worst_category"] == "error-handling"
    assert cats["error-handling"]["miss_rate"] == 1.0
    assert cats["correctness"]["miss_rate"] == 0.0


@pytest.mark.asyncio
async def test_direction_sr_breakdown_uses_stored_snapshot(
    client: AsyncClient, db_session
):
    """When sr_catch_rate_trends.by_direction is stored on the project, the endpoint
    uses it as the primary source — not the narrow COMPLETED-only task query.

    Proof: project has a stored snapshot for direction 'd-stored' but zero
    qualifying tasks in the DB. The endpoint must return the stored entry.
    """
    stored_by_dir = {
        "d-lower": {
            "sample_count": 5,
            "miss_rate": 0.2,
            "overall_catch_rate": 0.8,
            "categories": {
                "style-conventions": {
                    "hit_rate": 0.8,
                    "miss_rate": 0.2,
                    "sample_count": 5,
                }
            },
        },
        "d-stored": {
            "sample_count": 7,
            "miss_rate": 0.571,
            "overall_catch_rate": 0.429,
            "categories": {
                "scope-drift": {
                    "hit_rate": 0.8,
                    "miss_rate": 0.2,
                    "sample_count": 7,
                },
                "regression_risk": {
                    "hit_rate": 0.429,
                    "miss_rate": 0.571,
                    "sample_count": 7,
                },
            },
            "trend": "increasing",
            "previous_miss_rate": 0.4,
            "rate_history": [0.4, 0.571],
        },
    }
    project = make_project(
        directions=[{"id": "d-stored", "text": "Stored direction label"}],
        design_doc={"sr_catch_rate_trends": {"by_direction": stored_by_dir}},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(
        f"/api/admin/projects/{project.id}/direction-sr-breakdown"
    )
    assert response.status_code == 200
    body = response.json()
    assert [entry["direction_id"] for entry in body["directions"]] == [
        "d-stored",
        "d-lower",
    ]
    assert len(body["directions"]) == 2
    entry = body["directions"][0]
    assert entry["direction_id"] == "d-stored"
    assert entry["direction_text"] == "Stored direction label"
    assert entry["sample_size"] == 7
    assert entry["overall_catch_rate"] == 0.429
    assert entry["worst_category"] == "regression_risk"
    assert list(entry["categories"]) == ["regression_risk", "scope-drift"]
    assert entry["trend"] == "increasing"
    assert entry["previous_miss_rate"] == 0.4
    assert entry["rate_history"] == [0.4, 0.571]


@pytest.mark.asyncio
async def test_sr_missed_retrospectives_all_iterations_miss(
    client: AsyncClient, db_session
):
    """Late-iteration categories in the paginated backlog use all-iterations truth."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Late iter backlog task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_first_attempt_rejection_categories": {"correctness": 1},
                "_all_iterations_rejection_categories": {
                    "correctness": 1,
                    "architecture": 1,
                },
                "_self_review_outcome": {
                    "verdict": "pass",
                    "fixes_applied": False,
                    "check_results": {
                        "correctness": {"passed": False},
                        "architecture": {"passed": True},
                    },
                    "blind_spots": [],
                },
            },
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    row = body["items"][0]
    assert "architecture" in row["missed_categories"]
    assert "correctness" in row["caught_categories"]


@pytest.mark.asyncio
async def test_sr_missed_retrospectives_reviewer_origin_still_counts_as_miss(
    client: AsyncClient, db_session
):
    """Paginated backlog treats reviewer rescues as misses, not catches."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Reviewer rescue backlog",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_first_attempt_rejection_categories": {"correctness": 1},
                "_self_review_outcome": {
                    "verdict": "fail",
                    "fixes_applied": False,
                    "check_results": {
                        "correctness": {
                            "passed": False,
                            "origin": "reviewer_recovery_after_incomplete_sr",
                        }
                    },
                    "blind_spots": [],
                },
            },
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    row = body["items"][0]
    assert "correctness" in row["missed_categories"]
    assert "correctness" not in row["caught_categories"]


@pytest.mark.asyncio
async def test_sr_missed_retrospectives_total_not_capped_at_page_size(
    client: AsyncClient, db_session
):
    """Backlog total counts all matching tasks, not just the returned page."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add_all(
        [
            make_task(
                project_id=project.id,
                title=f"Backlog task {i}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_first_attempt_rejection_categories": {"correctness": 1},
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {"correctness": {"passed": True}},
                        "blind_spots": [],
                    },
                },
            )
            for i in range(205)
        ]
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives?page_size=3"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 205
    assert len(body["items"]) == 3


@pytest.mark.asyncio
async def test_sr_missed_retrospectives_task_types_returned(
    client: AsyncClient, db_session
):
    """task_types field lists all types in matched set, independent of task_type filter."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for ttype in ("create", "fix"):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Task {ttype}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": ttype}],
                context={
                    "_first_attempt_rejection_categories": {"correctness": 1},
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {"correctness": {"passed": True}},
                        "blind_spots": [],
                    },
                },
            )
        )
    await db_session.commit()

    # Without task_type filter: both types returned
    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives"
    )
    assert response.status_code == 200
    body = response.json()
    assert sorted(body["task_types"]) == ["create", "fix"]

    # With task_type filter: task_types still lists all types, total is filtered
    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives?task_type=create"
    )
    assert response.status_code == 200
    body = response.json()
    assert sorted(body["task_types"]) == ["create", "fix"]
    assert body["total"] == 1


@pytest.mark.asyncio
async def test_sr_missed_retrospectives_zero_based_pagination(
    client: AsyncClient, db_session
):
    """page parameter is 0-based: page=0 is the first page, page=1 skips the first batch."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add_all(
        [
            make_task(
                project_id=project.id,
                title=f"Paginate task {i}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_first_attempt_rejection_categories": {"correctness": 1},
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {"correctness": {"passed": True}},
                        "blind_spots": [],
                    },
                },
            )
            for i in range(5)
        ]
    )
    await db_session.commit()

    r0 = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives?page=0&page_size=3"
    )
    assert r0.status_code == 200
    body0 = r0.json()
    assert body0["total"] == 5
    assert len(body0["items"]) == 3

    r1 = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives?page=1&page_size=3"
    )
    assert r1.status_code == 200
    body1 = r1.json()
    assert body1["total"] == 5
    assert len(body1["items"]) == 2

    ids0 = {item["task_id"] for item in body0["items"]}
    ids1 = {item["task_id"] for item in body1["items"]}
    assert ids0.isdisjoint(ids1), (
        "page=0 and page=1 must return non-overlapping task sets"
    )


@pytest.mark.asyncio
async def test_sr_missed_retrospectives_category_counts_stable_across_pages(
    client: AsyncClient, db_session
):
    """category_counts is identical on every page and matches actual per-category totals."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 12 tasks missed 'correctness', 8 tasks missed 'architecture', 5 missed both
    for i in range(12):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Correctness-only task {i}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_first_attempt_rejection_categories": {"correctness": 1},
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {"correctness": {"passed": True}},
                        "blind_spots": [],
                    },
                },
            )
        )
    for i in range(8):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Architecture-only task {i}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={
                    "_first_attempt_rejection_categories": {"architecture": 1},
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {"architecture": {"passed": True}},
                        "blind_spots": [],
                    },
                },
            )
        )
    for i in range(5):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Both-category task {i}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_first_attempt_rejection_categories": {
                        "correctness": 1,
                        "architecture": 1,
                    },
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {
                            "correctness": {"passed": True},
                            "architecture": {"passed": True},
                        },
                        "blind_spots": [],
                    },
                },
            )
        )
    await db_session.commit()

    # Fetch page 0 and page 1 with small page_size to force multi-page
    r0 = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives?page=0&page_size=10"
    )
    assert r0.status_code == 200
    body0 = r0.json()

    r1 = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives?page=1&page_size=10"
    )
    assert r1.status_code == 200
    body1 = r1.json()

    # category_counts must be present on both pages
    assert "category_counts" in body0
    assert "category_counts" in body1

    # counts are identical across pages
    assert body0["category_counts"] == body1["category_counts"]

    # correctness: 12 solo + 5 both = 17; architecture: 8 solo + 5 both = 13
    assert body0["category_counts"]["correctness"] == 17
    assert body0["category_counts"]["architecture"] == 13

    # With task_type=create filter: only create tasks (12 correctness-only + 5 both)
    r_create = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives?task_type=create&page_size=5"
    )
    assert r_create.status_code == 200
    body_create = r_create.json()
    assert body_create["category_counts"]["correctness"] == 17
    assert body_create["category_counts"]["architecture"] == 5


@pytest.mark.asyncio
async def test_sr_missed_retrospectives_category_counts_invariant_to_category_filter(
    client: AsyncClient, db_session
):
    """category_counts reflects the full population regardless of the `category` filter.

    When a category chip is active, `items`/`total` must narrow to that
    category, but `category_counts` must stay byte-identical to the unfiltered
    response so the other chips keep showing their independent counts.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 12 correctness-only, 8 architecture-only, 5 both.
    for i in range(12):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Correctness-only task {i}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_first_attempt_rejection_categories": {"correctness": 1},
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {"correctness": {"passed": True}},
                        "blind_spots": [],
                    },
                },
            )
        )
    for i in range(8):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Architecture-only task {i}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={
                    "_first_attempt_rejection_categories": {"architecture": 1},
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {"architecture": {"passed": True}},
                        "blind_spots": [],
                    },
                },
            )
        )
    for i in range(5):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Both-category task {i}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_first_attempt_rejection_categories": {
                        "correctness": 1,
                        "architecture": 1,
                    },
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {
                            "correctness": {"passed": True},
                            "architecture": {"passed": True},
                        },
                        "blind_spots": [],
                    },
                },
            )
        )
    await db_session.commit()

    # Unfiltered: full population. correctness 12+5=17, architecture 8+5=13.
    r_all = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives?page_size=200"
    )
    assert r_all.status_code == 200
    body_all = r_all.json()
    assert body_all["category_counts"]["correctness"] == 17
    assert body_all["category_counts"]["architecture"] == 13
    # total = 12 + 8 + 5 distinct tasks = 25
    assert body_all["total"] == 25

    # Filtered to correctness: chip counts must be IDENTICAL to unfiltered.
    r_corr = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives?category=correctness&page_size=200"
    )
    assert r_corr.status_code == 200
    body_corr = r_corr.json()
    assert body_corr["category_counts"] == body_all["category_counts"]
    # ...but total / items narrow to only tasks that missed correctness (17).
    assert body_corr["total"] == 17
    assert len(body_corr["items"]) == 17
    assert all(
        "correctness" in item["missed_categories"] for item in body_corr["items"]
    )

    # Filtered to architecture: chip counts still identical to unfiltered.
    r_arch = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives?category=architecture&page_size=200"
    )
    assert r_arch.status_code == 200
    body_arch = r_arch.json()
    assert body_arch["category_counts"] == body_all["category_counts"]
    assert body_arch["total"] == 13
    assert len(body_arch["items"]) == 13
    assert all(
        "architecture" in item["missed_categories"] for item in body_arch["items"]
    )


@pytest.mark.asyncio
async def test_sr_missed_retrospectives_scope_filter(client: AsyncClient, db_session):
    """?scope=<value> filters items and rolls up total/category_counts honestly."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    def _ctx(scope: str, missed_cat: str = "correctness"):
        return {
            "scope": scope,
            "_first_attempt_rejection_categories": {missed_cat: 1},
            "_self_review_outcome": {
                "verdict": "pass",
                "fixes_applied": False,
                "check_results": {missed_cat: {"passed": True}},
                "blind_spots": [],
            },
        }

    db_session.add_all(
        [
            make_task(
                project_id=project.id,
                title="Small task A",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context=_ctx("small", "correctness"),
            ),
            make_task(
                project_id=project.id,
                title="Small task B",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context=_ctx("small", "performance"),
            ),
            make_task(
                project_id=project.id,
                title="Large task A",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context=_ctx("large", "correctness"),
            ),
        ]
    )
    await db_session.commit()

    # Unfiltered baseline
    r_all = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives"
    )
    assert r_all.status_code == 200
    body_all = r_all.json()
    assert body_all["total"] == 3
    assert body_all["category_counts"] == {"correctness": 2, "performance": 1}

    # Filter to scope=large — only the single large-scope task should match
    r_large = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives?scope=large"
    )
    assert r_large.status_code == 200
    body_large = r_large.json()
    assert body_large["total"] == 1
    scopes = {item["scope"] for item in body_large["items"]}
    assert scopes == {"large"}
    # category_counts must reflect ONLY the scope-filtered subset
    assert body_large["category_counts"] == {"correctness": 1}
    assert "performance" not in body_large["category_counts"]

    # Filter to scope=small — both small-scope tasks, both categories
    r_small = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives?scope=small"
    )
    assert r_small.status_code == 200
    body_small = r_small.json()
    assert body_small["total"] == 2
    assert body_small["category_counts"] == {"correctness": 1, "performance": 1}


@pytest.mark.asyncio
async def test_sr_unhandled_edges_task_types_returned(client: AsyncClient, db_session):
    """task_types field lists all types in matched set, independent of task_type filter."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for ttype in ("create", "fix"):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Task {ttype}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": ttype}],
                context={
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "edges_considered": [{"case": "empty input", "handled": False}],
                        "blind_spots": [],
                    },
                },
            )
        )
    await db_session.commit()

    # Without task_type filter: both types returned
    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-unhandled-edges-retrospectives"
    )
    assert response.status_code == 200
    body = response.json()
    assert sorted(body["task_types"]) == ["create", "fix"]

    # With task_type filter: task_types still lists all types, total is filtered
    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-unhandled-edges-retrospectives?task_type=create"
    )
    assert response.status_code == 200
    body = response.json()
    assert sorted(body["task_types"]) == ["create", "fix"]
    assert body["total"] == 1


@pytest.mark.asyncio
async def test_sr_unhandled_edges_total_not_capped_at_page_size(
    client: AsyncClient, db_session
):
    """Backlog total counts all matching tasks, not just the returned page."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add_all(
        [
            make_task(
                project_id=project.id,
                title=f"Edge task {i}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "edges_considered": [
                            {"case": "null pointer", "handled": False}
                        ],
                        "blind_spots": [],
                    },
                },
            )
            for i in range(205)
        ]
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-unhandled-edges-retrospectives?page_size=3"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 205
    assert len(body["items"]) == 3


@pytest.mark.asyncio
async def test_sr_unhandled_edges_blind_spot_counts(client: AsyncClient, db_session):
    """blind_spot_counts aggregates themes with first-6-token normalisation,
    dedupes per task, drops disallowed themes, and remains anchored to the
    days+task_type universe when the blind_spot filter narrows rows."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Task A: two unhandled edges, both blind-spots tokenize to the SAME
    # 6-token theme "no lock around state mutation that" — dedupe must count
    # this task as a single bump for that theme.
    db_session.add(
        make_task(
            project_id=project.id,
            title="task A",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_self_review_outcome": {
                    "edges_considered": [
                        {
                            "case": "race",
                            "handled": False,
                            "blind_spot": "No lock around state mutation that touches DB",
                        },
                        {
                            "case": "race-2",
                            "handled": False,
                            "blind_spot": "no LOCK around state mutation that runs concurrently",
                        },
                    ],
                },
            },
        )
    )
    # Task B: one unhandled edge with the SAME theme — second task bump.
    db_session.add(
        make_task(
            project_id=project.id,
            title="task B",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_self_review_outcome": {
                    "edges_considered": [
                        {
                            "case": "race",
                            "handled": False,
                            "blind_spot": "no lock around state mutation that breaks",
                        },
                    ],
                },
            },
        )
    )
    # Task C: distinct theme + a disallowed blind-spot that must be skipped +
    # an empty/whitespace blind-spot that must be skipped.
    db_session.add(
        make_task(
            project_id=project.id,
            title="task C",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "fix"}],
            context={
                "_self_review_outcome": {
                    "edges_considered": [
                        {
                            "case": "timeout",
                            "handled": False,
                            "blind_spot": "Partial retry on network timeout boundary",
                        },
                        {
                            "case": "skip",
                            "handled": False,
                            "blind_spot": "did not run the full backend test suite",
                        },
                        {
                            "case": "blank",
                            "handled": False,
                            "blind_spot": "   ",
                        },
                    ],
                },
            },
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-unhandled-edges-retrospectives"
    )
    assert response.status_code == 200
    body = response.json()
    counts = body["blind_spot_counts"]
    # Dedupe: A contributes 1, B contributes 1 → 2 for the shared theme.
    assert counts["no lock around state mutation that"] == 2
    # Distinct theme from task C.
    assert counts["partial retry on network timeout boundary"] == 1
    # Disallowed and empty entries excluded — only the two themes above.
    assert set(counts.keys()) == {
        "no lock around state mutation that",
        "partial retry on network timeout boundary",
    }

    # Filtering by blind_spot narrows items + total but counts stay anchored
    # to the (days + task_type) universe so pills remain swap-able.
    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-unhandled-edges-retrospectives"
        "?blind_spot=no+lock+around+state+mutation+that"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    titles = sorted(item["title"] for item in body["items"])
    assert titles == ["task A", "task B"]
    assert body["blind_spot_counts"] == counts

    # task_type filter narrows the counts universe.
    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-unhandled-edges-retrospectives?task_type=fix"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["blind_spot_counts"] == {
        "partial retry on network timeout boundary": 1,
    }


@pytest.mark.asyncio
async def test_sr_unhandled_edges_case_pattern_matches_exact_edge_case_label(
    client: AsyncClient, db_session
):
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add_all(
        [
            make_task(
                project_id=project.id,
                title="case match",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_self_review_outcome": {
                        "edges_considered": [
                            {
                                "case": "Retry path null pointer",
                                "handled": False,
                                "blind_spot": "missed null guard on cache write",
                            },
                        ],
                    },
                },
            ),
            make_task(
                project_id=project.id,
                title="case prefix match",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={
                    "_self_review_outcome": {
                        "edges_considered": [
                            {
                                "case": "network guard timeout fallback",
                                "handled": False,
                                "blind_spot": "Missing RETRY guard around network timeout fallback",
                            },
                        ],
                    },
                },
            ),
            make_task(
                project_id=project.id,
                title="non match",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={
                    "_self_review_outcome": {
                        "edges_considered": [
                            {
                                "case": "different issue",
                                "handled": False,
                                "blind_spot": "unrelated serialization gap",
                            },
                        ],
                    },
                },
            ),
        ]
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-unhandled-edges-retrospectives"
        "?case_pattern=retry+path+null+pointer"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert [item["title"] for item in body["items"]] == ["case match"]
    assert body["edge_case_counts"] == {
        "different issue": 1,
        "network guard timeout fallback": 1,
        "retry path null pointer": 1,
    }
    assert "blind_spot_counts" in body

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-unhandled-edges-retrospectives"
        "?case_pattern=network+guard+timeout+fallback"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert [item["title"] for item in body["items"]] == ["case prefix match"]
    assert body["edge_case_counts"] == {
        "different issue": 1,
        "network guard timeout fallback": 1,
        "retry path null pointer": 1,
    }

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-unhandled-edges-retrospectives"
        "?case_pattern=network+guard"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 0
    assert body["items"] == []
    assert body["edge_case_counts"] == {
        "different issue": 1,
        "network guard timeout fallback": 1,
        "retry path null pointer": 1,
    }


@pytest.mark.asyncio
async def test_sr_unhandled_edges_case_pattern_counts_stable_across_pages(
    client: AsyncClient, db_session
):
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add_all(
        [
            make_task(
                project_id=project.id,
                title="create retry task",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_self_review_outcome": {
                        "edges_considered": [
                            {
                                "case": "Retry path null pointer",
                                "handled": False,
                                "blind_spot": "Retry guard missing on write path cache",
                            },
                            {
                                "case": "timeout boundary",
                                "handled": False,
                                "blind_spot": "partial retry on timeout boundary",
                            },
                            {
                                "case": "serialization gap",
                                "handled": False,
                                "blind_spot": "missing JSON shape validation",
                            },
                        ],
                    },
                },
            ),
            make_task(
                project_id=project.id,
                title="fix retry task",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={
                    "_self_review_outcome": {
                        "edges_considered": [
                            {
                                "case": "Retry path null pointer",
                                "handled": False,
                                "blind_spot": "retry guard missing on write path db",
                            },
                        ],
                    },
                },
            ),
            make_task(
                project_id=project.id,
                title="non retry task",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={
                    "_self_review_outcome": {
                        "edges_considered": [
                            {
                                "case": "serialization gap",
                                "handled": False,
                                "blind_spot": "missing JSON shape validation",
                            },
                        ],
                    },
                },
            ),
            make_task(
                project_id=project.id,
                title="blind-spot sibling task",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={
                    "_self_review_outcome": {
                        "edges_considered": [
                            {
                                "case": "queue fallback",
                                "handled": False,
                                "blind_spot": "retry guard missing on write path queue",
                            },
                        ],
                    },
                },
            ),
        ]
    )
    await db_session.commit()

    response_full = await client.get(
        f"/api/admin/projects/{project.id}/sr-unhandled-edges-retrospectives"
        "?case_pattern=retry+path+null+pointer&page_size=2&page=0"
    )
    assert response_full.status_code == 200
    body_full = response_full.json()
    create_item = next(
        item for item in body_full["items"] if item["title"] == "create retry task"
    )
    assert create_item["unhandled_edges"] == [
        {
            "case": "Retry path null pointer",
            "handled": False,
            "blind_spot": "Retry guard missing on write path cache",
        },
        {
            "case": "timeout boundary",
            "handled": False,
            "blind_spot": "partial retry on timeout boundary",
        },
        {
            "case": "serialization gap",
            "handled": False,
            "blind_spot": "missing JSON shape validation",
        },
    ]

    response_page_1 = await client.get(
        f"/api/admin/projects/{project.id}/sr-unhandled-edges-retrospectives"
        "?case_pattern=retry+path+null+pointer&page_size=1&page=0"
    )
    assert response_page_1.status_code == 200
    body_page_1 = response_page_1.json()
    assert body_page_1["total"] == 2
    assert len(body_page_1["items"]) == 1
    assert sorted(body_page_1["task_types"]) == ["create", "fix"]
    assert body_page_1["blind_spot_counts"] == {
        "retry guard missing on write path": 2,
    }
    assert body_page_1["edge_case_counts"] == {
        "retry path null pointer": 2,
        "serialization gap": 2,
        "queue fallback": 1,
        "timeout boundary": 1,
    }

    response_page_2 = await client.get(
        f"/api/admin/projects/{project.id}/sr-unhandled-edges-retrospectives"
        "?case_pattern=retry+path+null+pointer&page_size=1&page=1"
    )
    assert response_page_2.status_code == 200
    body_page_2 = response_page_2.json()
    assert body_page_2["total"] == 2
    assert len(body_page_2["items"]) == 1
    assert body_page_2["edge_case_counts"] == body_page_1["edge_case_counts"]
    assert body_page_2["blind_spot_counts"] == body_page_1["blind_spot_counts"]

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-unhandled-edges-retrospectives"
        "?case_pattern=retry+path+null+pointer&blind_spot=retry+guard+missing+on+write+path"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["edge_case_counts"] == {
        "retry path null pointer": 2,
        "queue fallback": 1,
    }
    assert body["blind_spot_counts"] == body_page_1["blind_spot_counts"]
    create_item = next(
        item for item in body["items"] if item["title"] == "create retry task"
    )
    assert create_item["unhandled_edges"] == [
        {
            "case": "Retry path null pointer",
            "handled": False,
            "blind_spot": "Retry guard missing on write path cache",
        },
        {
            "case": "timeout boundary",
            "handled": False,
            "blind_spot": "partial retry on timeout boundary",
        },
        {
            "case": "serialization gap",
            "handled": False,
            "blind_spot": "missing JSON shape validation",
        },
    ]


@pytest.mark.asyncio
async def test_sr_unhandled_edges_case_pattern_dedupes_duplicate_labels_per_task(
    client: AsyncClient, db_session
):
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add_all(
        [
            make_task(
                project_id=project.id,
                title="duplicate label task",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_self_review_outcome": {
                        "edges_considered": [
                            {
                                "case": "Retry path null pointer",
                                "handled": False,
                                "blind_spot": "retry guard missing on cache write",
                            },
                            {
                                "case": "Retry path null pointer",
                                "handled": False,
                                "blind_spot": "retry guard missing on db write",
                            },
                        ],
                    },
                },
            ),
            make_task(
                project_id=project.id,
                title="single label task",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={
                    "_self_review_outcome": {
                        "edges_considered": [
                            {
                                "case": "Retry path null pointer",
                                "handled": False,
                                "blind_spot": "retry guard missing on queue write",
                            },
                        ],
                    },
                },
            ),
        ]
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-unhandled-edges-retrospectives"
        "?case_pattern=retry+path+null+pointer"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["edge_case_counts"] == {"retry path null pointer": 2}


@pytest.mark.asyncio
async def test_sr_recent_misses_empty(client: AsyncClient, db_session):
    """No tasks → endpoint returns empty list."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/projects/{project.id}/sr-recent-misses")
    assert response.status_code == 200
    body = response.json()
    assert body["misses"] == []
    assert body["total"] == 0


@pytest.mark.asyncio
async def test_sr_recent_misses_basic(client: AsyncClient, db_session):
    """Tasks where SR missed a human-caught category appear in the response."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Task where SR passed "correctness" but human rejected for it → miss
    task = make_task(
        project_id=project.id,
        title="Add feature X",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        context={
            "scope": "small",
            "_first_attempt_rejection_categories": ["correctness", "error-handling"],
            "_self_review_outcome": {
                "verdict": "pass",
                "fixes_applied": False,
                "check_results": {
                    "correctness": {"passed": True},
                    "error-handling": {"passed": False},
                },
                "blind_spots": [],
            },
        },
    )
    db_session.add(task)
    await db_session.commit()

    response = await client.get(f"/api/admin/projects/{project.id}/sr-recent-misses")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    row = body["misses"][0]
    assert row["title"] == "Add feature X"
    assert row["scope"] == "small"
    assert row["task_type"] == "create"
    # correctness was missed (SR passed it, human flagged it)
    assert "correctness" in row["missed_categories"]
    # error-handling was caught (SR failed it, human flagged it)
    assert "error-handling" in row["caught_categories"]
    assert "correctness" not in row["caught_categories"]


@pytest.mark.asyncio
async def test_sr_recent_misses_reviewer_origin_still_counts_as_miss(
    client: AsyncClient, db_session
):
    """Reviewer-stamped passed=False does not count as an author catch."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Reviewer rescue",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_first_attempt_rejection_categories": ["correctness"],
                "_self_review_outcome": {
                    "verdict": "fail",
                    "check_results": {
                        "correctness": {
                            "passed": False,
                            "issue": "reviewer found it",
                            "origin": "reviewer",
                        }
                    },
                    "blind_spots": [],
                },
            },
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/projects/{project.id}/sr-recent-misses")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    row = body["misses"][0]
    assert "correctness" in row["missed_categories"]
    assert "correctness" not in row["caught_categories"]


@pytest.mark.asyncio
async def test_sr_recent_misses_excludes_fully_caught(client: AsyncClient, db_session):
    """Tasks where SR caught all human-flagged categories are excluded."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # SR failed "correctness" AND human flagged "correctness" → no miss
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "fix"}],
            context={
                "_first_attempt_rejection_categories": ["correctness"],
                "_self_review_outcome": {
                    "verdict": "fail",
                    "fixes_applied": True,
                    "check_results": {"correctness": {"passed": False}},
                    "blind_spots": [],
                },
            },
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/projects/{project.id}/sr-recent-misses")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 0
    assert body["misses"] == []


@pytest.mark.asyncio
async def test_sr_recent_misses_excludes_tasks_without_sr_outcome(
    client: AsyncClient, db_session
):
    """Tasks missing _self_review_outcome or _first_attempt_rejection_categories are skipped."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # No SR outcome
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={"_first_attempt_rejection_categories": ["correctness"]},
        )
    )
    # No rejection categories
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={"_self_review_outcome": _sr_valid_outcome()},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/projects/{project.id}/sr-recent-misses")
    assert response.status_code == 200
    assert response.json()["total"] == 0


@pytest.mark.asyncio
async def test_sr_recent_misses_category_filter(client: AsyncClient, db_session):
    """The ?category= filter returns only rows that missed that specific category."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Task 1: missed "correctness"
    db_session.add(
        make_task(
            project_id=project.id,
            title="Task A",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_first_attempt_rejection_categories": ["correctness"],
                "_self_review_outcome": {
                    "verdict": "pass",
                    "fixes_applied": False,
                    "check_results": {"correctness": {"passed": True}},
                    "blind_spots": [],
                },
            },
        )
    )
    # Task 2: missed "security"
    db_session.add(
        make_task(
            project_id=project.id,
            title="Task B",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "fix"}],
            context={
                "_first_attempt_rejection_categories": ["security"],
                "_self_review_outcome": {
                    "verdict": "pass",
                    "fixes_applied": False,
                    "check_results": {"security": {"passed": True}},
                    "blind_spots": [],
                },
            },
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-recent-misses?category=correctness"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["misses"][0]["title"] == "Task A"


@pytest.mark.asyncio
async def test_sr_recent_misses_limit_param(client: AsyncClient, db_session):
    """The ?limit= parameter caps the number of returned rows."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for i in range(5):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Task {i}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_first_attempt_rejection_categories": ["correctness"],
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {"correctness": {"passed": True}},
                        "blind_spots": [],
                    },
                },
            )
        )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-recent-misses?limit=3"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 5
    assert len(body["misses"]) == 3


@pytest.mark.asyncio
async def test_sr_recent_misses_project_isolation(client: AsyncClient, db_session):
    """Tasks from other projects are not included in the response."""
    project_a = make_project()
    project_b = make_project()
    db_session.add(project_a)
    db_session.add(project_b)
    await db_session.commit()
    await db_session.refresh(project_a)
    await db_session.refresh(project_b)

    miss_ctx = {
        "_first_attempt_rejection_categories": ["correctness"],
        "_self_review_outcome": {
            "verdict": "pass",
            "fixes_applied": False,
            "check_results": {"correctness": {"passed": True}},
            "blind_spots": [],
        },
    }
    db_session.add(
        make_task(
            project_id=project_a.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context=miss_ctx,
        )
    )
    db_session.add(
        make_task(
            project_id=project_b.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context=miss_ctx,
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/projects/{project_a.id}/sr-recent-misses")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1


@pytest.mark.asyncio
async def test_sr_recent_misses_all_iterations_miss(client: AsyncClient, db_session):
    """A category only in _all_iterations_rejection_categories (not first-attempt) is detected."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Late-iteration category "architecture" is absent from first-attempt but present
    # in _all_iterations_rejection_categories.  SR passed it → miss.
    db_session.add(
        make_task(
            project_id=project.id,
            title="Late iter task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_first_attempt_rejection_categories": {"correctness": 1},
                "_all_iterations_rejection_categories": {
                    "correctness": 1,
                    "architecture": 1,
                },
                "_self_review_outcome": {
                    "verdict": "pass",
                    "fixes_applied": False,
                    "check_results": {
                        "correctness": {"passed": False},
                        "architecture": {"passed": True},
                    },
                    "blind_spots": [],
                },
            },
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/projects/{project.id}/sr-recent-misses")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    row = body["misses"][0]
    assert "architecture" in row["missed_categories"]
    assert "correctness" in row["caught_categories"]


@pytest.mark.asyncio
async def test_sr_recent_misses_total_before_limit(client: AsyncClient, db_session):
    """total reflects all matching tasks, not just the capped page."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for i in range(7):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Task {i}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_first_attempt_rejection_categories": {"correctness": 1},
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {"correctness": {"passed": True}},
                        "blind_spots": [],
                    },
                },
            )
        )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-recent-misses?limit=3"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 7
    assert len(body["misses"]) == 3


@pytest.mark.asyncio
async def test_sr_recent_misses_total_not_capped_at_scan_limit(
    client: AsyncClient, db_session
):
    """total keeps counting beyond the old 2000-row scan cap."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add_all(
        [
            make_task(
                project_id=project.id,
                title=f"Task {i}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_first_attempt_rejection_categories": {"correctness": 1},
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {"correctness": {"passed": True}},
                        "blind_spots": [],
                    },
                },
            )
            for i in range(2005)
        ]
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-recent-misses?limit=3"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2005
    assert len(body["misses"]) == 3


@pytest.mark.asyncio
async def test_sr_recent_misses_fallback_first_attempt(client: AsyncClient, db_session):
    """When _all_iterations is absent, first-attempt categories are used and review history alone is ignored."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Old task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "fix"}],
            context={
                "_first_attempt_rejection_categories": {"security": 1},
                "review_loop": {
                    "review_history": [
                        {
                            "iteration": 2,
                            "issues": [
                                {
                                    "description": "architecture issue",
                                    "severity": "MAJOR",
                                }
                            ],
                        }
                    ]
                },
                "_self_review_outcome": {
                    "verdict": "pass",
                    "fixes_applied": False,
                    "check_results": {"security": {"passed": True}},
                    "blind_spots": [],
                },
            },
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/projects/{project.id}/sr-recent-misses")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert "security" in body["misses"][0]["missed_categories"]
    assert "architecture" not in body["misses"][0]["missed_categories"]


@pytest.mark.asyncio
async def test_sr_alert_drilldown_returns_missed_tasks(client: AsyncClient, db_session):
    """Tasks where SR passed but human rejected on target category appear in drilldown."""
    project = make_project(
        design_doc={
            "sr_catch_rate_trends_alert": {
                "computed_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                "categories": ["correctness"],
            }
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        context={
            "_self_review_outcome": {
                "verdict": "pass",
                "check_results": {"correctness": {"passed": True}},
            },
            "_first_attempt_rejection_categories": ["correctness"],
        },
    )
    db_session.add(task)
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-alert-drilldown",
        params={"categories": "correctness", "alert_type": "trends"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["alert_type"] == "trends"
    assert data["categories"] == ["correctness"]
    item = data["items"][0]
    assert item["task_id"] == str(task.id)
    assert "correctness" in item["missed_categories"]


@pytest.mark.asyncio
async def test_sr_alert_drilldown_empty_when_no_alert(client: AsyncClient, db_session):
    """When no alert is active the window falls back to 14 days; no matching tasks → empty."""
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-alert-drilldown",
        params={"categories": "correctness"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0
    assert data["items"] == []


@pytest.mark.asyncio
async def test_sr_alert_drilldown_invalid_alert_type(client: AsyncClient, db_session):
    """alert_type values other than 'trends'/'floor' return 400."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-alert-drilldown",
        params={"categories": "correctness", "alert_type": "invalid"},
    )
    assert response.status_code == 400


def _sr_outcome(fixes_applied: bool) -> dict:
    return {
        "verdict": "pass",
        "fixes_applied": fixes_applied,
        "check_results": {},
        "blind_spots": [],
    }


@pytest.mark.asyncio
async def test_digest_self_review_effectiveness_includes_clean_and_fixed_counts(
    client: AsyncClient, db_session
):
    """sr_fixed_count and sr_clean_count are present and equal the integer counts of
    the fixed vs clean subsets of tasks_with_sr."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    # 2 fixed, 3 clean → sample_size=5
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
                payloads=[{"type": "fix"}],
                context={
                    "_first_attempt_succeeded": True,
                    "_self_review_outcome": _sr_outcome(fixes_applied=True),
                },
            )
        )
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
                payloads=[{"type": "fix"}],
                context={
                    "_first_attempt_succeeded": True,
                    "_self_review_outcome": _sr_outcome(fixes_applied=False),
                },
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    sre = response.json()["self_review_effectiveness"]
    assert sre is not None
    assert sre["sample_size"] == 5
    assert sre["sr_fixed_count"] == 2
    assert sre["sr_clean_count"] == 3
    # Existing fields are unchanged.
    assert "with_self_review_approval_rate" in sre
    assert "fixed_verdict_approval_rate" in sre
    assert "clean_verdict_approval_rate" in sre


@pytest.mark.asyncio
async def test_digest_self_review_effectiveness_below_threshold_is_none(
    client: AsyncClient, db_session
):
    """When tasks_with_sr is below the existing threshold (3), the entire
    self_review_effectiveness block remains None — the new count fields do not
    cause the block to render at smaller sample sizes."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    # 2 tasks with SR — below the threshold of 3.
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
                payloads=[{"type": "fix"}],
                context={
                    "_first_attempt_succeeded": True,
                    "_self_review_outcome": _sr_outcome(fixes_applied=False),
                },
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    assert response.json()["self_review_effectiveness"] is None


def _layer_entry(fingerprint: str, observed_at: datetime, sample: str = "") -> dict:
    return {
        "fingerprint": fingerprint,
        "observed_at": observed_at.isoformat(),
        "sample": sample,
    }


def _make_regression_case(regression_class: str, fix_log=None, symptoms=None):
    """Lightweight stub — only the fields accessed by the coverage helpers."""
    import types

    case = types.SimpleNamespace(
        regression_class=regression_class,
        fingerprint_to_fix_log=fix_log or [],
        symptoms_by_layer=symptoms or {},
    )
    return case


def _cross_layer_entry(layers_targeted=None, spawn_layers=None, days_ago=1):
    now = _utcnow()
    entry = {
        "fingerprint": "test_class",
        "fix_pr_number": 100,
        "merged_at": (now - timedelta(days=days_ago)).isoformat(),
        "spawn_was_cross_layer": True,
        "spawn_layers_enumerated": spawn_layers or ["CI_WORKFLOW", "AGENT_VM"],
    }
    if layers_targeted is not None:
        entry["layers_targeted"] = layers_targeted
    return entry


@pytest.mark.asyncio
async def test_evaluate_sr_catch_rate_targets_fires_after_2_windows(db_session):
    """Alert fires when >=2 consecutive windows are below the configured target."""
    from app.routers.admin_digest import _evaluate_sr_catch_rate_targets
    from app.models import Alert, AlertSeverity

    project = make_project()
    project.sr_catch_rate_targets = {"correctness": 0.80}
    project.design_doc = {
        "sr_catch_rate_trends": {
            "categories": {
                "correctness": {
                    "current_hit_rate": 0.60,
                    "current_reviewer_finding_count": 15,
                    "current_human_count": 15,
                    "rate_history": [0.62, 0.60],
                    "trend": "decreasing",
                }
            }
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _evaluate_sr_catch_rate_targets(db_session, project.id)

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.severity == AlertSeverity.WARNING,
                    Alert.title == "SR catch-rate below target: correctness",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "0.80" in alerts[0].message or "80%" in alerts[0].message
    assert "n=15" in alerts[0].message


@pytest.mark.asyncio
async def test_evaluate_sr_catch_rate_targets_suppressed_on_low_findings(db_session):
    """Alert is NOT emitted when current_human_count is below the minimum finding floor."""
    from app.routers.admin_digest import _evaluate_sr_catch_rate_targets
    from app.models import Alert

    project = make_project()
    project.sr_catch_rate_targets = {"correctness": 0.80}
    project.design_doc = {
        "sr_catch_rate_trends": {
            "categories": {
                "correctness": {
                    "current_hit_rate": 0.50,
                    "current_reviewer_finding_count": 4,
                    "current_human_count": 4,  # below _SR_CATCH_RATE_MIN_FINDINGS=10
                    "rate_history": [0.52, 0.50],
                    "trend": "decreasing",
                }
            }
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _evaluate_sr_catch_rate_targets(db_session, project.id)

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.title == "SR catch-rate below target: correctness",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_evaluate_sr_catch_rate_targets_suppressed_on_1_window(db_session):
    """Alert is NOT emitted when only 1 window is below target."""
    from app.routers.admin_digest import _evaluate_sr_catch_rate_targets
    from app.models import Alert

    project = make_project()
    project.sr_catch_rate_targets = {"security": 0.75}
    project.design_doc = {
        "sr_catch_rate_trends": {
            "categories": {
                "security": {
                    "current_hit_rate": 0.60,
                    "rate_history": [0.60],
                    "trend": "stable",
                }
            }
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _evaluate_sr_catch_rate_targets(db_session, project.id)

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.title == "SR catch-rate below target: security",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_evaluate_sr_catch_rate_targets_resolves_on_recovery(db_session):
    """Existing alert is dismissed when category recovers above target."""
    from app.routers.admin_digest import _evaluate_sr_catch_rate_targets
    from app.models import Alert, AlertSeverity

    project = make_project()
    project.sr_catch_rate_targets = {"regression_risk": 0.70}
    project.design_doc = {
        "sr_catch_rate_trends": {
            "categories": {
                "regression_risk": {
                    "current_hit_rate": 0.85,
                    "rate_history": [0.85],
                    "trend": "increasing",
                }
            }
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Seed an existing undismissed alert.
    existing_alert = Alert(
        project_id=project.id,
        severity=AlertSeverity.WARNING,
        title="SR catch-rate below target: regression_risk",
        message="old alert",
    )
    db_session.add(existing_alert)
    await db_session.commit()
    await db_session.refresh(existing_alert)

    await _evaluate_sr_catch_rate_targets(db_session, project.id)

    await db_session.refresh(existing_alert)
    assert existing_alert.dismissed_at is not None


@pytest.mark.asyncio
async def test_evaluate_sr_catch_rate_targets_no_fire_when_unset(db_session):
    """No alert fires when sr_catch_rate_targets is empty."""
    from app.routers.admin_digest import _evaluate_sr_catch_rate_targets
    from app.models import Alert

    project = make_project()
    project.sr_catch_rate_targets = {}
    project.design_doc = {
        "sr_catch_rate_trends": {
            "categories": {
                "correctness": {
                    "current_hit_rate": 0.30,
                    "rate_history": [0.30, 0.35],
                    "trend": "stable",
                }
            }
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _evaluate_sr_catch_rate_targets(db_session, project.id)

    count = len(
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert count == 0


@pytest.mark.asyncio
async def test_evaluate_sr_catch_rate_targets_prompt_version_changed_suppression(
    db_session,
):
    """Alert is suppressed when prompt_version_changed is True."""
    from app.routers.admin_digest import _evaluate_sr_catch_rate_targets
    from app.models import Alert

    project = make_project()
    project.sr_catch_rate_targets = {"correctness": 0.80}
    project.design_doc = {
        "sr_catch_rate_trends": {
            "prompt_version_changed": True,
            "categories": {
                "correctness": {
                    "current_hit_rate": 0.50,
                    "rate_history": [0.50, 0.48],
                    "trend": "decreasing",
                }
            },
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _evaluate_sr_catch_rate_targets(db_session, project.id)

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_evaluate_lone_checker_alert_fires_on_2_lone_checker_categories(
    db_session,
):
    """Alert fires when >=2 categories have time_to_miss_detect_lone_checker=True."""
    from app.routers.admin_digest import _evaluate_lone_checker_alert
    from app.models import Alert, AlertSeverity

    project = make_project()
    project.design_doc = {
        "sr_catch_rate_trends": {
            "categories": {
                "security": {"time_to_miss_detect_lone_checker": True},
                "regression_risk": {"time_to_miss_detect_lone_checker": True},
                "correctness": {"time_to_miss_detect_lone_checker": False},
            }
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _evaluate_lone_checker_alert(db_session, project.id)

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.WARNING
    assert "lone-checker" in alerts[0].title.lower()
    assert "security" in alerts[0].message
    assert "regression_risk" in alerts[0].message


@pytest.mark.asyncio
async def test_evaluate_lone_checker_alert_no_fire_on_1_category(db_session):
    """Alert does not fire when only one category is in lone-checker state."""
    from app.routers.admin_digest import _evaluate_lone_checker_alert
    from app.models import Alert

    project = make_project()
    project.design_doc = {
        "sr_catch_rate_trends": {
            "categories": {
                "security": {"time_to_miss_detect_lone_checker": True},
                "correctness": {"time_to_miss_detect_lone_checker": False},
            }
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _evaluate_lone_checker_alert(db_session, project.id)

    count = len(
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert count == 0


@pytest.mark.asyncio
async def test_evaluate_lone_checker_alert_dedup(db_session):
    """Alert is not duplicated if an open alert with the same title already exists."""
    from app.routers.admin_digest import (
        _evaluate_lone_checker_alert,
        _LONE_CHECKER_ALERT_TITLE,
    )
    from app.models import Alert, AlertSeverity

    project = make_project()
    project.design_doc = {
        "sr_catch_rate_trends": {
            "categories": {
                "security": {"time_to_miss_detect_lone_checker": True},
                "regression_risk": {"time_to_miss_detect_lone_checker": True},
            }
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    existing = Alert(
        project_id=project.id,
        severity=AlertSeverity.WARNING,
        title=_LONE_CHECKER_ALERT_TITLE,
        message="existing",
    )
    db_session.add(existing)
    await db_session.commit()

    await _evaluate_lone_checker_alert(db_session, project.id)

    all_alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(all_alerts) == 1
