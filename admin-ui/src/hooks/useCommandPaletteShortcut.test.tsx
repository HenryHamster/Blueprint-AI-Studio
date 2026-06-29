import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { useCommandPaletteShortcut } from './useCommandPaletteShortcut';

function Harness({ onOpen, enabled = true }: { onOpen: () => void; enabled?: boolean }) {
  useCommandPaletteShortcut(onOpen, enabled);
  return (
    <>
      <input aria-label="composer" />
      <textarea aria-label="notes" />
      <div aria-label="rich composer" contentEditable />
    </>
  );
}

describe('useCommandPaletteShortcut', () => {
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
    host?.remove();
    host = null;
  });

  function mount(onOpen = vi.fn(), enabled = true) {
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => {
      root!.render(<Harness onOpen={onOpen} enabled={enabled} />);
    });
    return onOpen;
  }

  test('opens on Ctrl+K and prevents browser default', () => {
    const onOpen = mount();
    const event = new KeyboardEvent('keydown', {
      key: 'k',
      ctrlKey: true,
      bubbles: true,
      cancelable: true,
    });

    document.dispatchEvent(event);

    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(event.defaultPrevented).toBe(true);
  });

  test('ignores command shortcuts from editable fields', () => {
    const onOpen = mount();
    const editableTargets = [
      host?.querySelector('input'),
      host?.querySelector('textarea'),
      host?.querySelector('[contenteditable]'),
    ];

    for (const target of editableTargets) {
      const event = new KeyboardEvent('keydown', {
        key: 'K',
        metaKey: true,
        bubbles: true,
        cancelable: true,
      });
      target?.dispatchEvent(event);
      expect(event.defaultPrevented).toBe(false);
    }

    expect(onOpen).not.toHaveBeenCalled();
  });

  test('does not capture Ctrl+K while disabled', () => {
    const onOpen = mount(vi.fn(), false);
    const event = new KeyboardEvent('keydown', {
      key: 'k',
      ctrlKey: true,
      bubbles: true,
      cancelable: true,
    });

    document.dispatchEvent(event);

    expect(onOpen).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);
  });

  test('ignores Ctrl+K that another handler already consumed', () => {
    const onOpen = mount();
    const event = new KeyboardEvent('keydown', {
      key: 'k',
      ctrlKey: true,
      bubbles: true,
      cancelable: true,
    });
    event.preventDefault();

    document.dispatchEvent(event);

    expect(onOpen).not.toHaveBeenCalled();
  });

  test('ignores unrelated keys', () => {
    const onOpen = mount(vi.fn(), false);

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'j', ctrlKey: true, bubbles: true }));

    expect(onOpen).not.toHaveBeenCalled();
  });
});
