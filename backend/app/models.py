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
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    String,
    Integer,
    Float,
    Index,
    Text,
    UniqueConstraint,
    ForeignKey,
    text,
    event,
    func,
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session as _OrmSession
from sqlalchemy.types import TypeDecorator
import logging
import uuid
from datetime import datetime, timezone
from app.time_utils import _utcnow
from app.vm_cost_rates import get_vm_rate_per_second


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


logger = logging.getLogger(__name__)
_UNLOADED = object()

_TASK_BANKED_VM_RUNTIME_COST_KEY = "vm_runtime_cost_banked"
_TASK_BANKED_LLM_INPUT_TOKENS_KEY = "llm_input_tokens_banked"
_TASK_BANKED_LLM_OUTPUT_TOKENS_KEY = "llm_output_tokens_banked"
_TASK_BANKED_LLM_CALL_COUNT_KEY = "llm_call_count_banked"
_TASK_ATTEMPT_HISTORY_KEY = "attempt_history"


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
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


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


class SourceKind(str, Enum):
    """Which task type produced a :class:`KnowledgeDocument`.

    A knowledge document is written by either a codebase-internal ``design``
    task or a web-grounded ``web_research`` task; ``UNKNOWN`` is the durable
    fallback so pre-existing rows (and any future producer that does not stamp
    a kind) backfill cleanly. Stored as the enum *value* in a plain String
    column, mirroring ``ArtifactType`` / ``TaskStatus``.
    """

    DESIGN = "design"
    WEB_RESEARCH = "web_research"
    UNKNOWN = "unknown"


class LLMProviderName(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GEMINI = "gemini"


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
    # Allowed values: 'admin' | 'user'. Enforced server-side by
    # ``chk_alloweduser_role`` (alembic 0065 / audit L-S1, Postgres-only).
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
    archived: bool = Field(default=False)
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
    #    "last_unverified_reason": str | absent}
    # Nullable/additive: existing rows and create_all test DBs read it as None,
    # and a fresh migration (separate task) adds the column to live DBs.
    merge_gate_runtime: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    # Regression-class trip markers used to persist first-tripped timestamps
    # across dispatcher ticks when a signal has no durable historical record.
    regression_signal_state: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
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
    # Per-project promotion (LTS) branch. NULL means fall back to 'main' —
    # identical semantics to integration_branch.
    # Like integration_branch, this is operator-managed and preserved once set.
    promotion_target: str | None = Field(
        default=None,
        sa_column=Column(String, nullable=True),
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
      [{"name": str, "query": str, "event_prefs": {...}, "sort_mode": str,
        "show_resolved": bool, "show_routine": bool, "only_new": bool}, ...]
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
    launched_by_user_id: str | None = Field(
        default=None
    )  # Firebase UID of the user who launched this task (for launcher-pays billing)
    idempotency_key: str | None = Field(
        default=None, sa_column=Column(String(255), nullable=True)
    )
    requires_approval: bool = Field(default=False)

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
    archived: bool = Field(default=False)

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
    infra_wait_until: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
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
    previous_error_fingerprint: str | None = Field(default=None)
    auto_remediation: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    strategic_cycle_id: str | None = Field(default=None, max_length=36)
    spawn_failed: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    last_remediation_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
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
        Index("ix_task_project_status_created", "project_id", "status", "created_at"),
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
                "('QUEUED','PROVISIONING','IN_PROGRESS','EVALUATING','BLOCKED')"
            ),
            sqlite_where=text(
                "idempotency_key IS NOT NULL AND status IN "
                "('QUEUED','PROVISIONING','IN_PROGRESS','EVALUATING','BLOCKED')"
            ),
        ),
        Index("ix_task_context_gin", "context", postgresql_using="gin"),
        Index("ix_task_depends_on_gin", "depends_on", postgresql_using="gin"),
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
        # Task.auto_remediation.is_(True) — keeps the scan tight to the small
        # fraction of tasks with auto_remediation enabled.
        Index(
            "ix_task_auto_remediation_active",
            "project_id",
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


def get_task_current_attempt_llm_totals(task: Task) -> tuple[int, int, int]:
    summary = task.api_call_summary or {}
    return (
        _coerce_nonnegative_int(summary.get("total_input_tokens")),
        _coerce_nonnegative_int(summary.get("total_output_tokens")),
        _coerce_nonnegative_int(summary.get("llm_call_count")),
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
    return runtime_seconds * get_vm_rate_per_second(
        machine_type=machine_type, spot=task.vm_spot
    )


def get_task_total_vm_cost_usd(
    task: Task, *, stopped_at: datetime | None = None
) -> float:
    return float(
        task.vm_runtime_cost_banked or 0.0
    ) + get_task_current_attempt_vm_cost_usd(task, stopped_at=stopped_at)


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

    return merged


def bank_task_current_attempt(
    task: Task,
    *,
    stopped_at: datetime | None = None,
    clear_current_attempt: bool = True,
) -> Dict[str, Any] | None:
    effective_stop = stopped_at or task.vm_stopped_at
    vm_cost = get_task_current_attempt_vm_cost_usd(task, stopped_at=effective_stop)
    input_tokens, output_tokens, call_count = get_task_current_attempt_llm_totals(task)
    has_attempt_data = (
        task.vm_provisioned_at is not None
        or task.api_call_summary is not None
        or vm_cost > 0
        or input_tokens > 0
        or output_tokens > 0
        or call_count > 0
    )
    if not has_attempt_data:
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

    snapshot: Dict[str, Any] = {
        "vm_provisioned_at": task.vm_provisioned_at.isoformat()
        if task.vm_provisioned_at
        else None,
        "vm_stopped_at": effective_stop.isoformat() if effective_stop else None,
        "vm_spot": bool(task.vm_spot),
        "vm_machine_type": task.vm_machine_type,
        "vm_runtime_cost_usd": round(vm_cost, 6),
        "llm_input_tokens": input_tokens,
        "llm_output_tokens": output_tokens,
        "llm_call_count": call_count,
    }
    history = list(task.attempt_history or [])
    history.append(snapshot)
    task.attempt_history = history

    total_vm_cost = float(task.vm_runtime_cost_banked or 0.0)
    task.estimated_cost_usd = round(total_vm_cost, 4) if total_vm_cost > 0 else None

    if clear_current_attempt:
        task.vm_provisioned_at = None
        task.vm_started_at = None
        task.vm_stopped_at = None
        task.api_call_summary = None

    return snapshot


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

    __table_args__ = (
        Index("ix_computejob_project_status", "project_id", "status"),
        Index("ix_computejob_task_status", "task_id", "status"),
        Index("ix_computejob_backend_external", "backend", "external_compute_id"),
        Index("ix_computejob_status_deadline", "status", "wall_clock_deadline"),
        Index("ix_computejob_next_poll", "next_poll_at"),
    )

    project: Project = Relationship(back_populates="compute_jobs")
    task: Task | None = Relationship(back_populates="compute_jobs")


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

        # BLOCKED is terminal-for-now: entering it releases the VM and must
        # revoke the agent JWT (emit_task_terminated_notify), and the
        # BLOCKED->QUEUED unblock must fire the revive branch so peer instances
        # evict the stale deny-list entry. Keep this in lockstep with the
        # terminal set used by _on_status_set's callbacks below.
        terminal = (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.BLOCKED,
        )
        for instance in session.dirty:
            if not isinstance(instance, Task):
                continue
            history = _sa_inspect(instance).attrs.status.history
            if not history.added:
                continue
            new_status = history.added[0]
            if new_status in terminal:
                emit_task_terminated_notify(session, str(instance.id))
            elif history.deleted and history.deleted[0] in terminal:
                # Real terminal → non-terminal transition (requeue/revive): the
                # prior status was terminal and the new one is not. Broadcast so
                # every instance evicts its stale deny-list entry. The guard on
                # the PRIOR status keeps normal non-terminal steps silent.
                emit_task_revived_notify(session, str(instance.id))
    except Exception as exc:  # noqa: BLE001 — never block the flush
        logger.warning("[MODELS] before_flush NOTIFY failed: %s", exc)


@event.listens_for(Task.status, "set")
def _on_status_set(target: Task, value, oldvalue, initiator):
    if value and isinstance(oldvalue, str) and value != oldvalue:
        entry = {
            "t": _utcnow().isoformat(),
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
        task_id = str(target.id)
        # Notify JWT revocation when task reaches terminal state. BLOCKED is
        # terminal-for-now: the VM is released and must stop, so revoke its JWT
        # exactly as for FAILED/CANCELLED. Recovery requeues the task to QUEUED,
        # which falls into the revive branch below and clears the deny-list entry.
        if value in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.BLOCKED,
        ):
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

    __table_args__ = (
        Index("ix_alert_task_id_dismissed", "task_id", "dismissed_at"),
        Index("ix_alert_project_id_dismissed", "project_id", "dismissed_at"),
    )


# Keys used in StrategicCycle.cycle_metadata for per-cycle memo-pathway tracking.
# Stored as plain integers so operators and digest endpoints can read them without
# knowing the exact key strings.
CYCLE_META_MEMOS_AVAILABLE = "memos_available"
CYCLE_META_MEMOS_CITED = "memos_cited"


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
    project_id: uuid.UUID = Field(foreign_key="project.id")

    # outcome enum-ish: in_progress | phase1_running | phase1_done |
    # phase2_running | completed | failed_phase1 | failed_phase2 |
    # stalled_phase1 | stalled_phase2 | failed_phase2_spawn
    outcome: str = Field(
        default="in_progress",
        sa_column=Column(String(32), nullable=False, server_default="in_progress"),
    )

    # Phase tracking. *_started_at = task creation time;
    # *_completed_at = terminal status.
    phase1_task_id: uuid.UUID | None = Field(default=None, foreign_key="task.id")
    phase1_started_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    phase1_completed_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    phase2_task_id: uuid.UUID | None = Field(default=None, foreign_key="task.id")
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


class OrchestratorErrorAck(SQLModel, table=True):
    """Persistent acknowledgement of an orchestrator error bucket.

    key is "exception_class|source" for a specific bucket, or "*" for a
    global ack covering every bucket whose last_seen_iso <= acknowledged_at.
    """

    key: str = Field(primary_key=True, max_length=255)
    acknowledged_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    acknowledged_by: str = Field(default="")


# Maximum stored length for operator/user-authored message content and
# resolution notes. Shared by the admin-notifications and project-directions
# message endpoints so their over-limit rejection cannot drift apart.
MESSAGE_CONTENT_MAX = 5000


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


class MessageType(str, Enum):
    QUESTION = "QUESTION"
    FEEDBACK = "FEEDBACK"
    BUG_REPORT = "BUG_REPORT"
    BLOCKER = "BLOCKER"
    NOTE = "NOTE"


class ProjectMessage(SQLModel, table=True):
    """Async message board between users and the strategic agent."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)
    message_type: MessageType = Field(
        default=MessageType.NOTE, sa_column=Column(String, default=MessageType.NOTE)
    )
    # Allowed values: 'user' | 'agent' | 'strategic' | 'system'. Enforced
    # server-side by ``chk_projectmessage_from_type`` (alembic 0065 /
    # audit L-S1, Postgres-only).
    from_type: str = Field(default="user")
    from_task_id: uuid.UUID | None = Field(default=None, foreign_key="task.id")
    content: str = Field(default="")
    context: Dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=True, default={})
    )
    status: str = Field(default="open")  # "open" | "resolved"
    resolution: str | None = None
    # Client-supplied idempotency key. A double-click / network retry of a
    # send that already succeeded server-side carries the same key, so the
    # create endpoint returns the existing row instead of inserting a
    # duplicate. NULL for legacy / keyless sends (backward-compatible); the
    # partial unique index below only constrains non-NULL keys per project.
    idempotency_key: str | None = Field(
        default=None, sa_column=Column(String(255), nullable=True)
    )
    resolved_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    # TIMESTAMPTZ + server_default (migration 0061 / audit H-S4 + H-S5).
    # NaiveUTCDateTime strips tz on read (round 2 audit / C3 + H1).
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )

    __table_args__ = (
        Index("ix_projectmessage_project_status", "project_id", "status"),
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
    # Previous deploy tracking — populated when a new deploy supersedes the
    # current one. Lets RecentChangesContext builders compute the commit range
    # (prior_deploy_sha..current_sha) without a live git checkout.
    prior_deploy_sha: str | None = Field(default=None)
    prior_deployed_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

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
    # Usage caps (null = unlimited).
    daily_task_cap: int | None = Field(default=None)
    max_concurrent_tasks: int | None = Field(default=None)
    auto_disable_after_rate_limits: int | None = Field(default=None)
    daily_task_count: int = Field(default=0)
    daily_task_count_reset_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
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
        except Exception:
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
        sa_column=Column(ForeignKey("task.id", ondelete="SET NULL"), nullable=True),
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
        ),
        Index(
            "ix_artifact_pr_url_trgm",
            "pr_url",
            postgresql_using="gin",
            postgresql_ops={"pr_url": "gin_trgm_ops"},
        ),
    )

    # Git-based outputs
    commit_hash: str | None = None
    repo_url: str | None = None
    pr_url: str | None = None

    # For visual feedback or logs
    storage_uri: str | None = None
    logs: str | None = None

    iteration: int = Field(default=0)
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )

    task: Task = Relationship(back_populates="artifacts")


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
    source_task_id: uuid.UUID = Field(foreign_key="task.id", index=True)

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
    head_sha: str | None = Field(
        default=None, sa_column=Column(String, nullable=True)
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


class ReleaseVersion(SQLModel, table=True):
    """Release-version lifecycle record for automain → main promotion gating.

    A "version" is the span of automain between two promotions.  The strategic
    layer drives phase transitions:

      build      — normal feature work on automain
      stabilize  — shift work-mix to audit/cleanup/hardening before qualify
      qualify    — heavy gate running once per version (Phase 3)
      promote    — transition to next version after successful promotion

    Only one row per project should have ``status="open"`` at a time.
    ``version_number`` is monotonically increasing per project.

    Columns named *_sha are nullable per migration linter rules.
    All non-null columns carry ``server_default`` for safe backfill.
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

    # Monotonically increasing per project (1-based).
    version_number: int = Field(
        default=1,
        sa_column=Column(Integer, nullable=False, server_default=text("1")),
    )

    # phase: build | stabilize  (qualify / promote are transient orchestrator ops,
    # not durable DB phases — they are reflected by status transitions).
    phase: str = Field(
        default="build",
        sa_column=Column(String(32), nullable=False, server_default="build"),
    )

    # status: open | promoted | abandoned
    status: str = Field(
        default="open",
        sa_column=Column(String(32), nullable=False, server_default="open"),
    )

    # The version's goal — a point-in-time snapshot of
    # ProblemModel.intent.current_focus captured when this version opened.
    # Frozen for the version's lifetime so each version keeps a stable record
    # of what it was for, even after current_focus drifts on.  Nullable:
    # pre-0111 rows and projects with no current_focus carry no goal.
    goal: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )

    # Stamped when the goal is edited by an operator.
    goal_updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

    # Editor provenance for the goal: "[operator:email]" format, mirroring phase_reasoning.
    # Populated only when an operator edits the goal; auto-derived goals carry no editor info.
    goal_editor_info: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
    )

    opened_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )

    # Stamped when phase changes (build → stabilize, etc.).
    phase_changed_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

    # Stamped when status transitions to "promoted".
    promoted_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

    # SHA columns are nullable per migration linter rules.
    # base_sha: automain HEAD when this version was opened.
    base_sha: str | None = Field(
        default=None,
        sa_column=Column(String(40), nullable=True),
    )

    # promoted_sha: automain HEAD at the time of successful promotion.
    promoted_sha: str | None = Field(
        default=None,
        sa_column=Column(String(40), nullable=True),
    )

    # Structured result of the last qualification run (Phase 3).
    # Shape: {score, sub_scores, blocker, attempted_at, …}
    last_qualification: Dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )

    # Total number of qualification attempts on this version (Phase 3).
    qualification_attempts: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )

    # Free-form reasoning text from the strategic agent explaining the latest
    # phase decision.
    phase_reasoning: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )

    # Durable deduplication anchor for calcification alerting.
    # Set to the blocker code when _check_version_calcification posts an alert.
    # The next sweep compares this field against the current blocker: if they
    # match, the alert has already been posted for this stall and is skipped;
    # if they differ (or the field is NULL), a new alert is warranted.
    # Storing the dedup state here avoids relying solely on Alert row existence
    # (which can be lost when an operator clears alerts).
    calcification_alerted_blocker: str | None = Field(
        default=None,
        sa_column=Column(String(128), nullable=True),
    )

    # Durable deduplication anchor for build-stall alerting.
    # Set to the build-phase START reference time (phase_changed_at, falling
    # back to opened_at) when the build-stall sweep posts an alert for a version
    # sitting too long in the 'build' phase without advancing. The next sweep
    # compares this field against the current build-phase reference time: if
    # they match, the alert has already been posted for this stall and is
    # skipped; if they differ (or the field is NULL), a new alert is warranted.
    # Because a version can move build → stabilize → build again (each
    # transition re-stamps phase_changed_at), storing the reference time lets
    # the dedup reset naturally when a fresh build phase begins while never
    # re-firing for the same stall. Storing the dedup state here avoids relying
    # solely on Alert row existence (which can be lost when an operator clears
    # alerts).
    build_stall_alerted_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

    # Durable deduplication anchor for stabilize-stall alerting.
    # Set to the stabilize-phase START reference time (phase_changed_at,
    # falling back to opened_at) when the stabilize-stall sweep posts an
    # alert for a version sitting too long in 'stabilize' without requesting
    # qualification.  Same dedup semantics as build_stall_alerted_at: storing
    # the reference time lets the dedup reset naturally when a fresh stabilize
    # phase begins (phase_changed_at re-stamped) while never re-firing for the
    # same stall.
    stabilize_stall_alerted_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

    # Durable deduplication anchor for promotion-readiness alerting.
    # Set to True when a promotion_readiness_reached notification fires for
    # the current ready window. Reset to False when compute_promotion_readiness
    # returns ready=False so the next not-ready→ready recovery can notify again.
    promotion_ready_notified: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
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
        Index("ix_releaseversion_project_status", "project_id", "status"),
        UniqueConstraint(
            "project_id",
            "version_number",
            name="uq_releaseversion_project_version",
        ),
    )


class QualificationVerdict(SQLModel, table=True):
    """Immutable per-verdict history of release-qualifier finalize outcomes.

    Phase 4 ("Calibration") of the automain LTS design.  ``ReleaseVersion.
    last_qualification`` is OVERWRITTEN on every finalize, so only the latest
    verdict survives there — and while ``promotion_enabled`` is False the open
    version is never closed, so cross-version history cannot be reconstructed.
    This table appends ONE row per finalized qualification verdict that is never
    overwritten, so the dry-run track record is durably retained and can be
    aggregated into a promotion-readiness recommendation
    (``release_qualifier.compute_promotion_readiness``).

    Additive only: it lives ALONGSIDE ``last_qualification`` and does not change
    how that field is written.  Writes are best-effort — a failure to record
    must never break or roll back a qualification or promotion.
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

    release_version_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("releaseversion.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
    )

    # The automain HEAD SHA that was scored.  Nullable per migration linter rules.
    candidate_sha: str | None = Field(
        default=None,
        sa_column=Column(String(40), nullable=True),
    )

    # Composite promotion confidence in [0,1].
    confidence: float = Field(
        default=0.0,
        sa_column=Column(Float, nullable=False, server_default=text("0")),
    )

    # Per-dimension sub-scores: {audit, test_erosion, smoketest}.
    sub_scores: Dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )

    # Hard-blocker code (compute_composite), or NULL when none fired.
    blocker: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
    )

    # Reason the audit sub-score was missing/unscoreable (from release_scoring).
    audit_missing_reason: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
    )

    # Whether the candidate cleared the promotion threshold.
    would_promote: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )

    # The runtime_config["promotion_enabled"] state AT THE TIME of this verdict.
    promotion_enabled: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )

    # Terminal outcome: dry_run | not_promoted | promoted | promotion_failed.
    terminal_status: str = Field(
        default="",
        sa_column=Column(String(32), nullable=False, server_default=""),
    )

    # The promotion threshold in force when this verdict was scored.
    threshold: float | None = Field(
        default=None,
        sa_column=Column(Float, nullable=True),
    )

    # When the qualification started / finalized (carried from the verdict).
    started_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    finalized_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )

    __table_args__ = (
        Index(
            "ix_qualificationverdict_project_created",
            "project_id",
            "created_at",
        ),
    )
