import { useEffect, useRef, useState } from 'react';
import { Bell, RefreshCw, Send } from 'lucide-react';
import { API_BASE_URL, apiFetch, authHeaders, fetchDeadLetters, redriveDeadLetters } from '../api';
import type { DeadLetterEntry } from '../api';

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
  { key: 'RUNNER_DEACTIVATED', label: 'Runner Deactivated', description: 'When a self-hosted fleet runner goes offline (stops sending heartbeats)', defaultOn: true },
  { key: 'RUNNER_DEGRADED', label: 'Runner Degraded', description: 'When a self-hosted fleet runner accumulates repeated consecutive job failures', defaultOn: true },
  { key: 'RUNNER_DEGRADED_ESCALATED', label: 'Runner Degraded Escalated', description: 'When a degraded-runner alert ages past the escalation threshold and is re-paged', defaultOn: true },
  { key: 'RUNNER_FLEET_OFFLINE', label: 'Fleet Offline', description: 'When an entire self-hosted fleet has no healthy runner available', defaultOn: true },
  { key: 'RUNNER_LOW_DISK', label: 'Runner Low Disk', description: 'When a self-hosted fleet runner reports free disk below the safe threshold', defaultOn: true },
  { key: 'RUNNER_OUTAGE_ESCALATED', label: 'Runner Outage Escalated', description: 'When a runner outage ages past the escalation threshold and is re-paged', defaultOn: true },
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
  selectedProjectId?: string | null;
}

interface WebhookDeliveryStats {
  total_dispatched: number;
  total_delivered: number;
  total_failed: number;
  last_failure_reason: string;
  last_failure_at: string | null;
  retry_queue_depth: number;
}

function summarizeWebhookDeliveryHealth(stats: WebhookDeliveryStats | null, webhookUrl: string, loading: boolean) {
  if (!webhookUrl) {
    return {
      tone: 'neutral' as const,
      label: 'Webhook not configured',
      detail: 'Add a webhook URL to inspect delivery health for this project.',
    };
  }

  if (loading) {
    return {
      tone: 'neutral' as const,
      label: 'Checking delivery health',
      detail: 'Loading project delivery stats...',
    };
  }

  if (!stats) {
    return {
      tone: 'neutral' as const,
      label: webhookUrl ? 'Delivery health unavailable' : 'Delivery health unavailable',
      detail: webhookUrl ? 'No delivery stats are available yet for this project.' : 'Select a project to inspect webhook delivery health.',
    };
  }

  if (stats.total_dispatched === 0) {
    return {
      tone: 'neutral' as const,
      label: 'No webhook deliveries yet',
      detail: webhookUrl
        ? 'The configured webhook has not been exercised for this project yet.'
        : 'Configure a webhook URL and send a notification to verify delivery.',
    };
  }

  if (stats.total_failed > 0 || stats.retry_queue_depth > 0) {
    return {
      tone: 'amber' as const,
      label: 'Delivery degraded',
      detail: [
        `${stats.total_delivered} delivered`,
        `${stats.total_failed} failed`,
        stats.retry_queue_depth > 0 ? `${stats.retry_queue_depth} queued` : null,
        stats.last_failure_reason ? `last: ${stats.last_failure_reason}` : null,
      ].filter(Boolean).join(' · '),
    };
  }

  return {
    tone: 'emerald' as const,
    label: 'Delivery healthy',
    detail: `${stats.total_delivered} delivered · ${stats.total_failed} failed · ${stats.retry_queue_depth} queued`,
  };
}

function WebhookDeliveryHealthCard({
  webhookUrl,
  selectedProjectId,
}: {
  webhookUrl: string;
  selectedProjectId: string;
}) {
  const [webhookStats, setWebhookStats] = useState<WebhookDeliveryStats | null>(null);
  const [webhookStatsError, setWebhookStatsError] = useState<string>('');

  const loading = webhookStats === null && webhookStatsError === '';
  const hasWebhookUrl = webhookUrl.trim().length > 0;
  const deliveryError = hasWebhookUrl ? webhookStatsError : '';
  const healthSummary = summarizeWebhookDeliveryHealth(webhookStats, webhookUrl, loading);

  useEffect(() => {
    if (!hasWebhookUrl) {
      return;
    }

    const controller = new AbortController();

    void apiFetch(
      `/api/admin/notification-dispatch/stats?project_id=${encodeURIComponent(selectedProjectId)}`,
      undefined,
      2,
      30000,
      controller.signal,
    )
      .then(async res => {
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}`);
        }
        return res.json();
      })
      .then(data => {
        if (controller.signal.aborted) return;
        const stats = data as Partial<WebhookDeliveryStats> | null;
        if (
          !stats
          || typeof stats.total_dispatched !== 'number'
          || typeof stats.total_delivered !== 'number'
          || typeof stats.total_failed !== 'number'
          || typeof stats.last_failure_reason !== 'string'
          || typeof stats.retry_queue_depth !== 'number'
        ) {
          throw new Error('Unexpected webhook stats payload');
        }
        setWebhookStats({
          total_dispatched: stats.total_dispatched,
          total_delivered: stats.total_delivered,
          total_failed: stats.total_failed,
          last_failure_reason: stats.last_failure_reason,
          last_failure_at: typeof stats.last_failure_at === 'string' ? stats.last_failure_at : null,
          retry_queue_depth: stats.retry_queue_depth,
        });
      })
      .catch(e => {
        if (controller.signal.aborted) return;
        setWebhookStats(null);
        setWebhookStatsError(e instanceof Error ? e.message : 'Failed to load webhook delivery health');
      });

    return () => controller.abort();
  }, [hasWebhookUrl, selectedProjectId, webhookUrl]);

  return (
    <div
      className={`mt-2 rounded-lg border px-3 py-2 text-ui-md ${
        deliveryError
          ? 'border-red-500/30 bg-red-950/20 text-red-300'
          : healthSummary.tone === 'emerald'
            ? 'border-emerald-500/30 bg-emerald-950/20 text-emerald-200'
            : healthSummary.tone === 'amber'
              ? 'border-amber-500/30 bg-amber-950/20 text-amber-200'
              : 'border-neutral-700 bg-workbench-base text-neutral-300'
      }`}
    >
      <div className="flex items-center gap-2">
        <span
          className={`h-2.5 w-2.5 rounded-full ${
            deliveryError
              ? 'bg-red-400'
              : loading
                ? 'bg-workbench-control'
                : healthSummary.tone === 'emerald'
                  ? 'bg-emerald-400'
                  : healthSummary.tone === 'amber'
                    ? 'bg-amber-400'
                    : 'bg-workbench-control'
          }`}
        />
        <span className="font-medium">
          {deliveryError ? 'Delivery health unavailable' : healthSummary.label}
        </span>
      </div>
      <p className="mt-1 text-ui-md text-inherit/80">
        {deliveryError ? deliveryError : healthSummary.detail}
      </p>
    </div>
  );
}

function DeadLetterPanel({
  webhookUrl,
  selectedProjectId,
}: {
  webhookUrl: string;
  selectedProjectId: string;
}) {
  const [deadLetters, setDeadLetters] = useState<DeadLetterEntry[] | null>(null);
  const [deadLettersError, setDeadLettersError] = useState<string>('');
  const [redrivingIds, setRedrivingIds] = useState<Set<string>>(new Set());
  const [redriveError, setRedriveError] = useState<string>('');

  const hasWebhookUrl = webhookUrl.trim().length > 0;
  const loading = deadLetters === null && deadLettersError === '' && hasWebhookUrl;
  const mountedRef = useRef(true);

  const reloadDeadLetters = () => {
    if (!hasWebhookUrl) return;
    fetchDeadLetters(selectedProjectId)
      .then(data => {
        if (!mountedRef.current) return;
        setDeadLetters(data.items);
        setDeadLettersError('');
      })
      .catch(e => {
        if (!mountedRef.current) return;
        setDeadLetters(null);
        setDeadLettersError(e instanceof Error ? e.message : 'Failed to load dead letters');
      });
  };

  useEffect(() => {
    mountedRef.current = true;
    if (!hasWebhookUrl) {
      return;
    }
    reloadDeadLetters();
    return () => { mountedRef.current = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasWebhookUrl, selectedProjectId]);

  const handleRedrive = async (id: string) => {
    setRedrivingIds(prev => new Set(prev).add(id));
    setRedriveError('');
    try {
      await redriveDeadLetters(selectedProjectId, [id]);
      // Reload the list to reflect the removed entry
      reloadDeadLetters();
    } catch (e) {
      setRedriveError(e instanceof Error ? e.message : 'Redrive failed');
    } finally {
      setRedrivingIds(prev => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    }
  };

  if (!hasWebhookUrl) return null;

  if (loading) {
    return (
      <div className="mt-3 rounded-lg border border-neutral-700 bg-workbench-base px-3 py-2 text-ui-md text-neutral-500">
        Loading dead-lettered notifications...
      </div>
    );
  }

  if (deadLettersError) {
    return (
      <div className="mt-3 rounded-lg border border-red-500/30 bg-red-950/20 px-3 py-2 text-ui-md text-red-300">
        Failed to load dead letters: {deadLettersError}
      </div>
    );
  }

  if (!deadLetters || deadLetters.length === 0) return null;

  return (
    <div className="mt-3 rounded-lg border border-amber-500/30 bg-amber-950/20 px-3 py-2">
      <div className="flex items-center gap-2 mb-2">
        <span className="h-2.5 w-2.5 rounded-full bg-amber-400" />
        <span className="text-ui-md font-medium text-amber-200">
          {deadLetters.length} dead-lettered notification{deadLetters.length !== 1 ? 's' : ''}
        </span>
      </div>
      {redriveError && (
        <p className="text-ui-md text-red-400 mb-2">{redriveError}</p>
      )}
      <div className="space-y-1.5 max-h-64 overflow-y-auto">
        {deadLetters.map(dl => (
          <div
            key={dl.id}
            className="flex items-center justify-between gap-2 rounded bg-workbench-base/60 px-2 py-1.5"
          >
            <div className="min-w-0">
              <div className="text-xs text-neutral-300 truncate">{dl.event_type}</div>
              <div className="text-ui-md text-neutral-500">
                {dl.reason}
                {dl.last_status != null ? ` · HTTP ${dl.last_status}` : ''}
                {dl.attempts > 0 ? ` · ${dl.attempts} attempt${dl.attempts !== 1 ? 's' : ''}` : ''}
              </div>
            </div>
            <button
              onClick={() => handleRedrive(dl.id)}
              disabled={redrivingIds.has(dl.id)}
              className="flex items-center gap-1 shrink-0 min-h-[44px] bg-workbench-layer hover:bg-workbench-hover disabled:opacity-30 disabled:cursor-not-allowed text-neutral-200 text-xs py-1 px-2 rounded transition-colors border border-neutral-600"
            >
              <RefreshCw className={`w-3 h-3 ${redrivingIds.has(dl.id) ? 'animate-spin' : ''}`} />
              Re-drive
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}

function Toggle({ enabled, onChange }: { enabled: boolean; onChange: (v: boolean) => void }) {
  return (
    // reason: notification event toggle switch; h-5 w-9 is standard toggle knob, not a tap target
    <button
      data-viewport-allow-small-target
      onClick={() => onChange(!enabled)}
      className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors shrink-0 ${enabled ? 'bg-emerald-600' : 'bg-workbench-layer'}`}
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
  selectedProjectId,
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
              className="w-32 bg-workbench-shell border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm font-mono workbench-focus"
            />
            <p className="text-ui-md text-neutral-600 mt-1">
              {dailyCostCeilingUsd === 0 ? 'No ceiling — pipeline runs without a daily cost limit.' : `Pipeline will raise a warning alert (and send a notification, if enabled) when daily spend exceeds $${dailyCostCeilingUsd}. Autonomous tasks keep running.`}
            </p>
          </>
        ) : (
          <div className="bg-workbench-base border border-neutral-800 rounded-lg px-3 py-2 text-sm text-neutral-400 w-32 font-mono">
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
              className={`w-full bg-workbench-shell border rounded-lg px-3 py-2 text-base md:text-sm ${
 !urlIsValid ? 'border-red-600 text-red-400' : 'border-neutral-700 text-neutral-100'
 } workbench-focus`}
            />
            {!urlIsValid && (
              <p className="text-ui-md text-red-400 mt-1">URL must start with http:// or https://</p>
            )}
            <p className="text-ui-md text-neutral-600 mt-1">POST requests with JSON payloads will be sent to this URL</p>
          </>
        ) : (
          <div className="bg-workbench-base border border-neutral-800 rounded-lg px-3 py-2 text-sm text-neutral-500">
            {webhookUrl ? (
              <span className="font-mono text-neutral-400">{webhookUrl.replace(/^(https?:\/\/[^/]+).*/, '$1/...')}</span>
            ) : (
              'Not configured'
            )}
          </div>
        )}
        {selectedProjectId && (
          <WebhookDeliveryHealthCard
            key={selectedProjectId}
            webhookUrl={webhookUrl}
            selectedProjectId={selectedProjectId}
          />
        )}
        {selectedProjectId && (
          <DeadLetterPanel
            key={`dead-${selectedProjectId}`}
            webhookUrl={webhookUrl}
            selectedProjectId={selectedProjectId}
          />
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
                <div className="text-ui-md text-neutral-600">{opt.description}</div>
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
            className="w-32 bg-workbench-shell border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-base md:text-sm font-mono workbench-focus"
          />
        ) : (
          <div className="bg-workbench-base border border-neutral-800 rounded-lg px-3 py-2 text-sm text-neutral-400 w-32 font-mono">
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
            className="flex items-center gap-2 min-h-[44px] bg-workbench-panel hover:bg-workbench-hover disabled:opacity-30 disabled:cursor-not-allowed text-neutral-300 text-xs py-2 px-4 rounded-lg transition-colors border border-neutral-700"
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
            <p className="text-ui-md text-neutral-600 mt-1">Configure a webhook URL above to enable test notifications.</p>
          )}
        </div>
      )}
    </div>
  );
}
