"""Canonical AllowedUser email normalization and legacy-safe resolution."""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import AllowedUser


class AllowedUserEmailConflict(RuntimeError):
    """Raised when case-variant legacy rows cannot be resolved safely."""


def normalize_allowed_user_email(value: str) -> str:
    """Return the one canonical representation used by every write path."""

    if not isinstance(value, str):
        raise ValueError("Invalid email address")
    normalized = value.strip().lower()
    if "@" not in normalized:
        raise ValueError("Invalid email address")
    return normalized


async def resolve_allowed_user_by_email(
    session: AsyncSession,
    email: str,
    *,
    firebase_uid: str | None = None,
    for_update: bool = False,
) -> AllowedUser | None:
    """Resolve a normalized email while failing closed on legacy duplicates.

    The historical exact-case UNIQUE index permits case variants.  A verified
    Firebase identity may select its own existing binding.  Otherwise exactly
    one unbound record is safe to claim; a different UID binding or multiple
    unbound candidates is an administrative conflict, never a ``first()`` pick.
    """

    normalized = normalize_allowed_user_email(email)
    statement = (
        select(AllowedUser)
        .where(func.lower(AllowedUser.email) == normalized)
        .order_by(AllowedUser.created_at.asc(), AllowedUser.id.asc())
    )
    if for_update:
        statement = statement.with_for_update()
    matches = list((await session.execute(statement)).scalars().all())
    if not matches:
        return None

    if firebase_uid is not None:
        own_bindings = [row for row in matches if row.firebase_uid == firebase_uid]
        if own_bindings:
            return own_bindings[0]

    unbound = [row for row in matches if row.firebase_uid is None]
    bound = [row for row in matches if row.firebase_uid is not None]
    if len(unbound) == 1 and not bound:
        return unbound[0]
    if len(matches) == 1 and firebase_uid is None:
        return matches[0]

    raise AllowedUserEmailConflict(
        "Conflicting allowlist records exist for this normalized email"
    )
