import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterAll, afterEach, beforeAll, describe, expect, test, vi } from 'vitest';
import {
  TaskComputeJobsSection,
  ResearchOriginSection,
  SalvageActivitySection,
  AdversarialGateDiagnosticSection,
  BlockedNeedSection,
  KnowledgeDocumentSection,
  EvaluationSummarySection,
  AcceptanceEvidenceSection,
  SystemMetricsSection,
  BlockerSection,
  ParkedSection,
  TaskConfigSection,
  MemoReasoningSection,
  InvestigationReportSection,
  MergeTestGateSection,
  StatusLogSection,
  HistorySection,
  CodeOutputSection,
  StructuralSerializationSection,
  BaseRecutHoldSection,
  ChildTasksSection,
  ViewportScreenshotsPanel,
} from './TaskDetailDrawerSections';
import { fetchComputeJobs, fetchKnowledgeDocument } from '../api';
import type { ComputeJob } from '../api';
import type { Task, AdversarialGateDiagnostic, TaskEvaluationSummary } from '../types/task';

vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  signOut: vi.fn(),
  getIdToken: vi.fn(async () => 'test-token'),
  getIdTokenAfterAuthRestore: vi.fn(async () => 'test-token'),
  onAuthChange: vi.fn(() => () => {}),
}));

// These section tests verify when fetched document content is surfaced, not
// the independently-covered lazy Markdown renderer. Keeping this boundary
// synchronous avoids making the fetch-state assertion depend on code-split
// module scheduling under the full Vitest suite.
vi.mock('./MarkdownContent', () => ({
  MarkdownContent: ({ content }: { content: string }) => <div>{content}</div>,
}));

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    authGet: vi.fn(async () => new Response(JSON.stringify({}), { status: 200 })),
    apiFetch: vi.fn(async () => new Response(JSON.stringify({}), { status: 200 })),
    API_BASE_URL: 'http://localhost:8000',
    fetchComputeJobs: vi.fn(async () => ({
      jobs: [], hasMore: false,
      summary: { total_jobs: 0, active_jobs: 0, terminal_jobs: 0, total_spend_usd: 0, outcome_breakdown: {}, criterion_measured: 0, criterion_met: 0 },
      limit: 50, offset: 0,
    })),
    fetchKnowledgeDocument: vi.fn(async () => ({ id: 'doc-1', body: 'Document body content' })),
    resolveViewportShotDownloadUrl: vi.fn(async () => 'https://example.com/shot.png'),
    resolveViewportFailureDownloadUrl: vi.fn(async () => 'https://example.com/failure.png'),
  });
});

// ---------------------------------------------------------------------------
// Factory helpers
// ---------------------------------------------------------------------------

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 'task-1',
    title: 'Test task',
    description: 'A test task',
    status: 'COMPLETED',
    task_type: 'code' as Task['task_type'],
    created_at: '2026-07-01T12:00:00Z',
    created_by: 'USER',
    requires_approval: false,
    project_id: 'proj-1',
    payloads: [{}],
    ...overrides,
  } as Task;
}

// ---------------------------------------------------------------------------
// Render helper
// ---------------------------------------------------------------------------

const mountedRoots: Array<{ host: HTMLElement; root: Root }> = [];
const reactActEnvironment = globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean };
const previousActEnvironment = reactActEnvironment.IS_REACT_ACT_ENVIRONMENT;

beforeAll(() => {
  reactActEnvironment.IS_REACT_ACT_ENVIRONMENT = true;
});

afterEach(() => {
  const mounted = mountedRoots.splice(0);
  act(() => {
    mounted.forEach(({ root }) => root.unmount());
  });
  mounted.forEach(({ host }) => host.remove());
  vi.clearAllMocks();
});

afterAll(() => {
  reactActEnvironment.IS_REACT_ACT_ENVIRONMENT = previousActEnvironment;
});

function render(element: React.ReactElement): HTMLElement {
  const host = document.createElement('div');
  document.body.appendChild(host);
  const root = createRoot(host);
  mountedRoots.push({ host, root });
  act(() => { root.render(element); });
  return host;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('ResearchOriginSection', () => {
  test('renders nothing when research_task_id is missing', () => {
    const task = makeTask();
    const host = render(<ResearchOriginSection task={task} />);
    expect(host.textContent).toBe('');
  });

  test('renders research_task_id and finding details', () => {
    const task = makeTask({
      context: { research_task_id: 'research-1', finding_index: 0 },
    } as unknown as Partial<Task>);
    const parentTask = {
      id: 'research-1', title: 'Parent research', research_findings: [
        { title: 'Finding title', scope: 'narrow', risk: 'medium', direction_id: 'dir-1' },
      ],
    } as unknown as Task;
    const host = render(<ResearchOriginSection task={task} tasks={[parentTask]} />);
    expect(host.textContent).toContain('Research Origin');
    expect(host.textContent).toContain('research-1');
    expect(host.textContent).toContain('Finding title');
    expect(host.textContent).toContain('narrow/medium');
  });

  test('renders spawn gate: unchallenged', () => {
    const task = makeTask({
      context: { research_task_id: 'research-2', research_spawn_gate_challenged: false },
    } as unknown as Partial<Task>);
    const host = render(<ResearchOriginSection task={task} />);
    expect(host.textContent).toContain('unchallenged');
  });

  test('renders spawn gate: challenged-pass', () => {
    const task = makeTask({
      context: {
        research_task_id: 'research-3',
        research_spawn_gate_challenged: true,
        research_spawn_gate_explicit: true,
        research_spawn_gate_reason: 'All assertions hold',
      },
    } as unknown as Partial<Task>);
    const host = render(<ResearchOriginSection task={task} />);
    expect(host.textContent).toContain('challenged — pass');
    expect(host.textContent).toContain('All assertions hold');
  });

  test('renders spawn gate: fail-open-pass', () => {
    const task = makeTask({
      context: {
        research_task_id: 'research-4',
        research_spawn_gate_challenged: true,
        research_spawn_gate_explicit: false,
        research_spawn_gate_reason: 'Degraded but acceptable',
      },
    } as unknown as Partial<Task>);
    const host = render(<ResearchOriginSection task={task} />);
    expect(host.textContent).toContain('fail-open pass');
    expect(host.textContent).toContain('Degraded but acceptable');
  });

  test('renders navigable research task link when onNavigateToTask provided', () => {
    const onNav = vi.fn();
    const task = makeTask({ context: { research_task_id: 'research-5' } } as unknown as Partial<Task>);
    const host = render(<ResearchOriginSection task={task} onNavigateToTask={onNav} />);
    const btn = host.querySelector('button');
    expect(btn).not.toBeNull();
    btn!.click();
    expect(onNav).toHaveBeenCalledWith('research-5');
  });

  test('renders CopyableId when no onNavigateToTask', () => {
    const task = makeTask({ context: { research_task_id: 'research-6' } } as unknown as Partial<Task>);
    const host = render(<ResearchOriginSection task={task} />);
    expect(host.textContent).toContain('research-6');
    // CopyableId renders a button for copy, not a navigation callback
    expect(host.textContent).not.toContain('No research origin');
  });
});

describe('SalvageActivitySection', () => {
  test('renders nothing when salvage_activity missing', () => {
    const host = render(<SalvageActivitySection task={makeTask()} />);
    expect(host.textContent).toBe('');
  });

  test('renders salvage metadata with navigable task link', () => {
    const onNav = vi.fn();
    const task = makeTask({
      context: {
        salvage_activity: { salvage_task_id: 'salvage-1', salvaged_count: 3, salvaged_at: '2026-07-01T13:00:00Z' },
      },
    } as unknown as Partial<Task>);
    const host = render(<SalvageActivitySection task={task} onNavigateToTask={onNav} />);
    expect(host.textContent).toContain('Salvage Activity');
    expect(host.textContent).toContain('3 findings');
    expect(host.textContent).toContain('salvage-1');
    host.querySelector('button')?.click();
    expect(onNav).toHaveBeenCalledWith('salvage-1');
  });

  test('renders singular "finding" when count is 1', () => {
    const task = makeTask({
      context: { salvage_activity: { salvage_task_id: 'salvage-2', salvaged_count: 1 } },
    } as unknown as Partial<Task>);
    const host = render(<SalvageActivitySection task={task} />);
    expect(host.textContent).toContain('1 finding');
  });
});

describe('AdversarialGateDiagnosticSection', () => {
  const baseDiag: AdversarialGateDiagnostic = {
    challenger_task_id: 'challenger-1',
    completion_state: 'completed',
    parse_state: 'ok',
    challenged_count: 5,
    unresolved_count: 1,
    parsed_verdict_count: 4,
  };

  test('renders all diagnostic fields', () => {
    const host = render(<AdversarialGateDiagnosticSection diag={baseDiag} />);
    expect(host.textContent).toContain('Adversarial Gate Diagnostic');
    expect(host.textContent).toContain('challenger-1');
    expect(host.textContent).toContain('completed');
    expect(host.textContent).toContain('ok');
    expect(host.textContent).toContain('5 findings');
    expect(host.textContent).toContain('1 no verdict');
    expect(host.textContent).toContain('4 explicit');
  });

  test('renders navigable challenger task link', () => {
    const onNav = vi.fn();
    const host = render(<AdversarialGateDiagnosticSection diag={baseDiag} onNavigateToTask={onNav} />);
    host.querySelector('button')?.click();
    expect(onNav).toHaveBeenCalledWith('challenger-1');
  });

  test('renders VM fields when present', () => {
    const diag: AdversarialGateDiagnostic = {
      ...baseDiag,
      vm_image_name: 'custom-image-v2',
      vm_image_build_ts: 1719800000,
      vm_runtime_fingerprint: 'abc123def456',
      vm_source_sha: 'deadbeefcafe',
    };
    const host = render(<AdversarialGateDiagnosticSection diag={diag} />);
    expect(host.textContent).toContain('custom-image-v2');
    expect(host.textContent).toContain('abc123def456'); // truncated fingerprint
    expect(host.textContent).toContain('deadbeefcafe'); // truncated SHA
  });
});

describe('BlockedNeedSection', () => {
  test('renders nothing when need is falsy', () => {
    const host = render(<BlockedNeedSection need={undefined} />);
    expect(host.textContent).toBe('');
  });

  test('renders nothing when kind or ref missing', () => {
    const host = render(<BlockedNeedSection need={{ kind: 'task' }} />);
    expect(host.textContent).toBe('');
    const host2 = render(<BlockedNeedSection need={{ ref: 'ref-1' }} />);
    expect(host2.textContent).toBe('');
  });

  test('renders task ref with navigable title', () => {
    const onNav = vi.fn();
    const tasks = [{ id: 'task-blocked-1', title: 'Blocked dependency task' }] as Task[];
    const host = render(
      <BlockedNeedSection need={{ kind: 'task', ref: 'task-blocked-1' }} tasks={tasks} onNavigateToTask={onNav} />,
    );
    expect(host.textContent).toContain('Waiting for task');
    expect(host.textContent).toContain('Blocked dependency task');
    host.querySelector('button')?.click();
    expect(onNav).toHaveBeenCalledWith('task-blocked-1');
  });

  test('renders non-task ref as plain text', () => {
    const host = render(<BlockedNeedSection need={{ kind: 'service', ref: 'github-api' }} />);
    expect(host.textContent).toContain('Waiting for service');
    expect(host.textContent).toContain('github-api');
  });

  test('renders optional label', () => {
    const host = render(<BlockedNeedSection need={{ kind: 'task', ref: 't-1', label: 'Awaiting CI' }} />);
    expect(host.textContent).toContain('Awaiting CI');
  });

  test('truncates long titles', () => {
    const longTitle = 'A'.repeat(60);
    const tasks = [{ id: 't-1', title: longTitle }] as Task[];
    const host = render(<BlockedNeedSection need={{ kind: 'task', ref: 't-1' }} tasks={tasks} />);
    expect(host.textContent).toContain('…');
  });
});

describe('KnowledgeDocumentSection', () => {
  test('renders nothing when no knowledge document', () => {
    const host = render(<KnowledgeDocumentSection task={makeTask()} />);
    expect(host.textContent).toBe('');
  });

  test('renders document title and summary', () => {
    const task = makeTask({
      plan_output: {
        knowledge_document: { id: 'doc-1', title: 'Research doc', summary: 'Key findings summary' },
      },
    } as unknown as Partial<Task>);
    const host = render(<KnowledgeDocumentSection task={task} />);
    expect(host.textContent).toContain('Knowledge Document');
    expect(host.textContent).toContain('Research doc');
    expect(host.textContent).toContain('Key findings summary');
  });

  test('renders forward quality verdict (passed)', () => {
    const task = makeTask({
      plan_output: {
        knowledge_document: {
          id: 'doc-2', title: 'Quality doc',
          forward_quality_verdict: { passed: true, reason: 'All criteria met' },
        },
      },
    } as unknown as Partial<Task>);
    const host = render(<KnowledgeDocumentSection task={task} />);
    expect(host.textContent).toContain('Forward quality: passed');
  });

  test('renders forward quality verdict (failed)', () => {
    const task = makeTask({
      plan_output: {
        knowledge_document: {
          id: 'doc-3', title: 'Failed doc',
          forward_quality_verdict: { passed: false, reason: 'Missing evidence' },
        },
      },
    } as unknown as Partial<Task>);
    const host = render(<KnowledgeDocumentSection task={task} />);
    expect(host.textContent).toContain('Forward quality: failed');
  });

  test('renders analysis_mode badge', () => {
    const task = makeTask({
      plan_output: {
        knowledge_document: { id: 'doc-4', title: 'Mode doc', analysis_mode: 'llm' },
      },
    } as unknown as Partial<Task>);
    const host = render(<KnowledgeDocumentSection task={task} />);
    expect(host.textContent).toContain('llm');
  });

  test('renders compute acceptance badge when source_kind is compute_experiment', () => {
    const task = makeTask({
      plan_output: {
        knowledge_document: {
          id: 'doc-5', title: 'Acceptance doc', source_kind: 'compute_experiment',
          compute_acceptance_grade: 'acceptance', compute_acceptance_reason: 'llm_artifact_cited',
          analysis_mode: 'llm', unavailable_cause: null, analysis_fallback_reason: null,
        },
      },
    } as unknown as Partial<Task>);
    const host = render(<KnowledgeDocumentSection task={task} />);
    expect(host.textContent).toContain('Acceptance');
  });

  test('renders measured_only badge when grade is measured_only', () => {
    const task = makeTask({
      plan_output: {
        knowledge_document: {
          id: 'doc-6', title: 'Measured doc', source_kind: 'compute_experiment',
          compute_acceptance_grade: 'measured_only', compute_acceptance_reason: 'no_source_citations',
          analysis_mode: 'llm', unavailable_cause: null, analysis_fallback_reason: null,
        },
      },
    } as unknown as Partial<Task>);
    const host = render(<KnowledgeDocumentSection task={task} />);
    expect(host.textContent).toContain('Measured only');
  });

  test('toggles document body on click', async () => {
    let resolveDocument!: (document: { id: string; body: string }) => void;
    const fullDocument = new Promise<{ id: string; body: string }>((resolve) => {
      resolveDocument = resolve;
    });
    vi.mocked(fetchKnowledgeDocument).mockReturnValueOnce(fullDocument as never);
    const task = makeTask({
      project_id: 'proj-1',
      plan_output: {
        knowledge_document: { id: 'doc-body', title: 'Body doc' },
      },
    } as unknown as Partial<Task>);
    const host = render(<KnowledgeDocumentSection task={task} />);
    // Click to expand
    const btn = host.querySelector('button');
    expect(btn).not.toBeNull();
    expect(host.textContent).toContain('View document');
    act(() => {
      btn!.click();
    });
    await act(async () => {
      resolveDocument({ id: 'doc-body', body: 'Document body content' });
      await fullDocument;
    });
    expect(fetchKnowledgeDocument).toHaveBeenCalledWith('proj-1', 'doc-body');
    await vi.waitFor(() => {
      expect(host.textContent).toContain('Document body content');
    });
    expect(host.textContent).toContain('Hide document');
  });
});

describe('EvaluationSummarySection', () => {
  const baseSummary: TaskEvaluationSummary = {
    status: 'completed',
    verdict: 'positive',
    evidence: [{ label: 'Code quality', value: 'Good' }],
    highlights: [{ label: 'Strength', value: 'Clean architecture' }],
  };

  test('renders status and verdict', () => {
    const host = render(<EvaluationSummarySection summary={baseSummary} />);
    expect(host.textContent).toContain('Evaluation Summary');
    expect(host.textContent).toContain('completed');
    expect(host.textContent).toContain('Positive');
  });

  test('renders evidence items', () => {
    const host = render(<EvaluationSummarySection summary={baseSummary} />);
    expect(host.textContent).toContain('Code quality');
    expect(host.textContent).toContain('Good');
  });

  test('renders highlights', () => {
    const host = render(<EvaluationSummarySection summary={baseSummary} />);
    expect(host.textContent).toContain('Strength');
    expect(host.textContent).toContain('Clean architecture');
  });

  test('handles empty evidence and highlights', () => {
    const summary: TaskEvaluationSummary = { status: 'pending', verdict: 'neutral' };
    const host = render(<EvaluationSummarySection summary={summary} />);
    expect(host.textContent).toContain('pending');
  });
});

describe('AcceptanceEvidenceSection', () => {
  test('renders nothing when no verdict', () => {
    const host = render(<AcceptanceEvidenceSection task={makeTask()} />);
    expect(host.textContent).toBe('');
  });

  test('renders proven verdict', () => {
    const task = makeTask({
      context: { code_acceptance_verdict: { proven: true, satisfied: false, kind: 'visual', reason: 'Matches spec' } },
    } as unknown as Partial<Task>);
    const host = render(<AcceptanceEvidenceSection task={task} />);
    expect(host.textContent).toContain('proven');
    expect(host.textContent).toContain('visual');
    expect(host.textContent).toContain('Matches spec');
  });

  test('renders satisfied verdict', () => {
    const task = makeTask({
      context: { code_acceptance_verdict: { proven: false, satisfied: true, reason: 'Acceptable trade-off' } },
    } as unknown as Partial<Task>);
    const host = render(<AcceptanceEvidenceSection task={task} />);
    expect(host.textContent).toContain('satisfied');
  });

  test('renders fail-closed verdict', () => {
    const task = makeTask({
      context: { code_acceptance_verdict: { proven: false, satisfied: false, reason: 'Does not compile' } },
    } as unknown as Partial<Task>);
    const host = render(<AcceptanceEvidenceSection task={task} />);
    expect(host.textContent).toContain('fail-closed');
  });
});

describe('SystemMetricsSection', () => {
  test('renders nothing when metrics missing', () => {
    const host = render(<SystemMetricsSection task={makeTask()} />);
    expect(host.textContent).toBe('');
  });

  test('renders cgroup memory with color thresholds', () => {
    const task = makeTask({
      last_metrics_json: { cgroup_mb: 512, cgroup_limit_mb: 1024 },
    } as unknown as Partial<Task>);
    const host = render(<SystemMetricsSection task={task} />);
    expect(host.textContent).toContain('System Metrics');
    expect(host.textContent).toContain('512 MB / 1024 MB');
  });

  test('renders RSS and heap metrics', () => {
    const task = makeTask({
      last_metrics_json: { rss_mb: 256, heap_used_mb: 128 },
    } as unknown as Partial<Task>);
    const host = render(<SystemMetricsSection task={task} />);
    expect(host.textContent).toContain('256 MB');
    expect(host.textContent).toContain('128 MB');
  });

  test('renders disk metrics', () => {
    const task = makeTask({
      last_metrics_json: { disk_free_mb: 5000, disk_total_mb: 10000 },
    } as unknown as Partial<Task>);
    const host = render(<SystemMetricsSection task={task} />);
    expect(host.textContent).toContain('5000.0 MB used');
  });
});

describe('BlockerSection', () => {
  test('renders nothing when no blocker data present', () => {
    const host = render(<BlockerSection task={makeTask()} now30={Date.now()} />);
    expect(host.textContent).toBe('');
  });

  test('renders blocker kind and reason', () => {
    const task = makeTask({
      status: 'BLOCKED', blocker_kind: 'external', blocked_reason: 'Waiting for API key',
      blocked_at: '2026-07-01T14:00:00Z', recheck_after: new Date(Date.now() + 120000).toISOString(),
    } as unknown as Partial<Task>);
    const host = render(<BlockerSection task={task} now30={Date.now()} />);
    expect(host.textContent).toContain('Blocker');
    expect(host.textContent).toContain('external');
    expect(host.textContent).toContain('Waiting for API key');
  });

  test('shows recheck due indicator', () => {
    const task = makeTask({
      status: 'BLOCKED', recheck_after: new Date(Date.now() - 60000).toISOString(),
    } as unknown as Partial<Task>);
    const host = render(<BlockerSection task={task} now30={Date.now()} />);
    expect(host.textContent).toContain('recheck due');
  });
});

describe('ParkedSection', () => {
  test('renders nothing when no park data', () => {
    const host = render(<ParkedSection task={makeTask()} />);
    expect(host.textContent).toBe('');
  });

  test('renders park reason', () => {
    const task = makeTask({
      status: 'PARKED', park_reason: 'Waiting for dependency', parked_at: '2026-07-01T15:00:00Z',
    } as unknown as Partial<Task>);
    const host = render(<ParkedSection task={task} />);
    expect(host.textContent).toContain('Parked');
    expect(host.textContent).toContain('Waiting for dependency');
  });

  test('renders wake registration details', () => {
    const task = makeTask({
      status: 'PARKED', park_reason: 'Awaiting signal',
      wake_registration: {
        wake_source_kind: 'webhook', wake_source_ref: 'hook-1',
        delivery_state: 'delivered', fired_at: '2026-07-01T16:00:00Z',
        deadline_at: '2026-07-02T16:00:00Z',
        result_state: 'success', result_detail: 'Completed',
      },
    } as unknown as Partial<Task>);
    const host = render(<ParkedSection task={task} />);
    expect(host.textContent).toContain('webhook');
    expect(host.textContent).toContain('hook-1');
    expect(host.textContent).toContain('Delivered');
    expect(host.textContent).toContain('success: Completed');
  });
});

describe('TaskConfigSection', () => {
  test('renders nothing when no payload', () => {
    const task = makeTask({ payloads: undefined } as unknown as Partial<Task>);
    const host = render(<TaskConfigSection task={task} />);
    expect(host.textContent).toBe('');
  });

  test('renders prompt, base branch, PR number, and branch', () => {
    const task = makeTask({
      payloads: [{ prompt: 'Do the thing', base_branch: 'main', pr_number: 42, branch: 'feature/foo' }],
      repo_url: 'https://github.com/test/repo',
    } as unknown as Partial<Task>);
    const host = render(<TaskConfigSection task={task} />);
    expect(host.textContent).toContain('Task Config');
    expect(host.textContent).toContain('Do the thing');
    expect(host.textContent).toContain('main');
    expect(host.textContent).toContain('#42');
    expect(host.textContent).toContain('feature/foo');
  });

  test('renders nothing when all optional fields are missing', () => {
    const task = makeTask({ payloads: [{ unrelated: true }] } as unknown as Partial<Task>);
    const host = render(<TaskConfigSection task={task} />);
    expect(host.textContent).toBe('');
  });
});

describe('MemoReasoningSection', () => {
  test('renders nothing when no memo', () => {
    const host = render(<MemoReasoningSection task={makeTask()} />);
    expect(host.textContent).toBe('');
  });

  test('renders summary, reasoning, tags, and citations', () => {
    const task = makeTask({
      plan_output: {
        memo: {
          summary: 'Design summary', reasoning: 'Because of X',
          tags: ['performance', 'security'], citations: ['task:t-1', 'direction:d-1', 'https://example.com'],
        },
      },
    } as unknown as Partial<Task>);
    const host = render(<MemoReasoningSection task={task} />);
    expect(host.textContent).toContain('Memo Reasoning');
    expect(host.textContent).toContain('Design summary');
    expect(host.textContent).toContain('Because of X');
    expect(host.textContent).toContain('performance');
    expect(host.textContent).toContain('t-1');
    expect(host.textContent).toContain('direction:d-1');
    expect(host.textContent).toContain('https://example.com');
  });

  test('renders navigable task citations', () => {
    const onNav = vi.fn();
    const task = makeTask({
      plan_output: { memo: { citations: ['task:navigate-me'] } },
    } as unknown as Partial<Task>);
    const host = render(<MemoReasoningSection task={task} onNavigateToTask={onNav} />);
    host.querySelector('button')?.click();
    expect(onNav).toHaveBeenCalledWith('navigate-me');
  });
});

describe('InvestigationReportSection', () => {
  test('renders nothing when not investigate type', () => {
    const host = render(<InvestigationReportSection task={makeTask()} />);
    expect(host.textContent).toBe('');
  });

  test('renders investigation report details', () => {
    const task = makeTask({
      task_type: 'investigate' as Task['task_type'],
      plan_output: {
        summary: 'Investigated crash', root_cause: 'Null pointer',
        severity: 'high', confidence: 'medium',
        affected_files: [{ path: 'src/main.ts', issue: 'Missing null check' }],
        recommended_fix: 'Add null guard',
      },
    } as unknown as Partial<Task>);
    const host = render(<InvestigationReportSection task={task} />);
    expect(host.textContent).toContain('Investigation Report');
    expect(host.textContent).toContain('high');
    expect(host.textContent).toContain('medium');
    expect(host.textContent).toContain('Investigated crash');
    expect(host.textContent).toContain('Null pointer');
    expect(host.textContent).toContain('src/main.ts');
    expect(host.textContent).toContain('Missing null check');
    expect(host.textContent).toContain('Add null guard');
  });

  test('renders report from investigation artifacts', () => {
    const task = makeTask({
      task_type: 'code' as Task['task_type'],
      artifacts: [{
        artifact_type: 'INVESTIGATION_REPORT',
        storage_uri: 'gs://bucket/report.json',
        iteration: 1,
        created_at: '2026-07-01T12:00:00Z',
      }],
      plan_output: { summary: 'Artifact report', severity: 'low' },
    } as unknown as Partial<Task>);
    const host = render(<InvestigationReportSection task={task} />);
    expect(host.textContent).toContain('low');
    expect(host.textContent).toContain('Artifact report');
  });
});

describe('StatusLogSection', () => {
  test('renders nothing when status_log is empty', () => {
    const host = render(<StatusLogSection task={makeTask({ status_log: [] })} />);
    expect(host.textContent).toBe('');
  });

  test('renders status log entries', () => {
    const task = makeTask({
      status_log: [
        { t: '2026-07-01T12:00:00Z', msg: 'Status changed: IN_PROGRESS' },
        { t: '2026-07-01T13:00:00Z', msg: 'Processing data' },
      ],
    } as unknown as Partial<Task>);
    const host = render(<StatusLogSection task={task} />);
    expect(host.textContent).toContain('Status Log');
    expect(host.textContent).toContain('Status changed: IN_PROGRESS');
    expect(host.textContent).toContain('Processing data');
  });
});

describe('HistorySection', () => {
  test('renders nothing when history is empty', () => {
    const host = render(<HistorySection task={makeTask({ history: [] })} />);
    expect(host.textContent).toBe('');
  });

  test('renders history entries with details', () => {
    const task = makeTask({
      history: [
        { timestamp: '2026-07-01T12:00:00Z', action: 'Created', details: { by: 'system' } },
        { timestamp: '2026-07-01T13:00:00Z', action: 'Updated', details: { field: 'status' } },
      ],
    } as unknown as Partial<Task>);
    const host = render(<HistorySection task={task} />);
    expect(host.textContent).toContain('History');
    expect(host.textContent).toContain('Created');
    expect(host.textContent).toContain('Updated');
  });
});

describe('CodeOutputSection', () => {
  test('returns null with no artifacts and no PR/commit/repo data', () => {
    const host = render(<CodeOutputSection task={makeTask()} />);
    expect(host.textContent).toBe('');
  });

  test('renders PR URL artifacts', () => {
    const task = makeTask({
      artifacts: [{
        artifact_type: 'PULL_REQUEST', pr_url: 'https://github.com/test/repo/pull/1',
        iteration: 1, created_at: '2026-07-01T12:00:00Z',
      }],
    } as unknown as Partial<Task>);
    const host = render(<CodeOutputSection task={task} />);
    expect(host.textContent).toContain('Code Output');
    expect(host.textContent).toContain('https://github.com/test/repo/pull/1');
  });

  test('renders commit hash and repo URL', () => {
    const task = makeTask({
      commit_hash: 'abc123def456',
      repo_url: 'https://github.com/test/repo',
    } as unknown as Partial<Task>);
    const host = render(<CodeOutputSection task={task} />);
    expect(host.textContent).toContain('abc123def456');
  });

  test('renders storage artifacts with type label', () => {
    const task = makeTask({
      artifacts: [{
        artifact_type: 'TEST_RESULT', storage_uri: 'gs://bucket/test-results.json',
        iteration: 1, created_at: '2026-07-01T12:00:00Z',
      }],
    } as unknown as Partial<Task>);
    const host = render(<CodeOutputSection task={task} />);
    expect(host.textContent).toContain('Test Result');
    expect(host.textContent).toContain('gs://bucket/test-results.json');
  });

  test('renders multi-iteration artifacts grouped', () => {
    const task = makeTask({
      artifacts: [
        { artifact_type: 'PULL_REQUEST', pr_url: 'https://github.com/test/repo/pull/1', iteration: 1 },
        { artifact_type: 'PULL_REQUEST', pr_url: 'https://github.com/test/repo/pull/2', iteration: 2 },
      ],
    } as unknown as Partial<Task>);
    const host = render(<CodeOutputSection task={task} />);
    expect(host.textContent).toContain('Iteration 1');
    expect(host.textContent).toContain('Iteration 2');
  });

  test('renders fallback repo link for research task types', () => {
    const task = makeTask({
      task_type: 'research' as Task['task_type'],
      repo_url: 'https://github.com/test/repo',
    } as unknown as Partial<Task>);
    const host = render(<CodeOutputSection task={task} />);
    expect(host.textContent).toContain('Code Output');
    expect(host.textContent).toContain('View Pull Requests');
  });
});

describe('StructuralSerializationSection', () => {
  test('renders nothing when no foundation_converged', () => {
    const host = render(<StructuralSerializationSection task={makeTask()} />);
    expect(host.textContent).toBe('');
  });

  test('renders foundation details with navigable link', () => {
    const onNav = vi.fn();
    const tasks = [{ id: 'foundation-1', title: 'Foundation analysis' }] as Task[];
    const task = makeTask({
      context: { _foundation_converged: { foundation_id: 'foundation-1', reason: 'Duplicate direction', shared_files: ['src/core.ts', 'src/utils.ts', 'src/types.ts', 'src/extra.ts'] } },
    } as unknown as Partial<Task>);
    const host = render(<StructuralSerializationSection task={task} tasks={tasks} onNavigateToTask={onNav} />);
    expect(host.textContent).toContain('Structural Serialization');
    expect(host.textContent).toContain('Foundation analysis');
    expect(host.textContent).toContain('Duplicate direction');
    expect(host.textContent).toContain('src/core.ts');
    expect(host.textContent).toContain('+1 more');
    host.querySelector('button')?.click();
    expect(onNav).toHaveBeenCalledWith('foundation-1');
  });
});

describe('BaseRecutHoldSection', () => {
  test('renders nothing when no recut data', () => {
    const host = render(<BaseRecutHoldSection task={makeTask()} />);
    expect(host.textContent).toBe('');
  });

  test('renders conflict outcome with red styling', () => {
    const task = makeTask({
      context: { _recut_recheck_attempts: 3, _recut_last_outcome: 'conflict' },
      payloads: [{ base_branch: 'main' }],
    } as unknown as Partial<Task>);
    const host = render(<BaseRecutHoldSection task={task} />);
    expect(host.textContent).toContain('Base Re-cut Hold');
    expect(host.textContent).toContain('Conflict');
    expect(host.textContent).toContain('3');
    expect(host.textContent).toContain('main');
  });

  test('renders transient error outcome', () => {
    const task = makeTask({
      context: { _recut_recheck_attempts: 1, _recut_last_outcome: 'transient' },
    } as unknown as Partial<Task>);
    const host = render(<BaseRecutHoldSection task={task} />);
    expect(host.textContent).toContain('Transient Error');
  });

  test('renders escalation badge', () => {
    const task = makeTask({
      context: { _recut_recheck_attempts: 5, _recut_last_outcome: 'conflict', _recut_escalated: true },
    } as unknown as Partial<Task>);
    const host = render(<BaseRecutHoldSection task={task} />);
    expect(host.textContent).toContain('Escalated');
  });
});

describe('ChildTasksSection', () => {
  test('renders nothing when no child_task_ids', () => {
    const host = render(<ChildTasksSection task={makeTask()} />);
    expect(host.textContent).toBe('');
  });

  test('renders child task links', () => {
    const onNav = vi.fn();
    const childTask = { ...makeTask({ id: 'child-1', title: 'Sub task 1' }), child_task_ids: undefined };
    const tasks = [childTask] as Task[];
    const task = makeTask({ child_task_ids: ['child-1'] } as unknown as Partial<Task>);
    const host = render(<ChildTasksSection task={task} tasks={tasks} onNavigateToTask={onNav} />);
    expect(host.textContent).toContain('Child Tasks');
    // DependencyItem renders the task title when a matching task is found
    expect(host.textContent).toContain('Sub task 1');
    expect(host.textContent).toContain('COMPLETED');
  });
});

describe('MergeTestGateSection', () => {
  test('renders nothing when no merge test result and no unverified reason', () => {
    const host = render(<MergeTestGateSection task={makeTask()} />);
    expect(host.textContent).toBe('');
  });

  test('renders unverified outcome', () => {
    const task = makeTask({ unverified_merge_reason: 'manual_bypass', artifacts: [] } as unknown as Partial<Task>);
    const host = render(<MergeTestGateSection task={task} />);
    expect(host.textContent).toContain('Merge Test Gate');
    expect(host.textContent).toContain('Merged unverified');
  });

  test('renders pass outcome from merge test artifacts', () => {
    const task = makeTask({
      artifacts: [{
        artifact_type: 'MERGE_TEST_RESULT', logs: JSON.stringify({
          detected: true, passed: true, output: '',
        }), iteration: 1,
      }],
    } as unknown as Partial<Task>);
    const host = render(<MergeTestGateSection task={task} />);
    expect(host.textContent).toContain('Passed');
  });

  test('renders blocked outcome', () => {
    const task = makeTask({
      artifacts: [{
        artifact_type: 'MERGE_TEST_RESULT', logs: JSON.stringify({
          detected: false, passed: false, output: '',
          probe: { ran: true, passed: false, could_not_run: false },
        }), iteration: 1,
      }],
    } as unknown as Partial<Task>);
    const host = render(<MergeTestGateSection task={task} />);
    expect(host.textContent).toContain('Blocked');
  });
});

describe('ViewportScreenshotsPanel', () => {
  test('renders nothing when no viewport_screenshots', () => {
    const host = render(<ViewportScreenshotsPanel task={makeTask()} />);
    expect(host.textContent).toBe('');
  });

  test('renders screenshot toggle and pairs', () => {
    const task = makeTask({
      context: {
        viewport_screenshots: [
          { name: 'button-before', storage_uri: 'gs://bucket/before.png' },
          { name: 'button-after', storage_uri: 'gs://bucket/after.png', magnitude: 0.15 },
          { name: 'button-diff', storage_uri: 'gs://bucket/diff.png' },
        ],
      },
    } as unknown as Partial<Task>);
    const host = render(<ViewportScreenshotsPanel task={task} />);
    expect(host.textContent).toContain('Viewport Screenshots');
    expect(host.textContent).toContain('View 3 screenshots');
    act(() => { host.querySelector('button')?.click(); });
    expect(host.textContent).toContain('button');
    expect(host.textContent).toContain('15.0% changed');
    expect(host.textContent).toContain('Before');
    expect(host.textContent).toContain('After');
  });

  test('shows diff toggle when diff entry present', () => {
    const task = makeTask({
      context: {
        viewport_screenshots: [
          { name: 'header-before', storage_uri: 'gs://bucket/header-before.png' },
          { name: 'header-after', storage_uri: 'gs://bucket/header-after.png' },
          { name: 'header-diff', storage_uri: 'gs://bucket/header-diff.png' },
        ],
      },
    } as unknown as Partial<Task>);
    const host = render(<ViewportScreenshotsPanel task={task} />);
    act(() => { host.querySelector('button')?.click(); });
    expect(host.textContent).toContain('Show diff');
  });
});

describe('TaskComputeJobsSection', () => {
  test('renders nothing when no project_id', () => {
    const task = makeTask({ project_id: undefined } as unknown as Partial<Task>);
    const host = render(<TaskComputeJobsSection task={task} />);
    expect(host.textContent).toBe('');
  });

  test('renders launch contract for compute_experiment tasks', async () => {
    const job: ComputeJob = {
      id: 'job-1',
      project_id: 'proj-1',
      task_id: 'task-1',
      backend: 'local-subprocess',
      external_compute_id: null,
      machine_type: null,
      status: 'SUCCEEDED',
      terminal_reason: null,
      outcome: 'SUCCESS',
      spend_cap_usd: 5.0,
      wall_clock_deadline: null,
      created_at: '2026-07-11T10:00:00Z',
      updated_at: '2026-07-11T10:30:00Z',
      started_at: '2026-07-11T10:00:10Z',
      completed_at: '2026-07-11T10:29:50Z',
      last_polled_at: '2026-07-11T10:29:50Z',
      accrued_cost_usd: 0,
      final_cost_usd: 0,
      result_reference: null,
      accelerator_type: null,
      accelerator_count: null,
      task_title: null,
      task_type: 'compute_experiment',
      runner_target_id: null,
      runner_claimed_at: null,
      artifact: null,
      artifact_id: null,
      runner: null,
      criterion_verdict: null,
    };
    vi.mocked(fetchComputeJobs).mockResolvedValueOnce({
      jobs: [job], hasMore: false,
      summary: { total_jobs: 1, active_jobs: 0, terminal_jobs: 1, total_spend_usd: 0, outcome_breakdown: {}, criterion_measured: 0, criterion_met: 0 },
      limit: 50, offset: 0,
    });
    const task = makeTask({
      task_type: 'compute_experiment' as Task['task_type'],
      payloads: [{ type: 'compute_experiment', command: 'python train.py', spend_cap_usd: 10, deadline_seconds: 3600 }],
      plan_output: {
        knowledge_document: {
          source_kind: 'compute_experiment', compute_acceptance_grade: 'acceptance',
          compute_acceptance_reason: 'llm_artifact_cited', analysis_mode: 'llm',
        },
      },
    } as unknown as Partial<Task>);
    const host = render(<TaskComputeJobsSection task={task} />);
    await vi.waitFor(() => { expect(host.textContent).toContain('Acceptance grade'); });
  });
});
