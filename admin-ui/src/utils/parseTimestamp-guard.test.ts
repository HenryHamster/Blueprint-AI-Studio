import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import path from 'path';

/**
 * Guard test: prevents regression of the timezone-shift bug.
 *
 * The backend sends naive UTC timestamps (no trailing 'Z'). Direct `new Date(apiString)`
 * silently interprets them as local time, shifting displays by the operator's UTC offset.
 *
 * This test validates that fixed files use parseTimestamp correctly.
 */
describe('parseTimestamp guard: prevent direct new Date(apiString)', () => {
  it('projectStatus.ts imports parseTimestamp', () => {
    const file = readFileSync(
      path.join(__dirname, 'projectStatus.ts'),
      'utf-8'
    );
    expect(file).toContain("import { parseTimestamp } from './time'");
  });

  it('projectStatus.ts uses parseTimestamp for created_at', () => {
    const file = readFileSync(
      path.join(__dirname, 'projectStatus.ts'),
      'utf-8'
    );
    expect(file).toContain('parseTimestamp(createdAt)');
  });

  it('TaskFixLoopPanel.tsx imports parseTimestamp', () => {
    const file = readFileSync(
      path.join(__dirname, '../components/TaskFixLoopPanel.tsx'),
      'utf-8'
    );
    expect(file).toContain("import { parseTimestamp } from '../utils/time'");
  });

  it('TaskFixLoopPanel.tsx uses parseTimestamp for timestamps', () => {
    const file = readFileSync(
      path.join(__dirname, '../components/TaskFixLoopPanel.tsx'),
      'utf-8'
    );
    expect(file).toContain('parseTimestamp(startedAt)');
    expect(file).toContain('parseTimestamp(endedAt)');
    expect(file).toContain('parseTimestamp(isoString)');
  });

  it('ProviderModelBreakdownPanel.tsx imports parseTimestamp', () => {
    const file = readFileSync(
      path.join(__dirname, '../components/ProviderModelBreakdownPanel.tsx'),
      'utf-8'
    );
    expect(file).toContain("import { parseTimestamp } from '../utils/time'");
  });

  it('ProviderModelBreakdownPanel.tsx uses parseTimestamp for dates', () => {
    const file = readFileSync(
      path.join(__dirname, '../components/ProviderModelBreakdownPanel.tsx'),
      'utf-8'
    );
    expect(file).toContain('parseTimestamp(iso)');
  });
});
