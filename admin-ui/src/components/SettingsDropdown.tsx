import { useEffect, useState } from 'react';
import { Settings, GitBranch, RotateCcw, Loader2, Shield, Play, Pause, RotateCw, Wrench, CheckCircle } from 'lucide-react';
import type { DeployState } from '../useAppState';


export interface MaxPlanAccount {
  id: string;
  name: string;
  enabled: boolean;
  active_tasks: number;
  total_tasks: number;
  token_created_at: string | null;
}


interface SettingsDropdownProps {
  showSettings: boolean;
  onToggleSettings: () => void;
  settingsRef: React.RefObject<HTMLDivElement | null>;
  // Defaults
  defaultTaskType: 'create' | 'plan' | 'research' | 'review' | 'fix' | 'migrate' | 'refactor';
  onUpdateDefaultTaskType: (value: 'create' | 'plan' | 'research' | 'review' | 'fix' | 'migrate' | 'refactor') => void;
  viewMode: 'kanban' | 'pipeline';
  onToggleViewMode: () => void;
  // Notifications
  notifyOnComplete: boolean;
  onUpdateNotifyOnComplete: (value: boolean) => void;
  soundOnComplete: boolean;
  onUpdateSoundOnComplete: (value: boolean) => void;
  // Existing
  maxConcurrentVMs: number;
  onUpdateMaxVMs: (value: number) => void;
  maxRetries: number;
  onUpdateMaxRetries: (value: number) => void;
  maxAutonomousTasksPerHour: number;
  onUpdateMaxAutonomousRate: (value: number) => void;
  autonomousResearchIntervalMinutes: number;
  onUpdateAutonomousResearchInterval: (value: number) => void;
  autonomousResearchModel: string;
  onUpdateAutonomousResearchModel: (value: string) => void;
  autonomousResearchEffort: string;
  onUpdateAutonomousResearchEffort: (value: string) => void;
  onSyncTaskTypeDefaults: () => void;
  githubAccount: string | null;
  onConnectGitHub: () => void;
  maxPlanAccounts: MaxPlanAccount[];
  onToggleAccount: (id: string, enabled: boolean) => void;
  onRemoveAccount: (id: string) => void;
  newAccountName: string;
  setNewAccountName: (value: string) => void;
  newAccountToken: string;
  setNewAccountToken: (value: string) => void;
  onAddAccount: () => void;
  refreshInterval: number;
  onUpdateRefreshInterval: (value: number) => void;
  pageSize: number;
  onUpdatePageSize: (value: number) => void;
  apiHealthy: boolean;
  wsConnected: boolean;
  lastFetchTime: number;
  onRestartOrchestrator: () => Promise<void>;
  anyAutonomousEnabled: boolean;
  onPauseAllAutonomous: () => Promise<void>;
  // Deploy state
  deployState: DeployState | null;
  onPausePipeline: () => Promise<void>;
  onResumePipeline: () => Promise<void>;
  onRollback: () => Promise<void>;
  onMarkStable: () => Promise<void>;
  onRepairState: () => Promise<void>;
}

function relativeTime(ms: number): string {
  if (!ms) return 'never';
  const diff = Math.floor((Date.now() - ms) / 1000);
  if (diff < 5) return 'just now';
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  return `${Math.floor(diff / 3600)}h ago`;
}

const REFRESH_PRESETS = [
  { label: '10s', value: 10000 },
  { label: '30s', value: 30000 },
  { label: '60s', value: 60000 },
  { label: 'Off', value: 0 },
];

const PAGE_PRESETS = [25, 50, 100, 200];
const ALL_PAGE_SIZE = 10000;

const TASK_TYPES = ['create', 'plan', 'research', 'review', 'fix', 'migrate', 'refactor'] as const;
type TaskType = typeof TASK_TYPES[number];

export function SettingsDropdown({
  showSettings,
  onToggleSettings,
  settingsRef,
  defaultTaskType,
  onUpdateDefaultTaskType,
  viewMode,
  onToggleViewMode,
  notifyOnComplete,
  onUpdateNotifyOnComplete,
  soundOnComplete,
  onUpdateSoundOnComplete,
  maxConcurrentVMs,
  onUpdateMaxVMs,
  maxRetries,
  onUpdateMaxRetries,
  maxAutonomousTasksPerHour,
  onUpdateMaxAutonomousRate,
  autonomousResearchIntervalMinutes,
  onUpdateAutonomousResearchInterval,
  autonomousResearchModel,
  onUpdateAutonomousResearchModel,
  autonomousResearchEffort,
  onUpdateAutonomousResearchEffort,
  onSyncTaskTypeDefaults,
  githubAccount,
  onConnectGitHub,
  maxPlanAccounts,
  onToggleAccount,
  onRemoveAccount,
  newAccountName,
  setNewAccountName,
  newAccountToken,
  setNewAccountToken,
  onAddAccount,
  refreshInterval,
  onUpdateRefreshInterval,
  pageSize,
  onUpdatePageSize,
  apiHealthy,
  wsConnected,
  lastFetchTime,
  onRestartOrchestrator,
  anyAutonomousEnabled,
  onPauseAllAutonomous,
  deployState,
  onPausePipeline,
  onResumePipeline,
  onRollback,
  onMarkStable,
  onRepairState,
}: SettingsDropdownProps) {
  const [, setTick] = useState(0);
  const [restarting, setRestarting] = useState(false);
  const [restartStatus, setRestartStatus] = useState<'idle' | 'success' | 'error'>('idle');
  const [refreshInputVal, setRefreshInputVal] = useState('');
  const [pageSizeInputVal, setPageSizeInputVal] = useState('');
  const [vmInputVal, setVmInputVal] = useState(String(maxConcurrentVMs));
  const [retriesInputVal, setRetriesInputVal] = useState(String(maxRetries));
  const [autonomousRateInputVal, setAutonomousRateInputVal] = useState(String(maxAutonomousTasksPerHour));

  // Global model/effort defaults
  const [defaultModel, setDefaultModel] = useState<string>(
    () => JSON.parse(localStorage.getItem('blueprint-admin:defaultModel') ?? 'null') ?? ''
  );
  const [defaultEffort, setDefaultEffort] = useState<string>(
    () => JSON.parse(localStorage.getItem('blueprint-admin:defaultEffort') ?? 'null') ?? ''
  );

  // Per-task-type overrides
  const [taskTypeModels, setTaskTypeModels] = useState<Record<TaskType, string>>(() => {
    const result = {} as Record<TaskType, string>;
    for (const t of ['create', 'plan', 'research', 'review', 'fix', 'migrate', 'refactor'] as TaskType[]) {
      result[t] = JSON.parse(localStorage.getItem(`blueprint-admin:taskTypeModel:${t}`) ?? 'null') ?? '';
    }
    return result;
  });
  const [taskTypeEfforts, setTaskTypeEfforts] = useState<Record<TaskType, string>>(() => {
    const result = {} as Record<TaskType, string>;
    for (const t of ['create', 'plan', 'research', 'review', 'fix', 'migrate', 'refactor'] as TaskType[]) {
      result[t] = JSON.parse(localStorage.getItem(`blueprint-admin:taskTypeEffort:${t}`) ?? 'null') ?? '';
    }
    return result;
  });
  useEffect(() => {
    const id = setInterval(() => setTick(t => t + 1), 1000);
    return () => clearInterval(id);
  }, []);
  useEffect(() => { setVmInputVal(String(maxConcurrentVMs)); }, [maxConcurrentVMs]);
  useEffect(() => { setRetriesInputVal(String(maxRetries)); }, [maxRetries]);
  useEffect(() => { setAutonomousRateInputVal(String(maxAutonomousTasksPerHour)); }, [maxAutonomousTasksPerHour]);

  return (
    <div className="relative" ref={settingsRef}>
      <button
        onClick={onToggleSettings}
        className="bg-neutral-800 hover:bg-neutral-700 text-neutral-300 p-2 rounded text-xs transition-colors border border-neutral-700"
        title="Settings"
      >
        <Settings className="w-4 h-4" />
      </button>
      {showSettings && (
        <div className="absolute right-0 top-full mt-2 w-80 bg-neutral-900 border border-neutral-700 rounded-lg shadow-2xl p-4 z-50 max-h-[70vh] overflow-y-auto custom-scrollbar">
          <div className={`flex items-center gap-4 mb-3 px-1 py-1.5 rounded ${ !apiHealthy ? 'bg-red-950/20' : ''}`}>
            <span className="flex items-center gap-1.5">
              <span className={`w-1.5 h-1.5 rounded-full ${apiHealthy ? 'bg-emerald-500' : 'bg-red-500 animate-pulse'}`} />
              <span className={`text-[10px] ${apiHealthy ? 'text-neutral-400' : 'text-red-400'}`}>{apiHealthy ? 'API Connected' : 'API Error'}</span>
            </span>
            <span className="flex items-center gap-1.5">
              <span className={`w-1.5 h-1.5 rounded-full ${wsConnected ? 'bg-blue-500' : 'bg-neutral-600'}`} />
              <span className={`text-[10px] ${wsConnected ? 'text-blue-400' : 'text-neutral-500'}`}>{wsConnected ? 'WS Active' : 'WS Idle'}</span>
            </span>
            <span className="text-[10px] text-neutral-500">Fetched {relativeTime(lastFetchTime)}</span>
          </div>

          <h3 className="text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-3">Defaults</h3>
          <div className="mb-4">
            <label className="block text-[11px] font-medium text-neutral-400 mb-1.5">Default Task Type</label>
            <select
              value={defaultTaskType}
              onChange={e => onUpdateDefaultTaskType(e.target.value as 'create' | 'plan' | 'research' | 'review' | 'fix' | 'migrate' | 'refactor')}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
            >
              <option value="create">Create</option>
              <option value="refactor">Refactor</option>
              <option value="plan">Plan</option>
              <option value="research">Research</option>
              <option value="review">Review</option>
              <option value="fix">Fix</option>
              <option value="migrate">Migrate</option>
            </select>
          </div>
          <div className="mb-4">
            <label className="block text-[11px] font-medium text-neutral-400 mb-1.5">Default View Mode</label>
            <button
              onClick={onToggleViewMode}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600 flex items-center justify-between"
            >
              <span>{viewMode === 'kanban' ? 'Kanban' : 'Pipeline'}</span>
              <span className="text-neutral-500 text-xs">Click to toggle</span>
            </button>
          </div>

          <div className="mb-4">
            <label className="block text-[11px] font-medium text-neutral-400 mb-1.5">Default Model</label>
            <select
              value={defaultModel}
              onChange={e => {
                setDefaultModel(e.target.value);
                localStorage.setItem('blueprint-admin:defaultModel', JSON.stringify(e.target.value));
              }}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
            >
              <option value="">Orchestrator default</option>
              <option value="claude-opus-4-6">claude-opus-4-6</option>
              <option value="claude-sonnet-4-6">claude-sonnet-4-6</option>
              <option value="claude-haiku-4-5-20251001">claude-haiku-4-5-20251001</option>
            </select>
          </div>
          <div className="mb-4">
            <label className="block text-[11px] font-medium text-neutral-400 mb-1.5">Default Effort</label>
            <select
              value={defaultEffort}
              onChange={e => {
                setDefaultEffort(e.target.value);
                localStorage.setItem('blueprint-admin:defaultEffort', JSON.stringify(e.target.value));
              }}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
            >
              <option value="">Orchestrator default</option>
              <option value="low">low</option>
              <option value="medium">medium</option>
              <option value="high">high</option>
              <option value="max">max</option>
            </select>
          </div>

          <div className="border-t border-neutral-800 pt-3 mb-3">
            <h4 className="text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2">Per-Task-Type Overrides</h4>
            <div className="space-y-2">
              {TASK_TYPES.map(type => (
                <div key={type} className="bg-neutral-950 border border-neutral-800 rounded px-2 py-2">
                  <div className="text-[11px] font-medium text-neutral-300 capitalize mb-2">{type}</div>
                  <div className="grid grid-cols-2 gap-2">
                    <div>
                      <div className="text-[10px] text-neutral-500 mb-1">Model</div>
                      <select
                        value={taskTypeModels[type]}
                        onChange={e => {
                          const val = e.target.value;
                          setTaskTypeModels(prev => ({ ...prev, [type]: val }));
                          localStorage.setItem(`blueprint-admin:taskTypeModel:${type}`, JSON.stringify(val));
                          setTimeout(onSyncTaskTypeDefaults, 0);
                        }}
                        className="w-full bg-neutral-900 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-600"
                      >
                        <option value="">default</option>
                        <option value="claude-opus-4-6">claude-opus-4-6</option>
                        <option value="claude-sonnet-4-6">claude-sonnet-4-6</option>
                        <option value="claude-haiku-4-5-20251001">claude-haiku-4-5-20251001</option>
                      </select>
                    </div>
                    <div>
                      <div className="text-[10px] text-neutral-500 mb-1">Effort</div>
                      <select
                        value={taskTypeEfforts[type]}
                        onChange={e => {
                          const val = e.target.value;
                          setTaskTypeEfforts(prev => ({ ...prev, [type]: val }));
                          localStorage.setItem(`blueprint-admin:taskTypeEffort:${type}`, JSON.stringify(val));
                          setTimeout(onSyncTaskTypeDefaults, 0);
                        }}
                        className="w-full bg-neutral-900 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-600"
                      >
                        <option value="">default</option>
                        <option value="low">low</option>
                        <option value="medium">medium</option>
                        <option value="high">high</option>
                        <option value="max">max</option>
                      </select>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div className="border-t border-neutral-800 pt-3 mb-4">
            <h4 className="text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2">Settings</h4>
          </div>
          <div className="mb-4">
            <label className="block text-[11px] font-medium text-neutral-400 mb-1.5">Max Concurrent VMs (1-30)</label>
            <input
              type="number"
              min={1}
              max={30}
              value={vmInputVal}
              onChange={e => setVmInputVal(e.target.value)}
              onBlur={() => { const n = parseInt(vmInputVal); if (n >= 1 && n <= 30) onUpdateMaxVMs(n); else setVmInputVal(String(maxConcurrentVMs)); }}
              onKeyDown={e => { if (e.key === 'Enter') { const n = parseInt(vmInputVal); if (n >= 1 && n <= 30) onUpdateMaxVMs(n); else setVmInputVal(String(maxConcurrentVMs)); } }}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
          <div className="mb-4">
            <label className="block text-[11px] font-medium text-neutral-400 mb-1.5">Max Retries (0-10)</label>
            <input
              type="number"
              min={0}
              max={10}
              value={retriesInputVal}
              onChange={e => setRetriesInputVal(e.target.value)}
              onBlur={() => { const n = parseInt(retriesInputVal); if (!isNaN(n) && n >= 0 && n <= 10) onUpdateMaxRetries(n); else setRetriesInputVal(String(maxRetries)); }}
              onKeyDown={e => { if (e.key === 'Enter') { const n = parseInt(retriesInputVal); if (!isNaN(n) && n >= 0 && n <= 10) onUpdateMaxRetries(n); else setRetriesInputVal(String(maxRetries)); } }}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
          <div className="mb-4">
            <label className="block text-[11px] font-medium text-neutral-400 mb-1.5">Max Autonomous Tasks/Hour (1-50)</label>
            <input
              type="number"
              min={1}
              max={50}
              value={autonomousRateInputVal}
              onChange={e => setAutonomousRateInputVal(e.target.value)}
              onBlur={() => { const n = parseInt(autonomousRateInputVal); if (n >= 1 && n <= 50) onUpdateMaxAutonomousRate(n); else setAutonomousRateInputVal(String(maxAutonomousTasksPerHour)); }}
              onKeyDown={e => { if (e.key === 'Enter') { const n = parseInt(autonomousRateInputVal); if (n >= 1 && n <= 50) onUpdateMaxAutonomousRate(n); else setAutonomousRateInputVal(String(maxAutonomousTasksPerHour)); } }}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>

          <div className="border-t border-neutral-800 pt-3 mb-3">
            <h4 className="text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2">Autonomous Research</h4>
          </div>
          <div className="mb-4">
            <label className="block text-[11px] font-medium text-neutral-400 mb-1.5">Research Interval (minutes)</label>
            <input
              type="number"
              min={1}
              max={1440}
              value={autonomousResearchIntervalMinutes}
              onChange={e => onUpdateAutonomousResearchInterval(parseInt(e.target.value) || 30)}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
          <div className="mb-4">
            <label className="block text-[11px] font-medium text-neutral-400 mb-1.5">Research Model</label>
            <select
              value={autonomousResearchModel}
              onChange={e => onUpdateAutonomousResearchModel(e.target.value)}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
            >
              <option value="">Orchestrator default</option>
              <option value="claude-opus-4-6">claude-opus-4-6</option>
              <option value="claude-sonnet-4-6">claude-sonnet-4-6</option>
              <option value="claude-haiku-4-5-20251001">claude-haiku-4-5-20251001</option>
            </select>
          </div>
          <div className="mb-4">
            <label className="block text-[11px] font-medium text-neutral-400 mb-1.5">Research Effort</label>
            <select
              value={autonomousResearchEffort}
              onChange={e => onUpdateAutonomousResearchEffort(e.target.value)}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
            >
              <option value="">Orchestrator default</option>
              <option value="low">low</option>
              <option value="medium">medium</option>
              <option value="high">high</option>
            </select>
          </div>

          <div className="mb-4">
            <button
              disabled={restarting}
              onClick={async () => {
                if (!window.confirm('Restart the orchestrator? In-flight task state will be preserved.')) return;
                setRestarting(true);
                setRestartStatus('idle');
                try {
                  await onRestartOrchestrator();
                  setRestartStatus('success');
                } catch {
                  setRestartStatus('error');
                } finally {
                  setRestarting(false);
                }
              }}
              className="w-full bg-neutral-800 hover:bg-amber-900/30 disabled:opacity-50 border border-neutral-700 text-amber-400 text-xs py-1.5 rounded transition-colors flex items-center justify-center gap-1.5"
            >
              {restarting
                ? <><Loader2 className="w-3 h-3 animate-spin" /> Restarting…</>
                : <><RotateCcw className="w-3 h-3" /> Restart Orchestrator</>}
            </button>
            {restartStatus === 'success' && <p className="text-[11px] text-emerald-400 mt-1">Orchestrator restarted successfully.</p>}
            {restartStatus === 'error' && <p className="text-[11px] text-red-400 mt-1">Restart failed. Check console for details.</p>}
          </div>

          <div className="mb-4">
            <label className="block text-[11px] font-medium text-neutral-400 mb-1.5">Auto-Refresh Interval</label>
            <div className="flex gap-1 mb-1.5">
              {REFRESH_PRESETS.map(p => (
                <button
                  key={p.value}
                  onClick={() => { onUpdateRefreshInterval(p.value); setRefreshInputVal(''); }}
                  className={`flex-1 text-[11px] py-1 rounded border transition-colors ${refreshInterval === p.value && !refreshInputVal ? 'bg-neutral-600 border-neutral-500 text-neutral-100' : 'bg-neutral-950 border-neutral-700 text-neutral-400 hover:text-neutral-200'}`}
                >
                  {p.label}
                </button>
              ))}
            </div>
            <div className="flex items-center gap-1.5">
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
                className="flex-1 bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600 placeholder-neutral-600"
              />
              <span className="text-[11px] text-neutral-500 shrink-0">
                {refreshInterval === 0 ? 'off' : `${refreshInterval / 1000}s`}
              </span>
            </div>
          </div>

          <div className="mb-4">
            <label className="block text-[11px] font-medium text-neutral-400 mb-1.5">Tasks Per Page</label>
            <div className="flex gap-1 mb-1.5">
              {PAGE_PRESETS.map(p => (
                <button
                  key={p}
                  onClick={() => { onUpdatePageSize(p); setPageSizeInputVal(''); }}
                  className={`flex-1 text-[11px] py-1 rounded border transition-colors ${pageSize === p && !pageSizeInputVal ? 'bg-neutral-600 border-neutral-500 text-neutral-100' : 'bg-neutral-950 border-neutral-700 text-neutral-400 hover:text-neutral-200'}`}
                >
                  {p}
                </button>
              ))}
              <button
                onClick={() => { onUpdatePageSize(ALL_PAGE_SIZE); setPageSizeInputVal(''); }}
                className={`flex-1 text-[11px] py-1 rounded border transition-colors ${pageSize === ALL_PAGE_SIZE && !pageSizeInputVal ? 'bg-neutral-600 border-neutral-500 text-neutral-100' : 'bg-neutral-950 border-neutral-700 text-neutral-400 hover:text-neutral-200'}`}
              >
                All
              </button>
            </div>
            <div className="flex items-center gap-1.5">
              <input
                type="number"
                min={1}
                placeholder="Custom"
                value={pageSizeInputVal}
                onChange={e => setPageSizeInputVal(e.target.value)}
                onBlur={() => {
                  const n = parseInt(pageSizeInputVal);
                  if (n > 0) onUpdatePageSize(n);
                  setPageSizeInputVal('');
                }}
                onKeyDown={e => {
                  if (e.key === 'Enter') {
                    const n = parseInt(pageSizeInputVal);
                    if (n > 0) onUpdatePageSize(n);
                    setPageSizeInputVal('');
                  }
                }}
                className="flex-1 bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600 placeholder-neutral-600"
              />
              <span className="text-[11px] text-neutral-500 shrink-0">
                {pageSize >= ALL_PAGE_SIZE ? 'all' : pageSize}
              </span>
            </div>
          </div>

          <div className="border-t border-neutral-800 pt-3 mb-4">
            <h4 className="text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2">Notifications</h4>
            <label className="flex items-center justify-between cursor-pointer">
              <span className="text-[11px] font-medium text-neutral-400">Show browser notifications on task completion</span>
              <button
                onClick={() => {
                  const next = !notifyOnComplete;
                  if (next && 'Notification' in window && Notification.permission === 'default') {
                    Notification.requestPermission();
                  }
                  onUpdateNotifyOnComplete(next);
                }}
                className={`relative w-8 h-4 rounded-full transition-colors ${notifyOnComplete ? 'bg-emerald-600' : 'bg-neutral-700'}`}
              >
                <span className={`absolute top-0.5 left-0.5 w-3 h-3 rounded-full bg-white transition-transform ${notifyOnComplete ? 'translate-x-4' : ''}`} />
              </button>
            </label>
            <label className="flex items-center justify-between cursor-pointer mt-2">
              <span className="text-[11px] font-medium text-neutral-400">Play sound on task completion</span>
              <button
                onClick={() => onUpdateSoundOnComplete(!soundOnComplete)}
                className={`relative w-8 h-4 rounded-full transition-colors ${soundOnComplete ? 'bg-emerald-600' : 'bg-neutral-700'}`}
              >
                <span className={`absolute top-0.5 left-0.5 w-3 h-3 rounded-full bg-white transition-transform ${soundOnComplete ? 'translate-x-4' : ''}`} />
              </button>
            </label>
          </div>

          <div className="border-t border-neutral-800 pt-3 mb-4">
            <h4 className="text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2">GitHub</h4>
            {githubAccount ? (
              <div className="flex items-center gap-2 bg-neutral-950 rounded p-2 border border-neutral-800">
                <span className="w-2 h-2 rounded-full bg-emerald-500 shrink-0" />
                <span className="text-xs text-neutral-200">Connected: <span className="font-mono">{githubAccount}</span></span>
              </div>
            ) : (
              <button onClick={onConnectGitHub}
                className="w-full bg-neutral-800 hover:bg-neutral-700 text-neutral-300 text-xs py-1.5 rounded transition-colors border border-neutral-700 flex items-center justify-center gap-1.5">
                <GitBranch className="w-3 h-3" /> Connect GitHub
              </button>
            )}
          </div>

          <div className="border-t border-neutral-800 pt-3 mb-4">
            <h4 className="text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2">Export / Import</h4>
            <div className="flex gap-2">
              <button
                onClick={() => {
                  const keys = ['viewMode', 'refreshInterval', 'pageSize', 'defaultTaskType', 'notifyOnComplete', 'soundOnComplete', 'archivePageSize'];
                  const settings: Record<string, unknown> = {};
                  keys.forEach(k => {
                    const val = localStorage.getItem(`blueprint-admin:${k}`);
                    if (val !== null) settings[k] = JSON.parse(val);
                  });
                  const blob = new Blob([JSON.stringify(settings, null, 2)], { type: 'application/json' });
                  const url = URL.createObjectURL(blob);
                  const a = document.createElement('a');
                  a.href = url;
                  a.download = 'blueprint-settings.json';
                  a.click();
                  URL.revokeObjectURL(url);
                }}
                className="flex-1 bg-neutral-800 hover:bg-neutral-700 text-neutral-300 text-xs py-1.5 rounded transition-colors border border-neutral-700"
              >
                Export Settings
              </button>
              <button
                onClick={() => {
                  const input = document.createElement('input');
                  input.type = 'file';
                  input.accept = '.json,application/json';
                  input.onchange = (e) => {
                    const file = (e.target as HTMLInputElement).files?.[0];
                    if (!file) return;
                    const reader = new FileReader();
                    reader.onload = (ev) => {
                      try {
                        const settings = JSON.parse(ev.target?.result as string);
                        const allowed = ['viewMode', 'refreshInterval', 'pageSize', 'defaultTaskType', 'notifyOnComplete', 'soundOnComplete', 'archivePageSize'];
                        allowed.forEach(k => {
                          if (k in settings) localStorage.setItem(`blueprint-admin:${k}`, JSON.stringify(settings[k]));
                        });
                        window.location.reload();
                      } catch { alert('Invalid settings file'); }
                    };
                    reader.readAsText(file);
                  };
                  input.click();
                }}
                className="flex-1 bg-neutral-800 hover:bg-neutral-700 text-neutral-300 text-xs py-1.5 rounded transition-colors border border-neutral-700"
              >
                Import Settings
              </button>
            </div>
          </div>

          <div className="border-t border-neutral-800 pt-3">
            <h4 className="text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2">Max Plan Accounts</h4>
            {maxPlanAccounts.length === 0 && (
              <p className="text-[11px] text-neutral-600 mb-2">No accounts — using API key fallback</p>
            )}
            {maxPlanAccounts.map(a => {
              let tokenAge: string | null = null;
              let isExpiring = false;
              if (a.token_created_at) {
                const created = new Date(a.token_created_at);
                const diffMs = new Date().getTime() - created.getTime();
                const days = Math.floor(diffMs / 86400000);
                isExpiring = days > 300; // warn at ~10 months
                if (days < 1) tokenAge = 'today';
                else if (days < 30) tokenAge = `${days}d old`;
                else tokenAge = `${Math.floor(days / 30)}mo old`;
              }
              return (
                <div key={a.id} className="flex items-center gap-2 mb-2 bg-neutral-950 rounded p-2 border border-neutral-800">
                  <button onClick={() => onToggleAccount(a.id, !a.enabled)}
                    className={`w-2 h-2 rounded-full shrink-0 ${a.enabled ? 'bg-emerald-500' : 'bg-neutral-600'}`}
                    title={a.enabled ? 'Enabled (click to disable)' : 'Disabled (click to enable)'} />
                  <div className="flex-1 min-w-0">
                    <div className="text-xs text-neutral-200 truncate">{a.name}</div>
                    <div className="text-[10px] text-neutral-500 font-mono">
                      {a.active_tasks} active
                      {tokenAge && <span className={`ml-1.5 ${isExpiring ? 'text-amber-400' : ''}`}>· token {tokenAge}</span>}
                    </div>
                  </div>
                  <button onClick={() => onRemoveAccount(a.id)}
                    className="text-neutral-600 hover:text-red-400 text-xs transition-colors shrink-0">&times;</button>
                </div>
              );
            })}
            <div className="mt-2 space-y-1.5">
              <input value={newAccountName} onChange={e => setNewAccountName(e.target.value)}
                placeholder="Account name" className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-600" />
              <input value={newAccountToken} onChange={e => setNewAccountToken(e.target.value)}
                placeholder="sk-ant-oat01-... (from claude setup-token)" type="password"
                className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-600 font-mono" />
              <button onClick={onAddAccount} disabled={!newAccountName || !newAccountToken}
                className="w-full bg-neutral-800 hover:bg-neutral-700 disabled:opacity-30 text-neutral-300 text-xs py-1 rounded transition-colors border border-neutral-700">
                Add Account
              </button>
            </div>
          </div>

          <div className="border-t border-neutral-800 pt-3 mt-3">
            <h4 className="text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2">Autonomous</h4>
            <button
              onClick={onPauseAllAutonomous}
              disabled={!anyAutonomousEnabled}
              className="w-full bg-amber-950/30 hover:bg-amber-900/40 disabled:opacity-30 disabled:cursor-not-allowed text-amber-400 text-xs py-1.5 rounded transition-colors border border-amber-900/50"
            >
              Pause All Autonomous
            </button>
          </div>

          <div className="border-t border-neutral-800 pt-3 mt-3">
            <h4 className="text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2 flex items-center gap-1.5">
              <Shield className="w-3 h-3" /> Deploy Health
            </h4>
            {deployState ? (
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <span className="text-[11px] text-neutral-500">Status</span>
                  <span className={`text-[11px] font-semibold ${
                    deployState.status === 'STABLE' ? 'text-emerald-400' :
                    deployState.status === 'MONITORING' ? 'text-blue-400' :
                    deployState.status === 'PAUSED' ? 'text-amber-400' :
                    deployState.status === 'ROLLED_BACK' ? 'text-red-400' :
                    deployState.status === 'HALTED' ? 'text-red-500' : 'text-neutral-400'
                  }`}>{deployState.status}</span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-[11px] text-neutral-500">SHA</span>
                  <span className="text-[11px] text-neutral-300 font-mono">{deployState.current_sha?.slice(0, 8) || '—'}</span>
                </div>
                {(deployState.failure_rate ?? 0) > 0 && (
                  <div className="flex items-center justify-between">
                    <span className="text-[11px] text-neutral-500">Failure Rate</span>
                    <span className={`text-[11px] font-mono ${(deployState.failure_rate ?? 0) > 0.3 ? 'text-red-400' : 'text-neutral-300'}`}>
                      {((deployState.failure_rate ?? 0) * 100).toFixed(0)}%
                    </span>
                  </div>
                )}
                {deployState.monitoring_window && (
                  <div className="flex items-center justify-between">
                    <span className="text-[11px] text-neutral-500">Monitor</span>
                    <span className="text-[11px] text-blue-400 font-mono">{deployState.monitoring_window.percent}%</span>
                  </div>
                )}
                {deployState.rollback_reason && (
                  <p className="text-[10px] text-amber-400/80 bg-amber-950/20 rounded px-2 py-1 mt-1">{deployState.rollback_reason}</p>
                )}
                {deployState.pause_reason && (
                  <p className="text-[10px] text-amber-400/80 bg-amber-950/20 rounded px-2 py-1 mt-1">{deployState.pause_reason}</p>
                )}
                <div className="flex gap-1.5 mt-2">
                  {deployState.status === 'PAUSED' || deployState.status === 'HALTED' ? (
                    <button onClick={onResumePipeline}
                      className="flex-1 bg-emerald-950/30 hover:bg-emerald-900/40 text-emerald-400 text-[10px] py-1 rounded transition-colors border border-emerald-900/50 flex items-center justify-center gap-1">
                      <Play className="w-2.5 h-2.5" /> Resume
                    </button>
                  ) : (
                    <button onClick={onPausePipeline}
                      className="flex-1 bg-amber-950/30 hover:bg-amber-900/40 text-amber-400 text-[10px] py-1 rounded transition-colors border border-amber-900/50 flex items-center justify-center gap-1">
                      <Pause className="w-2.5 h-2.5" /> Pause
                    </button>
                  )}
                  {deployState.status === 'MONITORING' && (
                    <button onClick={onMarkStable}
                      className="flex-1 bg-neutral-800 hover:bg-neutral-700 text-emerald-400 text-[10px] py-1 rounded transition-colors border border-neutral-700 flex items-center justify-center gap-1">
                      <CheckCircle className="w-2.5 h-2.5" /> Stable
                    </button>
                  )}
                </div>
                <div className="flex gap-1.5">
                  <button
                    onClick={() => { if (window.confirm('Rollback to last known good revision? This will pause the pipeline.')) onRollback(); }}
                    disabled={!deployState.last_known_good_sha || deployState.last_known_good_sha === deployState.current_sha}
                    className="flex-1 bg-red-950/30 hover:bg-red-900/40 disabled:opacity-30 disabled:cursor-not-allowed text-red-400 text-[10px] py-1 rounded transition-colors border border-red-900/50 flex items-center justify-center gap-1">
                    <RotateCw className="w-2.5 h-2.5" /> Rollback
                  </button>
                  <button onClick={onRepairState}
                    className="flex-1 bg-neutral-800 hover:bg-neutral-700 text-neutral-300 text-[10px] py-1 rounded transition-colors border border-neutral-700 flex items-center justify-center gap-1">
                    <Wrench className="w-2.5 h-2.5" /> Repair
                  </button>
                </div>
              </div>
            ) : (
              <p className="text-[11px] text-neutral-600">Loading...</p>
            )}
          </div>

          <div className="border-t border-neutral-800 pt-3 mt-3">
            <h4 className="text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2">Keyboard Shortcuts</h4>
            <div className="space-y-1">
              {[
                ['1', 'Kanban view'],
                ['2', 'Pipeline view'],
                ['/', 'Focus search'],
                ['N', 'New task'],
                ['R', 'Refresh'],
                ['D', 'Directions panel'],
                ['S', 'Settings'],
                ['M', 'Metrics'],
                ['A', 'Archive'],
                ['?', 'Shortcut help'],
                ['F', 'Fullscreen (in drawer)'],
                ['Esc', 'Close modal'],
              ].map(([key, label]) => (
                <div key={key} className="flex items-center justify-between">
                  <span className="text-[11px] text-neutral-500">{label}</span>
                  <kbd className="text-[10px] font-mono bg-neutral-800 border border-neutral-700 text-neutral-300 px-1.5 py-0.5 rounded">{key}</kbd>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
