// Single source of truth for the agent fix-loop "layers" (lint → test → check).
// Previously the ordered layer list and the layer→label abbreviation map were
// duplicated across FixLoopConvergenceWidget.tsx and TaskFixLoopPanel.tsx (and
// re-spelled inline as `['lint', 'test', 'check']`). Adding or renaming a layer
// in one place silently left the others stale; consolidating here keeps the
// order and labels in lockstep across every fix-loop view.

export const FIX_LOOP_LAYERS = ['lint', 'test', 'check'] as const;

export type FixLoopLayer = (typeof FIX_LOOP_LAYERS)[number];

export const LOOP_LABELS: Record<string, string> = {
  lint: 'Lint',
  test: 'Test',
  check: 'Check',
};
