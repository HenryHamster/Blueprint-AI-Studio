"""Task-branch naming + per-project branch/namespace resolution (split out of shared_state).

Owns the agent work-branch derivation, the per-project integration / promotion /
namespace / PR-title-prefix resolvers, and the two startup backfills that
populate the corresponding columns. Re-exported from :mod:`app.shared_state` as
a thin facade so existing importers are unaffected. Behaviour-preserving.
"""

import asyncio
import logging
from typing import Any, Dict

from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.deploy_identity import project_is_deploy_repo
from app.runtime_config_state import runtime_config

logger = logging.getLogger(__name__)

SELF_REPO_INTEGRATION_BRANCH = "automain"

# Canonical default for task scope when not explicitly set.
# Operational modules (orchestrator, review pipeline, research completion) use this.
# Analytics/tracking modules use "unknown" to distinguish missing data from a real scope.
DEFAULT_TASK_SCOPE = "small"

# Maps calibrated task scope to a baseline effort level used at spawn time.
# small work runs faster/cheaper; large work gets deeper decomposition/analysis.
# Used by research_completion and task_completion as a floor on top of
# finding/parent-specified effort via _effort_rank (finding wins when higher).
_SCOPE_EFFORT_MAP = {"small": "low", "medium": "medium", "large": "high"}


def scope_to_effort(scope: str | None) -> str:
    """Return the baseline effort for a calibrated scope.

    Unknown / missing scopes fall back to "medium" so callers can safely take
    the max against other effort signals without suppressing them.
    """
    return _SCOPE_EFFORT_MAP.get(scope or "", "medium")


# Canonical ordering of effort tiers, lowest to highest. A task's effort tier
# determines its wall-clock budget (and, where the model is chosen per effort,
# its model). Used by escalate_effort to step a timed-out task up one tier on
# retry so the retry gets a larger budget instead of repeating a doomed one.
EFFORT_ORDER = ("low", "medium", "high", "max")


def escalate_effort(effort: str | None) -> str | None:
    """Return the next-higher effort tier, or None if already at/above the cap.

    Steps one tier up the canonical EFFORT_ORDER (low -> medium -> high -> max).
    Returns None when ``effort`` is already the top tier ("max"), or is unknown /
    missing — in both cases the caller leaves the effort unchanged and retries as
    before. Pure and side-effect-free.
    """
    try:
        idx = EFFORT_ORDER.index(effort)
    except ValueError:
        return None
    if idx + 1 >= len(EFFORT_ORDER):
        return None
    return EFFORT_ORDER[idx + 1]


# Maps a task type to its branch prefix. Must stay in sync with the agent's
# branch derivation (agent/src/index.ts, handlers/specialized.ts). When an
# orchestrator path needs to address an agent-pushed branch (review/fix spawn,
# operator merge), it must derive the name with this map — hardcoding
# "blueprint/task-{8hex}" silently broke review spawns for non-default task
# types (refactor → blueprint/refactor-{8hex}, etc.); see 2026-05-25 incident
# where PRs #5779/#5812/#5815/#5819 all stalled at review cap because the
# clone targeted blueprint/task-* against agents that had pushed
# blueprint/refactor-*.
TASK_TYPE_BRANCH_PREFIX: Dict[str, str] = {
    "create": "task",
    "qa": "task",
    "fix": "task",
    "refactor": "refactor",
    "docs": "docs",
    "deps": "deps",
    "migrate": "migrate",
}


# Canonical namespace for the Blueprint self-repo (the "blueprint" segment of
# ``blueprint/{prefix}-{8hex}``).  This is NOT a silent default for
# ``derive_task_branch`` — callers must pass the project's resolved namespace
# explicitly (see :func:`get_project_task_branch_namespace`).  It survives only
# as the terminal fallback inside ``get_project_task_branch_namespace`` and as
# the value the namespace backfill writes for Blueprint self-repo projects.
TASK_BRANCH_NAMESPACE_DEFAULT = "blueprint"


_missing_project_branch_alerts: set[tuple[str, str]] = set()
_blocking_project_branch_alerts: set[tuple[str, str]] = set()

# Process-local queue of branch-misconfiguration alert intents recorded by the
# SYNCHRONOUS resolvers (which run on async orchestrator/reaper hot loops and
# must never issue DB I/O). Each item is a dict carrying ``project_id``,
# ``title``, ``message`` and ``severity``. The orchestrator drains this queue
# asynchronously via :func:`flush_pending_branch_alerts`, which performs the
# durable, idempotent SELECT-then-INSERT so exactly one undismissed alert
# exists per (project, missing column) — surviving restarts (the in-memory
# dedup sets above forget across restarts; the DB-level dedup does not).
_pending_durable_branch_alerts: list[dict] = []


class InvalidProjectBranchConfiguration(RuntimeError):
    """Raised when a project's integration branch would route work to promotion."""

    def __init__(self, project_id: Any, detail: str):
        self.project_id = project_id
        self.detail = detail
        super().__init__(detail)


def derive_task_branch(
    task_id: Any,
    task_type: str | None,
    namespace: str,
) -> str:
    """Derive an agent work branch name (``{namespace}/{prefix}-{8hex}``)
    from the task type and id, matching the agent's branch derivation.

    ``namespace`` is REQUIRED — there is no silent ``"blueprint"`` default, so
    every call site for an arbitrary repo must thread through the project's
    actual namespace (resolve it via
    :func:`get_project_task_branch_namespace`).  Passing the wrong namespace
    yields a branch name no agent ever pushed and strands downstream review /
    fix respawns at ``git clone --branch``.
    """
    prefix = TASK_TYPE_BRANCH_PREFIX.get(task_type or "", "task")
    return f"{namespace}/{prefix}-{str(task_id).replace('-', '')[:8]}"


def _project_id_str(project) -> str:
    project_id = getattr(project, "id", None)
    return str(project_id) if project_id is not None else "<unknown>"


def _stage_project_branch_alert(
    project,
    *,
    dedupe_bucket: set[tuple[str, str]],
    dedupe_key_suffix: str,
    title: str,
    message: str,
    log_level: int,
    severity: str = "ERROR",
) -> None:
    project_id = getattr(project, "id", None)
    project_id_str = _project_id_str(project)
    dedupe_key = (project_id_str, dedupe_key_suffix)
    if dedupe_key in dedupe_bucket:
        return
    dedupe_bucket.add(dedupe_key)
    logger.log(log_level, "[BRANCH_RESOLVER] %s", message)

    # These resolvers are SYNCHRONOUS and called from async orchestrator/reaper
    # hot loops, so we MUST NOT issue DB I/O here (a sync query from an async
    # context risks MissingGreenlet, and per-resolve I/O would load the hot
    # path). Instead record intent into a process-local queue that the
    # orchestrator drains asynchronously via flush_pending_branch_alerts(),
    # which performs the durable, idempotent persist. The in-memory dedupe
    # bucket above keeps this to one enqueue per (project, column) per process.
    # Skip when there is no real project id to key the durable dedup on (fake
    # test doubles / detached objects lacking ``id``).
    if project_id is None:
        return
    _pending_durable_branch_alerts.append(
        {
            "project_id": project_id,
            "title": title,
            "message": message,
            "severity": severity,
        }
    )


async def flush_pending_branch_alerts(session) -> int:
    """Drain the pending branch-misconfiguration queue into durable Alerts.

    Called from the orchestrator's periodic monitoring seam in an async-safe
    context (off the synchronous resolver hot path). For each queued intent it
    performs a SELECT-then-INSERT keyed on an *undismissed*, task-less
    ``(project_id, title)`` Alert — mirroring
    :func:`app.alert_dispatch._create_strategic_alert` — so exactly one
    undismissed alert exists per (project, missing column).

    This makes the missing-branch signal durable and idempotent across
    restarts: the in-memory dedupe sets forget on restart and the resolver
    re-enqueues, but the SELECT prevents creating a second row when an
    undismissed alert already exists. The signal clears naturally once the
    column is populated (the resolver stops enqueuing) or the alert is
    dismissed (a fresh row is then created on the next encounter).

    Never raises — per-item failures are logged and the item is re-queued for a
    later flush so a transient DB error does not silently drop the signal.
    Returns the number of new alerts created.
    """
    global _pending_durable_branch_alerts
    if not _pending_durable_branch_alerts:
        return 0
    pending = _pending_durable_branch_alerts
    _pending_durable_branch_alerts = []

    from sqlalchemy import tuple_
    from sqlmodel import select

    from app.broadcast import broadcast_to_project
    from app.models import Alert, AlertSeverity

    created = 0
    failed: list[dict] = []

    # Pre-filter valid items and batch-fetch all existing undismissed alerts in
    # one query instead of one SELECT per item.
    valid_items = [
        item
        for item in pending
        if item.get("project_id") is not None and item.get("title")
    ]
    if valid_items:
        pairs = list({
            (item["project_id"], item["title"]) for item in valid_items
        })
        _existing_result = await session.execute(
            select(Alert).where(
                tuple_(Alert.project_id, Alert.title).in_(pairs),
                Alert.task_id.is_(None),
                Alert.dismissed_at.is_(None),
            )
        )
        existing_keys: set[tuple] = {
            (a.project_id, a.title) for a in _existing_result.scalars().all()
        }
    else:
        existing_keys = set()

    for item in pending:
        project_id = item.get("project_id")
        title = item.get("title")
        if project_id is None or not title:
            continue
        try:
            if (project_id, title) in existing_keys:
                continue
            try:
                severity = AlertSeverity(item.get("severity") or "ERROR")
            except ValueError:
                severity = AlertSeverity.ERROR
            alert = Alert(
                project_id=project_id,
                severity=severity,
                title=title,
                message=item.get("message") or title,
            )
            session.add(alert)
            created += 1
            existing_keys.add((project_id, title))
            await broadcast_to_project(
                str(project_id),
                "alert_created",
                {
                    "alert_id": str(alert.id),
                    "severity": severity.value,
                    "title": title,
                },
            )
        except Exception:  # noqa: BLE001
            failed.append(item)
            logger.warning(
                "[BRANCH_RESOLVER] could not persist pending branch alert for "
                "project %s (will retry next flush)",
                project_id,
            )
    if failed:
        _pending_durable_branch_alerts.extend(failed)
    return created


def _signal_missing_project_branch(
    project,
    column_name: str,
    alert_title: str,
    *,
    fallback_branch: str,
) -> None:
    project_id = getattr(project, "id", None)
    project_id_str = str(project_id) if project_id is not None else "<unknown>"
    _stage_project_branch_alert(
        project,
        dedupe_bucket=_missing_project_branch_alerts,
        dedupe_key_suffix=column_name,
        title=alert_title,
        message=(
            f"Project {project_id_str} has NULL {column_name}; "
            f"falling back to {fallback_branch} until the column is populated."
        ),
        log_level=logging.WARNING,
    )


def _signal_blocking_project_branch_configuration(
    project,
    issue_code: str,
    detail: str,
) -> None:
    _stage_project_branch_alert(
        project,
        dedupe_bucket=_blocking_project_branch_alerts,
        dedupe_key_suffix=issue_code,
        title="Blocking branch configuration",
        message=detail,
        log_level=logging.ERROR,
    )


# Stable, escalated title for the blocking-class branch-safety signal. Used as
# the database-existence dedupe key (project_id + this title + not-dismissed) so
# the loud signal survives a process restart and is shared across backend
# instances — unlike the in-process ``_blocking_project_branch_alerts`` set,
# which resets on restart and is poisoned by a rolled-back caller. No new
# severity tier is introduced: the house convention is top severity (ERROR) +
# this escalated title + a paired BLOCKER operator message.
_UNSAFE_BRANCH_ALERT_TITLE = (
    "Unsafe branch configuration — fleet work would route to the promotion target"
)


async def signal_unsafe_branch_configuration(
    session,
    project,
    *,
    issue_code: str,
    detail: str,
) -> None:
    """Durably raise the loud, blocking-class operator signal for an unsafe
    branch configuration and commit it.

    Mirrors the release-stall pattern (an Alert paired with a high-visibility
    BLOCKER operator message). Both halves are deduplicated by a
    database-existence check rather than an in-process set, so the signal
    survives a restart and is shared across instances, and neither half is
    silently dropped by a rolled-back caller because this commits its own work.

    The Alert is emitted at the top severity (``ERROR``) under the stable
    escalated title :data:`_UNSAFE_BRANCH_ALERT_TITLE`; the paired
    :class:`ProjectMessage` is of type ``BLOCKER`` and carries its own stable
    dedupe key in ``context`` so it lands on the operator's high-signal message
    board exactly once per (project, issue) until resolved.

    Callers that raise the blocking signal from an async write path (rollup /
    admin merge gates) should ``await`` this instead of the best-effort sync
    :func:`_signal_blocking_project_branch_configuration`.
    """
    from sqlmodel import select

    from app.models import Alert, AlertSeverity, MessageType, ProjectMessage

    project_id = getattr(project, "id", None)
    logger.error("[BRANCH_RESOLVER] %s", detail)
    if session is None or project_id is None:
        return

    # Alert: database-existence dedupe on (project, stable title, not dismissed).
    existing_alert = await session.execute(
        select(Alert).where(
            Alert.project_id == project_id,
            Alert.title == _UNSAFE_BRANCH_ALERT_TITLE,
            Alert.dismissed_at.is_(None),
        )
    )
    if existing_alert.scalars().first() is None:
        session.add(
            Alert(
                project_id=project_id,
                severity=AlertSeverity.ERROR,
                title=_UNSAFE_BRANCH_ALERT_TITLE,
                message=detail,
            )
        )

    # Paired BLOCKER operator message, deduped by its own stable context key.
    dedupe_key = f"unsafe_branch_configuration:{project_id}:{issue_code}"
    _blocker_result = await session.execute(
        select(ProjectMessage.id)
        .where(
            ProjectMessage.project_id == project_id,
            ProjectMessage.message_type == MessageType.BLOCKER,
            ProjectMessage.status == "open",
            ProjectMessage.context["dedupe_key"].as_string() == dedupe_key,
        )
        .limit(1)
    )
    already_posted = _blocker_result.scalar() is not None
    if not already_posted:
        session.add(
            ProjectMessage(
                project_id=project_id,
                message_type=MessageType.BLOCKER,
                from_type="system",
                content=detail,
                context={
                    "event_type": "unsafe_branch_configuration",
                    "issue_code": issue_code,
                    "dedupe_key": dedupe_key,
                },
            )
        )

    await session.commit()


def _uses_legacy_branch_resolver_shape(project) -> bool:
    """Keep partial test doubles on the old fallback contract.

    Real Project rows expose both integration and promotion columns plus the
    self-repo metadata. Some older unit tests still use a minimal stub with only
    ``integration_branch``; preserve the legacy `'main'` fallback for those
    detached shapes so the runtime safety hardening only applies to real
    project-like objects.
    """
    return project is not None and not any(
        hasattr(project, attr)
        for attr in ("promotion_target", "is_self_repo", "github_repo")
    )


def resolve_task_branch(task: Any, namespace: str) -> str:
    """Return the branch a task's agent actually pushed to.

    Precedence:
      1. ``task.context["review_loop"]["branch"]`` — set by review/fix spawn
      2. ``task.first_payload["branch"]`` — set when an agent claims a task
      3. ``task.context["branch"]`` — legacy fallback
      4. ``derive_task_branch(task.id, task_type, namespace)`` — last-resort
         synthesis

    ``namespace`` is REQUIRED and is used only for the last-resort synthesis
    branch (4); callers should resolve it from the task's project via
    :func:`get_project_task_branch_namespace`.  It no longer silently defaults
    to ``"blueprint"`` so a non-Blueprint repo never synthesizes a branch under
    the wrong namespace.

    Fix tasks reuse the parent's PR branch — synthesizing
    ``{namespace}/task-{fix_id}`` from a fix child's own id yields a name no
    agent ever pushed, breaking downstream review respawns that try to clone
    it. The first three sources carry the real branch; derivation is only
    safe when none of them are present (a freshly-spawned create/refactor
    task with no payload-provided branch yet).
    """
    ctx = getattr(task, "context", None) or {}
    review_loop = ctx.get("review_loop") or {}
    branch = review_loop.get("branch")
    if branch:
        return branch
    payload = getattr(task, "first_payload", None) or {}
    branch = payload.get("branch")
    if branch:
        return branch
    branch = ctx.get("branch")
    if branch:
        return branch
    return derive_task_branch(getattr(task, "id", None), payload.get("type"), namespace)


def get_project_integration_branch(project) -> str:
    """Return the integration branch for a project.

    Fallback chain:
      1. project.integration_branch (per-project column, added by migration 0122+)
      2. "automain" for self-repo projects (qualification trunk)
      3. "main" for non-self-repo projects (promotion/release target)

    The deprecated runtime_config bridge was removed after the per-project
    column stabilized across the migration/backfill rollout.
    """
    if _uses_legacy_branch_resolver_shape(project):
        integration_branch = getattr(project, "integration_branch", None)
        return integration_branch or "main"

    integration_branch = getattr(project, "integration_branch", None)
    if integration_branch:
        return integration_branch

    # Per-project column is unset. Fail SAFE (never raise) so the resolver can
    # be called from read endpoints, background recovery loops, agent-facing
    # completion handlers, and the ``Project.protected_branches`` property
    # without a misconfiguration aborting the whole sweep or 500ing a request.
    # Self-repo projects map an unset integration branch to the qualification
    # trunk ("automain"); every other project falls back to "main". The
    # operator-facing signal is still staged so the unset column stays visible.
    # Promotion-target safety is enforced at the merge/rollup boundary (see
    # rollup_management/admin_task_mutations), not in this hot-path resolver.
    if _is_blueprint_self_repo(project):
        _signal_missing_project_branch(
            project,
            "integration_branch",
            "Missing integration_branch",
            fallback_branch=SELF_REPO_INTEGRATION_BRANCH,
        )
        return SELF_REPO_INTEGRATION_BRANCH
    _signal_missing_project_branch(
        project,
        "integration_branch",
        "Missing integration_branch",
        fallback_branch="main",
    )
    return "main"


def get_project_task_branch_namespace(project) -> str:
    """Return the task-branch namespace for a project.

    Fallback chain:
      1. project.build_config['task_branch_namespace'] (per-project override)
      2. runtime_config['task_branch_namespace'] (DEPRECATED global fallback)
      3. ``TASK_BRANCH_NAMESPACE_DEFAULT`` (``"blueprint"``)

    A non-Blueprint install (e.g. a project named ``autoproj``) configures its
    namespace through ``build_config`` so its agent work branches land under
    ``autoproj/{prefix}-{8hex}`` instead of the Blueprint default.
    """
    if project is not None:
        build_config = getattr(project, "build_config", None) or {}
        per_project = build_config.get("task_branch_namespace")
        if per_project:
            return str(per_project)
    return runtime_config.get("task_branch_namespace", TASK_BRANCH_NAMESPACE_DEFAULT)


def get_project_pr_title_prefix(project) -> str:
    """Return the PR title prefix string for a project, with a trailing space.

    Reads ``build_config['pr_title_prefix']``.  If set, returns the value with
    a single trailing space so callers can write ``f"{prefix}Rollup: ..."``
    directly.  If absent or empty, returns ``""`` so no prefix is injected.

    Example ``build_config`` entry::

        {"pr_title_prefix": "[Blueprint]"}
    """
    if project is not None:
        build_config = getattr(project, "build_config", None) or {}
        per_project = build_config.get("pr_title_prefix")
        if per_project:
            return f"{per_project} "
    return ""


def ensure_project_pr_title_prefix(project, title: str) -> str:
    """Return *title* with the project's PR title prefix prepended if absent.

    Used by the backend PR-creation endpoint (``/api/agent/github/pull-requests``)
    to normalise agent-authored PR titles so they carry the project's configured
    branding prefix even when the agent's prompt did not include it explicitly.

    Idempotent: if *title* already starts with the prefix token (ignoring the
    trailing space that :func:`get_project_pr_title_prefix` appends), returns
    *title* unchanged.  When no prefix is configured returns *title* as-is.
    """
    prefix = get_project_pr_title_prefix(project)
    if not prefix:
        return title
    # prefix already carries a trailing space (e.g. "[Blueprint] ").
    # Compare against the bare token so "[Blueprint] feat: x" is not re-prefixed.
    if title.startswith(prefix.rstrip()):
        return title
    return f"{prefix}{title}"


def get_project_promotion_target(project) -> str:
    """Return the promotion target branch for a project.

    Fallback chain:
      1. project.promotion_target (per-project column, added by migration 0125+)
      2. "main"

    The deprecated runtime_config bridge was removed after the per-project
    column stabilized across the migration/backfill rollout.
    """
    promotion_target = getattr(project, "promotion_target", None)
    if promotion_target:
        return promotion_target
    _signal_missing_project_branch(
        project,
        "promotion_target",
        "Missing promotion_target",
        fallback_branch="main",
    )
    return "main"


_PROMOTION_TARGET_LANDING_ALERT_TITLE = (
    "Merged PR landed on promotion target before integration"
)


def _promotion_target_landing_reason(
    *,
    pr_label: str,
    landed_branch: str,
    integration_branch: str,
    source: str,
) -> str:
    return (
        f"{pr_label} landed on {landed_branch!r} via {source}, which matches "
        f"promotion_target and is distinct from integration_branch "
        f"{integration_branch!r}. The merge does not count as landing on the "
        "integration trunk, so pr_merged was left unset."
    )


async def _create_promotion_target_landing_alert(
    task: Any,
    session,
    *,
    pr_number: int | None,
    landed_branch: str,
    integration_branch: str,
    source: str,
) -> None:
    from app.broadcast import broadcast_to_project
    from app.models import Alert, AlertSeverity
    from app.notification_dispatch import dispatch_external_notification

    existing = await session.execute(
        select(Alert).where(
            Alert.task_id == getattr(task, "id", None),
            Alert.dismissed_at.is_(None),
            Alert.title == _PROMOTION_TARGET_LANDING_ALERT_TITLE,
        )
    )
    if existing.scalars().first() is not None:
        return

    pr_label = f"PR #{pr_number}" if pr_number is not None else "A merged PR"
    reason = _promotion_target_landing_reason(
        pr_label=pr_label,
        landed_branch=landed_branch,
        integration_branch=integration_branch,
        source=source,
    )
    alert = Alert(
        project_id=getattr(task, "project_id"),
        severity=AlertSeverity.ERROR,
        title=_PROMOTION_TARGET_LANDING_ALERT_TITLE,
        message=reason,
        task_id=getattr(task, "id", None),
    )
    session.add(alert)
    await broadcast_to_project(
        str(getattr(task, "project_id")),
        "alert_created",
        {"alert_id": str(alert.id), "severity": "ERROR", "title": alert.title},
    )

    try:
        ctx = getattr(task, "context", None) or {}
        payload = getattr(task, "first_payload", None) or {}
        pr_url = (
            ctx.get("pr_url") or ctx.get("rollup_pr_url") or payload.get("pr_url") or ""
        )
        if not pr_url and pr_number is not None:
            repo_full = ctx.get("repo_full_name") or ctx.get("repo") or ""
            if repo_full:
                pr_url = f"https://github.com/{repo_full}/pull/{pr_number}"
        raw_critical = ctx.get("critical_files") or ctx.get("_pr_changed_files") or []
        critical_files = (
            [f for f in raw_critical if isinstance(f, str)][:10]
            if isinstance(raw_critical, list)
            else []
        )
        direction_text = (
            ctx.get("direction_text") or payload.get("direction_text") or ""
        )
        scope = ctx.get("scope") or payload.get("scope") or ""
        asyncio.ensure_future(  # noqa: RUF006
            dispatch_external_notification(
                getattr(task, "project_id"),
                "APPROVAL_NEEDED",
                {
                    "task_id": str(getattr(task, "id", "")),
                    "task_title": getattr(task, "title", None)
                    or str(getattr(task, "id", ""))[:8],
                    "pr_number": pr_number,
                    "reason": reason,
                    "approval_reason": reason,
                    "pr_url": pr_url,
                    "critical_files": critical_files,
                    "direction_id": ctx.get("direction_id") or "",
                    "direction_text": direction_text[:200],
                    "scope": scope,
                },
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[NOTIFY] promotion-target APPROVAL_NEEDED dispatch setup failed: %s",
            exc,
        )


def resolve_pr_landing_branch(
    task: Any, project, *, merged_base_branch: str | None = None
) -> str:
    """Return the branch a merged PR actually landed on for ``pr_merged`` decisions.

    Prefer the authoritative GitHub/webhook base branch when available. Fall back
    to the task's review-loop / payload base_branch so recovery paths still make a
    deterministic decision when the API payload omits ``base.ref``.
    """
    if merged_base_branch:
        return merged_base_branch
    ctx = getattr(task, "context", None) or {}
    review_loop = ctx.get("review_loop") or {}
    branch = review_loop.get("base_branch")
    if branch:
        return branch
    payload = getattr(task, "first_payload", None) or {}
    branch = payload.get("base_branch")
    if branch:
        return branch
    return get_project_integration_branch(project)


def is_custom_staged_rollup_root(task: Any, project, base_branch: str | None) -> bool:
    """True when a rollup/root task landed on a configured custom staging branch.

    The deferred-downstream semantics only apply to projects that replaced the
    default ``research/`` / ``plan/`` staging prefixes with a custom family
    such as ``release/``. Default Blueprint research/plan review merges keep
    their historical terminal behavior.
    """
    if project is None or not getattr(project, "is_staging_branch", None):
        return False
    if not project.is_staging_branch(base_branch):
        return False
    if project.staging_branch_prefixes == ("research/", "plan/"):
        return False

    ctx = getattr(task, "context", None) or {}
    if ctx.get("rollup_pr_number") or bool(ctx.get("_is_rollup_review")):
        return True

    payload = getattr(task, "first_payload", None) or {}
    return payload.get("type") in {"research", "plan"}


async def classify_rollup_merge_outcome(
    task: Any,
    project,
    session,
    *,
    pr_number: int | None,
    merged_base_branch: str | None = None,
    source: str,
) -> tuple[bool, bool, str]:
    """Return ``(record_pr_merged, terminal_rollup_merge, landed_branch)``.

    Custom staged rollup roots stay non-terminal when they land on an
    intermediate staging branch. Promotion-target landings are terminal but do
    not count as integration landings when promotion and integration differ.
    """
    landed_branch = resolve_pr_landing_branch(
        task, project, merged_base_branch=merged_base_branch
    )
    if (
        is_custom_staged_rollup_root(task, project, landed_branch)
        and project is not None
    ):
        integration_branch = get_project_integration_branch(project)
        promotion_target = get_project_promotion_target(project)
        landed_on_integration = landed_branch == integration_branch
        landed_on_promotion = landed_branch == promotion_target
        if not landed_on_integration and landed_on_promotion:
            await merged_pr_counts_as_integration_landing(
                task,
                project,
                session,
                pr_number=pr_number,
                merged_base_branch=landed_branch,
                source=source,
            )
        return (
            landed_on_integration,
            landed_on_integration or landed_on_promotion,
            landed_branch,
        )

    return (
        await merged_pr_counts_as_integration_landing(
            task,
            project,
            session,
            pr_number=pr_number,
            merged_base_branch=landed_branch,
            source=source,
        ),
        True,
        landed_branch,
    )


async def merged_pr_counts_as_integration_landing(
    task: Any,
    project,
    session,
    *,
    pr_number: int | None,
    merged_base_branch: str | None = None,
    source: str,
) -> bool:
    """Return whether a merged PR should set ``context['pr_merged']``.

    The one disallowed case is a merge that landed on the project's configured
    ``promotion_target`` while that branch is distinct from the resolved
    ``integration_branch``. In that case an ERROR alert is raised so operators
    see the out-of-band promotion landing immediately, and callers must leave
    ``pr_merged`` unset.
    """
    if project is None or session is None:
        return True

    integration_branch = get_project_integration_branch(project)
    promotion_target = get_project_promotion_target(project)
    landed_branch = resolve_pr_landing_branch(
        task, project, merged_base_branch=merged_base_branch
    )
    if landed_branch != promotion_target or promotion_target == integration_branch:
        return True

    await _create_promotion_target_landing_alert(
        task,
        session,
        pr_number=pr_number,
        landed_branch=landed_branch,
        integration_branch=integration_branch,
        source=source,
    )

    logger.error(
        "[PR_BRANCH_GUARD] Task %s PR #%s landed on promotion target %s "
        "(integration=%s) via %s; leaving pr_merged unset",
        getattr(task, "id", None),
        pr_number,
        landed_branch,
        integration_branch,
        source,
    )
    return False


async def recut_dependent_base_from_branch(
    task, project, session, *, source_branch: str
) -> str:
    """Re-cut a held code-dependent's base from a proven source branch.

    The source is normally project integration. For sibling plans under one
    research parent it can be their shared research staging branch after the
    dependency's plan parent has merged there.

    The dependent keeps its own staging branch (it still rolls up); only its
    content is brought up to current integration. Sibling work already on a
    shared staging branch is preserved because a merge never rewrites the base's
    own commits.

    Returns one of:
      * ``"ok"``        — base already bases on integration (no-op) OR the merge
        succeeded / was already up to date. The base now contains the landed
        code; the dependent is safe to dispatch.
      * ``"conflict"``  — the merge would conflict; the caller must NOT dispatch
        and should hold for follow-up (a fix task / operator).
      * ``"missing"``   — the dependent's base (or the integration head) no
        longer exists on GitHub (HTTP 404). This is a PERMANENT, non-transient
        condition (the staging base was cleaned up after its rollup merged, or
        never existed); the caller must NOT retry it forever as if transient.
      * ``"error"``     — a transient GitHub / credential error; retry next
        cycle (the dependent stays held).

    Deferred imports keep this module a leaf on the hot resolver path.
    """
    base_branch = (getattr(task, "first_payload", None) or {}).get(
        "base_branch"
    ) or get_project_integration_branch(project)
    if base_branch == source_branch:
        return "ok"

    try:
        from app.routers.agent import get_github_token_for_task
        from app.github import merge_branch

        token, owner, repo = await get_github_token_for_task(task.id, session)
        result = await merge_branch(
            token,
            owner,
            repo,
            base=base_branch,
            head=source_branch,
            commit_message=(
                f"Merge {source_branch} into {base_branch} so dependent "
                f"task {str(task.id)[:8]} bases on landed dependency code"
            ),
            project_id=str(getattr(task, "project_id", "") or "") or None,
        )
    except Exception:
        logger.exception(
            "[CROSS-BASE] Re-cut of base %r from source %r failed for task %s",
            base_branch,
            source_branch,
            getattr(task, "id", None),
        )
        return "error"

    if result.get("merged"):
        logger.info(
            "[CROSS-BASE] Re-cut base %r from source %r for task %s (%s)",
            base_branch,
            source_branch,
            getattr(task, "id", None),
            "already up to date" if result.get("already") else "merged",
        )
        return "ok"
    if result.get("conflict"):
        return "conflict"
    if result.get("missing"):
        # A deleted/absent base or head branch (HTTP 404) is PERMANENT — never
        # fold it into the transient "error" bucket that retries forever.
        return "missing"
    return "error"


async def recut_dependent_base_from_integration(task, project, session) -> str:
    """Backward-compatible wrapper for integration-sourced re-cuts."""
    return await recut_dependent_base_from_branch(
        task,
        project,
        session,
        source_branch=get_project_integration_branch(project),
    )


def is_single_branch_project(project) -> bool:
    """True when a project's integration branch IS its promotion target.

    This is the canonical "LTS model inactive" predicate: when the branch the
    pipeline builds on (:func:`get_project_integration_branch`) coincides with
    the branch promotions land on (:func:`get_project_promotion_target`) there
    is no separate release trunk.  It is the common default for an onboarded
    external repo (both columns backfilled to the GitHub default branch), versus
    Blueprint's own self-repo pair (``automain`` integration / ``main``
    promotion), where the two differ and the LTS model is active.

    The release machinery keys several short-circuits on this exact comparison —
    ``release_promotion.promote`` records an inert already-landed success rather
    than opening a ``release-vN -> promotion_target`` PR that would target the
    very branch work already merges to; ``hotfix_backmerge`` skips the
    ``promotion_target -> integration_branch`` back-merge as ``lts_inactive``;
    and ``release_reconciliation`` bails out rather than reconciling a candidate
    against a branch it already lives on.  Resolving both names through the
    per-project resolver here keeps that decision in one tested place instead of
    re-deriving the literal comparison at each call site.
    """
    return get_project_integration_branch(project) == get_project_promotion_target(
        project
    )


async def backfill_branch_columns(session, project_id=None) -> dict:
    """Populate NULL integration_branch / promotion_target from GitHub defaults.

    Runs once at startup (called from main.py lifespan) after migrations finish.
    Only writes to columns that are still NULL — skips rows already populated so
    the function is safe to re-run.  Self-repo projects with NULL
    integration_branch are left untouched; the runtime resolver maps that state
    to ``automain`` while preserving the explicit operator-facing signal that the
    column is still unset.  Self-repo projects with NULL promotion_target get
    'main' without a GitHub round-trip.

    For non-self-repo projects, resolves GitHub credentials and calls
    get_repo_default_branch().  When the GitHub API call fails, emits a per-project
    Alert so an operator can fix credentials and re-run rather than silently falling
    back to 'main'.

    When project_id is provided, only that project is processed.  Used by
    POST /api/admin/projects/{id}/backfill-branches to re-run the backfill for a
    single project without restarting the service.

    Returns a summary dict:
      {
        "filled": int,     # columns written
        "skipped": int,    # projects already fully populated
        "errored": int,    # projects where GitHub API call failed
        "self_repo_skipped": int,  # self-repo integration_branch left as-is
      }
    """
    from app.models import Alert, AlertSeverity, Project
    from app.release_promotion import _resolve_github_with_session
    from app import github as _github
    from sqlmodel import select

    summary = {"filled": 0, "skipped": 0, "errored": 0, "self_repo_skipped": 0}

    try:
        query = select(Project).where(
            (Project.integration_branch.is_(None))
            | (Project.promotion_target.is_(None))
        )
        if project_id is not None:
            query = query.where(Project.id == project_id)
        result = await session.execute(query)
        projects = result.scalars().all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[BRANCH_BACKFILL] Could not query projects: %s", exc)
        return summary

    for project in projects:
        need_integration = project.integration_branch is None
        need_promotion = project.promotion_target is None

        if not need_integration and not need_promotion:
            summary["skipped"] += 1
            continue

        # Self-repo: leave NULL as-is. The live resolver treats the self-repo's
        # unset integration branch as automain without silently rewriting the
        # column behind the operator's back. Classify via the deploy-repo
        # predicate (deploy identity when stamped, else the is_self_repo
        # fallback) so a misset flag can't misroute the deploy's own repo.
        is_deploy_repo = project_is_deploy_repo(project)
        if is_deploy_repo and need_integration:
            summary["self_repo_skipped"] += 1
            need_integration = False

        # Self-repo promotion_target: no GitHub round-trip needed.
        if is_deploy_repo and need_promotion:
            project.promotion_target = "main"
            flag_modified(project, "promotion_target")
            session.add(project)
            summary["filled"] += 1
            need_promotion = False

        if not need_integration and not need_promotion:
            continue

        # Non-self-repo: resolve GitHub credentials and fetch the default branch.
        try:
            resolved = await _resolve_github_with_session(session, project)
            if resolved is None:
                raise ValueError("no GitHub credentials available")
            token, owner, repo = resolved
            default_branch = await _github.get_repo_default_branch(token, owner, repo)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[BRANCH_BACKFILL] project %s (%s/%s): GitHub call failed: %s",
                project.id,
                getattr(project, "github_owner", "?"),
                getattr(project, "github_repo", "?"),
                exc,
            )
            try:
                alert = Alert(
                    project_id=project.id,
                    severity=AlertSeverity.WARNING,
                    title="Branch backfill failed — integration_branch or promotion_target still NULL",
                    message=(
                        f"Could not populate integration_branch/promotion_target for project "
                        f"{project.id} from GitHub: {str(exc)[:300]}. "
                        "Fix GitHub credentials and re-run the backfill (restart the service or "
                        "call POST /api/admin/projects/{id}/backfill-branches)."
                    ),
                )
                session.add(alert)
            except Exception as alert_exc:  # noqa: BLE001
                logger.debug(
                    "[BRANCH_BACKFILL] alert insertion failed (non-fatal): %s",
                    alert_exc,
                )
            summary["errored"] += 1
            continue

        if need_integration:
            project.integration_branch = default_branch
            flag_modified(project, "integration_branch")
            summary["filled"] += 1
        if need_promotion:
            project.promotion_target = default_branch
            flag_modified(project, "promotion_target")
            summary["filled"] += 1
        session.add(project)

    if summary["filled"] or summary["errored"]:
        logger.info(
            "[BRANCH_BACKFILL] complete: filled=%d skipped=%d errored=%d self_repo_skipped=%d",
            summary["filled"],
            summary["skipped"],
            summary["errored"],
            summary["self_repo_skipped"],
        )

    return summary


# Historical Blueprint repo aliases whose canonical task-branch namespace is
# "blueprint".  Slug-derivation of "BlueprintAIStudio" would otherwise produce
# "blueprintaistudio" — divergent from what the agent's hardcoded push path
# uses, which strands every merge-conflict fix spawn (incident 2026-05-26..28,
# six failed fix tasks before the wedge below landed).
_BLUEPRINT_REPO_ALIASES = frozenset({"blueprintaistudio"})


def _is_blueprint_self_repo(project) -> bool:
    """True when *project* should use the canonical ``"blueprint"`` namespace.

    Catches both the correctly-flagged case (``is_self_repo=True``) and the
    historically-broken case where ``is_self_repo`` is misset False but the
    GitHub repo name is a known Blueprint alias.

    INTENTIONALLY EXEMPT from deploy-identity de-gating: this is_self_repo read
    is a deliberate fallback for namespace-alias resolution (GitHub name matching),
    not deploy-level observability. Must remain as-is to preserve historical
    incident recovery for misset is_self_repo flags (2026-05-26..28).
    """
    if getattr(project, "is_self_repo", False):
        return True
    return _project_repo_is_blueprint(project)


def _project_repo_is_blueprint(project) -> bool:
    """True when *project*'s GitHub repo is a known Blueprint alias.

    Classifies purely by repo identity — it deliberately does NOT read the raw
    ``is_self_repo`` flag.  The canonical ``"blueprint"`` task-branch namespace
    is owned by the actual Blueprint repo (the aliases below), not by any
    project that merely happens to carry ``is_self_repo=True``; a non-Blueprint
    self-hosted repo must keep its own slug-derived namespace rather than being
    misrouted under ``blueprint/...``.
    """
    github_repo = getattr(project, "github_repo", "") or ""
    return github_repo.lower() in _BLUEPRINT_REPO_ALIASES


async def backfill_task_branch_namespace(session, project_id=None) -> dict:
    """Populate or correct ``task_branch_namespace`` in ``build_config``.

    Runs at startup and via the admin backfill endpoint.

    Two paths:
      * **Fill-when-empty (legacy):** if the namespace is absent or empty, set
        it to ``"blueprint"`` for Blueprint self-repo projects and to a
        slugified ``github_repo`` for everything else.  Mirrors
        ``_derive_task_branch_namespace`` in ``admin_onboard.py``.
      * **Corrective overwrite (incident 2026-05-26..28):** if the project is
        the Blueprint self-repo (by historical alias — ``BlueprintAIStudio``)
        but the persisted namespace differs from
        ``"blueprint"``, force-overwrite it.  The agent always pushes to
        ``blueprint/...``; an out-of-sync orchestrator-side namespace makes
        every merge-conflict fix-spawn try to clone a nonexistent branch.
        Six failed fix tasks tripped this in ~36h before the wedge landed.

    Idempotent for projects already at the expected value.  When *project_id*
    is provided only that project is processed.

    Returns a summary dict::

        {
          "namespace_filled":    int,  # missing value populated
          "namespace_skipped":   int,  # already correct, untouched
          "namespace_corrected": int,  # stale value overwritten
        }
    """
    import re as _re

    from app.models import Project
    from sqlmodel import select

    summary = {
        "namespace_filled": 0,
        "namespace_skipped": 0,
        "namespace_corrected": 0,
    }

    try:
        query = select(Project)
        if project_id is not None:
            query = query.where(Project.id == project_id)
        result = await session.execute(query)
        projects = result.scalars().all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[NS_BACKFILL] Could not query projects: %s", exc)
        return summary

    for project in projects:
        build_config = project.build_config or {}
        current_ns = build_config.get("task_branch_namespace") or ""
        github_repo = getattr(project, "github_repo", "") or ""
        is_blueprint = _project_repo_is_blueprint(project)

        if is_blueprint:
            expected_ns = "blueprint"
            if current_ns == expected_ns:
                summary["namespace_skipped"] += 1
                continue
            if current_ns:
                logger.warning(
                    "[NS_BACKFILL] project %s (%s): correcting stale namespace %r -> %r",
                    project.id,
                    github_repo or "<no-repo>",
                    current_ns,
                    expected_ns,
                )
                summary["namespace_corrected"] += 1
            else:
                summary["namespace_filled"] += 1
            project.build_config = {
                **build_config,
                "task_branch_namespace": expected_ns,
            }
            flag_modified(project, "build_config")
            session.add(project)
            continue

        # Non-Blueprint project: legacy fill-when-empty path remains idempotent.
        if current_ns:
            summary["namespace_skipped"] += 1
            continue

        slug = _re.sub(r"[^a-z0-9]+", "-", github_repo.lower()).strip("-")
        namespace = slug[:40] if slug else "project"
        project.build_config = {**build_config, "task_branch_namespace": namespace}
        flag_modified(project, "build_config")
        session.add(project)
        summary["namespace_filled"] += 1

    if summary["namespace_filled"] or summary["namespace_corrected"]:
        logger.info(
            "[NS_BACKFILL] complete: namespace_filled=%d namespace_skipped=%d namespace_corrected=%d",
            summary["namespace_filled"],
            summary["namespace_skipped"],
            summary["namespace_corrected"],
        )

    return summary


def get_project_broadcast_limit(project) -> int:
    """Return the broadcast alert limit for a project.

    Fallback chain:
      1. project.design_doc['_broadcast_limit'] (per-project override via admin endpoint)
      2. runtime_config["project_broadcast_limit"] (global default)
      3. 1000
    """
    per_project = (getattr(project, "design_doc", None) or {}).get("_broadcast_limit")
    if per_project is not None:
        return int(per_project)
    return runtime_config.get("project_broadcast_limit", 1000)
