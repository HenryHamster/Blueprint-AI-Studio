"""Rebuild project_lane_baseline without discarding legacy rows.

Migration 0658 created project_lane_baseline as a one-row-per-project table
whose ``data`` value is a whole version-keyed map.  The runtime requires one
row per project/version.  Preserve the original container in
``project_telemetry`` and expand every canonical, representable version entry.

Revision ID: 0681
Revises: 0680
Create Date: 2026-07-22
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

revision = "0681"
down_revision = "0680"
branch_labels = None
depends_on = None

_TABLE = "project_lane_baseline"
_LEGACY_TABLE = "project_lane_baseline_legacy_0681"
_DOWNGRADE_TABLE = "project_lane_baseline_multi_0681"
_LIVE_TELEMETRY_KEY = "lane_baselines"
_ARCHIVE_TELEMETRY_KEY = "migration_0681_lane_baseline_raw"
_PG_INTEGER_MAX = 2_147_483_647


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


def _canonical_version(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    if value == "0":
        return 0
    if not value or value[0] == "0" or not value.isascii() or not value.isdigit():
        return None
    parsed = int(value)
    return parsed if parsed <= _PG_INTEGER_MAX else None


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
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("baseline_def_version", sa.String(length=64), nullable=False),
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
            "version_number",
            name="uq_proj_lane_baseline",
        ),
    )
    op.create_index(
        "ix_project_lane_baseline_project_id",
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
        "(id, project_id, version_number, captured_at, "
        "baseline_def_version, blob) VALUES "
        "(CAST(:id AS uuid), CAST(:project_id AS uuid), :version_number, "
        ":captured_at, :baseline_def_version, CAST(:blob AS jsonb))"
    )
    for row in legacy_rows:
        project_id = row["project_id"]
        raw = row["data"]
        updated_at = _utc_datetime(row["updated_at"])
        assert updated_at is not None
        _preserve_raw(connection, project_id, raw, updated_at)
        if not isinstance(raw, Mapping):
            continue
        for raw_version, baseline in raw.items():
            version_number = _canonical_version(raw_version)
            if version_number is None or not isinstance(baseline, Mapping):
                continue
            connection.execute(
                insert_row,
                {
                    "id": str(uuid.uuid4()),
                    "project_id": str(project_id),
                    "version_number": version_number,
                    "captured_at": (
                        _utc_datetime(baseline.get("captured_at")) or updated_at
                    ),
                    "baseline_def_version": _definition_version(
                        baseline.get("baseline_def_version")
                    ),
                    "blob": _json(baseline),
                },
            )

    op.drop_table(_LEGACY_TABLE)


def downgrade() -> None:
    connection = op.get_bind()
    op.drop_index("ix_project_lane_baseline_project_id", table_name=_TABLE)
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
            f"SELECT project_id, version_number, captured_at, "
            f"baseline_def_version, blob "
            f'FROM "{_DOWNGRADE_TABLE}" '
            "ORDER BY project_id, version_number"
        )
    ).mappings()
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in multi_rows:
        grouped.setdefault(str(row["project_id"]), []).append(row)

    insert_old = sa.text(
        f"INSERT INTO {_TABLE} (project_id, data, updated_at) VALUES "
        "(CAST(:project_id AS uuid), CAST(:data AS jsonb), :updated_at)"
    )
    for project_id in sorted(set(preserved) | set(grouped)):
        project_times = [
            _utc_datetime(row["captured_at"])
            for row in grouped.get(project_id, [])
        ]
        deterministic_at = (
            preserved[project_id][1]
            if project_id in preserved
            else max(time for time in project_times if time)
        )
        authority_value, authority_at = preserved.get(
            project_id,
            ({}, deterministic_at),
        )
        source_value, source_at = legacy.get(
            project_id,
            (authority_value, authority_at),
        )
        expected: dict[int, tuple[datetime, str, Any]] = {}
        if isinstance(source_value, Mapping):
            for raw_version, baseline in source_value.items():
                version_number = _canonical_version(raw_version)
                if version_number is None or not isinstance(baseline, Mapping):
                    continue
                expected[version_number] = (
                    _utc_datetime(baseline.get("captured_at")) or source_at,
                    _definition_version(baseline.get("baseline_def_version")),
                    baseline,
                )
        actual = grouped.get(project_id, [])
        unchanged = len(actual) == len(expected) and all(
            row["version_number"] in expected
            and _utc_datetime(row["captured_at"])
            == expected[row["version_number"]][0]
            and row["baseline_def_version"]
            == expected[row["version_number"]][1]
            and row["blob"] == expected[row["version_number"]][2]
            for row in actual
        )
        data = authority_value
        updated_at = authority_at
        if not unchanged:
            data = (
                dict(authority_value)
                if isinstance(authority_value, Mapping)
                else {}
            )
            for version_number in expected:
                data.pop(str(version_number), None)
            changed_times: list[datetime] = []
            for row in actual:
                data[str(row["version_number"])] = row["blob"]
                expected_row = expected.get(row["version_number"])
                if (
                    expected_row is None
                    or row["blob"] != expected_row[2]
                    or row["baseline_def_version"] != expected_row[1]
                ):
                    captured_at = _utc_datetime(row["captured_at"])
                    assert captured_at is not None
                    changed_times.append(captured_at)
            if changed_times:
                updated_at = max([authority_at, *changed_times])
        connection.execute(
            insert_old,
            {
                "project_id": project_id,
                "data": _json(data),
                "updated_at": updated_at,
            },
        )

    op.drop_table(_DOWNGRADE_TABLE)
