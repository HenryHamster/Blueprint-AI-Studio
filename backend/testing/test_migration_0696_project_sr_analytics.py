"""Executable coverage for migration 0696's preserving SR backfill."""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0696_backfill_project_sr_analytics.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0696", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _schema(connection):
    metadata = sa.MetaData()
    project = sa.Table(
        "project",
        metadata,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("design_doc", sa.JSON(), nullable=False),
    )
    sr = sa.Table(
        "project_sr_analytics",
        metadata,
        sa.Column("project_id", sa.String(), primary_key=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    metadata.create_all(connection)
    return project, sr


def _run_upgrade(migration, connection):
    original_op = migration.op
    migration.op = Operations(MigrationContext.configure(connection))
    try:
        migration.upgrade()
    finally:
        migration.op = original_op


def test_upgrade_backfills_only_sr_keys_and_dedicated_values_win():
    migration = _migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        project, sr = _schema(connection)
        connection.execute(
            project.insert(),
            [
                {
                    "id": "legacy-only",
                    "design_doc": {
                        "sr_catch_rate_trends": {"source": "legacy"},
                        "sr_engagement_threshold": 0.8,
                        "unrelated": {"must_not_move": True},
                    },
                },
                {
                    "id": "partial-dedicated",
                    "design_doc": {
                        "sr_catch_rate_trends": {"source": "legacy"},
                        "sr_floor_threshold": 0.6,
                    },
                },
                {"id": "no-sr", "design_doc": {"notes": "leave me"}},
            ],
        )
        connection.execute(
            sr.insert(),
            {
                "project_id": "partial-dedicated",
                "data": {"sr_catch_rate_trends": {"source": "dedicated"}},
                "updated_at": datetime(2026, 7, 22),
            },
        )

        _run_upgrade(migration, connection)
        _run_upgrade(migration, connection)

        rows = {
            row.project_id: row.data
            for row in connection.execute(sa.select(sr)).all()
        }
        assert rows["legacy-only"] == {
            "sr_catch_rate_trends": {"source": "legacy"},
            "sr_engagement_threshold": 0.8,
        }
        assert rows["partial-dedicated"] == {
            "sr_catch_rate_trends": {"source": "dedicated"},
            "sr_floor_threshold": 0.6,
        }
        assert "no-sr" not in rows
        assert "unrelated" not in rows["legacy-only"]


def test_legacy_payload_accepts_json_text_and_preserves_falsey_values():
    migration = _migration()

    payload = migration._legacy_sr_payload(
        json.dumps(
            {
                "sr_floor_threshold": 0,
                "sr_engagement_min_passes": False,
                "sr_catch_rate_trends": {},
                "unrelated": "ignored",
            }
        )
    )

    assert payload == {
        "sr_floor_threshold": 0,
        "sr_engagement_min_passes": False,
        "sr_catch_rate_trends": {},
    }


def test_downgrade_is_non_destructive():
    migration = _migration()
    assert migration.downgrade() is None


def test_migration_key_inventory_matches_runtime_inventory():
    migration = _migration()
    from app.sr_analytics_store import LEGACY_SR_ANALYTICS_KEYS

    assert migration.LEGACY_SR_ANALYTICS_KEYS == LEGACY_SR_ANALYTICS_KEYS


def test_upgrade_backfills_every_documented_legacy_key():
    migration = _migration()
    engine = sa.create_engine("sqlite://")
    all_legacy_values = {
        key: {"inventory_key": key}
        for key in sorted(migration.LEGACY_SR_ANALYTICS_KEYS)
    }
    with engine.begin() as connection:
        project, sr = _schema(connection)
        connection.execute(
            project.insert(),
            {
                "id": "all-legacy-keys",
                "design_doc": {
                    **all_legacy_values,
                    "unrelated": {"must_not_move": True},
                },
            },
        )

        _run_upgrade(migration, connection)
        _run_upgrade(migration, connection)

        rows = connection.execute(sa.select(sr)).mappings().all()
        assert len(rows) == 1
        assert rows[0]["data"] == all_legacy_values
        assert set(rows[0]["data"]) == migration.LEGACY_SR_ANALYTICS_KEYS
        assert "unrelated" not in rows[0]["data"]
