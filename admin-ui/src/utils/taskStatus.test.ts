import { taskStatusTone, taskStatusDotColor, taskStatusBadgeClasses, taskStatusTextColor } from './taskStatus';

const ALL_STATUSES = ['QUEUED', 'PROVISIONING', 'IN_PROGRESS', 'EVALUATING', 'COMPLETED', 'FAILED', 'CANCELLED', 'BLOCKED', 'PARKED'];

describe('taskStatusTone', () => {
  it('maps COMPLETED to success', () => {
    expect(taskStatusTone('COMPLETED')).toBe('success');
  });
  it('maps FAILED to blocking', () => {
    expect(taskStatusTone('FAILED')).toBe('blocking');
  });
  it('maps IN_PROGRESS to warning', () => {
    expect(taskStatusTone('IN_PROGRESS')).toBe('warning');
  });
  it('maps BLOCKED to warning', () => {
    expect(taskStatusTone('BLOCKED')).toBe('warning');
  });
  it('maps PROVISIONING to info', () => {
    expect(taskStatusTone('PROVISIONING')).toBe('info');
  });
  it('maps EVALUATING to info', () => {
    expect(taskStatusTone('EVALUATING')).toBe('info');
  });
  it('maps QUEUED to neutral', () => {
    expect(taskStatusTone('QUEUED')).toBe('neutral');
  });
  it('maps CANCELLED to neutral', () => {
    expect(taskStatusTone('CANCELLED')).toBe('neutral');
  });
  it('maps PARKED to neutral', () => {
    expect(taskStatusTone('PARKED')).toBe('neutral');
  });
  it('maps unknown status to neutral', () => {
    expect(taskStatusTone('UNKNOWN_STATUS')).toBe('neutral');
  });
});

describe('taskStatusDotColor', () => {
  it('returns a non-empty bg-* class for each known status', () => {
    for (const s of ALL_STATUSES) {
      const cls = taskStatusDotColor(s);
      expect(cls).toBeTruthy();
      expect(cls).toMatch(/^bg-/);
    }
  });
  it('returns a fallback for unknown status', () => {
    expect(taskStatusDotColor('UNKNOWN')).toMatch(/^bg-/);
  });
  it('COMPLETED is emerald', () => {
    expect(taskStatusDotColor('COMPLETED')).toContain('emerald');
  });
  it('FAILED is red', () => {
    expect(taskStatusDotColor('FAILED')).toContain('red');
  });
});

describe('taskStatusBadgeClasses', () => {
  it('returns a non-empty class string for each known status', () => {
    for (const s of ALL_STATUSES) {
      const cls = taskStatusBadgeClasses(s);
      expect(cls).toBeTruthy();
    }
  });
  it('includes bg-status- and text-status- tokens', () => {
    for (const s of ALL_STATUSES) {
      const cls = taskStatusBadgeClasses(s);
      expect(cls).toMatch(/bg-status-/);
      expect(cls).toMatch(/text-status-/);
    }
  });
  it('COMPLETED uses success tone', () => {
    expect(taskStatusBadgeClasses('COMPLETED')).toContain('success');
  });
  it('FAILED uses blocking tone', () => {
    expect(taskStatusBadgeClasses('FAILED')).toContain('blocking');
  });
});

describe('taskStatusTextColor', () => {
  it('returns a text-* class for each known status', () => {
    for (const s of ALL_STATUSES) {
      const cls = taskStatusTextColor(s);
      expect(cls).toBeTruthy();
      expect(cls).toMatch(/^text-/);
    }
  });
  it('returns a fallback for unknown status', () => {
    expect(taskStatusTextColor('UNKNOWN')).toMatch(/^text-/);
  });
  it('COMPLETED is emerald', () => {
    expect(taskStatusTextColor('COMPLETED')).toContain('emerald');
  });
  it('FAILED is red', () => {
    expect(taskStatusTextColor('FAILED')).toContain('red');
  });
  it('BLOCKED is amber', () => {
    expect(taskStatusTextColor('BLOCKED')).toContain('amber');
  });
  it('PARKED is purple', () => {
    expect(taskStatusTextColor('PARKED')).toContain('purple');
  });
});
