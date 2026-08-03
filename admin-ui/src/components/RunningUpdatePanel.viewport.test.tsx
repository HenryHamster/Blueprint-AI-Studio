import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import RunningUpdatePanel from './RunningUpdatePanel';
import { apiFetch } from '../api';
import { expectNoOverflowAt, expectNoTextTruncationAt, expectMinTouchTargetAt } from '../test/viewport';

// RunningUpdatePanel imports apiFetch from ../api (transitively pulls in firebase).
vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
}));

const BASE_PROPS = {
  projectId: 'proj-1',
  document: null,
  acknowledgedAt: null,
  userLastActiveAt: null,
  addToast: () => {},
  onAcknowledged: () => {},
};

describe('RunningUpdatePanel — viewport regression', () => {
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

  test('empty state renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      host.style.height = '400px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<RunningUpdatePanel {...BASE_PROPS} />);
      });
    });
  });

  test('empty state text is not silently clipped at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      host.style.height = '400px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<RunningUpdatePanel {...BASE_PROPS} />);
      });
    });
  });

  test('populated document renders without horizontal overflow at 375px', async () => {
    const mdContent = `# Strategic Update\n\n## Current Focus\n\nThe pipeline is processing 12 active directions across 3 repositories. See the full dashboard at https://github.com/HenryHamster/BlueprintAIStudio/actions/runs/12345678901234567890/job/99887766554433221100 for the latest CI status.\n\n## Recent Activity\n\n- Merged 4 PRs improving test coverage in the authentication module\n- Identified a recurring regression in the CI workflow layer\n- Research cycle completed for performance optimization direction\n- Modified \`admin-ui/src/components/MetricsDashboardEfficiencySnapshotSection.tsx\` to fix the overflow\n\n## Code Sample\n\n\`\`\`typescript\nconst result = await apiFetch(\`/api/projects/\${projectId}/efficiency-snapshot\`);\nif (!result.ok) throw new Error(\`fetch failed: \${result.status}\`);\n\`\`\`\n\n## Next Steps\n\nScheduled architecture review in 2 cycles.`;

    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      host.style.height = '400px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <RunningUpdatePanel
            {...BASE_PROPS}
            document={mdContent}
            acknowledgedAt={new Date(Date.now() - 3_600_000).toISOString()}
            userLastActiveAt={new Date(Date.now() - 1_800_000).toISOString()}
          />,
        );
      });
    });
  });

  test('populated document text is not silently clipped at 375px', async () => {
    const mdContent = `# Running Update\n\nThis is the strategic agent's current-state snapshot of the project. It is rewritten each cycle.\n\nSee https://github.com/HenryHamster/BlueprintAIStudio/actions/runs/12345678901234567890/job/99887766554433221100 for CI history.\n\nKey file: \`admin-ui/src/components/MetricsDashboardEfficiencySnapshotSection.tsx\`\n\n\`\`\`\nnpm run build --workspace=admin-ui --if-present\n\`\`\``;

    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      host.style.height = '400px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<RunningUpdatePanel {...BASE_PROPS} document={mdContent} />);
      });
    });
  });

  test('Edit toggle reveals a textarea and Save issues a PUT with body+base', async () => {
    const mockFetch = vi.mocked(apiFetch);
    mockFetch.mockClear();
    const onEdited = vi.fn();
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <RunningUpdatePanel
          {...BASE_PROPS}
          readOnly={false}
          document="Original body"
          onEdited={onEdited}
        />,
      );
    });

    // No textarea until Edit is toggled.
    expect(host.querySelector('textarea')).toBeNull();

    const editButton = Array.from(host.querySelectorAll('button')).find(
      (b) => b.textContent?.trim() === 'Edit',
    )!;
    await act(async () => {
      editButton.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });

    const textarea = host.querySelector('textarea')!;
    expect(textarea).not.toBeNull();
    expect(textarea.value).toBe('Original body');

    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(
        HTMLTextAreaElement.prototype,
        'value',
      )!.set!;
      setter.call(textarea, 'Edited body');
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
    });

    const saveButton = Array.from(host.querySelectorAll('button')).find(
      (b) => b.textContent?.trim() === 'Save',
    )!;
    await act(async () => {
      saveButton.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });

    expect(mockFetch).toHaveBeenCalledWith(
      '/api/admin/projects/proj-1/running-update',
      expect.objectContaining({
        method: 'PUT',
        body: JSON.stringify({ body: 'Edited body', base: 'Original body' }),
      }),
    );
    expect(onEdited).toHaveBeenCalledWith('Edited body');
  });

  test('Save surfaces a 409 conflict without calling onEdited', async () => {
    const mockFetch = vi.mocked(apiFetch);
    mockFetch.mockClear();
    mockFetch.mockResolvedValueOnce(new Response('{}', { status: 409 }));
    const onEdited = vi.fn();
    const addToast = vi.fn();
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <RunningUpdatePanel
          {...BASE_PROPS}
          readOnly={false}
          document="Original body"
          addToast={addToast}
          onEdited={onEdited}
        />,
      );
    });

    const editButton = Array.from(host.querySelectorAll('button')).find(
      (b) => b.textContent?.trim() === 'Edit',
    )!;
    await act(async () => {
      editButton.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });
    const saveButton = Array.from(host.querySelectorAll('button')).find(
      (b) => b.textContent?.trim() === 'Save',
    )!;
    await act(async () => {
      saveButton.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });

    expect(onEdited).not.toHaveBeenCalled();
    expect(addToast).toHaveBeenCalledWith(expect.any(String), 'error');
  });

  test('reconciliation badge renders when an operator edit was preserved', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <RunningUpdatePanel
          {...BASE_PROPS}
          document="Reconciled body"
          reconciliation={{
            operator_wins_count: 2,
            last_operator_win_at: new Date(Date.now() - 3_600_000).toISOString(),
          }}
        />,
      );
    });

    expect(host.textContent).toContain('Your edit preserved');
    // Pluralized count surfaces the number of superseded agent proposals.
    expect(host.textContent).toContain('2 agent proposals superseded');
  });

  test('reconciliation badge is hidden when no operator edit was preserved', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <RunningUpdatePanel
          {...BASE_PROPS}
          document="Plain body"
          reconciliation={null}
        />,
      );
    });
    expect(host.textContent).not.toContain('Your edit preserved');

    // Also hidden when the count is explicitly zero.
    await act(async () => {
      r.render(
        <RunningUpdatePanel
          {...BASE_PROPS}
          document="Plain body"
          reconciliation={{ operator_wins_count: 0, last_operator_win_at: null }}
        />,
      );
    });
    expect(host.textContent).not.toContain('Your edit preserved');
  });

  test('Acknowledge button meets 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      host.style.height = '400px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <RunningUpdatePanel
            {...BASE_PROPS}
            readOnly={false}
            document="Test content"
            acknowledgedAt={new Date(Date.now() - 3_600_000).toISOString()}
          />,
        );
      });
    });
  });
});
