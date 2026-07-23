"""Rebuild project_efficiency_snapshot without discarding legacy rows.

Migration 0658 created project_efficiency_snapshot as a one-row-per-project
table.  Its ``data`` may be a full efficiency container or a direct snapshot.
Preserve that exact value in project_telemetry, then expand representable
snapshots into the runtime's time-series schema.

Revision ID: 0694
Revises: 0692
Create Date: 2026-07-23
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0694"
down_revision = "0692"
branch_labels = None
depends_on = None

_TABLE = "project_efficiency_snapshot"
_LEGACY_TABLE = "project_efficiency_snapshot_legacy_0694"
_DOWNGRADE_TABLE = "project_efficiency_snapshot_multi_0694"
_LIVE_TELEMETRY_KEY = "efficiency_baseline"
_ARCHIVE_TELEMETRY_KEY = "migration_0694_efficiency_snapshot_raw"


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def _utc_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _definition_version(value: Any) -> str:
    if isinstance(value, str) and value and len(value) <= 64:
        return value
    return "legacy"


def _create_multirow_table() -> None:
    op.create_table(
        _TABLE,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metric_def_version", sa.String(length=64), nullable=False),
        sa.Column(
            "blob",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id",
            "captured_at",
            name="uq_proj_efficiency_snap",
        ),
    )
    op.create_index(
        "ix_project_efficiency_snapshot_project_id",
        _TABLE,
        ["project_id"],
    )


def _create_legacy_table() -> None:
    op.create_table(
        _TABLE,
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "data",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("project_id"),
    )


def _telemetry_rows(connection, project_id: Any) -> list[Mapping[str, Any]]:
    return list(
        connection.execute(
            sa.text(
                "SELECT key, value, updated_at FROM project_telemetry "
                "WHERE project_id = CAST(:project_id AS uuid) "
                "AND (key = :live_key OR key = :archive_key "
                "OR key LIKE :archive_prefix) "
                "ORDER BY CASE WHEN key = :live_key THEN 0 ELSE 1 END, key"
            ),
            {
                "project_id": str(project_id),
                "live_key": _LIVE_TELEMETRY_KEY,
                "archive_key": _ARCHIVE_TELEMETRY_KEY,
                "archive_prefix": f"{_ARCHIVE_TELEMETRY_KEY}.%",
            },
        ).mappings()
    )


def _insert_telemetry(
    connection,
    project_id: Any,
    key: str,
    value: Any,
    updated_at: datetime,
) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO project_telemetry "
            "(id, project_id, key, value, updated_at) VALUES "
            "(CAST(:id AS uuid), CAST(:project_id AS uuid), :key, "
            "CAST(:value AS jsonb), :updated_at)"
        ),
        {
            "id": str(uuid.uuid4()),
            "project_id": str(project_id),
            "key": key,
            "value": _json(value),
            "updated_at": updated_at,
        },
    )


def _preserve_raw(
    connection,
    project_id: Any,
    raw: Any,
    updated_at: datetime,
) -> None:
    existing = _telemetry_rows(connection, project_id)
    live_row = next(
        (row for row in existing if row["key"] == _LIVE_TELEMETRY_KEY),
        None,
    )
    base_archive_row = next(
        (row for row in existing if row["key"] == _ARCHIVE_TELEMETRY_KEY),
        None,
    )
    existing_keys = {str(row["key"]) for row in existing}

    def archive(value: Any, archived_at: datetime) -> None:
        archive_key = _ARCHIVE_TELEMETRY_KEY
        suffix = 0
        while archive_key in existing_keys:
            suffix += 1
            archive_key = f"{_ARCHIVE_TELEMETRY_KEY}.{suffix}"
        _insert_telemetry(
            connection,
            project_id,
            archive_key,
            value,
            archived_at,
        )
        existing_keys.add(archive_key)

    # The base archive key always denotes the exact old dedicated-table row.
    # Any prior live value is archived second under a suffixed key.
    if base_archive_row is None:
        archive(raw, updated_at)
    else:
        archive(
            base_archive_row["value"],
            base_archive_row["updated_at"],
        )
        connection.execute(
            sa.text(
                "UPDATE project_telemetry "
                "SET value = CAST(:value AS jsonb), updated_at = :updated_at "
                "WHERE project_id = CAST(:project_id AS uuid) AND key = :key"
            ),
            {
                "project_id": str(project_id),
                "key": _ARCHIVE_TELEMETRY_KEY,
                "value": _json(raw),
                "updated_at": updated_at,
            },
        )
    if live_row is None:
        if isinstance(raw, Mapping):
            _insert_telemetry(
                connection,
                project_id,
                _LIVE_TELEMETRY_KEY,
                raw,
                updated_at,
            )
        return

    live_value = live_row["value"]
    if not isinstance(live_value, Mapping):
        archive(live_value, live_row["updated_at"])
        if isinstance(raw, Mapping):
            connection.execute(
                sa.text(
                    "UPDATE project_telemetry "
                    "SET value = CAST(:value AS jsonb), updated_at = :updated_at "
                    "WHERE project_id = CAST(:project_id AS uuid) AND key = :key"
                ),
                {
                    "project_id": str(project_id),
                    "key": _LIVE_TELEMETRY_KEY,
                    "value": _json(raw),
                    "updated_at": updated_at,
                },
            )
        else:
            connection.execute(
                sa.text(
                    "DELETE FROM project_telemetry "
                    "WHERE project_id = CAST(:project_id AS uuid) AND key = :key"
                ),
                {"project_id": str(project_id), "key": _LIVE_TELEMETRY_KEY},
            )
        return

    archive(live_value, live_row["updated_at"])
    if isinstance(raw, Mapping):
        raw_is_newer = updated_at > live_row["updated_at"]
        merged = (
            {**live_value, **raw}
            if raw_is_newer
            else {**raw, **live_value}
        )
        connection.execute(
            sa.text(
                "UPDATE project_telemetry "
                "SET value = CAST(:value AS jsonb), updated_at = :updated_at "
                "WHERE project_id = CAST(:project_id AS uuid) AND key = :key"
            ),
            {
                "project_id": str(project_id),
                "key": _LIVE_TELEMETRY_KEY,
                "value": _json(merged),
                "updated_at": max(updated_at, live_row["updated_at"]),
            },
        )


def _preserved_raw_by_project(connection) -> dict[str, tuple[Any, datetime]]:
    rows = connection.execute(
        sa.text(
            "SELECT DISTINCT ON (project_id) project_id, value, updated_at "
            "FROM project_telemetry "
            "WHERE key = :live_key OR key = :archive_key "
            "OR key LIKE :archive_prefix "
            "ORDER BY project_id, "
            "CASE WHEN key = :live_key THEN 0 ELSE 1 END, key"
        ),
        {
            "live_key": _LIVE_TELEMETRY_KEY,
            "archive_key": _ARCHIVE_TELEMETRY_KEY,
            "archive_prefix": f"{_ARCHIVE_TELEMETRY_KEY}.%",
        },
    ).mappings()
    return {
        str(row["project_id"]): (row["value"], row["updated_at"])
        for row in rows
    }


def _legacy_raw_by_project(connection) -> dict[str, tuple[Any, datetime]]:
    rows = connection.execute(
        sa.text(
            "SELECT DISTINCT ON (project_id) project_id, value, updated_at "
            "FROM project_telemetry "
            "WHERE key = :live_key OR key = :archive_key "
            "ORDER BY project_id, "
            "CASE WHEN key = :archive_key THEN 0 ELSE 1 END"
        ),
        {
            "live_key": _LIVE_TELEMETRY_KEY,
            "archive_key": _ARCHIVE_TELEMETRY_KEY,
        },
    ).mappings()
    return {
        str(row["project_id"]): (row["value"], row["updated_at"])
        for row in rows
    }


def _snapshot_candidates(raw: Any) -> list[Mapping[str, Any]]:
    if not isinstance(raw, Mapping):
        return []
    snapshots = raw.get("snapshots")
    if isinstance(snapshots, list):
        return [item for item in snapshots if isinstance(item, Mapping)]
    if "snapshots" in raw or "baseline" in raw:
        return []
    return [raw]


def _snapshot_blob(blob: Any, captured_at: datetime) -> Any:
    if isinstance(blob, Mapping):
        value = dict(blob)
        value.setdefault("captured_at", captured_at.isoformat())
        return value
    return {
        "captured_at": captured_at.isoformat(),
        "_preserved_blob": blob,
    }


def upgrade() -> None:
    connection = op.get_bind()
    op.rename_table(_TABLE, _LEGACY_TABLE)
    op.execute(
        sa.text(
            f'ALTER TABLE "{_LEGACY_TABLE}" '
            f'RENAME CONSTRAINT "{_TABLE}_pkey" '
            f'TO "{_LEGACY_TABLE}_pkey"'
        )
    )
    _create_multirow_table()

    legacy_rows = connection.execute(
        sa.text(
            f'SELECT project_id, data, updated_at FROM "{_LEGACY_TABLE}" '
            "ORDER BY project_id"
        )
    ).mappings()
    insert_row = sa.text(
        f"INSERT INTO {_TABLE} "
        "(id, project_id, captured_at, metric_def_version, blob) VALUES "
        "(CAST(:id AS uuid), CAST(:project_id AS uuid), :captured_at, "
        ":metric_def_version, CAST(:blob AS jsonb))"
    )
    for row in legacy_rows:
        project_id = row["project_id"]
        raw = row["data"]
        updated_at = _utc_datetime(row["updated_at"])
        assert updated_at is not None
        _preserve_raw(connection, project_id, raw, updated_at)

        # Normalize timestamps before de-duplication so equivalent offsets
        # choose the same slot. The later source item wins deterministically;
        # the exact source container remains preserved in telemetry.
        candidates_by_time: dict[datetime, Mapping[str, Any]] = {}
        for candidate in _snapshot_candidates(raw):
            captured_at = _utc_datetime(candidate.get("captured_at")) or updated_at
            candidates_by_time[captured_at] = candidate
        for captured_at in sorted(candidates_by_time):
            candidate = candidates_by_time[captured_at]
            connection.execute(
                insert_row,
                {
                    "id": str(uuid.uuid4()),
                    "project_id": str(project_id),
                    "captured_at": captured_at,
                    "metric_def_version": _definition_version(
                        candidate.get("metric_def_version")
                    ),
                    "blob": _json(candidate),
                },
            )

    op.drop_table(_LEGACY_TABLE)


def downgrade() -> None:
    connection = op.get_bind()
    op.drop_index("ix_project_efficiency_snapshot_project_id", table_name=_TABLE)
    op.rename_table(_TABLE, _DOWNGRADE_TABLE)
    op.execute(
        sa.text(
            f'ALTER TABLE "{_DOWNGRADE_TABLE}" '
            f'RENAME CONSTRAINT "{_TABLE}_pkey" '
            f'TO "{_DOWNGRADE_TABLE}_pkey"'
        )
    )
    _create_legacy_table()

    preserved = _preserved_raw_by_project(connection)
    legacy = _legacy_raw_by_project(connection)
    multi_rows = connection.execute(
        sa.text(
            f"SELECT project_id, captured_at, metric_def_version, blob "
            f'FROM "{_DOWNGRADE_TABLE}" '
            "ORDER BY project_id, captured_at"
        )
    ).mappings()
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    captured_max: dict[str, datetime] = {}
    for row in multi_rows:
        project_id = str(row["project_id"])
        grouped.setdefault(project_id, []).append(row)
        captured_at = _utc_datetime(row["captured_at"])
        assert captured_at is not None
        captured_max[project_id] = max(
            captured_max.get(project_id, captured_at),
            captured_at,
        )

    project_ids = set(preserved) | set(grouped)
    insert_old = sa.text(
        f"INSERT INTO {_TABLE} (project_id, data, updated_at) VALUES "
        "(CAST(:project_id AS uuid), CAST(:data AS jsonb), :updated_at)"
    )
    for project_id in sorted(project_ids):
        if project_id in preserved:
            preserved_value, preserved_at = preserved[project_id]
        else:
            preserved_value = {}
            preserved_at = captured_max[project_id]
        source_value, source_at = legacy.get(
            project_id,
            (preserved_value, preserved_at),
        )
        expected: dict[datetime, tuple[str, Any]] = {}
        for candidate in _snapshot_candidates(source_value):
            captured_at = _utc_datetime(candidate.get("captured_at")) or source_at
            expected[captured_at] = (
                _definition_version(candidate.get("metric_def_version")),
                candidate,
            )
        project_rows = grouped.get(project_id, [])
        unchanged = len(project_rows) == len(expected) and all(
            (_utc_datetime(row["captured_at"]) in expected)
            and row["metric_def_version"]
            == expected[_utc_datetime(row["captured_at"])][0]
            and row["blob"] == expected[_utc_datetime(row["captured_at"])][1]
            for row in project_rows
        )
        if unchanged:
            connection.execute(
                insert_old,
                {
                    "project_id": project_id,
                    "data": _json(preserved_value),
                    "updated_at": preserved_at,
                },
            )
            continue

        container = (
            dict(preserved_value)
            if isinstance(preserved_value, Mapping)
            else {}
        )
        original_snapshots = container.get("snapshots")
        source_snapshots = (
            list(original_snapshots)
            if isinstance(original_snapshots, list)
            else []
        )
        snapshots: list[Any] = list(source_snapshots)
        positions: dict[datetime, int] = {}
        for index, snapshot in enumerate(source_snapshots):
            if isinstance(snapshot, Mapping):
                key = _utc_datetime(snapshot.get("captured_at")) or source_at
                positions[key] = index
        actual_keys = {
            _utc_datetime(row["captured_at"]) for row in project_rows
        }
        removed_positions = {
            index
            for key, index in positions.items()
            if key not in actual_keys
        }
        snapshots = [
            value
            for index, value in enumerate(snapshots)
            if index not in removed_positions
        ]
        positions = {}
        for index, snapshot in enumerate(snapshots):
            if isinstance(snapshot, Mapping):
                positions[_utc_datetime(snapshot.get("captured_at")) or source_at] = index
        for row in project_rows:
            captured_at = _utc_datetime(row["captured_at"])
            assert captured_at is not None
            snapshot = _snapshot_blob(row["blob"], captured_at)
            if isinstance(snapshot, dict):
                snapshot["metric_def_version"] = row["metric_def_version"]
            if captured_at in positions:
                snapshots[positions[captured_at]] = snapshot
            else:
                positions[captured_at] = len(snapshots)
                snapshots.append(snapshot)

        if snapshots:
            snapshots.sort(
                key=lambda value: (
                    _utc_datetime(value.get("captured_at"))
                    if isinstance(value, Mapping)
                    else datetime.min.replace(tzinfo=timezone.utc)
                )
                or datetime.min.replace(tzinfo=timezone.utc)
            )
            container["snapshots"] = snapshots
            latest = snapshots[-1]
            if "baseline" not in container:
                container["baseline"] = latest
            if isinstance(latest, Mapping):
                container["last_run_at"] = latest.get("captured_at")

        data: Any = container
        if not project_rows and not isinstance(
            preserved_value,
            Mapping,
        ):
            data = preserved_value
        changed_times = [
            _utc_datetime(row["captured_at"])
            for row in project_rows
            if _utc_datetime(row["captured_at"]) not in expected
            or row["blob"]
            != expected[_utc_datetime(row["captured_at"])][1]
            or row["metric_def_version"]
            != expected[_utc_datetime(row["captured_at"])][0]
        ]
        updated_at = max(
            [preserved_at, *(time for time in changed_times if time)]
        )
        connection.execute(
            insert_old,
            {
                "project_id": project_id,
                "data": _json(data),
                "updated_at": updated_at,
            },
        )

    op.drop_table(_DOWNGRADE_TABLE)
