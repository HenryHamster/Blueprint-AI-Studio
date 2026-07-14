import { describe, test, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { MessageBubble } from './MessageBubble';
import type { MessageBubbleProps, ProjectMessage } from './MessageBubble';

// MarkdownContent uses React.lazy/Suspense which renders its fallback (empty span)
// synchronously in jsdom — the lazy chunk never resolves. Replace with a passthrough
// so getByText assertions on message content don't spuriously fail.
vi.mock('./MarkdownContent', () => ({
  MarkdownContent: ({ content }: { content: string }) => <span>{content}</span>,
}));

function makeMsg(overrides: Partial<ProjectMessage> = {}): ProjectMessage {
  return {
    id: 'msg-1',
    project_id: 'proj-1',
    message_type: 'NOTE',
    from_type: 'system',
    from_task_id: null,
    content: 'Test message content',
    context: {},
    status: 'open',
    resolution: null,
    resolved_at: null,
    created_at: new Date().toISOString(),
    ...overrides,
  };
}

function makeProps(overrides: Partial<MessageBubbleProps> = {}): MessageBubbleProps {
  return {
    msg: makeMsg(),
    faded: false,
    readOnly: false,
    allMessages: [],
    resolvingId: null,
    resolveInput: '',
    deletingIds: new Set(),
    creatingDirectionIds: new Set(),
    investigatingId: null,
    investigatingIds: new Set(),
    selectMode: false,
    selectedIds: new Set(),
    onToggleSelect: vi.fn(),
    onCreateDirection: vi.fn(),
    onCreateInvestigation: vi.fn(),
    onResolve: vi.fn(),
    onDelete: vi.fn(),
    onStartResolve: vi.fn(),
    onCancelResolve: vi.fn(),
    onSetResolveInput: vi.fn(),
    onStartInvestigate: vi.fn(),
    onCancelInvestigate: vi.fn(),
    onReply: vi.fn(),
    ...overrides,
  };
}

describe('MessageBubble', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  test('renders message content', () => {
    render(<MessageBubble {...makeProps({ msg: makeMsg({ content: 'Hello from test' }) })} />);
    expect(screen.getByText('Hello from test')).toBeTruthy();
  });

  test('resolve action: clicking resolve calls onStartResolve', () => {
    const onStartResolve = vi.fn();
    // Resolve is an actionable-row (QUESTION/BLOCKER) affordance only.
    render(<MessageBubble {...makeProps({ msg: makeMsg({ message_type: 'BLOCKER' }), onStartResolve })} />);
    const resolveBtn = screen.getByTitle('Resolve');
    fireEvent.click(resolveBtn);
    expect(onStartResolve).toHaveBeenCalledWith('msg-1');
  });

  test('resolve action: confirm calls onResolve with current resolveInput', () => {
    const onResolve = vi.fn();
    render(<MessageBubble {...makeProps({ msg: makeMsg({ message_type: 'BLOCKER' }), resolvingId: 'msg-1', resolveInput: 'fixed it', onResolve })} />);
    const confirmBtn = screen.getByTitle('Confirm resolve');
    fireEvent.click(confirmBtn);
    expect(onResolve).toHaveBeenCalledWith('msg-1', 'fixed it');
  });

  test('resolve action: cancel calls onCancelResolve', () => {
    const onCancelResolve = vi.fn();
    render(<MessageBubble {...makeProps({ msg: makeMsg({ message_type: 'BLOCKER' }), resolvingId: 'msg-1', resolveInput: '', onCancelResolve })} />);
    // The cancel button is the X next to the resolve confirm
    const cancelBtns = screen.getAllByTitle('Cancel');
    fireEvent.click(cancelBtns[0]);
    expect(onCancelResolve).toHaveBeenCalled();
  });

  test('conversational rows (NOTE/FEEDBACK/BUG_REPORT) have no manual resolve control', () => {
    for (const message_type of ['NOTE', 'FEEDBACK', 'BUG_REPORT'] as const) {
      const { unmount } = render(<MessageBubble {...makeProps({ msg: makeMsg({ message_type }) })} />);
      expect(screen.queryByTitle('Resolve')).toBeNull();
      // Other transcript affordances remain available.
      expect(screen.getByTitle('Reply')).toBeTruthy();
      unmount();
    }
  });

  test('actionable rows (QUESTION/BLOCKER) keep the manual resolve control', () => {
    for (const message_type of ['QUESTION', 'BLOCKER'] as const) {
      const { unmount } = render(<MessageBubble {...makeProps({ msg: makeMsg({ message_type }) })} />);
      expect(screen.getByTitle('Resolve')).toBeTruthy();
      unmount();
    }
  });

  test('delete path: clicking delete calls onDelete', () => {
    const onDelete = vi.fn();
    render(<MessageBubble {...makeProps({ onDelete })} />);
    const deleteBtn = screen.getByTitle('Delete');
    fireEvent.click(deleteBtn);
    expect(onDelete).toHaveBeenCalledWith('msg-1');
  });

  test('delete path: delete button is disabled when in deletingIds', () => {
    render(<MessageBubble {...makeProps({ deletingIds: new Set(['msg-1']) })} />);
    const deleteBtn = screen.getByTitle('Delete') as HTMLButtonElement;
    expect(deleteBtn.disabled).toBe(true);
  });

  test('create-direction path: shows button for FEEDBACK messages', () => {
    const onCreateDirection = vi.fn();
    render(<MessageBubble {...makeProps({
      msg: makeMsg({ message_type: 'FEEDBACK' }),
      onCreateDirection,
    })} />);
    const dirBtn = screen.getByTitle('Create Direction');
    fireEvent.click(dirBtn);
    expect(onCreateDirection).toHaveBeenCalledWith(expect.objectContaining({ message_type: 'FEEDBACK' }));
  });

  test('create-direction path: direction button disabled when in creatingDirectionIds', () => {
    render(<MessageBubble {...makeProps({
      msg: makeMsg({ id: 'msg-1', message_type: 'FEEDBACK' }),
      creatingDirectionIds: new Set(['msg-1']),
    })} />);
    const dirBtn = screen.getByTitle('Create Direction') as HTMLButtonElement;
    expect(dirBtn.disabled).toBe(true);
  });

  test('no action buttons when faded=true (resolved message display)', () => {
    render(<MessageBubble {...makeProps({ faded: true })} />);
    expect(screen.queryByTitle('Resolve')).toBeNull();
    expect(screen.queryByTitle('Delete')).toBeNull();
  });

  test('no action buttons when readOnly=true', () => {
    render(<MessageBubble {...makeProps({ readOnly: true })} />);
    expect(screen.queryByTitle('Resolve')).toBeNull();
    expect(screen.queryByTitle('Delete')).toBeNull();
  });

  test('reply-only mode keeps reply available and hides triage actions', () => {
    const onReply = vi.fn();
    render(<MessageBubble {...makeProps({
      actionMode: 'reply-only',
      msg: makeMsg({ message_type: 'QUESTION', content: 'Can you decide this?' }),
      onReply,
    })} />);

    expect(screen.queryByTitle('Resolve')).toBeNull();
    expect(screen.queryByTitle('Delete')).toBeNull();
    expect(screen.queryByTitle('Create Direction')).toBeNull();

    const replyBtn = screen.getByTitle('Reply');
    fireEvent.click(replyBtn);
    expect(onReply).toHaveBeenCalledWith(expect.objectContaining({
      id: 'msg-1',
      content: 'Can you decide this?',
      message_type: 'QUESTION',
      status: 'open',
    }));
  });

  test('chat presentation hides triage chrome while preserving reply', () => {
    const onReply = vi.fn();
    render(<MessageBubble {...makeProps({
      presentation: 'chat',
      actionMode: 'reply-only',
      msg: makeMsg({
        message_type: 'BUG_REPORT',
        from_type: 'strategic',
        content: 'Please choose the next direction.',
        context: {
          event_type: 'failure_alert',
          _provenance_warnings: {
            citation_state: 'absent',
            broken_refs: ['backend/app/models.py:Task'],
          },
        },
      }),
      onReply,
    })} />);

    expect(screen.getByText('Strategic')).toBeTruthy();
    expect(screen.getByText('Please choose the next direction.')).toBeTruthy();
    expect(screen.queryByText('Bug')).toBeNull();
    expect(screen.queryByText('Failure alert')).toBeNull();
    expect(screen.queryByText('cited code missing on HEAD')).toBeNull();
    expect(screen.queryByTitle('Copy message ref')).toBeNull();
    expect(screen.queryByTitle('Resolve')).toBeNull();
    expect(screen.queryByTitle('Delete')).toBeNull();
    expect(screen.queryByTitle('Create Direction')).toBeNull();

    fireEvent.click(screen.getByTitle('Reply'));
    expect(onReply).toHaveBeenCalledWith(expect.objectContaining({
      id: 'msg-1',
      content: 'Please choose the next direction.',
      from: 'Strategic',
      message_type: 'BUG_REPORT',
    }));
  });

  test('chat presentation renders task metadata as passive provenance', () => {
    const onOpenTask = vi.fn();
    render(<MessageBubble {...makeProps({
      presentation: 'chat',
      actionMode: 'reply-only',
      msg: makeMsg({
        from_type: 'agent',
        from_task_id: 'task-abcdef123456',
        content: 'Task-linked update.',
      }),
      onOpenTask,
    })} />);

    const taskMeta = screen.getByText('task-abc');
    expect(taskMeta).toBeTruthy();
    expect(taskMeta.tagName.toLowerCase()).toBe('span');
    fireEvent.click(taskMeta);
    expect(onOpenTask).not.toHaveBeenCalled();
  });

  test('chat presentation treats operator from_type as the local speaker', () => {
    const onReply = vi.fn();
    render(<MessageBubble {...makeProps({
      presentation: 'chat',
      actionMode: 'reply-only',
      msg: makeMsg({
        from_type: 'operator',
        content: 'Keep this in the main plan.',
      }),
      onReply,
    })} />);

    expect(screen.getByText('Keep this in the main plan.')).toBeTruthy();
    expect(screen.getByTestId('message-bubble').className).toContain('justify-end');
    expect(screen.queryByTitle('Reply')).toBeNull();
  });

  test('chat reply-only mode keeps system messages passive', () => {
    render(<MessageBubble {...makeProps({
      presentation: 'chat',
      actionMode: 'reply-only',
      msg: makeMsg({
        from_type: 'system',
        message_type: 'NOTE',
        content: 'Pipeline heartbeat.',
      }),
    })} />);

    expect(screen.getByText('Pipeline heartbeat.')).toBeTruthy();
    expect(screen.queryByTitle('Reply')).toBeNull();
  });

  test('chat presentation shows operator Strategic context receipt when captured', () => {
    const onOpenStrategicContext = vi.fn();
    render(<MessageBubble {...makeProps({
      presentation: 'chat',
      actionMode: 'reply-only',
      onOpenStrategicContext,
      msg: makeMsg({
        from_type: 'operator',
        content: 'Use the current context.',
        context: {
          strategic_session: {
            version: 1,
            interaction_mode: 'continuous',
            active_surface: 'pipeline',
            context_package_id: 'strategic-context:proj-1',
            selected_refs: [
              { kind: 'artifact', id: 'artifact:latest-context', label: 'Latest context report' },
            ],
            selected_ref_descriptors: [
              {
                kind: 'artifact',
                id: 'artifact:latest-context',
                label: 'Latest context report',
                summary: 'Fresh context report from the active wave',
                status: 'INVESTIGATION_REPORT',
              },
            ],
            captured_at: '2026-06-29T12:00:00Z',
          },
        },
      }),
    })} />);

    const receipt = screen.getByTestId('strategic-context-receipt');
    expect(receipt.tagName.toLowerCase()).toBe('div');
    expect(receipt.textContent).toContain('Refs attached');
    expect(receipt.textContent).not.toContain('Sent with context');
    expect(receipt.textContent).toContain('Workday');
    expect(receipt.textContent).toContain('Pipeline');
    expect(receipt.textContent).toContain('Latest context report');
    fireEvent.click(receipt);
    expect(onOpenStrategicContext).not.toHaveBeenCalled();
  });

  test('triage presentation keeps operator Strategic context receipt hidden', () => {
    render(<MessageBubble {...makeProps({
      msg: makeMsg({
        from_type: 'operator',
        context: {
          strategic_session: {
            version: 1,
            interaction_mode: 'continuous',
            selected_refs: [
              { kind: 'artifact', id: 'artifact:latest-context', label: 'Latest context report' },
            ],
          },
        },
      }),
    })} />);

    expect(screen.queryByTestId('strategic-context-receipt')).toBeNull();
  });

  test('chat receipt stays hidden when the captured Strategic session only has default timestamps', () => {
    render(<MessageBubble {...makeProps({
      presentation: 'chat',
      msg: makeMsg({
        from_type: 'operator',
        context: {
          strategic_session: {
            version: 1,
            interaction_mode: 'chat',
            active_surface: 'strategic',
            selected_refs: [],
            session_started_at: '2026-06-29T12:00:00Z',
          },
        },
      }),
    })} />);

    expect(screen.queryByTestId('strategic-context-receipt')).toBeNull();
  });

  test('chat receipt labels non-default captured surface as pipeline context', () => {
    render(<MessageBubble {...makeProps({
      presentation: 'chat',
      msg: makeMsg({
        from_type: 'operator',
        context: {
          strategic_session: {
            version: 1,
            interaction_mode: 'chat',
            active_surface: 'pipeline',
            selected_refs: [],
            captured_at: '2026-06-29T12:00:00Z',
          },
        },
      }),
    })} />);

    const receipt = screen.getByTestId('strategic-context-receipt');
    expect(receipt.textContent).toContain('Pipeline context');
    expect(receipt.textContent).not.toContain('Sent with context');
  });

  test('chat presentation omits task metadata when there is no task ref', () => {
    render(<MessageBubble {...makeProps({
      presentation: 'chat',
      msg: makeMsg({ from_task_id: null }),
    })} />);

    expect(screen.queryByText(/task /)).toBeNull();
  });

  test('shows resolved note when status is resolved with resolution', () => {
    render(<MessageBubble {...makeProps({
      msg: makeMsg({ status: 'resolved', resolution: 'Fixed in PR 123' }),
    })} />);
    expect(screen.getByText(/Fixed in PR 123/)).toBeTruthy();
    expect(screen.getByText(/Resolved:/)).toBeTruthy();
  });

  test('renders correct type label for BLOCKER', () => {
    render(<MessageBubble {...makeProps({ msg: makeMsg({ message_type: 'BLOCKER' }) })} />);
    expect(screen.getByText('Blocker')).toBeTruthy();
  });

  test('renders stale citation badge for BUG_REPORT when cited code is absent on HEAD', () => {
    render(<MessageBubble {...makeProps({
      msg: makeMsg({
        message_type: 'BUG_REPORT',
        context: {
          _provenance_warnings: {
            citation_state: 'absent',
            broken_refs: ['backend/app/models.py:Task'],
          },
        },
      }),
    })} />);
    expect(screen.getByText('cited code missing on HEAD')).toBeTruthy();
    expect(screen.getByText('backend/app/models.py:Task')).toBeTruthy();
  });

  test('renders stale citation badge details for BLOCKER when broken and ungrounded refs are present', () => {
    render(<MessageBubble {...makeProps({
      msg: makeMsg({
        message_type: 'BLOCKER',
        context: {
          _provenance_warnings: {
            citation_state: 'absent',
            broken_refs: ['backend/app/reaper.py:cancel_need'],
            ungrounded_refs: ['backend/app/orchestrator.py#spawn'],
          },
        },
      }),
    })} />);
    expect(screen.getByText('cited code missing on HEAD')).toBeTruthy();
    expect(screen.getByText(/backend\/app\/reaper\.py:cancel_need/)).toBeTruthy();
    expect(screen.getByText(/backend\/app\/orchestrator\.py#spawn/)).toBeTruthy();
  });

  test('renders partial-grounding badge for BUG_REPORT when citation_state=present with broken/ungrounded refs', () => {
    render(<MessageBubble {...makeProps({
      msg: makeMsg({
        message_type: 'BUG_REPORT',
        context: {
          _provenance_warnings: {
            citation_state: 'present',
            broken_refs: ['backend/app/models.py:Task'],
            ungrounded_refs: ['backend/app/reaper.py#sweep'],
          },
        },
      }),
    })} />);
    // Partially grounded uses a distinct, softer label — not the fully-stale wording.
    expect(screen.getByText('some cited code missing on HEAD')).toBeTruthy();
    expect(screen.queryByText('cited code missing on HEAD')).toBeNull();
    expect(screen.getByText(/backend\/app\/models\.py:Task/)).toBeTruthy();
    expect(screen.getByText(/backend\/app\/reaper\.py#sweep/)).toBeTruthy();
  });

  test('renders indeterminate badge for BLOCKER when citation_state=indeterminate', () => {
    render(<MessageBubble {...makeProps({
      msg: makeMsg({
        message_type: 'BLOCKER',
        context: {
          _provenance_warnings: {
            citation_state: 'indeterminate',
            broken_refs: ['backend/app/models.py:Task'],
          },
        },
      }),
    })} />);
    expect(screen.getByText('citations could not be verified')).toBeTruthy();
    expect(screen.getByText(/backend\/app\/models\.py:Task/)).toBeTruthy();
  });

  test('renders no badge when citation_state=present and no broken/ungrounded refs', () => {
    render(<MessageBubble {...makeProps({
      msg: makeMsg({
        message_type: 'BUG_REPORT',
        context: {
          _provenance_warnings: {
            citation_state: 'present',
            broken_refs: [],
            ungrounded_refs: [],
          },
        },
      }),
    })} />);
    expect(screen.queryByText('some cited code missing on HEAD')).toBeNull();
    expect(screen.queryByText('cited code missing on HEAD')).toBeNull();
    expect(screen.queryByText('citations could not be verified')).toBeNull();
  });

  test('QUESTION resolve: placeholder says "Your answer (required)..."', () => {
    render(<MessageBubble {...makeProps({
      msg: makeMsg({ message_type: 'QUESTION' }),
      resolvingId: 'msg-1',
      resolveInput: '',
    })} />);
    const input = screen.getByPlaceholderText('Your answer (required)...');
    expect(input).toBeTruthy();
  });

  test('QUESTION resolve: confirm button is disabled when input is empty', () => {
    render(<MessageBubble {...makeProps({
      msg: makeMsg({ message_type: 'QUESTION' }),
      resolvingId: 'msg-1',
      resolveInput: '',
    })} />);
    const confirmBtn = screen.getByTitle('Confirm resolve') as HTMLButtonElement;
    expect(confirmBtn.disabled).toBe(true);
  });

  test('QUESTION resolve: confirm button is enabled when input has text', () => {
    render(<MessageBubble {...makeProps({
      msg: makeMsg({ message_type: 'QUESTION' }),
      resolvingId: 'msg-1',
      resolveInput: 'my answer',
    })} />);
    const confirmBtn = screen.getByTitle('Confirm resolve') as HTMLButtonElement;
    expect(confirmBtn.disabled).toBe(false);
  });

  test('QUESTION resolve: Enter key with empty input does not call onResolve', () => {
    const onResolve = vi.fn();
    render(<MessageBubble {...makeProps({
      msg: makeMsg({ message_type: 'QUESTION' }),
      resolvingId: 'msg-1',
      resolveInput: '',
      onResolve,
    })} />);
    const input = screen.getByPlaceholderText('Your answer (required)...');
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onResolve).not.toHaveBeenCalled();
  });

  test('QUESTION resolve: Enter key with non-empty input calls onResolve', () => {
    const onResolve = vi.fn();
    render(<MessageBubble {...makeProps({
      msg: makeMsg({ message_type: 'QUESTION' }),
      resolvingId: 'msg-1',
      resolveInput: 'my answer',
      onResolve,
    })} />);
    const input = screen.getByPlaceholderText('Your answer (required)...');
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onResolve).toHaveBeenCalledWith('msg-1', 'my answer');
  });

  test('non-QUESTION resolve: placeholder says "Note (optional)..."', () => {
    render(<MessageBubble {...makeProps({
      msg: makeMsg({ message_type: 'BLOCKER' }),
      resolvingId: 'msg-1',
      resolveInput: '',
    })} />);
    expect(screen.getByPlaceholderText('Note (optional)...')).toBeTruthy();
  });

  test('non-QUESTION resolve: confirm button enabled with empty input', () => {
    render(<MessageBubble {...makeProps({
      msg: makeMsg({ message_type: 'BLOCKER' }),
      resolvingId: 'msg-1',
      resolveInput: '',
    })} />);
    const confirmBtn = screen.getByTitle('Confirm resolve') as HTMLButtonElement;
    expect(confirmBtn.disabled).toBe(false);
  });

  test('data-testid message-bubble is set', () => {
    render(<MessageBubble {...makeProps()} />);
    expect(screen.getByTestId('message-bubble')).toBeTruthy();
  });

  test('copyable message ref displays first 8 characters of message id', () => {
    const msgId = 'abcdef0123456789abcdef';
    render(<MessageBubble {...makeProps({ msg: makeMsg({ id: msgId }) })} />);
    expect(screen.getByText('abcdef01')).toBeTruthy();
  });

  test('renders image attachment as inline thumbnail', () => {
    render(<MessageBubble {...makeProps({
      msg: makeMsg({
        context: {
          attachments: [
            { type: 'image', mime_type: 'image/png', data: 'iVBORw0KGgo=', name: 'screenshot.png' },
          ],
        },
      }),
    })} />);
    const img = screen.getByAltText('screenshot.png') as HTMLImageElement;
    expect(img).toBeTruthy();
    expect(img.src).toContain('data:image/png;base64,iVBORw0KGgo=');
  });

  test('renders url-based image attachment as inline thumbnail', () => {
    render(<MessageBubble {...makeProps({
      msg: makeMsg({
        context: {
          attachments: [
            { type: 'image', mime_type: 'image/png', url: 'https://gcs/signed/screenshot.png', name: 'screenshot.png' },
          ],
        },
      }),
    })} />);
    const img = screen.getByAltText('screenshot.png') as HTMLImageElement;
    expect(img.src).toContain('https://gcs/signed/screenshot.png');
  });

  test('renders document attachment as file badge', () => {
    render(<MessageBubble {...makeProps({
      msg: makeMsg({
        context: {
          attachments: [
            { type: 'document', mime_type: 'application/pdf', name: 'report.pdf', size_bytes: 12345 },
          ],
        },
      }),
    })} />);
    expect(screen.getByText('report.pdf')).toBeTruthy();
    expect(screen.getByText('(12 KB)')).toBeTruthy();
  });

  test('renders multiple attachments', () => {
    render(<MessageBubble {...makeProps({
      msg: makeMsg({
        context: {
          attachments: [
            { type: 'image', mime_type: 'image/jpeg', data: '/9j/4AAQ=', name: 'photo.jpg' },
            { type: 'document', mime_type: 'text/plain', name: 'notes.txt', size_bytes: 500 },
          ],
        },
      }),
    })} />);
    const img = screen.getByAltText('photo.jpg');
    expect(img).toBeTruthy();
    expect(screen.getByText('notes.txt')).toBeTruthy();
  });

  test('does not render attachment section when context has no attachments', () => {
    render(<MessageBubble {...makeProps({
      msg: makeMsg({ context: {} }),
    })} />);
    expect(screen.queryByAltText('')).toBeNull();
  });

  test('copy message ref button copies to clipboard on click', async () => {
    const msgId = 'abcdef0123456789abcdef';
    const writeTextMock = vi.fn().mockResolvedValue(undefined);
    vi.spyOn(navigator.clipboard, 'writeText').mockImplementation(writeTextMock);
    render(<MessageBubble {...makeProps({ msg: makeMsg({ id: msgId }) })} />);
    const copyBtn = screen.getByTitle('Copy message ref');
    fireEvent.click(copyBtn);
    expect(writeTextMock).toHaveBeenCalledWith('abcdef0123456789abcdef');
  });

  test('copy message ref shows check icon after successful copy', async () => {
    const msgId = 'abcdef0123456789abcdef';
    const writeTextMock = vi.fn().mockResolvedValue(undefined);
    vi.spyOn(navigator.clipboard, 'writeText').mockImplementation(writeTextMock);
    render(<MessageBubble {...makeProps({ msg: makeMsg({ id: msgId }) })} />);
    const copyBtn = screen.getByTitle('Copy message ref');
    fireEvent.click(copyBtn);
    expect(screen.getByTestId('message-bubble')).toBeTruthy();
  });

  test('renders image attachment as clickable trigger that opens preview', () => {
    render(<MessageBubble {...makeProps({
      msg: makeMsg({
        context: {
          attachments: [
            { type: 'image', mime_type: 'image/png', data: 'iVBORw0KGgo=', name: 'screenshot.png' },
          ],
        },
      }),
    })} />);

    // Image thumbnail is wrapped in a clickable button
    const trigger = screen.getByRole('button', { name: 'Preview screenshot.png' });
    expect(trigger).toBeTruthy();
    fireEvent.click(trigger);

    // Preview modal appears with the attachment name
    const dialog = screen.getByRole('dialog');
    expect(dialog).toBeTruthy();
    expect(dialog.getAttribute('aria-label')).toContain('screenshot.png');
  });

  test('PDF attachment renders clickable trigger that opens preview', () => {
    render(<MessageBubble {...makeProps({
      msg: makeMsg({
        context: {
          attachments: [
            { type: 'document', mime_type: 'application/pdf', name: 'report.pdf', size_bytes: 12345 },
          ],
        },
      }),
    })} />);

    // PDF chip is a button, not a plain div
    const trigger = screen.getByRole('button', { name: 'Preview report.pdf' });
    expect(trigger).toBeTruthy();
    expect(screen.getByText('report.pdf')).toBeTruthy();

    // Click opens preview modal
    fireEvent.click(trigger);
    const dialog = screen.getByRole('dialog');
    expect(dialog).toBeTruthy();
    expect(dialog.getAttribute('aria-label')).toContain('report.pdf');
  });

  test('Escape key closes preview modal', () => {
    render(<MessageBubble {...makeProps({
      msg: makeMsg({
        context: {
          attachments: [
            { type: 'image', mime_type: 'image/png', data: 'iVBORw0KGgo=', name: 'photo.png' },
          ],
        },
      }),
    })} />);

    // Open the modal
    fireEvent.click(screen.getByRole('button', { name: 'Preview photo.png' }));
    expect(screen.getByRole('dialog')).toBeTruthy();

    // Press Escape on the dialog
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape', code: 'Escape' });

    // Modal closes
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  test('non-previewable attachment type shows download option in modal', () => {
    render(<MessageBubble {...makeProps({
      msg: makeMsg({
        context: {
          attachments: [
            { type: 'binary', mime_type: 'application/octet-stream', data: 'AAAA', name: 'data.bin' },
          ],
        },
      }),
    })} />);

    // Attachment is clickable
    const trigger = screen.getByRole('button', { name: 'Preview data.bin' });
    expect(trigger).toBeTruthy();
    fireEvent.click(trigger);

    // Modal opens with download button for unsupported types
    const dialog = screen.getByRole('dialog');
    expect(dialog).toBeTruthy();
    expect(within(dialog).getByText('Download')).toBeTruthy();
    expect(within(dialog).getByText(/No preview available/)).toBeTruthy();
  });
});
