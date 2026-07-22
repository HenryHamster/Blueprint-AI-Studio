"""
Shared helper functions for compute test modules.

These helpers are used across test_compute.py and test_compute_image_build_local.py.
"""

import base64
import io
import json
import tarfile

from app.models import Project, Task, TaskStatus


def _b64_text(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _terminal_record_bytes(
    *,
    status: str = "SUCCEEDED",
    exit_code=0,
    reason: str = "completed",
    metrics: dict | None = None,
    command_log: dict | None = None,
    release_sha: str = "",
    resume_provenance: dict | None = None,
    schema_version: int = 1,
) -> bytes:
    """terminal.json bytes exactly as the on-VM terminal_record.py would write."""
    record = {
        "schema_version": schema_version,
        "status": status,
        "exit_code": exit_code,
        "reason": reason,
        "metrics_b64": _b64_text(json.dumps(metrics)) if metrics is not None else "",
        "command_log_b64": (
            _b64_text(json.dumps(command_log)) if command_log is not None else ""
        ),
        "release_echo_b64": _b64_text(release_sha) if release_sha else "",
        "resume_provenance_b64": (
            _b64_text(json.dumps(resume_provenance))
            if resume_provenance is not None
            else ""
        ),
    }
    return json.dumps(record).encode("utf-8")


def _make_tar_gz_bundle(members: dict[str, bytes]) -> bytes:
    """Build a gzip tar bundle mirroring the on-VM ``tar -C dir -czf - .`` layout."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


async def _make_compute_task(db_session):
    project = Project(name="GPU Compute Job", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


