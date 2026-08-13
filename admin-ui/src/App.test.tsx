import { describe, test, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import {
  resolveCompletionToastKind,
  resolveCompletionToastMessage,
  shouldSuppressCompletionToast,
} from './utils/completionToastKind';

const appSource = readFileSync(path.join(process.cwd(), 'src', 'App.tsx'), 'utf8');

describe('App completion toast', () => {
  test('strategic_failed_and_cancelled_outcomes_are_not_shown_as_success', () => {
    // FAILED outcomes must produce error semantics, never success
    expect(resolveCompletionToastKind('failed')).toBe('error');
    expect(resolveCompletionToastKind('FAILED')).toBe('error');
    expect(resolveCompletionToastKind('error')).toBe('error');
    expect(resolveCompletionToastKind('failure')).toBe('error');
    expect(resolveCompletionToastKind('aborted')).toBe('error');
    expect(resolveCompletionToastKind('timeout')).toBe('error');
    expect(resolveCompletionToastKind('timed_out')).toBe('error');

    // CANCELLED outcomes must produce neutral (info) semantics, never success
    expect(resolveCompletionToastKind('cancelled')).toBe('info');
    expect(resolveCompletionToastKind('canceled')).toBe('info');
    expect(resolveCompletionToastKind('CANCELLED')).toBe('info');

    // Neither failed nor cancelled may be presented as success
    for (const failStatus of ['failed', 'error', 'failure', 'aborted', 'timeout', 'timed_out']) {
      expect(resolveCompletionToastKind(failStatus)).not.toBe('success');
    }
    for (const cancelStatus of ['cancelled', 'canceled']) {
      expect(resolveCompletionToastKind(cancelStatus)).not.toBe('success');
    }

    // Only genuine success outcomes produce 'success'
    expect(resolveCompletionToastKind('completed')).toBe('success');
    expect(resolveCompletionToastKind('ok')).toBe('success');
    expect(resolveCompletionToastKind(undefined)).toBe('success');
    expect(resolveCompletionToastKind('')).toBe('success');

    // Toast messages are semantically distinct across outcomes
    const failMsg = resolveCompletionToastMessage('error');
    const cancelMsg = resolveCompletionToastMessage('info');
    const successMsg = resolveCompletionToastMessage('success');
    expect(failMsg).not.toBe(successMsg);
    expect(cancelMsg).not.toBe(successMsg);
    expect(failMsg).not.toBe(cancelMsg);
  });

  test('cycle completion toast in App is conditional on outcome status', () => {
    // The unconditional success toast must be gone
    expect(appSource).not.toContain("addToast('Strategic cycle complete', 'success')");
    // The App delegates to the shared classification helper
    expect(appSource).toContain('resolveCompletionToastKind');
    expect(appSource).toContain('resolveCompletionToastMessage');
    // The helper is imported from the canonical module
    expect(appSource).toContain("from './utils/completionToastKind'");
    // Failure/cancellation toasts retain the terminal reason (never a bare message)
    expect(appSource).toContain('cycleCompletion.failure_reason');
    // Failure/cancellation toasts persist until dismissed; success stays transient
    expect(appSource).toContain("addToast(message, toastKind, toastKind !== 'success')");
  });

  test('intentional resident operator stop stays durable but does not toast', () => {
    expect(shouldSuppressCompletionToast(
      'CANCELLED',
      'terminal_task_reconciliation source=resident_operator_stop',
    )).toBe(true);
    expect(shouldSuppressCompletionToast(
      'cancelled',
      undefined,
      '{"source":"resident_operator_stop","reason":"operator stop"}',
    )).toBe(true);
    expect(appSource).toContain('shouldSuppressCompletionToast(');
  });

  test('unexpected cancellations and every failure still toast', () => {
    expect(shouldSuppressCompletionToast('CANCELLED', 'Runner disappeared')).toBe(false);
    expect(shouldSuppressCompletionToast('FAILED', 'source=resident_operator_stop')).toBe(false);
    expect(shouldSuppressCompletionToast('FAILED', 'Execution site unavailable')).toBe(false);
  });
});
