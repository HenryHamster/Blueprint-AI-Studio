import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { SaveViewDialog } from './SaveViewDialog';
import { expectNoOverflowAt } from '../test/viewport';

describe('SaveViewDialog — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  test('renders create mode with long summary text without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <SaveViewDialog
            open
            mode="create"
            name="Unresolved BLOCKERs from autonomous fix loop iterations across all directions"
            description="View pinned to investigate the recurring viewport regression that surfaces on narrow mobile widths during operator triage."
            error={null}
            summaryText="status=PENDING,IN_PROGRESS; type=fix,refactor; ordering=-updated_at; assignee=autonomous-agent-pool-1"
            onNameChange={() => { /* no-op */ }}
            onDescriptionChange={() => { /* no-op */ }}
            onCommit={() => { /* no-op */ }}
            onClose={() => { /* no-op */ }}
          />,
        );
      });
    });
  });

  test('renders rename mode with error message without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <SaveViewDialog
            open
            mode="rename"
            name="A duplicate view name that already exists on the project"
            description=""
            error="A view with this name already exists. Choose a different name or rename the existing view first to free up this slot."
            onNameChange={() => { /* no-op */ }}
            onDescriptionChange={() => { /* no-op */ }}
            onCommit={() => { /* no-op */ }}
            onClose={() => { /* no-op */ }}
          />,
        );
      });
    });
  });
});
