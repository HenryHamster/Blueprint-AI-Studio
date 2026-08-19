import { useState } from 'react';
import { CheckCircle2, Circle } from 'lucide-react';
import { timeAgo } from '../utils/time';
import { useTaskCapabilities } from '../taskCapabilities';
import type { UserLaunchableTaskType } from '../taskCapabilities';
import type { ViewMode } from '../hooks/useViewPreferences';
import type { OnboardingProof } from '../types/project';

const REFRESH_PRESETS = [
  { label: '10s', value: 10000 },
  { label: '30s', value: 30000 },
  { label: '60s', value: 60000 },
  { label: 'Off', value: 0 },
];

const PAGE_PRESETS = [25, 50, 100, 200];
const MAX_PAGE_SIZE = 200;

interface SettingsGeneralSectionProps {
  apiHealthy: boolean;
  wsConnected: boolean;
  lastFetchTime: number;
  defaultTaskType: UserLaunchableTaskType;
  onUpdateDefaultTaskType: (value: UserLaunchableTaskType) => void;
  viewMode: ViewMode;
  onSetViewMode: (mode: ViewMode) => void;
  allModelOptions: string[];
  refreshInterval: number;
  onUpdateRefreshInterval: (value: number) => void;
  pageSize: number;
  onUpdatePageSize: (value: number) => void;
  notifyOnComplete: boolean;
  onUpdateNotifyOnComplete: (value: boolean) => void;
  notifyOnFail: boolean;
  onUpdateNotifyOnFail: (value: boolean) => void;
  notifyOnApprovalNeeded: boolean;
  onUpdateNotifyOnApprovalNeeded: (value: boolean) => void;
  soundOnComplete: boolean;
  onUpdateSoundOnComplete: (value: boolean) => void;
  notifyOnHighSignalMessage: boolean;
  onUpdateNotifyOnHighSignalMessage: (value: boolean) => void;
  onNavigateToNotifications: () => void;
  selectedProjectOnboardingProof?: OnboardingProof | null;
}

function Toggle({ enabled, onChange }: { enabled: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      data-viewport-allow-small-target
      onClick={() => onChange(!enabled)}
      className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors shrink-0 ${enabled ? 'bg-emerald-600' : 'bg-workbench-layer'}`}
    >
      <span className={`inline-block h-3.5 w-3.5 rounded-full bg-white transition-transform ${enabled ? 'translate-x-[18px]' : 'translate-x-[3px]'}`} />
    </button>
  );
}

export function SettingsGeneralSection({
  apiHealthy,
  wsConnected,
  lastFetchTime,
  defaultTaskType,
  onUpdateDefaultTaskType,
  viewMode,
  onSetViewMode,
  allModelOptions,
  refreshInterval,
  onUpdateRefreshInterval,
  pageSize,
  onUpdatePageSize,
  notifyOnComplete,
  onUpdateNotifyOnComplete,
  notifyOnFail,
  onUpdateNotifyOnFail,
  notifyOnApprovalNeeded,
  onUpdateNotifyOnApprovalNeeded,
  soundOnComplete,
  onUpdateSoundOnComplete,
  notifyOnHighSignalMessage,
  onUpdateNotifyOnHighSignalMessage,
  onNavigateToNotifications,
  selectedProjectOnboardingProof,
}: SettingsGeneralSectionProps) {
  const { userLaunchableOptions } = useTaskCapabilities();
  const [refreshInputVal, setRefreshInputVal] = useState('');
  const [pageSizeInputVal, setPageSizeInputVal] = useState('');
  const [defaultModel, setDefaultModel] = useState<string>(
    () => JSON.parse(localStorage.getItem('blueprint-admin:defaultModel') ?? 'null') ?? ''
  );
  const [defaultEffort, setDefaultEffort] = useState<string>(
    () => JSON.parse(localStorage.getItem('blueprint-admin:defaultEffort') ?? 'null') ?? ''
  );

  const milestones = selectedProjectOnboardingProof ? [
    { key: 'repo_added_at', label: 'Repository Added' },
    { key: 'bootstrap_completed_at', label: 'Bootstrap Completed' },
    { key: 'first_task_spawned_at', label: 'First Task Spawned' },
    { key: 'first_pr_opened_at', label: 'First PR Opened' },
    { key: 'first_merge_gate_verdict_at', label: 'First Merge Gate Verdict' },
    { key: 'first_pr_merged_at', label: 'First PR Merged' },
    { key: 'first_autonomous_improvement_at', label: 'First Autonomous Improvement' },
  ] as const : [];

  return (
    <div className="space-y-6">
      {/* Onboarding progress */}
      {selectedProjectOnboardingProof && milestones.length > 0 && (
        <div>
          <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Onboarding Progress</h3>
          <div className="space-y-2">
            {milestones.map(m => {
              const value = selectedProjectOnboardingProof[m.key as keyof OnboardingProof];
              const completed = value !== null && value !== undefined;
              return (
                <div key={m.key} className="flex items-center gap-3">
                  {completed ? (
                    <CheckCircle2 className="w-4 h-4 text-emerald-500 shrink-0" />
                  ) : (
                    <Circle className="w-4 h-4 text-neutral-600 shrink-0" />
                  )}
                  <span className={`text-xs ${completed ? 'text-neutral-300' : 'text-neutral-500'}`}>
                    {m.label}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Connection status */}
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Connection Status</h3>
        <div className={`flex items-center gap-5 px-3 py-2.5 rounded-lg border ${!apiHealthy ? 'bg-red-950/20 border-red-900/40' : 'bg-workbench-base border-neutral-800'}`}>
          <span className="flex items-center gap-2">
            <span className={`w-2 h-2 rounded-full ${apiHealthy ? 'bg-emerald-500' : 'bg-red-500 animate-pulse'}`} />
            <span className={`text-xs ${apiHealthy ? 'text-neutral-300' : 'text-red-400'}`}>{apiHealthy ? 'API Connected' : 'API Error'}</span>
          </span>
          <span className="flex items-center gap-2">
            <span className={`w-2 h-2 rounded-full ${wsConnected ? 'bg-blue-500' : 'bg-workbench-card'}`} />
            <span className={`text-xs ${wsConnected ? 'text-blue-400' : 'text-neutral-500'}`}>{wsConnected ? 'WebSocket Active' : 'WebSocket Idle'}</span>
          </span>
          <span className="text-xs text-neutral-500">Fetched {timeAgo(lastFetchTime)}</span>
        </div>
      </div>

      {/* Defaults */}
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Defaults</h3>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <label className="block text-xs font-medium text-neutral-400 mb-1.5">Default Task Type</label>
            <select
              value={defaultTaskType}
              onChange={e => onUpdateDefaultTaskType(e.target.value as UserLaunchableTaskType)}
              className="w-full bg-workbench-shell border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm workbench-focus"
            >
              {userLaunchableOptions.map(option => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-neutral-400 mb-1.5">Default View Mode</label>
            <select
              value={viewMode}
              onChange={e => onSetViewMode(e.target.value as ViewMode)}
              className="w-full bg-workbench-shell border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm workbench-focus"
            >
              <option value="kanban">Kanban</option>
              <option value="pipeline">Pipeline</option>
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-neutral-400 mb-1.5">Default Model</label>
            <select
              value={defaultModel}
              onChange={e => {
                setDefaultModel(e.target.value);
                localStorage.setItem('blueprint-admin:defaultModel', JSON.stringify(e.target.value));
              }}
              className="w-full bg-workbench-shell border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm workbench-focus"
            >
              <option value="">Orchestrator default</option>
              {allModelOptions.map(model => (
                <option key={model} value={model}>{model}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-neutral-400 mb-1.5">Default Effort</label>
            <select
              value={defaultEffort}
              onChange={e => {
                setDefaultEffort(e.target.value);
                localStorage.setItem('blueprint-admin:defaultEffort', JSON.stringify(e.target.value));
              }}
              className="w-full bg-workbench-shell border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm workbench-focus"
            >
              <option value="">Orchestrator default</option>
              <option value="low">low</option>
              <option value="medium">medium</option>
              <option value="high">high</option>
              <option value="max">max</option>
            </select>
          </div>
        </div>
      </div>

      {/* UI Preferences */}
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">UI Preferences</h3>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <label className="block text-xs font-medium text-neutral-400 mb-1.5">Auto-Refresh Interval</label>
            <div className="flex gap-1 mb-1.5">
              {REFRESH_PRESETS.map(p => (
                <button
                  key={p.value}
                  onClick={() => { onUpdateRefreshInterval(p.value); setRefreshInputVal(''); }}
                  className={`flex-1 text-xs py-3.5 rounded-lg border transition-colors ${refreshInterval === p.value && !refreshInputVal ? 'bg-workbench-card border-neutral-500 text-neutral-100' : 'bg-workbench-shell border-neutral-700 text-neutral-400 hover:text-neutral-200'}`}
                >
                  {p.label}
                </button>
              ))}
            </div>
            <div className="flex items-center gap-2">
              <input
                type="number"
                min={1}
                placeholder="Custom (s)"
                value={refreshInputVal}
                onChange={e => setRefreshInputVal(e.target.value)}
                onBlur={() => {
                  const secs = parseInt(refreshInputVal);
                  if (secs > 0) onUpdateRefreshInterval(secs * 1000);
                  setRefreshInputVal('');
                }}
                onKeyDown={e => {
                  if (e.key === 'Enter') {
                    const secs = parseInt(refreshInputVal);
                    if (secs > 0) onUpdateRefreshInterval(secs * 1000);
                    setRefreshInputVal('');
                  }
                }}
                className="flex-1 bg-workbench-shell border border-neutral-700 text-neutral-100 rounded-lg px-3 py-1.5 text-xs font-mono placeholder-neutral-600 workbench-focus"
              />
              <span className="text-xs text-neutral-500 shrink-0">
                {refreshInterval === 0 ? 'off' : `${refreshInterval / 1000}s`}
              </span>
            </div>
          </div>
          <div>
            <label className="block text-xs font-medium text-neutral-400 mb-1.5">Tasks Per Page</label>
            <div className="flex gap-1 mb-1.5">
              {PAGE_PRESETS.map(p => (
                <button
                  key={p}
                  onClick={() => { onUpdatePageSize(p); setPageSizeInputVal(''); }}
                  className={`flex-1 text-xs py-3.5 rounded-lg border transition-colors ${pageSize === p && !pageSizeInputVal ? 'bg-workbench-card border-neutral-500 text-neutral-100' : 'bg-workbench-shell border-neutral-700 text-neutral-400 hover:text-neutral-200'}`}
                >
                  {p}
                </button>
              ))}
            </div>
            <div className="flex items-center gap-2">
              <input
                type="number"
                min={1}
                max={MAX_PAGE_SIZE}
                placeholder="Custom"
                value={pageSizeInputVal}
                onChange={e => setPageSizeInputVal(e.target.value)}
                onBlur={() => {
                  const n = parseInt(pageSizeInputVal);
                  if (n > 0) onUpdatePageSize(Math.min(n, MAX_PAGE_SIZE));
                  setPageSizeInputVal('');
                }}
                onKeyDown={e => {
                  if (e.key === 'Enter') {
                    const n = parseInt(pageSizeInputVal);
                    if (n > 0) onUpdatePageSize(Math.min(n, MAX_PAGE_SIZE));
                    setPageSizeInputVal('');
                  }
                }}
                className="flex-1 bg-workbench-shell border border-neutral-700 text-neutral-100 rounded-lg px-3 py-1.5 text-xs font-mono placeholder-neutral-600 workbench-focus"
              />
              <span className="text-xs text-neutral-500 shrink-0">
                {pageSize}
              </span>
            </div>
          </div>
        </div>
      </div>

      {/* Notifications */}
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Notifications</h3>
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <span className="text-xs text-neutral-400">Browser notification on task completion</span>
            <Toggle enabled={notifyOnComplete} onChange={v => {
              if (v && 'Notification' in window && Notification.permission === 'default') Notification.requestPermission();
              onUpdateNotifyOnComplete(v);
            }} />
          </div>
          <div className="flex items-center justify-between">
            <span className="text-xs text-neutral-400">Browser notification on failure</span>
            <Toggle enabled={notifyOnFail} onChange={v => {
              if (v && 'Notification' in window && Notification.permission === 'default') Notification.requestPermission();
              onUpdateNotifyOnFail(v);
            }} />
          </div>
          <div className="flex items-center justify-between">
            <span className="text-xs text-neutral-400">Browser notification on approval needed</span>
            <Toggle enabled={notifyOnApprovalNeeded} onChange={v => {
              if (v && 'Notification' in window && Notification.permission === 'default') Notification.requestPermission();
              onUpdateNotifyOnApprovalNeeded(v);
            }} />
          </div>
          <div className="flex items-center justify-between">
            <span className="text-xs text-neutral-400">Play sound on task completion</span>
            <Toggle enabled={soundOnComplete} onChange={onUpdateSoundOnComplete} />
          </div>
          <div className="flex items-center justify-between">
            <span className="text-xs text-neutral-400">Browser notification on high-signal message</span>
            <Toggle enabled={notifyOnHighSignalMessage} onChange={v => {
              if (v && 'Notification' in window && Notification.permission === 'default') Notification.requestPermission();
              onUpdateNotifyOnHighSignalMessage(v);
            }} />
          </div>
          <p className="text-ui-md text-neutral-600 pt-1">
            These are browser-only notifications. For external webhooks (Slack, PagerDuty, etc.), see the{' '}
            <button
              data-viewport-allow-small-target
              onClick={onNavigateToNotifications}
              className="text-neutral-400 underline hover:text-neutral-200 transition-colors"
            >
              Notifications tab
            </button>.
          </p>
        </div>
      </div>
    </div>
  );
}
