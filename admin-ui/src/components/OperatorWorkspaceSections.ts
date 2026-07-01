import {
  Activity,
  Bell,
  Gauge,
  GitBranch,
  Server,
  Settings,
  ShieldCheck,
  SlidersHorizontal,
  Users,
  Wrench,
  type LucideIcon,
} from 'lucide-react';
import type { DirectionsTab } from './DirectionsPanelHeader';
import type { SectionId } from './SettingsDropdown';

export type ProjectSection = DirectionsTab | 'manage';
export type ProjectManageSection =
  | 'overview'
  | Extract<
      SectionId,
      | 'build-config'
      | 'branches'
      | 'auto-review'
      | 'resource-envelope'
      | 'auto-remediation'
      | 'issue-implementation'
      | 'alert-domains'
      | 'access'
    >;
export type RuntimeSection = 'health' | 'devices' | 'compute' | 'accounts' | 'settings' | 'analytics';
export type RuntimeSettingsSection = Extract<
  SectionId,
  | 'general'
  | 'orchestrator'
  | 'deploy'
  | 'provider-routing'
  | 'provider-settings'
  | 'notifications'
  | 'advanced'
>;

export type SectionNavItem<T extends string> = {
  id: T;
  label: string;
  description: string;
  icon: LucideIcon;
  adminOnly?: boolean;
};

export const PROJECT_MANAGE_SECTIONS: SectionNavItem<ProjectManageSection>[] = [
  { id: 'overview', label: 'Overview', description: 'Repository, role, and project actions.', icon: Activity },
  { id: 'build-config', label: 'Build', description: 'Detection, test, and install commands.', icon: Wrench },
  { id: 'branches', label: 'Branches', description: 'Integration and promotion targets.', icon: GitBranch },
  { id: 'auto-review', label: 'Auto-review', description: 'Review gates and autonomous decisions.', icon: Gauge },
  { id: 'resource-envelope', label: 'Resources', description: 'Project runtime and budget envelope.', icon: Server },
  { id: 'auto-remediation', label: 'Remediation', description: 'Failure recovery behavior.', icon: ShieldCheck },
  { id: 'issue-implementation', label: 'Issues', description: 'Issue implementation policy.', icon: Settings },
  { id: 'alert-domains', label: 'Alerts', description: 'Project alert domain routing.', icon: Bell },
  { id: 'access', label: 'Access', description: 'Project members and user access.', icon: Users },
];

export const RUNTIME_SETTINGS_SECTIONS: SectionNavItem<RuntimeSettingsSection>[] = [
  { id: 'general', label: 'General', description: 'Defaults, refresh, and local UI behavior.', icon: SlidersHorizontal },
  { id: 'orchestrator', label: 'Orchestrator', description: 'Global slots, retries, and automation limits.', icon: Server },
  { id: 'deploy', label: 'Deploy', description: 'Pipeline health and deployment controls.', icon: ShieldCheck, adminOnly: true },
  { id: 'provider-routing', label: 'Provider routing', description: 'Task and category routing policy.', icon: GitBranch, adminOnly: true },
  { id: 'provider-settings', label: 'Provider settings', description: 'Model defaults and effort routing.', icon: Settings, adminOnly: true },
  { id: 'notifications', label: 'Notifications', description: 'Webhook, event, and spend alerts.', icon: Bell },
  { id: 'advanced', label: 'Advanced', description: 'Health-check endpoints and diagnostics.', icon: Wrench },
];

export const RUNTIME_SECTIONS: SectionNavItem<RuntimeSection>[] = [
  { id: 'health', label: 'Overview', description: 'Runtime health, task pressure, and fleet status at a glance.', icon: Activity },
  { id: 'devices', label: 'Devices', description: 'Warm VMs, local runner hardware, and project-scoped device readiness.', icon: Server },
  { id: 'compute', label: 'Compute', description: 'Running jobs, queued work, held approvals, and terminal run analysis.', icon: Gauge },
  { id: 'accounts', label: 'Accounts', description: 'GitHub identity, LLM account roster, usage caps, and pool availability.', icon: Users },
  { id: 'settings', label: 'Settings', description: 'Runtime defaults, automation limits, providers, deploy controls, and alerts.', icon: SlidersHorizontal },
  { id: 'analytics', label: 'Analytics', description: 'Cost, account health, portfolio signals, and provider/model breakdowns.', icon: Activity },
];

export function visibleRuntimeSettingsSections(userRole: 'admin' | 'user' | null) {
  return RUNTIME_SETTINGS_SECTIONS.filter(item => !item.adminOnly || userRole === 'admin');
}

export function resolveProjectManageItem(section: ProjectManageSection) {
  return PROJECT_MANAGE_SECTIONS.find(item => item.id === section) ?? PROJECT_MANAGE_SECTIONS[0]!;
}

export function resolveRuntimeItem(section: RuntimeSection) {
  return RUNTIME_SECTIONS.find(item => item.id === section) ?? RUNTIME_SECTIONS[0]!;
}

export function resolveRuntimeSettingsItem(
  section: RuntimeSettingsSection,
  userRole: 'admin' | 'user' | null,
) {
  const sections = visibleRuntimeSettingsSections(userRole);
  return sections.find(item => item.id === section) ?? sections[0]!;
}
