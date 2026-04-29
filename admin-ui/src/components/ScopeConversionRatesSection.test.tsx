import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ScopeConversionRatesSection } from './ScopeConversionRatesSection';

type ScopeRate = {
  created: number;
  merged: number;
  merged_cross_module?: number;
  failed_scope_overflow?: number;
  calibrated_in?: number;
  calibrated_out?: number;
  avg_expansion_ratio_merged?: number;
  avg_expansion_ratio_failed?: number;
  failure_stages?: Record<string, number>;
};

function makeRate(overrides: Partial<ScopeRate> = {}): ScopeRate {
  return { created: 5, merged: 3, ...overrides };
}

describe('ScopeConversionRatesSection', () => {
  describe('null/loading states', () => {
    it('returns null when loading=true', () => {
      const { container } = render(
        <ScopeConversionRatesSection scopeRates={{ small: makeRate() }} prevScopeRates={null} loading={true} />
      );
      expect(container.firstChild).toBeNull();
    });

    it('returns null when scopeRates is null', () => {
      const { container } = render(
        <ScopeConversionRatesSection scopeRates={null} prevScopeRates={null} loading={false} />
      );
      expect(container.firstChild).toBeNull();
    });

    it('returns null when scopeRates is undefined', () => {
      const { container } = render(
        <ScopeConversionRatesSection scopeRates={undefined} prevScopeRates={null} loading={false} />
      );
      expect(container.firstChild).toBeNull();
    });

    it('returns null when scopeRates is empty object', () => {
      const { container } = render(
        <ScopeConversionRatesSection scopeRates={{}} prevScopeRates={null} loading={false} />
      );
      expect(container.firstChild).toBeNull();
    });
  });

  describe('scope ordering and presence', () => {
    it('renders scopes in small/medium/large order regardless of input key order', () => {
      render(
        <ScopeConversionRatesSection
          scopeRates={{
            large: makeRate({ created: 2, merged: 1 }),
            small: makeRate({ created: 10, merged: 7 }),
            medium: makeRate({ created: 5, merged: 3 }),
          }}
          prevScopeRates={null}
          loading={false}
        />
      );
      const allLabels = screen.getAllByText(/^(small|medium|large)$/);
      expect(allLabels).toHaveLength(3);
      expect(allLabels[0].textContent).toBe('small');
      expect(allLabels[1].textContent).toBe('medium');
      expect(allLabels[2].textContent).toBe('large');
    });

    it('skips scopes absent from scopeRates', () => {
      render(
        <ScopeConversionRatesSection
          scopeRates={{ medium: makeRate({ created: 4, merged: 2 }) }}
          prevScopeRates={null}
          loading={false}
        />
      );
      expect(screen.getByText('medium')).toBeInTheDocument();
      expect(screen.queryByText('small')).toBeNull();
      expect(screen.queryByText('large')).toBeNull();
    });
  });

  describe('merge rate computation', () => {
    it('shows rate as merged/created rounded to percent', () => {
      render(
        <ScopeConversionRatesSection
          scopeRates={{ small: makeRate({ created: 5, merged: 3 }) }}
          prevScopeRates={null}
          loading={false}
        />
      );
      expect(screen.getByText('60%')).toBeInTheDocument();
      expect(screen.getByText('3/5 merged')).toBeInTheDocument();
    });

    it('shows -- when created is 0', () => {
      render(
        <ScopeConversionRatesSection
          scopeRates={{ small: makeRate({ created: 0, merged: 0 }) }}
          prevScopeRates={null}
          loading={false}
        />
      );
      expect(screen.getByText('--')).toBeInTheDocument();
    });
  });

  describe('rate color tiers', () => {
    it('applies text-emerald-400 when rate >= 0.6', () => {
      const { container } = render(
        <ScopeConversionRatesSection
          scopeRates={{ small: makeRate({ created: 10, merged: 6 }) }}
          prevScopeRates={null}
          loading={false}
        />
      );
      const rateEl = container.querySelector('.text-emerald-400.font-bold');
      expect(rateEl).not.toBeNull();
      expect(rateEl?.textContent).toBe('60%');
    });

    it('applies text-amber-400 when 0.3 <= rate < 0.6', () => {
      const { container } = render(
        <ScopeConversionRatesSection
          scopeRates={{ small: makeRate({ created: 10, merged: 4 }) }}
          prevScopeRates={null}
          loading={false}
        />
      );
      const rateEl = container.querySelector('.text-amber-400.font-bold');
      expect(rateEl).not.toBeNull();
      expect(rateEl?.textContent).toBe('40%');
    });

    it('applies text-red-400 when rate < 0.3', () => {
      const { container } = render(
        <ScopeConversionRatesSection
          scopeRates={{ small: makeRate({ created: 10, merged: 2 }) }}
          prevScopeRates={null}
          loading={false}
        />
      );
      const rateEl = container.querySelector('.text-red-400.font-bold');
      expect(rateEl).not.toBeNull();
      expect(rateEl?.textContent).toBe('20%');
    });
  });

  describe('trend icon resolution', () => {
    it('shows improving icon (emerald svg) when delta > +0.05', () => {
      const { container } = render(
        <ScopeConversionRatesSection
          scopeRates={{ small: makeRate({ created: 10, merged: 8 }) }}
          prevScopeRates={{ small: makeRate({ created: 10, merged: 7 }) }}
          loading={false}
        />
      );
      expect(container.querySelector('svg.text-emerald-400')).not.toBeNull();
    });

    it('shows declining icon (red svg) when delta < -0.05', () => {
      const { container } = render(
        <ScopeConversionRatesSection
          scopeRates={{ small: makeRate({ created: 10, merged: 7 }) }}
          prevScopeRates={{ small: makeRate({ created: 10, merged: 8 }) }}
          loading={false}
        />
      );
      expect(container.querySelector('svg.text-red-400')).not.toBeNull();
    });

    it('shows stable icon (neutral svg) when |delta| <= 0.05', () => {
      const { container } = render(
        <ScopeConversionRatesSection
          scopeRates={{ small: makeRate({ created: 10, merged: 7 }) }}
          prevScopeRates={{ small: makeRate({ created: 10, merged: 7 }) }}
          loading={false}
        />
      );
      expect(container.querySelector('svg.text-neutral-500')).not.toBeNull();
    });

    it('shows no trend icon when current rate is null (created=0)', () => {
      const { container } = render(
        <ScopeConversionRatesSection
          scopeRates={{ small: makeRate({ created: 0, merged: 0 }) }}
          prevScopeRates={{ small: makeRate({ created: 10, merged: 7 }) }}
          loading={false}
        />
      );
      expect(container.querySelectorAll('svg')).toHaveLength(0);
    });

    it('shows no trend icon when prevScopeRates is null', () => {
      const { container } = render(
        <ScopeConversionRatesSection
          scopeRates={{ small: makeRate({ created: 10, merged: 7 }) }}
          prevScopeRates={null}
          loading={false}
        />
      );
      expect(container.querySelectorAll('svg')).toHaveLength(0);
    });

    it('shows no trend icon when prev scope has created=0', () => {
      const { container } = render(
        <ScopeConversionRatesSection
          scopeRates={{ small: makeRate({ created: 10, merged: 7 }) }}
          prevScopeRates={{ small: makeRate({ created: 0, merged: 0 }) }}
          loading={false}
        />
      );
      expect(container.querySelectorAll('svg')).toHaveLength(0);
    });
  });

  describe('failure_stages display', () => {
    it('renders top 2 stages by count and skips count=0', () => {
      render(
        <ScopeConversionRatesSection
          scopeRates={{
            small: makeRate({
              failure_stages: { test: 5, lint: 3, build: 0, review: 1 },
            }),
          }}
          prevScopeRates={null}
          loading={false}
        />
      );
      expect(screen.getByText('test 5')).toBeInTheDocument();
      expect(screen.getByText('lint 3')).toBeInTheDocument();
      expect(screen.queryByText(/build/)).toBeNull();
      expect(screen.queryByText(/review \d/)).toBeNull();
    });

    it('breaks ties by STAGE_ORDER index (provision before test before lint)', () => {
      render(
        <ScopeConversionRatesSection
          scopeRates={{
            small: makeRate({
              failure_stages: { test: 5, provision: 5, lint: 5 },
            }),
          }}
          prevScopeRates={null}
          loading={false}
        />
      );
      // STAGE_ORDER: provision=0, test=2, lint=3 — top 2 are provision and test
      expect(screen.getByText('provision 5')).toBeInTheDocument();
      expect(screen.getByText('test 5')).toBeInTheDocument();
      expect(screen.queryByText('lint 5')).toBeNull();
    });

    it('renders no stage badges when failure_stages is absent', () => {
      render(
        <ScopeConversionRatesSection
          scopeRates={{ small: makeRate() }}
          prevScopeRates={null}
          loading={false}
        />
      );
      // No stage badge text like "test 3"
      expect(screen.queryByText(/\w+ \d+/)).toBeNull();
    });
  });

  describe('expansion ratio warning', () => {
    it('applies text-amber-500 when avg_expansion_ratio_failed > avg_expansion_ratio_merged + 0.5', () => {
      const { container } = render(
        <ScopeConversionRatesSection
          scopeRates={{
            small: makeRate({
              avg_expansion_ratio_merged: 1.0,
              avg_expansion_ratio_failed: 2.0,
            }),
          }}
          prevScopeRates={null}
          loading={false}
        />
      );
      expect(container.querySelector('.text-amber-500')).not.toBeNull();
    });

    it('uses neutral color when within threshold', () => {
      const { container } = render(
        <ScopeConversionRatesSection
          scopeRates={{
            small: makeRate({
              avg_expansion_ratio_merged: 1.0,
              avg_expansion_ratio_failed: 1.4,
            }),
          }}
          prevScopeRates={null}
          loading={false}
        />
      );
      expect(container.querySelector('.text-amber-500')).toBeNull();
      expect(screen.getByText(/exp .+x/)).toBeInTheDocument();
    });

    it('does not render expansion row when neither ratio is present', () => {
      render(
        <ScopeConversionRatesSection
          scopeRates={{ small: makeRate() }}
          prevScopeRates={null}
          loading={false}
        />
      );
      expect(screen.queryByText(/^exp /)).toBeNull();
    });
  });

  describe('conditional stat rows', () => {
    it('renders merged_cross_module line only when > 0', () => {
      const { rerender } = render(
        <ScopeConversionRatesSection
          scopeRates={{ small: makeRate({ merged_cross_module: 2 }) }}
          prevScopeRates={null}
          loading={false}
        />
      );
      expect(screen.getByText(/cross-dir/)).toBeInTheDocument();

      rerender(
        <ScopeConversionRatesSection
          scopeRates={{ small: makeRate({ merged_cross_module: 0 }) }}
          prevScopeRates={null}
          loading={false}
        />
      );
      expect(screen.queryByText(/cross-dir/)).toBeNull();
    });

    it('renders failed_scope_overflow line only when > 0', () => {
      const { rerender } = render(
        <ScopeConversionRatesSection
          scopeRates={{ small: makeRate({ failed_scope_overflow: 3 }) }}
          prevScopeRates={null}
          loading={false}
        />
      );
      expect(screen.getByText(/scope overflow/)).toBeInTheDocument();

      rerender(
        <ScopeConversionRatesSection
          scopeRates={{ small: makeRate({ failed_scope_overflow: 0 }) }}
          prevScopeRates={null}
          loading={false}
        />
      );
      expect(screen.queryByText(/scope overflow/)).toBeNull();
    });

    it('renders recalibrated line only when calibrated_in + calibrated_out > 0', () => {
      const { rerender } = render(
        <ScopeConversionRatesSection
          scopeRates={{ small: makeRate({ calibrated_in: 2, calibrated_out: 1 }) }}
          prevScopeRates={null}
          loading={false}
        />
      );
      expect(screen.getByText(/recalibrated/)).toBeInTheDocument();

      rerender(
        <ScopeConversionRatesSection
          scopeRates={{ small: makeRate({ calibrated_in: 0, calibrated_out: 0 }) }}
          prevScopeRates={null}
          loading={false}
        />
      );
      expect(screen.queryByText(/recalibrated/)).toBeNull();
    });
  });
});
