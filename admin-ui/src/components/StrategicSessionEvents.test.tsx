import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { StrategicSessionEvents } from './StrategicSessionEvents';
import type { StrategicWorkSessionEvent } from '../strategicStateApi';

const EVENTS: StrategicWorkSessionEvent[] = [
  {
    id: 'event-message',
    schema_version: 1,
    project_id: 'proj-1',
    resource: 'project_message',
    action: 'created',
    refetch: { surfaces: ['messages'] },
    occurred_at: '2026-06-30T18:55:00Z',
    source: 'strategic',
    title: 'Question message',
    summary: 'Strategic needs a runner choice.',
    tone: 'warning',
    target: { surface: 'messages', message_id: 'msg-1', action: 'reply' },
  },
  {
    id: 'event-task',
    schema_version: 1,
    project_id: 'proj-1',
    resource: 'task',
    action: 'in_progress',
    refetch: { surfaces: ['pipeline'] },
    occurred_at: '2026-06-30T18:00:00Z',
    source: 'agent',
    title: 'Task update',
    tone: 'active',
    target: { surface: 'task', task_id: 'task-1' },
  },
];

describe('StrategicSessionEvents', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-06-30T19:00:00Z'));
  });

  afterEach(() => {
    const r = root;
    if (r) act(() => { r.unmount(); });
    root = null;
    host?.remove();
    host = null;
    vi.useRealTimers();
  });

  function render(events: StrategicWorkSessionEvent[] = EVENTS, onOpenTarget = vi.fn()) {
    host = document.createElement('div');
    document.body.appendChild(host);
    const currentRoot = createRoot(host);
    root = currentRoot;
    act(() => {
      currentRoot.render(
        <StrategicSessionEvents
          events={events}
          limit={1}
          onOpenTarget={onOpenTarget}
        />,
      );
    });
    return { host, onOpenTarget };
  }

  test('renders bounded event rows with compact metadata', () => {
    const { host } = render();

    expect(host.textContent).toContain('Question message');
    expect(host.textContent).toContain('Strategic needs a runner choice.');
    expect(host.textContent).toContain('strategic / project message / 5m ago');
    expect(host.textContent).not.toContain('Task update');
  });

  test('opens event targets through the provided handler', () => {
    const onOpenTarget = vi.fn();
    const { host } = render(EVENTS, onOpenTarget);

    host.querySelector('button')?.click();

    expect(onOpenTarget).toHaveBeenCalledWith({ surface: 'messages', message_id: 'msg-1', action: 'reply' });
  });

  test('renders an empty label when requested', () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    const currentRoot = createRoot(host);
    root = currentRoot;
    act(() => {
      currentRoot.render(
        <StrategicSessionEvents
          events={[]}
          emptyLabel="No recent flow"
          onOpenTarget={vi.fn()}
        />,
      );
    });

    expect(host.textContent).toContain('No recent flow');
  });

  test('treats backend naive timestamps as UTC', () => {
    const { host } = render([
      {
        ...EVENTS[0],
        id: 'event-naive-time',
        occurred_at: '2026-06-30T18:55:00',
      },
    ]);

    expect(host.textContent).toContain('5m ago');
    expect(host.textContent).not.toContain('from now');
  });
});
