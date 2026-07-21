"""Independent verification of compute experiment results.

Checkpoint 1 from the v18 Verified Delivery design: add an independent,
adversarial grader for compute experiment results so the system does not
blindly trust the experiment agent's self-declared success/failure.

The grader runs as a side-car ``investigate`` task via
:func:`task_evaluators.spawn_forward_quality_grader` with a compute-specific
rubric that checks for common failure modes:

* ``measurable=False`` reported as SUCCESS — the declared metric was never
  emitted but the outcome claims success.
* Score inflation — uncapped values, duplicate counts, implausible results.
* Infra constraint mistaken for a capability gap (e.g. "no numpy" reported as
  a finding rather than a missing dependency).

Advisory mode (the default) records the verdict and notifies but does NOT
change the task's gate outcome.  Hard-gate mode (future) will block SUCCESS
when the grader rejects the result.
"""

import json
import logging
import os
from typing import Optional

from app.shared_state import runtime_config as _runtime_config
from app.models import Task

logger = logging.getLogger(__name__)

# Context key written on the source compute-experiment task once the
# independent verification grader returns.  Value shape:
#   {"verdict": "confirmed"|"rejected"|"uncertain",
#    "gaps": [str],
#    "raw_output_inspected": bool,
#    "metric_found_in_raw": bool,
#    "mode": "advisory"|"soft_gate"|"hard_gate"}
COMPUTE_VERIFICATION_VERDICT_KEY = "compute_verification"

# Context key set while a verification grader is in flight (single-shot guard).
COMPUTE_VERIFICATION_AWAITING_KEY = "awaiting_compute_verification"

# Env-var escape hatch: set to "1" to disable compute verification entirely,
# bypassing the runtime_config check.
_DISABLE_COMPUTE_VERIFICATION_ENV = "DISABLE_COMPUTE_VERIFICATION"


def _is_verification_disabled_via_env() -> bool:
    """Return True when the env-var escape hatch is set."""
    return os.environ.get(_DISABLE_COMPUTE_VERIFICATION_ENV, "").strip() == "1"


def _compute_verification_enabled() -> bool:
    """Return True when the independent grader is enabled in runtime config.

    Checks the ``compute_verification_enabled`` runtime config key (default
    ``False`` — dark by default, flip per project).  The env-var escape hatch
    ``DISABLE_COMPUTE_VERIFICATION=1`` takes precedence regardless of config.
    """
    if _is_verification_disabled_via_env():
        return False
    return bool(_runtime_config.get("compute_verification_enabled", False))


def _has_compute_verification(task: Task) -> bool:
    """Single-shot guard — True when a verdict or in-flight grader already exists.

    Prevents double-spawning the verification grader on re-entrant completion
    calls (e.g. after the grader itself completes and re-dispatches the source).
    """
    ctx = task.context or {}
    return (
        COMPUTE_VERIFICATION_VERDICT_KEY in ctx
        or ctx.get(COMPUTE_VERIFICATION_AWAITING_KEY) is not None
    )


def _build_compute_verification_rubric(
    task: Task,
    criterion: Optional[dict],
    raw_logs: str,
) -> str:
    """Build a gap-first/adversarial rubric prompt for a compute experiment.

    The rubric is designed for use with
    :func:`task_evaluators.spawn_forward_quality_grader` (via its ``rubric=``
    parameter).  It includes:

    * The original experiment description / hypothesis (from the task payload).
    * The declared success criterion (metric + operator + threshold).
    * The raw terminal output.

    The grader is asked to check:

    1. **Measurable false + outcome SUCCESS** — the declared metric was never
       emitted but the outcome claims success.
    2. **Score inflation** — uncapped or implausible values.
    3. **Metric presence** — does the raw output actually contain the value?
    4. **Consistency** — does the metric value match the experiment design?

    Output format (single JSON line):
      ``{"verdict": "confirmed"|"rejected"|"uncertain", "gaps": [string],
       "raw_output_inspected": bool, "metric_found_in_raw": bool}``

    Parameters
    ----------
    task : Task
        The compute-experiment task whose result is being verified.
    criterion : dict or None
        The declared success criterion dict (or None if undeclared).
    raw_logs : str
        The raw terminal output of the compute experiment.
    """
    payload = task.first_payload or {}
    hypothesis = (
        payload.get("prompt")
        or payload.get("description")
        or payload.get("hypothesis")
        or "(no hypothesis available)"
    )

    criterion_block = ""
    if criterion is not None:
        criterion_block = (
            "Experiment's self-declared success criterion:\n"
            f"```json\n{json.dumps(criterion, indent=2)}\n```\n"
        )
    else:
        criterion_block = (
            "No explicit success criterion was declared by the experiment.\n"
        )

    # Truncate raw logs to a reasonable window for the rubric
    log_window = raw_logs[-30000:] if len(raw_logs) > 30000 else raw_logs

    return (
        "You are an INDEPENDENT, ADVERSARIAL verifier of compute experiment "
        "results.  The agent that designed and ran this experiment has already "
        "self-declared its outcome.  Your job is to be SKEPTICAL — actively "
        "look for reasons to REJECT or mark UNCERTAIN the self-reported result.  "
        "A clean pass is earned only when all four checks below are satisfied.\n\n"
        "Experiment hypothesis / description:\n"
        "-----8<----- HYPOTHESIS START -----8<-----\n"
        f"{hypothesis[:4000]}\n"
        "-----8<----- HYPOTHESIS END -----8<-----\n\n"
        f"{criterion_block}"
        "Your four checks:\n\n"
        "1. MEASURABLE-FALSE / OUTCOME-MISMATCH — Is the outcome SUCCESS "
        "or FAILURE even though no measurable metric value was ever emitted?  "
        "A SUCCESS with measurable=False is a red flag: the agent claims "
        "success on an unmeasured criterion.\n\n"
        "2. SCORE INFLATION — If a metric value IS present, is it realistic?  "
        "Watch for: values outside the plausible range for the declared metric; "
        "round-trip suspicious numbers (e.g. 0.9999); duplicate or constant "
        "values; counts that look made up.\n\n"
        "3. METRIC PRESENCE — Does the raw terminal output ACTUALLY contain "
        "the declared metric value at the expected key?  Parse the raw output "
        "below — don't take the criterion dict's word for it.\n\n"
        "4. CONSISTENCY — Does the claimed result make sense given the "
        "experiment description?  Would a reasonable person looking at the "
        "hypothesis and the raw output agree with the self-declared outcome?\n\n"
        "Raw terminal output (last 30k chars):\n"
        "-----8<----- LOGS START -----8<-----\n"
        f"{log_window}\n"
        "-----8<----- LOGS END -----8<-----\n\n"
        "End your response with EXACTLY ONE JSON object on its own line "
        "(no other text after it), matching the format:\n"
        '{"verdict": "pass"|"fail", "reasoning": "<one-sentence justification>"}\n\n'
        'Use "pass" when the self-reported result survives all four checks.  '
        'Use "fail" when any check reveals a meaningful gap — an unmeasured metric '
        'claimed as SUCCESS, an implausible score, a missing value in the raw output, '
        'or a result that contradicts the experiment description.'
    )


def _parse_verification_verdict(verdict_text: str) -> Optional[dict]:
    """Parse the grader's JSON verdict from its output text.

    Strips surrounding text and extracts the last JSON object that matches
    the expected shape (``{"verdict": "pass"|"fail", "reasoning": "..."}``).

    Returns the parsed dict, or ``None`` on parse failure.
    """
    try:
        # Find the last JSON object in the text
        lines = verdict_text.strip().split("\n")
        for line in reversed(lines):
            line = line.strip()
            if line.startswith("{"):
                parsed = json.loads(line)
                if (
                    isinstance(parsed, dict)
                    and parsed.get("verdict") in ("pass", "fail")
                ):
                    return parsed
        # Fallback: try parsing the entire text as JSON
        parsed = json.loads(verdict_text.strip())
        if (
            isinstance(parsed, dict)
            and parsed.get("verdict") in ("pass", "fail")
        ):
            return parsed
    except (json.JSONDecodeError, AttributeError):
        pass
    return None
