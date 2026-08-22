import { afterEach, describe, expect, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import {
  buildConversationWorkdayStatus,
  type ConversationWorkdayStatus,
} from '../utils/conversationWorkdayStatus';
import type { StrategicChatLiveState } from '../utils/strategicSessionLive';
import { ConversationUtilityBar } from './ConversationUtilityBar';
import { expectNoOverflowAt } from '../test/viewport';

describe('ConversationUtilityBar — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => { r.unmount(); });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  async function renderBar(props: {
    expanded: boolean;
    searchQuery?: string;
    workdayStatus?: ConversationWorkdayStatus;
    chatLiveState?: StrategicChatLiveState;
  }) {
    host = document.createElement('div');
    host.style.width = '100%';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <ConversationUtilityBar
          expanded={props.expanded}
          onExpand={() => {}}
          onCollapse={() => {}}
          searchQuery={props.searchQuery ?? ''}
          onSearchChange={() => {}}
          workdayStatus={props.workdayStatus}
          chatLiveState={props.chatLiveState}
        />,
      );
    });
  }

  test('collapsed search control does not overflow at 375px', async () => {
    await expectNoOverflowAt(375, () => renderBar({ expanded: false }));
  });

  test('expanded search input does not overflow at 375px', async () => {
    await expectNoOverflowAt(375, () =>
      renderBar({ expanded: true, searchQuery: 'deployment rollback investigation notes and more text' }),
    );
  });

  test('Workday status and bounded task detail do not overflow at 375px', async () => {
    const workdayStatus = buildConversationWorkdayStatus({
      interactionMode: 'continuous',
      residentActivity: {
        phase: 'idle',
        durablePhase: 'waiting',
        label: 'Waiting',
        detail: 'Workday is watching for project movement',
      },
      taskFocus: {
        version: 1,
        bucket_limit: 8,
        has_more: false,
        buckets: {
          friction: {
            total: 1,
            limit: 8,
            has_more: false,
            items: [{
              id: 'd34da434-ec40-497c-8280-5f45a4a59612',
              bucket: 'friction',
              status: 'BLOCKED',
              title: 'A very long viewport capture task that must remain bounded on narrow phones',
              blocked_reason: 'Chromium bootstrap unavailable during the after-state capture',
              requires_approval: false,
              reasons: ['blocked'],
            }],
          },
          active: { total: 0, limit: 8, has_more: false, items: [] },
        },
      },
    });
    await expectNoOverflowAt(375, () => renderBar({ expanded: false, workdayStatus }));
  });

  test('expanded search plus Workday status does not overflow at 375px', async () => {
    const workdayStatus = buildConversationWorkdayStatus({
      interactionMode: 'chat',
      residentActivity: null,
    });
    await expectNoOverflowAt(375, () => renderBar({
      expanded: true,
      searchQuery: 'direction audit',
      workdayStatus,
    }));
  });

  test('requested and paused Workday remains truthful without hover at 375px', async () => {
    const workdayStatus = buildConversationWorkdayStatus({
      interactionMode: 'chat',
      residentActivity: {
        phase: 'idle',
        durablePhase: 'waiting',
        label: 'Waiting',
        reasonCode: 'consultation_queued',
        consultationId: '11111111-1111-4111-8111-111111111111',
        consultationVerified: true,
        autonomyState: 'paused',
        autonomyReasonCode: 'authority_not_held',
      },
      conversationMessages: [{
        id: '11111111-1111-4111-8111-111111111111',
        project_id: 'project-1',
        message_type: 'NOTE',
        from_type: 'strategic',
        from_task_id: 'resident-task',
        content: 'I am checking with Workday.',
        context: {
          message_role: 'operator_reply',
          _workday_consultation_requested: true,
        },
        status: 'handled',
        resolution: null,
        resolved_at: null,
        created_at: '2026-08-15T00:00:00Z',
        sequence: 1,
        surface: 'conversation',
      }],
    });
    await expectNoOverflowAt(375, async () => {
      await renderBar({ expanded: false, workdayStatus });
      const status = host?.querySelector('[data-testid="conversation-workday-status"]');
      if (!(status instanceof HTMLElement)) throw new Error('Workday status missing');
      const mobileText = [...status.querySelectorAll('.sm\\:hidden')]
        .map(node => node.textContent)
        .join(' ');
      if (!mobileText.includes('requested · paused')) {
        throw new Error(`Pause state is hover-only: ${mobileText}`);
      }
      if (!mobileText.includes('1 open · no authority')) {
        throw new Error(`Request count/reason is not visible: ${mobileText}`);
      }
      if (status.clientHeight > 32) throw new Error(`Workday status wrapped: ${status.clientHeight}px`);
    });
  });

  test('collapsed search control does not overflow at 1280px', async () => {
    await expectNoOverflowAt(1280, () => renderBar({ expanded: false }));
  });

  test('live Chat awareness remains visible and contained at 375px', async () => {
    await expectNoOverflowAt(375, () => renderBar({
      expanded: false,
      workdayStatus: buildConversationWorkdayStatus({ interactionMode: 'chat', residentActivity: null }),
      chatLiveState: { phase: 'streaming', label: 'Replying', detail: 'grep release gate' },
    }));
    const line = host?.querySelector('[data-testid="conversation-workday-status"]');
    expect(line?.textContent).toContain('Replying');
    expect(line?.className).not.toContain('hidden');
  });
});
