"""Release-version and qualification domain ORM models.

Peeled out of :mod:`app.models` (which grew past its size guardrail) as a
cohesive domain cluster covering the automain → main promotion lifecycle:
release versions, their acceptance criteria and evidence, completion-debt
carryover, deploy artifacts, per-verdict qualification history, and audit
finding resolutions.

These are SQLModel ``table=True`` classes registered on the shared
``SQLModel.metadata`` exactly as before — moving the definitions between
modules changes no schema. ``app.models`` imports and re-exports every class
here, so ``from app.models import ReleaseVersion`` (and siblings) is unchanged
for all callers. This module depends only on :mod:`app.models_base` and
:mod:`app.time_utils`, never on :mod:`app.models`, so no import cycle forms.
"""


from typing import Any, Dict, List

import uuid
from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship, SQLModel
from sqlmodel.sql.sqltypes import GUID

from app.models_base import NaiveUTCDateTime
from app.time_utils import _utcnow
# Paired raise 1642 -> 1750 (matching this module's module_size_limits.json
# entry): the candidate_sha column + its contract comment took the file past
# the previous cap, which automain sat exactly at.  Deliberate config edit per
# check_size_limits.py's governance-tightened remediation, not a refactor.
_MODULE_SIZE_GOVERNANCE_CAP: int = 2550


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

    # The version's goal — a concise statement of what this version is for,
    # authored by the strategist (new_version_goal field) or an operator.
    # Frozen for the version's lifetime so each version keeps a stable record
    # of its objective. Nullable: pre-0111 rows carry no goal.
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

    # ----- Goal-authoring compliance ledger -----
    # ``goal`` opens NULL by design: ``open_version`` leaves it unset and the
    # strategic agent is asked to author it via the ``new_version_goal`` output
    # field (``version_phase.goal_needed`` is emitted while ``goal is None``).
    # That ask used to have no owner — when the agent simply omitted the field,
    # ``_apply_new_version_goal`` returned on its first guard and nothing
    # recorded that a required output had been requested and had not arrived.
    # Six consecutive versions shipped with a NULL goal and no signal anywhere.
    #
    # These two columns give the ask an owner. They are a compliance ledger, not
    # a gate: a missing goal still degrades gracefully everywhere it is read
    # (see ``release_scoring.evaluate_goal_coverage_drift``, which returns
    # ``not_applicable``). Same durable-anchor pattern as
    # ``build_stall_alerted_at`` / ``calcification_alerted_blocker`` below.

    # Consecutive COMPLETED strategic cycles in which the goal was requested
    # (open version, ``goal is None``) and the agent's output did not carry a
    # usable ``new_version_goal``. Reset to 0 the moment a goal is authored.
    goal_omission_cycles: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )

    # Dedup anchor for the counter above: the strategic cycle id (or task id for
    # legacy cycles without one) that last incremented it. A single cycle can
    # reach the apply path more than once — ``set_problem_model`` applies the
    # field inline AND stows it in ``task.context`` for the completion handler —
    # so without this anchor one omitted output would count two or three times
    # and fire the escalation early.
    goal_omission_last_cycle: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
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

    # candidate_sha: the candidate this qualification attempt evaluates, stamped
    # by ``run_qualification`` from the integration-branch HEAD it resolved live
    # for that run — on a new attempt AND on a continued one, which re-resolves
    # HEAD and scores that fresher candidate everywhere else.  Never written by
    # unrelated ``last_qualification`` paths; it is the authoritative source for
    # ``candidate_authorship_range()``, since a mutable JSONB fallback let those
    # writes silently substitute a different candidate into the authorship gate.
    candidate_sha: str | None = Field(
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

    # Lifecycle identity for the qualification projected in last_qualification.
    active_qualification_attempt_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(GUID(), nullable=True, index=True),
    )

    # Bounded retry episode, distinct from the lifetime attempt count.
    qualification_retry_episode: int = Field(
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

    # ----- Post-promotion serving confirmation -----
    # Whether the promoted version's code is actually serving in production.
    # ``status`` flips to "promoted" the instant the candidate merges; these
    # columns track whether the deploy then stabilized.  All nullable so the
    # migration is purely additive and pre-existing rows backfill naturally.
    #
    # **IMPORTANT: This field is a legacy projection for single-artifact releases.**
    # It mirrors the ``backend_revision`` artifact's serving_status via
    # ``project_backend_revision_artifact()`` during multi-artifact windows.
    # When multiple required artifacts are registered (e.g. backend_revision + other
    # services), this scalar reflects ONLY the backend artifact's state, not the
    # whole-release aggregate. For the authoritative whole-release status, use
    # ``aggregate_required_artifact_status()`` to compute from the full
    # ReleaseDeployArtifact rows. This scalar is whole-release-accurate only
    # while backend_revision is the sole required artifact.
    #
    #   serving_status values: NULL (not-yet-promoted / NA) | "unconfirmed"
    #     | "serving_confirmed" | "confirmation_timed_out" | "deploy_failed".
    #   "confirmation_timed_out" = the confirmation window elapsed with NO active
    #     failure (no rollback/halt/sha-mismatch) — the promoted SHA just could
    #     not be confirmed serving in time; distinct from "deploy_failed" (an
    #     active failure) and recoverable to "serving_confirmed" if later observed.
    serving_status: str | None = Field(
        default=None,
        sa_column=Column(String(32), nullable=True),
    )

    # The production serving SHA captured when serving was confirmed.
    # **IMPORTANT: This is a legacy projection field.**
    # It mirrors ``ReleaseDeployArtifact.served_identifier`` for the backend_revision
    # artifact via ``project_backend_revision_artifact()``. During multi-artifact
    # windows, this scalar reflects ONLY the backend artifact's serving SHA.
    # The authoritative per-artifact serving identity is stored in
    # ReleaseDeployArtifact.served_identifier. This field is whole-release-accurate
    # only while backend_revision is the sole required artifact.
    served_sha: str | None = Field(
        default=None,
        sa_column=Column(String(40), nullable=True),
    )

    # When serving was confirmed.
    served_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

    # Bounded confirmation-window deadline, stamped at promotion.
    deploy_confirm_deadline: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

    # Durable deduplication anchor for the deploy-failed operator alert.
    # Mirrors the *_alerted_at dedup convention on this model: stamped when the
    # alert fires so the next sweep skips re-posting for the same failure.
    deploy_confirm_alerted_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

    # Short code for why confirmation did not reach serving_confirmed:
    #   active failure (serving_status=deploy_failed): rolled_back | halted
    #     | sha_mismatch
    #   timeout (serving_status=confirmation_timed_out): no_new_revision
    #     | serving_sha_unobservable
    deploy_failure_reason: str | None = Field(
        default=None,
        sa_column=Column(String(128), nullable=True),
    )

    # ----- Approved activation (in-product promotion activation control) -----
    # Stamped when an operator approves activation of THIS promoted version for
    # the trusted control-plane host to execute.  The approval binds EXACTLY the
    # version's immutable ``promoted_sha``: the activation-receipt path rejects
    # any reported revision that differs (or a receipt once a newer promotion has
    # superseded this one), so an approval can never authorize a different or
    # historical artifact.  Both nullable and purely additive — pre-existing
    # promoted rows backfill NULL and keep the legacy CLI path unchanged.
    #
    #   activation_requested_at is NULL          -> awaiting approval
    #   activation_requested_at set, unconfirmed -> approved-but-not-yet-executed
    #
    # The approved execution is additionally recorded as immutable audit
    # provenance in ``last_qualification["activation_approval"]``.
    activation_requested_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

    # Operator identity that approved activation, mirroring goal_editor_info
    # provenance conventions ("[operator:email]" / uid).
    activation_requested_by: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
    )

    # Immutable backend-authored trusted-CI candidate build/smoke evidence. The
    # receipt is request/run/candidate-bound and read by qualification; unlike
    # last_qualification it survives later qualification projections.
    prepromotion_rehearsal_receipt: Dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )

    deploy_artifacts: List["ReleaseDeployArtifact"] = Relationship(
        back_populates="version"
    )

    # Cumulative tally of merged tasks that failed to record criterion evidence,
    # persisted per open version so operators can see the gap without trawling
    # logs.  Shape (all counts are cumulative since the version was opened):
    #   {
    #     "no_criteria": int,          -- task carried no criterion_ids at all
    #     "verdict_not_proven": int,   -- task merged but verdict was not proven
    #     "no_active_criteria": int,   -- criterion_ids present but no active rows
    #     "unmatched_criterion_ids": list[str],  -- keys that found no active row
    #   }
    # NULL until the first gap is recorded (additive migration).
    merge_evidence_gaps: Dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
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
        Index("ix_releaseversion_project_opened_at", "project_id", "opened_at"),
        Index("ix_releaseversion_project_served_at", "project_id", "served_at"),
        Index("ix_releaseversion_project_promoted_at", "project_id", "promoted_at"),
        # Standalone status index for cross-project scans (metrics sweep,
        # release qualifier sweep, etc.) that filter by status without
        # a project_id constraint.
        Index("ix_releaseversion_status", "status"),
        UniqueConstraint(
            "project_id",
            "version_number",
            name="uq_releaseversion_project_version",
        ),
    )


class ReleasePrepromotionDispatch(SQLModel, table=True):
    """Durable outbox claim for one candidate-bound rehearsal dispatch."""

    __tablename__ = "releaseprepromotiondispatch"
    __table_args__ = (
        UniqueConstraint(
            "release_version_id",
            "candidate_sha",
            name="uq_releaseprepromotiondispatch_version_candidate",
        ),
        Index(
            "ix_releaseprepromotiondispatch_status_retry",
            "status",
            "next_retry_at",
            "claimed_at",
        ),
    )

    # The primary key is also the public workflow correlation/request id.
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    release_version_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("releaseversion.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    project_id: uuid.UUID = Field(
        sa_column=Column(GUID(), ForeignKey("project.id", ondelete="CASCADE"), nullable=False)
    )
    candidate_sha: str = Field(sa_column=Column(String(40), nullable=False))
    workflow_ref: str = Field(
        default="main",
        sa_column=Column(String(255), nullable=False, server_default="main"),
    )
    # claimed | attempting | dispatched | uncertain | observed | rejected
    # Rejected is terminal until an authenticated operator explicitly rearms it.
    status: str = Field(
        default="claimed",
        sa_column=Column(String(32), nullable=False, server_default="claimed"),
    )
    dispatch_attempts: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    workflow_run_id: int | None = Field(
        default=None, sa_column=Column(BigInteger, nullable=True)
    )
    claimed_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    attempted_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    next_retry_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    observed_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    last_error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            NaiveUTCDateTime,
            nullable=False,
            server_default=func.now(),
            onupdate=_utcnow,
        ),
    )


class VersionAcceptanceCriterion(SQLModel, table=True):
    """A single verifiable acceptance criterion scoped to a release version.

    Criteria are stable-ID records authored by the strategist or an operator
    before a version opens for feature work.  Each criterion has a
    ``criterion_key`` (e.g. ``c-<8hexchars>``) that is unique within the
    version and that Direction.criterion_keys / Task.criterion_ids reference so
    criterion coverage can be computed without joining on UUIDs.
    """

    __tablename__ = "versionacceptancecriterion"
    __table_args__ = (
        UniqueConstraint(
            "release_version_id",
            "criterion_key",
            name="uq_vac_version_key",
        ),
        Index("ix_vac_version", "release_version_id"),
        Index("ix_vac_project", "project_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    project_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )

    release_version_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("releaseversion.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )

    # Stable string ID, e.g. 'c-1a2b3c4d'.  Unique within the version.
    criterion_key: str = Field(sa_column=Column(String(64), nullable=False))

    # Human-readable criterion description.
    text: str = Field(sa_column=Column(Text, nullable=False))

    # Optional machine evidence contract.  This is deliberately separate from
    # ``text``: free-form rubric prose must never select a deterministic
    # evidence producer by substring matching.
    evidence_contract: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )

    evidence_contract_updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    evidence_contract_updated_by: str | None = Field(
        default=None,
        sa_column=Column(String(128), nullable=True),
    )

    # Lifecycle status: 'active' | 'retracted' | 'superseded'.
    status: str = Field(
        default="active",
        sa_column=Column(String(32), nullable=False, server_default="active"),
    )

    # Retraction/supersession audit metadata. Rows are retained so evidence
    # history stays inspectable even when coverage no longer counts them.
    retraction_reason: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )
    retracted_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    retracted_by: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
    )
    superseded_by_key: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
    )

    # Provenance: 'strategist' | 'operator' | etc. Nullable.
    created_by: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
    )

    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )


class ReleaseAuthorshipReconciliationState(SQLModel, table=True):
    """Durable singleton keyset cursor for serving-authorship reconciliation.

    The row is deliberately global: the sweep covers every project and the row
    lock is the short cross-process CAS that assigns one scan page at a time.
    ``cursor_partition`` is 0 for the NULL ``served_at`` prefix and 1 for the
    timestamp/id suffix.  ``generation`` records each EOF wrap so a restarted
    worker cannot mistake a wrapped cursor for forward progress.
    """

    __tablename__ = "releaseauthorshipreconciliationstate"

    id: int = Field(default=1, primary_key=True)
    cursor_partition: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    null_cursor_id: uuid.UUID | None = Field(
        default=None, sa_column=Column(GUID(), nullable=True)
    )
    served_at_cursor: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    served_id_cursor: uuid.UUID | None = Field(
        default=None, sa_column=Column(GUID(), nullable=True)
    )
    generation: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
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


class CriterionEvidence(SQLModel, table=True):
    """A single piece of evidence satisfying a VersionAcceptanceCriterion.

    One criterion can accumulate many evidence rows over the version's lifetime
    (e.g. multiple task merges, an operator attestation, a reachability signal).
    Coverage is derived by querying whether at least one evidence row exists for
    each criterion on the version.
    """

    __tablename__ = "criterionevidence"
    __table_args__ = (
        Index("ix_criterionevidence_criterion_id", "criterion_id"),
        Index("ix_criterionevidence_project", "project_id"),
        Index(
            "uq_criterionevidence_dedup",
            "criterion_id",
            "source_kind",
            "source_id",
            unique=True,
            postgresql_where=text("source_id IS NOT NULL"),
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    project_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )

    criterion_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("versionacceptancecriterion.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )

    # 'task_merge' | 'operator' | 'reachability' | etc.
    source_kind: str = Field(sa_column=Column(String(64), nullable=False))

    # Task UUID (as string) or other source identifier.  Nullable.
    source_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
    )

    recorded_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )

    # Arbitrary evidence metadata (JSONB).
    detail: Dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )


class CompletionDebtEntry(SQLModel, table=True):
    """A durable record of merged-but-unfinished work — "completion debt".

    An entry is opened whenever the system lands something in an incomplete,
    dormant, or observe-only state that still owes follow-through:
      * a feature/knob merged default-off or in observe mode (``dormant_knob``);
      * a capability running in observe/trial mode with no decided enforce
        verdict (``capability_trial``);
      * a no-CI merge gate that fell open to an UNVERIFIED merge (``no_ci_gate``);
      * a strategic retirement that left a residual handoff/deferral behind
        (``retirement_residual``);
    Entries are IDEMPOTENT on ``dedupe_key`` (globally unique): repeated recorder
    or seed invocations upsert the same logical obligation rather than creating a
    duplicate row.  Recording is best-effort at every write site — a ledger
    failure must never break the merge, verdict-persist, or retirement path that
    created the debt.
    """

    __tablename__ = "completiondebtentry"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_completiondebt_dedupe_key"),
        Index("ix_completiondebt_status", "status"),
        Index("ix_completiondebt_kind_status", "kind", "status"),
        Index("ix_completiondebt_project", "project_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    # Nullable: deployment-global debt (dormant runtime knobs, adversarial /
    # retirement observe-watch state) has no owning project.
    project_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(),
            ForeignKey("project.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )

    # Debt family, e.g. 'dormant_knob' | 'capability_trial' | 'no_ci_gate' |
    # 'retirement_residual' | 'capability_earn_obligation'.
    kind: str = Field(sa_column=Column(String(64), nullable=False))

    # Stable, globally-unique idempotency key for this obligation.
    dedupe_key: str = Field(
        default_factory=lambda: f"manual:{uuid.uuid4()}",
        sa_column=Column(String(255), nullable=False),
    )

    # Lifecycle: 'open' | 'resolved' | 'dismissed'.
    status: str = Field(
        default="open",
        sa_column=Column(String(32), nullable=False, server_default="open"),
    )

    # Short human-readable summary of the outstanding obligation.
    title: str = Field(sa_column=Column(Text, nullable=False))

    # Writer family / call site that opened the entry (audit provenance).
    source: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
    )

    # Bounded arbitrary metadata (JSONB) — capped by the recorder.
    detail: Dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )

    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    resolved_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    resolution_reason: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )


class ReleaseDeployArtifact(SQLModel, table=True):
    """Per-artifact serving confirmation record for a promoted release version."""

    __tablename__ = "releasedeployartifact"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    version_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(),
            ForeignKey("releaseversion.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
    )

    artifact_kind: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
    )

    # Values: "unconfirmed" | "serving_confirmed" | "confirmation_timed_out"
    #   | "deploy_failed".
    serving_status: str | None = Field(
        default=None,
        sa_column=Column(String(32), nullable=True),
    )

    # Generic observed identity for the served artifact (SHA, fingerprint, etc).
    served_identifier: str | None = Field(
        default=None,
        sa_column=Column(String(128), nullable=True),
    )

    confirmation_deadline: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

    reason_code: str | None = Field(
        default=None,
        sa_column=Column(String(128), nullable=True),
    )

    served_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

    created_at: datetime | None = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=True, server_default=func.now()),
    )
    updated_at: datetime | None = Field(
        default_factory=_utcnow,
        sa_column=Column(
            NaiveUTCDateTime,
            nullable=True,
            server_default=func.now(),
            onupdate=_utcnow,
        ),
    )

    version: ReleaseVersion | None = Relationship(back_populates="deploy_artifacts")

    __table_args__ = (
        UniqueConstraint(
            "version_id",
            "artifact_kind",
            name="uq_releasedeployartifact_version_kind",
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

    # Nullable attempt identity; migration 0607 backfills historical rows.
    qualification_attempt_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(GUID(), nullable=True, index=True),
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

    # ----- Post-promotion serving outcome -----
    # Immutable record of how this verdict's promotion actually played out in
    # production.  Nullable: only set for verdicts that promoted and then had
    # their deploy confirmed/failed.
    #   post_promotion_outcome values: "serving_confirmed" | "deploy_failed"
    #     | "confirmation_timed_out" | "unconfirmed".
    post_promotion_outcome: str | None = Field(
        default=None,
        sa_column=Column(String(32), nullable=True),
    )

    # When the post-promotion outcome was recorded.
    post_promotion_outcome_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

    # --- Per-attempt evidence mirrored from ``last_qualification`` ----------
    # These NULLABLE columns durably retain the richer evidence the qualifier
    # assembles into the ``last_qualification`` base_record but which was
    # previously dropped when the verdict row was written, so blocked/held
    # attempts no longer lose it on the next re-qualification overwrite.  All
    # are nullable — existing verdict rows carry no such evidence.

    # Migration-safety verdicts (e.g. 'pass'/'block') and their detail strings.
    migration_safety: str | None = Field(
        default=None,
        sa_column=Column(String(32), nullable=True),
    )
    migration_safety_detail: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )
    migration_apply_safety: str | None = Field(
        default=None,
        sa_column=Column(String(32), nullable=True),
    )
    migration_apply_safety_detail: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )

    # Audit findings and the findings excluded from scoring (JSONB lists).
    audit_findings: Any | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )
    excluded_audit_findings: Any | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )
    # Challenger-confirmed false positives: findings the independent challenger
    # marked refuted_false_positive with auditable reasons.  DISTINCT from
    # excluded_audit_findings (which captures the unverified-primary gap).
    # Each entry: {severity, summary, file, challenger_reason}.
    challenger_refuted_audit_findings: Any | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )

    # Structured erosion / smoketest / audit-reconciliation detail (JSONB).
    test_erosion_detail: Any | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )
    smoketest_detail: Any | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )
    audit_reconciliation: Any | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )

    # Goal-alignment grade: {covered, rationale, ...}
    # Nullable: existing verdict rows carry no goal_alignment data.
    goal_alignment: Any | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )

    # Blind merge-grade certificate: {candidate_sha, coverage_verdict,
    # biggest_gap, model, pr_count, graded_at}.
    # Nullable: existing verdict rows carry no blind merge-grade data.
    blind_merge_grade: Any | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )

    # --- Blind merge-grade goal-coverage gate (dir d-c0fe439f) --------------
    # An uninvested release_merge_grade agent grades the merged DIFFS against
    # the frozen version.goal (BLIND to running_update) and emits a goal-coverage
    # verdict.  These NULLABLE columns durably retain that verdict alongside the
    # other per-attempt evidence so a re-qualification overwrite of
    # last_qualification does not drop it.  All nullable with safe server
    # defaults so pre-existing verdict rows (written before this gate) stay
    # readable / backward-compatible.
    #   merge_grade_status: "pass" | "fail" | "not_applicable" (empty goal).
    #   merge_grade_coverage: goal-coverage score in [0,1] (NULL when ungraded).
    #   merge_grade_biggest_gap: one-sentence biggest thing not done toward goal.
    #   merge_grade_detail: the raw graded artifact (covers_goal, scores, etc.).
    merge_grade_status: str | None = Field(
        default=None,
        sa_column=Column(String(16), nullable=True),
    )
    merge_grade_coverage: float | None = Field(
        default=None,
        sa_column=Column(Float, nullable=True),
    )
    merge_grade_biggest_gap: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )
    merge_grade_detail: Any | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )

    # Per-attempt goal-verification verdict snapshot: the per-category verdicts
    # and residual gap that drove the criterion_coverage gate at finalize time.
    # Persisted so a re-qualification overwrite of last_qualification does NOT
    # lose which category/residual facts belonged to this attempt.  Nullable —
    # rows written before this column existed or for attempts where no rubric /
    # grade was issued carry None.
    category_verdicts: Any | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )
    residual_uncaptured_gap: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )

    # Promotion-attempt context (only set on the promotion_failed / promoted
    # paths): the merge PR URL, whether the merge hit a conflict, and the
    # error/detail string describing why the merge failed.
    promotion_pr_url: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True),
    )
    promotion_conflict: bool | None = Field(
        default=None,
        sa_column=Column(Boolean, nullable=True),
    )
    promotion_error_detail: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
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


class AuditFindingResolution(SQLModel, table=True):
    """Durable per-finding resolution ledger for carried qualification audit findings.

    The re-qualification trigger ("all high+medium carried audit findings
    merged-fixed or code-verified stale") needs per-finding resolution state that
    SURVIVES qualification finalize.  Today nothing tracks it: every finalize
    overwrites ``ReleaseVersion.last_qualification`` wholesale, and each immutable
    ``QualificationVerdict`` row is a point-in-time snapshot, not a mutable
    resolution record.  This table records ONE row per
    ``(release_version, finding_key)`` resolution, keyed on the deterministic
    content key ``release_scoring._audit_finding_key`` attaches to every carried
    finding.  It lives in its OWN table — never inside ``last_qualification`` —
    so it is never clobbered by a subsequent finalize overwrite.

    A unique constraint on ``(release_version_id, finding_key)`` makes a repeat
    write an UPSERT (latest write wins) rather than a duplicate; the write helper
    ``release_readiness.apply_finding_resolution`` performs that upsert.
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

    # Deterministic content key (release_scoring._audit_finding_key — sha256 of
    # normalized file|summary, 16 hex) of the carried finding this row resolves.
    finding_key: str = Field(
        sa_column=Column(String(64), nullable=False, index=True),
    )

    # The carried finding's severity at resolution time (low/medium/high/critical/...).
    severity: str | None = Field(
        default=None,
        sa_column=Column(String(32), nullable=True),
    )

    # Resolution state: "resolved" (a merged fix addressed it) or "stale" (it was
    # code-verified to no longer apply on the candidate).
    state: str = Field(
        sa_column=Column(String(16), nullable=False),
    )

    # Optional human/agent note explaining the resolution.
    note: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )

    # Optional resolving reference — e.g. the merged PR number or URL that fixed it.
    resolving_ref: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True),
    )

    # The candidate SHA the resolution/staleness verification was performed against.
    verified_against_sha: str | None = Field(
        default=None,
        sa_column=Column(String(40), nullable=True),
    )

    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )

    __table_args__ = (
        UniqueConstraint(
            "release_version_id",
            "finding_key",
            name="uq_auditfindingresolution_version_key",
        ),
    )


class ModuleFindingChain(SQLModel, table=True):
    """Project-lifetime ledger of recurring review and guard concerns."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    finding_key: str = Field(sa_column=Column(String(64), nullable=False))
    file_path: str = Field(sa_column=Column(Text, nullable=False))
    # Retained for file+Jaccard matching when reviewers paraphrase the concern.
    summary: str = Field(sa_column=Column(Text, nullable=False, server_default=""))
    occurrences: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    verified_occurrences: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    pr_numbers: list[int] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'")),
    )
    first_seen_pr: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    last_seen_pr: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    last_seen_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    status: str = Field(
        default="open",
        sa_column=Column(String(16), nullable=False, server_default="open"),
    )
    file_provenance: str = Field(
        default="unverified",
        sa_column=Column(String(16), nullable=False, server_default="unverified"),
    )
    # ``review`` rows retain the historical behaviour above. ``guard_suppression``
    # rows are keyed by an exact structured lane/FailureIdentity, never prose.
    chain_kind: str = Field(
        default="review",
        sa_column=Column(String(32), nullable=False, server_default="review"),
    )
    suppression_lane: str | None = Field(
        default=None, sa_column=Column(String(128), nullable=True)
    )
    failure_identity: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    # Bounded task UUID provenance provides exact-task idempotency without a
    # parallel evidence table. It is deliberately not derived from receipt history.
    source_task_ids: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'")),
    )
    remediation_task_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(), ForeignKey("task.id", ondelete="SET NULL"), nullable=True, index=True
        ),
    )
    resolved_by_task_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(), ForeignKey("task.id", ondelete="SET NULL"), nullable=True
        ),
    )
    resolved_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id", "finding_key", name="uq_modulefindingchain_project_key"
        ),
        Index(
            "ix_modulefindingchain_project_file", "project_id", "file_path"
        ),
    )


class QualificationDimensionResult(SQLModel, table=True):
    """Per-(candidate_sha, dimension) qualification fact ledger (Phase 1).

    Design doc "From Batch Attempts to a Per-Dimension Fact Ledger: Restructuring
    Qualification for Failure-Isolation and Incremental Scoring" (section 3).

    Today a qualification attempt is monolithic: ``_finalize_qualification``
    computes every gate/dimension, folds them into one ``last_qualification``
    blob (overwritten every finalize) plus one immutable ``QualificationVerdict``
    row, and a single failed gate discards the whole audit.  This table records
    ONE row PER dimension PER candidate tree, so a later re-check of one gate can
    re-run and re-record only its own ``(release_version_id, candidate_sha,
    dimension)`` fact without discarding the other dimensions that already
    passed for the same tree.

    Phase 1 is a DUAL-WRITE: ``_finalize_qualification`` writes these rows
    ALONGSIDE the existing ``last_qualification`` / ``QualificationVerdict``
    writes and changes no decision logic.  Writes are best-effort — a failure to
    record a dimension row must NEVER break or roll back a qualification or
    promotion (mirrors ``_record_qualification_verdict``).

    A unique constraint on ``(release_version_id, candidate_sha, dimension)``
    makes a repeat write for the same tree an UPSERT (latest write wins) rather
    than a duplicate; ``release_qualifier._upsert_dimension_result`` performs
    that SELECT-then-INSERT/UPDATE upsert.
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

    # The candidate tree (automain HEAD SHA) this dimension result is FOR.
    # Nullable per migration linter rules and to match QualificationVerdict; in
    # practice always set at finalize (the upsert key relies on it).
    candidate_sha: str | None = Field(
        default=None,
        sa_column=Column(String(40), nullable=True),
    )

    # Which qualification dimension this row records — one of: audit,
    # migration_identity, migration_apply, ci, smoketest, merge_grade,
    # test_erosion, acceptance_window, debt_pressure, challenge,
    # capability_proof.
    dimension: str = Field(
        sa_column=Column(String(32), nullable=False),
    )

    # Outcome for this dimension: passed | failed | blocked | unavailable | pending.
    status: str = Field(
        sa_column=Column(String(16), nullable=False),
    )

    # Per-dimension score in [0,1] when the dimension is numerically scored
    # (audit / test_erosion / smoketest / merge-grade coverage / debt pressure /
    # acceptance fraction).  NULL for the boolean/verdict dimensions.
    sub_score: float | None = Field(
        default=None,
        sa_column=Column(Float, nullable=True),
    )

    # Hard-blocker code when this dimension forced the composite confidence to
    # 0.0 (e.g. ci_not_green, migration_apply_drift, audit_missing), else NULL.
    blocker: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
    )

    # Content hash of the audited surface, for cross-SHA fact reuse in later
    # phases (a commit differing only in CI/fixture files can reuse the fact).
    audited_surface_hash: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
    )

    # Dimension-specific evidence (ci_resolution, migration detail, merge-grade
    # certificate, acceptance aggregate, debt snapshot, challenge summary, ...).
    detail: Any | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )

    # The child agent task that produced this dimension's evidence, when any
    # (audit / smoketest / challenge / merge-grade); NULL for gates computed
    # inline (ci / migration_identity / migration_apply).
    producing_task_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(GUID(), nullable=True),
    )

    # The attempt that actually measured/produced this candidate-scoped fact.
    # Reuse never overwrites this provenance.
    origin_attempt_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(GUID(), nullable=True, index=True),
    )

    # Attempt ids that explicitly reused this resolved fact.  JSONB is used
    # because this is append-only provenance, not ownership; the fact remains
    # uniquely keyed by release version, candidate SHA, and dimension.
    reused_by_attempt_ids: Any | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )

    computed_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )

    __table_args__ = (
        UniqueConstraint(
            "release_version_id",
            "candidate_sha",
            "dimension",
            name="uq_qualdimresult_version_sha_dimension",
        ),
    )


class CategoryVerdict(SQLModel, table=True):
    """Per-(candidate_sha, category) RE-GRADEABLE acceptance verdict.

    Goal-verification redesign persistence layer.  Replaces the *monotonic*
    ``CriterionEvidence`` accumulation (where a criterion, once it had any
    evidence row, could never lose coverage) with a per-candidate verdict that
    a re-grade OVERWRITES — so a category that regressed on a newer candidate
    tree flips back to ``not_met`` instead of staying satisfied forever.

    One row PER ``(version_id, candidate_sha, category_key)`` — or, for a
    DIRECTION-scoped grade, PER ``(direction_id, candidate_sha, category_key)``.
    Re-grading a single category is an UPSERT on that key: it rewrites ONLY its
    own row and never touches another category's verdict (within-candidate
    isolation).  Exactly one of ``version_id`` / ``direction_id`` is set (the
    other is NULL) — enforced by ``chk_categoryverdict_exactly_one_scope`` and
    by the ledger accessor.  The version-scoped guarantee that at most one row
    exists per ``(version_id, candidate_sha, category_key,
    qualification_attempt_id)`` is preserved by a partial unique index
    ``WHERE direction_id IS NULL``; the direction analogue lives under a
    ``WHERE version_id IS NULL`` partial unique index.

    The active category set is authored as ``VersionAcceptanceCriterion`` rows
    (``category_key`` here corresponds to ``VersionAcceptanceCriterion``'s
    ``criterion_key``); an active criterion with no ``met`` verdict for a
    candidate is NOT met (fail-closed) — see
    :func:`app.category_verdict_ledger.is_candidate_fully_met`.  A direction
    scope has no ``VersionAcceptanceCriterion`` rows; its active rubric is the
    caller-supplied ``active_category_keys`` parameter.

    ``category_key`` is nullable and the ``candidate_sha`` is nullable per the
    migration-linter rules and to mirror ``QualificationDimensionResult``; in
    practice both are always set at grade time (the upsert key relies on them).
    """

    __tablename__ = "categoryverdict"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    # Exactly one of version_id / direction_id is set; the other stays NULL.
    # Nullable because a direction-scoped verdict has no release version.
    version_id: uuid.UUID | None = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("releaseversion.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
    )

    # The DIRECTION scope for this verdict (the ``d-<hex>`` direction key) when
    # the grade targets a direction objective rather than a release version.
    # NULL for version-scoped verdicts.
    direction_id: str | None = Field(
        default=None,
        sa_column=Column(String(128), nullable=True, index=True),
    )

    # The candidate tree (automain HEAD SHA) this verdict is FOR.  Nullable per
    # migration-linter rules; the upsert key relies on it in practice.
    candidate_sha: str | None = Field(
        default=None,
        sa_column=Column(String(40), nullable=True),
    )

    # The acceptance category — corresponds to
    # ``VersionAcceptanceCriterion.criterion_key`` for the version.
    category_key: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
    )

    # Verdict for this category on this candidate: met | not_met | partial.
    status: str = Field(
        sa_column=Column(String(16), nullable=False),
    )

    # How the verdict was reached: judged (LLM grader) | machine (deterministic).
    tier: str = Field(
        sa_column=Column(String(16), nullable=False),
    )

    # Human/agent-readable description of what is still missing when the verdict
    # is not_met/partial; NULL when fully met.
    gap: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )

    # Supporting citations (file paths, PR refs, evidence blobs) backing the
    # verdict.  Free-form JSON (list or dict).
    citations: Any | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )

    # Truthful source for deterministic evidence.  Machine projections do not
    # borrow the LLM grader task id as their provenance.
    evidence_provenance: Any | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )

    graded_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )

    # The grader run (agent task / attempt id) that produced this verdict, for
    # provenance and cache-key invalidation.  Nullable.
    grader_run_id: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
    )

    # The qualification attempt this verdict belongs to, for attempt-scoped
    # querying and to prevent cross-attempt overwrites on same-SHA re-grades.
    # Nullable for backward compatibility with rows created before this field
    # was added.
    qualification_attempt_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(GUID(), nullable=True, index=True),
    )

    __table_args__ = (
        # Exactly-one-of scope: a verdict is either version-scoped or
        # direction-scoped, never both and never neither.
        CheckConstraint(
            "(version_id IS NULL AND direction_id IS NOT NULL) OR "
            "(version_id IS NOT NULL AND direction_id IS NULL)",
            name="chk_categoryverdict_exactly_one_scope",
        ),
        # Version-scoped uniqueness (Postgres NULL semantics treat NULLs as
        # distinct, so the partial predicates keep the two scopes isolated).
        Index(
            "uq_categoryverdict_version_sha_category_attempt",
            "version_id",
            "candidate_sha",
            "category_key",
            "qualification_attempt_id",
            unique=True,
            postgresql_where=text("direction_id IS NULL"),
            sqlite_where=text("direction_id IS NULL"),
        ),
        Index(
            "uq_categoryverdict_direction_sha_category_attempt",
            "direction_id",
            "candidate_sha",
            "category_key",
            "qualification_attempt_id",
            unique=True,
            postgresql_where=text("version_id IS NULL"),
            sqlite_where=text("version_id IS NULL"),
        ),
    )


class ResidualVerdict(SQLModel, table=True):
    """Per-(candidate_sha) "is any goal aspect uncaptured by the categories?".

    Goal-verification redesign persistence layer.  The category verdicts prove
    each authored acceptance criterion; the residual records whether the union
    of those categories still LEAVES a gap — a goal aspect no category covers.
    A non-null ``uncaptured_gap`` means the candidate cannot be considered fully
    met even if every category is ``met``
    (:func:`app.category_verdict_ledger.is_candidate_fully_met`).

    One row PER ``(version_id, candidate_sha, qualification_attempt_id)`` — or,
    for a DIRECTION-scoped grade, PER ``(direction_id, candidate_sha,
    qualification_attempt_id)``; a re-grade UPSERTs it within an attempt, and
    different attempts on the same candidate each retain their own residual row.
    Exactly one of ``version_id`` / ``direction_id`` is set (the other is NULL)
    — enforced by ``chk_residualverdict_exactly_one_scope``.
    """

    __tablename__ = "residualverdict"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    # Exactly one of version_id / direction_id is set; the other stays NULL.
    # Nullable because a direction-scoped verdict has no release version.
    version_id: uuid.UUID | None = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("releaseversion.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
    )

    # The DIRECTION scope for this residual (the ``d-<hex>`` direction key) when
    # the grade targets a direction objective rather than a release version.
    # NULL for version-scoped residuals.
    direction_id: str | None = Field(
        default=None,
        sa_column=Column(String(128), nullable=True, index=True),
    )

    # The candidate tree (automain HEAD SHA) this residual is FOR.  Nullable per
    # migration-linter rules; the upsert key relies on it in practice.
    candidate_sha: str | None = Field(
        default=None,
        sa_column=Column(String(40), nullable=True),
    )

    # Description of the goal aspect no acceptance category captures; NULL/absent
    # means the categories fully span the goal (no residual gap).
    uncaptured_gap: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )

    graded_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )

    # The qualification attempt this residual belongs to, for attempt-scoped
    # querying and to prevent cross-attempt overwrites on same-SHA re-grades.
    # Nullable for backward compatibility with rows created before this field
    # was added.
    qualification_attempt_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(GUID(), nullable=True, index=True),
    )

    __table_args__ = (
        # Exactly-one-of scope: a residual is either version-scoped or
        # direction-scoped, never both and never neither.
        CheckConstraint(
            "(version_id IS NULL AND direction_id IS NOT NULL) OR "
            "(version_id IS NOT NULL AND direction_id IS NULL)",
            name="chk_residualverdict_exactly_one_scope",
        ),
        # Version-scoped uniqueness (Postgres NULL semantics treat NULLs as
        # distinct, so the partial predicates keep the two scopes isolated).
        Index(
            "uq_residualverdict_version_sha_attempt",
            "version_id",
            "candidate_sha",
            "qualification_attempt_id",
            unique=True,
            postgresql_where=text("direction_id IS NULL"),
            sqlite_where=text("direction_id IS NULL"),
        ),
        Index(
            "uq_residualverdict_direction_sha_attempt",
            "direction_id",
            "candidate_sha",
            "qualification_attempt_id",
            unique=True,
            postgresql_where=text("version_id IS NULL"),
            sqlite_where=text("version_id IS NULL"),
        ),
    )


class DimensionState(BaseModel):
    """Read-side projection of a single qualification dimension's state.

    Not a DB model — derived from ``last_qualification`` by
    :func:`app.qualification_state_resolver.resolve_dimension_states`.
    """

    key: str
    label: str
    status: str  # "passed" | "failed" | "unavailable" | "not_applicable" | "pending"
    score: float | None = None
    reason: str | None = None
