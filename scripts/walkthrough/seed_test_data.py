#!/usr/bin/env python3
"""Seed minimal test data for the v26 operator walkthrough.

Creates a test project + owner membership in the DB so the ADMIN_API_KEY
bearer token (user_id='admin-api-key') can exercise project-scoped endpoints
without a real GitHub onboarding flow.

Usage:
    DATABASE_URL=postgresql+asyncpg://... python3 seed_test_data.py

Idempotent: re-running does not duplicate rows.
"""
import asyncio
import os
import uuid
from datetime import datetime, timezone

import asyncpg

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:walkthrough123@localhost:5433/blueprint",
)

# Normalise asyncpg URL (asyncpg doesn't accept the SQLAlchemy scheme prefix)
PG_DSN = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")

# Fixed IDs so re-runs are idempotent
PROJECT_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
MEMBER_ID = uuid.UUID("00000000-0000-4000-8000-000000000002")
ALLOWED_USER_ID = uuid.UUID("00000000-0000-4000-8000-000000000003")
USER_ID = "admin-api-key"  # matches ADMIN_API_KEY auth path in auth.py


async def seed():
    conn = await asyncpg.connect(PG_DSN)
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        # 1. AllowedUser (optional — lets ADMIN_API_KEY resolve a real UID)
        await conn.execute(
            """
            INSERT INTO alloweduser (id, email, role, firebase_uid, created_at)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (id) DO NOTHING
            """,
            ALLOWED_USER_ID,
            "admin@walkthrough.local",
            "admin",
            USER_ID,
            now,
        )

        # 2. Project
        await conn.execute(
            """
            INSERT INTO project (id, name, user_id, status, created_at, directions,
                                 github_owner, github_repo)
            VALUES ($1, $2, $3, 'DEPLOYING', $4, '[]'::jsonb, NULL, NULL)
            ON CONFLICT (id) DO NOTHING
            """,
            PROJECT_ID,
            "v26 Walkthrough Test Project",
            USER_ID,
            now,
        )

        # 3. ProjectMember (owner role)
        await conn.execute(
            """
            INSERT INTO projectmember (id, project_id, user_id, role, created_at)
            VALUES ($1, $2, $3, 'owner', $4)
            ON CONFLICT (id) DO NOTHING
            """,
            MEMBER_ID,
            PROJECT_ID,
            USER_ID,
            now,
        )

        print(f"✓ Project: {PROJECT_ID}")
        print(f"✓ ProjectMember: {MEMBER_ID} (user_id={USER_ID!r}, role=owner)")
        print(f"✓ AllowedUser: {ALLOWED_USER_ID} (email=admin@walkthrough.local)")

        # 4. Create a test ProjectMessage for the Messages flow test (upsert to reset status)
        message_id = uuid.UUID("00000000-0000-4000-8000-000000000010")
        await conn.execute(
            """
            INSERT INTO projectmessage (id, project_id, created_at, content,
                message_type, from_type, status, resolved_at)
            VALUES ($1, $2, $3, $4, 'NOTE', 'system', 'open', NULL)
            ON CONFLICT (id) DO UPDATE SET status = 'open', resolved_at = NULL, resolution = NULL
            """,
            message_id,
            PROJECT_ID,
            now,
            "Walkthrough test message: v26 qualification run 2026-07-16",
        )
        print(f"✓ ProjectMessage: {message_id}")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(seed())
    print("\nSeed complete. Test project ID:", PROJECT_ID)
