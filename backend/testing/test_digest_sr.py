"""Tests for digest self-review / SR analytics (app.sr_*)."""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlmodel import select

from app.models import (
    TaskStatus,
)
from app.sr_blind_spots import aggregate_sr_reviewer_disagreements
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
async def test_digest_fresh_reviewer_effectiveness_empty(client: AsyncClient):
    """No tasks → fresh_reviewer_effectiveness present with zero sample_size and null rates."""
    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    fre = response.json()["fresh_reviewer_effectiveness"]
    assert fre is not None
    assert fre["fresh_reviewer_sample_size"] == 0
    assert fre["fresh_reviewer_first_attempt_approval_rate"] is None
    assert fre["baseline_first_attempt_approval_rate"] is None
    assert fre["per_category_hit_rate"] == {}
    assert fre["reviewer_finding_added_value"] == {}
    assert fre["prediction_sample_size"] == 0


@pytest.mark.asyncio
async def test_digest_fresh_reviewer_effectiveness_approval_rate_lift(
    client: AsyncClient, db_session
):
    """Tasks with reviewer_findings have a separate approval rate from those without."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 2 tasks with fresh-reviewer findings: both approved on first attempt
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_first_attempt_succeeded": True,
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "check_results": {},
                        "blind_spots": [],
                        "reviewer_findings": [
                            {
                                "category": "correctness",
                                "severity": "HIGH",
                                "description": "x",
                            }
                        ],
                    },
                },
            )
        )
    # 2 tasks without fresh-reviewer: both rejected on first attempt
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={
                    "_first_attempt_succeeded": False,
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "check_results": {},
                        "blind_spots": [],
                    },
                },
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    fre = response.json()["fresh_reviewer_effectiveness"]
    assert fre["fresh_reviewer_sample_size"] == 2
    assert fre["fresh_reviewer_first_attempt_approval_rate"] == 1.0
    assert fre["baseline_first_attempt_approval_rate"] == 0.0


@pytest.mark.asyncio
async def test_digest_fresh_reviewer_effectiveness_per_category_hit_rate(
    client: AsyncClient, db_session
):
    """per_category_hit_rate reports confirmed fraction; reviewer_finding_added_value
    counts categories fresh-reviewer caught that the agent missed and human confirmed."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 3 tasks where fresh-reviewer flagged "correctness" and human confirmed it too.
    # Agent self-review passed correctness (so it counts as added value).
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_first_attempt_succeeded": False,
                    "_first_attempt_rejection_categories": ["correctness"],
                    "_self_review_outcome": {
                        "verdict": "fail",
                        "check_results": {"correctness": {"passed": True}},
                        "blind_spots": [],
                        "reviewer_findings": [
                            {
                                "category": "correctness",
                                "severity": "CRITICAL",
                                "description": "y",
                            }
                        ],
                    },
                },
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    fre = response.json()["fresh_reviewer_effectiveness"]
    assert fre["fresh_reviewer_sample_size"] == 3
    assert fre["prediction_sample_size"] == 3
    # 3/3 correctness findings confirmed → hit rate 1.0
    assert fre["per_category_hit_rate"]["correctness"] == 1.0
    # agent passed correctness, reviewer flagged it, human confirmed → added value
    assert fre["reviewer_finding_added_value"].get("correctness", 0) == 3


@pytest.mark.asyncio
async def test_digest_fresh_reviewer_effectiveness_excludes_reviewer_origin_catches(
    client: AsyncClient, db_session
):
    """Reviewer-stamped passed=False entries still count as reviewer added value."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_first_attempt_succeeded": False,
                    "_first_attempt_rejection_categories": ["correctness"],
                    "_self_review_outcome": {
                        "verdict": "fail",
                        "check_results": {
                            "correctness": {
                                "passed": False,
                                "origin": "reviewer_disagreement",
                            }
                        },
                        "blind_spots": [],
                        "reviewer_findings": [
                            {
                                "category": "correctness",
                                "severity": "CRITICAL",
                                "description": "caught by reviewer",
                            }
                        ],
                    },
                },
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    fre = response.json()["fresh_reviewer_effectiveness"]
    assert fre["fresh_reviewer_sample_size"] == 3
    assert fre["prediction_sample_size"] == 3
    assert fre["per_category_hit_rate"]["correctness"] == 1.0
    assert fre["reviewer_finding_added_value"].get("correctness", 0) == 3


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
async def test_compute_sr_blind_spot_themes_stamps_design_doc(db_session):
    """compute_sr_blind_spot_themes stamps sr_blind_spot_signal_last_computed_at."""
    from app.sr_blind_spots import compute_sr_blind_spot_themes

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    assert (project.design_doc or {}).get(
        "sr_blind_spot_signal_last_computed_at"
    ) is None

    fixed_now = _utcnow().replace(microsecond=0)
    await compute_sr_blind_spot_themes(db_session, project.id, now=fixed_now)
    await db_session.commit()
    await db_session.refresh(project)

    assert (
        project.design_doc["sr_blind_spot_signal_last_computed_at"]
        == fixed_now.isoformat()
    )


def _make_rr_task(project_id, *, sr_passed, test_attempts, task_type="create"):
    """Build a Task-like object with SR regression_risk + fix_loop_history.test."""
    check_results: dict = {}
    if sr_passed is not None:
        check_results["regression_risk"] = {"passed": sr_passed}
    history: dict = {}
    if test_attempts is not None:
        history["test"] = test_attempts
    return make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": task_type, "prompt": "x"}],
        context={"_self_review_outcome": {"check_results": check_results}},
        fix_loop_history=history,
    )


def test_aggregate_sr_regression_risk_test_signal_below_min_sample_returns_none():
    """Fewer than _SR_MIN_SAMPLE qualifying tasks → None."""
    from app.sr_blind_spots import aggregate_sr_regression_risk_test_signal
    import uuid

    pid = uuid.uuid4()
    tasks = [_make_rr_task(pid, sr_passed=True, test_attempts=[]) for _ in range(4)]
    assert aggregate_sr_regression_risk_test_signal(tasks) is None


def test_aggregate_sr_regression_risk_test_signal_passed_no_signal_is_not_a_miss():
    """SR passed + no test loop activity → true-pass, miss_by_test_signal_rate == 0."""
    from app.sr_blind_spots import aggregate_sr_regression_risk_test_signal
    import uuid

    pid = uuid.uuid4()
    tasks = [_make_rr_task(pid, sr_passed=True, test_attempts=[]) for _ in range(5)]
    result = aggregate_sr_regression_risk_test_signal(tasks)
    assert result is not None
    assert result["sr_passed_no_signal"] == 5
    assert result["sr_passed_with_signal"] == 0
    assert result["miss_by_test_signal_rate"] == 0.0
    assert result["numerator"] == 0
    assert result["denominator"] == 5


def test_aggregate_sr_regression_risk_test_signal_passed_with_testfix_counts_as_miss():
    """SR passed + testFix loop ran ≥1 non-trivial iteration → miss-by-test-signal."""
    from app.sr_blind_spots import aggregate_sr_regression_risk_test_signal
    import uuid

    pid = uuid.uuid4()
    miss_attempts = [
        {"iteration": 1, "exit_reason": "in_progress"},
        {"iteration": 2, "exit_reason": "passed"},
    ]
    tasks = [
        _make_rr_task(pid, sr_passed=True, test_attempts=miss_attempts)
        for _ in range(3)
    ] + [_make_rr_task(pid, sr_passed=True, test_attempts=[]) for _ in range(2)]
    result = aggregate_sr_regression_risk_test_signal(tasks)
    assert result is not None
    assert result["sr_passed_with_signal"] == 3
    assert result["sr_passed_no_signal"] == 2
    assert result["miss_by_test_signal_rate"] == round(3 / 5, 3)


def test_aggregate_sr_regression_risk_test_signal_failed_with_signal_counts_as_hit():
    """SR failed regression_risk + testFix loop ran → hit-by-test-signal (not a miss)."""
    from app.sr_blind_spots import aggregate_sr_regression_risk_test_signal
    import uuid

    pid = uuid.uuid4()
    hit_attempts = [{"iteration": 1, "exit_reason": "divergence_detected"}]
    tasks = [
        _make_rr_task(pid, sr_passed=False, test_attempts=hit_attempts)
        for _ in range(5)
    ]
    result = aggregate_sr_regression_risk_test_signal(tasks)
    assert result is not None
    assert result["sr_failed_with_signal"] == 5
    assert result["sr_passed_with_signal"] == 0
    # No SR-passed tasks → denominator is 0
    assert result["denominator"] == 0
    assert result["miss_by_test_signal_rate"] is None


def _make_sd_task(project_id, *, sr_passed, expansion_ratio, task_type="create"):
    """Build a Task-like object with SR scope-drift check + expansion_ratio in context."""
    ctx = {
        "_self_review_outcome": {
            "check_results": {"scope-drift": {"passed": sr_passed}}
        },
        "expansion_ratio": expansion_ratio,
    }
    return make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": task_type, "prompt": "x"}],
        context=ctx,
    )


def test_aggregate_sr_scope_drift_vs_expansion_below_min_sample_returns_none():
    """Fewer than _SR_MIN_SAMPLE qualifying tasks → None."""
    from app.sr_blind_spots import aggregate_sr_scope_drift_vs_expansion
    import uuid

    pid = uuid.uuid4()
    tasks = [_make_sd_task(pid, sr_passed=True, expansion_ratio=1.0) for _ in range(4)]
    assert aggregate_sr_scope_drift_vs_expansion(tasks) is None


def test_aggregate_sr_scope_drift_vs_expansion_consistent_bucket():
    """SR passed + ratio ≤1.5 → consistent, miss rate == 0."""
    from app.sr_blind_spots import aggregate_sr_scope_drift_vs_expansion
    import uuid

    pid = uuid.uuid4()
    tasks = [_make_sd_task(pid, sr_passed=True, expansion_ratio=1.2) for _ in range(5)]
    result = aggregate_sr_scope_drift_vs_expansion(tasks)
    assert result is not None
    assert result["consistent"] == 5
    assert result["miss_by_expansion"] == 0
    assert result["miss_by_expansion_rate"] == 0.0
    assert result["numerator"] == 0
    assert result["denominator"] == 5


def test_aggregate_sr_scope_drift_vs_expansion_miss_bucket():
    """SR passed + ratio ≥2.0 → miss_by_expansion; rate = misses / (consistent+misses)."""
    from app.sr_blind_spots import aggregate_sr_scope_drift_vs_expansion
    import uuid

    pid = uuid.uuid4()
    tasks = [
        _make_sd_task(pid, sr_passed=True, expansion_ratio=2.5) for _ in range(3)
    ] + [_make_sd_task(pid, sr_passed=True, expansion_ratio=1.0) for _ in range(2)]
    result = aggregate_sr_scope_drift_vs_expansion(tasks)
    assert result is not None
    assert result["miss_by_expansion"] == 3
    assert result["consistent"] == 2
    assert result["miss_by_expansion_rate"] == round(3 / 5, 3)


def test_aggregate_sr_scope_drift_vs_expansion_hit_bucket():
    """SR failed scope-drift + ratio ≥1.5 → hit_by_expansion; denominator is 0."""
    from app.sr_blind_spots import aggregate_sr_scope_drift_vs_expansion
    import uuid

    pid = uuid.uuid4()
    tasks = [_make_sd_task(pid, sr_passed=False, expansion_ratio=2.0) for _ in range(5)]
    result = aggregate_sr_scope_drift_vs_expansion(tasks)
    assert result is not None
    assert result["hit_by_expansion"] == 5
    assert result["miss_by_expansion"] == 0
    assert result["denominator"] == 0
    assert result["miss_by_expansion_rate"] is None


def test_aggregate_sr_scope_drift_vs_expansion_fp_candidate_bucket():
    """SR failed scope-drift + ratio ≤1.0 → fp_candidate."""
    from app.sr_blind_spots import aggregate_sr_scope_drift_vs_expansion
    import uuid

    pid = uuid.uuid4()
    tasks = [_make_sd_task(pid, sr_passed=False, expansion_ratio=1.0) for _ in range(5)]
    result = aggregate_sr_scope_drift_vs_expansion(tasks)
    assert result is not None
    assert result["fp_candidate"] == 5
    assert result["hit_by_expansion"] == 0
    assert result["miss_by_expansion"] == 0
    assert result["miss_by_expansion_rate"] is None


def test_aggregate_sr_scope_drift_vs_expansion_skip_tasks_without_expansion_ratio():
    """Tasks without expansion_ratio in context are excluded from qualification."""
    from app.sr_blind_spots import aggregate_sr_scope_drift_vs_expansion
    import uuid

    pid = uuid.uuid4()
    ctx = {"_self_review_outcome": {"check_results": {"scope-drift": {"passed": True}}}}
    no_ratio_task = make_task(
        project_id=pid,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "x"}],
        context=ctx,
    )
    # 10 tasks but all lack expansion_ratio → fewer than min_sample qualify
    assert aggregate_sr_scope_drift_vs_expansion([no_ratio_task] * 10) is None


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
                        "reviewer_findings": [
                            {
                                "category": "security",
                                "severity": "HIGH",
                                "description": "found",
                            }
                        ],
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
    assert a["reviewer_rescue_sample_size"] == 1
    assert a["reviewer_rescue_rate"] == 1.0
    assert a["sr_false_positive_rate"] == 0.0
    disagreements = {
        entry["category"]: (
            entry["reviewer_overrode_sr_count"],
            entry["sr_overrode_reviewer_count"],
        )
        for entry in a["sr_reviewer_disagreement_top_categories"]
    }
    assert disagreements == {"correctness": (0, 1), "security": (1, 0)}

    b = by_fp["bbbbbbbbbbbb"]
    assert b["sample_size"] == 3
    assert b["sr_sample_size"] == 1
    assert b["sr_catch_rate"] is None
    assert b["reviewer_rescue_rate"] is None
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
async def test_sr_reviewer_disagreements_basic(db_session):
    """Counts disagreements where reviewer flagged a category self-review passed."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 3 create tasks: reviewer flags "correctness", self-review passed it
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "check_results": {"correctness": {"passed": True}},
                        "blind_spots": [],
                        "reviewer_findings": [
                            {
                                "category": "correctness",
                                "severity": "HIGH",
                                "description": "missed edge",
                            }
                        ],
                    }
                },
            )
        )
    # 1 fix task: reviewer flags "security", self-review omitted the category
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "fix"}],
            context={
                "_self_review_outcome": {
                    "verdict": "pass",
                    "check_results": {},
                    "blind_spots": [],
                    "reviewer_findings": [
                        {
                            "category": "security",
                            "severity": "CRITICAL",
                            "description": "unvalidated input",
                        }
                    ],
                }
            },
        )
    )
    # 1 task where self-review already FAILED the category — should NOT count as disagreement
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "fix"}],
            context={
                "_self_review_outcome": {
                    "verdict": "fail",
                    "check_results": {"correctness": {"passed": False}},
                    "blind_spots": [],
                    "reviewer_findings": [
                        {
                            "category": "correctness",
                            "severity": "HIGH",
                            "description": "logic bug",
                        }
                    ],
                }
            },
        )
    )
    await db_session.commit()

    results = await aggregate_sr_reviewer_disagreements(
        db_session,
        project.id,
        days=30,
        limit=10,
    )
    by_key = {(r["category"], r["task_type"]): r for r in results}
    assert by_key[("correctness", "create")]["count"] == 3
    assert by_key[("security", "fix")]["count"] == 1
    # Already-failed category must not appear
    assert ("correctness", "fix") not in by_key
    # Examples must include task_id for frontend drill-through
    correctness_entry = by_key[("correctness", "create")]
    assert len(correctness_entry["examples"]) > 0
    example = correctness_entry["examples"][0]
    assert "task_id" in example
    assert example["task_id"]  # non-empty UUID string
    assert example["task_type"] == "create"


@pytest.mark.asyncio
async def test_sr_reviewer_disagreements_skipped_excluded(db_session):
    """Skipped/fallback SR envelopes are excluded from disagreement counts."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_self_review_outcome": {
                    "verdict": "unknown",
                    "check_results": {},
                    "blind_spots": [],
                    "reviewer_findings": [
                        {
                            "category": "correctness",
                            "severity": "HIGH",
                            "description": "x",
                        }
                    ],
                }
            },
        )
    )
    await db_session.commit()

    results = await aggregate_sr_reviewer_disagreements(
        db_session,
        project.id,
        days=30,
        limit=10,
    )
    assert results == []


@pytest.mark.asyncio
async def test_sr_reviewer_disagreements_no_reviewer_findings_excluded(db_session):
    """Tasks without reviewer_findings are not counted."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_self_review_outcome": {
                    "verdict": "pass",
                    "check_results": {"correctness": {"passed": True}},
                    "blind_spots": [],
                }
            },
        )
    )
    await db_session.commit()

    results = await aggregate_sr_reviewer_disagreements(
        db_session,
        project.id,
        days=30,
        limit=10,
    )
    assert results == []


@pytest.mark.asyncio
async def test_sr_reviewer_disagreements_endpoint(client: AsyncClient, db_session):
    """GET sr-reviewer-disagreements returns envelope with window_days and disagreements list."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_self_review_outcome": {
                    "verdict": "pass",
                    "check_results": {"correctness": {"passed": True}},
                    "blind_spots": [],
                    "reviewer_findings": [
                        {
                            "category": "correctness",
                            "severity": "HIGH",
                            "description": "x",
                        }
                    ],
                }
            },
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-reviewer-disagreements?days=30"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["window_days"] == 30
    # Legacy field preserved (= returned slice size).
    assert body["total_entries"] == 1
    # Additive true-total / returned-count fields.
    assert body["total_distinct_categories"] == 1
    assert body["returned_entries"] == 1
    assert body["disagreements"][0]["category"] == "correctness"
    assert body["disagreements"][0]["task_type"] == "create"
    assert body["disagreements"][0]["count"] == 1


@pytest.mark.asyncio
async def test_sr_reviewer_disagreements_endpoint_not_truncated(
    client: AsyncClient, db_session
):
    """Single distinct category: true total equals returned-entry count (no truncation)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_self_review_outcome": {
                    "verdict": "pass",
                    "check_results": {"correctness": {"passed": True}},
                    "blind_spots": [],
                    "reviewer_findings": [
                        {
                            "category": "correctness",
                            "severity": "HIGH",
                            "description": "x",
                        }
                    ],
                }
            },
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-reviewer-disagreements?days=30"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total_distinct_categories"] == 1
    assert body["returned_entries"] == 1
    assert body["total_entries"] == 1
    assert len(body["disagreements"]) == 1


@pytest.mark.asyncio
async def test_sr_reviewer_disagreements_endpoint_truncated(
    client: AsyncClient, db_session
):
    """With >=2 distinct categories and limit=1, true total exceeds returned slice."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Two distinct (category, task_type) disagreements.
    for category in ("correctness", "security"):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "check_results": {category: {"passed": True}},
                        "blind_spots": [],
                        "reviewer_findings": [
                            {
                                "category": category,
                                "severity": "HIGH",
                                "description": "x",
                            }
                        ],
                    }
                },
            )
        )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-reviewer-disagreements?days=30&limit=1"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total_distinct_categories"] == 2
    assert body["returned_entries"] == 1
    # Truncated list reflects returned_entries, not the true total.
    assert len(body["disagreements"]) == 1
    # Legacy field tracks the returned slice (back-compat), NOT the true total.
    assert body["total_entries"] == 1
    assert body["total_distinct_categories"] > body["returned_entries"]


@pytest.mark.asyncio
async def test_sr_reviewer_disagreements_fp_fields(client: AsyncClient, db_session):
    """sr-reviewer-disagreements returns false_positive_count, true_positive_count, false_positive_rate."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # miss: SR passed correctness, reviewer flagged it
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_self_review_outcome": {
                    "verdict": "pass",
                    "check_results": {"correctness": {"passed": True}},
                    "blind_spots": [],
                    "reviewer_findings": [
                        {
                            "category": "correctness",
                            "severity": "HIGH",
                            "description": "x",
                        }
                    ],
                }
            },
        )
    )
    # fp: SR flagged correctness failed, reviewer didn't independently flag it
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_self_review_outcome": {
                    "verdict": "fail",
                    "check_results": {"correctness": {"passed": False}},
                    "blind_spots": [],
                    "reviewer_findings": [],
                }
            },
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-reviewer-disagreements?days=30"
    )
    assert response.status_code == 200
    body = response.json()
    d = next(
        e
        for e in body["disagreements"]
        if e["category"] == "correctness" and e["task_type"] == "create"
    )
    assert d["count"] == 1
    assert d["false_positive_count"] == 1
    assert d["true_positive_count"] == 0
    assert d["false_positive_rate"] == 1.0


@pytest.mark.asyncio
async def test_sr_reviewer_disagreement_tasks_basic(client: AsyncClient, db_session):
    """GET sr-reviewer-disagreements/tasks returns task rows for disagreements."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_self_review_outcome": {
                    "verdict": "pass",
                    "check_results": {"correctness": {"passed": True}},
                    "blind_spots": [],
                    "reviewer_findings": [
                        {
                            "category": "correctness",
                            "severity": "HIGH",
                            "description": "Logic flaw in edge case",
                        }
                    ],
                }
            },
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-reviewer-disagreements/tasks?days=30"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["task_type"] == "create"
    assert item["reviewer_findings"][0]["category"] == "correctness"
    assert body["category_counts"]["correctness"] == 1


@pytest.mark.asyncio
async def test_sr_reviewer_disagreement_tasks_category_filter(
    client: AsyncClient, db_session
):
    """category filter limits results to tasks with that disagreement category."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # task with correctness disagreement
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "fix"}],
            context={
                "_self_review_outcome": {
                    "verdict": "pass",
                    "check_results": {"correctness": {"passed": True}},
                    "blind_spots": [],
                    "reviewer_findings": [
                        {"category": "correctness", "severity": "HIGH", "description": "x"}
                    ],
                }
            },
        )
    )
    # task with security disagreement only
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "fix"}],
            context={
                "_self_review_outcome": {
                    "verdict": "pass",
                    "check_results": {"security": {"passed": True}},
                    "blind_spots": [],
                    "reviewer_findings": [
                        {"category": "security", "severity": "CRITICAL", "description": "y"}
                    ],
                }
            },
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-reviewer-disagreements/tasks?days=30&category=correctness"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["reviewer_findings"][0]["category"] == "correctness"


@pytest.mark.asyncio
async def test_sr_reviewer_disagreement_tasks_empty_state(
    client: AsyncClient, db_session
):
    """Returns empty items list when no disagreements exist."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-reviewer-disagreements/tasks?days=30"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 0
    assert body["items"] == []
    assert body["category_counts"] == {}


@pytest.mark.asyncio
async def test_sr_reviewer_disagreement_tasks_pagination(
    client: AsyncClient, db_session
):
    """Pagination returns correct page slices."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "check_results": {"correctness": {"passed": True}},
                        "blind_spots": [],
                        "reviewer_findings": [
                            {
                                "category": "correctness",
                                "severity": "HIGH",
                                "description": "issue",
                            }
                        ],
                    }
                },
            )
        )
    await db_session.commit()

    resp_p0 = await client.get(
        f"/api/admin/projects/{project.id}/sr-reviewer-disagreements/tasks?days=30&page=0&page_size=2"
    )
    assert resp_p0.status_code == 200
    body_p0 = resp_p0.json()
    assert body_p0["total"] == 3
    assert len(body_p0["items"]) == 2

    resp_p1 = await client.get(
        f"/api/admin/projects/{project.id}/sr-reviewer-disagreements/tasks?days=30&page=1&page_size=2"
    )
    assert resp_p1.status_code == 200
    body_p1 = resp_p1.json()
    assert body_p1["total"] == 3
    assert len(body_p1["items"]) == 1


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
    stale_ts = (datetime.utcnow() - timedelta(days=8)).isoformat()
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
    fresh_ts = (datetime.utcnow() - timedelta(days=1)).isoformat()
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
async def test_digest_sr_trends_overall_sr_alone_passthrough(
    client: AsyncClient, db_session
):
    """Digest explicitly promotes overall_sr_alone_* fields from sr_catch_rate_trends."""
    # Case 1: all catches are reviewer-origin → combined high, author-only low
    project = make_project()
    project.design_doc = {
        "sr_catch_rate_trends": {
            "computed_at": "2026-05-01T00:00:00",
            "categories": {},
            "by_task_type": {},
            "overall_hit_rate": 0.80,
            "overall_sr_alone_hit_rate": 0.10,
            "previous_overall_sr_alone_hit_rate": 0.12,
            "overall_sr_alone_trend_verdict": "stable",
            "overall_sr_alone_trend_significant": False,
            "overall_sr_alone_trend_data_sufficient": True,
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    trends = response.json().get("self_review_prediction_accuracy_trends")
    assert trends is not None
    assert trends["overall_hit_rate"] == 0.80
    assert trends["overall_sr_alone_hit_rate"] == 0.10, (
        "combined high but author-only should be low"
    )
    assert trends["overall_sr_alone_trend_verdict"] == "stable"
    assert trends["overall_sr_alone_trend_significant"] is False
    assert trends["overall_sr_alone_trend_data_sufficient"] is True

    # Case 2: data insufficient → fields present but None / stable / data_sufficient=False
    project2 = make_project()
    project2.design_doc = {
        "sr_catch_rate_trends": {
            "computed_at": "2026-05-01T00:00:00",
            "categories": {},
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
    # All five SR-alone fields must be present even when source lacks them
    for field in (
        "overall_sr_alone_hit_rate",
        "previous_overall_sr_alone_hit_rate",
        "overall_sr_alone_trend_verdict",
        "overall_sr_alone_trend_significant",
        "overall_sr_alone_trend_data_sufficient",
    ):
        assert field in trends2, f"missing promoted field: {field}"
    assert trends2["overall_sr_alone_hit_rate"] is None
    assert trends2["overall_sr_alone_trend_significant"] is None
    assert trends2["overall_sr_alone_trend_data_sufficient"] is None


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
    assert ids0.isdisjoint(ids1), "page=0 and page=1 must return non-overlapping task sets"


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
                    "_first_attempt_rejection_categories": {"correctness": 1, "architecture": 1},
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
async def test_sr_missed_retrospectives_drilldown_fields_present(
    client: AsyncClient, db_session
):
    """sr_check_results and reviewer_findings are included in each response item."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    long_desc = "y" * 450
    db_session.add(
        make_task(
            project_id=project.id,
            title="Drilldown task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_first_attempt_rejection_categories": {"correctness": 1},
                "_self_review_outcome": {
                    "verdict": "pass",
                    "check_results": {
                        "correctness": {
                            "passed": True,
                            "issue": "sr note",
                            "origin": "reviewer",
                        },
                        "performance": {"passed": False},
                    },
                    "reviewer_findings": [
                        {
                            "category": "correctness",
                            "severity": "HIGH",
                            "description": long_desc,
                        },
                        {
                            "category": "security",
                            "severity": "LOW",
                            "description": "ignored category",
                        },
                    ],
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
    # sr_check_results must be present and filtered to missed categories only
    assert "sr_check_results" in row, "sr_check_results missing from response item"
    assert set(row["sr_check_results"]) == {"correctness"}
    assert row["sr_check_results"]["correctness"]["origin"] == "reviewer"
    assert row["sr_check_results"]["correctness"]["issue"] == "sr note"
    # reviewer_findings must be present and filtered to missed categories, description truncated
    assert "reviewer_findings" in row, "reviewer_findings missing from response item"
    assert row["reviewer_findings"] == [
        {
            "category": "correctness",
            "severity": "HIGH",
            "description": long_desc[:400],
        }
    ]


@pytest.mark.asyncio
async def test_sr_missed_retrospectives_scope_filter(
    client: AsyncClient, db_session
):
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
async def test_sr_missed_retrospectives_min_severity_filter(
    client: AsyncClient, db_session
):
    """?min_severity filters out tasks whose reviewer findings are all below the threshold.

    total and category_counts must reflect the severity-filtered subset.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    def _ctx(severity: str, missed_cat: str = "correctness"):
        return {
            "_first_attempt_rejection_categories": {missed_cat: 1},
            "_self_review_outcome": {
                "verdict": "pass",
                "fixes_applied": False,
                "check_results": {missed_cat: {"passed": True}},
                "reviewer_findings": [
                    {"category": missed_cat, "severity": severity, "description": "x"}
                ],
                "blind_spots": [],
            },
        }

    db_session.add_all(
        [
            make_task(
                project_id=project.id,
                title="Critical miss",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context=_ctx("CRITICAL", "correctness"),
            ),
            make_task(
                project_id=project.id,
                title="High miss",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context=_ctx("HIGH", "performance"),
            ),
            make_task(
                project_id=project.id,
                title="Low miss",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context=_ctx("LOW", "correctness"),
            ),
        ]
    )
    await db_session.commit()

    # Unfiltered baseline: all three tasks
    r_all = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives"
    )
    assert r_all.status_code == 200
    assert r_all.json()["total"] == 3

    # min_severity=HIGH keeps CRITICAL + HIGH, drops the LOW-only task
    r_high = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives?min_severity=HIGH"
    )
    assert r_high.status_code == 200
    body_high = r_high.json()
    assert body_high["total"] == 2
    titles = {item["title"] for item in body_high["items"]}
    assert titles == {"Critical miss", "High miss"}
    # category_counts must reflect ONLY the severity-filtered subset
    assert body_high["category_counts"] == {"correctness": 1, "performance": 1}

    # min_severity=CRITICAL keeps only the CRITICAL task
    r_crit = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives?min_severity=CRITICAL"
    )
    assert r_crit.status_code == 200
    body_crit = r_crit.json()
    assert body_crit["total"] == 1
    assert body_crit["category_counts"] == {"correctness": 1}


@pytest.mark.asyncio
async def test_sr_unhandled_edges_task_types_returned(
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
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "edges_considered": [
                            {"case": "empty input", "handled": False}
                        ],
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
async def test_sr_unhandled_edges_blind_spot_counts(
    client: AsyncClient, db_session
):
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
async def test_sr_recent_misses_includes_drilldown_fields(
    client: AsyncClient, db_session
):
    """SR drilldown fields are filtered to missed categories and truncated."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    long_desc = "x" * 450
    db_session.add(
        make_task(
            project_id=project.id,
            title="Add feature Y",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_first_attempt_rejection_categories": ["correctness", "performance"],
                "_self_review_outcome": {
                    "verdict": "pass",
                    "check_results": {
                        "correctness": {
                            "passed": True,
                            "issue": "sr note",
                            "origin": "reviewer",
                        },
                        "performance": {"passed": False},
                    },
                    "reviewer_findings": [
                        {
                            "category": "correctness",
                            "severity": "HIGH",
                            "description": long_desc,
                        },
                        {
                            "category": "security",
                            "severity": "LOW",
                            "description": "ignored",
                        },
                    ],
                },
            },
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/projects/{project.id}/sr-recent-misses")
    assert response.status_code == 200
    row = response.json()["misses"][0]
    assert set(row["sr_check_results"]) == {"correctness"}
    assert row["sr_check_results"]["correctness"]["origin"] == "reviewer"
    assert row["sr_check_results"]["correctness"]["issue"] == "sr note"
    assert row["reviewer_findings"] == [
        {
            "category": "correctness",
            "severity": "HIGH",
            "description": long_desc[:400],
        }
    ]


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
                    "reviewer_findings": [
                        {
                            "category": "correctness",
                            "severity": "HIGH",
                            "description": "reviewer rescue",
                        }
                    ],
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
    """When no _all_iterations and no review_history, _first_attempt_rejection_categories is used."""
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


# Retired: test_digest_fresh_reviewer_trend_* (null_without_design_doc,
# surfaces_from_design_doc, regressing_classification, null_when_below_minimum_sample)
# tested the digest's fresh_reviewer_trend emission, which was removed as
# consumer-less (no UI component reads digest.fresh_reviewer_trend). The
# underlying computation/persistence (compute_fresh_reviewer_trend writing
# design_doc["fresh_reviewer_trend"]) is untouched and still covered by
# test_playbook_sr_metrics.py.


@pytest.mark.asyncio
async def test_sr_alert_drilldown_returns_missed_tasks(client: AsyncClient, db_session):
    """Tasks where SR passed but human rejected on target category appear in drilldown."""
    project = make_project(
        design_doc={
            "sr_catch_rate_trends_alert": {
                "computed_at": datetime.utcnow().isoformat(),
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
