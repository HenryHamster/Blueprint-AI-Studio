import { describe, test, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MessageTypeDropdown } from './MessageTypeDropdown';

describe('MessageTypeDropdown', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  test('renders with initial value', () => {
    const onChange = vi.fn();
    render(<MessageTypeDropdown value="FEEDBACK" onChange={onChange} />);
    expect(screen.getByText('Feedback')).toBeInTheDocument();
  });

  test('toggles dropdown open/closed on button click', async () => {
    const onChange = vi.fn();
    render(<MessageTypeDropdown value="FEEDBACK" onChange={onChange} />);
    const button = screen.getByText('Feedback');

    fireEvent.click(button);
    await waitFor(() => {
      expect(screen.getByText('Question')).toBeInTheDocument();
    });

    fireEvent.click(button);
    await waitFor(() => {
      expect(screen.queryByText('Question')).not.toBeInTheDocument();
    });
  });

  test('calls onChange when option is selected', async () => {
    const onChange = vi.fn();
    render(<MessageTypeDropdown value="FEEDBACK" onChange={onChange} />);

    fireEvent.click(screen.getByText('Feedback'));
    await waitFor(() => {
      expect(screen.getByText('Bug Report')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText('Bug Report'));
    expect(onChange).toHaveBeenCalledWith('BUG_REPORT');
  });

  test('closes dropdown after selection', async () => {
    const onChange = vi.fn();
    render(<MessageTypeDropdown value="FEEDBACK" onChange={onChange} />);

    fireEvent.click(screen.getByText('Feedback'));
    await waitFor(() => {
      expect(screen.getByText('Bug Report')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText('Bug Report'));
    await waitFor(() => {
      expect(screen.queryByText('Question')).not.toBeInTheDocument();
    });
  });

  test('displays checkmark for current value', async () => {
    const onChange = vi.fn();
    const { rerender, container } = render(<MessageTypeDropdown value="FEEDBACK" onChange={onChange} />);

    // Trigger label is unique while the menu is closed.
    fireEvent.click(screen.getByText('Feedback'));
    await waitFor(() => {
      // Exactly one option (the selected one) renders a check icon.
      expect(container.querySelectorAll('.lucide-check').length).toBe(1);
    });

    // Changing the value moves the checkmark to the new selection; the menu
    // stays open, so there is still exactly one check icon.
    rerender(<MessageTypeDropdown value="BLOCKER" onChange={onChange} />);
    await waitFor(() => {
      expect(container.querySelectorAll('.lucide-check').length).toBe(1);
    });
  });

  test('closes dropdown on outside click', async () => {
    const onChange = vi.fn();
    render(
      <div>
        <MessageTypeDropdown value="FEEDBACK" onChange={onChange} />
        <div data-testid="outside">Outside</div>
      </div>
    );

    fireEvent.click(screen.getByText('Feedback'));
    await waitFor(() => {
      expect(screen.getByText('Question')).toBeInTheDocument();
    });

    const outside = screen.getByTestId('outside');
    fireEvent.mouseDown(outside);
    await waitFor(() => {
      expect(screen.queryByText('Question')).not.toBeInTheDocument();
    });
  });

  test('renders all message type options', async () => {
    const onChange = vi.fn();
    render(<MessageTypeDropdown value="FEEDBACK" onChange={onChange} />);

    fireEvent.click(screen.getByText('Feedback'));
    await waitFor(() => {
      // 'Feedback' is the selected value, so it appears in both the trigger
      // and the open menu (hence getAllByText). The other options appear once.
      expect(screen.getAllByText('Feedback').length).toBeGreaterThanOrEqual(2);
      expect(screen.getByText('Question')).toBeInTheDocument();
      expect(screen.getByText('Bug Report')).toBeInTheDocument();
      expect(screen.getByText('Blocker')).toBeInTheDocument();
      expect(screen.getByText('Note')).toBeInTheDocument();
    });
  });
});
