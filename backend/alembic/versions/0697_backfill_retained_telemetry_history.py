"""Backfill retained design-doc efficiency and lane history.

Revision ID: 0697
Revises: 0696
Create Date: 2026-07-22

The dedicated stores are authoritative. This migration only fills absent rows
from rollback-safe legacy values that remain in ``project.design_doc``. It does
not strip those values, so rollback and malformed-entry fallback remain safe.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0697"
down_revision = "0696"
branch_labels = None
depends_on = None

EFFICIENCY_KEY = "efficiency_baseline"
LANE_KEY = "lane_baselines"
_PG_INTEGER_MAX = 2_147_483_647


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(decoded) if isinstance(decoded, Mapping) else {}
    return {}


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _version_number(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed < 0 or str(parsed) != str(value).strip():
        return None
    return parsed if parsed <= _PG_INTEGER_MAX else None


def _definition_version(value: Any) -> str:
    if isinstance(value, str) and value and len(value) <= 64:
        return value
    return "legacy"


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def _datetime_key(value: Any) -> datetime | None:
    parsed = _as_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _db_datetime(value: datetime, *, is_postgres: bool) -> datetime | str:
    return value if is_postgres else value.isoformat()


def upgrade() -> None:
    conn = op.get_bind()
    is_postgres = conn.dialect.name == "postgresql"
    projects = conn.execute(
        sa.text("SELECT id, design_doc FROM project ORDER BY id")
    ).mappings().all()

    existing_efficiency_containers = {
        str(row["project_id"])
        for row in conn.execute(
            sa.text(
                "SELECT project_id FROM project_telemetry "
                "WHERE key = :key ORDER BY project_id"
            ),
            {"key": EFFICIENCY_KEY},
        ).mappings()
    }
    existing_efficiency_snapshots = {
        (str(row["project_id"]), _datetime_key(row["captured_at"]))
        for row in conn.execute(
            sa.text(
                "SELECT project_id, captured_at "
                "FROM project_efficiency_snapshot ORDER BY project_id, captured_at"
            )
        ).mappings()
    }
    existing_lane_baselines = {
        (str(row["project_id"]), int(row["version_number"]))
        for row in conn.execute(
            sa.text(
                "SELECT project_id, version_number "
                "FROM project_lane_baseline ORDER BY project_id, version_number"
            )
        ).mappings()
    }

    uuid_expr = "CAST(:id AS uuid)" if is_postgres else ":id"
    project_id_expr = "CAST(:project_id AS uuid)" if is_postgres else ":project_id"
    json_expr = "CAST(:payload AS jsonb)" if is_postgres else ":payload"

    insert_telemetry = sa.text(
        "INSERT INTO project_telemetry "
        "(id, project_id, key, value, updated_at) VALUES "
        f"({uuid_expr}, {project_id_expr}, :key, {json_expr}, CURRENT_TIMESTAMP)"
    )
    insert_efficiency = sa.text(
        "INSERT INTO project_efficiency_snapshot "
        "(id, project_id, captured_at, metric_def_version, blob) VALUES "
        f"({uuid_expr}, {project_id_expr}, :captured_at, "
        f":metric_def_version, {json_expr})"
    )
    insert_lane = sa.text(
        "INSERT INTO project_lane_baseline "
        "(id, project_id, version_number, captured_at, "
        "baseline_def_version, blob) VALUES "
        f"({uuid_expr}, {project_id_expr}, :version_number, :captured_at, "
        f":baseline_def_version, {json_expr})"
    )

    for project in projects:
        project_id = str(project["id"])
        design_doc = _as_mapping(project["design_doc"])
        efficiency = _as_mapping(design_doc.get(EFFICIENCY_KEY))

        if efficiency and project_id not in existing_efficiency_containers:
            conn.execute(
                insert_telemetry,
                {
                    "id": str(uuid.uuid4()),
                    "project_id": project_id,
                    "key": EFFICIENCY_KEY,
                    "payload": _json(efficiency),
                },
            )
            existing_efficiency_containers.add(project_id)

        snapshots = efficiency.get("snapshots")
        if isinstance(snapshots, list):
            for snapshot_value in snapshots:
                snapshot = _as_mapping(snapshot_value)
                captured_at = _as_datetime(snapshot.get("captured_at"))
                snapshot_key = (project_id, _datetime_key(captured_at))
                if not snapshot or captured_at is None:
                    continue
                if snapshot_key in existing_efficiency_snapshots:
                    continue
                metric_def_version = _definition_version(
                    snapshot.get("metric_def_version")
                )
                conn.execute(
                    insert_efficiency,
                    {
                        "id": str(uuid.uuid4()),
                        "project_id": project_id,
                        "captured_at": _db_datetime(
                            captured_at, is_postgres=is_postgres
                        ),
                        "metric_def_version": metric_def_version,
                        "payload": _json(snapshot),
                    },
                )
                existing_efficiency_snapshots.add(snapshot_key)

        lane_baselines = _as_mapping(design_doc.get(LANE_KEY))
        for raw_version, baseline_value in lane_baselines.items():
            version_number = _version_number(raw_version)
            baseline = _as_mapping(baseline_value)
            captured_at = _as_datetime(baseline.get("captured_at"))
            lane_key = (project_id, version_number)
            if version_number is None or not baseline or captured_at is None:
                continue
            if lane_key in existing_lane_baselines:
                continue
            baseline_def_version = _definition_version(
                baseline.get("baseline_def_version")
            )
            conn.execute(
                insert_lane,
                {
                    "id": str(uuid.uuid4()),
                    "project_id": project_id,
                    "version_number": version_number,
                    "captured_at": _db_datetime(
                        captured_at, is_postgres=is_postgres
                    ),
                    "baseline_def_version": baseline_def_version,
                    "payload": _json(baseline),
                },
            )
            existing_lane_baselines.add(lane_key)


def downgrade() -> None:
    # Forward-only data copy. Removing rows could destroy newer dedicated writes.
    pass
