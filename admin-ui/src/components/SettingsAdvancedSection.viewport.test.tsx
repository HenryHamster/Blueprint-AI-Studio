import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { SettingsAdvancedSection } from './SettingsAdvancedSection';
import { expectNoOverflowAt } from '../test/viewport';

describe('SettingsAdvancedSection — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  test('renders without horizontal overflow at 375px with empty health check urls', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <SettingsAdvancedSection
            healthCheckUrlsInput=""
            onHealthCheckUrlsChange={() => {}}
            onUpdateHealthCheckUrls={() => {}}
          />
        );
      });
    });
  });

  test('renders without horizontal overflow at 375px with populated health check urls', async () => {
    const urls = [
      'https://api.example.com/health',
      'https://backend.example.com/status',
      'https://worker.example.com/ping',
      'https://db.example.com/ready',
      'https://cache.example.com/health',
    ].join('\n');

    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <SettingsAdvancedSection
            healthCheckUrlsInput={urls}
            onHealthCheckUrlsChange={() => {}}
            onUpdateHealthCheckUrls={() => {}}
          />
        );
      });
    });
  });
});
