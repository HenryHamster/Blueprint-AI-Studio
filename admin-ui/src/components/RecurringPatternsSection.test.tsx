import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { RecurringPatternsSection } from './RecurringPatternsSection';

describe('RecurringPatternsSection', () => {
  it('renders nothing when recurring patterns array is empty', () => {
    const { container } = render(
      <RecurringPatternsSection
        recurringPatterns={[]}
        onDirectionClick={vi.fn()}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it('renders section heading with pattern count', () => {
    const patterns = [
      {
        fingerprint: 'fp-1',
        occurrence_count: 5,
        example_message: 'Error message',
        affected_direction_count: 2,
        affected_directions: [],
        remediation_status: 'unaddressed' as const,
      },
      {
        fingerprint: 'fp-2',
        occurrence_count: 3,
        example_message: 'Another error',
        affected_direction_count: 1,
        affected_directions: [],
        remediation_status: 'investigating' as const,
      },
    ];

    render(
      <RecurringPatternsSection
        recurringPatterns={patterns}
        onDirectionClick={vi.fn()}
      />,
    );

    expect(screen.getByText(/Recurring Patterns/)).toBeInTheDocument();
    expect(screen.getByText('· 2')).toBeInTheDocument();
  });

  it('renders remediation status badge with correct styles for each status', () => {
    const patterns = [
      {
        fingerprint: 'fp-1',
        occurrence_count: 1,
        example_message: 'test',
        affected_direction_count: 0,
        affected_directions: [],
        remediation_status: 'fix_verified' as const,
      },
      {
        fingerprint: 'fp-2',
        occurrence_count: 1,
        example_message: 'test',
        affected_direction_count: 0,
        affected_directions: [],
        remediation_status: 'fix_shipped' as const,
      },
      {
        fingerprint: 'fp-3',
        occurrence_count: 1,
        example_message: 'test',
        affected_direction_count: 0,
        affected_directions: [],
        remediation_status: 'investigating' as const,
      },
      {
        fingerprint: 'fp-4',
        occurrence_count: 1,
        example_message: 'test',
        affected_direction_count: 0,
        affected_directions: [],
        remediation_status: 'unaddressed' as const,
      },
    ];

    const { container } = render(
      <RecurringPatternsSection
        recurringPatterns={patterns}
        onDirectionClick={vi.fn()}
      />,
    );

    // Check that all status labels are rendered
    expect(screen.getByText('fix verified')).toBeInTheDocument();
    expect(screen.getByText('fix shipped')).toBeInTheDocument();
    expect(screen.getByText('investigating')).toBeInTheDocument();
    expect(screen.getByText('unaddressed')).toBeInTheDocument();

    // Check that appropriate styles are applied
    const badges = container.querySelectorAll('span[class*="text-"]');
    const verifiedBadge = Array.from(badges).find(el => el.textContent === 'fix verified');
    expect(verifiedBadge?.className).toMatch(/bg-green-500/);

    const shippedBadge = Array.from(badges).find(el => el.textContent === 'fix shipped');
    expect(shippedBadge?.className).toMatch(/bg-blue-500/);

    const investigatingBadge = Array.from(badges).find(el => el.textContent === 'investigating');
    expect(investigatingBadge?.className).toMatch(/bg-yellow-500/);

    const unaddressedBadge = Array.from(badges).find(el => el.textContent === 'unaddressed');
    expect(unaddressedBadge?.className).toMatch(/bg-red-500/);
  });

  it('truncates example message to 120 characters with ellipsis', () => {
    const longMessage = 'a'.repeat(150);
    const patterns = [
      {
        fingerprint: 'fp-1',
        occurrence_count: 1,
        example_message: longMessage,
        affected_direction_count: 0,
        affected_directions: [],
        remediation_status: 'unaddressed' as const,
      },
    ];

    render(
      <RecurringPatternsSection
        recurringPatterns={patterns}
        onDirectionClick={vi.fn()}
      />,
    );

    const snippet = 'a'.repeat(120) + '…';
    expect(screen.getByText(snippet)).toBeInTheDocument();
  });

  it('does not truncate message if it is 120 characters or less', () => {
    const message = 'a'.repeat(100);
    const patterns = [
      {
        fingerprint: 'fp-1',
        occurrence_count: 1,
        example_message: message,
        affected_direction_count: 0,
        affected_directions: [],
        remediation_status: 'unaddressed' as const,
      },
    ];

    render(
      <RecurringPatternsSection
        recurringPatterns={patterns}
        onDirectionClick={vi.fn()}
      />,
    );

    expect(screen.getByText(message)).toBeInTheDocument();
    expect(screen.queryByText(/…$/)).not.toBeInTheDocument();
  });

  it('displays occurrence count badge', () => {
    const patterns = [
      {
        fingerprint: 'fp-1',
        occurrence_count: 42,
        example_message: 'Error',
        affected_direction_count: 0,
        affected_directions: [],
        remediation_status: 'unaddressed' as const,
      },
    ];

    render(
      <RecurringPatternsSection
        recurringPatterns={patterns}
        onDirectionClick={vi.fn()}
      />,
    );

    expect(screen.getByText('×42')).toBeInTheDocument();
  });

  it('renders up to 4 direction chips without overflow indicator', () => {
    const patterns = [
      {
        fingerprint: 'fp-1',
        occurrence_count: 1,
        example_message: 'Error',
        affected_direction_count: 3,
        affected_directions: [
          { direction_id: 'dir-1', direction_text: 'Direction One' },
          { direction_id: 'dir-2', direction_text: 'Direction Two' },
          { direction_id: 'dir-3', direction_text: 'Direction Three' },
        ],
        remediation_status: 'unaddressed' as const,
      },
    ];

    render(
      <RecurringPatternsSection
        recurringPatterns={patterns}
        onDirectionClick={vi.fn()}
      />,
    );

    expect(screen.getByText('Direction One')).toBeInTheDocument();
    expect(screen.getByText('Direction Two')).toBeInTheDocument();
    expect(screen.getByText('Direction Three')).toBeInTheDocument();
    expect(screen.queryByText(/^\+/)).not.toBeInTheDocument();
  });

  it('renders overflow indicator when more than 4 directions', () => {
    const patterns = [
      {
        fingerprint: 'fp-1',
        occurrence_count: 1,
        example_message: 'Error',
        affected_direction_count: 6,
        affected_directions: [
          { direction_id: 'dir-1', direction_text: 'Direction One' },
          { direction_id: 'dir-2', direction_text: 'Direction Two' },
          { direction_id: 'dir-3', direction_text: 'Direction Three' },
          { direction_id: 'dir-4', direction_text: 'Direction Four' },
          { direction_id: 'dir-5', direction_text: 'Direction Five' },
          { direction_id: 'dir-6', direction_text: 'Direction Six' },
        ],
        remediation_status: 'unaddressed' as const,
      },
    ];

    render(
      <RecurringPatternsSection
        recurringPatterns={patterns}
        onDirectionClick={vi.fn()}
      />,
    );

    expect(screen.getByText('Direction One')).toBeInTheDocument();
    expect(screen.getByText('Direction Four')).toBeInTheDocument();
    expect(screen.queryByText('Direction Five')).not.toBeInTheDocument();
    expect(screen.queryByText('Direction Six')).not.toBeInTheDocument();
    expect(screen.getByText('+2 more')).toBeInTheDocument();
  });

  it('invokes onDirectionClick when a direction chip is clicked', async () => {
    const user = userEvent.setup();
    const onDirectionClick = vi.fn();
    const patterns = [
      {
        fingerprint: 'fp-1',
        occurrence_count: 1,
        example_message: 'Error',
        affected_direction_count: 1,
        affected_directions: [
          { direction_id: 'dir-123', direction_text: 'Test Direction' },
        ],
        remediation_status: 'unaddressed' as const,
      },
    ];

    render(
      <RecurringPatternsSection
        recurringPatterns={patterns}
        onDirectionClick={onDirectionClick}
      />,
    );

    const directionButton = screen.getByText('Test Direction');
    await user.click(directionButton);

    expect(onDirectionClick).toHaveBeenCalledWith('dir-123');
  });

  it('handles direction without direction_text, falling back to direction_id', () => {
    const patterns = [
      {
        fingerprint: 'fp-1',
        occurrence_count: 1,
        example_message: 'Error',
        affected_direction_count: 1,
        affected_directions: [
          { direction_id: 'dir-123', direction_text: '' },
        ],
        remediation_status: 'unaddressed' as const,
      },
    ];

    render(
      <RecurringPatternsSection
        recurringPatterns={patterns}
        onDirectionClick={vi.fn()}
      />,
    );

    expect(screen.getByText('dir-123')).toBeInTheDocument();
  });

  it('limits display to first 5 patterns when more are provided', () => {
    const patterns = Array.from({ length: 10 }, (_, i) => ({
      fingerprint: `fp-${i}`,
      occurrence_count: 1,
      example_message: `Error ${i}`,
      affected_direction_count: 0,
      affected_directions: [],
      remediation_status: 'unaddressed' as const,
    }));

    render(
      <RecurringPatternsSection
        recurringPatterns={patterns}
        onDirectionClick={vi.fn()}
      />,
    );

    expect(screen.getByText('Error 0')).toBeInTheDocument();
    expect(screen.getByText('Error 4')).toBeInTheDocument();
    expect(screen.queryByText('Error 5')).not.toBeInTheDocument();
    expect(screen.queryByText('Error 9')).not.toBeInTheDocument();
  });

  it('handles missing example_message gracefully', () => {
    const patterns = [
      {
        fingerprint: 'fp-1',
        occurrence_count: 1,
        example_message: null,
        affected_direction_count: 0,
        affected_directions: [],
        remediation_status: 'unaddressed' as const,
      },
    ];

    render(
      <RecurringPatternsSection
        recurringPatterns={patterns}
        onDirectionClick={vi.fn()}
      />,
    );

    // Should render without error and show empty snippet
    expect(screen.getByText('×1')).toBeInTheDocument();
  });
});
