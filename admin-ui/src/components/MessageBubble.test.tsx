import { describe, test, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MessageBubble } from './MessageBubble';
import type { MessageBubbleProps, ProjectMessage } from './MessageBubble';

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
    render(<MessageBubble {...makeProps({ onStartResolve })} />);
    const resolveBtn = screen.getByTitle('Resolve');
    fireEvent.click(resolveBtn);
    expect(onStartResolve).toHaveBeenCalledWith('msg-1');
  });

  test('resolve action: confirm calls onResolve with current resolveInput', () => {
    const onResolve = vi.fn();
    render(<MessageBubble {...makeProps({ resolvingId: 'msg-1', resolveInput: 'fixed it', onResolve })} />);
    const confirmBtn = screen.getByTitle('Confirm resolve');
    fireEvent.click(confirmBtn);
    expect(onResolve).toHaveBeenCalledWith('msg-1', 'fixed it');
  });

  test('resolve action: cancel calls onCancelResolve', () => {
    const onCancelResolve = vi.fn();
    render(<MessageBubble {...makeProps({ resolvingId: 'msg-1', resolveInput: '', onCancelResolve })} />);
    // The cancel button is the X next to the resolve confirm
    const cancelBtns = screen.getAllByTitle('Cancel');
    fireEvent.click(cancelBtns[0]);
    expect(onCancelResolve).toHaveBeenCalled();
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

  test('data-testid message-bubble is set', () => {
    render(<MessageBubble {...makeProps()} />);
    expect(screen.getByTestId('message-bubble')).toBeTruthy();
  });

  test('copyable message ref displays first 8 characters of message id', () => {
    const msgId = 'abcdef0123456789abcdef';
    render(<MessageBubble {...makeProps({ msg: makeMsg({ id: msgId }) })} />);
    expect(screen.getByText('abcdef01')).toBeTruthy();
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
});
