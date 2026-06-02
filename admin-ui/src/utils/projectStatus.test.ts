import { describe, it, expect } from 'vitest';
import { projectStatusBadge } from './projectStatus';

describe('projectStatusBadge', () => {
  it('maps each ProjectStatus enum value to a distinct label', () => {
    expect(projectStatusBadge('DEPLOYING').label).toBe('Deploying');
    expect(projectStatusBadge('COMPLETED').label).toBe('Completed');
    expect(projectStatusBadge('FAILED').label).toBe('Failed');
    expect(projectStatusBadge('CANCELLED').label).toBe('Cancelled');
    expect(projectStatusBadge('DRAFT').label).toBe('Draft');
  });

  it('maps legacy lowercase rows', () => {
    expect(projectStatusBadge('active').label).toBe('Deploying');
    expect(projectStatusBadge('paused').label).toBe('Paused');
  });

  it('is case-insensitive', () => {
    expect(projectStatusBadge('failed').label).toBe('Failed');
    expect(projectStatusBadge('Completed').label).toBe('Completed');
  });

  it('carries color classes for known statuses', () => {
    expect(projectStatusBadge('FAILED').classes).toContain('text-red-400');
    expect(projectStatusBadge('COMPLETED').classes).toContain('text-emerald-400');
    expect(projectStatusBadge('DEPLOYING').classes).toContain('text-blue-400');
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
