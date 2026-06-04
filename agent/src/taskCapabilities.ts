export const TASK_CAPABILITIES = [
  { id: 'create', label: 'Create', agentMode: 'write' },
  { id: 'review', label: 'Review', agentMode: 'read_only' },
  { id: 'fix', label: 'Fix', agentMode: 'write' },
  { id: 'plan', label: 'Plan', agentMode: 'read_only' },
  { id: 'research', label: 'Research', agentMode: 'read_only' },
  { id: 'strategic_review', label: 'Strategic Review', agentMode: 'read_only' },
  { id: 'design_doc_update', label: 'Design Doc', agentMode: 'read_only' },
  { id: 'security_audit', label: 'Security Audit', agentMode: 'read_only' },
  { id: 'qa', label: 'QA', agentMode: 'write' },
  { id: 'deps', label: 'Deps', agentMode: 'write' },
  { id: 'docs', label: 'Docs', agentMode: 'write' },
  { id: 'migrate', label: 'Migrate', agentMode: 'write' },
  { id: 'smoketest', label: 'Smoketest', agentMode: 'read_only' },
  { id: 'refactor', label: 'Refactor', agentMode: 'write' },
  { id: 'debug', label: 'Debug', agentMode: 'read_only' },
  { id: 'perf', label: 'Perf', agentMode: 'read_only' },
  { id: 'investigate', label: 'Investigate', agentMode: 'read_only' },
  { id: 'self_audit', label: 'Self Audit', agentMode: 'read_only' },
  { id: 'release_audit', label: 'Release Audit', agentMode: 'read_only' },
  { id: 'memo', label: 'Memo', agentMode: 'read_only' },
  { id: 'verify_tests', label: 'Verify Tests', agentMode: 'read_only' },
  { id: 'compute_experiment', label: 'Compute Experiment', agentMode: 'read_only' },
  { id: 'web_research', label: 'Web Research', agentMode: 'read_only' },
] as const;

export type TaskCapability = (typeof TASK_CAPABILITIES)[number];
export type AgentTaskType = TaskCapability['id'];
type ReadOnlyCapability = Extract<TaskCapability, { agentMode: 'read_only' }>;
type WriteCapability = Extract<TaskCapability, { agentMode: 'write' }>;

export const ALL_AGENT_TASK_TYPES: readonly AgentTaskType[] = TASK_CAPABILITIES.map(capability => capability.id);
export const READ_ONLY_TASK_TYPES: readonly ReadOnlyCapability['id'][] = TASK_CAPABILITIES
  .filter((capability): capability is ReadOnlyCapability => capability.agentMode === 'read_only')
  .map(capability => capability.id);
export const WRITE_TASK_TYPES: readonly WriteCapability['id'][] = TASK_CAPABILITIES
  .filter((capability): capability is WriteCapability => capability.agentMode === 'write')
  .map(capability => capability.id);

const READ_ONLY_TASK_TYPE_SET = new Set<string>(READ_ONLY_TASK_TYPES);

export function isReadOnlyTaskType(taskType: string): boolean {
  return READ_ONLY_TASK_TYPE_SET.has(taskType);
}
