import { describe, it, expect } from 'vitest';
import type { Task } from '../types/task';
import { getSidecarBackref, deriveSidecarBadge, SIDECAR_BACKREF_KEYS } from './sidecarTasks';

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 'task-1',
    title: 'task',
    description: '',
    status: 'COMPLETED',
    created_by: 'test',
    requires_approval: false,
    created_at: '2026-06-01T00:00:00Z',
    ...overrides,
  } as Task;
}

describe('getSidecarBackref', () => {
  it('returns null when there is no context or no back-reference key', () => {
    expect(getSidecarBackref(makeTask())).toBeNull();
    expect(getSidecarBackref(makeTask({ context: { spawned_by: 'x' } }))).toBeNull();
  });

  it('recognizes every registered back-reference key', () => {
    for (const [key, kind] of Object.entries(SIDECAR_BACKREF_KEYS)) {
      const t = makeTask({ context: { [key]: 'primary-id' } });
      expect(getSidecarBackref(t)).toEqual({ kind, primaryTaskId: 'primary-id' });
    }
  });

  it('ignores non-string and empty back-reference values', () => {
    expect(getSidecarBackref(makeTask({ context: { review_second_opinion_for_task_id: 42 } }))).toBeNull();
    expect(getSidecarBackref(makeTask({ context: { quality_grader_for_task_id: '' } }))).toBeNull();
  });
});

describe('deriveSidecarBadge — second-opinion', () => {
  const sidecar = makeTask({
    id: 'challenger-1',
    status: 'COMPLETED',
    context: { review_second_opinion_for_task_id: 'review-1' },
  });

  it.each([
    ['challenged_confirmed', 'challenged ✓', 'positive'],
    ['challenged_revised', 'challenged ~', 'mixed'],
    ['challenged_rejected', 'challenged ✗', 'negative'],
  ])('maps reconciliation state %s', (state, text, tone) => {
    const primary = makeTask({ id: 'review-1', context: { review_second_opinion: { state } } });
    expect(deriveSidecarBadge(sidecar, primary)).toMatchObject({ kind: 'second-opinion', text, tone });
  });

  it('is pending while the challenger is in flight', () => {
    const inflight = makeTask({ ...sidecar, status: 'IN_PROGRESS' });
    const primary = makeTask({ id: 'review-1' });
    expect(deriveSidecarBadge(inflight, primary)).toMatchObject({ tone: 'pending' });
  });

  it('degrades to no-verdict when the challenger is terminal but nothing was recorded', () => {
    const primary = makeTask({ id: 'review-1' });
    expect(deriveSidecarBadge(sidecar, primary)).toMatchObject({ tone: 'mixed', text: 'challenge —' });
  });
});

describe('deriveSidecarBadge — grader', () => {
  const sidecar = makeTask({
    id: 'grader-1',
    status: 'COMPLETED',
    context: { quality_grader_for_task_id: 'source-1' },
  });

  it('maps a passed forward-quality grade', () => {
    const primary = makeTask({ id: 'source-1', context: { quality_grader: { passed: true, reason: 'ok' } } });
    expect(deriveSidecarBadge(sidecar, primary)).toMatchObject({ kind: 'grader', text: 'grade ✓', tone: 'positive' });
  });

  it('maps a failed forward-quality grade', () => {
    const primary = makeTask({ id: 'source-1', context: { quality_grader: { passed: false, reason: 'bad' } } });
    expect(deriveSidecarBadge(sidecar, primary)).toMatchObject({ text: 'grade ✗', tone: 'negative' });
  });

  it('uses the adversarial gate verdicts for adversarial graders', () => {
    const adversarial = makeTask({
      ...sidecar,
      context: { quality_grader_for_task_id: 'source-1', adversarial_grader: true },
    });
    const primary = makeTask({
      id: 'source-1',
      context: { adversarial_gate: { finding_verdicts: { a: true, b: false, c: true } } },
    });
    expect(deriveSidecarBadge(adversarial, primary)).toMatchObject({ text: 'gate 2/3', tone: 'mixed' });
  });

  it('is pending while the grader is in flight', () => {
    const inflight = makeTask({ ...sidecar, status: 'QUEUED' });
    expect(deriveSidecarBadge(inflight, makeTask({ id: 'source-1' }))).toMatchObject({ tone: 'pending' });
  });
});

describe('deriveSidecarBadge — safeguard-challenge', () => {
  const sidecar = makeTask({
    id: 'challenge-1',
    status: 'COMPLETED',
    context: { safeguard_challenge_for_task_id: 'safeguard-1' },
  });

  it.each([
    ['confirmed', 'negative'],
    ['rejected', 'positive'],
    ['downgraded', 'mixed'],
  ])('maps outcome %s', (outcome, tone) => {
    const primary = makeTask({ id: 'safeguard-1', context: { safeguard_challenge_verdict: { outcome } } });
    expect(deriveSidecarBadge(sidecar, primary)).toMatchObject({ kind: 'safeguard-challenge', tone });
  });
});

describe('deriveSidecarBadge — finding-challenge', () => {
  const sidecar = makeTask({
    id: 'challenge-1',
    status: 'COMPLETED',
    context: { finding_challenge_for_task_id: 'research-1' },
  });

  it.each([
    ['agrees', '2nd opinion ✓', 'positive'],
    ['disagrees', '2nd opinion ✗', 'negative'],
    ['indeterminate', '2nd opinion ~', 'mixed'],
  ])('maps agreement %s', (agreement, text, tone) => {
    const primary = makeTask({ id: 'research-1', context: { finding_challenge: { agreement } } });
    expect(deriveSidecarBadge(sidecar, primary)).toMatchObject({ kind: 'finding-challenge', text, tone });
  });
});

describe('deriveSidecarBadge — non-side-car', () => {
  it('returns null for a task without a back-reference', () => {
    expect(deriveSidecarBadge(makeTask(), makeTask({ id: 'p' }))).toBeNull();
  });
});
