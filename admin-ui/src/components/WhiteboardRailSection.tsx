import { Suspense, lazy, useEffect, useState } from 'react';
import { X } from 'lucide-react';
import { WorkbenchPanel } from './WorkbenchLayout';
import { fetchProjectTools, type ProjectTool } from '../api/projectTools';

// The full canvas is a heavy surface (drawing, image/document nodes, pan/zoom).
// Lazy-load it so it never lands in the initial strategic-rail bundle — the
// admin-ui load time is an active operator complaint (g-admin-ui-load-performance).
const WhiteboardTool = lazy(() => import('./WhiteboardTool'));

interface Props {
  projectId?: string | null;
  readOnly?: boolean;
}

type State =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; tool: ProjectTool | null };

/**
 * Strategic-sidebar entry point for the project whiteboard/canvas.
 *
 * This is the whiteboard's new home (relocated out of the project Tools tab per
 * the 2026-07-14 operator directive). It intentionally carries NO tool
 * creation/management chrome — that lives in Project → Tools. If no whiteboard
 * tool exists yet, it points the operator there rather than exposing create
 * buttons in the strategic session.
 */
export function WhiteboardRailSection({ projectId, readOnly = true }: Props) {
  const [state, setState] = useState<State>({ status: 'idle' });
  const [open, setOpen] = useState(false);

  useEffect(() => {
    // Render short-circuits to null when there is no project, so no state
    // update is needed in that case (and avoids a synchronous setState here).
    if (!projectId) return;
    let cancelled = false;
    async function load() {
      setState({ status: 'loading' });
      try {
        const result = await fetchProjectTools(projectId as string);
        if (cancelled) return;
        const whiteboard = result.tools.find(t => t.kind === 'whiteboard' && t.enabled) ?? null;
        setState({ status: 'ready', tool: whiteboard });
      } catch (err) {
        if (cancelled) return;
        setState({ status: 'error', message: err instanceof Error ? err.message : 'Failed to load whiteboard' });
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  if (!projectId) return null;

  const tool = state.status === 'ready' ? state.tool : null;

  return (
    <>
      <WorkbenchPanel title="Whiteboard">
        {state.status === 'loading' && <p className="text-xs text-neutral-500">Loading whiteboard…</p>}
        {state.status === 'error' && <p className="text-xs text-red-400">{state.message}</p>}
        {state.status === 'ready' && !tool && (
          <p className="text-xs text-neutral-500">
            No whiteboard yet. Add one in <span className="text-neutral-300">Project → Tools</span>.
          </p>
        )}
        {state.status === 'ready' && tool && (
          <div className="space-y-2">
            <p className="text-xs text-neutral-400">
              A shared canvas for sketches, images, documents, and notes.
            </p>
            <button
              type="button"
              onClick={() => setOpen(true)}
              className="inline-flex min-h-11 items-center gap-1.5 rounded-md border border-workbench-border bg-workbench-layer px-3 text-xs font-semibold text-neutral-200 transition-colors hover:border-workbench-border-strong hover:bg-workbench-hover hover:text-neutral-100"
            >
              Open canvas
            </button>
          </div>
        )}
      </WorkbenchPanel>

      {open && tool && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
          role="dialog"
          aria-modal="true"
          aria-label="Whiteboard canvas"
          onClick={() => setOpen(false)}
        >
          <div
            className="flex h-[90vh] w-full max-w-6xl flex-col overflow-hidden rounded-lg border border-workbench-border bg-workbench-surface shadow-2xl"
            onClick={e => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b border-workbench-border px-4 py-2">
              <h2 className="text-sm font-semibold text-neutral-100">{tool.title}</h2>
              <button
                type="button"
                aria-label="Close whiteboard"
                onClick={() => setOpen(false)}
                className="flex min-h-11 min-w-11 items-center justify-center rounded p-1 text-neutral-400 hover:bg-workbench-hover hover:text-neutral-100"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="min-h-0 flex-1 overflow-auto p-4 custom-scrollbar">
              <Suspense fallback={<p className="text-sm text-neutral-500">Loading canvas…</p>}>
                <WhiteboardTool projectId={projectId} tool={tool} readOnly={readOnly} />
              </Suspense>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
