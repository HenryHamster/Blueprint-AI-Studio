import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { MarkdownContent } from './MarkdownContent';
import { expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';

const LONG_URL = 'https://github.com/HenryHamster/BlueprintAIStudio/actions/runs/12345678901234567890/job/99887766554433221100';
const LONG_INLINE_PATH = 'admin-ui/src/components/MetricsDashboardEfficiencySnapshotSection.tsx';

const RICH_MD = `# Heading

Paragraph with a long URL: ${LONG_URL}

Inline code path: \`${LONG_INLINE_PATH}\`

\`\`\`
npm run build --workspace=admin-ui --if-present --long-flag-that-makes-the-line-wide
\`\`\`

- List item one
- List item two
`;

describe('MarkdownContent — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => {
        r.unmount();
      });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  test('renders without horizontal overflow at 375px with long URLs and code', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<MarkdownContent content={RICH_MD} />);
      });
    });
  });

  test('text is not silently clipped at 375px with long URLs and code', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<MarkdownContent content={RICH_MD} />);
      });
    });
  });
});
