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

        # 4. Create a test ProjectMessage (system/NOTE) for the Messages flow test.
        # This is the message that 3c resolves — upsert to reset status on re-run.
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
        print(f"✓ ProjectMessage (system/NOTE, resolved by flow 3c): {message_id}")

        # 5. Create a second open operator-audience message (user/QUESTION) so
        # that flow 3e can assert visible message bubbles/composer after 3c resolves
        # the first message.  from_type='user' passes the isConversationMessage()
        # check and renders via MessageBubble with data-message-id.
        # created_at is 1 second later so this sorts after message_id in the list.
        import datetime as _dt
        later = now + _dt.timedelta(seconds=1)
        operator_msg_id = uuid.UUID("00000000-0000-4000-8000-000000000011")
        await conn.execute(
            """
            INSERT INTO projectmessage (id, project_id, created_at, content,
                message_type, from_type, status, resolved_at)
            VALUES ($1, $2, $3, $4, 'QUESTION', 'user', 'open', NULL)
            ON CONFLICT (id) DO UPDATE SET status = 'open', resolved_at = NULL, resolution = NULL
            """,
            operator_msg_id,
            PROJECT_ID,
            later,
            "Walkthrough seed: operator question — verifies MessagesPanel renders message bubbles (flow 3e).",
        )
        print(f"✓ ProjectMessage (user/QUESTION, stays open for flow 3e UI): {operator_msg_id}")

        # 6. Create an active (undismissed) ERROR alert so that flow 3f can assert
        # the bg-red-500 cross-tab alert indicator.  event_type=NULL passes the
        # operator_visible_alert_filter() (null is treated as operator-visible legacy).
        alert_id = uuid.UUID("00000000-0000-4000-8000-000000000020")
        await conn.execute(
            """
            INSERT INTO alert (id, project_id, created_at, title, message, severity,
                domain, event_type, dismissed_at)
            VALUES ($1, $2, $3, $4, $5, 'ERROR', 'core', NULL, NULL)
            ON CONFLICT (id) DO UPDATE SET dismissed_at = NULL
            """,
            alert_id,
            PROJECT_ID,
            now,
            "Walkthrough seed: active alert",
            "Seeded for flow 3f — verifies cross-tab alert indicator (bg-red-500 dot).",
        )
        print(f"✓ Alert (ERROR, undismissed, event_type=NULL, for flow 3f): {alert_id}")

        # 7. Seed a terminal SUCCEEDED ComputeJob with three viewable output files
        # (image/png, video/mp4, application/pdf) for Flow 4 — compute viewable
        # artifacts acceptance evidence.  polling_metadata.viewable_files is the
        # manifest read by _viewable_artifacts_descriptor() in admin_compute_jobs.py.
        import json as _json
        compute_job_id = uuid.UUID("00000000-0000-4000-8000-000000000030")
        _gs_base = f"gs://walkthrough-bucket/compute-jobs/{compute_job_id}"
        _viewable_files = [
            {
                "filename": "figure.png",
                "storage_uri": f"{_gs_base}/figure.png",
                "content_type": "image/png",
                "size_bytes": 12345,
                "kind": "image",
                "revision": "terminal",
                "live": False,
            },
            {
                "filename": "recording.mp4",
                "storage_uri": f"{_gs_base}/recording.mp4",
                "content_type": "video/mp4",
                "size_bytes": 987654,
                "kind": "video",
                "revision": "terminal",
                "live": False,
            },
            {
                "filename": "report.pdf",
                "storage_uri": f"{_gs_base}/report.pdf",
                "content_type": "application/pdf",
                "size_bytes": 54321,
                "kind": "pdf",
                "revision": "terminal",
                "live": False,
            },
        ]
        _polling_metadata = _json.dumps({
            "viewable_files": _viewable_files,
            "viewable_file_count": len(_viewable_files),
            "viewable_files_dropped": 0,
        })
        await conn.execute(
            """
            INSERT INTO computejob (
                id, project_id, task_id, backend, status, outcome,
                created_at, updated_at, started_at, completed_at,
                poll_attempt_count, final_cost_usd, terminal_reason,
                polling_metadata
            )
            VALUES (
                $1, $2, NULL, 'local_runner', 'SUCCEEDED', 'SUCCESS',
                $3, $3, $3, $3,
                1, 0.12, 'walkthrough_seed',
                $4::jsonb
            )
            ON CONFLICT (id) DO UPDATE SET
                status = 'SUCCEEDED',
                outcome = 'SUCCESS',
                polling_metadata = $4::jsonb,
                updated_at = $3
            """,
            compute_job_id,
            PROJECT_ID,
            now,
            _polling_metadata,
        )
        print(f"✓ ComputeJob (SUCCEEDED, 3 viewable files, for flow 4): {compute_job_id}")

        # 8. Seed matching ComputeJobFile rows so the signing allowlist recognises
        # each viewable storage_uri.  storage_uri has a UNIQUE constraint so use
        # ON CONFLICT DO UPDATE to keep the rows current across re-runs.
        _cjf_rows = [
            (uuid.UUID("00000000-0000-4000-8000-000000000031"), "figure.png",     "image", f"{_gs_base}/figure.png",    "image/png",       12345),
            (uuid.UUID("00000000-0000-4000-8000-000000000032"), "recording.mp4",  "video", f"{_gs_base}/recording.mp4", "video/mp4",       987654),
            (uuid.UUID("00000000-0000-4000-8000-000000000033"), "report.pdf",     "pdf",   f"{_gs_base}/report.pdf",    "application/pdf", 54321),
        ]
        for _cjf_id, _relpath, _media_type, _uri, _ct, _size in _cjf_rows:
            await conn.execute(
                """
                INSERT INTO computejobfile (
                    id, compute_job_id, safe_relpath, media_type,
                    storage_uri, content_type, file_size_bytes, created_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (storage_uri) DO UPDATE SET
                    compute_job_id = $2,
                    safe_relpath = $3,
                    media_type = $4,
                    content_type = $6,
                    file_size_bytes = $7
                """,
                _cjf_id, compute_job_id, _relpath, _media_type,
                _uri, _ct, _size, now,
            )
        print(f"✓ ComputeJobFile rows (3 viewable files for flow 4): {[str(r[0]) for r in _cjf_rows]}")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(seed())
    print("\nSeed complete. Test project ID:", PROJECT_ID)
