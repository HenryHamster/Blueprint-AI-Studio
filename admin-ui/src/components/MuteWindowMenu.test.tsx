import { describe, test, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MuteWindowMenu } from './MuteWindowMenu';

describe('MuteWindowMenu', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  test('inactive: shows arm options and closes after arming', async () => {
    const onSet = vi.fn();
    render(<MuteWindowMenu muteActive={false} muteWindow={null} onSetMuteWindow={onSet} />);

    const trigger = screen.getByTitle('Mute routine ops temporarily');
    fireEvent.click(trigger);
    await waitFor(() => expect(screen.getByText('Mute routine ops for')).toBeInTheDocument());
    expect(screen.getByText('1 hour')).toBeInTheDocument();
    expect(screen.getByText('4 hours')).toBeInTheDocument();
    expect(screen.getByText('8 hours')).toBeInTheDocument();

    const before = Date.now();
    fireEvent.click(screen.getByText('4 hours'));
    expect(onSet).toHaveBeenCalledTimes(1);
    const win = onSet.mock.calls[0][0];
    expect(win.expiresAt).toBeGreaterThanOrEqual(before + 14400000);
    // Menu closed after arming
    await waitFor(() => expect(screen.queryByText('Mute routine ops for')).not.toBeInTheDocument());
  });

  test('active: extend adds to existing expiry', async () => {
    const onSet = vi.fn();
    const expiresAt = 5_000_000;
    render(<MuteWindowMenu muteActive={true} muteWindow={{ expiresAt }} onSetMuteWindow={onSet} />);

    fireEvent.click(screen.getByText(/🔕/));
    await waitFor(() => expect(screen.getByText('Mute active')).toBeInTheDocument());

    fireEvent.click(screen.getByText('Extend +4h'));
    expect(onSet).toHaveBeenCalledWith({ expiresAt: expiresAt + 14400000 });
  });

  test('active: End mute early clears the window', async () => {
    const onSet = vi.fn();
    render(<MuteWindowMenu muteActive={true} muteWindow={{ expiresAt: 5_000_000 }} onSetMuteWindow={onSet} />);

    fireEvent.click(screen.getByText(/🔕/));
    await waitFor(() => expect(screen.getByText('End mute early')).toBeInTheDocument());

    fireEvent.click(screen.getByText('End mute early'));
    expect(onSet).toHaveBeenCalledWith(null);
  });

  test('backdrop click closes the menu without arming', async () => {
    const onSet = vi.fn();
    render(<MuteWindowMenu muteActive={false} muteWindow={null} onSetMuteWindow={onSet} />);

    fireEvent.click(screen.getByTitle('Mute routine ops temporarily'));
    await waitFor(() => expect(screen.getByText('Mute routine ops for')).toBeInTheDocument());

    // The fixed-inset backdrop is the first div sibling of the popover
    const backdrop = document.querySelector('.fixed.inset-0') as HTMLElement;
    fireEvent.click(backdrop);
    await waitFor(() => expect(screen.queryByText('Mute routine ops for')).not.toBeInTheDocument());
    expect(onSet).not.toHaveBeenCalled();
  });
});
