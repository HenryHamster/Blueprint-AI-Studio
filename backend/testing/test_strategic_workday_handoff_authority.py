"""Authority and bounded-cost regressions for recovered Workday handoffs."""

from __future__ import annotations

import hashlib
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, event, update
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

import app.strategic_workday_handoff_recovery as handoff_recovery
from app.conversation_coordinator import (
    _OPERATOR_TURN_DEAD_CLAIMS_CONTEXT_KEY,
    _OPERATOR_TURN_MAX_DEAD_CLAIMS,
)
from app.models import (
    MessageHandlingState,
    Project,
    ProjectMessage,
    ProjectMessageSequence,
    StrategicContextEnvelopeSnapshot,
    StrategicMemorySource,
)
from app.session_message_handoff import (
    RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY,
    handoff_session_messages,
)
from app.strategic_workday_handoff_recovery import (
    _canonical_source_receipt,
    _source_is_settled_or_live,
    _source_shape_is_canonical,
    recover_one_missing_workday_request,
)
from app.strategic_workday_mutation_rebind import (
    WORKDAY_MUTATION_RECEIPTS_CONTEXT_KEY,
    WORKDAY_MUTATION_RECEIPTS_HAS_MORE_CONTEXT_KEY,
    WORKDAY_OPERATION_LEDGER_CONTEXT_KEY,
)
from testing.test_strategic_session_messages import (
    _activate_typed_fast_worker,
    _add_typed_fast_input,
    _headers,
    _make_resident_poll_worker,
    _typed_fast_claim_payload,
)
from testing.test_strategic_workday_handoff_recovery import (
    _add_workday_request,
    _claim_body,
    _claim_continuation,
    _move_private_handoff_to_retired_session,
    _start_workday_request,
)

@pytest.mark.asyncio
async def test_broad_recovery_savepoint_rolls_back_complete_partial_result(
    client: AsyncClient, db_session, monkeypatch
):
    project, task, _incarnation, identity, source, packet = (
        await _start_workday_request(client, db_session, key="broad-savepoint")
    )
    await db_session.execute(
        delete(ProjectMessage).where(ProjectMessage.id == packet.id)
    )
    await db_session.commit()
    snapshot_ids_before = set(
        (
            await db_session.execute(
                select(StrategicContextEnvelopeSnapshot.snapshot_id).where(
                    StrategicContextEnvelopeSnapshot.project_id == project.id
                )
            )
        ).scalars().all()
    )
    counter_before = await db_session.get(ProjectMessageSequence, project.id)
    assert counter_before is not None
    counter_value_before = counter_before.last_sequence
    original_create = handoff_recovery._create_recovery_result

    async def create_then_fail(*args, **kwargs):
        await original_create(*args, **kwargs)
        raise ValueError("synthetic post-create failure")

    monkeypatch.setattr(
        handoff_recovery,
        "_create_recovery_result",
        create_then_fail,
    )
    recovered = await recover_one_missing_workday_request(
        db_session,
        project=project,
        task=task,
        worker_generation=identity["worker_generation"],
        worker_incarnation=uuid.UUID(identity["incarnation_id"]),
        session_id=identity["session_id"],
    )

    assert recovered is None
    assert task.context[
        "_workday_consultation_recovery_retry_source_ids"
    ] == [str(source.id)]
    snapshot_ids_after = set(
        (
            await db_session.execute(
                select(StrategicContextEnvelopeSnapshot.snapshot_id).where(
                    StrategicContextEnvelopeSnapshot.project_id == project.id
                )
            )
        ).scalars().all()
    )
    assert snapshot_ids_after == snapshot_ids_before
    counter_after = (
        await db_session.execute(
            select(ProjectMessageSequence)
            .where(ProjectMessageSequence.project_id == project.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    assert counter_after.last_sequence == counter_value_before
    partial_results = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.idempotency_key
                == f"workday-consultation-terminal-recovery:{source.id}",
            )
        )
    ).scalars().all()
    assert partial_results == []
    await db_session.commit()

    monkeypatch.setattr(
        handoff_recovery,
        "_create_recovery_result",
        original_create,
    )
    redriven = await recover_one_missing_workday_request(
        db_session,
        project=project,
        task=task,
        worker_generation=identity["worker_generation"],
        worker_incarnation=uuid.UUID(identity["incarnation_id"]),
        session_id=identity["session_id"],
    )
    assert redriven is not None
    await db_session.commit()
    await db_session.refresh(task)
    assert "_workday_consultation_recovery_retry_source_ids" not in task.context
    recovery_rows = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.idempotency_key
                == f"workday-consultation-terminal-recovery:{source.id}",
            )
        )
    ).scalars().all()
    assert len(recovery_rows) == 1
    counter_after_redrive = (
        await db_session.execute(
            select(ProjectMessageSequence)
            .where(ProjectMessageSequence.project_id == project.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    assert counter_after_redrive.last_sequence == counter_value_before + 1

    duplicate = await recover_one_missing_workday_request(
        db_session,
        project=project,
        task=task,
        worker_generation=identity["worker_generation"],
        worker_incarnation=uuid.UUID(identity["incarnation_id"]),
        session_id=identity["session_id"],
    )
    assert duplicate is None
    await db_session.commit()
    final_counter = (
        await db_session.execute(
            select(ProjectMessageSequence)
            .where(ProjectMessageSequence.project_id == project.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    assert final_counter.last_sequence == counter_value_before + 1

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    ("server-lineage", "result-content", "result-state"),
)
async def test_invalid_continuation_provenance_is_quarantined_without_starvation(
    client: AsyncClient, db_session, corruption: str
):
    project, task, incarnation, identity, source, packet = (
        await _start_workday_request(
            client,
            db_session,
            key=f"invalid-continuation-{corruption}",
        )
    )
    packet_claim = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/claim-next",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=_claim_body(identity),
    )
    acknowledged = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/{packet.id}/ack",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "workday_claim_id": packet_claim.json()["packet"]["workday_claim_id"],
            "outcome": "completed",
            "result_summary": "result whose source provenance will be corrupted",
        },
    )
    assert acknowledged.status_code == 200, acknowledged.text
    result = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.context["_workday_consultation_result"]
                .as_boolean()
                .is_(True),
            )
        )
    ).scalars().one()
    await _move_private_handoff_to_retired_session(
        db_session,
        result,
        retired_session_id="invalid-provenance-retired-session",
    )
    await db_session.refresh(result)
    canonical_result_context = dict(result.context or {})
    result_context = dict(canonical_result_context)
    poisoned_result_id = result.id
    corrupt_values: dict[str, object] = {"context": result_context}
    if corruption == "server-lineage":
        result_context.pop("source_packet_id", None)
        corrupt_values["idempotency_key"] = (
            f"workday-consultation-result:{packet.id}:{uuid.uuid4()}"
        )
    elif corruption == "result-content":
        corrupt_values["content"] = "private forged result text"
    else:
        result_context["result_state"] = "failed"
    # Direct SQL models a pre-fix/corrupted durable row without invoking the
    # normal privacy hook, which correctly purges derived packets whenever
    # application code mutates their governed context.
    await db_session.execute(
        update(ProjectMessage)
        .where(ProjectMessage.id == poisoned_result_id)
        .values(**corrupt_values)
    )
    await db_session.commit()

    _second_source, second_packet = await _add_workday_request(
        client,
        db_session,
        project=project,
        task=task,
        incarnation=incarnation,
        identity=identity,
        key="good-behind-poisoned",
    )
    second_claim = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/claim-next",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=_claim_body(identity),
    )
    assert second_claim.status_code == 200, second_claim.text
    assert second_claim.json()["packet"]["packet_id"] == str(second_packet.id)
    second_workday_claim_id = second_claim.json()["packet"]["workday_claim_id"]
    second_ack = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/"
        f"{second_packet.id}/ack",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "workday_claim_id": second_workday_claim_id,
            "outcome": "completed",
            "result_summary": "canonical second result",
        },
    )
    assert second_ack.status_code == 200, second_ack.text
    good_result = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.idempotency_key
                == (
                    f"workday-consultation-result:{second_packet.id}:"
                    f"{second_workday_claim_id}"
                ),
            )
        )
    ).scalars().one()
    snapshot_ids_before_delivery = set(
        (
            await db_session.execute(
                select(StrategicContextEnvelopeSnapshot.snapshot_id).where(
                    StrategicContextEnvelopeSnapshot.project_id == project.id
                )
            )
        ).scalars().all()
    )
    before_delivery_ids = set(
        (
            await db_session.execute(
                select(ProjectMessage.id).where(
                    ProjectMessage.project_id == project.id
                )
            )
        ).scalars().all()
    )
    assert poisoned_result_id in before_delivery_ids

    continuation = await _claim_continuation(client, project, task, identity)
    assert continuation.status_code == 200, continuation.text
    assert continuation.json()["status"] == "claimed"
    assert continuation.json()["continuation"]["result_summary"] == (
        "canonical second result"
    )
    snapshot_ids_after_delivery = set(
        (
            await db_session.execute(
                select(StrategicContextEnvelopeSnapshot.snapshot_id).where(
                    StrategicContextEnvelopeSnapshot.project_id == project.id
                )
            )
        ).scalars().all()
    )
    # Direct lineage may be unusable, but the bounded missing-request scan can
    # independently re-establish the canonical public source and its receipt.
    # Either path may create one neutral snapshot; corrupt result projections
    # must never authorize any additional rebind or delivery.
    assert len(snapshot_ids_after_delivery - snapshot_ids_before_delivery) <= 1
    completed_good = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/continuations/"
        f"{good_result.id}/complete",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "continuation_claim_id": continuation.json()["continuation"][
                "continuation_claim_id"
            ],
            "content": "Delivered the canonical second result.",
            "idempotency_key": "complete-good-behind-poisoned",
        },
    )
    assert completed_good.status_code == 200, completed_good.text
    result_rows = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.project_id == project.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    result_by_id = {row.id: row for row in result_rows}
    assert poisoned_result_id in result_by_id, [
        (str(row.id), row.status, str(row.resolution)) for row in result_rows
    ]
    result = result_by_id[poisoned_result_id]
    assert result.status == "resolved"
    assert result.handling_state == MessageHandlingState.SUPERSEDED
    assert result.resolution == "private_handoff_continuation_provenance_invalid"

    recovery_rows = [
        row
        for row in result_rows
        if row.idempotency_key
        == f"workday-consultation-terminal-recovery:{source.id}"
    ]
    assert len(recovery_rows) <= 1
    if recovery_rows:
        assert recovery_rows[0].content == handoff_recovery._RECOVERY_SUMMARY
    assert not any(
        str(row.idempotency_key or "").startswith(
            f"workday-consultation-terminal-recovery-replacement:{source.id}:"
        )
        for row in result_rows
    )
    recovered_missing = await _claim_continuation(client, project, task, identity)
    assert recovered_missing.status_code == 200, recovered_missing.text
    assert recovered_missing.json()["status"] == "claimed"
    recovery_body = recovered_missing.json()["continuation"]
    assert recovery_body["consultation_id"] == str(source.id)
    assert recovery_body["result_summary"] == handoff_recovery._RECOVERY_SUMMARY
    completed_recovery = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/continuations/"
        f"{recovery_body['result_packet_id']}/complete",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "continuation_claim_id": recovery_body["continuation_claim_id"],
            "content": "The unavailable private result was recovered neutrally.",
            "idempotency_key": "complete-poisoned-neutral-recovery",
        },
    )
    assert completed_recovery.status_code == 200, completed_recovery.text
    drained = await _claim_continuation(client, project, task, identity)
    assert drained.status_code == 200, drained.text
    assert drained.json()["status"] == "empty"
    assert "source provenance will be corrupted" not in continuation.text
    assert "private forged result text" not in continuation.text


@pytest.mark.asyncio
async def test_result_lineage_requires_exact_packet_and_ack_authority(
    client: AsyncClient, db_session
):
    project, task, _incarnation, identity, source, packet = (
        await _start_workday_request(client, db_session, key="lineage-authority")
    )
    packet_claim = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/claim-next",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=_claim_body(identity),
    )
    acknowledged = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/{packet.id}/ack",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "workday_claim_id": packet_claim.json()["packet"]["workday_claim_id"],
            "outcome": "completed",
            "result_summary": "canonical authority-bound result",
        },
    )
    assert acknowledged.status_code == 200, acknowledged.text
    result = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.context["_workday_consultation_result"]
                .as_boolean()
                .is_(True),
            )
        )
    ).scalars().one()
    assert await handoff_recovery.workday_result_source_id_from_server_lineage(
        db_session,
        row=result,
    ) == source.id
    lineage = await handoff_recovery.workday_result_server_lineage(
        db_session,
        row=result,
    )
    assert lineage is not None
    assert lineage.public_source_id == source.id
    assert lineage.authority_row_id == packet.id
    assert lineage.kind == "consultation_packet"

    # Result-owned fields never select receipt authority. The relational edge
    # still resolves packet/source identity, while the continuation route
    # separately rejects this forged projection by exact equality.
    result_context = dict(result.context or {})
    result_context["source_packet_id"] = str(uuid.uuid4())
    result.context = result_context
    db_session.add(result)
    await db_session.flush()
    forged_lineage = await handoff_recovery.workday_result_server_lineage(
        db_session,
        row=result,
    )
    assert forged_lineage is not None
    assert forged_lineage.public_source_id == lineage.public_source_id
    assert forged_lineage.authority_row_id == lineage.authority_row_id
    assert forged_lineage.projections_valid is False

    canonical_result_key = result.idempotency_key
    result.idempotency_key = (
        f"workday-consultation-result:{packet.id}:{uuid.uuid4()}"
    )
    db_session.add(result)
    await db_session.flush()
    assert await handoff_recovery.workday_result_source_id_from_server_lineage(
        db_session,
        row=result,
    ) is None

    result.idempotency_key = canonical_result_key
    packet.idempotency_key = f"forged-packet-authority:{packet.id}"
    db_session.add_all([result, packet])
    await db_session.flush()
    assert await handoff_recovery.workday_result_source_id_from_server_lineage(
        db_session,
        row=result,
    ) is None


@pytest.mark.asyncio
async def test_packet_source_context_tamper_recovers_relational_source_not_decoy(
    client: AsyncClient, db_session
):
    project, task, incarnation, identity, source, packet = (
        await _start_workday_request(client, db_session, key="packet-relational-source")
    )
    decoy_source, _decoy_packet = await _add_workday_request(
        client,
        db_session,
        project=project,
        task=task,
        incarnation=incarnation,
        identity=identity,
        key="packet-relational-decoy",
    )
    assert packet.reply_to_message_id == source.id
    packet_context = dict(packet.context or {})
    packet_context["_workday_consultation_atomic_reply_id"] = str(decoy_source.id)
    # Direct SQL models a corrupted durable projection without invoking the
    # governed privacy hook, which would correctly purge all derived packets
    # and make this authority-selection scenario disappear before recovery.
    await db_session.execute(
        update(ProjectMessage)
        .where(ProjectMessage.id == packet.id)
        .values(context=packet_context)
    )
    await db_session.commit()
    await db_session.refresh(packet)
    assert await handoff_recovery.workday_packet_immutable_source_id(
        db_session,
        row=packet,
    ) == source.id
    assert await handoff_recovery.workday_packet_source_id_from_server_lineage(
        db_session,
        row=packet,
    ) is None

    claimed_packet = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/claim-next",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=_claim_body(identity),
    )
    assert claimed_packet.status_code == 200, claimed_packet.text
    rows = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.project_id == project.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    assert sum(
        row.idempotency_key
        == f"workday-consultation-terminal-recovery:{source.id}"
        for row in rows
    ) == 1
    assert not any(
        row.idempotency_key
        == f"workday-consultation-terminal-recovery:{decoy_source.id}"
        for row in rows
    )

    recovered_ids = set()
    for _ in range(2):
        recovered = await _claim_continuation(client, project, task, identity)
        assert recovered.status_code == 200, recovered.text
        if recovered.json()["status"] == "claimed":
            recovered_ids.add(recovered.json()["continuation"]["consultation_id"])
    assert str(source.id) in recovered_ids


@pytest.mark.asyncio
async def test_result_source_context_tamper_recovers_packet_relational_source_not_decoy(
    client: AsyncClient, db_session
):
    project, task, incarnation, identity, source, packet = (
        await _start_workday_request(client, db_session, key="result-relational-source")
    )
    packet_claim = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/claim-next",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=_claim_body(identity),
    )
    assert packet_claim.status_code == 200, packet_claim.text
    acknowledged = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/{packet.id}/ack",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "workday_claim_id": packet_claim.json()["packet"]["workday_claim_id"],
            "outcome": "completed",
            "result_summary": "poisoned result must never be delivered",
        },
    )
    assert acknowledged.status_code == 200, acknowledged.text
    result = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.context["_workday_consultation_result"]
                .as_boolean()
                .is_(True),
            )
        )
    ).scalars().one()
    decoy_source, _decoy_packet = await _add_workday_request(
        client,
        db_session,
        project=project,
        task=task,
        incarnation=incarnation,
        identity=identity,
        key="result-relational-decoy",
    )
    for row in (packet, result):
        context = dict(row.context or {})
        context["_workday_consultation_atomic_reply_id"] = str(decoy_source.id)
        await db_session.execute(
            update(ProjectMessage)
            .where(ProjectMessage.id == row.id)
            .values(context=context)
        )
    await db_session.commit()
    await db_session.refresh(packet)
    await db_session.refresh(result)
    lineage = await handoff_recovery.workday_result_server_lineage(
        db_session,
        row=result,
    )
    assert lineage is not None
    assert lineage.public_source_id == source.id
    assert lineage.projections_valid is False

    recovered = await _claim_continuation(client, project, task, identity)
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["status"] == "claimed"
    rows = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.project_id == project.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    result_by_id = {row.id: row for row in rows}
    assert result_by_id[result.id].handling_state == MessageHandlingState.SUPERSEDED
    assert sum(
        row.idempotency_key
        == f"workday-consultation-terminal-recovery:{source.id}"
        for row in rows
    ) == 1
    assert not any(
        row.idempotency_key
        == f"workday-consultation-terminal-recovery:{decoy_source.id}"
        for row in rows
    )
    recovered_ids = {recovered.json()["continuation"]["consultation_id"]}
    for _ in range(2):
        next_recovery = await _claim_continuation(client, project, task, identity)
        assert next_recovery.status_code == 200, next_recovery.text
        if next_recovery.json()["status"] == "claimed":
            recovered_ids.add(
                next_recovery.json()["continuation"]["consultation_id"]
            )
    assert str(source.id) in recovered_ids


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    (
        "missing-source-packet",
        "result-content",
        "receipt-only",
        "receipt-ledger",
    ),
)
async def test_invalid_terminal_recovery_is_replaced_neutrally_exactly_once(
    client: AsyncClient, db_session, corruption: str
):
    project, task, _incarnation, identity, source, packet = (
        await _start_workday_request(client, db_session, key="invalid-terminal")
    )
    await db_session.execute(
        delete(ProjectMessage).where(ProjectMessage.id == packet.id)
    )
    await db_session.commit()
    original_recovery = await recover_one_missing_workday_request(
        db_session,
        project=project,
        task=task,
        worker_generation=identity["worker_generation"],
        worker_incarnation=uuid.UUID(identity["incarnation_id"]),
        session_id=identity["session_id"],
    )
    assert original_recovery is not None
    await db_session.commit()
    original_recovery_id = original_recovery.id
    original_context = dict(original_recovery.context or {})
    corrupt_values: dict[str, object]
    if corruption == "missing-source-packet":
        original_context.pop("source_packet_id", None)
        corrupt_values = {"context": original_context}
    elif corruption == "result-content":
        corrupt_values = {"content": "private forged terminal recovery text"}
    elif corruption == "receipt-only":
        original_context[WORKDAY_MUTATION_RECEIPTS_CONTEXT_KEY] = [
            {"malformed_private_receipt": "must never be projected"}
        ]
        corrupt_values = {"context": original_context}
    else:
        original_context.update(
            {
                WORKDAY_MUTATION_RECEIPTS_CONTEXT_KEY: [
                    {"private_forged_receipt": "must never be projected"}
                ],
                WORKDAY_MUTATION_RECEIPTS_HAS_MORE_CONTEXT_KEY: True,
                WORKDAY_OPERATION_LEDGER_CONTEXT_KEY: [
                    {"private_forged_operation": "must never be replayed"}
                ],
            }
        )
        corrupt_values = {"context": original_context}
    await db_session.execute(
        update(ProjectMessage)
        .where(ProjectMessage.id == original_recovery_id)
        .values(**corrupt_values)
    )
    await db_session.commit()
    snapshot_ids_before = set(
        (
            await db_session.execute(
                select(StrategicContextEnvelopeSnapshot.snapshot_id).where(
                    StrategicContextEnvelopeSnapshot.project_id == project.id
                )
            )
        ).scalars().all()
    )

    claimed = await _claim_continuation(client, project, task, identity)
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["status"] == "claimed"
    body = claimed.json()["continuation"]
    replacement_id = uuid.UUID(body["result_packet_id"])
    assert replacement_id != original_recovery_id
    assert body["consultation_id"] == str(source.id)
    assert body["result_summary"] == handoff_recovery._RECOVERY_SUMMARY
    assert "private forged terminal recovery text" not in claimed.text
    assert "private_forged_receipt" not in claimed.text
    assert "private_forged_operation" not in claimed.text
    snapshot_ids_after = set(
        (
            await db_session.execute(
                select(StrategicContextEnvelopeSnapshot.snapshot_id).where(
                    StrategicContextEnvelopeSnapshot.project_id == project.id
                )
            )
        ).scalars().all()
    )
    # Snapshot storage may deduplicate the exact same canonical authority.
    # Either way, validation happened before any rebind and can mint at most
    # the one replacement envelope.
    assert len(snapshot_ids_after - snapshot_ids_before) <= 1

    rows = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.project_id == project.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    by_id = {row.id: row for row in rows}
    quarantined = by_id[original_recovery_id]
    replacement = by_id[replacement_id]
    assert quarantined.status == "resolved"
    assert quarantined.handling_state == MessageHandlingState.SUPERSEDED
    assert quarantined.resolution == (
        "private_handoff_continuation_provenance_invalid"
    )
    assert replacement.idempotency_key == (
        "workday-consultation-terminal-recovery-replacement:"
        f"{source.id}:{original_recovery_id}"
    )
    assert replacement.context["source_packet_id"] == str(source.id)
    assert replacement.context["_workday_consultation_atomic_reply_id"] == str(
        source.id
    )
    assert replacement.context["delivery_key"] == "terminal-recovery"
    assert replacement.context["_workday_terminal_recovery"] is True
    public_continuations = [
        row
        for row in rows
        if row.from_type == "strategic"
        and (row.context or {}).get("message_role")
        == "workday_consultation_continuation"
    ]
    assert public_continuations == []

    completed = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/continuations/"
        f"{replacement_id}/complete",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "continuation_claim_id": body["continuation_claim_id"],
            "content": "The private check failed safely; no outcome is verified.",
            "idempotency_key": f"complete-invalid-terminal:{source.id}",
        },
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "created"
    drained = await _claim_continuation(client, project, task, identity)
    assert drained.status_code == 200, drained.text
    assert drained.json()["status"] == "empty"
    final_rows = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.project_id == project.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    assert len(
        [
            row
            for row in final_rows
            if row.from_type == "strategic"
            and (row.context or {}).get("message_role")
            == "workday_consultation_continuation"
            and (row.context or {}).get("_workday_consultation_atomic_reply_id")
            == str(source.id)
        ]
    ) == 1
    assert len(
        [
            row
            for row in final_rows
            if str(row.idempotency_key or "").startswith(
                "workday-consultation-terminal-recovery"
            )
        ]
    ) == 2


@pytest.mark.asyncio
async def test_terminal_recovery_replacement_requires_exact_root_chain(
    client: AsyncClient, db_session
):
    project, task, _incarnation, identity, source, packet = (
        await _start_workday_request(client, db_session, key="replacement-chain")
    )
    await db_session.execute(
        delete(ProjectMessage).where(ProjectMessage.id == packet.id)
    )
    await db_session.commit()
    root = await recover_one_missing_workday_request(
        db_session,
        project=project,
        task=task,
        worker_generation=identity["worker_generation"],
        worker_incarnation=uuid.UUID(identity["incarnation_id"]),
        session_id=identity["session_id"],
    )
    assert root is not None
    await db_session.commit()
    root_context = dict(root.context or {})
    root_context.pop("source_packet_id", None)
    await db_session.execute(
        update(ProjectMessage)
        .where(ProjectMessage.id == root.id)
        .values(context=root_context)
    )
    await db_session.commit()

    first_claim = await _claim_continuation(client, project, task, identity)
    assert first_claim.status_code == 200, first_claim.text
    assert first_claim.json()["status"] == "claimed"
    first_id = uuid.UUID(
        first_claim.json()["continuation"]["result_packet_id"]
    )
    first = await db_session.get(ProjectMessage, first_id)
    assert first is not None
    assert first.idempotency_key == (
        "workday-consultation-terminal-recovery-replacement:"
        f"{source.id}:{root.id}"
    )

    # Force a second legitimate replacement generation. Its authority now
    # depends on the complete immutable predecessor chain, not merely the
    # immediate predecessor's source prefix.
    await db_session.execute(
        update(ProjectMessage)
        .where(ProjectMessage.id == first_id)
        .values(content="private forged intermediate recovery text")
    )
    await db_session.commit()
    second_claim = await _claim_continuation(client, project, task, identity)
    assert second_claim.status_code == 200, second_claim.text
    assert second_claim.json()["status"] == "claimed"
    second_id = uuid.UUID(
        second_claim.json()["continuation"]["result_packet_id"]
    )
    assert second_id != first_id
    second = await db_session.get(ProjectMessage, second_id)
    assert second is not None
    assert second.idempotency_key == (
        "workday-consultation-terminal-recovery-replacement:"
        f"{source.id}:{first_id}"
    )

    # Corrupt the middle row's pointer to the root while preserving its source
    # prefix. A one-hop validator would accept the newest row; the bounded walk
    # must fail closed before resolving or rebinding its private envelope.
    await db_session.execute(
        update(ProjectMessage)
        .where(ProjectMessage.id == first_id)
        .values(
            idempotency_key=(
                "workday-consultation-terminal-recovery-replacement:"
                f"{source.id}:{uuid.uuid4()}"
            )
        )
    )
    await db_session.commit()
    assert await handoff_recovery.workday_result_source_id_from_server_lineage(
        db_session,
        row=second,
    ) is None
    original_key = second.idempotency_key
    original_context = dict(second.context or {})
    second.idempotency_key = f"{original_key}:garbage"
    assert await handoff_recovery.terminal_recovery_source_candidate_from_server_key(
        db_session,
        row=second,
    ) is None
    second.idempotency_key = (
        "workday-consultation-terminal-recovery-replacement:"
        f"{source.id}:not-a-uuid"
    )
    assert await handoff_recovery.terminal_recovery_source_candidate_from_server_key(
        db_session,
        row=second,
    ) is None
    second.idempotency_key = original_key
    second.context = {
        **original_context,
        "source_packet_id": str(uuid.uuid4()),
    }
    assert await handoff_recovery.terminal_recovery_source_candidate_from_server_key(
        db_session,
        row=second,
    ) is None
    second.context = {
        **original_context,
        "_workday_consultation_atomic_reply_id": str(uuid.uuid4()),
    }
    assert await handoff_recovery.terminal_recovery_source_candidate_from_server_key(
        db_session,
        row=second,
    ) is None
    second.context = original_context
    second.status = "resolved"
    assert await handoff_recovery.terminal_recovery_source_candidate_from_server_key(
        db_session,
        row=second,
    ) is None
    second.status = "open"
    assert await handoff_recovery.terminal_recovery_source_candidate_from_server_key(
        db_session,
        row=second,
    ) == source.id
    snapshot_ids_before = set(
        (
            await db_session.execute(
                select(StrategicContextEnvelopeSnapshot.snapshot_id).where(
                    StrategicContextEnvelopeSnapshot.project_id == project.id
                )
            )
        ).scalars().all()
    )
    recovered = await _claim_continuation(client, project, task, identity)
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["status"] == "claimed"
    recovery_body = recovered.json()["continuation"]
    assert recovery_body["result_packet_id"] != str(second_id)
    assert recovery_body["consultation_id"] == str(source.id)
    assert recovery_body["result_summary"] == handoff_recovery._RECOVERY_SUMMARY
    replacement = await db_session.get(
        ProjectMessage,
        uuid.UUID(recovery_body["result_packet_id"]),
    )
    assert replacement is not None
    assert replacement.idempotency_key == (
        "workday-consultation-terminal-recovery-replacement:"
        f"{source.id}:{root.id}"
    )
    snapshot_ids_after = set(
        (
            await db_session.execute(
                select(StrategicContextEnvelopeSnapshot.snapshot_id).where(
                    StrategicContextEnvelopeSnapshot.project_id == project.id
                )
            )
        ).scalars().all()
    )
    assert len(snapshot_ids_after - snapshot_ids_before) <= 1
    await db_session.refresh(second)
    assert second.status == "resolved"
    assert second.handling_state == MessageHandlingState.SUPERSEDED
    assert second.resolution == "private_handoff_continuation_provenance_invalid"
    assert "private forged intermediate recovery text" not in recovered.text
    completed = await client.post(
        f"/api/agent/projects/{project.id}/workday-consultations/continuations/"
        f"{recovery_body['result_packet_id']}/complete",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json={
            **identity,
            "continuation_claim_id": recovery_body["continuation_claim_id"],
            "content": "The corrupt replacement chain was recovered neutrally.",
            "idempotency_key": "complete-exact-root-chain-recovery",
        },
    )
    assert completed.status_code == 200, completed.text
    drained = await _claim_continuation(client, project, task, identity)
    assert drained.status_code == 200, drained.text
    assert drained.json()["status"] == "empty"


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_count", [1, 64])
async def test_missing_recovery_select_count_is_constant_across_candidate_cardinality(
    db_session,
    candidate_count: int,
):
    project, task = await _make_resident_poll_worker(db_session)
    inbound = ProjectMessage(
        project_id=project.id,
        sequence=1,
        surface="conversation",
        from_type="user",
        content="canonical covered input",
        context={},
    )
    db_session.add(inbound)
    await db_session.flush()
    sources = [
        ProjectMessage(
            project_id=project.id,
            sequence=index + 2,
            surface="conversation",
            from_type="strategic",
            reply_to_message_id=inbound.id,
            resident_session_id="resident-session",
            content=f"source {index}",
            context={
                "message_role": "operator_reply",
                "_workday_consultation_requested": True,
                "_conversation_public_reply_owner": "fast",
                # Keep the row query-eligible but semantically unrecoverable,
                # avoiding any side-effect path in this SELECT-count test.
                "privacy_state": "redacted",
            },
        )
        for index in range(candidate_count)
    ]
    counter = await db_session.get(ProjectMessageSequence, project.id)
    if counter is None:
        counter = ProjectMessageSequence(project_id=project.id)
    counter.last_sequence = candidate_count + 1
    db_session.add_all([*sources, counter])
    await db_session.commit()
    project_id = project.id
    task_id = task.id
    db_session.expunge_all()
    project = await db_session.get(Project, project_id)
    task = await db_session.get(type(task), task_id)
    assert project is not None and task is not None

    statements: list[tuple[str, object]] = []

    def _record_statement(_conn, _cursor, statement, params, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append((statement, params))

    engine = db_session.bind
    event.listen(engine.sync_engine, "before_cursor_execute", _record_statement)
    try:
        recovered = await recover_one_missing_workday_request(
            db_session,
            project=project,
            task=task,
            worker_generation=2,
            worker_incarnation=uuid.uuid4(),
            session_id="resident-session",
        )
    finally:
        event.remove(
            engine.sync_engine,
            "before_cursor_execute",
            _record_statement,
        )

    assert recovered is None
    assert len(statements) == 4, statements
    linked_statement, linked_params = next(
        (statement, params)
        for statement, params in statements
        if "AS settled_or_live" in statement
    )
    assert linked_statement.upper().count("EXISTS") == 2
    assert "reply_to_message_id" in linked_statement
    assert "_workday_consultation_atomic_reply_id" not in linked_statement
    assert "_workday_consultation_atomic_reply_id" not in repr(linked_params)
    if engine.dialect.name == "postgresql":
        candidate_statement = next(
            statement
            for statement, _params in statements
            if "_workday_consultation_requested" in statement
        )
        assert (
            "projectmessage.sequence IS NOT NULL AND "
            "projectmessage.from_type = 'strategic' AND "
            "projectmessage.surface = 'conversation' AND "
            "projectmessage.context @> "
            "'{\"_workday_consultation_requested\": true}'::jsonb"
        ) in candidate_statement
    if candidate_count == 64:
        assert task.context[
            "_workday_consultation_recovery_before_sequence"
        ] == 34
        assert task.context[
            "_workday_consultation_recovery_scan_ceiling"
        ] == 65


@pytest.mark.asyncio
async def test_retired_session_missing_handoff_recovers_exactly_once(
    client: AsyncClient, db_session
):
    """A missing packet remains recoverable after its Resident is replaced."""

    project, task, _incarnation, identity, source, packet = (
        await _start_workday_request(client, db_session, key="retired-missing")
    )
    assert await _canonical_source_receipt(db_session, source=source) is not None
    origin_session_id = identity["session_id"]
    changed = await handoff_session_messages(
        db_session,
        project.id,
        origin_session_id,
        replacement_resident_active=True,
    )
    assert changed >= 1
    await db_session.refresh(project)
    await db_session.refresh(task)
    replacement_session_id = "replacement-resident-session"
    task_context = dict(task.context or {})
    task_context["strategic_session_id"] = replacement_session_id
    task_context["_resident_worker_generation"] = 3
    task.context = task_context
    flag_modified(task, "context")
    resident = dict(project.vision["_resident_strategic_session"])
    resident["session_id"] = replacement_session_id
    resident["worker_generation"] = 3
    project.vision = {**project.vision, "_resident_strategic_session": resident}
    flag_modified(project, "vision")
    db_session.add_all([project, task])
    await db_session.commit()
    identity = {
        **identity,
        "session_id": replacement_session_id,
        "worker_generation": 3,
    }
    await db_session.execute(
        delete(ProjectMessage).where(ProjectMessage.id == packet.id)
    )
    await db_session.commit()
    fresh_source = (
        await db_session.execute(
            select(ProjectMessage)
            .where(ProjectMessage.id == source.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    assert fresh_source.status == "resolved"
    assert fresh_source.handling_state == MessageHandlingState.RESPONDED
    assert fresh_source.resident_session_id == origin_session_id
    assert RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY not in fresh_source.context
    receipt = (
        await db_session.execute(
            select(StrategicMemorySource).where(
                StrategicMemorySource.project_id == fresh_source.project_id,
                StrategicMemorySource.source_type == "project_message",
                StrategicMemorySource.source_key == str(fresh_source.id),
            )
        )
    ).scalars().one()
    assert receipt.resident_session_id == origin_session_id
    assert receipt.source_revision == str(fresh_source.sequence)
    assert receipt.native_sequence == fresh_source.sequence
    assert receipt.content_hash == hashlib.sha256(
        fresh_source.content.encode("utf-8")
    ).hexdigest()
    assert receipt.payload_state == "active"
    assert receipt.tombstoned_at is None
    assert receipt.actor_kind == "strategic"
    assert (receipt.source_metadata or {}).get("surface") == "conversation"
    assert _source_shape_is_canonical(fresh_source)
    assert await _canonical_source_receipt(
        db_session,
        source=fresh_source,
    ) is not None
    assert not await _source_is_settled_or_live(
        db_session,
        source=fresh_source,
    )

    recovered = await _claim_continuation(client, project, task, identity)
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["status"] == "claimed"
    continuation = recovered.json()["continuation"]
    assert continuation["consultation_id"] == str(source.id)
    assert "became unavailable" in continuation["result_summary"]
    recovery_rows = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.idempotency_key
                == f"workday-consultation-terminal-recovery:{source.id}",
            )
        )
    ).scalars().all()
    assert len(recovery_rows) == 1
    assert recovery_rows[0].resident_session_id == identity["session_id"]

    duplicate = await _claim_continuation(client, project, task, identity)
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["status"] == "claimed"
    assert duplicate.json()["continuation"]["result_packet_id"] == (
        continuation["result_packet_id"]
    )
    assert len(
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.idempotency_key
                    == f"workday-consultation-terminal-recovery:{source.id}",
                )
            )
        ).scalars().all()
    ) == 1


@pytest.mark.asyncio
async def test_operator_turn_park_is_committed_and_returned_by_the_claim_route(
    client: AsyncClient, db_session
):
    """A park must reach the operator AND survive the request.

    Incident: operator conversation messages sat at handling_state='claimed'
    forever, held by a task that reported "waiting for operator input". The
    coordinator now DECIDES instead of re-handing out a turn whose claim has
    died _OPERATOR_TURN_MAX_DEAD_CLAIMS times -- but the decision is written
    into the request's session, so a route that rolled back on the raised
    OperatorTurnParked would throw the answer away and re-enter the same loop
    on the next poll. This pins the commit and the reason-bearing 409.

    Lives here rather than in test_strategic_session_messages.py (which owns
    the route) only because that module is at its module_size_limits.json cap.
    """
    project, task = await _make_resident_poll_worker(db_session)
    project_id = project.id
    incarnation = await _activate_typed_fast_worker(db_session, project, task)
    inbound = await _add_typed_fast_input(db_session, project)
    inbound.context = {
        **dict(inbound.context or {}),
        _OPERATOR_TURN_DEAD_CLAIMS_CONTEXT_KEY: _OPERATOR_TURN_MAX_DEAD_CLAIMS,
    }
    db_session.add(inbound)
    await db_session.commit()

    parked = await client.post(
        f"/api/agent/projects/{project_id}/conversation/turns/claim",
        headers=_headers(task, vm="agent-vm-resident-poll"),
        json=_typed_fast_claim_payload(
            task, incarnation, inbound.id, client_claim_id=str(uuid.uuid4())
        ),
    )

    assert parked.status_code == 409, parked.text
    # Surface reasons, not states: never a bare "already claimed" 409.
    detail = parked.json()["error"]["message"]
    assert "could not process this message" in detail
    assert "Send it again to retry" in detail

    await db_session.refresh(inbound)
    assert inbound.handling_state == MessageHandlingState.RESPONDED
    assert inbound.status == "handled"
    reply = (
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.from_type == "strategic",
                    ProjectMessage.content == detail,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(reply) == 1
