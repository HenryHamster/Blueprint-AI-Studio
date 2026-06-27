"""Tests for the residual-fingerprint / per-account-failure-class subsystem.

These exercise the cohesive residual subsystem that lives in
``app.failure_residual`` (extracted from ``app.failure_classification``):
the in-memory residual-fingerprint store, message normalization /
fingerprint helpers, the durable ``ResidualRemediationCase`` async upsert,
and the per-account rolling-window failure-class log. The classifier itself
(``classify_failure``) stays in ``app.failure_classification`` and is
imported here only to cover the residual-recording integration paths.
"""

import uuid
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from app.failure_residual import (
    _fingerprint_message,
    _looks_like_error,
    get_account_failure_class_counts,
    get_account_failure_class_records,
    get_top_residual_fingerprints,
    get_top_viewport_residual_fingerprints,
    record_account_failure_class,
    record_residual_fingerprint,
    record_viewport_regression_fingerprint,
)
from app.failure_classification import (
    AGENT_TIMEOUT,
    ALL_FAILURE_CLASSES,
    CODE_ERROR,
    EXTERNAL_API,
    RATE_LIMIT,
    classify_failure,
    persist_failure_category,
)
from app.models import Task


def _task(msg: str = "", ctx: dict | None = None) -> Task:
    t = MagicMock(spec=Task)
    t.latest_status_message = msg
    t.context = ctx or {}
    return t


class TestFingerprintMessage:
    def test_strips_uuid(self):
        fp = _fingerprint_message("task 550e8400-e29b-41d4-a716-446655440000 failed")
        assert "<id>" in fp
        assert "550e8400" not in fp

    def test_strips_numbers(self):
        fp = _fingerprint_message("exit code 137 after 300s")
        assert "<n>" in fp
        assert "137" not in fp

    def test_strips_memory_prefixes(self):
        msg1 = "[16mb] using tool: bash"
        msg2 = "[16mb cg:512mb] using tool: bash"
        assert _fingerprint_message(msg1) == "using tool: bash"
        assert _fingerprint_message(msg2) == "using tool: bash"

    def test_strips_decimal_memory_prefixes(self):
        """Regression: the residual-fingerprint spawner was producing one
        fingerprint per decimal memory reading ([10.5MB] / [10.4MB] / …)
        because the old prefix regex required integer-only `\\d+mb`."""
        msg1 = "[10.5mb] clone complete, configuring workspace..."
        msg2 = "[10.4mb] clone complete, configuring workspace..."
        msg3 = "[10.5MB cg:512.4MB] clone complete, configuring workspace..."
        assert _fingerprint_message(msg1) == _fingerprint_message(msg2)
        assert _fingerprint_message(msg2) == _fingerprint_message(msg3)
        assert "mb" not in _fingerprint_message(msg1)

    def test_strips_embedded_size_tokens(self):
        """Size tokens mid-message ('15.4MB' not as prefix) collapse too."""
        msg1 = "rss 15.4mb heap 12.1mb after retry"
        msg2 = "rss 15.6mb heap 12.3mb after retry"
        assert _fingerprint_message(msg1) == _fingerprint_message(msg2)

    def test_strips_embedded_iec_and_byte_size_tokens(self):
        msg1 = "rss=15.4MiB heap 123456 bytes after retry"
        msg2 = "rss=15.6MiB heap 654321 bytes after retry"
        fp = _fingerprint_message(msg1)
        assert fp == _fingerprint_message(msg2)
        assert "mib" not in fp
        assert "bytes" not in fp

    def test_near_duplicates_cluster(self):
        fp1 = _fingerprint_message("AssertionError: expected 3, got 4")
        fp2 = _fingerprint_message("AssertionError: expected 9, got 12")
        assert fp1 == fp2

    def test_empty_returns_empty_string(self):
        assert _fingerprint_message("") == ""

    def test_truncates_long_messages(self):
        assert len(_fingerprint_message("x" * 500)) <= 200


class TestResidualStore:
    def setup_method(self):
        import app.failure_residual as fr

        fr._residual_store.clear()
        fr._residual_seen_task_ids.clear()
        fr._residual_closed_fingerprints.clear()

    def _task_with_id(self, msg: str, task_id: str, status=None) -> "Task":
        from app.models import TaskStatus

        t = MagicMock(spec=Task)
        t.latest_status_message = msg
        t.context = {}
        t.id = task_id
        # Residual-fingerprint Gate 1 requires FAILED. Tests that need a
        # different status pass it explicitly.
        t.status = status if status is not None else TaskStatus.FAILED
        return t

    def test_record_then_query(self):
        t = self._task_with_id("[10.5MB] Cloning repo...", "task-1")
        record_residual_fingerprint(t, logs="AttributeError: NoneType has no attr 'x'")
        results = get_top_residual_fingerprints(window_seconds=3600)
        assert len(results) == 1
        assert results[0]["count"] == 1
        assert results[0]["task_ids"] == ["task-1"]

    def test_dedup_same_task_id(self):
        t = self._task_with_id("[10.5MB] Cloning...", "task-abc")
        record_residual_fingerprint(t, logs="TypeError: some error")
        record_residual_fingerprint(t, logs="TypeError: some error")
        results = get_top_residual_fingerprints()
        assert results[0]["count"] == 1

    def test_near_duplicates_cluster(self):
        t1 = self._task_with_id("[10.5MB] Cloning...", "task-1")
        t2 = self._task_with_id("[10.5MB] Cloning...", "task-2")
        record_residual_fingerprint(t1, logs="AttributeError: expected 3, got 4")
        record_residual_fingerprint(t2, logs="AttributeError: expected 9, got 12")
        results = get_top_residual_fingerprints()
        assert len(results) == 1
        assert results[0]["count"] == 2

    def test_classify_failure_fallthrough_records(self):
        t = self._task_with_id("[10.5MB] Cloning...", "task-ff")
        # Status alone never drives the fingerprint — pass logs with an
        # error-shaped line.
        result = classify_failure(t, logs="AttributeError: NoneType has no attr 'x'")
        assert result == CODE_ERROR
        results = get_top_residual_fingerprints()
        assert any(
            r["task_ids"] == ["task-ff"] or "task-ff" in r["task_ids"] for r in results
        )

    def test_non_failed_status_does_not_record(self):
        """Gate 1: classify_failure may be called from analytics paths on
        COMPLETED / CANCELLED / EVALUATING tasks (read-only classification).
        Those calls must NOT pollute the residual store with success-shape
        inputs (rollup messages, plan output JSON, review approvals)."""
        from app.models import TaskStatus

        for non_failed_status in (
            TaskStatus.COMPLETED,
            TaskStatus.CANCELLED,
            TaskStatus.EVALUATING,
            TaskStatus.IN_PROGRESS,
        ):
            import app.failure_residual as fr

            fr._residual_store.clear()
            fr._residual_seen_task_ids.clear()
            t = self._task_with_id(
                "TypeError: real error in logs",
                f"task-{non_failed_status}",
                status=non_failed_status,
            )
            record_residual_fingerprint(t, logs="TypeError: real error in logs")
            results = get_top_residual_fingerprints()
            assert results == [], (
                f"status={non_failed_status} should not record, got {results}"
            )

    def test_no_error_line_in_logs_does_not_record(self):
        """Gate 2: no fallback to latest_status_message. If logs lack an
        error-shaped line, no fingerprint is recorded — the status message
        is almost always a benign heartbeat that pollutes the store."""
        t = self._task_with_id("[10.5MB] Cloning org/repo...", "task-noerr")
        record_residual_fingerprint(t)  # no logs at all
        assert get_top_residual_fingerprints() == []
        record_residual_fingerprint(
            t, logs="just some progress info\nnothing exceptional"
        )
        assert get_top_residual_fingerprints() == []

    def test_json_output_does_not_record_even_with_exception_keyword(self):
        """Regression for the 2026-05-04 plan-output false-positive: a
        plan task's serialized JSON output mentioning ``Exception`` inside
        a prompt string (e.g. lint guidance ``avoid except Exception:
        blocks (BLE001)``) used to fingerprint the JSON prefix as a
        CODE_ERROR sample, which then drove auto-remediation tasks that
        ``fixed`` a research prompt rather than a real bug."""
        json_logs = (
            '{"subtasks": [{"prompt": "avoid except Exception: blocks (BLE001) '
            'and prefer specific exception classes"}]}'
        )
        t = self._task_with_id("[10.5MB] Cloning...", "task-json")
        record_residual_fingerprint(t, logs=json_logs)
        assert get_top_residual_fingerprints() == []

    def test_lint_prose_mentioning_exception_does_not_record(self):
        """Substring-anywhere matching used to flag prose like
        ``avoid except Exception: blocks`` as error-shaped. Strict
        line-start patterns require the keyword at a structural position."""
        t = self._task_with_id("[10.5MB] Cloning...", "task-prose")
        record_residual_fingerprint(
            t, logs="avoid except Exception: blocks (BLE001)\nuse specific classes"
        )
        assert get_top_residual_fingerprints() == []

    def test_fingerprint_prefers_error_line_over_status_message(self):
        """Regression: status messages were dominating the fingerprint
        even when structured error text was available in the logs."""
        t1 = self._task_with_id("[10.5mb] Cloning repo...", "task-A")
        t2 = self._task_with_id("[10.4mb] Rebasing onto main...", "task-B")
        # Same error in logs — different status heartbeats. Should cluster.
        err_logs = "TypeError: getProviderRunner is not a function\n  at handleCreate"
        record_residual_fingerprint(t1, logs=err_logs)
        record_residual_fingerprint(t2, logs=err_logs)
        results = get_top_residual_fingerprints()
        assert len(results) == 1, f"expected 1 fingerprint, got {len(results)}"
        assert results[0]["count"] == 2

    def test_status_message_retained_as_context(self):
        """Even when fingerprint is driven by the error line, the agent
        status at failure time is still stored for human display."""
        t = self._task_with_id("[12.3mb] Running tests...", "task-status")
        err_logs = "AssertionError: expected true to be false"
        record_residual_fingerprint(t, logs=err_logs)
        results = get_top_residual_fingerprints()
        assert len(results) == 1
        # sample_message = error line (fingerprint source)
        assert "AssertionError" in results[0]["sample_message"]
        # sample_status = original heartbeat (human context)
        assert "Running tests" in results[0].get("sample_status", "")

    def test_window_filter_excludes_old(self):
        import app.failure_residual as fr

        fr._residual_store["old-fp"] = {
            "count": 5,
            "first_seen": 0.0,
            "last_seen": 0.0,
            "sample_message": "old error",
            "task_ids": ["t-old"],
        }
        results = get_top_residual_fingerprints(window_seconds=60)
        assert all(r["fingerprint"] != "old-fp" for r in results)

    def test_sorted_by_count_descending(self):
        import app.failure_residual as fr
        import time

        now = time.time()
        fr._residual_store["fp-a"] = {
            "count": 3,
            "first_seen": now,
            "last_seen": now,
            "sample_message": "a",
            "task_ids": [],
        }
        fr._residual_store["fp-b"] = {
            "count": 10,
            "first_seen": now,
            "last_seen": now,
            "sample_message": "b",
            "task_ids": [],
        }
        results = get_top_residual_fingerprints()
        assert results[0]["fingerprint"] == "fp-b"
        assert results[1]["fingerprint"] == "fp-a"

    def test_no_record_for_non_code_error(self):
        t = self._task_with_id("No heartbeat received after 300s", "task-timeout")
        result = classify_failure(t)
        assert result == AGENT_TIMEOUT
        results = get_top_residual_fingerprints()
        assert not any("task-timeout" in r["task_ids"] for r in results)

    def test_heartbeat_only_contributions_do_not_record(self):
        """Reversed from prior behavior (2026-05-04): heartbeat-only
        contributions used to be recorded with ``is_error_shaped=False``
        so the spawn-site threshold could filter them. But carrying them
        in the store still polluted the admin-UI residual list and
        reactivated CLOSED_ORGANICALLY DB rows. The strict gate now
        requires an error-shaped log line; heartbeat-only contributions
        are dropped entirely."""
        for benign in (
            "PROVISION_PROGRESS: creds fetched, entering env setup",
            "[12.1MB] Using tool: Grep",
            "Creating pull request...",
            "[10.5MB cg:512MB] Cloning org/repo...",
            "Rebasing onto main...",
            "Capturing test baseline...",
            "Fetching GitHub credentials...",
            "DIAGNOSTIC_TEST",
        ):
            import app.failure_residual as fr

            fr._residual_store.clear()
            fr._residual_seen_task_ids.clear()
            t = self._task_with_id(benign, f"task-benign-{hash(benign)}")
            record_residual_fingerprint(t)
            results = get_top_residual_fingerprints()
            assert results == [], f"{benign!r} should not record, got {results}"

    def test_error_shaped_logs_record_as_error_shaped(self):
        """``[ERROR] ...`` in logs is the agent's explicit error prefix
        (agent/src/index.ts:2606). With strict gating the signal must be
        in the logs argument, not just the status field — but matching is
        unchanged."""
        t = self._task_with_id("[10.5MB] Cloning...", "task-err-logs")
        record_residual_fingerprint(t, logs="[ERROR] connection refused after 30s")
        results = get_top_residual_fingerprints()
        assert len(results) == 1
        assert results[0]["is_error_shaped"] is True

    def test_logs_with_real_error_mark_cluster_error_shaped(self):
        """A heartbeat-shaped status combined with an error-shaped log
        line still produces an error-shaped cluster (fingerprint comes
        from the log line, not the status)."""
        t = self._task_with_id("[10.5MB] Using tool: Bash", "task-real-err")
        err_logs = "TypeError: 'NoneType' object is not callable\n  at handle"
        record_residual_fingerprint(t, logs=err_logs)
        results = get_top_residual_fingerprints()
        assert len(results) == 1
        assert results[0]["is_error_shaped"] is True
        assert "TypeError" in results[0]["sample_message"]

    def test_repeat_error_contributions_cluster_and_stay_error_shaped(self):
        """Two FAILED tasks with the same error line cluster into one
        fingerprint. ``is_error_shaped`` is always True under strict
        gating since unshaped contributions never reach the store."""
        t1 = self._task_with_id("[10.5MB] Cloning...", "task-1")
        t2 = self._task_with_id("[10.5MB] Cloning...", "task-2")
        err = "AttributeError: NoneType has no attr 'x'"
        record_residual_fingerprint(t1, logs=err)
        record_residual_fingerprint(t2, logs=err)
        results = get_top_residual_fingerprints()
        assert len(results) == 1
        assert results[0]["count"] == 2
        assert results[0]["is_error_shaped"] is True

    def test_looks_like_error_predicate(self):
        # Real error-shaped lines.
        assert _looks_like_error("AttributeError: NoneType has no attr 'x'")
        assert _looks_like_error("TypeError: foo is not a function")
        assert _looks_like_error("[ERROR] connection refused")
        assert _looks_like_error("[FATAL] DB connection lost")
        assert _looks_like_error("Traceback (most recent call last):")
        assert _looks_like_error("AssertionError: expected true")
        assert _looks_like_error("FAILED: build step 3")
        assert _looks_like_error("FAIL: test_foo")
        assert _looks_like_error("Error: file not found")
        assert _looks_like_error("cannot find module 'x'")
        assert _looks_like_error("no such file or directory")
        # Whitespace tolerated at line start.
        assert _looks_like_error("    raise AttributeError('nope')")
        assert _looks_like_error("  TypeError: bad arg")

        # Benign agent heartbeats — no match.
        assert not _looks_like_error("PROVISION_PROGRESS: payload ready")
        assert not _looks_like_error("[10.5MB] Using tool: Bash")
        assert not _looks_like_error("Creating pull request...")
        assert not _looks_like_error("Rebasing onto main...")
        assert not _looks_like_error("DIAGNOSTIC_TEST")
        assert not _looks_like_error("")
        assert not _looks_like_error(None)

        # Strict-shape regression cases (the prior substring matcher
        # would have accepted these and polluted the residual store).
        assert not _looks_like_error(
            "avoid except Exception: blocks (BLE001)"
        )  # exception keyword in the middle of prose
        assert not _looks_like_error(
            "review notes: this catches the regression risk exception cases"
        )  # exception keyword as a normal noun
        assert not _looks_like_error(
            '{"prompt": "see TypeError handling at line 42"}'
        )  # JSON object — skipped outright
        assert not _looks_like_error(
            "all 3 subtasks completed with no actionable changes"
        )  # success message
        assert not _looks_like_error(
            "research created 4 tasks from 5 findings"
        )  # success message
        assert not _looks_like_error(
            "approved. the pr cleanly wires already-persisted data"
        )  # review approval

    def test_get_top_residual_fingerprints_excludes_closed_cases(self):
        """Fingerprints whose case has been closed must be absent from results."""
        import app.failure_residual as fr
        import time

        now = time.time()
        fr._residual_store["open-fp"] = {
            "count": 5,
            "first_seen": now,
            "last_seen": now,
            "sample_message": "open error",
            "task_ids": ["t1"],
        }
        fr._residual_store["closed-fp"] = {
            "count": 8,
            "first_seen": now,
            "last_seen": now,
            "sample_message": "closed error",
            "task_ids": ["t2"],
        }
        fr._mark_residual_case_closed("closed-fp")

        results = get_top_residual_fingerprints()

        fps = [r["fingerprint"] for r in results]
        assert "open-fp" in fps
        assert "closed-fp" not in fps

    def test_record_residual_fingerprint_reactivates_closed_case(self):
        """A new occurrence of a closed fingerprint removes it from the closed set."""
        import app.failure_residual as fr

        fr._mark_residual_case_closed("some-fp")
        assert "some-fp" in fr._residual_closed_fingerprints

        t = self._task_with_id("[10.5MB] Cloning...", "task-reactivate")
        err = "AttributeError: some-fp error text"
        # Seed the store with the existing fingerprint so the update path runs
        fp = fr._fingerprint_message(err) or "<empty>"
        fr._mark_residual_case_closed(fp)
        record_residual_fingerprint(t, logs=err)

        # After recording a new occurrence the fingerprint must be gone from closed set
        assert fp not in fr._residual_closed_fingerprints


class TestAccountFailureClassTracking:
    def setup_method(self):
        import app.failure_residual as fr

        fr._account_failure_log.clear()

    def test_record_and_retrieve_counts(self):
        record_account_failure_class("acct-1", CODE_ERROR)
        record_account_failure_class("acct-1", CODE_ERROR)
        record_account_failure_class("acct-1", RATE_LIMIT)
        counts = get_account_failure_class_counts("acct-1")
        assert counts[CODE_ERROR] == 2
        assert counts[RATE_LIMIT] == 1
        assert counts[EXTERNAL_API] == 0

    def test_unknown_account_returns_zero_counts(self):
        counts = get_account_failure_class_counts("nonexistent-acct")
        assert all(v == 0 for v in counts.values())
        assert set(counts.keys()) == set(ALL_FAILURE_CLASSES)

    def test_window_filter_excludes_old_events(self):
        import app.failure_residual as fr
        import time

        fr._account_failure_log["acct-2"] = [(CODE_ERROR, 0.5, time.time() - 90000)]
        counts = get_account_failure_class_counts("acct-2", window_seconds=86400)
        assert counts[CODE_ERROR] == 0

    def test_record_and_retrieve_raw_records_with_confidence(self):
        record_account_failure_class("acct-raw", CODE_ERROR, confidence=0.5)
        record_account_failure_class("acct-raw", RATE_LIMIT, confidence=0.8)
        records = get_account_failure_class_records("acct-raw")
        assert len(records) == 2
        assert any(rec[0] == CODE_ERROR and rec[1] == 0.5 for rec in records)
        assert any(rec[0] == RATE_LIMIT and rec[1] == 0.8 for rec in records)

    def test_persist_failure_category_forwards_confidence(self):
        task = _task("some error", ctx={"llm_account_id": "acct-confidence"})
        persist_failure_category(task, CODE_ERROR, confidence=0.5)
        records = get_account_failure_class_records("acct-confidence")
        assert len(records) == 1
        assert records[0][0] == CODE_ERROR
        assert records[0][1] == 0.5

    def test_persist_failure_category_records_for_account(self):
        task = _task("some error", ctx={"llm_account_id": "acct-3"})
        persist_failure_category(task, CODE_ERROR)
        counts = get_account_failure_class_counts("acct-3")
        assert counts[CODE_ERROR] == 1

    def test_persist_without_account_id_does_not_raise(self):
        task = _task("some error")
        persist_failure_category(task, CODE_ERROR)

    def test_all_failure_classes_coverage(self):
        for cls in ALL_FAILURE_CLASSES:
            record_account_failure_class("acct-all", cls)
        counts = get_account_failure_class_counts("acct-all")
        assert all(counts[cls] == 1 for cls in ALL_FAILURE_CLASSES)


class TestResidualFingerprintDbWrite:
    """Tests for _upsert_residual_case_async DB persistence."""

    @pytest_asyncio.fixture
    async def session_factory(self):
        from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from sqlmodel import SQLModel

        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        yield factory
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.drop_all)
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_insert_new_fingerprint(self, session_factory, monkeypatch):
        from datetime import datetime, timezone
        import app.database as db_mod
        from app.failure_residual import _upsert_residual_case_async
        from app.models import ResidualRemediationCase, ResidualCaseStatus
        from sqlmodel import select

        project_id = uuid.uuid4()
        now_dt = datetime.now(tz=timezone.utc)
        monkeypatch.setattr(db_mod, "AsyncSessionLocal", session_factory)

        await _upsert_residual_case_async(project_id, "fp-new", now_dt, "some error")

        async with session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(ResidualRemediationCase).where(
                            ResidualRemediationCase.project_id == project_id,
                            ResidualRemediationCase.fingerprint == "fp-new",
                        )
                    )
                )
                .scalars()
                .first()
            )
        assert row is not None
        assert row.hit_count == 1
        assert row.status == ResidualCaseStatus.ACTIVE
        assert row.sample_message == "some error"

    @pytest.mark.asyncio
    async def test_second_hit_bumps_count(self, session_factory, monkeypatch):
        from datetime import datetime, timezone
        import app.database as db_mod
        from app.failure_residual import _upsert_residual_case_async
        from app.models import ResidualRemediationCase
        from sqlmodel import select

        project_id = uuid.uuid4()
        now_dt = datetime.now(tz=timezone.utc)
        monkeypatch.setattr(db_mod, "AsyncSessionLocal", session_factory)

        await _upsert_residual_case_async(project_id, "fp-bump", now_dt, "err")
        await _upsert_residual_case_async(project_id, "fp-bump", now_dt, "err2")

        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ResidualRemediationCase).where(
                            ResidualRemediationCase.project_id == project_id,
                            ResidualRemediationCase.fingerprint == "fp-bump",
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1
        assert rows[0].hit_count == 2

    @pytest.mark.asyncio
    async def test_closed_organically_reopened(self, session_factory, monkeypatch):
        from datetime import datetime, timezone
        import app.database as db_mod
        from app.failure_residual import _upsert_residual_case_async
        from app.models import ResidualRemediationCase, ResidualCaseStatus
        from sqlmodel import select

        project_id = uuid.uuid4()
        now_dt = datetime.now(tz=timezone.utc)
        monkeypatch.setattr(db_mod, "AsyncSessionLocal", session_factory)

        await _upsert_residual_case_async(project_id, "fp-closed", now_dt, "err")
        async with session_factory() as session, session.begin():
            row = (
                (
                    await session.execute(
                        select(ResidualRemediationCase).where(
                            ResidualRemediationCase.project_id == project_id,
                            ResidualRemediationCase.fingerprint == "fp-closed",
                        )
                    )
                )
                .scalars()
                .first()
            )
            row.status = ResidualCaseStatus.CLOSED_ORGANICALLY
            session.add(row)

        await _upsert_residual_case_async(project_id, "fp-closed", now_dt, "err again")

        async with session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(ResidualRemediationCase).where(
                            ResidualRemediationCase.project_id == project_id,
                            ResidualRemediationCase.fingerprint == "fp-closed",
                        )
                    )
                )
                .scalars()
                .first()
            )
        assert row.status == ResidualCaseStatus.ACTIVE
        assert row.hit_count == 2

    @pytest.mark.asyncio
    async def test_ignored_status_preserved_on_rehit(
        self, session_factory, monkeypatch
    ):
        from datetime import datetime, timezone
        import app.database as db_mod
        from app.failure_residual import _upsert_residual_case_async
        from app.models import ResidualRemediationCase, ResidualCaseStatus
        from sqlmodel import select

        project_id = uuid.uuid4()
        now_dt = datetime.now(tz=timezone.utc)
        monkeypatch.setattr(db_mod, "AsyncSessionLocal", session_factory)

        await _upsert_residual_case_async(project_id, "fp-ignored", now_dt, "err")
        async with session_factory() as session, session.begin():
            row = (
                (
                    await session.execute(
                        select(ResidualRemediationCase).where(
                            ResidualRemediationCase.project_id == project_id,
                            ResidualRemediationCase.fingerprint == "fp-ignored",
                        )
                    )
                )
                .scalars()
                .first()
            )
            row.status = ResidualCaseStatus.IGNORED
            session.add(row)

        await _upsert_residual_case_async(project_id, "fp-ignored", now_dt, "err2")

        async with session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(ResidualRemediationCase).where(
                            ResidualRemediationCase.project_id == project_id,
                            ResidualRemediationCase.fingerprint == "fp-ignored",
                        )
                    )
                )
                .scalars()
                .first()
            )
        assert row.status == ResidualCaseStatus.IGNORED
        assert row.hit_count == 2

    @pytest.mark.asyncio
    async def test_repeated_hits_are_not_undercounted(
        self, session_factory, monkeypatch
    ):
        """N sequential record calls must produce hit_count == N (SQL atomic increment).

        The SQL-level UPDATE hit_count = hit_count + 1 (not Python read-modify-write)
        ensures no lost updates. On Postgres, with_for_update() additionally serialises
        concurrent writers; SQLite StaticPool serialises them implicitly.
        """
        from datetime import datetime, timezone
        import app.database as db_mod
        from app.failure_residual import _upsert_residual_case_async
        from app.models import ResidualRemediationCase
        from sqlmodel import select

        N = 8
        project_id = uuid.uuid4()
        now_dt = datetime.now(tz=timezone.utc)
        monkeypatch.setattr(db_mod, "AsyncSessionLocal", session_factory)

        for _ in range(N):
            await _upsert_residual_case_async(project_id, "fp-repeated", now_dt, "err")

        async with session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(ResidualRemediationCase).where(
                            ResidualRemediationCase.project_id == project_id,
                            ResidualRemediationCase.fingerprint == "fp-repeated",
                        )
                    )
                )
                .scalars()
                .first()
            )
        assert row is not None
        assert row.hit_count == N


class TestViewportResidualStore:
    def setup_method(self):
        import app.failure_residual as fr

        fr._viewport_residual_store.clear()
        fr._viewport_residual_seen_task_ids.clear()

    def _task_with_id(self, msg: str, task_id: str, ctx=None, status=None):
        from app.models import TaskStatus

        t = MagicMock(spec=Task)
        t.latest_status_message = msg
        t.context = ctx or {}
        t.id = task_id
        t.project_id = None
        t.status = status if status is not None else TaskStatus.FAILED
        return t

    def test_record_viewport_regression_fingerprint_stores_component_key(self):
        """Acceptance test: FAILED task with a context key stores a viewport fingerprint."""
        t = self._task_with_id(
            "expectNoOverflowAt(375): overflow detected",
            "task-vp-1",
            ctx={"viewport_regression:TaskCard:overflow@375": True},
        )
        record_viewport_regression_fingerprint(t)
        results = get_top_viewport_residual_fingerprints(window_seconds=3600)
        assert len(results) == 1
        assert results[0]["fingerprint"] == "viewport:TaskCard:overflow"
        assert results[0]["component"] == "TaskCard"
        assert results[0]["assertion_type"] == "overflow"
        assert results[0]["count"] == 1
        assert results[0]["task_ids"] == ["task-vp-1"]

    def test_records_on_failed_status(self):
        from app.models import TaskStatus

        t = self._task_with_id(
            "expectNoOverflowAt failed",
            "task-vp-2",
            ctx={"viewport_regression:HeaderPanel:overflow@375": True},
            status=TaskStatus.FAILED,
        )
        record_viewport_regression_fingerprint(t)
        results = get_top_viewport_residual_fingerprints()
        assert len(results) == 1
        assert results[0]["count"] == 1

    def test_gates_non_failed_status(self):
        from app.models import TaskStatus

        for non_failed in (
            TaskStatus.COMPLETED,
            TaskStatus.CANCELLED,
            TaskStatus.IN_PROGRESS,
        ):
            import app.failure_residual as fr

            fr._viewport_residual_store.clear()
            fr._viewport_residual_seen_task_ids.clear()
            t = self._task_with_id(
                "expectNoOverflowAt failed",
                f"task-vp-nonfailed-{non_failed}",
                ctx={"viewport_regression:TaskCard:overflow@375": True},
                status=non_failed,
            )
            record_viewport_regression_fingerprint(t)
            assert get_top_viewport_residual_fingerprints() == []

    def test_dedup_same_task_id(self):
        t = self._task_with_id(
            "expectNoOverflowAt failed",
            "task-vp-dedup",
            ctx={"viewport_regression:TaskCard:overflow@375": True},
        )
        record_viewport_regression_fingerprint(t)
        record_viewport_regression_fingerprint(t)
        results = get_top_viewport_residual_fingerprints()
        assert results[0]["count"] == 1

    def test_context_key_extraction_via_parse_viewport_regression_class(self):
        t = self._task_with_id(
            "viewport test failed",
            "task-vp-ctx",
            ctx={"viewport_regression:MessagesPanel:text_truncation@375": True},
        )
        record_viewport_regression_fingerprint(t)
        results = get_top_viewport_residual_fingerprints()
        assert len(results) == 1
        assert results[0]["fingerprint"] == "viewport:MessagesPanel:text_truncation"
        assert results[0]["component"] == "MessagesPanel"
        assert results[0]["assertion_type"] == "text_truncation"

    def test_status_message_fallback_to_assertion_type_patterns(self):
        t = self._task_with_id(
            "expectNoTextTruncationAt(375) clipped text detected",
            "task-vp-msgfb",
            ctx={},
        )
        record_viewport_regression_fingerprint(t)
        results = get_top_viewport_residual_fingerprints()
        assert len(results) == 1
        assert results[0]["assertion_type"] == "text_truncation"
        assert results[0]["component"] == "unknown"

    def test_missing_component_noop(self):
        """No viewport context key and no assertion pattern → no fingerprint recorded."""
        t = self._task_with_id(
            "some other non-viewport failure",
            "task-vp-noop",
            ctx={},
        )
        record_viewport_regression_fingerprint(t)
        assert get_top_viewport_residual_fingerprints() == []

    def test_fingerprint_strips_width(self):
        """Different widths for the same component+assertion cluster into one fingerprint."""
        t1 = self._task_with_id(
            "viewport fail 1",
            "task-vp-w1",
            ctx={"viewport_regression:TaskCard:overflow@375": True},
        )
        t2 = self._task_with_id(
            "viewport fail 2",
            "task-vp-w2",
            ctx={"viewport_regression:TaskCard:overflow@768": True},
        )
        record_viewport_regression_fingerprint(t1)
        record_viewport_regression_fingerprint(t2)
        results = get_top_viewport_residual_fingerprints()
        assert len(results) == 1
        assert results[0]["count"] == 2
        assert results[0]["fingerprint"] == "viewport:TaskCard:overflow"

    def test_min_touch_target_assertion_type_from_status_message(self):
        t = self._task_with_id(
            "expectMinTouchTargetAt(375): button too small",
            "task-vp-touch",
            ctx={},
        )
        record_viewport_regression_fingerprint(t)
        results = get_top_viewport_residual_fingerprints()
        assert len(results) == 1
        assert results[0]["assertion_type"] == "min_touch_target"


# ─────────────────────────────────────────────────────────────────────────────
# Cooldown auto-recovery for SUSPENDED residual cases
# ─────────────────────────────────────────────────────────────────────────────


class TestSuspendedResidualCaseAutoRecovery:
    """Tests for exponential-backoff auto-recovery of SUSPENDED residual cases.

    These exercise the auto-recovery path added to
    ``_spawn_residual_fingerprint_remediation`` in
    ``app.autonomous_research_residual``. The pattern reuses the same SQLite
    in-memory engine used by TestUpsertResidualCaseAsync.
    """

    @pytest_asyncio.fixture
    async def session_factory(self):
        from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from sqlmodel import SQLModel

        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        yield factory
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.drop_all)
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_suspended_residual_case_auto_recovers_after_cooldown(
        self, session_factory, monkeypatch
    ):
        """SUSPENDED residual case auto-recovers once its backoff cooldown elapses.

        Sets up a SUSPENDED ResidualRemediationCase with suspended_at 48 h ago
        (past the 24 h base cooldown) and calls
        _spawn_residual_fingerprint_remediation. Verifies that the case status
        is set to ACTIVE and recovery_attempts is incremented, even though no
        task is spawned (blocked by the concurrency cap from 2 REMEDIATING rows).
        """
        import time
        from datetime import datetime, timezone, timedelta
        from unittest.mock import AsyncMock

        import app.database as db_mod
        from app.models import ResidualRemediationCase, ResidualCaseStatus, Project
        from app.autonomous_research_residual import (
            _spawn_residual_fingerprint_remediation,
        )
        from app.failure_residual import _residual_store
        from sqlmodel import select

        monkeypatch.setattr(db_mod, "AsyncSessionLocal", session_factory)

        # Suppress notification side-effects to keep the test self-contained.
        # The deferred `from app.notification_dispatch import ...` inside the
        # function picks up the patched attribute from the module object.
        import app.notification_dispatch as nd_mod
        monkeypatch.setattr(
            nd_mod,
            "post_residual_fix_auto_recovered_message",
            AsyncMock(return_value=None),
        )

        # Mock spend-ceiling check so it doesn't short-circuit before auto-recovery.
        import app.autonomous_research as ar_mod
        monkeypatch.setattr(
            ar_mod,
            "_project_spend_ceiling_spawn_defer_reason",
            AsyncMock(return_value=None),
        )

        project_id = uuid.uuid4()
        fp_suspended = "fp_auto_recover_cooldown_test"

        # Ensure the in-memory store is clean before and after this test.
        _residual_store.clear()

        # Inject the fingerprint into the in-memory store so the scan finds it.
        _residual_store[fp_suspended] = {
            "count": 5,
            "first_seen": time.time() - 7200,
            "last_seen": time.time(),
            "sample_message": "TypeError: auto-recover test error",
            "task_ids": [],
            "is_error_shaped": True,
        }

        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        # suspended_at is 48 h ago — well past the 24 h base cooldown.
        suspended_at = now_naive - timedelta(hours=48)

        async with session_factory() as session:
            project = Project(
                id=project_id,
                name="Auto Recovery Test",
                user_id="test-user",
                autonomous_enabled=True,
                directions=[],
            )
            session.add(project)

            # Two REMEDIATING cases so active_count >= max_concurrent (2) — this
            # prevents any spawn while still allowing auto-recovery to run first.
            for i in range(2):
                session.add(
                    ResidualRemediationCase(
                        project_id=project_id,
                        fingerprint=f"fp_remediating_{i}",
                        status=ResidualCaseStatus.REMEDIATING,
                        hit_count=3,
                        first_seen_at=now_naive,
                        last_seen_at=now_naive,
                        created_at=now_naive,
                        updated_at=now_naive,
                    )
                )

            # The SUSPENDED case under test — cooldown already elapsed.
            suspended_case = ResidualRemediationCase(
                project_id=project_id,
                fingerprint=fp_suspended,
                status=ResidualCaseStatus.SUSPENDED,
                suspended_at=suspended_at,
                recovery_attempts=0,
                hit_count=5,
                sample_message="TypeError: auto-recover test error",
                first_seen_at=suspended_at,
                last_seen_at=now_naive,
                created_at=suspended_at,
                updated_at=suspended_at,
            )
            session.add(suspended_case)
            await session.commit()
            await session.refresh(project)

            # Call the spawn function. It should auto-recover the SUSPENDED case
            # then hit the concurrency cap (2 REMEDIATING) and return 0.
            spawned = await _spawn_residual_fingerprint_remediation(project, session)

        assert spawned == 0, "Concurrency cap should block spawn (2 REMEDIATING cases)"

        async with session_factory() as read_session:
            recovered = (
                await read_session.execute(
                    select(ResidualRemediationCase).where(
                        ResidualRemediationCase.project_id == project_id,
                        ResidualRemediationCase.fingerprint == fp_suspended,
                    )
                )
            ).scalar_one_or_none()

        assert recovered is not None, "ResidualRemediationCase row must still exist"
        assert recovered.status == ResidualCaseStatus.ACTIVE, (
            f"SUSPENDED case must be ACTIVE after cooldown, got {recovered.status}"
        )
        assert recovered.suspended_at is None, "suspended_at must be cleared on auto-recovery"
        assert recovered.recovery_attempts == 1, (
            f"recovery_attempts must be incremented to 1, got {recovered.recovery_attempts}"
        )

    @pytest.mark.asyncio
    async def test_suspended_case_stays_blocked_within_cooldown(
        self, session_factory, monkeypatch
    ):
        """SUSPENDED case within its cooldown window must not auto-recover."""
        import time
        from datetime import datetime, timezone, timedelta
        from unittest.mock import AsyncMock

        import app.database as db_mod
        from app.models import ResidualRemediationCase, ResidualCaseStatus, Project
        from app.autonomous_research_residual import (
            _spawn_residual_fingerprint_remediation,
        )
        from app.failure_residual import _residual_store
        from sqlmodel import select

        monkeypatch.setattr(db_mod, "AsyncSessionLocal", session_factory)

        import app.notification_dispatch as nd_mod
        monkeypatch.setattr(
            nd_mod, "post_residual_fix_auto_recovered_message", AsyncMock(return_value=None)
        )

        import app.autonomous_research as ar_mod
        monkeypatch.setattr(
            ar_mod,
            "_project_spend_ceiling_spawn_defer_reason",
            AsyncMock(return_value=None),
        )

        project_id = uuid.uuid4()
        fp = "fp_within_cooldown_test"

        _residual_store.clear()
        _residual_store[fp] = {
            "count": 5,
            "first_seen": time.time() - 3600,
            "last_seen": time.time(),
            "sample_message": "some error within cooldown",
            "task_ids": [],
            "is_error_shaped": True,
        }

        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        # suspended_at is only 1 h ago — within the 24 h base cooldown.
        suspended_at = now_naive - timedelta(hours=1)

        async with session_factory() as session:
            project = Project(
                id=project_id,
                name="Within Cooldown Test",
                user_id="test-user",
                autonomous_enabled=True,
                directions=[],
            )
            session.add(project)
            session.add(
                ResidualRemediationCase(
                    project_id=project_id,
                    fingerprint=fp,
                    status=ResidualCaseStatus.SUSPENDED,
                    suspended_at=suspended_at,
                    recovery_attempts=0,
                    hit_count=5,
                    sample_message="some error within cooldown",
                    first_seen_at=suspended_at,
                    last_seen_at=now_naive,
                    created_at=suspended_at,
                    updated_at=suspended_at,
                )
            )
            await session.commit()
            await session.refresh(project)

            spawned = await _spawn_residual_fingerprint_remediation(project, session)

        assert spawned == 0, "Within-cooldown SUSPENDED case must not spawn"

        async with session_factory() as read_session:
            row = (
                await read_session.execute(
                    select(ResidualRemediationCase).where(
                        ResidualRemediationCase.project_id == project_id,
                        ResidualRemediationCase.fingerprint == fp,
                    )
                )
            ).scalar_one_or_none()

        assert row is not None
        assert row.status == ResidualCaseStatus.SUSPENDED, (
            "Within-cooldown case must remain SUSPENDED"
        )
        assert row.recovery_attempts == 0, "recovery_attempts must not increment"

    @pytest.mark.asyncio
    async def test_suspended_case_permanently_blocked_at_max_attempts(
        self, session_factory, monkeypatch
    ):
        """SUSPENDED case at MAX_AUTO_RECOVERY_ATTEMPTS stays permanently suspended."""
        import time
        from datetime import datetime, timezone, timedelta
        from unittest.mock import AsyncMock

        import app.database as db_mod
        from app.models import ResidualRemediationCase, ResidualCaseStatus, Project
        from app.autonomous_research_residual import (
            _spawn_residual_fingerprint_remediation,
        )
        from app.auto_remediation import MAX_AUTO_RECOVERY_ATTEMPTS
        from app.failure_residual import _residual_store
        from sqlmodel import select

        monkeypatch.setattr(db_mod, "AsyncSessionLocal", session_factory)

        import app.notification_dispatch as nd_mod
        monkeypatch.setattr(
            nd_mod, "post_residual_fix_auto_recovered_message", AsyncMock(return_value=None)
        )

        import app.autonomous_research as ar_mod
        monkeypatch.setattr(
            ar_mod,
            "_project_spend_ceiling_spawn_defer_reason",
            AsyncMock(return_value=None),
        )

        project_id = uuid.uuid4()
        fp = "fp_max_attempts_test"

        _residual_store.clear()
        _residual_store[fp] = {
            "count": 5,
            "first_seen": time.time() - 7200,
            "last_seen": time.time(),
            "sample_message": "max attempts error",
            "task_ids": [],
            "is_error_shaped": True,
        }

        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        # suspended_at is 48 h ago (past cooldown) but at max attempts → stays blocked.
        suspended_at = now_naive - timedelta(hours=48)

        async with session_factory() as session:
            project = Project(
                id=project_id,
                name="Max Attempts Test",
                user_id="test-user",
                autonomous_enabled=True,
                directions=[],
            )
            session.add(project)
            session.add(
                ResidualRemediationCase(
                    project_id=project_id,
                    fingerprint=fp,
                    status=ResidualCaseStatus.SUSPENDED,
                    suspended_at=suspended_at,
                    recovery_attempts=MAX_AUTO_RECOVERY_ATTEMPTS,
                    hit_count=5,
                    sample_message="max attempts error",
                    first_seen_at=suspended_at,
                    last_seen_at=now_naive,
                    created_at=suspended_at,
                    updated_at=suspended_at,
                )
            )
            await session.commit()
            await session.refresh(project)

            spawned = await _spawn_residual_fingerprint_remediation(project, session)

        assert spawned == 0, "Max-attempts SUSPENDED case must not spawn"

        async with session_factory() as read_session:
            row = (
                await read_session.execute(
                    select(ResidualRemediationCase).where(
                        ResidualRemediationCase.project_id == project_id,
                        ResidualRemediationCase.fingerprint == fp,
                    )
                )
            ).scalar_one_or_none()

        assert row is not None
        assert row.status == ResidualCaseStatus.SUSPENDED, (
            "Max-attempts case must remain SUSPENDED even past cooldown"
        )
        assert row.recovery_attempts == MAX_AUTO_RECOVERY_ATTEMPTS, (
            "recovery_attempts must not increase past MAX_AUTO_RECOVERY_ATTEMPTS"
        )
