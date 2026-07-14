import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import path from 'path';

function expectNamedImport(file: string, name: string, source: string) {
  const importPattern = new RegExp(
    `import\\s+\\{([^}]+)\\}\\s+from\\s+['"]${source.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}['"]`
  );
  const match = file.match(importPattern);
  expect(match?.[1].split(',').map(part => part.trim())).toContain(name);
}

/**
 * Guard test: prevents regression of the timezone-shift bug.
 *
 * The backend sends naive UTC timestamps (no trailing 'Z'). Direct `new Date(apiString)`
 * silently interprets them as local time, shifting displays by the operator's UTC offset.
 *
 * This test validates that fixed files use parseTimestamp correctly.
 */
describe('parseTimestamp guard: prevent direct new Date(apiString)', () => {
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
    expectNamedImport(file, 'parseTimestamp', '../utils/time');
  });

  it('ProviderModelBreakdownPanel.tsx uses parseTimestamp for dates', () => {
    const file = readFileSync(
      path.join(__dirname, '../components/ProviderModelBreakdownPanel.tsx'),
      'utf-8'
    );
    expect(file).toContain('parseTimestamp(iso)');
  });

  it('messageGrouping.ts imports parseTimestamp', () => {
    const file = readFileSync(
      path.join(__dirname, '../components/messageGrouping.ts'),
      'utf-8'
    );
    expect(file).toContain("import { parseTimestamp } from '../utils/time'");
  });

  it('messageGrouping.ts uses parseTimestamp for created_at', () => {
    const file = readFileSync(
      path.join(__dirname, '../components/messageGrouping.ts'),
      'utf-8'
    );
    expect(file).toContain('parseTimestamp(msg.created_at)');
  });

  it('messageGrouping.ts has no hand-rolled Z-append timestamp parsing', () => {
    const file = readFileSync(
      path.join(__dirname, '../components/messageGrouping.ts'),
      'utf-8'
    );
    // The recurring bug class: inline `v + 'Z'` re-implementations of the
    // shared guard. All backend-timestamp parsing must route through
    // parseTimestamp from ../utils/time instead.
    expect(file).not.toContain("v + 'Z'");
    // No raw new Date() on the API string field either.
    expect(file).not.toContain('new Date(msg.created_at)');
  });

  it('MessagesPanel.tsx imports parseTimestamp', () => {
    const file = readFileSync(
      path.join(__dirname, '../components/MessagesPanel.tsx'),
      'utf-8'
    );
    expect(file).toContain("import { parseTimestamp");
    expect(file).toContain("from '../utils/time'");
  });

  it('MessagesPanel.tsx uses parseTimestamp for the strategic-refresh receipt age gate', () => {
    const file = readFileSync(
      path.join(__dirname, '../components/MessagesPanel.tsx'),
      'utf-8'
    );
    expect(file).toContain('parseTimestamp(lastFiredAt)');
  });

  it('MessagesPanel.tsx has no raw new Date() parsing of the receipt timestamp', () => {
    const file = readFileSync(
      path.join(__dirname, '../components/MessagesPanel.tsx'),
      'utf-8'
    );
    // The strategic-refresh receipt age gate must not reintroduce raw
    // `new Date(lastFiredAt)` (timezone-shifted) or a hand-rolled Z-append.
    expect(file).not.toContain('new Date(lastFiredAt)');
    expect(file).not.toContain("v + 'Z'");
  });

  it('MessagesPanel.tsx uses parseTimestamp for Strategic session freshness', () => {
    const file = readFileSync(
      path.join(__dirname, '../components/MessagesPanel.tsx'),
      'utf-8'
    );
    expect(file).toContain('parseTimestamp(sessionFreshTs)');
    expect(file).not.toContain('new Date(sessionFreshTs)');
  });

  it('strategicContextDescriptor.ts imports parseTimestamp', () => {
    const file = readFileSync(
      path.join(__dirname, './strategicContextDescriptor.ts'),
      'utf-8'
    );
    expect(file).toContain("import { parseTimestamp } from './time'");
  });

  it('strategicContextDescriptor.ts uses parseTimestamp for session freshness', () => {
    const file = readFileSync(
      path.join(__dirname, './strategicContextDescriptor.ts'),
      'utf-8'
    );
    expect(file).toContain('parseTimestamp(ts)');
    expect(file).not.toContain('new Date(ts)');
  });

  it('strategicWorkbenchSession.ts imports parseTimestamp', () => {
    const file = readFileSync(
      path.join(__dirname, './strategicWorkbenchSession.ts'),
      'utf-8'
    );
    expect(file).toContain("import { parseTimestamp } from './time'");
  });

  it('strategicWorkbenchSession.ts uses parseTimestamp for stale-base detection', () => {
    const file = readFileSync(
      path.join(__dirname, './strategicWorkbenchSession.ts'),
      'utf-8'
    );
    expect(file).toContain('parseTimestamp(ts)');
    expect(file).not.toContain('new Date(ts)');
  });
});
