import { afterEach, describe, expect, it, vi } from 'vitest';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  fetchFleetUtilization,
  listFleetRunners,
  purgeArchive,
  registerFleetRunner,
  rotateFleetRunnerToken,
  type FleetRunner,
  type FleetUtilizationResponse,
} from './api';
import {
  fetchStrategicSession,
  fetchStrategicState,
  type StrategicSessionResponse,
  type StrategicStateResponse,
} from './strategicStateApi';
import type { WorkbenchEventEnvelopeV1 } from './types/workbenchEvents';

vi.mock('./firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  getIdToken: vi.fn(async () => 'test-token'),
  getIdTokenAfterAuthRestore: vi.fn(async () => 'test-token'),
  onAuthChange: vi.fn(() => () => {}),
  signOut: vi.fn(),
}));

const OPERATOR_WORKBENCH_FIXTURE_RELATIVE_PATH = 'backend/testing/__fixtures__/operator_workbench_contracts.json';

function operatorWorkbenchFixturePath(): string {
  const candidates = [
    import.meta.url.startsWith('file:')
      ? resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', OPERATOR_WORKBENCH_FIXTURE_RELATIVE_PATH)
      : null,
    resolve(process.cwd(), '..', OPERATOR_WORKBENCH_FIXTURE_RELATIVE_PATH),
    resolve(process.cwd(), OPERATOR_WORKBENCH_FIXTURE_RELATIVE_PATH),
  ].filter((path): path is string => Boolean(path));
  const fixturePath = candidates.find(candidate => existsSync(candidate));
  if (!fixturePath) throw new Error(`Missing operator workbench contract fixture. Checked: ${candidates.join(', ')}`);
  return fixturePath;
}

const operatorWorkbenchContractFixture = JSON.parse(
  readFileSync(operatorWorkbenchFixturePath(), 'utf-8'),
) as {
  strategic_state_response: StrategicStateResponse;
  strategic_session_response: StrategicSessionResponse;
  workbench_event_payload: WorkbenchEventEnvelopeV1;
};

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status });
}

const runnerFixture = {
  id: 'runner-1',
  hostname: 'operator-desk',
  version: 'runner-1.0',
  status: 'active',
  capabilities: {
    task_types: ['design_doc_update', 'strategic_review'],
    has_gpu: false,
    max_concurrent: 1,
    runner_preset: 'strategic_local',
    workload: 'strategic',
    agent_available: true,
    active_task_ids: ['task-1'],
    active_compute_job_ids: [],
  },
  health: {
    device_status: 'connected',
    device_status_reason: null,
    is_live: true,
    seconds_since_last_seen: 12,
    active_task_count: 1,
    active_compute_job_count: 0,
    active_work_count: 1,
    max_concurrent: 1,
    available_slots: 0,
    stale_reason: null,
    agent_available: true,
    liveness_window_seconds: 300,
  },
  last_seen_at: '2026-07-01T10:00:00Z',
  created_at: '2026-07-01T09:00:00Z',
} satisfies FleetRunner;

const strategicRunnerCapabilities = {
  task_types: ['design_doc_update', 'strategic_review'],
  has_gpu: false,
  max_concurrent: 1,
  runner_preset: 'strategic_local',
  workload: 'strategic',
  agent_available: true,
} satisfies NonNullable<NonNullable<StrategicStateResponse['runtime']>['local_runner']>['runners'][number]['capabilities'];

const workbenchEventFixture = {
  schema_version: 1,
  project_id: 'proj-1',
  resource: 'local_runner',
  action: 'heartbeat',
  refetch: { surfaces: ['runtime', 'strategic_session'] },
} satisfies WorkbenchEventEnvelopeV1;

const runtimeWork = {
  compute_jobs: {
    window_minutes: 60,
    counts: {
      active: 0,
      terminal_recent: 0,
      total: 0,
      queued: 0,
      starting: 0,
      running: 0,
      polling: 0,
      succeeded_recent: 0,
      failed_recent: 0,
      cancelled_recent: 0,
      timed_out_recent: 0,
      criterion_measured: 0,
      criterion_met: 0,
    },
    spend: {
      active_accrued_usd: 0,
      recent_terminal_usd: 0,
      window_total_usd: 0,
    },
    items: [],
  },
  held_approvals: { count: 0, items: [] },
  local_runner_holds: { count: 0, items: [] },
  parked_wakes: {
    counts: {
      active: 0,
      pending: 0,
      fired: 0,
      problem: 0,
      expired: 0,
      failed: 0,
    },
    items: [],
  },
};

const strategicStateFixture = {
  project_id: 'proj-1',
  strategic: {
    active_cycle: null,
    active_task: null,
    active_phase: null,
    latest_cycle: null,
    refresh: { pending: null, last_fired_at: null, last_fired_origin: null },
  },
  running_update: {
    excerpt: 'Strategic is validating the operator workbench contract.',
    char_count: 58,
    acknowledged_at: null,
  },
  problem_model: {
    revision: 1,
    current_focus: 'Local strategic runner contract',
    purpose: 'Operate Blueprint through a persistent Strategic workbench.',
    active_gap_count: 1,
    capability_count: 1,
    pending_confirmation_count: 0,
    top_gap: {
      id: 'gap-local-strategic',
      name: 'Local strategic status must be visible',
      priority: 'high',
      status: 'active',
    },
  },
  directions: {
    total_count: 1,
    active_count: 1,
    paused_count: 0,
    primary: {
      id: 'direction-main',
      text: 'Keep operator chat and monitoring coherent.',
      priority: 'high',
      status: 'active',
      mode: 'continuous',
    },
  },
  messages: {
    counts: { open: 1, linked: 0 },
    items: [],
  },
  tasks: {
    counts: {
      queued: 0,
      running: 1,
      evaluating: 0,
      blocked: 0,
      parked: 0,
      failed: 0,
      completed: 0,
      total: 1,
    },
    cumulative_estimated_cost_usd: 0,
    focus: { version: 1, bucket_limit: 8, has_more: false, buckets: {} },
  },
  attention: {
    level: 'watch',
    summary: '1 open message',
    last_seen_at: null,
    counts: {
      needs_operator: 0,
      watch: 1,
      unread: 1,
      questions: 0,
      blockers: 0,
      approvals: 0,
      manual_directions: 0,
      failed_tasks: 0,
      blocked_tasks: 0,
      parked_tasks: 0,
    },
    items: [],
  },
  context_package: {
    id: 'strategic-context:proj-1',
    title: 'Local strategic runner contract',
    status: 'ready',
    updated_at: '2026-07-01T10:00:00Z',
    item_count: 4,
    sources: [{ kind: 'runtime', count: 1 }],
    canvas: {
      id: 'strategic-canvas:proj-1',
      title: 'Strategic context map',
      kind: 'strategic_context',
      node_count: 1,
      edge_count: 0,
      nodes: [
        {
          id: 'runner:runner-1',
          kind: 'local_runner',
          label: 'operator-desk',
          status: 'connected',
          target: { surface: 'runtime', section: 'devices', runner_id: 'runner-1' },
        },
      ],
      edges: [],
      drilldowns: [],
    },
    resources: [],
    evidence: {
      knowledge: { total: 0, items: [] },
      artifacts: { total: 0, items: [] },
    },
    drilldowns: [],
  },
  runtime: {
    local_runner: {
      status: 'idle',
      location: 'locally',
      status_reason: null,
      mode: 'local_ready',
      strategic_runner_count: 1,
      live_runner_count: 1,
      eligible_live_count: 1,
      stale_runner_count: 0,
      busy_runner_count: 0,
      agent_unavailable_count: 0,
      active_runner_id: null,
      active_task_pinned: false,
      runners: [
        {
          id: runnerFixture.id,
          hostname: runnerFixture.hostname,
          status: runnerFixture.status,
          last_seen_at: runnerFixture.last_seen_at,
          created_at: runnerFixture.created_at,
          capabilities: strategicRunnerCapabilities,
          health: runnerFixture.health,
          handles_active_task_type: true,
          eligible_for_active_task: true,
          is_preferred: true,
          is_active_runner: false,
        },
      ],
      fallback_reason: null,
      liveness_window_seconds: 300,
    },
    work: runtimeWork,
  },
  workbench: {
    version: 1,
    interaction_mode: 'continuous',
    active_surface: 'strategic',
    preferred_runner_id: 'runner-1',
    selected_refs: [],
    session_started_at: '2026-07-01T09:55:00Z',
    last_interaction_at: '2026-07-01T10:00:00Z',
  },
  work_session: {
    version: 1,
    id: 'strategic-session:proj-1',
    mode: 'continuous',
    active_surface: 'strategic',
    status: 'idle',
    location: 'locally',
    status_reason: null,
    session_started_at: '2026-07-01T09:55:00Z',
    last_interaction_at: '2026-07-01T10:00:00Z',
    context_package_id: 'strategic-context:proj-1',
    attention_level: 'watch',
    selected_refs: [],
    selected_ref_descriptors: [],
    curated_metrics: [
      {
        id: 'runtime.local_strategic_ready',
        label: 'Local ready',
        value: 1,
        tone: 'active',
        target: { surface: 'runtime', section: 'devices' },
        provenance: {
          surface: 'runtime',
          path: 'runtime.local_runner.eligible_live_count',
        },
      },
    ],
    context_sources: [{ kind: 'runtime', count: 1 }],
    jobs: {
      version: 1,
      limit: 8,
      has_more: false,
      items: [
        {
          id: 'receipt-1',
          command_id: 'session.mode.continuous',
          label: 'Workday mode',
          status: 'succeeded',
          summary: 'Command completed: Workday mode',
          created_at: '2026-07-01T10:00:00Z',
          completed_at: '2026-07-01T10:00:00Z',
          target: { surface: 'messages', action: 'set_continuous_mode' },
          refetch_surfaces: ['strategic_session', 'runtime'],
          result: { kind: 'workbench_update' },
        },
      ],
    },
    events: {
      version: 1,
      limit: 30,
      has_more: false,
      items: [
        {
          ...workbenchEventFixture,
          id: 'event-1',
          occurred_at: '2026-07-01T10:00:00Z',
          source: 'runner',
          title: 'Runner heartbeat',
          summary: 'operator-desk connected',
          tone: 'active',
          ref: { kind: 'runner', id: 'runner-1' },
          target: { surface: 'runtime', section: 'devices', runner_id: 'runner-1' },
          data: { device_status: 'connected' },
        },
      ],
    },
    last_check_in: null,
    since_last_check_in: {
      version: 1,
      available: false,
      reason: 'no_prior_check_in',
      since: null,
      until: '2026-07-01T10:00:00Z',
    },
  },
  design: { has_design_doc: false, revision: null },
  sha_freshness: null,
} satisfies StrategicStateResponse;

const strategicConversationItems = [
  {
    id: 'msg-1',
    project_id: 'proj-1',
    message_type: 'FEEDBACK',
    from_type: 'user',
    from_task_id: null,
    content: 'Keep it simple.',
    status: 'open',
    created_at: '2026-07-01T10:00:00Z',
    context: {},
  },
] satisfies StrategicSessionResponse['conversation']['items'];

const strategicSessionFixture = {
  project_id: 'proj-1',
  status: 'idle',
  location: 'locally',
  status_reason: null,
  session: {
    ...strategicStateFixture.work_session,
    status: 'idle',
    location: 'locally',
    status_reason: null,
    runtime: strategicStateFixture.runtime.local_runner,
    conversation: {
      limit: 20,
      has_more: false,
      order: 'oldest_first',
      items: strategicConversationItems,
    },
    recent_outputs: [],
  },
  workbench: strategicStateFixture.workbench,
  strategic: strategicStateFixture.strategic,
  runtime: strategicStateFixture.runtime,
  context_package: strategicStateFixture.context_package,
  conversation: {
    limit: 20,
    has_more: false,
    order: 'oldest_first',
    items: strategicConversationItems,
  },
  recent_outputs: [],
  arming: null,
} satisfies StrategicSessionResponse;

const fleetFixture = {
  active_now: 1,
  nominal_capacity: 4,
  usable_capacity: 3,
  cooled_accounts: 0,
  util_now: 0.25,
  util_avg: 0.5,
  window_hours: 24,
  saturation: 'healthy',
  vm_cap: 8,
  active_task_vms: 1,
  managed_vm_count: 2,
  idle_pool_vms: 1,
  vm_slots_available: 7,
  vm_saturation: 'healthy',
  precomputed: false,
  precomputed_at: '2026-07-01T10:00:00Z',
  precompute_age_seconds: 3,
  served_stale: false,
} satisfies FleetUtilizationResponse;

describe('operator workbench contract fixtures', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('consumes the backend-owned operator workbench fixture', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse(operatorWorkbenchContractFixture.strategic_state_response))
      .mockResolvedValueOnce(jsonResponse(operatorWorkbenchContractFixture.strategic_session_response));

    const state = await fetchStrategicState('proj-1');
    expect(state.work_session?.commands?.[0]).toMatchObject({
      id: 'session.mode.continuous',
      executable: true,
      target: { surface: 'messages', project_id: 'proj-1' },
    });
    expect(state.work_session?.curated_metrics[0].target).toMatchObject({
      surface: 'runtime',
      section: 'devices',
      runner_id: 'runner-1',
    });
    expect(state.work_session?.events?.items[0]).toMatchObject(operatorWorkbenchContractFixture.workbench_event_payload);

    const session = await fetchStrategicSession('proj-1', 'msg-1');
    expect(session.session.commands?.[0].refetch_surfaces).toEqual(['strategic_session', 'runtime']);
    expect(session.session.runtime?.runners?.[0].health?.device_status).toBe('connected');
    expect(session.conversation.items).toEqual(session.session.conversation?.items);

    expect(String(fetchMock.mock.calls[0][0])).toContain('/api/admin/projects/proj-1/strategic-state');
    expect(String(fetchMock.mock.calls[1][0])).toContain(
      '/api/admin/projects/proj-1/strategic-session?include_message_id=msg-1',
    );
  });

  it('preserves Strategic state and session payload fields the workbench composes', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse(strategicStateFixture))
      .mockResolvedValueOnce(jsonResponse(strategicSessionFixture));

    const state = await fetchStrategicState('proj-1');
    expect(state.work_session?.curated_metrics[0].provenance).toMatchObject({
      surface: 'runtime',
      path: 'runtime.local_runner.eligible_live_count',
    });
    expect(state.work_session?.status).toBe('idle');
    expect(state.work_session?.location).toBe('locally');
    expect(state.work_session?.jobs?.items[0].command_id).toBe('session.mode.continuous');
    expect(state.work_session?.events?.items[0].schema_version).toBe(1);
    expect(state.runtime?.local_runner?.runners?.[0].health?.device_status).toBe('connected');

    const session = await fetchStrategicSession('proj-1', 'msg-1');
    expect(session.session.runtime?.runners?.[0].health?.active_work_count).toBe(1);
    expect(session.session.conversation?.order).toBe('oldest_first');
    expect(session.conversation.items).toEqual(session.session.conversation?.items);
    expect(session.session.runtime?.runners?.[0].capabilities).not.toHaveProperty('active_task_ids');
    expect(session.session.runtime?.runners?.[0].capabilities).not.toHaveProperty('active_compute_job_ids');

    expect(String(fetchMock.mock.calls[0][0])).toContain('/api/admin/projects/proj-1/strategic-state');
    expect(String(fetchMock.mock.calls[1][0])).toContain(
      '/api/admin/projects/proj-1/strategic-session?include_message_id=msg-1',
    );
  });

  it('keeps operational status helpers aligned with runner, fleet, and archive contracts', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse(fleetFixture))
      .mockResolvedValueOnce(jsonResponse([runnerFixture]))
      .mockResolvedValueOnce(jsonResponse({ runner_id: 'runner-new', token: 'runner-token' }))
      .mockResolvedValueOnce(jsonResponse({ runner_id: 'runner-1', token: 'rotated-token' }))
      .mockResolvedValueOnce(jsonResponse({ deleted_count: 3, has_more: false }));

    await expect(fetchFleetUtilization()).resolves.toMatchObject({
      served_stale: false,
      saturation: 'healthy',
      precompute_age_seconds: 3,
    });

    const runners = await listFleetRunners('proj-1');
    expect(runners[0].health?.device_status).toBe('connected');
    expect(runners[0].health?.active_compute_job_count).toBe(0);

    await expect(registerFleetRunner('proj-1', { preset: 'strategic_local' })).resolves.toEqual({
      runner_id: 'runner-new',
      token: 'runner-token',
    });
    expect(JSON.parse(String((fetchMock.mock.calls[2][1] as RequestInit).body))).toEqual({
      preset: 'strategic_local',
    });

    await expect(rotateFleetRunnerToken('proj-1', 'runner-1')).resolves.toEqual({
      runner_id: 'runner-1',
      token: 'rotated-token',
    });
    expect(String(fetchMock.mock.calls[3][0])).toContain(
      '/api/admin/projects/proj-1/runners/runner-1/rotate-token',
    );
    expect((fetchMock.mock.calls[3][1] as RequestInit).method).toBe('POST');

    await expect(purgeArchive(90)).resolves.toEqual({ deleted_count: 3, has_more: false });
    expect(JSON.parse(String((fetchMock.mock.calls[4][1] as RequestInit).body))).toEqual({
      older_than_days: 90,
    });
  });
});
