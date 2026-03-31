import { Settings, GitBranch } from 'lucide-react';

export interface MaxPlanAccount {
  id: string;
  name: string;
  enabled: boolean;
  active_tasks: number;
  total_tasks: number;
}

interface SettingsDropdownProps {
  showSettings: boolean;
  onToggleSettings: () => void;
  settingsRef: React.RefObject<HTMLDivElement | null>;
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
  newAccountCreds: string;
  setNewAccountCreds: (value: string) => void;
  onAddAccount: () => void;
}

export function SettingsDropdown({
  showSettings,
  onToggleSettings,
  settingsRef,
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
  newAccountCreds,
  setNewAccountCreds,
  onAddAccount,
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
          <h3 className="text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-3">Settings</h3>
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
            {maxPlanAccounts.map(a => (
              <div key={a.id} className="flex items-center gap-2 mb-2 bg-neutral-950 rounded p-2 border border-neutral-800">
                <button onClick={() => onToggleAccount(a.id, !a.enabled)}
                  className={`w-2 h-2 rounded-full shrink-0 ${a.enabled ? 'bg-emerald-500' : 'bg-neutral-600'}`}
                  title={a.enabled ? 'Enabled (click to disable)' : 'Disabled (click to enable)'} />
                <div className="flex-1 min-w-0">
                  <div className="text-xs text-neutral-200 truncate">{a.name}</div>
                  <div className="text-[10px] text-neutral-500 font-mono">{a.active_tasks} active / {a.total_tasks} total</div>
                </div>
                <button onClick={() => onRemoveAccount(a.id)}
                  className="text-neutral-600 hover:text-red-400 text-xs transition-colors shrink-0">&times;</button>
              </div>
            ))}
            <div className="mt-2 space-y-1.5">
              <input value={newAccountName} onChange={e => setNewAccountName(e.target.value)}
                placeholder="Account name" className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-600" />
              <textarea value={newAccountCreds} onChange={e => setNewAccountCreds(e.target.value)}
                placeholder="Paste credentials.json contents" rows={2}
                className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs resize-none focus:outline-none focus:ring-1 focus:ring-neutral-600 font-mono" />
              <button onClick={onAddAccount} disabled={!newAccountName || !newAccountCreds}
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
