import { afterEach, describe, expect, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { SettingsNotificationsSection } from './SettingsNotificationsSection';
import { expectNoOverflowAt, expectMinTouchTargetAt } from '../test/viewport';

const ALL_EVENTS = [
  'TASK_FAILED',
  'TASK_COMPLETED',
  'APPROVAL_NEEDED',
  'PR_MERGED',
  'DEPLOY_HEALTH_ALERT',
  'BUDGET_EXCEEDED',
  'AUTO_FIX_SPAWNED',
  'AUTO_FIX_MERGED',
  'REMEDIATION_ESCALATED',
  'DIRECTION_MILESTONE',
  'AUTO_FIX_ESCALATED',
  'RESIDUAL_FIX_ESCALATED',
  'RESIDUAL_FIX_UNSUSPENDED',
  'account_model_regression_quarantined',
  'MERGE_UNVERIFIED',
];

describe('SettingsNotificationsSection — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  test('renders without horizontal overflow at 375px with empty config (admin)', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <SettingsNotificationsSection
            webhookUrl=""
            onUpdateWebhookUrl={() => {}}
            enabledEvents={[]}
            onUpdateEnabledEvents={() => {}}
            throttleSeconds={0}
            onUpdateThrottleSeconds={() => {}}
            dailyCostCeilingUsd={0}
            onUpdateDailyCostCeiling={() => {}}
            isAdmin={true}
          />
        );
      });
    });
  });

  test('renders without horizontal overflow at 375px with all events enabled and webhook (admin)', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <SettingsNotificationsSection
            webhookUrl="https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX"
            onUpdateWebhookUrl={() => {}}
            enabledEvents={ALL_EVENTS}
            onUpdateEnabledEvents={() => {}}
            throttleSeconds={300}
            onUpdateThrottleSeconds={() => {}}
            dailyCostCeilingUsd={50}
            onUpdateDailyCostCeiling={() => {}}
            isAdmin={true}
          />
        );
      });
    });
  });

  test('renders without horizontal overflow at 375px in read-only (non-admin) view', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <SettingsNotificationsSection
            webhookUrl="https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX"
            onUpdateWebhookUrl={() => {}}
            enabledEvents={ALL_EVENTS}
            onUpdateEnabledEvents={() => {}}
            throttleSeconds={60}
            onUpdateThrottleSeconds={() => {}}
            dailyCostCeilingUsd={100}
            onUpdateDailyCostCeiling={() => {}}
            isAdmin={false}
          />
        );
      });
    });
  });

  // Backend contract: `_check_daily_cost_ceiling` (agent_status.py) raises a
  // WARNING alert and fires an optional BUDGET_EXCEEDED notification when daily
  // spend exceeds the ceiling — it does NOT pause autonomous tasks or stop
  // dispatch. The active-ceiling help copy must mirror that alert-only behavior
  // and must not promise a pause that the pipeline never performs.
  test('renders_daily_cost_ceiling_as_alert_only', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <SettingsNotificationsSection
            webhookUrl=""
            onUpdateWebhookUrl={() => {}}
            enabledEvents={[]}
            onUpdateEnabledEvents={() => {}}
            throttleSeconds={0}
            onUpdateThrottleSeconds={() => {}}
            dailyCostCeilingUsd={50}
            onUpdateDailyCostCeiling={() => {}}
            isAdmin={true}
          />
        );
      });
    });

    const copy = host?.textContent ?? '';
    // Truthful today: alert + optional notification, work continues.
    expect(copy).toContain('warning alert');
    expect(copy).toContain('Autonomous tasks keep running');
    // Must not promise enforcement the backend does not perform.
    expect(copy.toLowerCase()).not.toContain('pause autonomous');
  });

  test('interactive controls meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <SettingsNotificationsSection
            webhookUrl="https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX"
            onUpdateWebhookUrl={() => {}}
            enabledEvents={ALL_EVENTS}
            onUpdateEnabledEvents={() => {}}
            throttleSeconds={300}
            onUpdateThrottleSeconds={() => {}}
            dailyCostCeilingUsd={50}
            onUpdateDailyCostCeiling={() => {}}
            isAdmin={true}
          />
        );
      });
    }, { selector: 'button, a[href]' });
  });
});
