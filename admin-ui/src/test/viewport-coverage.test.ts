import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';

describe('viewport test coverage invariant', () => {
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
      // Sub-component of KanbanBoard; not rendered standalone, layout delegated.
      'KanbanColumn',
      // Toast notifications: positioned fixed/absolutely, exempt from viewport
      // flow overflow checks; positioned via Tailwind fixed positioning.
      'Toasts',
      // Internal mapper: generates DOM from regression data array; not used
      // standalone on operator pages, used within parent containers that handle layout.
      'catchupRows',
      // Internal mapper: generates chip DOM from phase data; not standalone,
      // layout controlled by parent container.
      'regressionPhaseChips',
      // Utility component: renders member list rows inside ProjectMembers table;
      // not standalone. Defer viewport test pending table layout clarification.
      'ProjectMembers',
      // Internal component: renders analysis metadata inside ResearchBrowser;
      // layout controlled by parent. Defer pending parent viewport coverage.
      'ProblemModelViewer',
      // Internal component: renders editor inside ResearchBrowser; layout
      // controlled by parent. Defer pending parent viewport coverage.
      'ProblemModelEditor',
      // Renders status row in MetricsDashboard/HeaderBar; layout delegated
      // to parent. Defer viewport test pending parent clarification.
      'StatusBar',
      // Utility component: renders status row in strategic direction contexts;
      // layout handled by parent. Defer viewport test pending parent viewport gate.
      'StrategicStatus',
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
