import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';

describe('viewport test coverage invariant', () => {
  it('fails CI when a required component is missing viewport assertions for a required width', () => {
    const requiredPath = path.join(__dirname, 'viewport-required.json');
    const { requiredWidths, components } = JSON.parse(fs.readFileSync(requiredPath, 'utf-8')) as {
      requiredWidths: number[];
      components: string[];
    };

    const componentsDir = path.join(__dirname, '../components');
    const missing: string[] = [];

    for (const component of components) {
      const testFile = path.join(componentsDir, `${component}.viewport.test.tsx`);
      if (!fs.existsSync(testFile)) {
        for (const width of requiredWidths) {
          missing.push(`${component} — no viewport test file (needs ${width}px coverage)`);
        }
        continue;
      }
      const content = fs.readFileSync(testFile, 'utf-8');
      for (const width of requiredWidths) {
        // Detect any call to an expect*At helper at the required width
        if (!new RegExp(`At\\s*\\(\\s*${width}[^\\d]`).test(content)) {
          missing.push(`${component} — missing ${width}px assertion in ${component}.viewport.test.tsx`);
        }
      }
    }

    const errorMessage =
      missing.length > 0
        ? `
The following required components are missing viewport assertions at required widths.
Add an expect*At(width, ...) call for each missing entry:

${missing.map(m => `  ${m}`).join('\n')}

Required widths come from admin-ui/src/test/viewport-required.json.
        `.trim()
        : '';

    expect(missing, errorMessage).toEqual([]);
  });

  it('fails CI when a new operator component lacks a *.viewport.test.tsx file', () => {
    const componentsDir = path.join(__dirname, '../components');
    const files = fs.readdirSync(componentsDir);

    // All .tsx component files (exclude test files)
    const components = files
      .filter(f => f.endsWith('.tsx') && !f.endsWith('.test.tsx') && !f.endsWith('.viewport.test.tsx'))
      .map(f => f.replace('.tsx', ''))
      .sort();

    // All .viewport.test.tsx files
    const viewportTests = files
      .filter(f => f.endsWith('.viewport.test.tsx'))
      .map(f => f.replace('.viewport.test.tsx', ''))
      .sort();

    // Allowlist: components that are exempt from the viewport-test invariant.
    // Each entry is a utility/internal component that is never rendered standalone
    // on an operator page — overflow is handled by the parent that composes it.
    const exemptFromViewportTest = new Set([
      // Internal decorator component for error isolation; not operator-facing.
      'ErrorBoundary',
      // Pure presentation: renders markdown blocks, used in help/documentation
      // panels where overflow is already handled by parent scroll container.
      'MarkdownContent',
      // Utility component: renders chip columns in KanbanBoard's table; layout
      // controlled entirely by parent, not rendering standalone in operator pages.
      'ColumnsPicker',
      // Toast notifications: positioned fixed/absolutely, exempt from viewport
      // flow overflow checks; positioned via Tailwind fixed positioning.
      'Toasts',
      // Internal mapper: generates DOM from regression data array; not used
      // standalone on operator pages, used within parent containers that handle layout.
      'catchupRows',
      // Internal mapper: generates chip DOM from phase data; not standalone,
      // layout controlled by parent container.
      'regressionPhaseChips',
    ]);

    const missingTests = components
      .filter(c => !viewportTests.includes(c) && !exemptFromViewportTest.has(c))
      .sort();

    const errorMessage =
      missingTests.length > 0
        ? `
The following operator components are missing *.viewport.test.tsx files.
Add a viewport test file for each (or add to the exemption list with justification):

${missingTests.map(c => `  admin-ui/src/components/${c}.viewport.test.tsx`).join('\n')}

If a component should be exempt from viewport testing, add it to the
exemptFromViewportTest Set in this file with a code comment justifying why.
        `.trim()
        : '';

    expect(missingTests, errorMessage).toEqual([]);
  });
});
