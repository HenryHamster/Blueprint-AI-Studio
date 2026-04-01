import { Settings, GitBranch } from 'lucide-react';

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
  defaultTaskType: 'create' | 'plan' | 'research' | 'review' | 'fix';
  onUpdateDefaultTaskType: (value: 'create' | 'plan' | 'research' | 'review' | 'fix') => void;
  viewMode: 'kanban' | 'pipeline';
  onToggleViewMode: () => void;
  // Notifications
  notifyOnComplete: boolean;
  onUpdateNotifyOnComplete: (value: boolean) => void;
  // Existing
  maxConcurrentVMs: number;
  onUpdateMaxVMs: (value: number) => void;
  maxRetries: number;
  onUpdateMaxRetries: (value: number) => void;
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
}

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
  maxConcurrentVMs,
  onUpdateMaxVMs,
  maxRetries,
  onUpdateMaxRetries,
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
}: SettingsDropdownProps) {
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
          <h3 className="text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-3">Defaults</h3>
          <div className="mb-4">
            <label className="block text-[11px] font-medium text-neutral-400 mb-1.5">Default Task Type</label>
            <select
              value={defaultTaskType}
              onChange={e => onUpdateDefaultTaskType(e.target.value as 'create' | 'plan' | 'research' | 'review' | 'fix')}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
            >
              <option value="create">Create</option>
              <option value="plan">Plan</option>
              <option value="research">Research</option>
              <option value="review">Review</option>
              <option value="fix">Fix</option>
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

          <div className="border-t border-neutral-800 pt-3 mb-4">
            <h4 className="text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2">Settings</h4>
          </div>
          <div className="mb-4">
            <label className="block text-[11px] font-medium text-neutral-400 mb-1.5">Max Concurrent VMs (1-30)</label>
            <input
              type="number"
              min={1}
              max={30}
              value={maxConcurrentVMs}
              onChange={e => onUpdateMaxVMs(parseInt(e.target.value) || 1)}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
          <div className="mb-4">
            <label className="block text-[11px] font-medium text-neutral-400 mb-1.5">Max Retries (0-10)</label>
            <input
              type="number"
              min={0}
              max={10}
              value={maxRetries}
              onChange={e => onUpdateMaxRetries(parseInt(e.target.value) || 0)}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>

          <div className="mb-4">
            <label className="block text-[11px] font-medium text-neutral-400 mb-1.5">Auto-Refresh Interval</label>
            <select
              value={refreshInterval}
              onChange={e => onUpdateRefreshInterval(Number(e.target.value))}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
            >
              <option value={10000}>10s</option>
              <option value={30000}>30s</option>
              <option value={60000}>60s</option>
              <option value={0}>Manual only</option>
            </select>
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
                      {a.active_tasks} active / {a.total_tasks} total
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
        </div>
      )}
    </div>
  );
}
