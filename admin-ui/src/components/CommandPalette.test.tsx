import { describe, expect, test, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { CommandPalette, type WorkbenchCommand } from './CommandPalette';

function makeCommands(overrides: Partial<WorkbenchCommand>[] = []): WorkbenchCommand[] {
  const base: WorkbenchCommand[] = [
    {
      id: 'open-task',
      label: 'Open task drawer',
      section: 'Navigation',
      detail: 'Jump to the selected task',
      keywords: ['drawer', 'task'],
      shortcut: 'O',
      run: vi.fn(),
    },
    {
      id: 'sync-fleet',
      label: 'Sync fleet',
      section: 'Operations',
      detail: 'Refresh runner capacity',
      keywords: ['runner', 'capacity'],
      shortcut: 'S',
      run: vi.fn(),
    },
    {
      id: 'danger-stop',
      label: 'Stop all workers',
      section: 'Operations',
      detail: 'Disabled while deployments are locked',
      keywords: ['pause', 'halt'],
      disabled: true,
      run: vi.fn(),
    },
    {
      id: 'strategic-mode',
      label: 'Switch Strategic to Workday',
      section: 'Strategic',
      detail: 'Keep Strategic ready for active work',
      keywords: ['mode', 'session', 'continuous'],
      run: vi.fn(),
    },
  ];

  return base.map((command, index) => ({ ...command, ...overrides[index] }));
}

describe('CommandPalette', () => {
  test('filters commands by label, keyword, and section', async () => {
    const user = userEvent.setup();
    render(
      <CommandPalette
        open
        commands={makeCommands()}
        onClose={vi.fn()}
      />,
    );

    const input = screen.getByLabelText('Search commands');

    await user.type(input, 'runner');
    expect(screen.getByRole('button', { name: /Sync fleet/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Open task drawer/i })).toBeNull();

    await user.click(screen.getByRole('button', { name: 'Clear command search' }));
    await user.type(input, 'navigation');
    expect(screen.getByRole('button', { name: /Open task drawer/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Sync fleet/i })).toBeNull();

    await user.click(screen.getByRole('button', { name: 'Clear command search' }));
    await user.type(input, 'stop');
    expect(screen.getByRole('button', { name: /Stop all workers/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Open task drawer/i })).toBeNull();

    await user.click(screen.getByRole('button', { name: 'Clear command search' }));
    await user.type(input, 'selected task');
    expect(screen.getByRole('button', { name: /Open task drawer/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Sync fleet/i })).toBeNull();

    await user.click(screen.getByRole('button', { name: 'Clear command search' }));
    await user.type(input, 'mode');
    expect(screen.getByRole('button', { name: /Switch Strategic to Workday/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Sync fleet/i })).toBeNull();
  });

  test('supports controlled query state', async () => {
    const user = userEvent.setup();
    const onQueryChange = vi.fn();
    const { rerender } = render(
      <CommandPalette
        open
        commands={makeCommands()}
        query="runner"
        onQueryChange={onQueryChange}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByLabelText('Search commands')).toHaveValue('runner');
    expect(screen.getByRole('button', { name: /Sync fleet/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Open task drawer/i })).toBeNull();

    await user.click(screen.getByRole('button', { name: 'Clear command search' }));
    expect(onQueryChange).toHaveBeenCalledWith('');

    rerender(
      <CommandPalette
        open
        commands={makeCommands()}
        query=""
        onQueryChange={onQueryChange}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByRole('button', { name: /Open task drawer/i })).toBeInTheDocument();
  });

  test('renders command shortcuts when provided', () => {
    render(
      <CommandPalette
        open
        commands={makeCommands()}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByText('O')).toBeInTheDocument();
    expect(screen.getByText('S')).toBeInTheDocument();
  });

  test('renders above workbench drawers', () => {
    const { container } = render(
      <CommandPalette
        open
        commands={makeCommands()}
        onClose={vi.fn()}
      />,
    );

    expect(container.firstElementChild).toHaveClass('z-[70]');
  });

  test('runs the active command with keyboard navigation and Enter', async () => {
    const user = userEvent.setup();
    const commands = makeCommands();
    render(
      <CommandPalette
        open
        commands={commands}
        onClose={vi.fn()}
      />,
    );

    await user.keyboard('{ArrowDown}{Enter}');

    expect(commands[0].run).not.toHaveBeenCalled();
    expect(commands[1].run).toHaveBeenCalledTimes(1);
  });

  test('does not run disabled commands from keyboard or mouse', async () => {
    const user = userEvent.setup();
    const commands = makeCommands([
      { disabled: true },
      { disabled: true },
      { disabled: true },
      { disabled: true },
    ]);
    render(
      <CommandPalette
        open
        commands={commands}
        onClose={vi.fn()}
      />,
    );

    await user.keyboard('{Enter}');
    await user.click(screen.getByRole('button', { name: /Stop all workers/i }));

    expect(commands[0].run).not.toHaveBeenCalled();
    expect(commands[1].run).not.toHaveBeenCalled();
    expect(commands[2].run).not.toHaveBeenCalled();
    expect(commands[3].run).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: /Stop all workers/i })).toBeDisabled();
  });

  test('Escape closes the palette', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(
      <CommandPalette
        open
        commands={makeCommands()}
        onClose={onClose}
      />,
    );

    await user.keyboard('{Escape}');

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  test('contains focus and restores focus after close', async () => {
    const user = userEvent.setup();
    const opener = document.createElement('button');
    opener.textContent = 'Open commands';
    document.body.appendChild(opener);
    opener.focus();
    const { rerender, unmount } = render(
      <CommandPalette
        open
        commands={makeCommands()}
        onClose={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByLabelText('Search commands')).toHaveFocus());

    await user.tab();
    expect(screen.getByRole('button', { name: /Open task drawer/i })).toHaveFocus();

    await user.tab();
    expect(screen.getByRole('button', { name: 'Close command palette' })).toHaveFocus();

    await user.tab();
    expect(screen.getByLabelText('Search commands')).toHaveFocus();

    rerender(
      <CommandPalette
        open={false}
        commands={makeCommands()}
        onClose={vi.fn()}
      />,
    );
    await waitFor(() => expect(opener).toHaveFocus());
    unmount();
    opener.remove();
  });

  test('includes the clear search button in the focus loop when visible', async () => {
    const user = userEvent.setup();
    render(
      <CommandPalette
        open
        commands={makeCommands()}
        onClose={vi.fn()}
      />,
    );

    const input = screen.getByLabelText('Search commands');
    await waitFor(() => expect(input).toHaveFocus());

    await user.type(input, 'runner');
    await user.tab();
    expect(screen.getByRole('button', { name: 'Clear command search' })).toHaveFocus();

    await user.tab();
    expect(screen.getByRole('button', { name: /Sync fleet/i })).toHaveFocus();

    await user.tab();
    expect(screen.getByRole('button', { name: 'Close command palette' })).toHaveFocus();
  });

  test('Enter on utility buttons uses the focused button instead of running the active command', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const commands = makeCommands();
    render(
      <CommandPalette
        open
        commands={commands}
        onClose={onClose}
      />,
    );

    const input = screen.getByLabelText('Search commands');
    await waitFor(() => expect(input).toHaveFocus());

    await user.type(input, 'runner');
    await user.tab();
    expect(screen.getByRole('button', { name: 'Clear command search' })).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(input).toHaveValue('');
    expect(commands[1].run).not.toHaveBeenCalled();

    screen.getByRole('button', { name: 'Close command palette' }).focus();
    expect(screen.getByRole('button', { name: 'Close command palette' })).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(commands[0].run).not.toHaveBeenCalled();
  });

  test('contains key events so document shortcuts do not run behind the palette', async () => {
    const user = userEvent.setup();
    const onDocumentKeyDown = vi.fn();
    render(
      <CommandPalette
        open
        commands={makeCommands()}
        onClose={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByLabelText('Search commands')).toHaveFocus());
    document.addEventListener('keydown', onDocumentKeyDown);

    await user.keyboard('n');

    document.removeEventListener('keydown', onDocumentKeyDown);
    expect(onDocumentKeyDown).not.toHaveBeenCalled();
  });
});
