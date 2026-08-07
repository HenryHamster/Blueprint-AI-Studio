# v29 draft: Campaigns (operator-approved 2026-08-06, opens when v28 closes)

**Goal in one sentence:** an operator declares one substantial objective, and the
engine plans it, bursts to fleet scale, and lands it as a coherent finished whole
within days — then scales back down.

Burst-scale and deep-work are not two themes: a campaign is deep work *executed
by* burst scale.

## Governing principle: generalize, don't bolt on

Operator constraint, stated verbatim in intent: *"there is a balance between
useful new systems and overbuilding — what can be made general/usable should
be."* Campaigns must be built by **generalizing existing machinery** —
directions, research lineages, rollups, integration branches, warm pools,
review/convergence — not by adding a parallel bespoke "campaign subsystem."
Every new primitive introduced for campaigns must be usable by ordinary
(non-campaign) work too, or it should not be built. Where a campaign need and an
existing mechanism conflict, prefer extending the mechanism over forking it.

## Components

1. **Campaign planning.** One objective → a coordinated task DAG on an
   integration branch: dependencies, interfaces, sequencing. Not independent
   drive-by PRs that happen to share a theme. (Generalizes: direction →
   research → create lineages and rollup integration branches.)
2. **Burst execution.** Fleet scales to ~30–50 concurrent for the campaign
   window (warm pools, shared package store, disk reservations — the v28
   hardware levers), and returns to baseline afterward. Burst, not sustained.
3. **Coherence over throughput.** A campaign completes when the *objective* is
   done and integrated — completion is reported against the objective, never as
   a task count.
4. **Quality and cost held.** Review/verification machinery unchanged under
   load; each campaign declares a cost envelope up front and stays inside it.

Standing hygiene folded in (not a theme): hard-task success rate — dud-direction
culling, retry-with-diagnosis — continues as ordinary engine work throughout.

## Exit tests (instances, not vibes)

- **(a) Internal campaign:** a named engine feature of ≥2k-line coherent scope,
  shipped end-to-end in ≤72h wall clock.
- **(b) External campaign:** the same machinery lands a campaign on a
  non-Blueprint repository.
- Both: inside the declared cost envelope; defect rate no worse than the
  small-PR baseline; zero mid-campaign operator interventions; fleet cost back
  at baseline after the window (no sustained creep).

## Non-goals

- Not a rewrite of dispatch/scheduling; extend what exists.
- Not sustained high concurrency — the envelope economics are burst-shaped.
- Not a new operator UI surface beyond declaring the objective + envelope and
  reading the completion report.
