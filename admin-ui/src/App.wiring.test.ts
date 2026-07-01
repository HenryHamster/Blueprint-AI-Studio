import { describe, expect, test } from 'vitest';
import { readFileSync } from 'node:fs';
import path from 'node:path';

const appSource = readFileSync(path.join(process.cwd(), 'src', 'App.tsx'), 'utf8');

describe('App workbench wiring', () => {
  test('wires the real Strategic MessagesPanel to the context workspace opener', () => {
    expect(appSource).toContain('const openStrategicContextWorkspace = useCallback(');
    expect(appSource).toMatch(/<MessagesPanel[\s\S]*onOpenCommandPalette=\{openCommandPalette\}[\s\S]*onOpenStrategicContextWorkspace=\{openStrategicContextWorkspace\}/);
  });
});
