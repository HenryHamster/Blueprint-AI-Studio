import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { ErrorBoundary } from './ErrorBoundary';
import {
  expectNoOverflowAt,
  expectNoTextTruncationAt,
  expectMinTouchTargetAt,
} from '../test/viewport';

// Suppress React error boundary console noise — we're deliberately crashing.
vi.spyOn(console, 'error').mockReturnValue(undefined);

function Thrower(): never {
  throw new Error('deliberate-crash-for-viewport-test');
}

describe('viewport assertions detect ErrorBoundary fallback', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => {
        r.unmount();
      });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  async function renderCrashedBoundary() {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <ErrorBoundary level="panel">
          <Thrower />
        </ErrorBoundary>,
      );
    });
  }

  test('expectNoOverflowAt rejects when wrapped child throws', async () => {
    let caughtError: Error | undefined;
    try {
      await expectNoOverflowAt(375, renderCrashedBoundary);
    } catch (e) {
      caughtError = e as Error;
    }
    expect(caughtError).toBeDefined();
    expect(caughtError!.message).toContain('[assertion_type:error_boundary]');
  });

  test('expectNoTextTruncationAt rejects when wrapped child throws', async () => {
    let caughtError: Error | undefined;
    try {
      await expectNoTextTruncationAt(375, renderCrashedBoundary);
    } catch (e) {
      caughtError = e as Error;
    }
    expect(caughtError).toBeDefined();
    expect(caughtError!.message).toContain('[assertion_type:error_boundary]');
  });

  test('expectMinTouchTargetAt rejects when wrapped child throws', async () => {
    let caughtError: Error | undefined;
    try {
      await expectMinTouchTargetAt(375, renderCrashedBoundary);
    } catch (e) {
      caughtError = e as Error;
    }
    expect(caughtError).toBeDefined();
    expect(caughtError!.message).toContain('[assertion_type:error_boundary]');
  });
});
