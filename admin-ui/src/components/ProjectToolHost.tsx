import { useEffect, useState } from 'react';
import { fetchProjectTools, type ProjectTool } from '../api/projectTools';
import { getToolComponent } from '../toolRegistry';
import { WorkbenchPanel } from './WorkbenchLayout';

interface Props {
  projectId: string;
}

type FetchState =
  | { status: 'loading'; projectId: string }
  | { status: 'error'; projectId: string; message: string }
  | { status: 'ok'; projectId: string; tools: ProjectTool[] };

export function ProjectToolHost({ projectId }: Props) {
  const [state, setState] = useState<FetchState>({ status: 'loading', projectId });

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

  const effective: FetchState = state.projectId === projectId
    ? state
    : { status: 'loading', projectId };

  if (effective.status === 'error') {
    return (
      <WorkbenchPanel title="Tools">
        <p className="text-sm text-red-400">{effective.message}</p>
      </WorkbenchPanel>
    );
  }

  if (effective.status === 'loading') {
    return (
      <WorkbenchPanel title="Tools">
        <div className="flex items-center gap-2 text-sm text-neutral-500">
          <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-neutral-600 border-t-neutral-300" />
          Loading tools…
        </div>
      </WorkbenchPanel>
    );
  }

  const enabledTools = effective.tools.filter(t => t.enabled);
  const registeredTools = enabledTools.filter(t => getToolComponent(t.kind) !== undefined);

  if (registeredTools.length === 0) {
    return (
      <WorkbenchPanel title="Tools">
        <p className="text-sm text-neutral-500">No tools registered for this project.</p>
      </WorkbenchPanel>
    );
  }

  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
      {registeredTools.map(tool => {
        const ToolComponent = getToolComponent(tool.kind);
        if (!ToolComponent) return null;
        return (
          <div key={tool.id} className="min-w-0">
            <WorkbenchPanel title={tool.title}>
              <ToolComponent projectId={projectId} tool={tool} />
            </WorkbenchPanel>
          </div>
        );
      })}
    </div>
  );
}
