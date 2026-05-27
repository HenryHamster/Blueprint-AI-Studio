"""Tests for backend/app/event_fanout.py.

Standalone (no DB): dialect guard, encode/decode helpers, publish no-op on
non-Postgres, listener inactive on non-Postgres, and the headline cross-instance
delivery proof — an event published by one logical instance is delivered to a
WebSocket client registered on a DIFFERENT logical ConnectionManager, routed
through the real listener's local-delivery path.
"""

import asyncio
import json
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import app.event_fanout as ef


# ---------------------------------------------------------------------------
# _get_raw_dsn
# ---------------------------------------------------------------------------


class TestGetRawDsn:
    def test_returns_none_for_sqlite(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "sqlite:///test.db")
        assert ef._get_raw_dsn() is None

    def test_returns_none_for_empty(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert ef._get_raw_dsn() is None

    def test_strips_driver_tag(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@host/db")
        assert ef._get_raw_dsn() == "postgresql://user:pass@host/db"

    def test_plain_postgresql_unchanged(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@host/db")
        assert ef._get_raw_dsn() == "postgresql://user:pass@host/db"


# ---------------------------------------------------------------------------
# _encode_payload / _decode_payload
# ---------------------------------------------------------------------------


class TestEncodePayload:
    def test_skips_agent_log(self):
        assert ef._encode_payload("p1", "agent_log", {"log": "x"}) is None

    def test_encodes_control_event(self):
        encoded = ef._encode_payload("p1", "task_updated", {"id": "t1"})
        assert encoded is not None
        decoded = json.loads(encoded)
        assert decoded == {
            "project_id": "p1",
            "event": "task_updated",
            "data": {"id": "t1"},
            "origin": ef._ORIGIN_ID,
        }

    def test_skips_oversized_payload(self):
        big = {"blob": "x" * (ef._MAX_NOTIFY_PAYLOAD_BYTES + 100)}
        assert ef._encode_payload("p1", "task_updated", big) is None

    def test_skips_non_serializable(self):
        # A set is not JSON-serializable — must skip, not raise.
        assert ef._encode_payload("p1", "task_updated", {"bad": {1, 2}}) is None


class TestDecodePayload:
    def test_roundtrip(self):
        encoded = ef._encode_payload("p1", "task_updated", {"id": "t1"})
        assert ef._decode_payload(encoded) == (
            "p1",
            "task_updated",
            {"id": "t1"},
            ef._ORIGIN_ID,
        )

    def test_malformed_json_returns_none(self):
        assert ef._decode_payload("not json{") is None

    def test_missing_key_returns_none(self):
        assert ef._decode_payload(json.dumps({"project_id": "p1"})) is None

    def test_missing_origin_returns_none(self):
        # A payload without the origin tag is incomplete and must be dropped
        # rather than delivered without a self-origin guard.
        assert (
            ef._decode_payload(
                json.dumps(
                    {"project_id": "p1", "event": "task_updated", "data": {"id": "t1"}}
                )
            )
            is None
        )


# ---------------------------------------------------------------------------
# publish_project_event
# ---------------------------------------------------------------------------


def _fake_sessionmaker():
    """Return (sessionmaker_mock, session_mock) where the maker yields an
    async-context-manager session."""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    maker = MagicMock(return_value=cm)
    return maker, session


class TestPublishProjectEvent:
    async def test_noop_when_not_postgres(self, monkeypatch):
        monkeypatch.setattr(ef, "_IS_POSTGRES", False)
        maker, session = _fake_sessionmaker()
        monkeypatch.setattr("app.database.AsyncSessionLocal", maker)
        await ef.publish_project_event("p1", "task_updated", {"id": "t1"})
        maker.assert_not_called()
        session.execute.assert_not_called()

    async def test_skips_agent_log_even_on_postgres(self, monkeypatch):
        monkeypatch.setattr(ef, "_IS_POSTGRES", True)
        maker, session = _fake_sessionmaker()
        monkeypatch.setattr("app.database.AsyncSessionLocal", maker)
        await ef.publish_project_event("p1", "agent_log", {"log": "x"})
        maker.assert_not_called()

    async def test_emits_pg_notify_and_commits(self, monkeypatch):
        monkeypatch.setattr(ef, "_IS_POSTGRES", True)
        maker, session = _fake_sessionmaker()
        monkeypatch.setattr("app.database.AsyncSessionLocal", maker)
        await ef.publish_project_event("p1", "task_updated", {"id": "t1"})
        session.execute.assert_awaited_once()
        call_args = session.execute.call_args
        assert "pg_notify" in str(call_args[0][0])
        params = call_args[0][1]
        assert params["channel"] == ef._NOTIFY_CHANNEL
        # Payload is the JSON-encoded control event.
        assert json.loads(params["payload"])["event"] == "task_updated"
        session.commit.assert_awaited_once()

    async def test_swallows_exceptions(self, monkeypatch):
        monkeypatch.setattr(ef, "_IS_POSTGRES", True)
        maker = MagicMock(side_effect=RuntimeError("pool exhausted"))
        monkeypatch.setattr("app.database.AsyncSessionLocal", maker)
        # Must not raise.
        await ef.publish_project_event("p1", "task_updated", {"id": "t1"})


# ---------------------------------------------------------------------------
# event_fanout_service — non-Postgres no-op
# ---------------------------------------------------------------------------


class TestEventFanoutServiceNoop:
    async def test_service_noop_on_non_postgres(self, monkeypatch):
        """Service sleeps and exits cleanly when DATABASE_URL is not Postgres."""
        monkeypatch.setattr(ef, "_get_raw_dsn", lambda: None)
        task = asyncio.create_task(ef.event_fanout_service())
        await asyncio.sleep(0)  # let it start sleeping
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Cross-instance delivery (headline regression test)
# ---------------------------------------------------------------------------


class TestCrossInstanceDelivery:
    async def test_event_delivered_to_client_on_other_instance(self, monkeypatch):
        """An event published by 'instance A' is delivered to a WebSocket client
        registered on 'instance B' via the real listener's local-delivery path,
        and the listener does NOT re-publish (loop-safety)."""
        import app.broadcast as broadcast_mod
        from app.broadcast import ConnectionManager

        project_id = str(uuid.uuid4())

        # --- Instance B: its own in-memory manager with one registered client ---
        manager_b = ConnectionManager()
        received: list = []

        async def _send_json(message):
            received.append(message)

        fake_ws = MagicMock()
        fake_ws.send_json = _send_json
        manager_b.active_connections[project_id] = [fake_ws]
        manager_b.last_activity[fake_ws] = time.time()

        # The listener reads the live module-level manager — point it at B's.
        monkeypatch.setattr(broadcast_mod, "manager", manager_b)

        # Guard: the listener must never re-publish a peer-received event.
        publish_spy = AsyncMock()
        monkeypatch.setattr(ef, "publish_project_event", publish_spy)

        # --- Capture the real _on_notify callback the service registers ---
        captured: dict = {}

        async def fake_add_listener(channel, callback):
            captured["callback"] = callback

        fake_conn = MagicMock()
        # Fire termination immediately so terminated_event.wait() returns and the
        # service proceeds to the reconnect sleep (which we cancel).
        fake_conn.add_termination_listener = MagicMock(
            side_effect=lambda cb: cb(fake_conn)
        )
        fake_conn.add_listener = fake_add_listener
        fake_conn.close = AsyncMock()

        async def _cancel_on_first_sleep(secs):
            raise asyncio.CancelledError()

        monkeypatch.setattr(ef, "_get_raw_dsn", lambda: "postgresql://host/db")
        with (
            patch(
                "app.event_fanout._asyncpg.connect",
                new=AsyncMock(return_value=fake_conn),
            ),
            patch("asyncio.sleep", side_effect=_cancel_on_first_sleep),
        ):
            try:
                await ef.event_fanout_service()
            except asyncio.CancelledError:
                pass

        assert "callback" in captured, (
            "service should have registered a NOTIFY listener"
        )

        # --- Instance A 'publishes': encode the event as the publish path would,
        # but stamped with A's DISTINCT origin id (a different process), then feed
        # it into B's captured listener callback (as asyncpg would on instance B).
        # A peer origin must NOT be dropped by B's self-origin guard. ---
        encoded = json.dumps(
            {
                "project_id": project_id,
                "event": "task_updated",
                "data": {"id": "t1"},
                "origin": "instance-A-origin-id",
            }
        )
        captured["callback"](None, 1234, ef._NOTIFY_CHANNEL, encoded)

        # The callback schedules manager_b.broadcast as a task — let it run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert received, "client on instance B should have received the event"
        assert received[0] == {"event": "task_updated", "data": {"id": "t1"}}
        # Loop-safety: receiving a peer event must not re-publish it.
        publish_spy.assert_not_called()


class TestEnqueueAgentLog:
    def _clear(self):
        ef._agent_log_buffer.clear()

    def test_noop_when_not_postgres(self, monkeypatch):
        self._clear()
        monkeypatch.setattr(ef, "_IS_POSTGRES", False)
        ef.enqueue_agent_log("p1", "t1", "hello")
        assert ef._agent_log_buffer == {}

    def test_noop_on_empty_line(self, monkeypatch):
        self._clear()
        monkeypatch.setattr(ef, "_IS_POSTGRES", True)
        ef.enqueue_agent_log("p1", "t1", "")
        assert ef._agent_log_buffer == {}

    def test_buffers_line_keyed_by_project_task(self, monkeypatch):
        self._clear()
        monkeypatch.setattr(ef, "_IS_POSTGRES", True)
        ef.enqueue_agent_log("p1", "t1", "line-a")
        ef.enqueue_agent_log("p1", "t1", "line-b")
        ef.enqueue_agent_log("p1", "t2", "other")
        assert ef._agent_log_buffer[("p1", "t1")] == ["line-a", "line-b"]
        assert ef._agent_log_buffer[("p1", "t2")] == ["other"]
        self._clear()

    def test_caps_buffer_dropping_oldest(self, monkeypatch):
        self._clear()
        monkeypatch.setattr(ef, "_IS_POSTGRES", True)
        monkeypatch.setattr(ef, "_MAX_BUFFERED_LINES_PER_TASK", 3)
        for i in range(5):
            ef.enqueue_agent_log("p1", "t1", f"line-{i}")
        # Only the newest 3 survive; the two oldest are dropped.
        assert ef._agent_log_buffer[("p1", "t1")] == ["line-2", "line-3", "line-4"]
        self._clear()

    def test_truncates_oversized_line(self, monkeypatch):
        self._clear()
        monkeypatch.setattr(ef, "_IS_POSTGRES", True)
        monkeypatch.setattr(ef, "_MAX_RELAY_LINE_BYTES", 10)
        ef.enqueue_agent_log("p1", "t1", "x" * 100)
        stored = ef._agent_log_buffer[("p1", "t1")][0]
        assert stored.startswith("x" * 10)
        assert stored.endswith("[relay-truncated]")
        self._clear()


class TestPackLogBatches:
    def test_single_batch_under_ceiling(self):
        batches = ef._pack_log_batches("p1", "t1", ["a", "b", "c"])
        assert len(batches) == 1
        decoded = json.loads(batches[0])
        assert decoded["event"] == ef._AGENT_LOG_BATCH_EVENT
        assert decoded["data"] == {"task_id": "t1", "lines": ["a", "b", "c"]}
        assert decoded["origin"] == ef._ORIGIN_ID

    def test_splits_when_exceeding_ceiling(self, monkeypatch):
        # Force a small ceiling (above the envelope base overhead) so the lines
        # span more than one batch.
        monkeypatch.setattr(ef, "_MAX_NOTIFY_PAYLOAD_BYTES", 200)
        lines = ["x" * 40, "y" * 40, "z" * 40]
        batches = ef._pack_log_batches("p1", "t1", lines)
        assert len(batches) >= 2
        # Every emitted batch stays strictly under the ceiling.
        for enc in batches:
            assert len(enc.encode("utf-8")) < 200
        # No line is lost across the split.
        relayed = [line for enc in batches for line in json.loads(enc)["data"]["lines"]]
        assert relayed == lines

    def test_dense_small_lines_stay_under_ceiling(self):
        # Regression: dense small-line batches (the worst case for per-line
        # overhead accounting) must produce encoded payloads strictly below the
        # NOTIFY ceiling. With ``json.dumps`` default separators (``", "``) and a
        # +1-per-comma estimate, the packer used to underestimate by 1 byte per
        # line and emit a single batch that exceeded _MAX_NOTIFY_PAYLOAD_BYTES;
        # compact separators in _encode_agent_log_batch align wire size with the
        # estimate.
        lines = ["a"] * 2000
        batches = ef._pack_log_batches("p1", "t1", lines)
        assert batches, "packer must emit at least one batch"
        for enc in batches:
            assert len(enc.encode("utf-8")) < ef._MAX_NOTIFY_PAYLOAD_BYTES, (
                f"batch {len(enc.encode('utf-8'))} bytes exceeds ceiling "
                f"{ef._MAX_NOTIFY_PAYLOAD_BYTES}"
            )
        # No line is lost across the split.
        relayed = [line for enc in batches for line in json.loads(enc)["data"]["lines"]]
        assert relayed == lines


class TestAgentLogRelayServiceNoop:
    async def test_service_noop_on_non_postgres(self, monkeypatch):
        monkeypatch.setattr(ef, "_IS_POSTGRES", False)
        task = asyncio.create_task(ef.agent_log_relay_service())
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


class TestAgentLogRelayServiceFlush:
    async def test_flush_publishes_batches_and_clears_buffer(self, monkeypatch):
        ef._agent_log_buffer.clear()
        monkeypatch.setattr(ef, "_IS_POSTGRES", True)
        monkeypatch.setattr(ef, "_AGENT_LOG_FLUSH_INTERVAL", 0)
        published: list = []

        async def _capture(encoded):
            published.append(json.loads(encoded))

        monkeypatch.setattr(ef, "_publish_encoded", _capture)
        ef.enqueue_agent_log("p1", "t1", "line-a")
        ef.enqueue_agent_log("p1", "t1", "line-b")

        task = asyncio.create_task(ef.agent_log_relay_service())
        # Let one flush cycle run.
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert published, "relay should publish at least one batch"
        assert published[0]["event"] == ef._AGENT_LOG_BATCH_EVENT
        assert published[0]["data"] == {"task_id": "t1", "lines": ["line-a", "line-b"]}
        # Buffer drained.
        assert ef._agent_log_buffer == {}

    async def test_flush_swallows_publish_errors(self, monkeypatch):
        ef._agent_log_buffer.clear()
        monkeypatch.setattr(ef, "_IS_POSTGRES", True)
        monkeypatch.setattr(ef, "_AGENT_LOG_FLUSH_INTERVAL", 0)

        async def _boom(encoded):
            raise RuntimeError("pool exhausted")

        monkeypatch.setattr(ef, "_publish_encoded", _boom)
        ef.enqueue_agent_log("p1", "t1", "line-a")

        task = asyncio.create_task(ef.agent_log_relay_service())
        for _ in range(5):
            await asyncio.sleep(0)
        # Service must still be running (error swallowed, not crashed).
        assert not task.done()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        ef._agent_log_buffer.clear()


class TestAgentLogBatchCrossInstanceDelivery:
    async def test_batch_expanded_to_per_line_on_peer_instance(self, monkeypatch):
        """A relayed agent_log_batch from a peer is expanded into individual
        agent_log deliveries to THIS instance's local clients, and is not
        re-published (loop-safety)."""
        import app.broadcast as broadcast_mod
        from app.broadcast import ConnectionManager

        project_id = str(uuid.uuid4())
        manager_b = ConnectionManager()
        received: list = []

        async def _send_json(message):
            received.append(message)

        fake_ws = MagicMock()
        fake_ws.send_json = _send_json
        manager_b.active_connections[project_id] = [fake_ws]
        manager_b.last_activity[fake_ws] = time.time()
        monkeypatch.setattr(broadcast_mod, "manager", manager_b)

        publish_spy = AsyncMock()
        monkeypatch.setattr(ef, "publish_project_event", publish_spy)

        captured: dict = {}

        async def fake_add_listener(channel, callback):
            captured["callback"] = callback

        fake_conn = MagicMock()
        fake_conn.add_termination_listener = MagicMock(
            side_effect=lambda cb: cb(fake_conn)
        )
        fake_conn.add_listener = fake_add_listener
        fake_conn.close = AsyncMock()

        async def _cancel_on_first_sleep(secs):
            raise asyncio.CancelledError()

        monkeypatch.setattr(ef, "_get_raw_dsn", lambda: "postgresql://host/db")
        with (
            patch(
                "app.event_fanout._asyncpg.connect",
                new=AsyncMock(return_value=fake_conn),
            ),
            patch("asyncio.sleep", side_effect=_cancel_on_first_sleep),
        ):
            try:
                await ef.event_fanout_service()
            except asyncio.CancelledError:
                pass

        assert "callback" in captured

        # Peer instance A relays a batch (DISTINCT origin) of two log lines.
        encoded = json.dumps(
            {
                "project_id": project_id,
                "event": ef._AGENT_LOG_BATCH_EVENT,
                "data": {"task_id": "t1", "lines": ["log-1", "log-2"]},
                "origin": "instance-A-origin-id",
            }
        )
        captured["callback"](None, 1234, ef._NOTIFY_CHANNEL, encoded)
        for _ in range(4):
            await asyncio.sleep(0)

        assert received == [
            {"event": "agent_log", "data": {"task_id": "t1", "log": "log-1"}},
            {"event": "agent_log", "data": {"task_id": "t1", "log": "log-2"}},
        ]
        publish_spy.assert_not_called()

    async def test_self_origin_batch_is_dropped(self, monkeypatch):
        """The originating instance already delivered its agent_log lines locally;
        Postgres echoes its batch NOTIFY back to its own listener, which the
        self-origin guard must drop so logs are not delivered twice."""
        import app.broadcast as broadcast_mod
        from app.broadcast import ConnectionManager

        project_id = str(uuid.uuid4())
        manager_a = ConnectionManager()
        received: list = []

        async def _send_json(message):
            received.append(message)

        fake_ws = MagicMock()
        fake_ws.send_json = _send_json
        manager_a.active_connections[project_id] = [fake_ws]
        manager_a.last_activity[fake_ws] = time.time()
        monkeypatch.setattr(broadcast_mod, "manager", manager_a)

        captured: dict = {}

        async def fake_add_listener(channel, callback):
            captured["callback"] = callback

        fake_conn = MagicMock()
        fake_conn.add_termination_listener = MagicMock(
            side_effect=lambda cb: cb(fake_conn)
        )
        fake_conn.add_listener = fake_add_listener
        fake_conn.close = AsyncMock()

        async def _cancel_on_first_sleep(secs):
            raise asyncio.CancelledError()

        monkeypatch.setattr(ef, "_get_raw_dsn", lambda: "postgresql://host/db")
        with (
            patch(
                "app.event_fanout._asyncpg.connect",
                new=AsyncMock(return_value=fake_conn),
            ),
            patch("asyncio.sleep", side_effect=_cancel_on_first_sleep),
        ):
            try:
                await ef.event_fanout_service()
            except asyncio.CancelledError:
                pass

        assert "callback" in captured
        # A batch stamped with THIS process's own origin id (the echo).
        encoded = ef._encode_agent_log_batch(project_id, "t1", ["log-1"])
        captured["callback"](None, 1234, ef._NOTIFY_CHANNEL, encoded)
        for _ in range(4):
            await asyncio.sleep(0)

        assert received == [], "self-originated batch echo must be dropped"


class TestOriginInstanceSingleDelivery:
    async def test_originating_instance_delivers_exactly_once(self, monkeypatch):
        """An instance that BOTH publishes and listens delivers a control event
        to its local clients exactly once. Postgres echoes a NOTIFY back to the
        publisher's own listener session; the self-origin guard must drop that
        echo so the event is not delivered twice (once via broadcast_to_project's
        direct manager.broadcast, once via the listener's _on_notify)."""
        import app.broadcast as broadcast_mod
        from app.broadcast import ConnectionManager

        project_id = str(uuid.uuid4())

        manager_a = ConnectionManager()
        received: list = []

        async def _send_json(message):
            received.append(message)

        fake_ws = MagicMock()
        fake_ws.send_json = _send_json
        manager_a.active_connections[project_id] = [fake_ws]
        manager_a.last_activity[fake_ws] = time.time()

        # The listener reads the live module-level manager — point it at A's.
        monkeypatch.setattr(broadcast_mod, "manager", manager_a)

        # --- Capture the real _on_notify callback the service registers ---
        captured: dict = {}

        async def fake_add_listener(channel, callback):
            captured["callback"] = callback

        fake_conn = MagicMock()
        fake_conn.add_termination_listener = MagicMock(
            side_effect=lambda cb: cb(fake_conn)
        )
        fake_conn.add_listener = fake_add_listener
        fake_conn.close = AsyncMock()

        async def _cancel_on_first_sleep(secs):
            raise asyncio.CancelledError()

        monkeypatch.setattr(ef, "_get_raw_dsn", lambda: "postgresql://host/db")
        with (
            patch(
                "app.event_fanout._asyncpg.connect",
                new=AsyncMock(return_value=fake_conn),
            ),
            patch("asyncio.sleep", side_effect=_cancel_on_first_sleep),
        ):
            try:
                await ef.event_fanout_service()
            except asyncio.CancelledError:
                pass

        assert "callback" in captured

        # Step 1: broadcast_to_project's local-delivery step delivers once.
        await broadcast_mod.broadcast_to_project(
            project_id, "task_updated", {"id": "t1"}
        )

        # Step 2: Postgres echoes the publisher's own NOTIFY back to its listener.
        # The payload carries THIS process's origin id, so the guard drops it.
        encoded = ef._encode_payload(project_id, "task_updated", {"id": "t1"})
        assert encoded is not None
        captured["callback"](None, 1234, ef._NOTIFY_CHANNEL, encoded)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert len(received) == 1, (
            f"originating instance must deliver exactly once, got {len(received)}"
        )
