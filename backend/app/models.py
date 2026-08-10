"""SQLModel ORM models and shared enums for the Blueprint orchestrator.

This module is the single source of truth for the persisted schema: the
SQLModel tables (``Project``, ``Task``, ``Artifact``, ``Direction``, and
related entities), the status/type enums used across the backend
(``ProjectStatus``, ``TaskStatus``, ``ArtifactType``, ``LLMProviderName``,
``LLMAuthMode``, ...), and the ``NaiveUTCDateTime`` column type used uniformly
for naive-UTC timestamps. Table definitions here must stay in lock-step with
the Alembic migrations under ``backend/alembic/versions/`` and with
``EXPECTED_SCHEMA_REVISION`` in ``app.main`` — see ``docs/DATA_MODELS.md`` for a
field-level reference.
"""

from enum import Enum
from typing import Dict, Any, List, Mapping
from sqlmodel import SQLModel, Field, Relationship
from sqlmodel.sql.sqltypes import GUID
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    JSON,
    String,
    Integer,
    LargeBinary,
    Float,
    Index,
    Text,
    UniqueConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Numeric,
    or_,
    text,
    event,
    func,
    select,
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session as _OrmSession
import hashlib
import logging
import uuid
from datetime import datetime
from app.capability_registry import derive_provider_marker
from app.models_base import NaiveUTCDateTime
# Release / qualification domain models were peeled into ``app.release_models``
# (see the marker comment further down where they used to be defined) to keep
# this module under its size guardrail. They are imported here and re-exported
# so ``from app.models import ReleaseVersion`` (and siblings) is unchanged and
# so importing this module registers their tables on ``SQLModel.metadata``.
# ``app.release_models`` depends only on ``app.models_base`` / ``app.time_utils``
# (never on ``app.models``), so this import forms no cycle.
from app.release_models import (  # noqa: F401
    AuditFindingResolution,
    CategoryVerdict,
    CompletionDebtEntry,
    CriterionEvidence,
    QualificationDimensionResult,
    QualificationVerdict,
    ReleaseDeployArtifact,
    ReleaseVersion,
    ResidualVerdict,
    VersionAcceptanceCriterion,
)
from app.discovery_models import (  # noqa: F401
    DiscoveryCycle,
    DiscoveryEvidenceItem,
)
from app.time_utils import _utcnow
from app.vm_cost_rates import (
    get_cloud_run_rate_for_machine_type,
    get_vm_rate_per_second,
)


# ``NaiveUTCDateTime`` now lives in :mod:`app.models_base` (a dependency-free
# module shared with the peeled-out domain model modules) and is imported at
# the top of this module. It remains importable as ``app.models.NaiveUTCDateTime``.


logger = logging.getLogger(__name__)
_UNLOADED = object()

_TASK_BANKED_VM_RUNTIME_COST_KEY = "vm_runtime_cost_banked"
_TASK_BANKED_LLM_INPUT_TOKENS_KEY = "llm_input_tokens_banked"
_TASK_BANKED_LLM_OUTPUT_TOKENS_KEY = "llm_output_tokens_banked"
_TASK_BANKED_LLM_CALL_COUNT_KEY = "llm_call_count_banked"
_TASK_ATTEMPT_HISTORY_KEY = "attempt_history"
TASK_CURRENT_ATTEMPT_ORDINAL_CONTEXT_KEY = "_lifetime_attempt_ordinal"
TASK_LAST_BANKED_ATTEMPT_ORDINAL_CONTEXT_KEY = "_last_banked_attempt_ordinal"


def _coerce_provider_context_values(raw: object) -> list[str]:
    values: list[str] = []
    if isinstance(raw, str) and raw.strip():
        values.append(raw.strip())
    elif isinstance(raw, (list, tuple, set)):
        values.extend(
            item.strip() for item in raw if isinstance(item, str) and item.strip()
        )
    return values


def check_model_kwargs(model_cls: type, kwargs: dict) -> None:
    """Raise TypeError if any key in *kwargs* is not a declared field of *model_cls*.

    SQLModel table=True models silently discard unknown constructor keywords,
    which lets stale tests reference removed columns without failing.  Call this
    helper in tests that construct model instances to surface that staleness
    immediately rather than at the next schema migration.

    Usage::
        check_model_kwargs(Project, {"name": "my-proj"})         # OK
        check_model_kwargs(Project, {"removed_column": "value"}) # TypeError
    """
    known = set(model_cls.model_fields.keys())
    unknown = set(kwargs.keys()) - known
    if unknown:
        raise TypeError(
            f"{model_cls.__name__}() received unknown field(s): "
            f"{sorted(unknown)}.  Known fields: {sorted(known)}."
        )


class ProjectStatus(str, Enum):
    DRAFT = "DRAFT"
    DEPLOYING = "DEPLOYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TaskStatus(str, Enum):
    QUEUED = "QUEUED"
    PROVISIONING = "PROVISIONING"
    IN_PROGRESS = "IN_PROGRESS"
    EVALUATING = "EVALUATING"
    # BLOCKED is wanted-but-not-runnable work: a precondition is absent (an
    # upstream dependency/rollup hasn't landed, a base branch or referenced file
    # doesn't exist yet, or the agent surfaced a ``BLOCKER:``). It is a durable
    # holding state — the task is parked until a recheck sweep re-evaluates it
    # at/after ``recheck_after`` (see Task.blocker_kind/blocked_reason). BLOCKED
    # is a first-class state distinct from FAILED — it does NOT count as a
    # code/infra failure and does NOT burn the retry budget. It is
    # terminal-for-now (the VM is released and the agent JWT revoked) but
    # recoverable: the auto-unblock sweep rechecks the precondition and returns
    # the task to QUEUED once its blocker clears, or escalates once a deadline
    # passes. Durable blocker metadata lives under ``Task.context['blocker']``
    # (see app.task_blocked); historical rows that terminated FAILED with the
    # BLOCKED_PRECONDITION failure class remain valid and are unaffected.
    # ``status`` is a plain VARCHAR column (not a Postgres ENUM type) and has no
    # CHECK constraint, so admitting this new value requires no DDL on the column.
    BLOCKED = "BLOCKED"
    # PARKED is non-terminal work that is *intentionally* waiting on an external,
    # non-agent signal (e.g. a ComputeJob reaching a terminal state). Unlike
    # BLOCKED — which is a precondition that is *absent* and rechecked on a timer
    # — a PARKED task has an explicit, durable wake registration
    # (``TaskWakeRegistration``) naming the source it waits on, the action to take
    # when that source fires (resume vs finalize), and a delivery/result state.
    # Entering PARKED releases the agent VM, JWT, and fleet slot immediately (the
    # supervising agent no longer pins a slot for the source's duration); a
    # leader-owned wake-delivery worker later advances the task exactly once when
    # the source fires, or the reaper ages it out at its deadline. It is
    # deliberately distinct from BLOCKED (precondition-recheck) and from a
    # childless EVALUATING parent (waiting on its own children) so dispatch,
    # digests, and operators can reason about *why* a task is idle. ``status`` is
    # a plain VARCHAR (no Postgres ENUM / CHECK constraint), so admitting this new
    # value requires no DDL on the column.
    PARKED = "PARKED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TASK_OUTCOME_TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }
)

# Statuses where the current agent/VM binding is no longer live and any issued
# agent JWT must be revoked. BLOCKED/PARKED are recoverable, but both release
# the agent slot immediately.
TASK_AGENT_RELEASE_STATUSES: frozenset[TaskStatus] = (
    TASK_OUTCOME_TERMINAL_STATUSES | {TaskStatus.BLOCKED, TaskStatus.PARKED}
)

# Agent status callbacks should be idempotent after true terminal outcomes and
# BLOCKED. PARKED has its own stale-callback response because it is non-terminal
# work owned by the wake-delivery worker.
TASK_AGENT_STATUS_IDEMPOTENT_STATUSES: frozenset[TaskStatus] = (
    TASK_OUTCOME_TERMINAL_STATUSES | {TaskStatus.BLOCKED}
)

TASK_PARKABLE_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
    }
)


def task_status_releases_agent_resources(status: TaskStatus | str | None) -> bool:
    """True when ``status`` releases the active agent/VM binding."""
    if status is None:
        return False
    return status in TASK_AGENT_RELEASE_STATUSES


# Durable task-origination classes (see ``Task.origination``). Distinct from
# ``Task.created_by``, which collapses research spawns, rollup spawns, review
# spawns and every other automatic spawn into the single 'ORCHESTRATOR' value —
# so the originating flow can only be recovered by walking parent lineage,
# which nothing does. These constants are stamped AT SPAWN TIME so merged-work
# analytics can attribute each landed PR to its origin without a lineage walk.
#
# EVERY class below is stamped by at least one live spawn site — a class with no
# stamping site is a lie in the denominator, because its work silently lands in
# SYSTEM while the taxonomy claims it is separately attributed. The stamping
# sites are:
#   USER       — operator-created task (admin.py task/retry/clone spawns,
#                sr_diagnostics_endpoints.py investigation spawn)
#   STRATEGIST — directly commissioned by the strategist (strategic_spawn_requests)
#   RESEARCH   — spawned by the autonomous research loop: the research parent
#                task itself (autonomous_research / direction_lifecycle) or a
#                finding/memo/salvage child of one
#   PLAN       — child subtask of a plan task (plan_completion; inherits
#                RESEARCH / STRATEGIST / USER when the plan parent is that class)
#   REVIEW     — review-pipeline child: fix, lane recovery, verify_tests
#                (review_pipeline)
#   HOTFIX     — hotfix backmerge child (hotfix_backmerge)
#   ROLLUP     — review/fix children of a rollup PR chain
#   QUALIFIER  — release-qualifier audit, challenge, walkthrough and grade sidecars
#   EVERGREEN  — utilization-triggered evergreen research probes
#   REMEDIATION— automated debug fixes, smoketest fixes and security rescans
#   SMOKETEST  — post-rollup smoketest tasks and their failure-fix plans
#   SYSTEM     — the default, and the honest residual for automation that has no
#                more specific class, plus every row that predates this column.
#                NEVER research- or strategist-attributed. Because SYSTEM is a
#                residual rather than a described class, treat it as such when
#                computing shares — see
#                ``research_completion.merged_work_origination_split``.
TASK_ORIGINATION_USER = "USER"
TASK_ORIGINATION_STRATEGIST = "STRATEGIST"
TASK_ORIGINATION_RESEARCH = "RESEARCH"
TASK_ORIGINATION_PLAN = "PLAN"
TASK_ORIGINATION_REVIEW = "REVIEW"
TASK_ORIGINATION_HOTFIX = "HOTFIX"
TASK_ORIGINATION_ROLLUP = "ROLLUP"
TASK_ORIGINATION_QUALIFIER = "QUALIFIER"
TASK_ORIGINATION_EVERGREEN = "EVERGREEN"
TASK_ORIGINATION_REMEDIATION = "REMEDIATION"
TASK_ORIGINATION_SMOKETEST = "SMOKETEST"
TASK_ORIGINATION_SYSTEM = "SYSTEM"
# Origination classes a child task inherits from its parent when the parent is
# that class (used by plan_completion subtask spawning). Everything else on a
# plan child falls back to PLAN.
_INHERITABLE_ORIGINATIONS = frozenset(
    {
        TASK_ORIGINATION_USER,
        TASK_ORIGINATION_STRATEGIST,
        TASK_ORIGINATION_RESEARCH,
    }
)


class WakeAction(str, Enum):
    """What the wake-delivery worker does with a PARKED task when its source fires.

    RESUME   — return the task to ``QUEUED`` so the orchestrator re-dispatches it
               on a fresh VM (the common case: the agent parked mid-flight and
               wants to continue once the external signal lands).
    FINALIZE — drive the task straight to a terminal state via the normal
               completion path (the source firing *is* the answer; no further
               agent work is needed).

    Stored as the enum *value* in a plain String column, mirroring ``TaskStatus``.
    """

    RESUME = "resume"
    FINALIZE = "finalize"


class WakeDeliveryState(str, Enum):
    """Lifecycle of a single :class:`TaskWakeRegistration` delivery.

    PENDING   — registered; the source has not fired yet. The task sits PARKED.
    FIRED     — the source fired (``fired_at`` set); the registration is now
                claimable by the leader-owned wake-delivery worker.
    DELIVERED — the worker claimed the registration and advanced the PARKED task
                exactly once (``delivered_at`` + ``result_state`` set).
    EXPIRED   — the deadline passed before the source fired; the reaper aged the
                registration (and its task) out rather than stranding it.
    FAILED    — the worker claimed a fired registration but could not advance the
                task (e.g. the task was no longer PARKED); recorded for operators.

    Stored as the enum *value* in a plain String column, mirroring ``TaskStatus``.
    """

    PENDING = "PENDING"
    FIRED = "FIRED"
    DELIVERED = "DELIVERED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


class ComputeJobStatus(str, Enum):
    QUEUED = "QUEUED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    POLLING = "POLLING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


class ComputeJobOutcome(str, Enum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    SPEND_CAP_EXCEEDED = "SPEND_CAP_EXCEEDED"
    SMOKE_FAILED = "SMOKE_FAILED"


class ArtifactType(str, Enum):
    GIT_COMMIT = "GIT_COMMIT"
    PULL_REQUEST = "PULL_REQUEST"
    SCREENSHOT = "SCREENSHOT"
    ERROR_LOG = "ERROR_LOG"
    RESEARCH_FINDINGS = "RESEARCH_FINDINGS"
    SECURITY_REPORT = "SECURITY_REPORT"
    SMOKETEST_RESULT = "SMOKETEST_RESULT"
    DEBUG_RESULT = "DEBUG_RESULT"
    PERF_REPORT = "PERF_REPORT"
    INVESTIGATION_REPORT = "INVESTIGATION_REPORT"
    SELF_AUDIT_REPORT = "SELF_AUDIT_REPORT"
    RELEASE_AUDIT_REPORT = "RELEASE_AUDIT_REPORT"
    # Binding second-opinion verdict from the release_audit_challenge handler:
    # an independent adversarial re-judgement of the primary release_audit's
    # higher-severity findings (confirmed / refuted_false_positive / uncertain).
    # Used as a fail-closed gate in _finalize_qualification — verified findings
    # must be challenger-confirmed to deduct; missing/uncertain verdicts HOLD.
    # Like MERGE_TEST_RESULT, the column is a plain String so no migration is
    # needed.
    RELEASE_AUDIT_CHALLENGE = "RELEASE_AUDIT_CHALLENGE"
    # Blind goal-coverage grade from the release_merge_grade handler: an
    # uninvested agent grades the merged DIFFS of a release version against the
    # frozen version.goal — BLIND to the strategist's running_update narrative —
    # and emits {covers_goal, coverage_score, biggest_gap}. Kept as diagnostic
    # artifact evidence; canonical VersionAcceptanceCriterion coverage now owns
    # the hard goal-coverage qualification gate. Like MERGE_TEST_RESULT the
    # artifact column is a plain String so no migration is needed for the
    # ArtifactType itself.
    RELEASE_MERGE_GRADE = "RELEASE_MERGE_GRADE"
    # Structured direction verdict emitted by the normal discovery_review task.
    # Stored in the plain String artifact column, so adding the value requires no
    # database enum migration.
    DISCOVERY_REVIEW = "DISCOVERY_REVIEW"
    MEMO = "MEMO"
    # Verdict from the verify_tests agent handler: the repo's detected test
    # suite run on a PR head, consulted by the no-CI merge gate. ArtifactType is
    # a plain String column (sa_column=Column(String)) so no migration is needed.
    MERGE_TEST_RESULT = "MERGE_TEST_RESULT"
    # Result of a compute_experiment task: the terminal outcome (success / spend
    # cap / deadline / failure) and final cost of a budgeted compute job the
    # agent launched and monitored. Like MERGE_TEST_RESULT, the column is a plain
    # String so no migration is needed.
    COMPUTE_RESULT = "COMPUTE_RESULT"
    # Result of a walkthrough_e2e task: per-flow verdicts (PASS/FAIL/DEAD_END)
    # from the live-stack harness. Read by the completion handler to write
    # CriterionEvidence rows. ArtifactType is a plain String column so no
    # migration is needed.
    WALKTHROUGH_E2E_RESULT = "WALKTHROUGH_E2E_RESULT"
    # Result of the AUTONOMOUS walkthrough_e2e evidence lane: the machine-readable
    # {git_sha, run_timestamp, results:[{criterion_key, passed, notes}]} report the
    # live-stack handler captured. Unlike WALKTHROUGH_E2E_RESULT this artifact is
    # NOT re-ingested by the completion handler — the handler POSTs SHA-pinned
    # CriterionEvidence directly to the ingestion endpoint. Plain String column so
    # no migration is needed.
    WALKTHROUGH_E2E_REPORT = "WALKTHROUGH_E2E_REPORT"


class SourceKind(str, Enum):
    """Which task type produced a :class:`KnowledgeDocument`.

    A knowledge document is written by a codebase-internal ``design`` task, a
    web-grounded ``web_research`` task, or a ``compute_experiment`` task whose
    measured verdict (outcome / metric / criterion-met / cost) is persisted so
    future research and strategist cycles can discover what was already measured.
    ``UNKNOWN`` is the durable fallback so pre-existing rows (and any future
    producer that does not stamp a kind) backfill cleanly. Stored as the enum
    *value* in a plain String column, mirroring ``ArtifactType`` / ``TaskStatus``.
    """

    DESIGN = "design"
    WEB_RESEARCH = "web_research"
    COMPUTE_EXPERIMENT = "compute_experiment"
    RESEARCH_AUDIT = "research_audit"
    REPO_DOMAIN = "repo_domain"
    DIRECTION_RETIREMENT = "direction_retirement"
    # A single durable behavioral-memory fact written by the ``set_memory``
    # strategic op. Each fact is one individually-addressable, trigram-searchable
    # ``KnowledgeDocument`` row (title/body/summary/topic/tags), replacing the
    # capped ``Project.strategic_memory_notes`` JSONB list — giving strategic
    # memory unbounded capacity, keyword retrieval, and per-fact lifecycle.
    STRATEGIC_MEMORY = "strategic_memory"
    UNKNOWN = "unknown"


class LLMProviderName(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GEMINI = "gemini"
    ZAI = "zai"
    DEEPSEEK = "deepseek"


class LLMAuthMode(str, Enum):
    OAUTH = "oauth"
    API_KEY = "api_key"
    SERVICE_ACCOUNT = "service_account"


class PoolVmState(str, Enum):
    """Lease state of a pre-provisioned warm-pool agent VM.

    PROVISIONING — the VM is booting (taskless) and has not yet reported ready.
    IDLE         — booted, credentialed, and available to be leased.
    LEASED       — currently claimed by a task (``leased_task_id`` set until
                   ``lease_expires_at``).
    RETIRING     — scheduled for teardown; never lease in this state.
    """

    PROVISIONING = "PROVISIONING"
    IDLE = "IDLE"
    LEASED = "LEASED"
    RETIRING = "RETIRING"


class MigrationReservationStatus(str, Enum):
    """Lifecycle of a provisional Alembic revision reservation.

    ACTIVE    — the reservation holds a provisional revision id for an
                in-flight migration-producing task; it participates in
                next-number allocation so concurrent tasks get distinct ids.
    FINALIZED — the migration merged; ``finalized_revision`` records the id the
                migration actually shipped under (which may differ from the
                provisional id if a merge-time renumber moved it).
    EXPIRED   — the reserving task was reaped/abandoned without shipping; the
                reservation no longer participates in allocation and its
                provisional id is free to be re-handed-out.

    Stored as the enum *value* in a plain String column, mirroring
    ``ComputeJobStatus`` / ``TaskStatus``.
    """

    ACTIVE = "ACTIVE"
    FINALIZED = "FINALIZED"
    EXPIRED = "EXPIRED"


# --- Auto-pause diagnosis workflow constants ---
# Shared values for the direction auto-pause -> investigate task spawn flow.
# Kept here so direction_lifecycle, anomaly_detection, task_completion, and
# strategic_completion all agree on the same thresholds and key names.

# Direction dict keys for diagnosis state. The `directions` JSONB field on
# Project stores these alongside existing pause keys.
#   DIAGNOSIS_IN_PROGRESS_KEY: bool — true while an investigate task is open
#   DIAGNOSIS_TASK_ID_KEY:     str  — UUID of the spawned investigate task
#   DIAGNOSIS_STARTED_AT_KEY:  str  — ISO datetime of spawn (for dedup window)
DIAGNOSIS_IN_PROGRESS_KEY = "diagnosis_in_progress"
DIAGNOSIS_TASK_ID_KEY = "diagnosis_task_id"
DIAGNOSIS_STARTED_AT_KEY = "diagnosis_started_at"

# Direction recommended_mode provenance metadata (stored in Project.directions JSONB).
RECOMMENDED_MODE_META_KEY = "recommended_mode_meta"
RECOMMENDED_MODE_SOURCE_STRATEGIC = "strategic"
RECOMMENDED_MODE_SOURCE_SELF_AUDIT = "self_audit"


# --- Models ---


class AllowedUser(SQLModel, table=True):
    """
    Allowlist of users permitted to access the Blueprint application.
    Roles are managed here instead of Firebase custom claims.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    email: str = Field(index=True, unique=True)
    firebase_uid: str | None = Field(default=None, index=True)
    # Allowed values: 'admin' | 'user' | 'invited'. ``invited`` is internal
    # pre-login provenance and is promoted to ``user`` on first verified claim.
    # Enforced by ``chk_alloweduser_role`` (alembic 0067, expanded by 0721).
    role: str = Field(default="user")
    display_name: str | None = None
    # TIMESTAMPTZ + server_default (migration 0061 / audit H-S4 + H-S5):
    # default_factory is retained as an additive Python-side guard so ORM
    # callers don't see ``None`` between construction and commit; the
    # server_default protects raw-SQL INSERT paths that bypass the ORM.
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    last_login_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )


class UserIntegration(SQLModel, table=True):
    """
    Stores third-party integrations for a user, such as GitHub App installations.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # ``user_id`` previously had ``index=True`` (yielding the unique index
    # ``ix_userintegration_user_id`` from migration 0001) — superseded by the
    # named ``uq_userintegration_user_id`` UNIQUE constraint added in 0049 and
    # dropped in 0060 (audit 2026-04-29 / H-S3). The constraint's underlying
    # index is the canonical lookup path; do not re-add ``index=True`` here.
    user_id: str = Field()

    # GitHub App Installation ID
    github_installation_id: str | None = None
    github_account_name: str | None = None

    # GitHub Fine-Grained Personal Access Token (Alternative to App Installation)
    github_pat: str | None = None

    # Caller-scoped OAuth token, stored as an encrypted Secret Manager reference.
    # Unlike an App installation token, this can prove what the linked GitHub
    # user is allowed to do to a repository during self-service onboarding.
    github_oauth_token: str | None = None

    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, onupdate=_utcnow),
    )

    __table_args__ = (UniqueConstraint("user_id", name="uq_userintegration_user_id"),)


class Project(SQLModel, table=True):
    """Repository-scoped container for tasks, strategic state, and build config.

    The onboarding flow seeds ``build_config`` with detected defaults and a
    per-repo task-branch namespace. Subsequent admin PATCHes and re-detects are
    expected to preserve operator-owned overrides such as branch naming,
    cleanup prefixes, PR-title prefixes, and self-improvement flags.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str
    # Firebase UID of the project owner. Intentionally NOT a FK to
    # ``alloweduser.firebase_uid`` (audit 2026-04-29 / L-S2): a Project may
    # be created via an admin token whose Firebase UID has not yet been
    # populated on the matching ``alloweduser`` row (firebase_uid is filled
    # on first login, but project creation can precede first login during
    # bootstrap / onboarding). A FK here would force a hard ordering
    # between allowlist seeding and project creation that the bootstrap
    # flow doesn't otherwise need. Loose-coupled by design.
    user_id: str = Field(index=True)
    # DEPRECATED: legacy finite-project lifecycle status (DRAFT/DEPLOYING/
    # COMPLETED/FAILED/CANCELLED). Autonomous, long-lived projects are created
    # DEPLOYING and never move, so this value is not a meaningful operator
    # signal — use ``paused``/``archived`` instead. The all-terminal aggregation
    # that used to auto-flip this to COMPLETED/FAILED has been removed; only the
    # explicit /cancel path still writes CANCELLED (and dispatch/spawn gating
    # reads it). The column is retained (and still returned in API responses)
    # for backward compatibility; do not surface it as project state in new UI.
    status: ProjectStatus = Field(
        default=ProjectStatus.DRAFT,
        sa_column=Column(String, default=ProjectStatus.DRAFT),
    )
    github_owner: str | None = None
    github_repo: str | None = None
    # TIMESTAMPTZ + server_default (migration 0061 / audit H-S4 + H-S5).
    # NaiveUTCDateTime strips tz on read (round 2 audit / C3 + H1).
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )

    # Autonomous improvement: user-defined high-level directions for research agents
    # Includes directions with status: 'active', 'paused', 'proposed', 'rejected', 'expired'
    # Auto-pause diagnosis keys (set when an investigate task is spawned to
    # diagnose repeated code failures — see DIAGNOSIS_* constants above):
    #   diagnosis_in_progress: bool, diagnosis_task_id: str, diagnosis_started_at: ISO str
    directions: List[Dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSONB, default=[])
    )
    # Strategic product vision maintained by the strategic research agent.
    # Legacy: being replaced by ``problem_model`` (see below). During migration
    # we render vision from the problem model; once all consumers migrate, this
    # column becomes a cache or is removed.
    vision: Dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=True, default={})
    )
    # Structured problem model — the strategic layer's durable understanding.
    # Replaces vision + design_doc/notes/strategic-context as the source of
    # truth for strategic state. Shape defined in app.problem_model.ProblemModel.
    problem_model: Dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=True, default={})
    )
    # Self-reflection log — append-only list of the strategic agent's
    # own observations about what was hard / what context it was missing
    # each cycle. Used on self-repo projects to drive the self-improvement
    # loop. Empty list on non-self-repo projects. Entry shape defined in
    # app.problem_model.SelfReflectionEntry; cap SELF_REFLECTION_LOG_MAX.
    self_reflection_log: List[Dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=True, default=[]),
    )
    # Strategic memory notes — append-only list of durable behavioral lessons
    # and operator preferences. Written by the set_memory strategic op so the
    # resident strategist can persist corrections / preferences across sessions.
    # Each entry is {"note": <string>, "set_at": <iso_timestamp>}.
    strategic_memory_notes: List[Dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=True, default=[]),
    )
    # Running update — a single markdown document the strategic agent
    # maintains as a living narrative of what the project is and what's
    # shifting. Rewritten each cycle (not appended) so the user always
    # sees a clean current view, not a log of per-cycle summaries. User
    # acknowledges to stamp running_update_acknowledged_at; the document
    # persists and the agent refines it on the next cycle.
    running_update: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )
    running_update_acknowledged_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    # Living architectural description maintained by design doc update agent
    design_doc: Dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=True, default={})
    )
    # Toggle autonomous research/task generation
    autonomous_enabled: bool = Field(default=True)
    # Operator pause switch. Distinct from ``autonomous_enabled`` (which only
    # gates NEW autonomous spawning) and from terminal ``status`` (DRAFT →
    # COMPLETED/FAILED/CANCELLED): while ``paused`` is True the orchestrator
    # must not select or provision this project's QUEUED work on any dispatch
    # path, and all autonomous spawn entry points skip it. In-flight
    # PROVISIONING/IN_PROGRESS tasks are allowed to drain. Resuming
    # (paused=False) restores normal dispatch with no re-configuration.
    paused: bool = Field(default=False)
    # Self-repo flag: enables rollback monitoring, extended windows, infra soft gates
    # Set True when github_owner/github_repo matches Blueprint's own repo
    is_self_repo: bool = Field(default=False)
    # Archived flag: durable, reversible "set aside" state so a project can be
    # hidden without deletion. Server default false backfills existing rows.
    archived: bool = Field(default=False, index=True)
    # Build configuration: onboarding seeds repo-derived defaults here, and
    # admin PATCH / re-detect preserves operator-owned branch/cleanup overrides.
    build_config: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    # Project-level CI auto-remediation outcomes for fixes that could not be
    # linked to a direction (novel failures whose fingerprint matched no
    # direction). Shape: {"attempts": int, "last_result": str, "last_at": iso,
    # "history": [{task_id, result, timestamp, fingerprint, failure_class}]}.
    remediation_tracking: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    # Runtime-backed readiness of the no-CI merge gate. Unlike the read-only
    # ``compute_merge_verification_verdict`` advisory (derived purely from stored
    # ``build_config``), this records what the gate ACTUALLY did the last time it
    # ran on a PR — so an operator can tell a config that *claims* tests are gated
    # from one where the live no-CI path fell open to an UNVERIFIED merge.
    # Written best-effort by ``ci_resolution.record_no_ci_gate_runtime`` at every
    # terminal gate outcome. Shape:
    #   {"last_run_at": iso, "executed_real_tests": bool,
    #    "last_unverified_at": iso | absent,
    #    "last_unverified_reason": str | absent,
    #    # additive rolling history (bounded at 20, oldest dropped):
    #    "recent_outcomes": [{"at": iso, "verified": bool,
    #                         "reason": key | None}, ...] | absent,
    #    # per-reason fall-open tally (incremented only on fall-opens):
    #    "unverified_reason_counts": {reason_key: count} | absent}
    # The history keys are purely additive — the column stays schemaless JSONB,
    # so no migration and no type change is needed; legacy four-key-only rows
    # upgrade transparently on the next gate run.
    # Nullable/additive: existing rows and create_all test DBs read it as None,
    # and a fresh migration (separate task) adds the column to live DBs.
    merge_gate_runtime: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    # Per-category self-review catch-rate targets set by the operator.
    # Shape: {category_id: float} e.g. {'security': 0.80, 'regression_risk': 0.75}.
    # Absent key = no target configured for that category (default: no alerting).
    sr_catch_rate_targets: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    # Approval gate configuration for PR file-path flagging.
    # Shape: {"infra_files": [...], "critical_files": [...]}.
    # When unset/empty, the file-based gate is disabled and reads as {}.
    approval_gate_config: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    # Per-project trunk branch for autonomous task spawning.
    # NULL means "fall back to 'main'".
    # Blueprint's own repo uses 'automain'; most other projects use 'main'.
    # Admin onboarding does not seed this column; it is set separately and
    # preserved across project config edits.
    integration_branch: str | None = Field(
        default=None,
        sa_column=Column(String, nullable=True),
    )
    # Durable evidence-grounded capability proof records.
    # Shape: {normalized_cap_name: [{task_id, proved_at, proof_kind,
    #   proof_ref, commit_sha}]} (capped at 10 entries per capability).
    # Written by capability_proof.record_capability_proof when a task that
    # advertises provides_capability completes with verified acceptance evidence
    # (named_test proven=True or downstream_artifact consumed=True).
    # NULL / absent key = capability is claimed but never proven.
    capability_proof_records: Dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=True, default={})
    )
    # Durable adopt-or-reject verdicts for capability trials.
    # Shape: {normalized_cap_name: {verdict, lift, n_with, n_without, window_hours,
    # evaluated_at, thresholds}} — newest verdict supersedes the prior one.
    # Written by capability_trial_verdict.record_trial_verdict during digest
    # computation when a trial has accumulated enough measurement data.
    # NULL / absent key = trial still running or not yet measured.
    capability_adoption_verdicts: Dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=True, default={})
    )
    # Per-project promotion (LTS) branch. NULL means fall back to 'main' —
    # identical semantics to integration_branch.
    # Like integration_branch, this is operator-managed and preserved once set.
    promotion_target: str | None = Field(
        default=None,
        sa_column=Column(String, nullable=True),
    )
    # Per-automain-HEAD CI-compliance status (module-size guardrails), keyed by
    # commit SHA. Written by ci_compliance.record_ci_compliance after
    # evaluate_ci_compliance_at_ref reads the capped modules at the HEAD ref via
    # the GitHub file-read-at-SHA path; read via get_ci_compliance and surfaced
    # in the strategist payload so guardrail violations are discovered
    # CONTINUOUSLY (per merge) rather than only when qualification CI fails.
    # Shape: {sha: {ref, compliant: bool, violations: [{section, rel, count, cap}]}}.
    # NULL = never evaluated.
    ci_compliance: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )

    @property
    def enable_self_improvement(self) -> bool:
        """True when build_config explicitly enables self-improvement mode."""
        return bool((self.build_config or {}).get("enable_self_improvement", False))

    @property
    def task_branch_namespace(self) -> str:
        """Namespace prefix for agent task branches (``{namespace}/{prefix}-{8hex}``).

        Reads from ``build_config.task_branch_namespace`` if set; otherwise
        returns Blueprint's default (``"blueprint"``). Onboarding seeds this
        value from the repo name, and later PATCH/re-detect flows preserve it
        unless an operator explicitly changes it. A non-Blueprint install can
        ship its own namespace (e.g. ``"autoproj"``) by setting this in
        build_config, and the canonical resolver lives in
        ``shared_state.get_project_task_branch_namespace``.
        """
        configured = (self.build_config or {}).get("task_branch_namespace")
        if configured:
            return str(configured)
        return "blueprint"

    @property
    def staging_branch_prefixes(self) -> tuple[str, ...]:
        """Ordered staging branch prefixes for research/plan rollups.

        Reads from ``build_config.staging_branch_prefixes`` if set; otherwise
        returns Blueprint's conventional pair (``research/``, ``plan/``).
        Admin re-detect preserves this list when already configured.
        """
        configured = (self.build_config or {}).get("staging_branch_prefixes")
        if configured is not None:
            prefixes = tuple(str(prefix) for prefix in configured if prefix)
            if prefixes:
                return prefixes
        return ("research/", "plan/")

    def is_staging_branch(self, branch: str | None) -> bool:
        """True when ``branch`` is one of this project's intermediate staging
        branches (research/plan rollup targets).

        Routes all "is this an intermediate staging branch?" decisions through
        the per-project ``staging_branch_prefixes`` so non-Blueprint installs
        with custom prefixes are classified correctly. Guards against
        ``None``/empty branch names.
        """
        if not branch:
            return False
        return branch.startswith(tuple(self.staging_branch_prefixes))

    @property
    def research_branch_prefix(self) -> str:
        """Prefix for research staging branches."""
        return self.staging_branch_prefixes[0]

    @property
    def plan_branch_prefix(self) -> str:
        """Prefix for plan staging branches."""
        return (
            self.staging_branch_prefixes[1]
            if len(self.staging_branch_prefixes) > 1
            else "plan/"
        )

    @property
    def stale_branch_prefixes(self) -> tuple[str, ...]:
        """Branch name prefixes eligible for stale cleanup.

        Reads from build_config.stale_branch_prefixes if set; otherwise returns
        the project's task-branch namespace alongside the project's staging
        branch prefixes. An empty list in build_config disables cleanup for this
        project, and admin PATCH/re-detect preserve the explicit empty-list
        opt-out.
        """
        configured = (self.build_config or {}).get("stale_branch_prefixes")
        if configured is not None:
            return tuple(configured)
        return (f"{self.task_branch_namespace}/", *self.staging_branch_prefixes)

    @property
    def protected_branches(self) -> frozenset[str]:
        """Branch names that must never be deleted by stale-branch cleanup.

        Reads from build_config.protected_branches if set; otherwise returns
        the default set (main, master, develop, dev). Always includes the
        project's integration_branch and promotion_target so misconfiguration
        cannot delete trunk branches. Admin re-detect preserves any operator
        override already stored in build_config.
        """
        from app.services import _DEFAULT_PROTECTED_BRANCHES
        from app.shared_state import (
            get_project_integration_branch,
            get_project_promotion_target,
        )

        configured = (self.build_config or {}).get("protected_branches")
        if configured is not None:
            base: frozenset[str] = frozenset(configured)
        else:
            base = frozenset(_DEFAULT_PROTECTED_BRANCHES)
        return base | {
            get_project_integration_branch(self),
            get_project_promotion_target(self),
        }

    @property
    def task_branch_prefix(self) -> str:
        """Namespace prefix for task branch names (e.g. 'blueprint/').

        By default this tracks ``task_branch_namespace`` so a project that
        overrides only the namespace (e.g. ``'autoproj'``) gets the matching
        prefix (``'autoproj/'``) automatically — keeping cleanup loops in
        sync with the project's actual branch naming. Onboarding seeds the
        namespace; later config saves preserve it unless the operator changes
        the value explicitly.

        Precedence: an explicit ``build_config.task_branch_prefix`` wins
        (legacy escape hatch so any project pinned to the older key keeps
        working byte-for-byte); otherwise the prefix is derived as
        ``f"{task_branch_namespace}/"``.
        """
        explicit = (self.build_config or {}).get("task_branch_prefix")
        if explicit:
            return str(explicit)
        return f"{self.task_branch_namespace}/"

    @property
    def alembic_versions_path(self) -> str:
        """Directory prefix for Alembic migration files in this project.

        Reads from build_config.alembic_versions_path if set; otherwise returns
        Blueprint's conventional path ('backend/alembic/versions/').
        """
        from app.migration_path import _DEFAULT_ALEMBIC_VERSIONS_PATH

        return (self.build_config or {}).get(
            "alembic_versions_path", _DEFAULT_ALEMBIC_VERSIONS_PATH
        )

    def append_routine_digest(self, event_type: str, summary: str) -> None:
        """Append a one-line digest entry for a routine event to running_update.

        Canonical home for this logic — callers (notification_dispatch, admin_notifications,
        strategic_completion) mutate running_update through this method so the format stays
        consistent. Does NOT commit — caller is responsible for session.add(project).

        The entry is ALWAYS a single physical line. Routine-event summaries come
        from free-text message content and may contain newlines; any line-breaking
        whitespace is collapsed to single spaces BEFORE truncation so the emitted
        line matches the canonical single-line digest shape consumed by
        ``running_update_merge._DIGEST_LINE_RE``. If a multi-line summary spanned
        several physical lines, the reconciliation classifier would misread the
        append as an operator body edit and silently discard the agent's refreshed
        running_update.
        """
        now_str = _utcnow().strftime("%Y-%m-%d %H:%M")
        # Collapse every newline form (\r\n, \r, \n) and any resulting runs of
        # whitespace into single spaces so the digest entry is one physical line.
        flattened = " ".join((summary or "").split())
        line = f"- [{now_str}] **{event_type}**: {flattened[:200]}"
        current = (self.running_update or "").rstrip()
        self.running_update = f"{current}\n{line}" if current else line

    __table_args__ = (
        Index("ix_project_github_owner_repo", "github_owner", "github_repo"),
    )

    @classmethod
    def not_archived(cls):
        """Canonical "project is not archived" filter expression.

        Archiving halts ALL of a project's work, so every project-selection
        query that drives or surfaces work (orchestrator candidate dispatch,
        autonomous schedulers, strategic-starvation and pipeline alert scans)
        filters on this. Centralized here so the exclusion clause cannot drift
        independently across call sites — production code and tests reference
        the same predicate. Usage: ``select(...).where(Project.not_archived())``.
        """
        return cls.archived.is_(False)

    tasks: List["Task"] = Relationship(
        back_populates="project",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    compute_jobs: List["ComputeJob"] = Relationship(
        back_populates="project",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class ProjectMember(SQLModel, table=True):
    """
    Many-to-many membership linking users to projects with roles.
    Roles: 'owner' (full control + invites), 'member' (tasks + directions), 'viewer' (read-only).
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)
    user_id: str = Field(index=True)
    # Allowed values: 'owner' | 'member' | 'viewer'. Enforced server-side by
    # ``chk_projectmember_role`` (alembic 0065 / audit L-S1, Postgres-only).
    role: str = Field(default="member")
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )

    # Existing production index from migration 0024. Declaring it here keeps
    # SQLModel-created test schemas aligned with Alembic and makes invitation
    # idempotency rely on the same canonical uniqueness in every dialect.
    __table_args__ = (
        Index(
            "ix_projectmember_project_user",
            "project_id",
            "user_id",
            unique=True,
        ),
    )


class MessagesLastSeen(SQLModel, table=True):
    """Per-operator, per-project last-seen timestamp for the messages panel divider.

    Enables cross-device persistence of the since-last-visit divider position.
    localStorage remains the offline fallback; this table is the authoritative
    source when the backend is reachable.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: str = Field(index=True, max_length=128)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)
    last_seen_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, onupdate=_utcnow),
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id", "project_id", name="uq_messageslastseen_user_project"
        ),
    )


class MessagesNoisePrefs(SQLModel, table=True):
    """Per-operator, per-project messages-panel noise-control preferences.

    Persists the operator's per-event-type visibility choices and saved filter
    views server-side so they follow the operator across devices. localStorage
    remains the offline fallback; this table is the authoritative source when
    the backend is reachable.

    prefs shape (stored as JSONB, validated by the admin_notifications router):
      {
        "event_type_visibility": {<event_type>: "show"|"mute"|"routine"},
        "saved_filter_views": [{"name": str, "filters": {...}}, ...]
      }
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: str = Field(index=True, max_length=128)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)
    prefs: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, onupdate=_utcnow),
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id", "project_id", name="uq_messagesnoiseprefs_user_project"
        ),
    )


class MessagesSavedViews(SQLModel, table=True):
    """Per-operator, per-project saved views for the messages panel.

    Persists named filter/sort combinations (saved views) server-side so they
    follow the operator across devices. localStorage remains the offline fallback;
    this table is the authoritative source when the backend is reachable.

    saved_views shape (stored as JSONB, validated by the admin_notifications router):
      [{"id": str, "name": str, "query": str, "prefs": {...}, "sort": str,
        "showResolved": bool, "routineExpanded": bool, "showOnlyNew": bool,
        "createdAt": number}, ...]
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: str = Field(index=True, max_length=128)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)
    saved_views: List[Dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'")),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, onupdate=_utcnow),
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id", "project_id", name="uq_messagessavedviews_user_project"
        ),
    )


class ProjectTool(SQLModel, table=True):
    """A per-project custom UI tool/panel registered against a single project.

    Backs the per-project custom-tools framework: a project registers entries
    (e.g. a whiteboard/canvas panel) that the admin-ui conditionally renders in
    that project's workspace only. Tools never leak across projects — every
    router handler resolves the row through ``_get_user_project_with_role`` on
    ``project_id`` first.

    ``config`` holds tool-kind-specific settings (validated against the kind's
    ``config_schema`` in ``TOOL_KINDS``). ``content`` holds the tool's mutable
    document (e.g. whiteboard strokes); it is written through an optimistic-lock
    path that bumps ``content_revision`` and rejects a stale ``base_revision``
    with HTTP 409, mirroring the design-doc revision guard.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
    )
    kind: str
    slug: str
    title: str
    enabled: bool = Field(default=True)
    placement: str = Field(default="project_section")
    config: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    content: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    content_revision: int = Field(default=0)
    created_by: str | None = None
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            NaiveUTCDateTime,
            nullable=False,
            server_default=func.now(),
            onupdate=_utcnow,
        ),
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id", "slug", name="uq_project_tool_project_slug"
        ),
    )


class ArtifactRef(SQLModel, table=True):
    """Immutable, project-scoped snapshot of a human-authored tool revision."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(), ForeignKey("project.id", ondelete="CASCADE"), nullable=False
        )
    )
    tool_id: uuid.UUID = Field(
        sa_column=Column(GUID(), nullable=False)
    )
    kind: str = Field(sa_column=Column(String(32), nullable=False))
    revision: int = Field(sa_column=Column(Integer, nullable=False))
    checksum: str = Field(sa_column=Column(String(64), nullable=False))
    author_user_id: str | None = Field(
        default=None, sa_column=Column(String(255), nullable=True)
    )
    content_snapshot: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    retrieval_metadata: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )

    __table_args__ = (
        CheckConstraint("kind IN ('notes','whiteboard')", name="chk_artifactref_kind"),
        CheckConstraint("revision >= 0", name="chk_artifactref_revision_nonnegative"),
        CheckConstraint("length(checksum) = 64", name="chk_artifactref_checksum_length"),
        UniqueConstraint(
            "project_id", "tool_id", "revision", name="uq_artifactref_project_tool_revision"
        ),
        Index("ix_artifactref_project_created", "project_id", "created_at"),
        Index("ix_artifactref_tool_revision", "tool_id", "revision"),
    )


class ArtifactRefCleanupIntent(SQLModel, table=True):
    """Durable, retryable cleanup for retained project-scoped render objects."""

    project_id: uuid.UUID = Field(primary_key=True)
    object_prefix: str = Field(sa_column=Column(Text, nullable=False))
    state: str = Field(
        default="pending",
        sa_column=Column(String(24), nullable=False, server_default="pending"),
    )
    attempt_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )
    next_attempt_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    lease_expires_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    claim_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(GUID(), nullable=True),
    )
    last_error: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    completed_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

    __table_args__ = (
        CheckConstraint(
            "state IN ('pending','processing','complete')",
            name="chk_artifactrefcleanupintent_state",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="chk_artifactrefcleanupintent_attempt_nonnegative",
        ),
        Index(
            "ix_artifactrefcleanupintent_due",
            "state",
            "next_attempt_at",
        ),
    )


class ArtifactRefPromotionIntent(SQLModel, table=True):
    """No-FK write-ahead record for one rendered ArtifactRef copy effect."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(index=False)
    tool_id: uuid.UUID = Field(index=False)
    revision: int = Field(sa_column=Column(Integer, nullable=False))
    receipt_id: uuid.UUID = Field(index=False)
    actor_user_id: str = Field(sa_column=Column(String(255), nullable=False))
    source_storage_uri: str = Field(sa_column=Column(Text, nullable=False))
    candidate_storage_uri: str = Field(sa_column=Column(Text, nullable=False))
    expected_size_bytes: int = Field(sa_column=Column(BigInteger, nullable=False))
    expected_sha256: str = Field(sa_column=Column(String(64), nullable=False))
    render_width: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    render_height: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    state: str = Field(default="pending", sa_column=Column(String(24), nullable=False, server_default="pending"))
    attempt_count: int = Field(default=0, sa_column=Column(Integer, nullable=False, server_default="0"))
    claim_id: uuid.UUID | None = Field(default=None, sa_column=Column(GUID(), nullable=True))
    lease_expires_at: datetime | None = Field(default=None, sa_column=Column(NaiveUTCDateTime, nullable=True))
    effect_deadline_at: datetime | None = Field(default=None, sa_column=Column(NaiveUTCDateTime, nullable=True))
    settle_until: datetime | None = Field(default=None, sa_column=Column(NaiveUTCDateTime, nullable=True))
    next_attempt_at: datetime = Field(default_factory=_utcnow, sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()))
    destination_generation: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    settlement_absent_count: int = Field(default=0, sa_column=Column(Integer, nullable=False, server_default="0"))
    artifact_ref_id: uuid.UUID | None = Field(default=None, sa_column=Column(GUID(), nullable=True))
    last_error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    created_at: datetime = Field(default_factory=_utcnow, sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()))
    updated_at: datetime = Field(default_factory=_utcnow, sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()))
    completed_at: datetime | None = Field(default=None, sa_column=Column(NaiveUTCDateTime, nullable=True))

    __table_args__ = (
        CheckConstraint("state IN ('pending','copying','promoted','owned','cleanup','cleanup_claimed','settled')", name="chk_artifactrefpromotionintent_state"),
        CheckConstraint("revision >= 0", name="chk_artifactrefpromotionintent_revision"),
        CheckConstraint("expected_size_bytes > 0", name="chk_artifactrefpromotionintent_size"),
        CheckConstraint("length(expected_sha256) = 64", name="chk_artifactrefpromotionintent_checksum"),
        CheckConstraint("attempt_count >= 0 AND settlement_absent_count >= 0", name="chk_artifactrefpromotionintent_counts"),
        CheckConstraint(
            "state != 'owned' OR (artifact_ref_id IS NOT NULL AND completed_at IS NOT NULL AND destination_generation IS NOT NULL)",
            name="chk_artifactrefpromotionintent_owned",
        ),
        CheckConstraint(
            "state NOT IN ('promoted','owned') OR destination_generation IS NOT NULL",
            name="chk_artifactrefpromotionintent_generation",
        ),
        CheckConstraint(
            "state NOT IN ('copying','promoted','cleanup_claimed') OR (claim_id IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="chk_artifactrefpromotionintent_claim",
        ),
        CheckConstraint(
            "state != 'copying' OR (effect_deadline_at IS NOT NULL AND settle_until IS NOT NULL AND settle_until >= effect_deadline_at)",
            name="chk_artifactrefpromotionintent_effect_window",
        ),
        CheckConstraint(
            "state != 'settled' OR completed_at IS NOT NULL",
            name="chk_artifactrefpromotionintent_settled",
        ),
        UniqueConstraint("receipt_id", name="uq_artifactrefpromotionintent_receipt"),
        Index("ix_artifactrefpromotionintent_due", "state", "next_attempt_at"),
        Index("ix_artifactrefpromotionintent_project", "project_id", "state"),
        Index("ix_artifactrefpromotionintent_logical_ref", "project_id", "tool_id", "revision"),
    )


class Task(SQLModel, table=True):
    """
    Extensible Task model. The 'payloads' JSONB column holds a flexible list
    of structured dictionaries defining the work (e.g., UI components, API routes).
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)

    # Multi-Agent & Hierarchy Fields
    title: str = Field(default="Untitled Task")
    description: str = Field(default="")
    parent_task_id: uuid.UUID | None = Field(
        default=None, foreign_key="task.id", index=True
    )
    discovery_cycle_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(),
            ForeignKey(
                "discoverycycle.id",
                name="fk_task_discovery_cycle_id",
                ondelete="SET NULL",
                use_alter=True,
            ),
            nullable=True,
            index=True,
        ),
    )
    latest_status_message: str | None = None
    status_log: List[Dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'")),
    )

    # Task provenance. Allowed canonical values:
    #   'USER' | 'ORCHESTRATOR' | 'STRATEGIC_AGENT' |
    #   'SYSTEM_AUTO_REMEDIATION' | 'SYSTEM_AUTO_REVIEW' |
    #   'SYSTEM_ISSUE_IMPLEMENTATION'
    # Test fixtures additionally use 'TEST' / 'test-user' / 'user' / 'user1'
    # — these are accepted by the CHECK constraint but should not be
    # introduced in production code paths. Enforced server-side by
    # ``chk_task_created_by`` (alembic 0065 / audit L-S1, Postgres-only).
    created_by: str = Field(default="USER", index=True)
    # Durable origination class, distinct from created_by. created_by collapses
    # every automatic spawn into 'ORCHESTRATOR', so the originating flow is
    # otherwise unrecoverable without walking parent lineage. Stamped at spawn
    # time with the TASK_ORIGINATION_* constants; unstamped rows read SYSTEM
    # (ordinary orchestration). Backed by migration 0735.
    origination: str = Field(default="SYSTEM")
    launched_by_user_id: str | None = Field(
        default=None
    )  # Firebase UID of the launcher for attribution; project owner funds execution.
    idempotency_key: str | None = Field(
        default=None, sa_column=Column(String(255), nullable=True)
    )
    requires_approval: bool = Field(default=False)
    spend_approval_cleared_at: datetime | None = Field(default=None, nullable=True)

    status: TaskStatus = Field(
        default=TaskStatus.QUEUED, sa_column=Column(String, default=TaskStatus.QUEUED)
    )

    # Push Model: Orchestrator assigns this task to a specific VM
    vm_instance_id: str | None = Field(default=None, index=True)

    # Task dependency DAG: list of task UUIDs that must be COMPLETED+merged before dispatch
    depends_on: List[str] = Field(
        default_factory=list, sa_column=Column(JSONB, default=[])
    )

    # Flexible Payloads: A list of structured dictionaries defining the work.
    # E.g., [{"interface": "ui_component_v1", "data": {...}}]
    payloads: List[Dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'")),
    )

    # Shared memory/context for the agent during execution
    context: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )

    # Promoted from Task.context JSONB (D2 over-fetch fix, dir d-57436105).
    # The orchestrator Max-Plan reconcile loop runs every cycle (~30s) and
    # previously issued ``select(Task.context)`` over all PROVISIONING/IN_PROGRESS
    # tasks purely to read this one scalar (``ctx['llm_account_id']`` /
    # legacy ``ctx['max_plan_account_id']``), TOAST-fetching the entire
    # multi-MB context blob of every active task each cycle. Promoting it to a
    # dedicated indexed column lets the reconcile loop project just this scalar.
    # Mirrors the heartbeat-hot promotions above (last_metrics_json /
    # heartbeat_count). The context keys are STILL written alongside this column
    # (see max_plan_pool.apply_task_llm_runtime_context) so in-flight rows and
    # rollbacks stay safe; this column is the preferred read path via
    # get_task_llm_account_id, with the context lookup as fallback.
    # Index declared explicitly in __table_args__ (ix_task_llm_account_id).
    llm_account_id: str | None = Field(default=None)

    # Per-task fix-loop telemetry. Shape:
    #   {"lint": [FixLoopAttempt, ...], "test": [...], "check": [...]}
    # Each FixLoopAttempt is a loose dict validated at the ingest boundary by
    # ``app.fix_loop_telemetry.FixLoopEventIn`` (loop, iteration, fingerprint_hash,
    # error_tail, change_summary, exit_reason, source, started_at, ended_at).
    # Append-or-replace by (loop, iteration); each per-loop list capped so a
    # runaway can't blow the column up. JSONB rather than a side table because
    # the per-task attempt count is small (single digits typical, ~50 max
    # capped) and the column is read together with the rest of Task on the
    # admin drilldown endpoint.
    fix_loop_history: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )

    # Stores parsed plan output from plan-type tasks (for debugging/admin visibility)
    plan_output: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )

    # Agent Heartbeat mechanism
    last_heartbeat: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

    # Heartbeat-hot fields promoted from Task.context JSONB (audit 2026-04-29 B2 / C3).
    # The heartbeat handler rewrote the whole context blob every 30s/VM — at 200
    # VMs that's 6.6 full-blob TOAST page rewrites per second. Promoting these
    # two hot subkeys to dedicated columns lets Postgres do in-place column
    # updates without TOAST churn. Audit-trail-ish ring buffers
    # (metrics_history, _tool_trace) stay in context but only persist every
    # 5th heartbeat (handler-side modulo).
    last_metrics_json: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    last_tool_use_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    # Stable 12-hex prompt digest for the assembled guidance that drove this task.
    prompt_fingerprint: str | None = Field(default=None, max_length=12)

    # Heartbeat audit-trail gate counter (R3-W1 / Round 3 C1, alembic 0066).
    # Round 2 F2 stored this in ``task.context['_heartbeat_count']`` to gate
    # the per-5th-cycle JSONB writes (metrics_history / _tool_trace). But
    # bumping a JSONB subkey re-serializes the whole blob, defeating B2's
    # TOAST savings. Promoted to a dedicated column so the heartbeat path
    # can ``Task.heartbeat_count = Task.heartbeat_count + 1`` in SQL
    # without dirtying ``context``.
    heartbeat_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )

    priority: int = Field(
        default=50, sa_column=Column(Integer, nullable=False, server_default=text("50"))
    )

    attempt_count: int = Field(default=0)
    # TIMESTAMPTZ + server_default (migration 0061 / audit H-S4 + H-S5).
    # NaiveUTCDateTime strips tz on read (round 2 audit / C3 + H1).
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    started_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    completed_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    # Auto-populated on INSERT (default_factory + server_default) and UPDATE
    # (onupdate). Consumed by reaper.py's EVALUATING stale sweep and
    # rollup_management.py age calculations so "last touched" can be
    # distinguished from "first started". TIMESTAMPTZ as of migration 0061;
    # ``server_default=now()`` was already in place per migration 0033.
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            NaiveUTCDateTime,
            nullable=False,
            server_default=func.now(),
            onupdate=_utcnow,
        ),
    )
    archived: bool = Field(default=False, index=True)

    # VM lifecycle timing for cost tracking
    # Write-once timestamp recorded at initial dispatch (before GCE create call).
    # Used as the anchor for vm_cold_start_ms so the measurement covers the full
    # dispatch→agent-ready interval including GCE boot latency.  Never re-stamped.
    vm_dispatched_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    vm_provisioned_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    vm_started_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    vm_stopped_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    vm_spot: bool = Field(default=False)
    vm_machine_type: str | None = Field(
        default=None, sa_column=Column(String(64), nullable=True)
    )

    # Efficiency-direction (d-91d8dede) foundational instrumentation.
    # vm_cold_start_ms = (first agent heartbeat - vm_provisioned_at) in ms.
    # Recorded once per VM lifecycle and guarded by IS NULL so a re-provision
    # restamp of vm_provisioned_at (orchestrator.py line ~3240) does not
    # overwrite the genuine cold-start measurement.
    vm_cold_start_ms: int | None = Field(
        default=None,
        sa_column=Column(Integer, nullable=True),
    )
    # vm_warm_start is True when the VM was reused via a warm claim / backlog
    # reuse path rather than freshly provisioned. Cold dispatch writes False
    # (orchestrator.py); the warm-claim path writes True
    # (routers/agent_warm_continuity._finalize_warm_claim). The efficiency
    # vm_reuse_rate metric reads context['_timing']['warm_start'] as its source,
    # which now agrees with this column on warm tasks.
    vm_warm_start: bool | None = Field(
        default=False,
        sa_column=Column(Boolean, nullable=True, server_default=text("false")),
    )
    # Cumulative per-task LLM telemetry posted by the agent on every heartbeat.
    # Shape: {"llm_call_count": int, "total_input_tokens": int,
    # "total_output_tokens": int, "last_updated_at": iso8601}.
    # Backwards-compatible — older agents that don't ship these fields leave
    # this column NULL.
    api_call_summary: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    # Cumulative per-task structured-output format-compliance telemetry posted
    # by the agent on every heartbeat, sibling to api_call_summary. Shape:
    # {"format_miss_by_reason": {<reason>: int, ...}, "format_error_total": int,
    # "format_repair_attempts": int, "format_repair_successes": int,
    # "last_updated_at": iso8601}. Backwards-compatible — older agents that
    # don't ship these fields leave this column NULL.
    format_compliance_summary: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    # Provider-reported token usage from the agent's independent accumulator
    # (Component B of cost-telemetry reconciliation). Sourced from the raw
    # provider response.usage object (Anthropic Messages API, OpenAI/Codex,
    # DeepSeek), accumulated through a SEPARATE code path from api_call_summary
    # so the reconciliation can cross-check the two paths and detect
    # accumulation bugs. Backwards-compatible — agents that don't ship this
    # field leave it NULL.
    # Shape (reported): {"input_tokens": int, "output_tokens": int,
    #         "cache_creation_input_tokens": int, "cache_read_input_tokens": int,
    #         "reported": true}.
    # Shape (not reported — LLM calls made but no usage block observed):
    #         {"reported": false} — an explicit marker, never a silent null.
    provider_reported_usage: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    # Retry-attribution banking now has dedicated nullable columns. The
    # accessors below still fall back to legacy context keys so in-flight rows
    # written before the migration keep their banked totals during rollout.
    vm_runtime_cost_banked_db: float | None = Field(
        default=None,
        sa_column=Column("vm_runtime_cost_banked", Float, nullable=True),
    )
    llm_input_tokens_banked_db: int | None = Field(
        default=None,
        sa_column=Column("llm_input_tokens_banked", Integer, nullable=True),
    )
    llm_output_tokens_banked_db: int | None = Field(
        default=None,
        sa_column=Column("llm_output_tokens_banked", Integer, nullable=True),
    )
    llm_call_count_banked_db: int | None = Field(
        default=None,
        sa_column=Column("llm_call_count_banked", Integer, nullable=True),
    )
    attempt_history_db: List[Dict[str, Any]] | None = Field(
        default=None,
        sa_column=Column("attempt_history", JSONB, nullable=True),
    )

    # Lifecycle flags promoted from JSONB context (audit 2026-04-25 W1).
    # Reading these via SELECT FOR UPDATE returns DB-fresh values without
    # the stale-in-memory races that JSONB-based idempotency guards had.
    post_commit_pending: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    post_commit_failed: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    requeue_in_flight: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    retry_after_until: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

    # Durable blocker metadata for the non-terminal ``BLOCKED`` state (alembic
    # 0138). When the orchestrator parks a task on a precondition blocker it
    # records WHY here so the recheck sweep — and operators — can reason about
    # the hold without replaying log scrollback. All nullable with no server
    # default: existing QUEUED/FAILED rows read NULL (never BLOCKED), so the
    # rollout is a pure additive no-op for them.
    #   blocker_kind   — short categorical code (e.g. "file_conflict",
    #                     "pending_rollup", "upstream_dependency"); mirrors the
    #                     ``blocker_kind`` strings already used by
    #                     orchestrator_filters / task_lifecycle_requeue.
    #   blocked_reason — human-readable detail string for admin surfaces.
    #   blocked_at     — when the task entered BLOCKED (for stall/age metrics).
    #   recheck_after  — earliest time the recheck sweep should re-evaluate the
    #                     blocker; the partial index ix_task_blocked_recheck
    #                     keeps that scan tight to due-and-blocked rows only.
    blocker_kind: str | None = Field(
        default=None, sa_column=Column(String(64), nullable=True)
    )
    blocked_reason: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    blocked_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    recheck_after: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

    # Durable operator-visible metadata for the non-terminal ``PARKED`` state
    # (alembic 0199). The authoritative wake state lives on ``TaskWakeRegistration``
    # rows; these two denormalized columns answer "is this task parked and since
    # when / why" without joining, and back a tight partial index for the
    # reaper's strand sweep. Both nullable with no server default: existing rows
    # read NULL (never PARKED), so the rollout is a pure additive no-op.
    #   parked_at   — when the task entered PARKED (for age/strand metrics).
    #   park_reason — short human-readable note shown on admin surfaces.
    parked_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    park_reason: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    previous_error_fingerprint: str | None = Field(default=None)
    auto_remediation: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    strategic_cycle_id: str | None = Field(default=None, max_length=36)
    last_token_refresh_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    provider: str | None = Field(default=None)
    account_id: str | None = Field(default=None)
    model: str | None = Field(default=None)
    estimated_cost_usd: float | None = Field(
        default=None, sa_column=Column(Float, nullable=True)
    )

    # Optimistic-concurrency version (audit 2026-04-29 Phase C4 / finding M-C4):
    # Round 2 audit (C4 + C5) dropped the ``__mapper_args__["version_id_col"]``
    # enforcement: bulk Core UPDATEs (``update(Task).where(...).values(...)``)
    # bypass the mapper entirely and there were zero ``StaleDataError`` handlers
    # in the codebase, so every ORM commit could surface a 500 on concurrent
    # stale writes. The column is retained as a forensic counter — bulk archive
    # paths bump it explicitly via ``.values(version=Task.version + 1)`` in F4.
    #
    # Round 3 H10 dropped the version-axis check from
    # ``_release_parent_lock`` / ``_relock_parent`` because the
    # ``onupdate=_utcnow`` on ``updated_at`` already advances on every
    # mutation. The version axis remains as a forensic counter only.
    #
    # Default 1 (not 0) so freshly-constructed rows match what the server
    # default emits on raw INSERTs that bypass the ORM (e.g. backfill SQL).
    version: int = Field(
        default=1,
        sa_column=Column(Integer, nullable=False, server_default=text("1")),
    )

    # Criterion keys this task provides evidence for (captured at dispatch).
    # List of criterion_key strings from VersionAcceptanceCriterion.
    criterion_ids: List[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'")),
    )

    @property
    def first_payload(self) -> Dict[str, Any]:
        """First payload dict, defaulting to empty dict if none exist.

        When the caller projected `payloads[0]` SQL-side (admin_digest.py does
        this to avoid loading the full JSONB array for 5000-row scans), the
        value is stashed on `_projected_first_payload` and returned directly —
        skipping the deferred-column round-trip that `self.payloads` would
        trigger when `load_only()` excluded it.
        """
        if hasattr(self, "_projected_first_payload"):
            return self._projected_first_payload or {}
        return (self.payloads or [{}])[0]

    __table_args__ = (
        # Note: ``ix_task_status_priority`` (status, priority) and
        # ``ix_task_project_id_created_at`` (project_id, created_at) were
        # dropped in alembic 0060 — both fully shadowed by the broader
        # composites below via left-prefix matching (audit 2026-04-29 / H-S1,
        # H-S2). Don't reintroduce without confirming a query needs only the
        # narrower prefix.
        Index("ix_task_status_priority_created", "status", "priority", "created_at"),
        Index(
            "ix_task_queued_created_at",
            "created_at",
            postgresql_where=text("status = 'QUEUED'"),
            sqlite_where=text("status = 'QUEUED'"),
        ),
        Index(
            "ix_task_completed_created_at",
            "created_at",
            postgresql_where=text("status = 'COMPLETED'"),
            sqlite_where=text("status = 'COMPLETED'"),
        ),
        Index("ix_task_project_status_created", "project_id", "status", "created_at"),
        Index("ix_task_project_status_updated", "project_id", "status", "updated_at"),
        Index(
            "ix_task_project_status_completed", "project_id", "status", "completed_at"
        ),
        # BLOCKED joins the active-status set here (alembic 0138): a blocked
        # task is non-terminal and still owns its idempotency slot, so a
        # duplicate insert with the same key while the original is parked must
        # still be deduped. Widening the predicate is rollout-safe — no row is
        # BLOCKED at migration time, QUEUED is already covered, and FAILED stays
        # excluded — so existing QUEUED/FAILED rows are unaffected.
        Index(
            "ux_task_active_idempotency_key",
            "idempotency_key",
            unique=True,
            postgresql_where=text(
                "idempotency_key IS NOT NULL AND status IN "
                "('QUEUED','PROVISIONING','IN_PROGRESS','EVALUATING','BLOCKED','PARKED')"
            ),
            sqlite_where=text(
                "idempotency_key IS NOT NULL AND status IN "
                "('QUEUED','PROVISIONING','IN_PROGRESS','EVALUATING','BLOCKED','PARKED')"
            ),
        ),
        Index("ix_task_context_gin", "context", postgresql_using="gin"),
        Index("ix_task_depends_on_gin", "depends_on", postgresql_using="gin"),
        # D2 over-fetch fix (d-57436105): the Max-Plan reconcile loop counts
        # active tasks per llm_account_id every ~30s; index the promoted column.
        Index("ix_task_llm_account_id", "llm_account_id"),
        Index("ix_task_provider_created_at", "provider", "created_at"),
        Index("ix_task_account_id_created_at", "account_id", "created_at"),
        Index(
            "ix_task_account_model_provider_created",
            "account_id",
            "provider",
            "model",
            "created_at",
        ),
        # Partial index for the orchestrator dispatch hot path
        # (.where(Task.status == QUEUED, Task.requires_approval.is_(False))).
        # Covers status, priority, created_at so the planner can satisfy the
        # ORDER BY priority / created_at sort without a full QUEUED-queue scan.
        # Postgres-only via alembic 0116 CONCURRENTLY DDL; the metadata Index
        # registers the name + columns for test_model_indexes_covered_by_migrations.
        Index(
            "ix_task_queued_no_approval",
            "status",
            "priority",
            "created_at",
            postgresql_where=text("requires_approval = false"),
            sqlite_where=text("requires_approval = 0"),
        ),
        # Partial expression index on the leading payload's pr_number so the
        # review-pipeline per-PR iteration-cap query (review_pipeline.py) can
        # filter in SQL rather than loading every terminal Task row in the
        # project and filtering in Python. See alembic 0057 / audit C2.
        # The metadata Index registers a column reference (``payloads``) for
        # the test_model_indexes_covered_by_migrations invariant; the alembic
        # 0057 migration creates the actual expression index on
        # ``(payloads -> 0 ->> 'pr_number')`` via raw SQL on Postgres.
        Index(
            "ix_task_payloads_pr_number",
            "payloads",
            postgresql_where=text("archived = false"),
            sqlite_where=text("archived = false"),
        ),
        # Composite indexes for reaper + claim hot paths (audit 2026-04-29
        # Phase E1 / findings M-P2, M-P3). Both partial on Postgres so the
        # index pages stay small (terminal/top-level rows excluded). The
        # alembic 0064 migration creates them CONCURRENTLY on Postgres;
        # the metadata Index entries here register the names + column
        # references for the test_model_indexes_covered_by_migrations
        # invariant.
        Index(
            "ix_task_status_last_heartbeat",
            "status",
            "last_heartbeat",
            postgresql_where=text("status IN ('IN_PROGRESS', 'PROVISIONING')"),
        ),
        # Partial index for the admin health-overview query that selects
        # COMPLETED/FAILED tasks with vm_instance_id IS NOT NULL ordered by
        # last_heartbeat desc. The existing ix_task_status_last_heartbeat is
        # partial on IN_PROGRESS/PROVISIONING so it actively excludes these rows.
        Index(
            "ix_task_terminal_last_heartbeat",
            "last_heartbeat",
            postgresql_where=text(
                "status IN ('COMPLETED', 'FAILED') AND vm_instance_id IS NOT NULL"
            ),
            sqlite_where=text(
                "status IN ('COMPLETED', 'FAILED') AND vm_instance_id IS NOT NULL"
            ),
        ),
        Index(
            "ix_task_parent_task_id_status",
            "parent_task_id",
            "status",
            postgresql_where=text("parent_task_id IS NOT NULL"),
        ),
        # Partial expression index on the leading payload's ``type`` so the
        # orchestrator pipeline budget query (every 5s) can filter active
        # tasks by type without SeqScanning every PROVISIONING/IN_PROGRESS
        # row. Postgres-only via alembic 0067 raw SQL on
        # ``(payloads -> 0 ->> 'type')`` WHERE status IN ('PROVISIONING',
        # 'IN_PROGRESS'). Metadata Index entry registers the name + a
        # column reference (``payloads``) for the
        # test_model_indexes_covered_by_migrations invariant. See R4-W2 / H5.
        Index(
            "ix_task_payloads_type_active",
            "payloads",
            postgresql_where=text("status IN ('PROVISIONING', 'IN_PROGRESS')"),
        ),
        # Expression index on payloads[0]->>'type' covering all statuses so
        # get_project_tasks task-type filters avoid a full table scan.
        # Added via alembic 0238 CONCURRENTLY DDL.
        Index("ix_task_payloads_type_all", "payloads"),
        # Partial index on retry_after_until so the reaper's stale
        # retry_after sweep (reaper.py: status=QUEUED AND
        # retry_after_until IS NOT NULL) doesn't degrade to linear scan
        # of the QUEUED queue at 1k users. Postgres-only via alembic 0069
        # CONCURRENTLY DDL; the metadata Index registers the name +
        # column for ``test_model_indexes_covered_by_migrations``.
        # See R5-W2-C.
        Index(
            "ix_task_retry_after_active",
            "retry_after_until",
            postgresql_where=text(
                "status = 'QUEUED' AND retry_after_until IS NOT NULL"
            ),
            sqlite_where=text("status = 'QUEUED' AND retry_after_until IS NOT NULL"),
        ),
        # Partial index on recheck_after so the blocked-task recheck sweep
        # (status = 'BLOCKED' AND recheck_after <= now()) hits a tight index
        # range scan instead of seq-scanning the whole table for the small set
        # of parked rows. Postgres-only via alembic 0138 CONCURRENTLY DDL; the
        # metadata Index registers the name + column for
        # test_model_indexes_covered_by_migrations. See the BLOCKED state.
        Index(
            "ix_task_blocked_recheck",
            "recheck_after",
            postgresql_where=text("status = 'BLOCKED'"),
            sqlite_where=text("status = 'BLOCKED'"),
        ),
        # Partial index on parked_at so the reaper's PARKED-strand sweep
        # (status = 'PARKED' ...) hits a tight index range scan instead of
        # seq-scanning the whole table for the small parked set. Postgres-only
        # via alembic 0199 CONCURRENTLY DDL; the metadata Index registers the
        # name + column for test_model_indexes_covered_by_migrations. See the
        # PARKED state and TaskWakeRegistration.
        Index(
            "ix_task_parked_at",
            "parked_at",
            postgresql_where=text("status = 'PARKED'"),
            sqlite_where=text("status = 'PARKED'"),
        ),
        # Defense-in-depth size cap on payloads JSONB (audit 2026-04-29 D4 /
        # finding M-S5). The Python layer's ``pack_under_budget`` already caps
        # strategic-spawn payloads at 120 KB, but a future code path could
        # insert a runaway. The 200 KB CHECK constraint chk_task_payloads_size
        # is enforced server-side by alembic 0063 (Postgres-only). Not declared
        # here in __table_args__ because the constraint expression
        # ``octet_length(payloads::text) < 200000`` is Postgres-specific syntax
        # that breaks SQLite-fixture tests using metadata.create_all.
        # See alembic 0063 / memory ``reference_strategic_payload_bloat.md``.
        # Partial index for auto_remediation.py queries that filter
        # Task.auto_remediation.is_(True) and
        # Task.previous_error_fingerprint == fingerprint — the equality filter on
        # previous_error_fingerprint narrows the result before the created_at range
        # scan, avoiding a full scan of the auto_remediation rows.
        Index(
            "ix_task_auto_remediation_active",
            "project_id",
            "previous_error_fingerprint",
            "created_at",
            postgresql_where=text("auto_remediation = true"),
            sqlite_where=text("auto_remediation = 1"),
        ),
        # Partial index for services.py post_commit_pending queries (lines 2233,
        # 2252) that filter Task.post_commit_pending.is_(True) — avoids a full
        # table scan since only a small subset of tasks have this flag set.
        Index(
            "ix_task_post_commit_pending",
            "project_id",
            "status",
            postgresql_where=text("post_commit_pending = true"),
            sqlite_where=text("post_commit_pending = 1"),
        ),
        # Leading-``created_at`` index for the global (project_id=None) admin
        # analytics scans — the digest (admin_digest._compute_digest) and
        # health-overview (admin_health._compute_health_overview) filter
        # ``created_at >= window`` over all projects. Every other composite has
        # created_at TRAILING (status/project/provider/account first), so the
        # global time-window scan had no usable index and full-scanned the
        # whole table — as the table grew it crossed the 120s statement_timeout
        # (incident 2026-05-29: digest timeout storm → pool starvation →
        # orchestrator hang). A leading-created_at range scan bounds the work to
        # the recent window. Postgres-only via alembic 0129 CONCURRENTLY DDL;
        # this metadata Index registers the name for
        # test_model_indexes_covered_by_migrations.
        Index("ix_task_created_at", "created_at"),
        Index("ix_task_started_at", "started_at"),
        # Standalone index on updated_at for reaper staleness sweeps,
        # strategic spawning (ORDER BY updated_at DESC), operator attention,
        # and work session recheck queries (~15 query sites total). Without
        # this index every staleness sweep does a full sort of the tasks table.
        Index("ix_task_updated_at", "updated_at"),
        # Partial index on vm_stopped_at for anomaly_detection cost-window queries
        # that filter vm_stopped_at IS NOT NULL and order by vm_stopped_at.
        # Without this, the query full-scans all task history linearly.
        # Postgres-only predicate; alembic 0381 creates it CONCURRENTLY.
        Index(
            "ix_task_vm_stopped_at",
            "vm_stopped_at",
            postgresql_where=text("vm_stopped_at IS NOT NULL"),
            sqlite_where=text("vm_stopped_at IS NOT NULL"),
        ),
        # Partial index on vm_dispatched_at for warm_pool_maintainer queries
        # that filter vm_dispatched_at >= cutoff every ~30s leader tick to
        # detect demand. Without this, the query scans all non-QUEUED tasks
        # across all projects. Matches the ix_task_vm_stopped_at precedent.
        # Postgres-only via alembic 0450 CONCURRENTLY DDL.
        Index(
            "ix_task_vm_dispatched_at",
            "vm_dispatched_at",
            postgresql_where=text("vm_dispatched_at IS NOT NULL"),
            sqlite_where=text("vm_dispatched_at IS NOT NULL"),
        ),
        # Partial index on (status, completed_at) for the global acceptance-gate
        # backfill query (acceptance_gate.py:1170-1175) that scans for the 200
        # oldest COMPLETED tasks WITHOUT a project_id filter.  The existing
        # ix_task_project_status_composite has project_id as the leading column,
        # so it cannot serve a project-less ORDER BY completed_at ASC scan.  This
        # index covers status=COMPLETED + completed_at ordering directly.
        # Postgres-only via alembic 0485 CONCURRENTLY DDL; the metadata Index
        # registers the name + columns for
        # test_model_indexes_covered_by_migrations.
        Index(
            "ix_task_status_completed_at",
            "status",
            "completed_at",
            postgresql_where=text("status = 'COMPLETED'"),
            sqlite_where=text("status = 'COMPLETED'"),
        ),
        # Trigram GIN indexes for the admin task-search ILIKE filter (title,
        # description, latest_status_message).  Without these every task search
        # triggers a full sequential table scan.  The ``postgresql_*`` kwargs are
        # dialect-scoped — SQLite create_all emits plain indexes — so the test
        # suite is unaffected.  See KnowledgeDocument trigram precedent above.
        Index(
            "ix_task_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
        Index(
            "ix_task_description_trgm",
            "description",
            postgresql_using="gin",
            postgresql_ops={"description": "gin_trgm_ops"},
        ),
        Index(
            "ix_task_latest_status_message_trgm",
            "latest_status_message",
            postgresql_using="gin",
            postgresql_ops={"latest_status_message": "gin_trgm_ops"},
        ),
        # Partial index on (updated_at) for the EVALUATING CI-monitor scan
        # (orchestrator_ci_monitor.py:289-303) which repeatedly selects
        # EVALUATING rows on the leader's hot cycle. The full ix_task_updated_at
        # spans every task; this partial index over only the (small) EVALUATING
        # subset lets the planner walk just those rows — same partial-by-status
        # shape as ix_task_blocked_recheck. Postgres-only via alembic 0525
        # CONCURRENTLY DDL; the metadata Index registers the name + column for
        # test_model_indexes_covered_by_migrations.
        Index(
            "ix_task_evaluating_updated_at",
            "updated_at",
            postgresql_where=text("status = 'EVALUATING'"),
            sqlite_where=text("status = 'EVALUATING'"),
        ),
    )

    # Optimistic concurrency: ``version_id_col`` mapper arg was dropped in
    # round 2 audit (C4 + C5) — see the comment on the ``version`` Field
    # above. Bulk Core UPDATEs bypassed it and no ``StaleDataError``
    # handlers existed, so ORM commits leaked 500s on concurrent stale
    # writes. The column itself is retained as a forensic counter; bulk
    # archive paths bump it explicitly in F4.

    project: Project = Relationship(back_populates="tasks")
    artifacts: List["Artifact"] = Relationship(
        back_populates="task", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    history: List["TaskHistory"] = Relationship(
        back_populates="task", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    # ComputeJob is durable, first-class persistence decoupled from Task: a
    # task delete/reset must PRESERVE the compute-job history (task_id is
    # detached via the DB-level ``ondelete="SET NULL"`` FK), so this link uses
    # NEITHER delete-orphan NOR an ORM null-out — ``passive_deletes`` leaves the
    # detach to the database, which also keeps bulk ``delete(Task)`` paths
    # (e.g. ``admin_projects.reset_project_admin``) from FK-violating.
    compute_jobs: List["ComputeJob"] = Relationship(
        back_populates="task", sa_relationship_kwargs={"passive_deletes": True}
    )

    def _get_context_backed_value(self, key: str) -> Any:
        return (self.context or {}).get(key)

    def _set_context_backed_value(self, key: str, value: Any) -> None:
        ctx = dict(self.context or {})
        if value is None:
            ctx.pop(key, None)
        else:
            ctx[key] = value
        self.context = ctx

    @property
    def vm_runtime_cost_banked(self) -> float | None:
        db_value = self.__dict__.get("vm_runtime_cost_banked_db", _UNLOADED)
        if isinstance(db_value, (int, float)):
            return float(db_value)
        value = self._get_context_backed_value(_TASK_BANKED_VM_RUNTIME_COST_KEY)
        return float(value) if isinstance(value, (int, float)) else None

    @vm_runtime_cost_banked.setter
    def vm_runtime_cost_banked(self, value: float | None) -> None:
        self.vm_runtime_cost_banked_db = None if value is None else float(value)
        self._set_context_backed_value(_TASK_BANKED_VM_RUNTIME_COST_KEY, None)

    @property
    def llm_input_tokens_banked(self) -> int | None:
        db_value = self.__dict__.get("llm_input_tokens_banked_db", _UNLOADED)
        if isinstance(db_value, (int, float)) and db_value >= 0:
            return int(db_value)
        value = self._get_context_backed_value(_TASK_BANKED_LLM_INPUT_TOKENS_KEY)
        return int(value) if isinstance(value, (int, float)) and value >= 0 else None

    @llm_input_tokens_banked.setter
    def llm_input_tokens_banked(self, value: int | None) -> None:
        self.llm_input_tokens_banked_db = None if value is None else int(value)
        self._set_context_backed_value(_TASK_BANKED_LLM_INPUT_TOKENS_KEY, None)

    @property
    def llm_output_tokens_banked(self) -> int | None:
        db_value = self.__dict__.get("llm_output_tokens_banked_db", _UNLOADED)
        if isinstance(db_value, (int, float)) and db_value >= 0:
            return int(db_value)
        value = self._get_context_backed_value(_TASK_BANKED_LLM_OUTPUT_TOKENS_KEY)
        return int(value) if isinstance(value, (int, float)) and value >= 0 else None

    @llm_output_tokens_banked.setter
    def llm_output_tokens_banked(self, value: int | None) -> None:
        self.llm_output_tokens_banked_db = None if value is None else int(value)
        self._set_context_backed_value(_TASK_BANKED_LLM_OUTPUT_TOKENS_KEY, None)

    @property
    def llm_call_count_banked(self) -> int | None:
        db_value = self.__dict__.get("llm_call_count_banked_db", _UNLOADED)
        if isinstance(db_value, (int, float)) and db_value >= 0:
            return int(db_value)
        value = self._get_context_backed_value(_TASK_BANKED_LLM_CALL_COUNT_KEY)
        return int(value) if isinstance(value, (int, float)) and value >= 0 else None

    @llm_call_count_banked.setter
    def llm_call_count_banked(self, value: int | None) -> None:
        self.llm_call_count_banked_db = None if value is None else int(value)
        self._set_context_backed_value(_TASK_BANKED_LLM_CALL_COUNT_KEY, None)

    @property
    def attempt_history(self) -> List[Dict[str, Any]] | None:
        db_value = self.__dict__.get("attempt_history_db", _UNLOADED)
        if isinstance(db_value, list):
            return list(db_value)
        value = self._get_context_backed_value(_TASK_ATTEMPT_HISTORY_KEY)
        return list(value) if isinstance(value, list) else None

    @attempt_history.setter
    def attempt_history(self, value: List[Dict[str, Any]] | None) -> None:
        self.attempt_history_db = None if value is None else list(value)
        self._set_context_backed_value(_TASK_ATTEMPT_HISTORY_KEY, None)


def _coerce_nonnegative_int(value: Any) -> int:
    if isinstance(value, (int, float)) and value >= 0:
        return int(value)
    return 0


def task_lifetime_attempt_ordinal(
    attempt_count: Any,
    banked_attempt_count: Any,
    *,
    current_attempt_ordinal: Any = 0,
    last_banked_attempt_ordinal: Any = 0,
) -> int:
    """Return the task's authoritative, one-based lifetime attempt ordinal.

    ``Task.attempt_count`` is the retry-budget counter for the current retry
    epoch and is intentionally reset by an operator fresh-attempt requeue.
    ``attempt_history`` banks closed executions across those epoch resets.  A
    normal retry advances both values, so they must be combined with ``max``
    rather than added; budget-only pre-claim failures and budget-exempt runtime
    retries are then covered by whichever source is larger.
    """

    return max(
        _coerce_nonnegative_int(attempt_count) + 1,
        _coerce_nonnegative_int(banked_attempt_count) + 1,
        _coerce_nonnegative_int(current_attempt_ordinal),
        _coerce_nonnegative_int(last_banked_attempt_ordinal) + 1,
    )


def _attempt_history_last_ordinal(history: Any) -> int:
    """Return the strongest closed-attempt ordinal carried by history."""

    if not isinstance(history, list):
        return 0
    highest = len(history)
    for snapshot in history:
        if not isinstance(snapshot, dict):
            continue
        highest = max(
            highest,
            _coerce_nonnegative_int(snapshot.get("attempt_ordinal")),
        )
    return highest


def get_task_lifetime_attempt_ordinal(task: Task) -> int:
    """Return the current one-based lifetime ordinal for a loaded task."""

    history = task.attempt_history or []
    ctx = task.context or {}
    return task_lifetime_attempt_ordinal(
        task.attempt_count,
        len(history),
        current_attempt_ordinal=ctx.get(
            TASK_CURRENT_ATTEMPT_ORDINAL_CONTEXT_KEY
        ),
        last_banked_attempt_ordinal=max(
            _attempt_history_last_ordinal(history),
            _coerce_nonnegative_int(
                ctx.get(TASK_LAST_BANKED_ATTEMPT_ORDINAL_CONTEXT_KEY)
            ),
        ),
    )


def advance_task_lifetime_attempt(task: Task) -> int:
    """Idempotently mark that ``task`` is entering its next fresh attempt."""

    history = task.attempt_history or []
    ctx = dict(task.context or {})
    last_banked = max(
        _attempt_history_last_ordinal(history),
        _coerce_nonnegative_int(
            ctx.get(TASK_LAST_BANKED_ATTEMPT_ORDINAL_CONTEXT_KEY)
        ),
    )
    current = _coerce_nonnegative_int(
        ctx.get(TASK_CURRENT_ATTEMPT_ORDINAL_CONTEXT_KEY)
    )
    next_ordinal = max(
        current,
        last_banked + 1,
        _coerce_nonnegative_int(task.attempt_count) + 1,
        len(history) + 1,
    )
    ctx[TASK_CURRENT_ATTEMPT_ORDINAL_CONTEXT_KEY] = next_ordinal
    ctx[TASK_LAST_BANKED_ATTEMPT_ORDINAL_CONTEXT_KEY] = last_banked
    task.context = ctx
    return next_ordinal


def get_task_current_attempt_llm_totals(task: Task) -> tuple[int, int, int]:
    summary = task.api_call_summary or {}
    return (
        _coerce_nonnegative_int(summary.get("total_input_tokens")),
        _coerce_nonnegative_int(summary.get("total_output_tokens")),
        _coerce_nonnegative_int(summary.get("llm_call_count")),
    )


def get_task_current_attempt_cache_input_tokens(task: Task) -> tuple[int, int]:
    """Cache-creation / cache-read input sub-counts for the live attempt.

    Reads the two optional cumulative counters the agent forwards on heartbeat
    (``total_cache_creation_input_tokens`` / ``total_cache_read_input_tokens``)
    out of ``api_call_summary``. ``total_input_tokens`` remains the INCLUSIVE sum
    of all provider-reported input classes, so these are sub-counts of it. Returns
    ``(0, 0)`` when the summary carries no breakdown (older agents / pre-tracking
    rows) so callers fall back to pricing the full inclusive input total at the
    base rate.
    """
    summary = task.api_call_summary or {}
    return (
        _coerce_nonnegative_int(summary.get("total_cache_creation_input_tokens")),
        _coerce_nonnegative_int(summary.get("total_cache_read_input_tokens")),
    )


def get_task_total_llm_totals(task: Task) -> tuple[int, int, int]:
    input_tokens, output_tokens, call_count = get_task_current_attempt_llm_totals(task)
    return (
        input_tokens + int(task.llm_input_tokens_banked or 0),
        output_tokens + int(task.llm_output_tokens_banked or 0),
        call_count + int(task.llm_call_count_banked or 0),
    )


def get_task_current_attempt_vm_cost_usd(
    task: Task, *, stopped_at: datetime | None = None
) -> float:
    if task.vm_provisioned_at is None:
        return 0.0
    effective_stop = stopped_at or task.vm_stopped_at
    if effective_stop is None:
        return 0.0
    runtime_seconds = max(
        0.0, (effective_stop - task.vm_provisioned_at).total_seconds()
    )
    if runtime_seconds <= 0:
        return 0.0

    machine_type = task.__dict__.get("vm_machine_type")
    if machine_type and machine_type.startswith("cloud-run-"):
        return runtime_seconds * get_cloud_run_rate_for_machine_type(machine_type)
    return runtime_seconds * get_vm_rate_per_second(
        machine_type=machine_type, spot=task.vm_spot
    )


def get_task_total_vm_cost_usd(
    task: Task, *, stopped_at: datetime | None = None
) -> float:
    return float(
        task.vm_runtime_cost_banked or 0.0
    ) + get_task_current_attempt_vm_cost_usd(task, stopped_at=stopped_at)


def get_task_cost_by_provider_model(
    task: Task,
    *,
    window_start: datetime | None = None,
) -> dict[str, float]:
    """Return per-(provider, model) VM cost breakdown for a task.

    Iterates attempt_history for banked attempts and the live span for the
    current attempt. Provider/model pairs are resolved atomically per attempt
    (both from the same source), so impossible 'chimera' buckets like
    openai|claude-opus-4-8 cannot appear. An unresolvable pair degrades to
    'unknown|unknown' as a whole, never a half-resolved key.

    When window_start is provided, banked attempts whose recorded vm_stopped_at
    falls before window_start are excluded. The live span is always included when
    it exists (digest callers filter tasks by vm_stopped_at before calling).
    """
    result: dict[str, float] = {}

    for attempt in task.attempt_history or []:
        cost = float(attempt.get("vm_runtime_cost_usd") or 0.0)
        if cost <= 0:
            continue
        if window_start is not None:
            stopped_at_str = attempt.get("vm_stopped_at")
            if stopped_at_str:
                try:
                    stopped_dt = datetime.fromisoformat(stopped_at_str)
                    # Strip tz if present (stored timestamps are naive UTC)
                    if stopped_dt.tzinfo is not None:
                        stopped_dt = stopped_dt.replace(tzinfo=None)
                    if stopped_dt < window_start:
                        continue
                except (ValueError, TypeError):
                    pass
        provider = attempt.get("provider")
        model = attempt.get("model")
        if provider and model:  # noqa: SIM108
            bucket = f"{provider}|{model}"
        else:
            bucket = "unknown|unknown"
        result[bucket] = result.get(bucket, 0.0) + cost

    # Live (un-banked) current attempt
    live_cost = get_task_current_attempt_vm_cost_usd(task)
    if live_cost > 0:
        provider = task.provider
        model = task.model
        if provider and model:  # noqa: SIM108
            bucket = f"{provider}|{model}"
        else:
            bucket = "unknown|unknown"
        result[bucket] = result.get(bucket, 0.0) + live_cost

    return result


def get_task_total_vm_cost_usd_or_none(
    task: Task, *, stopped_at: datetime | None = None
) -> float | None:
    """Total VM cost (banked retries + current attempt), or None for tasks that never ran.

    Drop-in replacement for local ``_estimated_cost_usd`` helpers that compute
    cost from the current vm_provisioned_at/vm_stopped_at interval only — those
    silently drop banked retry spend.  Returns None only when there is no VM
    cost evidence at all (no banked amount and no current provisioning window),
    preserving the "None means no data" contract those helpers expose to callers
    that skip or display-blank zero-data rows.
    """
    has_evidence = (
        task.vm_runtime_cost_banked is not None and task.vm_runtime_cost_banked > 0
    ) or task.vm_provisioned_at is not None
    if not has_evidence:
        return None
    return get_task_total_vm_cost_usd(task, stopped_at=stopped_at)


def with_task_attempt_banking(
    task: Task, context: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Return context plus the task's banked retry-attribution fields.

    Requeue paths sometimes keep mutating a stale pre-banking context copy
    after ``bank_task_current_attempt()`` runs. Copying only the banked
    attribution fields back into that context preserves retry costs without
    resurrecting runtime keys that the caller intentionally removed.
    """

    merged = dict(context or {})

    banked_vm_cost = task.vm_runtime_cost_banked
    if banked_vm_cost is None:
        merged.pop(_TASK_BANKED_VM_RUNTIME_COST_KEY, None)
    else:
        merged[_TASK_BANKED_VM_RUNTIME_COST_KEY] = float(banked_vm_cost)

    banked_input_tokens = task.llm_input_tokens_banked
    if banked_input_tokens is None:
        merged.pop(_TASK_BANKED_LLM_INPUT_TOKENS_KEY, None)
    else:
        merged[_TASK_BANKED_LLM_INPUT_TOKENS_KEY] = int(banked_input_tokens)

    banked_output_tokens = task.llm_output_tokens_banked
    if banked_output_tokens is None:
        merged.pop(_TASK_BANKED_LLM_OUTPUT_TOKENS_KEY, None)
    else:
        merged[_TASK_BANKED_LLM_OUTPUT_TOKENS_KEY] = int(banked_output_tokens)

    banked_call_count = task.llm_call_count_banked
    if banked_call_count is None:
        merged.pop(_TASK_BANKED_LLM_CALL_COUNT_KEY, None)
    else:
        merged[_TASK_BANKED_LLM_CALL_COUNT_KEY] = int(banked_call_count)

    attempt_history = task.attempt_history
    if attempt_history is None:
        merged.pop(_TASK_ATTEMPT_HISTORY_KEY, None)
    else:
        merged[_TASK_ATTEMPT_HISTORY_KEY] = list(attempt_history)

    task_ctx = task.context or {}
    for marker_key in (
        TASK_CURRENT_ATTEMPT_ORDINAL_CONTEXT_KEY,
        TASK_LAST_BANKED_ATTEMPT_ORDINAL_CONTEXT_KEY,
    ):
        marker_value = _coerce_nonnegative_int(task_ctx.get(marker_key))
        if marker_value > 0:
            merged[marker_key] = marker_value
        else:
            merged.pop(marker_key, None)

    return merged


def bank_task_current_attempt(
    task: Task,
    *,
    stopped_at: datetime | None = None,
    clear_current_attempt: bool = True,
    attempt_ordinal: int | None = None,
) -> Dict[str, Any] | None:
    effective_stop = stopped_at or task.vm_stopped_at
    vm_cost = get_task_current_attempt_vm_cost_usd(task, stopped_at=effective_stop)
    input_tokens, output_tokens, call_count = get_task_current_attempt_llm_totals(task)
    cache_creation_tokens, cache_read_tokens = (
        get_task_current_attempt_cache_input_tokens(task)
    )
    has_attempt_data = (
        task.vm_provisioned_at is not None
        or task.api_call_summary is not None
        or vm_cost > 0
        or input_tokens > 0
        or output_tokens > 0
        or call_count > 0
    )
    history = list(task.attempt_history or [])
    task_context = dict(task.context or {})
    history_last_ordinal = _attempt_history_last_ordinal(history)
    last_banked_ordinal = max(
        history_last_ordinal,
        _coerce_nonnegative_int(
            task_context.get(TASK_LAST_BANKED_ATTEMPT_ORDINAL_CONTEXT_KEY)
        ),
    )
    stored_current_ordinal = _coerce_nonnegative_int(
        task_context.get(TASK_CURRENT_ATTEMPT_ORDINAL_CONTEXT_KEY)
    )
    if attempt_ordinal is not None:
        current_ordinal = max(1, _coerce_nonnegative_int(attempt_ordinal))
    elif stored_current_ordinal:
        current_ordinal = stored_current_ordinal
        # New live telemetry after an already-banked ordinal proves that a new
        # attempt started even if an older direct requeue path did not stamp the
        # fresh-attempt marker.
        if has_attempt_data and current_ordinal <= last_banked_ordinal:
            current_ordinal = max(
                last_banked_ordinal + 1,
                _coerce_nonnegative_int(task.attempt_count) + 1,
            )
    else:
        # Legacy rows have no marker. Seed from the strongest available source;
        # callers that increment retry budget before banking pass the captured
        # pre-increment ordinal explicitly.
        current_ordinal = max(
            _coerce_nonnegative_int(task.attempt_count) + 1,
            history_last_ordinal + 1,
        )

    # A fresh-attempt marker is itself durable evidence that the attempt
    # existed. Bank a minimal placeholder even when provisioning died before
    # timestamps/token telemetry arrived. When current == last_banked and live
    # fields were already cleared, this is the repeated-bank idempotency guard.
    if not has_attempt_data and current_ordinal <= last_banked_ordinal:
        return None

    if vm_cost > 0:
        task.vm_runtime_cost_banked = (
            float(task.vm_runtime_cost_banked or 0.0) + vm_cost
        )
    if input_tokens > 0:
        task.llm_input_tokens_banked = (
            int(task.llm_input_tokens_banked or 0) + input_tokens
        )
    if output_tokens > 0:
        task.llm_output_tokens_banked = (
            int(task.llm_output_tokens_banked or 0) + output_tokens
        )
    if call_count > 0:
        task.llm_call_count_banked = int(task.llm_call_count_banked or 0) + call_count

    status_value = (
        task.status.value if isinstance(task.status, TaskStatus) else str(task.status)
    )
    snapshot: Dict[str, Any] = {
        # ``attempt_count`` is a retry-budget counter and some callers increment
        # it before closing the attempt.  Keep the immutable banked sequence and
        # the observed retry counter as two explicitly different concepts.
        "attempt_ordinal": current_ordinal,
        "retry_count_at_bank": _coerce_nonnegative_int(task.attempt_count),
        "status": status_value[:50],
        "vm_provisioned_at": task.vm_provisioned_at.isoformat()
        if task.vm_provisioned_at
        else None,
        "vm_stopped_at": effective_stop.isoformat() if effective_stop else None,
        "vm_spot": bool(task.vm_spot),
        "vm_machine_type": task.vm_machine_type,
        "provider": task.provider,
        "model": task.model,
        "vm_runtime_cost_usd": round(vm_cost, 6),
        "llm_input_tokens": input_tokens,
        "llm_output_tokens": output_tokens,
        "llm_call_count": call_count,
    }
    failure_class = task_context.get("failure_class") or task_context.get(
        "failure_category"
    )
    if isinstance(failure_class, str) and failure_class:
        snapshot["failure_class"] = failure_class[:100]
    raw_failure_category = task_context.get("agent_failure_category_raw")
    if isinstance(raw_failure_category, str) and raw_failure_category:
        snapshot["agent_failure_category_raw"] = raw_failure_category[:100]

    # The live message is already sanitized on the agent-status path, but
    # banking is also called by reapers/provisioning recovery.  Sanitize again
    # before retaining a small tail so those paths cannot persist credentials.
    failure_message = task.latest_status_message
    if isinstance(failure_message, str) and failure_message:
        from app.broadcast import _sanitize_log

        snapshot["failure_message_tail"] = _sanitize_log(failure_message)[-2000:]

    # Re-validate the current attempt's report rather than copying arbitrary
    # context.  Omit the key entirely when this attempt supplied no report;
    # callers/reset logic must never fall back to a previous snapshot's report.
    from app.failure_classification import (
        AGENT_ERROR_REPORT_CONTEXT_KEY,
        sanitize_agent_error_report,
    )

    agent_error_report = sanitize_agent_error_report(
        task_context.get(AGENT_ERROR_REPORT_CONTEXT_KEY)
    )
    if agent_error_report is not None:
        snapshot[AGENT_ERROR_REPORT_CONTEXT_KEY] = agent_error_report
    # Carry the cache-class sub-counts only when the live attempt actually had a
    # breakdown, so banked rows that predate cache tracking stay free of the keys
    # and downstream pricing falls back to the base-rate valuation for them.
    # llm_input_tokens above stays the INCLUSIVE input total (unchanged meaning).
    if cache_creation_tokens > 0 or cache_read_tokens > 0:
        snapshot["llm_cache_creation_input_tokens"] = cache_creation_tokens
        snapshot["llm_cache_read_input_tokens"] = cache_read_tokens
    # Carry the attempt's provider-reported usage probe (Component B) so
    # reconciliation accumulates across retried attempts instead of overwriting.
    # Omit the key for attempts without a probe or marker so legacy banked rows
    # stay byte-identical to pre-feature rows.
    attempt_provider_usage = task.provider_reported_usage
    if isinstance(attempt_provider_usage, dict) and attempt_provider_usage:
        snapshot["provider_reported_usage"] = dict(attempt_provider_usage)
    # Carry num_turns only when the agent reported it; older agents that omit the
    # field produce banked rows without this key so per-turn metrics stay None.
    num_turns_val = (task.api_call_summary or {}).get("num_turns")
    if isinstance(num_turns_val, int) and num_turns_val >= 0:
        snapshot["num_turns"] = num_turns_val
    # Carry total_tool_result_bytes only when the agent reported it; omit the key
    # for older agents so downstream analysis can distinguish zero from unreported.
    tool_result_bytes_val = (task.api_call_summary or {}).get("total_tool_result_bytes")
    if isinstance(tool_result_bytes_val, int) and tool_result_bytes_val >= 0:
        snapshot["total_tool_result_bytes"] = tool_result_bytes_val
    # Carry prompt-size telemetry only when present; omit for older agents or
    # non-research/plan tasks so downstream attribution can distinguish unknown.
    prompt_size_val = (task.api_call_summary or {}).get("prompt_size_bytes")
    if isinstance(prompt_size_val, int) and prompt_size_val >= 0:
        snapshot["prompt_size_bytes"] = prompt_size_val
    prompt_section_count_val = (task.api_call_summary or {}).get("prompt_section_count")
    if isinstance(prompt_section_count_val, int) and prompt_section_count_val >= 0:
        snapshot["prompt_section_count"] = prompt_section_count_val
    prompt_section_sizes_val = (task.api_call_summary or {}).get("prompt_section_sizes")
    if isinstance(prompt_section_sizes_val, list):
        snapshot["prompt_section_sizes"] = prompt_section_sizes_val
    history.append(snapshot)
    task.attempt_history = history
    marker_ctx = dict(task.context or {})
    marker_ctx[TASK_CURRENT_ATTEMPT_ORDINAL_CONTEXT_KEY] = current_ordinal
    marker_ctx[TASK_LAST_BANKED_ATTEMPT_ORDINAL_CONTEXT_KEY] = current_ordinal
    task.context = marker_ctx

    total_vm_cost = float(task.vm_runtime_cost_banked or 0.0)
    task.estimated_cost_usd = round(total_vm_cost, 4) if total_vm_cost > 0 else None

    if clear_current_attempt:
        task.vm_provisioned_at = None
        task.vm_started_at = None
        task.vm_stopped_at = None
        task.api_call_summary = None
        # The attempt's provider-reported usage was banked into the snapshot
        # above; clear the live column so the next attempt's heartbeat starts
        # fresh instead of double-counting the closed attempt.
        task.provider_reported_usage = None

    return snapshot


class ProjectComputeImage(SQLModel, table=True):
    """Durable record of a baked per-project compute image (dep_hash -> digest).

    The image-build pipeline (``compute_backends.build_project_compute_image`` ->
    ``compute_image_build.build_and_push``) content-hashes a project's declared
    dependency files and bakes (or reuses) a digest-pinned compute image. This
    table persists the resulting ``(dependency-hash -> pinned reference/digest)``
    mapping so a later trigger can record and re-read what was built without
    re-running the pipeline.

    Additive record of what was built. Dispatch now reads it: when a
    compute_experiment is dispatched with no explicit ``container_image``,
    ``_dispatch_compute_experiment_directly`` resolves the project's manifest
    default against this table (via ``resolve_digest_pinned_default_image``) to a
    digest-pinned ref, matching the completion authoring path. Rows UPSERT on
    ``(project_id, dep_hash)`` — repeated builds for the same dependency hash
    update the single row in place rather than accumulating duplicates.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)
    # Short image name within the registry repo (e.g. "compute-myproj").
    image_name: str = Field()
    # Registry repo base the image was pushed to (e.g. "ghcr.io/org/repo").
    registry_repo: str = Field()
    # Content hash of the project's dependency inputs — the UPSERT key.
    dep_hash: str = Field(index=True)
    tag: str = Field()
    # Full reference from BuildResult: "registry/repo@sha256:..." or ":tag".
    pinned_ref: str = Field()
    # The "sha256:..." digest parsed out of ``pinned_ref`` when it is
    # digest-pinned (contains ``@``); NULL when the ref is only tag-pinned.
    digest: str | None = Field(default=None)
    # Whether this build freshly built the image vs reused an already-present one.
    built: bool = Field(default=False)
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, onupdate=_utcnow),
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id", "dep_hash", name="uq_projectcomputeimage_project_dep_hash"
        ),
    )


class ComputeJob(SQLModel, table=True):
    """Durable compute-job record for budgeted work decoupled from ``Task``.

    A compute job represents external execution capacity (VM, process, hosted
    backend job, or similar) with its own budget/deadline/cost/result lifecycle.
    It is always owned by a project and may be associated with a Task, but the
    nullable task link lets future budgeted work be tracked independently.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)
    # Detach-on-delete (SET NULL), NOT cascade: a compute job is durable history
    # that must outlive its originating task. Mirrors Alert.task_id /
    # SystemMaturity.source_task_id so bulk task cleanup never FK-violates.
    task_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(),
            ForeignKey("task.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )

    backend: str = Field(sa_column=Column(String(64), nullable=False))
    external_compute_id: str | None = Field(
        default=None, sa_column=Column(String(255), nullable=True)
    )
    machine_type: str | None = Field(
        default=None, sa_column=Column(String(128), nullable=True)
    )
    # Single-GPU accelerator request. ``accelerator_type`` is the canonical short
    # name (e.g. "T4"/"L4"/"V100"); ``accelerator_count`` defaults to 1 when an
    # accelerator is requested. Both are NULL for CPU-only jobs — the create path
    # leaves them unset unless a valid single-GPU accelerator is paired with a
    # GPU machine type, so existing CPU jobs are byte-for-byte unchanged.
    accelerator_type: str | None = Field(
        default=None, sa_column=Column(String(64), nullable=True)
    )
    accelerator_count: int | None = Field(
        default=None, sa_column=Column(Integer, nullable=True)
    )

    status: ComputeJobStatus = Field(
        default=ComputeJobStatus.QUEUED,
        sa_column=Column(
            String,
            nullable=False,
            server_default=ComputeJobStatus.QUEUED.value,
        ),
    )
    terminal_reason: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    outcome: ComputeJobOutcome | None = Field(
        default=None, sa_column=Column(String, nullable=True)
    )

    spend_cap_usd: float | None = Field(
        default=None, sa_column=Column(Float, nullable=True)
    )
    wall_clock_deadline: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )

    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            NaiveUTCDateTime,
            nullable=False,
            server_default=func.now(),
            onupdate=_utcnow,
        ),
    )
    started_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    completed_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    last_polled_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    next_poll_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    # Timestamp of the last heartbeat received from the VM running this job.
    # Set on the first POST to /api/agent/compute-jobs/{id}/heartbeat, which
    # transitions the job from STARTING to RUNNING without waiting for the GCP
    # insert LRO to complete. A non-null value is the authoritative signal that
    # the VM booted and is alive; the provisioning timeout ignores this job.
    last_heartbeat_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    poll_attempt_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )

    accrued_cost_usd: float | None = Field(
        default=None, sa_column=Column(Float, nullable=True)
    )
    final_cost_usd: float | None = Field(
        default=None, sa_column=Column(Float, nullable=True)
    )
    result_reference: str | None = Field(
        default=None, sa_column=Column(String(1024), nullable=True)
    )
    # Artifacts are task-owned and CASCADE-deleted with their task; this
    # reference must therefore detach (SET NULL) rather than block task cleanup
    # or be deleted out from under us.
    artifact_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(),
            ForeignKey("artifact.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )
    polling_metadata: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    # Stable operator-facing execution contract. New jobs persist the readable
    # authored source separately from any generated Base64/Bash transport so
    # clients never need to reverse-engineer wrappers. Nullable for legacy jobs.
    execution_manifest: Dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )
    # Placement and terminal execution facts observed after dispatch: actual
    # backend/runner, hardware attestation, exit/cancellation classification,
    # and canonical log availability. Kept separate from requested machine_type.
    runtime_provenance: Dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )
    # Authoritative record of the local runner that actually CLAIMED and ran this
    # job (stamped at /next-compute-job claim time). Detach-on-delete (SET NULL),
    # NOT cascade: a compute job is durable history that must outlive a deleted
    # runner — mirrors task_id / artifact_id above. Distinct from the best-effort
    # heartbeat active-list and the polling_metadata['runner_target_id'] dispatch
    # PIN, neither of which authoritatively records the runner that ran the job.
    ran_on_runner_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(),
            ForeignKey("localrunner.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )

    __table_args__ = (
        Index("ix_computejob_project_status", "project_id", "status"),
        Index("ix_computejob_project_status_created", "project_id", "status", "created_at"),
        Index("ix_computejob_task_status", "task_id", "status"),
        Index("ix_computejob_backend_external", "backend", "external_compute_id"),
        Index("ix_computejob_status_deadline", "status", "wall_clock_deadline"),
        Index("ix_computejob_next_poll", "next_poll_at"),
        Index("ix_computejob_created_at", "created_at"),
        Index("ix_computejob_updated_at", "updated_at"),
        Index("ix_computejob_completed_at", "completed_at"),
        Index(
            "ix_computejob_polling_metadata_runner_target_id",
            "polling_metadata",
        ),
    )

    project: Project = Relationship(back_populates="compute_jobs")
    task: Task | None = Relationship(back_populates="compute_jobs")


class ComputeJobFile(SQLModel, table=True):
    """Individually-addressable viewable file extracted from a compute job bundle.

    Each row records one file (png, jpg, webp, gif, pdf, mp4, webm) that was
    extracted from the job's opaque ``bundle.tar.gz`` at completion time, so it
    can be signed and rendered inline without downloading the full tarball.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    compute_job_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("computejob.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
    )
    safe_relpath: str = Field(
        sa_column=Column(String(512), nullable=False),
    )
    media_type: str = Field(
        sa_column=Column(String(16), nullable=False),
    )
    storage_uri: str = Field(
        sa_column=Column(String(1024), nullable=False, unique=True),
    )
    content_type: str | None = Field(
        default=None, sa_column=Column(String(128), nullable=True)
    )
    file_size_bytes: int | None = Field(
        default=None, sa_column=Column(Integer, nullable=True)
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )

    __table_args__ = (
        Index("ix_computejobfile_job_media", "compute_job_id", "media_type"),
        Index("ix_computejobfile_storage_uri", "storage_uri"),
    )

    compute_job: ComputeJob = Relationship()


class PoolVm(SQLModel, table=True):
    """Durable fleet-wide registry of pre-provisioned, credentialed, idle agent VMs.

    One row per warm-pool member. A pool VM is cold-booted *without* a task,
    pinned to a specific LLM account/provider/auth_mode/model baked into its
    boot secret manifest (see ``compute_manifest.build_taskless_agent_secret_manifest``),
    and registered here so the dispatch layer can later lease an IDLE member to
    a newly-queued task instead of cold-provisioning.

    Leasing uses ``SELECT ... FOR UPDATE SKIP LOCKED`` over real rows (NOT a JSON
    blob) so concurrent dispatchers can't double-claim a member — mirroring the
    backlog-reuse selection in ``warm_backlog_reuse``. The lease helper only
    returns a row whose pinned identity is compatible with the candidate task's
    required account/provider/model AND whose ``project_id`` matches (a hard
    tenant-isolation gate). This table is INFRASTRUCTURE — nothing leases or
    dispatches to it until the dispatch hook / maintainer land (sibling tasks);
    it is inert behind the ``warm_pool_enabled`` runtime flag (default False).
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # Unique GCP instance name; the natural key the dispatch/heartbeat paths use.
    vm_instance_id: str = Field(
        sa_column=Column(String, nullable=False, unique=True, index=True)
    )
    # Hard tenant-isolation boundary: a member only ever serves tasks from this
    # project. Indexed (with state) for the lease hot path.
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)

    # Pinned LLM identity frozen into the VM's boot secret manifest. The lease
    # gate requires these to be compatible with the candidate task's resolved
    # account/provider/model — a reused VM runs on its boot credentials.
    account_id: str | None = Field(
        default=None, sa_column=Column(String, nullable=True)
    )
    provider: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    auth_mode: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    model: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    # Identifies the agent image the VM booted on, so a stale-image member can be
    # retired rather than leased after a rebake.
    agent_image_fingerprint: str | None = Field(
        default=None, sa_column=Column(String, nullable=True)
    )
    # Durable image identity observed from task / VM heartbeats so the fleet can
    # still be reasoned about after an orchestrator restart (the in-memory
    # heartbeat caches are intentionally lossy).
    agent_image_name: str | None = Field(
        default=None, sa_column=Column(String, nullable=True)
    )
    agent_image_build_ts: int | None = Field(
        default=None, sa_column=Column(Integer, nullable=True)
    )
    agent_runtime_fingerprint: str | None = Field(
        default=None, sa_column=Column(String(128), nullable=True)
    )
    agent_source_sha: str | None = Field(
        default=None, sa_column=Column(String(64), nullable=True)
    )
    last_heartbeat_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )

    state: PoolVmState = Field(
        default=PoolVmState.PROVISIONING,
        sa_column=Column(
            String,
            nullable=False,
            server_default=PoolVmState.PROVISIONING.value,
        ),
    )
    # When the member last entered IDLE; the idle reaper / maintainer (sibling
    # task) uses this to age out members that nobody leased.
    idle_since: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    # The task currently holding the lease (cleared on release). Plain GUID, no
    # FK — a leased task may be archived/deleted independently of the registry.
    leased_task_id: uuid.UUID | None = Field(
        default=None, sa_column=Column(GUID(), nullable=True)
    )
    lease_expires_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    # Idle-VM cost accumulator. Idle pool members still bill GCE wall-clock; this
    # banks that cost onto the row so cost accounting (sibling task) can attribute
    # it later WITHOUT a second model change.
    idle_cost_banked: float | None = Field(
        default=None, sa_column=Column(Float, nullable=True)
    )

    # Lane-scoped workspace reservation (improvement lanes, d-5fd69fe1). When
    # released from a lane phase, the member returns IDLE stamped with the lane's
    # id so the lane's next phase can reclaim its warm workspace. Plain GUID — no
    # FK, mirrors leased_task_id — so a lane row can be archived independently.
    reserved_lane_id: uuid.UUID | None = Field(
        default=None, sa_column=Column(GUID(), nullable=True, index=True)
    )
    # SOFT reservation expiry. The maintainer's liveness-aware sweep may RE-STAMP
    # this forward (extend) while the owning lane is still awaiting its fix, so it
    # is NOT a reliable anchor for the absolute lifetime cap.
    reservation_expires_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    # HARD absolute ceiling on a reservation's total lifetime. Stamped ONCE at
    # reservation creation (release_pool_vm) and never re-stamped, so liveness
    # extension of reservation_expires_at can never exceed it. The sweep ALWAYS
    # frees the member once this elapses, so a stuck/zombie lane can never pin a
    # warm VM indefinitely (pool-capacity protection).
    reservation_hard_expires_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )

    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            NaiveUTCDateTime,
            nullable=False,
            server_default=func.now(),
            onupdate=_utcnow,
        ),
    )

    __table_args__ = (
        # Lease hot path: WHERE project_id = ? AND state = 'IDLE' AND <identity>.
        Index("ix_poolvm_project_state", "project_id", "state"),
        # Standalone index for queries filtering only on state without project_id
        # (warm_pool.py dispatcher queries PoolVm.state IN (...) without project_id).
        Index("ix_poolvm_state", "state"),
        # Index for JOINs on leased_task_id (warm_pool.py dispatching JOIN path).
        Index("ix_poolvm_leased_task_id", "leased_task_id"),
        # Index for idle-sweeper ordering/filtering on idle_since.
        Index("ix_poolvm_idle_since", "idle_since"),
        # Reversed composite for per-project-by-state queries (warm_pool.py
        # lines 244-247, 302, 341-344) where state is the leading column.
        Index("ix_poolvm_state_project", "state", "project_id"),
        # Composite index for the lease hot path: filters on project_id, state,
        # AND identity columns (account_id, provider, auth_mode, model) so the
        # query planner can do an index-only scan instead of sequential scan +
        # in-memory filtering.
        Index("ix_poolvm_lease_compat", "project_id", "state", "account_id", "provider", "auth_mode", "model"),
    )


class PoolIdleCostLedger(SQLModel, table=True):
    """Durable carry-forward of a retired pool member's banked idle cost.

    A ``PoolVm`` row carries its accrued idle spend in ``idle_cost_banked`` while
    it lives, but the idle reaper HARD-DELETES the row on teardown (after the
    1800s idle-TTL). Efficiency snapshots run once per UTC day, so the vast
    majority of members are provisioned, accrue idle cost, and are deleted
    entirely between two snapshots — their spend would vanish, making
    ``cost_per_merged_pr_incl_pool_idle`` systematically undercount.

    To honor the documented invariant "teardown never erases measured idle
    spend" (``efficiency_snapshot.compute_pool_idle_cost``), the reaper writes
    one append-only ledger row carrying the member's banked cost in the SAME
    transaction that deletes the registry row, and ``compute_pool_idle_cost``
    sums these rows into ``settled_idle_cost_usd``. Append-only INSERT is
    concurrency-safe (no row lock / accumulator contention).
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # Tenant the retired member served — lets the snapshot scope the carry-forward
    # to a project (mirroring compute_pool_idle_cost's optional project filter).
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)
    # The retired VM's natural key (audit / dedup trail; not unique — defensive).
    vm_instance_id: str = Field(sa_column=Column(String, nullable=False))
    # Banked idle USD carried forward from the deleted PoolVm row.
    idle_cost_usd: float = Field(sa_column=Column(Float, nullable=False))
    # Teardown reason (pool_idle_ttl / pool_zombie_no_heartbeat / ...), for audit.
    reason: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    retired_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )


class PoolIdleSpan(SQLModel, table=True):
    """Durable per-settled-idle-span log for windowed pool idle cost attribution.

    Written by settle_pool_vm_idle_cost (when a session is provided) each time
    an idle span is banked onto PoolVm.idle_cost_banked, and by
    retire_and_delete_pool_vm for the final idle span at teardown.  Enables
    _compute_windowed_idle_cost to filter on settled_at (exact span end) rather
    than on PoolIdleCostLedger.retired_at (member teardown time), eliminating
    lump-attribution distortion for long-lived members with multiple idle cycles.
    Append-only INSERT is concurrency-safe (no accumulator contention).
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)
    vm_instance_id: str = Field(sa_column=Column(String, nullable=False))
    # Exact start/end of this idle span.
    started_at: datetime = Field(sa_column=Column(NaiveUTCDateTime, nullable=False))
    settled_at: datetime = Field(
        sa_column=Column(NaiveUTCDateTime, nullable=False, index=True)
    )
    cost_usd: float = Field(sa_column=Column(Float, nullable=False))
    reason: str | None = Field(default=None, sa_column=Column(String, nullable=True))


class TaskHistory(SQLModel, table=True):
    """
    Audit trail for task modifications, splits, and spawns.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    task_id: uuid.UUID = Field(foreign_key="task.id", index=True)

    timestamp: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    action: str  # e.g., "STATUS_CHANGE", "SPLIT", "SPAWNED_CHILD", "MODIFIED_PAYLOAD"
    details: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))

    task: Task = Relationship(back_populates="history")

    # Composite index covering the common query pattern:
    #   WHERE task_id IN (...) ORDER BY timestamp DESC LIMIT N
    # Without this PostgreSQL sorts all matching rows per task_id before
    # applying LIMIT; the composite index avoids the sort.
    __table_args__ = (
        Index("ix_taskhistory_task_id_timestamp_desc", "task_id", text("timestamp desc")),
    )


# Auto-append to status_log whenever latest_status_message is set
@event.listens_for(Task.latest_status_message, "set")
def _on_status_message_set(target: Task, value, oldvalue, initiator):
    if value and value != oldvalue:
        entry = {"t": _utcnow().isoformat(), "msg": str(value)[:500]}
        try:
            target.status_log = ((target.status_log or []) + [entry])[-90:]
        except Exception as e:  # noqa: BLE001
            # Reading target.status_log fires a sync lazy-load when the
            # attribute is expired (most common cause: the session was
            # rolled back by an upstream DB error, expiring every loaded
            # instance). Under an async session that raises
            # MissingGreenlet; if the underlying asyncpg connection is
            # also dead it raises InterfaceError. Either way the
            # status_log audit append is best-effort — the
            # latest_status_message SET itself still persists via the
            # outer assignment, so we swallow and skip the append rather
            # than crashing the caller's whole transaction.
            # Production 2026-05-08: orchestrator.py:4743 cascade.
            logger.debug(
                "[MODELS] _on_status_message_set: skipped status_log append (%s)",
                e,
            )


# Callbacks invoked when a task reaches a terminal state.
# main.py registers _terminated_task_ids.add here to revoke agent JWTs.
_on_task_terminal_callbacks: list = []

# Callbacks invoked when a task is revived from a terminal state OR set to
# any non-terminal status. Used to clear any in-memory deny-lists populated
# by terminal callbacks (notably ``_terminated_task_ids`` in agent.py).
#
# Production 2026-05-05: task 3d4bc05a was stuck in a claim 401 loop:
#   1. A prior in-flight transition set status=FAILED (in memory) — fired
#      terminal callback → ``_terminated_task_ids[task_id] = now``.
#   2. The transaction was rolled back (e.g. agent_status playbook block
#      poisoned the session), reverting the DB but leaving the cache
#      populated.
#   3. The reaper later requeued the task to QUEUED, the orchestrator
#      committed PROVISIONING, but the cache stayed set — so every new
#      VM's ``/api/agent/claim`` hit ``_terminated_task_ids`` and got 401
#      "Task is no longer active", looping forever until the orchestrator
#      revision restarted (wiping the in-memory cache).
#
# Firing revive callbacks on every non-terminal status set restores the
# cache invariant so the agent can claim cleanly.
_on_task_revive_callbacks: list = []


def notify_tasks_terminated(task_ids: list) -> None:
    """Fire terminal callbacks for a list of task IDs.

    Used by bulk UPDATE sites (e.g. force-delete) that bypass the ORM event
    listener, ensuring the in-memory deny-list is populated for all cancelled
    tasks exactly as if each transition had gone through the ORM setter.
    """
    for task_id in task_ids:
        tid = str(task_id)
        for cb in _on_task_terminal_callbacks:
            try:
                cb(tid)
            except Exception as e:  # noqa: PERF203
                logger.error(
                    f"[MODELS] Terminal callback failed for task {tid}: {e}",
                    exc_info=True,
                )


@event.listens_for(_OrmSession, "before_flush")
def _emit_jwt_revocation_on_terminal(session, flush_context, instances):
    """Emit pg_notify for Task rows crossing the terminal boundary.

    Two symmetric transitions are broadcast cross-instance so every Cloud Run
    instance keeps its in-memory ``_terminated_task_ids`` deny-list coherent:

    * non-terminal → terminal  → ``task_terminated``  (deny-list ADD)
    * terminal     → non-terminal (requeue/revive) → ``task_revived`` (EVICT)

    The revive broadcast closes an asymmetry: the in-process
    ``_on_task_revive_callbacks`` path (see ``_on_status_set``) only evicts on
    the single instance that handled the requeue flush, so a requeued task's
    new VM heartbeat could load-balance to a stale instance and get a spurious
    401 — dooming the whole retry until the 75-min deny-list TTL. The revive
    NOTIFY mirrors the proven terminal path so every instance evicts.

    Guardrail: revive only fires on a REAL terminal→non-terminal transition
    (prior status was terminal), never on ordinary non-terminal lifecycle
    steps (QUEUED→PROVISIONING→IN_PROGRESS…), to avoid notify spam.

    Runs inside the before_flush hook (sync context, greenlet-driven by
    AsyncSession.flush) so the NOTIFY lands in the same transaction as the
    status mutation.  Postgres only delivers the notification to listeners
    after commit — so receivers see the change as durable.

    Uses a late import to break the circular dependency:
      agent.py → models.py → jwt_revocation.py → agent.py
    Exceptions are fully swallowed so a NOTIFY failure never aborts the flush.
    """
    try:
        from sqlalchemy import inspect as _sa_inspect
        from app.jwt_revocation import (
            emit_task_revived_notify,
            emit_task_terminated_notify,
        )

        for instance in session.dirty:
            if not isinstance(instance, Task):
                continue
            history = _sa_inspect(instance).attrs.status.history
            if not history.added:
                continue
            new_status = history.added[0]
            # Expiry-safe pk read: post-rollback recovery paths (e.g. the
            # smoketest spawn retry) flush a Task whose attributes were
            # expired by the rollback. Reading ``instance.id`` on such an
            # instance triggers a sync lazy-load inside before_flush; the
            # load blows up (MissingGreenlet under the async engine) AND
            # poisons the flush's transaction, failing the whole flush the
            # handler promises never to block. The identity key is populated
            # for every persistent instance and needs no IO.
            _ident = _sa_inspect(instance).identity
            _instance_pk = (
                _ident[0] if _ident else instance.__dict__.get("id")
            )
            if _instance_pk is None:
                continue
            if task_status_releases_agent_resources(new_status):
                emit_task_terminated_notify(session, str(_instance_pk))
            elif history.deleted and task_status_releases_agent_resources(
                history.deleted[0]
            ):
                # Real terminal → non-terminal transition (requeue/revive): the
                # prior status was terminal and the new one is not. Broadcast so
                # every instance evicts its stale deny-list entry. The guard on
                # the PRIOR status keeps normal non-terminal steps silent.
                emit_task_revived_notify(session, str(_instance_pk))
    except Exception as exc:  # noqa: BLE001 — never block the flush
        logger.warning("[MODELS] before_flush NOTIFY failed: %s", exc)


@event.listens_for(Task, "before_insert")
def _stamp_task_provider_floor(mapper, connection, target: Task):
    """Persist the namespaced provider floor marker onto every new task.

    The matcher layer also derives this marker on the fly, but stamping it into
    ``context['provides_capability']`` ensures persisted rows are never silent
    non-providers and keeps future read paths JSONB-compatible without extra
    task-type wiring. Fail-open by construction: a stamp failure must never
    abort the insert.
    """
    try:
        derived = derive_provider_marker(target)
        if not derived:
            return
        existing_ctx = target.context if isinstance(target.context, dict) else {}
        # Treat EITHER accepted marker key as already-providing.  If the task
        # already advertises an explicit capability via `provides_capability` OR
        # `capability`, leave the stored value completely untouched — the floor
        # must never clobber or augment an explicit stamp.
        already_provides = _coerce_provider_context_values(
            existing_ctx.get("provides_capability")
        ) or _coerce_provider_context_values(existing_ctx.get("capability"))
        if already_provides:
            return
        new_ctx = dict(existing_ctx)
        new_ctx["provides_capability"] = derived
        target.context = new_ctx
    except Exception as exc:  # noqa: BLE001 — never block the flush
        logger.warning("[MODELS] before_insert provider-floor stamp failed: %s", exc)


def _terminal_time_from_status_log(target: Task) -> datetime | None:
    """Best-effort terminal timestamp from the last ``status_log`` entry.

    Mirrors ``admin_analytics._derive_completed_at``'s status_log tail read so a
    task inserted already-terminal records the SAME instant those helpers would
    otherwise derive from the log. Returns a naive-UTC datetime, or None when the
    log is empty/malformed.
    """
    log = target.status_log
    if not log:
        return None
    last = log[-1]
    if not isinstance(last, dict):
        return None
    ts = last.get("t")
    if not isinstance(ts, str):
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed


@event.listens_for(Task, "before_insert")
def _stamp_task_completed_at_on_insert(mapper, connection, target: Task):
    """Persist completion time for terminal tasks constructed already-terminal.

    Derives the stamp from the task's own terminal signal — the last
    ``status_log`` entry's timestamp, else ``created_at`` — rather than
    wall-clock now, so a task inserted already-terminal (a backfill, a replayed
    row, or a backdated test fixture) records its ACTUAL completion time. This
    mirrors the transition path, where ``_on_status_set`` stamps ``completed_at``
    with the same instant it appends to ``status_log``, keeping the durable
    column and the log tail consistent for downstream terminal-time helpers.
    """
    try:
        if (
            target.status in TASK_OUTCOME_TERMINAL_STATUSES
            and target.completed_at is None
        ):
            target.completed_at = (
                _terminal_time_from_status_log(target)
                or target.created_at
                or _utcnow()
            )
    except Exception as exc:  # noqa: BLE001 — never block the flush
        logger.warning(
            "[MODELS] before_insert completed_at stamp failed: %s",
            exc,
        )


@event.listens_for(Task.status, "set")
def _on_status_set(target: Task, value, oldvalue, initiator):
    is_terminal = value in TASK_OUTCOME_TERMINAL_STATUSES
    was_terminal = oldvalue in TASK_OUTCOME_TERMINAL_STATUSES
    status_changed = bool(value and isinstance(oldvalue, str) and value != oldvalue)
    if status_changed:
        now = _utcnow()
        entry = {
            "t": now.isoformat(),
            "msg": f"Status changed: {oldvalue} → {value}",
        }
        try:
            target.status_log = ((target.status_log or []) + [entry])[-90:]
        except Exception as e:  # noqa: BLE001
            # Best-effort audit append — see _on_status_message_set for
            # the expired-attribute / dead-connection rationale.
            logger.debug(
                "[MODELS] _on_status_set: skipped status_log append (%s)",
                e,
            )
        # Explicitly stamp updated_at so any transition path that leaves the
        # column null (e.g. a load_only fetch that never loaded the column, or
        # a code path that bypassed onupdate) is self-healing.  The reaper
        # staleness sweep filters on updated_at < cutoff, so a null value makes
        # the task invisible to the sweep and it orphans forever.
        try:
            target.updated_at = now
        except Exception as e:  # noqa: BLE001
            logger.debug(
                "[MODELS] _on_status_set: skipped updated_at stamp (%s)",
                e,
            )
    else:
        now = _utcnow()
    if is_terminal:
        try:
            if target.completed_at is None:
                target.completed_at = now
        except Exception as e:  # noqa: BLE001
            logger.debug(
                "[MODELS] _on_status_set: skipped completed_at stamp (%s)",
                e,
            )
    elif was_terminal:
        try:
            if target.completed_at is not None:
                target.completed_at = None
        except Exception as e:  # noqa: BLE001
            logger.debug(
                "[MODELS] _on_status_set: skipped completed_at clear (%s)",
                e,
            )
    if not status_changed:
        return
    task_id = str(target.id)
    # Notify JWT revocation when task reaches a resource-releasing state.
    # BLOCKED/PARKED are recoverable, but both release the active agent/VM
    # binding and must revoke the old token until a fresh QUEUED claim revives it.
    if task_status_releases_agent_resources(value):
        for cb in _on_task_terminal_callbacks:
            try:
                cb(task_id)
            except Exception as e:  # noqa: PERF203
                logger.error(
                    f"[MODELS] Terminal callback failed for task {task_id}: {e}",
                    exc_info=True,
                )
    else:
        # Non-terminal status set (QUEUED, PROVISIONING, IN_PROGRESS,
        # EVALUATING). Fire revive callbacks so any deny-list entries
        # populated by an earlier terminal transition are cleared.
        for cb in _on_task_revive_callbacks:
            try:
                cb(task_id)
            except Exception as e:  # noqa: PERF203
                logger.error(
                    f"[MODELS] Revive callback failed for task {task_id}: {e}",
                    exc_info=True,
                )


class AlertSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class Alert(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)
    severity: AlertSeverity = Field(
        default=AlertSeverity.ERROR,
        sa_column=Column(String, default=AlertSeverity.ERROR),
    )
    title: str
    message: str
    domain: str = Field(
        default="core",
        sa_column=Column(String, nullable=False, server_default="core"),
    )
    event_type: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    source: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    dedupe_key: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    occurrence_count: int = Field(
        default=1,
        sa_column=Column(Integer, nullable=False, server_default="1"),
    )
    last_seen_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    # Audit 2026-04-29 (M-S3): FK to task.id with ondelete=SET NULL added in
    # migration 0056. Alert messages survive parent-task GC for the admin UI.
    task_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(ForeignKey("task.id", ondelete="SET NULL"), nullable=True),
    )
    # TIMESTAMPTZ + server_default (migration 0061 / audit H-S4 + H-S5).
    # NaiveUTCDateTime strips tz on read (round 2 audit / C3 + H1).
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    dismissed_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    # Operator acknowledgement: set when the operator acknowledges an ongoing
    # outage alert (e.g. runner offline) to suppress further escalation re-pages
    # until the alert auto-resolves on recovery. Distinct from dismissed_at: the
    # alert remains visible; only escalation webhooks are gated on this field.
    acknowledged_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    # Persisted escalation cadence for the periodic re-page sweep
    # (_maybe_escalate_runner_deactivated_alerts). None = never escalated by the
    # sweep (first re-page fires after _RUNNER_ESCALATION_FIRST_SECONDS); set to
    # now() after each sweep dispatch so cadence survives orchestrator restart.
    # Distinct from last_seen_at, which alert_policy owns for dedup tracking.
    last_escalated_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

    __table_args__ = (
        Index("ix_alert_task_id_dismissed", "task_id", "dismissed_at"),
        Index("ix_alert_project_id_dismissed", "project_id", "dismissed_at"),
        Index("ix_alert_severity", "severity"),
        Index("ix_alert_project_severity", "project_id", "severity"),
        Index("ix_alert_project_domain", "project_id", "domain"),
        Index("ix_alert_project_event_type", "project_id", "event_type"),
        # Cross-project runner-health monitors scan event_type + dismissed_at
        # WITHOUT a project_id predicate (runner_health_monitors.py,
        # runner_health_alerts.py), so the project-prefixed indexes above cannot
        # serve them.  Leading column event_type turns each recurring scan into an
        # index range walk instead of a severity-only filter + heap scan.
        Index("ix_alert_event_type_dismissed", "event_type", "dismissed_at"),
        Index("ix_alert_project_source", "project_id", "source"),
        Index("ix_alert_project_created_at", "project_id", "created_at"),
        # title is filtered in dedupe/existence WHERE clauses across many alert
        # call sites; index it alongside project_id to avoid a seq-scan after
        # the project_id filter on every check.
        Index("ix_alert_project_title", "project_id", "title"),
        Index(
            "ux_alert_active_dedupe_key",
            "project_id",
            "dedupe_key",
            unique=True,
            postgresql_where=text("dedupe_key IS NOT NULL AND dismissed_at IS NULL"),
            sqlite_where=text("dedupe_key IS NOT NULL AND dismissed_at IS NULL"),
        ),
    )


# Keys used in StrategicCycle.cycle_metadata for per-cycle memo-pathway tracking.
# Stored as plain integers so operators and digest endpoints can read them without
# knowing the exact key strings.
CYCLE_META_MEMOS_AVAILABLE = "memos_available"
CYCLE_META_MEMOS_CITED = "memos_cited"
CYCLE_META_KNOWLEDGE_AVAILABLE = "knowledge_available"
CYCLE_META_KNOWLEDGE_CITED = "knowledge_cited"
CYCLE_META_INTEGRITY_CONFLICT_COUNTS = "integrity_conflict_counts"
# Pre-spawn effort tier a strategic_review cycle was spawned at (routine /
# consequential / pinned), plus the classifier reason. Written at spawn time so
# cost analysis can distinguish cheapened routine cycles without re-deriving
# the classifier.
CYCLE_META_EFFORT_TIER = "effort_tier"
CYCLE_META_EFFORT_REASON = "effort_tier_reason"


class StrategicCycle(SQLModel, table=True):
    """Strategic-research cycle record.

    Audit 2026-04-25 W2 (project audit_pipeline_2026_04_25): cycles previously
    lived in a 10-entry sliding JSONB list on ``Project.vision._strategic_cycle_history``.
    The cap silently dropped old entries — including ones stuck
    ``outcome="in_progress"`` from phase-1 crashes that never reached the
    completion handler — so the strategic agent had no diagnostic signal that
    cycles were failing.

    The cycle has two phases:
      - ``phase1`` (design_doc_update): the architecture-analysis task
      - ``phase2`` (strategic_review): the vision/direction-update task
    Phase 2 spawns synchronously on phase 1 completion (see
    ``task_completion._spawn_strategic_review``); the lifecycle hook in
    ``task_lifecycle`` is a backstop that still updates the cycle row when
    the synchronous chain crashes mid-flight.
    """

    id: str = Field(primary_key=True, max_length=36)  # cycle_id (UUID hex)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)

    # outcome enum-ish: in_progress | phase1_running | phase1_done |
    # phase2_running | completed | failed_phase1 | failed_phase2 |
    # stalled_phase1 | stalled_phase2 | failed_phase2_spawn
    outcome: str = Field(
        default="in_progress",
        sa_column=Column(String(32), nullable=False, server_default="in_progress"),
    )

    # Phase tracking. *_started_at = task creation time;
    # *_completed_at = terminal status.
    phase1_task_id: uuid.UUID | None = Field(default=None, foreign_key="task.id", index=True)
    phase1_started_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    phase1_completed_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    phase2_task_id: uuid.UUID | None = Field(default=None, foreign_key="task.id", index=True)
    phase2_started_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    phase2_completed_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

    # Outcome accounting (filled on phase 2 completion).
    directions_created: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    directions_retired: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    tasks_spawned: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    vision_updated: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )

    # Free-form text fields surfaced to the UI / next cycle's context.
    cycle_reasoning: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    summary_text: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    failure_reason: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )

    # Per-direction maps, baseline_vision snapshot for 3-way merge, etc.
    # No server_default — Python default_factory keeps it consistent across
    # SQLite (test) and Postgres (prod) without per-dialect cast syntax.
    cycle_metadata: Dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False, default=dict)
    )

    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, onupdate=_utcnow),
    )

    __table_args__ = (
        Index("ix_strategiccycle_project_created", "project_id", "created_at"),
        # Covers the SR-trend hot query run during claim_task enrichment:
        # WHERE project_id=? AND outcome='completed'
        # ORDER BY phase1_started_at DESC LIMIT 4. Without it that query
        # full-scanned + sorted every completed cycle and hit the 120s
        # statement_timeout, poisoning the claim session (incident 2026-05-29).
        # See migration 0128.
        Index(
            "ix_strategiccycle_project_outcome_phase1",
            "project_id",
            "outcome",
            "phase1_started_at",
        ),
        # Exact latest-terminal Resident wakeup lookup. ``outcome`` is a
        # multi-value predicate, so placing it before updated_at would force a
        # merge/sort across outcome ranges instead of serving ORDER BY + LIMIT
        # directly from this partial index.
        Index(
            "ix_strategiccycle_project_terminal_updated",
            "project_id",
            "updated_at",
            "id",
            postgresql_where=text(
                "outcome IN ('completed','failed_phase1','failed_phase2',"
                "'failed_phase2_spawn','stalled_phase1','stalled_phase2',"
                "'thinker_artifacts_missing')"
            ),
            sqlite_where=text(
                "outcome IN ('completed','failed_phase1','failed_phase2',"
                "'failed_phase2_spawn','stalled_phase1','stalled_phase2',"
                "'thinker_artifacts_missing')"
            ),
        ),
        # Covers the stalled-cycle reaper cross-project scan:
        # WHERE outcome IN (...) ORDER BY phase1_started_at LIMIT N.
        # The two project_id-led indexes above cannot serve this query.
        Index(
            "ix_strategiccycle_outcome_phase1_started",
            "outcome",
            "phase1_started_at",
        ),
    )

    @property
    def completed_at(self) -> datetime | None:
        """Effective cycle completion time: phase2 if available, else phase1, else updated_at."""
        return self.phase2_completed_at or self.phase1_completed_at or self.updated_at

    def cycle_ref(self) -> Dict[str, Any]:
        """Compact dict for embedding in stored results and API responses."""
        return {
            "id": self.id,
            "started_at": self.phase1_started_at.isoformat()
            if self.phase1_started_at
            else None,
            "completed_at": self.completed_at.isoformat()
            if self.completed_at
            else None,
        }

    def memo_stats(self) -> Dict[str, int]:
        """Return per-cycle memo counts from cycle_metadata.

        Keys match CYCLE_META_MEMOS_AVAILABLE / CYCLE_META_MEMOS_CITED constants.
        Returns zeros when not yet recorded (cycle in progress or pre-feature rows).
        """
        meta = self.cycle_metadata or {}
        return {
            CYCLE_META_MEMOS_AVAILABLE: int(meta.get(CYCLE_META_MEMOS_AVAILABLE) or 0),
            CYCLE_META_MEMOS_CITED: int(meta.get(CYCLE_META_MEMOS_CITED) or 0),
        }


class StrategicSpawnRequest(SQLModel, table=True):
    """Durable, idempotent intent to dispatch one task from Strategic.

    Persisting intent before admission means a transient capacity/abuse guard can
    defer work without losing its specification or making the agent rediscover
    the request by retrying. ``idempotency_key`` is scoped to the originating
    Strategic task; the eventual child task link detaches on task deletion so
    the request remains useful audit history.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)
    parent_task_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(),
            ForeignKey("task.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )
    idempotency_key: str = Field(
        sa_column=Column(String(128), nullable=False),
    )
    task_type: str = Field(sa_column=Column(String(64), nullable=False))
    specification: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    specification_hash: str = Field(
        sa_column=Column(String(64), nullable=False),
    )
    direction_id: str | None = Field(
        default=None, sa_column=Column(String(128), nullable=True)
    )
    discovery_cycle_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(),
            ForeignKey("discoverycycle.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )
    state: str = Field(
        default="accepted",
        sa_column=Column(String(32), nullable=False, server_default=text("'accepted'")),
    )
    admission_reason: str | None = Field(
        default=None, sa_column=Column(String(255), nullable=True)
    )
    policy_snapshot: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    not_before: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    task_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(),
            ForeignKey("task.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            NaiveUTCDateTime,
            nullable=False,
            server_default=func.now(),
            onupdate=_utcnow,
        ),
    )
    dispatched_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )

    __table_args__ = (
        UniqueConstraint(
            "parent_task_id",
            "idempotency_key",
            name="uq_strategicspawnrequest_parent_idempotency",
        ),
        Index(
            "ix_strategicspawnrequest_state_notbefore_created",
            "state",
            "not_before",
            "created_at",
        ),
        Index(
            "ix_strategicspawnrequest_project_state",
            "project_id",
            "state",
        ),
    )


class StrategicWatch(SQLModel, table=True):
    """One-shot durable receipt for a task reaching an actionable boundary.

    This is deliberately not ``TaskWakeRegistration``: a Resident watch must not
    park or advance the long-lived Strategic task. The existing heartbeat detects
    world-state movement; this row preserves which exact task mattered and keeps
    the fired receipt visible until Strategic acknowledges it. Knowledge tasks
    resolve at agent terminal; write tasks resolve only once their work lands or
    becomes blocked, fails, needs approval, or completes without a PR.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)
    created_by_task_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(),
            ForeignKey("task.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )
    strategic_session_id: str | None = Field(
        default=None, sa_column=Column(String(128), nullable=True)
    )
    target_task_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("task.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
    )
    purpose: str = Field(default="", sa_column=Column(String(1000), nullable=False))
    state: str = Field(
        default="pending",
        sa_column=Column(String(32), nullable=False, server_default=text("'pending'")),
    )
    terminal_status: str | None = Field(
        default=None, sa_column=Column(String(32), nullable=True)
    )
    receipt: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    deadline_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    fired_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    acknowledged_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    acknowledgement_disposition: str | None = Field(
        default=None, sa_column=Column(String(32), nullable=True)
    )
    acknowledgement_note: str | None = Field(
        default=None, sa_column=Column(String(1000), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            NaiveUTCDateTime,
            nullable=False,
            server_default=func.now(),
            onupdate=_utcnow,
        ),
    )

    __table_args__ = (
        UniqueConstraint(
            "created_by_task_id",
            "target_task_id",
            name="uq_strategicwatch_creator_target",
        ),
        Index("ix_strategicwatch_project_state", "project_id", "state"),
        Index(
            "ix_strategicwatch_state_deadline",
            "state",
            "deadline_at",
        ),
    )


class OrchestratorErrorAck(SQLModel, table=True):
    """Persistent acknowledgement of an orchestrator error bucket.

    key is "exception_class|source" for a specific bucket, or "*" for a
    global ack covering every bucket whose last_seen_iso <= acknowledged_at.
    """

    key: str = Field(primary_key=True, max_length=255)
    acknowledged_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, index=True),
    )
    acknowledged_by: str = Field(default="")


# Maximum stored length for operator/user-authored message content and
# resolution notes. Shared by the admin-notifications and project-directions
# message endpoints so their over-limit rejection cannot drift apart.
MESSAGE_CONTENT_MAX = 20000


class DigestCacheEntry(SQLModel, table=True):
    """Cross-instance shared L2 cache for precomputed digest / health payloads.

    Written by the leader-only digest precompute worker (one row per hot key) and
    read by every Cloud Run instance with a single primary-key SELECT when its
    in-process L1 (``DigestPrecomputeCache``) misses — i.e. on non-leader
    instances and on post-deploy cold starts. This replaces the per-request
    5000-row ``_compute_digest`` / ``_compute_health_overview`` scan on those
    instances (prod 2026-06-03 statement_timeout 500s) with a cheap keyed read,
    at no new infra cost (lives on the existing Cloud SQL instance). See
    ``app.digest_precompute`` for the read/write helpers and the freshness model.

    ``cache_key`` is ``"{kind}:{window}:{project_id|global}"`` (kind ∈
    {digest, health}). ``payload`` is the byte-identical compute output; readers
    serve it only while ``computed_at`` is within
    ``PRECOMPUTE_STALE_FALLBACK_SECONDS`` and otherwise fall back to the existing
    on-demand path. The table is created on Postgres by alembic 0140 (renumbered
    from its original 0138 by the main -> automain back-merge) and on the
    SQLite test fixture by ``SQLModel.metadata.create_all``.
    """

    # Explicit name: SQLModel would otherwise default to the lowercased class
    # name (``digestcacheentry``), which would NOT match the alembic-created
    # ``digestcache`` table.
    __tablename__ = "digestcache"

    cache_key: str = Field(sa_column=Column(String, primary_key=True))
    payload: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))
    computed_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    source: str = Field(
        default="worker",
        sa_column=Column(String, nullable=False, server_default="worker"),
    )


class VmContact(SQLModel, table=True):
    """Uncontended liveness signal: the last time the orchestrator heard from a VM.

    This exists ONLY to break the false-reap interaction between the heartbeat
    endpoint and the rollup. The heartbeat handler updates the contended Task
    row under ``with_for_update(skip_locked=True)``; when the rollup
    (``rollup_management`` locks all sibling children FOR UPDATE) holds that
    row, the heartbeat is skipped → ``Task.last_heartbeat`` never advances even
    though the agent IS alive and calling in. The reaper reads only
    ``Task.last_heartbeat``, so a lock-starved-but-live VM is misclassified as
    dead and reaped (AGENT_TIMEOUT) while its work is mid-flight.

    The fix records every backend contact from a VM onto its OWN row here,
    keyed on ``vm_instance_id`` — never the contended Task row — so the write
    can never be blocked by the rollup's task-row lock. The reaper then treats
    a ``last_contact_at`` within the SAME heartbeat-stale cutoff as proof the
    agent is alive and suppresses the reap; a genuinely dead agent (crashed /
    preempted / OOM — sends no requests) leaves this row stale or absent and is
    reaped on the existing schedule. One row per VM, upserted on every contact.

    ``last_tool_use_at`` (added by alembic 0144) carries the most recent
    tool-use timestamp the orchestrator heard from this VM, recorded OFF the
    contended Task row for the SAME reason as ``last_contact_at``: a heartbeat
    whose contended ``Task`` write is lock-starved also drops
    ``Task.last_tool_use_at``, so the reaper's progress-stall branch can
    false-reap a live PROGRESSING agent. The reaper consults this off-row
    stamp before reaping a suspected stall: a tool-use more recent than the
    progress-stale cutoff proves the agent is making tool calls (its on-row
    stamp was merely lock-dropped), so the stall reap is suppressed. A
    genuinely stalled agent (no tool calls anywhere) leaves this stale/None
    and is still reaped. Nullable: heartbeats without tool telemetry (or pre-
    backfill rows) leave it None, which falls back to the on-row behaviour.

    Created on Postgres by alembic 0142 (base table) + 0144 (last_tool_use_at)
    and on the SQLite test fixture by ``SQLModel.metadata.create_all``.
    """

    vm_instance_id: str = Field(sa_column=Column(String, primary_key=True))
    last_contact_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    last_tool_use_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )


class TaskContact(SQLModel, table=True):
    """TASK-scoped twin of :class:`VmContact` — off-row liveness/progress for ONE task.

    ``VmContact`` is primary-keyed on ``vm_instance_id`` — "one row per VM". That
    identity holds for GCE agent VMs (one task per VM, so the VM row IS the task
    row) but is FALSE for fleet runners: ``scripts/local_runner.py`` multiplexes
    MANY concurrent tasks onto a single ``vm_instance_id='runner:<id>'``. Every
    task on that runner therefore shares ONE ``vmcontact`` row, and the heartbeat
    handler advances its ``last_tool_use_at`` with ``GREATEST(...)`` from ANY
    agent's heartbeat on that runner.

    The consequence was a permanent zombie class (2026-07-12): a hung-but-
    heartbeating agent (wedged LLM call, spin loop) co-resident with ANY actively
    working sibling is never stall-reaped, because the sibling's tool calls keep
    the SHARED progress stamp fresh and the reaper's ``agent_stalled`` override
    reads that shared stamp. The runner self-report fix (#14413,
    ``_runner_report_excludes_task``) cannot help by construction: a hung-but-
    alive process is still listed in the runner's ``active_task_ids``. With no
    absolute IN_PROGRESS runtime ceiling anywhere, nothing else ever fires — the
    task holds a runner slot, an account slot, and pins its parent lineage
    forever. The same shared-row masking also survives on the heartbeat-stale
    branch for a hung process that stops heartbeating but stays in
    ``active_task_ids``.

    This table restores the "one row per unit of work" identity that the reaper's
    overrides actually assume, keyed on ``task_id``. The heartbeat handler
    (``routers/agent.py::_record_vm_contact``) upserts BOTH rows on the same
    independent session: the shared ``vmcontact`` row (unchanged — every existing
    consumer keeps working) and this per-task row. The reaper's contact/progress
    overrides then read THIS row for ``runner:``-prefixed vm ids, where a shared
    row is a fleet-wide signal rather than a task signal, and keep reading
    ``vmcontact`` for 1-task-per-VM GCE ids, where the shared row is already
    task-scoped by construction (so GCE lock-starvation protection is preserved
    byte-for-byte).

    Rollout safety: an ABSENT row is never evidence of death — the reaper falls
    back to the shared ``vmcontact`` row (i.e. today's behaviour) when no per-task
    row exists yet, so a task that has not yet heartbeated under the new handler
    cannot be false-reaped. A row is written on EVERY heartbeat (not only
    tool-bearing ones), so a live task acquires one within a single heartbeat
    interval (~30-60s) of deploy. Once the row exists it is authoritative for that
    task, which is exactly what defeats the sibling mask.

    Both stamps carry the same lock-starvation immunity as ``VmContact``: they are
    written OFF the contended ``Task`` row, on a session committed independently of
    the request session, so the rollup holding the task row FOR UPDATE can never
    starve them. ``last_tool_use_at`` is advanced monotonically (never regressed to
    NULL or to an older value) so a tool-less heartbeat between two tool-bearing
    ones cannot blank a fresh progress signal.

    Created on Postgres by alembic 0528 and on the SQLite test fixture by
    ``SQLModel.metadata.create_all``.
    """

    task_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("task.id", ondelete="CASCADE"),
            primary_key=True,
        )
    )
    last_contact_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    last_tool_use_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )


class MessageType(str, Enum):
    QUESTION = "QUESTION"
    FEEDBACK = "FEEDBACK"
    BUG_REPORT = "BUG_REPORT"
    BLOCKER = "BLOCKER"
    NOTE = "NOTE"


class MessageSurface(str, Enum):
    """Durable projection that owns a project message."""

    CONVERSATION = "conversation"
    ACTIVITY = "activity"


class MessageHandlingState(str, Enum):
    """Strategic Resident delivery state, separate from message resolution."""

    PENDING = "pending"
    CLAIMED = "claimed"
    RESPONDED = "responded"
    SUPERSEDED = "superseded"


class ProjectMessage(SQLModel, table=True):
    """Async message board between users and the strategic agent."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)
    # Auth-provider/Firebase UID of the human actor. This deliberately is not a
    # FK to AllowedUser.id: AllowedUser uses an internal UUID while project
    # membership and request identity use the external string UID. Nullable for
    # legacy/system messages whose actor cannot be inferred safely.
    actor_user_id: str | None = Field(
        default=None, sa_column=Column(String(255), nullable=True)
    )
    # Canonical conversation writers allocate a positive, monotonic sequence
    # from ProjectMessageSequence's short per-project counter row. It remains
    # nullable during the legacy-writer migration window; the partial unique
    # index fences every canonical/backfilled value without breaking untouched
    # emitters. Remove nullable=True only after all writers have migrated and
    # the rollback soak has completed.
    sequence: int | None = Field(
        default=None, sa_column=Column(BigInteger, nullable=True)
    )
    surface: MessageSurface = Field(
        default=MessageSurface.ACTIVITY,
        sa_column=Column(
            String(32),
            nullable=False,
            server_default=text("'activity'"),
        ),
    )
    handling_state: MessageHandlingState = Field(
        default=MessageHandlingState.PENDING,
        sa_column=Column(
            String(32),
            nullable=False,
            server_default=text("'pending'"),
        ),
    )
    handled_by_task_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(),
            ForeignKey("task.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    # Durable foreground-turn claim. A Resident may read an operator message
    # only after atomically claiming it; every message in one response batch
    # shares the same claim id. The short lease lets a replacement generation
    # reclaim the exact batch after a provider/process/runner failure.
    conversation_claim_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(GUID(), nullable=True),
    )
    conversation_claim_generation: int | None = Field(
        default=None,
        sa_column=Column(Integer, nullable=True),
    )
    conversation_claim_incarnation: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(GUID(), nullable=True),
    )
    conversation_claim_expires_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    conversation_claim_attempt: int = Field(
        default=0,
        sa_column=Column(
            Integer,
            nullable=False,
            server_default=text("0"),
        ),
    )
    reply_to_message_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(),
            ForeignKey("projectmessage.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    message_type: MessageType = Field(
        default=MessageType.NOTE, sa_column=Column(String, default=MessageType.NOTE)
    )
    # Allowed values: 'user' | 'agent' | 'strategic' | 'system'. Enforced
    # server-side by ``chk_projectmessage_from_type`` (alembic 0065 /
    # audit L-S1, Postgres-only).
    from_type: str = Field(default="user", index=True)
    from_task_id: uuid.UUID | None = Field(default=None, foreign_key="task.id")
    content: str = Field(default="")
    context: Dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=True, default={})
    )
    status: str = Field(default="open")  # "open" | "linked" | "resolved"
    resolution: str | None = None
    # Client-supplied idempotency key. A double-click / network retry of a
    # send that already succeeded server-side carries the same key, so the
    # create endpoint returns the existing row instead of inserting a
    # duplicate. NULL for legacy / keyless sends (backward-compatible); the
    # partial unique index below only constrains non-NULL keys per project.
    idempotency_key: str | None = Field(
        default=None, sa_column=Column(String(255), nullable=True)
    )
    # Stable identity of the persistent conversational Workday session this
    # message belongs to. NULL preserves visibility for pre-migration and
    # out-of-session messages.
    resident_session_id: str | None = Field(
        default=None, sa_column=Column(String(255), nullable=True)
    )
    resolved_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    # Server-stamped when at least one WS client acknowledges delivery of the
    # broadcast frame (message_ack). NULL for messages that were never delivered
    # or whose ack was lost — this IS the durable diagnostic record of a
    # delivery gap when a message exists with non-NULL created_at but NULL
    # delivered_at.
    delivered_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    # TIMESTAMPTZ + server_default (migration 0061 / audit H-S4 + H-S5).
    # NaiveUTCDateTime strips tz on read (round 2 audit / C3 + H1).
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            NaiveUTCDateTime,
            nullable=False,
            server_default=func.now(),
            onupdate=_utcnow,
        ),
    )

    __table_args__ = (
        CheckConstraint(
            "sequence IS NULL OR sequence > 0",
            name="chk_projectmessage_sequence_positive",
        ),
        CheckConstraint(
            "surface IN ('conversation','activity')",
            name="chk_projectmessage_surface",
        ),
        CheckConstraint(
            "handling_state IN ('pending','claimed','responded','superseded')",
            name="chk_projectmessage_handling_state",
        ),
        CheckConstraint(
            "reply_to_message_id IS NULL OR reply_to_message_id <> id",
            name="chk_projectmessage_reply_not_self",
        ),
        CheckConstraint(
            "conversation_claim_attempt >= 0",
            name="chk_projectmessage_claim_attempt_nonnegative",
        ),
        Index("ix_projectmessage_project_status", "project_id", "status"),
        Index(
            "ux_projectmessage_project_sequence",
            "project_id",
            "sequence",
            unique=True,
            postgresql_where=text("sequence IS NOT NULL"),
            sqlite_where=text("sequence IS NOT NULL"),
        ),
        Index(
            "ix_projectmessage_project_surface_sequence",
            "project_id",
            "surface",
            "sequence",
        ),
        Index(
            "ix_projectmessage_project_handling_sequence",
            "project_id",
            "handling_state",
            "sequence",
        ),
        Index("ix_projectmessage_actor_user_id", "actor_user_id"),
        Index("ix_projectmessage_handled_by_task_id", "handled_by_task_id"),
        Index(
            "ix_projectmessage_project_claim",
            "project_id",
            "conversation_claim_id",
        ),
        Index(
            "ix_projectmessage_project_claim_lease",
            "project_id",
            "handling_state",
            "conversation_claim_expires_at",
        ),
        Index("ix_projectmessage_reply_to_message_id", "reply_to_message_id"),
        # One logical message per (project, client key). Partial so keyless
        # legacy sends are unconstrained; the create endpoint queries this
        # before insert and the index is the concurrency-safe backstop.
        Index(
            "ux_projectmessage_project_idempotency_key",
            "project_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
            sqlite_where=text("idempotency_key IS NOT NULL"),
        ),
        Index("ix_projectmessage_created_at", "created_at"),
        Index("ix_projectmessage_project_created_at", "project_id", "created_at"),
        Index(
            "ix_projectmessage_content_trgm",
            "content",
            postgresql_using="gin",
            postgresql_ops={"content": "gin_trgm_ops"},
        ),
        # Composite index for the strategic cold-load live-message preview query
        # (project_id, status IN ('open','linked'), ORDER BY created_at DESC LIMIT 12).
        # Two-branch UNION ALL rewrite uses this index for each status branch.
        Index(
            "ix_projectmessage_project_status_created",
            "project_id",
            "status",
            "created_at",
        ),
        Index("ix_projectmessage_from_task_id", "from_task_id"),
        Index("ix_projectmessage_status_from_type", "status", "from_type"),
        # Cross-project reaper sweeps filter status + message_type and ORDER BY
        # created_at with no project_id predicate (reaper_bug_message_sweeps.py,
        # reaper_sweeps.py), so the project-prefixed indexes cannot serve them.
        # This composite turns each sweep into a bounded index range walk in the
        # desired order instead of a full status population scan + sort.
        Index(
            "ix_projectmessage_status_message_type_created",
            "status",
            "message_type",
            "created_at",
        ),
        # PostgreSQL migrations create expression indexes for the exact JSONB
        # scalar predicates.  The metadata entries use the backing column so
        # the model/migration coverage invariant can track their names.
        Index("ix_projectmessage_context_event_type", "context"),
        Index("ix_projectmessage_context_dedupe_key", "context"),
        Index("ix_projectmessage_context_audience", "context"),
        # Accelerates high-signal message fetches filtered by (project_id, message_type)
        # and ordered by created_at DESC (build_operator_attention, get_operator_message_counts).
        Index(
            "ix_projectmessage_project_message_created",
            "project_id",
            "message_type",
            "created_at",
        ),
        Index(
            "ix_projectmessage_project_session_created",
            "project_id",
            "resident_session_id",
            "created_at",
        ),
    )


class ProjectMessageSequence(SQLModel, table=True):
    """Per-project atomic allocator for durable conversation sequence values."""

    project_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("project.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        )
    )
    last_sequence: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default=text("0")),
    )

    __table_args__ = (
        CheckConstraint(
            "last_sequence >= 0",
            name="chk_projectmessagesequence_nonnegative",
        ),
    )


class StrategicMemoryProjectState(SQLModel, table=True):
    """Per-project fence generations for persisted Strategic context envelopes.

    This is deliberately separate from ``Project`` so envelope invalidation is
    a narrow, durable state change rather than a JSONB rewrite of the project
    record.  ``context_authority_generation`` is named explicitly: the bare
    ``authority_generation`` name is reserved by a repository-wide ORM
    invariant for the retired resident authority model.
    """

    project_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("project.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        )
    )
    context_authority_generation: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    privacy_generation: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    # ``source_sequence`` is a durable ingestion ordering distinct from event
    # time.  It is only used to drive contiguous receipt processing and must
    # never be used to answer historical/event-time questions.
    source_high_watermark: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default=text("0")),
    )
    incorporated_source_sequence: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default=text("0")),
    )
    # The semantic ledger is independently revisioned from source ingestion.
    # A successful proposal advances this only with its append-only rows.
    ledger_revision: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default=text("0")),
    )
    backfill_status: str = Field(
        default="pending",
        sa_column=Column(String(16), nullable=False, server_default=text("'pending'")),
    )
    extraction_status: str = Field(
        default="healthy",
        sa_column=Column(String(16), nullable=False, server_default=text("'healthy'")),
    )
    last_error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    attempt: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            NaiveUTCDateTime, nullable=False, server_default=func.now()
        ),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            NaiveUTCDateTime,
            nullable=False,
            server_default=func.now(),
            onupdate=_utcnow,
        ),
    )

    __table_args__ = (
        CheckConstraint(
            "context_authority_generation >= 0",
            name="ck_smem_state_ctx_gen",
        ),
        CheckConstraint(
            "privacy_generation >= 0",
            name="ck_smem_state_priv_gen",
        ),
        CheckConstraint(
            "source_high_watermark >= 0",
            name="ck_smem_state_source_high",
        ),
        CheckConstraint(
            "incorporated_source_sequence >= 0",
            name="ck_smem_state_source_incorporated",
        ),
        CheckConstraint(
            "incorporated_source_sequence <= source_high_watermark",
            name="ck_smem_state_source_watermark",
        ),
        CheckConstraint("ledger_revision >= 0", name="ck_smem_state_ledger_revision"),
        CheckConstraint(
            "attempt >= 0",
            name="ck_smem_state_attempt",
        ),
        CheckConstraint(
            "backfill_status IN ('pending','running','complete','failed')",
            name="ck_smem_state_backfill_status",
        ),
        CheckConstraint(
            "extraction_status IN ('healthy','lagging','quarantined','failed')",
            name="ck_smem_state_extraction_status",
        ),
    )


class StrategicContextEnvelopeSnapshot(SQLModel, table=True):
    """One immutable, fenced serialization of Strategic context.

    The canonical payload is binary on purpose.  Fast and Workday transport the
    exact stored bytes (base64 encoded at the boundary), never independently
    serialize a dict and accidentally create divergent prompts.
    """

    snapshot_id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(), ForeignKey("project.id", ondelete="CASCADE"), nullable=False
        )
    )
    session_id: str = Field(sa_column=Column(String(255), nullable=False))
    claim_id: uuid.UUID = Field(sa_column=Column(GUID(), nullable=False))
    worker_generation: int = Field(
        sa_column=Column(Integer, nullable=False)
    )
    worker_incarnation: uuid.UUID = Field(
        sa_column=Column(GUID(), nullable=False)
    )
    input_revision: int = Field(sa_column=Column(BigInteger, nullable=False))
    transcript_sequence: int = Field(
        sa_column=Column(BigInteger, nullable=False)
    )
    ledger_revision: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default=text("0")),
    )
    problem_model_revision: str = Field(
        default="0", sa_column=Column(String(128), nullable=False, server_default=text("'0'"))
    )
    decision_authority_revision: str = Field(
        default="0", sa_column=Column(String(128), nullable=False, server_default=text("'0'"))
    )
    context_authority_generation: int = Field(
        sa_column=Column(Integer, nullable=False)
    )
    privacy_generation: int = Field(sa_column=Column(Integer, nullable=False))
    canonical_envelope: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    canonical_sha256: str = Field(sa_column=Column(String(64), nullable=False))
    retrieval_trace: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            NaiveUTCDateTime, nullable=False, server_default=func.now()
        ),
    )
    expires_at: datetime = Field(
        sa_column=Column(NaiveUTCDateTime, nullable=False)
    )

    __table_args__ = (
        CheckConstraint(
            "worker_generation >= 0",
            name="ck_sctx_worker_gen",
        ),
        CheckConstraint(
            "input_revision >= 0",
            name="ck_sctx_input_rev",
        ),
        CheckConstraint(
            "transcript_sequence >= 0",
            name="ck_sctx_transcript_seq",
        ),
        CheckConstraint(
            "ledger_revision >= 0",
            name="ck_sctx_ledger_rev",
        ),
        CheckConstraint(
            "context_authority_generation >= 0",
            name="ck_sctx_context_gen",
        ),
        CheckConstraint(
            "privacy_generation >= 0",
            name="ck_sctx_privacy_gen",
        ),
        CheckConstraint(
            "length(canonical_sha256) = 64",
            name="ck_sctx_sha256_len",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_sctx_expiry",
        ),
        UniqueConstraint(
            "project_id",
            "session_id",
            "claim_id",
            "worker_generation",
            "worker_incarnation",
            "input_revision",
            "transcript_sequence",
            "ledger_revision",
            "problem_model_revision",
            "decision_authority_revision",
            "context_authority_generation",
            "privacy_generation",
            name="uq_sctx_input_fence",
        ),
        Index(
            "ix_sctx_project_expires",
            "project_id",
            "expires_at",
        ),
        Index(
            "ix_sctx_project_session_seq",
            "project_id",
            "session_id",
            "transcript_sequence",
        ),
        Index(
            "ix_sctx_project_claim",
            "project_id",
            "claim_id",
        ),
    )


class StrategicMemorySource(SQLModel, table=True):
    """Immutable, project-scoped receipt for one piece of source evidence.

    The receipt intentionally records an immutable identity and content hash,
    rather than copying ordinary transcript/artifact bodies.  Mutable sources
    may retain their canonical bytes in ``canonical_payload`` so exact spans
    remain resolvable after the native source changes.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(), ForeignKey("project.id", ondelete="CASCADE"), nullable=False
        )
    )
    source_sequence: int = Field(sa_column=Column(BigInteger, nullable=False))
    native_sequence: int | None = Field(
        default=None, sa_column=Column(BigInteger, nullable=True)
    )
    ingestion_origin: str = Field(
        default="live",
        sa_column=Column(String(16), nullable=False, server_default=text("'live'")),
    )
    source_type: str = Field(sa_column=Column(String(64), nullable=False))
    source_key: str = Field(sa_column=Column(String(512), nullable=False))
    source_revision: str = Field(sa_column=Column(String(255), nullable=False))
    content_hash: str = Field(sa_column=Column(String(64), nullable=False))
    actor_kind: str | None = Field(
        default=None, sa_column=Column(String(64), nullable=True)
    )
    actor_id: str | None = Field(
        default=None, sa_column=Column(String(255), nullable=True)
    )
    occurred_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    recorded_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    resident_session_id: str | None = Field(
        default=None, sa_column=Column(String(255), nullable=True)
    )
    visibility: str = Field(
        default="project",
        sa_column=Column(String(32), nullable=False, server_default=text("'project'")),
    )
    sensitivity: str = Field(
        default="normal",
        sa_column=Column(String(32), nullable=False, server_default=text("'normal'")),
    )
    payload_state: str = Field(
        default="active",
        sa_column=Column(String(16), nullable=False, server_default=text("'active'")),
    )
    tombstoned_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    # SQLAlchemy reserves the Python name ``metadata`` for declarative model
    # metadata.  Keep the physical schema column named ``metadata`` while
    # exposing a non-conflicting ORM field to all producers/consumers.
    source_metadata: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("metadata", JSONB, nullable=False, server_default=text("'{}'")),
    )
    canonical_payload: bytes | None = Field(
        default=None, sa_column=Column(LargeBinary, nullable=True)
    )

    __table_args__ = (
        CheckConstraint("source_sequence >= 1", name="ck_smem_source_sequence"),
        CheckConstraint(
            "native_sequence IS NULL OR native_sequence >= 0",
            name="ck_smem_source_native_sequence",
        ),
        CheckConstraint(
            "ingestion_origin IN ('live','backfill')",
            name="ck_smem_source_origin",
        ),
        CheckConstraint(
            "source_type IN ('project_message','artifact_revision','task_result',"
            "'problem_model_revision','decision','operator_action',"
            "'legacy_knowledge_document_revision')",
            name="ck_smem_source_type",
        ),
        CheckConstraint(
            "payload_state IN ('active','redacted','deleted')",
            name="ck_smem_source_payload_state",
        ),
        CheckConstraint(
            "length(content_hash) = 64", name="ck_smem_source_hash_len"
        ),
        UniqueConstraint(
            "project_id", "source_sequence", name="uq_smem_source_project_sequence"
        ),
        UniqueConstraint("project_id", "id", name="uq_smem_source_project_id"),
        UniqueConstraint(
            "project_id",
            "source_type",
            "source_key",
            "source_revision",
            name="uq_smem_source_receipt",
        ),
        Index("ix_smem_source_project_sequence", "project_id", "source_sequence"),
        Index("ix_smem_source_project_type_key", "project_id", "source_type", "source_key"),
        Index("ix_smem_source_project_hash", "project_id", "content_hash"),
        Index("ix_smem_source_project_occurred", "project_id", "occurred_at"),
    )


class StrategicMemoryBackfillCursor(SQLModel, table=True):
    """Frozen, resumable per-producer receipt-backfill state."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(), ForeignKey("project.id", ondelete="CASCADE"), nullable=False
        )
    )
    producer: str = Field(sa_column=Column(String(128), nullable=False))
    cutoff_spec: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    cursor: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    scanned_count: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default=text("0")),
    )
    admitted_count: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default=text("0")),
    )
    skipped_count: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default=text("0")),
    )
    status: str = Field(
        default="pending",
        sa_column=Column(String(16), nullable=False, server_default=text("'pending'")),
    )
    error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            NaiveUTCDateTime, nullable=False, server_default=func.now(), onupdate=_utcnow
        ),
    )

    __table_args__ = (
        UniqueConstraint("project_id", "producer", name="uq_smem_backfill_producer"),
        CheckConstraint("scanned_count >= 0", name="ck_smem_backfill_scanned"),
        CheckConstraint("admitted_count >= 0", name="ck_smem_backfill_admitted"),
        CheckConstraint("skipped_count >= 0", name="ck_smem_backfill_skipped"),
        CheckConstraint(
            "admitted_count + skipped_count <= scanned_count",
            name="ck_smem_backfill_counts",
        ),
        CheckConstraint(
            "status IN ('pending','running','complete','failed')",
            name="ck_smem_backfill_status",
        ),
        Index("ix_smem_backfill_project_status", "project_id", "status"),
    )


class StrategicMemoryConsolidationJob(SQLModel, table=True):
    """Durable leased work item for contiguous or targeted reconsolidation."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(), ForeignKey("project.id", ondelete="CASCADE"), nullable=False
        )
    )
    source_start: int | None = Field(
        default=None, sa_column=Column(BigInteger, nullable=True)
    )
    source_end: int | None = Field(
        default=None, sa_column=Column(BigInteger, nullable=True)
    )
    mode: str = Field(
        default="contiguous",
        sa_column=Column(String(32), nullable=False, server_default=text("'contiguous'")),
    )
    target_source_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(), ForeignKey("strategicmemorysource.id"), nullable=True
        ),
    )
    base_ledger_revision: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default=text("0")),
    )
    # These fences are snapshotted at enqueue time.  A worker must reject its
    # proposal when an operator/privacy action advanced either generation
    # while it was waiting or running.
    context_authority_generation: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    privacy_generation: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    status: str = Field(
        default="queued",
        sa_column=Column(String(16), nullable=False, server_default=text("'queued'")),
    )
    lease_owner: str | None = Field(
        default=None, sa_column=Column(String(255), nullable=True)
    )
    lease_generation: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    lease_expires_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    attempt: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    model: str | None = Field(default=None, sa_column=Column(String(255), nullable=True))
    prompt_version: str | None = Field(
        default=None, sa_column=Column(String(128), nullable=True)
    )
    # Accepted proposal identity makes a lost response retryable without
    # accepting a different proposal for the same completed job.
    accepted_proposal_idempotency_key: str | None = Field(
        default=None, sa_column=Column(String(255), nullable=True)
    )
    accepted_proposal_hash: str | None = Field(
        default=None, sa_column=Column(String(64), nullable=True)
    )
    proposal_schema_version: str | None = Field(
        default=None, sa_column=Column(String(64), nullable=True)
    )
    resulting_ledger_revision: int | None = Field(
        default=None, sa_column=Column(BigInteger, nullable=True)
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            NaiveUTCDateTime, nullable=False, server_default=func.now(), onupdate=_utcnow
        ),
    )
    completed_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )

    __table_args__ = (
        CheckConstraint(
            "mode IN ('contiguous','reconsolidation')", name="ck_smem_job_mode"
        ),
        CheckConstraint(
            "status IN ('queued','running','retry','complete','dead')",
            name="ck_smem_job_status",
        ),
        CheckConstraint(
            "base_ledger_revision >= 0", name="ck_smem_job_base_ledger"
        ),
        CheckConstraint(
            "context_authority_generation >= 0", name="ck_smem_job_context_gen"
        ),
        CheckConstraint(
            "privacy_generation >= 0", name="ck_smem_job_privacy_gen"
        ),
        CheckConstraint("lease_generation >= 0", name="ck_smem_job_lease_gen"),
        CheckConstraint("attempt >= 0", name="ck_smem_job_attempt"),
        CheckConstraint(
            "accepted_proposal_hash IS NULL OR length(accepted_proposal_hash) = 64",
            name="ck_smem_job_proposal_hash_len",
        ),
        CheckConstraint(
            "resulting_ledger_revision IS NULL OR resulting_ledger_revision >= 0",
            name="ck_smem_job_result_ledger",
        ),
        CheckConstraint(
            "(source_start IS NULL AND source_end IS NULL) OR "
            "(source_start >= 1 AND source_end >= source_start)",
            name="ck_smem_job_source_range",
        ),
        CheckConstraint(
            "(mode = 'contiguous' AND source_start IS NOT NULL AND source_end IS NOT NULL "
            "AND target_source_id IS NULL) OR "
            "(mode = 'reconsolidation' AND target_source_id IS NOT NULL)",
            name="ck_smem_job_mode_target",
        ),
        UniqueConstraint("project_id", "id", name="uq_smem_job_project_id"),
        Index("ix_smem_job_project_status", "project_id", "status"),
        Index("ix_smem_job_project_range", "project_id", "source_start", "source_end"),
        Index(
            "uq_smem_job_project_accepted_proposal",
            "project_id",
            "accepted_proposal_idempotency_key",
            unique=True,
            postgresql_where=text("accepted_proposal_idempotency_key IS NOT NULL"),
            sqlite_where=text("accepted_proposal_idempotency_key IS NOT NULL"),
        ),
        Index(
            "uq_smem_job_active_project",
            "project_id",
            unique=True,
            postgresql_where=text("status IN ('queued','running','retry')"),
            sqlite_where=text("status IN ('queued','running','retry')"),
        ),
    )


class StrategicMemorySourceDisposition(SQLModel, table=True):
    """Mutable processing disposition for exactly one immutable source receipt."""

    source_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("strategicmemorysource.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        )
    )
    status: str = Field(
        default="pending",
        sa_column=Column(String(16), nullable=False, server_default=text("'pending'")),
    )
    reason: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    classifier_version: str | None = Field(
        default=None, sa_column=Column(String(128), nullable=True)
    )
    extractor_model: str | None = Field(
        default=None, sa_column=Column(String(255), nullable=True)
    )
    prompt_version: str | None = Field(
        default=None, sa_column=Column(String(128), nullable=True)
    )
    consolidation_job_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(), ForeignKey("strategicmemoryconsolidationjob.id"), nullable=True
        ),
    )
    processed_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','incorporated','no_memory','quarantined')",
            name="ck_smem_disposition_status",
        ),
        Index("ix_smem_disposition_job", "consolidation_job_id"),
        Index("ix_smem_disposition_status", "status"),
    )


class StrategicMemoryItem(SQLModel, table=True):
    """Stable project-scoped identity whose semantic assertions are versioned."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(), ForeignKey("project.id", ondelete="CASCADE"), nullable=False
        )
    )
    scope_type: str = Field(
        default="project",
        sa_column=Column(String(32), nullable=False, server_default=text("'project'")),
    )
    # ``project`` uses the empty-string sentinel.  NULL would defeat the
    # project/scope/logical-key uniqueness fence on PostgreSQL.
    scope_key: str = Field(
        default="", sa_column=Column(String(255), nullable=False, server_default=text("''"))
    )
    kind: str = Field(sa_column=Column(String(32), nullable=False))
    logical_key: str = Field(sa_column=Column(String(512), nullable=False))
    current_version: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    current_status: str = Field(
        default="active",
        sa_column=Column(String(16), nullable=False, server_default=text("'active'")),
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            NaiveUTCDateTime, nullable=False, server_default=func.now(), onupdate=_utcnow
        ),
    )
    forgotten_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )

    __table_args__ = (
        CheckConstraint(
            "scope_type IN ('project','research_topic')", name="ck_smem_item_scope_type"
        ),
        CheckConstraint(
            "(scope_type = 'project' AND scope_key = '') OR "
            "(scope_type = 'research_topic' AND length(scope_key) > 0)",
            name="ck_smem_item_scope_key",
        ),
        CheckConstraint(
            "kind IN ('goal','preference','constraint','definition','notation',"
            "'assumption','claim','decision','result','counterexample',"
            "'open_question','procedure','lesson')",
            name="ck_smem_item_kind",
        ),
        CheckConstraint("current_version >= 0", name="ck_smem_item_current_version"),
        CheckConstraint(
            "current_status IN ('active','challenged','superseded','retracted','forgotten')",
            name="ck_smem_item_current_status",
        ),
        CheckConstraint(
            "(current_status = 'forgotten') = (forgotten_at IS NOT NULL)",
            name="ck_smem_item_forgotten_time",
        ),
        UniqueConstraint("project_id", "id", name="uq_smem_item_project_id"),
        UniqueConstraint(
            "project_id", "scope_type", "scope_key", "logical_key",
            name="uq_smem_item_project_scope_logical",
        ),
        Index("ix_smem_item_project_kind_status", "project_id", "kind", "current_status"),
        Index("ix_smem_item_project_updated", "project_id", "updated_at"),
    )


class StrategicMemoryItemVersion(SQLModel, table=True):
    """Immutable assertion content for one logical item version."""

    item_id: uuid.UUID = Field(sa_column=Column(GUID(), primary_key=True, nullable=False))
    version: int = Field(sa_column=Column(Integer, primary_key=True, nullable=False))
    project_id: uuid.UUID = Field(
        sa_column=Column(GUID(), ForeignKey("project.id", ondelete="CASCADE"), nullable=False)
    )
    canonical_text: str = Field(sa_column=Column(Text, nullable=False))
    structured_value: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    epistemic_status: str = Field(sa_column=Column(String(32), nullable=False))
    confidence: float = Field(sa_column=Column(Numeric(5, 4), nullable=False))
    asserted_effective_from: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    asserted_effective_until: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    recorded_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    recorded_source_sequence: int = Field(sa_column=Column(BigInteger, nullable=False))
    exact_terms: List[str] = Field(
        default_factory=list,
        sa_column=Column(ARRAY(Text).with_variant(JSON(), "sqlite"), nullable=False),
    )
    formula_fingerprints: List[str] = Field(
        default_factory=list,
        sa_column=Column(ARRAY(Text).with_variant(JSON(), "sqlite"), nullable=False),
    )
    content_hash: str = Field(sa_column=Column(String(64), nullable=False))
    created_by_job_id: uuid.UUID | None = Field(
        default=None, sa_column=Column(GUID(), nullable=True)
    )
    extractor_version: str | None = Field(
        default=None, sa_column=Column(String(128), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "item_id"],
            ["strategicmemoryitem.project_id", "strategicmemoryitem.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "created_by_job_id"],
            [
                "strategicmemoryconsolidationjob.project_id",
                "strategicmemoryconsolidationjob.id",
            ],
        ),
        CheckConstraint("version >= 1", name="ck_smem_item_version_positive"),
        CheckConstraint(
            "epistemic_status IN ('operator_explicit','verified_evidence',"
            "'agent_conclusion','agent_hypothesis')",
            name="ck_smem_item_version_epistemic",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_smem_item_version_confidence"
        ),
        CheckConstraint(
            "asserted_effective_until IS NULL OR asserted_effective_from IS NULL "
            "OR asserted_effective_until > asserted_effective_from",
            name="ck_smem_item_version_effective_interval",
        ),
        CheckConstraint("recorded_source_sequence >= 1", name="ck_smem_item_version_source_seq"),
        CheckConstraint("length(content_hash) = 64", name="ck_smem_item_version_hash_len"),
        UniqueConstraint(
            "project_id", "item_id", "version", name="uq_smem_item_version_project_item"
        ),
        Index("ix_smem_item_version_item_recorded", "item_id", "recorded_at"),
        Index(
            "ix_smem_item_version_project_source", "project_id", "recorded_source_sequence"
        ),
        Index(
            "ix_smem_item_version_effective",
            "project_id",
            "asserted_effective_from",
            "asserted_effective_until",
        ),
    )


class StrategicMemoryItemSource(SQLModel, table=True):
    """Exact, normalized receipt provenance for an immutable item version."""

    item_id: uuid.UUID = Field(sa_column=Column(GUID(), primary_key=True, nullable=False))
    item_version: int = Field(sa_column=Column(Integer, primary_key=True, nullable=False))
    source_id: uuid.UUID = Field(sa_column=Column(GUID(), primary_key=True, nullable=False))
    source_role: str = Field(sa_column=Column(String(16), primary_key=True, nullable=False))
    span_index: int = Field(sa_column=Column(Integer, primary_key=True, nullable=False))
    project_id: uuid.UUID = Field(
        sa_column=Column(GUID(), ForeignKey("project.id", ondelete="CASCADE"), nullable=False)
    )
    span_kind: str = Field(sa_column=Column(String(32), nullable=False))
    span_start: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    span_end: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    span_hash: str = Field(sa_column=Column(String(64), nullable=False))

    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "item_id", "item_version"],
            [
                "strategicmemoryitemversion.project_id",
                "strategicmemoryitemversion.item_id",
                "strategicmemoryitemversion.version",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "source_id"],
            ["strategicmemorysource.project_id", "strategicmemorysource.id"],
        ),
        CheckConstraint(
            "source_role IN ('assertion','evidence','correction','verification')",
            name="ck_smem_item_source_role",
        ),
        CheckConstraint(
            "span_kind IN ('text','inline_math','display_math','code','identifier',"
            "'artifact_region')",
            name="ck_smem_item_source_span_kind",
        ),
        CheckConstraint("span_index >= 0", name="ck_smem_item_source_span_index"),
        CheckConstraint(
            "(span_start IS NULL AND span_end IS NULL) OR "
            "(span_start >= 0 AND span_end > span_start)",
            name="ck_smem_item_source_span_range",
        ),
        CheckConstraint("length(span_hash) = 64", name="ck_smem_item_source_hash_len"),
        Index("ix_smem_item_source_project_source", "project_id", "source_id"),
    )


class StrategicMemoryTransition(SQLModel, table=True):
    """Append-only authority event changing an item version's current status."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(
        sa_column=Column(GUID(), ForeignKey("project.id", ondelete="CASCADE"), nullable=False)
    )
    item_id: uuid.UUID = Field(sa_column=Column(GUID(), nullable=False))
    item_version: int = Field(sa_column=Column(Integer, nullable=False))
    transition: str = Field(sa_column=Column(String(16), nullable=False))
    target_item_id: uuid.UUID | None = Field(
        default=None, sa_column=Column(GUID(), nullable=True)
    )
    target_version: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    effective_from: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    effective_until: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    recorded_source_sequence: int = Field(sa_column=Column(BigInteger, nullable=False))
    source_id: uuid.UUID = Field(sa_column=Column(GUID(), nullable=False))
    reason: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "item_id", "item_version"],
            [
                "strategicmemoryitemversion.project_id",
                "strategicmemoryitemversion.item_id",
                "strategicmemoryitemversion.version",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "target_item_id", "target_version"],
            [
                "strategicmemoryitemversion.project_id",
                "strategicmemoryitemversion.item_id",
                "strategicmemoryitemversion.version",
            ],
        ),
        ForeignKeyConstraint(
            ["project_id", "source_id"],
            ["strategicmemorysource.project_id", "strategicmemorysource.id"],
        ),
        CheckConstraint(
            "transition IN ('activate','challenge','supersede','retract','forget')",
            name="ck_smem_transition_kind",
        ),
        CheckConstraint(
            "(target_item_id IS NULL AND target_version IS NULL) OR "
            "(target_item_id IS NOT NULL AND target_version IS NOT NULL)",
            name="ck_smem_transition_target_pair",
        ),
        CheckConstraint(
            "(transition = 'supersede' AND target_item_id IS NOT NULL) OR "
            "(transition <> 'supersede' AND target_item_id IS NULL)",
            name="ck_smem_transition_target_kind",
        ),
        CheckConstraint("recorded_source_sequence >= 1", name="ck_smem_transition_source_seq"),
        CheckConstraint(
            "effective_until IS NULL OR effective_from IS NULL OR effective_until > effective_from",
            name="ck_smem_transition_effective_interval",
        ),
        Index("ix_smem_transition_project_item_sequence", "project_id", "item_id", "recorded_source_sequence"),
        Index("ix_smem_transition_project_source", "project_id", "source_id"),
    )


class StrategicMemoryCorrectionBarrier(SQLModel, table=True):
    """Append-only candidate suppression opened by a correction source."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(
        sa_column=Column(GUID(), ForeignKey("project.id", ondelete="CASCADE"), nullable=False)
    )
    correction_source_id: uuid.UUID = Field(sa_column=Column(GUID(), nullable=False))
    candidate_item_id: uuid.UUID = Field(sa_column=Column(GUID(), nullable=False))
    candidate_version: int = Field(sa_column=Column(Integer, nullable=False))
    match_kind: str = Field(sa_column=Column(String(16), nullable=False))
    match_score: float | None = Field(default=None, sa_column=Column(Numeric(5, 4), nullable=True))
    context_generation: int = Field(sa_column=Column(Integer, nullable=False))
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "correction_source_id"],
            ["strategicmemorysource.project_id", "strategicmemorysource.id"],
        ),
        ForeignKeyConstraint(
            ["project_id", "candidate_item_id", "candidate_version"],
            [
                "strategicmemoryitemversion.project_id",
                "strategicmemoryitemversion.item_id",
                "strategicmemoryitemversion.version",
            ],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "match_kind IN ('exact','reply_link','classifier')",
            name="ck_smem_barrier_match_kind",
        ),
        CheckConstraint(
            "match_score IS NULL OR (match_score >= 0 AND match_score <= 1)",
            name="ck_smem_barrier_match_score",
        ),
        CheckConstraint("context_generation >= 0", name="ck_smem_barrier_authority_gen"),
        UniqueConstraint(
            "project_id", "correction_source_id", "candidate_item_id", "candidate_version",
            name="uq_smem_barrier_candidate",
        ),
        UniqueConstraint("project_id", "id", name="uq_smem_barrier_project_id"),
        Index("ix_smem_barrier_project_candidate", "project_id", "candidate_item_id", "candidate_version"),
    )


class StrategicMemoryCorrectionBarrierEvent(SQLModel, table=True):
    """Append-only audit event; the latest valid event derives barrier state."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(
        sa_column=Column(GUID(), ForeignKey("project.id", ondelete="CASCADE"), nullable=False)
    )
    barrier_id: uuid.UUID = Field(sa_column=Column(GUID(), nullable=False))
    event_type: str = Field(sa_column=Column(String(32), nullable=False))
    source_id: uuid.UUID = Field(sa_column=Column(GUID(), nullable=False))
    actor_kind: str | None = Field(default=None, sa_column=Column(String(64), nullable=True))
    actor_id: str | None = Field(default=None, sa_column=Column(String(255), nullable=True))
    reason: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    recorded_source_sequence: int = Field(sa_column=Column(BigInteger, nullable=False))
    context_generation: int = Field(sa_column=Column(Integer, nullable=False))
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "barrier_id"],
            ["strategicmemorycorrectionbarrier.project_id", "strategicmemorycorrectionbarrier.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "source_id"],
            ["strategicmemorysource.project_id", "strategicmemorysource.id"],
        ),
        CheckConstraint(
            "event_type IN ('opened','target_confirmed','resolved','dismissed')",
            name="ck_smem_barrier_event_type",
        ),
        CheckConstraint("recorded_source_sequence >= 1", name="ck_smem_barrier_event_source_seq"),
        CheckConstraint("context_generation >= 0", name="ck_smem_barrier_event_authority_gen"),
        Index("ix_smem_barrier_event_project_barrier", "project_id", "barrier_id", "created_at"),
    )


class StrategicMemoryEdge(SQLModel, table=True):
    """Versioned, typed relation between two item-version endpoints."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(
        sa_column=Column(GUID(), ForeignKey("project.id", ondelete="CASCADE"), nullable=False)
    )
    from_node_type: str = Field(
        default="item_version",
        sa_column=Column(String(32), nullable=False, server_default=text("'item_version'")),
    )
    from_node_id: uuid.UUID = Field(sa_column=Column(GUID(), nullable=False))
    from_version: int = Field(sa_column=Column(Integer, nullable=False))
    relation: str = Field(sa_column=Column(String(32), nullable=False))
    to_node_type: str = Field(
        default="item_version",
        sa_column=Column(String(32), nullable=False, server_default=text("'item_version'")),
    )
    to_node_id: uuid.UUID = Field(sa_column=Column(GUID(), nullable=False))
    to_version: int = Field(sa_column=Column(Integer, nullable=False))
    confidence: float = Field(sa_column=Column(Numeric(5, 4), nullable=False))
    version: int = Field(
        default=1, sa_column=Column(Integer, nullable=False, server_default=text("1"))
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "from_node_id", "from_version"],
            [
                "strategicmemoryitemversion.project_id",
                "strategicmemoryitemversion.item_id",
                "strategicmemoryitemversion.version",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "to_node_id", "to_version"],
            [
                "strategicmemoryitemversion.project_id",
                "strategicmemoryitemversion.item_id",
                "strategicmemoryitemversion.version",
            ],
            ondelete="CASCADE",
        ),
        CheckConstraint("from_node_type = 'item_version'", name="ck_smem_edge_from_type"),
        CheckConstraint("to_node_type = 'item_version'", name="ck_smem_edge_to_type"),
        CheckConstraint(
            "relation IN ('supports','contradicts','derived_from','depends_on')",
            name="ck_smem_edge_relation",
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_smem_edge_confidence"),
        CheckConstraint("version >= 1", name="ck_smem_edge_version"),
        CheckConstraint(
            "from_node_id <> to_node_id OR from_version <> to_version",
            name="ck_smem_edge_no_self",
        ),
        UniqueConstraint("project_id", "id", name="uq_smem_edge_project_id"),
        UniqueConstraint(
            "project_id", "from_node_id", "from_version", "relation", "to_node_id", "to_version", "version",
            name="uq_smem_edge_versioned_relation",
        ),
        Index("ix_smem_edge_project_from", "project_id", "from_node_id", "from_version"),
        Index("ix_smem_edge_project_to", "project_id", "to_node_id", "to_version"),
    )


class StrategicMemoryEdgeSource(SQLModel, table=True):
    """Non-cascading provenance link from a typed edge to source evidence."""

    edge_id: uuid.UUID = Field(sa_column=Column(GUID(), primary_key=True, nullable=False))
    source_id: uuid.UUID = Field(sa_column=Column(GUID(), primary_key=True, nullable=False))
    project_id: uuid.UUID = Field(
        sa_column=Column(GUID(), ForeignKey("project.id", ondelete="CASCADE"), nullable=False)
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "edge_id"],
            ["strategicmemoryedge.project_id", "strategicmemoryedge.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "source_id"],
            ["strategicmemorysource.project_id", "strategicmemorysource.id"],
        ),
        Index("ix_smem_edge_source_project_source", "project_id", "source_id"),
    )


class StrategicMemoryProjection(SQLModel, table=True):
    """Rebuildable current-state cache; ledger rows remain the only truth."""

    project_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(), ForeignKey("project.id", ondelete="CASCADE"), primary_key=True, nullable=False
        )
    )
    ledger_revision: int = Field(sa_column=Column(BigInteger, nullable=False))
    through_source_sequence: int = Field(sa_column=Column(BigInteger, nullable=False))
    structured_state: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    canonical_text: str = Field(sa_column=Column(Text, nullable=False))
    canonical_sha256: str = Field(sa_column=Column(String(64), nullable=False))
    rebuilt_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )

    __table_args__ = (
        CheckConstraint("ledger_revision >= 0", name="ck_smem_projection_ledger_revision"),
        CheckConstraint(
            "through_source_sequence >= 0", name="ck_smem_projection_through_source"
        ),
        CheckConstraint("length(canonical_sha256) = 64", name="ck_smem_projection_sha_len"),
    )


_DERIVED_STRATEGIC_PACKET_CONTEXT_KEYS = (
    "_conversation_consultation_packet",
    "_workday_consultation_result",
)


def _is_derived_strategic_packet_context(value: object) -> bool:
    """Whether a message is private derived Workday coordination state.

    These records are not canonical conversation evidence.  Lease bookkeeping
    mutates their JSON context frequently, and must not impersonate a governed
    source redaction.  Actual privacy actions scrub them together with every
    envelope snapshot below.
    """

    return isinstance(value, Mapping) and any(
        value.get(key) is True for key in _DERIVED_STRATEGIC_PACKET_CONTEXT_KEYS
    )


def _purge_derived_strategic_context_for_privacy_mutation(
    session: _OrmSession,
    project_id: uuid.UUID,
) -> None:
    """Synchronously scrub every revocable Strategic derived copy for a project.

    This is deliberately shared by the ORM flush hook and the explicit async
    privacy helper.  Both paths must erase the snapshot bytes *and* the private
    Workday packet/result rows, while also making already-held ORM objects safe
    to inspect during the same request.
    """

    connection = session.connection()
    # Raw Core DML does not synchronize the identity map. Erase content before
    # detaching each loaded derived object so callers cannot recover stale text
    # from a same-request Python reference after the database delete starts.
    loaded_snapshots = [
        loaded
        for loaded in list(session.identity_map.values())
        if (
            isinstance(loaded, StrategicContextEnvelopeSnapshot)
            and loaded.__dict__.get("project_id") == project_id
        )
    ]
    for loaded in loaded_snapshots:
        loaded.canonical_envelope = b""
        loaded.canonical_sha256 = ""
        loaded.retrieval_trace = {}
        session.expunge(loaded)

    loaded_private_packets = [
        loaded
        for loaded in list(session.identity_map.values())
        if (
            isinstance(loaded, ProjectMessage)
            and loaded.__dict__.get("project_id") == project_id
            and _is_derived_strategic_packet_context(
                loaded.__dict__.get("context")
            )
        )
    ]
    for loaded in loaded_private_packets:
        loaded.content = ""
        loaded.context = {}
        session.expunge(loaded)

    connection.execute(
        StrategicContextEnvelopeSnapshot.__table__.delete().where(
            StrategicContextEnvelopeSnapshot.project_id == project_id
        )
    )
    packet_context = ProjectMessage.__table__.c.context
    connection.execute(
        ProjectMessage.__table__.delete().where(
            ProjectMessage.__table__.c.project_id == project_id,
            or_(
                packet_context[_DERIVED_STRATEGIC_PACKET_CONTEXT_KEYS[0]]
                .as_boolean()
                .is_(True),
                packet_context[_DERIVED_STRATEGIC_PACKET_CONTEXT_KEYS[1]]
                .as_boolean()
                .is_(True),
            ),
        )
    )


def _tombstone_project_message_source_receipts_for_privacy_mutation(
    session: _OrmSession,
    *,
    project_id: uuid.UUID,
    message_id: uuid.UUID,
    payload_state: str,
) -> None:
    """Make source-receipt privacy state explicit before invalidating caches.

    A receipt retains only identity/hash for immutable ProjectMessage content,
    but it must still say that its native evidence is no longer usable.  The
    Core update runs in the same native-message transaction as the privacy
    mutation; it does not delete semantic history or cascade into future
    ledger tables.  ``deleted`` dominates an earlier ``redacted`` state.
    """

    if payload_state not in {"redacted", "deleted"}:  # pragma: no cover - caller invariant
        raise ValueError("ProjectMessage source tombstone must be redacted or deleted")
    source_table = StrategicMemorySource.__table__
    predicate = (
        source_table.c.project_id == project_id,
        source_table.c.source_type == "project_message",
        source_table.c.source_key == str(message_id),
    )
    if payload_state == "redacted":
        predicate = (*predicate, source_table.c.payload_state == "active")
    now = _utcnow()
    connection = session.connection()
    affected = connection.execute(
        select(source_table.c.id, source_table.c.source_sequence).where(*predicate)
    ).all()
    # A source that became unavailable before semantic admission is
    # deterministically non-incorporable, not an eternal pending gap.  Preserve
    # already-terminal history for the later governed ledger privacy service.
    disposition_table = StrategicMemorySourceDisposition.__table__
    job_table = StrategicMemoryConsolidationJob.__table__
    for source_id, source_sequence in affected:
        # Any active job whose immutable input range included this receipt is
        # stale. Mark it dead transactionally; enqueue will advance across the
        # new terminal disposition and create a fresh range after the gap.
        connection.execute(
            job_table.update()
            .where(
                job_table.c.project_id == project_id,
                job_table.c.status.in_(("queued", "running", "retry")),
                or_(
                    job_table.c.target_source_id == source_id,
                    (
                        (job_table.c.source_start <= source_sequence)
                        & (job_table.c.source_end >= source_sequence)
                    ),
                ),
            )
            .values(
                status="dead",
                error=f"source_{payload_state}",
                lease_owner=None,
                lease_expires_at=None,
                completed_at=now,
                updated_at=now,
            )
        )
    # Completion uses Project -> state -> job -> source -> disposition.  The
    # privacy path follows exactly the same row order to avoid a job/source
    # cycle during concurrent governed redaction.
    connection.execute(
        source_table.update()
        .where(*predicate)
        .values(payload_state=payload_state, tombstoned_at=now)
    )
    for source_id, _source_sequence in affected:
        connection.execute(
            disposition_table.update()
            .where(
                disposition_table.c.source_id == source_id,
                disposition_table.c.status == "pending",
            )
            .values(
                status="no_memory",
                reason=f"source_{payload_state}",
                processed_at=now,
                consolidation_job_id=None,
            )
        )
    # Core DML bypasses identity-map synchronization.  Keep same-request
    # retrieval from observing an active object after a governed redaction.
    for loaded in session.identity_map.values():
        if (
            isinstance(loaded, StrategicMemorySource)
            and loaded.__dict__.get("project_id") == project_id
            and loaded.__dict__.get("source_type") == "project_message"
            and loaded.__dict__.get("source_key") == str(message_id)
            and (
                payload_state == "deleted"
                or loaded.__dict__.get("payload_state") == "active"
            )
        ):
            loaded.payload_state = payload_state
            loaded.tombstoned_at = now
        elif (
            isinstance(loaded, StrategicMemorySourceDisposition)
            and any(loaded.__dict__.get("source_id") == row[0] for row in affected)
            and loaded.__dict__.get("status") == "pending"
        ):
            session.expire(loaded)
        elif (
            isinstance(loaded, StrategicMemoryConsolidationJob)
            and loaded.__dict__.get("project_id") == project_id
            and loaded.__dict__.get("status") in {"queued", "running", "retry"}
        ):
            session.expire(loaded)


def _lock_project_before_strategic_state_for_flush(
    session: _OrmSession,
    project_id: uuid.UUID,
    *,
    exclusive: bool = True,
) -> None:
    """Enforce the global Project -> strategic-state row lock order."""

    statement = select(Project.id).where(Project.id == project_id)
    if exclusive:
        statement = statement.with_for_update()
    else:
        # Canonical message allocation already holds this parent row in
        # KEY SHARE mode. Reusing the same strength avoids a concurrent
        # KEY SHARE -> UPDATE lock-upgrade deadlock while the strategic-state
        # row still serializes receipt sequence allocation.
        statement = statement.with_for_update(read=True, key_share=True)
    locked = session.connection().execute(
        statement
    ).scalar_one_or_none()
    if locked is None:
        raise RuntimeError("Strategic state mutation requires an existing Project")


def _strategic_memory_state_upsert_for_flush(
    session: _OrmSession,
    *,
    project_id: uuid.UUID,
    authority_changed: bool,
    privacy_changed: bool,
) -> None:
    """Atomically advance envelope fences from the ORM write boundary.

    Project JSON mutations have many producers.  This hook is intentionally
    below that API surface: a direct ``project.problem_model = ...`` assignment
    cannot accidentally leave an old Fast/Workday envelope valid.  PostgreSQL
    and the SQLite test runtime both provide ON CONFLICT DO UPDATE with atomic
    ``column = column + 1`` semantics.
    """

    if not authority_changed and not privacy_changed:
        return
    _lock_project_before_strategic_state_for_flush(session, project_id)
    table = StrategicMemoryProjectState.__table__
    values = {
        "project_id": project_id,
        "context_authority_generation": int(authority_changed),
        "privacy_generation": int(privacy_changed),
        "created_at": _utcnow(),
        "updated_at": _utcnow(),
    }
    set_values = {
        "context_authority_generation": (
            table.c.context_authority_generation + int(authority_changed)
        ),
        "privacy_generation": table.c.privacy_generation + int(privacy_changed),
        "updated_at": _utcnow(),
    }
    connection = session.connection()
    if connection.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    elif connection.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    else:  # pragma: no cover - Blueprint supports PostgreSQL; SQLite is tests.
        raise RuntimeError(
            "Strategic envelope invalidation requires PostgreSQL or SQLite ON CONFLICT"
        )
    statement = dialect_insert(table).values(**values).on_conflict_do_update(
        index_elements=[table.c.project_id],
        set_=set_values,
    )
    connection.execute(statement)
    # The upsert is Core SQL rather than an ORM assignment so it is atomic
    # across independent Resident transactions. Expire an already-loaded state
    # row in this Session; otherwise a later envelope build could read the
    # identity-map's pre-increment generation.
    for loaded in session.identity_map.values():
        if (
            isinstance(loaded, StrategicMemoryProjectState)
            and loaded.__dict__.get("project_id") == project_id
        ):
            session.expire(loaded)
    if privacy_changed:
        _purge_derived_strategic_context_for_privacy_mutation(session, project_id)


@event.listens_for(_OrmSession, "before_flush")
def _invalidate_strategic_envelopes_for_authority_and_privacy_changes(
    session: _OrmSession,
    flush_context: object,
    instances: object,
) -> None:
    """Keep envelope generations transactional with their source mutation.

    Authority changes are the two current Project-owned inputs to the V1
    envelope: ``problem_model`` and retained legacy
    ``strategic_memory_notes``.  A ProjectMessage content rewrite/delete is a
    governed privacy action until the dedicated privacy UI lands, so it bumps
    the privacy generation and immediately deletes derived snapshots.

    Bulk SQL DML bypasses ORM events by definition.  The only current bulk
    ProjectMessage delete is the routine-system-message reaper (not canonical
    conversation evidence); any future privacy delete must use
    ``bump_privacy_generation_and_purge_context_envelopes`` before bulk DML.
    """

    from sqlalchemy import inspect as _sa_inspect

    changes: dict[uuid.UUID, tuple[bool, bool]] = {}
    # Keyed by the immutable native source identity.  Prefer a deletion over a
    # redaction if both appear in a complex flush.
    source_tombstones: dict[tuple[uuid.UUID, uuid.UUID], str] = {}

    def mark(
        project_id: uuid.UUID | None,
        *,
        authority: bool = False,
        privacy: bool = False,
    ) -> None:
        if project_id is None:
            return
        prior_authority, prior_privacy = changes.get(project_id, (False, False))
        changes[project_id] = (
            prior_authority or authority,
            prior_privacy or privacy,
        )

    def tombstone_message_source(
        project_id: uuid.UUID | None,
        message_id: uuid.UUID | None,
        *,
        payload_state: str,
    ) -> None:
        if project_id is None or message_id is None:
            return
        key = (project_id, message_id)
        existing = source_tombstones.get(key)
        if existing != "deleted":
            source_tombstones[key] = payload_state

    for instance in session.dirty:
        if isinstance(instance, Project):
            inspected = _sa_inspect(instance)
            project_id = (inspected.identity or (None,))[0]
            if project_id is None:
                continue
            authority = any(
                inspected.attrs[field].history.has_changes()
                for field in ("problem_model", "strategic_memory_notes")
            )
            mark(project_id, authority=authority)
        elif isinstance(instance, ProjectMessage):
            inspected = _sa_inspect(instance)
            # Every mutable value copied into the serialized transcript must
            # invalidate it.  Include the canonical visibility predicate too:
            # changing a row from/to the conversation is semantically a
            # redaction even when its body is unchanged.
            privacy_fields = (
                "content",
                "context",
                "from_type",
                "sequence",
                "surface",
                "reply_to_message_id",
            )
            context_history = inspected.attrs.context.history
            old_contexts = list(context_history.deleted)
            remains_private_packet = (
                _is_derived_strategic_packet_context(instance.__dict__.get("context"))
                and old_contexts
                and all(
                    _is_derived_strategic_packet_context(value)
                    for value in old_contexts
                )
            )
            if not remains_private_packet and any(
                inspected.attrs[field].history.has_changes()
                for field in privacy_fields
            ):
                mark(instance.__dict__.get("project_id"), privacy=True)
                tombstone_message_source(
                    instance.__dict__.get("project_id"),
                    (inspected.identity or (None,))[0] or instance.__dict__.get("id"),
                    payload_state="redacted",
                )
            if inspected.attrs.project_id.history.has_changes():
                # Project reassignment is unusual but must not retain this
                # message in either the source or destination project's cache.
                mark(instance.__dict__.get("project_id"), privacy=True)
                for previous_project_id in inspected.attrs.project_id.history.deleted:
                    mark(previous_project_id, privacy=True)
                    tombstone_message_source(
                        previous_project_id,
                        (inspected.identity or (None,))[0] or instance.__dict__.get("id"),
                        payload_state="redacted",
                    )

    for instance in session.deleted:
        if isinstance(instance, ProjectMessage):
            # ORM deletion is the conservative governed privacy path.  Routine
            # bulk pruning remains explicitly documented above.
            if not _is_derived_strategic_packet_context(
                instance.__dict__.get("context")
            ):
                mark(instance.__dict__.get("project_id"), privacy=True)
                tombstone_message_source(
                    instance.__dict__.get("project_id"),
                    instance.__dict__.get("id"),
                    payload_state="deleted",
                )

    # Acquire the canonical Project -> state locks before touching jobs,
    # sources, or dispositions. Any failure rolls the entire privacy mutation
    # and cache invalidation transaction back.
    for project_id in sorted(changes, key=str):
        authority, privacy = changes[project_id]
        _strategic_memory_state_upsert_for_flush(
            session,
            project_id=project_id,
            authority_changed=authority,
            privacy_changed=privacy,
        )
    for project_id, message_id in sorted(
        source_tombstones, key=lambda key: (str(key[0]), str(key[1]))
    ):
        payload_state = source_tombstones[(project_id, message_id)]
        _tombstone_project_message_source_receipts_for_privacy_mutation(
            session,
            project_id=project_id,
            message_id=message_id,
            payload_state=payload_state,
        )


def _next_strategic_memory_source_sequence_for_flush(
    session: _OrmSession,
    *,
    project_id: uuid.UUID,
) -> int:
    """Atomically reserve one receipt sequence inside the enclosing flush.

    Source receipts are allowed to interleave between live and backfill
    producers, but no two commits may receive the same per-project sequence.
    The state row doubles as the allocation fence; this keeps the receipt and
    watermark mutation in the same transaction as the native source write.
    """

    _lock_project_before_strategic_state_for_flush(
        session,
        project_id,
        exclusive=False,
    )
    table = StrategicMemoryProjectState.__table__
    connection = session.connection()
    if connection.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    elif connection.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    else:  # pragma: no cover - Blueprint supports PostgreSQL; SQLite is tests.
        raise RuntimeError(
            "Strategic memory receipt allocation requires PostgreSQL or SQLite ON CONFLICT"
        )
    now = _utcnow()
    statement = (
        dialect_insert(table)
        .values(
            project_id=project_id,
            source_high_watermark=1,
            incorporated_source_sequence=0,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[table.c.project_id],
            set_={
                "source_high_watermark": table.c.source_high_watermark + 1,
                "updated_at": now,
            },
        )
        .returning(table.c.source_high_watermark)
    )
    allocated = connection.execute(statement).scalar_one()
    for loaded in session.identity_map.values():
        if (
            isinstance(loaded, StrategicMemoryProjectState)
            and loaded.__dict__.get("project_id") == project_id
        ):
            session.expire(loaded)
    return int(allocated)


def _stage_strategic_memory_source_for_flush(
    session: _OrmSession,
    *,
    project_id: uuid.UUID,
    source_type: str,
    source_key: str,
    source_revision: str,
    content_hash: str,
    native_sequence: int | None,
    occurred_at: datetime,
    actor_kind: str | None,
    actor_id: str | None,
    resident_session_id: str | None = None,
    source_metadata: Dict[str, Any] | None = None,
    canonical_payload: bytes | None = None,
) -> None:
    """Add one pending receipt/disposition pair to the current transaction.

    This helper is deliberately narrow and synchronous so canonical ORM
    writers cannot commit source evidence without a durable receipt.  Services
    that ingest non-ORM/backfill producers use their own asynchronous contract
    but must preserve the same unique receipt key and state allocator rules.
    """

    source = StrategicMemorySource(
        project_id=project_id,
        source_sequence=_next_strategic_memory_source_sequence_for_flush(
            session, project_id=project_id
        ),
        native_sequence=native_sequence,
        ingestion_origin="live",
        source_type=source_type,
        source_key=source_key,
        source_revision=source_revision,
        content_hash=content_hash,
        actor_kind=actor_kind,
        actor_id=actor_id,
        occurred_at=occurred_at,
        recorded_at=_utcnow(),
        resident_session_id=resident_session_id,
        source_metadata=source_metadata or {},
        canonical_payload=canonical_payload,
    )
    session.add(source)
    session.add(StrategicMemorySourceDisposition(source_id=source.id, status="pending"))


def _canonical_problem_model_payload(value: object) -> bytes:
    """Reuse the receipt service's one canonical JSON implementation lazily.

    ``strategic_memory_sources`` imports models for its ORM contracts, so this
    import must stay inside the flush-time helper.  By then model import is
    complete and both live ORM writes and non-ORM/backfill receipts use the
    exact same strict number/key serialization rules.
    """

    from app.strategic_memory_sources import canonical_json_bytes

    return canonical_json_bytes(value)


def _current_context_authority_generation_for_flush(
    session: _OrmSession,
    *,
    project_id: uuid.UUID,
) -> int:
    """Read the generation advanced by the preceding authority flush hook."""

    value = session.connection().execute(
        select(StrategicMemoryProjectState.context_authority_generation).where(
            StrategicMemoryProjectState.project_id == project_id
        )
    ).scalar_one_or_none()
    if value is None:
        raise RuntimeError("Problem-model receipt requires an authority generation")
    return int(value)


@event.listens_for(_OrmSession, "before_flush")
def _dual_write_strategic_memory_receipts_from_canonical_orm_writers(
    session: _OrmSession,
    flush_context: object,
    instances: object,
) -> None:
    """Capture canonical live message/problem-model evidence transactionally.

    New ProjectMessages with a canonical sequence are source evidence on both
    activity and conversation surfaces.  Derived private Workday packets are
    execution coordination, not source evidence, and therefore remain
    excluded.  Legacy unsequenced writers are deliberately not silently
    upgraded here: they must first enter the canonical sequence path, rather
    than receive a fabricated source revision.

    The listener has no best-effort exception swallowing.  If receipt
    allocation fails, the native write fails too, preventing silent memory
    loss.  Later deterministic classification may mark a receipt no_memory;
    evidence capture itself is never model-gated.
    """

    from sqlalchemy import inspect as _sa_inspect

    flush_scope = (
        None
        if instances is None
        else {id(instance) for instance in instances}
    )

    for instance in list(session.new):
        if flush_scope is not None and id(instance) not in flush_scope:
            continue
        if not isinstance(instance, ProjectMessage):
            continue
        values = instance.__dict__
        if _is_derived_strategic_packet_context(values.get("context")):
            continue
        project_id = values.get("project_id")
        sequence = values.get("sequence")
        message_id = values.get("id")
        if project_id is None or message_id is None:
            raise RuntimeError("Canonical ProjectMessage receipt requires project_id and id")
        # A null sequence identifies a legacy/noncanonical writer.  It is not
        # valid evidence identity, so fail closed only for explicitly marked
        # canonical messages; the mixed-version migration window otherwise
        # remains readable until those writers are converted.
        if sequence is None:
            if values.get("surface") == MessageSurface.CONVERSATION:
                raise RuntimeError("Canonical ProjectMessage receipt requires a positive sequence")
            continue
        sequence_int = int(sequence)
        if sequence_int < 1:
            raise RuntimeError("Canonical ProjectMessage receipt requires a positive sequence")
        content = values.get("content") or ""
        content_bytes = str(content).encode("utf-8")
        _stage_strategic_memory_source_for_flush(
            session,
            project_id=project_id,
            source_type="project_message",
            source_key=str(message_id),
            source_revision=str(sequence_int),
            content_hash=hashlib.sha256(content_bytes).hexdigest(),
            native_sequence=sequence_int,
            occurred_at=values.get("created_at") or _utcnow(),
            actor_kind=values.get("from_type"),
            actor_id=values.get("actor_user_id"),
            resident_session_id=values.get("resident_session_id"),
            source_metadata={
                "surface": (
                    (values.get("surface") or MessageSurface.ACTIVITY).value
                    if isinstance(
                        values.get("surface") or MessageSurface.ACTIVITY,
                        MessageSurface,
                    )
                    else str(values.get("surface") or MessageSurface.ACTIVITY.value)
                )
            },
        )

    for instance in list(session.dirty):
        if flush_scope is not None and id(instance) not in flush_scope:
            continue
        if not isinstance(instance, Project):
            continue
        inspected = _sa_inspect(instance)
        if not inspected.attrs.problem_model.history.has_changes():
            continue
        project_id = (inspected.identity or (None,))[0]
        if project_id is None:
            raise RuntimeError("Problem-model receipt requires a persistent project identity")
        # Stable JSON bytes are the canonical immutable payload for this
        # mutable Project column; content_hash and source_revision are both
        # derived from exactly those bytes.
        canonical_payload = _canonical_problem_model_payload(
            instance.__dict__.get("problem_model") or {}
        )
        payload_hash = hashlib.sha256(canonical_payload).hexdigest()
        # The authority-generation value is monotonic even if an operator
        # changes a model and later restores byte-identical content.  The
        # receipt key therefore remains a temporal revision, while the hash
        # still identifies exactly what bytes were observed.
        context_generation = _current_context_authority_generation_for_flush(
            session, project_id=project_id
        )
        _stage_strategic_memory_source_for_flush(
            session,
            project_id=project_id,
            source_type="problem_model_revision",
            source_key=str(project_id),
            source_revision=str(context_generation),
            content_hash=payload_hash,
            native_sequence=None,
            occurred_at=_utcnow(),
            actor_kind="system",
            actor_id=None,
            source_metadata={"field": "problem_model"},
            canonical_payload=canonical_payload,
        )


class ProjectMessageAttachmentUpload(SQLModel, table=True):
    """Durable receipt for one authorized project-message attachment upload."""

    attachment_id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    actor_user_id: str = Field(sa_column=Column(String(255), nullable=False))
    storage_uri: str = Field(sa_column=Column(Text, nullable=False))
    filename: str = Field(sa_column=Column(String(512), nullable=False))
    content_type: str | None = Field(
        default=None,
        sa_column=Column(String(128), nullable=True),
    )
    expected_size_bytes: int = Field(sa_column=Column(BigInteger, nullable=False))
    expires_at: datetime = Field(
        sa_column=Column(NaiveUTCDateTime, nullable=False)
    )
    consumed_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    message_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(),
            # DEFERRABLE INITIALLY DEFERRED (migration 0634): the attachment
            # receipt's message_id is staged before the referenced projectmessage
            # row is INSERTed within the same create_operator_project_message
            # transaction, so the FK must be validated at COMMIT, not per-statement.
            ForeignKey(
                "projectmessage.id",
                ondelete="SET NULL",
                deferrable=True,
                initially="DEFERRED",
            ),
            nullable=True,
        ),
    )
    artifact_ref_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(),
            ForeignKey("artifactref.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            NaiveUTCDateTime,
            nullable=False,
            server_default=func.now(),
        ),
    )

    __table_args__ = (
        CheckConstraint(
            "expected_size_bytes >= 0",
            name="chk_projectmessageattachmentupload_size_nonnegative",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="chk_projectmessageattachmentupload_expiry_after_create",
        ),
        Index(
            "ix_projectmessageattachmentupload_project_expires",
            "project_id",
            "expires_at",
        ),
        Index(
            "ix_projectmessageattachmentupload_project_actor_created",
            "project_id",
            "actor_user_id",
            "created_at",
        ),
        Index(
            "ix_projectmessageattachmentupload_artifact_ref_id",
            "artifact_ref_id",
        ),
        Index(
            "ix_projectmessageattachmentupload_message_id",
            "message_id",
        ),
        Index(
            "ix_projectmessageattachmentupload_consumed_expires",
            "consumed_at",
            "expires_at",
        ),
    )


class DeploymentStatus(str, Enum):
    MONITORING = "MONITORING"  # New deploy, within observation window
    STABLE = "STABLE"  # Observation window passed, no issues
    ROLLED_BACK = "ROLLED_BACK"  # Reverted to last known good
    PAUSED = "PAUSED"  # External dependency down, dispatch halted
    HALTED = "HALTED"  # Multiple consecutive rollbacks, manual intervention needed


class DeploymentState(SQLModel, table=True):
    """
    Singleton row tracking the current deployment health.
    Used for rollback detection, monitoring windows, and progressive trust.
    """

    id: int = Field(default=1, primary_key=True)
    current_revision: str = Field(default="")  # Cloud Run K_REVISION
    current_sha: str = Field(default="")  # git commit SHA
    deployed_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    status: str = Field(
        default=DeploymentStatus.STABLE,
        sa_column=Column(String, default=DeploymentStatus.STABLE),
    )
    last_known_good_revision: str = Field(default="")
    last_known_good_sha: str = Field(default="")
    rolled_back_from_sha: str | None = Field(default=None)
    rollback_reason: str | None = Field(default=None)
    rollback_count: int = Field(default=0)  # consecutive rollbacks (reset on STABLE)
    pause_reason: str | None = Field(default=None)
    paused_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    # Cumulative pause-time, split by origin. Timers consult the sum via
    # app.pause_clock to subtract pause windows from their elapsed
    # calculations. Split for observability (self-inflicted vs external-dep
    # downtime); timer math always uses the sum.
    total_manual_paused_seconds: float = Field(default=0.0)
    total_auto_paused_seconds: float = Field(default=0.0)
    # "manual" or "auto" while paused; None while running. Routes the
    # in-flight window to the correct counter at resume.
    paused_kind: str | None = Field(default=None)
    # Pre-deploy saved max_concurrent_vms for PAUSE mode restore
    pre_pause_max_vms: int | None = Field(default=None)
    # Persisted runtime config — restored on startup so settings survive restarts
    runtime_max_concurrent_vms: int | None = Field(default=None)
    runtime_max_retries: int | None = Field(default=None)
    runtime_max_autonomous_tasks_per_hour: int | None = Field(default=None)
    runtime_max_global_autonomous_tasks_per_hour: int | None = Field(default=None)
    runtime_autonomous_research_interval_minutes: int | None = Field(default=None)
    runtime_autonomous_direction_cooldown_minutes: int | None = Field(default=None)
    runtime_autonomous_research_model: str | None = Field(default=None)
    runtime_autonomous_research_effort: str | None = Field(default=None)
    # Persisted circuit breaker states — survives restarts/deploys
    circuit_breaker_states: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    # Persisted zone health/cooldown — prevents retry storms after restart
    zone_health_state: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    # Persisted dict/list runtime config (task_type_models, agent_prompt_suffixes, etc.)
    runtime_config_json: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    # Persisted anomaly detection state — occurrence counts and active adjustments survive restarts
    persistent_anomaly_state: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    # Persisted direction failure streaks — holds survive restarts silently
    direction_streak_state: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    # Persisted pipeline events ring buffer — alert windows survive restarts
    pipeline_events_state: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    # Persisted GC-guard sweep state — cumulative totals survive process restarts
    # so the operator readiness signal (get_compute_image_gc_guard_readiness)
    # reflects durable observed history rather than resetting to zero on deploy.
    gc_guard_state: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    # Previous deploy tracking — populated when a new deploy supersedes the
    # current one. Lets RecentChangesContext builders compute the commit range
    # (prior_deploy_sha..current_sha) without a live git checkout.
    prior_deploy_sha: str | None = Field(default=None)

    # Timestamp when HALTED state was entered — used for auto-recovery timer
    halted_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    # Optimistic-concurrency version (audit 2026-04-25 W1 / F4): bumped on
    # every write so concurrent mutations from different code paths can
    # detect lost-write hazards without holding the row lock for the whole
    # post-commit cycle.
    version: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )


class MaxPlanAccount(SQLModel, table=True):
    """
    Persisted LLM account record used by the current Anthropic pool and future
    multi-provider account selection. In v1, Anthropic OAuth remains the only
    runnable path; other provider/auth combinations are stored for future use.
    Active task counts are tracked in-memory only.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: str = Field(index=True)
    name: str = Field(max_length=255)
    provider: LLMProviderName = Field(
        default=LLMProviderName.ANTHROPIC,
        sa_column=Column(
            SAEnum(
                LLMProviderName,
                name="llmprovider",
                values_callable=lambda obj: [e.value for e in obj],
            ),
            nullable=False,
            default=LLMProviderName.ANTHROPIC,
        ),
    )
    auth_mode: LLMAuthMode = Field(
        default=LLMAuthMode.OAUTH,
        sa_column=Column(
            SAEnum(
                LLMAuthMode,
                name="llmauthmode",
                values_callable=lambda obj: [e.value for e in obj],
            ),
            nullable=False,
            default=LLMAuthMode.OAUTH,
        ),
    )
    oauth_token: str | None = Field(
        default=None
    )  # Secret Manager resource name (or raw token for backward compat)
    claude_config_json: str | None = Field(
        default=None
    )  # ~/.claude.json content for onboarding bypass
    secret_blob_ref: str | None = Field(default=None)
    provider_metadata: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    enabled: bool = Field(default=True)
    total_tasks: int = Field(default=0)
    token_created_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )  # When the token was added (for age tracking)
    rate_limit_state: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    # Persisted rate-limit/usage headroom snapshot (latest probe data). Populated
    # by _persist_rate_limit_usage_to_db in max_plan_pool on each successful probe
    # so the data survives orchestrator restarts, and read back by
    # load_max_plan_pool to re-seed the in-memory usage meter.
    #
    # Two source schemas may appear here (keyed by "source"):
    #
    # 1. oauth_usage_probe (Anthropic OAuth accounts):
    #    {"five_hour": {"utilization": float, "resets_at": str},
    #     "seven_day":  {"utilization": float, "resets_at": str},
    #     "updated_at": str, "source": "oauth_usage_probe"}
    #
    # 2. messages_api_header (API-key accounts, from per-response rate-limit
    #    headers returned by the Messages API):
    #    {"unified_5h": {"utilization": float, "resets_at": str | None},
    #     "remaining_tokens": int | None, "limit_tokens": int | None,
    #     "updated_at": str, "source": "messages_api_header"}
    #    The unified_5h utilization is on a 0..100 scale; _snapshot_from_persisted
    #    _usage maps it to the five_hour window when rebuilding UsageSnapshot.
    #
    # Reader: _snapshot_from_persisted_usage (max_plan_pool) accepts both schemas.
    rate_limit_usage: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    # Usage caps (null = unlimited).
    daily_task_cap: int | None = Field(default=None)
    max_concurrent_tasks: int | None = Field(default=None)
    auto_disable_after_rate_limits: int | None = Field(default=None)
    daily_task_count: int = Field(default=0)
    daily_task_count_reset_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    # Per-project caps: JSON dicts mapping project_id (str) → cap value.
    # Null keys or a null dict means "no per-project cap configured" — the
    # global daily_task_cap / max_concurrent_tasks apply to all projects.
    # project_daily_task_cap: {project_id: int} — per-project daily cap
    # project_max_concurrent_tasks: {project_id: int} — per-project concurrent cap
    # project_state: {project_id: {active_tasks: int, daily_task_count: int}} —
    #   persisted per-project counters, mirrored in the in-memory pool entry.
    project_daily_task_cap: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    project_max_concurrent_tasks: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    project_state: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    disabled_reason: str | None = Field(default=None)
    # Set when auto-disable trips (rate-limit threshold). Persisted so
    # recheck_disabled_accounts can re-enable after cooldown across
    # orchestrator restarts. Null for never-disabled or admin-disabled
    # accounts (admin disable doesn't auto-recover).
    disabled_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, onupdate=_utcnow),
    )

    def is_in_rate_limit_cooldown(self, at_time: "datetime | None" = None) -> bool:
        """Return True if any model is still in rate-limit cooldown from persisted state.

        Reads self.rate_limit_state (JSONB, populated by _persist_rate_limit_state
        in max_plan_pool). Inlines the same cooldown-window math as
        max_plan_pool._cooldown_seconds_for_info to avoid a circular import
        (max_plan_pool imports models).

        at_time: reference timestamp for the check. Pass the failure instant so
        reaper lag between failure and classification cannot cause a missed
        correlation. Defaults to now.

        Returns False on any parse failure.
        """
        try:
            dm = (self.rate_limit_state or {}).get("disabled_models") or {}
            if not dm:
                return False
            now = at_time if at_time is not None else _utcnow()
            if getattr(now, "tzinfo", None) is not None:
                now = now.replace(tzinfo=None)
            for info in dm.values():
                at_str = info.get("at")
                if not at_str:
                    continue
                try:
                    at_dt = datetime.fromisoformat(str(at_str))
                    if at_dt.tzinfo is not None:
                        at_dt = at_dt.replace(tzinfo=None)
                except (ValueError, TypeError):
                    continue
                # Mirror max_plan_pool._cooldown_seconds_for_info inline.
                kind = info.get("kind")
                if kind == "quota_exhausted":
                    cooldown = 24 * 3600
                elif kind == "quarantine":
                    cooldown = 15 * 60
                elif kind == "regression":
                    cooldown = 6 * 3600
                else:
                    hits = info.get("hits", 1)
                    cooldown = min(30 * (2 ** (hits - 1)), 240) * 60
                    hint = info.get("retry_after_seconds")
                    if isinstance(hint, (int, float)) and hint > cooldown:
                        cooldown = int(hint)
                if (now - at_dt).total_seconds() < cooldown:
                    return True
            return False
        except Exception:  # noqa: BLE001 — documented: False on any parse failure
            return False


class ResidualCaseStatus(str, Enum):
    # Passive states (existing): set by record_residual_fingerprint + reaper
    ACTIVE = "ACTIVE"
    CLOSED_ORGANICALLY = "CLOSED_ORGANICALLY"
    IGNORED = "IGNORED"
    RESOLVED = "RESOLVED"
    # Lifecycle states (added 0054): set by autonomous_research spawn path +
    # notification_dispatch.post_residual_fix_result_message. Mirror the
    # CI auto-remediation lifecycle (open → remediating → merged|failed →
    # suspended after threshold) using existing ACTIVE as the open state.
    REMEDIATING = "REMEDIATING"
    MERGED = "MERGED"
    FAILED = "FAILED"
    SUSPENDED = "SUSPENDED"


# Backward-compatible alias used by admin_digest.py and autonomous_research.py
ResidualRemediationCaseStatus = ResidualCaseStatus


class ResidualRemediationCase(SQLModel, table=True):
    """Persistent record for a residual CODE_ERROR fingerprint.

    Created or updated whenever record_residual_fingerprint fires; closed by
    the reaper after 72 h of silence on the fingerprint.
    Status IGNORED suppresses auto-remediation spawning for that fingerprint.
    Status SUSPENDED suppresses auto-spawn after consecutive_failure_count
    reaches the escalation threshold; cleared manually via admin endpoint.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID | None = Field(
        default=None, foreign_key="project.id", index=True
    )
    fingerprint: str = Field(index=True, max_length=512)
    status: str = Field(
        default=ResidualCaseStatus.ACTIVE,
        sa_column=Column(String, nullable=False, default=ResidualCaseStatus.ACTIVE),
    )
    first_seen_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    last_seen_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    hit_count: int = Field(default=1)
    sample_message: str | None = Field(default=None)
    # Lifecycle fields (added 0054). All nullable / default-0 / default-[]
    # so existing rows backfilled by the migration's server_default work
    # without a Python-side touch.
    last_spawned_task_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(ForeignKey("task.id", ondelete="SET NULL"), nullable=True, index=True),
    )
    last_spawned_pr_number: int | None = Field(default=None)
    last_spawned_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    consecutive_failure_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    suspended_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    recovery_attempts: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    sample_messages: List[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'")),
    )
    sample_statuses: List[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'")),
    )
    task_ids: List[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'")),
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, onupdate=_utcnow),
    )
    # Audit-mode deferral fields (added 0056). Nullable / default-0 so existing
    # rows backfilled by migration server_default work without a Python-side touch.
    audit_defer_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    audit_deferred_system_id: str | None = Field(default=None)

    __table_args__ = (
        Index("ix_residual_case_status_last_seen", "status", "last_seen_at"),
        Index(
            "ix_residualremediationcase_project_status_spawned",
            "project_id",
            "status",
            "last_spawned_at",
        ),
        UniqueConstraint(
            "project_id",
            "fingerprint",
            name="ux_residualremediationcase_project_fingerprint",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'IGNORED', 'CLOSED_ORGANICALLY', 'RESOLVED', "
            "'REMEDIATING', 'SUSPENDED', 'MERGED', 'FAILED')",
            name="ck_residualremediationcase_status",
        ),
    )


class Artifact(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    task_id: uuid.UUID = Field(foreign_key="task.id", index=True)

    artifact_type: ArtifactType = Field(sa_column=Column(String, nullable=False))

    # Audit 2026-04-25 A14: partial UNIQUE on (task_id) WHERE artifact_type='PULL_REQUEST'
    # closes the SELECT-then-INSERT race in agent_status.update_agent_status that
    # could create two PR artifacts pointing at the same PR. Other artifact types
    # (GIT_COMMIT, ERROR_LOG, SCREENSHOT, ...) legitimately have multiple rows per
    # task, so the constraint is partial. Migration 0050.
    __table_args__ = (
        Index(
            "ux_artifact_task_pull_request",
            "task_id",
            unique=True,
            postgresql_where=text("artifact_type = 'PULL_REQUEST'"),
            sqlite_where=text("artifact_type = 'PULL_REQUEST'"),
        ),
        Index(
            "ix_artifact_pr_url_trgm",
            "pr_url",
            postgresql_using="gin",
            postgresql_ops={"pr_url": "gin_trgm_ops"},
        ),
        Index("ix_artifact_artifact_type", "artifact_type"),
        Index("ix_artifact_task_type", "task_id", "artifact_type"),
        Index("ix_artifact_created_at", "created_at"),
        Index(
            "ix_artifact_pull_request_created_at",
            "created_at",
            postgresql_where=text("artifact_type = 'PULL_REQUEST'"),
            sqlite_where=text("artifact_type = 'PULL_REQUEST'"),
        ),
    )

    # Git-based outputs
    commit_hash: str | None = None
    repo_url: str | None = None
    pr_url: str | None = None

    # For visual feedback or logs
    storage_uri: str | None = None
    logs: str | None = None

    # Logical identifier used by downstream consumers to reference this artifact
    # by slug rather than by UUID. NULL for legacy rows and artifact types that
    # don't participate in the consumption seam. Indexed for slug-lookup queries.
    logical_key: str | None = Field(
        default=None,
        sa_column=Column(String(256), nullable=True, index=True),
    )

    iteration: int = Field(default=0)
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )

    task: Task = Relationship(back_populates="artifacts")
    consumptions: list["ArtifactConsumption"] = Relationship(back_populates="artifact")


class ArtifactConsumption(SQLModel, table=True):
    """Durable record that a consumer task claimed a specific artifact.

    Provides a proof-of-consumption seam: when a downstream task actually reads
    and uses an artifact (identified by ``artifact_id`` and optionally the
    ``logical_key`` slug), it writes one row here. The unique constraint on
    ``(consumer_task_id, artifact_id)`` prevents double-recording. Producer and
    consumer task links are SET NULL on delete so consumption history survives
    task cleanup, mirroring ComputeJob.task_id.
    """

    __tablename__ = "artifactconsumption"
    __table_args__ = (
        UniqueConstraint(
            "consumer_task_id",
            "artifact_id",
            name="uq_artifactconsumption_consumer_artifact",
        ),
        Index("ix_artifactconsumption_artifact_id", "artifact_id"),
        Index("ix_artifactconsumption_consumer_task_id", "consumer_task_id"),
        Index("ix_artifactconsumption_producer_task_id", "producer_task_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    artifact_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("artifact.id", ondelete="SET NULL"),
            nullable=True,
            index=False,
        )
    )
    consumer_task_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(),
            ForeignKey("task.id", ondelete="SET NULL"),
            nullable=True,
            index=False,
        ),
    )
    producer_task_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(),
            ForeignKey("task.id", ondelete="SET NULL"),
            nullable=True,
            index=False,
        ),
    )

    # Snapshot of the slug at consumption time; may diverge from Artifact.logical_key
    # if the key is later updated, making consumption records self-describing.
    logical_key: str | None = Field(
        default=None,
        sa_column=Column(String(256), nullable=True),
    )

    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )

    artifact: "Artifact" = Relationship(back_populates="consumptions")


class KnowledgeDocument(SQLModel, table=True):
    """Durable, queryable home for ``output_kind='knowledge'`` work.

    Blueprint declares ``output_kind='knowledge'`` on several read-only task
    types, but historically that output lived only as ephemeral
    ``Task.plan_output`` read back through a narrow recent-findings window. A
    web-first ``web_research`` task instead persists ONE first-class
    ``KnowledgeDocument`` row here: a titled, summarized markdown document with
    web/file citations that the operator can list and full-text search, and
    that the forward-quality evaluator gated before it was stored.

    This is a PARALLEL lane to ``findings_index`` / the findings→spawn-code-task
    path — it does not replace or disturb them. ``source_task_id`` records the
    provenance of the producing task; ``forward_quality_verdict`` captures the
    gate verdict that admitted the document.

    Search: trigram GIN indexes on ``title`` and ``body`` (same precedent as
    ``Artifact.pr_url``) back project-scoped ILIKE / similarity search; a plain
    index on ``topic`` supports topic filtering. No new search dependency is
    introduced.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    # Project scope — every read path filters on this.
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)

    # Provenance: the task whose terminal output produced this document.
    # ON DELETE SET NULL (nullable): most documents are provenance-bound to their
    # producing task, but ``strategic_memory`` facts must OUTLIVE the task that
    # wrote them — a set_memory fact is durable behavioral memory, not a task
    # artifact. Nulling (rather than cascading) on task deletion preserves the
    # fact; ``source_citations`` retains the immutable provenance. Serialization
    # and read joins already treat this column as optional (see
    # knowledge_query.serialize_knowledge_document / the isouter join).
    source_task_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            ForeignKey("task.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )
    # Exact compute execution that produced this report. Unique when present so
    # terminal handlers and retries can idempotently ensure one document per job.
    source_compute_job_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(),
            ForeignKey("computejob.id", ondelete="SET NULL"),
            nullable=True,
            unique=True,
            index=True,
        ),
    )

    # Which task kind produced this document (``design`` vs ``web_research``).
    # Stored as the enum value in a plain String column so it is index-friendly
    # for origin filtering. Non-nullable with a Python default (so unseeded ORM
    # instances carry a value pre-flush) and a server default (so pre-existing
    # rows backfill to ``unknown``).
    source_kind: str = Field(
        default=SourceKind.UNKNOWN.value,
        sa_column=Column(
            String,
            nullable=False,
            server_default=text("'unknown'"),
        ),
    )

    title: str = Field(sa_column=Column(String, nullable=False))
    summary: str = Field(sa_column=Column(Text, nullable=False))
    body: str = Field(sa_column=Column(Text, nullable=False))

    # Optional free-form topic for filtering / grouping.
    topic: str | None = Field(default=None, sa_column=Column(String, nullable=True))

    # The strategic direction that motivated the producing task, carried on
    # ``task.context["direction_id"]`` (a string direction key like ``d-1a2b3c4d``,
    # NOT a FK into a direction table). Nullable: a web_research task may run
    # without a direction. Indexed for per-direction knowledge browse / counts.
    direction_id: str | None = Field(
        default=None, sa_column=Column(String, nullable=True)
    )

    # Sweep identity for compute_experiment docs. Populated when the task belongs
    # to a multi-task sweep (task.first_payload["sweep_id"]). Indexed for
    # per-sweep grouping queries. arm_label carries the human-readable arm name
    # (first_payload["experiment"] or ["arm_label"]); null for sweep-aggregate docs.
    sweep_id: str | None = Field(
        default=None, sa_column=Column(String(128), nullable=True)
    )
    arm_label: str | None = Field(
        default=None, sa_column=Column(String(256), nullable=True)
    )

    # Free-form tag list and citation list (web URLs plus any file refs).
    tags: List[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'")),
    )
    source_citations: List[Any] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'")),
    )

    # The forward-quality gate verdict that admitted this document:
    # {passed, reason, strategy, grader_task_id}. Always set on stored docs
    # (a failing verdict never reaches persistence), but nullable for safety.
    forward_quality_verdict: Dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )

    # Git provenance captured at write time (mirrors playbook entry stamping).
    # Nullable so legacy rows and older agents that don't supply them remain valid.
    authoring_branch: str | None = Field(
        default=None, sa_column=Column(String, nullable=True)
    )
    head_sha: str | None = Field(default=None, sa_column=Column(String, nullable=True))

    # Origin provenance for design/web_research documents spawned from research
    # findings. Nullable so legacy rows remain readable; read paths derive the
    # legacy/unknown state when older rows have no stamp.
    origin_state: str | None = Field(
        default=None, sa_column=Column(String(64), nullable=True)
    )
    origin_research_task_id: str | None = Field(
        default=None, sa_column=Column(String(64), nullable=True)
    )
    origin_parent_research_task_id: str | None = Field(
        default=None, sa_column=Column(String(64), nullable=True)
    )
    origin_finding_index: int | None = Field(
        default=None, sa_column=Column(Integer, nullable=True)
    )
    origin_finding_hash: str | None = Field(
        default=None, sa_column=Column(String(128), nullable=True)
    )
    origin_challenge_state: str | None = Field(
        default=None, sa_column=Column(String(64), nullable=True)
    )
    origin_challenge_record: Dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )

    # Lifecycle tracking for the strategist follow-up workflow.
    # New documents start as "pending"; the strategist can mark them
    # "consumed" (read + acted on) or "dismissed" (not actionable).
    lifecycle_status: str = Field(
        default="pending",
        sa_column=Column(
            String,
            nullable=False,
            server_default=text("'pending'"),
        ),
    )
    consumed_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    dismissed_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    dismissed_reason: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )

    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            NaiveUTCDateTime,
            nullable=False,
            server_default=func.now(),
            onupdate=_utcnow,
        ),
    )

    # Trigram GIN indexes mirror the Artifact.pr_url precedent so project-scoped
    # ILIKE / similarity search over title + body is index-backed on Postgres;
    # the topic b-tree index supports exact/prefix topic filtering. The
    # ``postgresql_*`` kwargs are dialect-scoped — SQLite create_all emits plain
    # indexes — so the test suite is unaffected.
    __table_args__ = (
        Index(
            "ix_knowledge_document_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
        Index(
            "ix_knowledge_document_body_trgm",
            "body",
            postgresql_using="gin",
            postgresql_ops={"body": "gin_trgm_ops"},
        ),
        Index("ix_knowledge_document_topic", "topic"),
        Index("ix_knowledge_document_direction", "direction_id"),
        Index("ix_knowledge_document_origin_state", "origin_state"),
        Index("ix_knowledge_document_origin_challenge_state", "origin_challenge_state"),
        Index("ix_knowledge_document_origin_finding_hash", "origin_finding_hash"),
        Index("ix_knowledge_document_sweep_id", "sweep_id"),
        Index("ix_knowledge_document_project_direction", "project_id", "direction_id"),
        Index("ix_knowledge_document_lifecycle_status", "lifecycle_status"),
        Index(
            "ix_knowledge_document_project_source_lifecycle",
            "project_id",
            "source_kind",
            "lifecycle_status",
        ),
        Index("ix_knowledge_document_project_lifecycle_created", "project_id", "lifecycle_status", "created_at"),
        # Covers the common list path: filter by project_id then ORDER BY
        # created_at, when no lifecycle_status predicate is present (the
        # existing project_lifecycle_created composite cannot serve those
        # queries). Used across knowledge_query.py and
        # strategic_context_package.py document-list loads.
        Index("ix_knowledge_document_project_created", "project_id", "created_at"),
    )


class SystemMaturity(SQLModel, table=True):
    """Per-system maturity snapshot written by self_audit tasks.

    Keyed on (project_id, system_id) with a unique constraint so upserts are
    well-defined. system_id is a free-form string that matches direction
    system_id values stored in the Project.directions JSONB list — no FK into
    a system catalog is enforced here.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)

    system_id: str = Field(index=True)

    # 0-100 overall health score; validated at ingest, not enforced by a DB check constraint.
    score: int

    # Per-component breakdown, e.g. {test_coverage, friction_density, claim_consistency, failure_mix}
    components: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )

    # audit | fundamentals | ship | pause — validated at ingest
    recommended_mode: str | None = None

    top_issues: List[Dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'")),
    )

    # Citations / file pointers / metric snapshots supporting the score
    evidence: List[Dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'")),
    )

    # TIMESTAMPTZ + NaiveUTCDateTime (alembic 0052 made these tz-aware;
    # round 3 audit H9 routes them through the type decorator so loads
    # return naive UTC consistent with the rest of the codebase, and
    # tz-aware writes get normalized via process_bind_param).
    last_audited_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )

    source_task_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(),
            ForeignKey("task.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )

    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            NaiveUTCDateTime,
            nullable=False,
            server_default=func.now(),
            onupdate=_utcnow,
        ),
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id", "system_id", name="uq_system_maturity_project_system"
        ),
    )


class AnomalyFingerprintState(SQLModel, table=True):
    """Durable per-project anomaly fingerprint record.

    Keyed by (project_id, fingerprint_key) so each distinct anomaly type
    for a project has exactly one row.  The in-memory persistent_anomalies
    dict in shared_state.py acts as a hot cache; this table is the source of
    truth that survives pod restarts, deploys, and OOM kills.

    Usage pattern:
      - Write: upsert after each anomaly detection cycle (see anomaly_detection.py).
      - Read: hydrate shared_state.persistent_anomalies on startup (see main.py).
    """

    __tablename__ = "anomalyfingerprintstate"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "fingerprint_key",
            name="ux_anomalyfingerprintstate_project_key",
        ),
        Index("ix_anomalyfingerprintstate_project_id", "project_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=False)
    fingerprint_key: str = Field(max_length=512)
    occurrence_count: int = Field(default=1)
    first_seen_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    last_seen_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    severity: str | None = Field(default=None, max_length=64)
    latest_details: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, onupdate=_utcnow),
    )


class Direction(SQLModel, table=True):
    """First-class normalised row for a single autonomous-improvement direction.

    P2 scale work (design-audit roadmap ``p2-direction-table``): historically
    every direction lived inside the unbounded ``Project.directions`` JSONB blob,
    so every per-direction mutation (research_history append, status flip, note
    add, proposal expiry, ...) took the project-row lock and rewrote (and
    TOAST-churned) the entire array. ~112 access sites contended on one global
    per-project mutex, and the ``_expire_stale_proposals`` sweep bulk-locked
    every autonomous project at once.

    This table promotes each direction to its own row so a single-direction
    mutation is a single-row write — no project-row lock, no whole-array
    rewrite. The natural key is ``(project_id, direction_key)`` where
    ``direction_key`` is the string ``id`` the rest of the system already uses
    (e.g. ``"d-1a2b3c4d"``, ``"crosscutting"``).

    Behaviour-preservation during the transition: the canonical write path
    (``task_locking.with_directions_locked``) DUAL-WRITES — it still maintains
    the ``Project.directions`` blob (so every existing read site is unchanged)
    AND mirrors each touched direction into this table via
    ``directions_repo.sync_directions``. The full direction dict round-trips in
    the ``data`` JSONB column, so no field is lost; the promoted columns
    (``status``, ``priority``, ``text``, timestamps) exist for indexed querying
    and to let future read sites move off the blob one at a time. See
    ``app.directions_repo`` for the accessor and the migration that backfills
    this table from the existing blobs.
    """

    __tablename__ = "direction"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "direction_key",
            name="ux_direction_project_key",
        ),
        # Hot path: list a project's directions, often filtered by status.
        Index("ix_direction_project_status", "project_id", "status"),
        # Reaper sweeps filter cross-project on status='staged' alone;
        # the composite index above cannot be used for a leading-column miss.
        Index(
            "ix_direction_status_staged",
            "status",
            postgresql_where=text("status = 'staged'"),
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    project_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
            index=False,
        ),
    )

    # The string direction id used everywhere else in the codebase
    # (``direction["id"]``). Free-form: agent-generated (``d-<hex>``),
    # ``"crosscutting"``, or user/strategic ids. Unique per project.
    direction_key: str = Field(sa_column=Column(String(128), nullable=False))

    # Promoted lifecycle status: active | paused | proposed | rejected |
    # expired | completed | retired | superseded. Stored as a free-form string
    # (no DB check constraint) to match the blob's open vocabulary.
    status: str = Field(
        default="active",
        sa_column=Column(String(32), nullable=False, server_default="active"),
    )

    # Promoted priority: high | medium | low (free-form; validated at ingest).
    priority: str | None = Field(
        default=None,
        sa_column=Column(String(16), nullable=True),
    )

    # The complete direction dict (everything the blob held: research_history,
    # history_aggregate, notes, research_context, recommended_mode_meta,
    # diagnosis_* keys, recent_findings_evidence, ...). Source of truth for the
    # row's full shape so behaviour is preserved across the cutover.
    # NOTE: declared before the ``text`` field below — that field name shadows
    # the module-level ``sqlalchemy.text`` within this class body, so the
    # ``server_default=text(...)`` call must come first.
    data: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )

    # Promoted direction text (the human-readable steer). Nullable because
    # synthetic/system directions may carry none.
    text: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )

    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            NaiveUTCDateTime,
            nullable=False,
            server_default=func.now(),
            onupdate=_utcnow,
        ),
    )

    # Criterion keys this direction is mapped to (captured at assignment).
    # List of criterion_key strings from VersionAcceptanceCriterion.
    # server_default uses a string literal (not text()) because the `text` name
    # is shadowed by the Direction.text field declared above in this class body.
    criterion_keys: List[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default="'[]'"),
    )


class DirectionEvent(SQLModel, table=True):
    """Append-only event log for direction lifecycle transitions.

    One row per notable lifecycle moment on a direction: creation, status
    transitions (active→paused, active→expired/retired, paused→active, ...),
    and hard deletion (``pruned``) from the table.  The table is written as a
    best-effort side-effect inside ``directions_repo.upsert_direction`` and
    ``sync_directions`` so every write path — lifecycle auto-pauses, strategic
    ops, user PATCH, staged activation, and whole-array pruning — emits events
    without touching any in-flight code.

    Queryable via ``GET /api/admin/projects/{pid}/directions/{key}/events``.
    """

    __tablename__ = "direction_event"
    __table_args__ = (
        Index("ix_direction_event_project_key", "project_id", "direction_key", "recorded_at"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    project_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )

    direction_key: str = Field(sa_column=Column(String(128), nullable=False))

    # "created" | "status_changed" | "pruned"
    event_kind: str = Field(sa_column=Column(String(32), nullable=False))

    prior_status: str | None = Field(
        default=None, sa_column=Column(String(32), nullable=True)
    )
    new_status: str | None = Field(
        default=None, sa_column=Column(String(32), nullable=True)
    )

    # Optional actor (agent id, "system", "operator") and human-readable reason.
    actor: str | None = Field(
        default=None, sa_column=Column(String(64), nullable=True)
    )
    reason: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )

    # Full direction dict at the moment of the event (nullable to keep row small
    # for no-op probes; always populated for created/status_changed/pruned).
    snapshot_data: Dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )

    recorded_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )


# --- Release / qualification domain models ---
# This cohesive domain cluster — ReleaseVersion, VersionAcceptanceCriterion,
# CriterionEvidence, CompletionDebtEntry, ReleaseDeployArtifact,
# QualificationVerdict, and AuditFindingResolution — was peeled into
# ``app.release_models`` to keep this module under its size guardrail. Those
# classes are imported and re-exported at the top of this module, so
# ``from app.models import ReleaseVersion`` (and siblings) still resolves and
# their tables register on ``SQLModel.metadata`` exactly as before.


class MigrationReservation(SQLModel, table=True):
    """A provisional Alembic revision id reserved for a migration-producing task.

    Migration authoring serializes the whole pipeline when the orchestrator
    holds a single "one migration-producing task at a time" slot for a task's
    entire agent run. A reservation breaks that convoy: the moment a task needs
    a revision id it is handed a provisional ``revision`` / ``down_revision`` off
    the merged chain head, and concurrent tasks each get the NEXT distinct
    monotonic id (the allocator considers both the merged head and every still
    ``ACTIVE`` reservation). The reservation persists enough lifecycle state for
    a later merge-time finalize (``FINALIZED`` + ``finalized_revision``) or an
    expiry sweep (``EXPIRED``) to reconcile the provisional id against what the
    migration actually shipped under.

    The agent-facing contract is intentionally additive (the ORM row is
    returned as-is) so a later claim-response rollout can expose the reservation
    without breaking existing consumers.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)
    # Detach-on-delete (SET NULL), NOT cascade: a reservation is durable
    # allocation history that must outlive its originating task so a merge-time
    # finalize/expiry can still reconcile the provisional id. Mirrors
    # ComputeJob.task_id.
    task_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(),
            ForeignKey("task.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )

    # The provisional revision id handed to the task, and the chain head it was
    # anchored onto. Both are the NNNN-style Alembic ids parsed by
    # ``migration_chain``; kept as strings to preserve zero-padding/width.
    provisional_revision: str = Field(
        sa_column=Column(String(64), nullable=False, index=True)
    )
    provisional_down_revision: str = Field(sa_column=Column(String(64), nullable=False))

    status: MigrationReservationStatus = Field(
        default=MigrationReservationStatus.ACTIVE,
        sa_column=Column(
            String,
            nullable=False,
            server_default=MigrationReservationStatus.ACTIVE.value,
        ),
    )
    # The revision id the migration actually shipped under, set at merge-time
    # finalize. Usually equals ``provisional_revision``; differs only when a
    # merge-time renumber moved the migration.
    finalized_revision: str | None = Field(
        default=None, sa_column=Column(String(64), nullable=True)
    )

    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            NaiveUTCDateTime,
            nullable=False,
            server_default=func.now(),
            onupdate=_utcnow,
        ),
    )

    __table_args__ = (
        # Hot path: scan a project's ACTIVE reservations during allocation.
        Index(
            "ix_migrationreservation_project_status",
            "project_id",
            "status",
        ),
    )


class ImprovementLaneStatus(str, Enum):
    """Lifecycle of a persistent improvement lane.

    PROPOSED -> ACTIVE (first phase dispatched)
    ACTIVE -> PARKED (checkpoint verified; no live task)
    PARKED -> ACTIVE (next phase dispatched; current_task_id bound)
    ACTIVE | PARKED -> RETIRED (goal met / version closed; terminal)
    ACTIVE | PARKED -> KILLED (pilot kill or operator action; terminal)
    PROPOSED -> KILLED (failed/abandoned activation aged out by the
        reconciliation sweep; terminal)
    PROPOSED -> RETIRED (chain merged/closed before the lane ever activated;
        terminal)
    """

    PROPOSED = "PROPOSED"
    ACTIVE = "ACTIVE"
    PARKED = "PARKED"
    RETIRED = "RETIRED"
    KILLED = "KILLED"


_IMPROVEMENT_LANE_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("PROPOSED", "ACTIVE"),
        ("ACTIVE", "PARKED"),
        ("PARKED", "ACTIVE"),
        ("ACTIVE", "RETIRED"),
        ("PARKED", "RETIRED"),
        ("ACTIVE", "KILLED"),
        ("PARKED", "KILLED"),
        # A PROPOSED lane whose activation never completed (failed/abandoned
        # dispatch, or an operator/admin close before the first phase ran) had
        # no permitted terminal exit, so it could strand forever holding its
        # direction scope. These two edges let the reconciliation sweep and the
        # pilot closers age such a lane to a terminal state. KILLED carries the
        # abandoned/failed-activation semantics the sweep uses; RETIRED covers a
        # chain that merged/closed before the lane ever activated.
        ("PROPOSED", "KILLED"),
        ("PROPOSED", "RETIRED"),
    }
)

# Maximum checkpoint records kept per lane (rolling window).
_LANE_CHECKPOINT_LIMIT = 50
# Maximum serialized manifest bytes (~8KB).
_LANE_MANIFEST_MAX_BYTES = 8192
# Maximum phase requeues before escalating to strategist.
_LANE_MAX_PHASE_REQUEUES = 3


class ImprovementLane(SQLModel, table=True):
    """Durable control-plane record for a multi-phase improvement lane.

    Product state (code, commits, PRs) lives in git on ``branch_name``.
    Control state (status, checkpoint history, manifest, accounting) lives
    here. Every phase executes as a normal fresh-context task; the lane row
    provides the bounded inter-phase carrier (manifest + checkpoint info).

    Schema matches knowledge doc 18840e55 (v10 Persistent Improvement Lanes).
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    direction_id: str | None = Field(
        default=None, sa_column=Column(String(64), nullable=True, index=True)
    )
    version_number: int | None = Field(
        default=None, sa_column=Column(Integer, nullable=True)
    )
    title: str = Field(sa_column=Column(Text, nullable=False))
    goal: str = Field(sa_column=Column(Text, nullable=False))

    status: str = Field(
        default=ImprovementLaneStatus.PROPOSED.value,
        sa_column=Column(
            String,
            nullable=False,
            server_default=ImprovementLaneStatus.PROPOSED.value,
        ),
    )

    # Product state pointers
    branch_name: str = Field(sa_column=Column(String(255), nullable=False))
    base_branch: str = Field(sa_column=Column(String(255), nullable=False))
    last_checkpoint_sha: str | None = Field(
        default=None, sa_column=Column(String(64), nullable=True)
    )
    last_checkpoint_phase: str | None = Field(
        default=None, sa_column=Column(String(64), nullable=True)
    )
    checkpoint_seq: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    checkpoints: List[Any] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'")),
    )
    manifest: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )

    # Execution state
    current_task_id: uuid.UUID | None = Field(
        default=None, sa_column=Column(GUID(), nullable=True)
    )
    # COLD | WARM_RESERVED | LEASED
    workspace_state: str = Field(
        default="COLD",
        sa_column=Column(String(32), nullable=False, server_default="COLD"),
    )
    workspace_vm_instance_id: str | None = Field(
        default=None, sa_column=Column(String(255), nullable=True)
    )
    phase_requeues: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )

    # Accounting rollups (per-phase detail lives in checkpoints[])
    llm_input_tokens_total: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    llm_output_tokens_total: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    vm_cost_usd_total: float = Field(
        default=0.0,
        sa_column=Column(Float, nullable=False, server_default=text("0.0")),
    )
    reserved_idle_cost_usd_total: float = Field(
        default=0.0,
        sa_column=Column(Float, nullable=False, server_default=text("0.0")),
    )

    # Reservation expiry-cause telemetry (warm-resume diagnosability). Each warm
    # reservation this lane held ends exactly one way; these counters make a low
    # warm_resume_fraction explainable on the lanes analytics surface:
    #   * claimed_by_fix       — the fix phase actually leased the reserved member
    #                            (the GOOD outcome that lifts warm_resume_fraction).
    #   * expired_before_fix   — the soft TTL lapsed and the sweep reclaimed the
    #                            member because the chain stopped awaiting its fix
    #                            (lane no longer PARKED) before the hard ceiling.
    #   * force_expired        — released by the hard absolute ceiling, a pilot
    #                            KILL, a chain RETIRE, a checkpoint-exhausted close,
    #                            or a reaped member.
    reservation_claimed_by_fix_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    reservation_expired_before_fix_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    reservation_force_expired_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )

    # Pilot arm: "lane" | "control" | None (intent-to-treat assignment)
    pilot_arm: str | None = Field(
        default=None, sa_column=Column(String(32), nullable=True)
    )

    # Timestamps
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    parked_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    last_resumed_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    retired_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            NaiveUTCDateTime,
            nullable=False,
            server_default=func.now(),
            onupdate=_utcnow,
        ),
    )

    __table_args__ = (
        # Sweep hot path: scan ACTIVE lanes looking for stuck tasks.
        Index("ix_improvementlane_project_status", "project_id", "status"),
        # Direction scope: find lanes for a given direction.
        Index("ix_improvementlane_direction_status", "direction_id", "status"),
        # Stale-parked-lane sweep: expired PARKED lanes.
        Index(
            "ix_improvementlane_status_parked_at",
            "status",
            "parked_at",
            postgresql_where=text("status = 'PARKED'"),
        ),
    )


class TaskWakeRegistration(SQLModel, table=True):
    """A durable registration that a :class:`Task` is PARKED waiting on an
    external, non-agent wake source, plus the delivery/result state of waking it.

    This is the persistence backing the task park-and-wake primitive. When a task
    parks, it releases its VM/JWT/fleet slot and writes one of these rows naming:

    * ``wake_source_kind`` / ``wake_source_ref`` — *which* external source it
      waits on (e.g. ``"compute_job_terminal"`` + the ComputeJob id). The kind is
      an open string, not an enum, so a future source (e.g. a rate-limit clearing)
      can register without a migration — only the consumer that fires it is new.
    * ``source_metadata`` — free-form context the firing side and operators read.
    * ``wake_action`` — RESUME (re-queue the task) vs FINALIZE (drive it terminal).
    * ``deadline_at`` — when the reaper should age the registration out rather than
      let it strand forever if the source never fires.
    * ``delivery_state`` — PENDING → FIRED → DELIVERED / EXPIRED / FAILED.

    Exactly-once is enforced at delivery: the leader-owned worker claims a FIRED
    row ``FOR UPDATE SKIP LOCKED`` and flips it to DELIVERED in the same critical
    section it advances the PARKED task, so neither a retry nor a concurrent
    worker can advance the same parked task twice.

    ``task_id`` is non-null and CASCADE-deletes with its task: unlike ComputeJob /
    MigrationReservation (durable history that must outlive the task), a wake
    registration is meaningless once its task is gone.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    task_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("task.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
    )
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)

    # Source identity. ``wake_source_kind`` is an open categorical string (the
    # first shipped value is "compute_job_terminal"); ``wake_source_ref`` is the
    # specific instance id within that kind (e.g. the ComputeJob UUID).
    wake_source_kind: str = Field(sa_column=Column(String(64), nullable=False))
    wake_source_ref: str | None = Field(
        default=None, sa_column=Column(String(255), nullable=True)
    )
    source_metadata: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )

    wake_action: WakeAction = Field(
        default=WakeAction.RESUME,
        sa_column=Column(
            String, nullable=False, server_default=WakeAction.RESUME.value
        ),
    )
    delivery_state: WakeDeliveryState = Field(
        default=WakeDeliveryState.PENDING,
        sa_column=Column(
            String, nullable=False, server_default=WakeDeliveryState.PENDING.value
        ),
    )

    requested_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    # Earliest the reaper should EXPIRE this registration if the source has not
    # fired. NULL means "no timeout" (wait indefinitely for the source).
    deadline_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    fired_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    claimed_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    delivered_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    # How many times the worker has attempted delivery for this row (forensic).
    attempt_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    # Terminal outcome of delivery: e.g. "resumed", "finalized", "expired",
    # "task_not_parked". Short categorical for operator surfaces.
    result_state: str | None = Field(
        default=None, sa_column=Column(String(32), nullable=True)
    )
    result_detail: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )

    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            NaiveUTCDateTime,
            nullable=False,
            server_default=func.now(),
            onupdate=_utcnow,
        ),
    )

    __table_args__ = (
        # Worker hot path: claim FIRED registrations and age out due PENDING ones.
        # The sweep filters on delivery_state and orders/bounds by deadline_at, so
        # a (delivery_state, deadline_at) composite keeps that scan tight to the
        # small set of live registrations.
        Index(
            "ix_taskwakeregistration_state_deadline",
            "delivery_state",
            "deadline_at",
        ),
        # Firing hot path: fire_task_wakes() point-looks-up PENDING registrations
        # by (wake_source_kind, wake_source_ref) on every compute-job terminal
        # transition. A (wake_source_kind, wake_source_ref, delivery_state)
        # composite keeps that lookup off a full table scan as parked-task
        # registrations accumulate.
        Index(
            "ix_taskwakeregistration_source_state",
            "wake_source_kind",
            "wake_source_ref",
            "delivery_state",
        ),
    )


class EvergreenBacklogItem(SQLModel, table=True):
    """Durable ledger of curated "evergreen" backlog candidates.

    The utilization-triggered evergreen auto-fill mechanism (direction
    d-2877ddc7) spawns LOW-priority research probes from this ledger when the
    fleet is idle (low utilization) or all active directions are parked /
    proof-waiting — additive parallelism that never competes with the
    release-critical path. Each row is one candidate, project-scoped and keyed
    by a stable ``finding_key`` so the same latent gap is never enrolled twice.

    Lifecycle (``status`` — closed vocabulary, see
    ``app.evergreen_backlog.BACKLOG_STATUSES``):

    * ``enrolled``  — eligible for selection, never spawned.
    * ``spawned``   — a research probe is live (``spawned_task_id`` set).
    * ``advanced``  — the probe's surviving findings spawned implementation
                      children through the existing research_completion gates.
    * ``verified_stale`` — the probe's findings were rejected / non-actionable
                      by the reproduce-on-HEAD / actionability gates; the gap no
                      longer reproduces, so the item retires.
    * ``retired``   — exhausted attempts or operator-retired; no longer eligible.

    NO direct fix/create spawn path touches this table — implementation children
    come only from research_completion's surviving-finding gates.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    project_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
    )

    # Stable per-project dedup key for the latent gap this item represents.
    finding_key: str = Field(
        sa_column=Column(String(128), nullable=False, index=True),
    )

    title: str = Field(sa_column=Column(String(512), nullable=False))
    detail: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    # Lifecycle status (closed vocabulary — validated in app.evergreen_backlog).
    status: str = Field(
        sa_column=Column(String(32), nullable=False, server_default="enrolled"),
    )
    # Terminal/interim outcome reason (closed vocabulary), nullable until set.
    outcome: str | None = Field(
        default=None, sa_column=Column(String(64), nullable=True)
    )
    # How the item was enrolled: e.g. "manual", "deferred_memo".
    provenance: str = Field(
        sa_column=Column(String(64), nullable=False, server_default="manual"),
    )

    # Optional direction this candidate belongs to (free-form id; conflict checks
    # use it to skip enrolling against a direction that already has live work).
    direction_id: str | None = Field(
        default=None, sa_column=Column(String(128), nullable=True, index=True)
    )

    attempts: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )

    # The research probe currently representing this item (nullable; SET NULL so
    # the ledger row outlives the task row for durable provenance).
    spawned_task_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(),
            ForeignKey("task.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )

    # SHA the most recent probe verified its finding against (reproduce-on-HEAD).
    verified_against_sha: str | None = Field(
        default=None, sa_column=Column(String(64), nullable=True)
    )

    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            NaiveUTCDateTime,
            nullable=False,
            server_default=func.now(),
            onupdate=_utcnow,
        ),
    )

    __table_args__ = (
        # Project-scoped finding-key uniqueness: the same latent gap can never be
        # enrolled twice within a project.
        UniqueConstraint(
            "project_id",
            "finding_key",
            name="uq_evergreenbacklogitem_project_finding",
        ),
        # Selection hot path: scan a project's enrolled candidates by status.
        Index(
            "ix_evergreenbacklogitem_project_status",
            "project_id",
            "status",
        ),
    )


class EvergreenFillDecisionLog(SQLModel, table=True):
    """Durable per-project, per-tick fill-decision ledger for evergreen observe-mode.

    One row is written for every project evaluated on each check_evergreen_fill
    tick, regardless of mode (off/observe/enforce) or decision (create/noop).
    The full FleetSignal context is persisted so observe-mode behavior can be
    reviewed across deploys for false-alarm analysis (direction d-15685fb3).

    project_id and item_id use ON DELETE SET NULL so the ledger row outlives
    the project/item rows for long-term audit purposes.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    ts: datetime = Field(
        sa_column=Column(NaiveUTCDateTime, nullable=False, index=True),
    )

    project_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(),
            ForeignKey("project.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )

    action: str = Field(sa_column=Column(String(16), nullable=False))
    reason: str = Field(sa_column=Column(String(64), nullable=False))
    trigger: str | None = Field(
        default=None, sa_column=Column(String(64), nullable=True)
    )
    mode: str = Field(sa_column=Column(String(16), nullable=False))

    # Fleet utilization snapshot at decision time.
    util_now: float | None = Field(default=None, sa_column=Column(Float, nullable=True))
    util_threshold: float = Field(sa_column=Column(Float, nullable=False))
    dispatchable_queue_depth: int = Field(sa_column=Column(Integer, nullable=False))
    vm_slots_available: int | None = Field(
        default=None, sa_column=Column(Integer, nullable=True)
    )
    active_evergreen_probes: int = Field(sa_column=Column(Integer, nullable=False))
    daily_spawn_count: int = Field(sa_column=Column(Integer, nullable=False))
    has_candidate: bool = Field(sa_column=Column(Boolean, nullable=False))

    # Candidate identity at decision time (nullable when no candidate selected).
    item_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(),
            ForeignKey("evergreenbacklogitem.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )
    finding_key: str | None = Field(
        default=None, sa_column=Column(String(128), nullable=True)
    )

    __table_args__ = (
        # Windowed-read hot path: per-project time-range scans.
        Index(
            "ix_evergreenfilldecisionlog_project_ts",
            "project_id",
            "ts",
        ),
    )


class AuditLaneDecisionLog(SQLModel, table=True):
    """Durable per-cycle audit-lane decision log for structural_health_audit and
    metrics_integrity_sweep observe-mode evidence.

    One row per audit cycle (structural_health_audit) or sweep tick
    (metrics_integrity_sweep), written regardless of mode (observe/enforce) and
    regardless of whether any findings tripped. This makes the observe-window
    evidence durable across process restarts and deploys, so the 'observe phase
    validated it' argument is backed by a queryable cross-deploy record rather
    than an in-memory ring that resets on every restart.

    Lane discriminator: 'structural_health' | 'metrics_integrity'.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    ts: datetime = Field(
        sa_column=Column(NaiveUTCDateTime, nullable=False, index=True),
    )

    lane: str = Field(sa_column=Column(String(32), nullable=False))
    mode: str = Field(sa_column=Column(String(16), nullable=False))

    # True when at least one check/axis tripped (would-have-fired signal).
    tripped: bool = Field(sa_column=Column(Boolean, nullable=False))

    # Count of checks/findings enrolled in this cycle (0 in observe mode).
    enrolled: int = Field(default=0, sa_column=Column(Integer, nullable=False))

    # Bounded summary payload (check counts, flag names, etc.) for diagnostics.
    payload: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False),
    )

    __table_args__ = (
        Index(
            "ix_auditlanedecisionlog_lane_ts",
            "lane",
            "ts",
        ),
    )


class LocalRunner(SQLModel, table=True):
    """A pull-based fleet runner registered on operator hardware.

    A runner enrolls via ``POST /api/admin/projects/{id}/runners/register`` (which
    returns a one-time secret token), then polls ``POST /api/runner/next-task`` with
    that token to receive tasks the orchestrator has pinned to it. The orchestrator
    pins QUEUED tasks to an active registered runner BEFORE the warm-pool/GCP dispatch
    paths, so operator hardware becomes a first-class fleet member with no LOCAL_MODE
    flag. Only the SHA256 hash of the token is ever stored.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # Deleted with the project so a runner can't outlive its owning project.
    project_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
    )
    # SHA256 hex digest of the runner's secret token (never the plaintext).
    token_hash: str = Field(sa_column=Column(String(64), nullable=False, index=True))
    hostname: str | None = Field(
        default=None, sa_column=Column(String(255), nullable=True)
    )
    version: str | None = Field(
        default=None, sa_column=Column(String(64), nullable=True)
    )
    # Clean git commit identity reported by the runner's checkout. This is
    # separate from ``version`` because the latter is a human-readable
    # ``git describe`` value that may include a tag or dirty marker.
    source_sha: str | None = Field(
        default=None, sa_column=Column(String(64), nullable=True)
    )
    # Durable FULL agent source git SHA the runner's agents are serving, observed
    # from agent heartbeats (persist_runner_agent_source_sha). Mirrors
    # PoolVm.agent_source_sha so fleet-serving release-artifact checks can reason
    # about self-hosted checkout runners across orchestrator restarts. Distinct
    # from ``source_sha`` above, which is derived from the runner's own
    # ``git describe`` value and may be abbreviated.
    agent_source_sha: str | None = Field(
        default=None, sa_column=Column(String(64), nullable=True)
    )
    # "active" | "inactive" | "deleted"
    status: str = Field(
        default="active",
        sa_column=Column(String(16), nullable=False, server_default=text("'active'")),
    )
    # {"task_types": "all" | [str], "has_gpu": bool (heartbeat-reported ground truth),
    #  "gpu_count": int, "gpu_names": [str], "gpu_utilization_pct": int (0-100),
    #  "disk_free_bytes": int, "max_concurrent": int, "active_task_ids": [str]}
    #  — has_gpu et al. are overwritten at first heartbeat. gpu_utilization_pct and
    #  disk_free_bytes are display-only trust telemetry, stashed here alongside the
    #  other heartbeat-reported hardware fields rather than as dedicated columns.
    #  Live-safe config channel (all here, no columns/migration): the operator-set
    #  desired subset is "max_concurrent" / "max_concurrent_compute" / "task_types"
    #  / "gpu_indices_allowed" ("all" | [int]); the daemon echoes back what it has
    #  applied as "applied_config" (same shape) + "applied_config_generation" (the
    #  content hash it last converged to). See app.runner_config.
    capabilities: Dict[str, Any] = Field(
        default_factory=lambda: {
            "task_types": "all",
            "has_gpu": False,
            "max_concurrent": 2,
        },
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    last_seen_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    # Trust telemetry (heartbeat-reported): the last time this runner reported a
    # job succeeding, and the count of job failures since its last success. First-
    # class columns because the "connected but failing" degraded state and the
    # Fleet Runners panel key on them — a runner can heartbeat fine while every job
    # fails (false-green), and these two fields are what distinguishes "healthy"
    # from "healthy AND working".
    last_successful_job_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    consecutive_job_failures: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    # When the runner's current token was created/rotated (for age/staleness tracking).
    token_created_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    # Monotonic credential generation. A heartbeat records the generation it
    # authenticated with; the Fleet UI confirms token rotation only when that
    # acknowledgement equals the current generation. This avoids inferring
    # credential use from last_seen_at wall-clock ordering.
    token_generation: int = Field(
        default=1,
        sa_column=Column(Integer, nullable=False, server_default=text("1")),
    )
    last_heartbeat_token_generation: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )

    __table_args__ = (
        # Dispatch-cache hot path: per-project active-runner scans filtered on
        # status + last_seen_at recency.
        Index(
            "ix_localrunner_project_status",
            "project_id",
            "status",
        ),
        # Cross-project fleet health scan (admin_runners.py) filters on
        # status alone (status != 'deleted') without a project_id predicate,
        # which the composite above cannot serve. Standalone status index
        # avoids a full sequential scan over the runners table.
        Index("ix_localrunner_status", "status"),
    )


class ServiceCredential(SQLModel, table=True):
    """A revocable machine credential for non-human API consumers.

    Authenticates as ``principal_user_id`` (a non-human user id holding ordinary
    ProjectMember rows), restricted to the endpoint allowlists of its ``scopes``
    (see app/service_credentials.py). Never admin; plaintext token is shown once
    at mint time and only its SHA256 hash is stored.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(sa_column=Column(String(64), nullable=False))
    description: str | None = Field(
        default=None, sa_column=Column(String(255), nullable=True)
    )
    principal_user_id: str = Field(
        sa_column=Column(String(128), nullable=False, index=True)
    )
    # SHA256 hex digest of the secret token (never the plaintext).
    token_hash: str = Field(sa_column=Column(String(64), nullable=False, unique=True))
    # First characters of the plaintext (e.g. "bpsc_Ab12Cd34") for display/identification.
    token_prefix: str = Field(sa_column=Column(String(16), nullable=False))
    scopes: List[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'")),
    )
    expires_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    revoked_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    created_by: str = Field(sa_column=Column(String(128), nullable=False))
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    last_used_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    last_used_path: str | None = Field(
        default=None, sa_column=Column(String(255), nullable=True)
    )

    __table_args__ = (
        # Name uniqueness is PARTIAL on revoked_at: at most one ACTIVE (non-
        # revoked) credential may hold a given name, but a revoked name may be
        # re-minted. Migration 0723's full UNIQUE on name blocked that reuse —
        # the app-level mint pre-check always allowed it — so 0724 narrows it
        # to this partial unique index.
        Index(
            "uq_servicecredential_name_unrevoked",
            "name",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
            sqlite_where=text("revoked_at IS NULL"),
        ),
    )


class RunnerProjectBinding(SQLModel, table=True):
    """Runner-level project allow-list — one row per (runner, project) grant.

    A fleet ``LocalRunner`` historically pinned to exactly one project via its
    ``project_id`` FK (the ENROLLMENT / default project). This join table adds a
    many-to-many allow-list so ONE daemon per physical box can serve N projects
    under a single unified GPU-index/concurrency ledger, without spinning up a
    second per-project daemon that would double-book the same hardware.

    This is the ADDITIVE, non-breaking schema foundation only: ``LocalRunner.
    project_id`` is UNCHANGED and remains the anchor member of the runner's own
    allow-list (a runner is always implicitly allowed on its enrollment project
    even if no binding row exists — see ``app.runner_project_binding.
    allowed_project_ids``). Dispatch/authz/claim rewiring lands in a follow-on
    that depends on this table.
    """

    __tablename__ = "runner_project_binding"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # Deleted with the runner so bindings never outlive their runner row.
    runner_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("localrunner.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
    )
    # Deleted with the project so bindings never outlive their project row.
    project_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )

    __table_args__ = (
        # A runner cannot be double-bound to the same project.
        UniqueConstraint(
            "runner_id",
            "project_id",
            name="uq_runner_project_binding_runner_project",
        ),
    )


class RunnerHeartbeatSnapshot(SQLModel, table=True):
    """Append-only time-series of runner heartbeat telemetry.

    The ``LocalRunner.capabilities`` JSONB holds only the *latest* telemetry
    values — each heartbeat overwrites the previous ones — so there is no
    history to sparkline GPU/disk/memory pressure over time, and per-runner
    outcome rollups can only be computed by scanning every ``ComputeJob`` row.
    This table captures one row per heartbeat (fire-and-forget on the heartbeat
    path) so the Fleet panel can show recent trends without touching the hot
    dispatch cache. Rows are retention-trimmed (default 7 days) by a periodic
    sweep — they are pure telemetry, never a source of truth for dispatch.
    """

    __tablename__ = "runner_heartbeat_snapshot"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # Deleted with the runner so snapshots never outlive their runner row.
    # No standalone index here: the composite (runner_id, captured_at) index
    # below already covers the FK prefix and the per-runner window query, so a
    # second single-column index would only add write cost on this hot path.
    runner_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("localrunner.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    # When the telemetry was captured (heartbeat receipt time). Indexed so the
    # per-runner recent-window query and the retention-cleanup delete are both
    # index-driven.
    captured_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, index=True),
    )
    gpu_utilization_pct: int | None = Field(
        default=None, sa_column=Column(Integer, nullable=True)
    )
    disk_free_bytes: int | None = Field(
        default=None, sa_column=Column(BigInteger, nullable=True)
    )
    memory_free_bytes: int | None = Field(
        default=None, sa_column=Column(BigInteger, nullable=True)
    )
    cpu_load_avg: float | None = Field(
        default=None, sa_column=Column(Float, nullable=True)
    )
    active_task_count: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    active_compute_job_count: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    agent_available: bool | None = Field(
        default=None, sa_column=Column(Boolean, nullable=True)
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )

    __table_args__ = (
        # Retention-cleanup + per-runner recent-window scans both filter on
        # (runner_id, captured_at). A composite index serves the panel query
        # (one runner, last hour) and the sweep's global age filter.
        Index(
            "ix_runner_heartbeat_snapshot_runner_captured",
            "runner_id",
            "captured_at",
        ),
    )


class UiLoadTimingSample(SQLModel, table=True):
    """Persisted admin-UI initial-load timing sample.

    Written on every POST /api/admin/ui-load-timing so samples survive
    process restarts and can span before/after deploy comparisons.
    Capped at 200 rows per surface via application-layer trim on insert.
    """

    __tablename__ = "ui_load_timing_sample"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    surface: str = Field(sa_column=Column(String(64), nullable=False, index=True))
    page_ready_ms: int = Field(sa_column=Column(Integer, nullable=False))
    sample_type: str = Field(default="first_commit", sa_column=Column(String(32), nullable=False))
    recorded_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )


class UiLoadTimingBaseline(SQLModel, table=True):
    """Pinned baseline snapshot of UI load timing per (surface, sample_type).

    Created on-demand via POST /api/admin/ui-load-timing/pin-baseline to
    capture a before-improvement baseline that survives the 200-sample FIFO
    cap. Immutable once written; a new pin creates new rows rather than
    updating existing ones.
    """

    __tablename__ = "ui_load_timing_baseline"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    surface: str = Field(sa_column=Column(String(64), nullable=False, index=True))
    sample_type: str = Field(sa_column=Column(String(32), nullable=False))
    p50: int = Field(sa_column=Column(Integer, nullable=False))
    p90: int = Field(sa_column=Column(Integer, nullable=False))
    count: int = Field(sa_column=Column(Integer, nullable=False))
    label: str = Field(default="", sa_column=Column(String(128), nullable=False))
    pinned_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )


class NotificationDeadLetter(SQLModel, table=True):
    """Durable record of an external notification that failed to deliver.

    The webhook retry queue is a bounded in-memory deque: entries are lost on
    orchestrator restart, the oldest is silently evicted when the queue is
    full, and a notification that exhausts its retries just disappears. When
    either terminal outcome occurs, a row is written here so the failed
    escalation is operator-visible and re-drivable instead of vanishing.

    ``reason`` is ``"exhausted"`` (all retry attempts used up) or
    ``"evicted"`` (dropped from the full retry queue to make room). ``payload``
    and ``body`` preserve the original notification so it can be re-driven.
    """

    __tablename__ = "notification_dead_letter"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: str = Field(sa_column=Column(String(64), nullable=False, index=True))
    event_type: str = Field(sa_column=Column(String(128), nullable=False))
    reason: str = Field(sa_column=Column(String(16), nullable=False))
    attempts: int = Field(default=0, sa_column=Column(Integer, nullable=False))
    last_status: int | None = Field(
        default=None, sa_column=Column(Integer, nullable=True)
    )
    # Fernet ciphertext can exceed the original 2048-character plaintext URL
    # allowance, so encrypted-at-rest values require unbounded text storage.
    webhook_url: str = Field(default="", sa_column=Column(Text, nullable=False))
    payload: dict = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    body: dict = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )

    # Composite index covering the common query pattern:
    #   WHERE project_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?
    # Without this PostgreSQL filters by project_id then sorts all
    # matching rows in memory; the composite index avoids the sort.
    __table_args__ = (
        Index(
            "ix_notification_dl_project_id_created_at_desc",
            "project_id",
            text("created_at desc"),
        ),
    )


class OperatorInboxLivenessObservation(SQLModel, table=True):
    """Durable record of one operator-inbox liveness tick for a measured project.

    Written once per tick for EVERY measured project (all statuses:
    ``"flagged"``, ``"ok"``, ``"skipped"``) so non-flagged rows break the
    consecutive flagged chain and episode boundaries are detected correctly.
    When a project goes [flagged×3, clean, flagged], the clean-tick row
    separates the two episodes and escalation only fires after >= 3 NEW
    consecutive flagged ticks.  Without clean-tick rows the DB sees an
    unbroken flagged sequence and escalates on the first flagged tick of the
    new episode.

    Modeled on :class:`NotificationDeadLetter` (durable record of an ephemeral
    in-memory signal) so the liveness signal survives restart.
    """

    __tablename__ = "operator_inbox_liveness_observation"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: str = Field(sa_column=Column(String(64), nullable=False, index=True))
    status: str = Field(
        sa_column=Column(String(16), nullable=False, server_default=text("'flagged'"))
    )
    forward_leaked: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    badge_invisible: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    info_alert_noise: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    detail: str = Field(default="", sa_column=Column(Text, nullable=False))
    tick_number: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )

    # Composite index for the common query pattern:
    #   SELECT ... WHERE project_id = ? ORDER BY created_at DESC LIMIT ?
    # Used by persistence-detection to read the last N ticks for a project.
    __table_args__ = (
        Index(
            "ix_olo_project_id_created_at_desc",
            "project_id",
            text("created_at desc"),
        ),
    )


class ProjectEfficiencySnapshot(SQLModel, table=True):
    """Time-series efficiency telemetry for a project.

    Migration 0694 repairs the original one-row-per-project table from 0658
    into the append-only shape used by project_telemetry.py.
    """

    __tablename__ = "project_efficiency_snapshot"

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(GUID(), primary_key=True, nullable=False),
    )
    project_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    captured_at: datetime = Field(
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    metric_def_version: str = Field(
        sa_column=Column(String(64), nullable=False),
    )
    blob: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    __table_args__ = (
        UniqueConstraint(
            "project_id", "captured_at", name="uq_proj_efficiency_snap"
        ),
        Index("ix_project_efficiency_snapshot_project_id", "project_id"),
    )


class FleetSizingReading(SQLModel, table=True):
    """Append-only global fleet-sizing reading (measured, with provenance).

    One row per recorded reading: the workspace disk footprint scaled to the
    fleet slot target, the task-table concurrency overlap peak/percentiles, and
    the target-concurrency-key search — computed from live data by
    ``fleet_utilization.get_recorded_fleet_sizing_readings`` and persisted by
    ``record_fleet_sizing_reading``. ``captured_at`` (when measured) and
    ``serving_sha`` (which deployed backend code computed it) are the
    provenance that ties each reading to the data it was derived from; the full
    reading dict lives in ``blob``.
    """

    __tablename__ = "fleet_sizing_reading"

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(GUID(), primary_key=True, nullable=False),
    )
    captured_at: datetime = Field(
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    serving_sha: str | None = Field(
        default=None, sa_column=Column(String(64), nullable=True)
    )
    window_start: datetime = Field(
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    window_end: datetime = Field(
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    blob: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    __table_args__ = (
        Index("ix_fleet_sizing_reading_captured_at", "captured_at"),
    )


class ProjectLaneBaseline(SQLModel, table=True):
    """Persisted lane-baseline telemetry for a project.

    Replaces design_doc["lane_baselines"] — one row per (project, version_number).
    Migration 0681 rebuilt this table from the original one-row-per-project
    schema to this multi-row schema so project_telemetry.py can store and
    retrieve baselines per release version with retention pruning.
    """

    __tablename__ = "project_lane_baseline"

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(GUID(), primary_key=True, nullable=False),
    )
    project_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    version_number: int = Field(
        sa_column=Column(Integer(), nullable=False),
    )
    captured_at: datetime = Field(
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    baseline_def_version: str = Field(
        sa_column=Column(String(64), nullable=False),
    )
    blob: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    __table_args__ = (
        UniqueConstraint("project_id", "version_number", name="uq_proj_lane_baseline"),
        Index("ix_project_lane_baseline_project_id", "project_id"),
    )


class ProjectSRAnalytics(SQLModel, table=True):
    """Persisted self-review analytics telemetry for a project.

    Replaces the sr_catch_rate_trends, edges_trend,
    sr_regression_risk_test_signal_trends,
    sr_false_attestation_trends, sr_blind_spot_signal_last_computed_at,
    viewport_coverage_snapshot, fix_loop_divergence_* keys in design_doc.
    One row per project; the data dict holds all SR analytics keys.
    """

    __tablename__ = "project_sr_analytics"

    project_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("project.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        )
    )
    data: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )


class ProjectTelemetry(SQLModel, table=True):
    """Per-project telemetry blobs moved out of design_doc to reduce row contention.

    Each row holds one reserved key (e.g. ``efficiency_baseline``) for a
    project.  Writers dual-write to both design_doc and this table during the
    transition; readers continue to use design_doc until migrated.
    """

    __tablename__ = "project_telemetry"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    key: str = Field(sa_column=Column(String(100), nullable=False))
    value: Any = Field(sa_column=Column(JSONB, nullable=True))
    updated_at: datetime = Field(
        sa_column=Column(NaiveUTCDateTime, nullable=False)
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id", "key", name="uq_project_telemetry_project_key"
        ),
        Index("ix_project_telemetry_project_id", "project_id"),
    )


class InlineLlmSpendLog(SQLModel, table=True):
    """Append-only ledger of backend-initiated inline LLM calls (``app.inline_llm``).

    ``call_inline_llm_json`` calls (compute_qa, compute_analysis, compute_draft,
    review_categorize) run outside the agent/Task attempt-history path, so their
    provider-reported usage previously never reached any cost ledger. One row
    is written per successful call, priced from the provider-reported
    ``resp.usage`` probe with the same tables ``app.llm_cost_attribution`` uses,
    so ``cost_reconciliation.py`` can fold this spend into its internal total.
    """

    __tablename__ = "inline_llm_spend_log"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    purpose: str = Field(sa_column=Column(String(32), nullable=False))
    provider: str = Field(sa_column=Column(String(32), nullable=False))
    model: str = Field(sa_column=Column(String(64), nullable=False))
    input_tokens: int = Field(sa_column=Column(Integer, nullable=False))
    output_tokens: int = Field(sa_column=Column(Integer, nullable=False))
    cache_creation_input_tokens: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default="0")
    )
    cache_read_input_tokens: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default="0")
    )
    # "priced" rows carry a real cost_usd; "unpriced" rows (model has no
    # LLM_TOKEN_PRICING entry) are recorded with their usage and cost_usd NULL
    # rather than silently dropped — a consumer summing cost_usd must treat the
    # total as a FLOOR when any unpriced rows exist.
    pricing_status: str = Field(
        default="priced",
        sa_column=Column(String(16), nullable=False, server_default="priced"),
    )
    cost_usd: float | None = Field(sa_column=Column(Float, nullable=True))

    __table_args__ = (
        Index(
            "ix_inline_llm_spend_log_provider_created_at",
            "provider",
            "created_at",
        ),
    )
