import { describe, it, expect } from 'vitest';
import { projectStatusBadge } from './projectStatus';

describe('projectStatusBadge', () => {
  describe('backward compatibility: string input', () => {
    it('maps each ProjectStatus enum value to a distinct label', () => {
      expect(projectStatusBadge('DEPLOYING').label).toBe('Running');
      expect(projectStatusBadge('COMPLETED').label).toBe('Completed');
      expect(projectStatusBadge('FAILED').label).toBe('Failed');
      expect(projectStatusBadge('CANCELLED').label).toBe('Cancelled');
      expect(projectStatusBadge('DRAFT').label).toBe('Draft');
    });

    it('maps legacy lowercase rows', () => {
      expect(projectStatusBadge('active').label).toBe('Running');
      expect(projectStatusBadge('paused').label).toBe('Paused');
    });

    it('is case-insensitive', () => {
      expect(projectStatusBadge('failed').label).toBe('Failed');
      expect(projectStatusBadge('Completed').label).toBe('Completed');
    });

    it('carries color classes for known statuses', () => {
      expect(projectStatusBadge('FAILED').classes).toContain('text-red-400');
      expect(projectStatusBadge('COMPLETED').classes).toContain('text-emerald-400');
      expect(projectStatusBadge('DEPLOYING').classes).toContain('text-emerald-400');
    });

    it('falls back to the raw value for unknown statuses', () => {
      expect(projectStatusBadge('WEIRD_STATE').label).toBe('WEIRD_STATE');
      expect(projectStatusBadge('WEIRD_STATE').classes).toContain('text-neutral-400');
    });

    it('falls back to Unknown for empty/missing statuses', () => {
      expect(projectStatusBadge('').label).toBe('Unknown');
      expect(projectStatusBadge(null).label).toBe('Unknown');
      expect(projectStatusBadge(undefined).label).toBe('Unknown');
    });
  });

  describe('object input with contextual signals', () => {
    it('shows terminal states regardless of other signals', () => {
      expect(projectStatusBadge({ status: 'COMPLETED', paused: true }).label).toBe('Completed');
      expect(projectStatusBadge({ status: 'FAILED', archived: true }).label).toBe('Failed');
      expect(projectStatusBadge({ status: 'CANCELLED' }).label).toBe('Cancelled');
    });

    it('shows archived state for archived projects', () => {
      expect(projectStatusBadge({ status: 'DEPLOYING', archived: true }).label).toBe('Archived');
      expect(projectStatusBadge({ status: 'DEPLOYING', archived: true }).classes).toContain('text-neutral-400');
    });

    it('shows paused state when paused flag is true', () => {
      expect(projectStatusBadge({ status: 'DEPLOYING', paused: true }).label).toBe('Paused');
      expect(projectStatusBadge({ status: 'DEPLOYING', paused: true }).classes).toContain('text-amber-400');
    });

    it('shows Bootstrapping for recently created projects', () => {
      const oneMinuteAgo = new Date(Date.now() - 60 * 1000).toISOString();
      expect(projectStatusBadge({ status: 'DEPLOYING', created_at: oneMinuteAgo }).label).toBe('Bootstrapping');
      expect(projectStatusBadge({ status: 'DEPLOYING', created_at: oneMinuteAgo }).classes).toContain('text-blue-400');
    });

    it('shows Running for long-running projects', () => {
      const twoHoursAgo = new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString();
      expect(projectStatusBadge({ status: 'DEPLOYING', created_at: twoHoursAgo }).label).toBe('Running');
      expect(projectStatusBadge({ status: 'DEPLOYING', created_at: twoHoursAgo }).classes).toContain('text-emerald-400');
    });

    it('shows Running by default for active projects without created_at', () => {
      expect(projectStatusBadge({ status: 'DEPLOYING' }).label).toBe('Running');
    });

    it('handles legacy active status with contextual signals', () => {
      const recentTime = new Date(Date.now() - 30 * 60 * 1000).toISOString();
      expect(projectStatusBadge({ status: 'active', created_at: recentTime }).label).toBe('Bootstrapping');

      const oldTime = new Date(Date.now() - 3 * 60 * 60 * 1000).toISOString();
      expect(projectStatusBadge({ status: 'active', created_at: oldTime }).label).toBe('Running');
    });

    it('prioritizes terminal states over all other signals', () => {
      expect(projectStatusBadge({
        status: 'FAILED',
        paused: true,
        archived: true,
        created_at: new Date(Date.now() - 30 * 1000).toISOString()
      }).label).toBe('Failed');
    });
  });
});
