import { describe, test, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { EventVisibilityMenu } from './EventVisibilityMenu';
import { type ProjectMessage } from './MessageBubble';
import { type EventPref } from './eventPrefs';

function msg(id: string, eventType: string): ProjectMessage {
  return {
    id,
    project_id: 'p1',
    message_type: 'NOTE',
    from_type: 'agent',
    from_task_id: null,
    content: 'c',
    context: { event_type: eventType },
    status: 'open',
    resolution: null,
    resolved_at: null,
    created_at: new Date().toISOString(),
  };
}

describe('EventVisibilityMenu', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  test('gear toggles open via onOpenChange', () => {
    const onOpenChange = vi.fn();
    render(
      <EventVisibilityMenu open={false} onOpenChange={onOpenChange} messages={[]} eventPrefs={{}}
        onUpdatePref={vi.fn()} onResetAll={vi.fn()} />,
    );
    fireEvent.click(screen.getByTitle('Event type visibility'));
    expect(onOpenChange).toHaveBeenCalledWith(true);
  });

  test('open + no events shows empty hint', () => {
    render(
      <EventVisibilityMenu open={true} onOpenChange={vi.fn()} messages={[]} eventPrefs={{}}
        onUpdatePref={vi.fn()} onResetAll={vi.fn()} />,
    );
    expect(screen.getByText('No events loaded')).toBeInTheDocument();
  });

  test('ranks event types by 24h volume (highest first) with counts', () => {
    const messages = [msg('1', 'alpha'), msg('2', 'beta'), msg('3', 'beta')];
    render(
      <EventVisibilityMenu open={true} onOpenChange={vi.fn()} messages={messages} eventPrefs={{}}
        onUpdatePref={vi.fn()} onResetAll={vi.fn()} />,
    );
    // beta (count 2) should render before alpha (count 1)
    const labels = [...document.querySelectorAll('span.truncate')].map(s => s.textContent);
    expect(labels).toEqual(['beta', 'alpha']);
    expect(screen.getByText('2')).toBeInTheDocument();
    expect(screen.getByText('1')).toBeInTheDocument();
  });

  test('clicking a preference button forwards (eventType, pref)', () => {
    const onUpdatePref = vi.fn();
    render(
      <EventVisibilityMenu open={true} onOpenChange={vi.fn()} messages={[msg('1', 'alpha')]} eventPrefs={{}}
        onUpdatePref={onUpdatePref} onResetAll={vi.fn()} />,
    );
    // Each row has surfaced/routine/mute buttons; mute is the '✕' glyph
    fireEvent.click(screen.getByTitle('mute'));
    expect(onUpdatePref).toHaveBeenCalledWith('alpha', 'mute');
  });

  test('Reset all shown only when prefs exist and forwards onResetAll', async () => {
    const onResetAll = vi.fn();
    const prefs: Record<string, EventPref> = { alpha: 'mute' };
    const { rerender } = render(
      <EventVisibilityMenu open={true} onOpenChange={vi.fn()} messages={[]} eventPrefs={{}}
        onUpdatePref={vi.fn()} onResetAll={onResetAll} />,
    );
    expect(screen.queryByText('Reset all')).not.toBeInTheDocument();

    rerender(
      <EventVisibilityMenu open={true} onOpenChange={vi.fn()} messages={[]} eventPrefs={prefs}
        onUpdatePref={vi.fn()} onResetAll={onResetAll} />,
    );
    await waitFor(() => expect(screen.getByText('Reset all')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Reset all'));
    expect(onResetAll).toHaveBeenCalledTimes(1);
  });
});
