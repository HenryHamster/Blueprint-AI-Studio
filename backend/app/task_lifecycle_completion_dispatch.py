"""Registry-driven completion-handler dispatch, extracted from task_lifecycle.py.

S3 of the fdd07b88 decomposition plan (knowledge doc
``fdd07b88-8bc5-4db3-89e0-016075fb7563``). This is the completion-dispatch
region that runs inside the ``payload.status == TaskStatus.COMPLETED`` branch
of ``_process_post_commit``, immediately before Stage T (the terminal tail,
S2, ``task_lifecycle_post_commit_terminal.py``): it looks up the concrete
completion handler for the task's type via the capability registry, invokes
it, and on failure recovers (requeue-on-transient, FAILED-with-classification,
or preserving the post-commit-pending marker for sweep retry).

Free-variable set (per the design doc's live-in analysis): ``task``,
``task_type``, ``task_id``, ``payload``, ``session``, ``locked_project``,
``preallocated_strategic_message_sequences``, ``_peek_capability``,
``_peek_task_type``, ``_expected_post_commit_claim_token``,
``_expected_childless_recovery_token``, and the ``_rollback_and_refetch_task``
closure (passed in as ``refetch`` — it closes over ``session``/``task_id``
only, so it is trivially passable).

Unlike Stage T, this is NOT the last stage — the terminal tail always runs
after it (on the non-early-return path). So this stage returns
``(task, response)``: ``task`` is the (possibly re-fetched/mutated) task the
caller must keep using, and ``response`` is the dict the caller should return
immediately, or ``None`` when dispatch succeeded/no-op and the caller should
fall through to Stage T.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from typing import Awaitable, Callable

from sqlalchemy.exc import (
    DisconnectionError,
    InterfaceError,
    InternalError,
    OperationalError,
    PendingRollbackError,
    TimeoutError as SQLAlchemyTimeoutError,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.broadcast import _sanitize_log
from app.models import Project, Task, TaskStatus
from app.post_commit_errors import PostCommitFinalCommitFailed
from app.runtime_config_state import runtime_config as _runtime_config
from app.schemas import AgentStatusPayload
from app.task_blocked import reset_task_for_fresh_attempt
from app.task_capabilities import get_task_capability

logger = logging.getLogger(__name__)


def _is_transient_completion_handler_error(exc: BaseException) -> bool:
    """Classify retryable completion failures without relying on message text."""
    if isinstance(
        exc,
        (
            PendingRollbackError,
            InterfaceError,
            InternalError,
            OperationalError,
            DisconnectionError,
            SQLAlchemyTimeoutError,
        ),
    ) or bool(getattr(exc, "connection_invalidated", False)):
        return True

    err_str = str(exc).lower()
    return any(
        kw in err_str
        for kw in [
            "api.github.com",
            "502",
            "503",
            "connection",
            "timeout",
            "rate limit",
            "ssl",
            "name resolution",
            "connection refused",
            "greenlet",  # post-rollback expired-attr lazy-load; retryable
        ]
    )


async def run_completion_dispatch(
    task: Task,
    *,
    task_type: str,
    payload: AgentStatusPayload,
    task_id: uuid.UUID,
    session: AsyncSession,
    locked_project: Project | None,
    preallocated_strategic_message_sequences: list[int],
    _peek_capability,
    _peek_task_type: str,
    _expected_post_commit_claim_token: str | None,
    _expected_childless_recovery_token: str | None,
    refetch: Callable[[], Awaitable[Task | None]],
) -> tuple[Task, dict | None]:
    """Dispatch to the registry-bound completion handler and recover on failure.

    Returns ``(task, response)``. When ``response`` is not ``None`` the caller
    must return it immediately (matching the early ``return`` statements in
    the original inline code); otherwise the caller continues to Stage T with
    the returned (possibly re-fetched) ``task``.
    """
    # Deferred imports so test patches on app.task_completion.* /
    # app.strategic_completion.* / app.research_completion.* /
    # app.knowledge_completion.* keep intercepting the call (matches the
    # original function-local imports at the top of _process_post_commit).
    from app.task_completion import (
        _handle_plan_completion,
        _handle_design_doc_completion,
        _handle_security_audit_completion,
        _handle_perf_completion,
        _handle_compute_experiment_completion,
        _handle_self_audit_completion,
        _handle_memo_completion,
        _handle_repo_bootstrap_completion,
        _handle_capability_proof_trial_completion,
        _handle_criterion_named_test_trial_completion,
        _handle_walkthrough_e2e_completion,
    )
    from app.strategic_completion import _handle_strategic_completion
    from app.research_completion import _handle_research_completion
    from app.knowledge_completion import (
        _handle_design_completion,
        _handle_web_research_completion,
    )
    from app.discovery_review import _handle_discovery_review_completion
    from app.task_lifecycle import _safe_exception_message

    # Completion handlers — wrapped in try/except to prevent VM leak.
    # If a handler throws after the early commit, BackgroundTasks (including
    # delete_agent_vm) won't run on a 500 response. We catch, log, and ensure
    # VM cleanup via asyncio.create_task as a fallback.
    try:
        # Registry-driven completion dispatch. The task-type registry
        # DECLARES which logical completion handler each type routes to
        # (TaskCapability.completion_handler); this map binds each
        # logical name to its concrete handler function. Adding a type
        # with a completion handler is therefore a single registry edit
        # plus a binding here — no new if/elif branch. This replaces the
        # former hand-maintained if/elif cascade; behaviour is identical
        # (each task type still routes to exactly the same handler with
        # the same log/project arguments).
        _completion_handlers = {
            "plan": _handle_plan_completion,
            "design_doc": _handle_design_doc_completion,
            "strategic": _handle_strategic_completion,
            "research": _handle_research_completion,
            "security_audit": _handle_security_audit_completion,
            "perf": _handle_perf_completion,
            "compute_experiment": _handle_compute_experiment_completion,
            "self_audit": _handle_self_audit_completion,
            "memo": _handle_memo_completion,
            "web_research": _handle_web_research_completion,
            "design": _handle_design_completion,
            "discovery_review": _handle_discovery_review_completion,
            "repo_bootstrap": _handle_repo_bootstrap_completion,
            "capability_proof_trial": _handle_capability_proof_trial_completion,
            "criterion_named_test_trial": _handle_criterion_named_test_trial_completion,
            "walkthrough_e2e": _handle_walkthrough_e2e_completion,
        }
        _capability = (
            _peek_capability
            if task_type == _peek_task_type
            else get_task_capability(task_type)
        )
        _handler_name = _capability.completion_handler if _capability else None
        if _handler_name:
            _handler = _completion_handlers[_handler_name]
            _handler_logs = payload.logs or task.latest_status_message or ""
            # Project-mutating completion handlers require the canonically
            # pre-locked Project. ``locked_project`` was
            # acquired in Project → Task order at the top of
            # _process_post_commit via ``lock_project_then_task`` (see
            # docs/concurrency.md); passing it lets the handler skip its
            # own re-acquisition. None propagates the legacy
            # bounded-retry path inside the handler (defensive only).
            if _handler_name == "strategic":
                await _handler(
                    task,
                    _handler_logs,
                    session,
                    project=locked_project,
                    message_sequences=preallocated_strategic_message_sequences,
                )
            elif _handler_name == "research":
                await _handler(
                    task, _handler_logs, session, project=locked_project
                )
            elif _capability.completion_needs_locked_project:
                await _handler(
                    task, _handler_logs, session, project=locked_project
                )
            else:
                await _handler(task, _handler_logs, session)
    except Exception as handler_exc:
        # NB: this log line must use the plain ``task_id`` argument,
        # NOT ``task.id`` — when the handler poisoned the session AND
        # expired ``task`` (an internal rollback inside the handler),
        # the attribute read issues a refresh against the aborted
        # transaction and re-raises PendingRollbackError OUT of this
        # except block, skipping all recovery below (prod burst
        # 2026-06-07 00:56).
        logger.error(
            f"[STATUS] Completion handler failed for {task_type} task {task_id}: {handler_exc!r}",
            exc_info=True,
        )
        # A handler that raised on a DB error (asyncpg QueryCanceledError
        # from statement_timeout, LockNotAvailableError, a closed
        # connection, …) leaves the session in an aborted transaction.
        # Every recovery write below — and the downstream
        # rollup/aggregation/final-commit sections — then fail on the
        # poisoned session ("current transaction is aborted" / "raised as
        # a result of Query-invoked autoflush"), so the requeue/FAIL never
        # persists and a task the agent reported terminal is silently
        # stranded with its work undispatched (research findings, plan
        # children, …). Production 2026-05-29: a research task hit a 120s
        # statement_timeout in _handle_research_completion, the requeue
        # commit then died on autoflush, and the task stuck COMPLETED with
        # 0 children and its findings undispatched. Roll the poisoned
        # transaction back so recovery runs on a clean transaction.
        # rollback() expires every ORM instance, so re-fetch the task by
        # its (stable, argument-supplied) id before reading or mutating
        # it — attribute access on the now-expired instance would issue a
        # synchronous lazy-load and raise MissingGreenlet.
        _recovered_task = await refetch()
        if _recovered_task is None:
            # Row vanished (archived/deleted) during post-commit — nothing
            # left to recover.
            logger.warning(
                "[HANDLER] Task %s gone after handler failure; skipping recovery",
                task_id,
            )
            return task, {"status": "accepted"}
        task = _recovered_task
        if _expected_post_commit_claim_token is not None:
            from app.review_execution_ownership import (
                POST_COMMIT_CONTINUATION_STATUSES,
            )

            pending_meta = dict(
                (task.context or {}).get("_post_commit_pending_meta") or {}
            )
            claim_is_current = bool(
                task.post_commit_pending
                and not task.archived
                and task.status in POST_COMMIT_CONTINUATION_STATUSES
                and pending_meta.get("claim_token")
                == _expected_post_commit_claim_token
            )
            if not claim_is_current:
                logger.info(
                    "[POST_COMMIT SWEEP] Pending recovery claim for task %s "
                    "lost authority during completion handler; preserving "
                    "the newer lifecycle state",
                    str(task_id)[:8],
                )
                return task, {"status": "accepted", "recovery_skipped": True}
        if _expected_childless_recovery_token is not None:
            recovery_meta = dict(
                (task.context or {}).get(
                    "_childless_post_commit_recovery"
                )
                or {}
            )
            claim_is_current = (
                task.status == TaskStatus.EVALUATING
                and not task.archived
                and not task.post_commit_pending
                and recovery_meta.get("claim_token")
                == _expected_childless_recovery_token
            )
            if not claim_is_current:
                logger.info(
                    "[POST_COMMIT SWEEP] Recovery claim for task %s "
                    "lost authority during completion handler; preserving "
                    "the newer lifecycle state",
                    str(task_id)[:8],
                )
                return task, {
                    "status": "accepted",
                    "recovery_skipped": True,
                }
            # The outer marker-less recovery loop owns retry accounting
            # and terminalization. Preserve EVALUATING and propagate the
            # handler error so that loop can record this exact claim's
            # failure without re-running the agent or failing early.
            raise
        # Detect transient errors (GitHub API, DB, network) and requeue instead
        # of FAILED.
        # SQLAlchemy's transaction-state and driver wrappers must be matched by
        # type: PendingRollbackError deliberately omits the original DB error
        # from its short message, and asyncpg InternalClientError is surfaced as
        # InternalError.  String-only matching turned the successful d8ae77b0
        # design-doc task into terminal FAILED after a transient disconnect.
        is_transient = _is_transient_completion_handler_error(handler_exc)
        # Research task interrupted after durable routing began:
        # _research_spawn_pending remains set until the authoritative final
        # receipt commits, including for carried-only batches whose pre-gate
        # plan_output is legitimately empty.
        # Requeuing the agent would discard those findings and start a new
        # research cycle. Instead raise PostCommitFinalCommitFailed — which
        # preserves the post_commit_pending marker so the sweep retries just
        # the durable routing/finalization state without re-running the agent.
        #
        # This recovery is NOT gated on the error being keyword-transient.
        # The previous version only preserved the marker for errors whose
        # string matched a transient keyword (timeout/connection/…), so a
        # mid-spawn failure that did NOT match (a lock contention surfaced
        # as a bare LockNotAvailableError, a SerializationFailure, an
        # autoflush IntegrityError, …) fell through to the FAILED branch
        # below and STRANDED its committed findings: the task showed
        # terminal FAILED / "no actionable findings" with 0 children while
        # plan_output held real, undispatched work — the irrecoverable
        # ~28%-of-zero-child class (cause B). Resuming the spawn is always
        # safe regardless of the original error: the resume path
        # (_handle_research_completion, _research_spawn_pending branch)
        # never re-runs the agent — it replays fresh plan_output and/or the
        # durable carried-finding envelope — and the duplicate-spawn guard
        # makes a re-run that actually did land children a no-op. So whenever
        # routing/finalization was in flight, prefer sweep-retry over a terminal
        # FAILED that buries the work.
        _spawn_pending = bool((task.context or {}).get("_research_spawn_pending"))
        if task_type == "research" and _spawn_pending:
            _stored_count = len(task.plan_output or []) + len(
                (task.context or {}).get("prior_deferred_findings") or []
            )
            logger.warning(
                "[HANDLER] Research task %s: error before final receipt with %d "
                "durable routing input(s) (transient=%s); preserving "
                "post_commit_pending for sweep retry rather than stranding "
                "them on a terminal FAILED "
                "(handler_exc=%r)",
                task_id,
                _stored_count,
                is_transient,
                handler_exc,
            )
            raise PostCommitFinalCommitFailed(
                f"research completion before final receipt failed for {task_id}; "
                f"sweep retries {_stored_count} durable routing input(s): "
                f"{handler_exc!r}"
            ) from handler_exc
        if is_transient and task.attempt_count < _runtime_config["max_retries"]:
            reset_task_for_fresh_attempt(task)
            task.status = TaskStatus.QUEUED
            # The earlier COMPLETED/FAILED assignment fired the SQLAlchemy set
            # event which added task.id to _terminated_task_ids, revoking the
            # JWT. Since the completion handler hit a transient error and we're
            # requeuing, drop the cache entry so the next VM can claim.
            from app.routers.agent import _terminated_task_ids

            _terminated_task_ids.pop(str(task.id), None)
            task.attempt_count += 1
            task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
            # Handler ran after a successful VM provision — reset provision budget
            # so a transient handler failure doesn't consume retries meant for VM issues.
            handler_ctx = dict(task.context or {})
            handler_ctx.pop("provision_failures", None)
            task.context = handler_ctx
            task.latest_status_message = _sanitize_log(
                f"Handler hit transient error, requeuing (attempt {task.attempt_count}): {type(handler_exc).__name__}: {handler_exc!s}"
            )[:500]
            logger.warning(
                f"[HANDLER] Transient error for {task.id}, requeuing: {handler_exc}"
            )
        else:
            task.status = TaskStatus.FAILED
            task.latest_status_message = _safe_exception_message(
                handler_exc, "Completed but handler failed"
            )
            from app.failure_classification import persist_failure_category
            from app.routers.agent_status import record_task_result
            from app.task_lifecycle import classify_failure

            _fc_handler = classify_failure(
                task, at_time=task.last_heartbeat or task.started_at
            )
            persist_failure_category(task, _fc_handler)
            record_task_result(
                success=False,
                failure_class=_fc_handler,
                task_id=str(task.id),
                task_type=(task.first_payload or {}).get("type", ""),
            )
        session.add(task)
        # Commit QUEUED immediately — the subsequent helpers each call
        # _rollback_and_refetch_task() on their own failures, which rolls
        # back the whole transaction and discards any uncommitted status
        # mutation, stranding the task as COMPLETED with work undispatched
        # (prod incident 2026-05-29). Commit now so a later rollback cannot
        # undo the requeue; then return early — all remaining helpers are
        # terminal-only (COMPLETED/FAILED/CANCELLED) and are no-ops for a
        # requeued task.
        if task.status == TaskStatus.QUEUED:
            try:
                await session.commit()
            except Exception as _requeue_commit_exc:
                logger.exception(
                    "[HANDLER] Failed to commit QUEUED state for task %s: %r — "
                    "preserving post_commit_pending for sweep retry",
                    task_id,
                    _requeue_commit_exc,
                )
                with contextlib.suppress(Exception):
                    await session.rollback()
                raise PostCommitFinalCommitFailed(
                    f"requeue commit failed for {task_id}: {_requeue_commit_exc!r}"
                ) from _requeue_commit_exc
            return task, {"status": "received"}

    return task, None
