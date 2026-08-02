import type { Task } from '../types/task';

/**
 * Side-car tasks (forward-quality/adversarial graders and per-finding
 * challenges) are spawned with
 * parent_task_id=null ON PURPOSE — they must not participate in the primary
 * task's rollup-child accounting (backend app/task_evaluators.py). The link to
 * the task they grade/challenge lives in context under a per-kind
 * back-reference key instead. This registry mirrors those backend keys so the
 * pipeline view can nest side-cars visually under their primary without
 * entangling them in real parent/child rollups.
 */
export type SidecarKind =
  | 'grader'
  | 'finding-challenge';

/** Back-reference context keys written ON THE SIDE-CAR (backend task_evaluators.py). */
export const SIDECAR_BACKREF_KEYS: Record<string, SidecarKind> = {
  // QUALITY_GRADER_SOURCE_KEY — forward-quality grade OR adversarial finding gate
  // (the adversarial side-car reuses the same back-reference key and is
  // distinguished by context.adversarial_grader on the side-car itself)
  quality_grader_for_task_id: 'grader',
  // FINDING_CHALLENGE_SOURCE_KEY — per-finding second opinion on a research verdict
  finding_challenge_for_task_id: 'finding-challenge',
};

export interface SidecarBackref {
  kind: SidecarKind;
  primaryTaskId: string;
}

/** Read the side-car back-reference from a task's context, if any. */
export function getSidecarBackref(task: Pick<Task, 'context'>): SidecarBackref | null {
  const ctx = task.context;
  if (!ctx) return null;
  for (const [key, kind] of Object.entries(SIDECAR_BACKREF_KEYS)) {
    const v = ctx[key];
    if (typeof v === 'string' && v) return { kind, primaryTaskId: v };
  }
  return null;
}

export type SidecarTone = 'pending' | 'positive' | 'negative' | 'mixed';

export interface SidecarBadge {
  kind: SidecarKind;
  text: string;
  tone: SidecarTone;
  title: string;
}

const TERMINAL_STATUSES = new Set(['COMPLETED', 'FAILED', 'CANCELLED']);

function badge(kind: SidecarKind, text: string, tone: SidecarTone, title: string): SidecarBadge {
  return { kind, text, tone, title };
}

function unresolved(kind: SidecarKind, label: string, sidecar: Task): SidecarBadge {
  if (TERMINAL_STATUSES.has(sidecar.status)) {
    return badge(
      kind,
      `${label} —`,
      'mixed',
      `The ${label} side-car finished (${sidecar.status}) but no verdict was recorded on the primary task`,
    );
  }
  return badge(
    kind,
    `${label} …`,
    'pending',
    `A ${label} side-car is in flight (${sidecar.status}); the primary verdict is parked until it resolves`,
  );
}

/**
 * Derive a compact presence/outcome badge for the PRIMARY task row from one of
 * its side-cars. Outcome records are written back onto the primary task's
 * context by the backend re-entrant completion path; while the side-car is
 * still in flight (or its verdict never landed) the badge degrades to a
 * pending/no-verdict state rather than guessing.
 */
export function deriveSidecarBadge(sidecar: Task, primary: Task): SidecarBadge | null {
  const backref = getSidecarBackref(sidecar);
  if (!backref) return null;
  const pctx = (primary.context ?? {}) as Record<string, unknown>;

  switch (backref.kind) {
    case 'grader': {
      const isAdversarial = sidecar.context?.adversarial_grader === true;
      if (isAdversarial) {
        // ADVERSARIAL_GATE_VERDICT_KEY: {finding_verdicts: {hash: bool}, ...}
        const gate = pctx['adversarial_gate'] as { finding_verdicts?: unknown } | undefined;
        const verdicts = gate?.finding_verdicts;
        if (verdicts && typeof verdicts === 'object') {
          const all = Object.values(verdicts as Record<string, unknown>);
          const resolveVerdict = (v: unknown): boolean =>
            v !== null && typeof v === 'object'
              ? (v as Record<string, unknown>)['passed'] === true
              : v === true;
          const passed = all.filter(resolveVerdict).length;
          const tone: SidecarTone = passed === all.length ? 'positive' : passed === 0 ? 'negative' : 'mixed';
          return badge(backref.kind, `gate ${passed}/${all.length}`, tone, `Adversarial gate: ${passed} of ${all.length} challenged findings survived`);
        }
        return unresolved(backref.kind, 'gate', sidecar);
      }
      // QUALITY_GRADER_VERDICT_KEY: {passed: bool, reason, grader_task_id}
      const verdict = pctx['quality_grader'] as { passed?: unknown; reason?: unknown } | undefined;
      if (typeof verdict?.passed === 'boolean') {
        const reason = typeof verdict.reason === 'string' ? `: ${verdict.reason}` : '';
        return verdict.passed
          ? badge(backref.kind, 'grade ✓', 'positive', `Forward-quality grade passed${reason}`)
          : badge(backref.kind, 'grade ✗', 'negative', `Forward-quality grade failed${reason}`);
      }
      return unresolved(backref.kind, 'grade', sidecar);
    }
    case 'finding-challenge': {
      // FINDING_CHALLENGE_VERDICT_KEY: FindingChallengeRecord {agreement, ...}
      const rec = pctx['finding_challenge'] as { agreement?: unknown } | undefined;
      const agreement = typeof rec?.agreement === 'string' ? rec.agreement : null;
      if (agreement === 'agrees') {
        return badge(backref.kind, '2nd opinion ✓', 'positive', 'Second opinion agrees with the original finding verdict');
      }
      if (agreement === 'disagrees') {
        return badge(backref.kind, '2nd opinion ✗', 'negative', 'Second opinion disagrees with the original finding verdict');
      }
      if (agreement === 'indeterminate') {
        return badge(backref.kind, '2nd opinion ~', 'mixed', 'Second opinion was indeterminate — the original verdict stands');
      }
      return unresolved(backref.kind, '2nd opinion', sidecar);
    }
  }
}

/** Tailwind classes per badge tone, matching the pipeline view's badge palette. */
export const SIDECAR_TONE_CLASSES: Record<SidecarTone, string> = {
  pending: 'text-neutral-400 bg-neutral-500/10 border-neutral-500/30',
  positive: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/30',
  negative: 'text-red-400 bg-red-500/10 border-red-500/30',
  mixed: 'text-amber-400 bg-amber-500/10 border-amber-500/30',
};
