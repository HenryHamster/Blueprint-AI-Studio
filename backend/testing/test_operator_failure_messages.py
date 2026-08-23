"""Guards for truthful operator-facing terminal-failure messages.

Two live defect classes, both instances of "surface reasons, not states" —
the system HELD the true reason and printed a different, misleading one:

* Defect A (tasks ``82eb352a``, ``4cd822c3``): every provision failure was
  formatted in GCP vocabulary — ``zone: unknown zone`` / ``GCP error: no GCP
  error captured`` — on a fleet that is three local bwrap runners. Both tasks
  actually died on ``hamsterwheel`` with ``vm_instance_id='runner:707de5fb-…'``
  and a real ``agent_failure_category_raw='walkthrough_harness_failed'``.
* Defect B (tasks ``1745bab4``, ``075854e6``): ``latest_status_message`` was
  the agent's last free-text line verbatim — a raw NDJSON provider-stream
  fragment in one case, a message flatly contradicting the recorded
  ``rate_limited`` category in the other.
* Defect C (the inverse of B): every non-terminal requeue/park branch threw
  the composed truth straight back away, overwriting the column with a bare
  ``f"{failure_class.title()} — requeued"``. The operator got a state and no
  reason for something the engine had received in the same request.

These are pure-function tests: no DB, no fixtures. The DB-backed halves live
in ``test_reaper.py`` (reaper terminal-persist path) and ``test_agent.py``
(POST /api/agent/status write path).
"""

from datetime import datetime, timezone

from app.operator_failure_messages import (
    NO_READABLE_AGENT_OUTPUT,
    compose_agent_terminal_message,
    compose_provision_failure_message,
    compose_requeue_status_message,
    is_local_runner_vm_id,
    local_runner_label,
    looks_like_provider_stream_fragment,
    strip_provider_stream_noise,
)

# The exact shape ``routers/agent_status.py`` persisted for task 1745bab4:
# an 8000-char tail slice through a provider NDJSON stream, so the stored
# text begins mid-token.
LIVE_1745BAB4_FRAGMENT = (
    'se_id":null,"uuid":"21ba040c-6d4a-4c1e-9e5f-2b7c0a91d3ef"} '
    '{"type":"stream_event","event":{"type":"content_block_delta",'
    '"delta":{"type":"text_delta","text":"…"}}}'
)

_PROVISIONED_AT = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Defect A — substrate vocabulary
# ---------------------------------------------------------------------------


def test_local_runner_vm_id_predicate_matches_the_pin_written_by_dispatch():
    """``orchestrator_runner_dispatch`` pins ``vm_instance_id='runner:<uuid>'``;
    the GCP provisioner writes ``agent-vm-<hex>``. Nothing else discriminates
    the substrate — there is no column and no enum for it."""
    assert is_local_runner_vm_id("runner:707de5fb-1c22-4f60-8a19-9d0be2f4a3c1")
    assert not is_local_runner_vm_id("agent-vm-9f2c1d4e7a6b5c8d9e0f1a2b3c4d5e6f")
    assert not is_local_runner_vm_id(None)
    assert not is_local_runner_vm_id(12345)


def test_local_runner_label_prefers_hostname_and_keeps_short_id():
    label = local_runner_label(
        "runner:707de5fb-1c22-4f60-8a19-9d0be2f4a3c1",
        {"runner_hostname": "hamsterwheel"},
    )
    assert label == "hamsterwheel (707de5fb)"


def test_local_runner_label_degrades_when_a_stamp_is_missing():
    """A task pinned by an older path may carry only one of the two stamps."""
    assert (
        local_runner_label("runner:707de5fb-1c22-4f60-8a19-9d0be2f4a3c1", {})
        == "707de5fb"
    )
    assert local_runner_label(None, {"runner_hostname": "dgx-1"}) == "dgx-1"
    assert local_runner_label(None, {}) == "unidentified runner"
    # The context stamp is the fallback source of the runner id.
    assert (
        local_runner_label(None, {"runner_target_id": "9c1de2ab-0000-0000-0000-0"})
        == "9c1de2ab"
    )


def test_provision_failure_message_on_local_runner_has_no_gcp_vocabulary():
    """Regression guard for tasks ``82eb352a`` and ``4cd822c3``.

    Both ran on ``hamsterwheel`` and were reported to the operator as
    ``Provision failed 3x — zone: unknown zone, acct: 406bb865-…. GCP error:
    no GCP error captured.`` The operator was sent to inspect a GCP zone that
    was never involved, while the recorded agent category sat unread in
    ``task.context``. A local-runner failure must name the runner and the real
    category, and must not contain a single GCP token.
    """
    msg = compose_provision_failure_message(
        vm_instance_id="runner:707de5fb-1c22-4f60-8a19-9d0be2f4a3c1",
        context={
            "runner_hostname": "hamsterwheel",
            "runner_target_id": "707de5fb-1c22-4f60-8a19-9d0be2f4a3c1",
            "agent_failure_category_raw": "walkthrough_harness_failed",
        },
        vm_provisioned_at=_PROVISIONED_AT,
        provision_failures=3,
        account_id="406bb865-2f11-4a8e-9c31-7b5d0e6a1f42",
        provision_error=None,
        reap_reason=(
            "Silent pre-claim death — 617s since VM provisioned, no heartbeat"
        ),
    )
    assert "GCP" not in msg, f"GCP vocabulary on a GCP-free substrate: {msg!r}"
    assert "zone" not in msg.lower(), f"no zone exists for a local runner: {msg!r}"
    assert "hamsterwheel" in msg, f"the runner must be named: {msg!r}"
    assert "707de5fb" in msg, f"the runner id must be named: {msg!r}"
    assert "walkthrough_harness_failed" in msg, (
        f"the recorded agent category is the real cause and must surface: {msg!r}"
    )
    assert "Last reap:" in msg, f"the reap reason must survive: {msg!r}"


def test_provision_failure_message_on_local_runner_never_dispatched():
    """No ``vm_provisioned_at`` on a runner-pinned task means no runner ever
    accepted it — that is a dispatch failure, not an agent-startup failure,
    and still carries zero GCP vocabulary."""
    msg = compose_provision_failure_message(
        vm_instance_id="runner:707de5fb-1c22-4f60-8a19-9d0be2f4a3c1",
        context={"runner_hostname": "iconlab"},
        vm_provisioned_at=None,
        provision_failures=2,
        account_id="no account",
        provision_error=None,
        reap_reason="stuck PROVISIONING",
    )
    assert "Runner dispatch failed" in msg, msg
    assert "iconlab" in msg, msg
    assert "GCP" not in msg and "zone" not in msg.lower(), msg


def test_provision_failure_message_keeps_gcp_wording_for_a_real_gcp_vm():
    """The fix is a branch, not a rename: a genuine GCP provisioning failure
    keeps the wording operators already triage against."""
    msg = compose_provision_failure_message(
        vm_instance_id="agent-vm-9f2c1d4e7a6b5c8d9e0f1a2b3c4d5e6f",
        context={"provision_zone": "us-central1-a"},
        vm_provisioned_at=None,
        provision_failures=3,
        account_id="acct-1",
        provision_error="ZONE_RESOURCE_POOL_EXHAUSTED",
        reap_reason="absolute_timeout",
    )
    assert msg == (
        "Provision failed 3x — zone: us-central1-a, acct: acct-1. "
        "GCP error: ZONE_RESOURCE_POOL_EXHAUSTED. Last reap: absolute_timeout"
    )


def test_provision_failure_message_gcp_vm_created_but_agent_never_claimed():
    """``vm_provisioned_at`` still discriminates WITHIN the GCP branch — it is
    stamped by both substrates, so it can never be the substrate signal."""
    msg = compose_provision_failure_message(
        vm_instance_id="agent-vm-9f2c1d4e7a6b5c8d9e0f1a2b3c4d5e6f",
        context={"blacklisted_accounts": ["a"], "provision_failed_accounts": ["b"]},
        vm_provisioned_at=_PROVISIONED_AT,
        provision_failures=3,
        account_id="acct-1",
        provision_error=None,
        reap_reason="Silent pre-claim death",
    )
    assert "Agent startup failed" in msg
    assert "VM created in unknown zone but agent never claimed task" in msg
    assert "blacklisted 2 account(s)" in msg
    assert "GCP error" not in msg


# ---------------------------------------------------------------------------
# Defect B — message vs. recorded category
# ---------------------------------------------------------------------------


def test_live_1745bab4_fragment_is_recognised_as_a_provider_stream_frame():
    assert looks_like_provider_stream_fragment(LIVE_1745BAB4_FRAGMENT)


def test_stream_fragment_detection_is_conservative_about_ordinary_prose():
    """A message that merely contains a brace, a quote, or a colon is a
    legitimate diagnostic and must survive untouched."""
    for benign in (
        "Error: build failed with exit code 1",
        'AssertionError: expected {"a": 1} to equal {"a": 2}',
        'No "foo" export is defined on the "bar" mock',
        "if (x) { return 1; }",
        "Traceback (most recent call last):",
        "",
    ):
        assert not looks_like_provider_stream_fragment(benign), benign
        if benign:
            assert strip_provider_stream_noise(benign) == benign, benign


def test_strip_keeps_prose_that_precedes_a_stream_frame_on_the_same_line():
    line = 'FAIL tests/x.spec.ts {"type":"stream_event","event":{}}'
    assert strip_provider_stream_noise(line) == "FAIL tests/x.spec.ts"


def test_strip_recovers_the_diagnostic_sitting_above_the_stream_frames():
    """The real error is usually just above the NDJSON the tail slice landed
    in — dropping the frames must expose it, not blank the message."""
    logs = (
        "ERROR harness: playwright walkthrough timed out after 900s\n"
        + LIVE_1745BAB4_FRAGMENT
        + "\n"
        + '{"type":"stream_event","event":{"type":"content_block_stop"}}'
    )
    assert (
        strip_provider_stream_noise(logs)
        == "ERROR harness: playwright walkthrough timed out after 900s"
    )


def test_terminal_message_never_persists_the_live_1745bab4_fragment():
    """Regression guard for task ``1745bab4``: the stored operator message was
    the raw NDJSON tail while the payload's own ``failure_category`` said
    ``blocked_precondition``. The category must lead, and the fragment must
    never be persisted."""
    msg = compose_agent_terminal_message(
        LIVE_1745BAB4_FRAGMENT, failure_category="blocked_precondition"
    )
    assert not looks_like_provider_stream_fragment(msg), (
        f"a raw provider stream frame reached the operator message: {msg!r}"
    )
    assert msg.startswith("blocked_precondition"), msg
    assert "stream_event" not in msg and "content_block_delta" not in msg, msg
    assert NO_READABLE_AGENT_OUTPUT in msg, (
        f"an unreadable payload must say so, not blank silently: {msg!r}"
    )


def test_terminal_message_leads_with_category_and_keeps_the_free_text():
    """Regression guard for task ``075854e6``: the stored message was "No
    uncommitted changes found — the branch is clean and matches the latest
    commit. There is no work to ship." while the recorded category was
    ``rate_limited``. Two mutually exclusive stories, and the wrong one was
    surfaced.

    The fix leads with the category — but a message that says only
    ``rate_limited`` is worse than one that says why, so the free text is
    kept as supporting detail, never discarded.
    """
    free_text = (
        "No uncommitted changes found — the branch is clean and matches the "
        "latest commit. There is no work to ship."
    )
    msg = compose_agent_terminal_message(free_text, failure_category="rate_limited")
    assert msg.startswith("rate_limited"), (
        f"the recorded category must lead the operator message: {msg!r}"
    )
    assert free_text in msg, (
        f"informative free text must survive, not be discarded: {msg!r}"
    )


def test_terminal_message_normalises_and_deduplicates_the_category():
    assert (
        compose_agent_terminal_message("Rate_Limited", failure_category="RATE_LIMITED")
        == "rate_limited"
    )


def test_terminal_message_without_a_category_still_strips_stream_noise():
    """A non-FAILED report (e.g. a no-artifact completion) carries no category,
    but must still never store a raw provider frame."""
    msg = compose_agent_terminal_message(
        "deliverable already present on base\n" + LIVE_1745BAB4_FRAGMENT
    )
    assert msg == "deliverable already present on base"


def test_terminal_message_respects_the_column_budget():
    msg = compose_agent_terminal_message(
        "x" * 20000, failure_category="code_error", max_chars=8000
    )
    assert len(msg) <= 8000, len(msg)
    assert msg.startswith("code_error — ")


# ---------------------------------------------------------------------------
# Defect C — the requeue paths overwrote the composed truth with a state
# ---------------------------------------------------------------------------


def test_requeue_message_keeps_the_agent_diagnosis_and_the_requeue_fact():
    """The whole defect in one assertion.

    ``routers/agent_status.py`` used to write ``f"{failure_class.title()} —
    requeued"`` over the composed message. "Code Error — requeued" is a state:
    it does not say WHAT broke, and the agent's own explanation had arrived in
    the same request. Both halves are real information and both must survive —
    the disposition so the operator knows the work was not abandoned, the
    diagnosis so they know why it had to be retried.
    """
    msg = compose_requeue_status_message(
        "pytest: 3 failures in tests/test_widget.py::test_alpha",
        failure_category="test_failed",
        disposition="Code Error — requeued",
    )
    assert msg == (
        "Code Error — requeued: test_failed — "
        "pytest: 3 failures in tests/test_widget.py::test_alpha"
    )


def test_requeue_message_with_no_agent_text_is_legible():
    """A requeue whose report carried no free text must degrade to the
    disposition alone — never a dangling separator promising a detail that
    does not exist."""
    msg = compose_requeue_status_message(
        "", failure_category=None, disposition="Code Error — requeued"
    )
    assert msg == "Code Error — requeued"
    assert not msg.endswith((":", "—", "- ", ": "))


def test_requeue_message_with_only_a_category_has_no_dangling_em_dash():
    """Empty logs but a reported category: the category is the diagnosis, and
    the terminal composer's ``f"{category} — {free_text}"`` shape would leave a
    trailing em dash pointing at nothing."""
    msg = compose_requeue_status_message(
        "   \n  ",
        failure_category="test_failed",
        disposition="Code Error — requeued",
    )
    assert msg == "Code Error — requeued: test_failed"


def test_terminal_message_with_a_category_and_no_free_text_has_no_dangling_dash():
    """Same guard one layer down, where the shape is actually produced."""
    assert compose_agent_terminal_message("", failure_category="rate_limited") == (
        "rate_limited"
    )


def test_requeue_message_strips_provider_stream_frames_too():
    """Proves the requeue path goes through the SHARED composer rather than
    re-deriving sanitization at the call site: the task ``1745bab4`` NDJSON
    fragment must be replaced by the explicit "no readable output" note here
    exactly as it is on the terminal path."""
    msg = compose_requeue_status_message(
        LIVE_1745BAB4_FRAGMENT,
        failure_category="blocked_precondition",
        disposition="Code Error — requeued",
    )
    assert not looks_like_provider_stream_fragment(msg), msg
    assert "stream_event" not in msg and "content_block_delta" not in msg, msg
    assert msg == (
        f"Code Error — requeued: blocked_precondition — {NO_READABLE_AGENT_OUTPUT}"
    )


def test_requeue_message_leads_the_diagnosis_with_the_contradicted_category():
    """The task ``075854e6`` shape, on the requeue path: the free text tells a
    story the recorded category contradicts, so the category must still lead
    the diagnosis half — the requeue disposition does not exempt it."""
    free_text = (
        "No uncommitted changes found — the branch is clean and matches the "
        "latest commit. There is no work to ship."
    )
    msg = compose_requeue_status_message(
        free_text,
        failure_category="rate_limited",
        disposition="Rate Limit — requeued",
    )
    assert msg == f"Rate Limit — requeued: rate_limited — {free_text}"


def test_requeue_message_does_not_state_the_same_thing_twice():
    """A report whose entire content was its own category adds nothing the
    disposition clause did not already say."""
    assert compose_requeue_status_message(
        "rate_limited",
        failure_category="rate_limited",
        disposition="Rate limited — requeued with exhausted account blacklisted",
    ) == "Rate limited — requeued with exhausted account blacklisted"


def test_requeue_message_carries_engine_evidence_when_there_is_no_agent():
    """The reaper's non-terminal requeues have no agent report at all — the
    diagnosis they hold and used to drop is ``reap_reason``."""
    assert compose_requeue_status_message(
        "No heartbeat for 620s (last: 2026-08-21T00:00:00+00:00)",
        disposition="Rate limited — requeued with exhausted account blacklisted",
    ) == (
        "Rate limited — requeued with exhausted account blacklisted: "
        "No heartbeat for 620s (last: 2026-08-21T00:00:00+00:00)"
    )


def test_requeue_message_respects_the_column_budget():
    """The disposition is charged against the same budget, so a huge log tail
    can never push the composed message past the column write size."""
    msg = compose_requeue_status_message(
        "x" * 20000,
        failure_category="code_error",
        disposition="Code Error — requeued",
        max_chars=8000,
    )
    assert len(msg) <= 8000, len(msg)
    assert msg.startswith("Code Error — requeued: code_error — ")
