import { describe, expect, test, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { KeyboardShortcutsOverlay } from './KeyboardShortcutsOverlay';

describe('KeyboardShortcutsOverlay', () => {
  test('renders grouped shortcut rows', () => {
    render(<KeyboardShortcutsOverlay open onClose={vi.fn()} />);

    expect(screen.getByRole('dialog', { name: 'Keyboard shortcuts' })).toBeInTheDocument();
    expect(screen.getByText('Navigate')).toBeInTheDocument();
    expect(screen.getByText('Operate')).toBeInTheDocument();
    expect(screen.getByText('Panels')).toBeInTheDocument();
    expect(screen.getByText('Previous section')).toBeInTheDocument();
    expect(screen.getByText('Command palette')).toBeInTheDocument();
    expect(screen.getByText('Close panel')).toBeInTheDocument();
  });

  test('closes from Escape, backdrop, and close button', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const { container, rerender } = render(<KeyboardShortcutsOverlay open onClose={onClose} />);

    await user.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalledTimes(1);

    rerender(<KeyboardShortcutsOverlay open onClose={onClose} />);
    await user.click(screen.getByRole('button', { name: 'Close keyboard shortcuts' }));
    expect(onClose).toHaveBeenCalledTimes(2);

    rerender(<KeyboardShortcutsOverlay open onClose={onClose} />);
    const backdrop = container.firstElementChild;
    if (!(backdrop instanceof HTMLElement)) throw new Error('Expected overlay backdrop');
    await user.click(backdrop);
    expect(onClose).toHaveBeenCalledTimes(3);
  });

  test('contains focus and restores focus after close', async () => {
    const user = userEvent.setup();
    const opener = document.createElement('button');
    opener.textContent = 'Open shortcuts';
    document.body.appendChild(opener);
    opener.focus();

    const { rerender, unmount } = render(<KeyboardShortcutsOverlay open onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Close keyboard shortcuts' })).toHaveFocus());

    await user.tab();
    expect(screen.getByRole('button', { name: 'Close keyboard shortcuts' })).toHaveFocus();

    await user.tab({ shift: true });
    expect(screen.getByRole('button', { name: 'Close keyboard shortcuts' })).toHaveFocus();

    rerender(<KeyboardShortcutsOverlay open={false} onClose={vi.fn()} />);
    await waitFor(() => expect(opener).toHaveFocus());

    unmount();
    opener.remove();
  });
});
