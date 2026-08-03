import type { ComponentType } from 'react';
import type { ProjectTool } from './api/projectTools';

export interface ToolComponentProps {
  projectId: string;
  tool: ProjectTool;
  /** Viewer-safe rendering: content remains visible but no writes or handoffs. */
  readOnly: boolean;
}

type ToolComponent = ComponentType<ToolComponentProps>;

const registry = new Map<string, ToolComponent>();

export function registerToolKind(kind: string, component: ToolComponent): void {
  registry.set(kind, component);
}

export function getToolComponent(kind: string): ToolComponent | undefined {
  return registry.get(kind);
}
