import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { ScopeConversionRatesSection } from './ScopeConversionRatesSection';
import { expectNoOverflowAt } from '../test/viewport';

const scopeRates = {
  small: {
    created: 142,
    merged: 98,
    merged_cross_module: 12,
    failed_scope_overflow: 3,
    calibrated_in: 4,
    calibrated_out: 2,
    avg_expansion_ratio_merged: 1.4,
    avg_expansion_ratio_failed: 2.6,
    failure_stages: { test: 8, lint: 5, build: 3 },
  },
  medium: {
    created: 76,
    merged: 31,
    merged_cross_module: 9,
    failed_scope_overflow: 6,
    calibrated_in: 3,
    calibrated_out: 5,
    avg_expansion_ratio_merged: 1.7,
    avg_expansion_ratio_failed: 3.1,
    failure_stages: { test: 12, build: 7, review: 4 },
  },
  large: {
    created: 18,
    merged: 4,
    merged_cross_module: 2,
    failed_scope_overflow: 4,
    calibrated_in: 1,
    calibrated_out: 1,
    avg_expansion_ratio_merged: 2.2,
    avg_expansion_ratio_failed: 4.1,
    failure_stages: { test: 5, lint: 2, pr_creation: 1 },
  },
};

const prevScopeRates = {
  small: { created: 100, merged: 60 },
  medium: { created: 50, merged: 25 },
  large: { created: 12, merged: 4 },
};

describe('ScopeConversionRatesSection — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => {
        r.unmount();
      });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ScopeConversionRatesSection
            scopeRates={scopeRates}
            prevScopeRates={prevScopeRates}
            loading={false}
          />,
        );
      });
    });
  });

  test('renders without horizontal overflow at 768px', async () => {
    await expectNoOverflowAt(768, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ScopeConversionRatesSection
            scopeRates={scopeRates}
            prevScopeRates={prevScopeRates}
            loading={false}
          />,
        );
      });
    });
  });
});
