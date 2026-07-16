import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ProjectAutoReviewSection } from './ProjectAutoReviewSection';

vi.mock('../api', () => ({
  apiFetch: vi.fn(),
}));

vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  getIdToken: vi.fn(async () => 'test-token'),
  getIdTokenAfterAuthRestore: vi.fn(async () => 'test-token'),
  onAuthChange: vi.fn(() => () => {}),
  signOut: vi.fn(),
}));

describe('ProjectAutoReviewSection', () => {
  it('hides save action for non-owners', () => {
    render(
      <ProjectAutoReviewSection
        config={{ enabled: false, max_per_hour: 5, max_concurrent: 3 }}
        canSave={false}
        onSave={vi.fn()}
      />,
    );

    expect(screen.queryByRole('button', { name: /save auto-review config/i })).not.toBeInTheDocument();
    expect(screen.getByText(/Only project owners can save auto-review settings/i)).toBeInTheDocument();
  });

  it('submits changed fields when enabled is toggled', async () => {
    const user = userEvent.setup();
    const onSave = vi.fn().mockResolvedValue(undefined);

    render(
      <ProjectAutoReviewSection
        config={{ enabled: false, max_per_hour: 5, max_concurrent: 3 }}
        canSave
        onSave={onSave}
      />,
    );

    await user.click(screen.getAllByRole('button')[0]);
    await user.click(screen.getByRole('button', { name: /save auto-review config/i }));

    expect(onSave).toHaveBeenCalledWith({ enabled: true });
  });
});
