import { type ReactNode, useEffect, useMemo, useRef, useState } from 'react';
import {
  fetchProjectTools,
  createProjectTool,
  deleteProjectTool as deleteProjectToolApi,
  updateProjectTool as updateProjectToolApi,
  fetchToolKinds,
  fetchProjectToolManifest,
  seedProjectTools as seedProjectToolsApi,
  fetchComputeImageStatus,
  TOOL_PLACEMENTS,
  type ProjectTool,
  type ProjectToolManifestEntry,
  type ProjectSkillManifestEntry,
  type ToolKindConfig,
  type ComputeImageStatusResponse,
} from '../api/projectTools';
import { getToolComponent } from '../toolRegistry';
import { WorkbenchPanel } from './WorkbenchLayout';
import { ProjectToolIframe } from './ProjectToolIframe';
import { loadHiddenManifestSlugs, saveHiddenManifestSlugs } from './projectToolHidden';

interface Props {
  projectId: string;
  /**
   * The caller's role on this project. Owner-only management controls
   * (add / seed / configure / rename / enable-disable / remove) are hidden
   * for non-owners so members never see buttons that only 403. When omitted
   * (e.g. legacy call sites, tests) the caller is treated as an owner; the
   * backend still enforces the owner requirement on every mutation.
   */
  myRole?: 'owner' | 'member' | 'viewer';
}

interface ConfigDraft {
  title: string;
  placement: string;
  config: Record<string, string>;
}

type FetchState =
  | { status: 'loading'; projectId: string }
  | { status: 'error'; projectId: string; message: string }
  | { status: 'ok'; projectId: string; tools: ProjectTool[] };

type ManifestState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ok'; tools: ProjectToolManifestEntry[]; skills: ProjectSkillManifestEntry[]; warning?: string };

type ComputeImageFetchState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ok'; data: ComputeImageStatusResponse; hasDrift: boolean };

const actionBtn =
  'inline-flex min-h-11 items-center gap-1.5 rounded-md border px-3 text-xs font-semibold transition-colors';

function formatDateTime(iso: string | null): string {
  if (!iso) return '—';
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return iso;
  }
}

/**
 * Coerce a raw text input into the type the config schema expects.
 * Falls back to the raw string when coercion is ambiguous — the backend
 * validates the final type and surfaces a 400 the caller renders.
 */
function coerceConfigValue(raw: string, type: string | undefined): unknown {
  switch (type) {
    case 'integer': {
      const n = Number(raw);
      return Number.isInteger(n) ? n : raw;
    }
    case 'boolean':
      return raw === 'true' ? true : raw === 'false' ? false : raw;
    default:
      return raw;
  }
}

export function ProjectToolHost({ projectId, myRole }: Props) {
  // UI gating only — the backend independently enforces owner on every mutation.
  const isOwner = myRole === undefined || myRole === 'owner';
  const [state, setState] = useState<FetchState>({ status: 'loading', projectId });
  const [manifestState, setManifestState] = useState<ManifestState>({ status: 'loading' });
  const abortRef = useRef<AbortController | null>(null);
  const [computeImageState, setComputeImageState] = useState<ComputeImageFetchState>({ status: 'idle' });
  const [driftLoading, setDriftLoading] = useState(false);

  // Management UI state
  const [showAddForm, setShowAddForm] = useState(false);
  const [toolKinds, setToolKinds] = useState<ToolKindConfig[] | null>(null);
  const [loadingKinds, setLoadingKinds] = useState(false);
  const [selectedKind, setSelectedKind] = useState('');
  const [newTitle, setNewTitle] = useState('');
  const [creating, setCreating] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [togglingId, setTogglingId] = useState<string | null>(null);
  const [lastCreatedSlug, setLastCreatedSlug] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [seeding, setSeeding] = useState(false);
  // Hidden manifest-tool slugs are persisted per-project in localStorage so the
  // "Hide" control survives a reload instead of resetting on every mount.
  //
  // The persisted set is derived (via useMemo) from the current project, so a
  // project switch automatically shows that project's own hidden state. Hides
  // and un-hides made in this session are layered on top as an override, which
  // is discarded when the project changes (its stored projectId no longer
  // matches). This keeps the derivation pure — no setState during render / in
  // an effect, which the React Compiler lint forbids.
  const persistedHiddenSlugs = useMemo(
    () => loadHiddenManifestSlugs(projectId),
    [projectId],
  );
  const [hiddenOverride, setHiddenOverride] =
    useState<{ projectId: string; slugs: Set<string> } | null>(null);
  const hiddenManifestSlugs =
    hiddenOverride && hiddenOverride.projectId === projectId
      ? hiddenOverride.slugs
      : persistedHiddenSlugs;

  // Per-tool configure/rename panel state
  const [configuringId, setConfiguringId] = useState<string | null>(null);
  const [configDraft, setConfigDraft] = useState<ConfigDraft | null>(null);
  const [savingConfig, setSavingConfig] = useState(false);

  // Fetch registered tools on mount / projectId change
  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const result = await fetchProjectTools(projectId);
        if (!cancelled) setState({ status: 'ok', projectId, tools: result.tools });
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

  // Fetch manifest-declared tools (project-declared from .blueprint/tools.json)
  useEffect(() => {
    // Abort any previous in-flight manifest fetch when projectId changes.
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    async function loadManifest() {
      try {
        const data = await fetchProjectToolManifest(projectId, controller.signal);
        if (controller.signal.aborted) return;
        const skills = data.skills ?? [];
        if (data.error) {
          // Partial success: some tools loaded, but there's a validation warning.
          setManifestState({ status: 'ok', tools: data.tools, skills, warning: data.error });
        } else {
          setManifestState({ status: 'ok', tools: data.tools, skills });
        }
      } catch (err) {
        if (controller.signal.aborted) return;
        // Manifest failures are non-fatal — registered tools still render.
        setManifestState({
          status: 'error',
          message: err instanceof Error ? err.message : 'Failed to load manifest',
        });
      }
    }

    void loadManifest();
    return () => { controller.abort(); };
  }, [projectId]);

  // Fetch compute image status on mount when user is project owner.
  // This is a cheap read (no git clone) — the expensive drift check is
  // triggered on demand by the user clicking "Check drift".
  useEffect(() => {
    if (!isOwner) return;
    let cancelled = false;
    fetchComputeImageStatus(projectId)
      .then(data => {
        if (!cancelled) setComputeImageState({ status: 'ok', data, hasDrift: false });
      })
      .catch(err => {
        if (!cancelled) {
          setComputeImageState({
            status: 'error',
            message: err instanceof Error ? err.message : 'Failed to fetch compute image status',
          });
        }
      });
    return () => { cancelled = true; };
  }, [projectId, isOwner]);

  // Hide a manifest tool and persist the choice.
  function hideManifestTool(slug: string) {
    const next = new Set(hiddenManifestSlugs);
    next.add(slug);
    setHiddenOverride({ projectId, slugs: next });
    saveHiddenManifestSlugs(projectId, next);
  }

  // Clear all hidden manifest tools — the escape hatch so Hide is never a
  // permanent dead-end (mirrors the disabled-tool Enable affordance).
  function unhideAllManifestTools() {
    const next = new Set<string>();
    setHiddenOverride({ projectId, slugs: next });
    saveHiddenManifestSlugs(projectId, next);
  }

  async function refresh() {
    try {
      const result = await fetchProjectTools(projectId);
      setState({ status: 'ok', projectId, tools: result.tools });
    } catch (err) {
      setState({
        status: 'error',
        projectId,
        message: err instanceof Error ? err.message : 'Failed to refresh',
      });
    }
  }

  // Lazy-load tool kinds (shared by the Add form and per-tool Configure panel).
  // Returns the loaded kinds, or [] on failure.
  async function ensureToolKinds(): Promise<ToolKindConfig[]> {
    if (toolKinds !== null) return toolKinds;
    setLoadingKinds(true);
    try {
      const kinds = await fetchToolKinds();
      setToolKinds(kinds);
      return kinds;
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Failed to load tool kinds');
      setToolKinds([]);
      return [];
    } finally {
      setLoadingKinds(false);
    }
  }

  async function handleOpenAddForm() {
    setShowAddForm(true);
    setActionError(null);
    setLastCreatedSlug(null);
    const kinds = await ensureToolKinds();
    if (!selectedKind && kinds.length > 0) setSelectedKind(kinds[0].kind);
  }

  // Look up the config schema (JSON-Schema `properties`) declared for a kind.
  function schemaPropsForKind(kind: string): [string, Record<string, unknown>][] {
    const spec = toolKinds?.find(k => k.kind === kind);
    const props = (spec?.config_schema as { properties?: Record<string, Record<string, unknown>> } | undefined)
      ?.properties;
    return props ? Object.entries(props) : [];
  }

  async function handleOpenConfigure(tool: ProjectTool) {
    setActionError(null);
    if (configuringId === tool.id) {
      // Toggle closed.
      setConfiguringId(null);
      setConfigDraft(null);
      return;
    }
    await ensureToolKinds();
    // Seed the draft config from the tool's existing config, stringifying
    // values so every schema field is editable as text.
    const existing = tool.config ?? {};
    const config: Record<string, string> = {};
    for (const [key, val] of Object.entries(existing)) {
      config[key] = typeof val === 'string' ? val : JSON.stringify(val);
    }
    setConfigDraft({ title: tool.title, placement: tool.placement, config });
    setConfiguringId(tool.id);
  }

  async function handleSaveConfig(tool: ProjectTool) {
    if (!configDraft) return;
    const title = configDraft.title.trim();
    if (!title) {
      setActionError('Title must not be empty');
      return;
    }
    setSavingConfig(true);
    setActionError(null);
    try {
      const config: Record<string, unknown> = {};
      for (const [key, spec] of schemaPropsForKind(tool.kind)) {
        const raw = configDraft.config[key];
        if (raw === undefined || raw === '') continue;
        config[key] = coerceConfigValue(raw, (spec as { type?: string }).type);
      }
      await updateProjectToolApi(projectId, tool.id, {
        title,
        placement: configDraft.placement,
        ...(schemaPropsForKind(tool.kind).length > 0 ? { config } : {}),
      });
      setConfiguringId(null);
      setConfigDraft(null);
      void refresh();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Failed to update tool');
    } finally {
      setSavingConfig(false);
    }
  }

  async function handleCreate() {
    if (!selectedKind || !newTitle.trim()) return;
    setCreating(true);
    setActionError(null);
    try {
      const tool = await createProjectTool(projectId, { kind: selectedKind, title: newTitle.trim() });
      setShowAddForm(false);
      setNewTitle('');
      setLastCreatedSlug(tool.slug);
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
    setTogglingId(tool.id);
    setActionError(null);
    try {
      await updateProjectToolApi(projectId, tool.id, { enabled: !tool.enabled });
      void refresh();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Failed to update tool');
    } finally {
      setTogglingId(null);
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

  async function handleCheckDrift() {
    setDriftLoading(true);
    try {
      const data = await fetchComputeImageStatus(projectId, true);
      setComputeImageState({ status: 'ok', data, hasDrift: true });
    } catch (err) {
      setComputeImageState({
        status: 'error',
        message: err instanceof Error ? err.message : 'Failed to check drift',
      });
    } finally {
      setDriftLoading(false);
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
        {isOwner && (
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
        )}
      </div>
    );
  }

  // ── Owner-only baked compute-image status panel ───────────────────────

  function renderComputeImagePanel() {
    if (!isOwner || computeImageState.status === 'idle') return null;

    if (computeImageState.status === 'loading') {
      return (
        <div className="flex items-center gap-2 text-sm text-neutral-500">
          <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-neutral-600 border-t-neutral-300" />
          Loading compute image status…
        </div>
      );
    }

    if (computeImageState.status === 'error') {
      return (
        <div className="rounded border border-red-500/30 bg-red-500/10 p-2 text-xs text-red-400">
          Compute image: {computeImageState.message}
        </div>
      );
    }

    const { data } = computeImageState;

    if (data.status === 'no_compute_image_declared') {
      return (
        <div className="space-y-1">
          <h2 className="text-sm font-semibold text-neutral-100">Baked compute image</h2>
          <p className="text-sm text-neutral-500">No compute image declared for this project.</p>
        </div>
      );
    }

    if (data.status === 'never_built') {
      return (
        <div className="space-y-1">
          <h2 className="text-sm font-semibold text-neutral-100">Baked compute image</h2>
          <p className="text-sm text-neutral-500">Compute image declared but never built.</p>
        </div>
      );
    }

    return (
      <div className="space-y-1">
        <h2 className="text-sm font-semibold text-neutral-100">Baked compute image</h2>
        <div className="workbench-card space-y-2 rounded-md border p-3">
          {/* Digest / pinning status */}
          <div className="flex items-center gap-2">
            <span className="text-xs text-neutral-400">Ref:</span>
            {data.is_digest_pinned && data.digest ? (
              <code className="truncate text-xs text-neutral-200">{data.digest}</code>
            ) : (
              <span className="inline-flex items-center gap-1 rounded border border-red-500/30 bg-red-500/10 px-1.5 py-0.5 text-xs font-medium text-red-400">
                MUTABLE tag — reproducibility not guaranteed
              </span>
            )}
          </div>

          <div className="flex items-center gap-2">
            <span className="text-xs text-neutral-400">Tag:</span>
            <code className="text-xs text-neutral-200">{data.tag}</code>
          </div>

          {/* Built flag */}
          <div className="flex items-center gap-2">
            <span className="text-xs text-neutral-400">Built:</span>
            <span className={`text-xs font-medium ${data.built ? 'text-green-400' : 'text-amber-400'}`}>
              {data.built ? 'Yes' : 'No'}
            </span>
          </div>

          {/* Build time */}
          <div className="flex items-center gap-2">
            <span className="text-xs text-neutral-400">Last build:</span>
            <span className="text-xs text-neutral-200">{formatDateTime(data.updated_at)}</span>
          </div>

          {/* Drift section */}
          {data.drift && (
            <div className="flex items-center gap-2">
              <span className="text-xs text-neutral-400">Drift:</span>
              <span
                className={`text-xs font-medium ${
                  data.drift.drifted === true
                    ? 'text-amber-400'
                    : data.drift.drifted === false
                      ? 'text-green-400'
                      : 'text-neutral-400'
                }`}
              >
                {data.drift.drifted === true
                  ? 'Drifted'
                  : data.drift.drifted === false
                    ? 'Up to date'
                    : 'Unknown'}
              </span>
              {data.drift.reason && (
                <span className="text-xs text-neutral-500">({data.drift.reason})</span>
              )}
            </div>
          )}

          {/* Drift check control (only shown when no drift data yet, or to re-check) */}
          <button
            type="button"
            data-viewport-allow-small-target
            onClick={handleCheckDrift}
            disabled={driftLoading}
            className={`${actionBtn} border-neutral-700 bg-transparent text-neutral-400 hover:text-neutral-200 disabled:opacity-50`}
          >
            {driftLoading ? 'Checking…' : data.drift ? 'Re-check drift' : 'Check drift'}
          </button>
        </div>
      </div>
    );
  }

  // Owner-only inline rename + placement + schema-driven config editor.
  function renderConfigPanel(tool: ProjectTool): ReactNode {
    if (configuringId !== tool.id || !configDraft) return null;
    const schemaProps = schemaPropsForKind(tool.kind);
    return (
      <div className="workbench-card space-y-3 rounded-md border p-3">
        <div>
          <label className="block text-xs font-medium text-neutral-400">Title</label>
          <input
            type="text"
            value={configDraft.title}
            onChange={e => setConfigDraft(d => (d ? { ...d, title: e.target.value } : d))}
            className="mt-1 w-full rounded border border-neutral-700 bg-neutral-900 px-2 py-1.5 text-sm text-neutral-200"
          />
        </div>
        <div>
          <label className="block text-xs font-medium text-neutral-400">Placement</label>
          <select
            value={configDraft.placement}
            onChange={e => setConfigDraft(d => (d ? { ...d, placement: e.target.value } : d))}
            className="mt-1 w-full rounded border border-neutral-700 bg-neutral-900 px-2 py-1.5 text-sm text-neutral-200"
          >
            {TOOL_PLACEMENTS.map(p => (
              <option key={p} value={p}>{p}</option>
            ))}
          </select>
        </div>
        {schemaProps.map(([key, spec]) => (
          <div key={key}>
            <label className="block text-xs font-medium text-neutral-400">
              {key}
              <span className="ml-1 text-neutral-600">({(spec as { type?: string }).type ?? 'string'})</span>
            </label>
            <input
              type="text"
              value={configDraft.config[key] ?? ''}
              onChange={e => setConfigDraft(d =>
                d ? { ...d, config: { ...d.config, [key]: e.target.value } } : d)}
              className="mt-1 w-full rounded border border-neutral-700 bg-neutral-900 px-2 py-1.5 text-sm text-neutral-200"
            />
          </div>
        ))}
        <div className="flex gap-2">
          <button
            type="button"
            onClick={() => { setConfiguringId(null); setConfigDraft(null); setActionError(null); }}
            className={`${actionBtn} border-neutral-700 bg-transparent text-neutral-400 hover:text-neutral-200`}
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => handleSaveConfig(tool)}
            disabled={savingConfig || !configDraft.title.trim()}
            className={`${actionBtn} border-indigo-600 bg-indigo-600 text-white hover:bg-indigo-500 disabled:opacity-50`}
          >
            {savingConfig ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    );
  }

  function renderCreatedSlugNotice() {
    if (!lastCreatedSlug) return null;
    return (
      <p className="text-xs text-neutral-400" data-testid="created-slug-notice">
        Tool created — slug: <code className="text-neutral-200">{lastCreatedSlug}</code>
      </p>
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
    // Disabled DB-backed tools are kept visible (their own "Disabled" section
    // below, with working Enable / Remove controls) — filtering them out of
    // existence entirely was the permanent-disable trap.
    const enabledTools = effective.tools.filter(t => t.enabled);
    const disabledTools = effective.tools.filter(t => !t.enabled);
    const registeredTools = enabledTools.filter(t => getToolComponent(t.kind) !== undefined);
    const allManifestTools = manifestState.status === 'ok' ? manifestState.tools : [];
    const manifestTools = allManifestTools.filter(t => !hiddenManifestSlugs.has(t.slug));
    const hiddenManifestCount = allManifestTools.length - manifestTools.length;
    const manifestSkills = manifestState.status === 'ok' ? manifestState.skills : [];
    const hasAnyTool =
      registeredTools.length > 0 ||
      disabledTools.length > 0 ||
      manifestTools.length > 0 ||
      // Keep the section alive when every manifest tool is hidden so the
      // "Show hidden" restore affordance remains reachable.
      hiddenManifestCount > 0 ||
      manifestSkills.length > 0;

    // A durable "N hidden" restore bar so hiding is never itself a trap — shown
    // even in the empty state so the last hidden tool can always be recovered.
    const restoreBar = hiddenManifestCount > 0 ? (
      <div className="flex items-center gap-2 text-xs text-neutral-500">
        <span>{hiddenManifestCount} project-declared tool{hiddenManifestCount === 1 ? '' : 's'} hidden.</span>
        <button
          type="button"
          data-viewport-allow-small-target
          onClick={unhideAllManifestTools}
          className="text-xs font-semibold text-indigo-400 hover:text-indigo-300"
        >
          Show hidden
        </button>
      </div>
    ) : null;

    if (!hasAnyTool) {
      return (
        <div className="space-y-3">
          <p className="text-sm text-neutral-500">No tools registered for this project.</p>
          {restoreBar}
        </div>
      );
    }

    return (
      <div className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {/* Registered (DB-backed) tools with management controls */}
          {registeredTools.map(tool => {
            const ToolComponent = getToolComponent(tool.kind);
            if (!ToolComponent) return null;
            return (
              <div
                key={tool.id}
                className={`min-w-0 space-y-1.5${tool.enabled ? '' : ' opacity-60'}`}
              >
                <WorkbenchPanel
                  title={tool.title}
                  meta={
                    tool.enabled ? undefined : (
                      <span className="inline-flex items-center gap-1 rounded border border-neutral-600 px-1.5 py-0.5 text-ui-sm font-medium uppercase tracking-wider text-neutral-400">
                        Disabled
                      </span>
                    )
                  }
                >
                  {tool.enabled ? (
                    <ToolComponent projectId={projectId} tool={tool} />
                  ) : (
                    <p className="py-8 text-center text-xs text-neutral-500">
                      This tool is disabled. Enable it to use it.
                    </p>
                  )}
                </WorkbenchPanel>
                {/* Per-tool management actions (owner only) */}
                {isOwner && (
                  <div className="flex items-center gap-2 px-1">
                    <button
                      type="button"
                      data-viewport-allow-small-target
                      onClick={() => handleOpenConfigure(tool)}
                      className="text-xs text-neutral-500 hover:text-neutral-300"
                    >
                      Configure
                    </button>
                    <button
                      type="button"
                      data-viewport-allow-small-target
                      onClick={() => handleToggleEnabled(tool)}
                      disabled={togglingId === tool.id}
                      className="text-xs text-neutral-500 hover:text-neutral-300 disabled:opacity-50"
                    >
                      {/* Always "Disable" here: this list is filtered to enabled
                          tools. Disabled tools move to the "Disabled" section
                          below, which carries its own Enable control — so a
                          disabled tool is never stranded. */}
                      {togglingId === tool.id ? 'Disabling…' : 'Disable'}
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
                )}
                {renderConfigPanel(tool)}
              </div>
            );
          })}

          {/* Manifest-declared (project-repo) tools — read-only, rendered via iframe */}
          {manifestTools.map(tool => (
            <div key={`manifest:${tool.slug}`} className="min-w-0 space-y-1.5">
              <WorkbenchPanel
                title={tool.title}
                meta={
                  <span className="inline-flex items-center gap-1 rounded border border-neutral-600 px-1.5 py-0.5 text-ui-sm font-medium uppercase tracking-wider text-neutral-400">
                    Project-declared
                  </span>
                }
              >
                <ProjectToolIframe projectId={projectId} tool={tool} />
              </WorkbenchPanel>
              <div className="flex items-center gap-2 px-1">
                <button
                  type="button"
                  data-viewport-allow-small-target
                  onClick={() => hideManifestTool(tool.slug)}
                  className="text-xs text-neutral-500 hover:text-neutral-300"
                >
                  Hide
                </button>
              </div>
            </div>
          ))}

          {/* Manifest warning (non-fatal validation error) */}
          {manifestState.status === 'ok' && manifestState.warning && (
            <div className="col-span-full rounded border border-amber-500/30 bg-amber-500/10 p-2 text-xs text-amber-400">
              Manifest warning: {manifestState.warning}
            </div>
          )}

          {/* Escape hatch: restore any hidden project-declared tools so Hide is
              never a permanent dead-end. */}
          {hiddenManifestCount > 0 && (
            <div className="col-span-full flex items-center gap-2 text-xs text-neutral-500">
              <span>
                {hiddenManifestCount} project-declared tool{hiddenManifestCount === 1 ? '' : 's'} hidden
              </span>
              <button
                type="button"
                data-viewport-allow-small-target
                onClick={unhideAllManifestTools}
                className="text-neutral-400 underline hover:text-neutral-200"
              >
                Show hidden
              </button>
            </div>
          )}
        </div>

        {restoreBar}

        {/* Disabled tools — kept visible so they can be re-enabled or removed
            (a disabled tool must never become a permanent dead-end). Owner-only:
            members have no mutation controls here, so the section is hidden. */}
        {isOwner && disabledTools.length > 0 && (
          <div className="space-y-2">
            <h3 className="text-sm font-semibold text-neutral-100">Disabled</h3>
            <div className="space-y-1">
              {disabledTools.map(tool => (
                <div
                  key={`disabled:${tool.id}`}
                  className="flex items-center justify-between gap-2 rounded-md border border-workbench-border bg-workbench-layer px-3 py-2"
                >
                  <span className="min-w-0 truncate text-sm text-neutral-400">{tool.title}</span>
                  <div className="flex shrink-0 items-center gap-2">
                    <button
                      type="button"
                      data-viewport-allow-small-target
                      onClick={() => handleToggleEnabled(tool)}
                      disabled={togglingId === tool.id}
                      className="text-xs text-neutral-400 hover:text-neutral-200 disabled:opacity-50"
                    >
                      {togglingId === tool.id ? 'Enabling…' : 'Enable'}
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
              ))}
            </div>
          </div>
        )}

        {/* Project-declared skills — read-only list from .blueprint/tools.json */}
        {manifestSkills.length > 0 && (
          <div className="space-y-2">
            <h3 className="text-sm font-semibold text-neutral-100">Skills</h3>
            <div className="space-y-1">
              {manifestSkills.map(skill => (
                <div
                  key={`skill:${skill.slug}`}
                  className="flex flex-col gap-0.5 rounded-md border border-workbench-border bg-workbench-layer px-3 py-2"
                >
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium text-neutral-200">{skill.title}</span>
                    <span className="inline-flex items-center gap-1 rounded border border-neutral-600 px-1.5 py-0.5 text-ui-sm font-medium uppercase tracking-wider text-neutral-400">
                      Skill
                    </span>
                  </div>
                  <code className="text-xs text-neutral-400">{skill.command}</code>
                  {skill.description && (
                    <p className="text-xs text-neutral-500">{skill.description}</p>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    );
  }

  // ── Main render ────────────────────────────────────────────────────────

  return (
    <div className="space-y-3">
      {renderManagementBar()}
      {actionError && <p className="text-sm text-red-400">{actionError}</p>}
      {renderComputeImagePanel()}
      {renderAddForm()}
      {renderCreatedSlugNotice()}
      {renderError()}
      {renderLoading()}
      {renderToolContent()}
    </div>
  );
}
