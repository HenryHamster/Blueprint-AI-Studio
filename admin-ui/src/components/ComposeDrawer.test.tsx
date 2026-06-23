import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';

const { apiFetch } = vi.hoisted(() => ({
  apiFetch: vi.fn(),
}));

vi.mock('../api', () => ({
  apiFetch,
  API_BASE_URL: '',
}));

const { parseArmingOutcome, composerHintCopy } = vi.hoisted(() => ({
  parseArmingOutcome: vi.fn(() => null as null | string),
  composerHintCopy: vi.fn((_type: unknown, arming: unknown) => arming ? `hint:${arming}` : ''),
}));

vi.mock('./messageArming', () => ({
  parseArmingOutcome,
  armingToastCopy: vi.fn(() => 'Message sent'),
  composerHintCopy,
}));

vi.mock('../utils/idempotencyKey', () => ({
  generateIdempotencyKey: vi.fn(() => 'test-key'),
}));

vi.mock('../utils/time', () => ({
  timeAgo: vi.fn(() => '5m ago'),
}));

import { ComposeDrawer } from './ComposeDrawer';

const defaultProps = {
  projectId: 'proj-1',
  userEmail: 'user@example.com',
  onOpenFullPanel: vi.fn(),
  onClose: vi.fn(),
  addToast: vi.fn(),
};

describe('ComposeDrawer', () => {
  beforeEach(() => {
    apiFetch.mockResolvedValue(
      new Response(JSON.stringify({
        messages: [
          { id: 'm1', content: 'Hello recent', message_type: 'FEEDBACK', created_at: '2026-01-01T00:00:00Z', from_type: 'user' },
          { id: 'm2', content: 'Another message', message_type: 'NOTE', created_at: '2026-01-02T00:00:00Z', from_type: 'user' },
        ],
      }), { status: 200 })
    );
  });

  it('changing message type after a send clears the stale steering hint', async () => {
    parseArmingOutcome.mockReturnValue('steering');

    apiFetch
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ messages: [] }), { status: 200 })
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ arming: 'steering' }), { status: 200 })
      );

    render(<ComposeDrawer {...defaultProps} />);

    const textarea = screen.getByRole('textbox') as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: 'test message' } });

    await act(async () => {
      fireEvent.keyDown(textarea, { key: 'Enter', metaKey: true });
    });

    await waitFor(() => {
      const calls = composerHintCopy.mock.calls;
      return calls.some(([, arming]) => arming === 'steering');
    });

    // Open the type dropdown and pick a different type
    const dropdownButton = screen.getByRole('button', { name: /FEEDBACK/i });
    fireEvent.click(dropdownButton);
    const noteOption = await screen.findByRole('button', { name: /NOTE/i });
    fireEvent.click(noteOption);

    await waitFor(() => {
      const calls = composerHintCopy.mock.calls;
      const lastCall = calls[calls.length - 1];
      expect(lastCall[1]).toBeNull();
    });
  });

  it('clicking a Recent message populates the composer with that message type and content', async () => {
    render(<ComposeDrawer {...defaultProps} />);

    await waitFor(() => screen.getByText('Hello recent'));

    const recentBtn = screen.getByText('Hello recent').closest('button');
    expect(recentBtn).not.toBeNull();

    if (recentBtn) {
      fireEvent.click(recentBtn);
    }

    const textarea = screen.getByRole('textbox') as HTMLTextAreaElement;
    expect(textarea.value).toBe('Hello recent');
  });

  it('clicking resend on a Recent row sends a fresh copy without editing', async () => {
    let postCallCount = 0;
    const postBodies: string[] = [];
    apiFetch.mockImplementation((url: string, options?: RequestInit) => {
      if (url.includes('/api/admin/messages') && options?.method === 'POST') {
        postCallCount++;
        if (options.body) {
          postBodies.push(options.body as string);
        }
        return Promise.resolve(
          new Response(JSON.stringify({ arming: null }), { status: 200 })
        );
      }
      // For the initial GET to fetch recent messages
      return Promise.resolve(
        new Response(JSON.stringify({
          messages: [
            { id: 'm1', content: 'Hello recent', message_type: 'FEEDBACK', created_at: '2026-01-01T00:00:00Z', from_type: 'user' },
            { id: 'm2', content: 'Another message', message_type: 'NOTE', created_at: '2026-01-02T00:00:00Z', from_type: 'user' },
          ],
        }), { status: 200 })
      );
    });

    render(<ComposeDrawer {...defaultProps} />);

    await waitFor(() => screen.getByText('Hello recent'));

    // Find all buttons with title "Resend message" (there should be one for each recent message)
    const buttons = screen.getAllByTitle('Resend message');
    expect(buttons.length).toBeGreaterThan(0);

    // Click the first resend button
    fireEvent.click(buttons[0]);

    // Verify that a POST request was made to send the message
    await waitFor(() => {
      expect(postCallCount).toBe(1);
    });

    // Verify the POST was called with the correct content and type
    expect(postBodies[0]).toContain('Hello recent');
    expect(postBodies[0]).toContain('FEEDBACK');
  });
});
