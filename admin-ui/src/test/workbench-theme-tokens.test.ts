import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, test } from 'vitest';

const WORKBENCH_THEME_FILES = [
  'src/components/HeaderBar.tsx',
  'src/components/CommandPalette.tsx',
  'src/components/MessagesComposer.tsx',
  'src/components/MessagesListView.tsx',
  'src/components/MessagesPanel.tsx',
  'src/components/OperatorWorkspaces.tsx',
  'src/components/PipelineWorkspace.tsx',
  'src/components/RuntimeAccountsPanel.tsx',
  'src/components/RuntimeWorkspace.tsx',
  'src/components/StrategicContextArtifactSlot.tsx',
  'src/components/StrategicContextWorkspace.tsx',
  'src/components/WorkbenchLayout.tsx',
];

describe('workbench theme tokens', () => {
  test('primary shell surfaces use semantic workbench tokens instead of raw dark color literals', () => {
    const rawSurfaceColorPattern = /(?:bg|border|text)-\[#|#[0-9a-fA-F]{6,8}/;
    const offenders = WORKBENCH_THEME_FILES
      .map(file => {
        const source = readFileSync(resolve(process.cwd(), file), 'utf8');
        return rawSurfaceColorPattern.test(source) ? file : null;
      })
      .filter(Boolean);

    expect(offenders).toEqual([]);
  });
});
