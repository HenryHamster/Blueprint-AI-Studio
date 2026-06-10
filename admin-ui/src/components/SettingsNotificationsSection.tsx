import { useState } from 'react';
import { Bell, Send } from 'lucide-react';
import { API_BASE_URL, authHeaders } from '../api';

export interface NotificationConfig {
  webhook_url: string;
  enabled_events: string[];
  throttle_seconds: number;
}

const EVENT_OPTIONS: { key: string; label: string; description: string; defaultOn: boolean }[] = [
  { key: 'TASK_FAILED', label: 'Task Failed', description: 'When a task fails after retries exhausted', defaultOn: true },
  { key: 'TASK_COMPLETED', label: 'Task Completed', description: 'When a task completes successfully', defaultOn: false },
  { key: 'APPROVAL_NEEDED', label: 'Approval Needed', description: 'When a task requires manual approval', defaultOn: true },
  { key: 'PR_MERGED', label: 'PR Merged', description: 'When a pull request is merged', defaultOn: false },
  { key: 'DEPLOY_HEALTH_ALERT', label: 'Deploy Health Alert', description: 'Deployment state changes and health alerts', defaultOn: true },
  { key: 'BUDGET_EXCEEDED', label: 'Budget Exceeded', description: 'When actual daily spend exceeds the cost ceiling', defaultOn: false },
  { key: 'AUTO_FIX_SPAWNED', label: 'CI Auto-Fix Spawned', description: 'When Blueprint auto-spawns a fix task for a CI failure', defaultOn: false },
  { key: 'AUTO_FIX_MERGED', label: 'CI Auto-Fix Merged', description: 'When a Blueprint auto-remediation fix PR is merged', defaultOn: false },
  { key: 'REMEDIATION_ESCALATED', label: 'Remediation Escalation', description: 'When a direction exhausts remediation and needs manual intervention', defaultOn: true },
  { key: 'DIRECTION_MILESTONE', label: 'Direction Milestone', description: 'When a direction is auto-completed, auto-paused, or auto-retired', defaultOn: true },
  { key: 'AUTO_FIX_ESCALATED', label: 'CI Auto-Fix Escalated', description: 'When CI auto-fix repeatedly fails and the branch is suspended', defaultOn: true },
  { key: 'RESIDUAL_FIX_ESCALATED', label: 'Residual Fix Escalated', description: 'When a residual fingerprint remediation repeatedly fails and is suspended', defaultOn: true },
  { key: 'RESIDUAL_FIX_UNSUSPENDED', label: 'Residual Fix Unsuspended', description: 'When a previously-suspended residual fingerprint case is manually re-enabled by an operator', defaultOn: false },
  { key: 'DIRECTION_BURN_PAUSE', label: 'Direction Burn Pause', description: 'When a direction is auto-paused for accumulating non-merging finished tasks beyond the safe rolling-window threshold', defaultOn: true },
  { key: 'account_model_regression_quarantined', label: 'Account/Model Regression Quarantined', description: 'When a provider/model pair is auto-quarantined due to detected regression in success rate', defaultOn: true },
  { key: 'MERGE_UNVERIFIED', label: 'Merged Without Test Verification', description: 'When a no-CI repo merges a PR without running tests (UNVERIFIED-merge fallback)', defaultOn: true },
];

interface SettingsNotificationsSectionProps {
  webhookUrl: string;
  onUpdateWebhookUrl: (url: string) => void;
  enabledEvents: string[];
  onUpdateEnabledEvents: (events: string[]) => void;
  throttleSeconds: number;
  onUpdateThrottleSeconds: (seconds: number) => void;
  dailyCostCeilingUsd: number;
  onUpdateDailyCostCeiling: (value: number) => void;
  isAdmin: boolean;
}

function Toggle({ enabled, onChange }: { enabled: boolean; onChange: (v: boolean) => void }) {
  return (
    // reason: notification event toggle switch; h-5 w-9 is standard toggle knob, not a tap target
    <button
      data-viewport-allow-small-target
      onClick={() => onChange(!enabled)}
      className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors shrink-0 ${enabled ? 'bg-emerald-600' : 'bg-neutral-700'}`}
    >
      <span className={`inline-block h-3.5 w-3.5 rounded-full bg-white transition-transform ${enabled ? 'translate-x-[18px]' : 'translate-x-[3px]'}`} />
    </button>
  );
}

export function SettingsNotificationsSection({
  webhookUrl,
  onUpdateWebhookUrl,
  enabledEvents,
  onUpdateEnabledEvents,
  throttleSeconds,
  onUpdateThrottleSeconds,
  dailyCostCeilingUsd,
  onUpdateDailyCostCeiling,
  isAdmin,
}: SettingsNotificationsSectionProps) {
  const [urlDraft, setUrlDraft] = useState(webhookUrl);
  const [testStatus, setTestStatus] = useState<'idle' | 'sending' | 'success' | 'error'>('idle');
  const [testError, setTestError] = useState<string>('');

  const urlIsValid = urlDraft === '' || urlDraft.startsWith('https://') || urlDraft.startsWith('http://');

  const handleUrlBlur = () => {
    if (urlIsValid) {
      onUpdateWebhookUrl(urlDraft);
    }
  };

  const toggleEvent = (key: string, enabled: boolean) => {
    if (enabled) {
      onUpdateEnabledEvents([...enabledEvents, key]);
    } else {
      onUpdateEnabledEvents(enabledEvents.filter(e => e !== key));
    }
  };

  const handleSendTest = async () => {
    setTestStatus('sending');
    setTestError('');
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify({ test_notification: true }),
      });
      if (res.ok) {
        setTestStatus('success');
        setTimeout(() => setTestStatus('idle'), 3000);
      } else {
        const data = await res.json().catch(() => ({}));
        setTestError(data?.error?.message ?? `HTTP ${res.status}`);
        setTestStatus('error');
        setTimeout(() => setTestStatus('idle'), 5000);
      }
    } catch (e) {
      setTestError(e instanceof Error ? e.message : 'Request failed');
      setTestStatus('error');
      setTimeout(() => setTestStatus('idle'), 5000);
    }
  };

  return (
    <div className="space-y-6">
      {/* Section header */}
      <div className="flex items-center gap-2">
        <Bell className="w-4 h-4 text-neutral-400" />
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider">External Notifications</h3>
      </div>

      {/* Daily Cost Ceiling */}
      <div>
        <label className="block text-xs font-medium text-neutral-400 mb-1.5">Daily Cost Ceiling ($)</label>
        {isAdmin ? (
          <>
            <input
              type="number"
              min={0}
              step={1}
              value={dailyCostCeilingUsd === 0 ? '' : dailyCostCeilingUsd}
              placeholder="Disabled"
              onChange={e => {
                const v = parseFloat(e.target.value);
                onUpdateDailyCostCeiling(isNaN(v) || v <= 0 ? 0 : v);
              }}
              className="w-32 bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
            <p className="text-[11px] text-neutral-600 mt-1">
              {dailyCostCeilingUsd === 0 ? 'No ceiling — pipeline runs without a daily cost limit.' : `Pipeline will raise a warning alert (and send a notification, if enabled) when daily spend exceeds $${dailyCostCeilingUsd}. Autonomous tasks keep running.`}
            </p>
          </>
        ) : (
          <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-3 py-2 text-sm text-neutral-400 w-32 font-mono">
            {dailyCostCeilingUsd === 0 ? 'Disabled' : `$${dailyCostCeilingUsd}`}
          </div>
        )}
      </div>

      {/* Webhook URL */}
      <div>
        <label className="block text-xs font-medium text-neutral-400 mb-1.5">Webhook URL</label>
        {isAdmin ? (
          <>
            <input
              type="url"
              value={urlDraft}
              onChange={e => setUrlDraft(e.target.value)}
              onBlur={handleUrlBlur}
              placeholder="https://hooks.slack.com/services/..."
              className={`w-full bg-neutral-950 border rounded-lg px-3 py-2 text-base md:text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600 ${
                !urlIsValid ? 'border-red-600 text-red-400' : 'border-neutral-700 text-neutral-100'
              }`}
            />
            {!urlIsValid && (
              <p className="text-[11px] text-red-400 mt-1">URL must start with http:// or https://</p>
            )}
            <p className="text-[11px] text-neutral-600 mt-1">POST requests with JSON payloads will be sent to this URL</p>
          </>
        ) : (
          <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-3 py-2 text-sm text-neutral-500">
            {webhookUrl ? (
              <span className="font-mono text-neutral-400">{webhookUrl.replace(/^(https?:\/\/[^/]+).*/, '$1/...')}</span>
            ) : (
              'Not configured'
            )}
          </div>
        )}
      </div>

      {/* Event toggles */}
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Notification Events</h3>
        <div className="space-y-3">
          {EVENT_OPTIONS.map(opt => (
            <div key={opt.key} className="flex items-center justify-between">
              <div className="min-w-0 mr-4">
                <div className="text-xs text-neutral-300">{opt.label}</div>
                <div className="text-[11px] text-neutral-600">{opt.description}</div>
              </div>
              {isAdmin ? (
                <Toggle
                  enabled={enabledEvents.includes(opt.key)}
                  onChange={v => toggleEvent(opt.key, v)}
                />
              ) : (
                <span className={`text-xs ${enabledEvents.includes(opt.key) ? 'text-emerald-400' : 'text-neutral-600'}`}>
                  {enabledEvents.includes(opt.key) ? 'On' : 'Off'}
                </span>
              )}
            </div>
          ))}
        </div>
      </div>

      {/* Throttle setting */}
      <div>
        <label className="block text-xs font-medium text-neutral-400 mb-1.5">
          Minimum seconds between notifications of the same type
        </label>
        {isAdmin ? (
          <input
            type="number"
            min={0}
            max={3600}
            value={throttleSeconds}
            onChange={e => {
              const v = parseInt(e.target.value);
              if (!isNaN(v) && v >= 0 && v <= 3600) onUpdateThrottleSeconds(v);
            }}
            className="w-32 bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
          />
        ) : (
          <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-3 py-2 text-sm text-neutral-400 w-32 font-mono">
            {throttleSeconds}s
          </div>
        )}
      </div>

      {/* Send test notification */}
      {isAdmin && (
        <div>
          <button
            disabled={!webhookUrl || testStatus === 'sending'}
            onClick={handleSendTest}
            className="flex items-center gap-2 min-h-[44px] bg-neutral-800 hover:bg-neutral-700 disabled:opacity-30 disabled:cursor-not-allowed text-neutral-300 text-xs py-2 px-4 rounded-lg transition-colors border border-neutral-700"
          >
            <Send className="w-3.5 h-3.5" />
            {testStatus === 'sending' ? 'Sending...' : 'Send Test Notification'}
          </button>
          {testStatus === 'success' && (
            <p className="text-xs text-emerald-400 mt-2">Test notification sent successfully.</p>
          )}
          {testStatus === 'error' && (
            <p className="text-xs text-red-400 mt-2">Failed: {testError}</p>
          )}
          {!webhookUrl && (
            <p className="text-[11px] text-neutral-600 mt-1">Configure a webhook URL above to enable test notifications.</p>
          )}
        </div>
      )}
    </div>
  );
}
