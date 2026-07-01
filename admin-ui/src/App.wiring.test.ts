import { describe, expect, test } from 'vitest';
import { readFileSync } from 'node:fs';
import path from 'node:path';

const appSource = readFileSync(path.join(process.cwd(), 'src', 'App.tsx'), 'utf8');

describe('App workbench wiring', () => {
  test('wires the real Strategic MessagesPanel to the context workspace opener', () => {
    expect(appSource).toContain('const openStrategicContextWorkspace = useCallback(');
    expect(appSource).toMatch(/<MessagesPanel[\s\S]*onOpenCommandPalette=\{openCommandPalette\}[\s\S]*onOpenStrategicContextWorkspace=\{openStrategicContextWorkspace\}/);
  });

  test('keeps Strategic check-in idempotency stable across retry attempts', () => {
    expect(appSource).toContain('const strategicCheckInIdempotencyKeyRef = useRef<string | null>(null);');
    expect(appSource).toMatch(/if \(!strategicCheckInIdempotencyKeyRef\.current\) \{[\s\S]*strategicCheckInIdempotencyKeyRef\.current = `strategic-check-in:\$\{projectId\}:\$\{Date\.now\(\)\}`;/);
    expect(appSource).toContain('idempotency_key: strategicCheckInIdempotencyKeyRef.current,');
    expect(appSource).toMatch(/await checkInStrategicSession[\s\S]*strategicCheckInIdempotencyKeyRef\.current = null;[\s\S]*Strategic check-in recorded/);
    expect(appSource).toContain('strategicReadModel.applySessionResponse(checkInResponse);');
  });
});
