import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { CriterionVerdictBadge } from './CriterionVerdictBadge';
import { expectNoOverflowAt } from '../test/viewport';

// CriterionVerdictBadge only uses KnowledgeDocument as a type — no runtime
// imports from '../api' — so no API mock is needed.

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) {
    act(() => root!.unmount());
    root = null;
  }
  if (container) {
    container.remove();
    container = null;
  }
});

const BASE_DOC = {
  source_kind: 'compute_experiment' as const,
};

describe('CriterionVerdictBadge — viewport at 400px', () => {
  test('renders criterion MET badge without overflow', async () => {
    await expectNoOverflowAt(400, () => {
      container = document.createElement('div');
      document.body.appendChild(container);
      root = createRoot(container);
      act(() => {
        root!.render(
          <CriterionVerdictBadge
            doc={{ ...BASE_DOC, criterion_met: true, criterion_unmeasured_reason: null }}
          />,
        );
      });
    });
  });

  test('renders criterion NOT MET badge without overflow', async () => {
    await expectNoOverflowAt(400, () => {
      container = document.createElement('div');
      document.body.appendChild(container);
      root = createRoot(container);
      act(() => {
        root!.render(
          <CriterionVerdictBadge
            doc={{ ...BASE_DOC, criterion_met: false, criterion_unmeasured_reason: null }}
          />,
        );
      });
    });
  });

  test('renders UNMEASURED badge with no_result_metrics reason without overflow', async () => {
    await expectNoOverflowAt(400, () => {
      container = document.createElement('div');
      document.body.appendChild(container);
      root = createRoot(container);
      act(() => {
        root!.render(
          <CriterionVerdictBadge
            doc={{
              ...BASE_DOC,
              criterion_met: null,
              criterion_unmeasured_reason: 'no_result_metrics',
            }}
          />,
        );
      });
    });
  });

  test('renders UNMEASURED badge with metric_key_absent reason without overflow', async () => {
    await expectNoOverflowAt(400, () => {
      container = document.createElement('div');
      document.body.appendChild(container);
      root = createRoot(container);
      act(() => {
        root!.render(
          <CriterionVerdictBadge
            doc={{
              ...BASE_DOC,
              criterion_met: null,
              criterion_unmeasured_reason: 'metric_key_absent',
            }}
          />,
        );
      });
    });
  });
});
