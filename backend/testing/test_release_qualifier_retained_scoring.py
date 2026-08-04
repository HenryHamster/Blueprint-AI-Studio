"""Retained qualification scoring regressions unrelated to scheduler ownership."""

from _release_qualifier_testhelpers import *  # noqa: F403


@pytest.mark.asyncio
async def test_observe_mode_records_enforce_preview_without_changing_confidence(
    db_session, mocker
):
    """NAMED ACCEPTANCE TEST — observe mode records enforce-mode confidence preview.

    With completion_debt_weight_mode='observe', _finalize_qualification must:
      1. Leave the live composite confidence UNCHANGED (debt not blended).
      2. Record debt_enforce_confidence_preview and debt_enforce_weight_preview in
         last_qualification — showing what enforce would have done.
      3. The preview confidence must differ from the observe confidence when there
         is non-zero debt pressure (proving the preview calculation ran).
    This exercises the production path end-to-end: observe mode → debt snapshot
    → compute_enforce_confidence_preview → base_record fields.
    """
    from app import shared_state

    audit_score = 0.92
    mocker.patch.dict(
        shared_state.runtime_config,
        {
            "promotion_enabled": False,
            "completion_debt_weight_mode": "observe",
            "promotion_threshold": 0.90,
            "promotion_weights": {
                "audit": 0.40,
                "test_erosion": 0.30,
                "smoketest": 0.30,
            },
        },
        clear=False,
    )

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": audit_score,
            "findings": [],
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "smoke-preview", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    # Seed open completion debt — 5 items gives a pressure score of 0.75 (1 - 5/20).
    for i in range(5):
        db_session.add(
            CompletionDebtEntry(
                project_id=project.id,
                kind="seed_inventory",
                dedupe_key=f"preview-debt-{i}",
                title=f"Open debt item {i}",
            )
        )
    await db_session.commit()

    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] in ("dry_run", "not_promoted"), (
        f"unexpected status: {result['status']}"
    )
    assert result.get("blocker") is None, f"unexpected blocker: {result.get('blocker')}"

    await db_session.refresh(version)
    qual = version.last_qualification

    # 1. Live confidence is the observe-mode composite (no debt blend).
    observe_confidence = qual["confidence"]
    assert observe_confidence > 0.0, "observe confidence should be positive"

    # 2. Preview fields must be present.
    assert "debt_enforce_confidence_preview" in qual, (
        "debt_enforce_confidence_preview must be recorded in observe mode"
    )
    assert "debt_enforce_weight_preview" in qual, (
        "debt_enforce_weight_preview must be recorded in observe mode"
    )

    preview_confidence = qual["debt_enforce_confidence_preview"]
    preview_weight = qual["debt_enforce_weight_preview"]

    # 3. Preview confidence must differ from observe confidence (debt < 1.0 → blends down).
    assert preview_confidence != pytest.approx(observe_confidence), (
        f"preview {preview_confidence} should differ from observe {observe_confidence} "
        f"when there is open debt"
    )
    # Debt pressure < 1.0 → enforce blend must pull confidence down.
    assert preview_confidence < observe_confidence, (
        "enforce preview with open debt should lower confidence vs observe"
    )

    # 4. Preview weight is the default enforce weight (no configured weight set).
    assert preview_weight == pytest.approx(rs.DEFAULT_ENFORCE_COMPLETION_DEBT_WEIGHT)

    # 5. format_qualification_for_api passes the preview fields through.
    from app.release_readiness import format_qualification_for_api
    api_view = format_qualification_for_api(qual)
    assert api_view["debt_enforce_confidence_preview"] == pytest.approx(preview_confidence)
    assert api_view["debt_enforce_weight_preview"] == pytest.approx(preview_weight)


# ===========================================================================
# _redrive_dry_run_candidate — post-dry-run candidate trigger on promotion enable
# ===========================================================================



