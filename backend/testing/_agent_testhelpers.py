# Shared test helpers extracted verbatim from test_agent.py (domain split).
# Star-imported by the test_agent__*.py bucket files; re-exports the
# complete original module namespace via __all__ so no test loses a symbol.
"""Shared test infrastructure for agent endpoint tests.

The domain slices in test_agent__*.py exercise specific `/api/agent/` endpoint
groups (claim, status, heartbeat, compute_jobs, playbook, design_doc, etc.).
This preamble collects the shared imports, fixtures, and seed helpers they
all depend on."""

import logging
import os
import asyncio
import hashlib
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from app.database import get_session
from app.main import app
from testing.conftest import USE_POSTGRES
from app.models import (
    Artifact,
    ArtifactType,
    ComputeJob,
    ComputeJobOutcome,
    ComputeJobStatus,
    DeploymentState,
    DeploymentStatus,
    KnowledgeDocument,
    LLMAuthMode,
    LLMProviderName,
    MaxPlanAccount,
    PoolVmState,
    Project,
    ProjectMember,
    ProjectStatus,
    ReleaseDeployArtifact,
    ReleaseVersion,
    SourceKind,
    Task,
    TaskHistory,
    TaskStatus,
    TaskWakeRegistration,
    WakeDeliveryState,
)
from app.auth import create_agent_token, create_vm_scoped_token
from app.schemas import AgentStatusPayload
from app.routers.agent_status import _is_task_llm_auth_hard_disable
from app.task_capabilities import TaskType, task_type_catalog
from app.time_utils import _utcnow


def _resolve_sr_recent_miss_examples_contract_fixture() -> Path:
    """Locate the SR-recent-miss-examples contract fixture across local + Docker.

    Search order:
      1. ``SR_CONTRACT_FIXTURE`` env override (caller-supplied path).
      2. ``backend/testing/__fixtures__/...`` (always present in the backend
         Docker build context — the path that works in CI).
      3. ``agent/src/prompts/__fixtures__/...`` resolved from the host
         repo root (works in local dev outside Docker).

    The two on-disk copies are kept byte-identical;
    ``test_claim_sr_recent_miss_examples_fixture_parity`` asserts this when
    both are reachable. The duplicate exists because the backend image's
    build context (``./backend``) cannot reach ``agent/``.
    """
    candidates: list[Path] = []
    env_override = os.getenv("SR_CONTRACT_FIXTURE")
    if env_override:
        candidates.append(Path(env_override))
    candidates.append(
        Path(__file__).parent / "__fixtures__" / "sr_recent_miss_examples_contract.json"
    )
    candidates.append(
        Path(__file__).resolve().parents[2]
        / "agent"
        / "src"
        / "prompts"
        / "__fixtures__"
        / "sr_recent_miss_examples_contract.json"
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise AssertionError(
        "SR contract fixture not found in any candidate path: "
        + ", ".join(str(c) for c in candidates)
    )


@pytest.fixture
def deploy_health_state_guard():
    """Snapshot deploy-health globals and shared state that these endpoint tests mutate."""
    import copy
    from app import shared_state
    import app.deploy_health as deploy_health

    snapshot = {
        "external_health": copy.deepcopy(shared_state.external_health),
        "deploy_monitoring": copy.deepcopy(shared_state.deploy_monitoring),
        "pause_failure_streak": deploy_health._pause_failure_streak,
        "health_recovery_history": list(deploy_health._health_recovery_history),
        "last_auto_resume_at": deploy_health._last_auto_resume_at,
        "recent_auto_resumes": list(deploy_health._recent_auto_resumes),
        "last_resume_was_forced": deploy_health._last_resume_was_forced,
        "consecutive_force_resumes": deploy_health._consecutive_force_resumes,
    }
    yield
    shared_state.external_health.clear()
    shared_state.external_health.update(snapshot["external_health"])
    shared_state.deploy_monitoring.clear()
    shared_state.deploy_monitoring.update(snapshot["deploy_monitoring"])
    deploy_health._pause_failure_streak = snapshot["pause_failure_streak"]
    deploy_health._health_recovery_history.clear()
    deploy_health._health_recovery_history.extend(snapshot["health_recovery_history"])
    deploy_health._last_auto_resume_at = snapshot["last_auto_resume_at"]
    deploy_health._recent_auto_resumes.clear()
    deploy_health._recent_auto_resumes.extend(snapshot["recent_auto_resumes"])
    deploy_health._last_resume_was_forced = snapshot["last_resume_was_forced"]
    deploy_health._consecutive_force_resumes = snapshot["consecutive_force_resumes"]


async def _seed_deploy_state(
    db_session,
    *,
    status: str = DeploymentStatus.PAUSED,
    pause_reason: str = "External dependency down: dispatch_api",
    pre_pause_max_vms: int | None = 4,
    runtime_max_concurrent_vms: int | None = 4,
    paused_at: datetime | None = None,
) -> DeploymentState:
    state = DeploymentState(
        id=1,
        current_revision="rev-test-1",
        current_sha="a" * 40,
        deployed_at=_utcnow() - timedelta(minutes=10),
        status=status,
        last_known_good_revision="rev-good-1",
        last_known_good_sha="b" * 40,
        pause_reason=pause_reason,
        paused_at=paused_at or (_utcnow() - timedelta(minutes=3)),
        pre_pause_max_vms=pre_pause_max_vms,
        runtime_max_concurrent_vms=runtime_max_concurrent_vms,
    )
    db_session.add(state)
    await db_session.commit()
    await db_session.refresh(state)
    return state


__all__ = [
    'logging',
    'os',
    'asyncio',
    'hashlib',
    'time',
    'uuid',
    'datetime',
    'timedelta',
    'Path',
    'AsyncMock',
    'patch',
    'pytest',
    'AsyncClient',
    'select',
    'text',
    'get_session',
    'app',
    'USE_POSTGRES',
    'Artifact',
    'ArtifactType',
    'ComputeJob',
    'ComputeJobOutcome',
    'ComputeJobStatus',
    'DeploymentState',
    'DeploymentStatus',
    'KnowledgeDocument',
    'LLMAuthMode',
    'LLMProviderName',
    'MaxPlanAccount',
    'PoolVmState',
    'Project',
    'ProjectMember',
    'ProjectStatus',
    'ReleaseDeployArtifact',
    'ReleaseVersion',
    'SourceKind',
    'Task',
    'TaskHistory',
    'TaskStatus',
    'TaskWakeRegistration',
    'WakeDeliveryState',
    'create_agent_token',
    'create_vm_scoped_token',
    'AgentStatusPayload',
    '_is_task_llm_auth_hard_disable',
    'TaskType',
    'task_type_catalog',
    '_utcnow',
    '_resolve_sr_recent_miss_examples_contract_fixture',
    'deploy_health_state_guard',
    '_seed_deploy_state',
]
