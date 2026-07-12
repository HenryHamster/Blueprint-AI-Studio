import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import ProblemModelViewer from './ProblemModelViewer';
import type { ProblemModel } from '../types';

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
  authGet: vi.fn(async () => ({ ok: true, json: async () => ({}) })),
  dismissPendingConfirmation: vi.fn(async () => {}),
}));

function makeProblemModel(overrides: Partial<ProblemModel> = {}): ProblemModel {
  return {
    intent: {
      purpose: 'Build an autonomous coding studio.',
      current_focus: 'Stabilize the tactical→review→merge loop.',
    },
    context: {
      project_type: 'startup',
      users: 'Engineering teams',
      team: '5 engineers',
      deploy_cadence: 'continuous',
    },
    constraints: [],
    capabilities: [],
    gaps: [
      {
        id: 'gap-1',
        name: 'Reduce agent iteration count on cross-system contract failures',
        description: 'When a generated URL or API shape mismatches, the agent retries from scratch.',
        priority: 'high',
        size: 'large',
      },
      {
        id: 'gap-2',
        name: 'Scope-drift detection in viewport test creation',
        description: 'Creating viewport tests occasionally introduces unrelated changes.',
        priority: 'medium',
        size: 'medium',
      },
    ],
    friction: [],
    pending_user_confirmation: [],
    archived_gaps: [],
    revision: 1,
    last_updated: '2026-06-01T00:00:00Z',
    last_updated_by: 'agent',
    ...overrides,
  };
}

const noop = () => {};
const defaultProps = {
  projectId: 'proj-test-1',
  addToast: noop,
  onUpdated: noop,
};

describe('ProblemModelViewer — gap-card callback wiring', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) { act(() => { r.unmount(); }); root = null; }
    if (host) { host.remove(); host = null; }
  });

  test('clicking Spawn direction invokes onSpawnDirection with (gapId, gapName)', async () => {
    const onSpawnDirection = vi.fn();
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    const pm = makeProblemModel();
    await act(async () => {
      r.render(
        <ProblemModelViewer
          problemModel={pm}
          onSpawnDirection={onSpawnDirection}
          {...defaultProps}
        />
      );
    });

    const spawnBtn = Array.from(host.querySelectorAll('button')).find(b =>
      b.textContent?.includes('Spawn direction')
    ) as HTMLButtonElement | undefined;
    expect(spawnBtn).toBeDefined();
    await act(async () => { spawnBtn!.click(); });

    expect(onSpawnDirection).toHaveBeenCalledTimes(1);
    expect(onSpawnDirection).toHaveBeenCalledWith(pm.gaps[0].id, pm.gaps[0].name);
  });

  test('clicking second gap Spawn direction passes the second gap id and name', async () => {
    const onSpawnDirection = vi.fn();
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    const pm = makeProblemModel();
    await act(async () => {
      r.render(
        <ProblemModelViewer
          problemModel={pm}
          onSpawnDirection={onSpawnDirection}
          {...defaultProps}
        />
      );
    });

    const spawnBtns = Array.from(host.querySelectorAll('button')).filter(b =>
      b.textContent?.includes('Spawn direction')
    ) as HTMLButtonElement[];
    expect(spawnBtns.length).toBeGreaterThanOrEqual(2);
    await act(async () => { spawnBtns[1].click(); });

    expect(onSpawnDirection).toHaveBeenCalledWith(pm.gaps[1].id, pm.gaps[1].name);
  });

  test('selecting a direction in GapLinkSelect invokes onLinkDirection with (directionId, gapId)', async () => {
    const onLinkDirection = vi.fn();
    const directions = [
      { id: 'd-abc12345', text: 'Fix the contract violation detection flow' },
      { id: 'd-def67890', text: 'Add structured contract extraction' },
    ];
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    const pm = makeProblemModel();
    await act(async () => {
      r.render(
        <ProblemModelViewer
          problemModel={pm}
          onLinkDirection={onLinkDirection}
          directions={directions}
          {...defaultProps}
        />
      );
    });

    const select = host.querySelector('select') as HTMLSelectElement | null;
    expect(select).not.toBeNull();

    await act(async () => {
      select!.value = 'd-def67890';
      select!.dispatchEvent(new Event('change', { bubbles: true }));
    });

    expect(onLinkDirection).toHaveBeenCalledTimes(1);
    expect(onLinkDirection).toHaveBeenCalledWith('d-def67890', pm.gaps[0].id);
  });

  test('selecting first direction in GapLinkSelect passes the first direction id', async () => {
    const onLinkDirection = vi.fn();
    const directions = [
      { id: 'd-first-111', text: 'First direction' },
      { id: 'd-second-222', text: 'Second direction' },
    ];
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    const pm = makeProblemModel();
    await act(async () => {
      r.render(
        <ProblemModelViewer
          problemModel={pm}
          onLinkDirection={onLinkDirection}
          directions={directions}
          {...defaultProps}
        />
      );
    });

    const select = host.querySelector('select') as HTMLSelectElement | null;
    expect(select).not.toBeNull();

    await act(async () => {
      select!.value = 'd-first-111';
      select!.dispatchEvent(new Event('change', { bubbles: true }));
    });

    expect(onLinkDirection).toHaveBeenCalledTimes(1);
    expect(onLinkDirection).toHaveBeenCalledWith('d-first-111', pm.gaps[0].id);
  });

  test('selecting placeholder (empty) option in GapLinkSelect does not invoke onLinkDirection', async () => {
    const onLinkDirection = vi.fn();
    const directions = [{ id: 'd-abc12345', text: 'Fix the flow' }];
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <ProblemModelViewer
          problemModel={makeProblemModel()}
          onLinkDirection={onLinkDirection}
          directions={directions}
          {...defaultProps}
        />
      );
    });

    const select = host.querySelector('select') as HTMLSelectElement | null;
    expect(select).not.toBeNull();

    await act(async () => {
      select!.value = '';
      select!.dispatchEvent(new Event('change', { bubbles: true }));
    });

    expect(onLinkDirection).not.toHaveBeenCalled();
  });
});

describe('ProblemModelViewer — proof coverage meter', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) { act(() => { r.unmount(); }); root = null; }
    if (host) { host.remove(); host = null; }
  });

  async function renderWith(pm: ProblemModel) {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<ProblemModelViewer problemModel={pm} {...defaultProps} />);
    });
  }

  test('excludes exempt capabilities from the denominator and counts only proven as covered', async () => {
    const pm = makeProblemModel({
      capabilities: [
        { name: 'Proven cap', status: 'working', proof_status: 'proven' },
        { name: 'Unproven cap', status: 'working', proof_status: 'unproven' },
        { name: 'Exempt cap', status: 'working', proof_exempt: true, proof_status: 'unproven' },
        { name: 'Planned cap', status: 'planned', proof_status: 'unproven' },
      ],
    });
    await renderWith(pm);

    // 1 proven of 2 non-exempt working (the exempt + planned caps are excluded).
    const value = host!.querySelector('[data-testid="proof-coverage-value"]');
    expect(value).not.toBeNull();
    expect(value!.textContent).toBe('1/2 (50%)');
  });

  test('shows n/a when there are zero non-exempt working capabilities', async () => {
    const pm = makeProblemModel({
      capabilities: [
        { name: 'Exempt cap', status: 'working', proof_exempt: true, proof_status: 'unproven' },
        { name: 'Planned cap', status: 'planned', proof_status: 'unproven' },
      ],
    });
    await renderWith(pm);

    const meter = host!.querySelector('[data-testid="proof-coverage-meter"]');
    expect(meter).not.toBeNull();
    expect(meter!.textContent).toContain('n/a');
    expect(host!.querySelector('[data-testid="proof-coverage-value"]')).toBeNull();
  });

  test('reports full coverage when every non-exempt working capability is proven', async () => {
    const pm = makeProblemModel({
      capabilities: [
        { name: 'A', status: 'working', proof_status: 'proven' },
        { name: 'B', status: 'working', proof_status: 'proven' },
        { name: 'C', status: 'working', proof_exempt: true, proof_status: 'unproven' },
      ],
    });
    await renderWith(pm);

    const value = host!.querySelector('[data-testid="proof-coverage-value"]');
    expect(value!.textContent).toBe('2/2 (100%)');
  });
});
