"""Shared SQLModel column primitives with no app-internal dependencies.

This module holds the low-level column types that the ORM table modules
(:mod:`app.models` and the domain sibling modules it re-exports, such as
:mod:`app.release_models`) build their columns from.  It is intentionally
dependency-free — importing nothing from ``app`` — so those table modules can
share it without forming an import cycle.  ``app.models`` re-exports everything
here, so ``from app.models import NaiveUTCDateTime`` keeps working.
"""

from datetime import timezone

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator


class NaiveUTCDateTime(TypeDecorator):
    """TIMESTAMPTZ in DB, naive UTC datetime in Python.

    Phase D2 (alembic 0061) migrated several columns to TIMESTAMPTZ for
    server-default support and clock-skew safety, but the rest of the codebase
    uses naive UTC throughout (_utcnow() returns naive). Rather than sweep
    every comparison site to handle aware datetimes, strip tz on read.

    Round 3 audit (M1): symmetric write path. Future tz-aware writers
    (e.g. anything calling ``datetime.now(timezone.utc)`` instead of
    ``_utcnow()``) get normalized to naive UTC on the way to the DB so the
    column stays internally consistent. asyncpg sends naive datetimes to
    TIMESTAMPTZ columns and Postgres interprets them as UTC.

    Pairs with the F1 helper rename _to_naive_utc -> _strip_tz_assume_utc;
    after this decorator lands, ORM-loaded datetimes are already naive and
    callsite strips become no-ops (audit 2026-04-29 round 2 / C3 + H1).
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
