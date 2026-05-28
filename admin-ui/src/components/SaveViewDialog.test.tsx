import { useState } from 'react';
import { describe, expect, test, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SaveViewDialog } from './SaveViewDialog';

describe('SaveViewDialog', () => {
  test('empty-name validation prevents commit', async () => {
    const user = userEvent.setup();
    const onCommit = vi.fn();
    function Harness() {
      const [name, setName] = useState('');
      const [error, setError] = useState<string | null>('Name is required');
      return (
        <SaveViewDialog
          open
          mode="create"
          name={name}
          description=""
          error={error}
          summaryText="No search · sort: importance"
          onNameChange={value => { setName(value); setError(null); }}
          onDescriptionChange={vi.fn()}
          onCommit={() => {
            if (!name.trim()) {
              setError('Name is required');
              return;
            }
            onCommit();
          }}
          onClose={vi.fn()}
        />
      );
    }
    render(<Harness />);

    await user.click(screen.getByRole('button', { name: 'Save view' }));
    expect(onCommit).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent('Name is required');
  });

  test('valid name triggers commit', async () => {
    const user = userEvent.setup();
    const onCommit = vi.fn();
    function Harness() {
      const [name, setName] = useState('Urgent');
      return (
        <SaveViewDialog
          open
          mode="create"
          name={name}
          description=""
          error={null}
          summaryText="No search · sort: importance"
          onNameChange={setName}
          onDescriptionChange={vi.fn()}
          onCommit={onCommit}
          onClose={vi.fn()}
        />
      );
    }
    render(<Harness />);

    await user.click(screen.getByRole('button', { name: 'Save view' }));
    expect(onCommit).toHaveBeenCalledTimes(1);
  });

  test('rename mode shows existing name pre-filled', () => {
    render(
      <SaveViewDialog
        open
        mode="rename"
        name="Existing view"
        description="Existing description"
        error={null}
        onNameChange={vi.fn()}
        onDescriptionChange={vi.fn()}
        onCommit={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByLabelText('Name')).toHaveValue('Existing view');
    expect(screen.getByLabelText('Description (optional)')).toHaveValue('Existing description');
    expect(screen.getByRole('button', { name: 'Save' })).toBeTruthy();
  });

  test('close button and backdrop close', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const { container } = render(
      <SaveViewDialog
        open
        mode="create"
        name="View"
        description=""
        error={null}
        summaryText="No search"
        onNameChange={vi.fn()}
        onDescriptionChange={vi.fn()}
        onCommit={vi.fn()}
        onClose={onClose}
      />,
    );

    await user.click(screen.getAllByRole('button')[0]);
    expect(onClose).toHaveBeenCalledTimes(1);
    onClose.mockClear();
    await user.click(container.firstElementChild as HTMLElement);
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
