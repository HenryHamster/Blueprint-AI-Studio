import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { RailMiniCanvas } from './RailMiniCanvas';

// StrategicContextMap.computeLayout is a pure layout algorithm that we want to
// exercise through the component (it's not async and has no external deps), so
// we do NOT mock it here — the real implementation is used.

describe('RailMiniCanvas — state 1: canvas data present', () => {
  const nodes = [
    { id: 'n1', kind: 'project', label: 'Project', summary: 'Root', status: 'active' },
    { id: 'n2', kind: 'direction', label: 'Direction', summary: 'Leaf', status: 'done' },
  ];
  const edges = [{ source: 'n1', target: 'n2', kind: 'dependency' }];

  it('renders the SVG canvas when nodes are provided', () => {
    const { container } = render(<RailMiniCanvas canvas={{ nodes, edges }} />);
    const svgEl = container.querySelector('svg');
    expect(svgEl).not.toBeNull();
  });

  it('uses the rail-mini-canvas testid', () => {
    render(<RailMiniCanvas canvas={{ nodes, edges }} />);
    expect(screen.getByTestId('rail-mini-canvas')).toBeInTheDocument();
  });

  it('renders circles for each positioned node', () => {
    const { container } = render(<RailMiniCanvas canvas={{ nodes, edges }} />);
    const circles = container.querySelectorAll('circle');
    expect(circles.length).toBe(2);
  });

  it('does not render the working or idle placeholder', () => {
    render(<RailMiniCanvas canvas={{ nodes, edges }} />);
    expect(screen.queryByTestId('rail-mini-canvas-working')).toBeNull();
    expect(screen.queryByTestId('rail-mini-canvas-idle')).toBeNull();
  });

  it('applies the correct fill color for active status (cyan)', () => {
    const activeNode = { id: 'n1', kind: 'project', label: 'P', summary: '', status: 'active' };
    const { container } = render(<RailMiniCanvas canvas={{ nodes: [activeNode], edges: [] }} />);
    const circle = container.querySelector('circle');
    expect(circle?.getAttribute('fill')).toBe('#22d3ee');
  });

  it('applies the correct fill color for failed/blocked status (rose)', () => {
    const failedNode = { id: 'n1', kind: 'task', label: 'T', summary: '', status: 'failed' };
    const { container } = render(<RailMiniCanvas canvas={{ nodes: [failedNode], edges: [] }} />);
    const circle = container.querySelector('circle');
    expect(circle?.getAttribute('fill')).toBe('#fb7185');
  });

  it('applies the correct fill color for done/complete status (green)', () => {
    const doneNode = { id: 'n1', kind: 'task', label: 'T', summary: '', status: 'done' };
    const { container } = render(<RailMiniCanvas canvas={{ nodes: [doneNode], edges: [] }} />);
    const circle = container.querySelector('circle');
    expect(circle?.getAttribute('fill')).toBe('#34d399');
  });

  it('applies the default neutral fill for an unrecognized status', () => {
    const unknownNode = { id: 'n1', kind: 'task', label: 'T', summary: '', status: 'unknown_status' };
    const { container } = render(<RailMiniCanvas canvas={{ nodes: [unknownNode], edges: [] }} />);
    const circle = container.querySelector('circle');
    expect(circle?.getAttribute('fill')).toBe('#525252');
  });

  it('also applies cyan fill for "running" and "pending" status keywords', () => {
    for (const status of ['running', 'building', 'pending']) {
      const { container } = render(
        <RailMiniCanvas canvas={{ nodes: [{ id: 'n1', kind: 'task', label: 'T', summary: '', status }], edges: [] }} />,
      );
      expect(container.querySelector('circle')?.getAttribute('fill')).toBe('#22d3ee');
    }
  });

  it('applies rose fill for "error" and "critical" status keywords', () => {
    for (const status of ['error', 'critical']) {
      const { container } = render(
        <RailMiniCanvas canvas={{ nodes: [{ id: 'n1', kind: 'task', label: 'T', summary: '', status }], edges: [] }} />,
      );
      expect(container.querySelector('circle')?.getAttribute('fill')).toBe('#fb7185');
    }
  });

  it('applies green fill for "complete" and "resolved" status keywords', () => {
    for (const status of ['complete', 'resolved', 'ready']) {
      const { container } = render(
        <RailMiniCanvas canvas={{ nodes: [{ id: 'n1', kind: 'task', label: 'T', summary: '', status }], edges: [] }} />,
      );
      expect(container.querySelector('circle')?.getAttribute('fill')).toBe('#34d399');
    }
  });

  it('renders edge paths in the SVG', () => {
    const { container } = render(<RailMiniCanvas canvas={{ nodes, edges }} />);
    const paths = container.querySelectorAll('path');
    expect(paths.length).toBeGreaterThan(0);
  });

  it('disables pointer events on the SVG (non-interactive)', () => {
    const { container } = render(<RailMiniCanvas canvas={{ nodes, edges }} />);
    const svg = container.querySelector('svg');
    expect(svg?.style.pointerEvents).toBe('none');
  });
});

describe('RailMiniCanvas — state 2: no canvas, strategist active', () => {
  it('renders the working indicator when strategistActive is true and no nodes', () => {
    render(<RailMiniCanvas canvas={null} strategistActive={true} />);
    expect(screen.getByTestId('rail-mini-canvas-working')).toBeInTheDocument();
  });

  it('shows the generic "Working…" label when no task or direction label is provided', () => {
    render(<RailMiniCanvas canvas={null} strategistActive={true} />);
    expect(screen.getByText('Working…')).toBeInTheDocument();
  });

  it('shows the active task label when provided', () => {
    render(
      <RailMiniCanvas
        canvas={null}
        strategistActive={true}
        activeTaskLabel="Implementing feature X"
      />,
    );
    expect(screen.getByText('Implementing feature X')).toBeInTheDocument();
    expect(screen.queryByText('Working…')).toBeNull();
  });

  it('shows the active direction label when provided', () => {
    render(
      <RailMiniCanvas
        canvas={null}
        strategistActive={true}
        activeDirectionLabel="Direction: Auth refactor"
      />,
    );
    expect(screen.getByText('Direction: Auth refactor')).toBeInTheDocument();
  });

  it('shows both task and direction labels when both are provided', () => {
    render(
      <RailMiniCanvas
        canvas={null}
        strategistActive={true}
        activeTaskLabel="Task: Implement auth"
        activeDirectionLabel="Auth refactor"
      />,
    );
    expect(screen.getByText('Task: Implement auth')).toBeInTheDocument();
    expect(screen.getByText('Auth refactor')).toBeInTheDocument();
  });

  it('does not render the idle placeholder or the SVG canvas', () => {
    render(<RailMiniCanvas canvas={null} strategistActive={true} />);
    expect(screen.queryByTestId('rail-mini-canvas-idle')).toBeNull();
    expect(screen.queryByTestId('rail-mini-canvas')).toBeNull();
  });

  it('treats empty canvas nodes as "no canvas data" and shows working indicator', () => {
    render(
      <RailMiniCanvas
        canvas={{ nodes: [], edges: [] }}
        strategistActive={true}
      />,
    );
    expect(screen.getByTestId('rail-mini-canvas-working')).toBeInTheDocument();
  });
});

describe('RailMiniCanvas — state 3: idle, no data', () => {
  it('renders the idle placeholder when there is no canvas and strategist is inactive', () => {
    render(<RailMiniCanvas canvas={null} strategistActive={false} />);
    expect(screen.getByTestId('rail-mini-canvas-idle')).toBeInTheDocument();
  });

  it('shows "No map data" text in the idle placeholder', () => {
    render(<RailMiniCanvas />);
    expect(screen.getByText('No map data')).toBeInTheDocument();
  });

  it('renders idle placeholder when strategistActive is omitted (defaults to false)', () => {
    render(<RailMiniCanvas canvas={undefined} />);
    expect(screen.getByTestId('rail-mini-canvas-idle')).toBeInTheDocument();
  });

  it('does not render working indicator or SVG canvas in idle state', () => {
    render(<RailMiniCanvas />);
    expect(screen.queryByTestId('rail-mini-canvas-working')).toBeNull();
    expect(screen.queryByTestId('rail-mini-canvas')).toBeNull();
  });
});
