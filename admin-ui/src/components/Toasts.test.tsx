import { describe, test, expect, vi, afterEach } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { ToastContainer, useToasts } from './Toasts';

function ToastHarness({ persistent }: { persistent: boolean }) {
  const { toasts, addToast, dismissToast } = useToasts();
  return (
    <div>
      <button
        type="button"
        onClick={() => addToast('terminal receipt', 'error', persistent)}
      >
        add
      </button>
      <ToastContainer toasts={toasts} onDismiss={dismissToast} />
    </div>
  );
}

describe('Toasts', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
    if (root) {
      const mountedRoot = root;
      act(() => { mountedRoot.unmount(); });
      root = null;
    }
    if (host) { host.remove(); host = null; }
  });

  async function mountToastHarness(persistent: boolean): Promise<HTMLDivElement> {
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => {
      root?.render(<ToastHarness persistent={persistent} />);
    });
    return host;
  }

  function addButton(el: HTMLElement): HTMLButtonElement {
    const button = [...el.querySelectorAll('button')].find(b => b.textContent === 'add');
    if (!button) throw new Error('add button not found');
    return button as HTMLButtonElement;
  }

  test('persistent toast stays until dismissed and never auto-hides', async () => {
    vi.useFakeTimers();
    const el = await mountToastHarness(true);
    await act(async () => { addButton(el).click(); });

    expect(el.textContent).toContain('terminal receipt');

    // Even after well past the non-persistent timeout the receipt remains.
    await act(async () => { vi.advanceTimersByTime(4300); });
    expect(el.textContent).toContain('terminal receipt');

    // Dismissal still works: click removes the toast after the exit animation.
    const dismiss = el.querySelector('[aria-label="Dismiss"]');
    if (!dismiss) throw new Error('dismiss button not found');
    await act(async () => { (dismiss as HTMLButtonElement).click(); });
    await act(async () => { vi.advanceTimersByTime(300); });
    expect(el.textContent).not.toContain('terminal receipt');
  });

  test('non-persistent toast auto-dismisses after the timeout', async () => {
    vi.useFakeTimers();
    const el = await mountToastHarness(false);
    await act(async () => { addButton(el).click(); });
    expect(el.textContent).toContain('terminal receipt');

    await act(async () => { vi.advanceTimersByTime(4000); });
    await act(async () => { vi.advanceTimersByTime(300); });
    expect(el.textContent).not.toContain('terminal receipt');
  });
});
