"""Spawn-side helpers for the agent status endpoint.

Extracted from app.routers.agent_status to reduce that module's size.
Contains the two artifact-gated spawn blocks that were inlined in
update_agent_status, plus the session-recovery helper they share.
"""

import asyncio
import contextlib
import json
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.broadcast import broadcast_to_project
from app.models import (
    Alert,
    AlertSeverity,
    ArtifactType,
    Project,
    TASK_ORIGINATION_REMEDIATION,
    TASK_ORIGINATION_SMOKETEST,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.payload_schemas import validate_internal_payload as _validate_internal_payload
from app.schemas import AgentStatusPayload
from app.shared_state import (
    get_project_integration_branch as _get_project_integration_branch,
    get_project_promotion_target as _get_project_promotion_target,
    runtime_config as _runtime_config,
)
from app.task_locking import (
    AUTOMATIC_TASK_CREATION_ALLOWED,
    automatic_task_creation_decision,
    automatic_task_creation_parent_lock,
)

logger = logging.getLogger(__name__)


async def _recover_poisoned_session(session: AsyncSession, task_id_str: str) -> bool:
    """Roll back the request session if a swallowed exception left it in
    pending-rollback state.

    The big update_agent_status() handler has several broad ``except Exception``
    clauses that log-and-continue. When the swallowed exception was a flush or
    commit failure, SQLAlchemy marks the transaction as "rollback only" — every
    subsequent ``session.flush()``/``session.commit()`` then raises
    PendingRollbackError, masking the real upstream cause as a misleading 500.
    This helper detects that state (transaction open but not active) and
    rolls it back so the caller can keep going on a clean session.

    Returns True if a rollback was performed (caller should re-apply any
    in-memory mutations it relies on, since they were discarded), False if
    the session was already healthy and nothing was changed.

    Hardening (2026-05-04): the original detection used
    ``session.in_transaction() and not session.is_active``. That misses two
    edge cases:
      1. ``in_transaction()`` itself can raise on certain corrupted-state
         sessions (e.g. mid-savepoint failure with no clean parent txn).
      2. The session may be ``is_active == True`` but have an explicit
         ``_rollback_only`` flag set on the underlying SyncSession — newer
         SQLAlchemy versions use this to communicate "must rollback" to
         async wrappers without flipping is_active immediately.
    Both are now caught by an additional ``except Exception`` around the
    state probe and an unconditional best-effort rollback when any flag
    indicates trouble.
    """
    try:
        in_tx = session.in_transaction()
    except Exception as _probe_exc:
        # State probe itself failed — session is in an unknown bad state.
        # Force a rollback unconditionally; if it fails too, return True so
        # the caller knows the session may still be poisoned.
        logger.warning(
            f"[STATUS] session.in_transaction() probe failed for {task_id_str}: "
            f"{_probe_exc}; attempting unconditional rollback"
        )
        with contextlib.suppress(Exception):
            await session.rollback()
        return True

    try:
        # Standard pending-rollback detection: open transaction marked inactive.
        if in_tx and not session.is_active:
            await session.rollback()
            return True
        # Belt-and-suspenders: some SQLAlchemy paths set _rollback_only on the
        # underlying sync session without immediately clearing is_active.
        sync_sess = getattr(session, "sync_session", None)
        if sync_sess is not None and getattr(sync_sess, "_rollback_only", False):
            await session.rollback()
            return True
    except Exception as _rb_exc:
        logger.error(
            f"[STATUS] Session recovery rollback failed for {task_id_str}: {_rb_exc}",
            exc_info=True,
        )
        # Best-effort: tell the caller a rollback was attempted so it can
        # decide whether to re-apply state or bail.
        return True
    return False


async def maybe_spawn_smoketest_fix(
    task: Task,
    payload: AgentStatusPayload,
    session: AsyncSession,
    task_id_str: str,
    *,
    locked_project: Project | None,
) -> None:
    """Handle SMOKETEST_RESULT: store in plan_output and on failure spawn a fix plan."""
    # 3b. Parse and store smoketest result in task.plan_output + document in history
    if payload.artifact_type == ArtifactType.SMOKETEST_RESULT and payload.logs:
        try:
            smoketest_data = await asyncio.to_thread(json.loads, payload.logs)
            # smoketest_data is always a dict (keys: passed, tests, suites) — query_findings_index
            # skips non-list plan_output, so smoketest tasks are excluded from the Findings Journal.
            task.plan_output = smoketest_data
            session.add(task)
            # Document every smoketest result (pass or fail) in task history
            all_tests = smoketest_data.get("tests", [])
            pass_count = len([t for t in all_tests if t.get("status") == "pass"])
            fail_count = len([t for t in all_tests if t.get("status") == "fail"])
            session.add(
                TaskHistory(
                    task_id=task.id,
                    action="SMOKETEST_RESULT",
                    details={
                        "passed": smoketest_data.get("passed", False),
                        "total_tests": len(all_tests),
                        "passed_count": pass_count,
                        "failed_count": fail_count,
                        "suite_results": smoketest_data.get("suites", {}),
                        "guardrail_violations": smoketest_data.get(
                            "guardrail_violations", []
                        ),
                    },
                )
            )
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(
                f"[SMOKETEST] Failed to parse smoketest result for task {task_id_str}: {e}",
                exc_info=True,
            )

    # 3b-ii. Qualifier-smoketest finalization is intentionally NOT done here.
    #
    # It used to live here, gated on ``artifact_type == SMOKETEST_RESULT``, and
    # had two bugs that wedged the ReleaseVersion at ``{status:"pending"}``:
    #   (1) a qualifier-smoketest that failed WITHOUT posting a SMOKETEST_RESULT
    #       (harness ERROR_LOG, VM crash, reaper kill) never reached the hook,
    #       so finalization never fired when it was the last child to go
    #       terminal; and
    #   (2) even on the happy path the SMOKETEST_RESULT Artifact was only
    #       ``session.add(...)``-ed (~30 lines up) and NOT flushed — and
    #       autoflush is disabled for this whole handler (see :859). The
    #       finalize SELECT (``_latest_artifact_logs``) therefore could not see
    #       the pending Artifact → ``_score_smoketest(None)`` → hard blocker →
    #       a passing candidate recorded ``not_promoted``.
    #
    # Both are fixed by relocating finalization into ``_process_post_commit``
    # (task_lifecycle.py), which runs AFTER the early commit on a fresh session
    # — so the Artifact and the task's terminal status are already committed and
    # visible to the finalize SELECT — and runs for COMPLETED / FAILED /
    # CANCELLED alike, regardless of ``artifact_type``.

    # 3b-iii. On smoketest failure, spawn a plan task and create a persistent
    # alert — skipped for qualifier smoketests. A qualifier-smoketest is a
    # pre-promotion qualifier input: its outcome feeds the composite
    # qualification score (finalized in _process_post_commit), NOT the
    # production fix-spawn loop, so it must not spawn a fix plan here.
    if (
        payload.artifact_type == ArtifactType.SMOKETEST_RESULT
        and payload.status == TaskStatus.FAILED
        and payload.logs
        and not (task.context or {}).get("qualifier_smoketest")
    ):
        _smoketest_admission = automatic_task_creation_decision(locked_project)
        if _smoketest_admission != AUTOMATIC_TASK_CREATION_ALLOWED:
            task.context = {
                **(task.context or {}),
                "automatic_fix_creation_suppressed": _smoketest_admission,
            }
            flag_modified(task, "context")
            session.add(task)
            logger.info(
                "[SMOKETEST] Suppressed automatic fix for task %s: %s",
                task_id_str,
                _smoketest_admission,
            )
            return

        # _smoketest_spawn_state captures the Task constructor kwargs before the
        # first session.flush() so that the except block can rebuild and retry the
        # spawn exactly once on the recovered session without re-running the
        # expensive project/config lookups.
        _smoketest_spawn_state: dict | None = None
        try:
            smoketest_data = await asyncio.to_thread(json.loads, payload.logs)
            if not smoketest_data.get("passed"):
                failed_tests = [
                    t
                    for t in smoketest_data.get("tests", [])
                    if t.get("status") == "fail"
                ]
                if failed_tests:
                    # Duplicate prevention: skip if a fix task already exists for this smoketest
                    existing_fix = (
                        (
                            await session.execute(
                                select(Task)
                                .where(Task.parent_task_id == task.id)
                                .limit(1)
                            )
                        )
                        .scalars()
                        .first()
                    )
                    if existing_fix:
                        logger.info(
                            f"[SMOKETEST] Fix task {existing_fix.id} already exists for smoketest {task.id}, skipping duplicate"
                        )
                    else:
                        # A base_branch==promotion_target smoketest is a post-promotion
                        # (production) failure only when the LTS model is active.
                        # The LTS model is active when the integration trunk and
                        # promotion target are distinct branches.
                        _smoketest_project = locked_project
                        if _smoketest_project is None:
                            logger.warning(
                                "[SMOKETEST] project not found for task %s "
                                "(project_id=%s) — skipping fix spawn",
                                task.id,
                                task.project_id,
                            )
                        else:
                            _promotion_target = _get_project_promotion_target(
                                _smoketest_project
                            )
                            _integration_branch = _get_project_integration_branch(
                                _smoketest_project
                            )
                            base_branch = (task.first_payload).get(
                                "base_branch"
                            ) or _integration_branch
                            is_post_promotion = base_branch == _promotion_target and (
                                _integration_branch != _promotion_target
                            )
                            failure_summary = "\n".join(
                                f"- {t['name']}: {t.get('message', 'no details')}"
                                for t in failed_tests
                            )

                            # Determine priority based on which suite failed (higher = dispatched first)
                            suite_results = smoketest_data.get("suites", {})
                            if suite_results.get("startup", {}).get("passed") is False:
                                fix_priority = 90  # Startup failure — critical
                            elif (
                                suite_results.get("curated", {}).get("passed") is False
                            ):
                                fix_priority = 70  # Curated test failure — high
                            else:
                                fix_priority = 50  # Regression-only failure — normal

                            # Post-promotion (production) smoketest failures are urgent:
                            # override suite-based ladder so priority is always >= 90.
                            if is_post_promotion:
                                fix_priority = max(fix_priority, 90)

                            type_model = (
                                _runtime_config.get("task_type_models", {}).get("plan")
                                or None
                            )
                            type_effort = (
                                _runtime_config.get("task_type_efforts", {}).get("plan")
                                or None
                            )
                            plan_task = Task(
                                project_id=task.project_id,
                                title=(
                                    f"[HOTFIX] Fix post-promotion smoketest failures on {base_branch}"
                                    if is_post_promotion
                                    else f"Fix smoketest failures on {base_branch}"
                                ),
                                description=f"{len(failed_tests)} smoke test(s) failed after merge to {base_branch}",
                                parent_task_id=task.id,
                                priority=fix_priority,
                                payloads=[
                                    _validate_internal_payload(
                                        {
                                            "type": "plan",
                                            "prompt": (
                                                f"Smoke tests failed after a merge to {base_branch}. "
                                                f"Investigate and plan fixes for these failures:\n\n"
                                                f"{failure_summary}\n\n"
                                                f"IMPORTANT GUARDRAILS:\n"
                                                f"- Do NOT delete or skip tests just to make them pass\n"
                                                f"- First diagnose: is the failure in the CODE or the TEST?\n"
                                                f"- If the test is wrong, fix it to correctly test the intended behavior\n"
                                                f"- If the code is wrong, fix the code\n"
                                                f"- Tests may only be removed if the tested functionality was removed — explain why\n"
                                                f"- Do NOT weaken assertions without justification\n"
                                                f"Diagnose root causes, identify which recent changes caused each failure, "
                                                f"and produce subtasks to fix them."
                                            ),
                                            "base_branch": base_branch,
                                            **(
                                                {"hotfix": True}
                                                if is_post_promotion
                                                else {}
                                            ),
                                            **(
                                                {"model": type_model}
                                                if type_model
                                                else {}
                                            ),
                                            **(
                                                {"effort": type_effort}
                                                if type_effort
                                                else {}
                                            ),
                                        },
                                        "main:smoketest_fix_plan",
                                    )
                                ],
                                context={
                                    "smoketest_task_id": str(task.id),
                                    "failed_tests": [t["name"] for t in failed_tests],
                                    "suite_results": suite_results,
                                    "guardrail_violations": smoketest_data.get(
                                        "guardrail_violations", []
                                    ),
                                    "total_tests": len(smoketest_data.get("tests", [])),
                                    "passed_count": len(
                                        [
                                            t
                                            for t in smoketest_data.get("tests", [])
                                            if t.get("status") == "pass"
                                        ]
                                    ),
                                    "failed_count": len(failed_tests),
                                    **({"hotfix": True} if is_post_promotion else {}),
                                },
                                vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
                                status=TaskStatus.QUEUED,
                                created_by="ORCHESTRATOR",
                                origination=TASK_ORIGINATION_SMOKETEST,
                            )
                            _smoketest_spawn_state = {
                                "project_id": task.project_id,
                                "title": plan_task.title,
                                "description": plan_task.description,
                                "parent_task_id": task.id,
                                "priority": fix_priority,
                                "payloads": plan_task.payloads,
                                "context": plan_task.context,
                                "status": TaskStatus.QUEUED,
                                "created_by": "ORCHESTRATOR",
                                "failed_tests": failed_tests,
                            }
                            session.add(plan_task)
                            await session.flush()
                            history = TaskHistory(
                                task_id=plan_task.id,
                                action="SPAWNED",
                                details={
                                    "spawned_by": "SMOKETEST_FAILURE",
                                    "priority": fix_priority,
                                    "failed_tests": [t["name"] for t in failed_tests],
                                },
                            )
                            session.add(history)

                            # Build suite breakdown for alert message
                            suite_detail = ""
                            for suite_name, suite_data in suite_results.items():
                                if (
                                    isinstance(suite_data, dict)
                                    and suite_data.get("passed") is False
                                ):
                                    suite_fails = [
                                        t
                                        for t in suite_data.get("tests", [])
                                        if t.get("status") == "fail"
                                    ]
                                    if suite_fails:
                                        suite_detail += f" [{suite_name}: {len(suite_fails)} failed]"

                            alert = Alert(
                                project_id=task.project_id,
                                severity=AlertSeverity.ERROR,
                                title=(
                                    f"[HOTFIX] Post-promotion smoke tests failed on {base_branch}"
                                    if is_post_promotion
                                    else f"Smoke tests failed on {base_branch}"
                                ),
                                message=(
                                    "[POST-PROMOTION PRODUCTION FAILURE] "
                                    if is_post_promotion
                                    else ""
                                )
                                + f"{len(failed_tests)} test(s) failed: {', '.join(t['name'] for t in failed_tests[:5])}"
                                + (
                                    f" (+{len(failed_tests) - 5} more)"
                                    if len(failed_tests) > 5
                                    else ""
                                )
                                + (suite_detail if suite_detail else ""),
                                task_id=task.id,
                                event_type="task.failed",
                            )
                            session.add(alert)

                            await broadcast_to_project(
                                str(task.project_id),
                                "task_updated",
                                {
                                    "task_id": str(plan_task.id),
                                    "status": TaskStatus.QUEUED,
                                },
                            )
                            await broadcast_to_project(
                                str(task.project_id),
                                "alert_created",
                                {
                                    "alert_id": str(alert.id),
                                    "severity": alert.severity,
                                    "title": alert.title,
                                },
                            )
                            logger.error(
                                f"[SMOKETEST] Spawned {'hotfix ' if is_post_promotion else ''}plan task {plan_task.id} (priority={fix_priority}) + alert for {len(failed_tests)} failed smoke tests"
                            )
        except Exception as e:
            logger.error(
                f"[SMOKETEST] Failed to spawn plan task after smoketest failure for task {task_id_str}: {e}",
                exc_info=True,
            )
            # Snapshot scalars BEFORE the rollback below: rollback expires
            # every loaded attribute on ``task``, so a later ``task.id`` /
            # ``task.project_id`` read (the spawn-failed alert path) would
            # trigger a sync lazy-load — MissingGreenlet under the async
            # engine. ``__dict__`` reads only — never attribute access — so
            # this can't itself trip a load on a poisoned session.
            _task_pk = task.__dict__.get("id")
            _project_pk = task.__dict__.get("project_id")
            # If the failure was the session.flush() at line ~905 (FK violation,
            # serialization conflict), the session is now in pending-rollback
            # state and any subsequent session op (including session.add below
            # and downstream commits) raises PendingRollbackError. Recover the
            # session, then re-apply the in-memory task mutations from the
            # status-update block (lines ~623-671).
            _rolled_back = await _recover_poisoned_session(session, task_id_str)
            if _rolled_back:
                # The rollback expired EVERY loaded attribute on ``task``. The
                # state re-apply just below (and the retry flush after it) then
                # operate on an expired instance: the first attribute
                # read/mutation that needs a value triggers a SYNC lazy-load,
                # which autoflushes OUTSIDE the greenlet and poisons the retry
                # flush (MissingGreenlet → PendingRollbackError, no plan task
                # ever spawned). Re-populate ``task`` from its DB row first so
                # every downstream read/mutation and the flush see a live
                # instance. populate_existing refreshes the same identity-map
                # object in place, so the ``task`` reference stays valid.
                if _task_pk is not None:
                    with contextlib.suppress(Exception), session.no_autoflush:
                        await session.execute(
                            select(Task)
                            .where(Task.id == _task_pk)
                            .execution_options(populate_existing=True)
                        )
                task.status = payload.status
                if payload.logs and (
                    not payload.artifact_type or payload.status == TaskStatus.FAILED
                ):
                    task.latest_status_message = payload.logs[-8000:]

            # Retry the spawn once on the recovered session before alerting the
            # operator. Transient serialization conflicts / poisoned sessions
            # clear after rollback, so a single retry is usually sufficient.
            #
            # Guard: only retry when the session was actually rolled back. If
            # _rolled_back is False the session is still healthy and plan_task
            # is still pending — it will be committed by the outer handler, so
            # spawning again would double-insert a second child task.
            _spawn_retry_succeeded = False
            if _smoketest_spawn_state is not None:
                if not _rolled_back:
                    # Original plan_task survived in the session; no retry needed.
                    _spawn_retry_succeeded = True
                    logger.info(
                        "[SMOKETEST] Retry skipped — session not rolled back; "
                        "original plan_task still pending for task %s",
                        task_id_str,
                    )
                else:
                    try:
                        # Rollback released the original Project/Task locks.
                        # Persist the re-applied terminal status, then reacquire
                        # Project -> parent Task before retrying the child INSERT.
                        session.add(task)
                        await session.commit()
                        async with automatic_task_creation_parent_lock(
                            session,
                            _smoketest_spawn_state["project_id"],
                            _smoketest_spawn_state["parent_task_id"],
                            skip_locked=False,
                        ) as (_retry_admission, _retry_project, _retry_parent):
                            if _retry_project is None or _retry_parent is None:
                                raise RuntimeError(
                                    "smoketest retry could not lock project/parent"
                                )

                        # Make the post-commit refresh explicit at this call
                        # site.  The combined lock helper already re-selected
                        # the parent, but a direct ``session.get`` both keeps
                        # the ORM instance fresh after the preceding commit
                        # and makes the MissingGreenlet invariant statically
                        # verifiable without an allowlist exception.
                        task = await session.get(
                            Task,
                            _smoketest_spawn_state["parent_task_id"],
                            populate_existing=True,
                        )
                        if task is None:
                            raise RuntimeError(
                                "smoketest retry parent disappeared after lock"
                            )

                        if _retry_admission != AUTOMATIC_TASK_CREATION_ALLOWED:
                            task.context = {
                                **(task.context or {}),
                                "automatic_fix_creation_suppressed": _retry_admission,
                            }
                            flag_modified(task, "context")
                            session.add(task)
                            _spawn_retry_succeeded = True
                            logger.info(
                                "[SMOKETEST] Retry fix suppressed for task %s: %s",
                                task_id_str,
                                _retry_admission,
                            )
                        else:
                            _retry_task = Task(
                                project_id=_smoketest_spawn_state["project_id"],
                                title=_smoketest_spawn_state["title"],
                                description=_smoketest_spawn_state["description"],
                                parent_task_id=_smoketest_spawn_state["parent_task_id"],
                                priority=_smoketest_spawn_state["priority"],
                                payloads=_smoketest_spawn_state["payloads"],
                                context=_smoketest_spawn_state["context"],
                                vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
                                status=_smoketest_spawn_state["status"],
                                created_by=_smoketest_spawn_state["created_by"],
                                origination=TASK_ORIGINATION_SMOKETEST,
                            )
                            session.add(_retry_task)
                            await session.flush()
                            session.add(
                                TaskHistory(
                                    task_id=_retry_task.id,
                                    action="SPAWNED",
                                    details={
                                        "spawned_by": "SMOKETEST_FAILURE",
                                        "priority": _smoketest_spawn_state["priority"],
                                        "failed_tests": [
                                            t["name"]
                                            for t in _smoketest_spawn_state["failed_tests"]
                                        ],
                                        "retry": True,
                                    },
                                )
                            )
                            _spawn_retry_succeeded = True
                            logger.info(
                                "[SMOKETEST] Retry spawn succeeded for task %s -> plan task %s",
                                task_id_str,
                                _retry_task.id,
                            )
                    except Exception as _retry_err:
                        logger.error(
                            "[SMOKETEST] Retry spawn also failed for task %s: %s",
                            task_id_str,
                            _retry_err,
                            exc_info=True,
                        )
                        await _recover_poisoned_session(session, task_id_str)

            if not _spawn_retry_succeeded:
                try:
                    spawn_alert = Alert(
                        project_id=_project_pk,
                        severity=AlertSeverity.ERROR,
                        title=f"Smoketest fix-plan spawn failed for task {task_id_str}",
                        message=(
                            f"A smoketest reported failures on task {task_id_str} but the "
                            f"follow-up plan task could not be spawned: {type(e).__name__}: {e!r}. "
                            f"Operator must manually create a plan task to address the smoketest failures."
                        )[:1000],
                        task_id=_task_pk,
                        event_type="task.failed",
                    )
                    session.add(spawn_alert)
                    await broadcast_to_project(
                        str(_project_pk),
                        "alert_created",
                        {
                            "alert_id": str(spawn_alert.id),
                            "severity": spawn_alert.severity,
                            "title": spawn_alert.title,
                        },
                    )
                except Exception as alert_err:
                    logger.error(
                        f"[SMOKETEST] Failed to create spawn-failed alert for task {task_id_str}: {alert_err}",
                        exc_info=True,
                    )
            session.add(task)


async def maybe_spawn_debug_create(
    task: Task,
    payload: AgentStatusPayload,
    session: AsyncSession,
    task_id_str: str,
    *,
    locked_project: Project | None,
) -> None:
    """Handle DEBUG_RESULT: store in plan_output and if confidence is high spawn a fix task."""
    # 3c. Parse debug result, store in plan_output, and optionally spawn a fix task
    if payload.artifact_type == ArtifactType.DEBUG_RESULT and payload.logs:
        try:
            debug_data = await asyncio.to_thread(json.loads, payload.logs)
            # debug_data is always a dict (keys: confidence, fix_prompt, root_cause) — query_findings_index
            # skips non-list plan_output, so debug tasks are excluded from the Findings Journal.
            task.plan_output = debug_data
            session.add(task)

            # If confidence is high, auto-spawn a create task using the fix_prompt
            if debug_data.get("confidence") == "high" and debug_data.get("fix_prompt"):
                _debug_admission = automatic_task_creation_decision(locked_project)
                if _debug_admission != AUTOMATIC_TASK_CREATION_ALLOWED:
                    task.context = {
                        **(task.context or {}),
                        "automatic_fix_creation_suppressed": _debug_admission,
                    }
                    flag_modified(task, "context")
                    session.add(task)
                    logger.info(
                        "[DEBUG] Suppressed automatic fix for task %s: %s",
                        task_id_str,
                        _debug_admission,
                    )
                    return

                failed_task_id = (task.context or {}).get("failed_task_id", "")
                _debug_project = locked_project
                base_branch = (task.first_payload).get(
                    "base_branch"
                ) or _get_project_integration_branch(_debug_project)
                debug_title = task.title or ""
                fix_title = (
                    debug_title.removeprefix("Debug: ")
                    if debug_title.startswith("Debug: ")
                    else debug_title
                )

                # Dedup: if _requeue_parent_task already replaced the failed task with a
                # clone (marked by _replaced_by_task_id on the cancelled child), spawning
                # a second Fix: replacement here creates duplicate work that races.
                _failed_task_obj = None
                _skip_fix_spawn = False
                if failed_task_id:
                    try:
                        _failed_uuid = uuid.UUID(failed_task_id)
                        _failed_task_obj = (
                            await session.execute(
                                select(Task).where(Task.id == _failed_uuid)
                            )
                        ).scalar_one_or_none()
                        if (
                            _failed_task_obj is not None
                            and _failed_task_obj.status == TaskStatus.CANCELLED
                            and (_failed_task_obj.context or {}).get(
                                "_replaced_by_task_id"
                            )
                        ):
                            _skip_fix_spawn = True
                            _replacement_id = (_failed_task_obj.context or {}).get(
                                "_replaced_by_task_id", ""
                            )
                            logger.info(
                                "[DEBUG] Skipping Fix: spawn — failed task %s already replaced "
                                "by requeue (→ task %s)",
                                failed_task_id[:8],
                                _replacement_id[:8],
                            )
                    except Exception:
                        logger.exception(
                            "[DEBUG] Failed to check failed task status for dedup (debug task %s)",
                            task_id_str,
                        )

                if not _skip_fix_spawn:
                    fix_task = Task(
                        project_id=task.project_id,
                        title=f"Fix: {fix_title}",
                        description=debug_data["fix_prompt"],
                        parent_task_id=task.parent_task_id or task.id,
                        payloads=[
                            _validate_internal_payload(
                                {
                                    "type": "create",
                                    "prompt": debug_data["fix_prompt"],
                                    "base_branch": base_branch,
                                },
                                "main:debug_auto_fix",
                            )
                        ],
                        context={
                            "debug_task_id": str(task.id),
                            "failed_task_id": failed_task_id,
                            "root_cause": debug_data.get("root_cause", ""),
                        },
                        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
                        status=TaskStatus.QUEUED,
                        created_by="ORCHESTRATOR",
                        origination=TASK_ORIGINATION_REMEDIATION,
                    )
                    session.add(fix_task)
                    await session.flush()

                    # Mark the failed task so _requeue_parent_task can see the debug-fix
                    # path already claimed it and avoid spawning a second replacement.
                    if (
                        _failed_task_obj is not None
                        and _failed_task_obj.status == TaskStatus.FAILED
                    ):
                        _failed_task_obj.context = {
                            **(_failed_task_obj.context or {}),
                            "_debug_fix_spawned_task_id": str(fix_task.id),
                        }
                        flag_modified(_failed_task_obj, "context")
                        session.add(_failed_task_obj)

                    await broadcast_to_project(
                        str(task.project_id),
                        "task_updated",
                        {
                            "task_id": str(fix_task.id),
                            "status": TaskStatus.QUEUED,
                            "title": fix_task.title,
                        },
                    )
                    logger.info(
                        f"[DEBUG] Spawned fix task {fix_task.id} from debug analysis (high confidence)"
                    )
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(
                f"[DEBUG] Failed to parse debug result for task {task_id_str}: {e}",
                exc_info=True,
            )
            # Same poisoning concern as the smoketest spawn block: the
            # session.flush() at line ~1018 can raise on FK/serialization,
            # leaving the session in pending-rollback state and breaking
            # downstream commits. Recover and re-apply core task mutations.
            if await _recover_poisoned_session(session, task_id_str):
                task.status = payload.status
                if payload.logs and (
                    not payload.artifact_type or payload.status == TaskStatus.FAILED
                ):
                    task.latest_status_message = payload.logs[-8000:]
                session.add(task)
