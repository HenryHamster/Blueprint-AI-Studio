import { type ReactNode, useEffect, useState } from 'react';
import {
  fetchProjectTools,
  createProjectTool,
  deleteProjectTool as deleteProjectToolApi,
  updateProjectTool as updateProjectToolApi,
  fetchToolKinds,
  seedProjectTools as seedProjectToolsApi,
  type ProjectTool,
  type ToolKindConfig,
} from '../api/projectTools';
import { getToolComponent } from '../toolRegistry';
import { WorkbenchPanel } from './WorkbenchLayout';

interface Props {
  projectId: string;
}

type FetchState =
  | { status: 'loading'; projectId: string }
  | { status: 'error'; projectId: string; message: string }
  | { status: 'ok'; projectId: string; tools: ProjectTool[] };

const actionBtn =
  'inline-flex min-h-11 items-center gap-1.5 rounded-md border px-3 text-xs font-semibold transition-colors';

export function ProjectToolHost({ projectId }: Props) {
  const [state, setState] = useState<FetchState>({ status: 'loading', projectId });

  // Management UI state
  const [showAddForm, setShowAddForm] = useState(false);
  const [toolKinds, setToolKinds] = useState<ToolKindConfig[] | null>(null);
  const [loadingKinds, setLoadingKinds] = useState(false);
  const [selectedKind, setSelectedKind] = useState('');
  const [newTitle, setNewTitle] = useState('');
  const [creating, setCreating] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [seeding, setSeeding] = useState(false);

  // Fetch tools on mount / projectId change
  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const data = await fetchProjectTools(projectId);
        if (!cancelled) setState({ status: 'ok', projectId, tools: data });
      } catch (err) {
        if (!cancelled) setState({
          status: 'error',
          projectId,
          message: err instanceof Error ? err.message : 'Failed to load tools',
        });
      }
    }
    void load();
    return () => { cancelled = true; };
  }, [projectId]);

  async function refresh() {
    try {
      const data = await fetchProjectTools(projectId);
      setState({ status: 'ok', projectId, tools: data });
    } catch (err) {
      setState({
        status: 'error',
        projectId,
        message: err instanceof Error ? err.message : 'Failed to refresh',
      });
    }
  }

  // Lazy-load tool kinds when "Add tool" is first clicked
  async function handleOpenAddForm() {
    setShowAddForm(true);
    setActionError(null);
    if (toolKinds !== null) return;
    setLoadingKinds(true);
    try {
      const kinds = await fetchToolKinds();
      setToolKinds(kinds);
      if (kinds.length > 0) setSelectedKind(kinds[0].kind);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Failed to load tool kinds');
      setToolKinds([]);
    } finally {
      setLoadingKinds(false);
    }
  }

  async function handleCreate() {
    if (!selectedKind || !newTitle.trim()) return;
    setCreating(true);
    setActionError(null);
    try {
      await createProjectTool(projectId, { kind: selectedKind, title: newTitle.trim() });
      setShowAddForm(false);
      setNewTitle('');
      void refresh();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Failed to create tool');
    } finally {
      setCreating(false);
    }
  }

  async function handleDelete(toolId: string) {
    setDeletingId(toolId);
    setActionError(null);
    try {
      await deleteProjectToolApi(projectId, toolId);
      void refresh();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Failed to delete tool');
    } finally {
      setDeletingId(null);
    }
  }

  async function handleToggleEnabled(tool: ProjectTool) {
    setActionError(null);
    try {
      await updateProjectToolApi(projectId, tool.id, { enabled: !tool.enabled });
      void refresh();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Failed to update tool');
    }
  }

  async function handleSeed() {
    setSeeding(true);
    setActionError(null);
    try {
      await seedProjectToolsApi(projectId);
      void refresh();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Failed to seed tools');
    } finally {
      setSeeding(false);
    }
  }

  const effective: FetchState = state.projectId === projectId
    ? state
    : { status: 'loading', projectId };

  // ── Helpers to render content ──────────────────────────────────────────

  function renderManagementBar() {
    return (
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-neutral-100">Tools</h2>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={handleOpenAddForm}
            className={`${actionBtn} border-workbench-border bg-workbench-layer text-neutral-300 hover:border-workbench-border-strong hover:bg-workbench-hover hover:text-neutral-100`}
          >
            + Add tool
          </button>
          <button
            type="button"
            onClick={handleSeed}
            disabled={seeding}
            className={`${actionBtn} border-neutral-700 bg-transparent text-neutral-400 hover:text-neutral-200 disabled:opacity-50`}
          >
            {seeding ? 'Seeding…' : 'Seed defaults'}
          </button>
        </div>
      </div>
    );
  }

  function renderAddForm() {
    if (!showAddForm) return null;
    return (
      <div className="workbench-card space-y-3 rounded-md border p-3">
        {loadingKinds ? (
          <p className="text-sm text-neutral-500">Loading tool kinds…</p>
        ) : toolKinds && toolKinds.length > 0 ? (
          <>
            <div>
              <label className="block text-xs font-medium text-neutral-400">Kind</label>
              <select
                value={selectedKind}
                onChange={e => setSelectedKind(e.target.value)}
                className="mt-1 w-full rounded border border-neutral-700 bg-neutral-900 px-2 py-1.5 text-sm text-neutral-200"
              >
                {toolKinds.map(k => (
                  <option key={k.kind} value={k.kind}>{k.label}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium text-neutral-400">Title</label>
              <input
                type="text"
                value={newTitle}
                onChange={e => setNewTitle(e.target.value)}
                placeholder="Tool title"
                className="mt-1 w-full rounded border border-neutral-700 bg-neutral-900 px-2 py-1.5 text-sm text-neutral-200"
              />
            </div>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => { setShowAddForm(false); setActionError(null); }}
                className={`${actionBtn} border-neutral-700 bg-transparent text-neutral-400 hover:text-neutral-200`}
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleCreate}
                disabled={creating || !selectedKind || !newTitle.trim()}
                className={`${actionBtn} border-indigo-600 bg-indigo-600 text-white hover:bg-indigo-500 disabled:opacity-50`}
              >
                {creating ? 'Creating…' : 'Create'}
              </button>
            </div>
          </>
        ) : (
          <p className="text-sm text-neutral-500">No tool kinds available.</p>
        )}
      </div>
    );
  }

  function renderError(): ReactNode {
    if (effective.status === 'error') {
      return <p className="text-sm text-red-400">{effective.message}</p>;
    }
    return null;
  }

  function renderLoading(): ReactNode {
    if (effective.status === 'loading') {
      return (
        <div className="flex items-center gap-2 text-sm text-neutral-500">
          <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-neutral-600 border-t-neutral-300" />
          Loading tools…
        </div>
      );
    }
    return null;
  }

  function renderToolContent(): ReactNode {
    if (effective.status !== 'ok') return null;
    const enabledTools = effective.tools.filter(t => t.enabled);
    const registeredTools = enabledTools.filter(t => getToolComponent(t.kind) !== undefined);

    if (registeredTools.length === 0) {
      return (
        <>
          <p className="text-sm text-neutral-500">No tools registered for this project.</p>
        </>
      );
    }

    return (
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        {registeredTools.map(tool => {
          const ToolComponent = getToolComponent(tool.kind);
          if (!ToolComponent) return null;
          return (
            <div key={tool.id} className="min-w-0 space-y-1.5">
              <WorkbenchPanel title={tool.title}>
                <ToolComponent projectId={projectId} tool={tool} />
              </WorkbenchPanel>
              {/* Per-tool management actions */}
              <div className="flex items-center gap-2 px-1">
                <button
                  type="button"
                  data-viewport-allow-small-target
                  onClick={() => handleToggleEnabled(tool)}
                  className="text-xs text-neutral-500 hover:text-neutral-300"
                >
                  {tool.enabled ? 'Disable' : 'Enable'}
                </button>
                <button
                  type="button"
                  data-viewport-allow-small-target
                  onClick={() => handleDelete(tool.id)}
                  disabled={deletingId === tool.id}
                  className="text-xs text-red-400 hover:text-red-300 disabled:opacity-50"
                >
                  {deletingId === tool.id ? 'Removing…' : 'Remove'}
                </button>
              </div>
            </div>
          );
        })}
      </div>
    );
  }

  // ── Main render ────────────────────────────────────────────────────────

  return (
    <div className="space-y-3">
      {renderManagementBar()}
      {actionError && <p className="text-sm text-red-400">{actionError}</p>}
      {renderAddForm()}
      {renderError()}
      {renderLoading()}
      {renderToolContent()}
    </div>
  );
}
