"""Constants for strategic operation validation in Blueprint AI Studio."""

STRATEGIC_DIRECTION_TEXT_MAX = 2000
STRATEGIC_RATIONALE_MAX = 2000
STRATEGIC_SYSTEM_ID_MAX = 100
STRATEGIC_NOTES_HISTORY_MAX = 50
STRATEGIC_MESSAGE_CONTENT_MAX = 20000
STRATEGIC_RESOLUTION_MAX = 20000
STATUS_LATEST_MESSAGE_MAX = 2000
CYCLE_SUMMARY_MAX = 4000

# Soft floor of "healthy" active directions. Each active direction is one
# parallel work-lane the fleet can fan out across (research/plan dispatch is
# per-direction), so a low active-direction count is the effective ceiling on
# how much of the VM fleet can work at once. This is ADVISORY only — it is
# surfaced as a prompt hint to the strategic agent and as a non-blocking warning
# when a retire drops the count below it; it never blocks an operation.
MIN_HEALTHY_ACTIVE_DIRECTIONS = 3
