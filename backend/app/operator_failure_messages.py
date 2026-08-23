"""Truthful operator-facing terminal-failure messages.

Both defect classes this module fixes are violations of the standing
"surface reasons, not states" rule: the system already HELD the true reason
and printed a different, misleading one.

**Defect A — GCP vocabulary on a fleet that has no GCP.** The reaper's
terminal PROVISION_FAILURE branch reached straight for ``provision_zone`` and
the GCP provisioning error, defaulting to the literal strings ``"unknown
zone"`` and ``"no GCP error captured"`` when absent. Blueprint's fleet is now
three local bwrap runners (``hamsterwheel``, ``dgx-1``, ``iconlab``), so a
local-runner death has neither field and rendered as::

    Provision failed 3x — zone: unknown zone, acct: 406bb865-...
    GCP error: no GCP error captured. Last reap: Silent pre-claim death ...

Live on tasks ``82eb352a`` and ``4cd822c3``: both ran on ``hamsterwheel``
with ``vm_instance_id='runner:707de5fb-...'`` and a real
``agent_failure_category_raw='walkthrough_harness_failed'``. The operator was
pointed at a GCP zone that was never involved while the actual cause sat
unread in ``task.context``. ``compose_provision_failure_message`` branches on
the execution substrate FIRST, names the runner, and surfaces the recorded
agent category; the GCP wording is preserved byte-for-byte for real GCP VMs.

**Defect B — the terminal message contradicts the recorded category.** The
status writer persisted the agent's last free-text log tail verbatim. Two
live shapes:

* Task ``1745bab4`` (create, deepseek-v4-flash) — ``latest_status_message``
  was a raw NDJSON provider-stream fragment starting mid-token
  (``se_id":null,"uuid":"21ba040c-..."} {"type":"stream_event",...``) while
  ``context['agent_failure_category_raw']`` said ``blocked_precondition``.
  Zero operator signal.
* Task ``075854e6`` (create, claude-sonnet-4-6) — the message read "No
  uncommitted changes found — the branch is clean ..." while the recorded
  category was ``rate_limited``. Two mutually exclusive stories, and the
  surfaced one was the wrong one.

``compose_agent_terminal_message`` leads with the structured category and
keeps the free text as supporting detail (a message that says only
``rate_limited`` is worse than one that says why), and never persists text
that parses as a raw provider stream frame.

**Defect C — the non-terminal paths threw the composed truth away again.**
Fixing Defect B made the terminal write truthful, but every requeue/park
branch in ``routers/agent_status.py`` then overwrote the column with a bare
synthesized state (``f"{failure_class.title()} — requeued"``), and the
reaper's own non-terminal requeues dropped the ``reap_reason`` they were
holding. ``Code Error — requeued`` names a state and no reason; the agent's
explanation arrived in the SAME request and was discarded.
``compose_requeue_status_message`` keeps both halves — the engine's
disposition clause leads, the shared terminal composer supplies the
sanitized, category-led diagnosis behind it.

Kept in its own module rather than inline because ``task_lifecycle.py`` and
``routers/agent_status.py`` are both chronic size offenders, and because pure
composition functions are unit-testable without a DB.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

__all__ = [
    "NO_READABLE_AGENT_OUTPUT",
    "REQUEUE_DISPOSITION_JOINER",
    "RUNNER_VM_ID_PREFIX",
    "compose_agent_terminal_message",
    "compose_provision_failure_message",
    "compose_requeue_status_message",
    "is_local_runner_vm_id",
    "local_runner_label",
    "looks_like_provider_stream_fragment",
    "strip_provider_stream_noise",
]

# The one literal that separates the two execution substrates. Fleet runners
# are pinned as ``vm_instance_id = f"runner:{runner.id}"``
# (orchestrator_runner_dispatch.py); GCP agent VMs carry a real instance name
# (``agent-vm-<hex>``). There is no substrate column on Task and no shared
# constant elsewhere in the tree — ~20 sites inline this same prefix test.
RUNNER_VM_ID_PREFIX = "runner:"


def is_local_runner_vm_id(vm_instance_id: object) -> bool:
    """True iff *vm_instance_id* names a fleet runner rather than a cloud VM.

    Delegates to ``reaper_orphan_classify._is_multiplexed_vm_instance_id``,
    which documents itself as "the single definition" of the ``runner:``
    prefix fact and is already cross-module imported by ``routers/agent.py``.
    Re-deriving the prefix test here would create a second definition that
    could silently drift from the vmcontact writer/reader pair.

    Imported lazily: this module is imported by ``reaper.py`` and
    ``routers/agent_status.py``, both of which sit upstream of orphan
    classification, and a load-time edge here would be a cycle waiting to
    happen for zero benefit on a once-per-terminal-failure call.
    """
    from app.reaper_orphan_classify import _is_multiplexed_vm_instance_id

    return _is_multiplexed_vm_instance_id(vm_instance_id)


def local_runner_label(
    vm_instance_id: object, context: Mapping[str, Any] | None = None
) -> str:
    """Operator-readable name for the runner a task was pinned to.

    Prefers ``context['runner_hostname']`` (``hamsterwheel``/``dgx-1``/
    ``iconlab``) because that is what an operator actually SSHes into, and
    appends the short runner id so two runners on one host stay
    distinguishable. Both stamps are written in the same commit as the
    ``runner:`` pin (orchestrator_runner_dispatch.py), but a task pinned by an
    older code path may carry only one, so every combination degrades rather
    than raising.
    """
    ctx = context or {}
    runner_id = ""
    if isinstance(vm_instance_id, str) and vm_instance_id.startswith(
        RUNNER_VM_ID_PREFIX
    ):
        runner_id = vm_instance_id.removeprefix(RUNNER_VM_ID_PREFIX).strip()
    if not runner_id:
        runner_id = str(ctx.get("runner_target_id") or "").strip()
    hostname = str(ctx.get("runner_hostname") or "").strip()
    short_id = runner_id[:8]
    if hostname and short_id:
        return f"{hostname} ({short_id})"
    return hostname or short_id or "unidentified runner"


def compose_provision_failure_message(
    *,
    vm_instance_id: object,
    context: Mapping[str, Any] | None,
    vm_provisioned_at: datetime | None,
    provision_failures: int,
    account_id: str,
    provision_error: str | None,
    reap_reason: str,
) -> str:
    """Compose the terminal PROVISION_FAILURE message for either substrate.

    The substrate is decided BEFORE any zone/GCP field is read, because those
    fields are structurally absent on a local-runner failure and their
    ``"unknown zone"`` / ``"no GCP error captured"`` defaults read as real
    findings to an operator (tasks ``82eb352a``, ``4cd822c3``).

    Within each substrate ``vm_provisioned_at`` still discriminates "the
    execution slot existed but the agent never claimed it" from "the slot was
    never obtained" — it is stamped by BOTH the GCP provisioner and the runner
    pin (``orchestrator_runner_dispatch.py`` sets it in the same commit as the
    pin), so it is substrate-independent and must not be read as a substrate
    signal itself.
    """
    ctx: Mapping[str, Any] = context or {}
    blocked_accounts = list(
        {
            *(ctx.get("blacklisted_accounts") or []),
            *(ctx.get("provision_failed_accounts") or []),
        }
    )

    if is_local_runner_vm_id(vm_instance_id):
        runner = local_runner_label(vm_instance_id, ctx)
        if vm_provisioned_at:
            label = "Agent startup failed"
            detail = f"pinned to runner {runner} but the agent never claimed the task"
        else:
            label = "Runner dispatch failed"
            detail = f"no runner accepted the task (last target: {runner})"
        # The category the agent itself reported is the thing an operator
        # needs and the thing the old message buried in context — on the live
        # cases it was "walkthrough_harness_failed", nothing zone-shaped.
        raw_category = str(ctx.get("agent_failure_category_raw") or "").strip()
        if raw_category:
            detail += f"; agent reported {raw_category}"
        if blocked_accounts:
            detail += f" (blacklisted {len(blocked_accounts)} account(s))"
        return (
            f"{label} {provision_failures}x — "
            f"runner: {runner}, acct: {account_id}. "
            f"{detail}. "
            f"Last reap: {reap_reason}"
        )

    provision_zone = ctx.get("provision_zone", "unknown zone")
    gcp_error_short = (provision_error or "no GCP error captured")[:200]
    if vm_provisioned_at:
        label = "Agent startup failed"
        detail = f"VM created in {provision_zone} but agent never claimed task"
        if blocked_accounts:
            detail += f" (blacklisted {len(blocked_accounts)} account(s))"
    else:
        label = "Provision failed"
        detail = f"GCP error: {gcp_error_short}"
    return (
        f"{label} {provision_failures}x — "
        f"zone: {provision_zone}, acct: {account_id}. "
        f"{detail}. "
        f"Last reap: {reap_reason}"
    )


# Signatures of a raw provider NDJSON stream frame. Deliberately narrow: an
# ordinary log line that merely contains a brace must survive untouched.
_STREAM_EVENT_MARKERS: tuple[str, ...] = (
    '{"type":"stream_event"',
    '{"type": "stream_event"',
    '"content_block_delta"',
    '"content_block_start"',
    '"content_block_stop"',
)

# A tail slice of an NDJSON stream begins mid-token: a run of non-space
# characters, a closing quote, a colon, then a JSON value — e.g. the live
# task ``1745bab4`` message ``se_id":null,"uuid":"21ba040c-..."``. Anchored,
# and the character class excludes whitespace/braces/quotes so prose like
# ``Error: boom`` or ``Test "foo": failed`` can never match.
_MID_TOKEN_JSON_HEAD = re.compile(
    r'^[^\s{\[\]"]{0,64}"\s*:\s*(?:null|true|false|"|\{|\[|-?\d)'
)

NO_READABLE_AGENT_OUTPUT = (
    "agent produced no readable diagnostic output (provider stream frames only)"
)


def looks_like_provider_stream_fragment(text: str) -> bool:
    """True iff *text* reads as a raw provider stream frame, not a message.

    Used as a guard predicate by tests and callers that want to assert the
    defect from task ``1745bab4`` (a mid-token NDJSON tail persisted as the
    operator-facing status) can never be stored again.
    """
    if not text:
        return False
    if any(marker in text for marker in _STREAM_EVENT_MARKERS):
        return True
    return bool(_MID_TOKEN_JSON_HEAD.match(text.lstrip()))


def strip_provider_stream_noise(text: str) -> str:
    """Drop provider stream frames, keeping any prose that preceded them.

    Line-scoped, and lossy only where it must be:

    * a line that BEGINS mid-token is discarded whole — nothing before the
      cut point survived the tail slice, so there is no trustworthy prefix;
    * a line that starts as prose and then runs into a stream frame keeps the
      prose head and drops the frame;
    * everything else is untouched.
    """
    kept: list[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            kept.append(line)
            continue
        if _MID_TOKEN_JSON_HEAD.match(stripped):
            continue
        marker_at = min(
            (idx for idx in (line.find(m) for m in _STREAM_EVENT_MARKERS) if idx >= 0),
            default=-1,
        )
        if marker_at >= 0:
            head = line[:marker_at].strip()
            if head:
                kept.append(head)
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def compose_agent_terminal_message(
    logs: str,
    *,
    failure_category: str | None = None,
    max_chars: int = 8000,
) -> str:
    """Compose the operator-facing status message for an agent status report.

    Leads with the structured ``failure_category`` the agent reported (the
    same value persisted as ``context['agent_failure_category_raw']``) and
    keeps the free text behind it. Both halves matter: task ``075854e6``
    surfaced "No uncommitted changes found ..." for a ``rate_limited``
    failure, so the category must come first — but a bare ``rate_limited``
    with no detail is worse than one that says why, so the free text is kept,
    not discarded.

    Free text that is only a raw provider stream frame (task ``1745bab4``) is
    replaced with an explicit "no readable output" note rather than persisted
    verbatim or silently blanked to an empty message.
    """
    source = logs or ""
    free_text = strip_provider_stream_noise(source)
    if source.strip() and not free_text:
        free_text = NO_READABLE_AGENT_OUTPUT

    category = (failure_category or "").strip().lower()
    if not category:
        return free_text[-max_chars:]

    if not free_text.strip():
        # Category but no free text at all (a report whose ``logs`` were
        # empty). ``f"{category} — {free_text}"`` would emit a dangling em
        # dash promising a detail that does not exist.
        return category[:max_chars]

    if free_text.strip().lower() == category:
        # The agent's free text WAS the category; don't say it twice.
        return category

    prefix = f"{category} — "
    budget = max(0, max_chars - len(prefix))
    return f"{prefix}{free_text[-budget:]}"


# Separator between the engine's disposition clause and the agent's own
# diagnosis. A colon, deliberately, and NOT another em dash: the disposition
# clauses already contain one ("Rate Limit — requeued"), and a second one
# reads as a third independent field rather than "…because:".
REQUEUE_DISPOSITION_JOINER = ": "


def _disposition_already_states(disposition: str, diagnosis: str) -> bool:
    """True iff *diagnosis* adds nothing the disposition clause did not say.

    Guards the degenerate case where the agent's whole report was the bare
    category (``failure_category='rate_limited'`` with empty logs): appending
    ``rate_limited`` to ``Rate Limit — requeued`` says the same thing twice.
    Underscores are normalised because the two halves come from different
    vocabularies — the agent's raw snake_case label vs. the engine's
    title-cased failure class.
    """
    normalised = diagnosis.replace("_", " ").strip().lower()
    return bool(normalised) and normalised in disposition.replace("_", " ").lower()


def compose_requeue_status_message(
    logs: str,
    *,
    failure_category: str | None = None,
    disposition: str,
    max_chars: int = 8000,
) -> str:
    """Compose the operator-facing status for a NON-TERMINAL requeue/park.

    **Defect C — the inverse of Defect B.** ``compose_agent_terminal_message``
    was wired into the terminal write path so a FAILED task finally told the
    truth, but every non-terminal path in ``routers/agent_status.py``
    immediately overwrote ``latest_status_message`` with a synthesized state
    string::

        task.latest_status_message = f"{failure_class.title()} — requeued"

    ``Code Error — requeued`` is a *state*. The agent's explanation of what
    actually broke arrived in the SAME request (``payload.logs`` +
    ``payload.failure_category``), was composed into a truthful message a few
    hundred lines earlier, and was then discarded — so the operator had to ask
    the engine for something it already held. Only the banked
    ``attempt_history`` snapshot kept it, and only because banking happens
    before the overwrite.

    Both halves are real information and both are kept:

    * *disposition* — the engine's own clause, e.g. ``"Rate Limit — requeued"``
      or ``"Dead auth session — requeued onto different account"``. It LEADS,
      byte-for-byte as before, so an operator scanning the queue still reads
      "this was requeued, not abandoned" first and so the substring-keyed
      classifier fallbacks in ``failure_classification.py`` see exactly the
      text they saw before this fix.
    * the agent's diagnosis — composed by the shared
      ``compose_agent_terminal_message``, so the requeue path inherits the
      same category-led ordering, the same provider-stream-frame stripping
      (task ``1745bab4``) and the same "no readable output" note rather than
      re-deriving any of it at the call site.

    A requeue with no agent free text and no category degrades to the
    disposition alone — never a dangling ``": "``.
    """
    head = (disposition or "").strip()
    budget = max_chars - len(head) - len(REQUEUE_DISPOSITION_JOINER)
    if budget <= 0:
        return head[:max_chars]

    diagnosis = compose_agent_terminal_message(
        logs,
        failure_category=failure_category,
        max_chars=budget,
    ).strip()
    if not diagnosis:
        return head[:max_chars]
    if not head:
        return diagnosis[:max_chars]
    if _disposition_already_states(head, diagnosis):
        return head[:max_chars]
    return f"{head}{REQUEUE_DISPOSITION_JOINER}{diagnosis}"[:max_chars]
