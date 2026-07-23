import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, test, vi } from 'vitest';
import { ToolResidentHandoffControls } from './ToolResidentHandoffControls';

describe('ToolResidentHandoffControls', () => {
  test('associates the disabled explanation with both handoff actions', () => {
    render(
      <ToolResidentHandoffControls
        toolLabel="Notes"
        disabledReason="Wait for the current save to finish."
        onHandoff={vi.fn()}
      />,
    );
    const reason = screen.getByText('Wait for the current save to finish.');
    for (const name of ['Share in conversation', 'Ask in conversation']) {
      const button = screen.getByRole('button', { name });
      expect(button.getAttribute('aria-describedby')).toBe(reason.id);
      expect(button).toBeDisabled();
    }
  });

  test('announces busy and draft-queue completion', async () => {
    const onHandoff = vi.fn().mockResolvedValue(undefined);
    render(<ToolResidentHandoffControls toolLabel="Whiteboard" onHandoff={onHandoff} />);
    fireEvent.click(screen.getByRole('button', { name: 'Ask in conversation' }));
    const confirmation = await screen.findByRole('status');
    expect(confirmation.textContent)
      .toBe('Whiteboard revision durably queued for your next conversation draft. Open Conversation, review, and send it to share.');
    expect(confirmation).not.toHaveClass('sr-only');
    expect(confirmation).toHaveClass('text-emerald-300');
    expect(confirmation.getAttribute('aria-live')).toBe('polite');
    expect(onHandoff).toHaveBeenCalledWith('ask');
  });

  test('coalesces a double click into one handoff operation', async () => {
    let resolveHandoff: (() => void) | undefined;
    const onHandoff = vi.fn(() => new Promise<void>(resolve => {
      resolveHandoff = resolve;
    }));
    render(<ToolResidentHandoffControls toolLabel="Whiteboard" onHandoff={onHandoff} />);
    const share = screen.getByRole('button', { name: 'Share in conversation' });
    fireEvent.click(share);
    fireEvent.click(share);
    expect(onHandoff).toHaveBeenCalledTimes(1);
    resolveHandoff?.();
    await waitFor(() => expect(onHandoff).toHaveBeenCalledTimes(1));
  });
});
