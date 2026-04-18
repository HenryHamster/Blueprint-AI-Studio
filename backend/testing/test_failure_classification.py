"""Tests for failure_classification subtype helpers."""
from app.failure_classification import (
    _timeout_subtype_from_message,
    classify_timeout_subtype,
    TIMEOUT_OOM,
    TIMEOUT_HEARTBEAT_LOST,
    TIMEOUT_ITERATION_LIMIT,
    TIMEOUT_CASCADE,
    TIMEOUT_PROCESS_CRASH,
    TIMEOUT_EXECUTION,
    TIMEOUT_UNKNOWN,
)


class _FakeTask:
    def __init__(self, msg: str) -> None:
        self.latest_status_message = msg


class TestTimeoutSubtypeFromMessage:
    def test_oom(self):
        assert _timeout_subtype_from_message("exit 137 out of memory") == TIMEOUT_OOM

    def test_heartbeat_lost(self):
        assert _timeout_subtype_from_message("No heartbeat received after 300s") == TIMEOUT_HEARTBEAT_LOST

    def test_iteration_limit(self):
        assert _timeout_subtype_from_message("review-fix cycle limit reached") == TIMEOUT_ITERATION_LIMIT
        assert _timeout_subtype_from_message("iteration cap exceeded") == TIMEOUT_ITERATION_LIMIT

    def test_cascade_via_cancelled_prefix(self):
        assert _timeout_subtype_from_message("Cancelled: parent evaluation timeout") == TIMEOUT_CASCADE

    def test_cascade_via_superseded(self):
        assert _timeout_subtype_from_message("task superseded by newer revision") == TIMEOUT_CASCADE

    def test_cascade_precedes_heartbeat(self):
        # cascade signal should win over a concurrent heartbeat keyword
        assert _timeout_subtype_from_message("Cancelled: no heartbeat") == TIMEOUT_CASCADE

    def test_process_crash(self):
        assert _timeout_subtype_from_message("agent process crashed unexpectedly") == TIMEOUT_PROCESS_CRASH

    def test_execution_timeout(self):
        assert _timeout_subtype_from_message("execution timed out after 3600s") == TIMEOUT_EXECUTION

    def test_unknown(self):
        assert _timeout_subtype_from_message("something weird happened") == TIMEOUT_UNKNOWN

    def test_empty(self):
        assert _timeout_subtype_from_message("") == TIMEOUT_UNKNOWN


class TestClassifyTimeoutSubtypeDelegates:
    def test_delegates_to_helper(self):
        task = _FakeTask("exit code 137 OOM killer")
        assert classify_timeout_subtype(task) == TIMEOUT_OOM

    def test_combines_logs(self):
        task = _FakeTask("")
        assert classify_timeout_subtype(task, logs="No heartbeat received") == TIMEOUT_HEARTBEAT_LOST

    def test_none_status(self):
        task = _FakeTask(None)  # type: ignore[arg-type]
        assert classify_timeout_subtype(task) == TIMEOUT_UNKNOWN
