import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { DirectionCard } from './DirectionCard';
import type { Direction } from './DirectionsPanel';
import { expectNoOverflowAt, expectNoTextTruncationAt, expectMinTouchTargetAt } from '../test/viewport';

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  BarChart: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Bar: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  Cell: () => null,
  Tooltip: () => null,
}));

const richDirection: Direction = {
  id: 'dir-c762aab8',
  text: 'Establish a viewport regression gate for the operator dashboard so widgets render cleanly at 375px without horizontal overflow or silent text clipping.',
  priority: 'high',
  status: 'active',
  category: 'product',
  added_at: '2026-04-15T00:00:00Z',
  completion_criteria:
    'Every widget on DashboardHome has a viewport test at 375px and 768px and the change-time CI gate enforces them.',
  paths: [
    'admin-ui/src/components/DashboardHome.tsx',
    'admin-ui/src/components/DirectionCard.tsx',
    'admin-ui/src/components/RemediationFunnelSection.tsx',
  ],
  last_researched_at: '2026-05-07T08:00:00Z',
  research_history: [
    {
      timestamp: '2026-05-01T10:00:00Z',
      task_id: 'task-research-1',
      tasks_created: 4,
      finding_titles: [
        'Widgets without viewport tests have overflow risk on iPhone SE.',
      ],
      key_finding_descriptions: [
        'KanbanBoard already covered; DirectionCard, RemediationFunnel, FixLoop are not.',
      ],
      tasks_completed: 4,
      tasks_failed: 0,
      prs_merged: 4,
      prs_rejected: 0,
      autonomous_prs_merged: 4,
      assisted_prs_merged: 0,
      product_ratio: 0.75,
    },
    {
      timestamp: '2026-05-04T10:00:00Z',
      task_id: 'task-research-2',
      tasks_created: 6,
      finding_titles: ['Viewport gate misses widgets that are imported transitively'],
      key_finding_descriptions: ['Per-widget tests catch regressions that DashboardHome-level tests miss.'],
      tasks_completed: 6,
      tasks_failed: 1,
      prs_merged: 5,
      prs_rejected: 0,
      autonomous_prs_merged: 5,
      assisted_prs_merged: 0,
    },
  ],
  history_aggregate: {
    total_prs_merged: 9,
    total_prs_rejected: 0,
    total_tasks_failed: 1,
    total_autonomous_prs_merged: 9,
    total_assisted_prs_merged: 0,
    total_operator_actions: 2,
  },
  cost_budget_usd: 25.0,
  estimated_cost_usd: 12.4,
  budget_utilization_pct: 49.6,
  budget_exceeded: false,
  cost_per_merged_pr: 1.38,
  cost_per_merge_exceeds_threshold: false,
  cost_by_provider_model: {
    'anthropic|claude-opus-4-7': 9.8,
    'anthropic|claude-sonnet-4-6': 2.6,
  },
  // Generalized cross-type success — exercises the dual-metric "eval" badge and
  // the outcome-kind breakdown chip row in the dense card layout.
  generalized_successes: 12,
  generalized_health_rate: 0.8,
  knowledge_document_count: 7,
  outcome_kind_breakdown: {
    code_merge: { success: 9, neutral: 0, failure: 1 },
    research_quality: { success: 2, neutral: 1, failure: 0 },
    audit_findings: { success: 1, neutral: 0, failure: 0 },
  },
};

const stagedDirection: Direction = {
  id: 'dir-staged-42',
  text: 'Introduce a registrable task-type spine so genuinely new kinds of work can be added next version without code-change-to-PR special-casing.',
  priority: 'medium',
  status: 'staged',
  category: 'structural',
  added_at: '2026-06-01T00:00:00Z',
  staged_for_version: 42,
  staged_at: '2026-06-01T00:00:00Z',
  completion_criteria:
    'A new task type can be added by registering its run-shape, produces, needs, and evaluator-slot — proven end-to-end for one reference non-code type.',
};

const noopProps = {
  readOnly: false,
  statusKey: 'active',
  activeItems: [richDirection],
  editingId: null,
  editText: '',
  editPriority: 'high' as const,
  editStatus: 'active' as const,
  editCategory: 'product' as const,
  editPathTags: [] as string[],
  editPathInput: '',
  editCompletionCriteria: '',
  setEditingId: () => {},
  setEditText: () => {},
  setEditPriority: () => {},
  setEditStatus: () => {},
  setEditCategory: () => {},
  setEditPathTags: () => {},
  setEditPathInput: () => {},
  setEditCompletionCriteria: () => {},
  editSuccessEvidence: '',
  editRetirementCondition: '',
  setEditSuccessEvidence: () => {},
  setEditRetirementCondition: () => {},
  startEdit: () => {},
  updateDirection: async () => {},
  approveProposal: async () => {},
  rejectProposal: async () => {},
  reviveProposal: async () => {},
  reactivateDirection: async () => {},
  activateStagedDirection: async () => {},
  cancelStagedDirection: async () => {},
  unholdDirection: async () => {},
  markDirectionComplete: async () => {},
  quickToggleStatus: async () => {},
  deleteDirection: async () => {},
  applyDirectionUpdate: async () => {},
  dismissDirectionUpdate: async () => {},
  triggerResearch: async () => {},
  reorderDirections: async () => {},
  taskCounts: { 'dir-c762aab8': 3 },
  activeTasks: {
    'dir-c762aab8': [
      { id: 'task-1', status: 'IN_PROGRESS', title: 'feat: viewport tests for DashboardHome widgets at 375px breakpoint' },
    ],
  },
  cooldownMinutes: 30,
  researchingIds: {} as Record<string, boolean>,
  researchSuccessIds: {} as Record<string, boolean>,
  forceConfirmId: null,
  setForceConfirmId: () => {},
  historyOpenIds: {} as Record<string, boolean>,
  setHistoryOpenIds: () => {},
  onViewFindings: () => {},
  onNavigateToTask: () => {},
};

describe('DirectionCard — viewport regression', () => {
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

  test('rich direction renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<DirectionCard {...noopProps} direction={richDirection} />);
      });
    });
  });

  test('rich direction renders without horizontal overflow at 768px', async () => {
    await expectNoOverflowAt(768, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<DirectionCard {...noopProps} direction={richDirection} />);
      });
    });
  });

  test('rich direction renders without silent text truncation at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<DirectionCard {...noopProps} direction={richDirection} />);
      });
    });
  });

  // Existing buttons in DirectionCard.tsx that intentionally render below 44px
  // (icon-only chevrons, dense edit/research/delete chips) carry
  // `data-viewport-allow-small-target` and are skipped by the helper. Any
  // *new* button added without that attribute will be flagged as a regression.
  test('rich direction has no undocumented small touch targets at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<DirectionCard {...noopProps} direction={richDirection} />);
      });
    });
  });

  test('recent research history does not disable research without a refractory hold', async () => {
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<DirectionCard {...noopProps} direction={richDirection} />);
    });
    const researchButton = Array.from(host.querySelectorAll('button')).find(
      button => button.textContent === 'Research',
    ) as HTMLButtonElement | undefined;
    expect(researchButton).toBeTruthy();
    expect(researchButton?.disabled).toBe(false);
    expect(researchButton?.title).toBe('Trigger research for this direction');
    expect(host.querySelector('button[title="Force research (bypass cooldown)"]')).toBeNull();
    expect(host.textContent).toContain('Last researched');
  });

  test('staged direction renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<DirectionCard {...noopProps} statusKey="staged" direction={stagedDirection} />);
      });
    });
  });

  test('staged direction renders without silent text truncation at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<DirectionCard {...noopProps} statusKey="staged" direction={stagedDirection} />);
      });
    });
  });

  test('staged direction has no undocumented small touch targets at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<DirectionCard {...noopProps} statusKey="staged" direction={stagedDirection} />);
      });
    });
  });

  test('staged direction surfaces the version it is waiting on', async () => {
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<DirectionCard {...noopProps} statusKey="staged" direction={stagedDirection} />);
    });
    expect(host.textContent).toContain('Waiting for v42');
    expect(host.textContent).toContain('Activate now');
    expect(host.textContent).toContain('Cancel');
  });

  test('staged direction surfaces how long it has been waiting', async () => {
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<DirectionCard {...noopProps} statusKey="staged" direction={stagedDirection} />);
    });
    // Reuses timeAgo => "staged Nd ago" / "staged Nh Mm ago" depending on age.
    expect(host.textContent).toMatch(/staged .* ago/);
  });

  test('long-waiting staged direction is visually emphasized', async () => {
    const staleDirection: Direction = {
      ...stagedDirection,
      id: 'dir-staged-stale',
      // Far past the STAGED_STALE_DAYS threshold (and well before the run date).
      staged_at: '2026-01-01T00:00:00Z',
    };
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<DirectionCard {...noopProps} statusKey="staged" direction={staleDirection} />);
    });
    // The stale badge carries red emphasis classes the fresh badge does not.
    const staleBadge = Array.from(host.querySelectorAll('span')).find(
      el => el.textContent?.startsWith('staged') && el.className.includes('text-red-400'),
    );
    expect(staleBadge).toBeTruthy();
  });

  test('staged direction without a staging timestamp shows no waiting badge', async () => {
    const noTimestamp: Direction = { ...stagedDirection, staged_at: null };
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<DirectionCard {...noopProps} statusKey="staged" direction={noTimestamp} />);
    });
    expect(host.textContent).not.toMatch(/staged .* ago/);
    // The version-waiting chip still renders — only the age badge is gated.
    expect(host.textContent).toContain('Waiting for v42');
  });

  test('edit mode offers an enabled Staged option when a version is open', async () => {
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <DirectionCard
          {...noopProps}
          direction={richDirection}
          editingId={richDirection.id}
          editStatus="staged"
          stageTargetVersion={7}
        />,
      );
    });
    const stagedOption = host.querySelector('option[value="staged"]') as HTMLOptionElement | null;
    if (!stagedOption) throw new Error('expected a Staged option in edit mode');
    expect(stagedOption.disabled).toBe(false);
    expect(stagedOption.textContent).toContain('Staged');
    // With staged selected, the target version is surfaced in the hint below the row.
    expect(host.textContent).toContain('after v7 promotes');
    expect(host.textContent).not.toContain('No open release version to stage against');
  });

  test('edit mode disables the Staged option and explains why when no version is open', async () => {
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <DirectionCard
          {...noopProps}
          direction={richDirection}
          editingId={richDirection.id}
          stageTargetVersion={null}
        />,
      );
    });
    const stagedOption = host.querySelector('option[value="staged"]') as HTMLOptionElement | null;
    if (!stagedOption) throw new Error('expected a Staged option in edit mode');
    expect(stagedOption.disabled).toBe(true);
    expect(host.textContent).toContain('No open release version to stage against');
  });

  test('edit row renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <DirectionCard
            {...noopProps}
            direction={richDirection}
            editingId={richDirection.id}
            stageTargetVersion={7}
          />,
        );
      });
    });
  });

  test('edit row has no undocumented small touch targets at 768px', async () => {
    await expectMinTouchTargetAt(768, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <DirectionCard
            {...noopProps}
            direction={richDirection}
            editingId={richDirection.id}
            stageTargetVersion={7}
          />,
        );
      });
    });
  });

  test('churn-retirement banner renders without horizontal overflow at 1280px', async () => {
    const churnDirection: Direction = {
      ...richDirection,
      id: 'dir-churn-rec',
      status: 'paused',
      churn_retirement_recommended: true,
      churn_retirement_recommended_reason: 'Direction has exceeded max pause cycles with no merges in window.',
      churn_retirement_recommended_at: '2026-06-01T00:00:00Z',
    };
    await expectNoOverflowAt(1280, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <DirectionCard
            {...noopProps}
            statusKey="paused"
            direction={churnDirection}
            decideChurnRetirement={async () => {}}
          />,
        );
      });
    });
  });

  test('churn-retirement banner has no undocumented small touch targets at 1280px', async () => {
    const churnDirection: Direction = {
      ...richDirection,
      id: 'dir-churn-rec-targets',
      status: 'paused',
      churn_retirement_recommended: true,
      churn_retirement_recommended_reason: 'Exceeded max pause cycles.',
      churn_retirement_recommended_at: '2026-06-01T00:00:00Z',
    };
    await expectMinTouchTargetAt(1280, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <DirectionCard
            {...noopProps}
            statusKey="paused"
            direction={churnDirection}
            decideChurnRetirement={async () => {}}
          />,
        );
      });
    });
  });
});
